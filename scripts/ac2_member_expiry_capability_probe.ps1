[CmdletBinding()]
param(
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$ServerName = $env:AC2_PROBE_SERVER_NAME,
    [string]$DatabaseName = $env:AC2_PROBE_DATABASE_NAME,
    [string]$UserId = $env:AC2_PROBE_USER_ID,
    [string]$PasswordEnvVar = "AC2_PROBE_PASSWORD",
    # ALL of the following explicit switches are required before any AutoCount write.
    # Missing any one leaves the probe inactive: it refuses before loading AutoCount.
    [switch]$EnableExpiryCapabilityProbe,     # master enable
    [switch]$ConfirmSyntheticExpiryDateTest,  # this is the synthetic ExpiryDate capability test
    [switch]$ConfirmSingleSyntheticMember,    # exactly one new synthetic member
    [switch]$ConfirmAutoCountWrite,           # a real AutoCount write is intended
    [switch]$ConfirmDryRunPreflightPassed,    # the main runner dry-run/preflight already passed
    [switch]$ConfirmNoUpdateOrDelete,         # no update or delete of any member will occur
    [switch]$AllowRootLogin,
    [string]$JsonOut
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --------------------------------------------------------------------------- #
# What this probe is (and is NOT):
#
# This is a MANUAL, INACTIVE-BY-DEFAULT, operator-run synthetic capability probe. Its
# only purpose is to prove, on the AutoCount VM, that a single ExpiryDate value can be
# assigned to a new member and read back after SaveMember. It is NOT the permanent
# production member-intake workflow and it does NOT use any real, form-derived member.
#
# It uses hard-coded, clearly synthetic, non-personal test values only. It requires an
# explicit current-turn owner approval that names the AutoCount target (server and
# database) and exactly one synthetic record before it is ever run (see the runbook
# docs/autocount2-automation/member_expiry_capability_probe_runbook.md).
#
# It performs NO member update, NO delete, NO rollback, and NO automatic cleanup. The
# one synthetic member it may create will REMAIN in AutoCount and must be reviewed and
# removed manually by the owner. An uncertain save outcome is terminal and is never
# retried automatically.
# --------------------------------------------------------------------------- #

$script:SyntheticMemberNo = "XBEXPIRYPROBE01"
$script:SyntheticName = "XB EXPIRYDATE PROBE"
$script:SyntheticEmail = "xb.expirydate.probe@example.invalid"
$script:SyntheticDob = "2000-03-01"
$script:SyntheticRegisterDate = "2026-07-01"
$script:SyntheticExpiryDate = "2028-06-30"
$script:SyntheticNoteMarker = "XB_AUTOMATION_EXPIRYDATE_PROBE_SYNTHETIC"

$residualRecordNote = "No automatic member update, delete, rollback, or cleanup is performed. A synthetic member may remain in AutoCount and must be reviewed and removed manually by the owner. Search Bonus Point > Member Maintenance for Note marker $script:SyntheticNoteMarker."

$allConfirmed = $EnableExpiryCapabilityProbe -and $ConfirmSyntheticExpiryDateTest -and `
    $ConfirmSingleSyntheticMember -and $ConfirmAutoCountWrite -and `
    $ConfirmDryRunPreflightPassed -and $ConfirmNoUpdateOrDelete

if (-not $allConfirmed) {
    $refusal = [ordered]@{
        mode                          = "member-expiry-capability-probe"
        probe_enabled                 = [bool]$EnableExpiryCapabilityProbe
        confirm_synthetic_expiry_test = [bool]$ConfirmSyntheticExpiryDateTest
        confirm_single_synthetic      = [bool]$ConfirmSingleSyntheticMember
        confirm_auto_count_write      = [bool]$ConfirmAutoCountWrite
        confirm_dry_run_preflight     = [bool]$ConfirmDryRunPreflightPassed
        confirm_no_update_or_delete   = [bool]$ConfirmNoUpdateOrDelete
        save_member_attempted         = $false
        terminal_outcome              = "REFUSED"
        residual_record_note          = $residualRecordNote
        refused                       = $true
        message                       = "Explicit opt-in is required. Re-run with -EnableExpiryCapabilityProbe, -ConfirmSyntheticExpiryDateTest, -ConfirmSingleSyntheticMember, -ConfirmAutoCountWrite, -ConfirmDryRunPreflightPassed, and -ConfirmNoUpdateOrDelete to create exactly one synthetic member and verify ExpiryDate persistence. Owner approval naming the AutoCount target and exactly one synthetic record is required first."
    }
    $refusal | ConvertTo-Json -Depth 4
    throw "Refusing to run the AC2 synthetic ExpiryDate capability probe without all explicit write opt-ins."
}

Write-Warning "AC2 synthetic ExpiryDate capability probe is explicit opt-in and write-capable. It creates exactly one synthetic member and verifies ExpiryDate persistence. It never updates, deletes, rolls back, or cleans up; the synthetic member will remain for manual owner review."

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

# --------------------------------------------------------------------------- #
# Sanitisation: secrets and synthetic identifiers never appear in output.
# --------------------------------------------------------------------------- #
function Get-SanitizedMessage {
    param([object]$Message)
    $text = [string]$Message
    foreach ($secret in @($ServerName, $DatabaseName, $UserId, [Environment]::GetEnvironmentVariable($PasswordEnvVar))) {
        if (-not [string]::IsNullOrEmpty($secret)) { $text = $text.Replace($secret, "<redacted>") }
    }
    $text = [regex]::Replace($text, "(?i)(password|pwd|user id|uid|server|database)\s*=\s*[^;\s]+", '$1=<redacted>')
    $text = $text.Replace($script:SyntheticMemberNo, "<synthetic-member-no>")
    $text = $text.Replace($script:SyntheticEmail, "<synthetic-email>")
    $text = $text.Replace($script:SyntheticName, "<synthetic-name>")
    $text = $text.Replace($script:SyntheticNoteMarker, "<synthetic-marker>")
    return $text
}

function Get-ExceptionMessage {
    param([System.Exception]$Exception)
    $current = $Exception
    while ($null -ne $current.InnerException) { $current = $current.InnerException }
    return Get-SanitizedMessage $current.Message
}

function Get-MaskedMemberNo {
    param([string]$MemberNo)
    if ([string]::IsNullOrEmpty($MemberNo) -or $MemberNo.Length -le 2) { return "***" }
    $MemberNo.Substring(0, 2) + "***" + $MemberNo.Substring($MemberNo.Length - 1, 1)
}

function Get-NormalizedDateValue {
    # Normalise an AutoCount date-shaped value to yyyy-MM-dd (or "" for null/blank), so
    # the read-back comparison is representation-independent (DateTime vs string).
    param([AllowNull()]$Value)
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return "" }
    if ($Value -is [datetime]) { return $Value.ToString("yyyy-MM-dd") }
    $text = ([string]$Value).Trim()
    $parsed = [datetime]::MinValue
    if ([datetime]::TryParse($text, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::None, [ref]$parsed)) {
        return $parsed.ToString("yyyy-MM-dd")
    }
    return $text
}

