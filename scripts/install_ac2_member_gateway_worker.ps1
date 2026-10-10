[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet("Install", "Uninstall", "ValidateOnly", "Upgrade", "Rollback")]
    [string]$Operation = "ValidateOnly",
    [string]$WorkerAccount,
    [Management.Automation.PSCredential]$TaskCredential,
    [string]$ReviewedPackageManifestPath,
    [switch]$LibraryOnly,
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
    "test_ac2_member_gateway_autocount_dependencies.ps1"
)
$script:XbWorkerUpgradeLockStream = $null

function Assert-XbWorkerInstallLayout {
    if ([IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') -cne "C:\Program Files\X-Boundaries\MemberGatewayWorker") { throw "install_root_contract_mismatch" }
    if ([IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\') -cne "C:\ProgramData\X-Boundaries\MemberGatewayWorker") { throw "runtime_root_contract_mismatch" }
}

function Invoke-XbIcacls {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string[]]$Arguments)
    & icacls.exe $Path @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "acl_application_failed" }
}

function Set-XbWorkerAcl {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][ValidateSet("Install", "Config", "Secrets", "Logs", "Rollback")][string]$Kind)
    Invoke-XbIcacls -Path $Path -Arguments @("/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")
    if ($Kind -eq "Install") { Invoke-XbIcacls -Path $Path -Arguments @("/grant:r", "${WorkerAccount}:(OI)(CI)RX") }
    if ($Kind -eq "Config") { Invoke-XbIcacls -Path $Path -Arguments @("/grant:r", "${WorkerAccount}:(OI)(CI)R") }
    if ($Kind -eq "Secrets") { Invoke-XbIcacls -Path $Path -Arguments @("/grant:r", "${WorkerAccount}:(OI)(CI)R") }
    if ($Kind -eq "Logs") { Invoke-XbIcacls -Path $Path -Arguments @("/grant:r", "${WorkerAccount}:(OI)(CI)M") }
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
        executable = "powershell.exe"
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
    [ordered]@{
        schema_version = "xb.member.gateway.worker.installation.v1"
        reviewed_source = $ReviewedIdentity.source
        install_root = ([IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') + '\')
        runtime_root = ([IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\') + '\')
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
    $manifestPath = Join-Path $InstallRoot "installation-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "installation_manifest_missing" }
    try { $installed = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json } catch { throw "installation_manifest_invalid" }
    Assert-XbExactProperties $installed @("schema_version", "reviewed_source", "install_root", "runtime_root", "package_files", "task", "rollback_owned_roots") "installation_manifest_invalid"
    Assert-XbExactProperties $installed.reviewed_source @("commit", "tree") "installation_manifest_invalid"
    Assert-XbExactProperties $installed.task @("path", "name", "enabled", "trigger_count", "action_mode", "production_switches", "multiple_instances", "execution_time_limit", "restart_count", "start_when_available", "executable", "launcher_path", "arguments", "working_directory", "principal_user_id", "principal_logon_type", "principal_run_level") "installation_manifest_invalid"
    if ([string]$installed.schema_version -cne "xb.member.gateway.worker.installation.v1") { throw "installation_manifest_invalid" }
    if ([string]$installed.install_root -cne "C:\Program Files\X-Boundaries\MemberGatewayWorker\" -or [string]$installed.runtime_root -cne "C:\ProgramData\X-Boundaries\MemberGatewayWorker\") { throw "installation_manifest_path_invalid" }
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
    if ([string]$installed.task.executable -cne "powershell.exe" -or [string]$installed.task.launcher_path -cne $expectedLauncher -or [string]$installed.task.arguments -cne (Get-XbWorkerTaskArguments -LauncherPath $expectedLauncher) -or [string]$installed.task.working_directory -cne "" -or [string]::IsNullOrWhiteSpace([string]$installed.task.principal_user_id) -or [string]$installed.task.principal_logon_type -cne "Password" -or [string]$installed.task.principal_run_level -cne "Limited") { throw "installation_manifest_task_invalid" }
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

function Assert-XbWorkerAdministrator {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw "windows_required" }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "administrator_required" }
}

function Get-XbWorkerUpgradePaths {
    $installFull = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
    $runtimeFull = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $parent = Split-Path -Parent $installFull
    $stem = Split-Path -Leaf $installFull
    return [ordered]@{
        install = $installFull
        runtime = $runtimeFull
        parent = $parent
        journal = Join-Path $parent ($stem + ".upgrade-journal.json")
        journal_temp = Join-Path $parent ($stem + ".upgrade-journal.json.tmp")
        lock = Join-Path $parent ($stem + ".upgrade.lock")
        candidate_prefix = ($stem + ".upgrade-candidate-")
        previous_prefix = ($stem + ".upgrade-previous-")
    }
}

function Use-XbWorkerUpgradeNativeFileInfo {
    if ($null -ne ("XbWorkerUpgradeNativeFileInfo" -as [type])) { return }
    $definition = @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class XbWorkerUpgradeNativeFileInfo {
    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out ByHandleFileInformation info);
    public static uint LinkCount(SafeFileHandle handle) {
        ByHandleFileInformation info;
        if (handle == null || handle.IsInvalid || !GetFileInformationByHandle(handle, out info)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return info.NumberOfLinks;
    }
    public static uint LinkCount(string name) {
        using (SafeFileHandle handle = CreateFile(name, 0x80, 0x1 | 0x2 | 0x4, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero)) {
            if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
            return LinkCount(handle);
        }
    }
}
"@
    try { Add-Type -TypeDefinition $definition -ErrorAction Stop | Out-Null } catch { throw "upgrade_file_identity_unavailable" }
}

function Assert-XbWorkerUpgradeFileNoHardLinks {
    param([Parameter(Mandatory)][string]$Path)
    Assert-XbWorkerUpgradePath -Path $Path
    try {
        Use-XbWorkerUpgradeNativeFileInfo
        $linkCount = [XbWorkerUpgradeNativeFileInfo]::LinkCount([IO.Path]::GetFullPath($Path))
    } catch { throw "upgrade_file_identity_unproven" }
    if ($linkCount -ne 1) { throw "upgrade_file_alias_ambiguous" }
}

function Get-XbWorkerUpgradeOrphanPaths {
    $paths = Get-XbWorkerUpgradePaths
    try {
        return @(
            Get-ChildItem -LiteralPath $paths.parent -Force -ErrorAction Stop |
                Where-Object { $_.Name.StartsWith($paths.candidate_prefix, [StringComparison]::OrdinalIgnoreCase) -or $_.Name.StartsWith($paths.previous_prefix, [StringComparison]::OrdinalIgnoreCase) } |
                ForEach-Object FullName
        )
    } catch { throw "upgrade_recovery_state_unproven" }
}

function Assert-XbWorkerUpgradePath {
    param([Parameter(Mandatory)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($full)
    if ([string]::IsNullOrWhiteSpace($root) -or -not [IO.Path]::IsPathRooted($full)) { throw "upgrade_path_unproven" }
    $current = $root
    foreach ($segment in @($full.Substring($root.Length).Split('\') | Where-Object { $_ -ne "" })) {
        $current = Join-Path $current $segment
        if (-not (Test-Path -LiteralPath $current)) { continue }
        try { $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop } catch { throw "upgrade_path_unproven" }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "upgrade_path_reparse_point" }
    }
}

function Get-XbWorkerUpgradeAclSddl {
    param([Parameter(Mandatory)][string]$Path)
    try {
        $acl = Get-Acl -LiteralPath $Path -Audit -ErrorAction Stop
        if (-not $acl.AreAccessRulesCanonical) { throw "acl_order_unproven" }
        return $acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::All)
    } catch { throw "upgrade_acl_unverifiable" }
}

function Convert-XbWorkerUpgradeAclSddl {
    param([Parameter(Mandatory)][string]$Sddl)
    try {
        $descriptor = [Security.AccessControl.RawSecurityDescriptor]::new($Sddl)
        $autoInherited = [int]([Security.AccessControl.ControlFlags]::DiscretionaryAclAutoInherited -bor [Security.AccessControl.ControlFlags]::SystemAclAutoInherited)
        $controlFlags = [int]$descriptor.ControlFlags -band (-bnot $autoInherited)
        $sections = @()
        foreach ($pair in @(
            [pscustomobject]@{ name = "D"; acl = $descriptor.DiscretionaryAcl },
            [pscustomobject]@{ name = "S"; acl = $descriptor.SystemAcl }
        )) {
            if ($null -eq $pair.acl) { $sections += ($pair.name + "=NULL"); continue }
            $aces = New-Object 'System.Collections.Generic.List[string]'
            for ($index = 0; $index -lt $pair.acl.Count; $index++) {
                $ace = $pair.acl[$index]
                $bytes = New-Object byte[] $ace.BinaryLength
                $ace.GetBinaryForm($bytes, 0)
                $aces.Add([Convert]::ToBase64String($bytes))
            }
            $orderedAces = [string[]]$aces.ToArray()
            [Array]::Sort($orderedAces, [StringComparer]::Ordinal)
            $sections += ($pair.name + "=" + ($orderedAces -join ","))
        }
        return @(
            [string]$descriptor.Owner,
            [string]$descriptor.Group,
            [string]$controlFlags,
            [string]::Join("|", $sections)
        ) -join "|"
    } catch { throw "upgrade_acl_descriptor_invalid" }
}

function Set-XbWorkerUpgradeAclSddl {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Sddl)
    try {
        $acl = Get-Acl -LiteralPath $Path -Audit -ErrorAction Stop
        $acl.SetSecurityDescriptorSddlForm($Sddl, [Security.AccessControl.AccessControlSections]::All)
        Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
    } catch { throw "upgrade_acl_preservation_failed" }
    $actualSddl = Get-XbWorkerUpgradeAclSddl -Path $Path
    if ((Convert-XbWorkerUpgradeAclSddl $actualSddl) -cne (Convert-XbWorkerUpgradeAclSddl $Sddl)) { throw "upgrade_acl_preservation_failed" }
}

function Set-XbWorkerUpgradeJournalAcl {
    param([Parameter(Mandatory)][string]$Path)
    try {
        $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))) { $acl.RemoveAccessRuleAll($rule) }
        $ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        if ($null -eq $ownerSid) { throw "journal_owner_unproven" }
        $acl.SetOwner($ownerSid)
        $identities = @(
            $ownerSid,
            [Security.Principal.SecurityIdentifier]::new("S-1-5-18"),
            [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
        ) | Select-Object -Unique
        foreach ($sid in $identities) {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, [Security.AccessControl.FileSystemRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow)
            $null = $acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
        Assert-XbWorkerUpgradeJournalAcl -Path $Path -ExpectedOwnerSid ([string]$ownerSid)
    } catch { throw "upgrade_journal_acl_invalid" }
}

function Assert-XbWorkerUpgradeJournalAcl {
    param([Parameter(Mandatory)][string]$Path, [string]$ExpectedOwnerSid)
    try {
        $observed = Get-Acl -LiteralPath $Path -ErrorAction Stop
        $rules = @($observed.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
        $ownerSid = [string]$observed.GetOwner([Security.Principal.SecurityIdentifier])
        $allowed = @($ownerSid, "S-1-5-18", "S-1-5-32-544") | Select-Object -Unique
        if (-not [string]::IsNullOrWhiteSpace($ExpectedOwnerSid) -and $ownerSid -cne $ExpectedOwnerSid) { throw "journal_acl_mismatch" }
        if (-not $observed.AreAccessRulesProtected -or $rules.Count -ne $allowed.Count) { throw "journal_acl_mismatch" }
        foreach ($rule in $rules) {
            if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or [Security.AccessControl.FileSystemRights]$rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
                ([string]$rule.IdentityReference -cnotin $allowed)) { throw "journal_acl_mismatch" }
        }
        if (@($rules | ForEach-Object { [string]$_.IdentityReference } | Sort-Object -Unique).Count -ne $allowed.Count) { throw "journal_acl_mismatch" }
    } catch { throw "upgrade_journal_acl_invalid" }
}

function Get-XbWorkerUpgradeAclIdentity {
    param([Parameter(Mandatory)][string]$Root)
    $result = [ordered]@{ root = (Get-XbWorkerUpgradeAclSddl -Path $Root); files = @() }
    foreach ($name in @($packageFiles + "installation-manifest.json")) {
        $path = Join-Path $Root $name
        $result.files += [ordered]@{ name = $name; sddl = (Get-XbWorkerUpgradeAclSddl -Path $path) }
    }
    return $result
}

function Set-XbWorkerUpgradeAclIdentity {
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)]$AclIdentity)
    Set-XbWorkerUpgradeAclSddl -Path $Root -Sddl ([string]$AclIdentity.root)
    foreach ($entry in @($AclIdentity.files)) {
        Set-XbWorkerUpgradeAclSddl -Path (Join-Path $Root ([string]$entry.name)) -Sddl ([string]$entry.sddl)
    }
}

function Assert-XbWorkerInstalledManifest {
    param([Parameter(Mandatory)]$Installed)
    Assert-XbExactProperties $Installed @("schema_version", "reviewed_source", "install_root", "runtime_root", "package_files", "task", "rollback_owned_roots") "installation_manifest_invalid"
    Assert-XbExactProperties $Installed.reviewed_source @("commit", "tree") "installation_manifest_invalid"
    Assert-XbExactProperties $Installed.task @("path", "name", "enabled", "trigger_count", "action_mode", "production_switches", "multiple_instances", "execution_time_limit", "restart_count", "start_when_available", "executable", "launcher_path", "arguments", "working_directory", "principal_user_id", "principal_logon_type", "principal_run_level") "installation_manifest_invalid"
    if ([string]$Installed.schema_version -cne "xb.member.gateway.worker.installation.v1" -or
        [string]$Installed.reviewed_source.commit -cnotmatch '^[0-9a-f]{40}$' -or
        [string]$Installed.reviewed_source.tree -cnotmatch '^[0-9a-f]{40}$') { throw "installation_manifest_invalid" }
    $paths = Get-XbWorkerUpgradePaths
    if ([string]$Installed.install_root -cne ($paths.install + '\') -or [string]$Installed.runtime_root -cne ($paths.runtime + '\')) { throw "installation_manifest_path_invalid" }
    $entries = @($Installed.package_files)
    if ($entries.Count -ne $packageFiles.Count) { throw "installation_manifest_membership_invalid" }
    $seen = @{}
    foreach ($entry in $entries) {
        Assert-XbExactProperties $entry @("name", "sha256") "installation_manifest_membership_invalid"
        $name = [string]$entry.name
        if ($name -cnotin $packageFiles -or $seen.ContainsKey($name) -or [string]$entry.sha256 -cnotmatch '^[0-9a-f]{64}$') { throw "installation_manifest_membership_invalid" }
        $seen[$name] = [string]$entry.sha256
    }
    foreach ($name in $packageFiles) { if (-not $seen.ContainsKey($name)) { throw "installation_manifest_membership_invalid" } }
    if (@(Compare-Object @($Installed.rollback_owned_roots | Sort-Object) @("config", "logs", "rollback", "secrets")).Count -ne 0) { throw "installation_runtime_roots_invalid" }
    $expectedLauncher = [IO.Path]::GetFullPath((Join-Path $InstallRoot "launch_ac2_member_gateway_worker.ps1"))
    $task = $Installed.task
    if ([string]$task.path -cne $taskPath -or [string]$task.name -cne $taskName -or
        $task.enabled -ne $false -or [int]$task.trigger_count -ne 0 -or
        [string]$task.action_mode -cne "DisabledProof" -or @($task.production_switches).Count -ne 0 -or
        [string]$task.multiple_instances -cne "IgnoreNew" -or [string]$task.execution_time_limit -cne "PT10M" -or
        [int]$task.restart_count -ne 0 -or $task.start_when_available -ne $false -or
        [string]$task.executable -cne "powershell.exe" -or [string]$task.launcher_path -cne $expectedLauncher -or
        [string]$task.arguments -cne (Get-XbWorkerTaskArguments -LauncherPath $expectedLauncher) -or
        [string]$task.working_directory -cne "" -or [string]::IsNullOrWhiteSpace([string]$task.principal_user_id) -or
        [string]$task.principal_logon_type -cne "Password" -or [string]$task.principal_run_level -cne "Limited") { throw "installation_manifest_task_invalid" }
}

function Get-XbWorkerPackageIdentity {
    param([Parameter(Mandatory)][string]$Root, [string]$ErrorId = "upgrade_package_identity_invalid")
    if (-not (Test-Path -LiteralPath $Root)) { return $null }
    Assert-XbWorkerUpgradePath -Path $Root
    try { $rootItem = Get-Item -LiteralPath $Root -Force -ErrorAction Stop } catch { throw $ErrorId }
    if (-not $rootItem.PSIsContainer -or ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw $ErrorId }
    try { $children = @(Get-ChildItem -LiteralPath $Root -Force -ErrorAction Stop) } catch { throw $ErrorId }
    $allowed = @($packageFiles + "installation-manifest.json" | Sort-Object)
    $actual = @($children | ForEach-Object Name | Sort-Object)
    if (@(Compare-Object $actual $allowed).Count -ne 0) { throw "upgrade_package_membership_invalid" }
    foreach ($child in $children) { if ($child.PSIsContainer -or ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "upgrade_package_membership_invalid" } }
    $manifestPath = Join-Path $Root "installation-manifest.json"
    Assert-XbWorkerUpgradeFileNoHardLinks -Path $manifestPath
    try { $installed = Get-Content -Raw -LiteralPath $manifestPath -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { throw "installation_manifest_invalid" }
    Assert-XbWorkerInstalledManifest -Installed $installed
    foreach ($name in $packageFiles) {
        $entry = @($installed.package_files | Where-Object { [string]$_.name -ceq $name })
        $path = Join-Path $Root $name
        Assert-XbWorkerUpgradeFileNoHardLinks -Path $path
        if ($entry.Count -ne 1 -or (Get-XbFileSha256 $path) -cne [string]$entry[0].sha256) { throw "upgrade_package_sha_mismatch" }
    }
    $files = foreach ($name in @($packageFiles + "installation-manifest.json")) { $path = Join-Path $Root $name; Assert-XbWorkerUpgradeFileNoHardLinks -Path $path; [ordered]@{ name = $name; sha256 = (Get-XbFileSha256 $path) } }
    return [ordered]@{
        reviewed_source = [ordered]@{ commit = [string]$installed.reviewed_source.commit; tree = [string]$installed.reviewed_source.tree }
        files = @($files)
        acl = (Get-XbWorkerUpgradeAclIdentity -Root $Root)
    }
}

function Assert-XbWorkerUpgradePackageMatches {
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)]$Expected)
    $observed = Get-XbWorkerPackageIdentity -Root $Root
    if ($null -eq $observed) { throw "upgrade_package_identity_mismatch" }
    $observedAcl = [ordered]@{ root = (Convert-XbWorkerUpgradeAclSddl ([string]$observed.acl.root)); files = @() }
    foreach ($entry in @($observed.acl.files)) { $observedAcl.files += [ordered]@{ name = [string]$entry.name; sddl = (Convert-XbWorkerUpgradeAclSddl ([string]$entry.sddl)) } }
    $expectedAcl = [ordered]@{ root = (Convert-XbWorkerUpgradeAclSddl ([string]$Expected.acl.root)); files = @() }
    foreach ($entry in @($Expected.acl.files)) { $expectedAcl.files += [ordered]@{ name = [string]$entry.name; sddl = (Convert-XbWorkerUpgradeAclSddl ([string]$entry.sddl)) } }
    if ((ConvertTo-Json -InputObject $observed.files -Depth 8 -Compress) -cne (ConvertTo-Json -InputObject $Expected.files -Depth 8 -Compress) -or
        [string]$observed.reviewed_source.commit -cne [string]$Expected.reviewed_source.commit -or
        [string]$observed.reviewed_source.tree -cne [string]$Expected.reviewed_source.tree -or
        (ConvertTo-Json -InputObject $observedAcl -Depth 8 -Compress) -cne (ConvertTo-Json -InputObject $expectedAcl -Depth 8 -Compress)) { throw "upgrade_package_identity_mismatch" }
}

function Get-XbWorkerUpgradeJournalPathSet {
    param([Parameter(Mandatory)]$Journal)
    Assert-XbExactProperties $Journal @("schema_version", "transaction_id", "state", "install_root", "runtime_root", "candidate_root", "previous_root", "task_identity", "old_identity", "new_identity") "upgrade_journal_invalid"
    if ([string]$Journal.schema_version -cne "xb.member.gateway.worker.upgrade-transaction.v1" -or [string]$Journal.transaction_id -cnotmatch '^[0-9a-f]{32}$') { throw "upgrade_journal_invalid" }
    if ([string]$Journal.state -cnotin @("preparing", "staged", "old_move_intent", "new_move_intent", "complete", "rollback_stage_cleanup", "rollback_new_intent", "rollback_old_intent", "rolled_back", "aborted")) { throw "upgrade_journal_invalid" }
    $paths = Get-XbWorkerUpgradePaths
    $candidateRoot = Join-Path $paths.parent ($paths.candidate_prefix + [string]$Journal.transaction_id)
    $previousRoot = Join-Path $paths.parent ($paths.previous_prefix + [string]$Journal.transaction_id)
    if ([string]$Journal.install_root -cne $paths.install -or [string]$Journal.runtime_root -cne $paths.runtime -or
        [string]$Journal.candidate_root -cne $candidateRoot -or [string]$Journal.previous_root -cne $previousRoot) { throw "upgrade_journal_path_invalid" }
    foreach ($path in @($paths.parent, $paths.journal, $paths.journal_temp, $paths.lock, $candidateRoot, $previousRoot)) { Assert-XbWorkerUpgradePath -Path $path }
    Assert-XbExactProperties $Journal.task_identity @("executable", "launcher_path", "arguments", "working_directory", "principal_user_id", "principal_logon_type", "principal_run_level") "upgrade_journal_invalid"
    Assert-XbExactProperties $Journal.old_identity @("reviewed_source", "files", "acl") "upgrade_journal_invalid"
    Assert-XbExactProperties $Journal.new_identity @("reviewed_source", "files", "acl") "upgrade_journal_invalid"
    foreach ($identity in @($Journal.old_identity, $Journal.new_identity)) {
        Assert-XbExactProperties $identity.reviewed_source @("commit", "tree") "upgrade_journal_invalid"
        if ([string]$identity.reviewed_source.commit -cnotmatch '^[0-9a-f]{40}$' -or [string]$identity.reviewed_source.tree -cnotmatch '^[0-9a-f]{40}$') { throw "upgrade_journal_invalid" }
        if (@($identity.files).Count -ne ($packageFiles.Count + 1)) { throw "upgrade_journal_invalid" }
        foreach ($file in @($identity.files)) {
            Assert-XbExactProperties $file @("name", "sha256") "upgrade_journal_invalid"
            if ([string]$file.name -cnotin @($packageFiles + "installation-manifest.json") -or [string]$file.sha256 -cnotmatch '^[0-9a-f]{64}$') { throw "upgrade_journal_invalid" }
        }
        if (@($identity.files | ForEach-Object name | Sort-Object | Select-Object -Unique).Count -ne ($packageFiles.Count + 1)) { throw "upgrade_journal_invalid" }
        Assert-XbExactProperties $identity.acl @("root", "files") "upgrade_journal_invalid"
        if ([string]::IsNullOrWhiteSpace([string]$identity.acl.root) -or @($identity.acl.files).Count -ne ($packageFiles.Count + 1)) { throw "upgrade_journal_invalid" }
        foreach ($aclFile in @($identity.acl.files)) {
            Assert-XbExactProperties $aclFile @("name", "sddl") "upgrade_journal_invalid"
            if ([string]$aclFile.name -cnotin @($packageFiles + "installation-manifest.json") -or [string]::IsNullOrWhiteSpace([string]$aclFile.sddl)) { throw "upgrade_journal_invalid" }
        }
    }
    if ((ConvertTo-Json -InputObject $Journal.old_identity.acl -Depth 8 -Compress) -cne (ConvertTo-Json -InputObject $Journal.new_identity.acl -Depth 8 -Compress)) { throw "upgrade_journal_acl_invalid" }
    $expectedLauncher = [IO.Path]::GetFullPath((Join-Path $InstallRoot "launch_ac2_member_gateway_worker.ps1"))
    if ([string]$Journal.task_identity.executable -cne "powershell.exe" -or [string]$Journal.task_identity.launcher_path -cne $expectedLauncher -or
        [string]$Journal.task_identity.arguments -cne (Get-XbWorkerTaskArguments -LauncherPath $expectedLauncher) -or
        [string]$Journal.task_identity.working_directory -cne "" -or [string]::IsNullOrWhiteSpace([string]$Journal.task_identity.principal_user_id) -or
        [string]$Journal.task_identity.principal_logon_type -cne "Password" -or [string]$Journal.task_identity.principal_run_level -cne "Limited") { throw "upgrade_journal_task_invalid" }
    return [ordered]@{ paths = $paths; candidate = [string]$Journal.candidate_root; previous = [string]$Journal.previous_root }
}

function Read-XbWorkerUpgradeJournal {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "upgrade_transaction_missing" }
    Assert-XbWorkerUpgradeFileNoHardLinks -Path $Path
    Assert-XbWorkerUpgradeJournalAcl -Path $Path
    try { $journal = Get-Content -Raw -LiteralPath $Path -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { throw "upgrade_journal_invalid" }
    $null = Get-XbWorkerUpgradeJournalPathSet -Journal $journal
    return $journal
}

function Write-XbWorkerUpgradeJournal {
    param([Parameter(Mandatory)]$Journal)
    try { $normalized = ConvertFrom-Json -InputObject (ConvertTo-Json -InputObject $Journal -Depth 12) -ErrorAction Stop }
    catch { throw "upgrade_journal_invalid" }
    $pathSet = Get-XbWorkerUpgradeJournalPathSet -Journal $normalized
    $paths = $pathSet.paths
    if (Test-Path -LiteralPath $paths.journal_temp) { throw "upgrade_journal_temp_exists" }
    $json = ConvertTo-Json -InputObject $Journal -Depth 12
    $bytes = ([Text.UTF8Encoding]::new($false)).GetBytes($json + "`n")
    $stream = $null
    try {
        $stream = [IO.FileStream]::new($paths.journal_temp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None, 4096, [IO.FileOptions]::WriteThrough)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
        $stream.Dispose(); $stream = $null
        Assert-XbWorkerUpgradeFileNoHardLinks -Path $paths.journal_temp
        Set-XbWorkerUpgradeJournalAcl -Path $paths.journal_temp
        Use-XbWorkerUpgradeNativeMove
        $flags = if (Test-Path -LiteralPath $paths.journal) { 9 } else { 8 }
        if (Test-Path -LiteralPath $paths.journal) { Assert-XbWorkerUpgradeFileNoHardLinks -Path $paths.journal }
        if (-not [XbWorkerUpgradeNativeMove]::Move($paths.journal_temp, $paths.journal, [uint32]$flags)) { throw "move_failed" }
    } catch {
        if ($null -ne $stream) { $stream.Dispose() }
        throw "upgrade_journal_write_failed"
    }
}

function Enter-XbWorkerUpgradeLock {
    $paths = Get-XbWorkerUpgradePaths
    Assert-XbWorkerUpgradePath -Path $paths.lock
    try { $script:XbWorkerUpgradeLockStream = [IO.File]::Open($paths.lock, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw "upgrade_maintenance_owner_present" }
    try {
        Use-XbWorkerUpgradeNativeFileInfo
        $linkCount = [XbWorkerUpgradeNativeFileInfo]::LinkCount($script:XbWorkerUpgradeLockStream.SafeFileHandle)
        if ($linkCount -ne 1) { throw "upgrade_file_alias_ambiguous" }
    } catch {
        $script:XbWorkerUpgradeLockStream.Dispose(); $script:XbWorkerUpgradeLockStream = $null
        if ([string]$_.Exception.Message -ceq "upgrade_file_alias_ambiguous") { throw }
        throw "upgrade_file_identity_unproven"
    }
}

function Exit-XbWorkerUpgradeLock {
    if ($null -ne $script:XbWorkerUpgradeLockStream) { $script:XbWorkerUpgradeLockStream.Dispose(); $script:XbWorkerUpgradeLockStream = $null }
}

function Assert-XbWorkerStopped {
    $launcher = [IO.Path]::GetFullPath((Join-Path $InstallRoot "launch_ac2_member_gateway_worker.ps1"))
    $worker = [IO.Path]::GetFullPath((Join-Path $InstallRoot "ac2_member_gateway_worker.ps1"))
    try { $processes = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction Stop) }
    catch { throw "worker_process_state_unproven" }
    foreach ($process in $processes) {
        $commandLineProperty = if ($null -ne $process) { $process.PSObject.Properties["CommandLine"] } else { $null }
        if ($null -eq $commandLineProperty -or [string]::IsNullOrWhiteSpace([string]$commandLineProperty.Value)) { throw "worker_process_state_unproven" }
        $commandLine = [string]$commandLineProperty.Value
        if ($commandLine.IndexOf($launcher, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or $commandLine.IndexOf($worker, [StringComparison]::OrdinalIgnoreCase) -ge 0) { throw "worker_process_active" }
    }
}

function Move-XbWorkerUpgradeDirectory {
    param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Destination)
    Assert-XbWorkerUpgradePath -Path $Source
    Assert-XbWorkerUpgradePath -Path $Destination
    if (-not (Test-Path -LiteralPath $Source -PathType Container) -or (Test-Path -LiteralPath $Destination)) { throw "upgrade_rename_precondition_failed" }
    if ([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Source)) -cne [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Destination))) { throw "upgrade_volume_mismatch" }
    Use-XbWorkerUpgradeNativeMove
    try {
        if (-not [XbWorkerUpgradeNativeMove]::Move($Source, $Destination, 8)) { throw "move_failed" }
    } catch { throw "upgrade_directory_rename_failed" }
}

