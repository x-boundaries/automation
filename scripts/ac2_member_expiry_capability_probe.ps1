[CmdletBinding()]
param(
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$ServerName = $env:AC2_PROBE_SERVER_NAME,
    [string]$DatabaseName = $env:AC2_PROBE_DATABASE_NAME,
    [string]$UserId = $env:AC2_PROBE_USER_ID,
    [string]$PasswordEnvVar = "AC2_PROBE_PASSWORD",
    # Opaque, non-secret identifier of the explicit current-turn owner approval (e.g. a
    # ticket or approval-record ID). It is copied verbatim into the durable evidence, so
    # it must NOT contain the server or database/account-book names (those live only in
    # the separate approval record); the target is bound to the evidence via the hashed
    # target_fingerprint instead. Never a credential. See the runbook.
    [string]$ApprovalReference,
    # Operator-provided private evidence directory (never in the repository). It holds
    # the permanent single-use attempt claim and the durable, non-overwriting result.
    [string]$StateDirectory,
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

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir "member_expiry_capability_probe_lib.ps1")

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
# Before the irreversible SaveMember it atomically creates a permanent single-use
# attempt claim in the operator-provided StateDirectory. The claim is both the
# concurrent-execution exclusion and the permanent no-retry boundary: it is never
# overwritten or deleted, so a crash after it is created makes every future invocation
# fail closed. There is no automatic claim removal or stale-claim recovery.
#
# It performs NO member update, NO delete, NO rollback, and NO automatic cleanup. The
# one synthetic member it may create will REMAIN in AutoCount and must be reviewed and
# removed manually by the owner. An uncertain save outcome is terminal and never
# retried; a confirmed save whose read-back fails is reported as
# WRITE_CONFIRMED_READBACK_FAILED, never as a pre-write failure.
# --------------------------------------------------------------------------- #

$script:SyntheticMemberNo = "XBEXPIRYPROBE01"
$script:SyntheticName = "XB EXPIRYDATE PROBE"
$script:SyntheticEmail = "xb.expirydate.probe@example.invalid"
$script:SyntheticDob = "2000-03-01"
$script:SyntheticRegisterDate = "2026-07-01"
$script:SyntheticExpiryDate = "2028-06-30"
$script:SyntheticNoteMarker = "XB_AUTOMATION_EXPIRYDATE_PROBE_SYNTHETIC"

$residualRecordNote = "No automatic member update, delete, rollback, or cleanup is performed, and the attempt claim is never removed. A synthetic member may remain in AutoCount and must be reviewed and removed manually by the owner. Search Bonus Point > Member Maintenance for Note marker $script:SyntheticNoteMarker."

$approvalReferenceRe = '^[A-Za-z0-9._:#/-]{3,120}$'

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

$operationId = "expop_" + [guid]::NewGuid().ToString("n")

$result = [ordered]@{
    schema_version                = "member_expiry_capability_probe/v1"
    mode                          = "member-expiry-capability-probe"
    operation_id                  = $operationId
    approval_reference            = $null
    executed_at_utc               = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    target_fingerprint            = $null
    synthetic_fingerprint         = $null
    attempt_fingerprint           = $null
    intended_expiry_date          = $script:SyntheticExpiryDate
    claim_basename                = $null
    result_basename               = $null
    activated                     = $false
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
    claim_created                 = $false
    claim_conflict                = $false
    save_member_method_found      = $false
    save_member_attempted         = $false
    save_member_confirmed         = $false
    save_outcome                  = "not_attempted"
    readback_found                = $false
    readback_error                = $null
    expiry_date_readback_value    = $null
    expiry_match                  = $false
    masked_member_no              = (Get-ExpiryProbeMaskedMemberNo -MemberNo $script:SyntheticMemberNo)
    synthetic_member_may_remain   = $false
    residual_record_note          = $residualRecordNote
    terminal_outcome              = $null
    evidence_persisted            = $true
    exit_code                     = 1
    error                         = $null
}

# --------------------------------------------------------------------------- #
# Finalisation: derive the terminal outcome from the accumulated flags (single
# source of truth), persist durable non-overwriting evidence, emit sanitised JSON,
# and return the truthful exit code. Never overwrites prior evidence.
# --------------------------------------------------------------------------- #
$script:ProbeExitCode = 1

