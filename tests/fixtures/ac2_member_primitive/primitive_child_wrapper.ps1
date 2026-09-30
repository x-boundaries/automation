# Test-only child used in place of the installed primitive entry point. It runs
# the REAL primitive (scripts/ac2_member_create_primitive.ps1, library mode)
# against the in-memory AutoCount double, with the same CLI and stdin/stdout
# contract as the installed entry point. Synthetic data only.
[CmdletBinding()]
param(
    [string]$Book,
    [switch]$EnableProductionAdapter
)
$ErrorActionPreference = "Stop"
# Dot-sourcing the primitive rebinds its own parameters in this scope.
$childBook = $Book
$childProductionAdapter = [bool]$EnableProductionAdapter
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
. (Join-Path $repositoryRoot "scripts\ac2_member_create_primitive.ps1") -LibraryOnly
. (Join-Path $PSScriptRoot "fake_autocount.ps1")

$line = [Console]::In.ReadLine()
$case = [Environment]::GetEnvironmentVariable("XB_TEST_FAKE_CASE", "Process") | ConvertFrom-Json
$request = $line | ConvertFrom-Json
$now = [DateTimeOffset]::Parse([Environment]::GetEnvironmentVariable("XB_TEST_UTC_NOW", "Process"), [Globalization.CultureInfo]::InvariantCulture)
$cfg = $case.config
$config = [pscustomobject]@{
    DatabaseName = [string]$cfg.DatabaseName
    LoginUserId = [string]$cfg.LoginUserId
    IntegrationUserId = [string]$cfg.IntegrationUserId
    ProductionBook = [string]$cfg.ProductionBook
    TestBookAllowlist = [string[]]@($cfg.TestBookAllowlist)
    Fault = ""
    PasswordEnvironmentVariable = [Environment]::GetEnvironmentVariable("XB_AC2_PASSWORD_ENV_VAR", "Process")
}
$fakeBook = New-XbFakeAutoCountBook -Case $case -Request $request -ServerNow $now -IntegrationUserId $config.IntegrationUserId -DatabaseName $config.DatabaseName
$result = Invoke-XbAc2MemberCreatePrimitive -RequestLine $line -Book $childBook -Config $config -EnableProductionAdapter:$childProductionAdapter -SessionFactory $fakeBook.SessionFactory -MemberCommandFactory $fakeBook.MemberCommandFactory -MutexName ([Environment]::GetEnvironmentVariable("XB_TEST_MUTEX_NAME", "Process")) -MutexWaitMilliseconds ([int][Environment]::GetEnvironmentVariable("XB_TEST_MUTEX_WAIT_MS", "Process")) -UtcNow $now
[Console]::Out.WriteLine((ConvertTo-XbAc2AsciiJson -Value $result))
exit 0
