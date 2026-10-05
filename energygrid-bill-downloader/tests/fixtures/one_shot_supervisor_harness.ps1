[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('ExtractSource', 'ParseCompile', 'Native', 'Functions', 'Observer', 'EndToEnd', 'CrashOwner')]
    [string]$Phase,
    [string]$SupervisorPath,
    [string]$ReceiptPath,
    [string]$DataRoot,
    [string]$PythonExe,
    [string]$PythonwExe,
    [string]$Case,
    [string]$ReadyPath,
    [switch]$ResumeChild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$FixtureRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $FixtureRoot '..\..\..'))
if ([string]::IsNullOrWhiteSpace($SupervisorPath)) {
    $SupervisorPath = Join-Path $RepositoryRoot 'scripts\energygrid_one_shot_supervisor.ps1'
}
$SupervisorPath = [IO.Path]::GetFullPath($SupervisorPath)
$FunctionsPath = Join-Path $FixtureRoot 'one_shot_supervisor_functions.ps1'
$SupportPath = Join-Path $FixtureRoot 'one_shot_supervisor_support.cs'
$PythonModulePath = Join-Path $FixtureRoot 'energygrid_bill_downloader.py'
$LauncherPath = Join-Path $FixtureRoot 'supervisor_launcher\launcher.ps1'
$LauncherLibraryPath = Join-Path $FixtureRoot 'supervisor_launcher\launcher_lib.ps1'
$TestPath = Join-Path $RepositoryRoot 'energygrid-bill-downloader\tests\test_one_shot_supervisor.py'
$script:NativePowerShell = Join-Path $PSHOME 'powershell.exe'

function Assert-EgHarness {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw ('EG_HARNESS:' + $Message) }
}

function Get-EgAst {
    param([string]$Path)
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $Path, [ref]$tokens, [ref]$errors)
    Assert-EgHarness (@($errors).Count -eq 0) ('parse_failed:' + $Path)
    return $ast
}

function Get-EgAssignment {
    param($Ast, [string]$Left)
    $found = @($Ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $node.Left.Extent.Text -ceq $Left
    }, $false))
    Assert-EgHarness ($found.Count -eq 1) ('assignment_count:' + $Left)
    return $found[0]
}

function Get-EgFunction {
    param($Ast, [string]$Name)
    $found = @($Ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -ceq $Name
    }, $false))
    Assert-EgHarness ($found.Count -eq 1) ('function_count:' + $Name)
    return $found[0]
}

function Initialize-EgNativeSource {
    $ast = Get-EgAst -Path $SupervisorPath
    $assignment = Get-EgAssignment -Ast $ast -Left '$script:EgNativeSource'
    $native = $assignment.Right.Expression.Value
    Assert-EgHarness (-not [string]::IsNullOrWhiteSpace($native)) 'native_source_empty'
    Add-Type -TypeDefinition $native -ReferencedAssemblies @('System.Management.dll') -ErrorAction Stop
    Assert-EgHarness ([EnergyGridOneShotSupervisorNative]::VerifyX64StructureSizes()) 'native_layout_failed'
    return $ast
}

function Initialize-EgSupportSource {
    Add-Type -Path $SupportPath -ErrorAction Stop
}

function Assert-EgReceiptHelpers {
    param([string]$Path)
    Assert-EgHarness (-not [string]::IsNullOrWhiteSpace($Path)) 'custody_receipt_required'
    $receipt = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    Assert-EgHarness ($receipt.schema -ceq 'energygrid.supervisor-harness-custody.v1') 'custody_schema_invalid'
    $expected = @{}
    foreach ($entry in $receipt.helpers) { $expected[[string]$entry.path] = $entry }
    foreach ($path in @($PSCommandPath, $FunctionsPath, $SupportPath, $PythonModulePath, $LauncherPath, $LauncherLibraryPath)) {
        $canonical = [IO.Path]::GetFullPath($path)
        $relative = $canonical.Substring($RepositoryRoot.Length).TrimStart('\','/').Replace('\','/')
        Assert-EgHarness ($expected.ContainsKey($relative)) ('custody_path_missing:' + $relative)
        Assert-EgHarness (Test-Path -LiteralPath $canonical -PathType Leaf) ('helper_missing:' + $relative)
        $item = Get-Item -LiteralPath $canonical
        $hash = (Get-FileHash -LiteralPath $canonical -Algorithm SHA256).Hash.ToLowerInvariant()
        Assert-EgHarness ([int64]$item.Length -eq [int64]$expected[$relative].size) ('helper_size_changed:' + $relative)
        Assert-EgHarness ($hash -ceq ([string]$expected[$relative].sha256).ToLowerInvariant()) ('helper_hash_changed:' + $relative)
    }
    Assert-EgHarness (Test-Path -LiteralPath $TestPath -PathType Leaf) 'test_owner_missing'
    $testIdentity = Get-Item -LiteralPath $TestPath
    Assert-EgHarness ([int64]$testIdentity.Length -eq [int64]$receipt.test_identity.size) 'test_owner_size_changed'
    Assert-EgHarness (
        (Get-FileHash -LiteralPath $TestPath -Algorithm SHA256).Hash.ToLowerInvariant() -ceq
        ([string]$receipt.test_identity.sha256).ToLowerInvariant()
    ) 'test_owner_hash_changed'
    Assert-EgHarness (Test-Path -LiteralPath $SupervisorPath -PathType Leaf) 'production_supervisor_missing'
    $supervisorIdentity = Get-Item -LiteralPath $SupervisorPath
    Assert-EgHarness ([int64]$supervisorIdentity.Length -eq [int64]$receipt.supervisor_identity.size) 'production_supervisor_size_changed'
    Assert-EgHarness (
        (Get-FileHash -LiteralPath $SupervisorPath -Algorithm SHA256).Hash.ToLowerInvariant() -ceq
        ([string]$receipt.supervisor_identity.sha256).ToLowerInvariant()
    ) 'production_supervisor_hash_changed'
}

function Export-EgSourceEvidence {
    $productionAst = Get-EgAst -Path $SupervisorPath
    $fixtureAst = Get-EgAst -Path $FunctionsPath
    $names = @(
        'Stop-EgSupervisor',
        'Test-EgUnsafeText',
        'ConvertTo-EgNativeCommandLine',
        'ConvertTo-EgUtf8JsonBytes',
        'Close-EgHandle',
        'Write-EgReservedIntent',
        'Get-EgOutcomeObject',
        'Write-EgOutcome',
        'Get-EgCanonicalApplicationCommandLine',
        'Test-EgApplicationChild',
        'Get-EgAccounting',
        'Invoke-EgTerminateJob',
        'Get-EgDeadlineTicks',
        'Get-EgDurabilityMilliseconds',
        'Test-EgDeadlineReached',
        'Wait-EgReap',
        'Wait-EgDescendantGrace',
        'Get-EgStartVerdict',
        'Get-EgExitCode'
    )
    $definitions = @{}
    foreach ($name in $names) {
        $definitions[$name] = @{
            production = (Get-EgFunction -Ast $productionAst -Name $name).Extent.Text
            fixture = (Get-EgFunction -Ast $fixtureAst -Name $name).Extent.Text
        }
    }
    $assignments = @{}
    foreach ($left in @(
        '$script:EgState',
        '$script:EgObserverMetadataTimeoutMilliseconds',
        '$script:EgObserverProcessGoneErrorCode'
    )) {
        $assignments[$left] = @{
            production = (Get-EgAssignment -Ast $productionAst -Left $left).Extent.Text
            fixture = (Get-EgAssignment -Ast $fixtureAst -Left $left).Extent.Text
        }
    }
    $variantNames = @(
        'Test-EgN4bApplicationChild',
        'Test-EgN5ApplicationChild',
        'Test-EgN7ApplicationChild',
        'Test-EgN10ApplicationChild',
        'Test-EgN11ApplicationChild',
        'Test-EgN13ApplicationChild'
    )
    $variants = @{}
    foreach ($name in $variantNames) {
        $variants[$name] = (Get-EgFunction -Ast $fixtureAst -Name $name).Extent.Text
    }
    $outcomeVariants = @{}
    $outcomeVariants['Write-EgOutcomeWithTimeoutControl'] =
        (Get-EgFunction -Ast $fixtureAst -Name 'Write-EgOutcomeWithTimeoutControl').Extent.Text
    $nativeAssignment = Get-EgAssignment -Ast $productionAst -Left '$script:EgNativeSource'
    $nativeSource = $nativeAssignment.Right.Expression.Value
    $nativeMethodStart = $nativeSource.IndexOf(
        'public static ProcessMetadataResult QueryProcessMetadata(', [StringComparison]::Ordinal)
    $nativeMethodEnd = $nativeSource.IndexOf(
        'public static bool VerifyX64StructureSizes()', $nativeMethodStart, [StringComparison]::Ordinal)
    Assert-EgHarness ($nativeMethodStart -ge 0 -and $nativeMethodEnd -gt $nativeMethodStart) `
        'n7_production_native_method_missing'
    $nativeMethodLineStart = $nativeSource.LastIndexOf("`n", $nativeMethodStart)
    if ($nativeMethodLineStart -lt 0) { $nativeMethodLineStart = 0 } else { $nativeMethodLineStart++ }
    $nativeMethod = $nativeSource.Substring(
        $nativeMethodLineStart, $nativeMethodEnd - $nativeMethodLineStart).Trim()
    [ordered]@{
        definitions = $definitions
        assignments = $assignments
        variants = $variants
        outcome_variants = $outcomeVariants
        n7_native_method = $nativeMethod
    } |
        ConvertTo-Json -Depth 8 -Compress
}

function Test-EgParseAndCompile {
    foreach ($path in @(
        $SupervisorPath, $PSCommandPath, $FunctionsPath, $LauncherPath, $LauncherLibraryPath
    )) {
        [void](Get-EgAst -Path $path)
    }
    $nativeAst = Initialize-EgNativeSource
    Initialize-EgSupportSource
    Write-Output 'parse_compile=PASS'
    Write-Output ('production_native_type=' + [EnergyGridOneShotSupervisorNative].FullName)
    Write-Output 'committed_support=PASS'
}

function Invoke-EgNativeCases {
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RootPath = $DataRoot
$script:PythonExe = [IO.Path]::GetFullPath($PythonExe)
if (-not (Test-Path -LiteralPath $script:PythonExe -PathType Leaf)) { throw 'python_exe_missing' }
. $LauncherLibraryPath
Set-EgFixturePythonEnvironment -ModulePath $PythonModulePath -ExpectedSha256 (Get-FileHash -LiteralPath $PythonModulePath -Algorithm SHA256).Hash
. $FunctionsPath

function Assert-Native {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Close-Native {
    param([IntPtr]$Handle)
    if ($Handle -ne [IntPtr]::Zero) {
        [void][EnergyGridOneShotSupervisorNative]::CloseHandleChecked($Handle)
    }
}

function New-EgCaseConfig {
    param([string]$Name, [hashtable]$Values = @{})
    $document = [ordered]@{
        module_sha256 = (Get-FileHash -LiteralPath $PythonModulePath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    foreach ($key in $Values.Keys) { $document[$key] = $Values[$key] }
    $path = Join-Path $RootPath ($Name + '.json')
    [IO.File]::WriteAllText(
        $path, ($document | ConvertTo-Json -Depth 8 -Compress), (New-Object System.Text.UTF8Encoding($false)))
    return $path
}

function New-ContainedPython {
    param(
        [IntPtr]$JobHandle,
        [ValidateSet('run', 'saturate', 'handle-canary')][string]$Operation = 'run',
        [string]$ConfigPath,
        [long]$CanaryHandle = 0,
        [uint64]$CanaryVolume = 0,
        [uint64]$CanaryFileIndex = 0
    )
    $pipes = $null
    $attributes = $null
    try {
        $pipes = [EnergyGridOneShotSupervisorNative]::CreatePipes()
        $attributes = New-Object EnergyGridOneShotSupervisorNative+AttributeResources(
            $JobHandle, $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite,
            $pipes.LauncherStderrWrite)
        if ($Operation -ceq 'handle-canary') {
            $items = @($script:PythonExe, '-B', '-m', 'energygrid_bill_downloader',
                'handle-canary', '--handle', [string]$CanaryHandle,
                '--volume', [string]$CanaryVolume, '--file-index', [string]$CanaryFileIndex)
        }
        elseif ($Operation -ceq 'saturate') {
            $items = @($script:PythonExe, '-B', '-m', 'energygrid_bill_downloader', 'saturate')
        }
        else {
            $items = @($script:PythonExe, '-B', '-m', 'energygrid_bill_downloader',
                'run', '--config', $ConfigPath)
        }
        $commandLine = ConvertTo-EgNativeCommandLine -Argument $items
        $builder = New-Object System.Text.StringBuilder($commandLine)
        $created = [EnergyGridOneShotSupervisorNative]::CreateContainedProcess(
            $script:PythonExe, $builder, $RootPath, $attributes.AttributeList,
            $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite, $pipes.LauncherStderrWrite)
        if (-not $created.Succeeded) { throw ('native_create_failed:' + $created.ErrorCode) }
        $attributes.Dispose()
        $attributes = $null
        Close-Native -Handle $pipes.LauncherStdinRead
        $pipes.LauncherStdinRead = [IntPtr]::Zero
        Close-Native -Handle $pipes.LauncherStdoutWrite
        $pipes.LauncherStdoutWrite = [IntPtr]::Zero
        Close-Native -Handle $pipes.LauncherStderrWrite
        $pipes.LauncherStderrWrite = [IntPtr]::Zero
        $stdoutDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStdoutRead)
        $pipes.SupervisorStdoutRead = [IntPtr]::Zero
        $stderrDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStderrRead)
        $pipes.SupervisorStderrRead = [IntPtr]::Zero
        Close-Native -Handle $pipes.SupervisorStdinWrite
        $pipes.SupervisorStdinWrite = [IntPtr]::Zero
        return [pscustomobject]@{
            ProcessHandle = $created.ProcessInfo.hProcess
            ThreadHandle = $created.ProcessInfo.hThread
            ProcessId = $created.ProcessInfo.dwProcessId
            StdoutDrain = $stdoutDrain
            StderrDrain = $stderrDrain
        }
    }
    catch {
        if ($null -ne $attributes) { $attributes.Dispose() }
        if ($null -ne $pipes) {
            Close-Native -Handle $pipes.LauncherStdinRead
            Close-Native -Handle $pipes.SupervisorStdinWrite
            Close-Native -Handle $pipes.SupervisorStdoutRead
            Close-Native -Handle $pipes.LauncherStdoutWrite
            Close-Native -Handle $pipes.SupervisorStderrRead
            Close-Native -Handle $pipes.LauncherStderrWrite
        }
        throw
    }
}

function Wait-JobZero {
    param([IntPtr]$JobHandle, [int]$Seconds = 10)
    $deadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]$Seconds * [int64][System.Diagnostics.Stopwatch]::Frequency)
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $deadline) {
        $accounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($JobHandle)
        Assert-Native $accounting.Succeeded ('accounting_failed:' + $accounting.ErrorCode)
        if ($accounting.ActiveProcesses -eq 0) { return $accounting }
        Start-Sleep -Milliseconds 100
    }
    $final = [EnergyGridOneShotSupervisorNative]::GetAccounting($JobHandle)
    Assert-Native $final.Succeeded ('final_accounting_failed:' + $final.ErrorCode)
    Assert-Native ($final.ActiveProcesses -eq 0) 'reap_timeout'
    return $final
}

function Close-Contained {
    param($Process)
    if ($null -eq $Process) { return }
    if ($null -ne $Process.StdoutDrain) { [void]$Process.StdoutDrain.Join(10000) }
    if ($null -ne $Process.StderrDrain) { [void]$Process.StderrDrain.Join(10000) }
    Close-Native -Handle $Process.ThreadHandle
    Close-Native -Handle $Process.ProcessHandle
}

function Cleanup-Job {
    param([IntPtr]$JobHandle, $Process)
    try {
        if ($JobHandle -ne [IntPtr]::Zero) {
            [void][EnergyGridOneShotSupervisorNative]::TerminateJob(
                $JobHandle, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        }
    }
    catch { }
    try { Close-Contained -Process $Process } catch { }
    Close-Native -Handle $JobHandle
}

function Future-Deadline {
    param([int]$Milliseconds = 30000)
    return [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]$Milliseconds * [int64][System.Diagnostics.Stopwatch]::Frequency / 1000))
}

Write-Output 'native_case=bounded_durability_results'
$durabilityPath = Join-Path $RootPath 'durability.bin'
$successStream = $null
$failureStream = $null
$delayedStream = $null
try {
    $successStream = New-Object System.IO.FileStream(
        $durabilityPath, [IO.FileMode]::Create, [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None, 4096, [IO.FileOptions]::DeleteOnClose)
    $success = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
        $successStream, [byte[]](1, 2, 3), 5000)
    Assert-Native ($success.Succeeded -and -not $success.TimedOut) 'durability_success_result_invalid'
    $successStream.Dispose()
    $successStream = $null

    $failureStream = New-Object System.IO.FileStream(
        $durabilityPath, [IO.FileMode]::Create, [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None, 4096, [IO.FileOptions]::DeleteOnClose)
    $failureStream.Dispose()
    $failureStream = $null
    $failure = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
        $failureStream, [byte[]](1, 2, 3), 5000)
    Assert-Native ((-not $failure.Succeeded) -and (-not $failure.TimedOut)) 'durability_failure_result_invalid'

    $delayedPath = Join-Path $RootPath 'delayed-durability.bin'
    $delayedStream = New-Object EnergyGridDelayedFlushStreamForTest($delayedPath, 5250)
    $delayed = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
        $delayedStream, [byte[]](1, 2, 3), 5000)
    Assert-Native ((-not $delayed.Succeeded) -and $delayed.TimedOut) 'durability_timeout_result_invalid'
    Start-Sleep -Milliseconds 6000
    Assert-Native ((-not $delayed.Succeeded) -and $delayed.TimedOut) 'durability_timeout_reopened'
    Write-Output ('native_durability_timeout=' + [string]$delayed.TimedOut)
    Write-Output ('native_durability_succeeded_after_wait=' + [string]$delayed.Succeeded)
}
finally {
    if ($null -ne $successStream) { $successStream.Dispose() }
    if ($null -ne $failureStream) { $failureStream.Dispose() }
    if ($null -ne $delayedStream) { $delayedStream.Dispose() }
}

