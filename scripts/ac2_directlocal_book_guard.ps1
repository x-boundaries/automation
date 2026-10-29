# DirectLocal #155: AutoCount book guard for the real bridge session.
#
# Dot-sourced by ac2_directlocal_member_bridge.ps1. Mirrors the E7 test-book
# guards (scripts/ac2_directlocal_e7_testbook_create.ps1, unchanged) so every
# real bridge child proves, before any member lookup, numbering or write:
#   - the target book is authorised by this source: AED_TESTINGAPI for every
#     operation; AED_XBOUNDARIES (go-live, Owner SCOPE-CONFIRMED 29 Oct 2026)
#     only for the #155 sync's sync-lookup, sync-changelog and write-once
#     with payload profile sync_p1. Names, operations and profiles compare
#     ordinally (a lookalike never passes);
#   - host DESKTOP-4I042L6 and no XB_AC2_SESSION_FACTORY override;
#   - the live AutoCount assemblies equal the pins the outbound qualification
#     was made against;
#   - the book is pinned for this process (the environment's
#     AC2_PROBE_DATABASE_NAME is never used) and the authenticated
#     DBSetting.DBName, vendor DB_NAME() and vendor MachineName all match;
#   - vendor error telemetry is off;
#   - UserScript, MailQueue and EInvoiceQueue are empty.
# Every vendor query goes through Get-XbDlScalar, which sends only the pinned
# read-only SQL texts (G2 H1; the SQL login is always sa): printable ASCII
# only, compared ordinally (G2 LOW-1).
# Credentials are never read here: the adapter reads them from the process
# environment, which only the Owner console procedure provides.

Set-StrictMode -Version Latest

$script:XbDlHost = "DESKTOP-4I042L6"
$script:XbDlAuthorisedBooks = @("AED_TESTINGAPI")
$script:XbDlForbiddenBooks = @("AED_XBOUNDARIES")
# Go-live R3: the production book is not in $XbDlAuthorisedBooks (that list
# serves every caller: Core, owner console, E7). It is admitted only for these
# sync operations, and write-once only with the sync_p1 profile. Q2 = A:
# next-member-no is not admitted.
$script:XbDlLiveBook = "AED_XBOUNDARIES"
$script:XbDlLiveBookOperations = @("sync-lookup", "sync-changelog", "write-once")
$script:XbDlLiveBookWriteProfile = "sync_p1"
# Must equal QUALIFIED_ASSEMBLIES in ac2_directlocal_e7_isolation_check.py.
$script:XbDlQualifiedAssemblies = [ordered]@{
    "AutoCount.dll" = "8e86273018c01c715e440c7975f759951f4a0e979fc3edb0c281db6696090f93"
    "AutoCount.Invoicing.dll" = "5c748b71e54f13313968276530fe110412a4d97a3c9b44d8f43100379849967f"
}

function Test-XbDlOrdinalIn {
    # Ordinal membership (go-live R6): PowerShell's -ccontains and -ceq are
    # culture-aware and ignore characters such as U+00AD.
    param([AllowNull()][AllowEmptyString()][string]$Value, [Parameter(Mandatory)][string[]]$Set)
    foreach ($item in $Set) { if ([string]::Equals($Value, $item, [StringComparison]::Ordinal)) { return $true } }
    return $false
}

function Assert-XbDlAuthorisedBook {
    # Test book: every operation (unchanged). Live book: only the sync's
    # operations; write-once only with the sync_p1 profile. Without
    # -Operation (any other caller) the live book is refused as before.
    # -ProfileNotYetRead is the bridge's pre-stdin check of write-once; the
    # profile is checked again once the payload is read, before any session.
    param(
        [AllowNull()][AllowEmptyString()][string]$Book,
        [AllowNull()][AllowEmptyString()][string]$Operation,
        [AllowNull()][AllowEmptyString()][string]$PayloadProfile,
        [switch]$ProfileNotYetRead
    )
    if (Test-XbDlOrdinalIn -Value $Book -Set $script:XbDlAuthorisedBooks) { return }
    if (-not (Test-XbDlLiveBook -Book $Book)) { throw "target_book_not_authorised" }
    if (-not (Test-XbDlOrdinalIn -Value $Operation -Set $script:XbDlLiveBookOperations)) { throw "production_book_not_authorised" }
    if ([string]::Equals($Operation, "write-once", [StringComparison]::Ordinal) -and -not $ProfileNotYetRead -and
        -not [string]::Equals($PayloadProfile, $script:XbDlLiveBookWriteProfile, [StringComparison]::Ordinal)) {
        throw "production_book_not_authorised"
    }
}

