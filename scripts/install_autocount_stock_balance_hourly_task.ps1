<#
Registers the hourly AC2 stock balance export (#245) as a Windows Task Scheduler task.

NOT RUN BY THE BUILD. Registering this task needs a separate owner OK, given only after the
manual VM proof runs (runbook steps V1-V6) pass. See
docs/autocount2-automation/stock_balance_hourly_runbook.md, step V7.

Behaviour:
- Runs every hour, 24/7, at RunMinute past the hour (00:00 + RunMinute, then hourly).
- MultipleInstances IgnoreNew: a new hour never starts while the previous run is still going.
- ExecutionTimeLimit 15 minutes.
- RunLevel Limited (Task Scheduler's least-privilege level; "LeastPrivilege" in task XML).
- LogonType S4U by default (runs whether or not the user is signed in, no stored password).
- UserId has no default: the owner names the Windows account explicitly.
- Refuses if a task with the same name already exists. It never replaces an existing task;
  remove or rename the old task by hand first if a change is really intended.

Windows PowerShell 5.1 compatible.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$UserId,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$TaskName = "XB AC2 Hourly Stock Balance",
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = "python.exe",
    [ValidateRange(0, 59)]
    [int]$RunMinute = 0,
    [ValidateSet("S4U", "Password")]
    [string]$LogonType = "S4U"
)

$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $RepoRoot "scripts\autocount_stock_balance_hourly.py"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Export script not found under the repository root."
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Local settings file not found."
}
if ([string]::IsNullOrWhiteSpace($UserId)) {
    throw "UserId is required."
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw "Refusing: a scheduled task with this name already exists. It is never replaced by this installer."
}

# Daily trigger at 00:00 + RunMinute, repeating every hour for one day. Each day's trigger
# starts a new one-day repetition window, so the task repeats hourly indefinitely. This form
# works on every Windows PowerShell 5.1 host, unlike an unbounded RepetitionDuration.
$startAt = (Get-Date).Date.AddMinutes($RunMinute)
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
$repeat = New-ScheduledTaskTrigger -Once -At $startAt `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 1)
$trigger.Repetition = $repeat.Repetition

$actionArgs = "`"$scriptPath`" --config `"$ConfigPath`""
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $actionArgs -WorkingDirectory $RepoRoot
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal `
    -UserId $UserId `
    -LogonType $LogonType `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Hourly read-only AC2 stock balance export (#245). Writes hourly CSV + manifest folders, a verified latest pair and last_run_status.json." | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Runs hourly at minute $RunMinute, IgnoreNew, 15 minute limit, RunLevel Limited, LogonType $LogonType."