function Use-XbWorkerUpgradeNativeMove {
    if ($null -ne ("XbWorkerUpgradeNativeMove" -as [type])) { return }
    $definition = @"
using System;
using System.Runtime.InteropServices;
public static class XbWorkerUpgradeNativeMove {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool MoveFileEx(string existingName, string newName, uint flags);
    public static bool Move(string existingName, string newName, uint flags) {
        return MoveFileEx(existingName, newName, flags);
    }
}
"@
    try { Add-Type -TypeDefinition $definition -ErrorAction Stop | Out-Null } catch { throw "upgrade_native_rename_unavailable" }
}

function Get-XbWorkerPartialPackageState {
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)]$Expected, [switch]$AllowContentMismatch)
    if (-not (Test-Path -LiteralPath $Root)) { return "absent" }
    Assert-XbWorkerUpgradePath -Path $Root
    try { $item = Get-Item -LiteralPath $Root -Force -ErrorAction Stop; $children = @(Get-ChildItem -LiteralPath $Root -Force -ErrorAction Stop) } catch { throw "upgrade_stage_identity_unproven" }
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "upgrade_stage_identity_invalid" }
    $allowed = @($Expected.files | ForEach-Object { [string]$_.name })
    $contentMismatch = $false
    foreach ($child in $children) {
        if ($child.PSIsContainer -or ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $child.Name -cnotin $allowed) { throw "upgrade_stage_membership_invalid" }
        $entry = @($Expected.files | Where-Object { [string]$_.name -ceq [string]$child.Name })
        if ($entry.Count -ne 1) { throw "upgrade_stage_membership_invalid" }
        Assert-XbWorkerUpgradeFileNoHardLinks -Path $child.FullName
        if ((Get-XbFileSha256 $child.FullName) -cne [string]$entry[0].sha256) {
            if (-not $AllowContentMismatch) { throw "upgrade_stage_sha_mismatch" }
            $contentMismatch = $true
        }
    }
    if ($contentMismatch) { return "mismatch" }
    if ($children.Count -eq $allowed.Count) {
        Assert-XbWorkerUpgradePackageMatches -Root $Root -Expected $Expected
        return "exact"
    }
    return "partial"
}

