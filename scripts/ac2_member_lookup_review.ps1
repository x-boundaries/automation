[CmdletBinding()]
param(
    [string]$DllRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$ServerName = $env:AC2_PROBE_SERVER_NAME,
    [string]$DatabaseName = $env:AC2_PROBE_DATABASE_NAME,
    [string]$UserId = $env:AC2_PROBE_USER_ID,
    [string]$MemberNo,
    [switch]$AllowRootLogin,
    [switch]$EnableMemberLookupReview
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function New-ConsoleResult {
    param(
        [string]$Status,
        [object]$ErrorValue = $null
    )

    return [ordered]@{
        status = $Status
        authentication_success = $false
        user_session_available = $false
        member_command_found = $false
        get_member_found = $false
        submitted_member_no_status = $null
        normalized_member_no_length = 0
        member_exists = $false
        member_found_by = $null
        manual_review_required = $false
        warning_count = 0
        error = $ErrorValue
    }
}

if (-not $EnableMemberLookupReview) {
    $refusal = New-ConsoleResult "refused" ([ordered]@{
        type = "ExplicitOptInRequired"
        message = "Refusing to run: explicit opt-in is required. Re-run with -EnableMemberLookupReview."
    })
    $refusal.warning_count = 1
    $refusal | ConvertTo-Json -Depth 6
    exit 2
}

$script:DllRootPath = [System.IO.Path]::GetFullPath($DllRoot)
$coreAssemblyName = "AutoCount.dll"
$memberAssemblyName = "AutoCount.Invoicing.dll"
$dbSettingTypeName = "AutoCount.Data.DBSetting"
$userSessionTypeName = "AutoCount.Authentication.UserSession"
$memberCommandTypeName = "AutoCount.BonusPoint.Member.MemberCommand"

function Get-RequiredValue {
    param(
        [string]$Label,
        [string]$Value,
        [string]$EnvLabel
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Label is required. Provide the parameter or set $EnvLabel for this PowerShell process."
    }

    return $Value
}

function Normalize-MemberNo {
    param([string]$RawMemberNo)

    $digits = [regex]::Replace($RawMemberNo, "\D", "")
    $status = "manual_review"
    $normalized = $digits
    $manualReview = $true

    if ($digits.Length -eq 8 -and ($digits.StartsWith("8") -or $digits.StartsWith("9"))) {
        $normalized = "65" + $digits
        $status = "canonical_65_mobile"
        $manualReview = $false
    }
    elseif ($digits.Length -eq 10 -and $digits.StartsWith("65")) {
        $normalized = $digits
        $status = "already_65_mobile"
        $manualReview = $false
    }

    if ($normalized.Length -gt 20) {
        $normalized = $normalized.Substring(0, 20)
        $manualReview = $true
        $status = "manual_review"
    }

    if ([string]::IsNullOrWhiteSpace($normalized)) {
        $status = "manual_review"
        $manualReview = $true
    }

    return [pscustomobject]@{
        Value = $normalized
        Status = $status
        ManualReview = $manualReview
    }
}

function Get-SanitizedMessage {
    param(
        [object]$Message,
        [string[]]$ExtraSecrets = @()
    )

    $text = [string]$Message
    foreach ($secret in @(
        $ServerName,
        $DatabaseName,
        $UserId,
        [Environment]::GetEnvironmentVariable("AC2_PROBE_PASSWORD"),
        $DllRoot,
        $MemberNo
    ) + $ExtraSecrets) {
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
        [string]$MethodName,
        [int]$ParameterCount
    )

    $flags = [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::Static

    $matches = @($Type.GetMethods($flags) | Where-Object {
        $_.Name -eq $MethodName -and $_.GetParameters().Count -eq $ParameterCount
    } | Select-Object -First 1)

    if ($matches.Count -gt 0) {
        return $matches[0]
    }

    return $null
}

function Find-PublicInstanceMethod {
    param(
        [Type]$Type,
        [string]$MethodName,
        [int]$ParameterCount
    )

    $flags = [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::Instance

    $matches = @($Type.GetMethods($flags) | Where-Object {
        $_.Name -eq $MethodName -and $_.GetParameters().Count -eq $ParameterCount
    } | Select-Object -First 1)

    if ($matches.Count -gt 0) {
        return $matches[0]
    }

    return $null
}

function Find-PublicProperty {
    param(
        [Type]$Type,
        [string]$PropertyName
    )

    $flags = [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::Instance

    return $Type.GetProperty($PropertyName, $flags)
}

function Find-PublicConstructor {
    param(
        [Type]$Type,
        [string[]]$ParameterTypeNames
    )

    $flags = [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::Instance

    foreach ($constructor in $Type.GetConstructors($flags)) {
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
        [string]$MethodName,
        [string[]]$ParameterTypeNames
    )

    $flags = [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::Static

    foreach ($method in $Type.GetMethods($flags)) {
        if ($method.Name -ne $MethodName) {
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

$result = New-ConsoleResult "started"
$warnings = @()
$normalizedForSecretScrub = $null

$assemblyResolveHandler = [System.ResolveEventHandler]{
    param($sender, $eventArgs)

    $assemblyName = [System.Reflection.AssemblyName]::new($eventArgs.Name)
    $candidatePath = Join-Path $script:DllRootPath ($assemblyName.Name + ".dll")
    if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
        return [System.Reflection.Assembly]::LoadFrom($candidatePath)
    }

    return $null
}
[System.AppDomain]::CurrentDomain.add_AssemblyResolve($assemblyResolveHandler)

try {
    $serverForReview = Get-RequiredValue "ServerName" $ServerName "AC2_PROBE_SERVER_NAME"
    $databaseForReview = Get-RequiredValue "DatabaseName" $DatabaseName "AC2_PROBE_DATABASE_NAME"
    $userForReview = Get-RequiredValue "UserId" $UserId "AC2_PROBE_USER_ID"
    $memberForReview = Get-RequiredValue "MemberNo" $MemberNo "MemberNo"
    $passwordForReview = [Environment]::GetEnvironmentVariable("AC2_PROBE_PASSWORD")
    if ([string]::IsNullOrEmpty($passwordForReview)) {
        throw "Environment variable 'AC2_PROBE_PASSWORD' is required for this PowerShell process."
    }

    $normalization = Normalize-MemberNo $memberForReview
    $normalizedForSecretScrub = $normalization.Value
    $result.submitted_member_no_status = $normalization.Status
    $result.normalized_member_no_length = $normalization.Value.Length
    $result.manual_review_required = [bool]$normalization.ManualReview
    if ($result.manual_review_required) {
        $warnings += "Submitted value shape requires manual review."
    }
    if ([string]::IsNullOrWhiteSpace($normalization.Value)) {
        throw "Submitted value did not contain any lookup characters after normalization."
    }

    if (-not (Test-Path -LiteralPath $script:DllRootPath -PathType Container)) {
        throw "AC2 DLL root path was not found."
    }

    $coreAssemblyPath = Join-Path $script:DllRootPath $coreAssemblyName
    if (-not (Test-Path -LiteralPath $coreAssemblyPath -PathType Leaf)) {
        throw "Required AutoCount core assembly was not found."
    }
    $coreAssembly = [System.Reflection.Assembly]::LoadFrom($coreAssemblyPath)

    $memberAssemblyPath = Join-Path $script:DllRootPath $memberAssemblyName
    if (-not (Test-Path -LiteralPath $memberAssemblyPath -PathType Leaf)) {
        throw "Required AutoCount member assembly was not found."
    }
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
    if ($null -eq $dbSettingFactory) {
        throw "DBSetting factory method was not found."
    }

    $authenticateMethod = Find-PublicStaticMethod $userSessionType "Authenticate" 3
    if ($null -eq $authenticateMethod) {
        throw "UserSession authentication method was not found."
    }

    $userSessionConstructor = Find-PublicConstructor $userSessionType @($dbSettingTypeName)
    $instanceLoginMethod = Find-PublicInstanceMethod $userSessionType "Login" 2
    $setAsCurrentMethod = Find-PublicInstanceMethod $userSessionType "SetAsCurrent" 0
    $checkHasLoginedMethod = Find-PublicInstanceMethod $userSessionType "CheckHasLogined" 0
    $isLoginProperty = Find-PublicProperty $userSessionType "IsLogin"
    $allowRootLoginProperty = Find-PublicProperty $userSessionType "AllowRootLogin"

    if ($null -eq $userSessionConstructor) {
        throw "UserSession DBSetting constructor was not found."
    }
    if ($null -eq $instanceLoginMethod) {
        throw "UserSession instance Login method was not found."
    }

    $dbSetting = $dbSettingFactory.Invoke($null, @($serverForReview, $databaseForReview))
    $authResult = $authenticateMethod.Invoke($null, @($dbSetting, $userForReview, $passwordForReview))

    $session = $userSessionConstructor.Invoke(@($dbSetting))
    if ($AllowRootLogin -and $null -ne $allowRootLoginProperty -and $allowRootLoginProperty.CanWrite) {
        $allowRootLoginProperty.SetValue($session, $true, $null)
    }

    $loginResult = $instanceLoginMethod.Invoke($session, @($userForReview, $passwordForReview))
    $instanceIsLogin = $false
    if ($null -ne $isLoginProperty) {
        $instanceIsLogin = [bool]$isLoginProperty.GetValue($session, $null)
    }

    if ([bool]$loginResult -and $null -ne $setAsCurrentMethod) {
        [void]$setAsCurrentMethod.Invoke($session, @())
    }

    if ([bool]$loginResult -and $null -ne $checkHasLoginedMethod) {
        [void]$checkHasLoginedMethod.Invoke($session, @())
    }

    $result.authentication_success = [bool]$authResult -or [bool]$loginResult
    $result.user_session_available = [bool]$loginResult -and $instanceIsLogin
    if (-not ($result.authentication_success -and $result.user_session_available)) {
        throw "Authentication/session was not available for member lookup review."
    }

    $memberCommandType = $memberAssembly.GetType($memberCommandTypeName, $false, $false)
    $result.member_command_found = $null -ne $memberCommandType
    if ($null -eq $memberCommandType) {
        throw "MemberCommand type was not found."
    }

    $memberCommandCreate = Find-PublicStaticMethodByTypes $memberCommandType "Create" @($userSessionTypeName, $dbSettingTypeName)
    if ($null -eq $memberCommandCreate) {
        throw "MemberCommand.Create factory method was not found."
    }

    $getMemberMethod = Find-PublicInstanceMethod $memberCommandType "GetMember" 1
    $result.get_member_found = $null -ne $getMemberMethod
    if ($null -eq $getMemberMethod) {
        throw "MemberCommand.GetMember method was not found."
    }

    $memberCommand = $memberCommandCreate.Invoke($null, @($session, $dbSetting))
    $memberRecord = $getMemberMethod.Invoke($memberCommand, @($normalization.Value))
    $result.member_exists = $null -ne $memberRecord
    if ($result.member_exists) {
        $result.member_found_by = "MemberCommand.GetMember"
    }

    $result.status = "ok"
}
catch {
    $result.status = "error"
    $result.error = [ordered]@{
        type = $_.Exception.GetType().FullName
        message = Get-SanitizedMessage $_.Exception.Message @($normalizedForSecretScrub)
    }
}
finally {
    [System.AppDomain]::CurrentDomain.remove_AssemblyResolve($assemblyResolveHandler)
    Remove-Variable passwordForReview -ErrorAction SilentlyContinue
}

$result.warning_count = $warnings.Count
$result | ConvertTo-Json -Depth 8
