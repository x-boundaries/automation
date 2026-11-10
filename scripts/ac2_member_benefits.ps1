# #268 member core: the benefits tool's AutoCount child, profile benefit_p1.
#
# Dot-sourced by ac2_member_benefits_bridge.ps1 after the gateway adapter
# (unchanged), the book guard and the #268 type move library (unchanged; its
# record, digest, request and type helpers are reused as they are).
#
# Upgrade only (owner scope 10 Nov 2026, Controller brief):
#   - The only target type is MEMBER. kind "upgrade" moves GUEST -> MEMBER and
#     sets ExpiryDate; kind "extend" keeps a MEMBER a MEMBER and moves its
#     ExpiryDate later. No other move exists: never to GUEST, never Default,
#     never from any type but GUEST or MEMBER.
#   - ExpiryDate only ever moves later: the request must carry a new date
#     after the expected one, and the member's current date read just before
#     SaveMember must still be the expected one and earlier than the new one.
#   - Staff win: the member is re-read just before SaveMember and saved only
#     if its type, LastModified and ExpiryDate still equal what the parent
#     read. Otherwise it is skipped with a code, before the claim file.
#   - Only MemberType (upgrade) and ExpiryDate are assigned. The read-back
#     compares every other column by salted digest (LastModified and
#     LastModifiedUserID excepted, as in the type move).
# The scan read uses its own pinned SQL text (Get-XbMbScalar), behind the
# same ASCII and deny checks as the book guard's allowlist.

Set-StrictMode -Version Latest

$script:XbMbProfile = "benefit_p1"
$script:XbMbTargetType = "MEMBER"
$script:XbMbFromTypes = [ordered]@{ upgrade = "GUEST"; extend = "MEMBER" }
$script:XbMbScanTypes = @("MEMBER", "GUEST")
# Columns the save itself may change; every other column must read back equal.
$script:XbMbUnchangedExclusions = @("MemberType", "ExpiryDate", "LastModified", "LastModifiedUserID")
$script:XbMbNewExpiryPattern = '\A[0-9]{4}-[0-9]{2}-[0-9]{2}T00:00:00\z'
$script:XbMbLastModifiedPattern = '\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}\z'
$script:XbMbSaltPattern = '\A[0-9a-f]{32}\z'
# One line of printable ASCII; "?" only as a placeholder. Pinned by
# tests/test_member_benefits_ps.py.
$script:XbMbSqlText = [ordered]@{
    members_name_page = ("SELECT (SELECT TOP 500 MemberNo AS member_no, Name AS name " +
        "FROM Member WHERE MemberType = ? AND MemberNo > ? ORDER BY MemberNo FOR JSON PATH, INCLUDE_NULL_VALUES)")
}

function Get-XbMbScalar {
    # Read-only vendor query through the authenticated DBSetting: only the
    # text above, after the book guard's ASCII and deny checks.
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)][string]$Sql, [object[]]$Params = @())
    if ($Sql -cnotmatch '\A[\x20-\x7E]+\z') { throw "vendor_query_not_ascii" }
    if ($Sql -match $script:XbDlDeniedSqlPattern) { throw "vendor_query_not_read_only" }
    if ([regex]::Matches($Sql, '\?').Count -ne @($Params).Count) { throw "vendor_query_not_allowlisted" }
    $known = $false
    foreach ($text in $script:XbMbSqlText.Values) {
        if ([string]::Equals($Sql, [string]$text, [StringComparison]::Ordinal)) { $known = $true }
    }
    if (-not $known) { throw "vendor_query_not_allowlisted" }
    return $DBSetting.ExecuteScalar($Sql, [object[]]$Params)
}

function Get-XbMbFieldsDigest {
    # Salted digest of every column except the save's own (MemberType,
    # ExpiryDate, LastModified, LastModifiedUserID); never printed.
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Record, [Parameter(Mandatory)][string]$Salt)
    $kept = [ordered]@{}
    foreach ($name in $Record.Keys) {
        if (-not (Test-XbDlOrdinalIn -Value $name -Set $script:XbMbUnchangedExclusions)) { $kept[$name] = $Record[$name] }
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Salt + ":" + ($kept | ConvertTo-Json -Compress -Depth 2))
        return "sha256:" + (-join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }))
    }
    finally { $sha.Dispose() }
}