function Assert-XbWorkerUpgradeTask {
    param([Parameter(Mandatory)]$Journal)
    $task = Get-XbWorkerTaskIfPresent
    if ($null -eq $task) { throw "installation_task_ownership_ambiguous" }
    Assert-XbWorkerTaskContract -Task $task -ExpectedIdentity $Journal.task_identity
}

function Assert-XbWorkerUpgradeLayout {
    param([Parameter(Mandatory)]$Journal)
    $paths = Get-XbWorkerUpgradeJournalPathSet -Journal $Journal
    $installState = "absent"
    if (Test-Path -LiteralPath $paths.paths.install) {
        $oldMatches = $false
        $newMatches = $false
        try { Assert-XbWorkerUpgradePackageMatches -Root $paths.paths.install -Expected $Journal.old_identity; $oldMatches = $true } catch { }
        if (-not $oldMatches) { try { Assert-XbWorkerUpgradePackageMatches -Root $paths.paths.install -Expected $Journal.new_identity; $newMatches = $true } catch { } }
        if ($oldMatches) { $installState = "old" } elseif ($newMatches) { $installState = "new" } else { throw "upgrade_install_identity_unknown" }
    }
    $allowIncompleteCandidate = [string]$Journal.state -cin @("preparing", "rollback_stage_cleanup")
    $candidateState = Get-XbWorkerPartialPackageState -Root $paths.candidate -Expected $Journal.new_identity -AllowContentMismatch:$allowIncompleteCandidate
    $previousState = Get-XbWorkerPartialPackageState -Root $paths.previous -Expected $Journal.old_identity
    if ($previousState -eq "exact") { $previousState = "old" }
    return [ordered]@{ install = $installState; candidate = $candidateState; previous = $previousState }
}

