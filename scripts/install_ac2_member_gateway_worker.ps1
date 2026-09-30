[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet("Install", "Upgrade", "Verify", "Uninstall", "ValidateOnly")]
    [string]$Operation = "ValidateOnly",
    [string]$WorkerAccount,
    [Management.Automation.PSCredential]$TaskCredential,
    [string]$ReviewedPackageManifestPath,
    [switch]$LibraryOnly,
    [switch]$RequireSecrets,
    [string]$InstallRoot = "C:\Program Files\X-Boundaries\MemberGatewayWorker",
    [string]$RuntimeRoot = "C:\ProgramData\X-Boundaries\MemberGatewayWorker"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskPath = "\X-Boundaries\"
$taskName = "AC2 Member Gateway Worker"
$packageFiles = @(
    "ac2_member_gateway_worker.ps1",
    "ac2_member_gateway_worker_lib.ps1",
    "ac2_member_gateway_autocount_adapter.ps1",
    "launch_ac2_member_gateway_worker.ps1",
    "test_ac2_member_gateway_autocount_dependencies.ps1",
    "ac2_member_create_primitive.ps1"
)
# The scheduled task interpreter is an absolute path (CI5), never PATH-resolved.
$script:XbWindowsPowerShellPath = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
$script:XbInstallationManifestSchema = "xb.member.gateway.worker.installation.v2"

# [CI1] Install layout: the fixed install and runtime roots.
function Assert-XbWorkerInstallLayout {
    if ([IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') -cne "C:\Program Files\X-Boundaries\MemberGatewayWorker") { throw "install_root_contract_mismatch" }
    if ([IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\') -cne "C:\ProgramData\X-Boundaries\MemberGatewayWorker") { throw "runtime_root_contract_mismatch" }
}

function Invoke-XbIcacls {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string[]]$Arguments)
    & icacls.exe $Path @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "acl_application_failed" }
}

function Set-XbWorkerLogsAcl {
    param([Parameter(Mandatory)][string]$Path)
    $systemSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
    $administratorsSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-32-544")
    $ownerRightsSid = New-Object Security.Principal.SecurityIdentifier("S-1-3-4")
    $workerSid = [Security.Principal.NTAccount]::new($WorkerAccount).Translate([Security.Principal.SecurityIdentifier])
    $objectInherit = [Security.AccessControl.InheritanceFlags]::ObjectInherit
    $containerAndObjectInherit = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor $objectInherit
    $inheritOnly = [Security.AccessControl.PropagationFlags]::InheritOnly
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
    $modify = [Security.AccessControl.FileSystemRights]::Modify -bor [Security.AccessControl.FileSystemRights]::Synchronize
    if ([int]$modify -ne 0x001301BF) { throw "acl_application_failed" }

    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administratorsSid)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($systemSid, $fullControl, $containerAndObjectInherit, [Security.AccessControl.PropagationFlags]::None, $allow))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($administratorsSid, $fullControl, $containerAndObjectInherit, [Security.AccessControl.PropagationFlags]::None, $allow))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($workerSid, [Security.AccessControl.FileSystemRights]0x001200AB, [Security.AccessControl.InheritanceFlags]::None, [Security.AccessControl.PropagationFlags]::None, $allow))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($workerSid, $modify, $objectInherit, $inheritOnly, $allow))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($ownerRightsSid, $modify, $objectInherit, $inheritOnly, $allow))
    Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
}

function Set-XbWorkerAcl {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][ValidateSet("Install", "Config", "Secrets", "Logs", "Rollback")][string]$Kind)
    if ($Kind -eq "Logs") { Set-XbWorkerLogsAcl -Path $Path; return }
    Invoke-XbIcacls -Path $Path -Arguments @("/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")
    if ($Kind -eq "Install") { Invoke-XbIcacls -Path $Path -Arguments @("/grant:r", "${WorkerAccount}:(OI)(CI)RX") }
    if ($Kind -eq "Config") { Invoke-XbIcacls -Path $Path -Arguments @("/grant:r", "${WorkerAccount}:(OI)(CI)R") }
    if ($Kind -eq "Secrets") { Invoke-XbIcacls -Path $Path -Arguments @("/grant:r", "${WorkerAccount}:(OI)(CI)R") }
}

function Get-XbFileSha256 {
    param([Parameter(Mandatory)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant() }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}

function Get-XbWorkerTaskArguments {
    param([Parameter(Mandatory)][string]$LauncherPath)
    return '-NoLogo -NoProfile -NonInteractive -File "{0}" -Mode DisabledProof' -f ([IO.Path]::GetFullPath($LauncherPath))
}

function Get-XbWorkerTaskIdentity {
    param([Parameter(Mandatory)][string]$LauncherPath, [Parameter(Mandatory)][string]$WorkerAccount)
    return [ordered]@{
        executable = $script:XbWindowsPowerShellPath
        launcher_path = [IO.Path]::GetFullPath($LauncherPath)
        arguments = Get-XbWorkerTaskArguments -LauncherPath $LauncherPath
        working_directory = ""
        principal_user_id = $WorkerAccount
        principal_logon_type = "Password"
        principal_run_level = "Limited"
    }
}

function New-XbWorkerInstallationManifest {
    param([Parameter(Mandatory)][string]$PackageRoot, [Parameter(Mandatory)]$ReviewedIdentity, [Parameter(Mandatory)][string]$WorkerAccount)
    $files = foreach ($name in $packageFiles) {
        $entry = @($ReviewedIdentity.package_files | Where-Object { [string]$_.name -ceq $name })
        if ($entry.Count -ne 1) { throw "reviewed_package_membership_invalid" }
        [ordered]@{ name = $name; sha256 = [string]$entry[0].sha256 }
    }
    $taskIdentity = Get-XbWorkerTaskIdentity -LauncherPath (Join-Path $InstallRoot "launch_ac2_member_gateway_worker.ps1") -WorkerAccount $WorkerAccount
    $releaseEntries = [ordered]@{}
    foreach ($file in @($files)) { $releaseEntries[[string]$file.name] = [string]$file.sha256 }
    [ordered]@{
        schema_version = $script:XbInstallationManifestSchema
        reviewed_source = $ReviewedIdentity.source
        release_sha256 = Get-XbReleaseIdentityFromEntries -Entries $releaseEntries
        install_root = "C:\Program Files\X-Boundaries\MemberGatewayWorker\"
        runtime_root = "C:\ProgramData\X-Boundaries\MemberGatewayWorker\"
        package_files = @($files)
        task = [ordered]@{
            path = $taskPath
            name = $taskName
            enabled = $false
            trigger_count = 0
            action_mode = "DisabledProof"
            production_switches = @()
            multiple_instances = "IgnoreNew"
            execution_time_limit = "PT10M"
            restart_count = 0
            start_when_available = $false
            executable = [string]$taskIdentity.executable
            launcher_path = [string]$taskIdentity.launcher_path
            arguments = [string]$taskIdentity.arguments
            working_directory = [string]$taskIdentity.working_directory
            principal_user_id = [string]$taskIdentity.principal_user_id
            principal_logon_type = [string]$taskIdentity.principal_logon_type
            principal_run_level = [string]$taskIdentity.principal_run_level
        }
        rollback_owned_roots = @("config", "secrets", "logs", "rollback")
    }
}

function Get-XbExactPropertyNames {
    param([Parameter(Mandatory)]$Value)
    return @($Value.PSObject.Properties.Name | Sort-Object)
}

function Assert-XbExactProperties {
    param([Parameter(Mandatory)]$Value, [Parameter(Mandatory)][string[]]$Expected, [Parameter(Mandatory)][string]$ErrorId)
    if (@(Compare-Object (Get-XbExactPropertyNames $Value) @($Expected | Sort-Object)).Count -ne 0) { throw $ErrorId }
}

# [CI2] Reviewed-package identity: commit, tree, git blob and sha256 membership.
function Read-XbReviewedPackageIdentity {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$PackageRoot)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "reviewed_package_identity_missing" }
    $identity = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    Assert-XbExactProperties $identity @("schema_version", "source", "package_files") "reviewed_package_identity_shape_invalid"
    Assert-XbExactProperties $identity.source @("commit", "tree") "reviewed_package_source_shape_invalid"
    if ([string]$identity.schema_version -cne "xb.member.gateway.worker.reviewed-package.v1") { throw "reviewed_package_identity_schema_invalid" }
    foreach ($value in @([string]$identity.source.commit, [string]$identity.source.tree)) {
        if ($value -cnotmatch '^[0-9a-f]{40}$') { throw "reviewed_package_source_invalid" }
    }
    $entries = @($identity.package_files)
    if ($entries.Count -ne $packageFiles.Count) { throw "reviewed_package_membership_invalid" }
    $seen = @{}
    foreach ($entry in $entries) {
        Assert-XbExactProperties $entry @("name", "sha256", "git_blob") "reviewed_package_entry_shape_invalid"
        $name = [string]$entry.name
        if ($name -cnotin $packageFiles -or $seen.ContainsKey($name) -or [string]$entry.sha256 -cnotmatch '^[0-9a-f]{64}$' -or [string]$entry.git_blob -cnotmatch '^[0-9a-f]{40}$') { throw "reviewed_package_membership_invalid" }
        $seen[$name] = [string]$entry.sha256
    }
    foreach ($name in $packageFiles) {
        $sourcePath = Join-Path $PackageRoot $name
        $entry = @($entries | Where-Object { [string]$_.name -ceq $name })[0]
        $currentBlob = (& git -C $PackageRoot hash-object $sourcePath).Trim()
        $reviewedBlob = (& git -C $PackageRoot rev-parse ("{0}:scripts/{1}" -f [string]$identity.source.commit, $name)).Trim()
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf) -or (Get-XbFileSha256 $sourcePath) -cne $seen[$name] -or $currentBlob -cne [string]$entry.git_blob -or $reviewedBlob -cne [string]$entry.git_blob) { throw "reviewed_package_source_bytes_mismatch" }
    }
    $observedCommit = (& git -C $PackageRoot rev-parse HEAD).Trim()
    $observedTree = (& git -C $PackageRoot rev-parse 'HEAD^{tree}').Trim()
    if ($LASTEXITCODE -ne 0 -or $observedCommit -cne [string]$identity.source.commit -or $observedTree -cne [string]$identity.source.tree) { throw "reviewed_package_source_identity_mismatch" }
    return $identity
}

