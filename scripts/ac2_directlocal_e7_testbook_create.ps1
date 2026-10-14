# AC2 #155 E7: ONE synthetic Member create on the AED_TESTINGAPI test book only.
#
# Isolated from the DirectLocal core: the core's synthetic hard-disarm and its
# bridge are untouched and never enabled here. This script reuses the unchanged
# adapter (reviewed login, GetNextMemberNo, GetMember, the single SaveMember in
# New-XbAutoCountMember with the shopify_m1 profile).
#
# Guards, in order (any failure stops before the next step):
#   host DESKTOP-4I042L6; owned state dir + marker bound to RUN/LOCK/book;
#   no XB_AC2_SESSION_FACTORY; exclusive lock; one-shot (intent/claim/outcome
#   absent); fresh ISOLATED evidence for the book; authenticated vendor session
#   whose DBSetting.DBName, vendor DB_NAME() and vendor MachineName all match
#   (production book refused); no prior E7 synthetic member.
# Write mode additionally needs three switches, then: durable intent ->
#   <= 3 GetNextMemberNo candidates (each probed with GetMember) -> re-probe ->
#   fence with deadline -> exclusive claim -> SaveMember at most once ->
#   one GetMember readback -> null-aware exact comparison -> outcome file.
# Any exception after the claim is WRITE_OUTCOME_UNKNOWN_HOLD: never retried;
# the same MemberNo is only re-read. Contact fields are deliberately NULL.
# The database is pinned to AED_TESTINGAPI for this process; the AC2_PROBE_*
# database name in the environment is never used.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$StateDir,
    [Parameter(Mandatory)][string]$IsolationEvidencePath,
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$ServerName = $env:AC2_PROBE_SERVER_NAME,
    [switch]$EnableTestBookWrite,
    [switch]$ConfirmTargetAedTestingApi,
    [switch]$ConfirmOwnerRiskAccepted,
    [ValidateRange(1, 600)][int]$FenceTtlSeconds = 120,
    [ValidateRange(1, 120)][int]$IsolationMaxAgeMinutes = 30,
    [string]$JsonOut,
    # Test-only seams. A -File invocation cannot pass a scriptblock, so the
    # production path always performs the reviewed vendor login.
    [scriptblock]$SessionFactory,
    [string]$TestExpectedHost,
    [int]$TestDelayBeforeClaimMs = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "ac2_member_gateway_autocount_adapter.ps1")

$script:E7Run = "2026-10-14-xb-155-directlocal-aed-testingapi-single-member-uat-001"
$script:E7Lock = "DL-XB-155-DIRECTLOCAL-AED_TESTINGAPI-ONE-SAVE-UAT-001"
$script:E7Book = "AED_TESTINGAPI"
$script:E7ForbiddenBooks = @("AED_XBOUNDARIES")
$script:E7Host = "DESKTOP-4I042L6"
$script:E7NamePrefix = "XB DIRECTLOCAL E7 SYNTHETIC"
$script:E7MaxCandidates = 3

function ConvertTo-E7Code {
    # Only bounded codes leave the process; vendor messages never do.
    param([AllowNull()][string]$Message)
    if ($Message -cmatch '^[a-z0-9_]{1,64}$') { return $Message }
    return "e7_failed"
}

function Write-E7File {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Value)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 6 -Compress))
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}

function Get-E7Scalar {
    # Read-only vendor query through the authenticated DBSetting.
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)][string]$Sql)
    if ($Sql -notmatch '^\s*SELECT\b' -or $Sql -match '(?i)\b(insert|update|delete|merge|drop|alter|create|exec|into)\b') { throw "e7_vendor_query_not_read_only" }
    return $DBSetting.ExecuteScalar($Sql, [object[]]@())
}

