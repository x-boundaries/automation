# Pure, dot-sourceable helper library for the synthetic ExpiryDate capability probe.
#
# This library contains NO AutoCount calls, no network, and no live action. It is
# dot-sourced by scripts/ac2_member_expiry_capability_probe.ps1 (which owns the
# AutoCount reflection and the single SaveMember call site) and is exercised directly
# by the tests without AutoCount. Every function here is deterministic given its
# inputs, so the terminal-state machine, the durable single-use attempt claim, the
# non-overwriting result writer, and the fingerprints can all be tested in isolation.

Set-StrictMode -Version Latest

$script:ExpiryProbeSchemaVersion = "member_expiry_capability_probe/v1"
$script:ExpiryProbeIntendedExpiry = "2028-06-30"

# The complete canonical terminal vocabulary. Every active-run outcome is exactly one
# of these; only EXPIRY_VERIFIED is a success.
$script:ExpiryProbeTerminalCodes = @(
    "REFUSED",
    "ATTEMPT_ALREADY_CLAIMED",
    "BLOCKED_MEMBER_EXISTS",
    "FAILED_BEFORE_WRITE",
    "WRITE_OUTCOME_UNCERTAIN",
    "WRITE_CONFIRMED_READBACK_FAILED",
    "EXPIRY_READBACK_MISMATCH",
    "EXPIRY_VERIFIED"
)
$script:ExpiryProbeSaveOutcomes = @("not_attempted", "confirmed", "uncertain")

# --------------------------------------------------------------------------- #
# Hashing / fingerprints (emit only fingerprints, never raw target/identity).
# --------------------------------------------------------------------------- #
function Get-ExpiryProbeSha256Hex {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        $hash = $sha.ComputeHash($bytes)
    }
    finally { $sha.Dispose() }
    -join ($hash | ForEach-Object { $_.ToString("x2") })
}

function Get-ExpiryProbeTargetFingerprint {
    # A non-secret SHA-256 over a canonical representation of the AutoCount target
    # (server + database/account book). The raw target values are never emitted.
    #
    # SQL Server instance names and AutoCount database/account-book names are
    # case-insensitive, so the canonical form is trimmed and lower-cased (invariant).
    # This makes casing variants (SERVER\INSTANCE vs server\instance) resolve to the
    # SAME target and attempt fingerprint, so the single-use claim is a real
    # concurrency/no-retry boundary regardless of how the target is spelled.
    param([Parameter(Mandatory)][AllowEmptyString()][string]$ServerName,
          [Parameter(Mandatory)][AllowEmptyString()][string]$DatabaseName)
    $canonical = "server=" + $ServerName.Trim().ToLowerInvariant() + "|database=" + $DatabaseName.Trim().ToLowerInvariant()
    "tfp_" + (Get-ExpiryProbeSha256Hex -Text $canonical)
}

function Get-ExpiryProbeSyntheticFingerprint {
    # A non-secret fingerprint of the synthetic member identifier, so evidence never
    # carries the raw synthetic member number.
    param([Parameter(Mandatory)][string]$MemberNo)
    "smf_" + (Get-ExpiryProbeSha256Hex -Text $MemberNo)
}

function Get-ExpiryProbeAttemptFingerprint {
    # The STABLE single-use key: identical across retries of the same logical operation
    # (same target, same synthetic record, same intended ExpiryDate), so a second
    # invocation finds the existing claim and refuses. Deliberately NOT derived from the
    # per-run operation_id, which changes every launch.
    param(
        [Parameter(Mandatory)][string]$TargetFingerprint,
        [Parameter(Mandatory)][string]$SyntheticFingerprint,
        [Parameter(Mandatory)][string]$IntendedExpiry
    )
    $canonical = "$script:ExpiryProbeSchemaVersion|$TargetFingerprint|$SyntheticFingerprint|$IntendedExpiry"
    "afp_" + (Get-ExpiryProbeSha256Hex -Text $canonical)
}

function Get-ExpiryProbeMaskedMemberNo {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$MemberNo)
    if ([string]::IsNullOrEmpty($MemberNo) -or $MemberNo.Length -le 2) { return "***" }
    $MemberNo.Substring(0, 2) + "***" + $MemberNo.Substring($MemberNo.Length - 1, 1)
}

