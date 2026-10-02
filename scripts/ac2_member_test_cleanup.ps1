# Test-book-only cleanup and absence check for member-write v2 live tests
# (W-G2-149 section 5, live step L4, T-17). NEVER part of the installable
# release package: the installer refuses any package containing this file.
#
# This is the ONLY repository script that calls DeleteMember. It deletes a row
# only when ALL of these hold:
#   - -Book test was given explicitly (production is not a valid value);
#   - the configured database is on the test-book allowlist and is not the
#     production book, and the authenticated session is bound to it;
#   - the session user is the configured integration user (IU);
#   - the row is fully synthetic: Name starts "ZZTEST " (exact) AND
#     EmailAddress ends "@example.invalid" (case-insensitive);
#   - the row's CreatedUserID is the IU;
#   - -ConfirmDelete was given.
# -VerifyAbsent is the required pre-test absence check: it reads only and
# reports how many fully synthetic rows exist in the test book.
#
# Output: one JSON line with counts only (no names, emails or phone numbers).

[CmdletBinding()]
param(
    [ValidateSet("test")][string]$Book,
    [switch]$VerifyAbsent,
    [switch]$ConfirmDelete,
    [switch]$EnableProductionAdapter,
    [switch]$LibraryOnly
)

Set-StrictMode -Version Latest
$script:XbCleanupBook = $Book
$script:XbCleanupVerifyAbsent = [bool]$VerifyAbsent
$script:XbCleanupConfirmDelete = [bool]$ConfirmDelete
$script:XbCleanupProductionAdapter = [bool]$EnableProductionAdapter
. (Join-Path $PSScriptRoot "ac2_member_create_primitive.ps1") -LibraryOnly

function Test-XbCleanupSyntheticRow {
    param([Parameter(Mandatory)]$Row)
    return ((Test-XbAc2SyntheticName ([string]$Row.Name)) -and (Test-XbAc2SyntheticEmail ([string]$Row.EmailAddress)))
}

