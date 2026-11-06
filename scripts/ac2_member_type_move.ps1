# #268: the one-off AutoCount member type move, Default -> MEMBER or GUEST.
#
# Dot-sourced by ac2_member_type_move_bridge.ps1 after the gateway adapter
# (unchanged) and the book guard. Design: G0 #268 option b (SDK GetMember ->
# set MemberType -> SaveMember), with the owner amendments of 6 Nov 2026:
#   - from_type is exactly Default; the target is MEMBER when the member is
#     active (IsActive T) AND ExpiryDate is later than the snapshot time, and
#     GUEST otherwise (inactive, ExpiryDate NULL, or at or before the snapshot
#     time). No other target exists. Default the type is never edited.
#   - Just before SaveMember the member is re-read and saved only if the type
#     is still the expected one, LastModified equals the expected value and
#     (move) the active flag and ExpiryDate still give the same target.
#     Otherwise it is skipped with a code, before the claim file.
#   - Only MemberType is assigned. The read-back compares every other column
#     (except the save's own LastModified/LastModifiedUserID) by digest.
#   - Rollback is the same path the other way (MEMBER/GUEST -> Default).
# The type move's reads use their own two pinned SQL texts (Get-XbTmScalar),
# behind the same ASCII and deny checks as the book guard's allowlist.

Set-StrictMode -Version Latest

$script:XbTmFromType = "Default"
$script:XbTmTargetTypes = @("MEMBER", "GUEST")
$script:XbTmScanTypes = @("Default", "MEMBER", "GUEST")
$script:XbTmProfile = "type_move_p1"
# Columns the save itself may change; every other column must read back equal.
$script:XbTmUnchangedExclusions = @("MemberType", "LastModified", "LastModifiedUserID")
$script:XbTmLocalTimePattern = '\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\z'
$script:XbTmLastModifiedPattern = '\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}\z'
$script:XbTmSaltPattern = '\A[0-9a-f]{32}\z'
# One line of printable ASCII each; "?" only as a placeholder. Pinned by
# tests/test_member_type_move_ps.py.
$script:XbTmSqlText = [ordered]@{
    members_by_type_page = ("SELECT (SELECT TOP 500 MemberNo AS member_no, IsActive AS is_active, " +
        "CONVERT(varchar(23), ExpiryDate, 126) AS expiry_local, CONVERT(varchar(23), LastModified, 126) AS last_modified_local " +
        "FROM Member WHERE MemberType = ? AND MemberNo > ? ORDER BY MemberNo FOR JSON PATH, INCLUDE_NULL_VALUES)")
    member_counts_by_type = ("SELECT (SELECT MemberType AS member_type, IsActive AS is_active, COUNT_BIG(*) AS members " +
        "FROM Member GROUP BY MemberType, IsActive ORDER BY MemberType, IsActive FOR JSON PATH, INCLUDE_NULL_VALUES)")
}

function Get-XbTmScalar {
    # Read-only vendor query through the authenticated DBSetting: only the two
    # texts above, after the book guard's ASCII and deny checks.
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)][string]$Sql, [object[]]$Params = @())
    if ($Sql -cnotmatch '\A[\x20-\x7E]+\z') { throw "vendor_query_not_ascii" }
    if ($Sql -match $script:XbDlDeniedSqlPattern) { throw "vendor_query_not_read_only" }
    if ([regex]::Matches($Sql, '\?').Count -ne @($Params).Count) { throw "vendor_query_not_allowlisted" }
    $known = $false
    foreach ($text in $script:XbTmSqlText.Values) {
        if ([string]::Equals($Sql, [string]$text, [StringComparison]::Ordinal)) { $known = $true }
    }
    if (-not $known) { throw "vendor_query_not_allowlisted" }
    return $DBSetting.ExecuteScalar($Sql, [object[]]$Params)
}

function ConvertTo-XbTmRequest {
    # Exactly the named keys, each present ($null when absent), so StrictMode
    # never meets a missing property; any other key is refused.
    param([AllowNull()]$Raw, [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Names)
    if ($Raw -isnot [System.Management.Automation.PSCustomObject]) { throw "type_move_request_invalid" }
    foreach ($property in $Raw.PSObject.Properties) {
        if (-not (Test-XbDlOrdinalIn -Value $property.Name -Set $Names)) { throw "type_move_request_invalid" }
    }
    $out = [ordered]@{}
    foreach ($name in $Names) {
        $property = $Raw.PSObject.Properties[$name]
        $out[$name] = if ($null -ne $property) { $property.Value } else { $null }
    }
    return [pscustomobject]$out
}

function ConvertFrom-XbTmJsonRows {
    param([AllowNull()][string]$Json)
    $rows = @()
    if ([string]::IsNullOrWhiteSpace($Json)) { return ,$rows }
    foreach ($row in (ConvertFrom-Json -InputObject $Json)) { $rows += $row }
    return ,$rows
}

function Test-XbTmMemberTypeExists {
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)][string]$MemberType)
    try { Assert-XbDlMemberTypeExists -DBSetting $DBSetting -MemberType $MemberType; return $true }
    catch {
        if ([string]$_.Exception.Message -ceq "member_type_unknown") { return $false }
        throw
    }
}