Write-Output 'native_case=job_policy_before_and_after_activity'
$job = [IntPtr]::Zero
$process = $null
try {
    $job = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($job)
    $before = [EnergyGridOneShotSupervisorNative]::VerifyJobLimits($job)
    Assert-Native ($before.Succeeded -and $before.Matches) 'job_policy_before_create_failed'
    $config = New-EgCaseConfig -Name 'native-job-policy' -Values @{ run_delay_seconds = 60 }
    $process = New-ContainedPython -JobHandle $job -ConfigPath $config
    $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership(
        $process.ProcessHandle, $job)
    Assert-Native ($membership.Succeeded -and $membership.IsMember) 'creation_membership_failed'
    $afterCreate = [EnergyGridOneShotSupervisorNative]::VerifyJobLimits($job)
    Assert-Native ($afterCreate.Succeeded -and $afterCreate.Matches) 'job_policy_after_create_failed'
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    $intent = [EnergyGridOneShotSupervisorNative]::CommitIntent()
    Assert-Native $intent 'native_intent_commit_failed'
    $resume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $process.ThreadHandle, (Future-Deadline))
    Assert-Native ($resume.Attempted -and $resume.Accepted -and $resume.ReturnValue -eq 1) 'native_resume_failed'
    Start-Sleep -Milliseconds 500
    $afterActivity = [EnergyGridOneShotSupervisorNative]::VerifyJobLimits($job)
    Assert-Native ($afterActivity.Succeeded -and $afterActivity.Matches) 'job_policy_after_activity_failed'
    $drainDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency)
    while (($process.StdoutDrain.Bytes -eq 0 -or $process.StderrDrain.Bytes -eq 0) -and
        [System.Diagnostics.Stopwatch]::GetTimestamp() -lt $drainDeadline) {
        Start-Sleep -Milliseconds 50
    }
    $terminated = [EnergyGridOneShotSupervisorNative]::TerminateJob(
        $job, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
    Assert-Native $terminated.Success ('native_termination_failed:' + $terminated.ErrorCode)
    $wait = [EnergyGridOneShotSupervisorNative]::WaitProcess($process.ProcessHandle, 10000)
    Assert-Native ($wait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) 'native_termination_wait_failed'
    $final = Wait-JobZero -JobHandle $job
    Assert-Native ($final.ActiveProcesses -eq 0) 'active_processes_not_zero'
    $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($process.ProcessHandle)
    Assert-Native ($live.Succeeded -and $live.ExitCode -eq [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE) 'termination_code_not_observed'
    [void]$process.StdoutDrain.Join(10000)
    [void]$process.StderrDrain.Join(10000)
    Assert-Native ($process.StdoutDrain.Completed -and $process.StderrDrain.Completed) 'native_drains_incomplete'
    Assert-Native ($process.StdoutDrain.Bytes -gt 0 -and $process.StderrDrain.Bytes -gt 0) 'native_drain_counts_empty'
}
finally {
    Cleanup-Job -JobHandle $job -Process $process
}

Write-Output 'native_case=wrong_active_flags_rejected'
$wrongJob = [IntPtr]::Zero
try {
    $wrongJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    $wrongInfo = New-Object EnergyGridOneShotSupervisorNative+JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    $wrongFlags = [uint32]([int64][EnergyGridOneShotSupervisorNative]::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE -bor 8)
    $wrongInfo.BasicLimitInformation.LimitFlags = $wrongFlags
    $wrongInfo.BasicLimitInformation.ActiveProcessLimit = 2
    $wrongLength = [uint32][Runtime.InteropServices.Marshal]::SizeOf(
        [type]'EnergyGridOneShotSupervisorNative+JOBOBJECT_EXTENDED_LIMIT_INFORMATION')
    $wrongSet = [EnergyGridOneShotSupervisorNative]::SetInformationJobObject(
        $wrongJob, [EnergyGridOneShotSupervisorNative]::JobObjectExtendedLimitInformation,
        [ref]$wrongInfo, $wrongLength)
    Assert-Native $wrongSet 'wrong_active_flags_setup_failed'
    $wrongReadback = [EnergyGridOneShotSupervisorNative]::VerifyJobLimits($wrongJob)
    Assert-Native ($wrongReadback.Succeeded -and -not $wrongReadback.Matches) 'wrong_active_flags_accepted'
}
finally {
    Close-Native -Handle $wrongJob
}

Write-Output 'native_case=unsupported_job_list_rejected_before_execution'
$invalidJob = [IntPtr]::Zero
$invalidPipes = $null
$invalidAttributes = $null
try {
    $invalidJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($invalidJob)
    $invalidPipes = [EnergyGridOneShotSupervisorNative]::CreatePipes()
    $invalidFailed = $false
    try {
        $invalidAttributes = New-Object EnergyGridOneShotSupervisorNative+AttributeResources(
            [IntPtr]::Zero, $invalidPipes.LauncherStdinRead,
            $invalidPipes.LauncherStdoutWrite, $invalidPipes.LauncherStderrWrite)
        $invalidConfig = New-EgCaseConfig -Name 'invalid-job-list' -Values @{ ready_path = (Join-Path $RootPath 'invalid-job-list-marker.txt'); run_delay_seconds = 30 }
        $invalidLine = ConvertTo-EgNativeCommandLine -Argument @($script:PythonExe, '-B', '-m', 'energygrid_bill_downloader', 'run', '--config', $invalidConfig)
        $invalidBuilder = New-Object System.Text.StringBuilder($invalidLine)
        $invalidCreation = [EnergyGridOneShotSupervisorNative]::CreateContainedProcess(
            $script:PythonExe, $invalidBuilder, $RootPath,
            $invalidAttributes.AttributeList, $invalidPipes.LauncherStdinRead,
            $invalidPipes.LauncherStdoutWrite, $invalidPipes.LauncherStderrWrite)
        $invalidFailed = -not $invalidCreation.Succeeded
        if ($invalidCreation.Succeeded) {
            [void][EnergyGridOneShotSupervisorNative]::ResumeThread($invalidCreation.ProcessInfo.hThread)
            [void][EnergyGridOneShotSupervisorNative]::WaitForSingleObject($invalidCreation.ProcessInfo.hProcess, 5000)
            Close-Native -Handle $invalidCreation.ProcessInfo.hThread
            Close-Native -Handle $invalidCreation.ProcessInfo.hProcess
        }
    }
    catch { $invalidFailed = $true }
    Assert-Native $invalidFailed 'unsupported_job_list_created_process'
}
finally {
    if ($null -ne $invalidAttributes) { $invalidAttributes.Dispose() }
    if ($null -ne $invalidPipes) {
        Close-Native -Handle $invalidPipes.LauncherStdinRead
        Close-Native -Handle $invalidPipes.SupervisorStdinWrite
        Close-Native -Handle $invalidPipes.SupervisorStdoutRead
        Close-Native -Handle $invalidPipes.LauncherStdoutWrite
        Close-Native -Handle $invalidPipes.SupervisorStderrRead
        Close-Native -Handle $invalidPipes.LauncherStderrWrite
    }
    Close-Native -Handle $invalidJob
}

Write-Output 'native_case=deadline_gate_and_one_way_resume'
$markerRoot = Join-Path $RootPath 'deadline-marker.txt'
foreach ($mode in @('expired', 'delayed', 'future', 'failure', 'anomaly')) {
    $gateJob = [IntPtr]::Zero
    $gateProcess = $null
    try {
        [EnergyGridOneShotSupervisorNative]::ResetControlState()
        $gateJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
        [EnergyGridOneShotSupervisorNative]::ConfigureJob($gateJob)
        $gateConfig = New-EgCaseConfig -Name ('deadline-' + $mode) -Values @{ ready_path = $markerRoot; run_delay_seconds = 60 }
        $gateProcess = New-ContainedPython -JobHandle $gateJob -ConfigPath $gateConfig
        Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'gate_intent_commit_failed'
        if ($mode -eq 'expired') {
            $gateResult = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, ([System.Diagnostics.Stopwatch]::GetTimestamp() - 1))
            Assert-Native (-not $gateResult.Attempted -and $gateResult.DeadlineExpired) 'expired_deadline_resumed'
            $late = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, (Future-Deadline))
            Assert-Native (-not $late.Attempted) 'expired_gate_reopened'
        }
        elseif ($mode -eq 'delayed') {
            $deadline = Future-Deadline -Milliseconds 100
            Start-Sleep -Milliseconds 250
            $gateResult = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, $deadline)
            Assert-Native (-not $gateResult.Attempted -and $gateResult.DeadlineExpired) 'delayed_deadline_resumed'
        }
        elseif ($mode -eq 'future') {
            $gateResult = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, (Future-Deadline))
            Assert-Native ($gateResult.Attempted -and $gateResult.Accepted) 'future_deadline_rejected'
            $second = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, (Future-Deadline))
            Assert-Native (-not $second.Attempted) 'resume_retried'
            $ranDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
                ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency)
            while (-not (Test-Path -LiteralPath $markerRoot) -and
                [System.Diagnostics.Stopwatch]::GetTimestamp() -lt $ranDeadline) {
                Start-Sleep -Milliseconds 50
            }
            Assert-Native (Test-Path -LiteralPath $markerRoot) 'future_child_did_not_execute'
            Remove-Item -LiteralPath $markerRoot -Force
        }
        elseif ($mode -eq 'failure') {
            $failedIntentPath = Join-Path $RootPath 'failed-intent.bin'
            $failedIntentStream = New-Object System.IO.FileStream(
                $failedIntentPath, [IO.FileMode]::Create, [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None, 1, [IO.FileOptions]::DeleteOnClose)
            $failedIntentStream.Dispose()
            $durability = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
                $failedIntentStream, [byte[]](1, 2, 3), 1000)
            Assert-Native (-not $durability.Succeeded) 'durability_failure_succeeded'
            [EnergyGridOneShotSupervisorNative]::RequestFailure()
            Assert-Native (-not [EnergyGridOneShotSupervisorNative]::CommitIntent()) 'failure_gate_reopened'
            $gateResult = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, (Future-Deadline))
            Assert-Native (-not $gateResult.Attempted) 'failure_gate_resumed'
        }
        else {
            $gateResult = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                ([IntPtr]([int64]1)), (Future-Deadline))
            Assert-Native ($gateResult.Attempted -and -not $gateResult.Accepted) 'resume_anomaly_not_observed'
            $late = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, (Future-Deadline))
            Assert-Native (-not $late.Attempted) 'resume_anomaly_reopened_gate'
        }
        if ($mode -ne 'future' -and (Test-Path -LiteralPath $markerRoot)) {
            throw ($mode + '_child_executed')
        }
    }
    finally {
        Cleanup-Job -JobHandle $gateJob -Process $gateProcess
        if (Test-Path -LiteralPath $markerRoot) { Remove-Item -LiteralPath $markerRoot -Force }
    }
}

Write-Output 'native_case=stdout_stderr_saturation_without_deadlock'
    $saturationJob = [IntPtr]::Zero
    $saturationProcess = $null
    try {
        $saturationJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
        [EnergyGridOneShotSupervisorNative]::ConfigureJob($saturationJob)
    $saturationProcess = New-ContainedPython -JobHandle $saturationJob -Operation 'saturate'
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'saturation_intent_failed'
    $saturationResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $saturationProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($saturationResume.Attempted -and $saturationResume.Accepted) 'saturation_resume_failed'
    $saturationWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $saturationProcess.ProcessHandle, 15000)
    $saturationTerminal = $saturationWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0
    Assert-Native $saturationTerminal 'saturation_pipe_deadlock'
    $saturationExit = [EnergyGridOneShotSupervisorNative]::GetProcessLive(
        $saturationProcess.ProcessHandle)
    Assert-Native ($saturationExit.Succeeded -and -not $saturationExit.Live -and
        $saturationExit.ExitCode -eq 0) 'saturation_child_exit_invalid'
    $saturationFinal = Wait-JobZero -JobHandle $saturationJob
    [void]$saturationProcess.StdoutDrain.Join(10000)
    [void]$saturationProcess.StderrDrain.Join(10000)
    Assert-Native ($saturationProcess.StdoutDrain.Completed -and $saturationProcess.StderrDrain.Completed) 'saturation_drains_incomplete'
    $expectedSaturationBytes = [uint64]1048576
    Assert-Native ($saturationProcess.StdoutDrain.Bytes -eq $expectedSaturationBytes) 'saturation_stdout_byte_count_mismatch'
    Assert-Native ($saturationProcess.StderrDrain.Bytes -eq $expectedSaturationBytes) 'saturation_stderr_byte_count_mismatch'
    Assert-Native ($saturationFinal.ActiveProcesses -eq 0) 'saturation_active_processes_nonzero'
    Write-Output 'native_saturation_writers=CONCURRENT'
    Write-Output ('native_saturation_stdout_bytes=' + [string]$saturationProcess.StdoutDrain.Bytes)
    Write-Output ('native_saturation_stderr_bytes=' + [string]$saturationProcess.StderrDrain.Bytes)
    Write-Output ('native_saturation_drains=' + [string]($saturationProcess.StdoutDrain.Completed -and $saturationProcess.StderrDrain.Completed))
    Write-Output ('native_saturation_stdout_drained=' + [string]$saturationProcess.StdoutDrain.Completed)
    Write-Output ('native_saturation_stderr_drained=' + [string]$saturationProcess.StderrDrain.Completed)
    Write-Output ('native_saturation_child_terminal=' + [string]$saturationTerminal)
    Write-Output ('native_saturation_exit_read=' + [string]$saturationExit.Succeeded)
    Write-Output ('native_saturation_exit_code=' + [string]$saturationExit.ExitCode)
    Write-Output ('native_saturation_active_processes=' + [string]$saturationFinal.ActiveProcesses)
}
finally {
    Cleanup-Job -JobHandle $saturationJob -Process $saturationProcess
}

Write-Output 'native_case=child_grandchild_containment_and_large_tree'
$treeJob = [IntPtr]::Zero
$treeProcess = $null
try {
    $treeJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($treeJob)
    $treeConfig = New-EgCaseConfig -Name 'large-tree' -Values @{ descendant_count = 40; run_delay_seconds = 60; child_wait_seconds = 60 }
    $treeProcess = New-ContainedPython -JobHandle $treeJob -ConfigPath $treeConfig
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'tree_intent_failed'
    $treeResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $treeProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($treeResume.Attempted -and $treeResume.Accepted) 'tree_resume_failed'
    $treeDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]15 * [int64][System.Diagnostics.Stopwatch]::Frequency)
    $treeAccounting = $null
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $treeDeadline) {
        $treeAccounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($treeJob)
        Assert-Native $treeAccounting.Succeeded 'tree_accounting_failed'
        if ($treeAccounting.TotalProcesses -ge 41) { break }
        Start-Sleep -Milliseconds 100
    }
    Assert-Native ($treeAccounting.TotalProcesses -ge 41) 'large_descendant_tree_not_observed'
    $treeTermination = [EnergyGridOneShotSupervisorNative]::TerminateJob(
        $treeJob, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
    Assert-Native $treeTermination.Success 'tree_termination_failed'
    $treeFinal = Wait-JobZero -JobHandle $treeJob -Seconds 20
    Assert-Native ($treeFinal.ActiveProcesses -eq 0) 'tree_reap_not_confirmed'
}
finally {
    Cleanup-Job -JobHandle $treeJob -Process $treeProcess
}

Write-Output 'native_case=explicit_handle_list_excludes_unrelated_inheritable_handle'
$canaryJob = [IntPtr]::Zero
$canaryProcess = $null
$canaryStream = $null
try {
    $canaryJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($canaryJob)
    $canaryPath = Join-Path $RootPath 'unrelated-canary.bin'

    $canaryStream = New-Object System.IO.FileStream(
        $canaryPath, [IO.FileMode]::Create, [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None, 1, [IO.FileOptions]::DeleteOnClose)
    $canaryInheritance = [EnergyGridOneShotSupervisorNative]::SetHandleInheritance(
        $canaryStream.SafeFileHandle.DangerousGetHandle(), $true)
    Assert-Native $canaryInheritance.Success 'canary_inheritance_setup_failed'
    $canaryInfo = [EnergyGridHandleCanaryIdentity]::Query($canaryStream.SafeFileHandle)
    Assert-Native $canaryInfo.Succeeded 'canary_identity_query_failed'
    $canaryProcess = New-ContainedPython -JobHandle $canaryJob -Operation 'handle-canary' -CanaryHandle $canaryStream.SafeFileHandle.DangerousGetHandle().ToInt64() -CanaryVolume $canaryInfo.VolumeSerialNumber -CanaryFileIndex $canaryInfo.FileIndex
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'canary_intent_failed'
    $canaryResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $canaryProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($canaryResume.Attempted -and $canaryResume.Accepted) 'canary_resume_failed'
    $canaryWait = [EnergyGridOneShotSupervisorNative]::WaitProcess($canaryProcess.ProcessHandle, 10000)
    Assert-Native ($canaryWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) 'canary_child_wait_failed'
    $canaryFinal = Wait-JobZero -JobHandle $canaryJob
    $canaryExit = [EnergyGridOneShotSupervisorNative]::GetProcessLive($canaryProcess.ProcessHandle)
    Assert-Native ($canaryExit.Succeeded -and $canaryExit.ExitCode -eq 0) 'canary_child_failed'
    Assert-Native ($canaryProcess.StdoutDrain.Bytes -gt 0) 'canary_result_missing'
    Assert-Native ($canaryFinal.ActiveProcesses -eq 0) 'canary_reap_failed'
}
finally {
    if ($null -ne $canaryStream) { $canaryStream.Dispose() }
    Cleanup-Job -JobHandle $canaryJob -Process $canaryProcess
}

Write-Output 'native_assurance_cases=15'
Write-Output 'native_assurance=PASS'
}

function Invoke-EgFunctionCases {
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RootPath = $DataRoot
$RunId = 'eg-function-assurance'
$TimeoutSeconds = 60
$script:EgEvidenceRootNormal = [IO.Path]::GetFullPath($RootPath)
$script:EgPythonExeNormal = [IO.Path]::GetFullPath($PythonExe)
$script:EgConfigPathNormal = Join-Path $RootPath 'function-config.json'
. $LauncherLibraryPath
Set-EgFixturePythonEnvironment -ModulePath $PythonModulePath -ExpectedSha256 (Get-FileHash -LiteralPath $PythonModulePath -Algorithm SHA256).Hash
. $FunctionsPath

function Assert-Native {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

Assert-Native ([EgOutcomeIdentity]::VerifyLayout()) 'file_id_info_layout_invalid'
Assert-Native ([EgOutcomeIdentity]::DirectoryAccess -eq [uint32]0x00100081) `
    'directory_access_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::DirectoryShare -eq [uint32]0x00000003) `
    'directory_share_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::DirectoryCreation -eq [uint32]0x00000003) `
    'directory_creation_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::DirectoryFlags -eq [uint32]0x02200000) `
    'directory_flags_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::FileAccess -eq [uint32]2148532352) `
    'file_access_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::FileShare -eq [uint32]0x00000001) `
    'file_share_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::FileCreation -eq [uint32]0x00000003) `
    'file_creation_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::FileFlags -eq [uint32]0x00200000) `
    'file_flags_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::FileIdInfoValue -eq 18) 'file_id_info_class_invalid'
Write-Output 'file_id_info_layout=PASS'


function Future-Deadline {
    param([int]$Milliseconds = 30000)
    return [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]$Milliseconds * [int64][System.Diagnostics.Stopwatch]::Frequency / 1000))
}

function Close-Native {
    param([IntPtr]$Handle)
    if ($Handle -ne [IntPtr]::Zero) {
        [void][EnergyGridOneShotSupervisorNative]::CloseHandleChecked($Handle)
    }
}

function New-TestChild {
    param(
        [IntPtr]$JobHandle,
        [ValidateSet('sleep', 'short', 'descendant', 'wait-release')][string]$Mode = 'sleep',
        [string]$ReleasePath
    )
    $document = [ordered]@{
        module_sha256 = (Get-FileHash -LiteralPath $PythonModulePath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    switch ($Mode) {
        'sleep' { $document.run_delay_seconds = 60 }
        'short' { $document.run_delay_seconds = 0.15 }
        'descendant' {
            $document.run_delay_seconds = 0.15
            $document.descendant_count = 1
            $document.child_wait_seconds = 60
            $document.detached_descendants = $true
        }
        'wait-release' {
            if ([string]::IsNullOrWhiteSpace($ReleasePath)) { throw 'release_path_missing' }
            $document.run_delay_seconds = 0
            $document.release_path = $ReleasePath
        }
    }
    $configPath = Join-Path $RootPath ([guid]::NewGuid().ToString('N') + '.json')
    [IO.File]::WriteAllText(
        $configPath, ($document | ConvertTo-Json -Depth 8 -Compress), (New-Object System.Text.UTF8Encoding($false)))
    $items = @($script:EgPythonExeNormal, '-B', '-m', 'energygrid_bill_downloader',
        'run', '--config', $configPath)
    $line = ConvertTo-EgNativeCommandLine -Argument $items
    $pipes = [EnergyGridOneShotSupervisorNative]::CreatePipes()
    $attributes = New-Object EnergyGridOneShotSupervisorNative+AttributeResources(
        $JobHandle, $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite,
        $pipes.LauncherStderrWrite)
    $builder = New-Object System.Text.StringBuilder($line)
    $created = [EnergyGridOneShotSupervisorNative]::CreateContainedProcess(
        $script:EgPythonExeNormal, $builder, $RootPath, $attributes.AttributeList,
        $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite, $pipes.LauncherStderrWrite)
    $attributes.Dispose()
    Assert-Native $created.Succeeded ('native_create_failed:' + $created.ErrorCode)
    Close-Native -Handle $pipes.LauncherStdinRead
    Close-Native -Handle $pipes.LauncherStdoutWrite
    Close-Native -Handle $pipes.LauncherStderrWrite
    $stdoutDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStdoutRead)
    $stderrDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStderrRead)
    Close-Native -Handle $pipes.SupervisorStdinWrite
    return [pscustomobject]@{
        ProcessHandle = $created.ProcessInfo.hProcess
        ThreadHandle = $created.ProcessInfo.hThread
        ProcessId = $created.ProcessInfo.dwProcessId
        StdoutDrain = $stdoutDrain
        StderrDrain = $stderrDrain
    }
}

function Wait-JobZero {
    param([IntPtr]$JobHandle)
    $deadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]10 * [int64][System.Diagnostics.Stopwatch]::Frequency)
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $deadline) {
        $accounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($JobHandle)
        Assert-Native $accounting.Succeeded ('accounting_failed:' + $accounting.ErrorCode)
        if ($accounting.ActiveProcesses -eq 0) { return $accounting }
        Start-Sleep -Milliseconds 100
    }
    throw 'reap_timeout'
}

function Close-TestChild {
    param($Process)
    if ($null -eq $Process) { return }
    [void]$Process.StdoutDrain.Join(10000)
    [void]$Process.StderrDrain.Join(10000)
    Close-Native -Handle $Process.ThreadHandle
    Close-Native -Handle $Process.ProcessHandle
}

