[CmdletBinding()]
param(
    [string]$GatewayBaseUrl = [Environment]::GetEnvironmentVariable("XB_MEMBER_GATEWAY_URL", "Process"),
    [string]$WorkerId = "ac2-member-worker",
    [string]$WorkerHostBinding = [Environment]::GetEnvironmentVariable("XB_MEMBER_GATEWAY_WORKER_HOST_BINDING", "Process"),
    [switch]$EnableProductionWorker,
    [switch]$EnableProductionAdapter,
    [switch]$ChildExternalWrite,
    [string]$WriteDeadlineUtc,
    [scriptblock]$SessionFactory,
    [scriptblock]$MemberCommandFactory
)

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "ac2_member_gateway_worker_lib.ps1")
. (Join-Path $PSScriptRoot "ac2_member_gateway_autocount_adapter.ps1")

function Invoke-XbMemberGatewayCreateMember {
    param(
        [Parameter(Mandatory)]$Session,
        [Parameter(Mandatory)]$Job,
        [Parameter(Mandatory)]$Allocation,
        [Parameter(Mandatory)][switch]$EnableProductionAdapter,
        [scriptblock]$MemberCommandFactory
    )
    $submitted = [DateTimeOffset]::Parse([string]$Job.member_payload.create_time)
    $singapore = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId($submitted, "Singapore Standard Time")
    $registerDate = $singapore.Date
    $expiryDate = $registerDate.AddYears(2).AddDays(-1)
    $birthdayMonth = [datetime]::ParseExact([string]$Job.member_payload.birthday_month, "MMMM", [Globalization.CultureInfo]::InvariantCulture).Month
    $member = @{
        MemberNo = [string]$Allocation.member_no
        MemberType = "Default"
        Name = [string]$Job.member_payload.name
        MobilePhone = [string]$Job.member_payload.phone
        EmailAddress = [string]$Job.member_payload.email
        DOB = ("2000-{0:00}-01" -f $birthdayMonth)
        RegisterDate = $registerDate.ToString("yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
        ExpiryDate = $expiryDate.ToString("yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
        OpeningPoints = 0
    }
    $outcome = New-XbAutoCountMember -Session $Session -Member $member -EnableProductionAdapter:$EnableProductionAdapter -MemberCommandFactory $MemberCommandFactory
    $check = Compare-XbAutoCountMemberReadBack -Expected $outcome.Expected -Actual $outcome.ReadBack
    $readbackFound = [bool]$check.Found
    $readbackMatch = [bool]$check.Match
    [pscustomobject]@{
        save_invocation_count = $outcome.SaveInvocationCount
        readback_found = $readbackFound
        readback_match = $readbackMatch
        status = if (-not $readbackFound) { "WRITE_OUTCOME_UNCERTAIN" } elseif ($readbackMatch) { "CREATED_VERIFIED" } else { "CREATED_READBACK_MISMATCH" }
        error_code = if ($readbackFound -and $readbackMatch) { $null } elseif (-not $readbackFound) { "readback_absent" } else { "readback_mismatch" }
        mismatches = $check.Mismatches
    }
}

if ($ChildExternalWrite) {
    $ErrorActionPreference = "Stop"
    $watchdog = $null
    $exitCode = 1
    try {
        if ([string]::IsNullOrWhiteSpace($WriteDeadlineUtc)) { throw "writer_deadline_missing" }
        $deadline = ConvertTo-XbMemberGatewayDateTimeOffset $WriteDeadlineUtc "writer_deadline_invalid"
        $delayMilliseconds = [int][Math]::Floor(($deadline - [DateTimeOffset]::UtcNow).TotalMilliseconds)
        if ($delayMilliseconds -le 0) { throw "writer_deadline_expired" }
        $watchdogCallback = [System.Threading.TimerCallback]{
            param($State)
            try { [System.Diagnostics.Process]::GetCurrentProcess().Kill() } catch { }
        }
        $watchdog = [System.Threading.Timer]::new($watchdogCallback, $null, $delayMilliseconds, -1)
        $payloadLine = [Console]::In.ReadLine()
        if ([string]::IsNullOrWhiteSpace($payloadLine)) { throw "writer_payload_missing" }
        $payload = $payloadLine | ConvertFrom-Json -ErrorAction Stop
        $childSession = New-XbAutoCountSession -EnableProductionAdapter:$EnableProductionAdapter
        $childOutcome = Invoke-XbMemberGatewayCreateMember -Session $childSession -Job $payload.job -Allocation $payload.allocation -EnableProductionAdapter:$EnableProductionAdapter
        $childOutcome | ConvertTo-Json -Depth 12 -Compress
        $exitCode = 0
    }
    catch {
        $exitCode = 1
    }
    finally {
        if ($null -ne $watchdog) { $watchdog.Dispose() }
    }
    exit $exitCode
}

if (-not $EnableProductionWorker) {
    [pscustomobject]@{ status = "disabled"; writes = 0; dispatch_fence = $false } | ConvertTo-Json -Compress
    exit 0
}
if ([string]::IsNullOrWhiteSpace($GatewayBaseUrl)) { throw "gateway_url_missing" }

$session = New-XbAutoCountSession -EnableProductionAdapter:$EnableProductionAdapter -SessionFactory $SessionFactory
$probe = {
    param([string]$Candidate)
    $member = Get-XbAutoCountMember -Session $session -MemberNo $Candidate -MemberCommandFactory $MemberCommandFactory
    if ($null -eq $member) {
        [pscustomobject]@{ status = "FREE" }
    } else {
        [pscustomobject]@{ status = "OCCUPIED" }
    }
}
$create = {
    param($Job, $Allocation)
    Invoke-XbMemberGatewayCreateMember -Session $session -Job $Job -Allocation $Allocation -EnableProductionAdapter:$EnableProductionAdapter -MemberCommandFactory $MemberCommandFactory
}

$writerScriptPath = $PSCommandPath
$writerProcessFactory = {
    param($Payload, $StopAt)
    Start-XbMemberGatewayChildWriter -ScriptPath $writerScriptPath -Arguments @("-ChildExternalWrite", "-EnableProductionAdapter", "-WriteDeadlineUtc", $StopAt.ToString("o"))
}.GetNewClosure()

$result = Invoke-XbMemberGatewayWorkerCycle -GatewayBaseUrl $GatewayBaseUrl -WorkerId $WorkerId -WorkerHostBinding $WorkerHostBinding -EnableProductionWorker:$EnableProductionWorker -EnableProductionAdapter:$EnableProductionAdapter -ProbeMember $probe -CreateMember $create -WriterProcessFactory $writerProcessFactory
$result | ConvertTo-Json -Compress