function ConvertTo-XbTmValue {
    # Exact, culture-free text of one column value; NULL stays $null.
    param([AllowNull()][object]$Value)
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return $null }
    if ($Value -is [datetime]) { return $Value.ToString("yyyy-MM-ddTHH:mm:ss.fff", [System.Globalization.CultureInfo]::InvariantCulture) }
    if ($Value -is [bool]) { return $(if ($Value) { "T" } else { "F" }) }
    if ($Value -is [byte[]]) { return [Convert]::ToBase64String($Value) }
    if ($Value -is [System.IFormattable]) { return $Value.ToString($null, [System.Globalization.CultureInfo]::InvariantCulture) }
    return [string]$Value
}

function Get-XbTmRecord {
    # Every column of the member's master row, by name (ordinal order).
    param([Parameter(Mandatory)]$Entity)
    $row = Get-XbAutoCountMemberRow -Entity $Entity
    if ($null -eq $row) { throw "autocount_member_row_unavailable" }
    $names = [System.Collections.Generic.List[string]]::new()
    foreach ($column in $row.Table.Columns) { $names.Add([string]$column.ColumnName) }
    $names.Sort([StringComparer]::Ordinal)
    $record = [ordered]@{}
    foreach ($name in $names) { $record[$name] = ConvertTo-XbTmValue -Value $row[$name] }
    return $record
}

function Get-XbTmFieldValue {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Record, [Parameter(Mandatory)][string]$Field)
    if (-not $Record.Contains($Field)) { throw "autocount_member_field_missing" }
    return $Record[$Field]
}

function Get-XbTmFieldsDigest {
    # Salted digest of every column except the save's own; never leaves the
    # private state (claim file, parent ledger) and is never printed.
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Record, [Parameter(Mandatory)][string]$Salt)
    $kept = [ordered]@{}
    foreach ($name in $Record.Keys) {
        if (-not (Test-XbDlOrdinalIn -Value $name -Set $script:XbTmUnchangedExclusions)) { $kept[$name] = $Record[$name] }
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Salt + ":" + ($kept | ConvertTo-Json -Compress -Depth 2))
        return "sha256:" + (-join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }))
    }
    finally { $sha.Dispose() }
}

function Get-XbTmChangedFields {
    # Column names (never values) that differ, outside the exclusions.
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Before, [Parameter(Mandatory)][System.Collections.IDictionary]$After)
    $names = @($Before.Keys) + @($After.Keys | Where-Object { -not $Before.Contains($_) })
    $changed = @()
    foreach ($name in $names) {
        if (Test-XbDlOrdinalIn -Value $name -Set $script:XbTmUnchangedExclusions) { continue }
        $a = if ($Before.Contains($name)) { $Before[$name] } else { "<absent>" }
        $b = if ($After.Contains($name)) { $After[$name] } else { "<absent>" }
        if (($null -eq $a) -ne ($null -eq $b) -or ($null -ne $a -and -not [string]::Equals([string]$a, [string]$b, [StringComparison]::Ordinal))) {
            $changed += ($(if ($name -cmatch '\A[A-Za-z0-9_]{1,64}\z') { $name } else { "column_name_unprintable" }))
        }
    }
    return ,$changed
}

function Get-XbTmTarget {
    # The pinned rule (owner, 6 Nov 2026). "" when the active flag is neither
    # T nor F: no target, so the caller skips the member.
    param([AllowNull()][object]$IsActive, [AllowNull()][object]$ExpiryDate, [Parameter(Mandatory)][datetime]$SnapshotAt)
    $flag = if ($null -eq $IsActive -or $IsActive -is [System.DBNull]) { "" } else { Convert-XbAutoCountNormalizedValue -Value $IsActive -Field "IsActive" }
    if ($flag -ceq "T" -and $ExpiryDate -is [datetime] -and $ExpiryDate -gt $SnapshotAt) { return "MEMBER" }
    if ($flag -ceq "T" -or $flag -ceq "F") { return "GUEST" }
    return ""
}

