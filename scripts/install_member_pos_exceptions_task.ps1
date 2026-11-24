<#
Registers the monthly in-store member discount check (#268) as the Windows Task Scheduler task
\XB\XB Member Discount Check. A copy of the AR/AP aging installer pattern
(scripts/install_autocount_arap_aging_task.ps1) for scripts/xb_member_pos_exceptions.py.

NOT RUN BY THE BUILD. Registering this task needs a separate owner OK on the VM. See
docs/autocount2-automation/member_pos_exceptions_runbook.md, section "Install".

Behaviour:
- ONE daily trigger at schedule.run_time (default 09:15). The script runs "run --scheduled": it
  does nothing until schedule.run_day (default the 2nd) of the month, then reports the previous
  month once. A failed run, a stale POS sync or a failed email is retried the next day.
  Windows PowerShell's ScheduledTasks module has no monthly trigger, so the month logic lives in
  the script, where the tests cover it.
- MultipleInstances IgnoreNew, ExecutionTimeLimit 30 minutes, RunLevel Limited.
- PythonExe is the full path to python.exe, never a name resolved from PATH.
- The settings are validated first with that same interpreter (check-settings, reads no data).
  The logon type comes from the validated settings, so only the two supported pairs can be
  registered:
    identity.auth_mode windows_integrated -> LogonType S4U (no stored password)
    identity.auth_mode sql_login          -> LogonType Password (Get-Credential prompt)
- UserId has no default: the owner names the Windows account explicitly.
- Task Scheduler folder: -TaskPath, default \XB\. It must start and end with a backslash and
  name at least one folder; letters, digits, spaces, _ and - only.
- Refuses if a task with the same name already exists in that folder or in the Task Scheduler
  root (\). It never replaces or moves an existing task.
- RepoRoot defaults to the clone this script is in (worked out in the body: Windows PowerShell
  5.1 leaves $PSScriptRoot empty inside param() defaults of a script with [Parameter()]).
- -DryRun runs every check (including pyodbc and check-settings), prints what it would register
  and stops before the password prompt and Register-ScheduledTask. Nothing is registered. The
  tests use it, and only it.

Windows PowerShell 5.1 compatible. Run it with -ExecutionPolicy Bypass (this one command only):
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_member_pos_exceptions_task.ps1 `
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
    [string]$TaskName = "XB Member Discount Check",
    [string]$TaskPath = "\XB\",
    [string]$RepoRoot = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ($TaskPath -cnotmatch '\A(\\[A-Za-z0-9_-]([A-Za-z0-9 _-]*[A-Za-z0-9_-])?)+\\\z') {
    throw "TaskPath must look like \XB\ (start and end with a backslash; letters, digits, spaces, _ and - only)."
}
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$scriptPath = Join-Path $RepoRoot "scripts\xb_member_pos_exceptions.py"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Report script not found under the repository root."
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
    throw "Refusing: a scheduled task with this name already exists in the Task Scheduler root (\). Remove or rename it first (owner OK)."
}

& $PythonExe -c "import pyodbc"
if ($LASTEXITCODE -ne 0) {
    throw "pyodbc is not installed for this python.exe."
}
$validation = & $PythonExe $scriptPath check-settings --config $ConfigPath
if ($LASTEXITCODE -ne 0) {
    throw "The settings are not valid for this python.exe. Run check-settings and fix them first."
}
$validated = ($validation | Out-String) | ConvertFrom-Json
$logonType = [string]$validated.scheduled_logon_type
$authMode = [string]$validated.auth_mode
$supported = (($authMode -ceq "windows_integrated") -and ($logonType -ceq "S4U")) -or
    (($authMode -ceq "sql_login") -and ($logonType -ceq "Password"))
if (-not $supported) {
    throw "Unsupported auth mode and logon type pair in the validated settings."
}
$runTime = [string]$validated.run_time
if ($runTime -cnotmatch '\A([01][0-9]|2[0-3]):[0-5][0-9]\z') {
    throw "The validated settings did not give a schedule.run_time as HH:MM."
}
$runDay = $validated.run_day
if (-not ($runDay -is [int] -or $runDay -is [long]) -or $runDay -lt 1 -or $runDay -gt 28) {
    throw "The validated settings did not give a schedule.run_day from 1 to 28."
}

$startAt = (Get-Date).Date.Add([TimeSpan]::Parse($runTime))
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
$actionArgs = "`"$scriptPath`" run --scheduled --config `"$ConfigPath`""
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $actionArgs -WorkingDirectory $RepoRoot
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$description = "Monthly read-only in-store member discount check (#268). Runs daily; reports the previous month once from day $runDay. Emails only when there is something to act on."

if ($DryRun) {
    Write-Host "Dry run: would register $TaskPath$TaskName"
    Write-Host "Repo root: $RepoRoot"
    Write-Host "Runs daily at $runTime (reports the previous month once from day $runDay), IgnoreNew, 30 minute limit, RunLevel Limited, LogonType $logonType ($authMode)."
    Write-Host "Nothing was registered."
    exit 0
}

if ($logonType -ceq "S4U") {
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
    # The Windows password typed here goes straight to Task Scheduler; it is never echoed,
    # logged or written to a file. Do not run this inside a PowerShell transcript.
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
Write-Host "Runs daily at $runTime (reports the previous month once from day $runDay), IgnoreNew, 30 minute limit, RunLevel Limited, LogonType $logonType ($authMode)."
Write-Host "Next (runbook Install step 5): run it once by hand for the last month with --no-send and read the record before leaving the task enabled."