function Test-XbDlLiveBook {
    param([AllowNull()][AllowEmptyString()][string]$Book)
    return [string]::Equals($Book, $script:XbDlLiveBook, [StringComparison]::Ordinal)
}

function Get-XbDlSha256Hex {
    param([Parameter(Mandatory)][byte[]]$Bytes)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString("x2") }) -join "") } finally { $sha.Dispose() }
}

function Assert-XbDlQualifiedAssemblies {
    param([Parameter(Mandatory)][string]$Root, $Pins = $script:XbDlQualifiedAssemblies)
    foreach ($name in $Pins.Keys) {
        $path = Join-Path $Root $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "autocount_build_not_qualified" }
        if ((Get-XbDlSha256Hex -Bytes ([System.IO.File]::ReadAllBytes($path))) -cne [string]$Pins[$name]) { throw "autocount_build_not_qualified" }
    }
}

# G2 H1: every AutoCount API session runs as SQL login sa (hard-coded in
# AutoCount.dll, whichever AutoCount user logs in), so Get-XbDlScalar is the
# only database-side limit. It sends only these exact texts, or the two sync
# read templates below whose only variable parts are validated, and only after
# the deny check on every text. Every pinned text and template is one line of
# printable ASCII (U+0020-U+007E; U+0020 is the only whitespace), so any other
# character is refused first (G2 LOW-1: PowerShell's -ccontains and -ceq are
# culture-aware and ignore characters such as U+00AD); every exact match is
# ordinal. Pinned by tests/test_member_sync_bridge_ps.py.
$script:XbDlSqlText = [ordered]@{
    db_name = "SELECT DB_NAME()"
    machine_name = "SELECT CAST(SERVERPROPERTY('MachineName') AS nvarchar(128))"
    user_script_rows = "SELECT COUNT_BIG(*) FROM UserScript"
    mail_queue_rows = "SELECT COUNT_BIG(*) FROM MailQueue"
    einvoice_queue_rows = "SELECT COUNT_BIG(*) FROM EInvoiceQueue"
    pos_sync_registrations = ("SELECT (SELECT Location AS location, SyncName AS sync_name, " +
        "CONVERT(varchar(19), LastFinishSyncTime, 126) AS last_finish_sync_local, CONVERT(varchar(19), LastAliveTime, 126) AS last_alive_local " +
        "FROM SyncRegistration ORDER BY Location, SyncName FOR JSON PATH, INCLUDE_NULL_VALUES)")
    server_now_local = "SELECT CONVERT(varchar(19), GETDATE(), 126)"
    member_change_counter = "SELECT ISNULL(MAX(Counter), 0) FROM ChangeCount WHERE TableName = 'Member'"
    changelog_max_id = "SELECT ISNULL(MAX(ID), 0) FROM ChangeLog"
    # Go-live R7 (Owner Q7, SCOPE-CONFIRMED 29 Oct 2026): exactly one parameter.
    member_type_exists = "SELECT COUNT_BIG(*) FROM MemberType WHERE MemberType = ?"
}
# Refused in any text: statement separators, comments, every non-read verb,
# xp_/sp_ procedures, the production book, and 3- or 4-part object names
# (two dots between identifiers: a.b.c, [a].[b].[c], a..c, srv.db.dbo.t).
$script:XbDlDeniedSqlPattern = '(?i);|--|/\*|\*/|\b(?:EXEC|EXECUTE|TRUNCATE|INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|GRANT|REVOKE|DENY|INTO|DBCC|SHUTDOWN|KILL|BACKUP|RESTORE|BULK|OPENROWSET|OPENDATASOURCE|OPENQUERY|WAITFOR|DECLARE|SET|USE)\b|\b(?:xp|sp)_|AED_XBOUNDARIES|\.\s*(?:\[[^\]]*\]|"[^"]*"|[A-Za-z_@#][A-Za-z0-9_@#$]*)?\s*\.'
# sync-changelog: only TOP (1-1000) and the regex-checked column vary.
$script:XbDlChangeLogSqlPattern = '^SELECT \(SELECT TOP \(([1-9][0-9]{0,3})\) ID AS id, CAST\(\[[A-Za-z][A-Za-z0-9_]{0,63}\] AS nvarchar\(max\)\) AS xml FROM ChangeLog WHERE TableName = ''Member'' AND ChangeType = ''I'' AND ID > \? ORDER BY ID FOR JSON PATH\)\z'
# sync-lookup: only the condition list varies (phone block, Singapore block,
# then up to 20 MemberNo conditions; at most 25 parameters).
$script:XbDlLookupSqlPrefix = "SELECT (SELECT TOP 50 MemberNo AS member_no, Name AS name, MobilePhone AS mobile_phone, EmailAddress AS email_address, CONVERT(varchar(10), DOB, 23) AS dob, CONVERT(varchar(23), CreatedTime, 126) AS created_time FROM Member WHERE "
$script:XbDlLookupSqlSuffix = " ORDER BY MemberNo FOR JSON PATH, INCLUDE_NULL_VALUES)"
$script:XbDlLookupKey = "UPPER(LTRIM(RTRIM(MemberNo))) = ?"
$script:XbDlLookupMobile = "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(ISNULL(MobilePhone, ''), ' ', ''), '-', ''), '+', ''), '(', ''), ')', ''), '.', '') = ?"
$script:XbDlLookupEmbedded = "CHARINDEX(?, MemberNo) > 0"
$script:XbDlLookupNumber = "UPPER(LTRIM(RTRIM(MemberNo))) = UPPER(LTRIM(RTRIM(?)))"

