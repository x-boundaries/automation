# Official local AutoCount boundary for the production member worker.
# This file is intentionally inert unless the worker supplies the explicit
# production adapter switch and a separately configured runtime session.

Set-StrictMode -Version Latest
$script:XbAutoCountAssignedFields = @(
    "MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress",
    "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual"
)
$script:XbAutoCountAdapterManagedFields = @("IsActive", "Individual")
# Shopify M1 (#155): the caller may supply only these fields. MemberType,
# OpeningPoints, IsActive and Individual are adapter-owned; DOB is never
# assigned; absent MobilePhone/EmailAddress stay DBNull, never "".
$script:XbShopifyCallerFields = @("MemberNo", "Name", "RegisterDate", "ExpiryDate", "MobilePhone", "EmailAddress")
$script:XbShopifyAdapterOwnedFields = @("MemberType", "OpeningPoints", "IsActive", "Individual")

function Get-XbAutoCountRuntimeValue {
    param(
        [Parameter(Mandatory)][string[]]$Names,
        [string]$Default
    )
    foreach ($name in $Names) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if (-not [string]::IsNullOrWhiteSpace($value)) { return $value }
    }
    return $Default
}

function Assert-XbAutoCountAdapterEnabled {
    param(
        [switch]$EnableProductionAdapter,
        [switch]$RequireAssemblyPath
    )
    if (-not $EnableProductionAdapter) {
        throw "autocount_adapter_disabled"
    }
    if ($RequireAssemblyPath -and [string]::IsNullOrWhiteSpace(
        [Environment]::GetEnvironmentVariable("XB_AC2_ASSEMBLY_PATH", "Process")
    )) {
        throw "autocount_assembly_path_missing"
    }
}