function Get-XbMbChangedFields {
    # Column names (never values) that differ, outside the exclusions.
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Before, [Parameter(Mandatory)][System.Collections.IDictionary]$After)
    $names = @($Before.Keys) + @($After.Keys | Where-Object { -not $Before.Contains($_) })
    $changed = @()
    foreach ($name in $names) {
        if (Test-XbDlOrdinalIn -Value $name -Set $script:XbMbUnchangedExclusions) { continue }
        $a = if ($Before.Contains($name)) { $Before[$name] } else { "<absent>" }
        $b = if ($After.Contains($name)) { $After[$name] } else { "<absent>" }
        if (($null -eq $a) -ne ($null -eq $b) -or ($null -ne $a -and -not [string]::Equals([string]$a, [string]$b, [StringComparison]::Ordinal))) {
            $changed += ($(if ($name -cmatch '\A[A-Za-z0-9_]{1,64}\z') { $name } else { "column_name_unprintable" }))
        }
    }
    return ,$changed
}

function ConvertFrom-XbMbNewExpiry {
    param([AllowNull()][object]$Text)
    [datetime]$parsed = [datetime]::MinValue
    if ($Text -isnot [string] -or $Text -cnotmatch $script:XbMbNewExpiryPattern -or
        -not [datetime]::TryParseExact($Text, "yyyy-MM-ddTHH:mm:ss", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::None, [ref]$parsed)) {
        throw "benefit_request_invalid"
    }
    return $parsed
}

function ConvertFrom-XbMbStoredTime {
    # The exact text ConvertTo-XbTmValue gives a datetime (yyyy-MM-ddTHH:mm:ss.fff).
    param([AllowNull()][object]$Text)
    [datetime]$parsed = [datetime]::MinValue
    if ($Text -isnot [string] -or $Text -cnotmatch $script:XbMbLastModifiedPattern -or
        -not [datetime]::TryParseExact($Text, "yyyy-MM-ddTHH:mm:ss.fff", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::None, [ref]$parsed)) {
        throw "benefit_request_invalid"
    }
    return $parsed
}

function Assert-XbMbSaveRequest {
    # Every value the save uses, checked before any session or claim. The
    # upgrade-only rules are enforced here, at the write boundary.
    param([Parameter(Mandatory)]$Request)
    Assert-XbTmMemberNo -MemberNo $Request.member_no
    foreach ($name in @("kind", "from_type", "to_type", "expected_last_modified_local", "new_expiry_local", "salt")) {
        if ($Request.$name -isnot [string]) { throw "benefit_request_invalid" }
    }
    if ($Request.salt -cnotmatch $script:XbMbSaltPattern -or $Request.expected_last_modified_local -cnotmatch $script:XbMbLastModifiedPattern) { throw "benefit_request_invalid" }
    if (-not $script:XbMbFromTypes.Contains([string]$Request.kind) -or -not (Test-XbDlOrdinalIn -Value $Request.kind -Set @($script:XbMbFromTypes.Keys))) { throw "benefit_request_invalid" }
    if (-not [string]::Equals($Request.to_type, $script:XbMbTargetType, [StringComparison]::Ordinal)) { throw "benefit_type_not_allowed" }
    if (-not [string]::Equals($Request.from_type, [string]$script:XbMbFromTypes[[string]$Request.kind], [StringComparison]::Ordinal)) { throw "benefit_type_not_allowed" }
    $new = ConvertFrom-XbMbNewExpiry -Text $Request.new_expiry_local
    if ($null -ne $Request.expected_expiry_local) {
        $old = ConvertFrom-XbMbStoredTime -Text $Request.expected_expiry_local
        if ($new -le $old) { throw "benefit_expiry_not_later" }
    }
}

