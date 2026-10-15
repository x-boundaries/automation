# DirectLocal #155 AutoCount child bridge: one operation per process.
# Reuses ac2_member_gateway_autocount_adapter.ps1 unchanged (shopify_m1).
# Protected values arrive only on stdin; stdout carries one PII-free JSON line;
# arguments never carry customer data. Inert unless -EnableProductionAdapter.
#
# Two session sources, chosen before stdin is read:
#   synthetic: -SessionFactory returning a binding marked
#     XbDirectLocalSyntheticFake, whose UserSession, DBSetting and every
#     MemberCommand are not AutoCount types. -TargetBook is refused with it.
#   real: -TargetBook naming a book this source authorises (AED_TESTINGAPI
#     only). New-XbDlRealSession (ac2_directlocal_book_guard.ps1) checks host,
#     assembly pins, pins the book for this process, proves the authenticated
#     book identity, vendor telemetry off and empty outbound queues before any
#     member operation. Credentials come only from the process environment,
#     which only the Owner console procedure supplies.
# With neither, the bridge refuses (autocount_real_bridge_disabled) before
# stdin is read, whatever flags, fence ids or AutoCount environment exist.
#
# write-once is the only path that reaches SaveMember. It additionally needs a
# fence id bound to the stdin payload, a payload digest match, an unexpired
# deadline (self-terminating watchdog) and an exclusive CreateNew claim file
# created immediately before New-XbAutoCountMember. An existing claim exits 3
# without SaveMember. Claim and outcome files are never deleted here.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet("book-check", "pos-sync-status", "legacy-precheck", "next-member-no", "probe", "reconcile", "write-once")][string]$Operation,
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

$script:XbDirectLocalMemberNoPattern = '^[A-Za-z0-9][A-Za-z0-9._/-]{0,19}$'

function Write-XbDirectLocalResult {
    param([Parameter(Mandatory)]$Value)
    [Console]::Out.WriteLine(($Value | ConvertTo-Json -Compress -Depth 4))
}

function ConvertTo-XbDirectLocalCode {
    # Only bounded adapter codes leave the process; vendor messages never do.
    param([AllowNull()][string]$Message)
    if ($Message -cmatch '^[a-z0-9_]{1,64}$') { return $Message }
    return "bridge_failed"
}

function Read-XbDirectLocalInput {
    $line = [Console]::In.ReadLine()
    if ([string]::IsNullOrWhiteSpace($line)) { throw "bridge_input_missing" }
    return ($line | ConvertFrom-Json)
}

function Get-XbDirectLocalPayloadDigest {
    param([Parameter(Mandatory)][string]$Salt, [Parameter(Mandatory)][string]$PayloadCanonical)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Salt + ":" + $PayloadCanonical)
        return "sha256:" + (-join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }))
    }
    finally { $sha.Dispose() }
}

function Assert-XbDirectLocalNotAutoCount {
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return }
    if ($Value.GetType().Assembly.GetName().Name -like "AutoCount*") { throw "real_autocount_object_refused" }
    foreach ($typeName in @($Value.PSObject.TypeNames)) {
        if ([string]$typeName -match '^(Deserialized\.)?AutoCount\.') { throw "real_autocount_object_refused" }
    }
}

function New-XbDirectLocalSyntheticSession {
    # The only session source: a positively marked fake binding. Every
    # MemberCommand it yields is re-checked before the adapter may use it.
    if ($null -eq $SessionFactory) { throw "autocount_real_bridge_disabled" }
    $bindings = @(& $SessionFactory)
    if ($bindings.Count -ne 1 -or $null -eq $bindings[0]) { throw "synthetic_session_binding_invalid" }
    $binding = $bindings[0]
    if ((Get-XbAutoCountBindingValue -Binding $binding -Name "XbDirectLocalSyntheticFake") -ne $true) { throw "synthetic_session_marker_missing" }
    $userSession = Get-XbAutoCountBindingValue -Binding $binding -Name "UserSession"
    $dbSetting = Get-XbAutoCountBindingValue -Binding $binding -Name "DBSetting"
    $innerFactory = Get-XbAutoCountBindingValue -Binding $binding -Name "MemberCommandFactory"
    if ($innerFactory -isnot [scriptblock]) { throw "synthetic_session_binding_invalid" }
    Assert-XbDirectLocalNotAutoCount -Value $userSession
    Assert-XbDirectLocalNotAutoCount -Value $dbSetting
    $assertFake = ${function:Assert-XbDirectLocalNotAutoCount}
    $guardedFactory = {
        param($Session)
        $commands = @(& $innerFactory $Session)
        foreach ($command in $commands) { & $assertFake -Value $command }
        return $commands
    }.GetNewClosure()
    $checked = [pscustomobject]@{ UserSession = $userSession; DBSetting = $dbSetting; MemberCommandFactory = $guardedFactory }
    # Validate the command type now, before any claim or adapter call.
    [void](& $guardedFactory $checked)
    return (New-XbAutoCountSession -EnableProductionAdapter:$EnableProductionAdapter -SessionFactory { $checked }.GetNewClosure())
}

