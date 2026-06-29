[CmdletBinding()]
param(
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$ServerName = $env:AC2_PROBE_SERVER_NAME,
    [string]$DatabaseName = $env:AC2_PROBE_DATABASE_NAME,
    [string]$UserId = $env:AC2_PROBE_USER_ID,
    [string]$PasswordEnvVar = "AC2_PROBE_PASSWORD",
    [string]$JsonOut,
    [int]$MaxRows = 50,
    [switch]$EnableMemberTypeBrowseProbe,
    [switch]$AllowRootLogin
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $EnableMemberTypeBrowseProbe) {
    $refusal = [ordered]@{
        mode = "member-type-browse-probe"
        member_type_browse_probe_enabled = $false
        refused = $true
        message = "Explicit opt-in is required. Re-run with -EnableMemberTypeBrowseProbe to browse member type values with runtime-supplied values."
    }
    $refusal | ConvertTo-Json -Depth 4
    throw "Refusing to run member type browse probe without -EnableMemberTypeBrowseProbe."
}

if ($MaxRows -lt 1) {
    throw "MaxRows must be 1 or greater."
}

$script:AcRootPath = [System.IO.Path]::GetFullPath($AcRoot)
$assemblyNames = @(
    "AutoCount.dll",
    "AutoCount.Accounting.dll",
    "AutoCount.Invoicing.dll",
    "AutoCount.ImportExport.dll",
    "AutoCount.Tools.dll"
)
$coreAssemblyName = "AutoCount.dll"
$memberAssemblyName = "AutoCount.Invoicing.dll"
$dbSettingTypeName = "AutoCount.Data.DBSetting"
$userSessionTypeName = "AutoCount.Authentication.UserSession"
$memberTypeCommandTypeName = "AutoCount.BonusPoint.Member.MemberTypeCommand"

Write-Warning "AC2 member type browse probe is explicit opt-in and read-only. It authenticates, creates the member type command, and calls LoadBrowseTable only."

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

