<#
Registers the #155 member sync (directlocal\xb_member_sync.py run --direction both) as a
Windows Task Scheduler task. Go-live design T1-T3, step S4 (shadow schedule).

NOT RUN BY THE BUILD. Without -Register this script only validates and prints the task XML
it would register (a dry run; the default). Registering needs -Register and the owner's OK
for go-live step S4 (OK-3). See docs/autocount2-automation/member_sync_phase1.md,
"Go-live (production)".

Behaviour:
- ONE trigger: a one-time start today at 00:00 that repeats every -IntervalMinutes with no end
  (no daily trigger, so no double start at midnight). -IntervalMinutes defaults to 15 and is
  refused below 10, the safe minimum (the docs give the reasoning: it equals the 10-minute
  execution limit, so a new start never meets a run that is still inside its limit).
- MultipleInstancesPolicy IgnoreNew: a start is skipped while a run is still going (the
  ledger lock refuses a second runner as well).
- ExecutionTimeLimit PT10M. RunLevel LeastPrivilege.
- LogonType Password only. The sync reads its AutoCount password from Windows Credential
  Manager, its Shopify token from a DPAPI cache and its Shopify app values from user-scope
  environment variables, all of the task account; an S4U task can read none of them, so
  -LogonType S4U (or anything but Password) is refused before anything else is checked.
- UserId has no default: the owner names the Windows account that owns the Credential
  Manager target, the token cache, the environment variables and the state root.
- PythonExe is the full path to python.exe, never a name resolved from PATH. The config is
  validated first with that interpreter (check-config: no login, no network, no ledger).
- Task Scheduler folder pinned to \XB\ (-TaskPath accepts only exactly \XB\, compared
  ordinally, so the runbook off switch \XB\XB-MemberSync always names this task); name
  -TaskName, default XB-MemberSync. Refuses if a task with that name already exists in that
  folder (never replaced).
- The printed XML is exactly the XML that -Register registers (Register-ScheduledTask -Xml).
  No password or secret is in it; the Windows password is typed into the Get-Credential
  prompt and goes straight to Task Scheduler. Do not run this inside a transcript.
- RepoRoot defaults to the clone this script is in (worked out in the body: Windows
  PowerShell 5.1 leaves $PSScriptRoot empty inside param() defaults).

Windows PowerShell 5.1 compatible. Dry run (the default):
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_member_sync_task.ps1 `
      -UserId "EXAMPLE-PC\example-user" -ConfigPath "C:\path\to\member_sync.live.json" `
      -PythonExe "C:\path\to\python.exe"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$UserId,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [int]$IntervalMinutes = 15,
    [string]$LogonType = "Password",
    [string]$TaskName = "XB-MemberSync",
    [string]$TaskPath = "\XB\",
    [string]$RepoRoot = "",
    [switch]$Register
)

$ErrorActionPreference = "Stop"

# Go-live T1: an S4U task cannot read Credential Manager or decrypt the DPAPI token cache.
if (-not [string]::Equals($LogonType, "Password", [StringComparison]::Ordinal)) {
    throw "LogonType must be Password: an S4U (or any other) logon cannot read the Credential Manager entry or the DPAPI token cache the sync needs."
}
# Safe minimum 10 minutes (the execution time limit); at most once a day.
$SafeMinimumIntervalMinutes = 10
if ($IntervalMinutes -lt $SafeMinimumIntervalMinutes -or $IntervalMinutes -gt 1440) {
    throw "IntervalMinutes must be from $SafeMinimumIntervalMinutes (the safe minimum) to 1440."
}
# G2 #281 LATER-4: the folder is pinned, so the runbook off switch \XB\XB-MemberSync always
# names the task this installer registered. Ordinal: another case, a trailing line break or a
# look-alike letter never passes.
$PinnedTaskPath = "\XB\"
if (-not [string]::Equals($TaskPath, $PinnedTaskPath, [StringComparison]::Ordinal)) {
    throw "TaskPath is pinned to \XB\ (the runbook off switch is \XB\XB-MemberSync)."
}
if ($TaskName -cnotmatch '\A[A-Za-z0-9][A-Za-z0-9 _-]{0,63}\z') {
    throw "TaskName may use letters, digits, spaces, _ and - only."
}
if ([string]::IsNullOrWhiteSpace($UserId)) {
    throw "UserId is required."
}
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$scriptPath = Join-Path $RepoRoot "directlocal\xb_member_sync.py"
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Sync script not found under the repository root."
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Config file not found."
}
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
if (-not [System.IO.Path]::IsPathRooted($PythonExe) -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "PythonExe must be the full path to an existing python.exe, not a name resolved from PATH."
}

# The config must pass the sync's own check with the interpreter the task will use.
$checked = & $PythonExe $scriptPath check-config --config $ConfigPath
if ($LASTEXITCODE -ne 0) {
    throw "The config is refused by check-config for this python.exe. Run check-config and fix it first."
}
$summary = ($checked | Out-String) | ConvertFrom-Json
if (-not [string]::Equals([string]$summary.status, "ok", [StringComparison]::Ordinal)) {
    throw "check-config did not return status ok."
}
$book = [string]$summary.autocount_book

$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw "Refusing: a scheduled task with this name already exists in $TaskPath. It is never replaced by this installer."
}

function ConvertTo-XbXmlText {
    param([AllowEmptyString()][string]$Value)
    return [System.Security.SecurityElement]::Escape($Value)
}

$interval = "PT$($IntervalMinutes)M"
$startAt = (Get-Date).Date.ToString("yyyy-MM-ddTHH:mm:ss", [System.Globalization.CultureInfo]::InvariantCulture)
$arguments = "`"$scriptPath`" run --config `"$ConfigPath`" --direction both"
$description = "#155 member sync, both directions, every $IntervalMinutes minutes (book $book). Create switches live in the config; see member_sync_phase1.md Go-live."
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
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
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

if (-not $Register) {
    Write-Host "Dry run (the default): would register $TaskPath$TaskName"
    Write-Host "Book $book; every $IntervalMinutes minutes, no end; IgnoreNew; PT10M limit; LeastPrivilege; LogonType Password."
    Write-Host "Task XML:"
    Write-Host $taskXml
    Write-Host "Nothing was registered. Registering needs -Register and the owner's OK (go-live step S4)."
    exit 0
}

# Password logon: the account's profile is loaded, so the run can read its Credential Manager
# entry, DPAPI cache and user environment variables. The Windows password typed here goes
# straight to Task Scheduler; it is never echoed, logged or written to a file.
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
Write-Host "Registered scheduled task: $TaskPath$TaskName (book $book, every $IntervalMinutes minutes)."
Write-Host "Next (go-live S4): Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName, wait for it to finish, check the run summary and digest, and keep it enabled only if the result is 0 or 3. Otherwise Disable-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName and report."
