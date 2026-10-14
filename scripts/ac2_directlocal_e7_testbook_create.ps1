# AC2 #155 E7: ONE synthetic Member create on the AED_TESTINGAPI test book only.
#
# Isolated from the DirectLocal core: the core's synthetic hard-disarm and its
# bridge are untouched and never enabled here. This script reuses the unchanged
# adapter (reviewed login, GetNextMemberNo, the single SaveMember in
# New-XbAutoCountMember with the shopify_m1 profile and its one GetMember
# readback).
#
# Guards, in order (any failure stops before the next step):
#   host DESKTOP-4I042L6; owned workspace + marker bound to RUN/LOCK/book;
#   workspace, state and evidence directories private (no inheritance; only
#   the current user, SYSTEM and Administrators hold any access); no
#   XB_AC2_SESSION_FACTORY; exclusive lock; one-shot (no prior records);
#   isolation evidence re-validated field by field (custody: file inside the
#   private evidence dir and SHA-256 equal to the collector's printed hash;
#   schema, host, book, freshness, every inspection complete, every negative
#   finding recomputed, assembly pins); live AutoCount assemblies equal the
#   pins the outbound qualification was made against; authenticated vendor
#   session whose DBSetting.DBName, vendor DB_NAME() and vendor MachineName all
#   match (production book refused); vendor error telemetry off; no user
#   scripts and empty mail/e-invoice queues; no prior E7 synthetic member
#   (COUNT only, no member details loaded).
# Write mode additionally needs three switches, then: durable intent ->
#   <= 3 GetNextMemberNo candidates (each probed by COUNT) -> re-probe ->
#   queue re-check -> fence with deadline -> exclusive claim -> SaveMember at
#   most once -> one GetMember readback -> null-aware exact comparison ->
#   outbound-queue re-check -> outcome file. Records are hash-chained.
# Any exception after the claim is WRITE_OUTCOME_UNKNOWN_HOLD: never retried;
# the same MemberNo is only counted. Contact fields are deliberately NULL.
# The real MemberNo stays in the private state records; stdout and -JsonOut
# carry only member_ref (a SHA-256 prefix).
# The database is pinned to AED_TESTINGAPI for this process; the AC2_PROBE_*
# database name in the environment is never used.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$StateDir,
    [Parameter(Mandatory)][string]$IsolationEvidencePath,
    [Parameter(Mandatory)][string]$IsolationEvidenceSha256,
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$ServerName = $env:AC2_PROBE_SERVER_NAME,
    [switch]$EnableTestBookWrite,
    [switch]$ConfirmTargetAedTestingApi,
    [switch]$ConfirmOwnerRiskAccepted,
    [ValidateRange(1, 600)][int]$FenceTtlSeconds = 120,
    [ValidateRange(1, 120)][int]$IsolationMaxAgeMinutes = 30,
    [string]$JsonOut,
    # Test-only seams. A -File invocation cannot pass a scriptblock or a
    # hashtable, so the production path always performs the reviewed vendor
    # login against the pinned assemblies.
    [scriptblock]$SessionFactory,
    [string]$TestExpectedHost,
    [hashtable]$TestAssemblyPins,
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
$script:E7EvidenceSchema = "xb155-e7-isolation-v2"
$script:E7MinSamples = 6
# Must equal QUALIFIED_ASSEMBLIES in ac2_directlocal_e7_isolation_check.py.
$script:E7QualifiedAssemblies = [ordered]@{
    "AutoCount.dll" = "8e86273018c01c715e440c7975f759951f4a0e979fc3edb0c281db6696090f93"
    "AutoCount.Invoicing.dll" = "5c748b71e54f13313968276530fe110412a4d97a3c9b44d8f43100379849967f"
}
$script:E7Inspections = @("host", "processes", "sessions", "sql_capabilities", "book_channels", "scheduled_tasks", "services", "assemblies")
$script:E7ZeroCounts = @("sql_agent_jobs_for_book", "book_user_assemblies", "book_database_triggers", "scheduled_tasks_mentioning_book",
    "pos_registrations_active_after_clone", "plugins_allowing_sync", "mail_queue_rows", "einvoice_queue_rows", "user_script_rows",
    "do_email_smtp_configured", "onerewards_members")