function Remove-XbWorkerUpgradeCandidate {
    param([Parameter(Mandatory)][string]$CandidateRoot, [Parameter(Mandatory)]$Expected, [switch]$AllowContentMismatch)
    $state = Get-XbWorkerPartialPackageState -Root $CandidateRoot -Expected $Expected -AllowContentMismatch:$AllowContentMismatch
    if ($state -ne "absent") {
        Remove-Item -LiteralPath $CandidateRoot -Recurse -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $CandidateRoot) { throw "upgrade_stage_cleanup_unproven" }
    }
}

function Invoke-XbWorkerUpgrade {
    param([Parameter(Mandatory)]$ReviewedIdentity)
    $paths = Get-XbWorkerUpgradePaths
    foreach ($path in @($paths.install, $paths.runtime, $paths.parent, $paths.journal, $paths.journal_temp)) { Assert-XbWorkerUpgradePath -Path $path }
    if (-not (Test-Path -LiteralPath $paths.install -PathType Container) -or -not (Test-Path -LiteralPath $paths.runtime -PathType Container)) { throw "installation_preimage_missing" }
    if (Test-Path -LiteralPath $paths.journal) {
        $existingJournal = Read-XbWorkerUpgradeJournal -Path $paths.journal
        $null = Assert-XbWorkerUpgradeLayout -Journal $existingJournal
        Assert-XbWorkerUpgradeTask -Journal $existingJournal
        if (Test-Path -LiteralPath $paths.journal_temp) {
            $tempItem = Get-Item -LiteralPath $paths.journal_temp -Force -ErrorAction Stop
            if ($tempItem.PSIsContainer -or ($tempItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "upgrade_journal_temp_invalid" }
            Assert-XbWorkerUpgradeFileNoHardLinks -Path $paths.journal_temp
            Remove-Item -LiteralPath $paths.journal_temp -Force -ErrorAction Stop
        }
        throw "upgrade_recovery_required_use_rollback"
    }
    if (@(Get-XbWorkerUpgradeOrphanPaths).Count -gt 0) { throw "upgrade_recovery_state_unjournaled" }
    if (Test-Path -LiteralPath $paths.journal_temp) {
        $null = Get-XbWorkerPackageIdentity -Root $paths.install
        $installed = Get-Content -Raw -LiteralPath (Join-Path $paths.install "installation-manifest.json") | ConvertFrom-Json
        Assert-XbWorkerUpgradeTask -Journal ([pscustomobject]@{ task_identity = [ordered]@{ executable = [string]$installed.task.executable; launcher_path = [string]$installed.task.launcher_path; arguments = [string]$installed.task.arguments; working_directory = [string]$installed.task.working_directory; principal_user_id = [string]$installed.task.principal_user_id; principal_logon_type = [string]$installed.task.principal_logon_type; principal_run_level = [string]$installed.task.principal_run_level } })
        $tempItem = Get-Item -LiteralPath $paths.journal_temp -Force -ErrorAction Stop
        if ($tempItem.PSIsContainer -or ($tempItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "upgrade_journal_temp_invalid" }
        Assert-XbWorkerUpgradeFileNoHardLinks -Path $paths.journal_temp
        Remove-Item -LiteralPath $paths.journal_temp -Force -ErrorAction Stop
    }
    Assert-XbWorkerStopped
    $oldObserved = Get-XbWorkerPackageIdentity -Root $paths.install
    $installed = Get-Content -Raw -LiteralPath (Join-Path $paths.install "installation-manifest.json") | ConvertFrom-Json
    $taskIdentity = [ordered]@{
        executable = [string]$installed.task.executable
        launcher_path = [string]$installed.task.launcher_path
        arguments = [string]$installed.task.arguments
        working_directory = [string]$installed.task.working_directory
        principal_user_id = [string]$installed.task.principal_user_id
        principal_logon_type = [string]$installed.task.principal_logon_type
        principal_run_level = [string]$installed.task.principal_run_level
    }
    Assert-XbWorkerUpgradeTask -Journal ([pscustomobject]@{ task_identity = $taskIdentity })
    Assert-XbWorkerStopped
    if ([string]$ReviewedIdentity.source.commit -cnotmatch '^[0-9a-f]{40}$' -or [string]$ReviewedIdentity.source.tree -cnotmatch '^[0-9a-f]{40}$' -or @($ReviewedIdentity.package_files).Count -ne $packageFiles.Count) { throw "reviewed_package_identity_invalid" }
    $reviewedNames = @{}
    foreach ($entry in @($ReviewedIdentity.package_files)) {
        $name = [string]$entry.name
        if ($name -cnotin $packageFiles -or $reviewedNames.ContainsKey($name) -or [string]$entry.sha256 -cnotmatch '^[0-9a-f]{64}$') { throw "reviewed_package_membership_invalid" }
        $reviewedNames[$name] = [string]$entry.sha256
        if ((Get-XbFileSha256 (Join-Path $PSScriptRoot $name)) -cne [string]$entry.sha256) { throw "reviewed_package_source_bytes_mismatch" }
    }
    $candidateManifest = New-XbWorkerInstallationManifest -PackageRoot $PSScriptRoot -ReviewedIdentity $ReviewedIdentity -WorkerAccount ([string]$taskIdentity.principal_user_id)
    $candidateManifestJson = (ConvertTo-Json -InputObject $candidateManifest -Depth 12) + "`n"
    $candidateManifestBytes = ([Text.UTF8Encoding]::new($false)).GetBytes($candidateManifestJson)
    $newFiles = foreach ($name in $packageFiles) { $entry = @($ReviewedIdentity.package_files | Where-Object { [string]$_.name -ceq $name }); if ($entry.Count -ne 1) { throw "reviewed_package_membership_invalid" }; [ordered]@{ name = $name; sha256 = [string]$entry[0].sha256 } }
    $newFiles += [ordered]@{ name = "installation-manifest.json"; sha256 = ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($candidateManifestBytes))).Replace("-", "").ToLowerInvariant() }
    $newIdentity = [ordered]@{ reviewed_source = [ordered]@{ commit = [string]$ReviewedIdentity.source.commit; tree = [string]$ReviewedIdentity.source.tree }; files = @($newFiles); acl = $oldObserved.acl }
    if ((ConvertTo-Json -InputObject $oldObserved.files -Depth 8 -Compress) -ceq (ConvertTo-Json -InputObject $newIdentity.files -Depth 8 -Compress)) { throw "upgrade_package_unchanged" }
    $transactionId = [Guid]::NewGuid().ToString("N")
    $candidateRoot = Join-Path $paths.parent ($paths.candidate_prefix + $transactionId)
    $previousRoot = Join-Path $paths.parent ($paths.previous_prefix + $transactionId)
    if ((Test-Path -LiteralPath $candidateRoot) -or (Test-Path -LiteralPath $previousRoot)) { throw "upgrade_recovery_state_exists" }
    $journal = [ordered]@{
        schema_version = "xb.member.gateway.worker.upgrade-transaction.v1"
        transaction_id = $transactionId
        state = "preparing"
        install_root = $paths.install
        runtime_root = $paths.runtime
        candidate_root = $candidateRoot
        previous_root = $previousRoot
        task_identity = $taskIdentity
        old_identity = $oldObserved
        new_identity = $newIdentity
    }
    Write-XbWorkerUpgradeJournal -Journal $journal
    New-Item -ItemType Directory -Path $journal.candidate_root -ErrorAction Stop | Out-Null
    foreach ($name in $packageFiles) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $journal.candidate_root $name) -ErrorAction Stop }
    [IO.File]::WriteAllBytes((Join-Path $journal.candidate_root "installation-manifest.json"), $candidateManifestBytes)
    Set-XbWorkerUpgradeAclIdentity -Root $journal.candidate_root -AclIdentity $oldObserved.acl
    Assert-XbWorkerUpgradePackageMatches -Root $journal.candidate_root -Expected $newIdentity
    $journal.state = "staged"
    Write-XbWorkerUpgradeJournal -Journal $journal

    Assert-XbWorkerStopped
    Assert-XbWorkerUpgradePackageMatches -Root $paths.install -Expected $oldObserved
    Assert-XbWorkerUpgradeTask -Journal $journal
    $journal.state = "old_move_intent"
    Write-XbWorkerUpgradeJournal -Journal $journal
    Move-XbWorkerUpgradeDirectory -Source $paths.install -Destination $journal.previous_root
    $journal.state = "new_move_intent"
    Write-XbWorkerUpgradeJournal -Journal $journal
    Move-XbWorkerUpgradeDirectory -Source $journal.candidate_root -Destination $paths.install
    Assert-XbWorkerUpgradePackageMatches -Root $paths.install -Expected $newIdentity
    $journal.state = "complete"
    Write-XbWorkerUpgradeJournal -Journal $journal
}