function Assert-E7BookIdentity {
    param([Parameter(Mandatory)]$Session, [Parameter(Mandatory)][string]$ExpectedHost)
    try {
        $db = $Session.DBSetting
        $declared = [string]$db.DBName
        $vendorDb = [string](Get-E7Scalar -DBSetting $db -Sql "SELECT DB_NAME()")
        $vendorHost = [string](Get-E7Scalar -DBSetting $db -Sql "SELECT CAST(SERVERPROPERTY('MachineName') AS nvarchar(128))")
    }
    catch { throw "book_identity_unverifiable" }
    foreach ($name in @($declared, $vendorDb)) {
        if ($script:E7ForbiddenBooks -contains $name) { throw "production_book_refused" }
    }
    if ([string]::IsNullOrWhiteSpace($declared) -or [string]::IsNullOrWhiteSpace($vendorDb)) { throw "book_identity_missing" }
    if ($declared -cne $vendorDb) { throw "book_identity_ambiguous" }
    if ($vendorDb -cne $script:E7Book) { throw "book_identity_wrong" }
    if ($vendorHost -ine $ExpectedHost) { throw "book_host_wrong" }
    return [ordered]@{ declared_db = $declared; vendor_db = $vendorDb; vendor_machine = $vendorHost }
}

function Get-E7AuditSnapshot {
    param([Parameter(Mandatory)]$DBSetting)
    [ordered]@{
        member_change_counter = [long](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT ISNULL(MAX(Counter), 0) FROM ChangeCount WHERE TableName = 'Member'")
        change_log_rows = [long](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM ChangeLog")
        mail_queue_rows = [long](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM MailQueue")
        member_rows = [long](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM Member")
        pos_last_alive = [string](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT CONVERT(nvarchar(30), MAX(LastAliveTime), 126) FROM SyncRegistration")
    }
}

function Test-E7IsolationEvidence {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "isolation_evidence_missing" }
    $evidence = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$evidence.verdict -cne "ISOLATED" -or [string]$evidence.book -cne $script:E7Book) { throw "isolation_not_proven" }
    $raw = $evidence.run_at
    $at = if ($raw -is [datetime]) { [DateTimeOffset]::new(([datetime]$raw).ToUniversalTime()) } else { [DateTimeOffset]::Parse([string]$raw, [System.Globalization.CultureInfo]::InvariantCulture) }
    $age = ([DateTimeOffset]::UtcNow - $at).TotalMinutes
    if ($age -lt -2 -or $age -gt $IsolationMaxAgeMinutes) { throw "isolation_evidence_stale" }
    return [ordered]@{ verdict = "ISOLATED"; evidence_age_minutes = [Math]::Round($age, 1); touching_processes = @($evidence.touching_processes | ForEach-Object { [string]$_.process }) }
}

$writeMode = [bool]($EnableTestBookWrite -and $ConfirmTargetAedTestingApi -and $ConfirmOwnerRiskAccepted)
$result = [ordered]@{
    run = $script:E7Run; lock = $script:E7Lock; mode = $(if ($writeMode) { "write" } else { "dry_run" })
    status = "STARTED"; code = $null; book_identity = $null; isolation = $null
    get_next_member_no_count = 0; save_member_invocations = 0; member_no = $null
    readback_found = $null; readback_match = $null; mismatches = @(); audit_before = $null; audit_after = $null; reconcile = $null
}
$lock = $null
$exitCode = 1
try {
    # 1. Host and test seams.
    if ($null -ne $SessionFactory) {
        if ([string]::IsNullOrWhiteSpace($TestExpectedHost)) { throw "test_host_required" }
        $expectedHost = $TestExpectedHost
    }
    else {
        if ($TestDelayBeforeClaimMs -ne 0 -or -not [string]::IsNullOrWhiteSpace($TestExpectedHost)) { throw "test_seam_without_test_session" }
        $expectedHost = $script:E7Host
    }
    if ($env:COMPUTERNAME -ine $expectedHost) { throw "host_not_authorised" }
    if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("XB_AC2_SESSION_FACTORY", "Process"))) { throw "session_factory_env_refused" }

    # 2. Owned state directory bound to this RUN/LOCK/book.
    $state = [System.IO.Path]::GetFullPath($StateDir)
    $markerPath = Join-Path (Split-Path -Parent $state) "XB_E7_UAT_OWNER.json"
    if (-not (Test-Path -LiteralPath $state -PathType Container) -or -not (Test-Path -LiteralPath $markerPath -PathType Leaf)) { throw "state_dir_not_owned" }
    $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$marker.run -cne $script:E7Run -or [string]$marker.lock -cne $script:E7Lock -or [string]$marker.target_book -cne $script:E7Book) { throw "state_dir_binding_mismatch" }

    # 3. Exclusive lock for the whole run; one-shot replay refusal.
    try { $lock = [System.IO.File]::Open((Join-Path $state "e7.lock"), [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None) }
    catch { throw "e7_lock_held" }
    foreach ($name in @("e7.intent.json", "e7.fence.json", "e7.claim", "e7.outcome.json")) {
        if (Test-Path -LiteralPath (Join-Path $state $name)) { throw "e7_already_executed" }
    }

    # 4. Fresh outbound-isolation evidence for exactly this book.
    $result.isolation = Test-E7IsolationEvidence -Path $IsolationEvidencePath

    # 5. Authenticated vendor session pinned to the test book, then identity proof.
    if ($null -ne $SessionFactory) {
        $session = New-XbAutoCountSession -EnableProductionAdapter -SessionFactory $SessionFactory
    }
    else {
        [Environment]::SetEnvironmentVariable("XB_AC2_DATABASE_NAME", $script:E7Book, "Process")
        [Environment]::SetEnvironmentVariable("XB_AC2_SERVER_NAME", $ServerName, "Process")
        [Environment]::SetEnvironmentVariable("XB_AC2_ASSEMBLY_PATH", $AcRoot, "Process")
        $session = New-XbAutoCountSession -EnableProductionAdapter
    }
    $result.book_identity = Assert-E7BookIdentity -Session $session -ExpectedHost $expectedHost
    $result.audit_before = Get-E7AuditSnapshot -DBSetting $session.DBSetting

    # 6. Conservative duplicate check: no earlier E7 synthetic member exists.
    $command = Get-XbAutoCountMemberCommand -Session $session
    $table = $command.LoadBrowseTable()
    if ($null -eq $table -or -not ($table -is [System.Data.DataTable]) -or -not $table.Columns.Contains("Name")) { throw "duplicate_check_unavailable" }
    foreach ($row in $table.Rows) {
        if (([string]$row["Name"]).StartsWith($script:E7NamePrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw "synthetic_member_already_present" }
    }

    if (-not $writeMode) {
        $result.status = "DRY_RUN_PASSED"
        $exitCode = 0
        return
    }

    # 7. Durable one-shot intent BEFORE any numbering call.
    $singapore = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow, "Singapore Standard Time")
    $register = $singapore.Date
    $member = @{
        Name = "{0} {1}" -f $script:E7NamePrefix, $register.ToString("yyyyMMdd", [Globalization.CultureInfo]::InvariantCulture)
        RegisterDate = $register.ToString("yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
        ExpiryDate = $register.AddYears(2).AddDays(-1).ToString("yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
    }
    Write-E7File -Path (Join-Path $state "e7.intent.json") -Value ([ordered]@{ run = $script:E7Run; lock = $script:E7Lock; book = $script:E7Book; created_utc = [DateTimeOffset]::UtcNow.ToString("o"); name = $member.Name; register_date = $member.RegisterDate; expiry_date = $member.ExpiryDate; contact_fields = "null" })

    # 8. Official numbering: at most three candidates, each probed read-only.
    $seen = @()
    for ($i = 0; $i -lt $script:E7MaxCandidates -and $null -eq $result.member_no; $i++) {
        $result.get_next_member_no_count++
        $candidate = Get-XbAutoCountNextMemberNo -Session $session
        if ($seen -ccontains $candidate) { throw "member_no_generator_not_advancing" }
        $seen += $candidate
        if ($null -eq (Get-XbAutoCountMember -Session $session -MemberNo $candidate)) { $result.member_no = $candidate }
    }
    if ($null -eq $result.member_no) { throw "member_no_candidates_exhausted" }
    if ($null -ne (Get-XbAutoCountMember -Session $session -MemberNo $result.member_no)) { throw "bound_member_no_recheck_failed" }
    $member.MemberNo = [string]$result.member_no

    # 9. Fence with deadline, then exclusive claim immediately before SaveMember.
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($FenceTtlSeconds)
    Write-E7File -Path (Join-Path $state "e7.fence.json") -Value ([ordered]@{ member_no = $member.MemberNo; deadline_utc = $deadline.ToString("o") })
    if ($null -ne $SessionFactory -and $TestDelayBeforeClaimMs -gt 0) { Start-Sleep -Milliseconds $TestDelayBeforeClaimMs }
    if ([DateTimeOffset]::UtcNow -gt $deadline) { throw "e7_fence_expired" }
    Write-E7File -Path (Join-Path $state "e7.claim") -Value ([ordered]@{ member_no = $member.MemberNo; pid = $PID; claimed_utc = [DateTimeOffset]::UtcNow.ToString("o") })

    # 10. The single SaveMember (inside the unchanged adapter), never retried.
    try {
        $result.save_member_invocations = 1
        $created = New-XbAutoCountMember -Session $session -Member $member -EnableProductionAdapter -FieldProfile "shopify_m1"
        $check = Compare-XbAutoCountMemberReadBack -Expected $created.Expected -Actual $created.ReadBack -FieldProfile "shopify_m1"
        $result.readback_found = [bool]$check.Found
        $result.readback_match = [bool]$check.Match
        $result.mismatches = @($check.Mismatches)
        $result.status = if (-not $check.Found) { "WRITE_OUTCOME_UNCERTAIN_HOLD" } elseif ($check.Match) { "CREATED_VERIFIED" } else { "CREATED_READBACK_MISMATCH_HOLD" }
    }
    catch {
        $result.status = "WRITE_OUTCOME_UNKNOWN_HOLD"
        $result.code = ConvertTo-E7Code $_.Exception.Message
        try {
            $existing = Get-XbAutoCountMember -Session $session -MemberNo $member.MemberNo
            $result.reconcile = [ordered]@{ found = ($null -ne $existing) }
        }
        catch { $result.reconcile = [ordered]@{ found = $null; code = "reconcile_lookup_failed" } }
    }
    try { $result.audit_after = Get-E7AuditSnapshot -DBSetting $session.DBSetting } catch { $result.audit_after = [ordered]@{ code = "audit_snapshot_failed" } }
    Write-E7File -Path (Join-Path $state "e7.outcome.json") -Value $result
    $exitCode = if ($result.status -eq "CREATED_VERIFIED") { 0 } else { 3 }
}
catch {
    if ($result.status -eq "STARTED") { $result.status = "REFUSED" }
    $result.code = ConvertTo-E7Code $_.Exception.Message
    if ($result.status -eq "REFUSED" -and $writeMode -and $null -ne $lock -and (Test-Path -LiteralPath (Join-Path ([System.IO.Path]::GetFullPath($StateDir)) "e7.intent.json"))) {
        # A refusal after the intent stays recorded so the one-shot cannot re-run.
        try { Write-E7File -Path (Join-Path ([System.IO.Path]::GetFullPath($StateDir)) "e7.outcome.json") -Value $result } catch { }
    }
}
finally {
    if ($null -ne $lock) { $lock.Dispose() }
    $json = $result | ConvertTo-Json -Depth 6 -Compress
    if (-not [string]::IsNullOrWhiteSpace($JsonOut)) { [System.IO.File]::WriteAllText($JsonOut, $json) }
    [Console]::Out.WriteLine($json)
}
exit $exitCode