# [CI3] Staged package bytes equal the reviewed identity.
function Assert-XbStagedPackageIdentity {
    param([Parameter(Mandatory)][string]$StageRoot, [Parameter(Mandatory)]$ReviewedIdentity)
    $actualNames = @(Get-ChildItem -LiteralPath $StageRoot -File | ForEach-Object Name | Sort-Object)
    if (@(Compare-Object $actualNames @($packageFiles | Sort-Object)).Count -ne 0) { throw "staged_package_membership_mismatch" }
    foreach ($entry in @($ReviewedIdentity.package_files)) {
        if ((Get-XbFileSha256 (Join-Path $StageRoot ([string]$entry.name))) -cne [string]$entry.sha256) { throw "staged_package_bytes_mismatch" }
    }
}

# A triggerless CIM task exposes Triggers as $null and @($null).Count is 1, so collection counts must drop nulls.
function Get-XbNonNullCount {
    param($Value)
    return @(@($Value) | Where-Object { $null -ne $_ }).Count
}

# COM interop maps HRESULTs to typed exceptions (0x80070002 surfaces as FileNotFoundException), so read the innermost one.
function Get-XbComHResult {
    param([Parameter(Mandatory)]$ErrorRecord)
    $exception = if ($ErrorRecord -is [Management.Automation.ErrorRecord]) { $ErrorRecord.Exception } else { $ErrorRecord }
    if ($null -eq $exception) { return $null }
    while ($null -ne $exception.InnerException) { $exception = $exception.InnerException }
    return [int]$exception.HResult
}

function Connect-XbTaskService {
    try {
        $service = New-Object -ComObject Schedule.Service
        $service.Connect()
    } catch { throw "task_service_unavailable" }
    return $service
}

function ConvertTo-XbComFolderPath {
    param([Parameter(Mandatory)][string]$Path)
    $trimmed = $Path.TrimEnd('\')
    if ($trimmed -eq "") { return "\" }
    return $trimmed
}

function Test-XbTaskFolderPresent {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$ErrorId)
    $service = Connect-XbTaskService
    try { $null = $service.GetFolder((ConvertTo-XbComFolderPath $Path)) }
    catch {
        # 0x80070002 is the only Schedule.Service answer accepted as proven folder absence.
        if ((Get-XbComHResult $_) -eq -2147024894) { return $false }
        throw $ErrorId
    }
    return $true
}

function Get-XbRegisteredWorkerTaskView {
    $service = Connect-XbTaskService
    try { $registered = $service.GetFolder((ConvertTo-XbComFolderPath $taskPath)).GetTask($taskName) }
    catch { throw "task_presence_unproven" }
    if ([string]$registered.Path -cne ($taskPath + $taskName)) { throw "task_identity_invalid" }
    return $registered
}

# Authoritative trigger oracle: Schedule.Service definition count plus namespaced XML /Task/Triggers/* must agree.
# The null-filtered CIM count is a consistency check only.
function Get-XbTaskTriggerOracleCount {
    param([Parameter(Mandatory)][int]$ComTriggerCount, [Parameter(Mandatory)][AllowEmptyString()][string]$TaskXml, $CimTriggers)
    $document = New-Object Xml.XmlDocument
    try { $document.LoadXml($TaskXml) } catch { throw "task_trigger_oracle_disagreement" }
    $namespaces = New-Object Xml.XmlNamespaceManager($document.NameTable)
    $namespaces.AddNamespace("t", "http://schemas.microsoft.com/windows/2004/02/mit/task")
    if ($document.SelectNodes("/t:Task", $namespaces).Count -ne 1) { throw "task_trigger_oracle_disagreement" }
    $xmlTriggerCount = $document.SelectNodes("/t:Task/t:Triggers/*", $namespaces).Count
    if ($ComTriggerCount -ne $xmlTriggerCount) { throw "task_trigger_oracle_disagreement" }
    if ((Get-XbNonNullCount $CimTriggers) -ne $ComTriggerCount) { throw "task_trigger_oracle_disagreement" }
    return $ComTriggerCount
}

# [CI5] Scheduled task contract.
function Assert-XbWorkerTaskContract {
    param([Parameter(Mandatory)]$Task, $ExpectedIdentity)
    if ([string]$Task.TaskPath -cne $taskPath -or [string]$Task.TaskName -cne $taskName) { throw "task_identity_invalid" }
    $registered = Get-XbRegisteredWorkerTaskView
    $definition = $registered.Definition
    if ($Task.State -ne "Disabled" -or [bool]$registered.Enabled -or [bool]$definition.Settings.Enabled) { throw "task_not_disabled" }
    $triggerCount = Get-XbTaskTriggerOracleCount -ComTriggerCount ([int]$definition.Triggers.Count) -TaskXml ([string]$registered.Xml) -CimTriggers $Task.Triggers
    if ($triggerCount -ne 0) { throw "task_triggers_present" }
    $actions = @(@($Task.Actions) | Where-Object { $null -ne $_ })
    if ($actions.Count -ne 1 -or [int]$definition.Actions.Count -ne 1) { throw "task_action_count_invalid" }
    $arguments = [string]$actions[0].Arguments
    if ($arguments -notmatch '(?:^|\s)-Mode\s+DisabledProof(?:\s|$)') { throw "task_action_mode_invalid" }
    if ($arguments -match 'EnableProduction(?:Worker|Adapter)') { throw "task_production_switch_present" }
    if ([string]$Task.Settings.MultipleInstances -ne "IgnoreNew") { throw "task_multiple_instances_invalid" }
    $executionLimit = $Task.Settings.ExecutionTimeLimit
    if ($executionLimit -is [string]) {
        try { $executionLimit = [Xml.XmlConvert]::ToTimeSpan($executionLimit) } catch { throw "task_execution_limit_invalid" }
    }
    if ([TimeSpan]$executionLimit -ne [TimeSpan]::FromMinutes(10)) { throw "task_execution_limit_invalid" }
    if ([int]$Task.Settings.RestartCount -ne 0 -or [bool]$Task.Settings.StartWhenAvailable) { throw "task_retry_contract_invalid" }
    if ($null -eq $ExpectedIdentity) {
        $ExpectedIdentity = Get-XbWorkerTaskIdentity -LauncherPath (Join-Path $InstallRoot "launch_ac2_member_gateway_worker.ps1") -WorkerAccount $WorkerAccount
    }
    $action = $actions[0]
    $comAction = $definition.Actions.Item(1)
    if ([int]$comAction.Type -ne 0 -or [string]$comAction.Path -cne [string]$ExpectedIdentity.executable -or [string]$comAction.Arguments -cne [string]$ExpectedIdentity.arguments) { throw "task_identity_invalid" }
    $workingDirectory = if ($action.PSObject.Properties.Name -contains "WorkingDirectory") { [string]$action.WorkingDirectory } else { "" }
    $principal = $Task.Principal
    if ([string]$action.Execute -cne [string]$ExpectedIdentity.executable -or
        [string]$action.Arguments -cne [string]$ExpectedIdentity.arguments -or
        $workingDirectory -cne [string]$ExpectedIdentity.working_directory -or
        $null -eq $principal -or
        [string]$principal.UserId -cne [string]$ExpectedIdentity.principal_user_id -or
        [string]$principal.LogonType -cne [string]$ExpectedIdentity.principal_logon_type -or
        [string]$principal.RunLevel -cne [string]$ExpectedIdentity.principal_run_level) { throw "task_identity_invalid" }
}

function Confirm-XbWorkerTaskAbsent {
    $task = Get-XbWorkerTaskIfPresent
    if ($null -eq $task) { return }
    if ($null -ne $task) { throw "task_still_present" }
}

function Get-XbWorkerTaskIfPresent {
    try {
        return Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop
    } catch {
        if ([string]$_.CategoryInfo.Category -eq "ObjectNotFound") { return $null }
        throw "task_presence_unproven"
    }
}

function Remove-XbWorkerScheduledTask {
    try {
        Unregister-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Confirm:$false -ErrorAction Stop | Out-Null
    } catch {
        throw "task_unregister_failed"
    }
    Confirm-XbWorkerTaskAbsent
}

function Remove-XbWorkerOwnedState {
    param([switch]$TaskMayExist)
    if ($TaskMayExist) { Remove-XbWorkerScheduledTask }
    if (Test-Path -LiteralPath $InstallRoot) { Remove-Item -LiteralPath $InstallRoot -Recurse -Force -ErrorAction Stop }
    if (Test-Path -LiteralPath $RuntimeRoot) { Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force -ErrorAction Stop }
}

function Test-XbContainerPresent {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$ErrorId)
    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch [Management.Automation.ItemNotFoundException] { return $false }
    catch { throw $ErrorId }
    if (-not $item.PSIsContainer) { throw $ErrorId }
    return $true
}