function Get-ExpiryProbeNormalizedDate {
    # Normalise a date-shaped value to yyyy-MM-dd (or "" for null/blank), so the
    # read-back comparison is representation-independent (DateTime vs string).
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
# Path safety
# --------------------------------------------------------------------------- #
function Test-ExpiryProbeSafePath {
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
# Durable single-use attempt claim (P1) and non-overwriting result (P2).
# --------------------------------------------------------------------------- #
function New-ExpiryProbeDurableArtifact {
    # Exclusive-create a durable artefact. Fails closed if it already exists so a prior
    # claim/result is never overwritten. WriteThrough + Flush(true) push the bytes past
    # OS caches where the platform supports it. Never deletes anything.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Content)
    $stream = [System.IO.FileStream]::new(
        $Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None, 4096, [System.IO.FileOptions]::WriteThrough)
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Content)
        $stream.Write($bytes, 0, $bytes.Length)
        # Flush(true) forces the data to the storage device. Only fall back to the
        # ordinary Flush() for a runtime that genuinely does not support the durable
        # overload (NotSupportedException); any other flush failure (e.g. an I/O error
        # that cannot confirm durability) must propagate so the caller fails closed
        # BEFORE the irreversible save rather than proceeding on a non-durable claim.
        try { $stream.Flush($true) } catch [System.NotSupportedException] { $stream.Flush() }
    }
    finally { $stream.Dispose() }
}

function Write-ExpiryProbeResultAtomic {
    # Same-directory temp file + atomic move for the terminal result. Never overwrites a
    # prior result: fails closed if the final path already exists.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Content)
    if (Test-Path -LiteralPath $Path) { throw "Terminal result artefact already exists; refusing to overwrite evidence." }
    $temp = $Path + ".tmp"
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
    New-ExpiryProbeDurableArtifact -Path $temp -Content $Content
    [System.IO.File]::Move($temp, $Path)
}

function New-ExpiryProbeClaimContent {
    # Sanitised, non-secret claim/evidence metadata only. Never the raw server,
    # database, credentials, password, user, synthetic name, or synthetic email.
    param(
        [Parameter(Mandatory)][string]$OperationId,
        [Parameter(Mandatory)][string]$ApprovalReference,
        [Parameter(Mandatory)][string]$TargetFingerprint,
        [Parameter(Mandatory)][string]$SyntheticFingerprint,
        [Parameter(Mandatory)][string]$AttemptFingerprint,
        [Parameter(Mandatory)][string]$IntendedExpiry
    )
    [ordered]@{
        schema_version        = $script:ExpiryProbeSchemaVersion
        operation_id          = $OperationId
        approval_reference    = $ApprovalReference
        target_fingerprint    = $TargetFingerprint
        synthetic_fingerprint = $SyntheticFingerprint
        attempt_fingerprint   = $AttemptFingerprint
        intended_expiry_date  = $IntendedExpiry
        created_at_utc        = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    } | ConvertTo-Json -Depth 4
}

# --------------------------------------------------------------------------- #
# Centralised, pure terminal-state derivation (P1).
# --------------------------------------------------------------------------- #
function Get-ExpiryProbeFlag { param($Flags, [string]$Name, $Default = $null)
    if ($Flags -is [System.Collections.IDictionary]) {
        if ($Flags.Contains($Name)) { return $Flags[$Name] }
        return $Default
    }
    $p = $Flags.PSObject.Properties[$Name]
    if ($null -ne $p) { return $p.Value }
    return $Default
}