function Invoke-XbMbScan {
    # Read-only: one page (at most 500) of MEMBER or GUEST members with their
    # names, after a MemberNo cursor ("" for the first page). Index only.
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)]$Request)
    $type = $Request.member_type
    if ($type -isnot [string] -or -not (Test-XbDlOrdinalIn -Value $type -Set $script:XbMbScanTypes)) { throw "benefit_type_not_allowed" }
    $after = $Request.after_member_no
    if ($after -isnot [string] -or $after.Length -gt 40 -or $after -cmatch '[\x00-\x1F\x7F]') { throw "benefit_request_invalid" }
    try { $json = [string](Get-XbMbScalar -DBSetting $DBSetting -Sql $script:XbMbSqlText.members_name_page -Params @($type, $after)) }
    catch {
        $code = [string]$_.Exception.Message
        if ($code -clike "vendor_query_*") { throw $code }
        throw "benefit_scan_failed"
    }
    return [ordered]@{ members = (ConvertFrom-XbTmJsonRows -Json $json) }
}

function Invoke-XbMbMemberRead {
    # Read-only: one member's type, name, active flag, ExpiryDate,
    # LastModified and the salted digest of its other columns.
    param([Parameter(Mandatory)]$Session, [Parameter(Mandatory)]$Request)
    Assert-XbTmMemberNo -MemberNo $Request.member_no
    if ($Request.salt -isnot [string] -or $Request.salt -cnotmatch $script:XbMbSaltPattern) { throw "benefit_request_invalid" }
    try { $entity = Get-XbAutoCountMember -Session $Session -MemberNo ([string]$Request.member_no) }
    catch { throw "benefit_member_read_failed" }
    if ($null -eq $entity) { return [ordered]@{ found = $false } }
    $record = Get-XbTmRecord -Entity $entity
    return [ordered]@{
        found = $true
        member_no = Get-XbTmFieldValue -Record $record -Field "MemberNo"
        member_type = Get-XbTmFieldValue -Record $record -Field "MemberType"
        name = Get-XbTmFieldValue -Record $record -Field "Name"
        is_active = Get-XbTmFieldValue -Record $record -Field "IsActive"
        expiry_local = Get-XbTmFieldValue -Record $record -Field "ExpiryDate"
        last_modified_local = Get-XbTmFieldValue -Record $record -Field "LastModified"
        fields_digest = Get-XbMbFieldsDigest -Record $record -Salt ([string]$Request.salt)
    }
}

