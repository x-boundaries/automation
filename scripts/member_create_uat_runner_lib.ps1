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

$script:CreateUatSchemaVersion = "member_create_uat_package/v1"
$script:CreateUatAssignableFields = @(
    "MemberNo", "Name", "EmailAddress", "MobilePhone", "DOB", "MemberType", "RegisterDate", "OpeningPoints"
)
# ExpiryDate is intentionally excluded from assignment until proven.
$script:CreateUatNeverAssignFields = @("ExpiryDate")
$script:CreateUatBusinessConfirmationRequired = @("MemberType", "RegisterDate", "ExpiryDate", "OpeningPoints")

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
    foreach ($prop in $Package.PSObject.Properties) {
        if ($prop.Name -eq "payload_hash") { continue }
        if ($prop.Name -eq "approval") {
            $approval = [ordered]@{}
            foreach ($ap in $prop.Value.PSObject.Properties) {
                if ($ap.Name -eq "bound_package_payload_hash") { continue }
                $approval[$ap.Name] = $ap.Value
            }
            $body["approval"] = $approval
            continue
        }
        $body[$prop.Name] = $prop.Value
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

    if ($null -eq $Package -or -not ($Package -is [System.Management.Automation.PSCustomObject])) {
        return [pscustomobject]@{ Valid = $false; FingerprintProblem = $false; Reasons = @("package_not_object") }
    }

    $topExpected = @(
        "schema_version", "operation_id", "source_record_id", "source_fingerprint", "row_number_hint",
        "created_at", "approval", "assignable_fields", "member_payload", "desired_business_fields",
        "business_confirmation_required", "payload_hash"
    )
    $topActual = @($Package.PSObject.Properties.Name)
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

    # Field whitelist and exactly-one-record payload.
    $payload = $Package.member_payload
    $payloadNames = @($payload.PSObject.Properties.Name)
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
        if (-not (($payload.OpeningPoints -is [int] -or $payload.OpeningPoints -is [long]) -and [long]$payload.OpeningPoints -eq 0)) { Add-Reason "opening_points_not_zero" }
    }
    foreach ($never in $script:CreateUatNeverAssignFields) {
        if ($payloadNames -contains $never) { Add-Reason "forbidden_assignable_field" }
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

    # Embedded approval internal consistency.
    if ($Package.approval.source_record_id -ne $Package.source_record_id) { Add-Reason "approval_source_record_id_mismatch"; $fingerprintProblem = $true }
    if ($Package.approval.source_fingerprint -ne $Package.source_fingerprint) { Add-Reason "approval_source_fingerprint_mismatch"; $fingerprintProblem = $true }
    if ($Package.approval.decision -ne "approved") { Add-Reason "approval_decision_not_approved" }
    if ($Package.approval.reviewer_id -notmatch '^[a-z0-9_-]{2,32}$') { Add-Reason "reviewer_id_invalid" }

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

function Test-CreateUatBusinessConfirmed {
    # $Confirmations is the parsed 'confirmations' object from the committed config.
    param([Parameter(Mandatory)][AllowNull()]$Confirmations)
    if ($null -eq $Confirmations) { return $false }
    foreach ($field in $script:CreateUatBusinessConfirmationRequired) {
        $entry = $Confirmations.$field
        if ($null -eq $entry -or $entry.confirmed -ne $true) { return $false }
    }
    return $true
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
# Pure terminal-state decision
# --------------------------------------------------------------------------- #
function Get-CreateUatTerminalCode {
    # Deterministic ordered decision. $Ctx is a hashtable of booleans plus SaveOutcome
    # in { "not_attempted", "confirmed", "uncertain" }.
    param([Parameter(Mandatory)][hashtable]$Ctx)

    if ($Ctx.PackageFingerprintProblem) { return "SOURCE_FINGERPRINT_MISMATCH" }
    if (-not $Ctx.PackageValid) { return "FAILED_BEFORE_WRITE" }
    if ($Ctx.ApprovalExpired) { return "APPROVAL_INVALID" }
    if ($Ctx.ForWrite -and -not $Ctx.WriteConfirmed) { return "WRITE_NOT_CONFIRMED" }
    if ($Ctx.ForWrite -and -not $Ctx.BusinessConfirmed) { return "OPERATOR_CONFIG_REQUIRED" }
    if (-not $Ctx.LockAcquired) { return "EXECUTION_LOCKED" }
    if ($Ctx.AlreadyConsumed) { return "PACKAGE_ALREADY_CONSUMED" }
    if ($Ctx.MemberExistsInitial) { return "BLOCKED_MEMBER_EXISTS" }
    if (-not $Ctx.ForWrite) { return "DRY_RUN_VALIDATED" }
    if ($Ctx.MemberExistsRecheck) { return "BLOCKED_MEMBER_EXISTS" }
    switch ($Ctx.SaveOutcome) {
        "uncertain" { return "WRITE_OUTCOME_UNCERTAIN" }
        "confirmed" {
            if ($Ctx.ReadbackMatch) { return "CREATED_VERIFIED" } else { return "CREATED_READBACK_MISMATCH" }
        }
        default { return "FAILED_BEFORE_WRITE" }
    }
}
