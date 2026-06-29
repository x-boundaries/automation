[CmdletBinding()]
param(
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$ServerName = $env:AC2_PROBE_SERVER_NAME,
    [string]$DatabaseName = $env:AC2_PROBE_DATABASE_NAME,
    [string]$UserId = $env:AC2_PROBE_USER_ID,
    [string]$PasswordEnvVar = "AC2_PROBE_PASSWORD",
    [string]$JsonOut,
    [switch]$EnableSessionProbe
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $EnableSessionProbe) {
    $refusal = [ordered]@{
        mode = "session-auth-probe"
        session_probe_enabled = $false
        refused = $true
        message = "Explicit opt-in is required. Re-run with -EnableSessionProbe to authenticate with runtime-supplied values."
    }
    $refusal | ConvertTo-Json -Depth 4
    throw "Refusing to run live authentication probe without -EnableSessionProbe."
}

$script:AcRootPath = [System.IO.Path]::GetFullPath($AcRoot)
$assemblyNames = @(
    "AutoCount.dll",
    "AutoCount.Accounting.dll",
    "AutoCount.Invoicing.dll",
    "AutoCount.ImportExport.dll",
    "AutoCount.Tools.dll"
)
$targetAssemblyName = "AutoCount.dll"
$dbSettingTypeName = "AutoCount.Data.DBSetting"
$userSessionTypeName = "AutoCount.Authentication.UserSession"

Write-Warning "AC2 session auth probe is explicit opt-in and authentication-only. It does not instantiate member commands, invoke member factories, read/list members, run SQL, or write data."

function Get-RequiredValue {
    param(
        [string]$Name,
        [string]$Value,
        [string]$EnvName
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is required. Provide the parameter or set $EnvName for this PowerShell process."
    }

    return $Value
}

function Get-SanitizedMessage {
    param([object]$Message)

    $text = [string]$Message
    foreach ($secret in @($ServerName, $DatabaseName, $UserId, [Environment]::GetEnvironmentVariable($PasswordEnvVar))) {
        if (-not [string]::IsNullOrEmpty($secret)) {
            $text = $text.Replace($secret, "<redacted>")
        }
    }

    $text = [regex]::Replace($text, "(?i)(password|pwd|user id|uid|server|database)\s*=\s*[^;\s]+", '$1=<redacted>')
    return $text
}