$script:E7InstanceFlags = @("linked_servers", "xp_cmdshell", "server_triggers", "book_broker_enabled", "database_mail_xps")
$script:E7AllowedProcesses = @("Accounting.exe", "AutoCountServerService.exe")
$script:E7AcServerTables = @("sys", "mailqueue")
$script:E7AllowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, "S-1-5-18", "S-1-5-32-544")

function ConvertTo-E7Code {
    # Only bounded codes leave the process; vendor messages never do.
    param([AllowNull()][string]$Message)
    if ($Message -cmatch '^[a-z0-9_]{1,64}$') { return $Message }
    return "e7_failed"
}

function Get-E7Sha256Hex {
    param([Parameter(Mandatory)][byte[]]$Bytes)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString("x2") }) -join "") } finally { $sha.Dispose() }
}

function Write-E7File {
    # Exclusive create; returns the SHA-256 of the bytes written for chaining.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Value)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 6 -Compress))
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    return (Get-E7Sha256Hex -Bytes $bytes)
}

function Get-E7MemberRef {
    param([Parameter(Mandatory)][string]$MemberNo)
    return "e7m-" + (Get-E7Sha256Hex -Bytes ([System.Text.Encoding]::UTF8.GetBytes($MemberNo))).Substring(0, 12)
}

function Assert-E7Private {
    # Only the current user, SYSTEM and Administrators may own or hold any
    # access (inherited or explicit). The workspace root must not inherit
    # (C:\ grants Authenticated Users Modify to new subfolders); its children
    # may inherit from it because every effective entry is checked here.
    # Read through .NET, not Get-Acl: a pwsh parent's PSModulePath can shadow
    # Microsoft.PowerShell.Security so Get-Acl cannot load in Windows PowerShell.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Code, [switch]$AllowInherited)
    $acl = if ([System.IO.Directory]::Exists($Path)) { [System.IO.Directory]::GetAccessControl($Path) } else { [System.IO.File]::GetAccessControl($Path) }
    if (-not $AllowInherited -and -not $acl.AreAccessRulesProtected) { throw "${Code}_acl_inherited" }
    if ($script:E7AllowedSids -notcontains $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value) { throw "${Code}_owner_unsafe" }
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($script:E7AllowedSids -notcontains $rule.IdentityReference.Value) { throw "${Code}_acl_unsafe" }
    }
}

function Get-E7Field {
    param([Parameter(Mandatory)]$Object, [Parameter(Mandatory)][string]$Name)
    if ($null -eq $Object -or $null -eq $Object.PSObject.Properties[$Name]) { throw "isolation_evidence_field_missing" }
    return $Object.$Name
}

function Get-E7Array {
    # Arrays must be present and be arrays; a null never iterates as "none found".
    param([Parameter(Mandatory)]$Object, [Parameter(Mandatory)][string]$Name)
    if ($null -eq $Object -or $null -eq $Object.PSObject.Properties[$Name]) { throw "isolation_evidence_field_missing" }
    $value = $Object.$Name
    if ($value -isnot [array]) { throw "isolation_evidence_field_missing" }
    Write-Output -NoEnumerate $value
}

function New-E7PrivateOutcome {
    # The private state record carries the real MemberNo; public output never does.
    param([AllowNull()][string]$MemberNo, [Parameter(Mandatory)]$Result)
    $outcome = [ordered]@{ member_no = $MemberNo }
    foreach ($key in $Result.Keys) { $outcome[$key] = $Result[$key] }
    return $outcome
}

function Get-E7Utc {
    param($Raw)
    if ($Raw -is [datetime]) { return [DateTimeOffset]::new(([datetime]$Raw).ToUniversalTime()) }
    $parsed = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse([string]$Raw, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$parsed)) { throw "isolation_evidence_time_invalid" }
    return $parsed
}

