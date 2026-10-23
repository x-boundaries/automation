<#
Registers the hourly AC2 stock balance export (#245) as a Windows Task Scheduler task.

NOT RUN BY THE BUILD. Registering this task needs a separate owner OK, given only after the
manual VM proof runs (runbook steps V1-V6) pass. See
docs/autocount2-automation/stock_balance_hourly_runbook.md, step V7.

Behaviour:
- Runs every hour, 24/7, at RunMinute past the hour (00:00 + RunMinute, then hourly).
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
- Refuses if a task with the same name already exists. It never replaces an existing task;
  remove or rename the old task by hand first if a change is really intended.
- After registering, do the supervised "Run now" in runbook step V7 before leaving it enabled.

Windows PowerShell 5.1 compatible.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$UserId,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [string]$TaskName = "XB AC2 Hourly Stock Balance",
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [ValidateRange(0, 59)]
    [int]$RunMinute = 0
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
if (-not [System.IO.Path]::IsPathRooted($PythonExe) -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "PythonExe must be the full path to an existing python.exe, not a name resolved from PATH."
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw "Refusing: a scheduled task with this name already exists. It is never replaced by this installer."
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
$description = "Hourly read-only AC2 stock balance export (#245). Writes hourly CSV + manifest folders, a verified latest pair and last_run_status.json."

if ($logonType -ceq "S4U") {
    # windows_integrated: S4U stores no password. The task signs in to SQL Server as UserId.
    $principal = New-ScheduledTaskPrincipal `
        -UserId $UserId `
        -LogonType S4U `
        -RunLevel Limited
    Register-ScheduledTask `
        -TaskName $TaskName `
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

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Runs hourly at minute $RunMinute, IgnoreNew, 15 minute limit, RunLevel Limited, LogonType $logonType ($authMode)."
Write-Host "Next (runbook V7): Start-ScheduledTask now, wait for it to finish, and confirm last_run_status.json shows success for that run before leaving the task enabled. If it does not, run Disable-ScheduledTask and report."
