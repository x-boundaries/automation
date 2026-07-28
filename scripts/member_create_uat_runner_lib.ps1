# Pure, dot-sourceable state-machine library for the single-member creation UAT.
#
# This library contains NO AutoCount calls, no network, and no live action. It is
# dot-sourced by scripts/ac2_member_create_uat_runner.ps1 (which owns the AutoCount
# reflection and the single SaveMember call site) and is exercised directly by the
# tests without AutoCount. Every function here is deterministic given its inputs.
#
# The canonical JSON serializer below reproduces, byte for byte, Python's
# json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=True) so the VM
# runner recomputes the same payload_hash that the laptop builder wrote. The two
# implementations are cross-checked by an automated test.

Set-StrictMode -Version Latest

# Bumped v1 -> v2: the package payload shape changed (member_payload and
# assignable_fields now include ExpiryDate). A v1 package built under the previous
# contract is refused fail-closed by the schema_version check below.
$script:CreateUatSchemaVersion = "member_create_uat_package/v2"
$script:CreateUatAssignableFields = @(
    "MemberNo", "Name", "EmailAddress", "MobilePhone", "DOB", "MemberType", "RegisterDate", "ExpiryDate", "OpeningPoints"
)
# ExpiryDate assignment/persistence is proven (synthetic capability probe), so it is now
# an active assignable field and the never-assign set is empty.
$script:CreateUatNeverAssignFields = @()
$script:CreateUatBusinessConfirmationRequired = @("MemberType", "RegisterDate", "ExpiryDate", "OpeningPoints")
$script:CreateUatDesiredBusinessFields = @("MemberType", "RegisterDate", "ExpiryDate", "OpeningPoints")
# Intended business-desired values; must match config/member_create_uat_contract.py.
$script:CreateUatIntended = @{
    MemberType    = "Default"
    RegisterDate  = "2026-07-01"
    ExpiryDate    = "2028-06-30"
    OpeningPoints = 0
}
# Active ExpiryDate assignment/read-back contract; mirrors member_create_uat_contract.py.
# ExpiryDate is now part of BOTH the intended assignment/read-back contract AND the active
# $script:CreateUatAssignableFields whitelist, because the capability flag
# $script:CreateUatExpiryDateAssignmentImplemented (defined below) is now $true after the
# synthetic proof. The invariant keeps the active/intended relationship explicit.
$script:CreateUatExpiryDateIntendedValue = "2028-06-30"
$script:CreateUatIntendedAssignmentFields = $script:CreateUatAssignableFields + $script:CreateUatNeverAssignFields
$script:CreateUatReadbackVerificationFields = $script:CreateUatIntendedAssignmentFields
# Runner-managed activation fields assigned in addition to the package whitelist, and the
# derived expected assigned-field count (9 assignable + 2 activation = 11). Kept as an
# explicit surface so the count is derived, not a hard-coded literal.
$script:CreateUatRunnerActivationFields = @("IsActive", "Individual")
$script:CreateUatExpectedAssignedFieldCount = $script:CreateUatAssignableFields.Count + $script:CreateUatRunnerActivationFields.Count
$script:CreateUatDateRe = '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$'
$script:CreateUatTimestampRe = '^[0-9T:+.Z-]{1,64}$'