function Test-E7IsolationEvidence {
    # Independent re-validation; the collector's verdict alone is never trusted.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Sha256, [Parameter(Mandatory)][string]$EvidenceDir,
        [Parameter(Mandatory)][string]$ExpectedHost, [Parameter(Mandatory)]$Pins)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "isolation_evidence_missing" }
    $full = [System.IO.Path]::GetFullPath($Path)
    if ((Split-Path -Parent $full) -ine $EvidenceDir) { throw "isolation_evidence_outside_custody" }
    Assert-E7Private -Path $full -Code "evidence_file" -AllowInherited
    if ($Sha256 -notmatch '^[0-9a-fA-F]{64}$') { throw "isolation_evidence_hash_required" }
    $bytes = [System.IO.File]::ReadAllBytes($full)
    if ((Get-E7Sha256Hex -Bytes $bytes) -ine $Sha256) { throw "isolation_evidence_hash_mismatch" }
    try { $e = [System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json } catch { throw "isolation_evidence_unparseable" }
    if ($null -eq $e) { throw "isolation_evidence_unparseable" }

    if ([string](Get-E7Field $e "schema") -cne $script:E7EvidenceSchema -or [string](Get-E7Field $e "book") -cne $script:E7Book -or [string](Get-E7Field $e "host") -ine $ExpectedHost) { throw "isolation_evidence_identity_wrong" }
    $now = [DateTimeOffset]::UtcNow
    foreach ($stamp in @((Get-E7Utc (Get-E7Field $e "run_at")), (Get-E7Utc (Get-E7Field $e "completed_at")))) {
        $age = ($now - $stamp).TotalMinutes
        if ($age -lt -2 -or $age -gt $IsolationMaxAgeMinutes) { throw "isolation_evidence_stale" }
    }
    $collection = Get-E7Field $e "collection"
    foreach ($name in $script:E7Inspections) {
        $entry = $collection.PSObject.Properties[$name]
        $ok = if ($null -ne $entry -and $null -ne $entry.Value -and $null -ne $entry.Value.PSObject.Properties["ok"]) { $entry.Value.ok } else { $null }
        if ($ok -isnot [bool] -or -not $ok) { throw "isolation_collection_incomplete" }
    }
    if ([int](Get-E7Field $e "samples") -lt $script:E7MinSamples -or [int](Get-E7Field $e "sample_failures") -ne 0) { throw "isolation_collection_incomplete" }

    # Negative findings, recomputed here.
    foreach ($item in (Get-E7Array $e "touching_processes")) {
        if ($script:E7AllowedProcesses -notcontains [string](Get-E7Field $item "process") -or [string](Get-E7Field $item "client") -cne "<local machine>") { throw "isolation_not_proven" }
    }
    foreach ($table in (Get-E7Array $e "acserver_book_tables")) { if ($script:E7AcServerTables -notcontains [string]$table) { throw "isolation_not_proven" } }
    foreach ($trigger in (Get-E7Array $e "member_triggers")) {
        $writes = Get-E7Array $trigger "writes_tables"
        if ((Get-E7Array $trigger "external_markers").Count -ne 0 -or $writes.Count -ne 1 -or @("ChangeLog", "ChangeCount") -cnotcontains [string]$writes[0]) { throw "isolation_not_proven" }
    }
    $instance = Get-E7Field $e "instance"
    foreach ($flag in $script:E7InstanceFlags) { if ($null -eq (Get-E7Field $instance $flag) -or [long]$instance.$flag -ne 0) { throw "isolation_not_proven" } }
    foreach ($count in $script:E7ZeroCounts) { if ($null -eq (Get-E7Field $e $count) -or [long]$e.$count -ne 0) { throw "isolation_not_proven" } }
    if ((Get-E7Array $e "services_mentioning_book").Count -ne 0) { throw "isolation_not_proven" }
    $assemblies = Get-E7Field $e "assemblies"
    foreach ($name in $Pins.Keys) { if ([string](Get-E7Field $assemblies $name) -cne [string]$Pins[$name]) { throw "isolation_build_not_qualified" } }
    if (@($assemblies.PSObject.Properties).Count -ne $Pins.Count) { throw "isolation_build_not_qualified" }
    if ([string](Get-E7Field $e "verdict") -cne "ISOLATED" -or (Get-E7Array $e "blockers").Count -ne 0) { throw "isolation_not_proven" }
    return [ordered]@{ verdict = "ISOLATED"; revalidated = $true; evidence_sha256 = $Sha256.ToLowerInvariant(); evidence_age_minutes = [Math]::Round(($now - (Get-E7Utc $e.run_at)).TotalMinutes, 1); touching_processes = @($e.touching_processes | ForEach-Object { [string]$_.process }) }
}