function Test-XbDlLookupSql {
    param([Parameter(Mandatory)][string]$Sql)
    $prefix = $script:XbDlLookupSqlPrefix
    $suffix = $script:XbDlLookupSqlSuffix
    if ($Sql.Length -le ($prefix.Length + $suffix.Length) -or -not $Sql.StartsWith($prefix, [StringComparison]::Ordinal) -or -not $Sql.EndsWith($suffix, [StringComparison]::Ordinal)) { return $false }
    $parts = @($Sql.Substring($prefix.Length, $Sql.Length - $prefix.Length - $suffix.Length) -csplit " OR ", 0, "SimpleMatch")
    $end = $parts.Count
    $numbers = 0
    while ($end -gt 0 -and [string]::Equals($parts[$end - 1], $script:XbDlLookupNumber, [StringComparison]::Ordinal)) { $end--; $numbers++ }
    $key = $script:XbDlLookupKey; $mobile = $script:XbDlLookupMobile
    $heads = @{ 0 = ""; 2 = "$key|$mobile"; 3 = "$key|$mobile|$($script:XbDlLookupEmbedded)"; 5 = "$key|$mobile|$key|$mobile|$($script:XbDlLookupEmbedded)" }
    $head = if ($end -gt 0) { @($parts[0..($end - 1)]) -join "|" } else { "" }
    return ($numbers -le 20 -and ($end + $numbers) -ge 1 -and $heads.ContainsKey($end) -and [string]::Equals($head, [string]$heads[$end], [StringComparison]::Ordinal))
}

