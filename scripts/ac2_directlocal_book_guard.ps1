# DirectLocal #155: AutoCount book guard for the real bridge session.
#
# Dot-sourced by ac2_directlocal_member_bridge.ps1. Mirrors the E7 test-book
# guards (scripts/ac2_directlocal_e7_testbook_create.ps1, unchanged) so every
# real bridge child proves, before any member lookup, numbering or write:
#   - the target book is authorised by this source (AED_TESTINGAPI only; the
#     production book is refused until a separately approved change, gate E9);
#   - host DESKTOP-4I042L6 and no XB_AC2_SESSION_FACTORY override;
#   - the live AutoCount assemblies equal the pins the outbound qualification
#     was made against;
#   - the book is pinned for this process (the environment's
#     AC2_PROBE_DATABASE_NAME is never used) and the authenticated
#     DBSetting.DBName, vendor DB_NAME() and vendor MachineName all match;
#   - vendor error telemetry is off;
#   - UserScript, MailQueue and EInvoiceQueue are empty.
# Credentials are never read here: the adapter reads them from the process
# environment, which only the Owner console procedure provides.

Set-StrictMode -Version Latest

$script:XbDlHost = "DESKTOP-4I042L6"
$script:XbDlAuthorisedBooks = @("AED_TESTINGAPI")
$script:XbDlForbiddenBooks = @("AED_XBOUNDARIES")
# Must equal QUALIFIED_ASSEMBLIES in ac2_directlocal_e7_isolation_check.py.
$script:XbDlQualifiedAssemblies = [ordered]@{
    "AutoCount.dll" = "8e86273018c01c715e440c7975f759951f4a0e979fc3edb0c281db6696090f93"
    "AutoCount.Invoicing.dll" = "5c748b71e54f13313968276530fe110412a4d97a3c9b44d8f43100379849967f"
}

function Assert-XbDlAuthorisedBook {
    param([AllowNull()][AllowEmptyString()][string]$Book)
    if ($script:XbDlForbiddenBooks -ccontains $Book) { throw "production_book_not_authorised" }
    if ($script:XbDlAuthorisedBooks -cnotcontains $Book) { throw "target_book_not_authorised" }
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

function Get-XbDlScalar {
    # Read-only vendor query through the authenticated DBSetting; "?" parameters.
    param([Parameter(Mandatory)]$DBSetting, [Parameter(Mandatory)][string]$Sql, [object[]]$Params = @())
    if ($Sql -notmatch '^\s*SELECT\b' -or $Sql -match '(?i)\b(insert|update|delete|merge|drop|alter|create|exec|into)\b') { throw "vendor_query_not_read_only" }
    return $DBSetting.ExecuteScalar($Sql, [object[]]$Params)
}

function Assert-XbDlBookIdentity {
    param([Parameter(Mandatory)]$Session, [Parameter(Mandatory)][string]$Book, [Parameter(Mandatory)][string]$ExpectedHost)
    try {
        $db = $Session.DBSetting
        $declared = [string]$db.DBName
        $vendorDb = [string](Get-XbDlScalar -DBSetting $db -Sql "SELECT DB_NAME()")
        $vendorHost = [string](Get-XbDlScalar -DBSetting $db -Sql "SELECT CAST(SERVERPROPERTY('MachineName') AS nvarchar(128))")
    }
    catch { throw "book_identity_unverifiable" }
    foreach ($name in @($declared, $vendorDb)) {
        if ($script:XbDlForbiddenBooks -ccontains $name) { throw "production_book_refused" }
    }
    if ([string]::IsNullOrWhiteSpace($declared) -or [string]::IsNullOrWhiteSpace($vendorDb)) { throw "book_identity_missing" }
    if ($declared -cne $vendorDb) { throw "book_identity_ambiguous" }
    if ($vendorDb -cne $Book) { throw "book_identity_wrong" }
    if ($vendorHost -ine $ExpectedHost) { throw "book_host_wrong" }
}

function Get-XbDlOutboundCounts {
    param([Parameter(Mandatory)]$DBSetting)
    [ordered]@{
        user_script_rows = [long](Get-XbDlScalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM UserScript")
        mail_queue_rows = [long](Get-XbDlScalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM MailQueue")
        einvoice_queue_rows = [long](Get-XbDlScalar -DBSetting $DBSetting -Sql "SELECT COUNT_BIG(*) FROM EInvoiceQueue")
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
    $json = [string](Get-XbDlScalar -DBSetting $DBSetting -Sql ("SELECT (SELECT Location AS location, SyncName AS sync_name, " +
        "CONVERT(varchar(19), LastFinishSyncTime, 126) AS last_finish_sync_local, CONVERT(varchar(19), LastAliveTime, 126) AS last_alive_local " +
        "FROM SyncRegistration ORDER BY Location, SyncName FOR JSON PATH, INCLUDE_NULL_VALUES)"))
    $outlets = if ([string]::IsNullOrWhiteSpace($json)) { @() } else { @($json | ConvertFrom-Json) }
    return [ordered]@{
        server_now_local = [string](Get-XbDlScalar -DBSetting $DBSetting -Sql "SELECT CONVERT(varchar(19), GETDATE(), 126)")
        member_change_counter = [long](Get-XbDlScalar -DBSetting $DBSetting -Sql "SELECT ISNULL(MAX(Counter), 0) FROM ChangeCount WHERE TableName = 'Member'")
        outlets = $outlets
    }
}

function New-XbDlRealSession {
    # Every guard runs in this order; any failure stops before the next step.
    param([Parameter(Mandatory)][string]$Book, [Parameter(Mandatory)][string]$AcRoot)
    Assert-XbDlAuthorisedBook -Book $Book
    if ($env:COMPUTERNAME -ine $script:XbDlHost) { throw "host_not_authorised" }
    if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("XB_AC2_SESSION_FACTORY", "Process"))) { throw "session_factory_env_refused" }
    Assert-XbDlQualifiedAssemblies -Root $AcRoot
    [Environment]::SetEnvironmentVariable("XB_AC2_DATABASE_NAME", $Book, "Process")
    [Environment]::SetEnvironmentVariable("XB_AC2_ASSEMBLY_PATH", $AcRoot, "Process")
    $session = New-XbAutoCountSession -EnableProductionAdapter
    # Vendor error telemetry (HTTPS to AutoCount HQ) is off unless a host sets it.
    $telemetry = "AutoCount.Application.ErrorLogClientHelper" -as [type]
    if ($null -eq $telemetry -or [bool]$telemetry::SubmitErrorLog) { throw "vendor_error_telemetry_enabled" }
    Assert-XbDlBookIdentity -Session $session -Book $Book -ExpectedHost $script:XbDlHost
    Assert-XbDlOutboundQuiet -Counts (Get-XbDlOutboundCounts -DBSetting $session.DBSetting)
    return $session
}
