[CmdletBinding()]
param(
    [string]$GatewayBaseUrl = [Environment]::GetEnvironmentVariable("XB_MEMBER_GATEWAY_URL", "Process"),
    [string]$WorkerId = "ac2-member-worker",
    [switch]$EnableProductionWorker,
    [switch]$EnableProductionAdapter,
    [scriptblock]$SessionFactory,
    [scriptblock]$MemberCommandFactory
)

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "ac2_member_gateway_worker_lib.ps1")
. (Join-Path $PSScriptRoot "ac2_member_gateway_autocount_adapter.ps1")

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
    $outcome = New-XbAutoCountMember -Session $session -Member $member -EnableProductionAdapter:$EnableProductionAdapter -MemberCommandFactory $MemberCommandFactory
    $check = Compare-XbAutoCountMemberReadBack -Expected $outcome.Expected -Actual $outcome.ReadBack
    $readbackFound = [bool]$check.Found
    $readbackMatch = [bool]$check.Match
    $status = if (-not $readbackFound) { "WRITE_OUTCOME_UNCERTAIN" } elseif ($readbackMatch) { "CREATED_VERIFIED" } else { "CREATED_READBACK_MISMATCH" }
    [pscustomobject]@{
        save_invocation_count = $outcome.SaveInvocationCount
        readback_found = $readbackFound
        readback_match = $readbackMatch
        status = $status
        error_code = if ($readbackFound -and $readbackMatch) { $null } elseif (-not $readbackFound) { "readback_absent" } else { "readback_mismatch" }
        mismatches = $check.Mismatches
    }
}

$result = Invoke-XbMemberGatewayWorkerCycle -GatewayBaseUrl $GatewayBaseUrl -WorkerId $WorkerId -EnableProductionWorker:$EnableProductionWorker -EnableProductionAdapter:$EnableProductionAdapter -ProbeMember $probe -CreateMember $create
$result | ConvertTo-Json -Compress