function Assert-E7QualifiedAssemblies {
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)]$Pins)
    foreach ($name in $Pins.Keys) {
        $path = Join-Path $Root $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "autocount_build_not_qualified" }
        if ((Get-E7Sha256Hex -Bytes ([System.IO.File]::ReadAllBytes($path))) -cne [string]$Pins[$name]) { throw "autocount_build_not_qualified" }
    }
}

function Get-E7Scalar {
    # Read-only vendor query through the authenticated DBSetting; "?" parameters.
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)][string]$Sql, [object[]]$Params = @())
    if ($Sql -notmatch '^\s*SELECT\b' -or $Sql -match '(?i)\b(insert|update|delete|merge|drop|alter|create|exec|into)\b') { throw "e7_vendor_query_not_read_only" }
    return $DBSetting.ExecuteScalar($Sql, [object[]]$Params)
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
        event_log_rows = [long](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM EventLog")
        activity_rows = [long](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM Activity")
        mail_queue_rows = [long](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM MailQueue")
        einvoice_queue_rows = [long](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM EInvoiceQueue")
        user_script_rows = [long](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM UserScript")
        member_rows = [long](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM Member")
        pos_last_alive = [string](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT CONVERT(nvarchar(30), MAX(LastAliveTime), 126) FROM SyncRegistration")
    }
}

function Assert-E7OutboundQuiet {
    # MemberEntity loads user scripts; AutoCountServerService dispatches MailQueue;
    # Accounting.exe dispatches EInvoiceQueue. All must be empty.
    param([Parameter(Mandatory)]$Snapshot)
    if ($Snapshot.user_script_rows -ne 0) { throw "user_script_present" }
    if ($Snapshot.mail_queue_rows -ne 0 -or $Snapshot.einvoice_queue_rows -ne 0) { throw "outbound_queue_not_empty" }
}

function Test-E7MemberNoUsed {
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)][string]$MemberNo)
    return ([long](Get-E7Scalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM Member WHERE MemberNo = ?" -Params @($MemberNo)) -ne 0)
}

$writeMode = [bool]($EnableTestBookWrite -and $ConfirmTargetAedTestingApi -and $ConfirmOwnerRiskAccepted)
$result = [ordered]@{
    run = $script:E7Run; lock = $script:E7Lock; mode = $(if ($writeMode) { "write" } else { "dry_run" })
    status = "STARTED"; code = $null; book_identity = $null; isolation = $null; private_paths = $null; vendor_error_telemetry = $null
    get_next_member_no_count = 0; save_member_invocations = 0; member_ref = $null
    readback_found = $null; readback_match = $null; mismatches = @(); audit_before = $null; audit_after = $null; reconcile = $null; records = $null
}
$memberNo = $null
$lock = $null
$exitCode = 1
try {
    # 1. Host and test seams.
    if ($null -ne $SessionFactory) {
        if ([string]::IsNullOrWhiteSpace($TestExpectedHost)) { throw "test_host_required" }
        $expectedHost = $TestExpectedHost
        $pins = if ($null -ne $TestAssemblyPins) { $TestAssemblyPins } else { $script:E7QualifiedAssemblies }
    }
    else {
        if ($TestDelayBeforeClaimMs -ne 0 -or -not [string]::IsNullOrWhiteSpace($TestExpectedHost) -or $null -ne $TestAssemblyPins) { throw "test_seam_without_test_session" }
        $expectedHost = $script:E7Host
        $pins = $script:E7QualifiedAssemblies
    }
    if ($env:COMPUTERNAME -ine $expectedHost) { throw "host_not_authorised" }
    if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("XB_AC2_SESSION_FACTORY", "Process"))) { throw "session_factory_env_refused" }

    # 2. Owned, private workspace bound to this RUN/LOCK/book.
    $state = [System.IO.Path]::GetFullPath($StateDir)
    $workspace = Split-Path -Parent $state
    $evidenceDir = Join-Path $workspace "evidence"
    $markerPath = Join-Path $workspace "XB_E7_UAT_OWNER.json"
    if (-not (Test-Path -LiteralPath $state -PathType Container) -or -not (Test-Path -LiteralPath $markerPath -PathType Leaf) -or -not (Test-Path -LiteralPath $evidenceDir -PathType Container)) { throw "state_dir_not_owned" }
    $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$marker.run -cne $script:E7Run -or [string]$marker.lock -cne $script:E7Lock -or [string]$marker.target_book -cne $script:E7Book) { throw "state_dir_binding_mismatch" }
    Assert-E7Private -Path $workspace -Code "workspace"
    Assert-E7Private -Path $state -Code "state" -AllowInherited
    Assert-E7Private -Path $evidenceDir -Code "evidence" -AllowInherited
    $result.private_paths = "verified"

    # 3. Exclusive lock for the whole run; one-shot replay refusal.
    try { $lock = [System.IO.File]::Open((Join-Path $state "e7.lock"), [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None) }
    catch { throw "e7_lock_held" }
    foreach ($name in @("e7.intent.json", "e7.fence.json", "e7.claim", "e7.outcome.json")) {
        if (Test-Path -LiteralPath (Join-Path $state $name)) { throw "e7_already_executed" }
    }

    # 4. Independently re-validated outbound-isolation evidence for this book,
    #    and the live assemblies the qualification was made against.
    $result.isolation = Test-E7IsolationEvidence -Path $IsolationEvidencePath -Sha256 $IsolationEvidenceSha256 -EvidenceDir $evidenceDir -ExpectedHost $expectedHost -Pins $pins
    Assert-E7QualifiedAssemblies -Root $AcRoot -Pins $pins

    # 5. Authenticated vendor session pinned to the test book, then identity proof.
    if ($null -ne $SessionFactory) {
        $session = New-XbAutoCountSession -EnableProductionAdapter -SessionFactory $SessionFactory
        $result.vendor_error_telemetry = "test_seam"
    }
    else {
        [Environment]::SetEnvironmentVariable("XB_AC2_DATABASE_NAME", $script:E7Book, "Process")
        [Environment]::SetEnvironmentVariable("XB_AC2_SERVER_NAME", $ServerName, "Process")
        [Environment]::SetEnvironmentVariable("XB_AC2_ASSEMBLY_PATH", $AcRoot, "Process")
        $session = New-XbAutoCountSession -EnableProductionAdapter
        # Vendor error telemetry (HTTPS to AutoCount HQ) is off unless a host sets it.
        $telemetry = "AutoCount.Application.ErrorLogClientHelper" -as [type]
        if ($null -eq $telemetry -or [bool]$telemetry::SubmitErrorLog) { throw "vendor_error_telemetry_enabled" }
        $result.vendor_error_telemetry = "off"
    }
    $result.book_identity = Assert-E7BookIdentity -Session $session -ExpectedHost $expectedHost
    $result.audit_before = Get-E7AuditSnapshot -DBSetting $session.DBSetting
    Assert-E7OutboundQuiet -Snapshot $result.audit_before

    # 6. Duplicate check by COUNT only: no member details are loaded.
    $prior = [long](Get-E7Scalar -DBSetting $session.DBSetting -Sql "SELECT COUNT_BIG(*) FROM Member WHERE Name LIKE ?" -Params @($script:E7NamePrefix + "%"))
    if ($prior -ne 0) { throw "synthetic_member_already_present" }
    # The parameterised MemberNo probe used after numbering must work before any intent.
    if (Test-E7MemberNoUsed -DBSetting $session.DBSetting -MemberNo "~E7~PROBE~") { throw "member_probe_unreliable" }

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
    $records = [ordered]@{}
    $records.intent = Write-E7File -Path (Join-Path $state "e7.intent.json") -Value ([ordered]@{ run = $script:E7Run; lock = $script:E7Lock; book = $script:E7Book; created_utc = [DateTimeOffset]::UtcNow.ToString("o"); name = $member.Name; register_date = $member.RegisterDate; expiry_date = $member.ExpiryDate; contact_fields = "null"; isolation_sha256 = $result.isolation.evidence_sha256 })
    $result.records = $records

    # 8. Official numbering: at most three candidates, each probed by COUNT.
    $seen = @()
    for ($i = 0; $i -lt $script:E7MaxCandidates -and $null -eq $memberNo; $i++) {
        $result.get_next_member_no_count++
        $candidate = Get-XbAutoCountNextMemberNo -Session $session
        if ($seen -ccontains $candidate) { throw "member_no_generator_not_advancing" }
        $seen += $candidate
        if (-not (Test-E7MemberNoUsed -DBSetting $session.DBSetting -MemberNo $candidate)) { $memberNo = [string]$candidate }
    }
    if ($null -eq $memberNo) { throw "member_no_candidates_exhausted" }
    if (Test-E7MemberNoUsed -DBSetting $session.DBSetting -MemberNo $memberNo) { throw "bound_member_no_recheck_failed" }
    $member.MemberNo = $memberNo
    $result.member_ref = Get-E7MemberRef -MemberNo $memberNo
    Assert-E7OutboundQuiet -Snapshot (Get-E7AuditSnapshot -DBSetting $session.DBSetting)

    # 9. Fence with deadline, then exclusive claim immediately before SaveMember.
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($FenceTtlSeconds)
    $records.fence = Write-E7File -Path (Join-Path $state "e7.fence.json") -Value ([ordered]@{ member_no = $memberNo; deadline_utc = $deadline.ToString("o"); intent_sha256 = $records.intent })
    if ($null -ne $SessionFactory -and $TestDelayBeforeClaimMs -gt 0) { Start-Sleep -Milliseconds $TestDelayBeforeClaimMs }
    if ([DateTimeOffset]::UtcNow -gt $deadline) { throw "e7_fence_expired" }
    $records.claim = Write-E7File -Path (Join-Path $state "e7.claim") -Value ([ordered]@{ member_no = $memberNo; pid = $PID; claimed_utc = [DateTimeOffset]::UtcNow.ToString("o"); fence_sha256 = $records.fence })

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
        try { $result.reconcile = [ordered]@{ found = (Test-E7MemberNoUsed -DBSetting $session.DBSetting -MemberNo $memberNo) } }
        catch { $result.reconcile = [ordered]@{ found = $null; code = "reconcile_lookup_failed" } }
    }
    try {
        $result.audit_after = Get-E7AuditSnapshot -DBSetting $session.DBSetting
        if ($result.status -eq "CREATED_VERIFIED" -and ($result.audit_after.mail_queue_rows -ne 0 -or $result.audit_after.einvoice_queue_rows -ne 0)) { $result.status = "CREATED_OUTBOUND_QUEUE_CHANGED_HOLD" }
    }
    catch {
        $result.audit_after = [ordered]@{ code = "audit_snapshot_failed" }
        if ($result.status -eq "CREATED_VERIFIED") { $result.status = "CREATED_AUDIT_UNVERIFIED_HOLD" }
    }
    $records.outcome = Write-E7File -Path (Join-Path $state "e7.outcome.json") -Value (New-E7PrivateOutcome -MemberNo $memberNo -Result $result)
    $exitCode = if ($result.status -eq "CREATED_VERIFIED") { 0 } else { 3 }
}
catch {
    if ($result.status -eq "STARTED") { $result.status = "REFUSED" }
    $result.code = ConvertTo-E7Code $_.Exception.Message
    if ($result.status -eq "REFUSED" -and $writeMode -and $null -ne $lock -and (Test-Path -LiteralPath (Join-Path ([System.IO.Path]::GetFullPath($StateDir)) "e7.intent.json"))) {
        # A refusal after the intent stays recorded so the one-shot cannot re-run.
        try { [void](Write-E7File -Path (Join-Path ([System.IO.Path]::GetFullPath($StateDir)) "e7.outcome.json") -Value (New-E7PrivateOutcome -MemberNo $memberNo -Result $result)) } catch { }
    }
}
finally {
    if ($null -ne $lock) { $lock.Dispose() }
    $json = $result | ConvertTo-Json -Depth 6 -Compress
    if (-not [string]::IsNullOrWhiteSpace($JsonOut)) { [System.IO.File]::WriteAllText($JsonOut, $json) }
    [Console]::Out.WriteLine($json)
}
exit $exitCode
