<#
Registers the #155 member sync daily summary (#288 slice 3) as a Windows Task Scheduler task:
\XB\XB-MemberSync-DailySummary, daily at 06:40 (VM local time, SGT), running
"directlocal\xb_member_sync_daily_summary.py daily --settings <settings>".

NOT RUN BY THE BUILD. Without -Register this script only validates and prints the task XML it
would register (a dry run; the default). -DryRun forces a dry run even with -Register (the
offline tests use it). Registering needs -Register and the owner's OK for that live step. See
docs/autocount2-automation/member_sync_phase1.md, "Daily summary email (#288 slice 3)".

Behaviour:
- ONE daily trigger at -StartTime (default 06:40): after the 06:00 deferred-create run and the
  06:15 and 06:30 runs (each capped at 10 minutes), before the shop opens.
- MultipleInstancesPolicy IgnoreNew, ExecutionTimeLimit PT5M, RunLevel LeastPrivilege,
  StartWhenAvailable false (a missed morning is not caught up later in the day).
- LogonType Password only, as the sync task: the summary runs digest, which must run as the
  account that owns the sync's state root, and it reads its webhook secret from that account's
  Credential Manager. -LogonType S4U (or anything but Password) is refused first.
- UserId has no default. PythonExe is the full path to python.exe, never a name from PATH.
  The settings are checked first with that interpreter (check-settings: no network, no child
  process, no SQL).
- Folder pinned to \XB\ and name pinned to XB-MemberSync-DailySummary (ordinal compare), so
  this installer can never name, replace or change the 15-minute sync task in the same folder.
- Refuses if a task with that name already exists (never replaced).
- The printed XML is exactly the XML that -Register registers. No password or secret is in it;
  the Windows password is typed into the Get-Credential prompt and goes straight to Task
  Scheduler. Do not run this inside a transcript.
- RepoRoot defaults to the clone this script is in (worked out in the body: Windows
  PowerShell 5.1 leaves $PSScriptRoot empty inside param() defaults).

Windows PowerShell 5.1 compatible. Dry run (the default):
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_member_sync_summary_task.ps1 `
      -UserId "EXAMPLE-PC\example-user" -SettingsPath "C:\path\to\member_sync_daily_summary.json" `
      -PythonExe "C:\path\to\python.exe"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$UserId,
    [Parameter(Mandatory = $true)]
    [string]$SettingsPath,
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [string]$StartTime = "06:40",
    [string]$LogonType = "Password",
    [string]$TaskName = "XB-MemberSync-DailySummary",
    [string]$TaskPath = "\XB\",
    [string]$RepoRoot = "",
    [switch]$Register,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not [string]::Equals($LogonType, "Password", [StringComparison]::Ordinal)) {
    throw "LogonType must be Password: an S4U (or any other) logon cannot read the Credential Manager entry, and digest must run as the account that owns the sync state root."
}
$PinnedTaskPath = "\XB\"
if (-not [string]::Equals($TaskPath, $PinnedTaskPath, [StringComparison]::Ordinal)) {
    throw "TaskPath is pinned to \XB\."
}
$PinnedTaskName = "XB-MemberSync-DailySummary"
if (-not [string]::Equals($TaskName, $PinnedTaskName, [StringComparison]::Ordinal)) {
    throw "TaskName is pinned to XB-MemberSync-DailySummary: this installer never names the sync task."
}
if ($StartTime -cnotmatch '\A(?:[01][0-9]|2[0-3]):[0-5][0-9]\z') {
    throw "StartTime must be HH:mm (24-hour)."
}
if ([string]::IsNullOrWhiteSpace($UserId)) {
    throw "UserId is required."
}
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$scriptPath = Join-Path $RepoRoot "directlocal\xb_member_sync_daily_summary.py"
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Summary script not found under the repository root."
}
if (-not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) {
    throw "Settings file not found."
}
$SettingsPath = (Resolve-Path -LiteralPath $SettingsPath).Path
if (-not [System.IO.Path]::IsPathRooted($PythonExe) -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "PythonExe must be the full path to an existing python.exe, not a name resolved from PATH."
}

# The settings must pass the summary's own check with the interpreter the task will use.
$checked = & $PythonExe $scriptPath check-settings --settings $SettingsPath
if ($LASTEXITCODE -ne 0) {
    throw "The settings are refused by check-settings for this python.exe. Run check-settings and fix them first."
}
$summary = ($checked | Out-String) | ConvertFrom-Json
if (-not [string]::Equals([string]$summary.status, "ok", [StringComparison]::Ordinal)) {
    throw "check-settings did not return status ok."
}

$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw "Refusing: a scheduled task with this name already exists in $TaskPath. It is never replaced by this installer."
}

function ConvertTo-XbXmlText {
    param([AllowEmptyString()][string]$Value)
    return [System.Security.SecurityElement]::Escape($Value)
}

$startAt = (Get-Date).Date.ToString("yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture) + "T" + $StartTime + ":00"
$arguments = "`"$scriptPath`" daily --settings `"$SettingsPath`""
$description = "#288 slice 3: #155 member sync daily summary email, daily at $StartTime. Read only; see member_sync_phase1.md Daily summary email."
$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$(ConvertTo-XbXmlText $description)</Description>
    <URI>$(ConvertTo-XbXmlText ($TaskPath + $TaskName))</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$startAt</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(ConvertTo-XbXmlText $UserId)</UserId>
      <LogonType>Password</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$(ConvertTo-XbXmlText $PythonExe)</Command>
      <Arguments>$(ConvertTo-XbXmlText $arguments)</Arguments>
      <WorkingDirectory>$(ConvertTo-XbXmlText $RepoRoot)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
# The XML must parse before anything is shown or registered.
[void]([xml]$taskXml)

if ($DryRun -or -not $Register) {
    Write-Host "Dry run (the default): would register $TaskPath$TaskName"
    Write-Host "Daily at $StartTime; IgnoreNew; PT5M limit; LeastPrivilege; LogonType Password; send enabled: $($summary.send_enabled)."
    Write-Host "Repo root: $RepoRoot"
    Write-Host "Task XML:"
    Write-Host $taskXml
    Write-Host "Nothing was registered. Registering needs -Register and the owner's OK (#288 slice 3, VM task registration)."
    exit 0
}

# Password logon: the account's profile is loaded, so the run can read its Credential Manager
# entry and the sync state root. The Windows password typed here goes straight to Task
# Scheduler; it is never echoed, logged or written to a file.
$credential = Get-Credential -UserName $UserId -Message "Windows password of the scheduled task account"
if ($null -eq $credential) {
    throw "No credential was entered."
}
if ($credential.UserName -ne $UserId) {
    throw "The account in the prompt must be the UserId given."
}
try {
    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Xml $taskXml -User $UserId -Password $credential.GetNetworkCredential().Password | Out-Null
}
finally {
    $credential = $null
}
Write-Host "Registered scheduled task: $TaskPath$TaskName (daily at $StartTime)."
Write-Host "Next: Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName once, then check daily_summary_<date>.json and the email. Off switch: Disable-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName."