# --------------------------------------------------------------------------- #
# Reflection helpers (generic; no AutoCount-specific state).
# --------------------------------------------------------------------------- #
function Find-PublicStaticMethod { param([Type]$Type, [string]$Name, [int]$ParameterCount)
    $f = [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Static
    @($Type.GetMethods($f) | Where-Object { $_.Name -eq $Name -and $_.GetParameters().Count -eq $ParameterCount } | Select-Object -First 1)[0]
}
function Find-PublicStaticMethodByParameterTypes { param([Type]$Type, [string]$Name, [string[]]$ParameterTypeNames)
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
function Get-RowRawValue { param([System.Data.DataRow]$Row, [string]$Field)
    if ($null -eq $Row -or -not $Row.Table.Columns.Contains($Field)) { return $null }
    $v = $Row[$Field]; if ($v -is [System.DBNull]) { return $null }; return $v
}
function Get-RowStringValue { param([System.Data.DataRow]$Row, [string]$Field)
    if ($null -eq $Row -or -not $Row.Table.Columns.Contains($Field)) { return "" }
    $v = $Row[$Field]; if ($null -eq $v -or $v -is [System.DBNull]) { return "" }; [string]$v
}
function Set-MemberRowValue { param([System.Data.DataRow]$Row, [string]$Field, [object]$Value)
    if ($null -eq $Row -or -not $Row.Table.Columns.Contains($Field)) { throw "Expected member field was not available." }
    if ($Row.Table.Columns[$Field].ReadOnly) { throw "Expected member field was read-only." }
    $Row[$Field] = $Value
}

# --------------------------------------------------------------------------- #
# The ONLY SaveMember call site. Invoked at most once, never inside a retry loop.
# There is no delete/update/rollback counterpart by design.
# --------------------------------------------------------------------------- #
function Invoke-ExpiryProbeSaveMemberOnce {
    param(
        [Parameter(Mandatory)]$SaveMemberMethod,
        [Parameter(Mandatory)]$MemberCommand,
        [Parameter(Mandatory)]$MemberEntity
    )
    [void]$SaveMemberMethod.Invoke($MemberCommand, @($MemberEntity))
}

$result = [ordered]@{
    mode                          = "member-expiry-capability-probe"
    probe_enabled                 = [bool]$EnableExpiryCapabilityProbe
    confirm_synthetic_expiry_test = [bool]$ConfirmSyntheticExpiryDateTest
    confirm_single_synthetic      = [bool]$ConfirmSingleSyntheticMember
    confirm_auto_count_write      = [bool]$ConfirmAutoCountWrite
    confirm_dry_run_preflight     = [bool]$ConfirmDryRunPreflightPassed
    confirm_no_update_or_delete   = [bool]$ConfirmNoUpdateOrDelete
    ac_root_exists                = $false
    required_assemblies_loaded    = $false
    authentication_success        = $false
    member_command_found          = $false
    get_member_found              = $false
    member_exists_initial         = $false
    new_member_success            = $false
    assignment_success            = $false
    expiry_date_assigned          = $false
    member_exists_recheck         = $false
    save_member_method_found      = $false
    save_member_attempted         = $false
    save_member_confirmed         = $false
    save_outcome                  = "not_attempted"
    readback_found                = $false
    expiry_date_expected          = $script:SyntheticExpiryDate
    expiry_date_readback_value    = $null
    expiry_date_readback_match    = $false
    masked_member_no              = Get-MaskedMemberNo $script:SyntheticMemberNo
    terminal_outcome              = $null
    synthetic_member_may_remain   = $false
    residual_record_note          = $residualRecordNote
    error                         = $null
}

$assemblyResolveHandler = [System.ResolveEventHandler] {
    param($s, $e)
    $an = [System.Reflection.AssemblyName]::new($e.Name)
    $cand = Join-Path $script:AcRootPath ($an.Name + ".dll")
    if (Test-Path -LiteralPath $cand -PathType Leaf) { return [System.Reflection.Assembly]::LoadFrom($cand) }
    return $null
}
[System.AppDomain]::CurrentDomain.add_AssemblyResolve($assemblyResolveHandler)

try {
    # ---- Connection inputs (from the AC2_PROBE_* environment; never printed). ----
    $serverForProbe = $ServerName; $databaseForProbe = $DatabaseName; $userForProbe = $UserId
    $passwordForProbe = [Environment]::GetEnvironmentVariable($PasswordEnvVar)
    foreach ($pair in @(@("ServerName", $serverForProbe), @("DatabaseName", $databaseForProbe), @("UserId", $userForProbe), @("Password", $passwordForProbe))) {
        if ([string]::IsNullOrWhiteSpace($pair[1])) { throw "$($pair[0]) is required for the AutoCount connection." }
    }

    $result.ac_root_exists = Test-Path -LiteralPath $script:AcRootPath -PathType Container
    if (-not $result.ac_root_exists) { throw "AC2 root path was not found." }
    foreach ($a in $assemblyNames) {
        $ap = Join-Path $script:AcRootPath $a
        if (-not (Test-Path -LiteralPath $ap -PathType Leaf)) { throw "Required AutoCount assembly was not found." }
        [void][System.Reflection.Assembly]::LoadFrom($ap)
    }
    $result.required_assemblies_loaded = $true
    $coreAssembly = [System.Reflection.Assembly]::LoadFrom((Join-Path $script:AcRootPath $coreAssemblyName))
    $memberAssembly = [System.Reflection.Assembly]::LoadFrom((Join-Path $script:AcRootPath $memberAssemblyName))

    $dbSettingType = $coreAssembly.GetType($dbSettingTypeName, $false, $false)
    $userSessionType = $coreAssembly.GetType($userSessionTypeName, $false, $false)
    if ($null -eq $dbSettingType -or $null -eq $userSessionType) { throw "Core AutoCount types were not found." }

    $dbSettingFactory = Find-PublicStaticMethod $dbSettingType "CreateAutoCountDefaultDBSetting" 2
    $authenticateMethod = Find-PublicStaticMethod $userSessionType "Authenticate" 3
    $userSessionConstructor = Find-PublicConstructor $userSessionType @($dbSettingTypeName)
    $instanceLoginMethod = Find-PublicInstanceMethod $userSessionType "Login" 2
    $setAsCurrentMethod = Find-PublicInstanceMethod $userSessionType "SetAsCurrent" 0
    $allowRootLoginProperty = Find-PublicProperty $userSessionType "AllowRootLogin"
    if ($null -eq $dbSettingFactory -or $null -eq $authenticateMethod -or $null -eq $userSessionConstructor -or $null -eq $instanceLoginMethod) {
        throw "Required AutoCount authentication members were not found."
    }

    $dbSetting = $dbSettingFactory.Invoke($null, @($serverForProbe, $databaseForProbe))
    [void]$authenticateMethod.Invoke($null, @($dbSetting, $userForProbe, $passwordForProbe))
    $session = $userSessionConstructor.Invoke(@($dbSetting))
    if ($AllowRootLogin -and $null -ne $allowRootLoginProperty -and $allowRootLoginProperty.CanWrite) {
        $allowRootLoginProperty.SetValue($session, $true, $null)
    }
    $loginOk = [bool]$instanceLoginMethod.Invoke($session, @($userForProbe, $passwordForProbe))
    if ($loginOk -and $null -ne $setAsCurrentMethod) { [void]$setAsCurrentMethod.Invoke($session, @()) }
    $result.authentication_success = $loginOk
    if (-not $loginOk) { throw "AutoCount authentication failed." }

    Remove-Variable passwordForProbe -ErrorAction SilentlyContinue

    # ---- MemberCommand + immediate duplicate check BEFORE constructing the entity. ----
    $memberCommandType = $memberAssembly.GetType($memberCommandTypeName, $false, $false)
    if ($null -eq $memberCommandType) { throw "MemberCommand type was not found." }
    $result.member_command_found = $true
    # MemberCommand.Create(UserSession, DBSetting) is the only command factory used here.
    $createMethod = Find-PublicStaticMethodByParameterTypes $memberCommandType "Create" @($userSessionTypeName, $dbSettingTypeName)
    if ($null -eq $createMethod) { throw "MemberCommand factory was not found." }
    $memberCommand = $createMethod.Invoke($null, @($session, $dbSetting))

    $getMemberMethod = Find-PublicInstanceMethod $memberCommandType "GetMember" 1
    $result.get_member_found = ($null -ne $getMemberMethod)
    if ($null -eq $getMemberMethod) { throw "GetMember method was not found." }

    $existing = $getMemberMethod.Invoke($memberCommand, @($script:SyntheticMemberNo))
    $result.member_exists_initial = ($null -ne $existing)
    if ($result.member_exists_initial) {
        # The synthetic member already exists (a prior probe run). Stop; never write,
        # update, or delete. The residual record must be reviewed manually.
        $result.synthetic_member_may_remain = $true
        $result.terminal_outcome = "BLOCKED_MEMBER_EXISTS"
        throw "Synthetic member already exists; refusing to write. Review/remove it manually."
    }

    # ---- NewMember(false) + narrow synthetic assignment, including ExpiryDate. ----
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

    $assignments = [ordered]@{
        MemberNo      = $script:SyntheticMemberNo
        MemberType    = "Default"
        Name          = $script:SyntheticName
        EmailAddress  = $script:SyntheticEmail
        DOB           = [datetime]::ParseExact($script:SyntheticDob, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture)
        RegisterDate  = [datetime]::ParseExact($script:SyntheticRegisterDate, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture)
        ExpiryDate    = [datetime]::ParseExact($script:SyntheticExpiryDate, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture)
        OpeningPoints = [decimal]0
        IsActive      = $isActive
        Individual    = $individual
        Note          = $script:SyntheticNoteMarker
    }
    foreach ($field in $assignments.Keys) { Set-MemberRowValue $memberRow $field $assignments[$field] }
    $result.assignment_success = $true
    $result.expiry_date_assigned = $true

    # ---- Fresh duplicate recheck immediately before the single save. ----
    $recheck = $getMemberMethod.Invoke($memberCommand, @($script:SyntheticMemberNo))
    $result.member_exists_recheck = ($null -ne $recheck)
    if ($result.member_exists_recheck) {
        $result.synthetic_member_may_remain = $true
        $result.terminal_outcome = "BLOCKED_MEMBER_EXISTS"
        throw "Synthetic member appeared on recheck; refusing to write."
    }

    $saveMemberMethod = $null
    foreach ($m in $memberCommandType.GetMethods([System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::Instance)) {
        if ($m.Name -ne "SaveMember") { continue }
        $p = @($m.GetParameters())
        if ($p.Count -eq 1 -and $p[0].ParameterType.FullName -eq $memberEntity.GetType().FullName) { $saveMemberMethod = $m; break }
    }
    $result.save_member_method_found = ($null -ne $saveMemberMethod)
    if ($null -eq $saveMemberMethod) { throw "SaveMember member entity method was not found." }

    # ---- The single irreversible SaveMember. No retry after this begins. ----
    # From this point a synthetic member may exist in AutoCount regardless of outcome.
    $result.synthetic_member_may_remain = $true
    $result.save_member_attempted = $true
    try {
        Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod $saveMemberMethod -MemberCommand $memberCommand -MemberEntity $memberEntity
        $result.save_member_confirmed = $true
        $result.save_outcome = "confirmed"
    }
    catch {
        # The call began; we cannot prove whether the write committed. This is terminal
        # and is never retried. Do not delete, update, or roll back anything.
        $result.save_outcome = "uncertain"
        $result.terminal_outcome = "SAVE_UNCERTAIN"
        $result.error = [ordered]@{ phase = "save"; message = "Save outcome could not be confirmed; not retried." }
        throw "SaveMember outcome uncertain; terminal, not retried."
    }

    # ---- GetMember read-back + explicit ExpiryDate normalisation/verification. ----
    $readback = $getMemberMethod.Invoke($memberCommand, @($script:SyntheticMemberNo))
    $result.readback_found = ($null -ne $readback)
    if (-not $result.readback_found) {
        $result.terminal_outcome = "READBACK_NOT_FOUND"
    }
    else {
        $rbRowProp = Find-PublicProperty $readback.GetType() "Row"
        $rbRow = $null
        if ($null -ne $rbRowProp -and $rbRowProp.CanRead) { $rbRow = $rbRowProp.GetValue($readback, $null) }
        $rbExpiryNormalized = Get-NormalizedDateValue (Get-RowRawValue $rbRow "ExpiryDate")
        $expectedNormalized = Get-NormalizedDateValue $script:SyntheticExpiryDate
        $result.expiry_date_readback_value = $rbExpiryNormalized
        $result.expiry_date_readback_match = ($rbExpiryNormalized -eq $expectedNormalized -and -not [string]::IsNullOrEmpty($rbExpiryNormalized))
        $result.terminal_outcome = if ($result.expiry_date_readback_match) { "EXPIRY_VERIFIED" } else { "EXPIRY_READBACK_MISMATCH" }
    }
}
catch {
    if ($null -eq $result.terminal_outcome) {
        if ($result.save_member_attempted -and -not $result.save_member_confirmed) {
            $result.save_outcome = "uncertain"
            $result.terminal_outcome = "SAVE_UNCERTAIN"
        }
        else {
            $result.terminal_outcome = "FAILED_BEFORE_WRITE"
        }
    }
    if ($null -eq $result.error) {
        $result.error = [ordered]@{
            type    = $_.Exception.GetType().FullName
            message = Get-ExceptionMessage $_.Exception
        }
    }
}
finally {
    [System.AppDomain]::CurrentDomain.remove_AssemblyResolve($assemblyResolveHandler)
    Remove-Variable passwordForProbe -ErrorAction SilentlyContinue
}

# Emit sanitised aggregate evidence only: no raw synthetic member number, name, or
# email is ever written to stdout or the JSON file.
$safeJson = $result | ConvertTo-Json -Depth 8
if (-not [string]::IsNullOrWhiteSpace($JsonOut)) {
    $jsonOutPath = [System.IO.Path]::GetFullPath($JsonOut)
    $jsonOutParent = [System.IO.Path]::GetDirectoryName($jsonOutPath)
    if (-not [string]::IsNullOrWhiteSpace($jsonOutParent)) { New-Item -ItemType Directory -Path $jsonOutParent -Force | Out-Null }
    Set-Content -LiteralPath $jsonOutPath -Value $safeJson -Encoding UTF8
    Write-Host "Wrote sanitized ExpiryDate capability probe JSON to $jsonOutPath"
}
$safeJson
