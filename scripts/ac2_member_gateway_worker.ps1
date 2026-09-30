[CmdletBinding()]
param(
    [string]$GatewayBaseUrl = [Environment]::GetEnvironmentVariable("XB_MEMBER_GATEWAY_URL", "Process"),
    [string]$Book,
    [switch]$EnableProductionWorker,
    [switch]$EnableProductionAdapter
)

# Member-write v2 worker entry point: one cycle of readyz -> claim ->
# primitive child -> result (W-G2-149 section 4.5). The AutoCount session,
# probe, decision, save and readback all live in the primitive child
# (ac2_member_create_primitive.ps1); this process never opens AutoCount.

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "ac2_member_gateway_worker_lib.ps1")
. (Join-Path $PSScriptRoot "ac2_member_gateway_autocount_adapter.ps1")

if (-not $EnableProductionWorker) {
    [pscustomobject]@{ status = "disabled"; writes = 0 } | ConvertTo-Json -Compress
    exit 0
}
if ([string]::IsNullOrWhiteSpace($GatewayBaseUrl)) { throw "gateway_url_missing" }
if ($Book -cne "production" -and $Book -cne "test") { throw "worker_book_invalid" }

# DPAPI custody: the launcher decrypted the integration-user password into
# this process environment. Move it out of this process environment at once;
# it is handed only to the primitive child environment and is never logged
# or placed on a command line.
$passwordEnvironmentVariable = [Environment]::GetEnvironmentVariable("XB_AC2_PASSWORD_ENV_VAR", "Process")
if ([string]::IsNullOrWhiteSpace($passwordEnvironmentVariable)) { $passwordEnvironmentVariable = "XB_AC2_PASSWORD" }
if ($passwordEnvironmentVariable -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw "worker_password_variable_invalid" }
$childEnvironment = @{
    $passwordEnvironmentVariable = [Environment]::GetEnvironmentVariable($passwordEnvironmentVariable, "Process")
    XB_AC2_PASSWORD_ENV_VAR = $passwordEnvironmentVariable
}
[Environment]::SetEnvironmentVariable($passwordEnvironmentVariable, $null, "Process")

$allowRaw = [Environment]::GetEnvironmentVariable("XB_AC2_TEST_BOOK_ALLOWLIST", "Process")
$faultGuardConfig = [pscustomobject]@{
    DatabaseName = [string][Environment]::GetEnvironmentVariable("XB_AC2_DATABASE_NAME", "Process")
    ProductionBook = [string][Environment]::GetEnvironmentVariable("XB_AC2_PRODUCTION_BOOK", "Process")
    TestBookAllowlist = [string[]]@($(if ([string]::IsNullOrWhiteSpace($allowRaw)) { @() } else { $allowRaw.Split([char[]]@(';'), [StringSplitOptions]::RemoveEmptyEntries) }))
}

$releaseSha256 = Get-XbAc2ReleaseIdentity -PackageRoot $PSScriptRoot
$result = Invoke-XbMemberGatewayWorkerCycle -GatewayBaseUrl $GatewayBaseUrl -Book $Book -EnableProductionWorker:$EnableProductionWorker -EnableProductionAdapter:$EnableProductionAdapter -ReleaseSha256 $releaseSha256 -PrimitiveScriptPath (Join-Path $PSScriptRoot "ac2_member_create_primitive.ps1") -ChildEnvironment $childEnvironment -FaultGuardConfig $faultGuardConfig -MutexName $script:XbAc2MemberCreateMutexName -DeadlineSeconds 300 -KillWaitMilliseconds 30000
$childEnvironment = $null
$result | ConvertTo-Json -Compress