# These helpers intentionally mirror the already-proven UAT reflection boundary.
function Find-XbAutoCountPublicStaticMethod {
    param([Type]$Type, [string]$Name, [int]$ParameterCount)
    $flags = [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Static
    @($Type.GetMethods($flags) |
        Where-Object { $_.Name -eq $Name -and $_.GetParameters().Count -eq $ParameterCount } |
        Select-Object -First 1)[0]
}
function Find-XbAutoCountPublicStaticMethodByTypes {
    param([Type]$Type, [string]$Name, [string[]]$ParameterTypeNames)
    $flags = [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Static
    foreach ($method in $Type.GetMethods($flags)) {
        if ($method.Name -ne $Name) { continue }
        $parameters = @($method.GetParameters())
        if ($parameters.Count -ne $ParameterTypeNames.Count) { continue }
        $matches = $true
        for ($i = 0; $i -lt $parameters.Count; $i++) {
            if ($parameters[$i].ParameterType.FullName -ne $ParameterTypeNames[$i]) {
                $matches = $false
                break
            }
        }
        if ($matches) { return $method }
    }
    return $null
}
function Find-XbAutoCountPublicInstanceMethod {
    param([Type]$Type, [string]$Name, [int]$ParameterCount)
    $flags = [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Instance
    @($Type.GetMethods($flags) |
        Where-Object { $_.Name -eq $Name -and $_.GetParameters().Count -eq $ParameterCount } |
        Select-Object -First 1)[0]
}
function Find-XbAutoCountPublicProperty {
    param([Type]$Type, [string]$Name)
    $Type.GetProperty($Name, [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Instance)
}
function Find-XbAutoCountPublicConstructor {
    param([Type]$Type, [string[]]$ParameterTypeNames)
    foreach ($constructor in $Type.GetConstructors(
        [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Instance
    )) {
        $parameters = @($constructor.GetParameters())
        if ($parameters.Count -ne $ParameterTypeNames.Count) { continue }
        $matches = $true
        for ($i = 0; $i -lt $parameters.Count; $i++) {
            if ($parameters[$i].ParameterType.FullName -ne $ParameterTypeNames[$i]) {
                $matches = $false
                break
            }
        }
        if ($matches) { return $constructor }
    }
    return $null
}

function Get-XbAutoCountBindingValue {
    param(
        [Parameter(Mandatory)]$Binding,
        [Parameter(Mandatory)][string]$Name
    )
    if ($Binding -is [System.Collections.IDictionary] -and $Binding.Contains($Name)) {
        return $Binding[$Name]
    }
    $property = $Binding.PSObject.Properties[$Name]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function Convert-XbAutoCountSessionBinding {
    param([Parameter(Mandatory)][AllowNull()][object]$Binding)
    if ($null -eq $Binding) { throw "autocount_session_binding_invalid" }
    $userSession = Get-XbAutoCountBindingValue -Binding $Binding -Name "UserSession"
    $dbSetting = Get-XbAutoCountBindingValue -Binding $Binding -Name "DBSetting"
    if ($null -eq $userSession -or $null -eq $dbSetting) {
        throw "autocount_session_binding_invalid"
    }
    $factory = Get-XbAutoCountBindingValue -Binding $Binding -Name "MemberCommandFactory"
    [pscustomobject]@{
        UserSession = $userSession
        DBSetting = $dbSetting
        MemberCommandFactory = if ($factory -is [scriptblock]) { $factory } else { $null }
    }
}

function Initialize-XbAutoCountReviewedSession {
    param([Parameter(Mandatory)][string]$AssemblyRoot)

    $acRootPath = [System.IO.Path]::GetFullPath($AssemblyRoot)
    if (-not (Test-Path -LiteralPath $acRootPath -PathType Container)) {
        throw "autocount_assembly_path_invalid"
    }

    $server = Get-XbAutoCountRuntimeValue -Names @("XB_AC2_SERVER_NAME", "AC2_PROBE_SERVER_NAME")
    $database = Get-XbAutoCountRuntimeValue -Names @("XB_AC2_DATABASE_NAME", "AC2_PROBE_DATABASE_NAME")
    $user = Get-XbAutoCountRuntimeValue -Names @("XB_AC2_USER_ID", "AC2_PROBE_USER_ID")
    $passwordEnvName = Get-XbAutoCountRuntimeValue -Names @("XB_AC2_PASSWORD_ENV_VAR") -Default "AC2_PROBE_PASSWORD"
    if ([string]::IsNullOrWhiteSpace($server) -or
        [string]::IsNullOrWhiteSpace($database) -or
        [string]::IsNullOrWhiteSpace($user) -or
        [string]::IsNullOrWhiteSpace($passwordEnvName) -or
        $passwordEnvName -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
        throw "autocount_connection_config_missing"
    }
    $password = [Environment]::GetEnvironmentVariable($passwordEnvName, "Process")
    if ([string]::IsNullOrWhiteSpace($password)) { throw "autocount_password_missing" }

    $assemblyResolveHandler = $null
    try {
        $assemblyResolveHandler = [System.ResolveEventHandler] {
            param($sender, $eventArgs)
            $assemblyName = [System.Reflection.AssemblyName]::new($eventArgs.Name)
            $candidate = Join-Path $acRootPath ($assemblyName.Name + ".dll")
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return [System.Reflection.Assembly]::LoadFrom($candidate)
            }
            return $null
        }
        [System.AppDomain]::CurrentDomain.add_AssemblyResolve($assemblyResolveHandler)

        $assemblyNames = @(
            "AutoCount.dll", "AutoCount.Accounting.dll", "AutoCount.Invoicing.dll",
            "AutoCount.ImportExport.dll", "AutoCount.Tools.dll"
        )
        foreach ($assemblyName in $assemblyNames) {
            $assemblyPath = Join-Path $acRootPath $assemblyName
            if (-not (Test-Path -LiteralPath $assemblyPath -PathType Leaf)) {
                throw "autocount_assembly_missing"
            }
            [void][System.Reflection.Assembly]::LoadFrom($assemblyPath)
        }

        $coreAssembly = [System.Reflection.Assembly]::LoadFrom((Join-Path $acRootPath "AutoCount.dll"))
        $dbSettingType = $coreAssembly.GetType("AutoCount.Data.DBSetting", $false, $false)
        $userSessionType = $coreAssembly.GetType("AutoCount.Authentication.UserSession", $false, $false)
        if ($null -eq $dbSettingType -or $null -eq $userSessionType) {
            throw "autocount_core_types_missing"
        }

        $dbSettingFactory = Find-XbAutoCountPublicStaticMethod -Type $dbSettingType -Name "CreateAutoCountDefaultDBSetting" -ParameterCount 2
        $authenticateMethod = Find-XbAutoCountPublicStaticMethod -Type $userSessionType -Name "Authenticate" -ParameterCount 3
        $sessionConstructor = Find-XbAutoCountPublicConstructor -Type $userSessionType -ParameterTypeNames @("AutoCount.Data.DBSetting")
        $loginMethod = Find-XbAutoCountPublicInstanceMethod -Type $userSessionType -Name "Login" -ParameterCount 2
        $setAsCurrentMethod = Find-XbAutoCountPublicInstanceMethod -Type $userSessionType -Name "SetAsCurrent" -ParameterCount 0
        if ($null -eq $dbSettingFactory -or $null -eq $authenticateMethod -or
            $null -eq $sessionConstructor -or $null -eq $loginMethod) {
            throw "autocount_authentication_surface_missing"
        }

        $dbSetting = $dbSettingFactory.Invoke($null, @($server, $database))
        [void]$authenticateMethod.Invoke($null, @($dbSetting, $user, $password))
        $session = $sessionConstructor.Invoke(@($dbSetting))
        $loginOk = [bool]$loginMethod.Invoke($session, @($user, $password))
        if ($loginOk -and $null -ne $setAsCurrentMethod) {
            [void]$setAsCurrentMethod.Invoke($session, @())
        }
        if (-not $loginOk) { throw "autocount_authentication_failed" }

        [pscustomobject]@{
            UserSession = $session
            DBSetting = $dbSetting
            MemberCommandFactory = $null
            AssemblyResolveHandler = $assemblyResolveHandler
        }
    }
    catch {
        if ($null -ne $assemblyResolveHandler) {
            [System.AppDomain]::CurrentDomain.remove_AssemblyResolve($assemblyResolveHandler)
        }
        throw "autocount_session_initialization_failed"
    }
    finally {
        Remove-Variable password -ErrorAction SilentlyContinue
    }
}

function New-XbAutoCountSession {
    param(
        [Parameter(Mandatory)][switch]$EnableProductionAdapter,
        [scriptblock]$SessionFactory
    )
    if ($null -ne $SessionFactory) {
        Assert-XbAutoCountAdapterEnabled -EnableProductionAdapter:$EnableProductionAdapter
        $bindings = @(& $SessionFactory)
        if ($bindings.Count -ne 1) { throw "autocount_session_binding_invalid" }
        return (Convert-XbAutoCountSessionBinding -Binding $bindings[0])
    }

    Assert-XbAutoCountAdapterEnabled -EnableProductionAdapter:$EnableProductionAdapter -RequireAssemblyPath
    $factoryName = [Environment]::GetEnvironmentVariable("XB_AC2_SESSION_FACTORY", "Process")
    if (-not [string]::IsNullOrWhiteSpace($factoryName)) {
        $factoryCommand = Get-Command -Name $factoryName -CommandType Function -ErrorAction SilentlyContinue
        if ($null -eq $factoryCommand) { throw "autocount_session_factory_not_found" }
        $bindings = @(& $factoryCommand.Name)
        if ($bindings.Count -ne 1) { throw "autocount_session_binding_invalid" }
        return (Convert-XbAutoCountSessionBinding -Binding $bindings[0])
    }

    $assemblyRoot = [Environment]::GetEnvironmentVariable("XB_AC2_ASSEMBLY_PATH", "Process")
    return (Initialize-XbAutoCountReviewedSession -AssemblyRoot $assemblyRoot)
}

function Get-XbAutoCountMemberRow {
    param([AllowNull()][object]$Entity)
    if ($null -eq $Entity) { return $null }
    if ($Entity -is [System.Data.DataRow]) { return $Entity }

    $rowProperty = $Entity.PSObject.Properties["Row"]
    if ($null -ne $rowProperty -and $rowProperty.Value -is [System.Data.DataRow]) {
        return $rowProperty.Value
    }

    $tableProperty = $Entity.PSObject.Properties["MemberTable"]
    if ($null -ne $tableProperty -and $tableProperty.Value -is [System.Data.DataTable] -and
        $tableProperty.Value.Rows.Count -gt 0) {
        return $tableProperty.Value.Rows[0]
    }
    return $null
}

function Set-XbAutoCountMemberRowValue {
    param(
        [Parameter(Mandatory)][System.Data.DataRow]$Row,
        [Parameter(Mandatory)][string]$Field,
        [Parameter(Mandatory)][AllowNull()][object]$Value
    )
    if ($null -eq $Row.Table -or -not $Row.Table.Columns.Contains($Field)) {
        throw "autocount_member_field_missing"
    }
    if ($Row.Table.Columns[$Field].ReadOnly) { throw "autocount_member_field_read_only" }
    $Row[$Field] = $Value
}

function Get-XbAutoCountMemberRowValue {
    param(
        [AllowNull()][System.Data.DataRow]$Row,
        [Parameter(Mandatory)][string]$Field
    )
    if ($null -eq $Row -or $null -eq $Row.Table -or -not $Row.Table.Columns.Contains($Field)) {
        return $null
    }
    $value = $Row[$Field]
    if ($value -is [System.DBNull]) { return $null }
    return $value
}

function Get-XbAutoCountEntityValue {
    param(
        [Parameter(Mandatory)]$Entity,
        [Parameter(Mandatory)][string]$Field
    )
    if ($Entity -is [System.Collections.IDictionary] -and $Entity.Contains($Field)) {
        return $Entity[$Field]
    }
    $row = Get-XbAutoCountMemberRow -Entity $Entity
    if ($null -ne $row) {
        return (Get-XbAutoCountMemberRowValue -Row $row -Field $Field)
    }
    $property = $Entity.PSObject.Properties[$Field]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function Convert-XbAutoCountNormalizedValue {
    param(
        [AllowNull()][object]$Value,
        [string]$Field
    )
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return "" }
    if ($Field -in $script:XbAutoCountAdapterManagedFields) {
        if ($Value -is [bool]) { return $(if ($Value) { "T" } else { "F" }) }
        $text = ([string]$Value).Trim().ToLowerInvariant()
        if ($text -in @("t", "true", "yes", "y", "1")) { return "T" }
        if ($text -in @("f", "false", "no", "n", "0")) { return "F" }
        return $text.ToUpperInvariant()
    }
    if ($Value -is [datetime]) {
        return $Value.ToString("yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture)
    }
    if ($Value -is [datetimeoffset]) {
        return $Value.ToString("yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture)
    }
    if ($Value -is [bool]) { return $(if ($Value) { "T" } else { "F" }) }
    if ($Value -is [byte] -or $Value -is [int16] -or $Value -is [int32] -or
        $Value -is [int64] -or $Value -is [decimal] -or $Value -is [double] -or
        $Value -is [single]) {
        return ([System.Convert]::ToString($Value, [System.Globalization.CultureInfo]::InvariantCulture)).Trim()
    }
    return ([string]$Value).Trim()
}

function Convert-XbAutoCountNullableValue {
    # Null-preserving normalizer for the Shopify M1 profile: NULL/DBNull stay
    # $null so an empty string can never satisfy an expected NULL.
    param(
        [AllowNull()][object]$Value,
        [string]$Field
    )
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return $null }
    return (Convert-XbAutoCountNormalizedValue -Value $Value -Field $Field)
}

function Convert-XbAutoCountMemberRecord {
    param(
        [AllowNull()][object]$Entity,
        [ValidateSet("forms_v1", "shopify_m1")][string]$FieldProfile = "forms_v1"
    )
    if ($null -eq $Entity) { return $null }
    $record = [ordered]@{}
    foreach ($field in $script:XbAutoCountAssignedFields) {
        $raw = Get-XbAutoCountEntityValue -Entity $Entity -Field $field
        if ($FieldProfile -eq "shopify_m1") {
            $record[$field] = Convert-XbAutoCountNullableValue -Value $raw -Field $field
        } else {
            $record[$field] = Convert-XbAutoCountNormalizedValue -Value $raw -Field $field
        }
    }
    return $record
}

function Assert-XbAutoCountShopifyMemberInput {
    param([Parameter(Mandatory)][hashtable]$Member)
    foreach ($field in @($Member.Keys)) {
        if ($script:XbShopifyAdapterOwnedFields -contains $field) {
            throw "adapter_managed_defaults_are_not_caller_inputs"
        }
        if ($script:XbShopifyCallerFields -cnotcontains $field) { throw "member_field_not_allowed" }
    }
    foreach ($field in @("MemberNo", "Name", "RegisterDate", "ExpiryDate")) {
        if (-not $Member.Contains($field) -or $Member[$field] -isnot [string] -or [string]::IsNullOrWhiteSpace($Member[$field])) {
            throw "member_field_required"
        }
    }
    foreach ($field in @("MobilePhone", "EmailAddress")) {
        if ($Member.Contains($field) -and $null -ne $Member[$field]) {
            if ($Member[$field] -isnot [string] -or [string]::IsNullOrWhiteSpace($Member[$field])) {
                throw "empty_string_not_null_equivalent"
            }
        }
    }
    foreach ($field in @("RegisterDate", "ExpiryDate")) {
        [datetime]$parsed = [datetime]::MinValue
        if (-not [datetime]::TryParseExact([string]$Member[$field], "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::None, [ref]$parsed)) {
            throw "member_date_invalid"
        }
    }
    if ([string]::CompareOrdinal([string]$Member.ExpiryDate, [string]$Member.RegisterDate) -lt 0) { throw "member_expiry_before_register" }
}

function New-XbAutoCountShopifyExpectedRecord {
    # Exact expected AC2 record for a Shopify M1 create. Dates are the exact
    # Shopify dates (never recomputed); DOB is expected unassigned ($null).
    param(
        [Parameter(Mandatory)][string]$MemberNo,
        [Parameter(Mandatory)]$CreatePayload
    )
    $member = @{
        MemberNo = $MemberNo
        Name = [string]$CreatePayload.name
        RegisterDate = [string]$CreatePayload.register_date
        ExpiryDate = [string]$CreatePayload.expiry_date
    }
    if ($null -ne $CreatePayload.mobile_phone) { $member.MobilePhone = $CreatePayload.mobile_phone }
    if ($null -ne $CreatePayload.email_address) { $member.EmailAddress = $CreatePayload.email_address }
    Assert-XbAutoCountShopifyMemberInput -Member $member
    $expected = [ordered]@{}
    $expected.MemberNo = Convert-XbAutoCountNullableValue -Value $member.MemberNo -Field "MemberNo"
    $expected.MemberType = "Default"
    $expected.Name = Convert-XbAutoCountNullableValue -Value $member.Name -Field "Name"
    $expected.MobilePhone = if ($member.Contains("MobilePhone")) { Convert-XbAutoCountNullableValue -Value $member.MobilePhone -Field "MobilePhone" } else { $null }
    $expected.EmailAddress = if ($member.Contains("EmailAddress")) { Convert-XbAutoCountNullableValue -Value $member.EmailAddress -Field "EmailAddress" } else { $null }
    $expected.DOB = $null
    $expected.RegisterDate = $member.RegisterDate
    $expected.ExpiryDate = $member.ExpiryDate
    $expected.OpeningPoints = "0"
    $expected.IsActive = "T"
    $expected.Individual = "T"
    return $expected
}

function Get-XbAutoCountNextMemberNo {
    # Official MemberCommand.GetNextMemberNo(); never phone-derived.
    param(
        [Parameter(Mandatory)]$Session,
        [scriptblock]$MemberCommandFactory
    )
    try {
        $command = Get-XbAutoCountMemberCommand -Session $Session -MemberCommandFactory $MemberCommandFactory
        $value = $command.GetNextMemberNo()
    }
    catch { throw "member_no_generator_failed" }
    if ($value -isnot [string] -or $value -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._/-]{0,19}$') { throw "member_no_generator_failed" }
    return $value
}

function Test-XbAutoCountLegacyMemberCandidate {
    # Conservative duplicate evidence only: exact normalised phone against
    # legacy MemberNo and MobilePhone, exact normalised email. Name is never
    # consulted. Any lookup failure is LOOKUP_FAILED. Only booleans leave here.
    param(
        [Parameter(Mandatory)]$Session,
        [AllowNull()][string]$MobilePhone,
        [AllowNull()][string]$EmailAddress,
        [scriptblock]$MemberCommandFactory
    )
    $phoneMemberNoHit = $false
    $mobilePhoneHit = $false
    $emailHit = $false
    try {
        $command = Get-XbAutoCountMemberCommand -Session $Session -MemberCommandFactory $MemberCommandFactory
        $phoneInput = [string]$MobilePhone
        $phoneKey = if ([string]::IsNullOrWhiteSpace($phoneInput)) { $null } else { $phoneInput -replace '[^0-9]', '' }
        $emailKey = if ([string]::IsNullOrWhiteSpace($EmailAddress)) { $null } else { $EmailAddress.Trim().ToLowerInvariant() }
        if ($null -ne $phoneKey) {
            if ($phoneKey -cnotmatch '^[0-9]{6,15}$') { throw "legacy_lookup_input_invalid" }
            if ($null -ne $command.GetMember($phoneKey)) { $phoneMemberNoHit = $true }
        }
        if ($null -ne $phoneKey -or $null -ne $emailKey) {
            $table = $command.LoadBrowseTable()
            if ($null -eq $table -or -not ($table -is [System.Data.DataTable])) { throw "legacy_lookup_table_unavailable" }
            if (-not $table.Columns.Contains("MobilePhone") -or -not $table.Columns.Contains("EmailAddress")) { throw "legacy_lookup_columns_missing" }
            foreach ($row in $table.Rows) {
                if ($null -ne $phoneKey) {
                    $rowPhone = [string]$row["MobilePhone"]
                    $rowDigits = $rowPhone -replace '[^0-9]', ''
                    if ($rowDigits.Length -gt 0 -and $rowDigits -ceq $phoneKey) { $mobilePhoneHit = $true }
                }
                if ($null -ne $emailKey) {
                    $rowEmail = ([string]$row["EmailAddress"]).Trim().ToLowerInvariant()
                    if ($rowEmail.Length -gt 0 -and $rowEmail -ceq $emailKey) { $emailHit = $true }
                }
            }
        }
    }
    catch {
        return [pscustomobject]@{ outcome = "LOOKUP_FAILED"; phone_member_no_hit = $false; mobile_phone_hit = $false; email_hit = $false }
    }
    $hit = ($phoneMemberNoHit -or $mobilePhoneHit -or $emailHit)
    return [pscustomobject]@{
        outcome = if ($hit) { "CANDIDATE_FOUND" } else { "NO_CANDIDATE" }
        phone_member_no_hit = $phoneMemberNoHit
        mobile_phone_hit = $mobilePhoneHit
        email_hit = $emailHit
    }
}

function Get-XbAutoCountMemberCommand {
    param(
        [Parameter(Mandatory)]$Session,
        [scriptblock]$MemberCommandFactory
    )
    if ($null -eq $MemberCommandFactory) {
        $candidate = Get-XbAutoCountBindingValue -Binding $Session -Name "MemberCommandFactory"
        if ($candidate -is [scriptblock]) { $MemberCommandFactory = $candidate }
    }
    if ($null -ne $MemberCommandFactory) {
        $commands = @(& $MemberCommandFactory $Session)
        if ($commands.Count -ne 1 -or $null -eq $commands[0]) {
            throw "autocount_member_command_binding_invalid"
        }
        return $commands[0]
    }
    return [AutoCount.BonusPoint.Member.MemberCommand]::Create($Session.UserSession, $Session.DBSetting)
}

function Get-XbAutoCountMember {
    param(
        [Parameter(Mandatory)]$Session,
        [Parameter(Mandatory)][string]$MemberNo,
        [scriptblock]$MemberCommandFactory
    )
    if ([string]::IsNullOrWhiteSpace($MemberNo)) { throw "member_no_required" }
    $command = Get-XbAutoCountMemberCommand -Session $Session -MemberCommandFactory $MemberCommandFactory
    return $command.GetMember($MemberNo)
}

function New-XbAutoCountMember {
    param(
        [Parameter(Mandatory)]$Session,
        [Parameter(Mandatory)][hashtable]$Member,
        [Parameter(Mandatory)][switch]$EnableProductionAdapter,
        [scriptblock]$MemberCommandFactory,
        [ValidateSet("forms_v1", "shopify_m1")][string]$FieldProfile = "forms_v1"
    )
    Assert-XbAutoCountAdapterEnabled -EnableProductionAdapter:$EnableProductionAdapter
    if ([string]::IsNullOrWhiteSpace([string]$Member.MemberNo)) { throw "member_no_required" }

    if ($FieldProfile -eq "shopify_m1") { Assert-XbAutoCountShopifyMemberInput -Member $Member }
    foreach ($field in $Member.Keys) {
        if ($FieldProfile -eq "shopify_m1") { continue }
        if ($script:XbAutoCountAdapterManagedFields -contains $field) {
            throw "adapter_managed_defaults_are_not_caller_inputs"
        }
        if ($field -notin @(
            "MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress",
            "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints"
        )) {
            throw "member_field_not_allowed"
        }
    }

    $command = Get-XbAutoCountMemberCommand -Session $Session -MemberCommandFactory $MemberCommandFactory
    $entity = $command.NewMember($false)
    if ($null -eq $entity) { throw "autocount_new_member_failed" }
    $row = Get-XbAutoCountMemberRow -Entity $entity
    if ($null -eq $row) { throw "autocount_member_row_unavailable" }

    if ($FieldProfile -eq "shopify_m1") {
        # DOB is deliberately not assigned. Absent contact fields are DBNull.
        $assignments = [ordered]@{
            MemberNo      = [string]$Member.MemberNo
            MemberType    = "Default"
            Name          = [string]$Member.Name
            MobilePhone   = if ($Member.Contains("MobilePhone") -and $null -ne $Member.MobilePhone) { [string]$Member.MobilePhone } else { [System.DBNull]::Value }
            EmailAddress  = if ($Member.Contains("EmailAddress") -and $null -ne $Member.EmailAddress) { [string]$Member.EmailAddress } else { [System.DBNull]::Value }
            RegisterDate  = [datetime]::ParseExact(
                [string]$Member.RegisterDate, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture
            )
            ExpiryDate    = [datetime]::ParseExact(
                [string]$Member.ExpiryDate, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture
            )
            OpeningPoints = [decimal]0
            IsActive      = "T"
            Individual    = "T"
        }
        $effectiveExpected = [ordered]@{}
        foreach ($field in $script:XbAutoCountAssignedFields) {
            $effectiveExpected[$field] = if ($assignments.Contains($field)) { Convert-XbAutoCountNullableValue -Value $assignments[$field] -Field $field } else { $null }
        }
    } else {
        # These values are adapter-owned effective defaults, not source/customer inputs.
        $assignments = [ordered]@{
            MemberNo      = [string]$Member.MemberNo
            MemberType    = "Default"
            Name          = [string]$Member.Name
            MobilePhone   = [string]$Member.MobilePhone
            EmailAddress  = [string]$Member.EmailAddress
            DOB           = [datetime]::ParseExact(
                [string]$Member.DOB, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture
            )
            RegisterDate  = [datetime]::ParseExact(
                [string]$Member.RegisterDate, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture
            )
            ExpiryDate    = [datetime]::ParseExact(
                [string]$Member.ExpiryDate, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture
            )
            OpeningPoints = [decimal]0
            IsActive      = "T"
            Individual    = "T"
        }
        $effectiveExpected = [ordered]@{}
        foreach ($field in $script:XbAutoCountAssignedFields) {
            $effectiveExpected[$field] = Convert-XbAutoCountNormalizedValue -Value $assignments[$field] -Field $field
        }
    }
    foreach ($field in $assignments.Keys) {
        Set-XbAutoCountMemberRowValue -Row $row -Field $field -Value $assignments[$field]
    }

    $script:XbSaveMemberInvocationCount = 0
    $script:XbSaveMemberInvocationCount++
    if ($script:XbSaveMemberInvocationCount -ne 1) {
        throw "save_member_invocation_count_invalid"
    }
    # This is the sole irreversible member write call in the repository.
    $command.SaveMember($entity)

    $readbackEntity = $command.GetMember([string]$Member.MemberNo)
    $readbackFound = ($null -ne $readbackEntity)
    [pscustomobject]@{
        SaveInvocationCount = $script:XbSaveMemberInvocationCount
        Expected = $effectiveExpected
        ReadBackFound = $readbackFound
        ReadBack = if ($readbackFound) {
            Convert-XbAutoCountMemberRecord -Entity $readbackEntity -FieldProfile $FieldProfile
        } else {
            $null
        }
    }
}

function Compare-XbAutoCountMemberReadBack {
    param(
        [Parameter(Mandatory)][System.Collections.IDictionary]$Expected,
        [AllowNull()][object]$Actual,
        [ValidateSet("forms_v1", "shopify_m1")][string]$FieldProfile = "forms_v1"
    )
    if ($null -eq $Actual) {
        return [pscustomobject]@{
            Found = $false
            Match = $false
            Mismatches = @("record_absent")
        }
    }

    $actualValues = Convert-XbAutoCountMemberRecord -Entity $Actual -FieldProfile $FieldProfile
    $mismatches = @()
    foreach ($field in $script:XbAutoCountAssignedFields) {
        if (-not $Expected.Contains($field)) {
            $mismatches += $field
            continue
        }
        if ($FieldProfile -eq "shopify_m1") {
            $expectedValue = Convert-XbAutoCountNullableValue -Value $Expected[$field] -Field $field
            $actualValue = $actualValues[$field]
            # NULL equals only NULL; "" is a different value.
            if (($null -eq $expectedValue) -ne ($null -eq $actualValue)) { $mismatches += $field; continue }
            if ($null -eq $expectedValue) { continue }
        } else {
            $expectedValue = Convert-XbAutoCountNormalizedValue -Value $Expected[$field] -Field $field
            $actualValue = $actualValues[$field]
        }
        if ($expectedValue -cne $actualValue) { $mismatches += $field }
    }
    [pscustomobject]@{
        Found = $true
        Match = ($mismatches.Count -eq 0)
        Mismatches = $mismatches
    }
}
