# #268 member type move: AutoCount child bridge, one operation per process.
# Started only by directlocal/xb_member_type_move.py. Protected values arrive
# only on stdin; stdout carries one JSON line for the parent; arguments never
# carry member data. Inert unless -EnableProductionAdapter.
#
# The #155 sync bridge (ac2_directlocal_member_bridge.ps1) is not used or
# changed: this is a separate entry point with its own four operations.
#   type-move-scan    read: one page of members on Default, MEMBER or GUEST
#   type-move-counts  read: members per type and active flag, the three types'
#                     existence, HQ POS sync state
#   type-move-member  read: one member's type, LastModified and digest
#   type-move-save    write: one SaveMember (MemberType only) behind a fence
#                     id, payload digest, deadline watchdog and claim file
# Session sources, chosen before stdin is read:
#   synthetic: -SessionFactory returning a binding marked
#     XbDirectLocalSyntheticFake (tests only); -TargetBook is refused with it.
#   real: -TargetBook; New-XbDlRealSession (ac2_directlocal_book_guard.ps1)
#     admits AED_XBOUNDARIES for these operations only with payload profile
#     type_move_p1, then proves host, assembly pins, book identity, telemetry
#     off and empty outbound queues before any member read.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet("type-move-scan", "type-move-counts", "type-move-member", "type-move-save")][string]$Operation,
    [switch]$EnableProductionAdapter,
    [string]$FenceId,
    [string]$ClaimDirectory,
    [string]$WriteDeadlineUtc,
    [string]$TargetBook,
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [scriptblock]$SessionFactory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "ac2_member_gateway_autocount_adapter.ps1")
. (Join-Path $PSScriptRoot "ac2_directlocal_book_guard.ps1")
. (Join-Path $PSScriptRoot "ac2_member_type_move.ps1")

$script:XbTmRequestKeys = @{
    "type-move-scan" = @("member_type", "after_member_no")
    "type-move-counts" = @()
    "type-move-member" = @("member_no", "salt")
    "type-move-save" = @("fence_id", "payload_canonical", "payload_digest", "digest_salt")
}
$script:XbTmSaveKeys = @("kind", "member_no", "from_type", "to_type", "expected_last_modified_local", "snapshot_at_local", "salt")

function Write-XbTmResult {
    param([Parameter(Mandatory)]$Value)
    [Console]::Out.WriteLine(($Value | ConvertTo-Json -Compress -Depth 5))
}

function ConvertTo-XbTmCode {
    # Only bounded codes leave the process; vendor messages never do.
    param([AllowNull()][string]$Message)
    if ($Message -cmatch '^[a-z0-9_]{1,64}$') { return $Message }
    return "bridge_failed"
}

function Assert-XbTmNotAutoCount {
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return }
    if ($Value.GetType().Assembly.GetName().Name -like "AutoCount*") { throw "real_autocount_object_refused" }
    foreach ($typeName in @($Value.PSObject.TypeNames)) {
        if ([string]$typeName -match '^(Deserialized\.)?AutoCount\.') { throw "real_autocount_object_refused" }
    }
}

function New-XbTmSyntheticSession {
    # Same rule as the #155 bridge: only a positively marked fake binding,
    # every MemberCommand re-checked before use.
    $bindings = @(& $SessionFactory)
    if ($bindings.Count -ne 1 -or $null -eq $bindings[0]) { throw "synthetic_session_binding_invalid" }
    $binding = $bindings[0]
    if ((Get-XbAutoCountBindingValue -Binding $binding -Name "XbDirectLocalSyntheticFake") -ne $true) { throw "synthetic_session_marker_missing" }
    $userSession = Get-XbAutoCountBindingValue -Binding $binding -Name "UserSession"
    $dbSetting = Get-XbAutoCountBindingValue -Binding $binding -Name "DBSetting"
    $innerFactory = Get-XbAutoCountBindingValue -Binding $binding -Name "MemberCommandFactory"
    if ($innerFactory -isnot [scriptblock]) { throw "synthetic_session_binding_invalid" }
    Assert-XbTmNotAutoCount -Value $userSession
    Assert-XbTmNotAutoCount -Value $dbSetting
    $assertFake = ${function:Assert-XbTmNotAutoCount}
    $guardedFactory = {
        param($Session)
        $commands = @(& $innerFactory $Session)
        foreach ($command in $commands) { & $assertFake -Value $command }
        return $commands
    }.GetNewClosure()
    $checked = [pscustomobject]@{ UserSession = $userSession; DBSetting = $dbSetting; MemberCommandFactory = $guardedFactory }
    [void](& $guardedFactory $checked)
    return (New-XbAutoCountSession -EnableProductionAdapter:$EnableProductionAdapter -SessionFactory { $checked }.GetNewClosure())
}