function Assert-XbDlSqlAllowed {
    param([Parameter(Mandatory)][string]$Sql, [object[]]$Params = @())
    if ($Sql -cnotmatch '\A[\x20-\x7E]+\z') { throw "vendor_query_not_ascii" }
    if ($Sql -match $script:XbDlDeniedSqlPattern) { throw "vendor_query_not_read_only" }
    if ([regex]::Matches($Sql, '\?').Count -ne @($Params).Count) { throw "vendor_query_not_allowlisted" }
    foreach ($text in $script:XbDlSqlText.Values) {
        if ([string]::Equals($Sql, [string]$text, [StringComparison]::Ordinal)) { return }
    }
    if ($Sql -cmatch $script:XbDlChangeLogSqlPattern -and [int]$Matches[1] -le 1000) { return }
    if (Test-XbDlLookupSql -Sql $Sql) { return }
    throw "vendor_query_not_allowlisted"
}

function Get-XbDlScalar {
    # Read-only vendor query through the authenticated DBSetting; "?" parameters.
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)][string]$Sql, [object[]]$Params = @())
    Assert-XbDlSqlAllowed -Sql $Sql -Params $Params
    return $DBSetting.ExecuteScalar($Sql, [object[]]$Params)
}

function Assert-XbDlBookIdentity {
    param([Parameter(Mandatory)]$Session, [Parameter(Mandatory)][string]$Book, [Parameter(Mandatory)][string]$ExpectedHost)
    try {
        $db = $Session.DBSetting
        $declared = [string]$db.DBName
        $vendorDb = [string](Get-XbDlScalar -DBSetting $db -Sql $script:XbDlSqlText.db_name)
        $vendorHost = [string](Get-XbDlScalar -DBSetting $db -Sql $script:XbDlSqlText.machine_name)
    }
    catch { throw "book_identity_unverifiable" }
    # Go-live R3: the production name is refused here unless the live book is
    # the book this session was admitted for (Assert-XbDlAuthorisedBook ran
    # first, with the operation); the identity proof below is unchanged.
    if (-not (Test-XbDlLiveBook -Book $Book)) {
        foreach ($name in @($declared, $vendorDb)) {
            if (Test-XbDlOrdinalIn -Value $name -Set $script:XbDlForbiddenBooks) { throw "production_book_refused" }
        }
    }
    if ([string]::IsNullOrWhiteSpace($declared) -or [string]::IsNullOrWhiteSpace($vendorDb)) { throw "book_identity_missing" }
    # Go-live R6: ordinal compares.
    if (-not [string]::Equals($declared, $vendorDb, [StringComparison]::Ordinal)) { throw "book_identity_ambiguous" }
    if (-not [string]::Equals($vendorDb, $Book, [StringComparison]::Ordinal)) { throw "book_identity_wrong" }
    if (-not [string]::Equals($vendorHost, $ExpectedHost, [StringComparison]::OrdinalIgnoreCase)) { throw "book_host_wrong" }
}

function Assert-XbDlMemberTypeExists {
    # Go-live R7: a sync_p1 create needs its member type code to exist in the
    # book (VM fact V1: MemberType.MemberType). Read-only, before the claim.
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)][string]$MemberType)
    $count = [long](Get-XbDlScalar -DBSetting $DBSetting -Sql $script:XbDlSqlText.member_type_exists -Params @($MemberType))
    if ($count -lt 1) { throw "member_type_unknown" }
}

function Get-XbDlOutboundCounts {
    param([Parameter(Mandatory)]$DBSetting)
    [ordered]@{
        user_script_rows = [long](Get-XbDlScalar -DBSetting $DBSetting -Sql $script:XbDlSqlText.user_script_rows)
        mail_queue_rows = [long](Get-XbDlScalar -DBSetting $DBSetting -Sql $script:XbDlSqlText.mail_queue_rows)
        einvoice_queue_rows = [long](Get-XbDlScalar -DBSetting $DBSetting -Sql $script:XbDlSqlText.einvoice_queue_rows)
    }
}