function New-XbDirectLocalSession {
    if ($null -ne $SessionFactory) { return (New-XbDirectLocalSyntheticSession) }
    return (New-XbDlRealSession -Book $TargetBook -AcRoot $AcRoot)
}

function Assert-XbDirectLocalMemberNo {
    param([AllowNull()][string]$MemberNo)
    if ([string]::IsNullOrEmpty($MemberNo) -or $MemberNo -cnotmatch $script:XbDirectLocalMemberNoPattern) { throw "member_no_invalid" }
}

function Invoke-XbDirectLocalLegacyPrecheck {
    # Symmetric +65 matching: one unchanged adapter lookup per phone key.
    param([Parameter(Mandatory)]$Session, [Parameter(Mandatory)]$Request)
    $keys = @($Request.phone_keys | Where-Object { $null -ne $_ })
    if ($keys.Count -gt 2) { throw "phone_keys_invalid" }
    foreach ($key in $keys) { if ([string]$key -cnotmatch '^[0-9]{6,15}$') { throw "phone_keys_invalid" } }
    $email = $Request.email_address
    $results = @()
    if ($keys.Count -eq 0) {
        $results += Test-XbAutoCountLegacyMemberCandidate -Session $Session -MobilePhone $null -EmailAddress $email
    }
    foreach ($key in $keys) {
        $results += Test-XbAutoCountLegacyMemberCandidate -Session $Session -MobilePhone ([string]$key) -EmailAddress $email
    }
    $failed = @($results | Where-Object { $_.outcome -ne "NO_CANDIDATE" -and $_.outcome -ne "CANDIDATE_FOUND" }).Count -gt 0
    $phoneMemberNo = @($results | Where-Object { $_.phone_member_no_hit -eq $true }).Count -gt 0
    $mobile = @($results | Where-Object { $_.mobile_phone_hit -eq $true }).Count -gt 0
    $emailHit = @($results | Where-Object { $_.email_hit -eq $true }).Count -gt 0
    $outcome = if ($failed) { "LOOKUP_FAILED" } elseif ($phoneMemberNo -or $mobile -or $emailHit) { "CANDIDATE_FOUND" } else { "NO_CANDIDATE" }
    return [ordered]@{ outcome = $outcome; phone_member_no_hit = $phoneMemberNo; mobile_phone_hit = $mobile; email_hit = $emailHit; lookups = $results.Count }
}

