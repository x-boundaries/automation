[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet("Install", "Uninstall", "ValidateOnly")]
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

function Assert-XbWorkerTaskContract {
    param([Parameter(Mandatory)]$Task, $ExpectedIdentity)
    if ($Task.State -ne "Disabled") { throw "task_not_disabled" }
    if (@($Task.Triggers).Count -ne 0) { throw "task_triggers_present" }
    if (@($Task.Actions).Count -ne 1) { throw "task_action_count_invalid" }
    $arguments = [string]$Task.Actions[0].Arguments
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
    $action = $Task.Actions[0]
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

function Register-XbWorkerScheduledTask {
    param([Parameter(Mandatory)][string]$LauncherPath)
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ('-NoLogo -NoProfile -NonInteractive -File "{0}" -Mode DisabledProof' -f $LauncherPath)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) -RestartCount 0 -StartWhenAvailable:$false -Disable
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $WorkerAccount -LogonType Password -RunLevel Limited
    $task = New-ScheduledTask -Action $action -Settings $settings -Principal $taskPrincipal
    $plainPassword = $TaskCredential.GetNetworkCredential().Password
    $script:XbTaskMayExist = $true
    try { Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -InputObject $task -User $WorkerAccount -Password $plainPassword -Force | Out-Null; $script:XbTaskCreated = $true }
    finally { $plainPassword = $null }
    Assert-XbWorkerTaskContract -Task (Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop) -ExpectedIdentity (Get-XbWorkerTaskIdentity -LauncherPath $LauncherPath -WorkerAccount $WorkerAccount)
}

function Invoke-XbWorkerInstaller {
Assert-XbWorkerInstallLayout
$sourceRoot = $PSScriptRoot
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

    $stageRoot = Join-Path ([IO.Path]::GetTempPath()) ("xb-member-worker-" + [Guid]::NewGuid().ToString("N"))
    $script:XbTaskCreated = $false
    $script:XbTaskMayExist = $false
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
        try { Remove-XbWorkerOwnedState -TaskMayExist:([bool]$script:XbTaskMayExist) }
        catch { if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction Stop }; throw "install_rollback_failed: $($_.Exception.Message)" }
        if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction Stop }
        throw $originalError
    }
    return
}

$null = Assert-XbUninstallOwnership
Remove-XbWorkerOwnedState -TaskMayExist
}

if (-not $LibraryOnly) { Invoke-XbWorkerInstaller }
