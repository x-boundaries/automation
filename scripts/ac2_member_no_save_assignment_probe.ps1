[CmdletBinding()]
param(
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$ServerName = $env:AC2_PROBE_SERVER_NAME,
    [string]$DatabaseName = $env:AC2_PROBE_DATABASE_NAME,
    [string]$UserId = $env:AC2_PROBE_USER_ID,
    [string]$PasswordEnvVar = "AC2_PROBE_PASSWORD",
    [switch]$EnableMemberNoSaveAssignmentProbe,
    [switch]$AllowRootLogin,
    [string]$JsonOut
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $EnableMemberNoSaveAssignmentProbe) {
    $refusal = [ordered]@{
        mode = "member-no-save-assignment-probe"
        probe_enabled = $false
        refused = $true
        message = "Explicit opt-in is required. Re-run with -EnableMemberNoSaveAssignmentProbe to run the fake-data no-save assignment probe."
    }
    $refusal | ConvertTo-Json -Depth 4
    throw "Refusing to run live fake-data no-save member assignment probe without -EnableMemberNoSaveAssignmentProbe."
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

Write-Warning "AC2 member fake-data no-save assignment probe is explicit opt-in. It assigns synthetic values to an in-memory member row only and emits sanitized assignment status."

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
    $text = $text.Replace("dryrun.member@example.invalid", "<synthetic-email>")
    $text = $text.Replace("XB Dry Run Member", "<synthetic-name>")
    $text = $text.Replace("NO_SAVE_ASSIGNMENT_PROBE", "<synthetic-marker>")
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

function New-AssignmentResult {
    param(
        [string]$Field,
        [System.Data.DataRow]$Row,
        [object]$Value
    )

    $columnExists = $false
    $attempted = $false
    $success = $false
    $dataType = $null
    $maxLength = $null
    $readOnly = $null
    $errorType = $null
    $errorMessage = $null

    try {
        if ($null -eq $Row) {
            throw "Member row was not available."
        }

        $columnExists = $Row.Table.Columns.Contains($Field)
        if ($columnExists) {
            $column = $Row.Table.Columns[$Field]
            $dataType = $column.DataType.FullName
            $maxLength = $column.MaxLength
            $readOnly = $column.ReadOnly

            if (-not $column.ReadOnly) {
                $attempted = $true
                $Row[$Field] = $Value
                $success = $true
            }
        }
    }
    catch {
        $errorType = $_.Exception.GetType().FullName
        $errorMessage = Get-ExceptionMessage $_.Exception
        $valueText = [string]$Value
        if (-not [string]::IsNullOrEmpty($valueText)) {
            $errorMessage = $errorMessage.Replace($valueText, "<synthetic-value>")
        }
    }

    return [ordered]@{
        field = $Field
        column_exists = $columnExists
        attempted = $attempted
        success = $success
        data_type = $dataType
        max_length = $maxLength
        read_only = $readOnly
        error_type = $errorType
        sanitized_error_message = $errorMessage
    }
}

function Test-AllAssignmentSuccess {
    param(
        [object[]]$Results,
        [string[]]$Fields
    )

    foreach ($field in $Fields) {
        $result = @($Results | Where-Object { $_.field -eq $field } | Select-Object -First 1)
        if ($result.Count -ne 1 -or -not [bool]$result[0].success) {
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
    mode = "member-no-save-assignment-probe"
    probe_enabled = $true
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
    member_entity_type_name = $null
    member_table_available = $false
    member_row_available = $false
    assignment_results = @()
    required_assignment_results = @()
    intake_assignment_results = @()
    all_required_assignment_success = $false
    all_intake_assignment_success = $false
    member_type_default_assignment_success = $false
    no_save_confirmed = $true
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
        throw "Authentication/session did not become available; member assignment probe was not attempted."
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

    $newMemberMethod = Find-PublicInstanceMethod $memberCommandType "NewMember" 1
    $result.new_member_found = $null -ne $newMemberMethod
    if ($null -eq $newMemberMethod) {
        throw "NewMember method was not found."
    }

    $memberEntity = $newMemberMethod.Invoke($memberCommand, @($false))
    $result.new_member_success = $true
    $result.member_entity_available = $null -ne $memberEntity
    if ($null -ne $memberEntity) {
        $result.member_entity_type_name = $memberEntity.GetType().FullName

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

        $memberNumberForAssignment = $generatedNumber
        if ([string]::IsNullOrWhiteSpace($memberNumberForAssignment)) {
            $memberNumberForAssignment = "DRYRUN"
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
            MemberNo = $memberNumberForAssignment
            MemberType = "Default"
            Name = "XB Dry Run Member"
            MobilePhone = "+6590000000"
            EmailAddress = "dryrun.member@example.invalid"
            DOB = [datetime]"1990-01-01"
            IsActive = $isActiveForAssignment
            RegisterDate = [datetime]"2026-01-01"
            Note = "NO_SAVE_ASSIGNMENT_PROBE"
            OpeningPoints = [decimal]0
            Individual = $individualForAssignment
        }

        $assignmentResults = @()
        foreach ($field in @($requiredAssignmentFields + $intakeAssignmentFields)) {
            $assignmentResults += New-AssignmentResult $field $memberRow $plannedAssignments[$field]
        }

        $result.assignment_results = $assignmentResults
        $result.required_assignment_results = @($assignmentResults | Where-Object { $requiredAssignmentFields -contains $_.field })
        $result.intake_assignment_results = @($assignmentResults | Where-Object { $intakeAssignmentFields -contains $_.field })
        $result.all_required_assignment_success = Test-AllAssignmentSuccess $assignmentResults $requiredAssignmentFields
        $result.all_intake_assignment_success = Test-AllAssignmentSuccess $assignmentResults $intakeAssignmentFields
        $memberTypeResult = @($assignmentResults | Where-Object { $_.field -eq "MemberType" } | Select-Object -First 1)
        $result.member_type_default_assignment_success = ($memberTypeResult.Count -eq 1 -and [bool]$memberTypeResult[0].success)
    }
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
    Remove-Variable generatedNumber -ErrorAction SilentlyContinue
}

$json = $result | ConvertTo-Json -Depth 8

if (-not [string]::IsNullOrWhiteSpace($JsonOut)) {
    $jsonOutPath = [System.IO.Path]::GetFullPath($JsonOut)
    $jsonOutParent = [System.IO.Path]::GetDirectoryName($jsonOutPath)
    if (-not [string]::IsNullOrWhiteSpace($jsonOutParent)) {
        New-Item -ItemType Directory -Path $jsonOutParent -Force | Out-Null
    }
    Set-Content -LiteralPath $jsonOutPath -Value $json -Encoding UTF8
    Write-Host "Wrote sanitized member no-save assignment probe JSON to $jsonOutPath"
}

$json
