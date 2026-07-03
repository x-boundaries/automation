[CmdletBinding()]
param(
    [string]$DllRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$ServerName = $env:AC2_PROBE_SERVER_NAME,
    [string]$DatabaseName = $env:AC2_PROBE_DATABASE_NAME,
    [string]$UserId = $env:AC2_PROBE_USER_ID,
    [switch]$AllowRootLogin,
    [string]$OutputDirectory,
    [int]$MaxRows = 0,
    [switch]$EnableMemberBrowseExtractReview
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
        member_command_create_found = $false
        load_browse_table_found = $false
        load_browse_table_success = $false
        row_count = 0
        column_names = @()
        output_file_paths = @()
        warning_count = 0
        error = $ErrorValue
    }
}

if (-not $EnableMemberBrowseExtractReview) {
    $refusal = New-ConsoleResult "refused" ([ordered]@{
        type = "ExplicitOptInRequired"
        message = "Refusing to run: explicit opt-in is required. Re-run with -EnableMemberBrowseExtractReview to browse members for local reconciliation review."
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
$recommendedColumns = @(
    "MemberNo",
    "MemberType",
    "Name",
    "MobilePhone",
    "EmailAddress",
    "DOB",
    "IsActive",
    "RegisterDate",
    "ExpiryDate",
    "Note",
    "OpeningPoints",
    "Individual",
    "CreatedTime",
    "LastModified"
)

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
    foreach ($secret in @(
        $ServerName,
        $DatabaseName,
        $UserId,
        [Environment]::GetEnvironmentVariable("AC2_PROBE_PASSWORD"),
        $DllRoot,
        $OutputDirectory
    )) {
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

    $matches = @($Type.GetMethods($flags) | Where-Object {
        $_.Name -eq $Name -and $_.GetParameters().Count -eq $ParameterCount
    } | Select-Object -First 1)

    if ($matches.Count -gt 0) {
        return $matches[0]
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

    $matches = @($Type.GetMethods($flags) | Where-Object {
        $_.Name -eq $Name -and $_.GetParameters().Count -eq $ParameterCount
    } | Select-Object -First 1)

    if ($matches.Count -gt 0) {
        return $matches[0]
    }

    return $null
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

function Convert-FieldValue {
    param([object]$Value)

    if ($null -eq $Value -or [System.DBNull]::Value.Equals($Value)) {
        return $null
    }

    if ($Value -is [datetime]) {
        return $Value.ToString("o")
    }

    return $Value
}

function Get-OrderedColumnNames {
    param(
        [System.Data.DataTable]$Table,
        [string[]]$PreferredNames
    )

    $allNames = @($Table.Columns | ForEach-Object { $_.ColumnName })
    $preferred = @($PreferredNames | Where-Object { $allNames -contains $_ })
    $remaining = @($allNames | Where-Object { $preferred -notcontains $_ })
    return @($preferred + $remaining)
}

function Convert-TableRows {
    param(
        [System.Data.DataTable]$Table,
        [string[]]$ColumnNames,
        [int]$Limit
    )

    $rowLimit = $Table.Rows.Count
    if ($Limit -gt 0) {
        $rowLimit = [Math]::Min($Limit, $Table.Rows.Count)
    }

    $rows = @()
    for ($rowIndex = 0; $rowIndex -lt $rowLimit; $rowIndex++) {
        $row = $Table.Rows[$rowIndex]
        $item = [ordered]@{
            private_review_notice = "PRIVATE - DO NOT COMMIT - contains PII"
            extracted_at_utc = [DateTime]::UtcNow.ToString("o")
        }

        foreach ($columnName in $ColumnNames) {
            $item[$columnName] = Convert-FieldValue $row[$columnName]
        }

        $rows += [pscustomobject]$item
    }

    return $rows
}

$result = New-ConsoleResult "started"
$warnings = @(
    "Output contains PII and must stay local.",
    "Generated member browse extract files must not be committed."
)

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
    if ($MaxRows -lt 0) {
        throw "MaxRows must be 0 for all rows or a positive row limit."
    }

    $serverForReview = Get-RequiredValue "ServerName" $ServerName "AC2_PROBE_SERVER_NAME"
    $databaseForReview = Get-RequiredValue "DatabaseName" $DatabaseName "AC2_PROBE_DATABASE_NAME"
    $userForReview = Get-RequiredValue "UserId" $UserId "AC2_PROBE_USER_ID"
    if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
        throw "OutputDirectory is required and must point to a local private review folder."
    }

    $passwordForReview = [Environment]::GetEnvironmentVariable("AC2_PROBE_PASSWORD")
    if ([string]::IsNullOrEmpty($passwordForReview)) {
        throw "Password environment variable 'AC2_PROBE_PASSWORD' is required for this PowerShell process."
    }

    $outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
    $csvPath = Join-Path $outputRoot "ac2_member_browse_extract.csv"
    $summaryPath = Join-Path $outputRoot "ac2_member_browse_extract_summary.json"
    $warningPath = Join-Path $outputRoot "PRIVATE_DO_NOT_COMMIT_MEMBER_BROWSE_EXTRACT.txt"

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
        throw "Authentication/session was not available for member browse extract review."
    }

    $memberCommandType = $memberAssembly.GetType($memberCommandTypeName, $false, $false)
    $result.member_command_found = $null -ne $memberCommandType
    if ($null -eq $memberCommandType) {
        throw "MemberCommand type was not found."
    }

    $memberCommandCreate = Find-PublicStaticMethodByTypes $memberCommandType "Create" @($userSessionTypeName, $dbSettingTypeName)
    $result.member_command_create_found = $null -ne $memberCommandCreate
    if ($null -eq $memberCommandCreate) {
        throw "MemberCommand.Create factory method was not found."
    }

    $loadBrowseTableMethod = Find-PublicInstanceMethod $memberCommandType "LoadBrowseTable" 0
    $result.load_browse_table_found = $null -ne $loadBrowseTableMethod
    if ($null -eq $loadBrowseTableMethod) {
        throw "LoadBrowseTable method was not found."
    }

    $memberCommand = $memberCommandCreate.Invoke($null, @($session, $dbSetting))
    $table = $loadBrowseTableMethod.Invoke($memberCommand, @())
    if ($null -eq $table -or -not ($table -is [System.Data.DataTable])) {
        throw "LoadBrowseTable did not return a DataTable."
    }

    $result.load_browse_table_success = $true
    $result.row_count = $table.Rows.Count
    $columnNames = Get-OrderedColumnNames $table $recommendedColumns
    $result.column_names = @($columnNames)

    if ($MaxRows -gt 0 -and $table.Rows.Count -gt $MaxRows) {
        $warnings += "MaxRows limited the local extract row count for this review run."
    }

    New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
    $rows = @(Convert-TableRows $table $columnNames $MaxRows)
    if ($rows.Count -gt 0) {
        $rows | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8
    }
    else {
        $csvHeaderColumns = @("private_review_notice", "extracted_at_utc") + $columnNames
        $csvHeader = @($csvHeaderColumns | ForEach-Object {
            '"' + ([string]$_).Replace('"', '""') + '"'
        }) -join ","
        Set-Content -LiteralPath $csvPath -Value $csvHeader -Encoding UTF8
    }

    $summary = [ordered]@{
        private_review_notice = "PRIVATE - DO NOT COMMIT - Output contains PII."
        status = "ok"
        mode = "ac2_member_browse_extract_review"
        generated_at_utc = [DateTime]::UtcNow.ToString("o")
        read_only_api = "MemberCommand.LoadBrowseTable"
        max_rows = $MaxRows
        exported_row_count = $rows.Count
        source_row_count = $table.Rows.Count
        column_names = @($columnNames)
        recommended_columns_present = @($recommendedColumns | Where-Object { $columnNames -contains $_ })
        output_file_paths = @($csvPath, $summaryPath, $warningPath)
        warnings = @($warnings)
    }
    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
    @(
        "PRIVATE - DO NOT COMMIT",
        "Output contains PII and is for local migration reconciliation review only.",
        "Do not paste raw member rows into Git, pull requests, chat, tickets, or screenshots.",
        "Review folder: $outputRoot"
    ) | Set-Content -LiteralPath $warningPath -Encoding UTF8

    $result.status = "ok"
    $result.output_file_paths = @($csvPath, $summaryPath, $warningPath)
}
catch {
    $result.status = "error"
    $result.error = [ordered]@{
        type = $_.Exception.GetType().FullName
        message = Get-SanitizedMessage $_.Exception.Message
    }
}
finally {
    [System.AppDomain]::CurrentDomain.remove_AssemblyResolve($assemblyResolveHandler)
    Remove-Variable passwordForReview -ErrorAction SilentlyContinue
}

$result.warning_count = $warnings.Count
$result | ConvertTo-Json -Depth 8
