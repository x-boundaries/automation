[CmdletBinding()]
param(
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$ServerName = $env:AC2_PROBE_SERVER_NAME,
    [string]$DatabaseName = $env:AC2_PROBE_DATABASE_NAME,
    [string]$UserId = $env:AC2_PROBE_USER_ID,
    [string]$PasswordEnvVar = "AC2_PROBE_PASSWORD",
    [switch]$EnableMemberFakeCreateProbe,
    [switch]$ConfirmAutoCountWrite,
    [switch]$ConfirmSyntheticDataOnly,
    [switch]$ConfirmSingleFakeMember,
    [switch]$AllowRootLogin,
    [string]$JsonOut
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$cleanupInstruction = "Open AutoCount Bonus Point > Member Maintenance and search for Note marker XB_AUTOMATION_FAKE_CREATE_PROBE_DELETE_ME or the locally recorded fake member number, then delete the fake test member after verification if desired."

if (-not ($EnableMemberFakeCreateProbe -and $ConfirmAutoCountWrite -and $ConfirmSyntheticDataOnly -and $ConfirmSingleFakeMember)) {
    $refusal = [ordered]@{
        mode = "member-fake-create-probe"
        probe_enabled = [bool]$EnableMemberFakeCreateProbe
        confirm_auto_count_write = [bool]$ConfirmAutoCountWrite
        confirm_synthetic_data_only = [bool]$ConfirmSyntheticDataOnly
        confirm_single_fake_member = [bool]$ConfirmSingleFakeMember
        save_member_attempted = $false
        manual_cleanup_required = $false
        cleanup_instruction = $cleanupInstruction
        refused = $true
        message = "Explicit opt-in is required. Re-run with -EnableMemberFakeCreateProbe, -ConfirmAutoCountWrite, -ConfirmSyntheticDataOnly, and -ConfirmSingleFakeMember to create exactly one synthetic fake member."
    }
    $refusal | ConvertTo-Json -Depth 4
    throw "Refusing to run AC2 fake member create probe without all four explicit write opt-ins."
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
$memberCommandTypeName = "AutoCount.BonusPoint.Member.MemberCommand"
$requiredAssignmentFields = @("MemberNo", "MemberType", "IsActive", "OpeningPoints", "Individual")
$intakeAssignmentFields = @("Name", "MobilePhone", "EmailAddress", "DOB", "RegisterDate", "Note")
$fakeMemberMarker = "XB_AUTOMATION_FAKE_CREATE_PROBE_DELETE_ME"

Write-Warning "AC2 fake member create probe is explicit opt-in and write-capable. It creates exactly one synthetic fake member only after all four write confirmations are supplied."

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
    $text = $text.Replace("xb.api.save.probe@example.invalid", "<synthetic-email>")
    $text = $text.Replace("XB API SAVE PROBE", "<synthetic-name>")
    $text = $text.Replace($fakeMemberMarker, "<synthetic-marker>")
    return $text
}

function Get-ExceptionMessage {
    param([System.Exception]$Exception)

    $current = $Exception
    while ($null -ne $current.InnerException) {
        $current = $current.InnerException
    }

    return Get-SanitizedMessage $current.Message
}

function Get-MaskedMemberNo {
    param([string]$MemberNo)

    if ([string]::IsNullOrEmpty($MemberNo) -or $MemberNo.Length -le 2) {
        return "***"
    }

    return $MemberNo.Substring(0, 2) + "***" + $MemberNo.Substring($MemberNo.Length - 1, 1)
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

function Find-PublicStaticMethodByParameterTypes {
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

function Get-RowStringValue {
    param(
        [System.Data.DataRow]$Row,
        [string]$Field
    )

    if ($null -eq $Row -or -not $Row.Table.Columns.Contains($Field)) {
        return ""
    }

    $value = $Row[$Field]
    if ($null -eq $value -or $value -eq [System.DBNull]::Value) {
        return ""
    }

    return [string]$value
}

function Set-MemberRowValue {
    param(
        [System.Data.DataRow]$Row,
        [string]$Field,
        [object]$Value
    )

    if ($null -eq $Row) {
        throw "Member row was not available."
    }
    if (-not $Row.Table.Columns.Contains($Field)) {
        throw "Expected member field was not available."
    }
    if ($Row.Table.Columns[$Field].ReadOnly) {
        throw "Expected member field was read-only."
    }

    $Row[$Field] = $Value
}

function Test-AssignmentSucceeded {
    param(
        [System.Data.DataRow]$Row,
        [string[]]$Fields
    )

    foreach ($field in $Fields) {
        if ($null -eq $Row -or -not $Row.Table.Columns.Contains($field)) {
            return $false
        }
        $value = $Row[$field]
        if ($null -eq $value -or $value -eq [System.DBNull]::Value) {
            return $false
        }
    }

    return $true
}

$serverForProbe = Get-RequiredValue "ServerName" $ServerName "AC2_PROBE_SERVER_NAME"
$databaseForProbe = Get-RequiredValue "DatabaseName" $DatabaseName "AC2_PROBE_DATABASE_NAME"
$userForProbe = Get-RequiredValue "UserId" $UserId "AC2_PROBE_USER_ID"
$passwordForProbe = [Environment]::GetEnvironmentVariable($PasswordEnvVar)
if ([string]::IsNullOrEmpty($passwordForProbe)) {
    throw "Password environment variable '$PasswordEnvVar' is required for this PowerShell process."
}

$result = [ordered]@{
    mode = "member-fake-create-probe"
    probe_enabled = [bool]$EnableMemberFakeCreateProbe
    confirm_auto_count_write = [bool]$ConfirmAutoCountWrite
    confirm_synthetic_data_only = [bool]$ConfirmSyntheticDataOnly
    confirm_single_fake_member = [bool]$ConfirmSingleFakeMember
    ac_root_exists = $false
    required_assemblies_loaded = $false
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
    current_session_method_found = $false
    current_session_available_after_set = $false
    check_has_logined_method_found = $false
    check_has_logined_success = $false
    authentication_success = $false
    user_session_available = $false
    member_command_found = $false
    member_command_create_found = $false
    member_command_available = $false
    get_next_member_no_found = $false
    get_next_member_no_success = $false
    next_member_no_length = 0
    next_member_no_nonempty = $false
    new_member_found = $false
    new_member_success = $false
    member_entity_available = $false
    member_table_available = $false
    member_row_available = $false
    assignment_success = $false
    save_member_method_found = $false
    save_member_attempted = $false
    save_member_success = $false
    created_fake_member_marker = $fakeMemberMarker
    created_fake_member_no_masked = $null
    created_fake_member_no_length = 0
    manual_cleanup_required = $false
    cleanup_instruction = $cleanupInstruction
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
        if (-not (Test-Path -LiteralPath $assemblyPath -PathType Leaf)) {
            throw "Required AutoCount assembly was not found."
        }
        [void][System.Reflection.Assembly]::LoadFrom($assemblyPath)
    }
    $result.required_assemblies_loaded = $true

    $coreAssemblyPath = Join-Path $script:AcRootPath $coreAssemblyName
    $memberAssemblyPath = Join-Path $script:AcRootPath $memberAssemblyName
    $coreAssembly = [System.Reflection.Assembly]::LoadFrom($coreAssemblyPath)
    $memberAssembly = [System.Reflection.Assembly]::LoadFrom($memberAssemblyPath)

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

    $currentSessionMethod = Find-PublicStaticMethod $userSessionType "get_CurrentUserSession" 0
    $result.current_session_method_found = $null -ne $currentSessionMethod
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

    if ($result.instance_login_success -and $null -ne $currentSessionMethod) {
        $currentSession = $currentSessionMethod.Invoke($null, @())
        $result.current_session_available_after_set = $null -ne $currentSession
    }

    if ($result.instance_login_success -and $null -ne $checkHasLoginedMethod) {
        [void]$checkHasLoginedMethod.Invoke($session, @())
        $result.check_has_logined_success = $true
    }

    $result.authentication_success = $result.static_auth_success -or $result.instance_login_success
    $result.user_session_available = (
        ($result.instance_login_success -and $null -ne $session) -or
        $result.current_session_available_after_set
    )

    if (-not $result.authentication_success -or -not $result.user_session_available) {
        throw "Authentication/session did not become available; fake member create probe was not attempted."
    }

    $memberCommandType = $memberAssembly.GetType($memberCommandTypeName, $false, $false)
    $result.member_command_found = $null -ne $memberCommandType
    if ($null -eq $memberCommandType) {
        throw "MemberCommand type was not found."
    }

    # MemberCommand.Create(UserSession, DBSetting) is the only command factory used by this probe.
    $memberCommandCreateMethod = Find-PublicStaticMethodByParameterTypes $memberCommandType "Create" @($userSessionTypeName, $dbSettingTypeName)
    $result.member_command_create_found = $null -ne $memberCommandCreateMethod
    if ($null -eq $memberCommandCreateMethod) {
        throw "MemberCommand public static factory was not found."
    }

    $memberCommand = $memberCommandCreateMethod.Invoke($null, @($session, $dbSetting))
    $result.member_command_available = $null -ne $memberCommand

    $generatedNumber = ""
    $getNextMethod = Find-PublicInstanceMethod $memberCommandType "GetNextMemberNo" 0
    $result.get_next_member_no_found = $null -ne $getNextMethod
    if ($null -ne $getNextMethod) {
        $generatedNumber = [string]$getNextMethod.Invoke($memberCommand, @())
        $result.get_next_member_no_success = $true
        $result.next_member_no_length = $generatedNumber.Length
        $result.next_member_no_nonempty = -not [string]::IsNullOrEmpty($generatedNumber)
    }
    if ([string]::IsNullOrWhiteSpace($generatedNumber)) {
        throw "Generated member number was empty; fake member create probe was not attempted."
    }

    $newMemberMethod = Find-PublicInstanceMethod $memberCommandType "NewMember" 1
    $result.new_member_found = $null -ne $newMemberMethod
    if ($null -eq $newMemberMethod) {
        throw "NewMember method was not found."
    }

    $memberEntity = $newMemberMethod.Invoke($memberCommand, @($false))
    $result.new_member_success = $true
    $result.member_entity_available = $null -ne $memberEntity
    if ($null -eq $memberEntity) {
        throw "NewMember returned no member entity."
    }

    $memberTableProperty = Find-PublicProperty $memberEntity.GetType() "MemberTable"
    $rowProperty = Find-PublicProperty $memberEntity.GetType() "Row"
    $memberTable = $null
    $memberRow = $null
    if ($null -ne $memberTableProperty -and $memberTableProperty.CanRead) {
        $memberTable = $memberTableProperty.GetValue($memberEntity, $null)
    }
    if ($null -ne $rowProperty -and $rowProperty.CanRead) {
        $memberRow = $rowProperty.GetValue($memberEntity, $null)
    }
    if ($null -eq $memberRow -and $memberTable -is [System.Data.DataTable] -and $memberTable.Rows.Count -gt 0) {
        $memberRow = $memberTable.Rows[0]
    }

    $result.member_table_available = $null -ne $memberTable
    $result.member_row_available = $null -ne $memberRow
    if ($null -eq $memberRow) {
        throw "Member row was not available."
    }

    $isActiveForAssignment = Get-RowStringValue $memberRow "IsActive"
    if ([string]::IsNullOrWhiteSpace($isActiveForAssignment)) {
        $isActiveForAssignment = "T"
    }

    $individualForAssignment = Get-RowStringValue $memberRow "Individual"
    if ([string]::IsNullOrWhiteSpace($individualForAssignment)) {
        $individualForAssignment = "T"
    }

    $plannedAssignments = [ordered]@{
        MemberNo = $generatedNumber
        MemberType = "Default"
        Name = "XB API SAVE PROBE"
        MobilePhone = "90000000"
        EmailAddress = "xb.api.save.probe@example.invalid"
        DOB = [datetime]"1990-01-01"
        IsActive = $isActiveForAssignment
        RegisterDate = [datetime]"2026-01-01"
        Note = $fakeMemberMarker
        OpeningPoints = [decimal]0
        Individual = $individualForAssignment
    }

    foreach ($field in @($requiredAssignmentFields + $intakeAssignmentFields)) {
        Set-MemberRowValue $memberRow $field $plannedAssignments[$field]
    }
    $result.assignment_success = Test-AssignmentSucceeded $memberRow @($requiredAssignmentFields + $intakeAssignmentFields)
    if (-not $result.assignment_success) {
        throw "Synthetic fake member assignment did not complete; SaveMember was not attempted."
    }

    $saveMemberMethod = $null
    foreach ($method in $memberCommandType.GetMethods([System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Instance)) {
        if ($method.Name -ne "SaveMember") {
            continue
        }
        $parameters = @($method.GetParameters())
        if ($parameters.Count -eq 1 -and $parameters[0].ParameterType.FullName -eq $memberEntity.GetType().FullName) {
            $saveMemberMethod = $method
            break
        }
    }
    $result.save_member_method_found = $null -ne $saveMemberMethod
    if ($null -eq $saveMemberMethod) {
        throw "SaveMember member entity method was not found."
    }

    $result.save_member_attempted = $true
    [void]$saveMemberMethod.Invoke($memberCommand, @($memberEntity))
    $result.save_member_success = $true
    $result.created_fake_member_no_masked = Get-MaskedMemberNo $generatedNumber
    $result.created_fake_member_no_length = $generatedNumber.Length
    $result.manual_cleanup_required = $true
}
catch {
    $result.error = [ordered]@{
        type = $_.Exception.GetType().FullName
        message = Get-ExceptionMessage $_.Exception
    }
}
finally {
    [System.AppDomain]::CurrentDomain.remove_AssemblyResolve($assemblyResolveHandler)
    Remove-Variable passwordForProbe -ErrorAction SilentlyContinue
}

$safeJson = $result | ConvertTo-Json -Depth 8

if (-not [string]::IsNullOrWhiteSpace($JsonOut)) {
    $fileResult = [ordered]@{}
    foreach ($key in $result.Keys) {
        $fileResult[$key] = $result[$key]
    }
    if ($result.save_member_success -and $null -ne (Get-Variable generatedNumber -ErrorAction SilentlyContinue)) {
        $fileResult["created_fake_member_no_local"] = $generatedNumber
    }

    $jsonOutPath = [System.IO.Path]::GetFullPath($JsonOut)
    $jsonOutParent = [System.IO.Path]::GetDirectoryName($jsonOutPath)
    if (-not [string]::IsNullOrWhiteSpace($jsonOutParent)) {
        New-Item -ItemType Directory -Path $jsonOutParent -Force | Out-Null
    }
    Set-Content -LiteralPath $jsonOutPath -Value ($fileResult | ConvertTo-Json -Depth 8) -Encoding UTF8
    Write-Host "Wrote sanitized member fake create probe JSON to $jsonOutPath"
}

Remove-Variable generatedNumber -ErrorAction SilentlyContinue
$safeJson