function Find-PublicInstanceMethod {
    param(
        [Type]$Type,
        [string]$Name,
        [int]$ParameterCount
    )

    $flags = [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::Instance

    return @($Type.GetMethods($flags) | Where-Object {
        $_.Name -eq $Name -and $_.GetParameters().Count -eq $ParameterCount
    } | Select-Object -First 1)[0]
}

function Find-PublicProperty {
    param(
        [Type]$Type,
        [string]$Name
    )

    $flags = [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::Instance

    return $Type.GetProperty($Name, $flags)
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

function Find-PublicStaticMethodByTypes {
    param(
        [Type]$Type,
        [string]$Name,
        [string[]]$ParameterTypeNames
    )

    $flags = [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::Static

    foreach ($method in $Type.GetMethods($flags)) {
        if ($method.Name -ne $Name) {
            continue
        }

        $parameters = @($method.GetParameters())
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
            return $method
        }
    }

    return $null
}

function Get-SafeText {
    param([object]$Value)

    if ($null -eq $Value -or [System.DBNull]::Value.Equals($Value)) {
        return $null
    }

    $text = [string]$Value
    if ($text.Length -gt 120) {
        return $text.Substring(0, 120)
    }

    return $text
}

function Get-SafeColumnNames {
    param([System.Data.DataTable]$Table)

    $preferred = @("MemberType", "Description", "Level")
    $names = @($Table.Columns | ForEach-Object { $_.ColumnName })
    $selected = @($preferred | Where-Object { $names -contains $_ })
    if ($selected.Count -gt 0) {
        return $selected
    }

    $blocked = "(?i)(name|phone|mobile|email|address|contact|id|remark|note|birth|dob)"
    return @($Table.Columns | Where-Object {
        $_.ColumnName -notmatch $blocked -and
        ($_.DataType -eq [string] -or $_.DataType.IsPrimitive -or $_.DataType -eq [decimal])
    } | Select-Object -First 5 | ForEach-Object { $_.ColumnName })
}

function Convert-MemberTypeRows {
    param(
        [System.Data.DataTable]$Table,
        [string[]]$ColumnNames,
        [int]$Limit
    )

    $rows = @()
    $count = [Math]::Min($Limit, $Table.Rows.Count)
    for ($rowIndex = 0; $rowIndex -lt $count; $rowIndex++) {
        $row = $Table.Rows[$rowIndex]
        $item = [ordered]@{}
        foreach ($columnName in $ColumnNames) {
            $item[$columnName] = Get-SafeText $row[$columnName]
        }
        $rows += [pscustomobject]$item
    }

    return $rows
}

function Test-DefaultMemberTypeSeen {
    param([System.Data.DataTable]$Table)

    $candidateNames = @("MemberType", "Member Type", "Type", "Code")
    $names = @($Table.Columns | ForEach-Object { $_.ColumnName })
    $columns = @($candidateNames | Where-Object { $names -contains $_ })
    foreach ($columnName in $columns) {
        foreach ($row in $Table.Rows) {
            if ([string]$row[$columnName] -eq "Default") {
                return $true
            }
        }
    }

    return $false
}

$serverForProbe = Get-RequiredValue "ServerName" $ServerName "AC2_PROBE_SERVER_NAME"
$databaseForProbe = Get-RequiredValue "DatabaseName" $DatabaseName "AC2_PROBE_DATABASE_NAME"
$userForProbe = Get-RequiredValue "UserId" $UserId "AC2_PROBE_USER_ID"
$passwordForProbe = [Environment]::GetEnvironmentVariable($PasswordEnvVar)
if ([string]::IsNullOrEmpty($passwordForProbe)) {
    throw "Password environment variable '$PasswordEnvVar' is required for this PowerShell process."
}

$result = [ordered]@{
    mode = "member-type-browse-probe"
    member_type_browse_probe_enabled = $true
    ac_root_exists = $false
    core_assembly_loaded = $false
    member_assembly_loaded = $false
    dbsetting_factory_found = $false
    static_auth_method_found = $false
    static_auth_success = $false
    user_session_constructor_found = $false
    allow_root_login_requested = [bool]$AllowRootLogin
    allow_root_login_property_found = $false
    allow_root_login_set = $false
    instance_login_method_found = $false
    instance_login_success = $false
    instance_is_login = $false
    set_as_current_method_found = $false
    set_as_current_called = $false
    check_has_logined_method_found = $false
    check_has_logined_success = $false
    authentication_success = $false
    user_session_available = $false
    member_type_command_found = $false
    member_type_command_create_found = $false
    load_browse_table_found = $false
    load_browse_table_success = $false
    row_count = 0
    column_names = @()
    rows = @()
    default_member_type_seen = $false
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

    $coreAssemblyPath = Join-Path $script:AcRootPath $coreAssemblyName
    if (-not (Test-Path -LiteralPath $coreAssemblyPath -PathType Leaf)) {
        throw "Required AutoCount core assembly was not found."
    }
    $coreAssembly = [System.Reflection.Assembly]::LoadFrom($coreAssemblyPath)
    $result.core_assembly_loaded = $true

    $memberAssemblyPath = Join-Path $script:AcRootPath $memberAssemblyName
    if (-not (Test-Path -LiteralPath $memberAssemblyPath -PathType Leaf)) {
        throw "Required AutoCount member assembly was not found."
    }
    $memberAssembly = [System.Reflection.Assembly]::LoadFrom($memberAssemblyPath)
    $result.member_assembly_loaded = $true

    $dbSettingType = $coreAssembly.GetType($dbSettingTypeName, $false, $false)
    if ($null -eq $dbSettingType) {
        throw "DBSetting type was not found."
    }

    $userSessionType = $coreAssembly.GetType($userSessionTypeName, $false, $false)
    if ($null -eq $userSessionType) {
        throw "UserSession type was not found."
    }

    $dbSettingFactory = Find-PublicStaticMethod $dbSettingType "CreateAutoCountDefaultDBSetting" 2
    $result.dbsetting_factory_found = $null -ne $dbSettingFactory
    if ($null -eq $dbSettingFactory) {
        throw "DBSetting factory method was not found."
    }

    $authenticateMethod = Find-PublicStaticMethod $userSessionType "Authenticate" 3
    $result.static_auth_method_found = $null -ne $authenticateMethod
    if ($null -eq $authenticateMethod) {
        throw "UserSession authentication method was not found."
    }

    $userSessionConstructor = Find-PublicConstructor $userSessionType @($dbSettingTypeName)
    $result.user_session_constructor_found = $null -ne $userSessionConstructor
    $instanceLoginMethod = Find-PublicInstanceMethod $userSessionType "Login" 2
    $result.instance_login_method_found = $null -ne $instanceLoginMethod
    $setAsCurrentMethod = Find-PublicInstanceMethod $userSessionType "SetAsCurrent" 0
    $result.set_as_current_method_found = $null -ne $setAsCurrentMethod
    $checkHasLoginedMethod = Find-PublicInstanceMethod $userSessionType "CheckHasLogined" 0
    $result.check_has_logined_method_found = $null -ne $checkHasLoginedMethod
    $isLoginProperty = Find-PublicProperty $userSessionType "IsLogin"
    $allowRootLoginProperty = Find-PublicProperty $userSessionType "AllowRootLogin"
    $result.allow_root_login_property_found = $null -ne $allowRootLoginProperty

    $dbSetting = $dbSettingFactory.Invoke($null, @($serverForProbe, $databaseForProbe))
    $authResult = $authenticateMethod.Invoke($null, @($dbSetting, $userForProbe, $passwordForProbe))
    $result.static_auth_success = [bool]$authResult

    if ($null -eq $userSessionConstructor) {
        throw "UserSession DBSetting constructor was not found."
    }
    if ($null -eq $instanceLoginMethod) {
        throw "UserSession instance Login method was not found."
    }

    $session = $userSessionConstructor.Invoke(@($dbSetting))
    if ($AllowRootLogin -and $null -ne $allowRootLoginProperty -and $allowRootLoginProperty.CanWrite) {
        $allowRootLoginProperty.SetValue($session, $true, $null)
        $result.allow_root_login_set = $true
    }

    $loginResult = $instanceLoginMethod.Invoke($session, @($userForProbe, $passwordForProbe))
    $result.instance_login_success = [bool]$loginResult
    if ($null -ne $isLoginProperty) {
        $result.instance_is_login = [bool]$isLoginProperty.GetValue($session, $null)
    }

    if ($result.instance_login_success -and $null -ne $setAsCurrentMethod) {
        [void]$setAsCurrentMethod.Invoke($session, @())
        $result.set_as_current_called = $true
    }

    if ($result.instance_login_success -and $null -ne $checkHasLoginedMethod) {
        [void]$checkHasLoginedMethod.Invoke($session, @())
        $result.check_has_logined_success = $true
    }

    $result.authentication_success = $result.static_auth_success -or $result.instance_login_success
    $result.user_session_available = $result.instance_login_success -and $result.instance_is_login
    if (-not ($result.authentication_success -and $result.user_session_available)) {
        throw "Authentication/session was not available for member type browse."
    }

    $memberTypeCommandType = $memberAssembly.GetType($memberTypeCommandTypeName, $false, $false)
    $result.member_type_command_found = $null -ne $memberTypeCommandType
    if ($null -eq $memberTypeCommandType) {
        throw "MemberTypeCommand type was not found."
    }

    $memberTypeCommandCreate = Find-PublicStaticMethodByTypes $memberTypeCommandType "Create" @($userSessionTypeName, $dbSettingTypeName)
    $result.member_type_command_create_found = $null -ne $memberTypeCommandCreate
    if ($null -eq $memberTypeCommandCreate) {
        throw "MemberTypeCommand.Create factory method was not found."
    }

    $loadBrowseTableMethod = Find-PublicInstanceMethod $memberTypeCommandType "LoadBrowseTable" 0
    $result.load_browse_table_found = $null -ne $loadBrowseTableMethod
    if ($null -eq $loadBrowseTableMethod) {
        throw "LoadBrowseTable method was not found."
    }

    $memberTypeCommand = $memberTypeCommandCreate.Invoke($null, @($session, $dbSetting))
    $table = $loadBrowseTableMethod.Invoke($memberTypeCommand, @())
    if ($null -eq $table -or -not ($table -is [System.Data.DataTable])) {
        throw "LoadBrowseTable did not return a DataTable."
    }

    $result.load_browse_table_success = $true
    $result.row_count = $table.Rows.Count
    $safeColumnNames = Get-SafeColumnNames $table
    $result.column_names = @($safeColumnNames)
    $result.rows = @(Convert-MemberTypeRows $table $safeColumnNames $MaxRows)
    $result.default_member_type_seen = Test-DefaultMemberTypeSeen $table
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

$json = $result | ConvertTo-Json -Depth 8

if (-not [string]::IsNullOrWhiteSpace($JsonOut)) {
    $jsonOutPath = [System.IO.Path]::GetFullPath($JsonOut)
    $jsonOutParent = [System.IO.Path]::GetDirectoryName($jsonOutPath)
    if (-not [string]::IsNullOrWhiteSpace($jsonOutParent)) {
        New-Item -ItemType Directory -Path $jsonOutParent -Force | Out-Null
    }
    Set-Content -LiteralPath $jsonOutPath -Value $json -Encoding UTF8
    Write-Host "Wrote sanitized member type browse probe JSON to $jsonOutPath"
}

$json
