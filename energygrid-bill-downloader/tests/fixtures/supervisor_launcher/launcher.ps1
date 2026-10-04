[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$CheckoutRoot,
    [Parameter(Mandatory = $true)][string]$CredentialPath,
    [Parameter(Mandatory = $true)][string]$BrowserCachePath,
    [Parameter(Mandatory = $true)][string]$ExpectedBranch,
    [Parameter(Mandatory = $true)][string[]]$AuthorisedLauncherRootWriteSid,
    [Parameter(Mandatory = $true)][ValidateSet('run')][string]$Command,
    [Parameter(Mandatory = $true)][string]$LogRoot,
    [Parameter(Mandatory = $true)][string]$RunId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher_lib.ps1')

$config = ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($ConfigPath))
if ($null -eq $config -or
    $config.mode -notin @('positive', 'noise', 'wrongcmd', 'early', 'wrongparent', 'launcher-exit', 'quick-exit', 'deadchild', 'idle') -or
    $config.module_sha256 -notmatch '^[0-9a-fA-F]{64}$') {
    throw 'EG_FIXTURE_CONFIG_INVALID'
}
$fixtureModule = Join-Path (Split-Path -Parent $PSScriptRoot) 'energygrid_bill_downloader.py'
Set-EgFixturePythonEnvironment -ModulePath $fixtureModule -ExpectedSha256 $config.module_sha256
$exitCode = Invoke-EgFixtureApplication -PythonExe $PythonExe -ConfigPath $ConfigPath -Mode ([string]$config.mode)
exit $exitCode