function Find-PublicStaticMethod {
    param(
        [Type]$Type,
        [string]$Name,
        [int]$ParameterCount
    )

    $flags = [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::Static

    return @($Type.GetMethods($flags) | Where-Object {
        $_.Name -eq $Name -and $_.GetParameters().Count -eq $ParameterCount
    } | Select-Object -First 1)[0]
}

function Find-PublicConstructor {
    param(
        [Type]$Type,
        [string[]]$ParameterTypeNames
    )

    foreach ($constructor in $Type.GetConstructors([System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Instance)) {
        $parameters = @($constructor.GetParameters())
        if ($parameters.Count -ne $ParameterTypeNames.Count) {
            continue
        }

        $matches = $true
        for ($index = 0; $index -lt $parameters.Count; $index++) {
            if ($parameters[$index].ParameterType.FullName -ne $ParameterTypeNames[$index]) {
                $matches = $false
                break
            }
        }

        if ($matches) {
            return $constructor
        }
    }

    return $null
}

$serverForProbe = Get-RequiredValue "ServerName" $ServerName "AC2_PROBE_SERVER_NAME"
$databaseForProbe = Get-RequiredValue "DatabaseName" $DatabaseName "AC2_PROBE_DATABASE_NAME"
$userForProbe = Get-RequiredValue "UserId" $UserId "AC2_PROBE_USER_ID"
$passwordForProbe = [Environment]::GetEnvironmentVariable($PasswordEnvVar)
if ([string]::IsNullOrEmpty($passwordForProbe)) {
    throw "Password environment variable '$PasswordEnvVar' is required for this PowerShell process."
}

$result = [ordered]@{
    mode = "session-auth-probe"
    session_probe_enabled = $true
    ac_root_exists = $false
    target_assembly_loaded = $false
    dbsetting_factory_found = $false
    authenticate_method_found = $false
    current_session_method_found = $false
    user_session_constructor_found = $false
    authentication_success = $false
    user_session_available = $false
    error = $null
}

$assemblyResolveHandler = [System.ResolveEventHandler]{
    param($sender, $eventArgs)

    $assemblyName = [System.Reflection.AssemblyName]::new($eventArgs.Name)
    $candidatePath = Join-Path $script:AcRootPath ($assemblyName.Name + ".dll")
    if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
        return [System.Reflection.Assembly]::LoadFrom($candidatePath)
    }

    return $null
}
[System.AppDomain]::CurrentDomain.add_AssemblyResolve($assemblyResolveHandler)

try {
    $result.ac_root_exists = Test-Path -LiteralPath $script:AcRootPath -PathType Container
    if (-not $result.ac_root_exists) {
        throw "AC2 root path was not found."
    }

    foreach ($assemblyName in $assemblyNames) {
        $assemblyPath = Join-Path $script:AcRootPath $assemblyName
        if (Test-Path -LiteralPath $assemblyPath -PathType Leaf) {
            [void][System.Reflection.Assembly]::LoadFrom($assemblyPath)
        }
    }

    $targetAssemblyPath = Join-Path $script:AcRootPath $targetAssemblyName
    if (-not (Test-Path -LiteralPath $targetAssemblyPath -PathType Leaf)) {
        throw "Required AutoCount target assembly was not found."
    }

    $targetAssembly = [System.Reflection.Assembly]::LoadFrom($targetAssemblyPath)
    $result.target_assembly_loaded = $true

    $dbSettingType = $targetAssembly.GetType($dbSettingTypeName, $false, $false)
    if ($null -eq $dbSettingType) {
        throw "DBSetting type was not found."
    }

    $userSessionType = $targetAssembly.GetType($userSessionTypeName, $false, $false)
    if ($null -eq $userSessionType) {
        throw "UserSession type was not found."
    }

    $dbSettingFactory = Find-PublicStaticMethod $dbSettingType "CreateAutoCountDefaultDBSetting" 2
    $result.dbsetting_factory_found = $null -ne $dbSettingFactory
    if ($null -eq $dbSettingFactory) {
        throw "DBSetting factory method was not found."
    }

    $authenticateMethod = Find-PublicStaticMethod $userSessionType "Authenticate" 3
    $result.authenticate_method_found = $null -ne $authenticateMethod
    if ($null -eq $authenticateMethod) {
        throw "UserSession authentication method was not found."
    }

    $currentSessionMethod = Find-PublicStaticMethod $userSessionType "get_CurrentUserSession" 0
    $result.current_session_method_found = $null -ne $currentSessionMethod
    $userSessionConstructor = Find-PublicConstructor $userSessionType @($dbSettingTypeName)
    $result.user_session_constructor_found = $null -ne $userSessionConstructor

    $dbSetting = $dbSettingFactory.Invoke($null, @($serverForProbe, $databaseForProbe))
    $authResult = $authenticateMethod.Invoke($null, @($dbSetting, $userForProbe, $passwordForProbe))
    $result.authentication_success = [bool]$authResult

    if ($result.authentication_success -and $null -ne $currentSessionMethod) {
        $currentSession = $currentSessionMethod.Invoke($null, @())
        $result.user_session_available = $null -ne $currentSession
    }

    if ($result.authentication_success -and -not $result.user_session_available -and $null -ne $userSessionConstructor) {
        $session = $userSessionConstructor.Invoke(@($dbSetting))
        $result.user_session_available = $null -ne $session
    }
}
catch {
    $result.error = [ordered]@{
        type = $_.Exception.GetType().FullName
        message = Get-SanitizedMessage $_.Exception.Message
    }
}
finally {
    [System.AppDomain]::CurrentDomain.remove_AssemblyResolve($assemblyResolveHandler)
    Remove-Variable passwordForProbe -ErrorAction SilentlyContinue
}

$json = $result | ConvertTo-Json -Depth 6

if (-not [string]::IsNullOrWhiteSpace($JsonOut)) {
    $jsonOutPath = [System.IO.Path]::GetFullPath($JsonOut)
    $jsonOutParent = [System.IO.Path]::GetDirectoryName($jsonOutPath)
    if (-not [string]::IsNullOrWhiteSpace($jsonOutParent)) {
        New-Item -ItemType Directory -Path $jsonOutParent -Force | Out-Null
    }
    Set-Content -LiteralPath $jsonOutPath -Value $json -Encoding UTF8
    Write-Host "Wrote sanitized session auth probe JSON to $jsonOutPath"
}

$json
