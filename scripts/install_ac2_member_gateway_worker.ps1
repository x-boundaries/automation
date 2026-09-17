[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet("Install", "Uninstall", "ValidateOnly")]
    [string]$Operation = "ValidateOnly",
    [string]$WorkerAccount,
    [Management.Automation.PSCredential]$TaskCredential,
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

function New-XbWorkerInstallationManifest {
    param([Parameter(Mandatory)][string]$PackageRoot)
    $files = foreach ($name in $packageFiles) {
        $path = Join-Path $PackageRoot $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "package_file_missing" }
        [ordered]@{ name = $name; sha256 = Get-XbFileSha256 -Path $path }
    }
    [ordered]@{
        schema_version = "xb.member.gateway.worker.installation.v1"
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
        }
        rollback_owned_roots = @("config", "secrets", "logs", "rollback")
    }
}

function Assert-XbWorkerTaskContract {
    param([Parameter(Mandatory)]$Task)
    if ($Task.State -ne "Disabled") { throw "task_not_disabled" }
    if (@($Task.Triggers).Count -ne 0) { throw "task_triggers_present" }
    if (@($Task.Actions).Count -ne 1) { throw "task_action_count_invalid" }
    $arguments = [string]$Task.Actions[0].Arguments
    if ($arguments -notmatch '(?:^|\s)-Mode\s+DisabledProof(?:\s|$)') { throw "task_action_mode_invalid" }
    if ($arguments -match 'EnableProduction(?:Worker|Adapter)') { throw "task_production_switch_present" }
    if ([string]$Task.Settings.MultipleInstances -ne "IgnoreNew") { throw "task_multiple_instances_invalid" }
    if ([Xml.XmlConvert]::ToString($Task.Settings.ExecutionTimeLimit) -ne "PT10M") { throw "task_execution_limit_invalid" }
    if ([int]$Task.Settings.RestartCount -ne 0 -or [bool]$Task.Settings.StartWhenAvailable) { throw "task_retry_contract_invalid" }
}

Assert-XbWorkerInstallLayout
$sourceRoot = $PSScriptRoot
$manifest = New-XbWorkerInstallationManifest -PackageRoot $sourceRoot
if ($Operation -eq "ValidateOnly") {
    $manifest | ConvertTo-Json -Depth 12
    exit 0
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw "windows_required" }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "administrator_required" }
if ([string]::IsNullOrWhiteSpace($WorkerAccount)) { throw "worker_account_required" }

if ($Operation -eq "Install") {
    if ($null -eq $TaskCredential -or $TaskCredential.UserName -cne $WorkerAccount) { throw "task_credential_required" }
    if (Test-Path -LiteralPath $InstallRoot) { throw "install_preimage_exists" }
    if (Test-Path -LiteralPath $RuntimeRoot) { throw "runtime_preimage_exists" }
    if ($null -ne (Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue)) { throw "task_preimage_exists" }

    $stageRoot = Join-Path ([IO.Path]::GetTempPath()) ("xb-member-worker-" + [Guid]::NewGuid().ToString("N"))
    $taskCreated = $false
    try {
        New-Item -ItemType Directory -Path $stageRoot | Out-Null
        foreach ($name in $packageFiles) { Copy-Item -LiteralPath (Join-Path $sourceRoot $name) -Destination (Join-Path $stageRoot $name) }
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
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ('-NoLogo -NoProfile -NonInteractive -File "{0}" -Mode DisabledProof' -f $launcher)
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) -RestartCount 0 -StartWhenAvailable:$false
        $taskPrincipal = New-ScheduledTaskPrincipal -UserId $WorkerAccount -LogonType Password -RunLevel Limited
        $task = New-ScheduledTask -Action $action -Settings $settings -Principal $taskPrincipal
        $plainPassword = $TaskCredential.GetNetworkCredential().Password
        try { Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -InputObject $task -User $WorkerAccount -Password $plainPassword -Force | Out-Null }
        finally { $plainPassword = $null }
        $taskCreated = $true
        Disable-ScheduledTask -TaskPath $taskPath -TaskName $taskName | Out-Null
        Assert-XbWorkerTaskContract -Task (Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName)
    }
    catch {
        if ($taskCreated) { Unregister-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $InstallRoot) { Remove-Item -LiteralPath $InstallRoot -Recurse -Force }
        if (Test-Path -LiteralPath $RuntimeRoot) { Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force }
        if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force }
        throw
    }
    exit 0
}

$installedManifestPath = Join-Path $InstallRoot "installation-manifest.json"
if (-not (Test-Path -LiteralPath $installedManifestPath -PathType Leaf)) { throw "installation_manifest_missing" }
$installedManifest = Get-Content -Raw -LiteralPath $installedManifestPath | ConvertFrom-Json
if ([string]$installedManifest.schema_version -cne "xb.member.gateway.worker.installation.v1") { throw "installation_manifest_invalid" }
foreach ($entry in @($installedManifest.package_files)) {
    if ([string]$entry.name -notin $packageFiles) { throw "installation_manifest_scope_invalid" }
}
Unregister-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $InstallRoot -Recurse -Force
Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force