function Get-ExpiryProbeStateContradictions {
    # Return a list of impossible-flag reasons; empty means internally consistent.
    param([Parameter(Mandatory)]$Flags)
    $reasons = [System.Collections.Generic.List[string]]::new()
    $attempted = [bool](Get-ExpiryProbeFlag $Flags 'save_member_attempted' $false)
    $confirmed = [bool](Get-ExpiryProbeFlag $Flags 'save_member_confirmed' $false)
    $outcome = Get-ExpiryProbeFlag $Flags 'save_outcome' 'not_attempted'
    $rbFound = [bool](Get-ExpiryProbeFlag $Flags 'readback_found' $false)
    $match = [bool](Get-ExpiryProbeFlag $Flags 'expiry_match' $false)
    $claimConflict = [bool](Get-ExpiryProbeFlag $Flags 'claim_conflict' $false)
    $activated = [bool](Get-ExpiryProbeFlag $Flags 'activated' $false)
    $recheck = [bool](Get-ExpiryProbeFlag $Flags 'member_exists_recheck' $false)

    if ($script:ExpiryProbeSaveOutcomes -notcontains $outcome) { $reasons.Add('save_outcome_invalid') }
    if ($confirmed -and -not $attempted) { $reasons.Add('confirmed_without_attempt') }
    if ($outcome -eq 'confirmed' -and -not $confirmed) { $reasons.Add('outcome_confirmed_without_confirmed_flag') }
    if ($outcome -eq 'not_attempted' -and $attempted) { $reasons.Add('not_attempted_but_attempted') }
    if ($outcome -eq 'uncertain' -and -not $attempted) { $reasons.Add('uncertain_without_attempt') }
    if ($rbFound -and $outcome -ne 'confirmed') { $reasons.Add('readback_without_confirmed_save') }
    if ($match -and -not $rbFound) { $reasons.Add('match_without_found') }
    if ($attempted -and -not $activated) { $reasons.Add('attempt_without_activation') }
    if ($attempted -and $claimConflict) { $reasons.Add('attempt_with_claim_conflict') }
    if ($attempted -and $recheck) { $reasons.Add('recheck_block_after_attempt') }
    return $reasons.ToArray()
}

function Get-ExpiryProbeTerminalOutcome {
    # The single source of truth for the terminal outcome. Ordered so that once a save
    # was attempted (confirmed or uncertain) the result can ONLY be a post-save code:
    # FAILED_BEFORE_WRITE and BLOCKED_MEMBER_EXISTS are unreachable after SaveMember
    # begins. A confirmed save whose read-back throws or is not found becomes
    # WRITE_CONFIRMED_READBACK_FAILED, never FAILED_BEFORE_WRITE.
    param([Parameter(Mandatory)]$Flags)
    if (-not [bool](Get-ExpiryProbeFlag $Flags 'activated' $false)) { return 'REFUSED' }
    if ([bool](Get-ExpiryProbeFlag $Flags 'claim_conflict' $false)) { return 'ATTEMPT_ALREADY_CLAIMED' }
    $outcome = Get-ExpiryProbeFlag $Flags 'save_outcome' 'not_attempted'
    if ($outcome -eq 'confirmed') {
        if (-not [bool](Get-ExpiryProbeFlag $Flags 'readback_found' $false)) { return 'WRITE_CONFIRMED_READBACK_FAILED' }
        if ([bool](Get-ExpiryProbeFlag $Flags 'expiry_match' $false)) { return 'EXPIRY_VERIFIED' }
        return 'EXPIRY_READBACK_MISMATCH'
    }
    if ($outcome -eq 'uncertain') { return 'WRITE_OUTCOME_UNCERTAIN' }
    if ([bool](Get-ExpiryProbeFlag $Flags 'member_exists_initial' $false) -or
        [bool](Get-ExpiryProbeFlag $Flags 'member_exists_recheck' $false)) { return 'BLOCKED_MEMBER_EXISTS' }
    return 'FAILED_BEFORE_WRITE'
}

function Get-ExpiryProbeExitCode {
    # Truthful process exit status: 0 ONLY for EXPIRY_VERIFIED whose durable evidence was
    # persisted; nonzero for every other terminal outcome (including REFUSED) AND for a
    # verified run whose durable result could not be written. A wrapper that gates the
    # follow-up capability flip on exit 0 therefore never proceeds without retained audit
    # evidence.
    param([Parameter(Mandatory)][string]$TerminalOutcome, [bool]$EvidencePersisted = $true)
    if ($TerminalOutcome -eq 'EXPIRY_VERIFIED' -and $EvidencePersisted) { return 0 }
    return 1
}
