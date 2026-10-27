<#
Registers the hourly AC2 stock balance export (#245) as a Windows Task Scheduler task.

NOT RUN BY THE BUILD. Registering this task needs a separate owner OK, given only after the
manual VM proof runs (runbook steps V1-V6) pass. See
docs/autocount2-automation/stock_balance_hourly_runbook.md, step V7.

Behaviour:
- Runs every hour, 24/7, at schedule.run_minute past the hour (00:00 + run_minute, then
  hourly). The minute comes from the validated settings; there is no separate parameter.
- Repetition StopAtDurationEnd is false, so the end of each day's repetition window never
  stops a run that is still going.
- MultipleInstances IgnoreNew: a new hour never starts while the previous run is still going.
- ExecutionTimeLimit 15 minutes (the exporter's lock rules assume this limit).
- RunLevel Limited (Task Scheduler's least-privilege level; "LeastPrivilege" in task XML).
- PythonExe is the full path to python.exe, never a name resolved from PATH.
- The settings are validated first with that same interpreter (--dry-run-config, reads no
  data). The logon type comes from the validated settings, so only the two supported pairs
  can be registered:
    identity.auth_mode windows_integrated -> LogonType S4U (no stored password)
    identity.auth_mode sql_login          -> LogonType Password (Get-Credential prompt)
- UserId has no default: the owner names the Windows account explicitly.
- Task Scheduler folder: -TaskPath, default \XB\ (the company folder). It must start and end
  with a backslash and name at least one folder; letters, digits, spaces, _ and - only, and
  no folder name starts or ends with a space.
- Refuses if a task with the same name already exists in that folder. It never replaces an
  existing task; remove or rename the old task by hand first if a change is really intended.
  It warns (but goes on) if a task with the same name is in the Task Scheduler root, as the
  earlier installer put it there; moving that task is a separate owner-approved step.
- RepoRoot defaults to the clone this script is in. It is worked out in the script body
  because Windows PowerShell 5.1 leaves $PSScriptRoot empty inside param() defaults of a
  script with [Parameter()] attributes.
- -DryRun runs every check (including pyodbc and --dry-run-config), prints what it would
  register and stops before the password prompt and Register-ScheduledTask. Nothing is
  registered. The tests use it.
- After registering, do the supervised "Run now" in runbook step V7 before leaving it enabled.

Windows PowerShell 5.1 compatible. Run it with -ExecutionPolicy Bypass (this one command only):
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autocount_stock_balance_hourly_task.ps1 `
      -UserId "EXAMPLE-PC\example-user" -ConfigPath "C:\path\to\settings.local.json" `
      -PythonExe "C:\path\to\python.exe" -DryRun
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$UserId,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [string]$TaskName = "XB AC2 Hourly Stock Balance",
    [string]$TaskPath = "\XB\",
    [string]$RepoRoot = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# Case-sensitive and anchored with \A and \z, so a trailing line break or a look-alike letter
# never passes.
if ($TaskPath -cnotmatch '\A(\\[A-Za-z0-9_-]([A-Za-z0-9 _-]*[A-Za-z0-9_-])?)+\\\z') {
    throw "TaskPath must look like \XB\ (start and end with a backslash; letters, digits, spaces, _ and - only)."
}
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

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
if (-not [System.IO.Path]::IsPathRooted($PythonExe) -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "PythonExe must be the full path to an existing python.exe, not a name resolved from PATH."
}

$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw "Refusing: a scheduled task with this name already exists in $TaskPath. It is never replaced by this installer."
}
$inRoot = Get-ScheduledTask -TaskName $TaskName -TaskPath "\" -ErrorAction SilentlyContinue
if ($null -ne $inRoot) {
    Write-Warning "A task with this name is also in the Task Scheduler root (\). Registering in $TaskPath would make two hourly tasks; move or remove the root task first (runbook step V7, owner OK)."
}

# Prerequisites in the interpreter the task will use (runbook "VM prerequisites"). The settings
# check also proves the time zone data is installed. Neither reads any data.
& $PythonExe -c "import pyodbc"
if ($LASTEXITCODE -ne 0) {
    throw "pyodbc is not installed for this python.exe."
}
$validation = & $PythonExe $scriptPath --config $ConfigPath --dry-run-config
if ($LASTEXITCODE -ne 0) {
    throw "The settings are not valid for this python.exe. Run --dry-run-config and fix them first."
}
$validated = ($validation | Out-String) | ConvertFrom-Json
$logonType = [string]$validated.scheduled_logon_type
$authMode = [string]$validated.auth_mode
$supported = (($authMode -ceq "windows_integrated") -and ($logonType -ceq "S4U")) -or
    (($authMode -ceq "sql_login") -and ($logonType -ceq "Password"))
if (-not $supported) {
    throw "Unsupported auth mode and logon type pair in the validated settings."
}
$RunMinute = $validated.run_minute
if (-not ($RunMinute -is [int] -or $RunMinute -is [long]) -or $RunMinute -lt 0 -or $RunMinute -gt 59) {
    throw "The validated settings did not give a schedule.run_minute from 0 to 59."
}
$RunMinute = [int]$RunMinute

# Daily trigger at 00:00 + RunMinute, repeating every hour for one day. Each day's trigger
# starts a new one-day repetition window, so the task repeats hourly indefinitely. This form
# works on every Windows PowerShell 5.1 host, unlike an unbounded RepetitionDuration.
# StopAtDurationEnd is set to false so the end of a day's window never stops a running run
# (New-ScheduledTaskTrigger sets it to true for this form).
$startAt = (Get-Date).Date.AddMinutes($RunMinute)
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
$repeat = New-ScheduledTaskTrigger -Once -At $startAt `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 1)
$repeat.Repetition.StopAtDurationEnd = $false
$trigger.Repetition = $repeat.Repetition

$actionArgs = "`"$scriptPath`" --config `"$ConfigPath`""
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $actionArgs -WorkingDirectory $RepoRoot
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$description = "Hourly read-only AC2 stock balance export (#245). Writes hourly CSV + manifest folders, a verified latest pair and last_run_status.json."

if ($DryRun) {
    Write-Host "Dry run: would register $TaskPath$TaskName"
    Write-Host "Repo root: $RepoRoot"
    Write-Host "Runs hourly at minute $RunMinute, IgnoreNew, 15 minute limit, RunLevel Limited, LogonType $logonType ($authMode)."
    Write-Host "Nothing was registered."
    exit 0
}

if ($logonType -ceq "S4U") {
    # windows_integrated: S4U stores no password. The task signs in to SQL Server as UserId.
    $principal = New-ScheduledTaskPrincipal `
        -UserId $UserId `
        -LogonType S4U `
        -RunLevel Limited
    Register-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $description | Out-Null
}
else {
    # sql_login: a Password logon loads the account's profile, so the job can read the SQL
    # password from that account's Credential Manager. The Windows password typed here goes
    # straight to Task Scheduler; it is never echoed, logged or written to a file. Do not run
    # this inside a PowerShell transcript.
    $credential = Get-Credential -UserName $UserId -Message "Windows password of the scheduled task account"
    if ($null -eq $credential) {
        throw "No credential was entered."
    }
    if ($credential.UserName -ne $UserId) {
        throw "The account in the prompt must be the UserId given."
    }
    try {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath $TaskPath `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -User $UserId `
            -Password $credential.GetNetworkCredential().Password `
            -RunLevel Limited `
            -Description $description | Out-Null
    }
    finally {
        $credential = $null
    }
}

Write-Host "Registered scheduled task: $TaskPath$TaskName"
Write-Host "Runs hourly at minute $RunMinute, IgnoreNew, 15 minute limit, RunLevel Limited, LogonType $logonType ($authMode)."
Write-Host "Next (runbook V7): Start-ScheduledTask -TaskPath $TaskPath now, wait for it to finish, and confirm last_run_status.json shows success for that run before leaving the task enabled. If it does not, run Disable-ScheduledTask -TaskPath $TaskPath and report."
