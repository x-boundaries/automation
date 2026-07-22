[CmdletBinding()]
param(
    # The single immutable approved package. Loaded and validated once, in-process.
    [Parameter(Mandatory)][string]$PackagePath,
    # Committed fail-closed business confirmation config (default: repo config).
    [string]$BusinessConfigPath,
    # VM-owned state directory for the exclusive lock, write-intent and consumed markers.
    [Parameter(Mandatory)][string]$StateDir,
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$ServerName = $env:AC2_PROBE_SERVER_NAME,
    [string]$DatabaseName = $env:AC2_PROBE_DATABASE_NAME,
    [string]$UserId = $env:AC2_PROBE_USER_ID,
    [string]$PasswordEnvVar = "AC2_PROBE_PASSWORD",
    [switch]$AllowRootLogin,
    # Write-mode confirmations. ALL are required to attempt a real SaveMember.
    [switch]$EnableMemberCreateUat,
    [switch]$ConfirmAutoCountWrite,
    [switch]$ConfirmExactlyOneMember,
    [switch]$ConfirmDryRunPassed,
    [switch]$ConfirmNoExistingMemberUpdate,
    [string]$JsonOut
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir "member_create_uat_runner_lib.ps1")

if ([string]::IsNullOrWhiteSpace($BusinessConfigPath)) {
    $BusinessConfigPath = Join-Path (Split-Path -Parent $scriptDir) "config\member_create_uat_business_confirmation.json"
}

