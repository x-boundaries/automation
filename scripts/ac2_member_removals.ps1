# #268 slice E: the removals tool's AutoCount child, profiles benefit_x1
# (removal) and benefit_x1r (restore, undo only).
#
# Dot-sourced by ac2_member_removals_bridge.ps1 after the gateway adapter
# (unchanged), the book guard and the #268 type move library (unchanged; its
# record, digest, request and type helpers are reused as they are).
#
# One move only (G0 slice E design, Controller decisions 22 Nov 2026):
#   - removal-save: MEMBER -> GUEST. kind "expire" (ExpiryDate before the run
#     day, on or after 2020-01-01) or "revoke" (the request carries the
#     upgrade's basis expiry, which must equal the expected one). No other
#     move exists: never from any type but MEMBER, never to any type but GUEST.
#   - removal-restore: GUEST -> MEMBER for one member this tool removed,
#     bound to that removal (its id travels in the payload digest).
#   - Staff win: the member is re-read just before SaveMember and saved only
#     if its type, LastModified and ExpiryDate still equal what the parent
#     read; otherwise it is skipped with a code, before the claim file.
#   - Only MemberType is assigned. ExpiryDate is never written: it is NOT in
#     the read-back exclusions (the type move's digest helper excludes only
#     MemberType, LastModified and LastModifiedUserID), so a changed
#     ExpiryDate is a read-back mismatch (HELD).
# The scan uses its own pinned SQL text (Get-XbMxScalar), behind the same
# ASCII and deny checks as the book guard's allowlist. The server time comes
# from the book guard's own Get-XbDlPosSyncState read.

Set-StrictMode -Version Latest

$script:XbMxProfile = "benefit_x1"
$script:XbMxRestoreProfile = "benefit_x1r"
$script:XbMxFromType = "MEMBER"
$script:XbMxToType = "GUEST"
$script:XbMxKinds = @("expire", "revoke")
$script:XbMxExpiryFloor = [datetime]::new(2020, 1, 1)
$script:XbMxStoredPattern = '\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}\z'
$script:XbMxDayPattern = '\A[0-9]{4}-[0-9]{2}-[0-9]{2}\z'
$script:XbMxSaltPattern = '\A[0-9a-f]{32}\z'
$script:XbMxRemovalIdPattern = '\Arm-[0-9a-f]{32}\z'
# One line of printable ASCII; "?" only as a placeholder (type, cursor, run
# day as yyyyMMdd, floor as yyyyMMdd). Pinned by tests/test_member_removals_ps.py.
$script:XbMxSqlText = [ordered]@{
    expiring_members_page = ("SELECT (SELECT TOP 500 MemberNo AS member_no, IsActive AS is_active, " +
        "CONVERT(varchar(23), ExpiryDate, 126) AS expiry_local, CONVERT(varchar(23), LastModified, 126) AS last_modified_local " +
        "FROM Member WHERE MemberType = ? AND MemberNo > ? AND (ExpiryDate < ? OR ExpiryDate IS NULL OR ExpiryDate < ?) " +
        "ORDER BY MemberNo FOR JSON PATH, INCLUDE_NULL_VALUES)")
}

function Get-XbMxScalar {
    # Read-only vendor query through the authenticated DBSetting: only the
    # text above, after the book guard's ASCII and deny checks.
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)][string]$Sql, [object[]]$Params = @())
    if ($Sql -cnotmatch '\A[\x20-\x7E]+\z') { throw "vendor_query_not_ascii" }
    if ($Sql -match $script:XbDlDeniedSqlPattern) { throw "vendor_query_not_read_only" }
    if ([regex]::Matches($Sql, '\?').Count -ne @($Params).Count) { throw "vendor_query_not_allowlisted" }
    $known = $false
    foreach ($text in $script:XbMxSqlText.Values) {
        if ([string]::Equals($Sql, [string]$text, [StringComparison]::Ordinal)) { $known = $true }
    }
    if (-not $known) { throw "vendor_query_not_allowlisted" }
    return $DBSetting.ExecuteScalar($Sql, [object[]]$Params)
}

function Get-XbMxServerNow {
    # The AutoCount server's local time (yyyy-MM-ddTHH:mm:ss) through the book
    # guard's own read (Get-XbDlPosSyncState, as the type move's counts read;
    # pinned guard texts only); the parent checks it against the VM clock.
    param([Parameter(Mandatory)]$DBSetting)
    try { return [string](Get-XbDlPosSyncState -DBSetting $DBSetting).server_now_local }
    catch { throw "pos_sync_state_unreadable" }
}