function Get-XbInstallPreimage {
    return [ordered]@{
        program_files_parent = Test-XbContainerPresent -Path (Split-Path -Parent $InstallRoot) -ErrorId "container_preimage_unproven"
        program_data_parent = Test-XbContainerPresent -Path (Split-Path -Parent $RuntimeRoot) -ErrorId "container_preimage_unproven"
        scheduler_folder = Test-XbTaskFolderPresent -Path $taskPath -ErrorId "task_folder_preimage_unproven"
    }
}

# Rollback may remove an attempt-created parent only while it is an ordinary, empty, non-reparse directory.
function Remove-XbAttemptCreatedContainer {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-XbContainerPresent -Path $Path -ErrorId "container_not_owned_empty")) { return }
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        $childCount = @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop).Count
    } catch { throw "container_not_owned_empty" }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $childCount -ne 0) { throw "container_not_owned_empty" }
    Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (Test-XbContainerPresent -Path $Path -ErrorId "container_presence_unproven") { throw "container_still_present" }
}

function Remove-XbAttemptCreatedTaskFolder {
    $service = Connect-XbTaskService
    $folderPath = ConvertTo-XbComFolderPath $taskPath
    try { $folder = $service.GetFolder($folderPath) }
    catch {
        if ((Get-XbComHResult $_) -eq -2147024894) { return }
        throw "container_not_owned_empty"
    }
    try {
        $taskCount = [int]$folder.GetTasks(1).Count
        $folderCount = [int]$folder.GetFolders(0).Count
    } catch { throw "container_not_owned_empty" }
    if ($folderPath -eq "\" -or $taskCount -ne 0 -or $folderCount -ne 0) { throw "container_not_owned_empty" }
    $separator = $folderPath.LastIndexOf('\')
    $parentPath = if ($separator -eq 0) { "\" } else { $folderPath.Substring(0, $separator) }
    try { $service.GetFolder($parentPath).DeleteFolder($folderPath.Substring($separator + 1), 0) }
    catch { throw "task_folder_delete_failed" }
    if (Test-XbTaskFolderPresent -Path $taskPath -ErrorId "task_folder_presence_unproven") { throw "task_folder_still_present" }
}

# Task preimage is proven absent before Install, so an absent task after an attempted registration is restored state.
function Remove-XbAttemptRegisteredTask {
    param([Parameter(Mandatory)][string]$LauncherPath)
    $task = Get-XbWorkerTaskIfPresent
    if ($null -eq $task) { return }
    $expected = Get-XbWorkerTaskIdentity -LauncherPath $LauncherPath -WorkerAccount $WorkerAccount
    $actions = @(@($task.Actions) | Where-Object { $null -ne $_ })
    if ($actions.Count -ne 1 -or
        [string]$actions[0].Execute -cne [string]$expected.executable -or
        [string]$actions[0].Arguments -cne [string]$expected.arguments -or
        $null -eq $task.Principal -or
        [string]$task.Principal.UserId -cne [string]$expected.principal_user_id) { throw "task_identity_unexpected" }
    Remove-XbWorkerScheduledTask
}

function Invoke-XbWorkerInstallRollback {
    param([Parameter(Mandatory)]$Preimage, [Parameter(Mandatory)][string]$StageRoot, [Parameter(Mandatory)][bool]$RegistrationAttempted)
    if ($RegistrationAttempted) { Remove-XbAttemptRegisteredTask -LauncherPath (Join-Path $InstallRoot "launch_ac2_member_gateway_worker.ps1") }
    Remove-XbWorkerOwnedState
    if (Test-Path -LiteralPath $StageRoot) { Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction Stop }
    if (-not $Preimage.scheduler_folder) { Remove-XbAttemptCreatedTaskFolder }
    if (-not $Preimage.program_files_parent) { Remove-XbAttemptCreatedContainer -Path (Split-Path -Parent $InstallRoot) }
    if (-not $Preimage.program_data_parent) { Remove-XbAttemptCreatedContainer -Path (Split-Path -Parent $RuntimeRoot) }
}

function Assert-XbUninstallOwnership {
    $installed = Read-XbInstalledManifest
    Assert-XbExactProperties $installed.reviewed_source @("commit", "tree") "installation_manifest_invalid"
    Assert-XbExactProperties $installed.task @("path", "name", "enabled", "trigger_count", "action_mode", "production_switches", "multiple_instances", "execution_time_limit", "restart_count", "start_when_available", "executable", "launcher_path", "arguments", "working_directory", "principal_user_id", "principal_logon_type", "principal_run_level") "installation_manifest_invalid"
    $entries = @($installed.package_files)
    if ($entries.Count -ne $packageFiles.Count) { throw "installation_manifest_membership_invalid" }
    foreach ($name in $packageFiles) {
        $matches = @($entries | Where-Object { [string]$_.name -ceq $name })
        foreach ($match in $matches) { Assert-XbExactProperties $match @("name", "sha256") "installation_manifest_membership_invalid" }
        $path = Join-Path $InstallRoot $name
        if ($matches.Count -ne 1 -or -not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-XbFileSha256 $path) -cne [string]$matches[0].sha256) { throw "installation_manifest_membership_invalid" }
    }
    $allowedInstall = @($packageFiles + "installation-manifest.json" | Sort-Object)
    $actualInstall = @(Get-ChildItem -LiteralPath $InstallRoot -Force | ForEach-Object Name | Sort-Object)
    if (@(Compare-Object $actualInstall $allowedInstall).Count -ne 0) { throw "installation_owned_surface_unknown" }
    if (@(Compare-Object @($installed.rollback_owned_roots | Sort-Object) @("config", "logs", "rollback", "secrets")).Count -ne 0) { throw "installation_runtime_roots_invalid" }
    $expectedLauncher = [IO.Path]::GetFullPath((Join-Path $InstallRoot "launch_ac2_member_gateway_worker.ps1"))
    if ([string]$installed.task.executable -cne $script:XbWindowsPowerShellPath -or [string]$installed.task.launcher_path -cne $expectedLauncher -or [string]$installed.task.arguments -cne (Get-XbWorkerTaskArguments -LauncherPath $expectedLauncher) -or [string]$installed.task.working_directory -cne "" -or [string]::IsNullOrWhiteSpace([string]$installed.task.principal_user_id) -or [string]$installed.task.principal_logon_type -cne "Password" -or [string]$installed.task.principal_run_level -cne "Limited") { throw "installation_manifest_task_invalid" }
    if ([string]$installed.task.path -cne $taskPath -or [string]$installed.task.name -cne $taskName -or $installed.task.enabled -ne $false -or [int]$installed.task.trigger_count -ne 0 -or [string]$installed.task.action_mode -cne "DisabledProof" -or @($installed.task.production_switches).Count -ne 0 -or [string]$installed.task.multiple_instances -cne "IgnoreNew" -or [string]$installed.task.execution_time_limit -cne "PT10M" -or [int]$installed.task.restart_count -ne 0 -or $installed.task.start_when_available -ne $false) { throw "installation_manifest_task_invalid" }
    $runtimeChildren = @(Get-ChildItem -LiteralPath $RuntimeRoot -Force)
    if (@(Compare-Object @($runtimeChildren.Name | Sort-Object) @("config", "logs", "rollback", "secrets")).Count -ne 0) { throw "installation_owned_surface_unknown" }
    foreach ($child in $runtimeChildren) { if (-not $child.PSIsContainer -or @(Get-ChildItem -LiteralPath $child.FullName -Force).Count -ne 0) { throw "installation_owned_surface_unknown" } }
    $task = Get-XbWorkerTaskIfPresent
    if ($null -eq $task) { throw "installation_task_ownership_ambiguous" }
    $expectedIdentity = [ordered]@{
        executable = [string]$installed.task.executable
        launcher_path = [string]$installed.task.launcher_path
        arguments = [string]$installed.task.arguments
        working_directory = [string]$installed.task.working_directory
        principal_user_id = [string]$installed.task.principal_user_id
        principal_logon_type = [string]$installed.task.principal_logon_type
        principal_run_level = [string]$installed.task.principal_run_level
    }
    Assert-XbWorkerTaskContract -Task $task -ExpectedIdentity $expectedIdentity
    return $installed
}

function Register-XbWorkerScheduledTask {
    param([Parameter(Mandatory)][string]$LauncherPath)
    $action = New-ScheduledTaskAction -Execute $script:XbWindowsPowerShellPath -Argument ('-NoLogo -NoProfile -NonInteractive -File "{0}" -Mode DisabledProof' -f $LauncherPath)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) -RestartCount 0 -StartWhenAvailable:$false -Disable
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $WorkerAccount -LogonType Password -RunLevel Limited
    $task = New-ScheduledTask -Action $action -Settings $settings -Principal $taskPrincipal
    $plainPassword = $TaskCredential.GetNetworkCredential().Password
    $script:XbTaskRegistrationAttempted = $true
    try { Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -InputObject $task -User $WorkerAccount -Password $plainPassword -Force | Out-Null; $script:XbTaskCreated = $true }
    finally { $plainPassword = $null }
    Assert-XbWorkerTaskContract -Task (Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop) -ExpectedIdentity (Get-XbWorkerTaskIdentity -LauncherPath $LauncherPath -WorkerAccount $WorkerAccount)
}

# ---------------------------------------------------------------------------
# Release integrity checks CI1-CI7 (bound in this installer, the tests and
# docs/autocount2-automation/member_write_v2_live_runbook.md):
#   CI1 install layout/roots          Assert-XbWorkerInstallLayout
#   CI2 reviewed-package identity      Read-XbReviewedPackageIdentity (commit, tree, git blob, sha256 membership)
#   CI3 staged package bytes           Assert-XbStagedPackageIdentity
#   CI4 release content                Assert-XbReleaseContent (exactly one SaveMember( call site, no
#                                      DeleteMember, no UAT/probe/cleanup script in the package)
#   CI5 scheduled task contract        Assert-XbWorkerTaskContract (disabled, no triggers, IgnoreNew,
#                                      10 minute limit, no retries, absolute interpreter, DisabledProof only)
#   CI6 DPAPI/runtime ACL custody      Assert-XbRuntimeCustody
#   CI7 install verifier               Invoke-XbInstallVerifier (re-runs CI1, CI4-CI6 on the installed
#                                      bytes, recomputes the release identity, effective-rights check)
# ---------------------------------------------------------------------------

# Release identity (primitive.release_sha256): SHA-256 over the ASCII lines
# "<file name>:<sha256>" + LF for every release package file, names sorted
# ordinally. Identical to Get-XbAc2ReleaseIdentityFromEntries in the adapter.
function Get-XbReleaseIdentityFromEntries {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Entries)
    $names = [string[]]@($Entries.Keys)
    [Array]::Sort($names, [StringComparer]::Ordinal)
    $builder = New-Object System.Text.StringBuilder
    foreach ($name in $names) {
        $digest = [string]$Entries[$name]
        if ($name -cnotmatch '^[A-Za-z0-9_.-]{1,120}$' -or $digest -cnotmatch '^[0-9a-f]{64}$') { throw "release_identity_entry_invalid" }
        [void]$builder.Append($name).Append(":").Append($digest).Append("`n")
    }
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::ASCII.GetBytes($builder.ToString())))).Replace("-", "").ToLowerInvariant() }
    finally { $algorithm.Dispose() }
}