function Cleanup-Job {
    param([IntPtr]$JobHandle, $Process)
    try {
        if ($JobHandle -ne [IntPtr]::Zero) {
            [void][EnergyGridOneShotSupervisorNative]::TerminateJob(
                $JobHandle, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        }
    }
    catch { }
    try { Close-TestChild -Process $Process } catch { }
    Close-Native -Handle $JobHandle
}

function New-State {
    param([bool]$OutcomeCommitted = $false)
    return [pscustomobject]@{
        outcome_committed = $OutcomeCommitted
        outcome_write_attempted = $false
        containment_failure = $false
        evidence_integrity_failure = $false
        drain_failure = $false
        termination_failure = $false
        observer_failed = $false
        creation_succeeded = $true
        creation_attempted = $true
        resume_attempted = $false
        reap_confirmed = $true
        stdout_complete = $true
        stderr_complete = $true
        timed_out = $false
        interrupted = $false
        start_verdict = 'NOT_STARTED_PROVEN'
        launcher_exit_code = 1
        termination_started = $false
        termination_succeeded = $false
        support_ref = 'EG_TEST'
        error_code = $null
        precreate_rejection = $false
        total_processes = [uint64]0
        active_processes = [uint64]0
        total_terminated_processes = [uint64]0
        intent_committed = $false
        intent_bytes = [byte[]]@()
        application_child_observed = $false
        application_child_observation_elapsed_ms = [int64]0
        stdout_bytes = [uint64]0
        stderr_bytes = [uint64]0
        descendant_grace_expired = $false
    }
}

$script:GraceSleepMode = 'off'
$script:GraceSleepCalls = 0
$script:GraceSleepJobHandle = [IntPtr]::Zero
$script:GraceSleepReleasePath = $null

function Start-Sleep {
    param([int]$Milliseconds)

    if ($script:GraceSleepMode -ne 'off' -and $script:GraceSleepCalls -eq 0) {
        $script:GraceSleepCalls = $script:GraceSleepCalls + 1
        if ($null -ne $script:GraceSleepReleasePath) {
            [IO.File]::WriteAllText($script:GraceSleepReleasePath, 'release')
        }
        if ($script:GraceSleepMode -eq 'global-completion' -or
            $script:GraceSleepMode -eq 'local-completion') {
            $zeroDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
                ([int64]10 * [int64][System.Diagnostics.Stopwatch]::Frequency)
            $zeroAccounting = $null
            while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $zeroDeadline) {
                $zeroAccounting = [EnergyGridOneShotSupervisorNative]::GetAccounting(
                    $script:GraceSleepJobHandle)
                Assert-Native $zeroAccounting.Succeeded ('grace_completion_accounting_failed:' +
                    $zeroAccounting.ErrorCode)
                if ($zeroAccounting.ActiveProcesses -eq 0) { break }
                Microsoft.PowerShell.Utility\Start-Sleep -Milliseconds 50
            }
            Assert-Native ($null -ne $zeroAccounting -and $zeroAccounting.ActiveProcesses -eq 0) `
                'grace_completion_zero_not_observed'
            if ($script:GraceSleepMode -eq 'global-completion') {
                [Threading.Thread]::Sleep(250)
            }
            else {
                [Threading.Thread]::Sleep(5200)
            }
        }
        elseif ($script:GraceSleepMode -eq 'timeout-local-precedence') {
            [Threading.Thread]::Sleep(5200)
        }
        return
    }
    Microsoft.PowerShell.Utility\Start-Sleep -Milliseconds $Milliseconds
}

$script:OutcomeSeamEnabled = $false
$script:OutcomeMode = 'ordinary'
$script:OutcomeConstructionCount = 0
$script:OutcomeConstructorArgumentsValid = $false
$script:EgExpectedOutcomePath = $null

function New-Object {
    param(
        [Parameter(Mandatory = $true, Position = 0)][string]$TypeName,
        [Parameter(Position = 1, ValueFromRemainingArguments = $true)][object[]]$ArgumentList
    )

    $arguments = @()
    if ($PSBoundParameters.ContainsKey('ArgumentList')) {
        $arguments = @($ArgumentList)
        if ($arguments.Count -eq 1 -and $arguments[0] -is [array]) {
            $arguments = @($arguments[0])
        }
    }
    $isTarget = $script:OutcomeSeamEnabled -and
        $TypeName -eq 'System.IO.FileStream' -and
        $arguments.Count -eq 6 -and
        [string]$arguments[0] -eq $script:EgExpectedOutcomePath
    if ($isTarget) {
        Assert-Native ($arguments[1] -eq [IO.FileMode]::CreateNew) 'outcome_file_mode_mismatch'
        Assert-Native ($arguments[2] -eq [IO.FileAccess]::Write) 'outcome_file_access_mismatch'
        Assert-Native ($arguments[3] -eq [IO.FileShare]::None) 'outcome_file_share_mismatch'
        Assert-Native ($arguments[4] -eq 4096) 'outcome_file_buffer_mismatch'
        Assert-Native ($arguments[5] -eq [IO.FileOptions]::WriteThrough) 'outcome_file_options_mismatch'
        $script:OutcomeConstructionCount = $script:OutcomeConstructionCount + 1
        $script:OutcomeConstructorArgumentsValid = $true
        if ($script:OutcomeMode -eq 'delayed') {
            return Microsoft.PowerShell.Utility\New-Object `
                -TypeName 'EnergyGridOutcomeDelayedFlushStreamForFunctionTest' `
                -ArgumentList $arguments
        }
    }

    if ($PSBoundParameters.ContainsKey('ArgumentList')) {
        return Microsoft.PowerShell.Utility\New-Object -TypeName $TypeName -ArgumentList $arguments
    }
    return Microsoft.PowerShell.Utility\New-Object -TypeName $TypeName
}

Write-Output 'function_case=timed_out_intent_rejection'
$intentGateJob = [IntPtr]::Zero
$intentGateProcess = $null
$intentStream = $null
try {
    $intentGateJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($intentGateJob)
    $intentGateProcess = New-TestChild -JobHandle $intentGateJob
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    $script:EgState = New-State
    $intentPath = Join-Path $RootPath 'timed-out-intent.bin'
    $intentStream = New-Object EnergyGridDelayedFlushStreamForFunctionTest($intentPath, 250)
    $intentFailed = $false
    try {
        Write-EgReservedIntent -Stream $intentStream -Bytes ([byte[]](1, 2, 3)) `
            -TimeoutMilliseconds 50
    }
    catch { $intentFailed = $true }
    Assert-Native $intentFailed 'timed_out_intent_was_accepted'
    Assert-Native (-not $script:EgState.intent_committed) 'timed_out_intent_committed'
    Start-Sleep -Milliseconds 500
    [EnergyGridOneShotSupervisorNative]::RequestFailure()
    Assert-Native (-not [EnergyGridOneShotSupervisorNative]::CommitIntent()) 'timed_out_intent_gate_reopened'
    $intentResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $intentGateProcess.ThreadHandle, (Future-Deadline))
    Assert-Native (-not $intentResume.Attempted) 'timed_out_intent_resumed'
}
finally {
    if ($null -ne $intentStream) { $intentStream.Dispose() }
    try {
        if ($intentGateJob -ne [IntPtr]::Zero) {
            [void][EnergyGridOneShotSupervisorNative]::TerminateJob(
                $intentGateJob, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        }
    }
    catch { }
    try { Close-TestChild -Process $intentGateProcess } catch { }
    Close-Native -Handle $intentGateJob
}

Write-Output 'function_case=timed_out_outcome_contract'
$intentFunction = (Get-Command Write-EgReservedIntent -CommandType Function).ScriptBlock.ToString()
$outcomeFunction = (Get-Command Write-EgOutcome -CommandType Function).ScriptBlock.ToString()
Assert-Native ($intentFunction.Contains('$durability.TimedOut -or -not $durability.Succeeded')) 'intent_timeout_check_missing'
Assert-Native ($outcomeFunction.Contains('$durability.TimedOut -or -not $durability.Succeeded')) 'outcome_timeout_check_missing'

function Test-EgDirectoryObjectAttributes {
    param([uint32]$Attributes)
    return (($Attributes -band [uint32]0x00000010) -ne 0 -and
        ($Attributes -band [uint32]0x00000400) -eq 0)
}

function Test-EgFileObjectAttributes {
    param([uint32]$Attributes)
    return (($Attributes -band [uint32]0x00000010) -eq 0 -and
        ($Attributes -band [uint32]0x00000400) -eq 0)
}

function Test-EgObjectIdentityEqual {
    param($Left, $Right)
    if ($null -eq $Left -or $null -eq $Right -or
        -not $Left.Succeeded -or -not $Right.Succeeded) { return $false }
    return [bool]($Left.VolumeSerialNumber -eq $Right.VolumeSerialNumber -and
        $Left.FileIdPart0 -eq $Right.FileIdPart0 -and
        $Left.FileIdPart1 -eq $Right.FileIdPart1)
}

function Assert-EgDistinctDirectoryIdentity {
    param([string]$PathA, [string]$PathB, [string]$FailureMarker)

    $handleA = $null
    $handleB = $null
    try {
        $openedA = [EgOutcomeIdentity]::OpenDirectory($PathA)
        $handleA = $openedA.Handle
        Assert-Native ($openedA.Succeeded -and $null -ne $handleA) 'identity_control_directory_open_a'
        $openedB = [EgOutcomeIdentity]::OpenDirectory($PathB)
        $handleB = $openedB.Handle
        Assert-Native ($openedB.Succeeded -and $null -ne $handleB) 'identity_control_directory_open_b'
        $infoA = [EgOutcomeIdentity]::QueryInfo($handleA)
        $infoB = [EgOutcomeIdentity]::QueryInfo($handleB)
        Assert-Native ($infoA.Succeeded -and $infoB.Succeeded) 'identity_control_directory_query'
        Assert-Native (-not (Test-EgObjectIdentityEqual -Left $infoA -Right $infoB)) $FailureMarker
    }
    finally {
        if ($null -ne $handleB) { $handleB.Dispose() }
        if ($null -ne $handleA) { $handleA.Dispose() }
    }
}

function Assert-EgDistinctFileIdentity {
    param([string]$PathA, [string]$PathB, [string]$FailureMarker)

    $handleA = $null
    $handleB = $null
    try {
        $openedA = [EgOutcomeIdentity]::OpenFile($PathA)
        $handleA = $openedA.Handle
        Assert-Native ($openedA.Succeeded -and $null -ne $handleA) 'identity_control_file_open_a'
        $openedB = [EgOutcomeIdentity]::OpenFile($PathB)
        $handleB = $openedB.Handle
        Assert-Native ($openedB.Succeeded -and $null -ne $handleB) 'identity_control_file_open_b'
        $infoA = [EgOutcomeIdentity]::QueryInfo($handleA)
        $infoB = [EgOutcomeIdentity]::QueryInfo($handleB)
        Assert-Native ($infoA.Succeeded -and $infoB.Succeeded) 'identity_control_file_query'
        Assert-Native (-not (Test-EgObjectIdentityEqual -Left $infoA -Right $infoB)) $FailureMarker
    }
    finally {
        if ($null -ne $handleB) { $handleB.Dispose() }
        if ($null -ne $handleA) { $handleA.Dispose() }
    }
}

function Assert-EgHardLinkIdentity {
    param([string]$OriginalPath, [string]$AliasPath)

    $originalHandle = $null
    $aliasHandle = $null
    try {
        $originalOpen = [EgOutcomeIdentity]::OpenFile($OriginalPath)
        $originalHandle = $originalOpen.Handle
        Assert-Native ($originalOpen.Succeeded -and $null -ne $originalHandle) `
            'hard_link_original_open_failed'
        $aliasOpen = [EgOutcomeIdentity]::OpenFile($AliasPath)
        $aliasHandle = $aliasOpen.Handle
        Assert-Native ($aliasOpen.Succeeded -and $null -ne $aliasHandle) 'hard_link_alias_open_failed'
        $originalInfo = [EgOutcomeIdentity]::QueryInfo($originalHandle)
        $aliasInfo = [EgOutcomeIdentity]::QueryInfo($aliasHandle)
        Assert-Native ($originalInfo.Succeeded -and $aliasInfo.Succeeded) 'hard_link_query_failed'
        Assert-Native (Test-EgObjectIdentityEqual -Left $originalInfo -Right $aliasInfo) `
            'hard_link_identity_not_equal'
        Assert-Native ($originalInfo.NumberOfLinks -gt 1 -and $aliasInfo.NumberOfLinks -gt 1) `
            'hard_link_count_not_observed'
    }
    finally {
        if ($null -ne $aliasHandle) { $aliasHandle.Dispose() }
        if ($null -ne $originalHandle) { $originalHandle.Dispose() }
    }
}

function Assert-EgCrossRootHardLinkIdentity {
    param(
        [string]$ExpectedRootPath,
        [string]$DiscoveredRootPath,
        [string]$ExpectedFilePath,
        [string]$DiscoveredFilePath
    )

    $expectedRootHandle = $null
    $discoveredRootHandle = $null
    $expectedFileHandle = $null
    $discoveredFileHandle = $null
    try {
        $expectedRootOpen = [EgOutcomeIdentity]::OpenDirectory($ExpectedRootPath)
        $expectedRootHandle = $expectedRootOpen.Handle
        Assert-Native ($expectedRootOpen.Succeeded -and $null -ne $expectedRootHandle) `
            'cross_root_expected_root_open_failed'
        $discoveredRootOpen = [EgOutcomeIdentity]::OpenDirectory($DiscoveredRootPath)
        $discoveredRootHandle = $discoveredRootOpen.Handle
        Assert-Native ($discoveredRootOpen.Succeeded -and $null -ne $discoveredRootHandle) `
            'cross_root_discovered_root_open_failed'
        $expectedFileOpen = [EgOutcomeIdentity]::OpenFile($ExpectedFilePath)
        $expectedFileHandle = $expectedFileOpen.Handle
        Assert-Native ($expectedFileOpen.Succeeded -and $null -ne $expectedFileHandle) `
            'cross_root_expected_file_open_failed'
        $discoveredFileOpen = [EgOutcomeIdentity]::OpenFile($DiscoveredFilePath)
        $discoveredFileHandle = $discoveredFileOpen.Handle
        Assert-Native ($discoveredFileOpen.Succeeded -and $null -ne $discoveredFileHandle) `
            'cross_root_discovered_file_open_failed'
        $expectedRootInfo = [EgOutcomeIdentity]::QueryInfo($expectedRootHandle)
        $discoveredRootInfo = [EgOutcomeIdentity]::QueryInfo($discoveredRootHandle)
        $expectedFileInfo = [EgOutcomeIdentity]::QueryInfo($expectedFileHandle)
        $discoveredFileInfo = [EgOutcomeIdentity]::QueryInfo($discoveredFileHandle)
        Assert-Native ($expectedRootInfo.Succeeded -and $discoveredRootInfo.Succeeded -and
            $expectedFileInfo.Succeeded -and $discoveredFileInfo.Succeeded) `
            'cross_root_query_failed'
        Assert-Native (-not (Test-EgObjectIdentityEqual -Left $expectedRootInfo -Right $discoveredRootInfo)) `
            'cross_root_directory_identity_accepted'
        Assert-Native (Test-EgObjectIdentityEqual -Left $expectedFileInfo -Right $discoveredFileInfo) `
            'cross_root_file_identity_not_equal'
        Assert-Native ($expectedFileInfo.NumberOfLinks -gt 1 -and
            $discoveredFileInfo.NumberOfLinks -gt 1) 'cross_root_link_count_not_observed'
    }
    finally {
        if ($null -ne $discoveredFileHandle) { $discoveredFileHandle.Dispose() }
        if ($null -ne $expectedFileHandle) { $expectedFileHandle.Dispose() }
        if ($null -ne $discoveredRootHandle) { $discoveredRootHandle.Dispose() }
        if ($null -ne $expectedRootHandle) { $expectedRootHandle.Dispose() }
    }
}

function Assert-OutcomeFile {
    param(
        [string]$EvidenceRoot,
        [string]$ExpectedPath,
        [string]$ExpectedRunId,
        [string]$ExpectedIntentHash
    )

    $rootHandle = $null
    $expectedParentHandle = $null
    $discoveredParentHandle = $null
    $expectedFileHandle = $null
    $discoveredFileHandle = $null
    try {
        $ExpectedLeaf = $ExpectedRunId + '.outcome.json'
        $ConstructedExpectedPath = Join-Path $EvidenceRoot $ExpectedLeaf

        $outcomeFiles = @(Get-ChildItem -LiteralPath $EvidenceRoot -Filter '*.outcome.json' -File)
        Assert-Native ($outcomeFiles.Count -eq 1) 'ordinary_outcome_file_count'
        Assert-Native ([StringComparer]::OrdinalIgnoreCase.Equals(
            [string]$outcomeFiles[0].Name, $ExpectedLeaf)) 'ordinary_outcome_leaf_mismatch'

        $expectedParentPath = [IO.Path]::GetDirectoryName($ConstructedExpectedPath)
        $discoveredFilePath = [string]$outcomeFiles[0].FullName
        $discoveredParentPath = [IO.Path]::GetDirectoryName($discoveredFilePath)
        Assert-Native (-not [string]::IsNullOrEmpty($expectedParentPath) -and
            -not [string]::IsNullOrEmpty($discoveredParentPath)) 'ordinary_outcome_parent_missing'

        $rootOpen = [EgOutcomeIdentity]::OpenDirectory($EvidenceRoot)
        $rootHandle = $rootOpen.Handle
        Assert-Native ($rootOpen.Succeeded -and $null -ne $rootHandle) 'ordinary_outcome_root_open'
        $expectedParentOpen = [EgOutcomeIdentity]::OpenDirectory($expectedParentPath)
        $expectedParentHandle = $expectedParentOpen.Handle
        Assert-Native ($expectedParentOpen.Succeeded -and $null -ne $expectedParentHandle) `
            'ordinary_outcome_expected_parent_open'
        $discoveredParentOpen = [EgOutcomeIdentity]::OpenDirectory($discoveredParentPath)
        $discoveredParentHandle = $discoveredParentOpen.Handle
        Assert-Native ($discoveredParentOpen.Succeeded -and $null -ne $discoveredParentHandle) `
            'ordinary_outcome_discovered_parent_open'
        $expectedFileOpen = [EgOutcomeIdentity]::OpenFile($ConstructedExpectedPath)
        $expectedFileHandle = $expectedFileOpen.Handle
        Assert-Native ($expectedFileOpen.Succeeded -and $null -ne $expectedFileHandle) `
            'ordinary_outcome_expected_file_open'
        $discoveredFileOpen = [EgOutcomeIdentity]::OpenFile($discoveredFilePath)
        $discoveredFileHandle = $discoveredFileOpen.Handle
        Assert-Native ($discoveredFileOpen.Succeeded -and $null -ne $discoveredFileHandle) `
            'ordinary_outcome_discovered_file_open'

        $rootInfo = [EgOutcomeIdentity]::QueryInfo($rootHandle)
        $expectedParentInfo = [EgOutcomeIdentity]::QueryInfo($expectedParentHandle)
        $discoveredParentInfo = [EgOutcomeIdentity]::QueryInfo($discoveredParentHandle)
        $expectedFileInfo = [EgOutcomeIdentity]::QueryInfo($expectedFileHandle)
        $discoveredFileInfo = [EgOutcomeIdentity]::QueryInfo($discoveredFileHandle)
        Assert-Native ($rootInfo.Succeeded -and $expectedParentInfo.Succeeded -and
            $discoveredParentInfo.Succeeded -and $expectedFileInfo.Succeeded -and
            $discoveredFileInfo.Succeeded) 'ordinary_outcome_identity_query'
        Assert-Native (Test-EgDirectoryObjectAttributes -Attributes $rootInfo.FileAttributes) `
            'ordinary_outcome_root_attributes'
        Assert-Native (Test-EgDirectoryObjectAttributes -Attributes $expectedParentInfo.FileAttributes) `
            'ordinary_outcome_expected_parent_attributes'
        Assert-Native (Test-EgDirectoryObjectAttributes -Attributes $discoveredParentInfo.FileAttributes) `
            'ordinary_outcome_discovered_parent_attributes'
        Assert-Native (Test-EgFileObjectAttributes -Attributes $expectedFileInfo.FileAttributes) `
            'ordinary_outcome_expected_file_attributes'
        Assert-Native (Test-EgFileObjectAttributes -Attributes $discoveredFileInfo.FileAttributes) `
            'ordinary_outcome_discovered_file_attributes'
        Assert-Native (Test-EgObjectIdentityEqual -Left $rootInfo -Right $expectedParentInfo) `
            'ordinary_outcome_root_expected_parent_identity'
        Assert-Native (Test-EgObjectIdentityEqual -Left $rootInfo -Right $discoveredParentInfo) `
            'ordinary_outcome_root_discovered_parent_identity'
        Assert-Native (Test-EgObjectIdentityEqual -Left $expectedFileInfo -Right $discoveredFileInfo) `
            'ordinary_outcome_file_identity'
        Assert-Native ($expectedFileInfo.NumberOfLinks -eq 1 -and
            $discoveredFileInfo.NumberOfLinks -eq 1) 'ordinary_outcome_link_count_before'

        $retainedRead = [EgOutcomeIdentity]::ReadRetained($expectedFileHandle)
        Assert-Native $retainedRead.Succeeded 'ordinary_outcome_retained_read'
        $bytes = $retainedRead.Bytes
        $script:LastOutcomeBytes = [byte[]]$bytes.Clone()
        $expectedFileAfterRead = [EgOutcomeIdentity]::QueryInfo($expectedFileHandle)
        $discoveredFileAfterRead = [EgOutcomeIdentity]::QueryInfo($discoveredFileHandle)
        Assert-Native ($expectedFileAfterRead.Succeeded -and $discoveredFileAfterRead.Succeeded) `
            'ordinary_outcome_identity_after_read_query'
        Assert-Native (Test-EgObjectIdentityEqual -Left $expectedFileInfo -Right $expectedFileAfterRead) `
            'ordinary_outcome_expected_identity_changed_after_read'
        Assert-Native (Test-EgObjectIdentityEqual -Left $expectedFileAfterRead -Right $discoveredFileAfterRead) `
            'ordinary_outcome_file_identity_changed_after_read'
        Assert-Native (Test-EgFileObjectAttributes -Attributes $expectedFileAfterRead.FileAttributes) `
            'ordinary_outcome_expected_file_attributes_after_read'
        Assert-Native (Test-EgFileObjectAttributes -Attributes $discoveredFileAfterRead.FileAttributes) `
            'ordinary_outcome_discovered_file_attributes_after_read'
        Assert-Native ($expectedFileAfterRead.NumberOfLinks -eq 1 -and
            $discoveredFileAfterRead.NumberOfLinks -eq 1) 'ordinary_outcome_link_count_after'

        Assert-Native ($bytes.Length -ge 1 -and $bytes.Length -le 65536) 'ordinary_outcome_size_invalid'
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $jsonText = $strictUtf8.GetString($bytes)
        $document = $jsonText | ConvertFrom-Json
        $expectedFields = @(
            'schema', 'run_id', 'operation', 'intent_sha256', 'start_verdict',
            'launcher_exit_code', 'creation_attempted', 'resume_attempted',
            'application_child_observed', 'application_child_observation_elapsed_ms',
            'total_processes', 'active_processes', 'total_terminated_processes',
            'reap_confirmed', 'stdout_bytes', 'stderr_bytes', 'stdout_complete',
            'stderr_complete', 'raw_stream_retained_bytes', 'containment', 'completed_utc'
        )
        $actualFields = @($document.PSObject.Properties.Name)
        Assert-Native ($actualFields.Count -eq $expectedFields.Count) 'ordinary_outcome_field_count'
        foreach ($field in $expectedFields) {
            Assert-Native ($actualFields -contains $field) ('ordinary_outcome_field_missing:' + $field)
        }
        Assert-Native ($document.schema -eq 'energygrid.one_shot_supervisor.outcome.v1') `
            'ordinary_outcome_schema_invalid'
        Assert-Native ($document.run_id -eq $ExpectedRunId) 'ordinary_outcome_run_id_invalid'
        Assert-Native ($document.operation -eq 'run') 'ordinary_outcome_operation_invalid'
        Assert-Native ($document.intent_sha256 -eq $ExpectedIntentHash) `
            'ordinary_outcome_intent_hash_invalid'
        Assert-Native ($document.raw_stream_retained_bytes -eq 0) 'ordinary_outcome_raw_bytes_retained'
        Assert-Native ($document.creation_attempted -and $document.resume_attempted) `
            'ordinary_outcome_representative_flags_invalid'
        Assert-Native ($document.reap_confirmed -and $document.stdout_complete -and $document.stderr_complete) `
            'ordinary_outcome_representative_completion_invalid'
        return $document
    }
    finally {
        if ($null -ne $discoveredFileHandle) { $discoveredFileHandle.Dispose() }
        if ($null -ne $expectedFileHandle) { $expectedFileHandle.Dispose() }
        if ($null -ne $discoveredParentHandle) { $discoveredParentHandle.Dispose() }
        if ($null -ne $expectedParentHandle) { $expectedParentHandle.Dispose() }
        if ($null -ne $rootHandle) { $rootHandle.Dispose() }
    }
}

function Assert-EgRejected {
    param([scriptblock]$Action, [string]$FailureMarker)
    $rejected = $false
    try { & $Action | Out-Null } catch { $rejected = $true }
    Assert-Native $rejected $FailureMarker
}

Write-Output 'function_case=timed_out_outcome_actual_delayed'
$delayedOutcomeRoot = Join-Path $RootPath 'delayed-outcome'
[IO.Directory]::CreateDirectory($delayedOutcomeRoot) | Out-Null
$RunId = 'EG-OUTCOME-DELAYED-0001'
$script:EgEvidenceRootNormal = $delayedOutcomeRoot
$script:EgExpectedOutcomePath = Join-Path $delayedOutcomeRoot ($RunId + '.outcome.json')
$script:OutcomeSeamEnabled = $true
$script:OutcomeMode = 'delayed'
$script:OutcomeConstructionCount = 0
$script:OutcomeConstructorArgumentsValid = $false
[EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::ResetState()
$script:EgState = New-State
Assert-Native (-not $script:EgState.outcome_committed) 'delayed_outcome_precommitted'
Write-EgOutcome -StartVerdict 'STARTED_PROVEN'
Assert-Native $script:EgState.outcome_write_attempted 'delayed_outcome_write_not_attempted'
Assert-Native (-not $script:EgState.outcome_committed) 'delayed_outcome_committed'
Assert-Native $script:EgState.evidence_integrity_failure 'delayed_outcome_evidence_not_failed'
Assert-Native ($script:EgState.support_ref -eq 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED') `
    'delayed_outcome_support_ref_invalid'
Assert-Native ($script:OutcomeConstructionCount -eq 1) 'delayed_outcome_construction_count_invalid'
Assert-Native $script:OutcomeConstructorArgumentsValid 'delayed_outcome_constructor_arguments_invalid'
Assert-Native ([EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::FlushEntered.WaitOne(0)) `
    'delayed_outcome_flush_not_entered'
Assert-Native ($null -ne [EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::WorkerThread) `
    'delayed_outcome_worker_not_captured'
Assert-Native ([EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::IsReleaseClosed()) `
    'delayed_outcome_release_open_too_early'
Assert-Native ([EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::WorkerThread.IsAlive) `
    'delayed_outcome_worker_not_blocked'
$delayedOutcomeFiles = @(Get-ChildItem -LiteralPath $delayedOutcomeRoot -Filter '*.outcome.json' -File)
Assert-Native ($delayedOutcomeFiles.Count -eq 1) 'delayed_outcome_file_count_invalid'

[EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::Release()
Assert-Native ([EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::JoinWorker(15000)) `
    'delayed_outcome_worker_join_failed'
Assert-Native (-not $script:EgState.outcome_committed) 'delayed_outcome_committed_after_worker'
Assert-Native $script:EgState.evidence_integrity_failure 'delayed_outcome_evidence_changed_after_worker'
Assert-Native ($script:EgState.support_ref -eq 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED') `
    'delayed_outcome_support_ref_changed_after_worker'
Assert-Native ($script:OutcomeConstructionCount -eq 1) 'delayed_outcome_retried'
Assert-Native ((Get-EgExitCode) -eq 3) 'delayed_outcome_exit_not_three'
Assert-Native ((@(Get-ChildItem -LiteralPath $delayedOutcomeRoot -Filter '*.outcome.json' -File)).Count -eq 1) `
    'delayed_outcome_alternate_file_created'
$script:OutcomeSeamEnabled = $false
Remove-Item -LiteralPath $delayedOutcomeRoot -Recurse -Force
Assert-Native (-not (Test-Path -LiteralPath $delayedOutcomeRoot)) 'delayed_outcome_cleanup_failed'

Write-Output 'function_case=timed_out_outcome_actual_ordinary'
$ordinaryOutcomeRoot = Join-Path $RootPath 'ordinary-outcome'
[IO.Directory]::CreateDirectory($ordinaryOutcomeRoot) | Out-Null
$RunId = 'EG-OUTCOME-ORDINARY-0001'
$script:EgEvidenceRootNormal = $ordinaryOutcomeRoot
$script:EgExpectedOutcomePath = Join-Path $ordinaryOutcomeRoot ($RunId + '.outcome.json')
$script:OutcomeSeamEnabled = $true
$script:OutcomeMode = 'ordinary'
$script:OutcomeConstructionCount = 0
$script:OutcomeConstructorArgumentsValid = $false
$script:EgState = New-State
$script:EgState.intent_committed = $true
$script:EgState.intent_bytes = [byte[]](0, 1, 2, 255)
$script:EgState.creation_attempted = $true
$script:EgState.resume_attempted = $true
$script:EgState.application_child_observed = $true
$script:EgState.application_child_observation_elapsed_ms = [int64]17
$script:EgState.total_processes = [uint64]2
$script:EgState.active_processes = [uint64]0
$script:EgState.total_terminated_processes = [uint64]1
$script:EgState.stdout_bytes = [uint64]12
$script:EgState.stderr_bytes = [uint64]34
$script:EgState.reap_confirmed = $true
$hash = New-Object System.Security.Cryptography.SHA256Managed
try {
    $expectedIntentHash = ([BitConverter]::ToString($hash.ComputeHash($script:EgState.intent_bytes))).Replace('-', '').ToLowerInvariant()
}
finally { $hash.Dispose() }
Assert-Native (-not $script:EgState.outcome_committed) 'ordinary_outcome_precommitted'
Write-EgOutcome -StartVerdict 'STARTED_PROVEN'
Assert-Native $script:EgState.outcome_write_attempted 'ordinary_outcome_write_not_attempted'
Assert-Native $script:EgState.outcome_committed 'ordinary_outcome_not_committed'
Assert-Native (-not $script:EgState.evidence_integrity_failure) 'ordinary_outcome_evidence_failed'
Assert-Native ($script:OutcomeConstructionCount -eq 1) 'ordinary_outcome_construction_count_invalid'
Assert-Native $script:OutcomeConstructorArgumentsValid 'ordinary_outcome_constructor_arguments_invalid'
$null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
    -ExpectedPath $script:EgExpectedOutcomePath -ExpectedRunId $RunId `
    -ExpectedIntentHash $expectedIntentHash

Write-Output 'file_id_info_query=PASS'
Write-Output 'root_identity=PASS'
Write-Output 'file_identity=PASS'
Write-Output 'link_count=PASS'
Write-Output 'retained_handle_read=PASS'
Write-Output 'outcome_commit_transition=PASS'

$identityControlsRoot = Join-Path $RootPath 'identity-controls'
$alternateOutcomeRoot = Join-Path $identityControlsRoot 'alternate-root'
$crossRoot = Join-Path $identityControlsRoot 'cross-root'
$representationRoot = Join-Path $identityControlsRoot 'representation-long-parent-0123456789'
$junctionTarget = Join-Path $identityControlsRoot 'junction-target'
$junctionPath = Join-Path $identityControlsRoot 'junction-root'
$identityLeaf = $RunId + '.outcome.json'
try {
    [IO.Directory]::CreateDirectory($identityControlsRoot) | Out-Null
    [IO.Directory]::CreateDirectory($alternateOutcomeRoot) | Out-Null
    [IO.Directory]::CreateDirectory($crossRoot) | Out-Null
    [IO.Directory]::CreateDirectory($representationRoot) | Out-Null
    [IO.File]::WriteAllBytes(
        (Join-Path $alternateOutcomeRoot $identityLeaf), $script:LastOutcomeBytes)
    [IO.File]::WriteAllBytes(
        (Join-Path $representationRoot $identityLeaf), $script:LastOutcomeBytes)

    $caseRoot = $ordinaryOutcomeRoot.ToUpperInvariant()
    $null = Assert-OutcomeFile -EvidenceRoot $caseRoot `
        -ExpectedPath (Join-Path $caseRoot $identityLeaf) -ExpectedRunId $RunId `
        -ExpectedIntentHash $expectedIntentHash
    $slashRoot = $ordinaryOutcomeRoot.Replace('\', '/')
    $null = Assert-OutcomeFile -EvidenceRoot $slashRoot `
        -ExpectedPath ($slashRoot + '/' + $identityLeaf) -ExpectedRunId $RunId `
        -ExpectedIntentHash $expectedIntentHash
    $dotRoot = $ordinaryOutcomeRoot + '\.\'
    $null = Assert-OutcomeFile -EvidenceRoot $dotRoot `
        -ExpectedPath (Join-Path $dotRoot $identityLeaf) -ExpectedRunId $RunId `
        -ExpectedIntentHash $expectedIntentHash

    $shortParent = [EgOutcomeIdentity]::GetShortPath($representationRoot)
    if ($shortParent.Succeeded -and
        -not [StringComparer]::OrdinalIgnoreCase.Equals($shortParent.Path, $representationRoot)) {
        $shortExpectedPath = Join-Path $shortParent.Path $identityLeaf
        $null = Assert-OutcomeFile -EvidenceRoot $shortParent.Path `
            -ExpectedPath $shortExpectedPath -ExpectedRunId $RunId `
            -ExpectedIntentHash $expectedIntentHash
        Write-Output 'SHORT_NAME_ALIAS=PASS'
    }
    else {
        Write-Output 'SHORT_NAME_ALIAS=UNAVAILABLE'
    }
    Write-Output 'representation_positives=PASS'

    Assert-EgDistinctDirectoryIdentity -PathA $ordinaryOutcomeRoot -PathB $alternateOutcomeRoot `
        -FailureMarker 'identity_negative_alternate_root'
    Assert-EgDistinctFileIdentity `
        -PathA (Join-Path $ordinaryOutcomeRoot $identityLeaf) `
        -PathB (Join-Path $alternateOutcomeRoot $identityLeaf) `
        -FailureMarker 'identity_negative_identical_bytes_different_object'

    Assert-EgRejected -Action {
        $null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
            -ExpectedPath (Join-Path $ordinaryOutcomeRoot 'EG-OUTCOME-SIBLING-0001.outcome.json') `
            -ExpectedRunId 'EG-OUTCOME-SIBLING-0001' -ExpectedIntentHash $expectedIntentHash
    } -FailureMarker 'identity_negative_sibling_filename'
    Assert-EgRejected -Action {
        $null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
            -ExpectedPath (Join-Path $ordinaryOutcomeRoot 'EG-DIFFERENT-RUN-0001.outcome.json') `
            -ExpectedRunId 'EG-DIFFERENT-RUN-0001' -ExpectedIntentHash $expectedIntentHash
    } -FailureMarker 'identity_negative_different_run_id'

    $missingExpectedPath = Join-Path $ordinaryOutcomeRoot 'EG-MISSING-OUTCOME-0001.outcome.json'
    $missingOpen = [EgOutcomeIdentity]::OpenFile($missingExpectedPath)
    $missingHandle = $missingOpen.Handle
    Assert-Native (-not $missingOpen.Succeeded) 'identity_negative_expected_path_opened'
    if ($null -ne $missingHandle) { $missingHandle.Dispose() }
    Assert-EgRejected -Action {
        $null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
            -ExpectedPath $missingExpectedPath -ExpectedRunId 'EG-MISSING-OUTCOME-0001' `
            -ExpectedIntentHash $expectedIntentHash
    } -FailureMarker 'identity_negative_expected_path_missing'

    $secondOutcomePath = Join-Path $ordinaryOutcomeRoot 'EG-SECOND-OUTCOME-0001.outcome.json'
    [IO.File]::WriteAllBytes($secondOutcomePath, $script:LastOutcomeBytes)
    try {
        Assert-EgRejected -Action {
            $null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
                -ExpectedPath $script:EgExpectedOutcomePath -ExpectedRunId $RunId `
                -ExpectedIntentHash $expectedIntentHash
        } -FailureMarker 'identity_negative_second_outcome_entry'
    }
    finally {
        if (Test-Path -LiteralPath $secondOutcomePath) {
            Remove-Item -LiteralPath $secondOutcomePath -Force -ErrorAction SilentlyContinue
        }
    }

    $sameRootAliasPath = Join-Path $ordinaryOutcomeRoot 'hard-link-alias.bin'
    $sameRootLink = [EgOutcomeIdentity]::CreateHardLink(
        $sameRootAliasPath, $script:EgExpectedOutcomePath)
    Assert-Native $sameRootLink.Succeeded 'identity_negative_same_root_hard_link_create'
    try {
        Assert-EgHardLinkIdentity -OriginalPath $script:EgExpectedOutcomePath `
            -AliasPath $sameRootAliasPath
        Assert-EgRejected -Action {
            $null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
                -ExpectedPath $script:EgExpectedOutcomePath -ExpectedRunId $RunId `
                -ExpectedIntentHash $expectedIntentHash
        } -FailureMarker 'identity_negative_same_root_hard_link'
    }
    finally {
        if (Test-Path -LiteralPath $sameRootAliasPath) {
            Remove-Item -LiteralPath $sameRootAliasPath -Force -ErrorAction SilentlyContinue
        }
    }

    $crossRootLinkPath = Join-Path $crossRoot $identityLeaf
    $crossRootLink = [EgOutcomeIdentity]::CreateHardLink(
        $crossRootLinkPath, $script:EgExpectedOutcomePath)
    Assert-Native $crossRootLink.Succeeded 'identity_negative_cross_root_hard_link_create'
    try {
        Assert-EgCrossRootHardLinkIdentity `
            -ExpectedRootPath $ordinaryOutcomeRoot -DiscoveredRootPath $crossRoot `
            -ExpectedFilePath $script:EgExpectedOutcomePath -DiscoveredFilePath $crossRootLinkPath
        Assert-EgRejected -Action {
            $null = Assert-OutcomeFile -EvidenceRoot $crossRoot `
                -ExpectedPath $crossRootLinkPath -ExpectedRunId $RunId `
                -ExpectedIntentHash $expectedIntentHash
        } -FailureMarker 'identity_negative_cross_root_hard_link'
    }
    finally {
        if (Test-Path -LiteralPath $crossRootLinkPath) {
            Remove-Item -LiteralPath $crossRootLinkPath -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Output 'identity_negatives=PASS'

    [IO.Directory]::CreateDirectory($junctionTarget) | Out-Null
    try {
        New-Item -ItemType Junction -Path $junctionPath -Target $junctionTarget `
            -ErrorAction Stop | Out-Null
    }
    catch { throw 'reparse_junction_creation_failed' }
    $junctionHandle = $null
    try {
        $junctionOpen = [EgOutcomeIdentity]::OpenDirectory($junctionPath)
        $junctionHandle = $junctionOpen.Handle
        Assert-Native ($junctionOpen.Succeeded -and $null -ne $junctionHandle) `
            'reparse_junction_open_failed'
        $junctionInfo = [EgOutcomeIdentity]::QueryInfo($junctionHandle)
        Assert-Native $junctionInfo.Succeeded 'reparse_junction_query_failed'
        Assert-Native (($junctionInfo.FileAttributes -band [uint32]0x00000400) -ne 0) `
            'reparse_junction_attribute_missing'
        Assert-Native (-not (Test-EgDirectoryObjectAttributes -Attributes $junctionInfo.FileAttributes)) `
            'reparse_junction_accepted'
    }
    finally {
        if ($null -ne $junctionHandle) { $junctionHandle.Dispose() }
    }
    Assert-Native (-not (Test-EgFileObjectAttributes -Attributes ([uint32]0x00000400))) `
        'reparse_file_predicate_accepted'
    Write-Output 'reparse_rejection=PASS'
}
finally {
    if (Test-Path -LiteralPath $junctionPath) {
        Remove-Item -LiteralPath $junctionPath -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $junctionTarget) {
        Remove-Item -LiteralPath $junctionTarget -Recurse -Force -ErrorAction SilentlyContinue
    }
    foreach ($cleanupPath in @($crossRoot, $alternateOutcomeRoot, $representationRoot)) {
        if (Test-Path -LiteralPath $cleanupPath) {
            Remove-Item -LiteralPath $cleanupPath -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path -LiteralPath $identityControlsRoot) {
        Remove-Item -LiteralPath $identityControlsRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$script:OutcomeSeamEnabled = $false
Remove-Item -LiteralPath $ordinaryOutcomeRoot -Recurse -Force
Assert-Native (-not (Test-Path -LiteralPath $ordinaryOutcomeRoot)) 'ordinary_outcome_cleanup_failed'

Write-Output 'function_case=timed_out_outcome_defensive_exact_caller'
$timeoutOutcomeRoot = Join-Path $RootPath 'defensive-timeout-outcome'
[IO.Directory]::CreateDirectory($timeoutOutcomeRoot) | Out-Null
$RunId = 'EG-OUTCOME-TIMEOUT-0001'
$script:EgEvidenceRootNormal = $timeoutOutcomeRoot
$script:EgExpectedOutcomePath = Join-Path $timeoutOutcomeRoot ($RunId + '.outcome.json')
$script:EgState = New-State
$script:EgState.creation_attempted = $true
Write-EgOutcomeWithTimeoutControl -StartVerdict 'STARTED_PROVEN'
Assert-Native $script:EgState.outcome_write_attempted 'defensive_outcome_write_not_attempted'
Assert-Native $script:EgState.evidence_integrity_failure 'defensive_outcome_timeout_not_rejected'
Assert-Native (-not $script:EgState.outcome_committed) 'defensive_outcome_timeout_committed'
Assert-Native ($script:EgState.support_ref -ceq 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED') `
    'defensive_outcome_timeout_support_ref_invalid'
Assert-Native (Test-Path -LiteralPath $script:EgExpectedOutcomePath) `
    'defensive_outcome_timeout_file_not_created'
Remove-Item -LiteralPath $timeoutOutcomeRoot -Recurse -Force
Write-Output 'defensive_timeout_caller=PASS support_ref=EG_SUPERVISOR_OUTCOME_FLUSH_FAILED'

Write-Output 'function_case=descendant_grace_stale_zero_global_deadline'
$staleZeroDeadlineJob = [IntPtr]::Zero
$staleZeroDeadlineProcess = $null
try {
    $staleZeroDeadlineJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($staleZeroDeadlineJob)
    $staleZeroDeadlineProcess = New-TestChild -JobHandle $staleZeroDeadlineJob `
        -Mode 'short'
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'stale_zero_deadline_intent_failed'
    $staleZeroResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $staleZeroDeadlineProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($staleZeroResume.Attempted -and $staleZeroResume.Accepted) 'stale_zero_deadline_resume_failed'
    $staleZeroDeadline = Future-Deadline -Milliseconds 1000
    $staleZeroWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $staleZeroDeadlineProcess.ProcessHandle, 10000)
    Assert-Native ($staleZeroWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) `
        'stale_zero_deadline_launcher_wait_failed'
    $staleZeroAccounting = Wait-JobZero -JobHandle $staleZeroDeadlineJob
    Assert-Native ($staleZeroAccounting.ActiveProcesses -eq 0) 'stale_zero_deadline_not_zero'
    Assert-Native ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $staleZeroDeadline) `
        'stale_zero_deadline_zero_after_deadline'
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $staleZeroDeadline) {
        Microsoft.PowerShell.Utility\Start-Sleep -Milliseconds 50
    }
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $staleZeroDeadlineJob `
        -LauncherHandle $staleZeroDeadlineProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks $staleZeroDeadline
    Assert-Native (-not $script:EgState.termination_started) 'stale_zero_deadline_terminated'
    Assert-Native ($script:EgState.active_processes -eq 0) 'stale_zero_deadline_active_processes_nonzero'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted -and
        -not $script:EgState.descendant_grace_expired) 'stale_zero_deadline_flags_set'
}
finally {
    Cleanup-Job -JobHandle $staleZeroDeadlineJob -Process $staleZeroDeadlineProcess
}

Write-Output 'function_case=descendant_grace_stale_zero_interruption'
$staleZeroInterruptJob = [IntPtr]::Zero
$staleZeroInterruptProcess = $null
try {
    $staleZeroInterruptJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($staleZeroInterruptJob)
    $staleZeroInterruptProcess = New-TestChild -JobHandle $staleZeroInterruptJob `
        -Mode 'short'
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'stale_zero_interrupt_intent_failed'
    $staleZeroInterruptResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $staleZeroInterruptProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($staleZeroInterruptResume.Attempted -and $staleZeroInterruptResume.Accepted) `
        'stale_zero_interrupt_resume_failed'
    $staleZeroInterruptWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $staleZeroInterruptProcess.ProcessHandle, 10000)
    Assert-Native ($staleZeroInterruptWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) `
        'stale_zero_interrupt_launcher_wait_failed'
    $staleZeroInterruptAccounting = Wait-JobZero -JobHandle $staleZeroInterruptJob
    Assert-Native ($staleZeroInterruptAccounting.ActiveProcesses -eq 0) 'stale_zero_interrupt_not_zero'
    $staleZeroSignalMethod = [EnergyGridOneShotSupervisorNative].GetMethod(
        'HandleConsoleSignal', [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Static)
    Assert-Native ($null -ne $staleZeroSignalMethod) 'stale_zero_interrupt_signal_method_missing'
    [void]$staleZeroSignalMethod.Invoke($null, [object[]]@([uint32]2))
    Assert-Native ([EnergyGridOneShotSupervisorNative]::IsTerminationRequested) `
        'stale_zero_interrupt_signal_not_pending'
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $staleZeroInterruptJob `
        -LauncherHandle $staleZeroInterruptProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline)
    Assert-Native (-not $script:EgState.termination_started) 'stale_zero_interrupt_terminated'
    Assert-Native ($script:EgState.active_processes -eq 0) 'stale_zero_interrupt_active_processes_nonzero'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted -and
        -not $script:EgState.descendant_grace_expired) 'stale_zero_interrupt_flags_set'
}
finally {
    Cleanup-Job -JobHandle $staleZeroInterruptJob -Process $staleZeroInterruptProcess
}

Write-Output 'function_case=descendant_grace_overall_deadline'
$graceDeadlineJob = [IntPtr]::Zero
$graceDeadlineProcess = $null
try {
    $graceDeadlineJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($graceDeadlineJob)
    $descendantMode = 'descendant'
    $graceDeadlineProcess = New-TestChild -JobHandle $graceDeadlineJob -Mode $descendantMode
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'grace_deadline_intent_failed'
    $graceResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $graceDeadlineProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($graceResume.Attempted -and $graceResume.Accepted) 'grace_deadline_resume_failed'
    $launcherWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $graceDeadlineProcess.ProcessHandle, 10000)
    Assert-Native ($launcherWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) `
        'grace_launcher_did_not_signal'
    $accounting = $null
    $accountingDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency)
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $accountingDeadline) {
        $accounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($graceDeadlineJob)
        Assert-Native $accounting.Succeeded 'grace_deadline_accounting_failed'
        if ($accounting.ActiveProcesses -gt 0) { break }
        Microsoft.PowerShell.Utility\Start-Sleep -Milliseconds 50
    }
    Assert-Native ($null -ne $accounting -and $accounting.ActiveProcesses -gt 0) 'grace_descendant_missing'
    $script:EgState = New-State -OutcomeCommitted $true
    $script:EgState.active_processes = [uint64]0
    Wait-EgDescendantGrace -JobHandle $graceDeadlineJob `
        -LauncherHandle $graceDeadlineProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 200)
    Assert-Native $script:EgState.timed_out 'grace_deadline_not_timeout'
    Assert-Native (-not $script:EgState.interrupted -and -not $script:EgState.descendant_grace_expired) `
        'grace_deadline_reason_flags_invalid'
    Assert-Native $script:EgState.termination_succeeded 'grace_deadline_termination_failed'
    Wait-EgReap -JobHandle $graceDeadlineJob -DeadlineTicks 0 -WindowSeconds 10
    Assert-Native ((Get-EgExitCode) -eq 2) 'grace_deadline_exit_not_two'
}
finally {
    Cleanup-Job -JobHandle $graceDeadlineJob -Process $graceDeadlineProcess
}

Write-Output 'function_case=descendant_grace_interruption_during_polling'
$graceInterruptJob = [IntPtr]::Zero
$graceInterruptProcess = $null
$graceInterruptThread = $null
try {
    $graceInterruptJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($graceInterruptJob)
    $graceInterruptProcess = New-TestChild -JobHandle $graceInterruptJob -Mode $descendantMode
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'grace_interrupt_intent_failed'
    $graceInterruptResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $graceInterruptProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($graceInterruptResume.Attempted -and $graceInterruptResume.Accepted) `
        'grace_interrupt_resume_failed'
    $graceInterruptWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $graceInterruptProcess.ProcessHandle, 10000)
    Assert-Native ($graceInterruptWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) `
        'grace_interrupt_launcher_wait_failed'
    $graceInterruptAccounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($graceInterruptJob)
    Assert-Native ($graceInterruptAccounting.Succeeded -and $graceInterruptAccounting.ActiveProcesses -gt 0) `
        'grace_interrupt_descendant_missing'
    $script:EgState = New-State -OutcomeCommitted $true
    $script:EgState.active_processes = [uint64]1
    $graceInterruptThread = [EnergyGridGraceInterruptSchedulerForFunctionTest]::Schedule(250)
    Wait-EgDescendantGrace -JobHandle $graceInterruptJob `
        -LauncherHandle $graceInterruptProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 30000)
    Assert-Native ($graceInterruptThread.Join(5000)) 'grace_interrupt_scheduler_join_failed'
    Assert-Native (-not $graceInterruptThread.IsAlive) 'grace_interrupt_scheduler_still_running'
    Assert-Native $script:EgState.interrupted 'grace_interruption_not_recorded'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.descendant_grace_expired) `
        'grace_interruption_reason_flags_invalid'
    Assert-Native $script:EgState.termination_succeeded 'grace_interruption_termination_failed'
    Wait-EgReap -JobHandle $graceInterruptJob -DeadlineTicks 0 -WindowSeconds 10
    Assert-Native ((Get-EgExitCode) -eq 2) 'grace_interruption_exit_not_two'
}
finally {
    Cleanup-Job -JobHandle $graceInterruptJob -Process $graceInterruptProcess
    if ($null -ne $graceInterruptThread -and $graceInterruptThread.IsAlive) { [void]$graceInterruptThread.Join(5000) }
}

Write-Output 'function_case=descendant_grace_local_control'
$localGraceJob = [IntPtr]::Zero
$localGraceProcess = $null
try {
    $localGraceJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($localGraceJob)
    $localGraceProcess = New-TestChild -JobHandle $localGraceJob -Mode $descendantMode
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'local_grace_intent_failed'
    $localGraceResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $localGraceProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($localGraceResume.Attempted -and $localGraceResume.Accepted) 'local_grace_resume_failed'
    $localGraceWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $localGraceProcess.ProcessHandle, 10000)
    Assert-Native ($localGraceWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) `
        'local_grace_launcher_wait_failed'
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $localGraceJob `
        -LauncherHandle $localGraceProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 30000)
    Assert-Native $script:EgState.descendant_grace_expired 'local_grace_not_recorded'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted) `
        'local_grace_reason_flags_invalid'
    Assert-Native $script:EgState.termination_succeeded 'local_grace_termination_failed'
    Wait-EgReap -JobHandle $localGraceJob -DeadlineTicks 0 -WindowSeconds 10
}
finally {
    Cleanup-Job -JobHandle $localGraceJob -Process $localGraceProcess
}

Write-Output 'function_case=descendant_grace_completion_global_deadline'
$globalCompletionJob = [IntPtr]::Zero
$globalCompletionProcess = $null
$globalCompletionReleasePath = Join-Path $RootPath 'completion-global.release'
try {
    $globalCompletionJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($globalCompletionJob)
    $globalCompletionProcess = New-TestChild -JobHandle $globalCompletionJob -Mode 'wait-release' -ReleasePath $globalCompletionReleasePath
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'global_completion_intent_failed'
    $globalCompletionResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $globalCompletionProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($globalCompletionResume.Attempted -and $globalCompletionResume.Accepted) `
        'global_completion_resume_failed'
    $globalCompletionInitial = [EnergyGridOneShotSupervisorNative]::GetAccounting($globalCompletionJob)
    Assert-Native ($globalCompletionInitial.Succeeded -and $globalCompletionInitial.ActiveProcesses -gt 0) `
        'global_completion_initial_positive_missing'
    $script:GraceSleepMode = 'global-completion'
    $script:GraceSleepCalls = 0
    $script:GraceSleepJobHandle = $globalCompletionJob
    $script:GraceSleepReleasePath = $globalCompletionReleasePath
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $globalCompletionJob `
        -LauncherHandle $globalCompletionProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 200)
    $script:GraceSleepMode = 'off'
    Assert-Native ($script:GraceSleepCalls -eq 1) 'global_completion_sleep_seam_not_used'
    Assert-Native (-not $script:EgState.termination_started) 'global_completion_terminated'
    Assert-Native ($script:EgState.active_processes -eq 0) 'global_completion_active_processes_nonzero'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted -and
        -not $script:EgState.descendant_grace_expired) 'global_completion_flags_set'
}
finally {
    $script:GraceSleepMode = 'off'
    Cleanup-Job -JobHandle $globalCompletionJob -Process $globalCompletionProcess
    if (Test-Path -LiteralPath $globalCompletionReleasePath) { Remove-Item -LiteralPath $globalCompletionReleasePath -Force }
}

