# Official local AutoCount boundary for the member-write v2 primitive.
# This file is intentionally inert unless the caller supplies the explicit
# production adapter switch and a separately configured runtime session.
# It contains the only member write call site of the installable package
# (Invoke-XbAutoCountSaveMember); it has no row removal, row edit or bulk surface.

Set-StrictMode -Version Latest
$script:XbAutoCountAssignedFields = @(
    "MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress",
    "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual"
)
$script:XbAutoCountAdapterManagedFields = @("IsActive", "Individual")
# Columns the fixed parameterless probe read (MemberCommand.LoadBrowseTable)
# must expose (contract section 2.4); confirmed live by T-2/T-3.
$script:XbAutoCountProbeColumns = @(
    "MemberNo", "MobilePhone", "Name", "EmailAddress", "IsActive", "Guid", "CreatedUserID", "CreatedTime"
)
# The single named kernel mutex serialising probe -> save -> readback on AC2_VM.
$script:XbAc2MemberCreateMutexName = "Global\XB-AC2-MemberCreate"
# Installable release package (release identity input and installer membership).
$script:XbAc2ReleasePackageFiles = @(
    "ac2_member_create_primitive.ps1",
    "ac2_member_gateway_autocount_adapter.ps1",
    "ac2_member_gateway_worker.ps1",
    "ac2_member_gateway_worker_lib.ps1",
    "launch_ac2_member_gateway_worker.ps1",
    "test_ac2_member_gateway_autocount_dependencies.ps1"
)
$script:XbSaveMemberInvocationCount = 0

function Get-XbAc2FileSha256 {
    param([Parameter(Mandatory)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant() }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}

# Release identity (primitive.release_sha256): the lowercase SHA-256 hex of
# the ASCII text made of one line "<file name>:<lowercase sha256 of the file
# bytes>" plus LF per release package file, file names sorted ordinally. The
# installer records the same value in installation-manifest.json and the CI7
# verifier recomputes it from the installed bytes.
function Get-XbAc2ReleaseIdentityFromEntries {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Entries)
    $names = [string[]]@($Entries.Keys)
    [Array]::Sort($names, [StringComparer]::Ordinal)
    $builder = New-Object System.Text.StringBuilder
    foreach ($name in $names) {
        $digest = [string]$Entries[$name]
        if ($name -cnotmatch '^[A-Za-z0-9_.-]{1,120}$' -or $digest -cnotmatch '^[0-9a-f]{64}$') { throw "release_identity_entry_invalid" }
        [void]$builder.Append($name).Append(":").Append($digest).Append("`n")
    }
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes($builder.ToString())
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally { $algorithm.Dispose() }
}

function Get-XbAc2ReleaseIdentity {
    param([Parameter(Mandatory)][string]$PackageRoot)
    $entries = [ordered]@{}
    foreach ($name in $script:XbAc2ReleasePackageFiles) {
        $path = Join-Path $PackageRoot $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "release_identity_file_missing" }
        $entries[$name] = Get-XbAc2FileSha256 -Path $path
    }
    return (Get-XbAc2ReleaseIdentityFromEntries -Entries $entries)
}

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
        DatabaseName = [string](Get-XbAutoCountBindingValue -Binding $Binding -Name "DatabaseName")
        LoginUserId = [string](Get-XbAutoCountBindingValue -Binding $Binding -Name "LoginUserId")
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

        # DatabaseName and LoginUserId are the exact values this successful
        # authentication was bound to (book and integration-user binding).
        [pscustomobject]@{
            UserSession = $session
            DBSetting = $dbSetting
            MemberCommandFactory = $null
            AssemblyResolveHandler = $assemblyResolveHandler
            DatabaseName = [string]$database
            LoginUserId = [string]$user
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
        # Compare numbers by value, not by text: a SQL-backed readback may carry
        # the column scale ([decimal]0.00), which must equal the assigned 0.
        return ([decimal]$Value).ToString("0.#############################", [System.Globalization.CultureInfo]::InvariantCulture)
    }
    return ([string]$Value).Trim()
}