function Get-XbReleaseIdentityFromRoot {
    param([Parameter(Mandatory)][string]$Root)
    $entries = [ordered]@{}
    foreach ($name in $packageFiles) {
        $path = Join-Path $Root $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "release_identity_file_missing" }
        $entries[$name] = Get-XbFileSha256 $path
    }
    return (Get-XbReleaseIdentityFromEntries -Entries $entries)
}

# [CI4] Release content over a package root (staged or installed).
function Assert-XbReleaseContent {
    param([Parameter(Mandatory)][string]$Root)
    $names = @(Get-ChildItem -LiteralPath $Root -File -Force | ForEach-Object Name)
    foreach ($name in $names) {
        if ($name -match '(?i)(uat|probe|cleanup)') { throw "release_content_forbidden_file" }
        if ($name -cne "installation-manifest.json" -and $packageFiles -cnotcontains $name) { throw "release_content_unknown_file" }
    }
    $saveSites = 0
    foreach ($name in $packageFiles) {
        $path = Join-Path $Root $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "release_content_file_missing" }
        $text = [IO.File]::ReadAllText($path)
        if ($text.Contains("DeleteMember")) { throw "release_content_delete_present" }
        $sites = ([regex]::Matches($text, [regex]::Escape("SaveMember("))).Count
        if ($sites -gt 0 -and $name -cne "ac2_member_gateway_autocount_adapter.ps1") { throw "release_content_save_call_sites_invalid" }
        $saveSites += $sites
    }
    if ($saveSites -ne 1) { throw "release_content_save_call_sites_invalid" }
}

# [CI6] DPAPI secret artifacts: a CLIXML export of a SecureString (<SS>),
# never a plain string. The content is checked without decrypting it.
function Test-XbSecureStringArtifact {
    param([Parameter(Mandatory)][string]$Path)
    try {
        $document = New-Object Xml.XmlDocument
        $document.Load($Path)
        $namespaces = New-Object Xml.XmlNamespaceManager($document.NameTable)
        $namespaces.AddNamespace("ps", "http://schemas.microsoft.com/powershell/2004/04")
        $root = $document.SelectNodes("/ps:Objs/*", $namespaces)
        return ($root.Count -eq 1 -and $root[0].LocalName -ceq "SS" -and -not [string]::IsNullOrWhiteSpace($root[0].InnerText))
    }
    catch { return $false }
}

$script:XbBroadPrincipalSids = @("S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-4", "S-1-5-2", "S-1-5-7")

function Get-XbAccountSid {
    param([Parameter(Mandatory)][string]$Account)
    try { return ([Security.Principal.NTAccount]::new($Account)).Translate([Security.Principal.SecurityIdentifier]).Value }
    catch { throw "worker_account_unresolved" }
}

# Rights the ACL grants (allow rules) to the given SIDs on one path.
function Get-XbGrantedRights {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string[]]$Sids)
    $mask = 0
    $acl = Get-Acl -LiteralPath $Path
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        if ($Sids -contains $rule.IdentityReference.Value) { $mask = $mask -bor [int]$rule.FileSystemRights }
    }
    return $mask
}

# [CI6] Runtime custody: protected (non-inherited) ACLs on every runtime root,
# no broad principal grants, and DPAPI secret artifacts when required.
function Assert-XbRuntimeCustody {
    param([switch]$RequireSecrets)
    foreach ($child in @("config", "secrets", "logs", "rollback")) {
        $path = Join-Path $RuntimeRoot $child
        if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw "runtime_custody_root_missing" }
        $acl = Get-Acl -LiteralPath $path
        if (-not $acl.AreAccessRulesProtected) { throw "runtime_custody_acl_invalid" }
        if ((Get-XbGrantedRights -Path $path -Sids $script:XbBroadPrincipalSids) -ne 0) { throw "runtime_custody_acl_invalid" }
    }
    foreach ($artifact in @("worker-token.clixml", "autocount-password.clixml")) {
        $path = Join-Path (Join-Path $RuntimeRoot "secrets") $artifact
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            if (-not (Test-XbSecureStringArtifact -Path $path)) { throw "runtime_custody_secret_invalid" }
        }
        elseif ($RequireSecrets) { throw "runtime_custody_secret_missing" }
    }
}