function Invoke-XbWorkerRollback {
    $paths = Get-XbWorkerUpgradePaths
    foreach ($path in @($paths.install, $paths.runtime, $paths.parent, $paths.journal, $paths.journal_temp)) { Assert-XbWorkerUpgradePath -Path $path }
    if (-not (Test-Path -LiteralPath $paths.journal)) {
        if (@(Get-XbWorkerUpgradeOrphanPaths).Count -gt 0) { throw "upgrade_recovery_state_unjournaled" }
        if (Test-Path -LiteralPath $paths.journal_temp) {
            $null = Get-XbWorkerPackageIdentity -Root $paths.install
            $tempItem = Get-Item -LiteralPath $paths.journal_temp -Force -ErrorAction Stop
            if ($tempItem.PSIsContainer -or ($tempItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "upgrade_journal_temp_invalid" }
            Assert-XbWorkerUpgradeFileNoHardLinks -Path $paths.journal_temp
            Remove-Item -LiteralPath $paths.journal_temp -Force -ErrorAction Stop
        }
        throw "upgrade_transaction_missing"
    }
    $journal = Read-XbWorkerUpgradeJournal -Path $paths.journal
    $null = Get-XbWorkerUpgradeJournalPathSet -Journal $journal
    Assert-XbWorkerStopped
    Assert-XbWorkerUpgradeTask -Journal $journal
    $layout = Assert-XbWorkerUpgradeLayout -Journal $journal
    if (Test-Path -LiteralPath $paths.journal_temp) {
        $tempItem = Get-Item -LiteralPath $paths.journal_temp -Force -ErrorAction Stop
        if ($tempItem.PSIsContainer -or ($tempItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "upgrade_journal_temp_invalid" }
        Assert-XbWorkerUpgradeFileNoHardLinks -Path $paths.journal_temp
        Remove-Item -LiteralPath $paths.journal_temp -Force -ErrorAction Stop
    }
    switch ([string]$journal.state) {
        { $_ -in @("preparing", "staged") } {
            if ($layout.install -ne "old" -or $layout.previous -ne "absent" -or ($journal.state -eq "staged" -and $layout.candidate -ne "exact")) { throw "upgrade_transaction_layout_unknown" }
            $journal.state = "rollback_stage_cleanup"
            Write-XbWorkerUpgradeJournal -Journal $journal
            Remove-XbWorkerUpgradeCandidate -CandidateRoot $journal.candidate_root -Expected $journal.new_identity -AllowContentMismatch
            $journal.state = "aborted"
            Write-XbWorkerUpgradeJournal -Journal $journal
            return
        }
        "rollback_stage_cleanup" {
            if ($layout.install -ne "old" -or $layout.previous -ne "absent") { throw "upgrade_transaction_layout_unknown" }
            Remove-XbWorkerUpgradeCandidate -CandidateRoot $journal.candidate_root -Expected $journal.new_identity -AllowContentMismatch
            $journal.state = "aborted"
            Write-XbWorkerUpgradeJournal -Journal $journal
            return
        }
        "aborted" {
            if ($layout.install -ne "old" -or $layout.candidate -ne "absent" -or $layout.previous -ne "absent") { throw "upgrade_transaction_layout_unknown" }
            return
        }
        "rolled_back" {
            if ($layout.install -ne "old" -or $layout.candidate -ne "exact" -or $layout.previous -ne "absent") { throw "upgrade_transaction_layout_unknown" }
            return
        }
        "old_move_intent" {
            if ($layout.install -eq "old" -and $layout.previous -eq "absent" -and $layout.candidate -eq "exact") {
                $journal.state = "rollback_stage_cleanup"
                Write-XbWorkerUpgradeJournal -Journal $journal
                Remove-XbWorkerUpgradeCandidate -CandidateRoot $journal.candidate_root -Expected $journal.new_identity
                $journal.state = "aborted"
                Write-XbWorkerUpgradeJournal -Journal $journal
                return
            }
            if ($layout.install -ne "absent" -or $layout.previous -ne "old" -or $layout.candidate -ne "exact") { throw "upgrade_transaction_layout_unknown" }
            $journal.state = "rollback_old_intent"
            Write-XbWorkerUpgradeJournal -Journal $journal
            Move-XbWorkerUpgradeDirectory -Source $journal.previous_root -Destination $paths.install
            Assert-XbWorkerUpgradePackageMatches -Root $paths.install -Expected $journal.old_identity
            $journal.state = "rolled_back"
            Write-XbWorkerUpgradeJournal -Journal $journal
            return
        }
        "new_move_intent" {
            if ($layout.install -eq "absent" -and $layout.previous -eq "old" -and $layout.candidate -eq "exact") {
                $journal.state = "rollback_old_intent"
                Write-XbWorkerUpgradeJournal -Journal $journal
                Move-XbWorkerUpgradeDirectory -Source $journal.previous_root -Destination $paths.install
                Assert-XbWorkerUpgradePackageMatches -Root $paths.install -Expected $journal.old_identity
                $journal.state = "rolled_back"
                Write-XbWorkerUpgradeJournal -Journal $journal
                return
            }
            if ($layout.install -ne "new" -or $layout.previous -ne "old" -or $layout.candidate -ne "absent") { throw "upgrade_transaction_layout_unknown" }
        }
        { $_ -in @("complete", "rollback_new_intent") } {
            if ($layout.install -eq "absent" -and $layout.previous -eq "old" -and $layout.candidate -eq "exact" -and $journal.state -eq "rollback_new_intent") {
                $journal.state = "rollback_old_intent"
                Write-XbWorkerUpgradeJournal -Journal $journal
                Move-XbWorkerUpgradeDirectory -Source $journal.previous_root -Destination $paths.install
                Assert-XbWorkerUpgradePackageMatches -Root $paths.install -Expected $journal.old_identity
                $journal.state = "rolled_back"
                Write-XbWorkerUpgradeJournal -Journal $journal
                return
            }
            if ($layout.install -ne "new" -or $layout.previous -ne "old" -or $layout.candidate -ne "absent") { throw "upgrade_transaction_layout_unknown" }
        }
        "rollback_old_intent" {
            if ($layout.install -eq "old" -and $layout.previous -eq "absent" -and $layout.candidate -eq "exact") {
                $journal.state = "rolled_back"
                Write-XbWorkerUpgradeJournal -Journal $journal
                return
            }
            if ($layout.install -ne "absent" -or $layout.previous -ne "old" -or $layout.candidate -ne "exact") { throw "upgrade_transaction_layout_unknown" }
            Move-XbWorkerUpgradeDirectory -Source $journal.previous_root -Destination $paths.install
            Assert-XbWorkerUpgradePackageMatches -Root $paths.install -Expected $journal.old_identity
            $journal.state = "rolled_back"
            Write-XbWorkerUpgradeJournal -Journal $journal
            return
        }
        default { throw "upgrade_journal_invalid" }
    }
    $journal.state = "rollback_new_intent"
    Write-XbWorkerUpgradeJournal -Journal $journal
    Move-XbWorkerUpgradeDirectory -Source $paths.install -Destination $journal.candidate_root
    $journal.state = "rollback_old_intent"
    Write-XbWorkerUpgradeJournal -Journal $journal
    Move-XbWorkerUpgradeDirectory -Source $journal.previous_root -Destination $paths.install
    Assert-XbWorkerUpgradePackageMatches -Root $paths.install -Expected $journal.old_identity
    Assert-XbWorkerUpgradePackageMatches -Root $journal.candidate_root -Expected $journal.new_identity
    $journal.state = "rolled_back"
    Write-XbWorkerUpgradeJournal -Journal $journal
}

function Register-XbWorkerScheduledTask {
    param([Parameter(Mandatory)][string]$LauncherPath)
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ('-NoLogo -NoProfile -NonInteractive -File "{0}" -Mode DisabledProof' -f $LauncherPath)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) -RestartCount 0 -StartWhenAvailable:$false -Disable
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $WorkerAccount -LogonType Password -RunLevel Limited
    $task = New-ScheduledTask -Action $action -Settings $settings -Principal $taskPrincipal
    $plainPassword = $TaskCredential.GetNetworkCredential().Password
    $script:XbTaskRegistrationAttempted = $true
    try { Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -InputObject $task -User $WorkerAccount -Password $plainPassword -Force | Out-Null; $script:XbTaskCreated = $true }
    finally { $plainPassword = $null }
    Assert-XbWorkerTaskContract -Task (Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop) -ExpectedIdentity (Get-XbWorkerTaskIdentity -LauncherPath $LauncherPath -WorkerAccount $WorkerAccount)
}

function Invoke-XbWorkerInstaller {
Assert-XbWorkerInstallLayout
$sourceRoot = $PSScriptRoot
if ($Operation -ne "Rollback") {
    if ([string]::IsNullOrWhiteSpace($ReviewedPackageManifestPath)) { throw "reviewed_package_identity_required" }
    if ($Operation -eq "Upgrade") {
        if (-not (Test-Path -LiteralPath $ReviewedPackageManifestPath -PathType Leaf)) { throw "reviewed_package_identity_missing" }
        Assert-XbWorkerUpgradeFileNoHardLinks -Path $ReviewedPackageManifestPath
        foreach ($name in $packageFiles) {
            $sourcePath = Join-Path $sourceRoot $name
            if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) { throw "reviewed_package_source_bytes_mismatch" }
            Assert-XbWorkerUpgradeFileNoHardLinks -Path $sourcePath
        }
    }
    $reviewedIdentity = Read-XbReviewedPackageIdentity -Path $ReviewedPackageManifestPath -PackageRoot $sourceRoot
}
if ($Operation -eq "ValidateOnly") { (New-XbWorkerInstallationManifest -PackageRoot $sourceRoot -ReviewedIdentity $reviewedIdentity -WorkerAccount "<validate-only>") | ConvertTo-Json -Depth 12; return }

Assert-XbWorkerAdministrator
if ($Operation -eq "Rollback") {
    Enter-XbWorkerUpgradeLock
    try { Invoke-XbWorkerRollback } finally { Exit-XbWorkerUpgradeLock }
    return
}
if ($Operation -eq "Upgrade") {
    Enter-XbWorkerUpgradeLock
    try { Invoke-XbWorkerUpgrade -ReviewedIdentity $reviewedIdentity } finally { Exit-XbWorkerUpgradeLock }
    return
}
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
    }
    catch {
        $originalError = $_
        try { Invoke-XbWorkerInstallRollback -Preimage $preimage -StageRoot $stageRoot -RegistrationAttempted ([bool]$script:XbTaskRegistrationAttempted) }
        catch { if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction Stop }; throw "install_rollback_failed: $($_.Exception.Message)" }
        throw $originalError
    }
    return
}

$null = Assert-XbUninstallOwnership
Remove-XbWorkerOwnedState -TaskMayExist
}

if (-not $LibraryOnly) { Invoke-XbWorkerInstaller }