function Convert-XbAutoCountMemberRecord {
    param([AllowNull()][object]$Entity)
    if ($null -eq $Entity) { return $null }
    $record = [ordered]@{}
    foreach ($field in $script:XbAutoCountAssignedFields) {
        $record[$field] = Convert-XbAutoCountNormalizedValue -Value (Get-XbAutoCountEntityValue -Entity $Entity -Field $field) -Field $field
    }
    return $record
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

# Fixed, parameterless, read-only probe (contract 2.4): every Member row,
# active and inactive, projected to the eight probe columns only.
function Get-XbAutoCountMemberProbeRows {
    param(
        [Parameter(Mandatory)]$Session,
        [scriptblock]$MemberCommandFactory
    )
    $command = Get-XbAutoCountMemberCommand -Session $Session -MemberCommandFactory $MemberCommandFactory
    $table = $command.LoadBrowseTable()
    if ($null -eq $table -or $table -isnot [System.Data.DataTable]) { throw "probe_table_unavailable" }
    foreach ($column in $script:XbAutoCountProbeColumns) {
        if (-not $table.Columns.Contains($column)) { throw "probe_columns_missing" }
    }
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($row in $table.Rows) {
        $projected = [ordered]@{}
        foreach ($column in $script:XbAutoCountProbeColumns) {
            $projected[$column] = Get-XbAutoCountMemberRowValue -Row $row -Field $column
        }
        $rows.Add([pscustomobject]$projected)
    }
    return ,$rows.ToArray()
}

# The effective record the adapter writes for a member (adapter mapping).
# The caller supplies only non-managed fields; IsActive and Individual are
# adapter-owned effective defaults, not caller inputs.
function Get-XbAutoCountMemberAssignments {
    param([Parameter(Mandatory)][hashtable]$Member)
    if ([string]::IsNullOrWhiteSpace([string]$Member.MemberNo)) { throw "member_no_required" }
    foreach ($field in $Member.Keys) {
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
    return [ordered]@{
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
}

function Get-XbAutoCountExpectedMemberRecord {
    param([Parameter(Mandatory)][hashtable]$Member)
    $assignments = Get-XbAutoCountMemberAssignments -Member $Member
    $expected = [ordered]@{}
    foreach ($field in $script:XbAutoCountAssignedFields) {
        $expected[$field] = Convert-XbAutoCountNormalizedValue -Value $assignments[$field] -Field $field
    }
    return $expected
}

# NewMember(false) plus the adapter field fill. Nothing is written here.
function New-XbAutoCountMemberEntity {
    param(
        [Parameter(Mandatory)]$Session,
        [Parameter(Mandatory)][hashtable]$Member,
        [Parameter(Mandatory)][switch]$EnableProductionAdapter,
        [scriptblock]$MemberCommandFactory
    )
    Assert-XbAutoCountAdapterEnabled -EnableProductionAdapter:$EnableProductionAdapter
    $assignments = Get-XbAutoCountMemberAssignments -Member $Member
    $command = Get-XbAutoCountMemberCommand -Session $Session -MemberCommandFactory $MemberCommandFactory
    $entity = $command.NewMember($false)
    if ($null -eq $entity) { throw "autocount_new_member_failed" }
    $row = Get-XbAutoCountMemberRow -Entity $entity
    if ($null -eq $row) { throw "autocount_member_row_unavailable" }
    foreach ($field in $assignments.Keys) {
        Set-XbAutoCountMemberRowValue -Row $row -Field $field -Value $assignments[$field]
    }
    [pscustomobject]@{
        Command = $command
        Entity = $entity
        Expected = (Get-XbAutoCountExpectedMemberRecord -Member $Member)
    }
}

# The sole irreversible member write call site of the installable package.
# At most one invocation per process: a second call fails before the write.
# Returns $true when the call returned normally and $false when it threw. The
# exception text is never surfaced because it may carry personal data.
function Invoke-XbAutoCountSaveMember {
    param(
        [Parameter(Mandatory)]$Prepared,
        [Parameter(Mandatory)][switch]$EnableProductionAdapter
    )
    Assert-XbAutoCountAdapterEnabled -EnableProductionAdapter:$EnableProductionAdapter
    if ($script:XbSaveMemberInvocationCount -ne 0) { throw "save_member_invocation_count_invalid" }
    $script:XbSaveMemberInvocationCount = 1
    $command = $Prepared.Command
    $entity = $Prepared.Entity
    try {
        $command.SaveMember($entity)
        return $true
    }
    catch {
        return $false
    }
}

# Readback audit columns (contract 2.7 step 7). Guid is returned in the
# lowercase 8-4-4-4-12 form, or "" when absent or unparseable.
function Get-XbAutoCountMemberAudit {
    param([AllowNull()][object]$Entity)
    if ($null -eq $Entity) { return $null }
    $guidValue = Get-XbAutoCountEntityValue -Entity $Entity -Field "Guid"
    $guidText = ""
    if ($null -ne $guidValue -and $guidValue -isnot [System.DBNull]) {
        [Guid]$parsed = [Guid]::Empty
        if ([Guid]::TryParse(([string]$guidValue).Trim(), [ref]$parsed) -and $parsed -ne [Guid]::Empty) {
            $guidText = $parsed.ToString("D").ToLowerInvariant()
        }
    }
    $createdUser = Get-XbAutoCountEntityValue -Entity $Entity -Field "CreatedUserID"
    $createdTime = Get-XbAutoCountEntityValue -Entity $Entity -Field "CreatedTime"
    [pscustomobject]@{
        Guid = $guidText
        CreatedUserID = $(if ($null -eq $createdUser -or $createdUser -is [System.DBNull]) { "" } else { ([string]$createdUser).Trim() })
        CreatedTime = $(if ($createdTime -is [datetime]) { $createdTime } else { $null })
    }
}

function Compare-XbAutoCountMemberReadBack {
    param(
        [Parameter(Mandatory)][System.Collections.IDictionary]$Expected,
        [AllowNull()][object]$Actual
    )
    if ($null -eq $Actual) {
        return [pscustomobject]@{
            Found = $false
            Match = $false
            Mismatches = @("record_absent")
        }
    }

    $actualValues = Convert-XbAutoCountMemberRecord -Entity $Actual
    $mismatches = @()
    foreach ($field in $script:XbAutoCountAssignedFields) {
        if (-not $Expected.Contains($field)) {
            $mismatches += $field
            continue
        }
        $expectedValue = Convert-XbAutoCountNormalizedValue -Value $Expected[$field] -Field $field
        $actualValue = $actualValues[$field]
        if ($expectedValue -cne $actualValue) { $mismatches += $field }
    }
    [pscustomobject]@{
        Found = $true
        Match = ($mismatches.Count -eq 0)
        Mismatches = $mismatches
    }
}
