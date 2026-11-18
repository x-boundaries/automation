# Registers the read-only daily health check task on the server PC (#325 slice H1).
#
# REGISTERING THIS TASK NEEDS A SEPARATE OWNER OK ("HEALTH TASK", live step L6). Do not run it with
# -Register as part of a build, a review or a test. Run the dry run first; it is the default.
#
# What it registers (only with -Register and without -DryRun):
# - \XB\XB-DailyHealth, once a day at 07:30 local time (the server PC runs on Singapore time).
# - StartWhenAvailable: a missed 07:30 run starts as soon as the user is next logged on.
# - MultipleInstances IgnoreNew and a 5-minute ExecutionTimeLimit (PT5M).
# - Runs as the named user, only when that user is logged on (Interactive logon), least privilege
#   (RunLevel Limited). Use the same user as the EnergyGrid tasks: Google Drive for desktop drive
#   letters and the Credential Manager entry exist only for that signed-in user.
# - Action: <PythonExe> "<RepoRoot>\scripts\xb_daily_health.py" run --config "<ConfigPath>"
# - Task Scheduler folder: -TaskPath, default \XB\. It must start and end with a backslash and name
#   at least one folder; letters, digits, spaces, _ and - only.
# - RepoRoot defaults to the clone this script is in. It is worked out in the script body because
#   Windows PowerShell 5.1 leaves $PSScriptRoot empty inside param() defaults of a script with
#   [Parameter()] attributes (the #273 lesson).
#
# Before anything is registered it runs `xb_daily_health.py check-settings` with the same Python and
# settings file, and stops if the settings are not valid. That check reads the settings file only.
#
# It refuses to replace an existing task with the same name in that folder (no -Force). To change
# the task, remove it in Task Scheduler first (see docs/daily_health_check.md), then run this again
# with the owner's OK.
#
# Dry run (the default, and always when -DryRun is given): checks every argument and the settings,
# builds the task, prints what it would register and stops before Register-ScheduledTask. Nothing is
# registered. The tests use -DryRun.
#
# Example (fake user name):
#   powershell -NoProfile -File scripts\install_daily_health_task.ps1 `
#     -PythonExe "C:\Program Files\Python312\python.exe" -UserId "EXAMPLE-PC\example-user"
#   ... then, with the owner's OK, the same command with -Register.

param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,

    [Parameter(Mandatory = $true)]
    [string]$UserId,

    [string]$TaskName = "XB-DailyHealth",
    [string]$TaskPath = "\XB\",
    [string]$RepoRoot = "",
    [string]$ConfigPath = "config\daily_health.local.json",
    [switch]$Register,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ($TaskPath -notmatch '^(\\[A-Za-z0-9_-][A-Za-z0-9 _-]*)+\\$') {
    throw "TaskPath must look like \XB\ (start and end with a backslash; letters, digits, spaces, _ and - only)."
}
if ($TaskName -notmatch '^[A-Za-z0-9][A-Za-z0-9 ()._-]{0,127}$') {
    throw "TaskName may use letters, digits, spaces and ( ) . _ - only."
}
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

if (-not [System.IO.Path]::IsPathRooted($PythonExe)) {
    throw "PythonExe must be the full path to python.exe."
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "PythonExe was not found."
}
if ([string]::IsNullOrWhiteSpace($UserId)) {
    throw "UserId is required (the Windows user who is signed in to Google Drive for desktop)."
}

$scriptPath = Join-Path $RepoRoot "scripts\xb_daily_health.py"
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Health check script not found under RepoRoot."
}

if (-not [System.IO.Path]::IsPathRooted($ConfigPath)) {
    $ConfigPath = Join-Path $RepoRoot $ConfigPath
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Local settings file not found. Copy config\daily_health.example.json first."
}
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path

# Read-only: the script checks the settings file and prints {"ok": true} or a reason code.
$check = & $PythonExe $scriptPath check-settings --config $ConfigPath
$checkExit = $LASTEXITCODE
if ($checkExit -ne 0) {
    throw "Settings check failed: $check"
}

$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw "A scheduled task with this name already exists in $TaskPath. Nothing was changed."
}

$startAt = (Get-Date).Date.AddHours(7).AddMinutes(30)
$actionArgs = "`"$scriptPath`" run --config `"$ConfigPath`""
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $actionArgs -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal `
    -UserId $UserId `
    -LogonType Interactive `
    -RunLevel Limited

if ($DryRun -or -not $Register) {
    Write-Host "Dry run: would register $TaskPath$TaskName"
    Write-Host "Schedule: daily at 07:30 local time; ExecutionTimeLimit PT5M; MultipleInstances IgnoreNew."
    Write-Host "Repo root: $RepoRoot"
    Write-Host "Settings check: $check"
    Write-Host "Nothing was registered."
    exit 0
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -TaskPath $TaskPath `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Read-only daily health check (#325): judges the scheduled jobs and posts one codes-only event to n8n XB Daily Health." | Out-Null

Write-Host "Registered scheduled task: $TaskPath$TaskName"
Write-Host "Schedule: daily at 07:30 local time; a missed run starts at next logon."
Write-Host "Runs only when the named user is logged on."