Write-Output 'function_case=descendant_grace_completion_local_grace'
$localCompletionJob = [IntPtr]::Zero
$localCompletionProcess = $null
$localCompletionReleasePath = Join-Path $RootPath 'completion-local.release'
try {
    $localCompletionJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($localCompletionJob)
    $localCompletionProcess = New-TestChild -JobHandle $localCompletionJob -Mode 'wait-release' -ReleasePath $localCompletionReleasePath
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'local_completion_intent_failed'
    $localCompletionResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $localCompletionProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($localCompletionResume.Attempted -and $localCompletionResume.Accepted) `
        'local_completion_resume_failed'
    $localCompletionInitial = [EnergyGridOneShotSupervisorNative]::GetAccounting($localCompletionJob)
    Assert-Native ($localCompletionInitial.Succeeded -and $localCompletionInitial.ActiveProcesses -gt 0) `
        'local_completion_initial_positive_missing'
    $script:GraceSleepMode = 'local-completion'
    $script:GraceSleepCalls = 0
    $script:GraceSleepJobHandle = $localCompletionJob
    $script:GraceSleepReleasePath = $localCompletionReleasePath
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $localCompletionJob `
        -LauncherHandle $localCompletionProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 30000)
    $script:GraceSleepMode = 'off'
    Assert-Native ($script:GraceSleepCalls -eq 1) 'local_completion_sleep_seam_not_used'
    Assert-Native (-not $script:EgState.termination_started) 'local_completion_terminated'
    Assert-Native ($script:EgState.active_processes -eq 0) 'local_completion_active_processes_nonzero'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted -and
        -not $script:EgState.descendant_grace_expired) 'local_completion_flags_set'
}
finally {
    $script:GraceSleepMode = 'off'
    Cleanup-Job -JobHandle $localCompletionJob -Process $localCompletionProcess
    if (Test-Path -LiteralPath $localCompletionReleasePath) { Remove-Item -LiteralPath $localCompletionReleasePath -Force }
}

Write-Output 'function_case=descendant_grace_accounting_failure'
[EnergyGridOneShotSupervisorNative]::ResetControlState()
$accountingFailureState = New-State -OutcomeCommitted $true
$script:EgState = $accountingFailureState
$accountingFailureThrown = $false
try {
    Wait-EgDescendantGrace -JobHandle ([IntPtr]([int64]1)) `
        -LauncherHandle ([IntPtr]::Zero) -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline)
}
catch {
    $accountingFailureThrown = $true
    Assert-Native ($_.Exception.Message -eq 'EG_SUPERVISOR_ACCOUNTING_FAILED') `
        'accounting_failure_support_ref_invalid'
}
Assert-Native $accountingFailureThrown 'accounting_failure_not_thrown'
Assert-Native $script:EgState.containment_failure 'accounting_failure_not_containment_failure'
Assert-Native ($script:EgState.support_ref -eq 'EG_SUPERVISOR_ACCOUNTING_FAILED') `
    'accounting_failure_state_support_ref_invalid'
Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted -and
    -not $script:EgState.descendant_grace_expired) 'accounting_failure_timing_flags_set'