function Initialize-XbWorkerNativeAccess {
    $tokenType = "XbWorkerBatchToken" -as [type]
    $resultType = "XbWorkerAccessResult" -as [type]
    $state = Get-Variable -Name XbWorkerNativeAccessState -Scope Script -ErrorAction SilentlyContinue
    if ($null -ne $state) {
        if ([string]$state.Value -ceq "xb-worker-native-access-v1" -and $null -ne $tokenType -and $null -ne $resultType) { return }
        throw "effective_rights_unproven"
    }
    if ($null -ne $tokenType -or $null -ne $resultType) { throw "effective_rights_unproven" }

    $nativeSource = @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security;
using System.Security.Principal;

public sealed class XbWorkerAccessResult
{
    public bool Allowed { get; private set; }
    public uint GrantedAccess { get; private set; }

    public XbWorkerAccessResult(bool allowed, uint granted)
    {
        Allowed = allowed;
        GrantedAccess = granted;
    }
}

public sealed class XbWorkerBatchToken : IDisposable
{
    private const int ErrorInsufficientBuffer = 122;
    private const int LOGON32_LOGON_BATCH = 4;
    private const int Logon32ProviderDefault = 0;
    private const int SecurityImpersonation = 2;
    private const int TokenImpersonation = 2;
    private const int TokenQuery = 0x0008;
    private const int TokenDuplicate = 0x0002;
    private const int TokenImpersonate = 0x0004;
    private const int TokenTypeInformationClass = 8;
    private const int TokenImpersonationLevelInformationClass = 9;
    private const int TokenIsAppContainerInformationClass = 29;

    [StructLayout(LayoutKind.Sequential)]
    private struct GenericMapping
    {
        public uint GenericRead;
        public uint GenericWrite;
        public uint GenericExecute;
        public uint GenericAll;
    }

    private IntPtr token;

    public string UserSid { get; private set; }
    public int TokenType { get; private set; }
    public int ImpersonationLevel { get; private set; }

    [DllImport("advapi32.dll", EntryPoint = "LogonUserW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool LogonUser(
        string user, string domain, IntPtr password, int logonType, int provider, out IntPtr token);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DuplicateTokenEx(
        IntPtr source, uint desiredAccess, IntPtr attributes, int impersonationLevel, int tokenType, out IntPtr token);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetTokenInformation(
        IntPtr token, int informationClass, IntPtr information, uint informationLength, out uint returnedLength);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AccessCheck(
        IntPtr securityDescriptor,
        IntPtr clientToken,
        uint desiredAccess,
        ref GenericMapping genericMapping,
        IntPtr privilegeSet,
        ref uint privilegeSetLength,
        out uint grantedAccess,
        [MarshalAs(UnmanagedType.Bool)] out bool accessAllowed);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ImpersonateLoggedOnUser(IntPtr token);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool RevertToSelf();

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);

    private XbWorkerBatchToken(IntPtr handle, string sid, int type, int level)
    {
        token = handle;
        UserSid = sid;
        TokenType = type;
        ImpersonationLevel = level;
    }

    private static void Zero(IntPtr buffer, int length)
    {
        if (buffer == IntPtr.Zero) return;
        for (int index = 0; index < length; index++) Marshal.WriteByte(buffer, index, 0);
    }

    private static int ReadTokenInteger(IntPtr handle, int informationClass)
    {
        uint length = 0;
        bool querySucceeded = GetTokenInformation(handle, informationClass, IntPtr.Zero, 0, out length);
        int error = Marshal.GetLastWin32Error();
        if (querySucceeded || error != ErrorInsufficientBuffer || length == 0 || length > 65536)
            throw new InvalidOperationException("token_information_unproven");

        IntPtr buffer = Marshal.AllocHGlobal((int)length);
        try
        {
            uint returned;
            if (!GetTokenInformation(handle, informationClass, buffer, length, out returned) || returned < sizeof(int))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return Marshal.ReadInt32(buffer);
        }
        finally
        {
            Zero(buffer, (int)length);
            Marshal.FreeHGlobal(buffer);
        }
    }

    public static XbWorkerBatchToken OpenBatch(string user, string domain, SecureString password, string expectedSid)
    {
        if (String.IsNullOrWhiteSpace(user) || password == null || password.Length == 0 || String.IsNullOrWhiteSpace(expectedSid))
            throw new InvalidOperationException("token_input_unproven");

        IntPtr secret = IntPtr.Zero;
        IntPtr primaryToken = IntPtr.Zero;
        IntPtr impersonationToken = IntPtr.Zero;
        try
        {
            secret = Marshal.SecureStringToGlobalAllocUnicode(password);
            if (!LogonUser(user, domain, secret, LOGON32_LOGON_BATCH, Logon32ProviderDefault, out primaryToken))
                throw new Win32Exception(Marshal.GetLastWin32Error());

            uint desiredAccess = TokenQuery | TokenDuplicate | TokenImpersonate;
            if (!DuplicateTokenEx(
                    primaryToken, desiredAccess, IntPtr.Zero, SecurityImpersonation, TokenImpersonation, out impersonationToken))
                throw new Win32Exception(Marshal.GetLastWin32Error());

            int tokenType = ReadTokenInteger(impersonationToken, TokenTypeInformationClass);
            int impersonationLevel = ReadTokenInteger(impersonationToken, TokenImpersonationLevelInformationClass);
            int isAppContainer = ReadTokenInteger(impersonationToken, TokenIsAppContainerInformationClass);
            if (tokenType != TokenImpersonation || impersonationLevel != SecurityImpersonation || isAppContainer != 0)
                throw new InvalidOperationException("token_profile_unproven");

            string userSid;
            using (WindowsIdentity identity = new WindowsIdentity(impersonationToken))
            {
                if (identity.User == null) throw new InvalidOperationException("token_user_unproven");
                userSid = identity.User.Value;
            }
            if (!String.Equals(userSid, expectedSid, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("token_user_mismatch");

            XbWorkerBatchToken result = new XbWorkerBatchToken(impersonationToken, userSid, tokenType, impersonationLevel);
            impersonationToken = IntPtr.Zero;
            return result;
        }
        finally
        {
            if (secret != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(secret);
            if (impersonationToken != IntPtr.Zero) CloseHandle(impersonationToken);
            if (primaryToken != IntPtr.Zero) CloseHandle(primaryToken);
        }
    }

    public XbWorkerAccessResult Check(byte[] securityDescriptorBytes, uint desiredAccess)
    {
        if (token == IntPtr.Zero || securityDescriptorBytes == null ||
            securityDescriptorBytes.Length < 20 || securityDescriptorBytes.Length > 1048576)
            throw new InvalidOperationException("access_check_input_unproven");

        IntPtr securityDescriptor = IntPtr.Zero;
        IntPtr privilegeSet = IntPtr.Zero;
        uint privilegeSetLength = 256;
        try
        {
            securityDescriptor = Marshal.AllocHGlobal(securityDescriptorBytes.Length);
            Marshal.Copy(securityDescriptorBytes, 0, securityDescriptor, securityDescriptorBytes.Length);

            GenericMapping genericMapping = new GenericMapping();
            genericMapping.GenericRead = 0x00120089;
            genericMapping.GenericWrite = 0x00120116;
            genericMapping.GenericExecute = 0x001200A0;
            genericMapping.GenericAll = 0x001F01FF;

            for (int attempt = 0; attempt < 3; attempt++)
            {
                if (privilegeSetLength == 0 || privilegeSetLength > 65536)
                    throw new InvalidOperationException("access_check_buffer_unproven");

                privilegeSet = Marshal.AllocHGlobal((int)privilegeSetLength);
                uint grantedAccess;
                uint returnedLength = privilegeSetLength;
                bool accessAllowed;
                if (AccessCheck(
                        securityDescriptor, token, desiredAccess, ref genericMapping, privilegeSet,
                        ref returnedLength, out grantedAccess, out accessAllowed))
                    return new XbWorkerAccessResult(accessAllowed, grantedAccess);

                int error = Marshal.GetLastWin32Error();
                if (error != ErrorInsufficientBuffer || returnedLength <= privilegeSetLength || attempt == 2)
                    throw new Win32Exception(error);

                Zero(privilegeSet, (int)privilegeSetLength);
                Marshal.FreeHGlobal(privilegeSet);
                privilegeSet = IntPtr.Zero;
                privilegeSetLength = returnedLength;
            }
            throw new InvalidOperationException("access_check_unproven");
        }
        finally
        {
            if (privilegeSet != IntPtr.Zero)
            {
                Zero(privilegeSet, (int)privilegeSetLength);
                Marshal.FreeHGlobal(privilegeSet);
            }
            if (securityDescriptor != IntPtr.Zero)
            {
                Zero(securityDescriptor, securityDescriptorBytes.Length);
                Marshal.FreeHGlobal(securityDescriptor);
            }
        }
    }

    public void Impersonate()
    {
        if (token == IntPtr.Zero || !ImpersonateLoggedOnUser(token))
            throw new Win32Exception(Marshal.GetLastWin32Error());
    }

    public static void Revert()
    {
        if (!RevertToSelf()) throw new Win32Exception(Marshal.GetLastWin32Error());
    }

    public void Dispose()
    {
        if (token == IntPtr.Zero) return;
        CloseHandle(token);
        token = IntPtr.Zero;
    }
}
"@
    try {
        Add-Type -TypeDefinition $nativeSource -Language CSharp -ErrorAction Stop
        if ($null -eq ("XbWorkerBatchToken" -as [type]) -or $null -eq ("XbWorkerAccessResult" -as [type])) {
            throw "native_type_missing"
        }
        $script:XbWorkerNativeAccessState = "xb-worker-native-access-v1"
    } catch {
        throw "effective_rights_unproven"
    }
}

function New-XbWorkerBatchToken {
    param([Management.Automation.PSCredential]$Credential)
    if ($null -eq $Credential -or [string]$Credential.UserName -cne [string]$WorkerAccount) {
        throw "effective_rights_unproven"
    }

    try {
        $separator = ([string]$WorkerAccount).LastIndexOf('\')
        if ($separator -ge 0) {
            $domain = ([string]$WorkerAccount).Substring(0, $separator)
            $userName = ([string]$WorkerAccount).Substring($separator + 1)
            if ($domain -ceq ".") { $domain = [Environment]::MachineName }
        }
        else {
            if ([string]$WorkerAccount -match '@') { throw "effective_rights_unproven" }
            $domain = [Environment]::MachineName
            $userName = [string]$WorkerAccount
        }
        if ([string]::IsNullOrWhiteSpace($userName) -or [string]::IsNullOrWhiteSpace($domain)) {
            throw "effective_rights_unproven"
        }

        $sid = Get-XbAccountSid -Account $WorkerAccount
        Initialize-XbWorkerNativeAccess
        return [XbWorkerBatchToken]::OpenBatch($userName, $domain, $Credential.Password, $sid)
    }
    catch {
        throw "effective_rights_unproven"
    }
}

function Get-XbNativePathChain {
    param([Parameter(Mandatory)][string]$Path)

    try {
        $fullPath = [IO.Path]::GetFullPath($Path)
        if ($fullPath -notmatch '^[A-Za-z]:\\' -or $fullPath.StartsWith('\\\\') -or $fullPath.Substring(2).Contains(':')) {
            throw "effective_rights_unproven"
        }

        $root = [IO.Path]::GetPathRoot($fullPath)
        $drive = [IO.DriveInfo]::new($root)
        if ($drive.DriveType -ne [IO.DriveType]::Fixed -or -not $drive.IsReady) {
            throw "effective_rights_unproven"
        }

        $chain = @($root)
        $currentPath = $root
        foreach ($part in @($fullPath.Substring($root.Length).Split('\') | Where-Object { $_ })) {
            $currentPath = Join-Path $currentPath $part
            if (-not (Test-Path -LiteralPath $currentPath -ErrorAction Stop)) {
                throw "effective_rights_unproven"
            }
            $item = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "effective_rights_unproven"
            }
            $chain += $currentPath
        }
        return ,$chain
    }
    catch {
        throw "effective_rights_unproven"
    }
}

function Invoke-XbNativeAccessCheck {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Token,
        [Parameter(Mandatory)][uint32]$DesiredAccess
    )

    try {
        $securityDescriptor = (Get-Acl -LiteralPath $Path -ErrorAction Stop).GetSecurityDescriptorBinaryForm()
        return $Token.Check($securityDescriptor, $DesiredAccess)
    }
    catch {
        throw "effective_rights_unproven"
    }
}

function Test-XbNativeAccessAllowed {
    param([string]$Path, $Token, [uint32]$DesiredAccess)
    return [bool](Invoke-XbNativeAccessCheck -Path $Path -Token $Token -DesiredAccess $DesiredAccess).Allowed
}

function Assert-XbLogsRootAclShape {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$WorkerSid
    )

    try {
        $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
        if (-not $acl.AreAccessRulesProtected) { throw "effective_rights_unproven" }
        $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($ownerSid -cnotin @("S-1-5-18", "S-1-5-32-544")) { throw "effective_rights_unproven" }

        $fullControl = [int][Security.AccessControl.FileSystemRights]::FullControl
        $modifyAndSynchronize = [int]([Security.AccessControl.FileSystemRights]::Modify -bor [Security.AccessControl.FileSystemRights]::Synchronize)
        if ($modifyAndSynchronize -ne 0x001301BF) { throw "effective_rights_unproven" }

        $actual = @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]) | ForEach-Object {
            "{0}|{1}|{2}|{3}|{4}|{5}" -f $_.IdentityReference.Value, [int]$_.FileSystemRights,
                [int]$_.AccessControlType, [int]$_.InheritanceFlags, [int]$_.PropagationFlags, [bool]$_.IsInherited
        } | Sort-Object)
        $expected = @(
            ("{0}|{1}|0|3|0|False" -f "S-1-5-18", $fullControl)
            ("{0}|{1}|0|3|0|False" -f "S-1-5-32-544", $fullControl)
            ("{0}|{1}|0|0|0|False" -f $WorkerSid, 0x001200AB)
            ("{0}|{1}|0|2|2|False" -f $WorkerSid, $modifyAndSynchronize)
            ("{0}|{1}|0|2|2|False" -f "S-1-3-4", $modifyAndSynchronize)
        ) | Sort-Object
        if (@(Compare-Object $actual $expected).Count -ne 0) { throw "effective_rights_unproven" }
    }
    catch {
        if ([string]$_.Exception.Message -eq "effective_rights_unproven") { throw }
        throw "effective_rights_unproven"
    }
}

