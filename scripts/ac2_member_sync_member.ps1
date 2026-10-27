# #155 phase 1 member sync: the sync_p1 AutoCount member create.
#
# Dot-sourced by ac2_directlocal_member_bridge.ps1 after the gateway adapter,
# whose bytes are pinned for the production worker and stay unchanged. This
# file reuses the adapter's helpers (command binding, row access,
# null-preserving normalisation) and adds only the sync_p1 field set:
#   MemberNo (the phone number or its name fallback), MemberType (the
#   configured ac2_create_member_type, validated here; round 6),
#   Name, MobilePhone/EmailAddress (or NULL), DOB 2000-MM-01 (or NULL),
#   RegisterDate, ExpiryDate NULL, OpeningPoints 0, IsActive T, Individual T.
# It is reached only from the bridge's write-once path (fence, digest,
# deadline watchdog, exclusive claim file).

Set-StrictMode -Version Latest

$script:XbSyncCallerFields = @("MemberNo", "Name", "RegisterDate", "MobilePhone", "EmailAddress", "DOB")
$script:XbSyncAdapterOwnedFields = @("MemberType", "ExpiryDate", "OpeningPoints", "IsActive", "Individual")
# A member type code, never a caller field on the member (round 6).
$script:XbSyncMemberTypePattern = '\A[A-Za-z0-9][A-Za-z0-9_-]{0,19}\z'

function Assert-XbSyncMemberInput {
    param([Parameter(Mandatory)][hashtable]$Member)
    foreach ($field in @($Member.Keys)) {
        if ($script:XbSyncAdapterOwnedFields -contains $field) { throw "adapter_managed_defaults_are_not_caller_inputs" }
        if ($script:XbSyncCallerFields -cnotcontains $field) { throw "member_field_not_allowed" }
    }
    foreach ($field in @("MemberNo", "Name", "RegisterDate")) {
        if (-not $Member.Contains($field) -or $Member[$field] -isnot [string] -or [string]::IsNullOrWhiteSpace($Member[$field])) {
            throw "member_field_required"
        }
    }
    foreach ($field in @("MobilePhone", "EmailAddress", "DOB")) {
        if ($Member.Contains($field) -and $null -ne $Member[$field]) {
            if ($Member[$field] -isnot [string] -or [string]::IsNullOrWhiteSpace($Member[$field])) { throw "empty_string_not_null_equivalent" }
        }
    }
    [datetime]$parsed = [datetime]::MinValue
    if (-not [datetime]::TryParseExact([string]$Member.RegisterDate, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::None, [ref]$parsed)) {
        throw "member_date_invalid"
    }
    # DOB carries only the birthday month: 2000-MM-01.
    if ($Member.Contains("DOB") -and $null -ne $Member.DOB -and [string]$Member.DOB -cnotmatch '^2000-(0[1-9]|1[0-2])-01$') {
        throw "member_date_invalid"
    }
}

function Assert-XbSyncMemberType {
    param([AllowNull()][AllowEmptyString()][string]$MemberType)
    if ([string]::IsNullOrEmpty($MemberType) -or $MemberType -cnotmatch $script:XbSyncMemberTypePattern) { throw "member_type_invalid" }
}

function New-XbSyncAutoCountMember {
    param(
        [Parameter(Mandatory)]$Session,
        [Parameter(Mandatory)][hashtable]$Member,
        [Parameter(Mandatory)][string]$MemberType,
        [Parameter(Mandatory)][switch]$EnableProductionAdapter
    )
    Assert-XbAutoCountAdapterEnabled -EnableProductionAdapter:$EnableProductionAdapter
    Assert-XbSyncMemberInput -Member $Member
    Assert-XbSyncMemberType -MemberType $MemberType
    $command = Get-XbAutoCountMemberCommand -Session $Session
    $entity = $command.NewMember($false)
    if ($null -eq $entity) { throw "autocount_new_member_failed" }
    $row = Get-XbAutoCountMemberRow -Entity $entity
    if ($null -eq $row) { throw "autocount_member_row_unavailable" }

    $assignments = [ordered]@{
        MemberNo      = [string]$Member.MemberNo
        MemberType    = $MemberType
        Name          = [string]$Member.Name
        MobilePhone   = if ($Member.Contains("MobilePhone") -and $null -ne $Member.MobilePhone) { [string]$Member.MobilePhone } else { [System.DBNull]::Value }
        EmailAddress  = if ($Member.Contains("EmailAddress") -and $null -ne $Member.EmailAddress) { [string]$Member.EmailAddress } else { [System.DBNull]::Value }
        DOB           = if ($Member.Contains("DOB") -and $null -ne $Member.DOB) {
            [datetime]::ParseExact([string]$Member.DOB, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture)
        } else { [System.DBNull]::Value }
        RegisterDate  = [datetime]::ParseExact([string]$Member.RegisterDate, "yyyy-MM-dd", [System.Globalization.CultureInfo]::InvariantCulture)
        ExpiryDate    = [System.DBNull]::Value
        OpeningPoints = [decimal]0
        IsActive      = "T"
        Individual    = "T"
    }
    $expected = [ordered]@{}
    foreach ($field in $script:XbAutoCountAssignedFields) {
        $expected[$field] = Convert-XbAutoCountNullableValue -Value $assignments[$field] -Field $field
    }
    foreach ($field in $assignments.Keys) {
        Set-XbAutoCountMemberRowValue -Row $row -Field $field -Value $assignments[$field]
    }

    $saveCount = 1
    # The sync's only irreversible member write; reached once per write-once child.
    $command.SaveMember($entity)

    $readback = $command.GetMember([string]$Member.MemberNo)
    [pscustomobject]@{
        SaveInvocationCount = $saveCount
        Expected = $expected
        # shopify_m1 is the adapter's null-preserving read-back normalisation.
        ReadBack = if ($null -ne $readback) { Convert-XbAutoCountMemberRecord -Entity $readback -FieldProfile "shopify_m1" } else { $null }
    }
}

function Compare-XbSyncMemberReadBack {
    # NULL equals only NULL; "" is a different value.
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Expected, [AllowNull()][object]$Actual)
    if ($null -eq $Actual) { return [pscustomobject]@{ Found = $false; Match = $false } }
    $mismatches = 0
    foreach ($field in $script:XbAutoCountAssignedFields) {
        $want = $Expected[$field]
        $have = $Actual[$field]
        if (($null -eq $want) -ne ($null -eq $have)) { $mismatches++; continue }
        if ($null -ne $want -and $want -cne $have) { $mismatches++ }
    }
    [pscustomobject]@{ Found = $true; Match = ($mismatches -eq 0) }
}