function Invoke-XbMbSave {
    # The tool's only member write: one SaveMember, after the pre-checks and
    # an exclusive claim file; then a read-back of every column.
    param(
        [Parameter(Mandatory)]$Session,
        [Parameter(Mandatory)]$Request,
        [Parameter(Mandatory)][string]$ClaimPath,
        [Parameter(Mandatory)][string]$FenceId,
        [switch]$RealSession
    )
    $memberNo = [string]$Request.member_no
    $from = [string]$Request.from_type
    $isUpgrade = ([string]$Request.kind -ceq "upgrade")
    $newExpiry = ConvertFrom-XbMbNewExpiry -Text $Request.new_expiry_local
    $newExpiryText = $newExpiry.ToString("yyyy-MM-ddTHH:mm:ss.fff", [System.Globalization.CultureInfo]::InvariantCulture)
    $command = Get-XbAutoCountMemberCommand -Session $Session
    foreach ($type in @($from, $script:XbMbTargetType)) {
        if (-not (Test-XbTmMemberTypeExists -DBSetting $Session.DBSetting -MemberType $type)) { throw "member_type_unknown" }
    }
    $entity = $command.GetMember($memberNo)
    if ($null -eq $entity) { return [ordered]@{ status = "skipped"; code = "member_not_found" } }
    $row = Get-XbAutoCountMemberRow -Entity $entity
    if ($null -eq $row) { throw "autocount_member_row_unavailable" }
    $before = Get-XbTmRecord -Entity $entity
    # Upgrade only: the member must be GUEST or MEMBER now, and exactly the
    # type the parent read. Default, held and any other type: never written.
    $typeNow = [string](Get-XbTmFieldValue -Record $before -Field "MemberType")
    if (-not (Test-XbDlOrdinalIn -Value $typeNow -Set $script:XbMbScanTypes)) { return [ordered]@{ status = "skipped"; code = "type_not_in_scope_now" } }
    if (-not [string]::Equals($typeNow, $from, [StringComparison]::Ordinal)) { return [ordered]@{ status = "skipped"; code = "type_changed_since_read" } }
    # Staff win: LastModified and ExpiryDate must still be what the parent read.
    if (-not [string]::Equals([string](Get-XbTmFieldValue -Record $before -Field "LastModified"), [string]$Request.expected_last_modified_local, [StringComparison]::Ordinal)) {
        return [ordered]@{ status = "skipped"; code = "changed_since_read" }
    }
    $expiryNow = Get-XbTmFieldValue -Record $before -Field "ExpiryDate"
    if (($null -eq $expiryNow) -ne ($null -eq $Request.expected_expiry_local) -or
        ($null -ne $expiryNow -and -not [string]::Equals([string]$expiryNow, [string]$Request.expected_expiry_local, [StringComparison]::Ordinal))) {
        return [ordered]@{ status = "skipped"; code = "changed_since_read" }
    }
    # Never earlier: the date read now must be before the new one.
    if ($null -ne $expiryNow -and (ConvertFrom-XbMbStoredTime -Text ([string]$expiryNow)) -ge $newExpiry) { return [ordered]@{ status = "skipped"; code = "expiry_not_later_now" } }
    $beforeDigest = Get-XbMbFieldsDigest -Record $before -Salt ([string]$Request.salt)

    # Exclusive claim immediately before the sole SaveMember.
    try {
        $stream = [System.IO.File]::Open($ClaimPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    }
    catch {
        if (Test-Path -LiteralPath $ClaimPath) { throw "fence_already_claimed" }
        throw "claim_create_failed"
    }
    try {
        $claim = [System.Text.Encoding]::ASCII.GetBytes((@{ fence_id = $FenceId; pid = $PID; claimed_at = [DateTimeOffset]::UtcNow.ToString("o"); before_digest = $beforeDigest } | ConvertTo-Json -Compress))
        $stream.Write($claim, 0, $claim.Length)
        $stream.Flush($true)
    }
    finally { $stream.Dispose() }

    # The only columns assigned: ExpiryDate, and MemberType on an upgrade.
    if ($isUpgrade) { Set-XbAutoCountMemberRowValue -Row $row -Field "MemberType" -Value $script:XbMbTargetType }
    Set-XbAutoCountMemberRowValue -Row $row -Field "ExpiryDate" -Value $newExpiry
    $saveCount = 1
    $command.SaveMember($entity)

    $readback = $command.GetMember($memberNo)
    $result = [ordered]@{
        status = "saved"
        save_invocation_count = $saveCount
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
        $result.readback_type_match = [string]::Equals([string](Get-XbTmFieldValue -Record $after -Field "MemberType"), $script:XbMbTargetType, [StringComparison]::Ordinal)
        $result.readback_expiry_match = [string]::Equals([string](Get-XbTmFieldValue -Record $after -Field "ExpiryDate"), $newExpiryText, [StringComparison]::Ordinal)
        $result.fields_unchanged = [string]::Equals((Get-XbMbFieldsDigest -Record $after -Salt ([string]$Request.salt)), $beforeDigest, [StringComparison]::Ordinal)
        $result.changed_fields = Get-XbMbChangedFields -Before $before -After $after
        $post = Get-XbTmFieldValue -Record $after -Field "LastModified"
        $result.post_last_modified_local = $(if ($null -ne $post -and [string]$post -cmatch $script:XbMbLastModifiedPattern) { [string]$post } else { $null })
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