function New-XbTmSession {
    if ($null -ne $SessionFactory) { return (New-XbTmSyntheticSession) }
    return (New-XbDlRealSession -Book $TargetBook -AcRoot $AcRoot -Operation $Operation -PayloadProfile $script:XbTmProfile)
}

function Get-XbTmPayloadDigest {
    param([Parameter(Mandatory)][string]$Salt, [Parameter(Mandatory)][string]$PayloadCanonical)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Salt + ":" + $PayloadCanonical)
        return "sha256:" + (-join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }))
    }
    finally { $sha.Dispose() }
}

$exitCode = 1
$watchdog = $null
try {
    if (-not $EnableProductionAdapter) { throw "autocount_adapter_disabled" }
    if ($null -eq $SessionFactory -and [string]::IsNullOrEmpty($TargetBook)) { throw "autocount_real_bridge_disabled" }
    if ($null -ne $SessionFactory -and -not [string]::IsNullOrEmpty($TargetBook)) { throw "synthetic_session_with_target_book" }
    # The profile is fixed for this entry point, so the book is checked in full
    # before stdin is read.
    if ($null -eq $SessionFactory) { Assert-XbDlAuthorisedBook -Book $TargetBook -Operation $Operation -PayloadProfile $script:XbTmProfile }
    if ($Operation -ceq "type-move-save") {
        if ($FenceId -cnotmatch '^fence-[0-9a-f]{32}$') { throw "fence_id_invalid" }
        if ([string]::IsNullOrWhiteSpace($ClaimDirectory) -or -not (Test-Path -LiteralPath $ClaimDirectory -PathType Container)) { throw "claim_directory_invalid" }
        [DateTimeOffset]$deadline = [DateTimeOffset]::MinValue
        if (-not [DateTimeOffset]::TryParseExact([string]$WriteDeadlineUtc, "yyyy-MM-ddTHH:mm:ssZ", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AssumeUniversal, [ref]$deadline)) {
            throw "writer_deadline_invalid"
        }
        $delayMilliseconds = [int][Math]::Floor(($deadline - [DateTimeOffset]::UtcNow).TotalMilliseconds)
        if ($delayMilliseconds -le 0) { throw "writer_deadline_expired" }
        $watchdogCallback = [System.Threading.TimerCallback]{
            param($State)
            try { [System.Diagnostics.Process]::GetCurrentProcess().Kill() } catch { }
        }
        $watchdog = [System.Threading.Timer]::new($watchdogCallback, $null, $delayMilliseconds, -1)
    }
    $line = [Console]::In.ReadLine()
    if ([string]::IsNullOrWhiteSpace($line)) { throw "bridge_input_missing" }
    try { $raw = $line | ConvertFrom-Json } catch { throw "type_move_request_invalid" }
    $request = ConvertTo-XbTmRequest -Raw $raw -Names $script:XbTmRequestKeys[$Operation]
    if ($Operation -ceq "type-move-save") {
        if ($request.fence_id -isnot [string] -or $request.fence_id -cne $FenceId) { throw "fence_binding_mismatch" }
        if ($request.payload_canonical -isnot [string] -or $request.digest_salt -isnot [string] -or
            (Get-XbTmPayloadDigest -Salt $request.digest_salt -PayloadCanonical $request.payload_canonical) -cne [string]$request.payload_digest) {
            throw "payload_digest_mismatch"
        }
        try { $payloadRaw = $request.payload_canonical | ConvertFrom-Json } catch { throw "type_move_request_invalid" }
        $payload = ConvertTo-XbTmRequest -Raw $payloadRaw -Names $script:XbTmSaveKeys
        # Every value is checked before any session, login or claim.
        Assert-XbTmSaveRequest -Request $payload
        $session = New-XbTmSession
        $result = Invoke-XbTmSave -Session $session -Request $payload -ClaimPath (Join-Path $ClaimDirectory ($FenceId + ".claim")) -FenceId $FenceId -RealSession:($null -eq $SessionFactory)
    }
    else {
        $session = New-XbTmSession
        switch ($Operation) {
            "type-move-scan" { $result = Invoke-XbTmScan -DBSetting $session.DBSetting -Request $request }
            "type-move-counts" { $result = Invoke-XbTmCounts -DBSetting $session.DBSetting }
            "type-move-member" { $result = Invoke-XbTmMemberRead -Session $session -Request $request }
        }
    }
    Write-XbTmResult -Value $result
    $exitCode = 0
}
catch {
    $code = ConvertTo-XbTmCode -Message $_.Exception.Message
    if ($code -eq "fence_already_claimed") { $exitCode = 3 }
    Write-XbTmResult -Value ([ordered]@{ error = $code })
}
finally {
    if ($null -ne $watchdog) { $watchdog.Dispose() }
}
exit $exitCode
