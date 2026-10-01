# xb-ac2-member create: the member-write v2 primitive (W-G2-149 section 2.7).
#
# One process per invocation. It is started only by the worker as a child of
# C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile with
#   -File ac2_member_create_primitive.ps1 -Book production|test [-EnableProductionAdapter]
#
# Closed primitive I/O contract
# -----------------------------
# stdin : exactly one JSON line (ASCII; non-ASCII escaped as \uXXXX) with the
#         claim v2 `request` fields plus four claim fields:
#           rule, base_member_no, name_component, phone, name, email,
#           MemberType, DOB, RegisterDate, ExpiryDate, OpeningPoints,
#           IsActive, Individual, job_id, attempt_no, first_claimed_at,
#           server_time_utc
# stdout: exactly one JSON line, a closed object with exactly the keys
#           outcome, rule, branch, member_no, member_guid, save_invoked,
#           save_invocation_count, readback, reason_code, dq_flags,
#           primitive {release_sha256, rule_version}, error_code
#         using the enums of schemas/member_gateway_result.v2.schema.json.
#         No personal data is emitted except member_no. The worker adds
#         schema_version, job_id, attempt_no, lease_id and state_version to
#         build the frozen result v2 body.
# exit  : 0 whenever a result line was written.
#
# Runtime configuration (process environment, set by the launcher/worker):
#   XB_AC2_SERVER_NAME, XB_AC2_DATABASE_NAME, XB_AC2_USER_ID,
#   XB_AC2_PASSWORD_ENV_VAR (+ the named password variable, DPAPI custody,
#   cleared from this process after login), XB_AC2_ASSEMBLY_PATH,
#   XB_AC2_INTEGRATION_USER_ID, XB_AC2_PRODUCTION_BOOK,
#   XB_AC2_TEST_BOOK_ALLOWLIST (semicolon separated), XB_AC2_FAULT (test only).
#
# Guarantees: at most one member save per invocation, from exactly one call
# site (Invoke-XbAutoCountSaveMember in the adapter), never in a loop; no
# update or delete of any existing row; no suffix allocation; no fuzzy match.

[CmdletBinding()]
param(
    [string]$Book,
    [switch]$EnableProductionAdapter,
    [switch]$LibraryOnly
)

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "ac2_member_gateway_autocount_adapter.ps1")
$script:XbAc2PrimitivePackageRoot = $PSScriptRoot

$script:XbAc2RequestFields = @(
    "rule", "base_member_no", "name_component", "phone", "name", "email", "MemberType", "DOB",
    "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual",
    "job_id", "attempt_no", "first_claimed_at", "server_time_utc"
)
$script:XbAc2DqFlagOrder = @("email_seen_on_other_member", "post_save_same_person_other_row", "malformed_member_no_excluded")
$script:XbAc2AllowedFaults = @("save_error_after_commit", "exit_after_save", "hang_after_save", "readback_error")
$script:XbAc2ClockSkewSeconds = 120
$script:XbAc2PriorAttemptWindowMinutes = 10
$script:XbAc2SyntheticNamePrefix = "ZZTEST "
$script:XbAc2SyntheticEmailSuffix = "@example.invalid"

# ---------------------------------------------------------------------------
# Pure text helpers (contract 2.4 / 2.5)
# ---------------------------------------------------------------------------

function ConvertTo-XbAc2Nfkc {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return "" }
    return ([string]$Value).Normalize([Text.NormalizationForm]::FormKC)
}

# digits(x) = ASCII [0-9] of NFKC(trim(x)); "" if null.
function Get-XbAc2Digits {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return "" }
    $text = (ConvertTo-XbAc2Nfkc -Value ([string]$Value).Trim())
    $builder = New-Object System.Text.StringBuilder
    foreach ($ch in $text.ToCharArray()) {
        if ($ch -ge [char]'0' -and $ch -le [char]'9') { [void]$builder.Append($ch) }
    }
    return $builder.ToString()
}

# MNU(row) = ASCII-upper(NFKC(trim(MemberNo))).
function Get-XbAc2MemberNoUpper {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return "" }
    $text = ConvertTo-XbAc2Nfkc -Value ([string]$Value).Trim()
    $builder = New-Object System.Text.StringBuilder
    foreach ($ch in $text.ToCharArray()) {
        if ($ch -ge [char]'a' -and $ch -le [char]'z') { [void]$builder.Append([char]([int]$ch - 32)) }
        else { [void]$builder.Append($ch) }
    }
    return $builder.ToString()
}

# Scientific-notation MemberNo (for example 6.59E+09): excluded from P, flagged.
function Test-XbAc2MalformedMemberNo {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return $false }
    $text = ConvertTo-XbAc2Nfkc -Value ([string]$Value).Trim()
    return ($text -cmatch '^[0-9](\.[0-9]+)?[Ee][+-]?[0-9]+$')
}

# em(x) = ToLowerInvariant(trim(NFKC(x))).
function Get-XbAc2EmailKey {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return "" }
    return (ConvertTo-XbAc2Nfkc -Value $Value).Trim().ToLowerInvariant()
}

