param(
    [string]$TaskName = "AutoCount Daily Stock Extract",
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = "python.exe",
    [string]$ConfigPath = "D:\AutoCountStockExtract\autocount_stock_extract.local.json",
    [string]$StartTime = "02:00"
)

$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $RepoRoot "scripts\autocount_stock_extract.py"

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Extractor script not found: $scriptPath"
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Local config not found: $ConfigPath"
}

$timeParts = $StartTime.Split(":")
if ($timeParts.Count -ne 2) {
    throw "StartTime must use HH:mm format, for example 02:00"
}

$today = Get-Date
$triggerAt = Get-Date -Year $today.Year -Month $today.Month -Day $today.Day -Hour ([int]$timeParts[0]) -Minute ([int]$timeParts[1]) -Second 0

$actionArgs = "`"$scriptPath`" --config `"$ConfigPath`""
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $actionArgs -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerAt
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel LeastPrivilege

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Runs the read-only AutoCount stock extractor and writes archive batches plus run manifests." `
    -Force

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Script: $scriptPath"
Write-Host "Config: $ConfigPath"
Write-Host "Daily start time: $StartTime"
