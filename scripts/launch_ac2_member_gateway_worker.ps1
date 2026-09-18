[CmdletBinding()]
param(
    [ValidateSet("DisabledProof", "Production")]
    [string]$Mode = "DisabledProof",
    [string]$InstallRoot = "C:\Program Files\X-Boundaries\MemberGatewayWorker",
    [string]$RuntimeRoot = "C:\ProgramData\X-Boundaries\MemberGatewayWorker",
    [ValidateRange(100, 600000)][int]$ExecutionTimeoutMilliseconds = 600000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function ConvertTo-XbLauncherArgument {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Remove-XbWorkerExpiredLogs {
    param([Parameter(Mandatory)][string]$LogRoot)
    if (-not (Test-Path -LiteralPath $LogRoot -PathType Container)) { return }
    $files = @(Get-ChildItem -LiteralPath $LogRoot -Filter "launcher-*.jsonl" -File | Sort-Object LastWriteTimeUtc, Name)
    foreach ($file in @($files | Where-Object { $_.LastWriteTimeUtc -lt [DateTime]::UtcNow.Date.AddDays(-30) })) {
        Remove-Item -LiteralPath $file.FullName -Force
    }
    $files = @(Get-ChildItem -LiteralPath $LogRoot -Filter "launcher-*.jsonl" -File | Sort-Object LastWriteTimeUtc, Name)
    $total = [long]0
    if ($files.Count -gt 0) { $total = [long](($files | Measure-Object -Property Length -Sum).Sum) }
    foreach ($file in $files) {
        if ($total -le 100MB) { break }
        $total -= [long]$file.Length
        Remove-Item -LiteralPath $file.FullName -Force
    }
}

function Write-XbWorkerLauncherEvent {
    param([string]$LogRoot, [string]$RunId, [string]$LauncherMode, [int]$ExitCode, [string]$TerminalStatus, [int]$WriteCount, [string]$SupportReference)
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    Remove-XbWorkerExpiredLogs -LogRoot $LogRoot
    $line = [ordered]@{
        utc_timestamp = [DateTimeOffset]::UtcNow.ToString("o")
        run_id = $RunId
        launcher_mode = $LauncherMode
        exit_code = $ExitCode
        terminal_status = $TerminalStatus
        write_count = $WriteCount
        support_reference = $SupportReference
    } | ConvertTo-Json -Compress
    Add-Content -LiteralPath (Join-Path $LogRoot ("launcher-{0}.jsonl" -f [DateTime]::UtcNow.ToString("yyyy-MM-dd"))) -Value $line -Encoding UTF8
    Remove-XbWorkerExpiredLogs -LogRoot $LogRoot
}

function Read-XbCurrentUserSecretArtifact {
    param([Parameter(Mandatory)][string]$Path)
    $secureValue = Import-Clixml -LiteralPath $Path
    if ($secureValue -isnot [Security.SecureString]) { throw "launcher_secret_artifact_invalid" }
    $holder = [Management.Automation.PSCredential]::new("xb-worker", $secureValue)
    return $holder.GetNetworkCredential().Password
}

function New-XbWorkerProcessStartInfo {
    param([string]$WorkerScript, [string]$LauncherMode, [string]$RuntimeRootPath)
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = Join-Path $PSHOME "powershell.exe"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $arguments = @("-NoLogo", "-NoProfile", "-NonInteractive", "-File", $WorkerScript)

    # DisabledProof never enters this branch: it does not read config or
    # user-scoped secure-string artifacts and supplies no production switch.
    if ($LauncherMode -eq "Production") {
        $config = Get-Content -Raw -LiteralPath (Join-Path $RuntimeRootPath "config\worker.config.json") | ConvertFrom-Json
        $workerToken = Read-XbCurrentUserSecretArtifact -Path (Join-Path $RuntimeRootPath "secrets\worker-token.clixml")
        $ac2Password = Read-XbCurrentUserSecretArtifact -Path (Join-Path $RuntimeRootPath "secrets\autocount-password.clixml")
        if ([string]::IsNullOrWhiteSpace($workerToken) -or [string]::IsNullOrWhiteSpace($ac2Password)) { throw "launcher_secret_invalid" }
        $startInfo.EnvironmentVariables["XB_MEMBER_GATEWAY_WORKER_TOKEN"] = $workerToken
        $startInfo.EnvironmentVariables["XB_AC2_PASSWORD"] = $ac2Password
        $startInfo.EnvironmentVariables["XB_AC2_PASSWORD_ENV_VAR"] = "XB_AC2_PASSWORD"
        $startInfo.EnvironmentVariables["XB_MEMBER_GATEWAY_URL"] = [string]$config.gateway_base_url
        $startInfo.EnvironmentVariables["XB_MEMBER_GATEWAY_WORKER_HOST_BINDING"] = [string]$config.worker_host_binding
        $startInfo.EnvironmentVariables["XB_AC2_ASSEMBLY_PATH"] = [string]$config.autocount_assembly_path
        $startInfo.EnvironmentVariables.Remove("XB_AC2_SESSION_FACTORY")
        $arguments += @("-EnableProductionWorker", "-EnableProductionAdapter")
        $workerToken = $null
        $ac2Password = $null
    }
    $startInfo.Arguments = (($arguments | ForEach-Object { ConvertTo-XbLauncherArgument -Value ([string]$_) }) -join " ")
    return $startInfo
}

function Stop-XbOwnedWorkerProcess {
    param([Parameter(Mandatory)][Diagnostics.Process]$Process)
    try {
        if (-not $Process.HasExited) { $Process.Kill() }
    } catch { }
    try {
        if (-not $Process.WaitForExit(30000)) { throw "worker_reap_failed" }
    } catch { throw "worker_reap_failed" }
}

$runId = "run-$(([Guid]::NewGuid().ToString('N')).ToLowerInvariant())"
$supportReference = "support-$(([Guid]::NewGuid().ToString('N')).ToLowerInvariant())"
$exitCode = 1
$terminalStatus = "launcher_failed"
$writeCount = 0
$process = $null
try {
    $workerScript = Join-Path $InstallRoot "ac2_member_gateway_worker.ps1"
    if (-not (Test-Path -LiteralPath $workerScript -PathType Leaf)) { throw "worker_script_missing" }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = New-XbWorkerProcessStartInfo -WorkerScript $workerScript -LauncherMode $Mode -RuntimeRootPath $RuntimeRoot
    if (-not $process.Start()) { throw "worker_start_failed" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($ExecutionTimeoutMilliseconds)) {
        Stop-XbOwnedWorkerProcess -Process $process
        throw "worker_execution_ceiling_exceeded"
    }
    $process.WaitForExit()
    [void]$stderrTask.GetAwaiter().GetResult()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $exitCode = [int]$process.ExitCode
    if ($exitCode -ne 0) { throw "worker_nonzero_exit" }
    $result = $stdout | ConvertFrom-Json -ErrorAction Stop
    if ($Mode -eq "DisabledProof") {
        if ($exitCode -ne 0 -or [string]$result.status -cne "disabled" -or [int]$result.writes -ne 0 -or [bool]$result.dispatch_fence) {
            throw "disabled_proof_result_invalid"
        }
        $terminalStatus = "disabled_proof_pass"
    } else {
        $terminalStatus = "worker_completed"
        $writeCount = if ($null -ne $result.writes) { [int]$result.writes } else { 0 }
    }
}
catch {
    $terminalStatus = "launcher_failed"
    $exitCode = 1
}
finally {
    if ($null -ne $process) {
        if (-not $process.HasExited) { Stop-XbOwnedWorkerProcess -Process $process }
        $process.Dispose()
    }
    Write-XbWorkerLauncherEvent -LogRoot (Join-Path $RuntimeRoot "logs") -RunId $runId -LauncherMode $Mode -ExitCode $exitCode -TerminalStatus $terminalStatus -WriteCount $writeCount -SupportReference $supportReference
}

[pscustomobject]@{
    run_id = $runId
    launcher_mode = $Mode
    exit_code = $exitCode
    terminal_status = $terminalStatus
    write_count = $writeCount
    support_reference = $supportReference
} | ConvertTo-Json -Compress
exit $exitCode
