<#
Re-points one existing scheduled task to a pinned clone (#117, 24 Nov 2026), or puts its earlier
action back from a backup. See docs/autocount2-automation/vm_pinned_clones_runbook.md.

NOT RUN BY THE BUILD. Without -Apply it only prints the change (a dry run; the default).
-Apply changes a live scheduled task: it needs the owner's OK for that exact task.

What -Apply changes: the task's one program action, and in it only the quoted script path at the
start of the arguments (same file, other clone) and the working folder. The program, every other
argument, the triggers, the account and logon type, the settings and the enabled state stay as
they were; the script reads them back and stops with a rollback line if any of them moved.

Steps with -Apply:
1. Refuses unless the task exists, has exactly one program action, and is not running now.
2. Re-point: refuses unless -NewRepoRoot passes Get-XbPinnedCloneProblem (scripts\xb_pinned_clone.ps1)
   and Get-XbRepointedAction can work out the new action. Restore (-RestoreFromXml): takes the
   program, arguments and working folder from a backup this script wrote for the same task.
3. Exports the task's current XML to a new file in -BackupDir (never overwrites a file).
4. Set-ScheduledTask -Action. A Password-logon task asks for that account's Windows password
   (Get-Credential; never echoed, logged or saved). An S4U task stores no password.
5. Reads the task back and compares.

Windows PowerShell 5.1 compatible, run as administrator:
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\xb_repoint_task_clone.ps1 `
      -TaskName "Example Task" -NewRepoRoot "C:\XB\golive\automation-example-0123abc" `
      -BackupDir "C:\path\to\task-backups"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$TaskName,
    [string]$TaskPath = "\XB\",
    [string]$NewRepoRoot = "",
    [string]$RestoreFromXml = "",
    [string]$BackupDir = "",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "xb_pinned_clone.ps1")

$restore = -not [string]::IsNullOrWhiteSpace($RestoreFromXml)
if ($restore -eq (-not [string]::IsNullOrWhiteSpace($NewRepoRoot))) {
    throw "Give exactly one of -NewRepoRoot (re-point) or -RestoreFromXml (roll back)."
}

$task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    throw "No scheduled task $TaskPath$TaskName."
}
$actions = @($task.Actions)
if ($actions.Count -ne 1 -or -not ($actions[0].PSObject.Properties.Name -contains "Execute")) {
    throw "The task must have exactly one program action."
}
$current = $actions[0]
$taskNs = "http://schemas.microsoft.com/windows/2004/02/mit/task"

function Get-XbTaskXmlPart {
    param([xml]$Xml, [string]$Name)
    $ns = New-Object System.Xml.XmlNamespaceManager($Xml.NameTable)
    $ns.AddNamespace("t", $taskNs)
    $node = $Xml.SelectSingleNode("/t:Task/t:$Name", $ns)
    if ($null -eq $node) { return "" }
    return $node.OuterXml
}

