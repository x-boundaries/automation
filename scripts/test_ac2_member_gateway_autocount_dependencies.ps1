[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$AssemblyRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSEdition -ne "Desktop" -or $PSVersionTable.PSVersion.Major -ne 5) {
    throw "autocount_windows_powershell_5_required"
}
if (-not [Environment]::Is64BitProcess) { throw "autocount_64bit_powershell_required" }

$root = [IO.Path]::GetFullPath($AssemblyRoot)
if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw "autocount_assembly_path_invalid" }
$requiredAssemblies = @(
    "AutoCount.dll",
    "AutoCount.Accounting.dll",
    "AutoCount.Invoicing.dll",
    "AutoCount.ImportExport.dll",
    "AutoCount.Tools.dll"
)
$loaded = @{}
foreach ($name in $requiredAssemblies) {
    $path = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "autocount_assembly_missing" }
    $loaded[$name] = [Reflection.Assembly]::ReflectionOnlyLoadFrom($path)
}

$core = $loaded["AutoCount.dll"]
$invoicing = $loaded["AutoCount.Invoicing.dll"]
$requiredTypes = @(
    @($core, "AutoCount.Data.DBSetting"),
    @($core, "AutoCount.Authentication.UserSession"),
    @($invoicing, "AutoCount.BonusPoint.Member.MemberCommand")
)
foreach ($requirement in $requiredTypes) {
    if ($null -eq $requirement[0].GetType($requirement[1], $false, $false)) { throw "autocount_required_type_missing" }
}

$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)
$methodNames = @($memberCommand.GetMethods() | ForEach-Object Name)
foreach ($name in @("Create", "GetMember", "NewMember", "SaveMember")) {
    if ($methodNames -notcontains $name) { throw "autocount_required_method_missing" }
}

[pscustomobject]@{
    status = "dependency_probe_pass"
    powershell_major = $PSVersionTable.PSVersion.Major
    process_bitness = 64
    assembly_count = $requiredAssemblies.Count
    required_type_count = $requiredTypes.Count
    required_method_count = 4
} | ConvertTo-Json -Compress