function Complete-ExpiryProbeRun {
    # Derive the terminal outcome from the accumulated flags, persist durable
    # non-overwriting evidence, emit the sanitised JSON to stdout, and record the
    # truthful exit code in $script:ProbeExitCode (the caller exits on it, so this
    # function's only pipeline output is the JSON). If durable evidence is required but
    # cannot be written, the run does NOT report success: exit is forced nonzero and
    # evidence_persisted is false, so a verified capability with no retained audit
    # artefact can never be mistaken for a clean pass.
    param([switch]$DurableEvidence, [string]$ResultPath)
    $result.terminal_outcome = Get-ExpiryProbeTerminalOutcome -Flags $result
    if ($DurableEvidence -and -not [string]::IsNullOrWhiteSpace($ResultPath)) {
        $result.evidence_persisted = $true
        $result.exit_code = Get-ExpiryProbeExitCode -TerminalOutcome $result.terminal_outcome -EvidencePersisted $true
        $content = $result | ConvertTo-Json -Depth 8
        try {
            Write-ExpiryProbeResultAtomic -Path $ResultPath -Content $content
        }
        catch {
            $result.evidence_persisted = $false
            if ($null -eq $result.error) { $result.error = [ordered]@{ phase = "evidence"; message = (Get-SanitizedMessage $_.Exception.Message) } }
            $result.exit_code = Get-ExpiryProbeExitCode -TerminalOutcome $result.terminal_outcome -EvidencePersisted $false
        }
    }
    else {
        $result.exit_code = Get-ExpiryProbeExitCode -TerminalOutcome $result.terminal_outcome -EvidencePersisted $true
    }
    $script:ProbeExitCode = $result.exit_code
    $safeJson = $result | ConvertTo-Json -Depth 8
    if (-not [string]::IsNullOrWhiteSpace($JsonOut)) {
        # Optional secondary sanitised copy; also non-overwriting to protect audit evidence.
        try {
            $p = [System.IO.Path]::GetFullPath($JsonOut)
            $parent = [System.IO.Path]::GetDirectoryName($p)
            if (-not [string]::IsNullOrWhiteSpace($parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
            New-ExpiryProbeDurableArtifact -Path $p -Content $safeJson
        }
        catch { }
    }
    $safeJson
}

# ---- Inactive by default: refuse before anything else when a switch is missing. ----
$allConfirmed = $EnableExpiryCapabilityProbe -and $ConfirmSyntheticExpiryDateTest -and `
    $ConfirmSingleSyntheticMember -and $ConfirmAutoCountWrite -and `
    $ConfirmDryRunPreflightPassed -and $ConfirmNoUpdateOrDelete

if (-not $allConfirmed) {
    # Advisory to stderr only; stdout is reserved for the sanitised JSON contract.
    [Console]::Error.WriteLine("AC2 synthetic ExpiryDate capability probe refused: all explicit write switches are required.")
    Complete-ExpiryProbeRun
    exit $script:ProbeExitCode
}
$result.activated = $true
$result.approval_reference = $ApprovalReference

# Advisory to stderr only; stdout is reserved for the sanitised JSON contract.
[Console]::Error.WriteLine("AC2 synthetic ExpiryDate capability probe is explicit opt-in and write-capable. It creates exactly one synthetic member and verifies ExpiryDate persistence. It never updates, deletes, rolls back, or cleans up; the synthetic member will remain for manual owner review.")

$assemblyResolveHandler = $null
$claimPath = $null
$resultPath = $null
$stateReady = $false

try {
    # ---- Bind evidence to approval + target (before any AutoCount contact). ----
    if ([string]::IsNullOrWhiteSpace($ApprovalReference) -or $ApprovalReference -notmatch $approvalReferenceRe) {
        throw "A valid non-secret -ApprovalReference is required (matching $approvalReferenceRe)."
    }
    if (-not (Test-ExpiryProbeSafePath -Path $StateDirectory)) {
        throw "A safe absolute -StateDirectory is required."
    }
    if (-not (Test-Path -LiteralPath $StateDirectory -PathType Container)) {
        throw "The -StateDirectory does not exist (operator setup prerequisite; the probe never creates it)."
    }
    foreach ($pair in @(@("ServerName", $ServerName), @("DatabaseName", $DatabaseName))) {
        if ([string]::IsNullOrWhiteSpace($pair[1])) { throw "$($pair[0]) is required to bind the capability evidence to the target." }
    }

    $targetFingerprint = Get-ExpiryProbeTargetFingerprint -ServerName $ServerName -DatabaseName $DatabaseName
    $syntheticFingerprint = Get-ExpiryProbeSyntheticFingerprint -MemberNo $script:SyntheticMemberNo
    $attemptFingerprint = Get-ExpiryProbeAttemptFingerprint -TargetFingerprint $targetFingerprint -SyntheticFingerprint $syntheticFingerprint -IntendedExpiry $script:SyntheticExpiryDate
    $result.target_fingerprint = $targetFingerprint
    $result.synthetic_fingerprint = $syntheticFingerprint
    $result.attempt_fingerprint = $attemptFingerprint

    $claimPath = Join-Path $StateDirectory ("expiry_probe_claim_" + $attemptFingerprint + ".claim")
    $resultPath = Join-Path $StateDirectory ("expiry_probe_result_" + $operationId + ".json")
    $result.claim_basename = [System.IO.Path]::GetFileName($claimPath)
    $result.result_basename = [System.IO.Path]::GetFileName($resultPath)
    $stateReady = $true

    # ---- Permanent single-use claim: refuse before AutoCount if it already exists. ----
    if (Test-Path -LiteralPath $claimPath) {
        $result.claim_conflict = $true
        $result.synthetic_member_may_remain = $true
        throw "A permanent attempt claim already exists for this target/record; refusing (no retry, no AutoCount contact)."
    }
    if (Test-Path -LiteralPath $resultPath) {
        throw "A prior result artefact already exists for this operation id; refusing to overwrite evidence."
    }

    # ---- AutoCount connection inputs (from the AC2_PROBE_* environment; never printed). ----
    $userForProbe = $UserId
    $passwordForProbe = [Environment]::GetEnvironmentVariable($PasswordEnvVar)
    foreach ($pair in @(@("UserId", $userForProbe), @("Password", $passwordForProbe))) {
        if ([string]::IsNullOrWhiteSpace($pair[1])) { throw "$($pair[0]) is required for the AutoCount connection." }
    }

    $script:AcRootPath = [System.IO.Path]::GetFullPath($AcRoot)
    $result.ac_root_exists = Test-Path -LiteralPath $script:AcRootPath -PathType Container
    if (-not $result.ac_root_exists) { throw "AC2 root path was not found." }

    $assemblyResolveHandler = [System.ResolveEventHandler] {
        param($s, $e)
        $an = [System.Reflection.AssemblyName]::new($e.Name)
        $cand = Join-Path $script:AcRootPath ($an.Name + ".dll")
        if (Test-Path -LiteralPath $cand -PathType Leaf) { return [System.Reflection.Assembly]::LoadFrom($cand) }
        return $null
    }
    [System.AppDomain]::CurrentDomain.add_AssemblyResolve($assemblyResolveHandler)

    $assemblyNames = @("AutoCount.dll", "AutoCount.Accounting.dll", "AutoCount.Invoicing.dll", "AutoCount.ImportExport.dll", "AutoCount.Tools.dll")
    foreach ($a in $assemblyNames) {
        $ap = Join-Path $script:AcRootPath $a
        if (-not (Test-Path -LiteralPath $ap -PathType Leaf)) { throw "Required AutoCount assembly was not found." }
        [void][System.Reflection.Assembly]::LoadFrom($ap)
    }
    $result.required_assemblies_loaded = $true
    $coreAssembly = [System.Reflection.Assembly]::LoadFrom((Join-Path $script:AcRootPath "AutoCount.dll"))
    $memberAssembly = [System.Reflection.Assembly]::LoadFrom((Join-Path $script:AcRootPath "AutoCount.Invoicing.dll"))

    $dbSettingTypeName = "AutoCount.Data.DBSetting"
    $userSessionTypeName = "AutoCount.Authentication.UserSession"
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

    $dbSetting = $dbSettingFactory.Invoke($null, @($ServerName, $DatabaseName))
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
    $memberCommandType = $memberAssembly.GetType("AutoCount.BonusPoint.Member.MemberCommand", $false, $false)
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
        $result.synthetic_member_may_remain = $true
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

    # ---- Fresh duplicate recheck immediately before claiming + saving. ----
    $recheck = $getMemberMethod.Invoke($memberCommand, @($script:SyntheticMemberNo))
    $result.member_exists_recheck = ($null -ne $recheck)
    if ($result.member_exists_recheck) {
        $result.synthetic_member_may_remain = $true
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

    # ---- Atomically claim the write (concurrency exclusion + permanent no-retry). ----
    $claimContent = New-ExpiryProbeClaimContent -OperationId $operationId -ApprovalReference $ApprovalReference `
        -TargetFingerprint $targetFingerprint -SyntheticFingerprint $syntheticFingerprint `
        -AttemptFingerprint $attemptFingerprint -IntendedExpiry $script:SyntheticExpiryDate
    try {
        New-ExpiryProbeDurableArtifact -Path $claimPath -Content $claimContent
        $result.claim_created = $true
    }
    catch {
        # Fail closed without any save. If a claim now exists, another launch won the
        # race (or a prior claim exists) -> ATTEMPT_ALREADY_CLAIMED. Otherwise the claim
        # could not be durably created (e.g. a flush that cannot confirm durability
        # propagated), which is a pre-write failure, not a conflict.
        if (Test-Path -LiteralPath $claimPath) {
            $result.claim_conflict = $true
            $result.synthetic_member_may_remain = $true
            throw "A permanent attempt claim already exists (concurrent or prior); refusing to write."
        }
        throw "The attempt claim could not be durably created; refusing to write before any save."
    }

    # ---- The single irreversible SaveMember. No retry after this begins. ----
    $result.synthetic_member_may_remain = $true
    $result.save_member_attempted = $true
    try {
        Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod $saveMemberMethod -MemberCommand $memberCommand -MemberEntity $memberEntity
        $result.save_member_confirmed = $true
        $result.save_outcome = "confirmed"
    }
    catch {
        # The call began; we cannot prove whether the write committed. Terminal, never
        # retried. Do not delete, update, or roll back anything.
        $result.save_outcome = "uncertain"
        $result.error = [ordered]@{ phase = "save"; message = "Save outcome could not be confirmed; not retried." }
    }

    # ---- Read-back is isolated: a failure here NEVER downgrades a confirmed save. ----
    if ($result.save_outcome -eq "confirmed") {
        try {
            $readback = $getMemberMethod.Invoke($memberCommand, @($script:SyntheticMemberNo))
            $result.readback_found = ($null -ne $readback)
            if ($result.readback_found) {
                $rbRowProp = Find-PublicProperty $readback.GetType() "Row"
                $rbRow = $null
                if ($null -ne $rbRowProp -and $rbRowProp.CanRead) { $rbRow = $rbRowProp.GetValue($readback, $null) }
                $rbExpiryNormalized = Get-ExpiryProbeNormalizedDate (Get-RowRawValue $rbRow "ExpiryDate")
                $expectedNormalized = Get-ExpiryProbeNormalizedDate $script:SyntheticExpiryDate
                $result.expiry_date_readback_value = $rbExpiryNormalized
                $result.expiry_match = ($rbExpiryNormalized -eq $expectedNormalized -and -not [string]::IsNullOrEmpty($rbExpiryNormalized))
            }
        }
        catch {
            # Save is CONFIRMED; only the read-back failed. Preserve a sanitised reason.
            $result.readback_found = $false
            $result.readback_error = Get-SanitizedMessage $_.Exception.Message
        }
    }
}
catch {
    # This handler is only reached for pre-save failures, an uncertain save, or a claim
    # conflict. The terminal outcome is derived from flags in Complete-ExpiryProbeRun,
    # so a confirmed-save-with-failed-read-back is NEVER classified here.
    if ($null -eq $result.error) {
        $result.error = [ordered]@{
            type    = $_.Exception.GetType().FullName
            message = Get-ExceptionMessage $_.Exception
        }
    }
}
finally {
    if ($null -ne $assemblyResolveHandler) { [System.AppDomain]::CurrentDomain.remove_AssemblyResolve($assemblyResolveHandler) }
    Remove-Variable passwordForProbe -ErrorAction SilentlyContinue
}

# Persist durable, non-overwriting evidence (when the state directory was resolved),
# emit sanitised JSON, and exit with the truthful code. Cleanup already ran in finally.
Complete-ExpiryProbeRun -DurableEvidence:$stateReady -ResultPath $resultPath
exit $script:ProbeExitCode