if ($restore) {
    [xml]$backup = Get-Content -LiteralPath $RestoreFromXml -Raw
    $ns = New-Object System.Xml.XmlNamespaceManager($backup.NameTable)
    $ns.AddNamespace("t", $taskNs)
    $uri = [string]$backup.SelectSingleNode("/t:Task/t:RegistrationInfo/t:URI", $ns).InnerText
    if (-not [string]::Equals($uri, $TaskPath + $TaskName, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The backup is for $uri, not $TaskPath$TaskName."
    }
    $execs = @($backup.SelectNodes("/t:Task/t:Actions/t:Exec", $ns))
    if ($execs.Count -ne 1) {
        throw "The backup must have exactly one program action."
    }
    $readField = { param($name) $n = $execs[0].SelectSingleNode("t:$name", $ns); if ($null -eq $n) { "" } else { [string]$n.InnerText } }
    $planned = [pscustomobject]@{
        Execute          = & $readField "Command"
        Arguments        = & $readField "Arguments"
        WorkingDirectory = & $readField "WorkingDirectory"
        Notes            = @()
    }
}
else {
    $pinProblem = Get-XbPinnedCloneProblem -RepoRoot $NewRepoRoot
    if ($pinProblem) {
        throw "Refusing: $pinProblem"
    }
    $NewRepoRoot = (Resolve-Path -LiteralPath $NewRepoRoot).Path.TrimEnd('\')
    $planned = Get-XbRepointedAction -Execute ([string]$current.Execute) -Arguments ([string]$current.Arguments) `
        -WorkingDirectory ([string]$current.WorkingDirectory) -NewRepoRoot $NewRepoRoot
}
if (-not [string]::Equals([string]$current.Execute, [string]$planned.Execute, [StringComparison]::OrdinalIgnoreCase)) {
    throw "The program would change; this script never changes the program."
}

Write-Host "Task: $TaskPath$TaskName (state $($task.State), logon $($task.Principal.LogonType))"
Write-Host "Program:        $($current.Execute)"
Write-Host "Now arguments:  $($current.Arguments)"
Write-Host "Now folder:     $($current.WorkingDirectory)"
Write-Host "New arguments:  $($planned.Arguments)"
Write-Host "New folder:     $($planned.WorkingDirectory)"
foreach ($note in $planned.Notes) { Write-Host "Note: $note" }

if (-not $Apply) {
    Write-Host "Dry run: nothing changed."
    exit 0
}

if ([string]::IsNullOrWhiteSpace($BackupDir) -or -not (Test-Path -LiteralPath $BackupDir -PathType Container)) {
    throw "-BackupDir must be an existing folder."
}
if ([string]$task.State -eq "Running") {
    throw "The task is running now; nothing changed. Try again after it ends."
}
$safeName = ($TaskName -replace '[^A-Za-z0-9_-]', '_')
$backupPath = Join-Path $BackupDir ("{0}-{1}.xml" -f $safeName, (Get-Date).ToString("yyyyMMdd-HHmmss", [System.Globalization.CultureInfo]::InvariantCulture))
if (Test-Path -LiteralPath $backupPath) {
    throw "The backup file already exists; nothing changed."
}
$beforeText = Export-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
[System.IO.File]::WriteAllText($backupPath, $beforeText, (New-Object System.Text.UnicodeEncoding($false, $true)))
Write-Host "Backup: $backupPath"

$newAction = New-ScheduledTaskAction -Execute $planned.Execute -Argument $planned.Arguments -WorkingDirectory $planned.WorkingDirectory
if ([string]$task.Principal.LogonType -eq "Password") {
    # The Windows password typed here goes straight to Task Scheduler; it is never echoed,
    # logged or written to a file. Do not run this inside a PowerShell transcript.
    $credential = Get-Credential -UserName $task.Principal.UserId -Message "Windows password of the scheduled task account"
    if ($null -eq $credential) {
        throw "No credential was entered; nothing changed."
    }
    try {
        Set-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Action $newAction `
            -User $task.Principal.UserId -Password $credential.GetNetworkCredential().Password | Out-Null
    }
    finally {
        $credential = $null
    }
}
else {
    Set-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Action $newAction | Out-Null
}

$after = @((Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName).Actions)
[xml]$beforeXml = $beforeText
[xml]$afterXml = Export-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
$problems = @()
if ($after.Count -ne 1 -or
    -not [string]::Equals([string]$after[0].Execute, [string]$planned.Execute, [StringComparison]::Ordinal) -or
    -not [string]::Equals([string]$after[0].Arguments, [string]$planned.Arguments, [StringComparison]::Ordinal) -or
    -not [string]::Equals([string]$after[0].WorkingDirectory, [string]$planned.WorkingDirectory, [StringComparison]::Ordinal)) {
    $problems += "action"
}
foreach ($part in @("Triggers", "Principals", "Settings")) {
    if (-not [string]::Equals((Get-XbTaskXmlPart $beforeXml $part), (Get-XbTaskXmlPart $afterXml $part), [StringComparison]::Ordinal)) {
        $problems += $part
    }
}
$rollback = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -TaskPath `"$TaskPath`" -TaskName `"$TaskName`" -RestoreFromXml `"$backupPath`" -BackupDir `"$BackupDir`" -Apply"
if ($problems.Count -gt 0) {
    Write-Host "Readback differs in: $($problems -join ', '). Roll back with:"
    Write-Host $rollback
    exit 1
}
Write-Host "Done: $TaskPath$TaskName now runs from $($planned.WorkingDirectory). Readback matches (action as planned; triggers, account and settings unchanged)."
Write-Host "Roll back with: $rollback"
