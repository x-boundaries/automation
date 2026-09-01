# Outbound-only member worker library.
# It has no listener, inbound endpoint, SQL/RDP/remoting, scheduler, or
# parallel worker surface.  Production use requires explicit switches and
# runtime credentials supplied outside the repository.

Set-StrictMode -Version Latest

function New-XbMemberGatewayWorkerSession {
    $value = ([Guid]::NewGuid().ToString("N")).ToLowerInvariant()
    if ($value -cnotmatch '^[0-9a-f]{32}$') { throw "worker_session_generation_failed" }
    return "ws-$value"
}

function New-XbMemberGatewayProbeReference {
    $value = ([Guid]::NewGuid().ToString("N")).ToLowerInvariant()
    if ($value -cnotmatch '^[0-9a-f]{32}$') { throw "probe_reference_generation_failed" }
    return "probe-$value"
}

function Get-XbMemberGatewayRuntimeToken {
    param([string]$EnvironmentVariable = "XB_MEMBER_GATEWAY_WORKER_TOKEN")
    $token = [Environment]::GetEnvironmentVariable($EnvironmentVariable, "Process")
    if ([string]::IsNullOrWhiteSpace($token)) { throw "worker_credential_missing" }
    return $token
}

function ConvertTo-XbGatewayJson {
    param([Parameter(Mandatory)]$Body)
    return ($Body | ConvertTo-Json -Depth 12 -Compress)
}

function Invoke-XbMemberGatewayRequest {
    param(
        [Parameter(Mandatory)][string]$GatewayBaseUrl,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][ValidateSet("GET", "POST")][string]$Method,
        [AllowNull()]$Body,
        [Parameter(Mandatory)][ValidatePattern('^ws-[0-9a-f]{32}$')][string]$WorkerSession,
        [string]$WorkerTokenEnvironmentVariable = "XB_MEMBER_GATEWAY_WORKER_TOKEN",
        [ValidateRange(1, 300)][int]$TimeoutSec = 30
    )
    if ($GatewayBaseUrl -notmatch '^https://') { throw "gateway_https_required" }
    if ($WorkerSession -cnotmatch '^ws-[0-9a-f]{32}$') { throw "worker_session_invalid" }
    $token = Get-XbMemberGatewayRuntimeToken -EnvironmentVariable $WorkerTokenEnvironmentVariable
    $headers = @{ Authorization = "Bearer $token"; Accept = "application/json"; "X-XB-Worker-Session" = $WorkerSession }
    try {
        $params = @{
            Uri = ($GatewayBaseUrl.TrimEnd('/') + $Path)
            Method = $Method
            Headers = $headers
            ErrorAction = "Stop"
            TimeoutSec = $TimeoutSec
        }
        if ($null -ne $Body) {
            $params.Body = ConvertTo-XbGatewayJson -Body $Body
            $params.ContentType = "application/json"
        }
        return Invoke-RestMethod @params
    }
    catch {
        # Do not print the URI, headers, body, token, or remote error.
        throw "gateway_request_failed"
    }
}

function ConvertTo-XbMemberGatewayPositiveInteger {
    param(
        [AllowNull()]$Value,
        [Parameter(Mandatory)][string]$ErrorCode
    )
    if ($null -eq $Value -or $Value -is [bool] -or ($Value -isnot [int] -and $Value -isnot [long])) { throw $ErrorCode }
    if ([long]$Value -le 0 -or [long]$Value -gt [int]::MaxValue) { throw $ErrorCode }
    return [int]$Value
}

function ConvertTo-XbMemberGatewayDateTimeOffset {
    param(
        [AllowNull()]$Value,
        [Parameter(Mandatory)][string]$ErrorCode
    )
    [DateTimeOffset]$parsed = [DateTimeOffset]::MinValue
    if ($null -eq $Value -or -not [DateTimeOffset]::TryParse(
        [string]$Value,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind,
        [ref]$parsed
    )) { throw $ErrorCode }
    return $parsed.ToUniversalTime()
}