function Assert-XbOwnerRightsLogFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$WorkerSid
    )

    try {
        $null = Get-XbNativePathChain -Path $Path
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($item.PSIsContainer -or $item.Name -notlike "launcher-*.jsonl" -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "effective_rights_unproven"
        }

        $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
        if ($acl.AreAccessRulesProtected -or
            $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne $WorkerSid) {
            throw "effective_rights_unproven"
        }

        $ownerRightsSid = "S-1-3-4"
        $ownerRules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
            Where-Object { $_.IdentityReference.Value -ceq $ownerRightsSid })
        if ($ownerRules.Count -ne 1 -or
            -not $ownerRules[0].IsInherited -or
            $ownerRules[0].AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $ownerRules[0].InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]::None -or
            $ownerRules[0].PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None -or
            [int]$ownerRules[0].FileSystemRights -ne 0x001301BF) {
            throw "effective_rights_unproven"
        }
    }
    catch {
        if ([string]$_.Exception.Message -eq "effective_rights_unproven") { throw }
        throw "effective_rights_unproven"
    }
}

function Assert-XbPathNotDeleteable {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Token
    )

    $chain = @(Get-XbNativePathChain -Path $Path)
    for ($index = $chain.Count - 1; $index -ge 0; $index--) {
        if (Test-XbNativeAccessAllowed -Path $chain[$index] -Token $Token -DesiredAccess ([uint32]0x00010000)) {
            throw "effective_rights_exceeded"
        }
        if ($index -gt 0 -and
            (Test-XbNativeAccessAllowed -Path $chain[$index - 1] -Token $Token -DesiredAccess ([uint32]0x00000040))) {
            throw "effective_rights_exceeded"
        }
    }
}