function ConvertFrom-XbTmLocalTime {
    param([AllowNull()][string]$Text)
    [datetime]$parsed = [datetime]::MinValue
    if ([string]::IsNullOrEmpty($Text) -or $Text -cnotmatch $script:XbTmLocalTimePattern -or
        -not [datetime]::TryParseExact($Text, "yyyy-MM-ddTHH:mm:ss", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::None, [ref]$parsed)) {
        throw "type_move_request_invalid"
    }
    return $parsed
}

function Invoke-XbTmScan {
    # Read-only: one page (at most 500) of members on one of the three types,
    # after a MemberNo cursor ("" for the first page).
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)]$Request)
    $type = $Request.member_type
    if ($type -isnot [string] -or -not (Test-XbDlOrdinalIn -Value $type -Set $script:XbTmScanTypes)) { throw "type_move_type_not_allowed" }
    $after = $Request.after_member_no
    if ($after -isnot [string] -or $after.Length -gt 40 -or $after -cmatch '[\x00-\x1F\x7F]') { throw "type_move_request_invalid" }
    try { $json = [string](Get-XbTmScalar -DBSetting $DBSetting -Sql $script:XbTmSqlText.members_by_type_page -Params @($type, $after)) }
    catch {
        $code = [string]$_.Exception.Message
        if ($code -clike "vendor_query_*") { throw $code }
        throw "type_move_scan_failed"
    }
    return [ordered]@{ members = (ConvertFrom-XbTmJsonRows -Json $json) }
}

function Invoke-XbTmCounts {
    # Read-only: members per type and active flag, whether each of the three
    # types exists, and the HQ POS sync state (server time, outlets).
    param([Parameter(Mandatory)]$DBSetting)
    try { $json = [string](Get-XbTmScalar -DBSetting $DBSetting -Sql $script:XbTmSqlText.member_counts_by_type) }
    catch {
        $code = [string]$_.Exception.Message
        if ($code -clike "vendor_query_*") { throw $code }
        throw "type_move_counts_failed"
    }
    $types = [ordered]@{}
    foreach ($type in $script:XbTmScanTypes) { $types[$type] = Test-XbTmMemberTypeExists -DBSetting $DBSetting -MemberType $type }
    try { $pos = Get-XbDlPosSyncState -DBSetting $DBSetting } catch { throw "pos_sync_state_unreadable" }
    return [ordered]@{
        counts = (ConvertFrom-XbTmJsonRows -Json $json)
        types_exist = $types
        server_now_local = [string]$pos.server_now_local
        member_change_counter = [long]$pos.member_change_counter
        outlets = @($pos.outlets)
    }
}

function Invoke-XbTmMemberRead {
    # Read-only: one member's type, LastModified, active flag, expiry and the
    # salted digest of its other columns (UNCERTAIN resolution).
    param([Parameter(Mandatory)]$Session, [Parameter(Mandatory)]$Request)
    Assert-XbTmMemberNo -MemberNo $Request.member_no
    if ($Request.salt -isnot [string] -or $Request.salt -cnotmatch $script:XbTmSaltPattern) { throw "type_move_request_invalid" }
    try { $entity = Get-XbAutoCountMember -Session $Session -MemberNo ([string]$Request.member_no) }
    catch { throw "type_move_member_read_failed" }
    if ($null -eq $entity) { return [ordered]@{ found = $false } }
    $record = Get-XbTmRecord -Entity $entity
    return [ordered]@{
        found = $true
        member_type = Get-XbTmFieldValue -Record $record -Field "MemberType"
        last_modified_local = Get-XbTmFieldValue -Record $record -Field "LastModified"
        is_active = Get-XbTmFieldValue -Record $record -Field "IsActive"
        expiry_local = Get-XbTmFieldValue -Record $record -Field "ExpiryDate"
        fields_digest = Get-XbTmFieldsDigest -Record $record -Salt ([string]$Request.salt)
    }
}

function Assert-XbTmMemberNo {
    param([AllowNull()][object]$MemberNo)
    if ($MemberNo -isnot [string] -or $MemberNo -cnotmatch '\A[A-Za-z0-9][A-Za-z0-9._/-]{0,19}\z') { throw "member_no_invalid" }
}

