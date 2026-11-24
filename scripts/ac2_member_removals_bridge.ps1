# #268 slice E: AutoCount child bridge of the removals tool, one operation
# per process. Started only by directlocal/xb_member_removals.py. Protected
# values arrive only on stdin; stdout carries one JSON line for the parent;
# arguments never carry member data. Inert unless -EnableProductionAdapter.
#
# The #155 sync, type move and benefits bridges are not used or changed: this
# is a separate entry point with its own four operations.
#   removal-scan     read: one page of MEMBERs whose ExpiryDate is before the
#                    run day, NULL or a placeholder, and the server time
#   removal-member   read: one member's type, active flag, expiry,
#                    LastModified, digest, and the server time
#   removal-save     write (benefit_x1): one SaveMember, MemberType MEMBER ->
#                    GUEST only, behind a fence id, payload digest, deadline
#                    watchdog and claim file
#   removal-restore  write (benefit_x1r): one SaveMember, MemberType GUEST ->
#                    MEMBER only, for one removal of this tool (undo)
# Session sources, chosen before stdin is read:
#   synthetic: -SessionFactory returning a binding marked
#     XbDirectLocalSyntheticFake (tests only); -TargetBook is refused with it.
#   real: -TargetBook; New-XbDlRealSession (ac2_directlocal_book_guard.ps1)
#     admits AED_XBOUNDARIES for removal-* only with profile benefit_x1 and
#     for removal-restore only with benefit_x1r, then proves host, assembly
#     pins, book identity, telemetry off and empty outbound queues before any
#     member read.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet("removal-scan", "removal-member", "removal-save", "removal-restore")][string]$Operation,
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
. (Join-Path $PSScriptRoot "ac2_member_removals.ps1")

$script:XbMxRequestKeys = @{
    "removal-scan" = @("after_member_no", "run_day_local")
    "removal-member" = @("member_no", "salt")
    "removal-save" = @("fence_id", "payload_canonical", "payload_digest", "digest_salt")
    "removal-restore" = @("fence_id", "payload_canonical", "payload_digest", "digest_salt")
}
$script:XbMxSaveKeys = @("kind", "member_no", "from_type", "to_type", "expected_last_modified_local", "expected_expiry_local", "run_day_local", "basis_expiry_local", "salt")
$script:XbMxRestoreKeys = @("kind", "member_no", "from_type", "to_type", "expected_last_modified_local", "expected_expiry_local", "removal_id", "salt")
$script:XbMxOperationProfile = $(if ($Operation -ceq "removal-restore") { $script:XbMxRestoreProfile } else { $script:XbMxProfile })

function Write-XbMxResult {
    param([Parameter(Mandatory)]$Value)
    [Console]::Out.WriteLine(($Value | ConvertTo-Json -Compress -Depth 5))
}

function ConvertTo-XbMxCode {
    # Only bounded codes leave the process; vendor messages never do.
    param([AllowNull()][string]$Message)
    if ($Message -cmatch '^[a-z0-9_]{1,64}$') { return $Message }
    return "bridge_failed"
}

function Assert-XbMxNotAutoCount {
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return }
    if ($Value.GetType().Assembly.GetName().Name -like "AutoCount*") { throw "real_autocount_object_refused" }
    foreach ($typeName in @($Value.PSObject.TypeNames)) {
        if ([string]$typeName -match '^(Deserialized\.)?AutoCount\.') { throw "real_autocount_object_refused" }
    }
}

