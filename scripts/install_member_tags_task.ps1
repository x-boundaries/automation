<#
Registers the #268 slice C1 tagging tool (directlocal\xb_member_tags.py observe, or apply) as the
Windows Task Scheduler task \XB\XB-MemberTags. Same pattern as the member core installer
(scripts\install_member_benefits_task.ps1); see docs\autocount2-automation\member_tags_runbook.md.

NOT RUN BY THE BUILD. Without -Register this script only validates and prints the task XML it
would register (a dry run; the default). Registering needs -Register and the owner's OK for that
runbook step.

Behaviour:
- -Mode observe (the default) runs "observe": Shopify and AutoCount reads only, the tool's own
  ledger, would_tag / would_untag counts; no Shopify mutation is reachable. -Mode apply runs
  "apply" and is refused unless the config's tags_write_enabled is true (check-config).
- ONE trigger: a one-time start today at 00:11 that repeats every -IntervalMinutes (default 120,
  60 to 240) with no end, so a run falls just after Singapore midnight (the expiry day changes).
- MultipleInstancesPolicy IgnoreNew (the ledger lock refuses a second runner as well).
  ExecutionTimeLimit PT20M. RunLevel LeastPrivilege.
- LogonType Password only: the tool reads its AutoCount password from Windows Credential Manager,
  its Shopify token from a DPAPI cache and its Shopify app values from user-scope environment
  variables of the task account; an S4U task can read none of them.
- -Disabled registers the task disabled (Enabled false).
- Task Scheduler folder pinned to \XB\ (ordinal). The task name defaults to XB-MemberTags; the
  #155 sync's, the type move's and the member core's task names are refused. Refuses if a task
  with that name already exists (never replaced).
- PythonExe is the full path to python.exe. The config is validated first with that interpreter
  (check-config: no login, no network, no ledger).
- The printed XML is exactly the XML that -Register registers. No password or secret is in it.

Windows PowerShell 5.1 compatible. Dry run (the default):
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_member_tags_task.ps1 `
      -UserId "EXAMPLE-PC\example-user" -ConfigPath "C:\path\to\member_tags.live.json" `
      -PythonExe "C:\path\to\python.exe"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$UserId,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [string]$Mode = "observe",
    [int]$IntervalMinutes = 120,
    [string]$LogonType = "Password",
    [string]$TaskName = "XB-MemberTags",
    [string]$TaskPath = "\XB\",
    [string]$RepoRoot = "",
    [switch]$Disabled,
    [switch]$Register
)

$ErrorActionPreference = "Stop"

if (-not [string]::Equals($LogonType, "Password", [StringComparison]::Ordinal)) {
    throw "LogonType must be Password: an S4U (or any other) logon cannot read the Credential Manager entry or the DPAPI token cache the tool needs."
}
if (-not ([string]::Equals($Mode, "observe", [StringComparison]::Ordinal) -or [string]::Equals($Mode, "apply", [StringComparison]::Ordinal))) {
    throw "Mode must be observe or apply."
}
if ($IntervalMinutes -lt 60 -or $IntervalMinutes -gt 240) {
    throw "IntervalMinutes must be from 60 to 240."
}
$PinnedTaskPath = "\XB\"
if (-not [string]::Equals($TaskPath, $PinnedTaskPath, [StringComparison]::Ordinal)) {
    throw "TaskPath is pinned to \XB\ (the runbook off switch is \XB\XB-MemberTags)."
}
if ($TaskName -cnotmatch '\A[A-Za-z0-9][A-Za-z0-9 _-]{0,63}\z') {
    throw "TaskName may use letters, digits, spaces, _ and - only."
}
$trimmed = $TaskName.Trim()
if ($trimmed -like "XB-MemberSync*" -or $trimmed -like "XB-TypeMove*" -or $trimmed -like "XB-MemberBenefits*") {
    throw "TaskName may not be the #155 sync's, the type move's or the member core's task name."
}
if ([string]::IsNullOrWhiteSpace($UserId)) {
    throw "UserId is required."
}
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$scriptPath = Join-Path $RepoRoot "directlocal\xb_member_tags.py"
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Tags script not found under the repository root."
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Config file not found."
}
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
if (-not [System.IO.Path]::IsPathRooted($PythonExe) -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "PythonExe must be the full path to an existing python.exe, not a name resolved from PATH."
}

$checked = & $PythonExe $scriptPath check-config --config $ConfigPath
if ($LASTEXITCODE -ne 0) {
    throw "The config is refused by check-config for this python.exe. Run check-config and fix it first."
}
$summary = ($checked | Out-String) | ConvertFrom-Json
if (-not [string]::Equals([string]$summary.status, "ok", [StringComparison]::Ordinal)) {
    throw "check-config did not return status ok."
}
$book = [string]$summary.autocount_book
if ($Mode -ceq "apply" -and $summary.tags_write_enabled -ne $true) {
    throw "Mode apply needs tags_write_enabled true in the config (and the owner's OK for the tag step)."
}

$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw "Refusing: a scheduled task with this name already exists in $TaskPath. It is never replaced by this installer."
}

function ConvertTo-XbXmlText {
    param([AllowEmptyString()][string]$Value)
    return [System.Security.SecurityElement]::Escape($Value)
}

$hours = [math]::Floor($IntervalMinutes / 60)
$minutes = $IntervalMinutes % 60
$interval = "PT"
if ($hours -gt 0) { $interval += "$($hours)H" }
if ($minutes -gt 0) { $interval += "$($minutes)M" }
$startAt = (Get-Date).Date.AddMinutes(11).ToString("yyyy-MM-ddTHH:mm:ss", [System.Globalization.CultureInfo]::InvariantCulture)
$enabled = $(if ($Disabled) { "false" } else { "true" })
$arguments = "`"$scriptPath`" $Mode --config `"$ConfigPath`""
$description = "#268 member tags ($Mode, book $book), every $IntervalMinutes minutes from 00:11. Write switch tags_write_enabled lives in the config; see member_tags_runbook.md."
$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$(ConvertTo-XbXmlText $description)</Description>
    <URI>$(ConvertTo-XbXmlText ($TaskPath + $TaskName))</URI>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>$interval</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>$startAt</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
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
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>$enabled</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT20M</ExecutionTimeLimit>
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
[void]([xml]$taskXml)

if (-not $Register) {
    Write-Host "Dry run (the default): would register $TaskPath$TaskName"
    Write-Host "Mode $Mode. Book $book; every $IntervalMinutes minutes ($interval) from 00:11, no end; IgnoreNew; PT20M limit; LeastPrivilege; LogonType Password; enabled $enabled."
    Write-Host "Task XML:"
    Write-Host $taskXml
    Write-Host "Nothing was registered. Registering needs -Register and the owner's OK (runbook step T2, or T4 for -Mode apply)."
    exit 0
}

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
Write-Host "Registered scheduled task: $TaskPath$TaskName (book $book, mode $Mode, enabled $enabled)."
Write-Host "Off switch: Disable-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName. Check the run output and digest after the first run."