# [CI7] Native token-based effective access over the full security descriptor.
function Assert-XbWorkerEffectiveRights {
    param([Management.Automation.PSCredential]$TaskCredential)
    if ($null -eq $TaskCredential -or
        [string]::IsNullOrWhiteSpace($WorkerAccount) -or
        [string]$TaskCredential.UserName -cne [string]$WorkerAccount) {
        throw "effective_rights_unproven"
    }

    $token = $null
    try {
        try {
            $token = New-XbWorkerBatchToken -Credential $TaskCredential
        }
        catch {
            throw "effective_rights_unproven"
        }

        $workerSid = Get-XbAccountSid -Account $WorkerAccount
        if ($null -eq $token -or
            [string]$token.UserSid -cne $workerSid -or
            [int]$token.TokenType -ne 2 -or
            [int]$token.ImpersonationLevel -ne 2) {
            throw "effective_rights_unproven"
        }

        $readExecute = [uint32]0x001200A9
        $read = [uint32]0x00120089
        $logsRoot = [uint32]0x001200AB
        $requiredInstall = @([uint32]0x00000001, [uint32]0x00000008, [uint32]0x00000020,
            [uint32]0x00000080, [uint32]0x00020000, [uint32]0x00100000)
        $requiredRead = @([uint32]0x00000001, [uint32]0x00000008, [uint32]0x00000080,
            [uint32]0x00020000, [uint32]0x00100000)
        $requiredLogsRoot = @([uint32]0x00000001, [uint32]0x00000002, [uint32]0x00000008,
            [uint32]0x00000080, [uint32]0x00020000, [uint32]0x00100000)
        $profiles = @(
            @{ Path = $InstallRoot; Allowed = $readExecute; Required = $requiredInstall }
            @{ Path = (Join-Path $RuntimeRoot "config"); Allowed = $read; Required = $requiredRead }
            @{ Path = (Join-Path $RuntimeRoot "secrets"); Allowed = $read; Required = $requiredRead }
            @{ Path = (Join-Path $RuntimeRoot "logs"); Allowed = $logsRoot; Required = $requiredLogsRoot }
            @{ Path = (Join-Path $RuntimeRoot "rollback"); Allowed = [uint32]0; Required = @() }
        )

        $maximumAllowed = [uint32]0x02000000
        $delete = [uint32]0x00010000
        $deleteChild = [uint32]0x00000040
        $writeDac = [uint32]0x00040000
        $writeOwner = [uint32]0x00080000
        $prohibitedRights = @($delete, $deleteChild, $writeDac, $writeOwner)
        $protectedPaths = New-Object System.Collections.Generic.List[string]

        foreach ($profile in $profiles) {
            $path = [string]$profile.Path
            $null = Get-XbNativePathChain -Path $path
            if (-not (Test-Path -LiteralPath $path -PathType Container -ErrorAction Stop)) {
                throw "effective_rights_unproven"
            }

            foreach ($required in $profile.Required) {
                if (-not (Test-XbNativeAccessAllowed -Path $path -Token $token -DesiredAccess ([uint32]$required))) {
                    throw "effective_rights_missing"
                }
            }
            foreach ($right in $prohibitedRights) {
                if (Test-XbNativeAccessAllowed -Path $path -Token $token -DesiredAccess $right) {
                    throw "effective_rights_exceeded"
                }
            }

            $maximum = Invoke-XbNativeAccessCheck -Path $path -Token $token -DesiredAccess $maximumAllowed
            $allowed = [uint32]$profile.Allowed
            if (([uint32]$maximum.GrantedAccess -band ([uint32]::MaxValue -bxor $allowed)) -ne 0) {
                throw "effective_rights_exceeded"
            }
            $protectedPaths.Add($path)
        }

        $logsPath = Join-Path $RuntimeRoot "logs"
        Assert-XbLogsRootAclShape -Path $logsPath -WorkerSid $workerSid
        foreach ($path in $protectedPaths) {
            Assert-XbPathNotDeleteable -Path $path -Token $token
        }

        foreach ($item in @(Get-ChildItem -LiteralPath $logsPath -Force -ErrorAction Stop)) {
            if ($item.PSIsContainer -or $item.Name -notlike "launcher-*.jsonl") {
                throw "effective_rights_unproven"
            }
            Assert-XbOwnerRightsLogFile -Path $item.FullName -WorkerSid $workerSid

            $fileRights = [uint32]0x001301BF
            if (-not (Test-XbNativeAccessAllowed -Path $item.FullName -Token $token -DesiredAccess $fileRights)) {
                throw "effective_rights_missing"
            }
            if (-not (Test-XbNativeAccessAllowed -Path $item.FullName -Token $token -DesiredAccess $delete)) {
                throw "effective_rights_missing"
            }
            foreach ($right in @($writeDac, $writeOwner)) {
                if (Test-XbNativeAccessAllowed -Path $item.FullName -Token $token -DesiredAccess $right) {
                    throw "effective_rights_exceeded"
                }
            }
            if (Test-XbNativeAccessAllowed -Path $logsPath -Token $token -DesiredAccess $deleteChild) {
                throw "effective_rights_exceeded"
            }

            $maximum = Invoke-XbNativeAccessCheck -Path $item.FullName -Token $token -DesiredAccess $maximumAllowed
            if (([uint32]$maximum.GrantedAccess -band ([uint32]::MaxValue -bxor $fileRights)) -ne 0) {
                throw "effective_rights_exceeded"
            }
        }
    }
    catch {
        $reason = [string]$_.Exception.Message
        if ($reason -in @("effective_rights_missing", "effective_rights_exceeded", "effective_rights_unproven")) {
            throw $reason
        }
        throw "effective_rights_unproven"
    }
    finally {
        if ($null -ne $token) {
            try { $token.Dispose() }
            catch { throw "effective_rights_unproven" }
        }
    }
}

function Read-XbInstalledManifest {
    param([switch]$AllowPrevious)
    $manifestPath = Join-Path $InstallRoot "installation-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "installation_manifest_missing" }
    try { $installed = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json } catch { throw "installation_manifest_invalid" }
    $schema = [string]$installed.schema_version
    if ($schema -ceq $script:XbInstallationManifestSchema) {
        Assert-XbExactProperties $installed @("schema_version", "reviewed_source", "release_sha256", "install_root", "runtime_root", "package_files", "task", "rollback_owned_roots") "installation_manifest_invalid"
        if ([string]$installed.release_sha256 -cnotmatch '^[0-9a-f]{64}$') { throw "installation_manifest_invalid" }
    }
    elseif ($AllowPrevious -and $schema -ceq "xb.member.gateway.worker.installation.v1") {
        Assert-XbExactProperties $installed @("schema_version", "reviewed_source", "install_root", "runtime_root", "package_files", "task", "rollback_owned_roots") "installation_manifest_invalid"
    }
    else { throw "installation_manifest_invalid" }
    if ([string]$installed.install_root -cne "C:\Program Files\X-Boundaries\MemberGatewayWorker\" -or [string]$installed.runtime_root -cne "C:\ProgramData\X-Boundaries\MemberGatewayWorker\") { throw "installation_manifest_path_invalid" }
    foreach ($entry in @($installed.package_files)) {
        Assert-XbExactProperties $entry @("name", "sha256") "installation_manifest_membership_invalid"
        if ([string]$entry.name -cnotmatch '^[A-Za-z0-9_.-]{1,120}$' -or [string]$entry.sha256 -cnotmatch '^[0-9a-f]{64}$') { throw "installation_manifest_membership_invalid" }
    }
    return $installed
}

# [CI7] Install verifier: read-only; re-proves the installed release.
function Invoke-XbInstallVerifier {
    param([Management.Automation.PSCredential]$TaskCredential, [switch]$RequireSecrets)
    $checks = [ordered]@{}
    Assert-XbWorkerInstallLayout
    $checks.CI1_install_layout = "pass"
    $installed = Read-XbInstalledManifest
    $entries = @($installed.package_files)
    if ($entries.Count -ne $packageFiles.Count) { throw "installation_manifest_membership_invalid" }
    foreach ($name in $packageFiles) {
        $match = @($entries | Where-Object { [string]$_.name -ceq $name })
        $path = Join-Path $InstallRoot $name
        if ($match.Count -ne 1 -or -not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-XbFileSha256 $path) -cne [string]$match[0].sha256) { throw "installation_manifest_membership_invalid" }
    }
    if ((Get-XbReleaseIdentityFromRoot -Root $InstallRoot) -cne [string]$installed.release_sha256) { throw "release_identity_mismatch" }
    $checks.CI2_reviewed_package_identity = "pass"
    $actualInstall = @(Get-ChildItem -LiteralPath $InstallRoot -Force | ForEach-Object Name | Sort-Object)
    if (@(Compare-Object $actualInstall @($packageFiles + "installation-manifest.json" | Sort-Object)).Count -ne 0) { throw "installation_owned_surface_unknown" }
    $checks.CI3_installed_package_bytes = "pass"
    Assert-XbReleaseContent -Root $InstallRoot
    $checks.CI4_release_content = "pass"
    $task = Get-XbWorkerTaskIfPresent
    if ($null -eq $task) { throw "installation_task_missing" }
    Assert-XbWorkerTaskContract -Task $task
    $checks.CI5_task_contract = "pass"
    Assert-XbRuntimeCustody -RequireSecrets:$RequireSecrets
    $checks.CI6_runtime_custody = "pass"
    Assert-XbWorkerEffectiveRights -TaskCredential $TaskCredential
    $checks.CI7_effective_rights = "pass"
    return [ordered]@{ status = "install_verified"; release_sha256 = [string]$installed.release_sha256; checks = $checks }
}