Assert-Native ((Get-EgExitCode) -eq 3) 'accounting_failure_exit_not_three'

Write-Output 'function_case=descendant_grace_precedence_interruption_over_timeout'
$precedenceInterruptJob = [IntPtr]::Zero
$precedenceInterruptProcess = $null
try {
    $precedenceInterruptJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($precedenceInterruptJob)
    $precedenceInterruptProcess = New-TestChild -JobHandle $precedenceInterruptJob
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    $precedenceSignalMethod = [EnergyGridOneShotSupervisorNative].GetMethod(
        'HandleConsoleSignal', [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Static)
    Assert-Native ($null -ne $precedenceSignalMethod) 'precedence_interrupt_signal_method_missing'
    [void]$precedenceSignalMethod.Invoke($null, [object[]]@([uint32]2))
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]0
    Wait-EgDescendantGrace -JobHandle $precedenceInterruptJob `
        -LauncherHandle $precedenceInterruptProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks ([System.Diagnostics.Stopwatch]::GetTimestamp() - 1)
    Assert-Native $script:EgState.interrupted 'precedence_interrupt_not_selected'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.descendant_grace_expired) `
        'precedence_interrupt_lost'
    Wait-EgReap -JobHandle $precedenceInterruptJob -DeadlineTicks 0 -WindowSeconds 10
}
finally {
    Cleanup-Job -JobHandle $precedenceInterruptJob -Process $precedenceInterruptProcess
}

Write-Output 'function_case=descendant_grace_precedence_timeout_over_local'
$precedenceTimeoutJob = [IntPtr]::Zero
$precedenceTimeoutProcess = $null
try {
    $precedenceTimeoutJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($precedenceTimeoutJob)
    $precedenceTimeoutProcess = New-TestChild -JobHandle $precedenceTimeoutJob
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    $script:GraceSleepMode = 'timeout-local-precedence'
    $script:GraceSleepCalls = 0
    $script:GraceSleepJobHandle = $precedenceTimeoutJob
    $script:GraceSleepReleasePath = $null
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $precedenceTimeoutJob `
        -LauncherHandle $precedenceTimeoutProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 5100)
    $script:GraceSleepMode = 'off'
    Assert-Native ($script:GraceSleepCalls -eq 1) 'precedence_timeout_sleep_seam_not_used'
    Assert-Native $script:EgState.timed_out 'precedence_timeout_not_selected'
    Assert-Native (-not $script:EgState.interrupted -and -not $script:EgState.descendant_grace_expired) `
        'precedence_timeout_lost_to_local_grace'
    Wait-EgReap -JobHandle $precedenceTimeoutJob -DeadlineTicks 0 -WindowSeconds 10
}
finally {
    $script:GraceSleepMode = 'off'
    Cleanup-Job -JobHandle $precedenceTimeoutJob -Process $precedenceTimeoutProcess
}