# --------------------------------------------------------------------------- #
# Hashing and canonical JSON
# --------------------------------------------------------------------------- #
function Get-Sha256Hex {
    param([Parameter(Mandatory)][string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        $hash = $sha.ComputeHash($bytes)
    }
    finally {
        $sha.Dispose()
    }
    -join ($hash | ForEach-Object { $_.ToString("x2") })
}

# --------------------------------------------------------------------------- #
# No-date-coercion JSON parsing.
#
# Windows PowerShell 5.1 ConvertFrom-Json keeps ISO-date-shaped strings (DOB,
# RegisterDate, timestamps) as strings, but PowerShell 7 ConvertFrom-Json coerces
# them to [datetime], which would corrupt the payload_hash and every string field
# check. We parse without any date coercion on both runtimes and return ordered
# dictionaries and arrays of primitives, so the canonical serializer and field
# checks see exactly the original strings.
# --------------------------------------------------------------------------- #
function ConvertFrom-CreateUatJsonElement {
    param($Element)
    switch ($Element.ValueKind.ToString()) {
        'Object' {
            $o = [ordered]@{}
            foreach ($p in $Element.EnumerateObject()) { $o[$p.Name] = ConvertFrom-CreateUatJsonElement $p.Value }
            return $o
        }
        'Array' {
            $a = [System.Collections.Generic.List[object]]::new()
            foreach ($item in $Element.EnumerateArray()) { [void]$a.Add((ConvertFrom-CreateUatJsonElement $item)) }
            return , $a.ToArray()
        }
        'String' { return $Element.GetString() }
        'Number' {
            $l = [long]0
            if ($Element.TryGetInt64([ref]$l)) { return $l }
            return $Element.GetDouble()
        }
        'True' { return $true }
        'False' { return $false }
        default { return $null }
    }
}

function ConvertFrom-CreateUatLegacyJson {
    param($Value)
    if ($Value -is [System.Collections.IDictionary]) {
        $o = [ordered]@{}
        foreach ($k in $Value.Keys) { $o[[string]$k] = ConvertFrom-CreateUatLegacyJson $Value[$k] }
        return $o
    }
    if ($Value -isnot [string] -and $Value -is [System.Collections.IEnumerable]) {
        $a = [System.Collections.Generic.List[object]]::new()
        foreach ($item in $Value) { [void]$a.Add((ConvertFrom-CreateUatLegacyJson $item)) }
        return , $a.ToArray()
    }
    if ($Value -is [decimal] -or $Value -is [double]) {
        if ($Value -eq [math]::Truncate([double]$Value)) { return [long]$Value }
    }
    return $Value
}

function ConvertFrom-CreateUatJson {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Raw)
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        $doc = [System.Text.Json.JsonDocument]::Parse($Raw)
        try { return (ConvertFrom-CreateUatJsonElement $doc.RootElement) }
        finally { $doc.Dispose() }
    }
    Add-Type -AssemblyName System.Web.Extensions
    $js = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $js.MaxJsonLength = [int]::MaxValue
    return (ConvertFrom-CreateUatLegacyJson ($js.DeserializeObject($Raw)))
}

function Get-CreateUatMemberNames {
    param($Obj)
    if ($Obj -is [System.Collections.IDictionary]) { return @($Obj.Keys) }
    return @($Obj.PSObject.Properties.Name)
}

function ConvertTo-CreateUatJsonString {
    # Escapes a string exactly like Python json.dumps(ensure_ascii=True).
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    foreach ($ch in $Value.ToCharArray()) {
        $code = [int][char]$ch
        switch ($code) {
            0x22 { [void]$builder.Append('\"'); continue }
            0x5C { [void]$builder.Append('\\'); continue }
            0x08 { [void]$builder.Append('\b'); continue }
            0x09 { [void]$builder.Append('\t'); continue }
            0x0A { [void]$builder.Append('\n'); continue }
            0x0C { [void]$builder.Append('\f'); continue }
            0x0D { [void]$builder.Append('\r'); continue }
            default {
                if ($code -lt 0x20 -or $code -ge 0x7F) {
                    [void]$builder.Append('\u')
                    [void]$builder.Append($code.ToString("x4"))
                }
                else {
                    [void]$builder.Append($ch)
                }
            }
        }
    }
    [void]$builder.Append('"')
    $builder.ToString()
}

