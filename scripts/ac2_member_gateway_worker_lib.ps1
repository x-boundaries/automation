# Outbound-only member worker library (member-write v2, W-G2-149 section 4.5).
# It has no listener, inbound endpoint, SQL/RDP/remoting, scheduler, or
# parallel worker surface. Production use requires explicit switches and
# runtime credentials supplied outside the repository.
#
# One cycle makes exactly these gateway calls, in this order:
#   GET /readyz -> POST /v2/worker/claim -> POST /v2/jobs/{job_id}/result
# (the result post is repeated with the identical body at most 3 times in
# total, only on transport failure or HTTP 5xx, and only within the lease).
# There is no heartbeat, precheck, allocation, write-intent, fence, writer
# registration/termination/quarantine/recovery or host binding.

Set-StrictMode -Version Latest

# Absolute interpreter for the primitive child (never resolved from PATH).
$script:XbWindowsPowerShellPath = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
$script:XbWorkerClockSkewSeconds = 120
$script:XbWorkerMaxResultPosts = 3
$script:XbWorkerAllowedFaults = @("skip_result_post")
$script:XbPrimitiveOutputFields = @(
    "outcome", "rule", "branch", "member_no", "member_guid", "save_invoked", "save_invocation_count",
    "readback", "reason_code", "dq_flags", "primitive", "error_code"
)
$script:XbResultOutcomes = @(
    "CREATED_VERIFIED", "CREATED_VERIFIED_PRIOR_ATTEMPT", "LINKED_EXISTING", "MANUAL_REVIEW", "REJECTED_VALIDATION",
    "FAILED_BEFORE_WRITE", "NOT_CREATED", "NOT_CREATED_CONFLICT", "OUTCOME_UNCERTAIN", "CREATED_READBACK_MISMATCH", "MUTEX_BUSY"
)
$script:XbResultRules = @("R0", "R1", "R2a", "R2b", "R2c", "R3", "R3b", "R4", "NONE")
$script:XbResultBranches = @("BASE", "NAME_APPENDED", "EXISTING", "NONE")
$script:XbResultReasonCodes = @(
    "prior_attempt_ambiguous", "multiple_same_person", "inactive_match", "format_variant_other_person",
    "holder_identity_unknown", "name_component_empty", "name_candidate_collision", "request_contract_violation",
    "readback_foreign_row", "name_exceeds_autocount_limit", "email_exceeds_autocount_limit", "synthetic_in_production",
    "clock_skew", "mutex_unavailable", "session_unavailable", "book_binding_mismatch", "integration_user_mismatch",
    "probe_unavailable", "fault_injection_refused", "test_book_requires_synthetic", "primitive_config_invalid",
    "primitive_launch_failed", "readback_absent_after_save", "readback_unavailable", "unexpected_error",
    "child_deadline_exceeded", "child_termination_unconfirmed", "primitive_output_invalid"
)
$script:XbResultDqFlags = @("email_seen_on_other_member", "post_save_same_person_other_row", "malformed_member_no_excluded")
$script:XbClaimRequestFields = @(
    "rule", "base_member_no", "name_component", "phone", "name", "email", "MemberType", "DOB",
    "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual"
)

function New-XbMemberGatewayWorkerSession {
    $value = ([Guid]::NewGuid().ToString("N")).ToLowerInvariant()
    if ($value -cnotmatch '^[0-9a-f]{32}$') { throw "worker_session_generation_failed" }
    return "ws-$value"
}

function Get-XbMemberGatewayRuntimeToken {
    param([string]$EnvironmentVariable = "XB_MEMBER_GATEWAY_WORKER_TOKEN")
    $token = [Environment]::GetEnvironmentVariable($EnvironmentVariable, "Process")
    if ([string]::IsNullOrWhiteSpace($token)) { throw "worker_credential_missing" }
    return $token
}

# JSON text with every non-ASCII character escaped as \uXXXX, so the bytes on
# stdin and on the wire are identical under any console or HTTP code page.
function ConvertTo-XbGatewayJson {
    param([Parameter(Mandatory)]$Body)
    $json = $Body | ConvertTo-Json -Depth 12 -Compress
    $builder = New-Object System.Text.StringBuilder
    foreach ($ch in $json.ToCharArray()) {
        if ([int]$ch -gt 126) { [void]$builder.AppendFormat("\u{0:x4}", [int]$ch) } else { [void]$builder.Append($ch) }
    }
    return $builder.ToString()
}