# In-place upgrade: preserves runtime custody (config, secrets, logs), keeps
# the task disabled, snapshots the previous install for restore.
function Invoke-XbWorkerUpgrade {
    param([Parameter(Mandatory)][string]$SourceRoot, [Parameter(Mandatory)]$ReviewedIdentity)
    $previous = Read-XbInstalledManifest -AllowPrevious
    if ($null -eq (Get-XbWorkerTaskIfPresent)) { throw "upgrade_task_missing" }
    foreach ($child in @("config", "secrets", "logs", "rollback")) {
        if (-not (Test-Path -LiteralPath (Join-Path $RuntimeRoot $child) -PathType Container)) { throw "upgrade_runtime_root_missing" }
    }
    $previousNames = @(@($previous.package_files) | ForEach-Object { [string]$_.name })
    $actualInstall = @(Get-ChildItem -LiteralPath $InstallRoot -Force | ForEach-Object Name)
    foreach ($name in $actualInstall) {
        if ($name -cne "installation-manifest.json" -and $previousNames -cnotcontains $name) { throw "installation_owned_surface_unknown" }
    }
    $manifest = New-XbWorkerInstallationManifest -PackageRoot $SourceRoot -ReviewedIdentity $ReviewedIdentity -WorkerAccount $WorkerAccount
    $stageRoot = Join-Path ([IO.Path]::GetTempPath()) ("xb-member-worker-" + [Guid]::NewGuid().ToString("N"))
    $snapshot = Join-Path (Join-Path $RuntimeRoot "rollback") ("upgrade-" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"))
    try {
        New-Item -ItemType Directory -Path $stageRoot | Out-Null
        foreach ($name in $packageFiles) { Copy-Item -LiteralPath (Join-Path $SourceRoot $name) -Destination (Join-Path $stageRoot $name) }
        Assert-XbStagedPackageIdentity -StageRoot $stageRoot -ReviewedIdentity $ReviewedIdentity
        Assert-XbReleaseContent -Root $stageRoot
        if (Test-Path -LiteralPath $snapshot) { throw "upgrade_snapshot_exists" }
        New-Item -ItemType Directory -Path $snapshot | Out-Null
        foreach ($name in $actualInstall) { Copy-Item -LiteralPath (Join-Path $InstallRoot $name) -Destination (Join-Path $snapshot $name) }
        try {
            foreach ($name in $actualInstall) { Remove-Item -LiteralPath (Join-Path $InstallRoot $name) -Force -ErrorAction Stop }
            foreach ($name in $packageFiles) { Copy-Item -LiteralPath (Join-Path $stageRoot $name) -Destination (Join-Path $InstallRoot $name) }
            ($manifest | ConvertTo-Json -Depth 12) + "`n" | Set-Content -LiteralPath (Join-Path $InstallRoot "installation-manifest.json") -Encoding UTF8
            Set-XbWorkerAcl -Path $InstallRoot -Kind Install
            Register-XbWorkerScheduledTask -LauncherPath (Join-Path $InstallRoot "launch_ac2_member_gateway_worker.ps1")
            Assert-XbRuntimeCustody
        }
        catch {
            $reason = [string]$_.Exception.Message
            try {
                foreach ($item in @(Get-ChildItem -LiteralPath $InstallRoot -Force)) { Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop }
                foreach ($name in $actualInstall) { Copy-Item -LiteralPath (Join-Path $snapshot $name) -Destination (Join-Path $InstallRoot $name) }
            }
            catch { throw "upgrade_rollback_failed" }
            throw ("upgrade_failed_restored: {0}" -f $reason)
        }
    }
    finally {
        if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction Stop }
    }
    return [ordered]@{ status = "upgraded"; release_sha256 = [string]$manifest.release_sha256; snapshot = (Split-Path -Leaf $snapshot) }
}

function Invoke-XbWorkerInstaller {
Assert-XbWorkerInstallLayout
$sourceRoot = $PSScriptRoot
if ($Operation -eq "Verify") {
    # [CI7] runs on the installed host without a source checkout.
    if ([string]::IsNullOrWhiteSpace($WorkerAccount)) { throw "worker_account_required" }
    (Invoke-XbInstallVerifier -TaskCredential $TaskCredential -RequireSecrets:$RequireSecrets) | ConvertTo-Json -Depth 6 -Compress
    return
}
if ([string]::IsNullOrWhiteSpace($ReviewedPackageManifestPath)) { throw "reviewed_package_identity_required" }
$reviewedIdentity = Read-XbReviewedPackageIdentity -Path $ReviewedPackageManifestPath -PackageRoot $sourceRoot
if ($Operation -eq "ValidateOnly") { (New-XbWorkerInstallationManifest -PackageRoot $sourceRoot -ReviewedIdentity $reviewedIdentity -WorkerAccount "<validate-only>") | ConvertTo-Json -Depth 12; return }

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw "windows_required" }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "administrator_required" }
if ([string]::IsNullOrWhiteSpace($WorkerAccount)) { throw "worker_account_required" }

if ($Operation -eq "Install") {
    if ($null -eq $TaskCredential -or $TaskCredential.UserName -cne $WorkerAccount) { throw "task_credential_required" }
    $manifest = New-XbWorkerInstallationManifest -PackageRoot $sourceRoot -ReviewedIdentity $reviewedIdentity -WorkerAccount $WorkerAccount
    if (Test-Path -LiteralPath $InstallRoot) { throw "install_preimage_exists" }
    if (Test-Path -LiteralPath $RuntimeRoot) { throw "runtime_preimage_exists" }
    if ($null -ne (Get-XbWorkerTaskIfPresent)) { throw "task_preimage_exists" }

    $preimage = Get-XbInstallPreimage
    $stageRoot = Join-Path ([IO.Path]::GetTempPath()) ("xb-member-worker-" + [Guid]::NewGuid().ToString("N"))
    $script:XbTaskCreated = $false
    $script:XbTaskRegistrationAttempted = $false
    try {
        New-Item -ItemType Directory -Path $stageRoot | Out-Null
        foreach ($name in $packageFiles) { Copy-Item -LiteralPath (Join-Path $sourceRoot $name) -Destination (Join-Path $stageRoot $name) }
        Assert-XbStagedPackageIdentity -StageRoot $stageRoot -ReviewedIdentity $reviewedIdentity
        Assert-XbReleaseContent -Root $stageRoot
        ($manifest | ConvertTo-Json -Depth 12) + "`n" | Set-Content -LiteralPath (Join-Path $stageRoot "installation-manifest.json") -Encoding UTF8
        New-Item -ItemType Directory -Path (Split-Path -Parent $InstallRoot) -Force | Out-Null
        Move-Item -LiteralPath $stageRoot -Destination $InstallRoot
        foreach ($child in @("config", "secrets", "logs", "rollback")) { New-Item -ItemType Directory -Path (Join-Path $RuntimeRoot $child) -Force | Out-Null }
        Set-XbWorkerAcl -Path $InstallRoot -Kind Install
        Set-XbWorkerAcl -Path (Join-Path $RuntimeRoot "config") -Kind Config
        Set-XbWorkerAcl -Path (Join-Path $RuntimeRoot "secrets") -Kind Secrets
        Set-XbWorkerAcl -Path (Join-Path $RuntimeRoot "logs") -Kind Logs
        Set-XbWorkerAcl -Path (Join-Path $RuntimeRoot "rollback") -Kind Rollback

        $launcher = Join-Path $InstallRoot "launch_ac2_member_gateway_worker.ps1"
        Register-XbWorkerScheduledTask -LauncherPath $launcher
        Assert-XbRuntimeCustody
    }
    catch {
        $originalError = $_
        try { Invoke-XbWorkerInstallRollback -Preimage $preimage -StageRoot $stageRoot -RegistrationAttempted ([bool]$script:XbTaskRegistrationAttempted) }
        catch { if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction Stop }; throw "install_rollback_failed: $($_.Exception.Message)" }
        throw $originalError
    }
    return
}

if ($Operation -eq "Upgrade") {
    if ($null -eq $TaskCredential -or $TaskCredential.UserName -cne $WorkerAccount) { throw "task_credential_required" }
    (Invoke-XbWorkerUpgrade -SourceRoot $sourceRoot -ReviewedIdentity $reviewedIdentity) | ConvertTo-Json -Depth 6 -Compress
    return
}

$null = Assert-XbUninstallOwnership
Remove-XbWorkerOwnedState -TaskMayExist
}

if (-not $LibraryOnly) { Invoke-XbWorkerInstaller }