function Get-XbMemberGatewayTiming {
    param([Parameter(Mandatory)]$Ready)
    $leaseSeconds = ConvertTo-XbMemberGatewayPositiveInteger $Ready.lease_seconds "gateway_timing_invalid"
    $heartbeatSeconds = ConvertTo-XbMemberGatewayPositiveInteger $Ready.heartbeat_seconds "gateway_timing_invalid"
    $deadlineSeconds = ConvertTo-XbMemberGatewayPositiveInteger $Ready.execution_deadline_seconds "gateway_timing_invalid"
    if ($heartbeatSeconds -ge $deadlineSeconds -or $deadlineSeconds -ge $leaseSeconds) { throw "gateway_timing_invalid" }
    return [pscustomobject]@{
        lease_seconds = $leaseSeconds
        heartbeat_seconds = $heartbeatSeconds
        execution_deadline_seconds = $deadlineSeconds
    }
}

function Stop-XbMemberGatewayWriterProcess {
    param(
        [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
        [ValidateRange(100, 10000)][int]$TimeoutMilliseconds = 1000
    )
    try {
        if ($Process.HasExited) { return }
    }
    catch { throw "writer_termination_unconfirmed" }
    try {
        $Process.Kill()
    }
    catch {
        try {
            if ($Process.HasExited) { return }
        }
        catch { throw "writer_termination_unconfirmed" }
        throw "writer_termination_failed"
    }
    $stopAt = [DateTimeOffset]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    while ($true) {
        try {
            if ($Process.HasExited) { break }
            if ([DateTimeOffset]::UtcNow -ge $stopAt) { throw "writer_termination_unconfirmed" }
            [void]$Process.WaitForExit(50)
        }
        catch {
            if ($_.Exception.Message -eq "writer_termination_unconfirmed") { throw }
            throw "writer_termination_unconfirmed"
        }
    }
    try {
        if (-not $Process.HasExited) { throw "writer_termination_unconfirmed" }
    }
    catch {
        throw "writer_termination_unconfirmed"
    }
}

function ConvertTo-XbMemberGatewayProcessArgument {
    param([AllowEmptyString()][string]$Value)
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Get-XbMemberGatewayPowerShellPath {
    try {
        $path = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    }
    catch { throw "writer_process_path_unavailable" }
    if ([string]::IsNullOrWhiteSpace($path)) { throw "writer_process_path_unavailable" }
    return $path
}

function Start-XbMemberGatewayChildWriter {
    param(
        [Parameter(Mandatory)][string]$ScriptPath,
        [Parameter(Mandatory)]$Payload,
        [string[]]$Arguments = @("-ChildExternalWrite")
    )
    if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) { throw "writer_script_missing" }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = Get-XbMemberGatewayPowerShellPath
    $argumentValues = @("-NoProfile", "-NonInteractive", "-File", $ScriptPath) + @($Arguments)
    $startInfo.Arguments = (($argumentValues | ForEach-Object {
        ConvertTo-XbMemberGatewayProcessArgument -Value ([string]$_)
    }) -join " ")
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $started = $false
    try {
        if (-not $process.Start()) { throw "writer_start_failed" }
        $started = $true
        [void]$process.StandardInput.WriteLine((ConvertTo-XbGatewayJson -Body $Payload))
        $process.StandardInput.Close()
    }
    catch {
        if ($started) {
            try { Stop-XbMemberGatewayWriterProcess -Process $process } catch { throw "writer_termination_unconfirmed" }
        }
        throw "writer_start_failed"
    }
    return $process
}

function Read-XbMemberGatewayWriterOutcome {
    param([Parameter(Mandatory)][System.Diagnostics.Process]$Process)
    try {
        $raw = $Process.StandardOutput.ReadToEnd()
        [void]$Process.StandardError.ReadToEnd()
        $outcome = $raw | ConvertFrom-Json -ErrorAction Stop
        $saveCount = ConvertTo-XbMemberGatewayPositiveInteger $outcome.save_invocation_count "writer_outcome_invalid"
        if ($saveCount -ne 1 -or $outcome.readback_found -isnot [bool] -or $outcome.readback_match -isnot [bool]) {
            throw "writer_outcome_invalid"
        }
        return [pscustomobject]@{
            save_invocation_count = $saveCount
            readback_found = [bool]$outcome.readback_found
            readback_match = [bool]$outcome.readback_match
        }
    }
    catch {
        throw "writer_outcome_invalid"
    }
}

function Invoke-XbMemberGatewayProtectedWrite {
    param(
        [Parameter(Mandatory)][string]$GatewayBaseUrl,
        [Parameter(Mandatory)][string]$JobId,
        [Parameter(Mandatory)][string]$WorkerSession,
        [Parameter(Mandatory)]$JobStatus,
        [Parameter(Mandatory)]$Lease,
        [Parameter(Mandatory)]$Timing,
        [Parameter(Mandatory)]$Payload,
        [Parameter(Mandatory)][scriptblock]$GatewayRequest,
        [Parameter(Mandatory)][scriptblock]$WriterProcessFactory
    )
    $attemptStartedAt = ConvertTo-XbMemberGatewayDateTimeOffset $JobStatus.attempt_started_at "writer_attempt_timestamp_invalid"
    $leaseExpiresAt = ConvertTo-XbMemberGatewayDateTimeOffset $Lease.lease_expires_at "writer_lease_timestamp_invalid"
    $stateVersion = ConvertTo-XbMemberGatewayPositiveInteger $Lease.state_version "writer_state_version_invalid"
    $jobStateVersion = ConvertTo-XbMemberGatewayPositiveInteger $JobStatus.state_version "writer_state_version_invalid"
    if ($stateVersion -ne ($jobStateVersion + 1)) { throw "writer_lease_not_advanced" }
    $deadlineAt = $attemptStartedAt.AddSeconds([int]$Timing.execution_deadline_seconds)
    $terminationMarginSeconds = [Math]::Min(5, [Math]::Max(2, [Math]::Floor(([int]$Timing.lease_seconds - [int]$Timing.execution_deadline_seconds) / 2)))
    if ($leaseExpiresAt -le [DateTimeOffset]::UtcNow -or $leaseExpiresAt -le $deadlineAt) { throw "writer_protection_window_invalid" }
    $protectionCutoffAt = $leaseExpiresAt.AddSeconds(-$terminationMarginSeconds)
    $deadlineCutoffAt = $deadlineAt.AddSeconds(-$terminationMarginSeconds)
    $stopAt = if ($deadlineCutoffAt -lt $protectionCutoffAt) { $deadlineCutoffAt } else { $protectionCutoffAt }
    if ($stopAt -le [DateTimeOffset]::UtcNow) { throw "writer_deadline_expired" }

    $process = $null
    try {
        $process = & $WriterProcessFactory $Payload $stopAt
        if ($process -isnot [System.Diagnostics.Process]) { throw "writer_process_required" }
        $nextHeartbeatAt = [DateTimeOffset]::UtcNow.AddSeconds([int]$Timing.heartbeat_seconds)
        while ($true) {
            $now = [DateTimeOffset]::UtcNow
            if ($process.HasExited) {
                if ($now -ge $stopAt -or $now -ge $deadlineAt) { throw "writer_deadline_expired" }
                return Read-XbMemberGatewayWriterOutcome -Process $process
            }
            if ($now -ge $stopAt) {
                Stop-XbMemberGatewayWriterProcess -Process $process
                throw "writer_deadline_expired"
            }
            if ($now -ge $nextHeartbeatAt) {
                try {
                    $heartbeatTimeoutSeconds = [int][Math]::Max(1, [Math]::Min([int]$Timing.heartbeat_seconds, [Math]::Floor(($stopAt - $now).TotalSeconds / 2)))
                    $heartbeat = & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/lease" -f $JobId) "POST" @{ state_version = $stateVersion } $WorkerSession $heartbeatTimeoutSeconds
                    $nextStateVersion = ConvertTo-XbMemberGatewayPositiveInteger $heartbeat.state_version "writer_heartbeat_invalid"
                    $nextLeaseExpiresAt = ConvertTo-XbMemberGatewayDateTimeOffset $heartbeat.lease_expires_at "writer_heartbeat_invalid"
                    $heartbeatNow = [DateTimeOffset]::UtcNow
                    if ($nextStateVersion -ne ($stateVersion + 1) -or $nextLeaseExpiresAt -le $heartbeatNow -or $nextLeaseExpiresAt -le $deadlineAt) { throw "writer_heartbeat_invalid" }
                    $stateVersion = $nextStateVersion
                    $leaseExpiresAt = $nextLeaseExpiresAt
                    $protectionCutoffAt = $leaseExpiresAt.AddSeconds(-$terminationMarginSeconds)
                    $deadlineCutoffAt = $deadlineAt.AddSeconds(-$terminationMarginSeconds)
                    $stopAt = if ($deadlineCutoffAt -lt $protectionCutoffAt) { $deadlineCutoffAt } else { $protectionCutoffAt }
                    if ($stopAt -le $heartbeatNow) { throw "writer_heartbeat_invalid" }
                    $nextHeartbeatAt = $heartbeatNow.AddSeconds([int]$Timing.heartbeat_seconds)
                }
                catch {
                    try { Stop-XbMemberGatewayWriterProcess -Process $process } catch { throw "writer_termination_unconfirmed" }
                    throw "writer_heartbeat_failed"
                }
                continue
            }
            $waitUntil = $nextHeartbeatAt
            if ($stopAt -lt $waitUntil) { $waitUntil = $stopAt }
            $waitMilliseconds = [int][Math]::Max(25, [Math]::Min(250, ($waitUntil - $now).TotalMilliseconds))
            [void]$process.WaitForExit($waitMilliseconds)
        }
    }
    catch {
        if ($null -ne $process) {
            try {
                if (-not $process.HasExited) { Stop-XbMemberGatewayWriterProcess -Process $process }
            }
            catch {
                throw "writer_termination_unconfirmed"
            }
        }
        throw
    }
}

function Invoke-XbMemberGatewayWorkerCycle {
    param(
        [Parameter(Mandatory)][string]$GatewayBaseUrl,
        [Parameter(Mandatory)][string]$WorkerId,
        [Parameter(Mandatory)][switch]$EnableProductionWorker,
        [Parameter(Mandatory)][switch]$EnableProductionAdapter,
        [Parameter(Mandatory)][scriptblock]$ProbeMember,
        [Parameter(Mandatory)][scriptblock]$CreateMember,
        [string]$WorkerSession,
        [scriptblock]$GatewayRequest,
        [scriptblock]$WriterProcessFactory
    )
    if (-not $EnableProductionWorker) {
        return [pscustomobject]@{ status = "disabled"; writes = 0; dispatch_fence = $false }
    }
    if ($null -eq $WriterProcessFactory) { throw "writer_process_factory_required" }
    if ($null -eq $GatewayRequest) {
        $GatewayRequest = {
            param($base, $path, $method, $body, $session, $timeout)
            $requestTimeout = if ($null -eq $timeout) { 30 } else { [int]$timeout }
            Invoke-XbMemberGatewayRequest -GatewayBaseUrl $base -Path $path -Method $method -Body $body -WorkerSession $session -TimeoutSec $requestTimeout
        }
    }
    if ([string]::IsNullOrWhiteSpace($WorkerSession)) {
        $WorkerSession = New-XbMemberGatewayWorkerSession
    }
    elseif ($WorkerSession -cnotmatch '^ws-[0-9a-f]{32}$') {
        throw "worker_session_invalid"
    }

    $ready = & $GatewayRequest $GatewayBaseUrl "/readyz" "GET" $null $WorkerSession
    if ($ready.ready -ne $true) { throw "gateway_not_ready" }
    $timing = Get-XbMemberGatewayTiming -Ready $ready
    $claim = & $GatewayRequest $GatewayBaseUrl "/v1/worker/claim" "POST" @{} $WorkerSession
    if ($claim.claimed -ne $true) {
        return [pscustomobject]@{ status = "idle"; writes = 0; dispatch_fence = $false }
    }

    $job = $claim.job
    $jobId = [string]$job.job_id
    & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/precheck" -f $jobId) "POST" @{} $WorkerSession | Out-Null
    $candidateResponse = & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/allocation/candidate" -f $jobId) "POST" @{} $WorkerSession
    $allocation = $null
    for ($index = 0; $index -lt 10000; $index++) {
        if ($candidateResponse.bound -eq $true) {
            $allocation = $candidateResponse
            break
        }
        $candidate = [string]$candidateResponse.candidate
        $probe = & $ProbeMember $candidate
        $probeStatus = [string]$probe.status
        $probeBody = @{
            candidate = $candidate
            status = $probeStatus
            probe_reference = New-XbMemberGatewayProbeReference
        }
        $candidateResponse = & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/allocation/probe" -f $jobId) "POST" $probeBody $WorkerSession
        if ($candidateResponse.bound -eq $true) {
            $allocation = $candidateResponse
            break
        }
        if ($probeStatus -ne "OCCUPIED") { throw "allocation_probe_not_positive_free" }
    }
    if ($null -eq $allocation) { throw "member_no_allocation_exhausted" }

    # Recheck the same bound candidate.  The gateway marks any non-FREE result
    # for manual review; this worker never advances to another suffix here.
    $boundProbe = & $ProbeMember ([string]$allocation.member_no)
    $recheck = & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/allocation/recheck" -f $jobId) "POST" @{
        status = [string]$boundProbe.status
        probe_reference = New-XbMemberGatewayProbeReference
    } $WorkerSession
    if ([string]$recheck.state -ne "ALLOCATION_BOUND") { throw "bound_member_no_recheck_failed" }

    & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/write-intent" -f $jobId) "POST" @{
        operation = "member.create"
        member_no = [string]$allocation.member_no
        payload_hash = [string]$job.payload_hash
    } $WorkerSession | Out-Null
    $fence = & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/dispatch-fence" -f $jobId) "POST" @{
        operation = "member.create"
        member_no = [string]$allocation.member_no
    } $WorkerSession
    if ([string]$fence.state -ne "WRITING") { throw "dispatch_fence_not_created" }

    try {
        $jobStatus = & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/status" -f $jobId) "GET" $null $WorkerSession
        if ([string]$jobStatus.state -ne "WRITING") { throw "writer_state_invalid" }
        $jobStateVersion = ConvertTo-XbMemberGatewayPositiveInteger $jobStatus.state_version "writer_state_version_invalid"
        $lease = & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/lease" -f $jobId) "POST" @{ state_version = $jobStateVersion } $WorkerSession ([int]$timing.heartbeat_seconds)
        $leaseStateVersion = ConvertTo-XbMemberGatewayPositiveInteger $lease.state_version "writer_lease_invalid"
        $leaseExpiresAt = ConvertTo-XbMemberGatewayDateTimeOffset $lease.lease_expires_at "writer_lease_invalid"
        if ($leaseStateVersion -ne ($jobStateVersion + 1) -or $leaseExpiresAt -le [DateTimeOffset]::UtcNow) { throw "writer_lease_invalid" }
        $writeOutcome = Invoke-XbMemberGatewayProtectedWrite -GatewayBaseUrl $GatewayBaseUrl -JobId $jobId -WorkerSession $WorkerSession -JobStatus $jobStatus -Lease $lease -Timing $timing -Payload ([pscustomobject]@{ job = $job; allocation = $allocation }) -GatewayRequest $GatewayRequest -WriterProcessFactory $WriterProcessFactory
        $readbackFound = [bool]$writeOutcome.readback_found
        $readbackMatch = [bool]$writeOutcome.readback_match
        $status = if (-not $readbackFound) { "WRITE_OUTCOME_UNCERTAIN" } elseif ($readbackMatch) { "CREATED_VERIFIED" } else { "CREATED_READBACK_MISMATCH" }
        $errorCode = if (-not $readbackFound) { "readback_absent" } elseif (-not $readbackMatch) { "readback_mismatch_manual_review" } else { $null }
        & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/result" -f $jobId) "POST" @{
            schema_version = "xb.member.gateway.result.v1"
            job_id = $jobId
            operation = "member.create"
            dispatch_fence_id = [string]$fence.dispatch_fence_id
            status = $status
            member_no = [string]$allocation.member_no
            save_invocation_count = [int]$writeOutcome.save_invocation_count
            readback_found = $readbackFound
            readback_match = $readbackMatch
            error_code = $errorCode
        } $WorkerSession | Out-Null
        return [pscustomobject]@{ status = $status; writes = 1; dispatch_fence = $true }
    }
    catch {
        # After the fence, a timeout/exception/crash is uncertain.  A later
        # reconciliation may only use this same bound MemberNo.
        & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/result" -f $jobId) "POST" @{
            schema_version = "xb.member.gateway.result.v1"
            job_id = $jobId
            operation = "member.create"
            dispatch_fence_id = [string]$fence.dispatch_fence_id
            status = "WRITE_OUTCOME_UNCERTAIN"
            member_no = [string]$allocation.member_no
            save_invocation_count = 1
            readback_found = $false
            readback_match = $false
            error_code = "save_outcome_uncertain"
        } $WorkerSession | Out-Null
        return [pscustomobject]@{ status = "WRITE_OUTCOME_UNCERTAIN"; writes = 1; dispatch_fence = $true }
    }
}