# Real outbound HTTPS request. Failures are reduced to fixed codes:
# gateway_http_<status> or gateway_transport_failed. The URI, headers, body,
# token and remote error text are never printed.
function Invoke-XbMemberGatewayRequest {
    param(
        [Parameter(Mandatory)][string]$GatewayBaseUrl,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][ValidateSet("GET", "POST")][string]$Method,
        [AllowNull()][string]$Body,
        [Parameter(Mandatory)][ValidatePattern('^ws-[0-9a-f]{32}$')][string]$WorkerSession,
        [string]$WorkerTokenEnvironmentVariable = "XB_MEMBER_GATEWAY_WORKER_TOKEN",
        [ValidateRange(1, 300)][int]$TimeoutSec = 30
    )
    if ($GatewayBaseUrl -notmatch '^https://') { throw "gateway_https_required" }
    if ($WorkerSession -cnotmatch '^ws-[0-9a-f]{32}$') { throw "worker_session_invalid" }
    $token = Get-XbMemberGatewayRuntimeToken -EnvironmentVariable $WorkerTokenEnvironmentVariable
    $headers = @{ Authorization = "Bearer $token"; Accept = "application/json"; "X-XB-Worker-Session" = $WorkerSession }
    $params = @{
        Uri = ($GatewayBaseUrl.TrimEnd('/') + $Path)
        Method = $Method
        Headers = $headers
        ErrorAction = "Stop"
        TimeoutSec = $TimeoutSec
        UseBasicParsing = $true
    }
    if ($null -ne $Body) {
        $params.Body = [Text.Encoding]::UTF8.GetBytes($Body)
        $params.ContentType = "application/json; charset=utf-8"
    }
    try {
        $response = Invoke-WebRequest @params
    }
    catch {
        $status = $null
        try { if ($null -ne $_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode } } catch { $status = $null }
        if ($null -ne $status -and $status -gt 0) { throw ("gateway_http_{0}" -f $status) }
        throw "gateway_transport_failed"
    }
    try {
        $text = [Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray())
        return ($text | ConvertFrom-Json -ErrorAction Stop)
    }
    catch { throw "gateway_response_invalid" }
}

function ConvertTo-XbMemberGatewayDateTimeOffset {
    param(
        [AllowNull()]$Value,
        [Parameter(Mandatory)][string]$ErrorCode
    )
    if ($Value -is [datetime]) { return ([DateTimeOffset]$Value).ToUniversalTime() }
    [DateTimeOffset]$parsed = [DateTimeOffset]::MinValue
    if ($null -eq $Value -or -not [DateTimeOffset]::TryParse(
        [string]$Value,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind,
        [ref]$parsed
    )) { throw $ErrorCode }
    return $parsed.ToUniversalTime()
}

function Test-XbMemberGatewayInteger {
    param([AllowNull()]$Value, [long]$Minimum)
    if ($null -eq $Value -or $Value -is [bool] -or ($Value -isnot [int] -and $Value -isnot [long])) { return $false }
    return ([long]$Value -ge $Minimum -and [long]$Value -le [int]::MaxValue)
}

# Worker-side probe of the AC2 mutex without waiting. Returns $true when
# another process holds it (or it cannot be opened); the worker then stops
# before claiming. A mutex acquired here is released immediately.
function Test-XbAc2MemberCreateMutexHeld {
    param([Parameter(Mandatory)][string]$Name)
    $mutex = $null
    try {
        if (-not [System.Threading.Mutex]::TryOpenExisting($Name, [ref]$mutex)) { return $false }
    }
    catch { return $true }
    try {
        $acquired = $false
        try { $acquired = $mutex.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $acquired = $true }
        if ($acquired) {
            $mutex.ReleaseMutex()
            return $false
        }
        return $true
    }
    finally { $mutex.Dispose() }
}