$forWrite = [bool]$EnableMemberCreateUat
$writeConfirmed = $EnableMemberCreateUat -and $ConfirmAutoCountWrite -and $ConfirmExactlyOneMember -and `
    $ConfirmDryRunPassed -and $ConfirmNoExistingMemberUpdate

$result = [ordered]@{
    mode                        = if ($forWrite) { "write" } else { "dry-run" }
    runtime_location            = "autocount_vm"
    terminal_code               = $null
    package_structural_valid    = $false
    package_fingerprint_problem = $false
    approval_not_expired        = $false
    write_confirmed             = [bool]$writeConfirmed
    business_confirmed          = $false
    lock_acquired               = $false
    already_consumed            = $false
    authentication_success      = $false
    member_command_found        = $false
    get_member_found            = $false
    member_exists_initial       = $false
    new_member_success          = $false
    assignment_success          = $false
    assigned_field_count        = 0
    expiry_date_assigned        = $false
    member_exists_recheck       = $false
    write_intent_recorded       = $false
    consumed_marker_written      = $false
    save_member_attempted       = $false
    save_member_confirmed       = $false
    save_outcome                = "not_attempted"
    readback_found              = $false
    readback_match              = $false
    masked_member_no            = $null
    operation_id                = $null
    source_record_id            = $null
    source_fingerprint          = $null
    error                       = $null
}

# --------------------------------------------------------------------------- #
# Reflection helpers (generic; no AutoCount-specific state).
# --------------------------------------------------------------------------- #
function Find-PublicStaticMethod { param([Type]$Type, [string]$Name, [int]$ParameterCount)
    $f = [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Static
    @($Type.GetMethods($f) | Where-Object { $_.Name -eq $Name -and $_.GetParameters().Count -eq $ParameterCount } | Select-Object -First 1)[0]
}
function Find-PublicStaticMethodByTypes { param([Type]$Type, [string]$Name, [string[]]$ParameterTypeNames)
    $f = [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Static
    foreach ($m in $Type.GetMethods($f)) {
        if ($m.Name -ne $Name) { continue }
        $p = @($m.GetParameters()); if ($p.Count -ne $ParameterTypeNames.Count) { continue }
        $ok = $true; for ($i = 0; $i -lt $p.Count; $i++) { if ($p[$i].ParameterType.FullName -ne $ParameterTypeNames[$i]) { $ok = $false; break } }
        if ($ok) { return $m }
    }
    return $null
}
function Find-PublicInstanceMethod { param([Type]$Type, [string]$Name, [int]$ParameterCount)
    $f = [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Instance
    @($Type.GetMethods($f) | Where-Object { $_.Name -eq $Name -and $_.GetParameters().Count -eq $ParameterCount } | Select-Object -First 1)[0]
}
function Find-PublicProperty { param([Type]$Type, [string]$Name)
    $Type.GetProperty($Name, [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Instance)
}
function Find-PublicConstructor { param([Type]$Type, [string[]]$ParameterTypeNames)
    foreach ($c in $Type.GetConstructors([System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Instance)) {
        $p = @($c.GetParameters()); if ($p.Count -ne $ParameterTypeNames.Count) { continue }
        $ok = $true; for ($i = 0; $i -lt $p.Count; $i++) { if ($p[$i].ParameterType.FullName -ne $ParameterTypeNames[$i]) { $ok = $false; break } }
        if ($ok) { return $c }
    }
    return $null
}
function Set-MemberRowValue { param([System.Data.DataRow]$Row, [string]$Field, [object]$Value)
    if ($null -eq $Row -or -not $Row.Table.Columns.Contains($Field)) { throw "Expected member field was not available." }
    if ($Row.Table.Columns[$Field].ReadOnly) { throw "Expected member field was read-only." }
    $Row[$Field] = $Value
}
function Get-RowStringValue { param([System.Data.DataRow]$Row, [string]$Field)
    if ($null -eq $Row -or -not $Row.Table.Columns.Contains($Field)) { return "" }
    $v = $Row[$Field]; if ($null -eq $v -or $v -eq [System.DBNull]::Value) { return "" }; [string]$v
}

# --------------------------------------------------------------------------- #
# The ONLY SaveMember call site. Invoked at most once, never in a retry loop.
# --------------------------------------------------------------------------- #
function Invoke-CreateUatSaveMemberOnce {
    param(
        [Parameter(Mandatory)]$SaveMemberMethod,
        [Parameter(Mandatory)]$MemberCommand,
        [Parameter(Mandatory)]$MemberEntity
    )
    [void]$SaveMemberMethod.Invoke($MemberCommand, @($MemberEntity))
}

function Write-CreateUatResult {
    param([string]$TerminalCode)
    $result.terminal_code = $TerminalCode
    $safe = $result | ConvertTo-Json -Depth 8
    if (-not [string]::IsNullOrWhiteSpace($JsonOut)) {
        $p = [System.IO.Path]::GetFullPath($JsonOut)
        $parent = [System.IO.Path]::GetDirectoryName($p)
        if (-not [string]::IsNullOrWhiteSpace($parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Set-Content -LiteralPath $p -Value $safe -Encoding UTF8
    }
    $safe
}

$lockStream = $null
$lockPath = Join-Path $StateDir "create_uat.lock"
$assemblyResolveHandler = $null

try {
    # ---- 1. Load and validate ONE immutable in-memory package snapshot. ----
    if (-not (Test-CreateUatSafePath -Path $PackagePath)) { throw "The package path is unsafe or a reparse point." }
    $packageRaw = Get-Content -LiteralPath $PackagePath -Raw -Encoding UTF8
    $package = $packageRaw | ConvertFrom-Json
    $validation = Test-CreateUatPackage -Package $package
    $result.package_structural_valid = $validation.Valid
    $result.package_fingerprint_problem = $validation.FingerprintProblem
    $result.operation_id = [string]$package.operation_id
    $result.source_record_id = [string]$package.source_record_id
    $result.source_fingerprint = [string]$package.source_fingerprint
    $result.masked_member_no = Get-CreateUatMaskedMemberNo -MemberNo ([string]$package.member_payload.MemberNo)

    if ($validation.FingerprintProblem) { return (Write-CreateUatResult "SOURCE_FINGERPRINT_MISMATCH") }
    if (-not $validation.Valid) { return (Write-CreateUatResult "FAILED_BEFORE_WRITE") }

    # ---- 2. Approval expiry (uses VM wall clock). ----
    $result.approval_not_expired = Test-CreateUatApprovalNotExpired -Package $package -NowUtc ([datetime]::UtcNow)
    if (-not $result.approval_not_expired) { return (Write-CreateUatResult "APPROVAL_INVALID") }

    # ---- 3. Write-mode confirmations and business gate: BEFORE any AutoCount load. ----
    if ($forWrite -and -not $writeConfirmed) { return (Write-CreateUatResult "WRITE_NOT_CONFIRMED") }

    $confirmations = $null
    if (Test-Path -LiteralPath $BusinessConfigPath) {
        try { $confirmations = (Get-Content -LiteralPath $BusinessConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json).confirmations } catch { $confirmations = $null }
    }
    $result.business_confirmed = Test-CreateUatBusinessConfirmed -Confirmations $confirmations
    if ($forWrite -and -not $result.business_confirmed) { return (Write-CreateUatResult "OPERATOR_CONFIG_REQUIRED") }

    # ---- 4. Exclusive execution lock (VM-owned). ----
    if (-not (Test-Path -LiteralPath $StateDir)) { throw "The VM state directory does not exist (operator setup prerequisite)." }
    $lockStream = New-CreateUatExclusiveLock -LockPath $lockPath
    $result.lock_acquired = ($null -ne $lockStream)
    if (-not $result.lock_acquired) { return (Write-CreateUatResult "EXECUTION_LOCKED") }

    # ---- 5. Single-use consumed marker check (VM-owned). ----
    $consumedPath = Join-Path $StateDir ("consumed_" + $result.source_record_id + ".marker")
    if (Test-Path -LiteralPath $consumedPath) { $result.already_consumed = $true; return (Write-CreateUatResult "PACKAGE_ALREADY_CONSUMED") }

    # ---- 6. Load AutoCount and authenticate (only now). ----
    $server = $ServerName; $database = $DatabaseName; $user = $UserId
    $password = [Environment]::GetEnvironmentVariable($PasswordEnvVar)
    foreach ($pair in @(@("ServerName", $server), @("DatabaseName", $database), @("UserId", $user), @("Password", $password))) {
        if ([string]::IsNullOrWhiteSpace($pair[1])) { throw "$($pair[0]) is required for the AutoCount connection." }
    }

    $acRootPath = [System.IO.Path]::GetFullPath($AcRoot)
    $assemblyResolveHandler = [System.ResolveEventHandler] {
        param($s, $e)
        $an = [System.Reflection.AssemblyName]::new($e.Name)
        $cand = Join-Path $acRootPath ($an.Name + ".dll")
        if (Test-Path -LiteralPath $cand -PathType Leaf) { return [System.Reflection.Assembly]::LoadFrom($cand) }
        return $null
    }
    [System.AppDomain]::CurrentDomain.add_AssemblyResolve($assemblyResolveHandler)

    $assemblyNames = @("AutoCount.dll", "AutoCount.Accounting.dll", "AutoCount.Invoicing.dll", "AutoCount.ImportExport.dll", "AutoCount.Tools.dll")
    foreach ($a in $assemblyNames) {
        $ap = Join-Path $acRootPath $a
        if (-not (Test-Path -LiteralPath $ap -PathType Leaf)) { throw "Required AutoCount assembly was not found." }
        [void][System.Reflection.Assembly]::LoadFrom($ap)
    }
    $coreAssembly = [System.Reflection.Assembly]::LoadFrom((Join-Path $acRootPath "AutoCount.dll"))
    $memberAssembly = [System.Reflection.Assembly]::LoadFrom((Join-Path $acRootPath "AutoCount.Invoicing.dll"))

    $dbSettingType = $coreAssembly.GetType("AutoCount.Data.DBSetting", $false, $false)
    $userSessionType = $coreAssembly.GetType("AutoCount.Authentication.UserSession", $false, $false)
    if ($null -eq $dbSettingType -or $null -eq $userSessionType) { throw "Core AutoCount types were not found." }

    $dbSettingFactory = Find-PublicStaticMethod $dbSettingType "CreateAutoCountDefaultDBSetting" 2
    $authenticateMethod = Find-PublicStaticMethod $userSessionType "Authenticate" 3
    $userSessionConstructor = Find-PublicConstructor $userSessionType @("AutoCount.Data.DBSetting")
    $instanceLoginMethod = Find-PublicInstanceMethod $userSessionType "Login" 2
    $setAsCurrentMethod = Find-PublicInstanceMethod $userSessionType "SetAsCurrent" 0
    $allowRootLoginProperty = Find-PublicProperty $userSessionType "AllowRootLogin"
    if ($null -eq $dbSettingFactory -or $null -eq $authenticateMethod -or $null -eq $userSessionConstructor -or $null -eq $instanceLoginMethod) {
        throw "Required AutoCount authentication members were not found."
    }

    $dbSetting = $dbSettingFactory.Invoke($null, @($server, $database))
    [void]$authenticateMethod.Invoke($null, @($dbSetting, $user, $password))
    $session = $userSessionConstructor.Invoke(@($dbSetting))
    if ($AllowRootLogin -and $null -ne $allowRootLoginProperty -and $allowRootLoginProperty.CanWrite) {
        $allowRootLoginProperty.SetValue($session, $true, $null)
    }
    $loginOk = [bool]$instanceLoginMethod.Invoke($session, @($user, $password))
    if ($loginOk -and $null -ne $setAsCurrentMethod) { [void]$setAsCurrentMethod.Invoke($session, @()) }
    $result.authentication_success = $loginOk
    if (-not $loginOk) { throw "AutoCount authentication failed." }

    Remove-Variable password -ErrorAction SilentlyContinue

    # ---- 7. MemberCommand + GetMember duplicate check. ----
    $memberCommandType = $memberAssembly.GetType("AutoCount.BonusPoint.Member.MemberCommand", $false, $false)
    if ($null -eq $memberCommandType) { throw "MemberCommand type was not found." }
    $result.member_command_found = $true
    $createMethod = Find-PublicStaticMethodByTypes $memberCommandType "Create" @("AutoCount.Authentication.UserSession", "AutoCount.Data.DBSetting")
    if ($null -eq $createMethod) { throw "MemberCommand factory was not found." }
    $memberCommand = $createMethod.Invoke($null, @($session, $dbSetting))

    $memberNo = [string]$package.member_payload.MemberNo
    $getMemberMethod = Find-PublicInstanceMethod $memberCommandType "GetMember" 1
    $result.get_member_found = ($null -ne $getMemberMethod)
    if ($null -eq $getMemberMethod) { throw "GetMember method was not found." }

    $existing = $getMemberMethod.Invoke($memberCommand, @($memberNo))
    $result.member_exists_initial = ($null -ne $existing)
    if ($result.member_exists_initial) { return (Write-CreateUatResult "BLOCKED_MEMBER_EXISTS") }

    # ---- 8. NewMember(false) + whitelisted assignment (ExpiryDate never assigned). ----
    $newMemberMethod = Find-PublicInstanceMethod $memberCommandType "NewMember" 1
    if ($null -eq $newMemberMethod) { throw "NewMember method was not found." }
    $memberEntity = $newMemberMethod.Invoke($memberCommand, @($false))
    if ($null -eq $memberEntity) { throw "NewMember returned no entity." }
    $result.new_member_success = $true

    $rowProperty = Find-PublicProperty $memberEntity.GetType() "Row"
    $memberTableProperty = Find-PublicProperty $memberEntity.GetType() "MemberTable"
    $memberRow = $null
    if ($null -ne $rowProperty -and $rowProperty.CanRead) { $memberRow = $rowProperty.GetValue($memberEntity, $null) }
    if ($null -eq $memberRow -and $null -ne $memberTableProperty) {
        $mt = $memberTableProperty.GetValue($memberEntity, $null)
        if ($mt -is [System.Data.DataTable] -and $mt.Rows.Count -gt 0) { $memberRow = $mt.Rows[0] }
    }
    if ($null -eq $memberRow) { throw "Member row was not available." }

    $isActive = Get-RowStringValue $memberRow "IsActive"; if ([string]::IsNullOrWhiteSpace($isActive)) { $isActive = "T" }
    $individual = Get-RowStringValue $memberRow "Individual"; if ([string]::IsNullOrWhiteSpace($individual)) { $individual = "T" }

    # Only whitelisted assignable fields. ExpiryDate is deliberately absent.
    $assignments = [ordered]@{
        MemberNo      = [string]$package.member_payload.MemberNo
        MemberType    = [string]$package.member_payload.MemberType
        Name          = [string]$package.member_payload.Name
        MobilePhone   = [string]$package.member_payload.MobilePhone
        EmailAddress  = [string]$package.member_payload.EmailAddress
        DOB           = [datetime]::ParseExact([string]$package.member_payload.DOB, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture)
        RegisterDate  = [datetime]::ParseExact([string]$package.member_payload.RegisterDate, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture)
        OpeningPoints = [decimal]0
        IsActive      = $isActive
        Individual    = $individual
    }
    foreach ($field in $assignments.Keys) {
        if ($script:CreateUatNeverAssignFields -contains $field) { throw "A forbidden field entered the assignment payload." }
        Set-MemberRowValue $memberRow $field $assignments[$field]
    }
    $result.assigned_field_count = $assignments.Count
    $result.expiry_date_assigned = $false
    $result.assignment_success = $true

    # ---- 9. Dry-run stops here: no SaveMember. ----
    if (-not $forWrite) { return (Write-CreateUatResult "DRY_RUN_VALIDATED") }

    # ---- 10. Write path: fresh duplicate recheck immediately before save. ----
    $recheck = $getMemberMethod.Invoke($memberCommand, @($memberNo))
    $result.member_exists_recheck = ($null -ne $recheck)
    if ($result.member_exists_recheck) { return (Write-CreateUatResult "BLOCKED_MEMBER_EXISTS") }

    $saveMemberMethod = $null
    foreach ($m in $memberCommandType.GetMethods([System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Instance)) {
        if ($m.Name -ne "SaveMember") { continue }
        $p = @($m.GetParameters())
        if ($p.Count -eq 1 -and $p[0].ParameterType.FullName -eq $memberEntity.GetType().FullName) { $saveMemberMethod = $m; break }
    }
    if ($null -eq $saveMemberMethod) { throw "SaveMember member entity method was not found." }

    # Durable write-intent marker, then mark consumed BEFORE entering the irreversible section.
    $writeIntentPath = Join-Path $StateDir ("write_intent_" + $result.operation_id + ".marker")
    $markerBody = ($result | ConvertTo-Json -Depth 8)
    Set-Content -LiteralPath $writeIntentPath -Value $markerBody -Encoding UTF8
    $result.write_intent_recorded = $true

    $consumedStream = [System.IO.File]::Open($consumedPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes("consumed operation_id=" + $result.operation_id)
        $consumedStream.Write($bytes, 0, $bytes.Length); $consumedStream.Flush()
    }
    finally { $consumedStream.Close(); $consumedStream.Dispose() }
    $result.consumed_marker_written = $true

    # ---- 11. The single irreversible SaveMember. No retry after this begins. ----
    $result.save_member_attempted = $true
    try {
        Invoke-CreateUatSaveMemberOnce -SaveMemberMethod $saveMemberMethod -MemberCommand $memberCommand -MemberEntity $memberEntity
        $result.save_member_confirmed = $true
        $result.save_outcome = "confirmed"
    }
    catch {
        # The call began; we cannot prove whether the write committed. Do not retry,
        # delete, or update. Require a separate read-only recovery check.
        $result.save_outcome = "uncertain"
        $result.error = [ordered]@{ phase = "save"; message = "Save outcome could not be confirmed." }
        return (Write-CreateUatResult "WRITE_OUTCOME_UNCERTAIN")
    }

    # ---- 12. Read-back and compare approved safe fields. ----
    $readback = $getMemberMethod.Invoke($memberCommand, @($memberNo))
    $result.readback_found = ($null -ne $readback)
    if (-not $result.readback_found) { return (Write-CreateUatResult "WRITE_OUTCOME_UNCERTAIN") }

    $rbRowProp = Find-PublicProperty $readback.GetType() "Row"
    $rbRow = $null
    if ($null -ne $rbRowProp -and $rbRowProp.CanRead) { $rbRow = $rbRowProp.GetValue($readback, $null) }
    $match = $true
    if ($null -eq $rbRow) { $match = $false }
    else {
        if ((Get-RowStringValue $rbRow "MemberNo") -ne [string]$package.member_payload.MemberNo) { $match = $false }
        if ((Get-RowStringValue $rbRow "Name") -ne [string]$package.member_payload.Name) { $match = $false }
        if ((Get-RowStringValue $rbRow "EmailAddress") -ne [string]$package.member_payload.EmailAddress) { $match = $false }
        if ((Get-RowStringValue $rbRow "MemberType") -ne [string]$package.member_payload.MemberType) { $match = $false }
    }
    $result.readback_match = $match
    if ($match) { return (Write-CreateUatResult "CREATED_VERIFIED") }
    return (Write-CreateUatResult "CREATED_READBACK_MISMATCH")
}
catch {
    if ($null -eq $result.terminal_code) {
        if ($result.save_member_attempted -and -not $result.save_member_confirmed) {
            $result.save_outcome = "uncertain"
            $result.error = [ordered]@{ phase = "post_attempt"; message = "Save outcome could not be confirmed." }
            $out = Write-CreateUatResult "WRITE_OUTCOME_UNCERTAIN"
        }
        else {
            $result.error = [ordered]@{ phase = "pre_write"; message = "Runner stopped before any write." }
            $out = Write-CreateUatResult "FAILED_BEFORE_WRITE"
        }
        $out
    }
}
finally {
    if ($null -ne $assemblyResolveHandler) { [System.AppDomain]::CurrentDomain.remove_AssemblyResolve($assemblyResolveHandler) }
    Remove-CreateUatExclusiveLock -LockStream $lockStream -LockPath $lockPath
    Remove-Variable password -ErrorAction SilentlyContinue
}