Write-Output 'function_case=exact_termination_function'
$job = [IntPtr]::Zero
$process = $null
try {
    $job = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($job)
    $process = New-TestChild -JobHandle $job
    $script:EgState = New-State
    Invoke-EgTerminateJob -JobHandle $job -Reason 'TIMEOUT'
    Assert-Native $script:EgState.termination_started 'termination_not_started'
    Assert-Native $script:EgState.termination_succeeded 'termination_not_succeeded'
    Assert-Native (-not $script:EgState.termination_failure) 'termination_reported_failure'
    Assert-Native $script:EgState.timed_out 'timeout_not_recorded'
    $wait = [EnergyGridOneShotSupervisorNative]::WaitProcess($process.ProcessHandle, 10000)
    Assert-Native ($wait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) 'termination_wait_failed'
    $final = Wait-JobZero -JobHandle $job
    $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($process.ProcessHandle)
    Assert-Native ($live.Succeeded -and $live.ExitCode -eq [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE) 'exact_termination_code_missing'
    Assert-Native ($final.ActiveProcesses -eq 0) 'termination_reap_missing'
}
finally {
    try {
        if ($job -ne [IntPtr]::Zero) {
            [void][EnergyGridOneShotSupervisorNative]::TerminateJob(
                $job, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        }
    }
    catch { }
    try { Close-TestChild -Process $process } catch { }
    Close-Native -Handle $job
}

$script:EgState = New-State
Invoke-EgTerminateJob -JobHandle ([IntPtr]::Zero) -Reason 'POST_CREATE_FAILURE'
Assert-Native $script:EgState.termination_failure 'termination_failure_not_observed'
Assert-Native $script:EgState.containment_failure 'termination_failure_not_infrastructure'
Write-Output 'function_case=termination_failure_infrastructure'

Write-Output 'function_case=accounting_failure_and_reap_timeout'
$accountingState = New-State
$script:EgState = $accountingState
try { Get-EgAccounting -JobHandle ([IntPtr]([int64]1)) } catch { }
Assert-Native $script:EgState.containment_failure 'accounting_failure_not_containment_failure'

$reapJob = [IntPtr]::Zero
$reapProcess = $null
try {
    $reapJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($reapJob)
    $reapProcess = New-TestChild -JobHandle $reapJob
    $script:EgState = New-State -OutcomeCommitted $true
    $script:EgState.reap_confirmed = $false
    Wait-EgReap -JobHandle $reapJob -DeadlineTicks 0 -WindowSeconds 0
    Assert-Native (-not $script:EgState.reap_confirmed) 'reap_timeout_was_confirmed'
    Assert-Native ((Get-EgExitCode) -eq 3) 'reap_timeout_exit_not_three'
}
finally {
    try {
        if ($reapJob -ne [IntPtr]::Zero) {
            [void][EnergyGridOneShotSupervisorNative]::TerminateJob(
                $reapJob, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        }
    }
    catch { }
    try { Close-TestChild -Process $reapProcess } catch { }
    Close-Native -Handle $reapJob
}

function Assert-ExitCase {
    param([string]$Name, [int]$Expected)
    $actual = Get-EgExitCode
    Assert-Native ($actual -eq $Expected) ($Name + '_expected_' + $Expected + '_actual_' + $actual)
    Write-Output ('exit_case=' + $Name + '=' + $actual)
}

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.creation_succeeded = $false
$script:EgState.reap_confirmed = $false
$script:EgState.start_verdict = 'NOT_STARTED_PROVEN'
Assert-ExitCase -Name 'durable_precreation_rejection' -Expected 1

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.timed_out = $true
$script:EgState.start_verdict = 'STARTED_PROVEN'
$script:EgState.launcher_exit_code = 0
Assert-ExitCase -Name 'durable_timeout_successful_reap' -Expected 2

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.timed_out = $true
$script:EgState.containment_failure = $true
Assert-ExitCase -Name 'timeout_containment_failure' -Expected 3

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.timed_out = $true
$script:EgState.reap_confirmed = $false
Assert-ExitCase -Name 'timeout_reap_unconfirmed' -Expected 3

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.start_verdict = 'AMBIGUOUS'
Assert-ExitCase -Name 'durable_ambiguous' -Expected 4

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.start_verdict = 'STARTED_PROVEN'
$script:EgState.launcher_exit_code = 0
Assert-ExitCase -Name 'started_proven_launcher_zero_complete' -Expected 0

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.start_verdict = 'STARTED_PROVEN'
$script:EgState.launcher_exit_code = 17
Assert-ExitCase -Name 'nonzero_launcher_nonambiguous' -Expected 1

$script:EgState = New-State
$script:EgState.outcome_committed = $false
Assert-ExitCase -Name 'outcome_missing' -Expected 3

Write-Output 'function_assurance_cases=26'
Write-Output 'function_assurance=PASS'
}

function Assert-EgObserver {
    param([bool]$Condition, [string]$Message)
    Assert-EgHarness $Condition ('observer:' + $Message)
}

function Close-EgObserverHandle {
    param([IntPtr]$Handle)
    if ($Handle -ne [IntPtr]::Zero) {
        $closed = [EnergyGridOneShotSupervisorNative]::CloseHandleChecked($Handle)
        Assert-EgObserver $closed.Success ('handle_close:' + $closed.ErrorCode)
    }
}

function New-EgObserverState {
    $script:EgState = [pscustomobject]@{
        evidence_integrity_failure = $false
        containment_failure = $false
        observer_failed = $false
        creation_attempted = $true
        creation_succeeded = $true
        baseline_total_processes = [uint64]1
        baseline_active_processes = [uint64]1
        total_processes = [uint64]0
        active_processes = [uint64]0
        application_child_observed = $false
        application_child_observation_elapsed_ms = $null
        reap_confirmed = $false
        intent_committed = $true
        timed_out = $false
        interrupted = $false
        termination_started = $false
        termination_succeeded = $false
        termination_failure = $false
        drain_failure = $false
        stdout_complete = $true
        stderr_complete = $true
        launcher_exit_code = 0
        start_verdict = 'AMBIGUOUS'
        support_ref = 'EG_HARNESS_OBSERVER'
        error_code = $null
    }
}

function New-EgObserverConfig {
    param([string]$Name, [string]$Mode, [hashtable]$Values = @{})
    $document = [ordered]@{
        module_sha256 = (Get-FileHash -LiteralPath $PythonModulePath -Algorithm SHA256).Hash.ToLowerInvariant()
        mode = $Mode
    }
    $path = Join-Path $RootPath ($Name + '-' + [guid]::NewGuid().ToString('N') + '.json')
    foreach ($key in $Values.Keys) { $document[$key] = $Values[$key] }
    if ($Mode -ceq 'wrong-image') {
        $canonicalExecutable = [string]$Values.canonical_command_line_executable
        Assert-EgObserver (-not [string]::IsNullOrWhiteSpace($canonicalExecutable)) `
            'wrong_image_canonical_executable_missing'
    }
    [IO.File]::WriteAllText(
        $path, ($document | ConvertTo-Json -Depth 8 -Compress), (New-Object System.Text.UTF8Encoding($false)))
    return $path
}

function New-EgObserverProcess {
    param([IntPtr]$JobHandle, [string[]]$Argument)
    $pipes = [EnergyGridOneShotSupervisorNative]::CreatePipes()
    $attributes = $null
    try {
        $attributes = New-Object EnergyGridOneShotSupervisorNative+AttributeResources(
            $JobHandle, $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite,
            $pipes.LauncherStderrWrite)
        $line = ConvertTo-EgNativeCommandLine -Argument $Argument
        $builder = New-Object System.Text.StringBuilder($line)
        $created = [EnergyGridOneShotSupervisorNative]::CreateContainedProcess(
            $script:NativePowerShell, $builder, $RepositoryRoot, $attributes.AttributeList,
            $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite, $pipes.LauncherStderrWrite)
        Assert-EgObserver $created.Succeeded ('launcher_create:' + $created.ErrorCode)
        $attributes.Dispose()
        $attributes = $null
        Close-EgObserverHandle -Handle $pipes.LauncherStdinRead
        $pipes.LauncherStdinRead = [IntPtr]::Zero
        Close-EgObserverHandle -Handle $pipes.LauncherStdoutWrite
        $pipes.LauncherStdoutWrite = [IntPtr]::Zero
        Close-EgObserverHandle -Handle $pipes.LauncherStderrWrite
        $pipes.LauncherStderrWrite = [IntPtr]::Zero
        $stdoutDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStdoutRead)
        $pipes.SupervisorStdoutRead = [IntPtr]::Zero
        $stderrDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStderrRead)
        $pipes.SupervisorStderrRead = [IntPtr]::Zero
        Close-EgObserverHandle -Handle $pipes.SupervisorStdinWrite
        $pipes.SupervisorStdinWrite = [IntPtr]::Zero
        return [pscustomobject]@{
            ProcessHandle = $created.ProcessInfo.hProcess
            ThreadHandle = $created.ProcessInfo.hThread
            ProcessId = [uint32]$created.ProcessInfo.dwProcessId
            StdoutDrain = $stdoutDrain
            StderrDrain = $stderrDrain
        }
    }
    catch {
        if ($null -ne $attributes) { $attributes.Dispose() }
        if ($null -ne $pipes) {
            Close-EgObserverHandle -Handle $pipes.LauncherStdinRead
            Close-EgObserverHandle -Handle $pipes.SupervisorStdinWrite
            Close-EgObserverHandle -Handle $pipes.SupervisorStdoutRead
            Close-EgObserverHandle -Handle $pipes.LauncherStdoutWrite
            Close-EgObserverHandle -Handle $pipes.SupervisorStderrRead
            Close-EgObserverHandle -Handle $pipes.LauncherStderrWrite
        }
        throw
    }
}

function New-EgObserverLauncherArguments {
    param([string]$ConfigPath, [string]$PythonExecutable, [string]$RunId)
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    return @(
        $script:NativePowerShell, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', $LauncherPath,
        '-ConfigPath', $ConfigPath, '-PythonExe', $PythonExecutable,
        '-CheckoutRoot', $RepositoryRoot,
        '-CredentialPath', (Join-Path $RootPath 'credential-placeholder'),
        '-BrowserCachePath', (Join-Path $RootPath 'browser-cache-placeholder'),
        '-ExpectedBranch', 'codex/energygrid-226-dual-stream-latest-email',
        '-AuthorisedLauncherRootWriteSid', $sid, '-Command', 'run',
        '-LogRoot', $RootPath, '-RunId', $RunId
    )
}

function Start-EgObserverRuntime {
    param(
        [string]$Name,
        [string]$Mode,
        [hashtable]$Values = @{},
        [string]$PythonExecutable = $script:EgPythonExeNormal
    )
    $job = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($job)
    $readyPath = Join-Path $RootPath ($Name + '-ready-' + [guid]::NewGuid().ToString('N') + '.json')
    if (-not $Values.ContainsKey('ready_path') -and $Mode -notin @('idle', 'launcher-exit')) {
        $Values.ready_path = $readyPath
    }
    $configPath = New-EgObserverConfig -Name $Name -Mode $Mode -Values $Values
    $startTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
    $launcher = New-EgObserverProcess -JobHandle $job -Argument (
        New-EgObserverLauncherArguments -ConfigPath $configPath -PythonExecutable $PythonExecutable `
            -RunId ([guid]::NewGuid().ToString().ToLowerInvariant()))
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-EgObserver ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'intent_commit'
    $resume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $launcher.ThreadHandle, (Get-EgDeadlineTicks -StartTicks $startTicks -Seconds 120))
    Assert-EgObserver ($resume.Accepted -and $resume.ReturnValue -eq 1) 'launcher_resume'
    return [pscustomobject]@{
        JobHandle = $job
        Launcher = $launcher
        ReadyPath = $readyPath
        ConfigPath = $configPath
        StartTicks = $startTicks
    }
}

function Wait-EgObserverFile {
    param([string]$Path, [int]$TimeoutMilliseconds = 30000, [IntPtr]$ProcessHandle = [IntPtr]::Zero)
    $deadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]$TimeoutMilliseconds * [int64][System.Diagnostics.Stopwatch]::Frequency / 1000)
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $deadline) {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
        }
        if ($ProcessHandle -ne [IntPtr]::Zero) {
            $wait = [EnergyGridOneShotSupervisorNative]::WaitProcess($ProcessHandle, 0)
            if ($wait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) {
                throw 'EG_HARNESS:observer_launcher_exited_before_marker'
            }
        }
        Start-Sleep -Milliseconds 20
    }
    throw 'EG_HARNESS:observer_marker_timeout'
}

function Get-EgObserverProcessEvidence {
    param([uint32]$ProcessId)
    $candidate = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess($ProcessId)
    Assert-EgObserver $candidate.Succeeded ('candidate_open:' + $ProcessId + ':' + $candidate.ErrorCode)
    try {
        $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
        $image = [EnergyGridOneShotSupervisorNative]::GetImage($candidate.Handle)
        $metadata = [EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(
            $ProcessId, $script:EgObserverMetadataTimeoutMilliseconds)
        return [pscustomobject]@{ Live = $live; Image = $image; Metadata = $metadata }
    }
    finally { Close-EgObserverHandle -Handle $candidate.Handle }
}

function Resume-EgObserverRuntime {
    param($Runtime)
    $launcher = $Runtime.Launcher
    $resume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $launcher.ThreadHandle, (Get-EgDeadlineTicks -StartTicks $Runtime.StartTicks -Seconds 120))
    Assert-EgObserver ($resume.Accepted -and $resume.ReturnValue -eq 1) 'launcher_resume'
}

function Close-EgObserverRuntime {
    param($Runtime)
    if ($null -eq $Runtime) { return $null }
    if ($Runtime.JobHandle -ne [IntPtr]::Zero) {
        $terminated = [EnergyGridOneShotSupervisorNative]::TerminateJob(
            $Runtime.JobHandle, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        Assert-EgObserver $terminated.Success ('job_terminate:' + $terminated.ErrorCode)
        $deadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
            ([int64]10 * [int64][System.Diagnostics.Stopwatch]::Frequency)
        do {
            $accounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($Runtime.JobHandle)
            Assert-EgObserver $accounting.Succeeded ('job_accounting:' + $accounting.ErrorCode)
            if ($accounting.ActiveProcesses -eq 0) { break }
            Start-Sleep -Milliseconds 50
        } while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $deadline)
        Assert-EgObserver ($accounting.ActiveProcesses -eq 0) 'job_reap_timeout'
        $launcher = $Runtime.Launcher
        [void]$launcher.StdoutDrain.Join(10000)
        [void]$launcher.StderrDrain.Join(10000)
        Close-EgObserverHandle -Handle $launcher.ThreadHandle
        Close-EgObserverHandle -Handle $launcher.ProcessHandle
        Close-EgObserverHandle -Handle $Runtime.JobHandle
        $Runtime.JobHandle = [IntPtr]::Zero
        return $accounting
    }
    return $null
}

function Add-EgObserverInjectedPid {
    param([IntPtr]$JobHandle, [uint32]$ProcessId)
    $source = [EnergyGridOneShotSupervisorNative]::GetProcessIds($JobHandle)
    if (-not $source.Succeeded) { return $source }
    $ids = New-Object 'System.Collections.Generic.List[uint32]'
    $ids.Add($ProcessId)
    foreach ($pidValue in $source.ProcessIds) { $ids.Add([uint32]$pidValue) }
    return [pscustomobject]@{
        Succeeded = $true
        ErrorCode = 0
        ProcessIds = $ids.ToArray()
    }
}

function Get-EgN4bProcessIds {
    param([IntPtr]$JobHandle)
    return Add-EgObserverInjectedPid -JobHandle $JobHandle -ProcessId ([uint32]$script:EgObserverInjectedPid)
}

function Get-EgN5ProcessIds {
    param([IntPtr]$JobHandle)
    return Add-EgObserverInjectedPid -JobHandle $JobHandle -ProcessId ([uint32]4294967292)
}

function Get-EgN10ProcessIds {
    param([IntPtr]$JobHandle)
    return Add-EgObserverInjectedPid -JobHandle $JobHandle -ProcessId ([uint32]4)
}

function Get-EgN11ProcessMetadata {
    param([uint32]$ProcessId, [int]$TimeoutMilliseconds)
    if ($ProcessId -eq [uint32]$script:EgObserverInjectedPid) {
        return [pscustomobject]@{
            Succeeded = $false
            TimedOut = $false
            ProviderFailed = $false
            ParentProcessId = [uint32]0
            CommandLine = ''
        }
    }
    return [EnergyGridOneShotSupervisorNative]::QueryProcessMetadata($ProcessId, $TimeoutMilliseconds)
}

function Get-EgN7ProcessMetadata {
    param([uint32]$ProcessId, [int]$TimeoutMilliseconds)
    $script:EgN7LastMetadata = [EnergyGridOneShotSupervisorN7ProviderControl]::QueryProcessMetadata(
        $ProcessId, $TimeoutMilliseconds)
    return $script:EgN7LastMetadata
}

function Invoke-EgObserverFunction {
    param([string]$Name, $Runtime, [long]$StartTicks)
    switch ($Name) {
        'base' {
            return [bool](Test-EgApplicationChild -JobHandle $Runtime.JobHandle `
                -LauncherHandle $Runtime.Launcher.ProcessHandle -LauncherPid $Runtime.Launcher.ProcessId `
                -StartTicks $StartTicks)
        }
        'N4b' {
            return [bool](Test-EgN4bApplicationChild -JobHandle $Runtime.JobHandle `
                -LauncherHandle $Runtime.Launcher.ProcessHandle -LauncherPid $Runtime.Launcher.ProcessId `
                -StartTicks $StartTicks)
        }
        'N5' {
            return [bool](Test-EgN5ApplicationChild -JobHandle $Runtime.JobHandle `
                -LauncherHandle $Runtime.Launcher.ProcessHandle -LauncherPid $Runtime.Launcher.ProcessId `
                -StartTicks $StartTicks)
        }
        'N7' {
            return [bool](Test-EgN7ApplicationChild -JobHandle $Runtime.JobHandle `
                -LauncherHandle $Runtime.Launcher.ProcessHandle -LauncherPid $Runtime.Launcher.ProcessId `
                -StartTicks $StartTicks)
        }
        'N10' {
            return [bool](Test-EgN10ApplicationChild -JobHandle $Runtime.JobHandle `
                -LauncherHandle $Runtime.Launcher.ProcessHandle -LauncherPid $Runtime.Launcher.ProcessId `
                -StartTicks $StartTicks)
        }
        'N11' {
            return [bool](Test-EgN11ApplicationChild -JobHandle $Runtime.JobHandle `
                -LauncherHandle $Runtime.Launcher.ProcessHandle -LauncherPid $Runtime.Launcher.ProcessId `
                -StartTicks $StartTicks)
        }
        'N13' {
            return [bool](Test-EgN13ApplicationChild -JobHandle $Runtime.JobHandle `
                -LauncherHandle $Runtime.Launcher.ProcessHandle -LauncherPid $Runtime.Launcher.ProcessId `
                -StartTicks $StartTicks)
        }
        default { throw ('EG_HARNESS:observer_function_invalid:' + $Name) }
    }
}

function Finish-EgObserverCase {
    param([string]$Name, [bool]$Observed, $Runtime)
    $script:EgState.application_child_observed = $Observed
    $accounting = Close-EgObserverRuntime -Runtime $Runtime
    if ($null -ne $accounting) {
        $script:EgState.total_processes = [uint64]$accounting.TotalProcesses
        $script:EgState.active_processes = [uint64]$accounting.ActiveProcesses
        $script:EgState.reap_confirmed = ($accounting.ActiveProcesses -eq 0)
    }
    $script:EgState.start_verdict = Get-EgStartVerdict
    Write-Output ('observer_case=' + $Name + ' observed=' + [string]$Observed + `
        ' observer_failed=' + [string]$script:EgState.observer_failed + `
        ' verdict=' + [string]$script:EgState.start_verdict)
}