function Get-XbMemberGatewayClaim {
    param([Parameter(Mandatory)]$Claim)
    if ($null -eq $Claim -or $Claim.PSObject.Properties.Name -cnotcontains "claimed") { throw "claim_invalid" }
    if ($Claim.claimed -ne $true) { return $null }
    foreach ($field in @("schema_version", "job_id", "attempt_no", "lease_id", "state_version", "lease_expires_at", "first_claimed_at", "server_time_utc", "request")) {
        if ($Claim.PSObject.Properties.Name -cnotcontains $field) { throw "claim_invalid" }
    }
    if ([string]$Claim.schema_version -cne "xb.member.gateway.worker_claim.v2") { throw "claim_invalid" }
    if ($Claim.job_id -isnot [string] -or $Claim.job_id -cnotmatch '^job-[A-Za-z0-9]{16,64}$') { throw "claim_invalid" }
    if ($Claim.lease_id -isnot [string] -or $Claim.lease_id -cnotmatch '^lease-[0-9a-f]{32}$') { throw "claim_invalid" }
    if (-not (Test-XbMemberGatewayInteger $Claim.attempt_no 1) -or -not (Test-XbMemberGatewayInteger $Claim.state_version 0)) { throw "claim_invalid" }
    $leaseExpiresAt = ConvertTo-XbMemberGatewayDateTimeOffset $Claim.lease_expires_at "claim_invalid"
    [void](ConvertTo-XbMemberGatewayDateTimeOffset $Claim.first_claimed_at "claim_invalid")
    [void](ConvertTo-XbMemberGatewayDateTimeOffset $Claim.server_time_utc "claim_invalid")
    if ($null -eq $Claim.request -or $Claim.request -isnot [pscustomobject]) { throw "claim_invalid" }
    return [pscustomobject]@{ Claim = $Claim; LeaseExpiresAt = $leaseExpiresAt }
}

# The one-line primitive request: claim.request plus four claim fields.
function New-XbAc2PrimitiveRequestLine {
    param([Parameter(Mandatory)]$Claim)
    $request = [ordered]@{}
    foreach ($property in $Claim.request.PSObject.Properties) { $request[$property.Name] = $property.Value }
    $request.job_id = [string]$Claim.job_id
    $request.attempt_no = [int]$Claim.attempt_no
    $request.first_claimed_at = [string]$Claim.first_claimed_at
    $request.server_time_utc = [string]$Claim.server_time_utc
    return (ConvertTo-XbGatewayJson -Body $request)
}

function Test-XbAc2FullySyntheticRequest {
    param([Parameter(Mandatory)]$Request)
    $name = [string]$Request.name
    $email = [string]$Request.email
    return ($name.StartsWith("ZZTEST ", [StringComparison]::Ordinal) -and $email.EndsWith("@example.invalid", [StringComparison]::OrdinalIgnoreCase))
}

# XB_WORKER_FAULT is inert unless -Book test, the configured database is on
# the test allowlist and is not the production book, and the request is
# fully synthetic. Returns "none", "refused" or the accepted fault name.
function Get-XbWorkerFaultDecision {
    param(
        [AllowNull()][AllowEmptyString()][string]$Fault,
        [Parameter(Mandatory)][string]$Book,
        [Parameter(Mandatory)]$Request,
        [AllowNull()]$FaultGuardConfig
    )
    if ([string]::IsNullOrEmpty($Fault)) { return "none" }
    if ($Book -cne "test" -or $null -eq $FaultGuardConfig) { return "refused" }
    $database = [string]$FaultGuardConfig.DatabaseName
    $production = [string]$FaultGuardConfig.ProductionBook
    if ([string]::IsNullOrWhiteSpace($database) -or [string]::IsNullOrWhiteSpace($production)) { return "refused" }
    if ([string]::Equals($database.Trim(), $production.Trim(), [StringComparison]::OrdinalIgnoreCase)) { return "refused" }
    $allowlisted = $false
    foreach ($entry in @($FaultGuardConfig.TestBookAllowlist)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$entry) -and [string]::Equals(([string]$entry).Trim(), $database.Trim(), [StringComparison]::OrdinalIgnoreCase)) { $allowlisted = $true }
    }
    if (-not $allowlisted -or -not (Test-XbAc2FullySyntheticRequest -Request $Request)) { return "refused" }
    if ($script:XbWorkerAllowedFaults -cnotcontains $Fault) { return "refused" }
    return $Fault
}