function Invoke-XbDirectLocalWriteOnce {
    param([Parameter(Mandatory)]$Request, [Parameter(Mandatory)][string]$ClaimPath)
    if ([string]$Request.fence_id -cne $FenceId) { throw "fence_binding_mismatch" }
    $payloadText = [string]$Request.payload_canonical
    if ((Get-XbDirectLocalPayloadDigest -Salt ([string]$Request.salt) -PayloadCanonical $payloadText) -cne [string]$Request.payload_digest) {
        throw "payload_digest_mismatch"
    }
    Assert-XbDirectLocalMemberNo -MemberNo ([string]$Request.member_no)
    $payload = $payloadText | ConvertFrom-Json
    $member = @{
        MemberNo = [string]$Request.member_no
        Name = [string]$payload.name
        RegisterDate = [string]$payload.register_date
        ExpiryDate = [string]$payload.expiry_date
    }
    if ($null -ne $payload.mobile_phone) { $member.MobilePhone = [string]$payload.mobile_phone }
    if ($null -ne $payload.email_address) { $member.EmailAddress = [string]$payload.email_address }
    # A login, identity or outbound-queue failure here stops before the claim.
    $session = New-XbDirectLocalSession

    # Exclusive claim immediately before the sole SaveMember path.
    try {
        $stream = [System.IO.File]::Open($ClaimPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    }
    catch {
        if (Test-Path -LiteralPath $ClaimPath) { throw "fence_already_claimed" }
        throw "claim_create_failed"
    }
    try {
        $claim = [System.Text.Encoding]::ASCII.GetBytes((@{ fence_id = $FenceId; pid = $PID; claimed_at = [DateTimeOffset]::UtcNow.ToString("o") } | ConvertTo-Json -Compress))
        $stream.Write($claim, 0, $claim.Length)
        $stream.Flush($true)
    }
    finally { $stream.Dispose() }

    $created = New-XbAutoCountMember -Session $session -Member $member -EnableProductionAdapter:$EnableProductionAdapter -FieldProfile "shopify_m1"
    $check = Compare-XbAutoCountMemberReadBack -Expected $created.Expected -Actual $created.ReadBack -FieldProfile "shopify_m1"
    $outboundChanged = if ($null -ne $SessionFactory) { $false } else { Test-XbDlOutboundChanged -DBSetting $session.DBSetting }
    $result = [ordered]@{
        save_invocation_count = [int]$created.SaveInvocationCount
        readback_found = [bool]$check.Found
        readback_match = [bool]$check.Match
        outbound_queue_changed = [bool]$outboundChanged
    }
    try {
        $outcomeBytes = [System.Text.Encoding]::ASCII.GetBytes(($result | ConvertTo-Json -Compress))
        $outcomeStream = [System.IO.File]::Open(($ClaimPath -replace '\.claim$', '.outcome'), [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try { $outcomeStream.Write($outcomeBytes, 0, $outcomeBytes.Length); $outcomeStream.Flush($true) } finally { $outcomeStream.Dispose() }
    }
    catch { $result.outcome_file = "write_failed" }
    return $result
}

$exitCode = 1
$watchdog = $null
try {
    if (-not $EnableProductionAdapter) { throw "autocount_adapter_disabled" }
    # Session source decided before stdin, claims or any adapter call.
    if ($null -eq $SessionFactory -and [string]::IsNullOrEmpty($TargetBook)) { throw "autocount_real_bridge_disabled" }
    if ($null -ne $SessionFactory -and -not [string]::IsNullOrEmpty($TargetBook)) { throw "synthetic_session_with_target_book" }
    if ($null -eq $SessionFactory) { Assert-XbDlAuthorisedBook -Book $TargetBook }
    if ($Operation -eq "write-once") {
        if ($FenceId -cnotmatch '^fence-[0-9a-f]{32}$') { throw "fence_id_invalid" }
        if ([string]::IsNullOrWhiteSpace($ClaimDirectory) -or -not (Test-Path -LiteralPath $ClaimDirectory -PathType Container)) { throw "claim_directory_invalid" }
        [DateTimeOffset]$deadline = [DateTimeOffset]::MinValue
        if (-not [DateTimeOffset]::TryParseExact([string]$WriteDeadlineUtc, "yyyy-MM-ddTHH:mm:ssZ", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AssumeUniversal, [ref]$deadline)) {
            throw "writer_deadline_invalid"
        }
        $delayMilliseconds = [int][Math]::Floor(($deadline - [DateTimeOffset]::UtcNow).TotalMilliseconds)
        if ($delayMilliseconds -le 0) { throw "writer_deadline_expired" }
        # Same self-termination pattern as the gateway child writer.
        $watchdogCallback = [System.Threading.TimerCallback]{
            param($State)
            try { [System.Diagnostics.Process]::GetCurrentProcess().Kill() } catch { }
        }
        $watchdog = [System.Threading.Timer]::new($watchdogCallback, $null, $delayMilliseconds, -1)
        $request = Read-XbDirectLocalInput
        $result = Invoke-XbDirectLocalWriteOnce -Request $request -ClaimPath (Join-Path $ClaimDirectory ($FenceId + ".claim"))
    }
    else {
        $request = Read-XbDirectLocalInput
        $session = New-XbDirectLocalSession
        switch ($Operation) {
            "book-check" {
                # Read-only E5 qualification: New-XbDlRealSession already proved
                # identity and empty queues; nothing member-level is touched.
                if ($null -ne $SessionFactory) { throw "book_check_requires_real_session" }
                $result = [ordered]@{ book = $TargetBook; identity_verified = $true; outbound_quiet = $true }
            }
            "pos-sync-status" {
                if ($null -ne $SessionFactory) { throw "pos_sync_status_requires_real_session" }
                try { $result = Get-XbDlPosSyncState -DBSetting $session.DBSetting } catch { throw "pos_sync_state_unreadable" }
            }
            "legacy-precheck" { $result = Invoke-XbDirectLocalLegacyPrecheck -Session $session -Request $request }
            "next-member-no" { $result = [ordered]@{ member_no = (Get-XbAutoCountNextMemberNo -Session $session) } }
            "probe" {
                Assert-XbDirectLocalMemberNo -MemberNo ([string]$request.member_no)
                try { $found = Get-XbAutoCountMember -Session $session -MemberNo ([string]$request.member_no) }
                catch { throw "member_no_probe_failed" }
                $result = [ordered]@{ status = $(if ($null -eq $found) { "FREE" } else { "OCCUPIED" }) }
            }
            "reconcile" {
                Assert-XbDirectLocalMemberNo -MemberNo ([string]$request.member_no)
                $payload = ([string]$request.payload_canonical) | ConvertFrom-Json
                $expected = New-XbAutoCountShopifyExpectedRecord -MemberNo ([string]$request.member_no) -CreatePayload $payload
                $actual = Get-XbAutoCountMember -Session $session -MemberNo ([string]$request.member_no)
                $check = Compare-XbAutoCountMemberReadBack -Expected $expected -Actual $actual -FieldProfile "shopify_m1"
                $result = [ordered]@{ found = [bool]$check.Found; match = [bool]$check.Match }
            }
        }
    }
    Write-XbDirectLocalResult -Value $result
    $exitCode = 0
}
catch {
    $code = ConvertTo-XbDirectLocalCode -Message $_.Exception.Message
    if ($code -eq "fence_already_claimed") { $exitCode = 3 }
    Write-XbDirectLocalResult -Value ([ordered]@{ error = $code })
}
finally {
    if ($null -ne $watchdog) { $watchdog.Dispose() }
}
exit $exitCode