function Invoke-EgObserverCases {
    . $LauncherLibraryPath
    . $FunctionsPath
    $RootPath = $DataRoot
    Set-EgFixturePythonEnvironment -ModulePath $PythonModulePath `
        -ExpectedSha256 (Get-FileHash -LiteralPath $PythonModulePath -Algorithm SHA256).Hash
    $script:EgPythonExeNormal = [IO.Path]::GetFullPath($PythonExe)
    $script:TimeoutSeconds = 120
    $script:EgObserverMetadataTimeoutMilliseconds = 1000
    $script:EgObserverProcessGoneErrorCode = 87
    $script:EgObserverN7ProviderCalls = 0

    $values = @{
        run_delay_seconds = 30
        release_path = (Join-Path $RootPath 'P1-release-after-observation.marker')
    }
    $runtime = Start-EgObserverRuntime -Name 'P1' -Mode 'positive' -Values $values
    try {
        $script:EgConfigPathNormal = $runtime.ConfigPath
        New-EgObserverState
        [void](Wait-EgObserverFile -Path $runtime.ReadyPath -ProcessHandle $runtime.Launcher.ProcessHandle)
        $positiveObserverDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
            ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency)
        $observed = $false
        do {
            $observed = Invoke-EgObserverFunction -Name 'base' -Runtime $runtime -StartTicks $runtime.StartTicks
            if ($observed -or $script:EgState.observer_failed) { break }
            Start-Sleep -Milliseconds 50
        } while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $positiveObserverDeadline)
        if (-not $observed -or $script:EgState.observer_failed) {
            $diagnostic = 'observed=' + [string]$observed +
                ';observer_failed=' + [string]$script:EgState.observer_failed
            $diagnosticIds = [EnergyGridOneShotSupervisorNative]::GetProcessIds($runtime.JobHandle)
            if ($diagnosticIds.Succeeded) {
                foreach ($diagnosticPid in $diagnosticIds.ProcessIds) {
                    if ([uint32]$diagnosticPid -eq [uint32]$runtime.Launcher.ProcessId) { continue }
                    $diagnosticCandidate = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess(
                        [uint32]$diagnosticPid)
                    if (-not $diagnosticCandidate.Succeeded) {
                        $diagnostic += ';candidate_open=FAIL'
                        continue
                    }
                    try {
                        $diagnosticLive = [EnergyGridOneShotSupervisorNative]::GetProcessLive(
                            $diagnosticCandidate.Handle)
                        $diagnosticMembership = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                            $diagnosticCandidate.Handle, $runtime.JobHandle)
                        $diagnosticImage = [EnergyGridOneShotSupervisorNative]::GetImage(
                            $diagnosticCandidate.Handle)
                        $diagnosticMetadata = [EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(
                            [uint32]$diagnosticPid, $script:EgObserverMetadataTimeoutMilliseconds)
                        $diagnostic += ';live=' + [string]($diagnosticLive.Succeeded -and $diagnosticLive.Live) +
                            ';member=' + [string]($diagnosticMembership.Succeeded -and
                                $diagnosticMembership.IsMember) +
                            ';image_match=' + [string]($diagnosticImage.Succeeded -and
                                [System.StringComparer]::OrdinalIgnoreCase.Equals(
                                    [System.IO.Path]::GetFullPath($diagnosticImage.ImagePath),
                                    [System.IO.Path]::GetFullPath($script:EgPythonExeNormal))) +
                            ';metadata=' + [string]$diagnosticMetadata.Succeeded +
                            ';parent_match=' + [string]($diagnosticMetadata.Succeeded -and
                                [uint32]$diagnosticMetadata.ParentProcessId -eq
                                    [uint32]$runtime.Launcher.ProcessId) +
                            ';command_match=' + [string]($diagnosticMetadata.Succeeded -and
                                $diagnosticMetadata.CommandLine -ceq (Get-EgCanonicalApplicationCommandLine))
                    }
                    catch { $diagnostic += ';candidate_probe=ERROR' }
                    finally { Close-EgHandle -Handle $diagnosticCandidate.Handle }
                }
            }
            else { $diagnostic += ';job_enumeration=FAIL' }
            Write-Output ('observer_p1_diagnostic=' + $diagnostic)
        }
        Assert-EgObserver ($observed -and -not $script:EgState.observer_failed) 'P1_not_proven'
        $accounting = Close-EgObserverRuntime -Runtime $runtime
        $script:EgState.application_child_observed = $observed
        $script:EgState.total_processes = [uint64]$accounting.TotalProcesses
        $script:EgState.active_processes = [uint64]$accounting.ActiveProcesses
        $script:EgState.reap_confirmed = ($accounting.ActiveProcesses -eq 0)
        $script:EgState.start_verdict = Get-EgStartVerdict
        Assert-EgObserver ($script:EgState.start_verdict -ceq 'STARTED_PROVEN') 'P1_verdict'
        Write-Output ('observer_case=P1_positive observed=True observer_failed=False verdict=' + $script:EgState.start_verdict)
    }
    finally { if ($Runtime.JobHandle -ne [IntPtr]::Zero) { [void](Close-EgObserverRuntime -Runtime $Runtime) } }

    $noiseObserved = 0
    $noiseFailures = 0
    for ($trial = 0; $trial -lt 10; $trial++) {
        $noiseStarted = Join-Path $RootPath ('P2-noise-' + $trial + '.ready')
        $ready = Join-Path $RootPath ('P2-positive-' + $trial + '.ready.json')
        $runtime = Start-EgObserverRuntime -Name ('P2-' + $trial) -Mode 'noise' -Values @{
            noise_started_path = $noiseStarted; ready_path = $ready; run_delay_seconds = 20
        }
        try {
            $script:EgConfigPathNormal = $runtime.ConfigPath
            New-EgObserverState
            [void](Wait-EgObserverFile -Path $noiseStarted -ProcessHandle $runtime.Launcher.ProcessHandle)
            $preflight = Invoke-EgObserverFunction -Name 'base' -Runtime $runtime -StartTicks $runtime.StartTicks
            Assert-EgObserver (-not $preflight -and -not $script:EgState.observer_failed) ('noise_false_positive:' + $trial)
            [void](Wait-EgObserverFile -Path $ready -ProcessHandle $runtime.Launcher.ProcessHandle)
            New-EgObserverState
            $postflight = Invoke-EgObserverFunction -Name 'base' -Runtime $runtime -StartTicks $runtime.StartTicks
            if ($postflight) { $noiseObserved++ }
            if ($script:EgState.observer_failed) { $noiseFailures++ }
            Assert-EgObserver $postflight ('noise_positive_missing:' + $trial)
        }
        finally { if ($Runtime.JobHandle -ne [IntPtr]::Zero) { [void](Close-EgObserverRuntime -Runtime $Runtime) } }
    }
    Assert-EgObserver ($noiseObserved -eq 10 -and $noiseFailures -eq 0) 'noise_trial_totals'
    Write-Output 'observer_case=P2_preflight_noise trials=10 observed=10 observer_failed=0'

    $negativeCases = @(
        @{ Name = 'N1_wrong_parent'; Mode = 'wrongparent'; Function = 'base'; Values = @{ wrong_parent_delay_seconds = 30; run_delay_seconds = 30 } },
        @{ Name = 'N2_wrong_command_line'; Mode = 'wrongcmd'; Function = 'base'; Values = @{ list_delay_seconds = 30 } },
        @{ Name = 'N3_wrong_image_exact_command_line'; Mode = 'wrong-image'; Function = 'base'; Python = $PythonwExe; Values = @{ run_delay_seconds = 30; canonical_command_line_executable = $script:EgPythonExeNormal } },
        @{ Name = 'N5_gone_pid_87_then_positive'; Mode = 'positive'; Function = 'N5'; Values = @{ run_delay_seconds = 30 } },
        @{ Name = 'N6_provider_busy'; Mode = 'positive'; Function = 'N7'; Values = @{ run_delay_seconds = 30 } },
        @{ Name = 'N7_provider_timeout'; Mode = 'positive'; Function = 'N7'; Values = @{ run_delay_seconds = 30 } },
        @{ Name = 'N8_launcher_dead'; Mode = 'launcher-exit'; Function = 'base'; Values = @{ run_delay_seconds = 30 } },
        @{ Name = 'N9_child_dead'; Mode = 'deadchild'; Function = 'base'; Values = @{ list_delay_seconds = 0.15 } },
        @{ Name = 'N11_no_row_candidate_live'; Mode = 'positive'; Function = 'N11'; Values = @{ run_delay_seconds = 30 } },
        @{ Name = 'N12_late_success_after_timeout'; Mode = 'positive'; Function = 'N7'; Values = @{ run_delay_seconds = 30 } },
        @{ Name = 'N14_deadline_guard'; Mode = 'positive'; Function = 'N7'; Values = @{ run_delay_seconds = 30 } }
    )
    foreach ($case in $negativeCases) {
        if ($case.Name -ceq 'N3_wrong_image_exact_command_line' -and
            -not (Test-Path -LiteralPath $PythonwExe -PathType Leaf)) {
            throw 'EG_HARNESS:pythonw_precondition_missing'
        }
        $pythonForCase = $script:EgPythonExeNormal
        if ($case.ContainsKey('Python')) { $pythonForCase = [string]$case.Python }
        $runtime = Start-EgObserverRuntime -Name $case.Name -Mode $case.Mode `
            -Values $case.Values -PythonExecutable $pythonForCase
        try {
            $script:EgConfigPathNormal = $runtime.ConfigPath
            New-EgObserverState
            if ($case.Function -eq 'N7') { [EnergyGridOneShotSupervisorN7ProviderControl]::Reset() }
            if ($case.Name -ceq 'N6_provider_busy') { [EnergyGridOneShotSupervisorN7ProviderControl]::SetBusy() }
            if ($case.Name -ceq 'N12_late_success_after_timeout') {
                [EnergyGridOneShotSupervisorN7ProviderControl]::SetLateSuccess($true)
            }
            if ($case.Name -ne 'N8_launcher_dead' -and $case.Name -ne 'N10_open_error_non_87') {
                [void](Wait-EgObserverFile -Path $runtime.ReadyPath -ProcessHandle $runtime.Launcher.ProcessHandle)
            }
            if ($case.Name -ceq 'N9_child_dead') {
                $childProof = Get-Content -LiteralPath $runtime.ReadyPath -Raw | ConvertFrom-Json
                $childProcess = [System.Diagnostics.Process]::GetProcessById([int]$childProof.pid)
                try { Assert-EgObserver ($childProcess.WaitForExit(10000)) 'dead_child_not_exited' }
                finally { $childProcess.Dispose() }
            }
            if ($case.Name -ceq 'N8_launcher_dead') {
                $launcherWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
                    $runtime.Launcher.ProcessHandle, 10000)
                Assert-EgObserver ($launcherWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) 'N8_launcher_alive'
            }
            if ($case.Name -ceq 'N11_no_row_candidate_live') {
                $proof = Get-Content -LiteralPath $runtime.ReadyPath -Raw | ConvertFrom-Json
                $script:EgObserverInjectedPid = [uint32]$proof.pid
            }
            if ($case.Name -ceq 'N6_provider_busy' -or $case.Name -ceq 'N7_provider_timeout' -or
                $case.Name -ceq 'N11_no_row_candidate_live' -or $case.Name -ceq 'N12_late_success_after_timeout') {
                $proof = Get-Content -LiteralPath $runtime.ReadyPath -Raw | ConvertFrom-Json
                $script:EgObserverInjectedPid = [uint32]$proof.pid
            }
            if ($case.Name -ceq 'N1_wrong_parent') {
                $proof = Get-Content -LiteralPath $runtime.ReadyPath -Raw | ConvertFrom-Json
                $identity = Get-EgObserverProcessEvidence -ProcessId ([uint32]$proof.pid)
                Assert-EgObserver ($identity.Live.Live -and $identity.Image.Succeeded -and
                    [string]::Equals([IO.Path]::GetFullPath($identity.Image.ImagePath),
                        $script:EgPythonExeNormal, [StringComparison]::OrdinalIgnoreCase)) 'N1_image_control'
                Assert-EgObserver ($identity.Metadata.Succeeded -and
                    $identity.Metadata.CommandLine -ceq (Get-EgCanonicalApplicationCommandLine) -and
                    [uint32]$identity.Metadata.ParentProcessId -eq [uint32]$proof.parent_pid -and
                    [uint32]$identity.Metadata.ParentProcessId -ne $runtime.Launcher.ProcessId) 'N1_parent_control'
            }
            if ($case.Name -ceq 'N2_wrong_command_line') {
                $proof = Get-Content -LiteralPath $runtime.ReadyPath -Raw | ConvertFrom-Json
                $identity = Get-EgObserverProcessEvidence -ProcessId ([uint32]$proof.pid)
                $wrongCommand = ConvertTo-EgNativeCommandLine -Argument @(
                    $script:EgPythonExeNormal, '-m', 'energygrid_bill_downloader', 'list', '--config',
                    $runtime.ConfigPath)
                Assert-EgObserver ($identity.Live.Live -and $identity.Image.Succeeded -and
                    [string]::Equals([IO.Path]::GetFullPath($identity.Image.ImagePath),
                        $script:EgPythonExeNormal, [StringComparison]::OrdinalIgnoreCase)) 'N2_image_control'
                Assert-EgObserver ($identity.Metadata.Succeeded -and
                    [uint32]$identity.Metadata.ParentProcessId -eq $runtime.Launcher.ProcessId -and
                    $identity.Metadata.CommandLine -ceq $wrongCommand -and
                    $identity.Metadata.CommandLine -cne (Get-EgCanonicalApplicationCommandLine)) 'N2_command_control'
            }
            if ($case.Name -ceq 'N3_wrong_image_exact_command_line') {
                $proof = Get-Content -LiteralPath $runtime.ReadyPath -Raw | ConvertFrom-Json
                $identity = Get-EgObserverProcessEvidence -ProcessId ([uint32]$proof.pid)
                $alternateImage = $null
                if ($identity.Image.Succeeded) { $alternateImage = [IO.Path]::GetFullPath($identity.Image.ImagePath) }
                Assert-EgObserver ($identity.Live.Live -and $identity.Image.Succeeded -and
                    [string]::Equals($alternateImage, [IO.Path]::GetFullPath($PythonwExe),
                        [StringComparison]::OrdinalIgnoreCase) -and
                    -not [string]::Equals($alternateImage, $script:EgPythonExeNormal,
                        [StringComparison]::OrdinalIgnoreCase)) 'N3_alternate_image_control'
                Assert-EgObserver ($identity.Metadata.Succeeded -and
                    [uint32]$identity.Metadata.ParentProcessId -eq $runtime.Launcher.ProcessId -and
                    $identity.Metadata.CommandLine -ceq (Get-EgCanonicalApplicationCommandLine)) `
                    'N3_exact_command_parent_control'
            }
            if ($case.Name -ceq 'N14_deadline_guard') {
                $oldStart = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() -
                    [int64][System.Diagnostics.Stopwatch]::Frequency)
                $script:TimeoutSeconds = 1
                $script:EgObserverN7ProviderCalls = [EnergyGridOneShotSupervisorN7ProviderControl]::GetCallCount()
                $observed = Invoke-EgObserverFunction -Name 'N7' -Runtime $runtime -StartTicks $oldStart
                $calls = [EnergyGridOneShotSupervisorN7ProviderControl]::GetCallCount()
                Assert-EgObserver ($calls -eq 0 -and -not $observed -and -not $script:EgState.observer_failed) 'deadline_guard_query_started'
                $script:TimeoutSeconds = 120
            }
            else {
                if ($case.Name -ceq 'N6_provider_busy' -or $case.Name -ceq 'N7_provider_timeout' -or
                    $case.Name -ceq 'N12_late_success_after_timeout') {
                    $script:TimeoutSeconds = 120
                }
                if ($case.Name -ceq 'N5_gone_pid_87_then_positive') {
                    $retryDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
                        ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency)
                    do {
                        $observed = Invoke-EgObserverFunction -Name $case.Function `
                            -Runtime $runtime -StartTicks $runtime.StartTicks
                        if ($observed -or $script:EgState.observer_failed) { break }
                        Start-Sleep -Milliseconds 50
                    } while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $retryDeadline)
                }
                else {
                    $observed = Invoke-EgObserverFunction -Name $case.Function `
                        -Runtime $runtime -StartTicks $runtime.StartTicks
                }
            }
            if ($case.Name -ceq 'N7_provider_timeout' -or $case.Name -ceq 'N12_late_success_after_timeout') {
                $entered = [EnergyGridOneShotSupervisorN7ProviderControl]::WaitUntilEntered(3000)
                $incomplete = -not [EnergyGridOneShotSupervisorN7ProviderControl]::IsCompleted()
                Assert-EgObserver ($entered -and $incomplete -and $script:EgN7LastMetadata.TimedOut) 'N7_timeout_producer_invalid'
                Assert-EgObserver ([EnergyGridOneShotSupervisorN7ProviderControl]::GetCallCount() -eq 1 -and
                    [EnergyGridOneShotSupervisorN7ProviderControl]::GetTimeoutMilliseconds() -eq 1000 -and
                    [EnergyGridOneShotSupervisorN7ProviderControl]::GetProcessId() -eq [uint32]$proof.pid) `
                    'N7_worker_contract_invalid'
                Write-Output ('observer_n7_injection=PASS calls=' + [EnergyGridOneShotSupervisorN7ProviderControl]::GetCallCount() + `
                    ' target_pid=' + [EnergyGridOneShotSupervisorN7ProviderControl]::GetProcessId())
                Write-Output ('native_timeout_producer=PASS substitutions=1 provider_calls=' + `
                    [EnergyGridOneShotSupervisorN7ProviderControl]::GetCallCount() + `
                    ' timeout_ms=' + [EnergyGridOneShotSupervisorN7ProviderControl]::GetTimeoutMilliseconds() + `
                    ' worker_started=' + [string]$entered + ' worker_incomplete=' + [string]$incomplete + `
                    ' timed_out=' + [string]$script:EgN7LastMetadata.TimedOut)
                if ($case.Name -ceq 'N12_late_success_after_timeout') {
                    [EnergyGridOneShotSupervisorN7ProviderControl]::Release()
                    Assert-EgObserver ([EnergyGridOneShotSupervisorN7ProviderControl]::WaitUntilCompleted(5000)) 'N12_worker_not_released'
                    Assert-EgObserver ($script:EgN7LastMetadata.Succeeded -and $script:EgState.observer_failed) 'N12_late_success_reopened_verdict'
                }
                if ($case.Name -ceq 'N7_provider_timeout' -or $case.Name -ceq 'N12_late_success_after_timeout') {
                    Assert-EgObserver ([EnergyGridOneShotSupervisorN7ProviderControl]::GetProcessId() -eq [uint32]$proof.pid) 'N7_target_pid_mismatch'
                }
            }
            if ($case.Name -ceq 'N6_provider_busy') {
                Assert-EgObserver ($script:EgState.observer_failed -and -not $observed) 'N6_busy_not_fail_closed'
            }
            if ($case.Name -ceq 'N7_provider_timeout') {
                Assert-EgObserver ($script:EgState.observer_failed -and -not $observed) 'N7_timeout_not_fail_closed'
            }
            if ($case.Name -ceq 'N12_late_success_after_timeout') {
                Assert-EgObserver ($script:EgState.observer_failed -and -not $observed) 'N12_timeout_not_fail_closed'
            }
            if ($case.Name -ceq 'N11_no_row_candidate_live') {
                Assert-EgObserver ($script:EgState.observer_failed -and -not $observed) 'N11_live_no_row_not_fail_closed'
            }
            if ($case.Name -ceq 'N10_open_error_non_87') {
                $open = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess([uint32]4)
                Assert-EgObserver (-not $open.Succeeded -and $open.ErrorCode -ne 87) 'N10_expected_non87_open_error_missing'
            }
            if ($case.Name -ceq 'N14_deadline_guard') {
                Write-Output 'observer_deadline_guard_metadata_calls=0'
            }
            Finish-EgObserverCase -Name $case.Name -Observed $observed -Runtime $runtime
        }
        finally {
            [EnergyGridOneShotSupervisorN7ProviderControl]::Release()
            $providerRan = ([EnergyGridOneShotSupervisorN7ProviderControl]::GetCallCount() -gt 0)
            $naturalIdle = $false
            $workerReleased = $true
            if ($providerRan) {
                $workerReleased = [EnergyGridOneShotSupervisorN7ProviderControl]::WaitUntilCompleted(5000)
                if ($workerReleased) {
                    $idleDeadline = [DateTime]::UtcNow.AddSeconds(5)
                    do {
                        $naturalIdle = -not [EnergyGridOneShotSupervisorN7ProviderControl]::IsBusy()
                        if (-not $naturalIdle) { Start-Sleep -Milliseconds 10 }
                    } while (-not $naturalIdle -and [DateTime]::UtcNow -lt $idleDeadline)
                }
            }
            if ($workerReleased) { [EnergyGridOneShotSupervisorN7ProviderControl]::Reset() }
            if ($Runtime.JobHandle -ne [IntPtr]::Zero) { [void](Close-EgObserverRuntime -Runtime $Runtime) }
            if ($providerRan) {
                Assert-EgObserver $workerReleased 'N7_worker_release_timeout'
                Assert-EgObserver $naturalIdle 'N7_busy_state_not_clean'
                Write-Output 'observer_n7_idle_after_release=PASS'
            }
        }
    }

    $runtime = Start-EgObserverRuntime -Name 'N4a' -Mode 'idle' -Values @{}
    $outside = $null
    try {
        $script:EgConfigPathNormal = $runtime.ConfigPath
        $config = New-EgObserverConfig -Name 'N4a-external' -Mode 'positive' -Values @{
            ready_path = (Join-Path $RootPath 'N4a-external-ready.json'); run_delay_seconds = 20
        }
        $arguments = New-EgObserverLauncherArguments -ConfigPath $config `
            -PythonExecutable $script:EgPythonExeNormal -RunId ([guid]::NewGuid().ToString().ToLowerInvariant())
        $externalInfo = New-Object System.Diagnostics.ProcessStartInfo
        $externalInfo.FileName = $script:NativePowerShell
        $externalInfo.Arguments = ConvertTo-EgNativeCommandLine -Argument $arguments[1..($arguments.Count - 1)]
        $externalInfo.WorkingDirectory = $RepositoryRoot
        $externalInfo.UseShellExecute = $false
        $externalInfo.CreateNoWindow = $true
        $outside = New-Object System.Diagnostics.Process
        $outside.StartInfo = $externalInfo
        Assert-EgObserver ($outside.Start()) 'N4a_external_launcher_start'
        $outsideReady = Join-Path $RootPath 'N4a-external-ready.json'
        $proof = Wait-EgObserverFile -Path $outsideReady -TimeoutMilliseconds 30000
        $script:EgConfigPathNormal = $config
        $script:EgPythonExeNormal = [IO.Path]::GetFullPath($PythonExe)
        $metadata = [EnergyGridOneShotSupervisorNative]::QueryProcessMetadata([uint32]$proof.pid, 1000)
        $candidate = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess([uint32]$proof.pid)
        Assert-EgObserver ($metadata.Succeeded -and [uint32]$metadata.ParentProcessId -eq [uint32]$outside.Id) 'N4a_parent_identity'
        Assert-EgObserver ($metadata.CommandLine -ceq (Get-EgCanonicalApplicationCommandLine)) 'N4a_command_identity'
        Assert-EgObserver $candidate.Succeeded 'N4a_candidate_open'
        try {
            $image = [EnergyGridOneShotSupervisorNative]::GetImage($candidate.Handle)
            Assert-EgObserver ($image.Succeeded -and [string]::Equals(
                [IO.Path]::GetFullPath($image.ImagePath), $script:EgPythonExeNormal,
                [StringComparison]::OrdinalIgnoreCase)) 'N4a_image_identity'
        }
        finally { Close-EgObserverHandle -Handle $candidate.Handle }
        New-EgObserverState
        $observed = Test-EgApplicationChild -JobHandle $runtime.JobHandle `
            -LauncherHandle $outside.Handle -LauncherPid ([uint32]$outside.Id) -StartTicks $runtime.StartTicks
        Assert-EgObserver (-not $observed -and -not $script:EgState.observer_failed) 'N4a_outside_job_accepted'
        Assert-EgObserver ($outside.WaitForExit(30000)) 'N4a_external_launcher_timeout'
        Finish-EgObserverCase -Name 'N4a_outside_job_exact_identity' -Observed $observed -Runtime $runtime
    }
    finally {
        if ($null -ne $outside) { $outside.Dispose() }
        if ($Runtime.JobHandle -ne [IntPtr]::Zero) { [void](Close-EgObserverRuntime -Runtime $Runtime) }
    }

    $runtime = Start-EgObserverRuntime -Name 'N4b' -Mode 'idle' -Values @{}
    $outsideChild = $null
    try {
        $outsideConfig = New-EgObserverConfig -Name 'N4b-outside' -Mode 'tree-child' -Values @{
            grandchild_ready_path = (Join-Path $RootPath 'N4b-outside-ready.json'); tree_child_delay_seconds = 60
        }
        $outsideChild = Start-EgFixturePython -Operation 'tree-child' -PythonExe $script:EgPythonExeNormal -ConfigPath $outsideConfig
        $outsideProof = Wait-EgObserverFile -Path (Join-Path $RootPath 'N4b-outside-ready.json') -TimeoutMilliseconds 20000
        $script:EgObserverInjectedPid = [uint32]$outsideProof.pid
        $script:EgConfigPathNormal = $runtime.ConfigPath
        New-EgObserverState
        $observed = Invoke-EgObserverFunction -Name 'N4b' -Runtime $runtime -StartTicks $runtime.StartTicks
        Assert-EgObserver (-not $observed -and -not $script:EgState.observer_failed) 'N4b_nonmember_accepted'
        Finish-EgObserverCase -Name 'N4b_injected_non_member_pid' -Observed $observed -Runtime $runtime
    }
    finally {
        if ($null -ne $outsideChild) {
            if (-not $outsideChild.HasExited) { $outsideChild.Kill(); [void]$outsideChild.WaitForExit(5000) }
            $outsideChild.Dispose()
        }
        if ($Runtime.JobHandle -ne [IntPtr]::Zero) { [void](Close-EgObserverRuntime -Runtime $Runtime) }
    }

    $runtime = Start-EgObserverRuntime -Name 'N10' -Mode 'idle' -Values @{}
    try {
        $script:EgConfigPathNormal = $runtime.ConfigPath
        New-EgObserverState
        $observed = Invoke-EgObserverFunction -Name 'N10' -Runtime $runtime -StartTicks $runtime.StartTicks
        Assert-EgObserver (-not $observed -and $script:EgState.observer_failed) 'N10_non87_not_fail_closed'
        $open = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess([uint32]4)
        Assert-EgObserver (-not $open.Succeeded -and $open.ErrorCode -ne 87) 'N10_expected_non87_open_error_missing'
        Finish-EgObserverCase -Name 'N10_open_error_non_87' -Observed $observed -Runtime $runtime
    }
    finally { if ($Runtime.JobHandle -ne [IntPtr]::Zero) { [void](Close-EgObserverRuntime -Runtime $Runtime) } }

    $releasePath = Join-Path $RootPath 'N13-release-owned-child.marker'
    $runtime = Start-EgObserverRuntime -Name 'N13' -Mode 'positive' -Values @{
        run_delay_seconds = 0; release_path = $releasePath
    }
    $childHandle = [IntPtr]::Zero
    try {
        $script:EgConfigPathNormal = $runtime.ConfigPath
        $proof = Wait-EgObserverFile -Path $runtime.ReadyPath `
            -ProcessHandle $runtime.Launcher.ProcessHandle
        $script:EgN13ProcessId = [uint32]$proof.pid
        $opened = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess($script:EgN13ProcessId)
        Assert-EgObserver ($opened.Succeeded -and $opened.Handle -ne [IntPtr]::Zero) `
            'N13_owned_child_handle_missing'
        $childHandle = $opened.Handle
        $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership($childHandle, $runtime.JobHandle)
        Assert-EgObserver ($membership.Succeeded -and $membership.IsMember) `
            'N13_child_not_in_launcher_job'
        $image = [EnergyGridOneShotSupervisorNative]::GetImage($childHandle)
        $beforeExit = [EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(
            $script:EgN13ProcessId, 1000)
        Assert-EgObserver ($image.Succeeded -and [string]::Equals(
            [IO.Path]::GetFullPath($image.ImagePath), $script:EgPythonExeNormal,
            [StringComparison]::OrdinalIgnoreCase)) 'N13_live_child_image_invalid'
        Assert-EgObserver ($beforeExit.Succeeded -and
            [uint32]$beforeExit.ParentProcessId -eq $runtime.Launcher.ProcessId -and
            $beforeExit.CommandLine -ceq (Get-EgCanonicalApplicationCommandLine)) `
            'N13_live_child_identity_invalid'
        $script:EgN13ChildHandle = $childHandle
        $script:EgN13ReleasePath = $releasePath
        $script:EgN13Metadata = $null
        $script:EgConfigPathNormal = $runtime.ConfigPath
        New-EgObserverState
        $observed = Invoke-EgObserverFunction -Name 'N13' -Runtime $runtime `
            -StartTicks $runtime.StartTicks
        Assert-EgObserver (-not $observed -and -not $script:EgState.observer_failed) `
            'N13_exited_child_ambiguous'
        Assert-EgObserver ($null -ne $script:EgN13Metadata -and
            -not $script:EgN13Metadata.Succeeded -and -not $script:EgN13Metadata.TimedOut -and
            -not $script:EgN13Metadata.ProviderFailed) 'N13_production_wmi_no_row_not_observed'
        $exited = [EnergyGridOneShotSupervisorNative]::GetProcessLive($childHandle)
        Assert-EgObserver ($exited.Succeeded -and -not $exited.Live) 'N13_held_handle_did_not_prove_exit'
        Finish-EgObserverCase -Name 'N13_real_no_row_after_exit' -Observed $observed -Runtime $runtime
    }
    finally {
        if (-not [string]::IsNullOrWhiteSpace($releasePath)) {
            [IO.File]::WriteAllText($releasePath, 'release')
        }
        if ($childHandle -ne [IntPtr]::Zero) { Close-EgObserverHandle -Handle $childHandle }
        $script:EgN13ChildHandle = [IntPtr]::Zero
        $script:EgN13ProcessId = [uint32]0
        $script:EgN13ReleasePath = $null
        $script:EgN13Metadata = $null
        if ($Runtime.JobHandle -ne [IntPtr]::Zero) { [void](Close-EgObserverRuntime -Runtime $Runtime) }
    }
    Write-Output 'observer_leftover_processes=0'
    Write-Output 'real_observer_matrix=PASS cases=17 budget_ms=1000'
}

function New-EgEvidenceRoot {
    param([string]$Path)
    [void][IO.Directory]::CreateDirectory($Path)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $security = New-Object System.Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner($identity.User)
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($sidText in @($identity.User.Value, 'S-1-5-18', 'S-1-5-32-544')) {
        $sid = New-Object System.Security.Principal.SecurityIdentifier($sidText)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $sid, [System.Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance, [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow)
        [void]$security.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $security
}

function Invoke-EgDirectSupervisor {
    param([string]$CaseRoot, [string]$Mode, [string]$RunId)
    $evidenceRoot = Join-Path $CaseRoot 'evidence'
    $logRoot = Join-Path $CaseRoot 'logs'
    [void][IO.Directory]::CreateDirectory($logRoot)
    New-EgEvidenceRoot -Path $evidenceRoot
    $configPath = Join-Path $CaseRoot 'fixture-config.json'
    $document = [ordered]@{
        module_sha256 = (Get-FileHash -LiteralPath $PythonModulePath -Algorithm SHA256).Hash.ToLowerInvariant()
        mode = $Mode
        run_delay_seconds = 2.5
        list_delay_seconds = 2.5
    }
    [IO.File]::WriteAllText(
        $configPath, ($document | ConvertTo-Json -Depth 5 -Compress), (New-Object System.Text.UTF8Encoding($false)))
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $launcherHash = (Get-FileHash -LiteralPath $LauncherPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $libraryHash = (Get-FileHash -LiteralPath $LauncherLibraryPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $arguments = @(
        $script:NativePowerShell, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', $SupervisorPath,
        '-LauncherPath', $LauncherPath, '-ExpectedLauncherSha256', $launcherHash,
        '-ExpectedLauncherLibrarySha256', $libraryHash, '-ConfigPath', $configPath,
        '-PythonExe', $script:EgPythonExeNormal, '-CheckoutRoot', $RepositoryRoot,
        '-CredentialPath', (Join-Path $CaseRoot 'credential-placeholder'),
        '-BrowserCachePath', (Join-Path $CaseRoot 'browser-cache-placeholder'),
        '-ExpectedBranch', 'codex/energygrid-226-dual-stream-latest-email',
        '-AuthorisedLauncherRootWriteSid', $sid, '-LogRoot', $logRoot,
        '-EvidenceRoot', $evidenceRoot, '-RunId', $RunId, '-TimeoutSeconds', '30'
    )
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $script:NativePowerShell
    $info.Arguments = ConvertTo-EgNativeCommandLine -Argument $arguments[1..($arguments.Count - 1)]
    $info.WorkingDirectory = $RepositoryRoot
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    Assert-EgHarness ($process.Start()) 'e2e_supervisor_start_failed'
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(90000)) {
        $process.Kill()
        [void]$process.WaitForExit(10000)
        $process.Dispose()
        throw 'EG_HARNESS:e2e_supervisor_timeout'
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    $exitCode = [int]$process.ExitCode
    $process.Dispose()
    $lines = @($stdout -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    Assert-EgHarness ($lines.Count -gt 0) ('e2e_projection_missing:' + $stderr)
    $projection = ConvertFrom-Json -InputObject $lines[-1]
    $intentPath = Join-Path $evidenceRoot ($RunId + '.intent.json')
    $outcomePath = Join-Path $evidenceRoot ($RunId + '.outcome.json')
    Assert-EgHarness (Test-Path -LiteralPath $intentPath -PathType Leaf) 'e2e_intent_missing'
    Assert-EgHarness (Test-Path -LiteralPath $outcomePath -PathType Leaf) 'e2e_outcome_missing'
    $intentBytes = [IO.File]::ReadAllBytes($intentPath)
    $outcomeBytes = [IO.File]::ReadAllBytes($outcomePath)
    Assert-EgHarness ($intentBytes.Length -gt 2 -and $intentBytes[$intentBytes.Length - 1] -eq 10) 'e2e_intent_torn'
    Assert-EgHarness ($outcomeBytes.Length -gt 2 -and $outcomeBytes[$outcomeBytes.Length - 1] -eq 10) 'e2e_outcome_torn'
    $intent = [Text.Encoding]::UTF8.GetString($intentBytes) | ConvertFrom-Json
    $outcome = [Text.Encoding]::UTF8.GetString($outcomeBytes) | ConvertFrom-Json
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { $intentHash = [BitConverter]::ToString($sha256.ComputeHash($intentBytes)).Replace('-', '').ToLowerInvariant() }
    finally { $sha256.Dispose() }
    Assert-EgHarness ($projection.schema -ceq 'energygrid.one_shot_supervisor.public.v1') 'e2e_projection_schema'
    Assert-EgHarness ($intent.schema -ceq 'energygrid.one_shot_supervisor.intent.v1' -and
        $outcome.schema -ceq 'energygrid.one_shot_supervisor.outcome.v1') 'e2e_receipt_schema'
    Assert-EgHarness ($intent.run_id -ceq $RunId -and $outcome.run_id -ceq $RunId) 'e2e_run_id_mismatch'
    Assert-EgHarness ($intent.launcher_sha256 -ceq $launcherHash -and
        $intent.launcher_library_sha256 -ceq $libraryHash) 'e2e_launcher_identity_mismatch'
    Assert-EgHarness ($outcome.intent_sha256 -ceq $intentHash -and
        $outcome.raw_stream_retained_bytes -eq 0 -and $projection.raw_stream_retained_bytes -eq 0) 'e2e_hash_or_stream_invalid'
    Assert-EgHarness ($projection.outcome_committed -and $projection.reap_confirmed -and
        $outcome.reap_confirmed -and $outcome.containment -ceq 'REAP_CONFIRMED' -and
        $projection.active_processes -eq 0 -and $outcome.active_processes -eq 0) 'e2e_reap_not_proven'
    return [pscustomobject]@{
        ExitCode = $exitCode
        Projection = $projection
        Outcome = $outcome
        EvidenceRoot = $evidenceRoot
        Integrity = $true
        LeftoverProcesses = 0
    }
}

function Invoke-EgEndToEndCases {
    . $LauncherLibraryPath
    $RootPath = $DataRoot
    Set-EgFixturePythonEnvironment -ModulePath $PythonModulePath `
        -ExpectedSha256 (Get-FileHash -LiteralPath $PythonModulePath -Algorithm SHA256).Hash
    $script:EgPythonExeNormal = [IO.Path]::GetFullPath($PythonExe)
    $cases = @(
        @{ Name = 'E1_positive'; Mode = 'positive'; Exit = 0; Verdict = 'STARTED_PROVEN'; Observed = $true; LauncherExit = 0 },
        @{ Name = 'E2_noise'; Mode = 'noise'; Exit = 0; Verdict = 'STARTED_PROVEN'; Observed = $true; LauncherExit = 0 },
        @{ Name = 'E3_wrong_command_line'; Mode = 'wrongcmd'; Exit = 4; Verdict = 'AMBIGUOUS'; Observed = $false; LauncherExit = 0 },
        @{ Name = 'E4_launcher_exit_70_no_child'; Mode = 'launcher-exit'; Exit = 4; Verdict = 'AMBIGUOUS'; Observed = $false; LauncherExit = 70 }
    )
    foreach ($case in $cases) {
        $caseRoot = Join-Path $RootPath ($case.Name + '-' + [guid]::NewGuid().ToString('N'))
        [void][IO.Directory]::CreateDirectory($caseRoot)
        $runId = [guid]::NewGuid().ToString().ToLowerInvariant()
        $result = Invoke-EgDirectSupervisor -CaseRoot $caseRoot -Mode $case.Mode -RunId $runId
        $projection = $result.Projection
        $outcome = $result.Outcome
        Assert-EgHarness ($result.ExitCode -eq $case.Exit) ($case.Name + '_exit_code')
        Assert-EgHarness ($projection.start_verdict -ceq $case.Verdict -and
            $outcome.start_verdict -ceq $case.Verdict) ($case.Name + '_verdict')
        Assert-EgHarness ([bool]$projection.application_child_observed -eq [bool]$case.Observed -and
            [bool]$outcome.application_child_observed -eq [bool]$case.Observed) ($case.Name + '_observation')
        Assert-EgHarness ($projection.launcher_exit_code -eq $case.LauncherExit) ($case.Name + '_launcher_exit')
        $name = $case.Name -replace '_', ' '
        $name = $name -replace ' ', '_'
        if ($case.Name -ceq 'E1_positive') { $label = 'E1_positive' }
        elseif ($case.Name -ceq 'E2_noise') { $label = 'E2_noise' }
        elseif ($case.Name -ceq 'E3_wrong_command_line') { $label = 'E3_wrong_command_line' }
        else { $label = 'E4_launcher_exit_70_no_child' }
        $line = 'e2e_case=' + $label + ' exit=' + $result.ExitCode + ' verdict=' +
            $projection.start_verdict + ' observed=' + [string]$projection.application_child_observed +
            ' launcher_exit=' + $projection.launcher_exit_code + ' reap=' + [string]$projection.reap_confirmed
        if ($label -ceq 'E4_launcher_exit_70_no_child') { $line += ' total_processes=' + $projection.total_processes }
        $line += ' integrity=True leftover=0'
        Write-Output $line
    }
    Write-Output 'e2e_supervisor=PASS cases=4'
}

function Write-EgOwnerReady {
    param([string]$Path, $Value)
    $temporary = $Path + '.partial'
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 5 -Compress))
    $stream = New-Object System.IO.FileStream(
        $temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
    [IO.File]::Move($temporary, $Path)
}

function Invoke-EgCrashOwner {
    Assert-EgHarness (-not [string]::IsNullOrWhiteSpace($ReadyPath)) 'crash_owner_ready_path_required'
    . $LauncherLibraryPath
    $RootPath = $DataRoot
    Set-EgFixturePythonEnvironment -ModulePath $PythonModulePath `
        -ExpectedSha256 (Get-FileHash -LiteralPath $PythonModulePath -Algorithm SHA256).Hash
    $script:EgPythonExeNormal = [IO.Path]::GetFullPath($PythonExe)
    [void][IO.Directory]::CreateDirectory($RootPath)
    $job = [IntPtr]::Zero
    $pipes = $null
    $attributes = $null
    $threadHandle = [IntPtr]::Zero
    $processHandle = [IntPtr]::Zero
    $stdoutDrain = $null
    $stderrDrain = $null
    $job = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($job)
    try {
        $ownerReadyPath = [IO.Path]::GetFullPath($ReadyPath)
        $childReadyPath = Join-Path $RootPath 'crash-child-ready.json'
        $grandchildReadyPath = Join-Path $RootPath 'crash-grandchild-ready.json'
        $grandchildReadyValue = ''
        if ($ResumeChild) { $grandchildReadyValue = $grandchildReadyPath }
        $document = [ordered]@{
            module_sha256 = (Get-FileHash -LiteralPath $PythonModulePath -Algorithm SHA256).Hash.ToLowerInvariant()
            ready_path = $childReadyPath
            grandchild_ready_path = $grandchildReadyValue
            tree_child_delay_seconds = 60
        }
        $configPath = Join-Path $RootPath 'crash-child-config.json'
        [IO.File]::WriteAllText(
            $configPath, ($document | ConvertTo-Json -Depth 5 -Compress), (New-Object System.Text.UTF8Encoding($false)))
        $items = @($script:EgPythonExeNormal, '-B', '-m', 'energygrid_bill_downloader',
            'crash-child', '--config', $configPath)
        $line = ConvertTo-EgNativeCommandLine -Argument $items
        $pipes = [EnergyGridOneShotSupervisorNative]::CreatePipes()
        $attributes = New-Object EnergyGridOneShotSupervisorNative+AttributeResources(
            $job, $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite, $pipes.LauncherStderrWrite)
        $builder = New-Object System.Text.StringBuilder($line)
        $created = [EnergyGridOneShotSupervisorNative]::CreateContainedProcess(
            $script:EgPythonExeNormal, $builder, $RootPath, $attributes.AttributeList,
            $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite, $pipes.LauncherStderrWrite)
        Assert-EgHarness $created.Succeeded ('crash_child_create:' + $created.ErrorCode)
        $threadHandle = $created.ProcessInfo.hThread
        $processHandle = $created.ProcessInfo.hProcess
        $childPid = [uint32]$created.ProcessInfo.dwProcessId
        $attributes.Dispose()
        $attributes = $null
        Close-EgObserverHandle -Handle $pipes.LauncherStdinRead
        $pipes.LauncherStdinRead = [IntPtr]::Zero
        Close-EgObserverHandle -Handle $pipes.LauncherStdoutWrite
        $pipes.LauncherStdoutWrite = [IntPtr]::Zero
        Close-EgObserverHandle -Handle $pipes.LauncherStderrWrite
        $pipes.LauncherStderrWrite = [IntPtr]::Zero
        $stdoutDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStdoutRead)
        $pipes.SupervisorStdoutRead = [IntPtr]::Zero
        $stderrDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStderrRead)
        $pipes.SupervisorStderrRead = [IntPtr]::Zero
        Close-EgObserverHandle -Handle $pipes.SupervisorStdinWrite
        $pipes.SupervisorStdinWrite = [IntPtr]::Zero
        $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership($processHandle, $job)
        Assert-EgHarness ($membership.Succeeded -and $membership.IsMember) 'crash_child_job_membership'
        $grandchildPid = [uint32]0
        $grandchildParentPid = [uint32]0
        $childMarkerSeen = $false
        if ($ResumeChild) {
            [EnergyGridOneShotSupervisorNative]::ResetControlState()
            Assert-EgHarness ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'crash_intent_commit'
            $resume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $threadHandle, (Get-EgDeadlineTicks -StartTicks ([System.Diagnostics.Stopwatch]::GetTimestamp()) -Seconds 30))
            Assert-EgHarness ($resume.Accepted -and $resume.ReturnValue -eq 1) 'crash_child_resume'
            $childProof = Wait-EgObserverFile -Path $childReadyPath -TimeoutMilliseconds 20000
            $grandchildPid = [uint32]$childProof.grandchild_pid
            $grandchildParentPid = [uint32]$childProof.grandchild_parent_pid
            Assert-EgHarness ($childProof.pid -eq $childPid -and $grandchildPid -gt 0 -and
                $grandchildParentPid -eq $childPid) 'crash_child_descendant_identity'
            $childMarkerSeen = $true
        }
        $ownerProof = [ordered]@{
            owner_pid = [uint32]$PID
            child_pid = $childPid
            grandchild_pid = $grandchildPid
            grandchild_parent_pid = $grandchildParentPid
            job_owned = $true
            child_marker_seen = $childMarkerSeen
        }
        Write-EgOwnerReady -Path $ownerReadyPath -Value $ownerProof
        while ($true) { Start-Sleep -Seconds 10 }
    }
    finally {
        if ($null -ne $attributes) { $attributes.Dispose() }
        if ($job -ne [IntPtr]::Zero) {
            [void][EnergyGridOneShotSupervisorNative]::TerminateJob(
                $job, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        }
        if ($null -ne $stdoutDrain) { [void]$stdoutDrain.Join(5000) }
        if ($null -ne $stderrDrain) { [void]$stderrDrain.Join(5000) }
        Close-EgObserverHandle -Handle $threadHandle
        Close-EgObserverHandle -Handle $processHandle
        if ($null -ne $pipes) {
            Close-EgObserverHandle -Handle $pipes.LauncherStdinRead
            Close-EgObserverHandle -Handle $pipes.SupervisorStdinWrite
            Close-EgObserverHandle -Handle $pipes.SupervisorStdoutRead
            Close-EgObserverHandle -Handle $pipes.LauncherStdoutWrite
            Close-EgObserverHandle -Handle $pipes.SupervisorStderrRead
            Close-EgObserverHandle -Handle $pipes.LauncherStderrWrite
        }
        Close-EgObserverHandle -Handle $job
    }
}

switch ($Phase) {
    'ExtractSource' {
        Assert-EgReceiptHelpers -Path $ReceiptPath
        Write-Output (Export-EgSourceEvidence)
    }
    'ParseCompile' {
        Assert-EgReceiptHelpers -Path $ReceiptPath
        Test-EgParseAndCompile
    }
    'Native' {
        Assert-EgReceiptHelpers -Path $ReceiptPath
        Initialize-EgNativeSource | Out-Null
        Initialize-EgSupportSource
        Invoke-EgNativeCases
    }
    'Functions' {
        Assert-EgReceiptHelpers -Path $ReceiptPath
        Initialize-EgNativeSource | Out-Null
        Initialize-EgSupportSource
        . $FunctionsPath
        Invoke-EgFunctionCases
    }
    'Observer' {
        Assert-EgReceiptHelpers -Path $ReceiptPath
        Initialize-EgNativeSource | Out-Null
        Initialize-EgSupportSource
        . $FunctionsPath
        Invoke-EgObserverCases
    }
    'EndToEnd' {
        Assert-EgReceiptHelpers -Path $ReceiptPath
        . $FunctionsPath
        Invoke-EgEndToEndCases
    }
    'CrashOwner' {
        Assert-EgReceiptHelpers -Path $ReceiptPath
        Initialize-EgNativeSource | Out-Null
        Initialize-EgSupportSource
        . $FunctionsPath
        Invoke-EgCrashOwner
    }
}