function Get-CreateUatCanonicalJson {
    # Deterministic canonical JSON matching Python's canonical_json().
    param([Parameter(Mandatory)][AllowNull()]$Value)

    if ($null -eq $Value) { return "null" }
    if ($Value -is [bool]) { if ($Value) { return "true" } else { return "false" } }
    if ($Value -is [string]) { return (ConvertTo-CreateUatJsonString -Value $Value) }
    if ($Value -is [int] -or $Value -is [long] -or $Value -is [int16] -or $Value -is [byte] -or `
            $Value -is [sbyte] -or $Value -is [uint16] -or $Value -is [uint32] -or $Value -is [uint64]) {
        return ([long]$Value).ToString([System.Globalization.CultureInfo]::InvariantCulture)
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $names = [string[]]@($Value.Keys)
        [Array]::Sort($names, [System.StringComparer]::Ordinal)
        $parts = foreach ($name in $names) {
            (ConvertTo-CreateUatJsonString -Value $name) + ":" + (Get-CreateUatCanonicalJson -Value $Value[$name])
        }
        return "{" + ($parts -join ",") + "}"
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $names = [string[]]@($Value.PSObject.Properties.Name)
        [Array]::Sort($names, [System.StringComparer]::Ordinal)
        $parts = foreach ($name in $names) {
            (ConvertTo-CreateUatJsonString -Value $name) + ":" + (Get-CreateUatCanonicalJson -Value $Value.$name)
        }
        return "{" + ($parts -join ",") + "}"
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $parts = foreach ($item in $Value) { Get-CreateUatCanonicalJson -Value $item }
        return "[" + ($parts -join ",") + "]"
    }
    throw "Unsupported value type in canonical JSON: $($Value.GetType().FullName)"
}

# --------------------------------------------------------------------------- #
# Identity recomputation
# --------------------------------------------------------------------------- #
function Get-CreateUatSourceRecordId {
    param([Parameter(Mandatory)][string]$MemberNo)
    "srcrec_" + (Get-Sha256Hex -Text ("$script:CreateUatSchemaVersion|$MemberNo"))
}

function Get-CreateUatSourceFingerprint {
    param([Parameter(Mandatory)]$Package)
    $payload = $Package.member_payload
    $desired = $Package.desired_business_fields
    $fields = [ordered]@{
        schema_version = $script:CreateUatSchemaVersion
        MemberNo       = [string]$payload.MemberNo
        Name           = [string]$payload.Name
        EmailAddress   = [string]$payload.EmailAddress
        DOB            = [string]$payload.DOB
        MemberType     = [string]$desired.MemberType
        RegisterDate   = [string]$desired.RegisterDate
        ExpiryDate     = [string]$desired.ExpiryDate
        OpeningPoints  = [long]$desired.OpeningPoints
    }
    "fp_" + (Get-Sha256Hex -Text (Get-CreateUatCanonicalJson -Value $fields))
}

function Get-CreateUatPayloadHash {
    # Recompute payload_hash over the package with the two derived hash mirrors removed.
    param([Parameter(Mandatory)]$Package)
    $body = [ordered]@{}
    foreach ($name in (Get-CreateUatMemberNames $Package)) {
        if ($name -eq "payload_hash") { continue }
        if ($name -eq "approval") {
            $approval = [ordered]@{}
            $approvalObj = $Package.$name
            foreach ($an in (Get-CreateUatMemberNames $approvalObj)) {
                if ($an -eq "bound_package_payload_hash") { continue }
                $approval[$an] = $approvalObj.$an
            }
            $body["approval"] = $approval
            continue
        }
        $body[$name] = $Package.$name
    }
    "sha256:" + (Get-Sha256Hex -Text (Get-CreateUatCanonicalJson -Value $body))
}

# --------------------------------------------------------------------------- #
# Path safety (independent of the shared-folder handoff module)
# --------------------------------------------------------------------------- #
function Test-CreateUatSafePath {
    param([Parameter(Mandatory)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    if (-not [System.IO.Path]::IsPathRooted($Path)) { return $false }
    $name = [System.IO.Path]::GetFileName($Path)
    if ($name -notmatch '^[A-Za-z0-9._-]{1,128}$') { return $false }
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if ($item.Attributes.HasFlag([System.IO.FileAttributes]::ReparsePoint)) { return $false }
    }
    return $true
}

# --------------------------------------------------------------------------- #
# Package structure validation (mirrors the JSON Schema and Python validate_package)
# --------------------------------------------------------------------------- #
function Test-CreateUatPackage {
    # Returns [pscustomobject]@{ Valid=$bool; FingerprintProblem=$bool; Reasons=@() }
    param([Parameter(Mandatory)][AllowNull()]$Package)

    $reasons = [System.Collections.Generic.List[string]]::new()
    $fingerprintProblem = $false

    function Add-Reason([string]$code) { $reasons.Add($code) }

    if ($null -eq $Package -or -not ($Package -is [System.Collections.IDictionary] -or $Package -is [System.Management.Automation.PSCustomObject])) {
        return [pscustomobject]@{ Valid = $false; FingerprintProblem = $false; Reasons = @("package_not_object") }
    }

    $topExpected = @(
        "schema_version", "operation_id", "source_record_id", "source_fingerprint", "row_number_hint",
        "created_at", "approval", "assignable_fields", "member_payload", "desired_business_fields",
        "business_confirmation_required", "payload_hash"
    )
    $topActual = @(Get-CreateUatMemberNames $Package)
    if (@(Compare-Object $topExpected $topActual).Count -ne 0) {
        return [pscustomobject]@{ Valid = $false; FingerprintProblem = $false; Reasons = @("top_level_field_set_mismatch") }
    }

    if ($Package.schema_version -ne $script:CreateUatSchemaVersion) { Add-Reason "schema_version_mismatch" }
    if ($Package.operation_id -notmatch '^mcuat_[0-9a-f]{32}$') { Add-Reason "operation_id_invalid" }
    if ($Package.source_record_id -notmatch '^srcrec_[0-9a-f]{64}$') { Add-Reason "source_record_id_invalid" }
    if ($Package.source_fingerprint -notmatch '^fp_[0-9a-f]{64}$') { Add-Reason "source_fingerprint_invalid" }
    if (-not (($Package.row_number_hint -is [int] -or $Package.row_number_hint -is [long]) -and [long]$Package.row_number_hint -ge 2)) {
        Add-Reason "row_number_hint_invalid"
    }
    if ([string]$Package.created_at -notmatch $script:CreateUatTimestampRe) { Add-Reason "created_at_invalid" }

    # Fixed control arrays must match exactly (ordered-equivalent).
    if (@(Compare-Object $script:CreateUatAssignableFields @($Package.assignable_fields)).Count -ne 0) { Add-Reason "assignable_fields_mismatch" }
    if (@(Compare-Object $script:CreateUatBusinessConfirmationRequired @($Package.business_confirmation_required)).Count -ne 0) { Add-Reason "business_confirmation_required_mismatch" }

    # Field whitelist and exactly-one-record payload.
    $payload = $Package.member_payload
    $payloadNames = @(Get-CreateUatMemberNames $payload)
    if (@(Compare-Object $script:CreateUatAssignableFields $payloadNames).Count -ne 0) {
        Add-Reason "member_payload_field_set_mismatch"
    }
    else {
        $memberNo = [string]$payload.MemberNo
        $name = [string]$payload.Name
        $email = [string]$payload.EmailAddress
        $memberType = [string]$payload.MemberType
        if ($memberNo -notmatch '^[0-9A-Za-z]{1,20}$') { Add-Reason "member_no_invalid" }
        if ($name.Length -lt 1 -or $name.Length -gt 100) { Add-Reason "name_invalid" }
        if ($email.Length -lt 3 -or $email.Length -gt 200) { Add-Reason "email_invalid" }
        if ([string]$payload.MobilePhone -ne "") { Add-Reason "mobile_phone_not_blank" }
        if ([string]$payload.DOB -notmatch '^2000-(0[1-9]|1[0-2])-01$') { Add-Reason "dob_invalid" }
        if ($memberType.Length -lt 1 -or $memberType.Length -gt 20) { Add-Reason "member_type_invalid" }
        if ([string]$payload.RegisterDate -notmatch '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$') { Add-Reason "register_date_invalid" }
        if ([string]$payload.ExpiryDate -notmatch $script:CreateUatDateRe) { Add-Reason "expiry_date_invalid" }
        if (-not (($payload.OpeningPoints -is [int] -or $payload.OpeningPoints -is [long]) -and [long]$payload.OpeningPoints -eq 0)) { Add-Reason "opening_points_not_zero" }
        if ($memberType -ne $script:CreateUatIntended.MemberType) { Add-Reason "member_payload_MemberType_not_intended" }
        if ([string]$payload.RegisterDate -ne $script:CreateUatIntended.RegisterDate) { Add-Reason "member_payload_RegisterDate_not_intended" }
        if ([string]$payload.ExpiryDate -ne $script:CreateUatIntended.ExpiryDate) { Add-Reason "member_payload_ExpiryDate_not_intended" }
        if (-not (($payload.OpeningPoints -is [int] -or $payload.OpeningPoints -is [long]) -and [long]$payload.OpeningPoints -eq [long]$script:CreateUatIntended.OpeningPoints)) { Add-Reason "member_payload_OpeningPoints_not_intended" }
    }
    foreach ($never in $script:CreateUatNeverAssignFields) {
        if ($payloadNames -contains $never) { Add-Reason "forbidden_assignable_field" }
    }

    # desired_business_fields: exact field set, formats/types, and intended values.
    $desired = $Package.desired_business_fields
    $desiredNames = @(Get-CreateUatMemberNames $desired)
    if (@(Compare-Object $script:CreateUatDesiredBusinessFields $desiredNames).Count -ne 0) {
        Add-Reason "desired_business_fields_mismatch"
    }
    else {
        $desiredMemberType = [string]$desired.MemberType
        if ($desiredMemberType.Length -lt 1 -or $desiredMemberType.Length -gt 20) { Add-Reason "desired_member_type_invalid" }
        if ([string]$desired.RegisterDate -notmatch $script:CreateUatDateRe) { Add-Reason "desired_register_date_invalid" }
        if ([string]$desired.ExpiryDate -notmatch $script:CreateUatDateRe) { Add-Reason "desired_expiry_date_invalid" }
        if (-not (($desired.OpeningPoints -is [int] -or $desired.OpeningPoints -is [long]) -and [long]$desired.OpeningPoints -eq 0)) { Add-Reason "desired_opening_points_not_zero" }
        if ([string]$desired.MemberType -ne $script:CreateUatIntended.MemberType) { Add-Reason "desired_MemberType_not_intended" }
        if ([string]$desired.RegisterDate -ne $script:CreateUatIntended.RegisterDate) { Add-Reason "desired_RegisterDate_not_intended" }
        if ([string]$desired.ExpiryDate -ne $script:CreateUatIntended.ExpiryDate) { Add-Reason "desired_ExpiryDate_not_intended" }
    }

    # Approval binding and payload_hash integrity.
    if ($Package.payload_hash -notmatch '^sha256:[0-9a-f]{64}$') {
        Add-Reason "payload_hash_format_invalid"; $fingerprintProblem = $true
    }
    else {
        $recomputed = Get-CreateUatPayloadHash -Package $Package
        if ($recomputed -ne $Package.payload_hash) { Add-Reason "payload_hash_integrity_failed"; $fingerprintProblem = $true }
        if ($Package.approval.bound_package_payload_hash -ne $Package.payload_hash) { Add-Reason "approval_binding_mismatch"; $fingerprintProblem = $true }
    }

    # Recompute the stable identity and change-detection fingerprint.
    if ($payloadNames -contains "MemberNo") {
        $srid = Get-CreateUatSourceRecordId -MemberNo ([string]$payload.MemberNo)
        if ($srid -ne $Package.source_record_id) { Add-Reason "source_record_id_recompute_mismatch"; $fingerprintProblem = $true }
    }
    try {
        $fp = Get-CreateUatSourceFingerprint -Package $Package
        if ($fp -ne $Package.source_fingerprint) { Add-Reason "source_fingerprint_recompute_mismatch"; $fingerprintProblem = $true }
    }
    catch {
        Add-Reason "source_fingerprint_recompute_error"; $fingerprintProblem = $true
    }

    # Embedded approval: exact field set, formats, ordering, identity/fingerprint binding.
    $approval = $Package.approval
    $approvalExpected = @("approval_id", "reviewer_id", "decision", "approved_at", "expires_at", "source_record_id", "source_fingerprint", "bound_package_payload_hash")
    if (@(Compare-Object $approvalExpected @(Get-CreateUatMemberNames $approval)).Count -ne 0) {
        Add-Reason "approval_field_set_mismatch"
    }
    else {
        if ([string]$approval.approval_id -notmatch '^appr_[0-9a-f]{32}$') { Add-Reason "approval_id_invalid" }
        if ([string]$approval.reviewer_id -notmatch '^[a-z0-9_-]{2,32}$') { Add-Reason "reviewer_id_invalid" }
        if ([string]$approval.decision -ne "approved") { Add-Reason "approval_decision_not_approved" }
        if ([string]$approval.approved_at -notmatch $script:CreateUatTimestampRe) { Add-Reason "approval_approved_at_invalid" }
        if ([string]$approval.expires_at -notmatch $script:CreateUatTimestampRe) { Add-Reason "approval_expires_at_invalid" }
        if ([string]$approval.bound_package_payload_hash -notmatch '^sha256:[0-9a-f]{64}$') { Add-Reason "bound_package_payload_hash_invalid"; $fingerprintProblem = $true }
        try {
            $ap = [datetime]::Parse([string]$approval.approved_at, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor [System.Globalization.DateTimeStyles]::AssumeUniversal)
            $ex = [datetime]::Parse([string]$approval.expires_at, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor [System.Globalization.DateTimeStyles]::AssumeUniversal)
            if ($ap -ge $ex) { Add-Reason "approval_expiry_not_after_approved_at" }
        }
        catch { Add-Reason "approval_timestamp_unparseable" }
        if ([string]$approval.source_record_id -ne [string]$Package.source_record_id) { Add-Reason "approval_source_record_id_mismatch"; $fingerprintProblem = $true }
        if ([string]$approval.source_fingerprint -ne [string]$Package.source_fingerprint) { Add-Reason "approval_source_fingerprint_mismatch"; $fingerprintProblem = $true }
    }

    [pscustomobject]@{
        Valid              = ($reasons.Count -eq 0)
        FingerprintProblem = $fingerprintProblem
        Reasons            = $reasons.ToArray()
    }
}

# --------------------------------------------------------------------------- #
# Approval expiry and business gate
# --------------------------------------------------------------------------- #
function Test-CreateUatApprovalNotExpired {
    param([Parameter(Mandatory)]$Package, [Parameter(Mandatory)][datetime]$NowUtc)
    try {
        $expires = [datetime]::Parse([string]$Package.approval.expires_at, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor [System.Globalization.DateTimeStyles]::AssumeUniversal)
    }
    catch {
        return $false
    }
    return ($NowUtc -lt $expires)
}

# Code-level capability flag (finding 1): ExpiryDate assignment AND its read-back
# verification are now implemented and proven (synthetic capability probe;
# terminal_outcome=EXPIRY_VERIFIED), so this is $true. It must remain in exact agreement
# with the Python EXPIRYDATE_ASSIGNMENT_IMPLEMENTED constant. The independent
# business-confirmation gate and the five write-confirmation switches are unchanged, so
# a real write still requires the explicit operator step.
$script:CreateUatExpiryDateAssignmentImplemented = $true
$script:CreateUatBusinessConfigSchemaVersion = "member_create_uat_business_confirmation/v1"

function Get-CreateUatBusinessGate {
    # Strictly validate the committed business-confirmation config object and return
    # whether a real write is permitted. Confirmed requires: exact schema version,
    # exact four-field confirmations set, each a boolean-typed confirmed=true, AND the
    # code-level ExpiryDate capability being implemented.
    param([Parameter(Mandatory)][AllowNull()]$ConfigObject)
    $reasons = [System.Collections.Generic.List[string]]::new()
    if ($null -eq $ConfigObject) {
        $reasons.Add('config_missing')
    }
    else {
        if ([string]$ConfigObject.schema_version -ne $script:CreateUatBusinessConfigSchemaVersion) { $reasons.Add('schema_version_mismatch') }
        $conf = $ConfigObject.confirmations
        if ($null -eq $conf) {
            $reasons.Add('confirmations_missing')
        }
        elseif (@(Compare-Object $script:CreateUatBusinessConfirmationRequired @(Get-CreateUatMemberNames $conf)).Count -ne 0) {
            $reasons.Add('confirmations_field_set_mismatch')
        }
        else {
            foreach ($field in $script:CreateUatBusinessConfirmationRequired) {
                $entry = $conf.$field
                if ($null -eq $entry -or ($entry.confirmed -isnot [bool])) { $reasons.Add("confirmation_${field}_structure_invalid") }
                elseif ($entry.confirmed -ne $true) { $reasons.Add("confirmation_${field}_not_confirmed") }
            }
        }
    }
    if (-not $script:CreateUatExpiryDateAssignmentImplemented) { $reasons.Add('expiry_date_capability_unproven') }
    [pscustomobject]@{ Confirmed = ($reasons.Count -eq 0); Reasons = $reasons.ToArray() }
}

# --------------------------------------------------------------------------- #
# Durable write state (finding 2)
# --------------------------------------------------------------------------- #
function Write-CreateUatDurableArtifact {
    # Exclusive-create a durable artefact. Fails closed if it already exists so a prior
    # intent/consumed/terminal record is never overwritten. WriteThrough + Flush(true)
    # push the bytes past OS caches where the platform supports it.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Content)
    $stream = [System.IO.FileStream]::new(
        $Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None, 4096, [System.IO.FileOptions]::WriteThrough)
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Content)
        $stream.Write($bytes, 0, $bytes.Length)
        try { $stream.Flush($true) } catch { $stream.Flush() }
    }
    finally { $stream.Dispose() }
}

function Write-CreateUatDurableResultAtomic {
    # Same-volume temp + atomic move for the terminal result. Never overwrites a prior
    # terminal result.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Content)
    if (Test-Path -LiteralPath $Path) { throw "Terminal result artefact already exists." }
    $temp = $Path + ".tmp"
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
    Write-CreateUatDurableArtifact -Path $temp -Content $Content
    [System.IO.File]::Move($temp, $Path)
}

function New-CreateUatSanitizedMarker {
    # Sanitised marker body: only the approved identifiers plus a UTC timestamp.
    param([Parameter(Mandatory)]$Package)
    [ordered]@{
        operation_id       = [string]$Package.operation_id
        approval_id        = [string]$Package.approval.approval_id
        payload_hash       = [string]$Package.payload_hash
        source_record_id   = [string]$Package.source_record_id
        source_fingerprint = [string]$Package.source_fingerprint
        recorded_at_utc    = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    } | ConvertTo-Json -Depth 4
}

function Get-CreateUatRecoveryState {
    # Deterministic recovery classification from the durable artefacts. Never permits
    # an automatic second SaveMember: any non-'none' state is terminal for this run.
    param(
        [Parameter(Mandatory)][string]$StateDir,
        [Parameter(Mandatory)][string]$OperationId,
        [Parameter(Mandatory)][string]$SourceRecordId
    )
    $consumed = Join-Path $StateDir ("consumed_" + $SourceRecordId + ".marker")
    $intent = Join-Path $StateDir ("write_intent_" + $OperationId + ".marker")
    $terminal = Join-Path $StateDir ("result_" + $OperationId + ".json")
    foreach ($p in @($consumed, $intent, $terminal)) {
        if (Test-Path -LiteralPath $p) {
            try { [void](ConvertFrom-CreateUatJson -Raw (Get-Content -LiteralPath $p -Raw -Encoding UTF8)) }
            catch { return 'malformed' }
        }
    }
    if (Test-Path -LiteralPath $terminal) { return 'terminal_exists' }
    if (Test-Path -LiteralPath $consumed) { return 'consumed_no_terminal' }
    if (Test-Path -LiteralPath $intent) { return 'intent_no_consumed' }
    return 'none'
}

# --------------------------------------------------------------------------- #
# Read-back verification (finding 3)
# --------------------------------------------------------------------------- #
function Get-CreateUatNormalizedFieldValue {
    param([AllowNull()]$Value)
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return '' }
    if ($Value -is [datetime]) { return $Value.ToString('yyyy-MM-dd') }
    if ($Value -is [bool]) { if ($Value) { return 'T' } else { return 'F' } }
    if ($Value -is [decimal] -or $Value -is [double] -or $Value -is [int] -or $Value -is [long]) {
        # Canonical numeric string: strip trailing zeros/scale so [decimal]0.0 and 0 match.
        return ([decimal]$Value).ToString('0.#############################', [System.Globalization.CultureInfo]::InvariantCulture)
    }
    return ([string]$Value).Trim()
}

function Test-CreateUatReadbackMatch {
    # Compare every assigned field to the read-back row value, normalising AutoCount
    # date / decimal / blank / boolean-string representations explicitly.
    param([Parameter(Mandatory)]$Assigned, [Parameter(Mandatory)]$Readback)
    $mismatches = [System.Collections.Generic.List[string]]::new()
    foreach ($field in @(Get-CreateUatMemberNames $Assigned)) {
        $a = Get-CreateUatNormalizedFieldValue $Assigned[$field]
        $rbValue = $null
        if ($Readback -is [System.Collections.IDictionary] -and $Readback.Contains($field)) { $rbValue = $Readback[$field] }
        elseif ($Readback -isnot [System.Collections.IDictionary]) { $rbValue = $Readback.$field }
        $b = Get-CreateUatNormalizedFieldValue $rbValue
        if ($a -ne $b) { $mismatches.Add($field) }
    }
    [pscustomobject]@{ Match = ($mismatches.Count -eq 0); Mismatches = $mismatches.ToArray() }
}

# --------------------------------------------------------------------------- #
# Exclusive execution lock (VM-owned)
# --------------------------------------------------------------------------- #
function New-CreateUatExclusiveLock {
    # Returns the open FileStream on success, or $null if the lock is already held.
    param([Parameter(Mandatory)][string]$LockPath)
    try {
        return [System.IO.File]::Open($LockPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    }
    catch [System.IO.IOException] {
        return $null
    }
}

function Remove-CreateUatExclusiveLock {
    param([Parameter(Mandatory)][AllowNull()]$LockStream, [Parameter(Mandatory)][string]$LockPath)
    if ($null -ne $LockStream) { $LockStream.Close(); $LockStream.Dispose() }
    if (Test-Path -LiteralPath $LockPath) { Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue }
}

# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
function Get-CreateUatMaskedMemberNo {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$MemberNo)
    if ([string]::IsNullOrEmpty($MemberNo) -or $MemberNo.Length -le 2) { return "***" }
    $MemberNo.Substring(0, 2) + "***" + $MemberNo.Substring($MemberNo.Length - 1, 1)
}

# --------------------------------------------------------------------------- #
# Canonical terminal-state table (finding 4).
#
# $Flags is a hashtable / ordered dictionary keyed exactly like the runner result
# (snake_case), so the runner passes its $result straight in, the Python result
# precheck recomputes the same way, and the n8n code applies the same table.
# --------------------------------------------------------------------------- #
function Get-CreateUatFlag { param($Flags, [string]$Name, $Default = $null)
    if ($Flags.Contains($Name)) { return $Flags[$Name] }
    return $Default
}

function Get-CreateUatStateContradictions {
    param([Parameter(Mandatory)]$Flags)
    $reasons = [System.Collections.Generic.List[string]]::new()
    $mode = Get-CreateUatFlag $Flags 'mode'
    $write = ($mode -eq 'write')
    $attempted = [bool](Get-CreateUatFlag $Flags 'save_member_attempted' $false)
    $confirmed = [bool](Get-CreateUatFlag $Flags 'save_member_confirmed' $false)
    $outcome = Get-CreateUatFlag $Flags 'save_outcome' 'not_attempted'
    $rbFound = [bool](Get-CreateUatFlag $Flags 'readback_found' $false)
    $rbMatch = [bool](Get-CreateUatFlag $Flags 'readback_match' $false)

    if ($mode -ne 'dry-run' -and $mode -ne 'write') { $reasons.Add('mode_invalid') }
    if (@('none', 'terminal_exists', 'consumed_no_terminal', 'intent_no_consumed', 'malformed') -notcontains (Get-CreateUatFlag $Flags 'recovery_state' 'none')) { $reasons.Add('recovery_state_invalid') }
    if (@('not_attempted', 'confirmed', 'uncertain') -notcontains $outcome) { $reasons.Add('save_outcome_invalid') }
    if ($confirmed -and -not $attempted) { $reasons.Add('confirmed_without_attempt') }
    if ($outcome -eq 'confirmed' -and -not $confirmed) { $reasons.Add('outcome_confirmed_without_confirmed_flag') }
    if ($outcome -eq 'not_attempted' -and $attempted) { $reasons.Add('not_attempted_but_attempted') }
    if ($outcome -eq 'uncertain' -and -not $attempted) { $reasons.Add('uncertain_without_attempt') }
    if ($rbMatch -and -not $rbFound) { $reasons.Add('match_without_found') }
    if (($rbFound -or $rbMatch) -and $outcome -ne 'confirmed') { $reasons.Add('readback_without_confirmed_save') }
    if ($attempted -and -not $write) { $reasons.Add('attempt_in_non_write_mode') }
    if ($attempted -and -not [bool](Get-CreateUatFlag $Flags 'lock_acquired' $false)) { $reasons.Add('attempt_without_lock') }
    # ExpiryDate is an active assignable field, so any real save is preceded by an
    # ExpiryDate assignment and the full expected field set. A save attempt (or a matched
    # read-back) without expiry_date_assigned, or with a stale assigned-field count, is a
    # contradiction, so CREATED_VERIFIED is impossible unless ExpiryDate was assigned and
    # the full field set was written.
    $expiryAssigned = [bool](Get-CreateUatFlag $Flags 'expiry_date_assigned' $false)
    $assignedCount = Get-CreateUatFlag $Flags 'assigned_field_count' $null
    if (($attempted -or $rbMatch) -and -not $expiryAssigned) { $reasons.Add('expiry_date_not_assigned') }
    if (($attempted -or $rbMatch) -and ([long]($assignedCount) -ne [long]$script:CreateUatExpectedAssignedFieldCount)) { $reasons.Add('assigned_field_count_stale') }
    if (-not $write -and (
            [bool](Get-CreateUatFlag $Flags 'write_confirmed' $false) -or
            [bool](Get-CreateUatFlag $Flags 'member_exists_recheck' $false) -or
            $attempted -or $confirmed -or ($outcome -ne 'not_attempted'))) {
        $reasons.Add('dry_run_has_write_state')
    }
    return $reasons.ToArray()
}

function Get-CreateUatTerminalCode {
    param([Parameter(Mandatory)]$Flags)
    $write = ((Get-CreateUatFlag $Flags 'mode') -eq 'write')
    $recovery = Get-CreateUatFlag $Flags 'recovery_state' 'none'
    $outcome = Get-CreateUatFlag $Flags 'save_outcome' 'not_attempted'

    if ([bool](Get-CreateUatFlag $Flags 'package_fingerprint_problem' $false)) { return 'SOURCE_FINGERPRINT_MISMATCH' }
    if (-not [bool](Get-CreateUatFlag $Flags 'package_structural_valid' $false)) { return 'FAILED_BEFORE_WRITE' }
    if (-not [bool](Get-CreateUatFlag $Flags 'approval_not_expired' $false)) { return 'APPROVAL_INVALID' }
    if ($write -and -not [bool](Get-CreateUatFlag $Flags 'write_confirmed' $false)) { return 'WRITE_NOT_CONFIRMED' }
    if ($write -and -not [bool](Get-CreateUatFlag $Flags 'business_confirmed' $false)) { return 'OPERATOR_CONFIG_REQUIRED' }
    if (-not [bool](Get-CreateUatFlag $Flags 'lock_acquired' $false)) { return 'EXECUTION_LOCKED' }
    if ($recovery -eq 'terminal_exists') { return 'PACKAGE_ALREADY_CONSUMED' }
    if ($recovery -eq 'consumed_no_terminal') { return 'WRITE_OUTCOME_UNCERTAIN' }
    if ($recovery -eq 'intent_no_consumed') { return 'FAILED_BEFORE_WRITE' }
    if ($recovery -eq 'malformed') { return 'WRITE_OUTCOME_UNCERTAIN' }
    if ([bool](Get-CreateUatFlag $Flags 'execution_error' $false)) {
        if ([bool](Get-CreateUatFlag $Flags 'save_member_attempted' $false)) { return 'WRITE_OUTCOME_UNCERTAIN' }
        return 'FAILED_BEFORE_WRITE'
    }
    if ([bool](Get-CreateUatFlag $Flags 'member_exists_initial' $false)) { return 'BLOCKED_MEMBER_EXISTS' }
    if (-not $write) { return 'DRY_RUN_VALIDATED' }
    if ([bool](Get-CreateUatFlag $Flags 'member_exists_recheck' $false)) { return 'BLOCKED_MEMBER_EXISTS' }
    if ($outcome -eq 'uncertain') { return 'WRITE_OUTCOME_UNCERTAIN' }
    if ($outcome -eq 'not_attempted') { return 'FAILED_BEFORE_WRITE' }
    if ($outcome -eq 'confirmed') {
        if (-not [bool](Get-CreateUatFlag $Flags 'readback_found' $false)) { return 'WRITE_OUTCOME_UNCERTAIN' }
        if ([bool](Get-CreateUatFlag $Flags 'readback_match' $false)) { return 'CREATED_VERIFIED' }
        return 'CREATED_READBACK_MISMATCH'
    }
    return 'FAILED_BEFORE_WRITE'
}