function Assert-XbTmSaveRequest {
    # Every value the save uses, checked before any session or claim.
    param([Parameter(Mandatory)]$Request)
    Assert-XbTmMemberNo -MemberNo $Request.member_no
    foreach ($name in @("kind", "from_type", "to_type", "expected_last_modified_local", "salt")) {
        if ($Request.$name -isnot [string]) { throw "type_move_request_invalid" }
    }
    if ($Request.salt -cnotmatch $script:XbTmSaltPattern -or $Request.expected_last_modified_local -cnotmatch $script:XbTmLastModifiedPattern) { throw "type_move_request_invalid" }
    if ($Request.kind -ceq "move") {
        if (-not [string]::Equals($Request.from_type, $script:XbTmFromType, [StringComparison]::Ordinal)) { throw "type_move_type_not_allowed" }
        if (-not (Test-XbDlOrdinalIn -Value $Request.to_type -Set $script:XbTmTargetTypes)) { throw "type_move_type_not_allowed" }
        [void](ConvertFrom-XbTmLocalTime -Text $Request.snapshot_at_local)
    }
    elseif ($Request.kind -ceq "rollback") {
        if (-not (Test-XbDlOrdinalIn -Value $Request.from_type -Set $script:XbTmTargetTypes)) { throw "type_move_type_not_allowed" }
        if (-not [string]::Equals($Request.to_type, $script:XbTmFromType, [StringComparison]::Ordinal)) { throw "type_move_type_not_allowed" }
    }
    else { throw "type_move_request_invalid" }
}

function Invoke-XbTmSave {
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
    $to = [string]$Request.to_type
    $isMove = ([string]$Request.kind -ceq "move")
    $command = Get-XbAutoCountMemberCommand -Session $Session
    # Both types must exist before any write (amendment 1).
    foreach ($type in @($from, $to)) {
        if (-not (Test-XbTmMemberTypeExists -DBSetting $Session.DBSetting -MemberType $type)) { throw "member_type_unknown" }
    }
    $entity = $command.GetMember($memberNo)
    if ($null -eq $entity) { return [ordered]@{ status = "skipped"; code = "member_not_found" } }
    $row = Get-XbAutoCountMemberRow -Entity $entity
    if ($null -eq $row) { throw "autocount_member_row_unavailable" }
    $before = Get-XbTmRecord -Entity $entity
    # Amendment 2: re-check at save; a skip is proven not written (no claim).
    if (-not [string]::Equals([string](Get-XbTmFieldValue -Record $before -Field "MemberType"), $from, [StringComparison]::Ordinal)) {
        return [ordered]@{ status = "skipped"; code = $(if ($isMove) { "not_default_now" } else { "not_moved_type_now" }) }
    }
    if (-not [string]::Equals([string](Get-XbTmFieldValue -Record $before -Field "LastModified"), [string]$Request.expected_last_modified_local, [StringComparison]::Ordinal)) {
        return [ordered]@{ status = "skipped"; code = $(if ($isMove) { "changed_since_snapshot" } else { "changed_since_move" }) }
    }
    if ($isMove) {
        $snapshotAt = ConvertFrom-XbTmLocalTime -Text ([string]$Request.snapshot_at_local)
        foreach ($field in @("IsActive", "ExpiryDate")) { [void](Get-XbTmFieldValue -Record $before -Field $field) }
        $target = Get-XbTmTarget -IsActive $row["IsActive"] -ExpiryDate $row["ExpiryDate"] -SnapshotAt $snapshotAt
        if (-not [string]::Equals($target, $to, [StringComparison]::Ordinal)) { return [ordered]@{ status = "skipped"; code = "target_changed" } }
    }
    $beforeDigest = Get-XbTmFieldsDigest -Record $before -Salt ([string]$Request.salt)

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

    # Amendment 3: MemberType is the only column assigned.
    Set-XbAutoCountMemberRowValue -Row $row -Field "MemberType" -Value $to
    $saveCount = 1
    $command.SaveMember($entity)

    $readback = $command.GetMember($memberNo)
    $result = [ordered]@{
        status = "saved"
        save_invocation_count = $saveCount
        readback_found = ($null -ne $readback)
        readback_type_match = $false
        fields_unchanged = $false
        changed_fields = @()
        post_last_modified_local = $null
        outbound_queue_changed = $false
    }
    if ($null -ne $readback) {
        $after = Get-XbTmRecord -Entity $readback
        $result.readback_type_match = [string]::Equals([string](Get-XbTmFieldValue -Record $after -Field "MemberType"), $to, [StringComparison]::Ordinal)
        $result.fields_unchanged = [string]::Equals((Get-XbTmFieldsDigest -Record $after -Salt ([string]$Request.salt)), $beforeDigest, [StringComparison]::Ordinal)
        $result.changed_fields = Get-XbTmChangedFields -Before $before -After $after
        $post = Get-XbTmFieldValue -Record $after -Field "LastModified"
        $result.post_last_modified_local = $(if ($null -ne $post -and [string]$post -cmatch $script:XbTmLastModifiedPattern) { [string]$post } else { $null })
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