function ConvertFrom-XbMxStoredTime {
    # The exact text ConvertTo-XbTmValue gives a datetime (yyyy-MM-ddTHH:mm:ss.fff).
    param([AllowNull()][object]$Text)
    [datetime]$parsed = [datetime]::MinValue
    if ($Text -isnot [string] -or $Text -cnotmatch $script:XbMxStoredPattern -or
        -not [datetime]::TryParseExact($Text, "yyyy-MM-ddTHH:mm:ss.fff", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::None, [ref]$parsed)) {
        throw "removal_request_invalid"
    }
    return $parsed
}

function ConvertFrom-XbMxDay {
    param([AllowNull()][object]$Text)
    [datetime]$parsed = [datetime]::MinValue
    if ($Text -isnot [string] -or $Text -cnotmatch $script:XbMxDayPattern -or
        -not [datetime]::TryParseExact($Text, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::None, [ref]$parsed)) {
        throw "removal_request_invalid"
    }
    return $parsed
}

function Test-XbMxDue {
    # Valid through the expiry day: due only when the expiry's date is
    # strictly before the run day, and never for a placeholder date.
    param([Parameter(Mandatory)][datetime]$Expiry, [Parameter(Mandatory)][datetime]$RunDay)
    return ($Expiry.Date -ge $script:XbMxExpiryFloor -and $Expiry.Date -lt $RunDay.Date)
}

function Assert-XbMxSaveRequest {
    # Every value the removal uses, checked before any session or claim. The
    # one-move rule is enforced here, at the write boundary.
    param([Parameter(Mandatory)]$Request)
    Assert-XbTmMemberNo -MemberNo $Request.member_no
    foreach ($name in @("kind", "from_type", "to_type", "expected_last_modified_local", "expected_expiry_local", "salt")) {
        if ($Request.$name -isnot [string]) { throw "removal_request_invalid" }
    }
    if ($Request.salt -cnotmatch $script:XbMxSaltPattern -or $Request.expected_last_modified_local -cnotmatch $script:XbMxStoredPattern) { throw "removal_request_invalid" }
    if (-not (Test-XbDlOrdinalIn -Value $Request.kind -Set $script:XbMxKinds)) { throw "removal_request_invalid" }
    if (-not [string]::Equals($Request.from_type, $script:XbMxFromType, [StringComparison]::Ordinal)) { throw "removal_type_not_allowed" }
    if (-not [string]::Equals($Request.to_type, $script:XbMxToType, [StringComparison]::Ordinal)) { throw "removal_type_not_allowed" }
    $expiry = ConvertFrom-XbMxStoredTime -Text $Request.expected_expiry_local
    if ([string]$Request.kind -ceq "expire") {
        if ($null -ne $Request.basis_expiry_local) { throw "removal_request_invalid" }
        $runDay = ConvertFrom-XbMxDay -Text $Request.run_day_local
        if (-not (Test-XbMxDue -Expiry $expiry -RunDay $runDay)) { throw "removal_not_due" }
    }
    else {
        if ($null -ne $Request.run_day_local) { throw "removal_request_invalid" }
        if ($Request.basis_expiry_local -isnot [string] -or -not [string]::Equals($Request.basis_expiry_local, $Request.expected_expiry_local, [StringComparison]::Ordinal)) {
            throw "removal_request_invalid"
        }
    }
}

function Assert-XbMxRestoreRequest {
    # Undo only: GUEST -> MEMBER, bound to one removal of this tool.
    param([Parameter(Mandatory)]$Request)
    Assert-XbTmMemberNo -MemberNo $Request.member_no
    foreach ($name in @("kind", "from_type", "to_type", "expected_last_modified_local", "expected_expiry_local", "removal_id", "salt")) {
        if ($Request.$name -isnot [string]) { throw "removal_request_invalid" }
    }
    if (-not [string]::Equals($Request.kind, "restore", [StringComparison]::Ordinal)) { throw "removal_request_invalid" }
    if ($Request.salt -cnotmatch $script:XbMxSaltPattern -or $Request.removal_id -cnotmatch $script:XbMxRemovalIdPattern) { throw "removal_request_invalid" }
    if (-not [string]::Equals($Request.from_type, $script:XbMxToType, [StringComparison]::Ordinal)) { throw "removal_type_not_allowed" }
    if (-not [string]::Equals($Request.to_type, $script:XbMxFromType, [StringComparison]::Ordinal)) { throw "removal_type_not_allowed" }
    [void](ConvertFrom-XbMxStoredTime -Text $Request.expected_last_modified_local)
    [void](ConvertFrom-XbMxStoredTime -Text $Request.expected_expiry_local)
}