function New-XbMxSyntheticSession {
    # Same rule as the #155, type move and benefits bridges: only a positively
    # marked fake binding, every MemberCommand re-checked before use.
    $bindings = @(& $SessionFactory)
    if ($bindings.Count -ne 1 -or $null -eq $bindings[0]) { throw "synthetic_session_binding_invalid" }
    $binding = $bindings[0]
    if ((Get-XbAutoCountBindingValue -Binding $binding -Name "XbDirectLocalSyntheticFake") -ne $true) { throw "synthetic_session_marker_missing" }
    $userSession = Get-XbAutoCountBindingValue -Binding $binding -Name "UserSession"
    $dbSetting = Get-XbAutoCountBindingValue -Binding $binding -Name "DBSetting"
    $innerFactory = Get-XbAutoCountBindingValue -Binding $binding -Name "MemberCommandFactory"
    if ($innerFactory -isnot [scriptblock]) { throw "synthetic_session_binding_invalid" }
    Assert-XbMxNotAutoCount -Value $userSession
    Assert-XbMxNotAutoCount -Value $dbSetting
    $assertFake = ${function:Assert-XbMxNotAutoCount}
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

function New-XbMxSession {
    if ($null -ne $SessionFactory) { return (New-XbMxSyntheticSession) }
    return (New-XbDlRealSession -Book $TargetBook -AcRoot $AcRoot -Operation $Operation -PayloadProfile $script:XbMxOperationProfile)
}

function Get-XbMxPayloadDigest {
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
    # The profile is fixed per operation, so the book is checked in full
    # before stdin is read.
    if ($null -eq $SessionFactory) { Assert-XbDlAuthorisedBook -Book $TargetBook -Operation $Operation -PayloadProfile $script:XbMxOperationProfile }
    $isWrite = ($Operation -ceq "removal-save" -or $Operation -ceq "removal-restore")
    if ($isWrite) {
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
    try { $raw = $line | ConvertFrom-Json } catch { throw "removal_request_invalid" }
    try { $request = ConvertTo-XbTmRequest -Raw $raw -Names $script:XbMxRequestKeys[$Operation] }
    catch { throw "removal_request_invalid" }
    if ($isWrite) {
        if ($request.fence_id -isnot [string] -or $request.fence_id -cne $FenceId) { throw "fence_binding_mismatch" }
        if ($request.payload_canonical -isnot [string] -or $request.digest_salt -isnot [string] -or
            (Get-XbMxPayloadDigest -Salt $request.digest_salt -PayloadCanonical $request.payload_canonical) -cne [string]$request.payload_digest) {
            throw "payload_digest_mismatch"
        }
        try { $payloadRaw = $request.payload_canonical | ConvertFrom-Json } catch { throw "removal_request_invalid" }
        $keys = $(if ($Operation -ceq "removal-save") { $script:XbMxSaveKeys } else { $script:XbMxRestoreKeys })
        try { $payload = ConvertTo-XbTmRequest -Raw $payloadRaw -Names $keys }
        catch { throw "removal_request_invalid" }
        # Every value is checked before any session, login or claim.
        if ($Operation -ceq "removal-save") { Assert-XbMxSaveRequest -Request $payload } else { Assert-XbMxRestoreRequest -Request $payload }
        $session = New-XbMxSession
        $claimPath = Join-Path $ClaimDirectory ($FenceId + ".claim")
        if ($Operation -ceq "removal-save") {
            $result = Invoke-XbMxSave -Session $session -Request $payload -ClaimPath $claimPath -FenceId $FenceId -RealSession:($null -eq $SessionFactory)
        }
        else {
            $result = Invoke-XbMxRestore -Session $session -Request $payload -ClaimPath $claimPath -FenceId $FenceId -RealSession:($null -eq $SessionFactory)
        }
    }
    else {
        $session = New-XbMxSession
        switch ($Operation) {
            "removal-scan" { $result = Invoke-XbMxScan -DBSetting $session.DBSetting -Request $request }
            "removal-member" { $result = Invoke-XbMxMemberRead -Session $session -Request $request }
        }
    }
    Write-XbMxResult -Value $result
    $exitCode = 0
}
catch {
    $code = ConvertTo-XbMxCode -Message $_.Exception.Message
    if ($code -eq "fence_already_claimed") { $exitCode = 3 }
    Write-XbMxResult -Value ([ordered]@{ error = $code })
}
finally {
    if ($null -ne $watchdog) { $watchdog.Dispose() }
}
exit $exitCode
