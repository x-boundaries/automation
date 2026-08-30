[CmdletBinding()]
param(
    [string]$GatewayBaseUrl = [Environment]::GetEnvironmentVariable("XB_MEMBER_GATEWAY_URL", "Process"),
    [string]$WorkerId = "ac2-member-worker",
    [switch]$EnableProductionWorker,
    [switch]$EnableProductionAdapter
)

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "ac2_member_gateway_worker_lib.ps1")
. (Join-Path $PSScriptRoot "ac2_member_gateway_autocount_adapter.ps1")

if (-not $EnableProductionWorker) {
    [pscustomobject]@{ status = "disabled"; writes = 0; dispatch_fence = $false } | ConvertTo-Json -Compress
    exit 0
}
if ([string]::IsNullOrWhiteSpace($GatewayBaseUrl)) { throw "gateway_url_missing" }

$session = New-XbAutoCountSession -EnableProductionAdapter:$EnableProductionAdapter
$probe = {
    param([string]$Candidate)
    $member = Get-XbAutoCountMember -Session $session -MemberNo $Candidate
    if ($null -eq $member) {
        [pscustomobject]@{ status = "FREE"; probe_reference = "local-read-only" }
    } else {
        [pscustomobject]@{ status = "OCCUPIED"; probe_reference = "local-read-only" }
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
    $outcome = New-XbAutoCountMember -Session $session -Member $member -EnableProductionAdapter:$EnableProductionAdapter
    $check = Compare-XbAutoCountMemberReadBack -Expected $member -Actual $outcome.ReadBack
    [pscustomobject]@{ save_invocation_count = $outcome.SaveInvocationCount; readback_match = $check.Match; mismatches = $check.Mismatches }
}

$result = Invoke-XbMemberGatewayWorkerCycle -GatewayBaseUrl $GatewayBaseUrl -WorkerId $WorkerId -EnableProductionWorker:$EnableProductionWorker -EnableProductionAdapter:$EnableProductionAdapter -ProbeMember $probe -CreateMember $create
$result | ConvertTo-Json -Compress
