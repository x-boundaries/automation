# Outbound-only member worker library.
# It has no listener, inbound endpoint, SQL/RDP/remoting, scheduler, or
# parallel worker surface.  Production use requires explicit switches and
# runtime credentials supplied outside the repository.

Set-StrictMode -Version Latest

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
        [string]$WorkerTokenEnvironmentVariable = "XB_MEMBER_GATEWAY_WORKER_TOKEN"
    )
    if ($GatewayBaseUrl -notmatch '^https://') { throw "gateway_https_required" }
    $token = Get-XbMemberGatewayRuntimeToken -EnvironmentVariable $WorkerTokenEnvironmentVariable
    $headers = @{ Authorization = "Bearer $token"; Accept = "application/json" }
    try {
        $params = @{
            Uri = ($GatewayBaseUrl.TrimEnd('/') + $Path)
            Method = $Method
            Headers = $headers
            ErrorAction = "Stop"
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

function Invoke-XbMemberGatewayWorkerCycle {
    param(
        [Parameter(Mandatory)][string]$GatewayBaseUrl,
        [Parameter(Mandatory)][string]$WorkerId,
        [Parameter(Mandatory)][switch]$EnableProductionWorker,
        [Parameter(Mandatory)][switch]$EnableProductionAdapter,
        [Parameter(Mandatory)][scriptblock]$ProbeMember,
        [Parameter(Mandatory)][scriptblock]$CreateMember,
        [scriptblock]$GatewayRequest
    )
    if (-not $EnableProductionWorker) {
        return [pscustomobject]@{ status = "disabled"; writes = 0; dispatch_fence = $false }
    }
    if ($null -eq $GatewayRequest) {
        $GatewayRequest = {
            param($base, $path, $method, $body)
            Invoke-XbMemberGatewayRequest -GatewayBaseUrl $base -Path $path -Method $method -Body $body
        }
    }

    $ready = & $GatewayRequest $GatewayBaseUrl "/readyz" "GET" $null
    if ($ready.ready -ne $true) { throw "gateway_not_ready" }
    $claim = & $GatewayRequest $GatewayBaseUrl "/v1/worker/claim" "POST" @{}
    if ($claim.claimed -ne $true) {
        return [pscustomobject]@{ status = "idle"; writes = 0; dispatch_fence = $false }
    }

    $job = $claim.job
    $jobId = [string]$job.job_id
    & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/precheck" -f $jobId) "POST" @{} | Out-Null
    $candidateResponse = & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/allocation/candidate" -f $jobId) "POST" @{}
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
            probe_reference = [string]$probe.probe_reference
        }
        $candidateResponse = & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/allocation/probe" -f $jobId) "POST" $probeBody
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
        probe_reference = [string]$boundProbe.probe_reference
    }
    if ([string]$recheck.state -ne "ALLOCATION_BOUND") { throw "bound_member_no_recheck_failed" }

    & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/write-intent" -f $jobId) "POST" @{
        operation = "member.create"
        member_no = [string]$allocation.member_no
        payload_hash = [string]$job.payload_hash
    } | Out-Null
    $fence = & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/dispatch-fence" -f $jobId) "POST" @{
        operation = "member.create"
        member_no = [string]$allocation.member_no
    }
    if ([string]$fence.state -ne "WRITING") { throw "dispatch_fence_not_created" }

    try {
        $writeOutcome = & $CreateMember $job $allocation
        $readbackMatch = [bool]$writeOutcome.readback_match
        $status = if ($readbackMatch) { "CREATED_VERIFIED" } else { "CREATED_READBACK_MISMATCH" }
        & $GatewayRequest $GatewayBaseUrl ("/v1/jobs/{0}/result" -f $jobId) "POST" @{
            schema_version = "xb.member.gateway.result.v1"
            job_id = $jobId
            operation = "member.create"
            dispatch_fence_id = [string]$fence.dispatch_fence_id
            status = $status
            member_no = [string]$allocation.member_no
            save_invocation_count = [int]$writeOutcome.save_invocation_count
            readback_found = $true
            readback_match = $readbackMatch
            error_code = if ($readbackMatch) { $null } else { "readback_mismatch_manual_review" }
        } | Out-Null
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
        } | Out-Null
        return [pscustomobject]@{ status = "WRITE_OUTCOME_UNCERTAIN"; writes = 1; dispatch_fence = $true }
    }
}