function Invoke-XbMxScan {
    # Read-only: one page (at most 500) of MEMBERs whose ExpiryDate is before
    # the run day, NULL or a placeholder, after a MemberNo cursor ("" first).
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)]$Request)
    $after = $Request.after_member_no
    if ($after -isnot [string] -or $after.Length -gt 40 -or $after -cmatch '[\x00-\x1F\x7F]') { throw "removal_request_invalid" }
    $runDay = ConvertFrom-XbMxDay -Text $Request.run_day_local
    $dayText = $runDay.ToString("yyyyMMdd", [System.Globalization.CultureInfo]::InvariantCulture)
    $floorText = $script:XbMxExpiryFloor.ToString("yyyyMMdd", [System.Globalization.CultureInfo]::InvariantCulture)
    try { $json = [string](Get-XbMxScalar -DBSetting $DBSetting -Sql $script:XbMxSqlText.expiring_members_page -Params @($script:XbMxFromType, $after, $dayText, $floorText)) }
    catch {
        $code = [string]$_.Exception.Message
        if ($code -clike "vendor_query_*") { throw $code }
        throw "removal_scan_failed"
    }
    return [ordered]@{ members = (ConvertFrom-XbTmJsonRows -Json $json); server_now_local = (Get-XbMxServerNow -DBSetting $DBSetting) }
}

function Invoke-XbMxMemberRead {
    # Read-only: one member's type, active flag, ExpiryDate, LastModified, the
    # salted digest of its other columns, and the server's local time.
    param([Parameter(Mandatory)]$Session, [Parameter(Mandatory)]$Request)
    Assert-XbTmMemberNo -MemberNo $Request.member_no
    if ($Request.salt -isnot [string] -or $Request.salt -cnotmatch $script:XbMxSaltPattern) { throw "removal_request_invalid" }
    try { $entity = Get-XbAutoCountMember -Session $Session -MemberNo ([string]$Request.member_no) }
    catch { throw "removal_member_read_failed" }
    $now = Get-XbMxServerNow -DBSetting $Session.DBSetting
    if ($null -eq $entity) { return [ordered]@{ found = $false; server_now_local = $now } }
    $record = Get-XbTmRecord -Entity $entity
    return [ordered]@{
        found = $true
        member_type = Get-XbTmFieldValue -Record $record -Field "MemberType"
        is_active = Get-XbTmFieldValue -Record $record -Field "IsActive"
        expiry_local = Get-XbTmFieldValue -Record $record -Field "ExpiryDate"
        last_modified_local = Get-XbTmFieldValue -Record $record -Field "LastModified"
        fields_digest = Get-XbTmFieldsDigest -Record $record -Salt ([string]$Request.salt)
        server_now_local = $now
    }
}