function Assert-XbDlOutboundQuiet {
    # MemberEntity loads user scripts; AutoCountServerService dispatches
    # MailQueue; Accounting.exe dispatches EInvoiceQueue. All must be empty.
    param([Parameter(Mandatory)]$Counts)
    if ($Counts.user_script_rows -ne 0) { throw "user_script_present" }
    if ($Counts.mail_queue_rows -ne 0 -or $Counts.einvoice_queue_rows -ne 0) { throw "outbound_queue_not_empty" }
}

function Test-XbDlOutboundChanged {
    # After SaveMember: any new queue row (or an unreadable queue) holds the
    # created member for review. Never throws.
    param([Parameter(Mandatory)]$DBSetting)
    try {
        $after = Get-XbDlOutboundCounts -DBSetting $DBSetting
        return ($after.user_script_rows -ne 0 -or $after.mail_queue_rows -ne 0 -or $after.einvoice_queue_rows -ne 0)
    }
    catch { return $true }
}

function Get-XbDlPosSyncState {
    # HQ-side POS sync liveness (AutoCount POS 5.2 SyncRegistration) and the
    # Member change counter the incremental sync reads. No member data. Uses
    # only the qualified DBSetting.ExecuteScalar path (one FOR JSON value).
    param([Parameter(Mandatory)]$DBSetting)
    $json = [string](Get-XbDlScalar -DBSetting $DBSetting -Sql $script:XbDlSqlText.pos_sync_registrations)
    $outlets = if ([string]::IsNullOrWhiteSpace($json)) { @() } else { @($json | ConvertFrom-Json) }
    return [ordered]@{
        server_now_local = [string](Get-XbDlScalar -DBSetting $DBSetting -Sql $script:XbDlSqlText.server_now_local)
        member_change_counter = [long](Get-XbDlScalar -DBSetting $DBSetting -Sql $script:XbDlSqlText.member_change_counter)
        outlets = $outlets
    }
}

function Get-XbDlSessionFailureStage {
    # Called only after the adapter's reviewed login failed. Read-only:
    # loaded-type checks and one unauthenticated DBSetting with SELECT DB_NAME()
    # on the pinned book. Returns a fixed code; never vendor text.
    #   autocount_assembly_load_failed  vendor types never loaded
    #   autocount_sql_connection_failed DBSetting to the pinned book failed
    #   autocount_sql_book_mismatch     connected, but not to the pinned book
    #   autocount_login_rejected        connection fine, so the login itself failed
    param([Parameter(Mandatory)][string]$Book)
    $dbSettingType = "AutoCount.Data.DBSetting" -as [type]
    $userSessionType = "AutoCount.Authentication.UserSession" -as [type]
    if ($null -eq $dbSettingType -or $null -eq $userSessionType) { return "autocount_assembly_load_failed" }
    $server = Get-XbAutoCountRuntimeValue -Names @("XB_AC2_SERVER_NAME", "AC2_PROBE_SERVER_NAME")
    try {
        $factory = Find-XbAutoCountPublicStaticMethod -Type $dbSettingType -Name "CreateAutoCountDefaultDBSetting" -ParameterCount 2
        $db = $factory.Invoke($null, @($server, $Book))
        $name = [string](Get-XbDlScalar -DBSetting $db -Sql $script:XbDlSqlText.db_name)
    }
    catch { return "autocount_sql_connection_failed" }
    if (-not [string]::Equals($name, $Book, [StringComparison]::Ordinal)) { return "autocount_sql_book_mismatch" }
    return "autocount_login_rejected"
}