function Invoke-XbAc2MemberTestCleanup {
    param(
        [AllowNull()][AllowEmptyString()][string]$Book,
        [switch]$VerifyAbsent,
        [switch]$ConfirmDelete,
        [Parameter(Mandatory)]$Config,
        [switch]$EnableProductionAdapter,
        [scriptblock]$SessionFactory,
        [scriptblock]$MemberCommandFactory,
        [string]$MutexName = $script:XbAc2MemberCreateMutexName,
        [ValidateRange(0, 30000)][int]$MutexWaitMilliseconds = 30000
    )
    $report = [ordered]@{ status = "refused"; mode = $(if ($VerifyAbsent) { "verify_absent" } else { "delete" }); synthetic_rows = 0; deleted = 0; kept_not_created_by_iu = 0; remaining_after = $null; reason = $null }
    if ($Book -cne "test") { $report.reason = "test_book_required"; return $report }
    $bookError = Test-XbAc2BookBinding -Book "test" -Config $Config
    if ($null -ne $bookError) { $report.reason = $bookError; return $report }
    if ([string]::IsNullOrWhiteSpace($Config.IntegrationUserId) -or -not (Test-XbAc2SameText $Config.LoginUserId $Config.IntegrationUserId)) { $report.reason = "integration_user_mismatch"; return $report }
    if (-not $VerifyAbsent -and -not $ConfirmDelete) { $report.reason = "confirm_delete_required"; return $report }

    $mutex = $null
    $owned = $false
    try {
        # Serialise with any primitive run on this host.
        $mutex = New-Object System.Threading.Mutex($false, $MutexName)
        try { $owned = $mutex.WaitOne($MutexWaitMilliseconds) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
        if (-not $owned) { $report.reason = "mutex_busy"; return $report }
        $session = $null
        try { $session = New-XbAutoCountSession -EnableProductionAdapter:$EnableProductionAdapter -SessionFactory $SessionFactory } catch { $session = $null }
        finally {
            if (-not [string]::IsNullOrWhiteSpace($Config.PasswordEnvironmentVariable) -and $Config.PasswordEnvironmentVariable -match '^[A-Za-z_][A-Za-z0-9_]*$') {
                [Environment]::SetEnvironmentVariable($Config.PasswordEnvironmentVariable, $null, "Process")
            }
        }
        if ($null -eq $session) { $report.reason = "session_unavailable"; return $report }
        if (-not (Test-XbAc2SameText ([string]$session.DatabaseName) $Config.DatabaseName)) { $report.reason = "book_binding_mismatch"; return $report }
        if (-not (Test-XbAc2SameText ([string]$session.LoginUserId) $Config.IntegrationUserId)) { $report.reason = "integration_user_mismatch"; return $report }

        $rows = Get-XbAutoCountMemberProbeRows -Session $session -MemberCommandFactory $MemberCommandFactory
        $synthetic = @(@($rows) | Where-Object { Test-XbCleanupSyntheticRow -Row $_ })
        $report.synthetic_rows = $synthetic.Count
        if ($VerifyAbsent) {
            $report.status = $(if ($synthetic.Count -eq 0) { "absent" } else { "present" })
            return $report
        }

        $command = Get-XbAutoCountMemberCommand -Session $session -MemberCommandFactory $MemberCommandFactory
        if ($null -eq $command.PSObject.Methods["DeleteMember"]) { $report.reason = "delete_surface_missing"; return $report }
        foreach ($row in $synthetic) {
            if (-not (Test-XbAc2SameText ([string]$row.CreatedUserID) $Config.IntegrationUserId)) { $report.kept_not_created_by_iu++; continue }
            $memberNo = ([string]$row.MemberNo).Trim()
            # Re-read the exact row and re-check every row condition before deleting.
            $entity = Get-XbAutoCountMember -Session $session -MemberNo $memberNo -MemberCommandFactory $MemberCommandFactory
            if ($null -eq $entity) { continue }
            $name = [string](Get-XbAutoCountEntityValue -Entity $entity -Field "Name")
            $email = [string](Get-XbAutoCountEntityValue -Entity $entity -Field "EmailAddress")
            $audit = Get-XbAutoCountMemberAudit -Entity $entity
            if (-not ((Test-XbAc2SyntheticName $name) -and (Test-XbAc2SyntheticEmail $email) -and (Test-XbAc2SameText $audit.CreatedUserID $Config.IntegrationUserId))) { $report.kept_not_created_by_iu++; continue }
            [void]$command.DeleteMember($memberNo)
            $report.deleted++
        }
        $after = Get-XbAutoCountMemberProbeRows -Session $session -MemberCommandFactory $MemberCommandFactory
        $report.remaining_after = @(@($after) | Where-Object { (Test-XbCleanupSyntheticRow -Row $_) -and (Test-XbAc2SameText ([string]$_.CreatedUserID) $Config.IntegrationUserId) }).Count
        $report.status = $(if ($report.remaining_after -eq 0) { "cleaned" } else { "incomplete" })
        return $report
    }
    finally {
        if ($null -ne $mutex) {
            if ($owned) { try { $mutex.ReleaseMutex() } catch { } }
            $mutex.Dispose()
        }
    }
}

if ($LibraryOnly) { return }

$ErrorActionPreference = "Stop"
$report = Invoke-XbAc2MemberTestCleanup -Book $script:XbCleanupBook -VerifyAbsent:$script:XbCleanupVerifyAbsent -ConfirmDelete:$script:XbCleanupConfirmDelete -Config (Get-XbAc2PrimitiveEnvironmentConfig) -EnableProductionAdapter:$script:XbCleanupProductionAdapter
[Console]::Out.WriteLine(($report | ConvertTo-Json -Compress))
if ($report.status -in @("absent", "cleaned")) { exit 0 }
exit 1