function New-XbMxClaim {
    # Exclusive claim immediately before the sole SaveMember of a process.
    param([Parameter(Mandatory)][string]$ClaimPath, [Parameter(Mandatory)][string]$FenceId, [Parameter(Mandatory)][string]$BeforeDigest)
    try {
        $stream = [System.IO.File]::Open($ClaimPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    }
    catch {
        if (Test-Path -LiteralPath $ClaimPath) { throw "fence_already_claimed" }
        throw "claim_create_failed"
    }
    try {
        $claim = [System.Text.Encoding]::ASCII.GetBytes((@{ fence_id = $FenceId; pid = $PID; claimed_at = [DateTimeOffset]::UtcNow.ToString("o"); before_digest = $BeforeDigest } | ConvertTo-Json -Compress))
        $stream.Write($claim, 0, $claim.Length)
        $stream.Flush($true)
    }
    finally { $stream.Dispose() }
}

function Get-XbMxReadback {
    # After SaveMember: the read-back of every column (names only, never values).
    param([Parameter(Mandatory)]$Command, [Parameter(Mandatory)][string]$MemberNo, [Parameter(Mandatory)][string]$TargetType,
          [Parameter(Mandatory)][string]$ExpectedExpiry, [Parameter(Mandatory)][System.Collections.IDictionary]$Before,
          [Parameter(Mandatory)][string]$BeforeDigest, [Parameter(Mandatory)][string]$Salt, [Parameter(Mandatory)]$Session,
          [Parameter(Mandatory)][string]$ClaimPath, [switch]$RealSession)
    $readback = $Command.GetMember($MemberNo)
    $result = [ordered]@{
        status = "saved"
        save_invocation_count = 1
        readback_found = ($null -ne $readback)
        readback_type_match = $false
        readback_expiry_match = $false
        fields_unchanged = $false
        changed_fields = @()
        post_last_modified_local = $null
        outbound_queue_changed = $false
    }
    if ($null -ne $readback) {
        $after = Get-XbTmRecord -Entity $readback
        $result.readback_type_match = [string]::Equals([string](Get-XbTmFieldValue -Record $after -Field "MemberType"), $TargetType, [StringComparison]::Ordinal)
        $result.readback_expiry_match = [string]::Equals([string](Get-XbTmFieldValue -Record $after -Field "ExpiryDate"), $ExpectedExpiry, [StringComparison]::Ordinal)
        $result.fields_unchanged = [string]::Equals((Get-XbTmFieldsDigest -Record $after -Salt $Salt), $BeforeDigest, [StringComparison]::Ordinal)
        $result.changed_fields = Get-XbTmChangedFields -Before $Before -After $after
        $post = Get-XbTmFieldValue -Record $after -Field "LastModified"
        $result.post_last_modified_local = $(if ($null -ne $post -and [string]$post -cmatch $script:XbMxStoredPattern) { [string]$post } else { $null })
    }
    if ($RealSession) { $result.outbound_queue_changed = [bool](Test-XbDlOutboundChanged -DBSetting $Session.DBSetting) }
    try {
        $outcomeBytes = [System.Text.Encoding]::ASCII.GetBytes(($result | ConvertTo-Json -Compress -Depth 3))
        $outcomeStream = [System.IO.File]::Open(($ClaimPath -replace '\.claim$', '.outcome'), [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try { $outcomeStream.Write($outcomeBytes, 0, $outcomeBytes.Length); $outcomeStream.Flush($true) } finally { $outcomeStream.Dispose() }
    }
    catch { $result.outcome_file = "write_failed" }
    return $result
}

function Invoke-XbMxSave {
    # The removal: one SaveMember (MemberType MEMBER -> GUEST only), after the
    # pre-checks and an exclusive claim file; then a read-back of every column.
    param(
        [Parameter(Mandatory)]$Session,
        [Parameter(Mandatory)]$Request,
        [Parameter(Mandatory)][string]$ClaimPath,
        [Parameter(Mandatory)][string]$FenceId,
        [switch]$RealSession
    )
    $memberNo = [string]$Request.member_no
    $command = Get-XbAutoCountMemberCommand -Session $Session
    foreach ($type in @($script:XbMxFromType, $script:XbMxToType)) {
        if (-not (Test-XbTmMemberTypeExists -DBSetting $Session.DBSetting -MemberType $type)) { throw "member_type_unknown" }
    }
    $entity = $command.GetMember($memberNo)
    if ($null -eq $entity) { return [ordered]@{ status = "skipped"; code = "member_not_found" } }
    $row = Get-XbAutoCountMemberRow -Entity $entity
    if ($null -eq $row) { throw "autocount_member_row_unavailable" }
    $before = Get-XbTmRecord -Entity $entity
    $typeNow = [string](Get-XbTmFieldValue -Record $before -Field "MemberType")
    if ([string]::Equals($typeNow, $script:XbMxToType, [StringComparison]::Ordinal)) { return [ordered]@{ status = "skipped"; code = "type_changed_since_read" } }
    if (-not [string]::Equals($typeNow, $script:XbMxFromType, [StringComparison]::Ordinal)) { return [ordered]@{ status = "skipped"; code = "type_not_in_scope_now" } }
    # Staff win: LastModified and ExpiryDate must still be what the parent read.
    if (-not [string]::Equals([string](Get-XbTmFieldValue -Record $before -Field "LastModified"), [string]$Request.expected_last_modified_local, [StringComparison]::Ordinal)) {
        return [ordered]@{ status = "skipped"; code = "changed_since_read" }
    }
    $expiryNow = Get-XbTmFieldValue -Record $before -Field "ExpiryDate"
    if ($null -eq $expiryNow -or -not [string]::Equals([string]$expiryNow, [string]$Request.expected_expiry_local, [StringComparison]::Ordinal)) {
        return [ordered]@{ status = "skipped"; code = "changed_since_read" }
    }
    # Expire: the date read now must still be due on the run day.
    if ([string]$Request.kind -ceq "expire" -and -not (Test-XbMxDue -Expiry (ConvertFrom-XbMxStoredTime -Text ([string]$expiryNow)) -RunDay (ConvertFrom-XbMxDay -Text $Request.run_day_local))) {
        return [ordered]@{ status = "skipped"; code = "not_due_now" }
    }
    $salt = [string]$Request.salt
    $beforeDigest = Get-XbTmFieldsDigest -Record $before -Salt $salt
    New-XbMxClaim -ClaimPath $ClaimPath -FenceId $FenceId -BeforeDigest $beforeDigest
    # The only column assigned: MemberType. ExpiryDate is never written.
    Set-XbAutoCountMemberRowValue -Row $row -Field "MemberType" -Value $script:XbMxToType
    $command.SaveMember($entity)
    return (Get-XbMxReadback -Command $command -MemberNo $memberNo -TargetType $script:XbMxToType -ExpectedExpiry ([string]$Request.expected_expiry_local) `
        -Before $before -BeforeDigest $beforeDigest -Salt $salt -Session $Session -ClaimPath $ClaimPath -RealSession:$RealSession)
}

function Invoke-XbMxRestore {
    # Undo: one SaveMember (MemberType GUEST -> MEMBER only) for a member this
    # tool removed, only if untouched since (type, LastModified, ExpiryDate).
    param(
        [Parameter(Mandatory)]$Session,
        [Parameter(Mandatory)]$Request,
        [Parameter(Mandatory)][string]$ClaimPath,
        [Parameter(Mandatory)][string]$FenceId,
        [switch]$RealSession
    )
    $memberNo = [string]$Request.member_no
    $command = Get-XbAutoCountMemberCommand -Session $Session
    foreach ($type in @($script:XbMxFromType, $script:XbMxToType)) {
        if (-not (Test-XbTmMemberTypeExists -DBSetting $Session.DBSetting -MemberType $type)) { throw "member_type_unknown" }
    }
    $entity = $command.GetMember($memberNo)
    if ($null -eq $entity) { return [ordered]@{ status = "skipped"; code = "member_not_found" } }
    $row = Get-XbAutoCountMemberRow -Entity $entity
    if ($null -eq $row) { throw "autocount_member_row_unavailable" }
    $before = Get-XbTmRecord -Entity $entity
    $expiryNow = Get-XbTmFieldValue -Record $before -Field "ExpiryDate"
    if (-not [string]::Equals([string](Get-XbTmFieldValue -Record $before -Field "MemberType"), $script:XbMxToType, [StringComparison]::Ordinal) -or
        -not [string]::Equals([string](Get-XbTmFieldValue -Record $before -Field "LastModified"), [string]$Request.expected_last_modified_local, [StringComparison]::Ordinal) -or
        $null -eq $expiryNow -or -not [string]::Equals([string]$expiryNow, [string]$Request.expected_expiry_local, [StringComparison]::Ordinal)) {
        return [ordered]@{ status = "skipped"; code = "changed_since_removal" }
    }
    $salt = [string]$Request.salt
    $beforeDigest = Get-XbTmFieldsDigest -Record $before -Salt $salt
    New-XbMxClaim -ClaimPath $ClaimPath -FenceId $FenceId -BeforeDigest $beforeDigest
    # The only column assigned: MemberType. ExpiryDate is never written.
    Set-XbAutoCountMemberRowValue -Row $row -Field "MemberType" -Value $script:XbMxFromType
    $command.SaveMember($entity)
    return (Get-XbMxReadback -Command $command -MemberNo $memberNo -TargetType $script:XbMxFromType -ExpectedExpiry ([string]$Request.expected_expiry_local) `
        -Before $before -BeforeDigest $beforeDigest -Salt $salt -Session $Session -ClaimPath $ClaimPath -RealSession:$RealSession)
}