function Install-XbDlResolveGuard {
    # The unchanged adapter registers a PowerShell scriptblock as an
    # AppDomain.AssemblyResolve handler and keeps it after a successful login.
    # PowerShell's resources are neutral "en", so under any other UI culture
    # (the host's en-US included) its first lookup of a resource set probes a
    # satellite that does not exist; the failed bind runs the scriptblock, whose
    # own resource lookup probes again: unbounded recursion and an uncatchable
    # StackOverflowException in System.Management.Automation (E5, 16 Oct 2026).
    # This compiled handler is registered first, so it runs before the
    # adapter's on every resolve: it re-pins the neutral UI culture (PowerShell
    # resets it per pipeline; a vendor may change it) and returns null, so it
    # never changes what resolves. Idempotent per process.
    if ($null -eq ("XbDirectLocal.ResolveGuard" -as [type])) {
        try {
            Add-Type -TypeDefinition @'
using System;
using System.Globalization;
using System.Reflection;
using System.Threading;
namespace XbDirectLocal {
    public static class ResolveGuard {
        static CultureInfo pinned;
        public static bool Installed { get { return pinned != null; } }
        public static void Install(CultureInfo culture) {
            if (pinned != null) return;
            pinned = culture;
            CultureInfo.DefaultThreadCurrentUICulture = culture;
            Thread.CurrentThread.CurrentUICulture = culture;
            AppDomain.CurrentDomain.AssemblyResolve += Repin;
        }
        static Assembly Repin(object sender, ResolveEventArgs args) {
            if (!Thread.CurrentThread.CurrentUICulture.Equals(pinned)) Thread.CurrentThread.CurrentUICulture = pinned;
            return null;
        }
    }
}
'@
        }
        catch { throw "resolve_guard_unavailable" }
    }
    $neutral = [System.Management.Automation.PSObject].Assembly.GetCustomAttributes([System.Resources.NeutralResourcesLanguageAttribute], $false) | Select-Object -First 1
    if ($null -eq $neutral -or [string]::IsNullOrWhiteSpace($neutral.CultureName)) { throw "resolve_guard_unavailable" }
    [XbDirectLocal.ResolveGuard]::Install([System.Globalization.CultureInfo]::GetCultureInfo($neutral.CultureName))
}

function New-XbDlRealSession {
    # Every guard runs in this order; any failure stops before the next step.
    # Operation and PayloadProfile (go-live R3/R4) admit the live book for the sync
    # only; a caller without them (Core, owner console, E7) gets the test book.
    param(
        [Parameter(Mandatory)][string]$Book,
        [Parameter(Mandatory)][string]$AcRoot,
        [AllowNull()][AllowEmptyString()][string]$Operation,
        [AllowNull()][AllowEmptyString()][string]$PayloadProfile
    )
    Assert-XbDlAuthorisedBook -Book $Book -Operation $Operation -PayloadProfile $PayloadProfile
    if ($env:COMPUTERNAME -ine $script:XbDlHost) { throw "host_not_authorised" }
    if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("XB_AC2_SESSION_FACTORY", "Process"))) { throw "session_factory_env_refused" }
    Assert-XbDlQualifiedAssemblies -Root $AcRoot
    [Environment]::SetEnvironmentVariable("XB_AC2_DATABASE_NAME", $Book, "Process")
    [Environment]::SetEnvironmentVariable("XB_AC2_ASSEMBLY_PATH", $AcRoot, "Process")
    # Must precede the adapter, whose resolve handler it has to run before.
    Install-XbDlResolveGuard
    try { $session = New-XbAutoCountSession -EnableProductionAdapter }
    catch {
        $code = [string]$_.Exception.Message
        # The unchanged adapter folds every login-stage error into one code;
        # classify the stage read-only (no vendor text leaves the process).
        if ($code -ceq "autocount_session_initialization_failed") { $code = Get-XbDlSessionFailureStage -Book $Book }
        throw $code
    }
    # Vendor error telemetry (HTTPS to AutoCount HQ) is off unless a host sets it.
    $telemetry = "AutoCount.Application.ErrorLogClientHelper" -as [type]
    if ($null -eq $telemetry -or [bool]$telemetry::SubmitErrorLog) { throw "vendor_error_telemetry_enabled" }
    Assert-XbDlBookIdentity -Session $session -Book $Book -ExpectedHost $script:XbDlHost
    Assert-XbDlOutboundQuiet -Counts (Get-XbDlOutboundCounts -DBSetting $session.DBSetting)
    return $session
}