# nm(x) = NFKC(x).ToLowerInvariant(); chars not in {L*, M*, Nd} -> ' ';
# split; drop empty; sort ordinal; join ' '. Native scripts compare exactly.
function Get-XbAc2NameKey {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return "" }
    $text = (ConvertTo-XbAc2Nfkc -Value $Value).ToLowerInvariant()
    $builder = New-Object System.Text.StringBuilder
    $index = 0
    while ($index -lt $text.Length) {
        $width = 1
        if ([char]::IsSurrogatePair($text, $index)) { $width = 2 }
        $category = [Globalization.CharUnicodeInfo]::GetUnicodeCategory($text, $index)
        $keep = $category -in @(
            [Globalization.UnicodeCategory]::UppercaseLetter,
            [Globalization.UnicodeCategory]::LowercaseLetter,
            [Globalization.UnicodeCategory]::TitlecaseLetter,
            [Globalization.UnicodeCategory]::ModifierLetter,
            [Globalization.UnicodeCategory]::OtherLetter,
            [Globalization.UnicodeCategory]::NonSpacingMark,
            [Globalization.UnicodeCategory]::SpacingCombiningMark,
            [Globalization.UnicodeCategory]::EnclosingMark,
            [Globalization.UnicodeCategory]::DecimalDigitNumber
        )
        if ($keep) { [void]$builder.Append($text.Substring($index, $width)) } else { [void]$builder.Append(' ') }
        $index += $width
    }
    $tokens = [string[]]@($builder.ToString().Split([char[]]@(' '), [StringSplitOptions]::RemoveEmptyEntries))
    if ($tokens.Count -eq 0) { return "" }
    [Array]::Sort($tokens, [StringComparer]::Ordinal)
    return [string]::Join(" ", $tokens)
}

function Test-XbAc2SyntheticName {
    param([AllowNull()][string]$Name)
    if ($null -eq $Name) { return $false }
    return $Name.StartsWith($script:XbAc2SyntheticNamePrefix, [StringComparison]::Ordinal)
}

function Test-XbAc2SyntheticEmail {
    param([AllowNull()][string]$Email)
    if ($null -eq $Email) { return $false }
    return $Email.EndsWith($script:XbAc2SyntheticEmailSuffix, [StringComparison]::OrdinalIgnoreCase)
}

function Test-XbAc2SameText {
    param([AllowNull()][string]$Left, [AllowNull()][string]$Right)
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) { return $false }
    return [string]::Equals($Left.Trim(), $Right.Trim(), [StringComparison]::OrdinalIgnoreCase)
}

# ---------------------------------------------------------------------------
# Result construction (closed primitive output object)
# ---------------------------------------------------------------------------

function New-XbAc2PrimitiveResult {
    param(
        [Parameter(Mandatory)][string]$Outcome,
        [string]$Rule = "NONE",
        [string]$Branch = "NONE",
        [AllowNull()][string]$MemberNo = $null,
        [AllowNull()][string]$MemberGuid = $null,
        [bool]$SaveInvoked = $false,
        [AllowNull()]$Readback = $null,
        [AllowNull()][string]$ReasonCode = $null,
        [string[]]$DqFlags = @(),
        [AllowNull()][string]$ErrorCode = $null,
        [Parameter(Mandatory)][string]$ReleaseSha256
    )
    $flags = New-Object System.Collections.Generic.List[string]
    foreach ($flag in $script:XbAc2DqFlagOrder) { if (@($DqFlags) -contains $flag) { $flags.Add($flag) } }
    $readbackValue = $null
    if ($null -ne $Readback) {
        $readbackValue = [ordered]@{
            found = [bool]$Readback.found
            match = [bool]$Readback.match
            created_by_integration_user = [bool]$Readback.created_by_integration_user
        }
    }
    [ordered]@{
        outcome = $Outcome
        rule = $Rule
        branch = $Branch
        member_no = $(if ([string]::IsNullOrEmpty($MemberNo)) { $null } else { $MemberNo })
        member_guid = $(if ([string]::IsNullOrEmpty($MemberGuid)) { $null } else { $MemberGuid })
        save_invoked = $SaveInvoked
        save_invocation_count = $(if ($SaveInvoked) { 1 } else { 0 })
        readback = $readbackValue
        reason_code = $(if ([string]::IsNullOrEmpty($ReasonCode)) { $null } else { $ReasonCode })
        dq_flags = [object[]]$flags.ToArray()
        primitive = [ordered]@{ release_sha256 = $ReleaseSha256; rule_version = "XB-MN-1" }
        error_code = $(if ([string]::IsNullOrEmpty($ErrorCode)) { $null } else { $ErrorCode })
    }
}

function ConvertTo-XbAc2AsciiJson {
    param([Parameter(Mandatory)]$Value)
    $json = $Value | ConvertTo-Json -Depth 8 -Compress
    $builder = New-Object System.Text.StringBuilder
    foreach ($ch in $json.ToCharArray()) {
        if ([int]$ch -gt 126) { [void]$builder.AppendFormat("\u{0:x4}", [int]$ch) } else { [void]$builder.Append($ch) }
    }
    return $builder.ToString()
}

# ---------------------------------------------------------------------------
# Request validation (contract 2.7 step 1, 2.2, 2.3 as amended)
# ---------------------------------------------------------------------------

