# Registers the weekly read-only Drive mirror task (#273).
#
# REGISTERING THIS TASK NEEDS A SEPARATE OWNER OK. Do not run it as part of a build, a
# review or a test. Do the one manual run in docs/drive_mirror_runbook.md first.
#
# What it registers:
# - One run per week, Sunday 03:00 (local time on the server PC, which is set to Singapore
#   time). No hourly repetition.
# - StartWhenAvailable: a missed Sunday run starts as soon as the user is next logged on.
# - MultipleInstances IgnoreNew and a 30-minute ExecutionTimeLimit.
# - Runs as the named user, only when that user is logged on (Interactive logon). Google
#   Drive for desktop drive letters exist only inside the signed-in user's session, so S4U
#   or a service account would not see the Drive folder.
# - Action: <PythonExe> "<RepoRoot>\scripts\mirror_main_to_drive.py" --config "<ConfigPath>"
#
# It refuses to replace an existing task (no -Force). To change the task, remove it in
# Task Scheduler first (see the runbook), then run this again with the owner's OK.
#
# Example (fake user name):
#   powershell -NoProfile -File scripts\install_mirror_main_to_drive_task.ps1 `
#     -PythonExe "C:\Program Files\Python312\python.exe" -UserId "EXAMPLE-PC\example-user"

param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,

    [Parameter(Mandatory = $true)]
    [string]$UserId,

    [string]$TaskName = "XB Drive Mirror of main (weekly)",
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$ConfigPath = "config\mirror_main_to_drive.local.json"
)

$ErrorActionPreference = "Stop"

if (-not [System.IO.Path]::IsPathRooted($PythonExe)) {
    throw "PythonExe must be the full path to python.exe."
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "PythonExe was not found."
}
if ([string]::IsNullOrWhiteSpace($UserId)) {
    throw "UserId is required (the Windows user who is signed in to Google Drive for desktop)."
}

$scriptPath = Join-Path $RepoRoot "scripts\mirror_main_to_drive.py"
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Mirror script not found under RepoRoot."
}

if (-not [System.IO.Path]::IsPathRooted($ConfigPath)) {
    $ConfigPath = Join-Path $RepoRoot $ConfigPath
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Local settings file not found. Copy config\mirror_main_to_drive.example.json first."
}
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw "A scheduled task with this name already exists. Nothing was changed."
}

$startAt = (Get-Date).Date.AddHours(3)
$actionArgs = "`"$scriptPath`" --config `"$ConfigPath`""
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $actionArgs -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Sunday -At $startAt
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal `
    -UserId $UserId `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Weekly read-only copy of GitHub main into the marked company Drive folder (#273). Do not edit the Drive copy." | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Schedule: every Sunday at 03:00 local time; a missed run starts at next logon."
Write-Host "Runs only when the named user is logged on."