# Worker-synthesised primitive output (no primitive result available).
function New-XbWorkerSynthesisedOutput {
    param([Parameter(Mandatory)][string]$Outcome, [Parameter(Mandatory)][string]$ReasonCode, [Parameter(Mandatory)][bool]$SaveMayHaveHappened, [Parameter(Mandatory)][string]$ReleaseSha256)
    [ordered]@{
        outcome = $Outcome
        rule = "NONE"
        branch = "NONE"
        member_no = $null
        member_guid = $null
        # Conservative upper bound when the child may have saved.
        save_invoked = $SaveMayHaveHappened
        save_invocation_count = $(if ($SaveMayHaveHappened) { 1 } else { 0 })
        readback = $null
        reason_code = $ReasonCode
        dq_flags = [object[]]@()
        primitive = [ordered]@{ release_sha256 = $ReleaseSha256; rule_version = "XB-MN-1" }
        error_code = $null
    }
}

# Strict closed-shape check of the primitive stdout line. Returns an ordered
# copy on success, or $null when the output is unreadable or out of contract.
function ConvertFrom-XbAc2PrimitiveOutput {
    param([AllowNull()][string]$Text, [Parameter(Mandatory)][string]$ReleaseSha256)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $lines = @($Text -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($lines.Count -ne 1) { return $null }
    try { $value = $lines[0] | ConvertFrom-Json -ErrorAction Stop } catch { return $null }
    if ($null -eq $value -or $value -isnot [pscustomobject]) { return $null }
    $names = @($value.PSObject.Properties.Name)
    if ($names.Count -ne $script:XbPrimitiveOutputFields.Count) { return $null }
    foreach ($field in $script:XbPrimitiveOutputFields) { if ($names -cnotcontains $field) { return $null } }
    if ($value.outcome -isnot [string] -or $script:XbResultOutcomes -cnotcontains $value.outcome) { return $null }
    if ($value.rule -isnot [string] -or $script:XbResultRules -cnotcontains $value.rule) { return $null }
    if ($value.branch -isnot [string] -or $script:XbResultBranches -cnotcontains $value.branch) { return $null }
    if ($null -ne $value.member_no -and ($value.member_no -isnot [string] -or $value.member_no.Length -lt 1 -or $value.member_no.Length -gt 20)) { return $null }
    if ($null -ne $value.member_guid -and ($value.member_guid -isnot [string] -or $value.member_guid -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')) { return $null }
    if ($value.save_invoked -isnot [bool]) { return $null }
    if ($value.save_invocation_count -isnot [int] -or ([int]$value.save_invocation_count -ne 0 -and [int]$value.save_invocation_count -ne 1)) { return $null }
    if ([int]$value.save_invocation_count -ne $(if ($value.save_invoked) { 1 } else { 0 })) { return $null }
    $readback = $null
    if ($null -ne $value.readback) {
        if ($value.readback -isnot [pscustomobject]) { return $null }
        $readbackNames = @($value.readback.PSObject.Properties.Name)
        if ($readbackNames.Count -ne 3) { return $null }
        foreach ($field in @("found", "match", "created_by_integration_user")) {
            if ($readbackNames -cnotcontains $field -or $value.readback.$field -isnot [bool]) { return $null }
        }
        $readback = [ordered]@{ found = [bool]$value.readback.found; match = [bool]$value.readback.match; created_by_integration_user = [bool]$value.readback.created_by_integration_user }
    }
    if ($null -ne $value.reason_code -and ($value.reason_code -isnot [string] -or $script:XbResultReasonCodes -cnotcontains $value.reason_code)) { return $null }
    $flags = @($value.dq_flags)
    if ($null -eq $value.dq_flags -or $value.dq_flags -isnot [array] -or $flags.Count -gt 3) { return $null }
    $seen = @{}
    foreach ($flag in $flags) {
        if ($flag -isnot [string] -or $script:XbResultDqFlags -cnotcontains $flag -or $seen.ContainsKey($flag)) { return $null }
        $seen[$flag] = $true
    }
    if ($value.primitive -isnot [pscustomobject]) { return $null }
    $primitiveNames = @($value.primitive.PSObject.Properties.Name)
    if ($primitiveNames.Count -ne 2 -or $primitiveNames -cnotcontains "release_sha256" -or $primitiveNames -cnotcontains "rule_version") { return $null }
    if ($value.primitive.release_sha256 -cne $ReleaseSha256 -or $value.primitive.rule_version -cne "XB-MN-1") { return $null }
    if ($null -ne $value.error_code -and ($value.error_code -isnot [string] -or $value.error_code -cnotmatch '^[a-z0-9_.:-]{1,80}$')) { return $null }
    [ordered]@{
        outcome = [string]$value.outcome
        rule = [string]$value.rule
        branch = [string]$value.branch
        member_no = $value.member_no
        member_guid = $value.member_guid
        save_invoked = [bool]$value.save_invoked
        save_invocation_count = [int]$value.save_invocation_count
        readback = $readback
        reason_code = $value.reason_code
        dq_flags = [object[]]$flags
        primitive = [ordered]@{ release_sha256 = [string]$value.primitive.release_sha256; rule_version = "XB-MN-1" }
        error_code = $value.error_code
    }
}

function ConvertTo-XbWorkerProcessArgument {
    param([AllowEmptyString()][string]$Value)
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

# Runs the primitive child with a hard deadline. The request travels on stdin
# only; secrets travel only in the child environment, never in arguments.
function Invoke-XbAc2PrimitiveChild {
    param(
        [Parameter(Mandatory)][string]$PrimitiveScriptPath,
        [Parameter(Mandatory)][string]$Book,
        [Parameter(Mandatory)][string]$RequestLine,
        [switch]$EnableProductionAdapter,
        [hashtable]$ChildEnvironment = @{},
        [ValidateRange(1, 300)][int]$DeadlineSeconds = 300,
        [ValidateRange(100, 30000)][int]$KillWaitMilliseconds = 30000,
        [scriptblock]$KillAction = { param($Process) $Process.Kill() },
        [Parameter(Mandatory)][string]$ReleaseSha256
    )
    if (-not (Test-Path -LiteralPath $PrimitiveScriptPath -PathType Leaf) -or -not (Test-Path -LiteralPath $script:XbWindowsPowerShellPath -PathType Leaf)) {
        return (New-XbWorkerSynthesisedOutput -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "primitive_launch_failed" -SaveMayHaveHappened $false -ReleaseSha256 $ReleaseSha256)
    }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $script:XbWindowsPowerShellPath
    $arguments = @("-NoLogo", "-NoProfile", "-NonInteractive", "-File", $PrimitiveScriptPath, "-Book", $Book)
    if ($EnableProductionAdapter) { $arguments += "-EnableProductionAdapter" }
    $startInfo.Arguments = (($arguments | ForEach-Object { ConvertTo-XbWorkerProcessArgument -Value ([string]$_) }) -join " ")
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    [void]$startInfo.EnvironmentVariables.Remove("XB_WORKER_FAULT")
    # Least privilege: the AutoCount child never holds the gateway bearer.
    [void]$startInfo.EnvironmentVariables.Remove("XB_MEMBER_GATEWAY_WORKER_TOKEN")
    foreach ($key in @($ChildEnvironment.Keys)) {
        if ($null -eq $ChildEnvironment[$key]) { [void]$startInfo.EnvironmentVariables.Remove([string]$key) }
        else { $startInfo.EnvironmentVariables[[string]$key] = [string]$ChildEnvironment[$key] }
    }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw "primitive_start_failed" }
    }
    catch {
        $process.Dispose()
        return (New-XbWorkerSynthesisedOutput -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "primitive_launch_failed" -SaveMayHaveHappened $false -ReleaseSha256 $ReleaseSha256)
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $requestDelivered = $false
    try {
        $process.StandardInput.WriteLine($RequestLine)
        $process.StandardInput.Close()
        $requestDelivered = $true
    }
    catch { $requestDelivered = $false }
    try {
        if (-not $process.WaitForExit($DeadlineSeconds * 1000)) {
            try { & $KillAction $process } catch { }
            $exited = $false
            try { $exited = $process.WaitForExit($KillWaitMilliseconds) } catch { $exited = $false }
            if (-not $exited) {
                return (New-XbWorkerSynthesisedOutput -Outcome "OUTCOME_UNCERTAIN" -ReasonCode "child_termination_unconfirmed" -SaveMayHaveHappened $true -ReleaseSha256 $ReleaseSha256)
            }
            return (New-XbWorkerSynthesisedOutput -Outcome "OUTCOME_UNCERTAIN" -ReasonCode "child_deadline_exceeded" -SaveMayHaveHappened $true -ReleaseSha256 $ReleaseSha256)
        }
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        [void]$stderrTask.GetAwaiter().GetResult()
        if (-not $requestDelivered) {
            # The child cannot act without its request line; it has exited.
            return (New-XbWorkerSynthesisedOutput -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "primitive_launch_failed" -SaveMayHaveHappened $false -ReleaseSha256 $ReleaseSha256)
        }
        $output = $null
        if ([int]$process.ExitCode -eq 0) { $output = ConvertFrom-XbAc2PrimitiveOutput -Text $stdout -ReleaseSha256 $ReleaseSha256 }
        if ($null -eq $output) {
            return (New-XbWorkerSynthesisedOutput -Outcome "OUTCOME_UNCERTAIN" -ReasonCode "primitive_output_invalid" -SaveMayHaveHappened $true -ReleaseSha256 $ReleaseSha256)
        }
        return $output
    }
    finally {
        if ($process.HasExited) { $process.Dispose() }
    }
}

function New-XbMemberGatewayResultBody {
    param([Parameter(Mandatory)]$Claim, [Parameter(Mandatory)]$Output)
    $body = [ordered]@{
        schema_version = "xb.member.gateway.result.v2"
        job_id = [string]$Claim.job_id
        attempt_no = [int]$Claim.attempt_no
        lease_id = [string]$Claim.lease_id
        state_version = [int]$Claim.state_version
    }
    foreach ($field in $script:XbPrimitiveOutputFields) { $body[$field] = $Output[$field] }
    return $body
}

# Posts the identical body at most three times, only on transport failure or
# HTTP 5xx and only while the lease is live. HTTP 409 is final: discard.
function Send-XbMemberGatewayResult {
    param(
        [Parameter(Mandatory)][scriptblock]$GatewayRequest,
        [Parameter(Mandatory)][string]$GatewayBaseUrl,
        [Parameter(Mandatory)][string]$WorkerSession,
        [Parameter(Mandatory)][string]$JobId,
        [Parameter(Mandatory)][string]$BodyJson,
        [Parameter(Mandatory)][DateTimeOffset]$LeaseExpiresAt,
        [Parameter(Mandatory)][scriptblock]$UtcNow
    )
    $posts = 0
    $status = "result_unacknowledged"
    for ($attempt = 1; $attempt -le $script:XbWorkerMaxResultPosts; $attempt++) {
        if ((& $UtcNow) -ge $LeaseExpiresAt) { $status = "result_lease_expired"; break }
        $posts++
        try {
            [void](& $GatewayRequest $GatewayBaseUrl ("/v2/jobs/{0}/result" -f $JobId) "POST" $BodyJson $WorkerSession)
            $status = "result_accepted"
            break
        }
        catch {
            $code = [string]$_.Exception.Message
            if ($code -ceq "gateway_http_409") { $status = "result_conflict_discarded"; break }
            if ($code -ceq "gateway_transport_failed" -or $code -cmatch '^gateway_http_5[0-9][0-9]$') { $status = "result_unacknowledged"; continue }
            $status = "result_rejected"
            break
        }
    }
    return [pscustomobject]@{ status = $status; posts = $posts }
}

function Invoke-XbMemberGatewayWorkerCycle {
    param(
        [Parameter(Mandatory)][string]$GatewayBaseUrl,
        [Parameter(Mandatory)][switch]$EnableProductionWorker,
        [switch]$EnableProductionAdapter,
        [string]$Book,
        [string]$ReleaseSha256,
        [string]$PrimitiveScriptPath,
        [hashtable]$ChildEnvironment = @{},
        [AllowNull()]$FaultGuardConfig = $null,
        [AllowNull()][AllowEmptyString()][string]$WorkerFault = [Environment]::GetEnvironmentVariable("XB_WORKER_FAULT", "Process"),
        [string]$WorkerSession,
        [scriptblock]$GatewayRequest,
        [string]$MutexName = "Global\XB-AC2-MemberCreate",
        [ValidateRange(1, 300)][int]$DeadlineSeconds = 300,
        [ValidateRange(100, 30000)][int]$KillWaitMilliseconds = 30000,
        [scriptblock]$KillAction = { param($Process) $Process.Kill() },
        [scriptblock]$UtcNow = { [DateTimeOffset]::UtcNow }
    )
    if (-not $EnableProductionWorker) {
        return [pscustomobject]@{ status = "disabled"; writes = 0 }
    }
    if ($Book -cne "production" -and $Book -cne "test") { throw "worker_book_invalid" }
    if ($ReleaseSha256 -cnotmatch '^[0-9a-f]{64}$') { throw "worker_release_identity_invalid" }
    if ([string]::IsNullOrWhiteSpace($PrimitiveScriptPath)) { throw "worker_primitive_path_missing" }
    if ($null -eq $GatewayRequest) {
        $GatewayRequest = {
            param($base, $path, $method, $body, $session)
            Invoke-XbMemberGatewayRequest -GatewayBaseUrl $base -Path $path -Method $method -Body $body -WorkerSession $session
        }
    }
    if ([string]::IsNullOrWhiteSpace($WorkerSession)) {
        $WorkerSession = New-XbMemberGatewayWorkerSession
    }
    elseif ($WorkerSession -cnotmatch '^ws-[0-9a-f]{32}$') {
        throw "worker_session_invalid"
    }

    # 1. Readiness, dispatch and clock.
    try { $ready = & $GatewayRequest $GatewayBaseUrl "/readyz" "GET" $null $WorkerSession }
    catch { return [pscustomobject]@{ status = "gateway_unavailable"; writes = 0 } }
    if ($null -eq $ready -or $ready.ready -ne $true) { return [pscustomobject]@{ status = "gateway_not_ready"; writes = 0 } }
    if ($ready.dispatch_enabled -ne $true) { return [pscustomobject]@{ status = "dispatch_disabled"; writes = 0 } }
    try { $serverTime = ConvertTo-XbMemberGatewayDateTimeOffset $ready.server_time_utc "gateway_time_invalid" }
    catch { return [pscustomobject]@{ status = "clock_skew"; writes = 0 } }
    if ([Math]::Abs(((& $UtcNow) - $serverTime).TotalSeconds) -gt $script:XbWorkerClockSkewSeconds) {
        return [pscustomobject]@{ status = "clock_skew"; writes = 0 }
    }

    # 2. Mutex probe without waiting: a held mutex means another primitive is live.
    if (Test-XbAc2MemberCreateMutexHeld -Name $MutexName) {
        return [pscustomobject]@{ status = "mutex_held"; writes = 0 }
    }

    # 3. Claim.
    try { $claimResponse = & $GatewayRequest $GatewayBaseUrl "/v2/worker/claim" "POST" "{}" $WorkerSession }
    catch { return [pscustomobject]@{ status = "claim_failed"; writes = 0 } }
    try { $claimed = Get-XbMemberGatewayClaim -Claim $claimResponse }
    catch { return [pscustomobject]@{ status = "claim_invalid"; writes = 0 } }
    if ($null -eq $claimed) { return [pscustomobject]@{ status = "idle"; writes = 0 } }
    $claim = $claimed.Claim

    # 4. The primitive child (or a worker-side refusal of a fault hook).
    $faultDecision = Get-XbWorkerFaultDecision -Fault $WorkerFault -Book $Book -Request $claim.request -FaultGuardConfig $FaultGuardConfig
    if ($faultDecision -ceq "refused") {
        $output = New-XbWorkerSynthesisedOutput -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "fault_injection_refused" -SaveMayHaveHappened $false -ReleaseSha256 $ReleaseSha256
    }
    else {
        $requestLine = New-XbAc2PrimitiveRequestLine -Claim $claim
        $output = Invoke-XbAc2PrimitiveChild -PrimitiveScriptPath $PrimitiveScriptPath -Book $Book -RequestLine $requestLine -EnableProductionAdapter:$EnableProductionAdapter -ChildEnvironment $ChildEnvironment -DeadlineSeconds $DeadlineSeconds -KillWaitMilliseconds $KillWaitMilliseconds -KillAction $KillAction -ReleaseSha256 $ReleaseSha256
    }

    # 5. Result.
    $bodyJson = ConvertTo-XbGatewayJson -Body (New-XbMemberGatewayResultBody -Claim $claim -Output $output)
    if ($faultDecision -ceq "skip_result_post") {
        return [pscustomobject]@{ status = "result_skipped_by_fault"; outcome = $output.outcome; writes = [int]$output.save_invocation_count; posts = 0 }
    }
    $sent = Send-XbMemberGatewayResult -GatewayRequest $GatewayRequest -GatewayBaseUrl $GatewayBaseUrl -WorkerSession $WorkerSession -JobId ([string]$claim.job_id) -BodyJson $bodyJson -LeaseExpiresAt $claimed.LeaseExpiresAt -UtcNow $UtcNow
    return [pscustomobject]@{ status = $sent.status; outcome = $output.outcome; writes = [int]$output.save_invocation_count; posts = $sent.posts }
}