function Test-XbAc2IsoDate {
    param([AllowNull()][object]$Value)
    if ($Value -isnot [string] -or $Value -cnotmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}$') { return $false }
    [datetime]$parsed = [datetime]::MinValue
    return [datetime]::TryParseExact($Value, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$parsed)
}

function ConvertTo-XbAc2UtcTimestamp {
    param([AllowNull()][object]$Value)
    if ($Value -is [datetime]) { return ([DateTimeOffset]$Value).ToUniversalTime() }
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value)) { return $null }
    [DateTimeOffset]$parsed = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse($Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind, [ref]$parsed)) { return $null }
    return $parsed.ToUniversalTime()
}

# Returns $null when the request is a valid closed object, else the reason.
function Test-XbAc2RequestShape {
    param([AllowNull()]$Request)
    if ($null -eq $Request -or $Request -isnot [pscustomobject]) { return "request_not_object" }
    $names = @($Request.PSObject.Properties.Name)
    if ($names.Count -ne $script:XbAc2RequestFields.Count) { return "request_fields_invalid" }
    foreach ($field in $script:XbAc2RequestFields) { if ($names -cnotcontains $field) { return "request_fields_invalid" } }
    $r = $Request
    if ($r.rule -cne "XB-MN-1") { return "request_rule_invalid" }
    if ($r.base_member_no -isnot [string] -or $r.base_member_no -cnotmatch '^[0-9]{6,15}$') { return "request_base_invalid" }
    if ($r.name_component -isnot [string]) { return "request_component_invalid" }
    foreach ($field in @("phone", "name", "email")) {
        $value = $r.$field
        if ($value -isnot [string] -or $value.Length -lt 1) { return "request_text_invalid" }
    }
    if ($r.phone.Length -gt 64 -or $r.email.Length -gt 254) { return "request_text_invalid" }
    if ($r.MemberType -cne "Default") { return "request_member_type_invalid" }
    foreach ($field in @("DOB", "RegisterDate", "ExpiryDate")) { if (-not (Test-XbAc2IsoDate $r.$field)) { return "request_date_invalid" } }
    if ($r.OpeningPoints -isnot [int] -or [int]$r.OpeningPoints -ne 0) { return "request_opening_points_invalid" }
    if ($r.IsActive -isnot [bool] -or -not $r.IsActive -or $r.Individual -isnot [bool] -or -not $r.Individual) { return "request_flags_invalid" }
    if ($r.job_id -isnot [string] -or $r.job_id -cnotmatch '^job-[A-Za-z0-9]{16,64}$') { return "request_job_invalid" }
    if (($r.attempt_no -isnot [int] -and $r.attempt_no -isnot [long]) -or [long]$r.attempt_no -lt 1) { return "request_attempt_invalid" }
    if ($null -eq (ConvertTo-XbAc2UtcTimestamp $r.first_claimed_at) -or $null -eq (ConvertTo-XbAc2UtcTimestamp $r.server_time_utc)) { return "request_time_invalid" }
    return $null
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

function Get-XbAc2PrimitiveEnvironmentConfig {
    $read = { param($name) [Environment]::GetEnvironmentVariable($name, "Process") }
    $allowRaw = & $read "XB_AC2_TEST_BOOK_ALLOWLIST"
    $allow = @()
    if (-not [string]::IsNullOrWhiteSpace($allowRaw)) {
        $allow = @($allowRaw.Split([char[]]@(';'), [StringSplitOptions]::RemoveEmptyEntries) | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
    }
    [pscustomobject]@{
        DatabaseName = [string](& $read "XB_AC2_DATABASE_NAME")
        LoginUserId = [string](& $read "XB_AC2_USER_ID")
        IntegrationUserId = [string](& $read "XB_AC2_INTEGRATION_USER_ID")
        ProductionBook = [string](& $read "XB_AC2_PRODUCTION_BOOK")
        TestBookAllowlist = [string[]]$allow
        Fault = [string](& $read "XB_AC2_FAULT")
        PasswordEnvironmentVariable = [string](& $read "XB_AC2_PASSWORD_ENV_VAR")
    }
}

function Test-XbAc2BookAllowlisted {
    param([string]$DatabaseName, [string[]]$Allowlist)
    foreach ($entry in @($Allowlist)) { if (Test-XbAc2SameText $entry $DatabaseName) { return $true } }
    return $false
}

# Book binding for the explicit mode. Returns $null or the reason code.
function Test-XbAc2BookBinding {
    param([Parameter(Mandatory)][string]$Book, [Parameter(Mandatory)]$Config)
    if ([string]::IsNullOrWhiteSpace($Config.DatabaseName) -or [string]::IsNullOrWhiteSpace($Config.ProductionBook)) { return "primitive_config_invalid" }
    if ($Book -ceq "production") {
        if (-not (Test-XbAc2SameText $Config.DatabaseName $Config.ProductionBook)) { return "book_binding_mismatch" }
        return $null
    }
    if (Test-XbAc2SameText $Config.DatabaseName $Config.ProductionBook) { return "book_binding_mismatch" }
    if (-not (Test-XbAc2BookAllowlisted -DatabaseName $Config.DatabaseName -Allowlist $Config.TestBookAllowlist)) { return "book_binding_mismatch" }
    return $null
}

# ---------------------------------------------------------------------------
# Projection and decision (contract 2.4, 2.5, 2.6)
# ---------------------------------------------------------------------------

function Get-XbAc2Projection {
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Rows, [Parameter(Mandatory)]$Request)
    $base = [string]$Request.base_member_no
    $emReq = Get-XbAc2EmailKey $Request.email
    $nmReq = Get-XbAc2NameKey $Request.name
    $projected = New-Object System.Collections.Generic.List[object]
    $malformed = 0
    foreach ($row in @($Rows)) {
        $p = New-Object System.Collections.Generic.List[string]
        if (Test-XbAc2MalformedMemberNo $row.MemberNo) { $malformed++ }
        else {
            $digitsNo = Get-XbAc2Digits $row.MemberNo
            if ($digitsNo -ne "") { $p.Add($digitsNo) }
        }
        $digitsPhone = Get-XbAc2Digits $row.MobilePhone
        if ($digitsPhone -ne "" -and -not $p.Contains($digitsPhone)) { $p.Add($digitsPhone) }
        $inH = $p.Contains($base)
        $inV = $false
        if (-not $inH -and $base.Length -ge 8) {
            foreach ($q in $p) {
                if ($q.Length -ge 8 -and ($q.EndsWith($base, [StringComparison]::Ordinal) -or $base.EndsWith($q, [StringComparison]::Ordinal))) { $inV = $true; break }
            }
        }
        $emRow = Get-XbAc2EmailKey $row.EmailAddress
        $nmRow = Get-XbAc2NameKey $row.Name
        $same = (($emReq -ne "" -and $emReq -ceq $emRow) -or ($nmReq -ne "" -and $nmReq -ceq $nmRow))
        $projected.Add([pscustomobject]@{
            Row = $row
            InH = $inH
            InV = $inV
            SamePerson = $same
            EmailOnly = ((-not $inH) -and (-not $inV) -and $emReq -ne "" -and $emReq -ceq $emRow)
            IdentityUnknown = ($emRow -eq "" -and $nmRow -eq "")
            Mnu = Get-XbAc2MemberNoUpper $row.MemberNo
        })
    }
    $hv = @($projected | Where-Object { $_.InH -or $_.InV })
    $flags = New-Object System.Collections.Generic.List[string]
    if (@($projected | Where-Object { $_.EmailOnly }).Count -gt 0) { $flags.Add("email_seen_on_other_member") }
    if ($malformed -gt 0) { $flags.Add("malformed_member_no_excluded") }
    [pscustomobject]@{
        All = [object[]]$projected.ToArray()
        HV = [object[]]$hv
        V = [object[]]@($hv | Where-Object { $_.InV })
        H = [object[]]@($hv | Where-Object { $_.InH })
        Flags = [string[]]$flags.ToArray()
        MalformedCount = $malformed
    }
}

function Test-XbAc2RowActive {
    param([AllowNull()][object]$Value)
    return ((Convert-XbAutoCountNormalizedValue -Value $Value -Field "IsActive") -ceq "T")
}

# First-match decision over R0, R1, R2a, R2b, R2c, R3, R3b, R4.
# Returns Action (CREATE | PRIOR_CHECK | LINK | REVIEW), Rule, Reason, target.
function Get-XbAc2Decision {
    param(
        [Parameter(Mandatory)]$Projection,
        [Parameter(Mandatory)]$Request,
        [Parameter(Mandatory)][string]$IntegrationUserId,
        [Parameter(Mandatory)][datetime]$WindowStartLocal
    )
    $base = [string]$Request.base_member_no
    $comp = [string]$Request.name_component
    $hv = @($Projection.HV)
    # R0: attempt >= 2 and our own recent rows exist in H union V.
    if ([long]$Request.attempt_no -ge 2) {
        $q = @($hv | Where-Object {
            $createdTime = $_.Row.CreatedTime
            (Test-XbAc2SameText ([string]$_.Row.CreatedUserID) $IntegrationUserId) -and
                ($createdTime -is [datetime]) -and
                ($createdTime -ge $WindowStartLocal)
        })
        if ($q.Count -gt 0) {
            if ($q.Count -eq 1) {
                $candidate = ([string]$q[0].Row.MemberNo).Trim()
                if ($candidate -ceq $base -or ($comp -ne "" -and $candidate -ceq ($base + $comp))) {
                    return [pscustomobject]@{ Action = "PRIOR_CHECK"; Rule = "R0"; Reason = $null; MemberNo = $candidate; Target = $q[0].Row }
                }
            }
            return [pscustomobject]@{ Action = "REVIEW"; Rule = "R0"; Reason = "prior_attempt_ambiguous"; MemberNo = $null; Target = $null }
        }
    }
    # R1: nothing holds this number, exactly or as a variant.
    if ($hv.Count -eq 0) {
        return [pscustomobject]@{ Action = "CREATE"; Rule = "R1"; Reason = $null; MemberNo = $base; Target = $null }
    }
    $same = @($hv | Where-Object { $_.SamePerson })
    if ($same.Count -ge 2) {
        return [pscustomobject]@{ Action = "REVIEW"; Rule = "R2a"; Reason = "multiple_same_person"; MemberNo = $null; Target = $null }
    }
    if ($same.Count -eq 1) {
        if (-not (Test-XbAc2RowActive $same[0].Row.IsActive)) {
            return [pscustomobject]@{ Action = "REVIEW"; Rule = "R2b"; Reason = "inactive_match"; MemberNo = $null; Target = $null }
        }
        return [pscustomobject]@{ Action = "LINK"; Rule = "R2c"; Reason = $null; MemberNo = ([string]$same[0].Row.MemberNo).Trim(); Target = $same[0].Row }
    }
    if (@($Projection.V).Count -gt 0) {
        return [pscustomobject]@{ Action = "REVIEW"; Rule = "R3"; Reason = "format_variant_other_person"; MemberNo = $null; Target = $null }
    }
    if (@($Projection.H | Where-Object { $_.IdentityUnknown }).Count -gt 0) {
        return [pscustomobject]@{ Action = "REVIEW"; Rule = "R3b"; Reason = "holder_identity_unknown"; MemberNo = $null; Target = $null }
    }
    # R4: every holder is clearly another person.
    if ($comp -eq "") {
        return [pscustomobject]@{ Action = "REVIEW"; Rule = "R4"; Reason = "name_component_empty"; MemberNo = $null; Target = $null }
    }
    $candidateUpper = Get-XbAc2MemberNoUpper ($base + $comp)
    if (@($Projection.All | Where-Object { $_.Mnu -ceq $candidateUpper }).Count -gt 0) {
        return [pscustomobject]@{ Action = "REVIEW"; Rule = "R4"; Reason = "name_candidate_collision"; MemberNo = $null; Target = $null }
    }
    return [pscustomobject]@{ Action = "CREATE"; Rule = "R4"; Reason = $null; MemberNo = ($base + $comp); Target = $null }
}

function Get-XbAc2MemberFields {
    param([Parameter(Mandatory)]$Request, [Parameter(Mandatory)][string]$MemberNo)
    return @{
        MemberNo = $MemberNo
        MemberType = "Default"
        Name = [string]$Request.name
        MobilePhone = [string]$Request.base_member_no
        EmailAddress = [string]$Request.email
        DOB = [string]$Request.DOB
        RegisterDate = [string]$Request.RegisterDate
        ExpiryDate = [string]$Request.ExpiryDate
        OpeningPoints = 0
    }
}

function Get-XbAc2BranchFor {
    param([Parameter(Mandatory)]$Request, [AllowNull()][string]$MemberNo)
    if ([string]$MemberNo -ceq [string]$Request.base_member_no) { return "BASE" }
    if ([string]$Request.name_component -ne "" -and [string]$MemberNo -ceq ([string]$Request.base_member_no + [string]$Request.name_component)) { return "NAME_APPENDED" }
    return "NONE"
}

# ---------------------------------------------------------------------------
# Fault hooks (test book only)
# ---------------------------------------------------------------------------

function Invoke-XbAc2FaultPoint {
    param([AllowNull()][string]$Fault, [Parameter(Mandatory)][string]$Point)
    if ([string]::IsNullOrEmpty($Fault)) { return }
    if ($Point -ceq "after_save") {
        if ($Fault -ceq "exit_after_save") { [Environment]::Exit(97) }
        if ($Fault -ceq "hang_after_save") { Start-Sleep -Seconds 900 }
        if ($Fault -ceq "save_error_after_commit") { throw "fault_save_error_after_commit" }
    }
    if ($Point -ceq "readback" -and $Fault -ceq "readback_error") { throw "fault_readback_error" }
}

# ---------------------------------------------------------------------------
# Primitive
# ---------------------------------------------------------------------------

function Invoke-XbAc2MemberCreatePrimitive {
    param(
        [AllowNull()][AllowEmptyString()][string]$RequestLine,
        [AllowNull()][AllowEmptyString()][string]$Book,
        [Parameter(Mandatory)]$Config,
        [switch]$EnableProductionAdapter,
        [scriptblock]$SessionFactory,
        [scriptblock]$MemberCommandFactory,
        [string]$MutexName = $script:XbAc2MemberCreateMutexName,
        [ValidateRange(0, 30000)][int]$MutexWaitMilliseconds = 30000,
        [AllowNull()]$UtcNow = $null,
        [string]$PackageRoot = $script:XbAc2PrimitivePackageRoot
    )
    $release = Get-XbAc2ReleaseIdentity -PackageRoot $PackageRoot
    $state = @{ save_invoked = $false; flags = @() }
    $mutex = $null
    $owned = $false
    try {
        # Step 1: explicit book, request shape, identity, limits, synthetic rules.
        if ($Book -cne "production" -and $Book -cne "test") {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "primitive_config_invalid" -ErrorCode "book_mode_invalid" -ReleaseSha256 $release)
        }
        $request = $null
        try { $request = $RequestLine | ConvertFrom-Json -ErrorAction Stop } catch { $request = $null }
        $shapeError = Test-XbAc2RequestShape -Request $request
        if ($null -ne $shapeError) {
            return (New-XbAc2PrimitiveResult -Outcome "MANUAL_REVIEW" -ReasonCode "request_contract_violation" -ErrorCode $shapeError -ReleaseSha256 $release)
        }
        $base = [string]$request.base_member_no
        $comp = [string]$request.name_component
        if ((Get-XbAc2Digits $request.phone) -cne $base) {
            return (New-XbAc2PrimitiveResult -Outcome "MANUAL_REVIEW" -ReasonCode "request_contract_violation" -ErrorCode "request_phone_base_mismatch" -ReleaseSha256 $release)
        }
        if ($comp -cnotmatch '^[A-Z]{0,14}$' -or ($base.Length + $comp.Length) -gt 20) {
            return (New-XbAc2PrimitiveResult -Outcome "MANUAL_REVIEW" -ReasonCode "request_contract_violation" -ErrorCode "request_component_invalid" -ReleaseSha256 $release)
        }
        # AutoCount limits are UTF-16 code units (.NET string length).
        if (([string]$request.name).Length -gt 100) {
            return (New-XbAc2PrimitiveResult -Outcome "REJECTED_VALIDATION" -ReasonCode "name_exceeds_autocount_limit" -ReleaseSha256 $release)
        }
        if (([string]$request.email).Length -gt 200) {
            return (New-XbAc2PrimitiveResult -Outcome "REJECTED_VALIDATION" -ReasonCode "email_exceeds_autocount_limit" -ReleaseSha256 $release)
        }
        $syntheticName = Test-XbAc2SyntheticName ([string]$request.name)
        $syntheticEmail = Test-XbAc2SyntheticEmail ([string]$request.email)
        $fullySynthetic = $syntheticName -and $syntheticEmail
        if ($Book -ceq "production" -and ($syntheticName -or $syntheticEmail)) {
            return (New-XbAc2PrimitiveResult -Outcome "REJECTED_VALIDATION" -ReasonCode "synthetic_in_production" -ReleaseSha256 $release)
        }
        if ($Book -ceq "test" -and -not $fullySynthetic) {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "test_book_requires_synthetic" -ReleaseSha256 $release)
        }

        # Fault hooks are inert unless test mode + allowlisted non-production book + fully synthetic.
        $fault = [string]$Config.Fault
        if (-not [string]::IsNullOrEmpty($fault)) {
            $guard = ($Book -ceq "test") -and $fullySynthetic -and
                (-not [string]::IsNullOrWhiteSpace($Config.ProductionBook)) -and
                (-not (Test-XbAc2SameText $Config.DatabaseName $Config.ProductionBook)) -and
                (Test-XbAc2BookAllowlisted -DatabaseName $Config.DatabaseName -Allowlist $Config.TestBookAllowlist) -and
                ($script:XbAc2AllowedFaults -ccontains $fault)
            if (-not $guard) {
                return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "fault_injection_refused" -ReleaseSha256 $release)
            }
        }

        # Step 2: clock.
        $now = $(if ($null -eq $UtcNow) { [DateTimeOffset]::UtcNow } else { ([DateTimeOffset]$UtcNow).ToUniversalTime() })
        $serverTime = ConvertTo-XbAc2UtcTimestamp $request.server_time_utc
        if ([Math]::Abs(($now - $serverTime).TotalSeconds) -gt $script:XbAc2ClockSkewSeconds) {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "clock_skew" -ReleaseSha256 $release)
        }

        # Configured book and integration-user binding for the explicit mode.
        $bookError = Test-XbAc2BookBinding -Book $Book -Config $Config
        if ($null -ne $bookError) {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode $bookError -ReleaseSha256 $release)
        }
        if ([string]::IsNullOrWhiteSpace($Config.IntegrationUserId) -or [string]::IsNullOrWhiteSpace($Config.LoginUserId)) {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "primitive_config_invalid" -ErrorCode "integration_user_unset" -ReleaseSha256 $release)
        }
        if (-not (Test-XbAc2SameText $Config.LoginUserId $Config.IntegrationUserId)) {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "integration_user_mismatch" -ReleaseSha256 $release)
        }

        # Step 3: the named mutex, held through readback.
        try { $mutex = New-Object System.Threading.Mutex($false, $MutexName) }
        catch {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "mutex_unavailable" -ReleaseSha256 $release)
        }
        try { $owned = $mutex.WaitOne($MutexWaitMilliseconds) }
        catch [System.Threading.AbandonedMutexException] { $owned = $true }
        catch {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "mutex_unavailable" -ReleaseSha256 $release)
        }
        if (-not $owned) {
            return (New-XbAc2PrimitiveResult -Outcome "MUTEX_BUSY" -ReleaseSha256 $release)
        }

        # Step 4: integration-user session bound to the configured book.
        $session = $null
        try {
            $session = New-XbAutoCountSession -EnableProductionAdapter:$EnableProductionAdapter -SessionFactory $SessionFactory
        }
        catch { $session = $null }
        finally {
            if (-not [string]::IsNullOrWhiteSpace($Config.PasswordEnvironmentVariable) -and $Config.PasswordEnvironmentVariable -match '^[A-Za-z_][A-Za-z0-9_]*$') {
                [Environment]::SetEnvironmentVariable($Config.PasswordEnvironmentVariable, $null, "Process")
            }
        }
        if ($null -eq $session) {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "session_unavailable" -ReleaseSha256 $release)
        }
        if (-not (Test-XbAc2SameText ([string]$session.DatabaseName) $Config.DatabaseName)) {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "book_binding_mismatch" -ErrorCode "session_book_mismatch" -ReleaseSha256 $release)
        }
        if (-not (Test-XbAc2SameText ([string]$session.LoginUserId) $Config.IntegrationUserId)) {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "integration_user_mismatch" -ErrorCode "session_user_mismatch" -ReleaseSha256 $release)
        }
        $iu = [string]$Config.IntegrationUserId

        # Step 5: probe and decide.
        $rows = $null
        try { $rows = Get-XbAutoCountMemberProbeRows -Session $session -MemberCommandFactory $MemberCommandFactory }
        catch {
            return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "probe_unavailable" -ReleaseSha256 $release)
        }
        $projection = Get-XbAc2Projection -Rows @($rows) -Request $request
        $state.flags = @($projection.Flags)
        $firstClaimed = ConvertTo-XbAc2UtcTimestamp $request.first_claimed_at
        $windowStartLocal = $firstClaimed.AddMinutes(-$script:XbAc2PriorAttemptWindowMinutes).ToLocalTime().DateTime
        $decision = Get-XbAc2Decision -Projection $projection -Request $request -IntegrationUserId $iu -WindowStartLocal $windowStartLocal

        if ($decision.Action -ceq "REVIEW") {
            return (New-XbAc2PrimitiveResult -Outcome "MANUAL_REVIEW" -Rule $decision.Rule -ReasonCode $decision.Reason -DqFlags $state.flags -ReleaseSha256 $release)
        }
        if ($decision.Action -ceq "LINK") {
            $guid = (Get-XbAutoCountMemberAudit -Entity $decision.Target).Guid
            if ($guid -eq "") {
                return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -Rule "R2c" -ReasonCode "probe_unavailable" -ErrorCode "probe_guid_missing" -DqFlags $state.flags -ReleaseSha256 $release)
            }
            return (New-XbAc2PrimitiveResult -Outcome "LINKED_EXISTING" -Rule "R2c" -Branch "EXISTING" -MemberNo $decision.MemberNo -MemberGuid $guid -DqFlags $state.flags -ReleaseSha256 $release)
        }
        if ($decision.Action -ceq "PRIOR_CHECK") {
            # R0: exactly one own recent row with an exact MemberNo; all 11 fields must equal.
            $existing = $null
            try { $existing = Get-XbAutoCountMember -Session $session -MemberNo $decision.MemberNo -MemberCommandFactory $MemberCommandFactory }
            catch {
                return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -Rule "R0" -ReasonCode "probe_unavailable" -ErrorCode "prior_attempt_read_failed" -DqFlags $state.flags -ReleaseSha256 $release)
            }
            $expected = Get-XbAutoCountExpectedMemberRecord -Member (Get-XbAc2MemberFields -Request $request -MemberNo $decision.MemberNo)
            $check = Compare-XbAutoCountMemberReadBack -Expected $expected -Actual $existing
            $audit = Get-XbAutoCountMemberAudit -Entity $existing
            if ($check.Found -and $check.Match -and $null -ne $audit -and $audit.Guid -ne "" -and
                (Test-XbAc2SameText $audit.CreatedUserID $iu) -and
                ($audit.CreatedTime -is [datetime]) -and
                ($audit.CreatedTime -ge $windowStartLocal)) {
                return (New-XbAc2PrimitiveResult -Outcome "CREATED_VERIFIED_PRIOR_ATTEMPT" -Rule "R0" -Branch (Get-XbAc2BranchFor -Request $request -MemberNo $decision.MemberNo) -MemberNo $decision.MemberNo -MemberGuid $audit.Guid -DqFlags $state.flags -ReleaseSha256 $release)
            }
            return (New-XbAc2PrimitiveResult -Outcome "MANUAL_REVIEW" -Rule "R0" -ReasonCode "prior_attempt_ambiguous" -DqFlags $state.flags -ReleaseSha256 $release)
        }

        # Step 6: CREATE. NewMember(false) + adapter fill; then exactly one save.
        $memberNo = [string]$decision.MemberNo
        $rule = [string]$decision.Rule
        $branch = Get-XbAc2BranchFor -Request $request -MemberNo $memberNo
        $prepared = New-XbAutoCountMemberEntity -Session $session -Member (Get-XbAc2MemberFields -Request $request -MemberNo $memberNo) -EnableProductionAdapter:$EnableProductionAdapter -MemberCommandFactory $MemberCommandFactory
        $state.save_invoked = $true
        $saveReturned = Invoke-XbAutoCountSaveMember -Prepared $prepared -EnableProductionAdapter:$EnableProductionAdapter
        if ($saveReturned) {
            try { Invoke-XbAc2FaultPoint -Fault $fault -Point "after_save" } catch { $saveReturned = $false }
        }

        # Step 7: readback and 4.2 classification.
        $readbackEntity = $null
        try {
            Invoke-XbAc2FaultPoint -Fault $fault -Point "readback"
            $readbackEntity = Get-XbAutoCountMember -Session $session -MemberNo $memberNo -MemberCommandFactory $MemberCommandFactory
        }
        catch {
            return (New-XbAc2PrimitiveResult -Outcome "OUTCOME_UNCERTAIN" -Rule $rule -Branch $branch -MemberNo $memberNo -SaveInvoked $true -ReasonCode "readback_unavailable" -DqFlags $state.flags -ReleaseSha256 $release)
        }
        $saveError = $(if ($saveReturned) { $null } else { "save_threw" })
        if ($null -eq $readbackEntity) {
            if ($saveReturned) {
                return (New-XbAc2PrimitiveResult -Outcome "OUTCOME_UNCERTAIN" -Rule $rule -Branch $branch -MemberNo $memberNo -SaveInvoked $true -Readback @{ found = $false; match = $false; created_by_integration_user = $false } -ReasonCode "readback_absent_after_save" -DqFlags $state.flags -ReleaseSha256 $release)
            }
            return (New-XbAc2PrimitiveResult -Outcome "NOT_CREATED" -Rule $rule -Branch $branch -MemberNo $memberNo -SaveInvoked $true -Readback @{ found = $false; match = $false; created_by_integration_user = $false } -DqFlags $state.flags -ErrorCode $saveError -ReleaseSha256 $release)
        }
        $check = Compare-XbAutoCountMemberReadBack -Expected $prepared.Expected -Actual $readbackEntity
        $audit = Get-XbAutoCountMemberAudit -Entity $readbackEntity
        $byIu = Test-XbAc2SameText $audit.CreatedUserID $iu
        $readback = @{ found = $true; match = [bool]$check.Match; created_by_integration_user = $byIu }
        if (-not $byIu) {
            if ($saveReturned) {
                return (New-XbAc2PrimitiveResult -Outcome "MANUAL_REVIEW" -Rule $rule -Branch $branch -MemberNo $memberNo -SaveInvoked $true -Readback $readback -ReasonCode "readback_foreign_row" -DqFlags $state.flags -ReleaseSha256 $release)
            }
            return (New-XbAc2PrimitiveResult -Outcome "NOT_CREATED_CONFLICT" -Rule $rule -Branch $branch -MemberNo $memberNo -SaveInvoked $true -Readback $readback -DqFlags $state.flags -ErrorCode $saveError -ReleaseSha256 $release)
        }
        if (-not $check.Match) {
            return (New-XbAc2PrimitiveResult -Outcome "CREATED_READBACK_MISMATCH" -Rule $rule -Branch $branch -MemberNo $memberNo -MemberGuid $audit.Guid -SaveInvoked $true -Readback $readback -DqFlags $state.flags -ErrorCode $saveError -ReleaseSha256 $release)
        }
        if ($audit.Guid -eq "") {
            return (New-XbAc2PrimitiveResult -Outcome "CREATED_READBACK_MISMATCH" -Rule $rule -Branch $branch -MemberNo $memberNo -SaveInvoked $true -Readback $readback -DqFlags $state.flags -ErrorCode "readback_guid_missing" -ReleaseSha256 $release)
        }

        # Step 8: staff-duplicate report on CREATED_VERIFIED only (no state change).
        $flags = @($state.flags)
        try {
            $afterRows = Get-XbAutoCountMemberProbeRows -Session $session -MemberCommandFactory $MemberCommandFactory
            $after = Get-XbAc2Projection -Rows @($afterRows) -Request $request
            $createdUpper = Get-XbAc2MemberNoUpper $memberNo
            if (@($after.HV | Where-Object { $_.SamePerson -and $_.Mnu -cne $createdUpper }).Count -gt 0) { $flags += "post_save_same_person_other_row" }
        }
        catch { }
        return (New-XbAc2PrimitiveResult -Outcome "CREATED_VERIFIED" -Rule $rule -Branch $branch -MemberNo $memberNo -MemberGuid $audit.Guid -SaveInvoked $true -Readback $readback -DqFlags $flags -ErrorCode $saveError -ReleaseSha256 $release)
    }
    catch {
        if ($state.save_invoked) {
            return (New-XbAc2PrimitiveResult -Outcome "OUTCOME_UNCERTAIN" -SaveInvoked $true -ReasonCode "unexpected_error" -DqFlags $state.flags -ReleaseSha256 $release)
        }
        return (New-XbAc2PrimitiveResult -Outcome "FAILED_BEFORE_WRITE" -ReasonCode "unexpected_error" -DqFlags $state.flags -ReleaseSha256 $release)
    }
    finally {
        # Step 9: release the lock (process death also releases it).
        if ($null -ne $mutex) {
            if ($owned) { try { $mutex.ReleaseMutex() } catch { } }
            $mutex.Dispose()
        }
    }
}

if ($LibraryOnly) { return }

$ErrorActionPreference = "Stop"
$line = [Console]::In.ReadLine()
$result = Invoke-XbAc2MemberCreatePrimitive -RequestLine $line -Book $Book -Config (Get-XbAc2PrimitiveEnvironmentConfig) -EnableProductionAdapter:$EnableProductionAdapter
[Console]::Out.WriteLine((ConvertTo-XbAc2AsciiJson -Value $result))
exit 0
