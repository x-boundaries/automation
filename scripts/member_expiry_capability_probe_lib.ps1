# Pure, dot-sourceable helper library for the synthetic ExpiryDate capability probe.
#
# This library contains NO AutoCount calls, no network, and no live action. It is
# dot-sourced by scripts/ac2_member_expiry_capability_probe.ps1 (which owns the
# AutoCount reflection and the single SaveMember call site) and is exercised directly
# by the tests without AutoCount. Every function here is deterministic given its
# inputs, so the terminal-state machine, the durable single-use attempt claim, the
# no-clobber result publication, and the fingerprints can all be tested in isolation.

Set-StrictMode -Version Latest

$script:ExpiryProbeSchemaVersion = "member_expiry_capability_probe/v1"
$script:ExpiryProbeIntendedExpiry = "2028-06-30"

# The ONE canonical claim/result root for this machine. It is a fixed reviewed constant: it
# is NOT read from an environment variable, NOT derived from the current or deployment
# directory, and NOT selectable from the command line, so the permanent single-use attempt
# claim has exactly one namespace per machine and concurrent launches cannot each claim a
# private directory. The probe never creates, repairs, migrates, cleans or redirects it; the
# operator creates it once (see the runbook). This is also the previously documented
# location, so an attempt claim written by an earlier run stays discoverable.
$script:ExpiryProbeCanonicalStateRoot = "C:\XB\create_uat\expiry_probe_state"

# Content-borne publication contract. A result artefact carries the exact basename it is
# authoritative under, so a staged (or otherwise renamed or copied) artefact invalidates
# itself no matter which terminal outcome its bytes happen to contain.
$script:ExpiryProbePublicationContractVersion = "member_expiry_capability_probe_publication/v1"
$script:ExpiryProbeAuthorityRule = "This artefact is authoritative ONLY when its current file basename is exactly equal to authoritative_result_basename. Any other basename, including an expiry_probe_staging_<operation_id>.incomplete staging artefact, is NON-AUTHORITATIVE regardless of the terminal_outcome, evidence_persisted or exit_code it contains."

# The complete canonical terminal vocabulary. Every active-run outcome is exactly one
# of these; only EXPIRY_VERIFIED is a success.
$script:ExpiryProbeTerminalCodes = @(
    "REFUSED",
    "CLAIM_ROOT_UNAVAILABLE",
    "ATTEMPT_ALREADY_CLAIMED",
    "ATTEMPT_CLAIM_LOST_AFTER_CONTACT",
    "CLAIM_PERSISTENCE_FAILED",
    "BLOCKED_MEMBER_EXISTS",
    "FAILED_BEFORE_WRITE",
    "WRITE_OUTCOME_UNCERTAIN",
    "WRITE_CONFIRMED_READBACK_FAILED",
    "EXPIRY_READBACK_MISMATCH",
    "EXPIRY_VERIFIED",
    "EVIDENCE_PERSISTENCE_FAILED"
)

# Marker prefix a durable-write helper uses when the file was created but the content
# could not be durably persisted (write/flush failed AFTER an exclusive CreateNew), as
# distinct from the file already existing (a conflict). Callers key fail-closed handling
# off this so a storage failure is never mislabelled as a pre-existing claim.
$script:ExpiryProbePostCreatePersistTag = "post-create-persist-failed"
$script:ExpiryProbeSaveOutcomes = @("not_attempted", "confirmed", "uncertain")

# --------------------------------------------------------------------------- #
# Hashing / fingerprints (emit only fingerprints, never raw target/identity).
# --------------------------------------------------------------------------- #
function Get-ExpiryProbeSha256Hex {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        $hash = $sha.ComputeHash($bytes)
    }
    finally { $sha.Dispose() }
    -join ($hash | ForEach-Object { $_.ToString("x2") })
}

function Get-ExpiryProbeTargetFingerprint {
    # A non-secret SHA-256 over a canonical representation of the AutoCount target
    # (server + database/account book). The raw target values are never emitted.
    #
    # SQL Server instance names and AutoCount database/account-book names are
    # case-insensitive, so the canonical form is trimmed and lower-cased (invariant).
    # This makes casing variants (SERVER\INSTANCE vs server\instance) resolve to the
    # SAME target and attempt fingerprint, so the single-use claim is a real
    # concurrency/no-retry boundary regardless of how the target is spelled.
    param([Parameter(Mandatory)][AllowEmptyString()][string]$ServerName,
          [Parameter(Mandatory)][AllowEmptyString()][string]$DatabaseName)
    $canonical = "server=" + $ServerName.Trim().ToLowerInvariant() + "|database=" + $DatabaseName.Trim().ToLowerInvariant()
    "tfp_" + (Get-ExpiryProbeSha256Hex -Text $canonical)
}

function Get-ExpiryProbeSyntheticFingerprint {
    # A non-secret fingerprint of the synthetic member identifier, so evidence never
    # carries the raw synthetic member number.
    param([Parameter(Mandatory)][string]$MemberNo)
    "smf_" + (Get-ExpiryProbeSha256Hex -Text $MemberNo)
}

function Get-ExpiryProbeAttemptFingerprint {
    # The STABLE single-use key: identical across retries of the same logical operation
    # (same target, same synthetic record, same intended ExpiryDate), so a second
    # invocation finds the existing claim and refuses. Deliberately NOT derived from the
    # per-run operation_id, which changes every launch.
    param(
        [Parameter(Mandatory)][string]$TargetFingerprint,
        [Parameter(Mandatory)][string]$SyntheticFingerprint,
        [Parameter(Mandatory)][string]$IntendedExpiry
    )
    $canonical = "$script:ExpiryProbeSchemaVersion|$TargetFingerprint|$SyntheticFingerprint|$IntendedExpiry"
    "afp_" + (Get-ExpiryProbeSha256Hex -Text $canonical)
}

function Get-ExpiryProbeMaskedMemberNo {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$MemberNo)
    if ([string]::IsNullOrEmpty($MemberNo) -or $MemberNo.Length -le 2) { return "***" }
    $MemberNo.Substring(0, 2) + "***" + $MemberNo.Substring($MemberNo.Length - 1, 1)
}

function Get-ExpiryProbePathRedacted {
    # Replace Windows drive-letter and UNC paths with <path> so a sanitised diagnostic
    # cannot leak a private state directory, result path, or AutoCount root (which may
    # embed a machine username or an internal share). Best-effort; never raises.
    param([AllowNull()]$Text)
    $s = "" + $Text
    $s = [regex]::Replace($s, '\\\\[^\s"'']+', '<path>')          # UNC \\host\share\...
    $s = [regex]::Replace($s, '[A-Za-z]:\\[^\s"'']*', '<path>')   # drive-letter C:\...
    return $s
}

function Get-ExpiryProbeNormalizedDate {
    # Normalise a date-shaped value to yyyy-MM-dd (or "" for null/blank), so the
    # read-back comparison is representation-independent (DateTime vs string).
    param([AllowNull()]$Value)
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return "" }
    if ($Value -is [datetime]) { return $Value.ToString("yyyy-MM-dd") }
    $text = ([string]$Value).Trim()
    $parsed = [datetime]::MinValue
    if ([datetime]::TryParse($text, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::None, [ref]$parsed)) {
        return $parsed.ToString("yyyy-MM-dd")
    }
    return $text
}

# --------------------------------------------------------------------------- #
# Canonical state root and the artefact namespace derived from it.
#
# There is exactly ONE claim/result root per machine and nothing outside this library can
# select it. Every artefact basename is derived purely from the schema-stable attempt
# fingerprint (claim) or the per-run operation id (result/staging), so the claim namespace
# does not depend on any process argument, working directory or deployment path.
# --------------------------------------------------------------------------- #
function Get-ExpiryProbeCanonicalStateRoot {
    # Nullary production accessor: the single fixed claim/result root. Deliberately takes
    # no parameters so no caller (and no executable script switch) can redirect it.
    $script:ExpiryProbeCanonicalStateRoot
}

function Get-ExpiryProbeClaimBasename {
    param([Parameter(Mandatory)][string]$AttemptFingerprint)
    "expiry_probe_claim_" + $AttemptFingerprint + ".claim"
}

function Get-ExpiryProbeResultBasename {
    param([Parameter(Mandatory)][string]$OperationId)
    "expiry_probe_result_" + $OperationId + ".json"
}

function Get-ExpiryProbeStagingBasename {
    param([Parameter(Mandatory)][string]$OperationId)
    "expiry_probe_staging_" + $OperationId + ".incomplete"
}

function Get-ExpiryProbeStatePaths {
    # Resolve the three artefact paths under a state root. Production always passes
    # (Get-ExpiryProbeCanonicalStateRoot); the -Root parameter exists ONLY so the pure
    # unit tests can inject a temporary directory. No executable script parameter reaches
    # it, so there is no live override.
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$AttemptFingerprint,
        [Parameter(Mandatory)][string]$OperationId
    )
    $claim = Get-ExpiryProbeClaimBasename -AttemptFingerprint $AttemptFingerprint
    $final = Get-ExpiryProbeResultBasename -OperationId $OperationId
    $staging = Get-ExpiryProbeStagingBasename -OperationId $OperationId
    [pscustomobject]@{
        claim_basename   = $claim
        result_basename  = $final
        staging_basename = $staging
        claim_path       = (Join-Path $Root $claim)
        result_path      = (Join-Path $Root $final)
        staging_path     = (Join-Path $Root $staging)
    }
}

function Get-ExpiryProbePathComponents {
    # Ordered existing-path chain from the volume/filesystem root through $Path. The chain
    # is built from the lexically normalised path only: no component is followed, resolved
    # or created, so a redirected component is reported rather than silently traversed.
    param([Parameter(Mandatory)][string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    $chain = [System.Collections.Generic.List[string]]::new()
    $dir = [System.IO.DirectoryInfo]::new($full)
    while ($null -ne $dir) {
        $chain.Insert(0, $dir.FullName)
        $dir = $dir.Parent
    }
    return $chain.ToArray()
}

function Test-ExpiryProbeTrustedStateRoot {
    # Fail-closed trust validation for a claim/result root, run BEFORE any assembly load,
    # authentication or live read. Every existing component from the volume root through the
    # root directory must be a plain, present, non-redirected directory. Nothing is created,
    # repaired, migrated or cleaned, and no reparse point is followed.
    #
    # Production always passes (Get-ExpiryProbeCanonicalStateRoot) with -RequireWindows; the
    # -Root parameter exists ONLY for deterministic unit tests and is not reachable from the
    # executable script.
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Root,
        [switch]$RequireWindows
    )
    # Exactly one failure reason is recorded: the first fail-closed condition found while
    # walking outward-in from the volume root. $reason stays empty only when every component
    # passed every check.
    $reason = ""
    if ($RequireWindows -and [System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        # The active probe path is Windows-only. On any other platform it fails closed here
        # rather than silently skipping the rest of the safety contract.
        $reason = 'platform_not_windows'
    }
    elseif ([string]::IsNullOrWhiteSpace($Root) -or -not [System.IO.Path]::IsPathRooted($Root)) {
        $reason = 'root_not_absolute'
    }
    else {
        $full = $null
        $chain = $null
        try {
            $full = [System.IO.Path]::GetFullPath($Root)
            $chain = @(Get-ExpiryProbePathComponents -Path $Root)
        }
        catch { $reason = 'root_unresolvable' }
        if ($reason -eq "" -and ($null -eq $chain -or $chain.Count -eq 0)) { $reason = 'root_unresolvable' }
        if ($reason -eq "" -and $RequireWindows -and $full -notmatch '^[A-Za-z]:\\') {
            # A UNC/network root is a redirected location by construction.
            $reason = 'root_not_local_volume'
        }
        if ($reason -eq "") {
            $expectedRoot = $null
            try { $expectedRoot = [System.IO.Path]::GetPathRoot($full) } catch { $expectedRoot = $null }
            if ([string]::IsNullOrEmpty($expectedRoot) -or
                -not $chain[0].Equals($expectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                $reason = 'volume_root_unexpected'
            }
        }
        if ($reason -eq "") {
            for ($i = 0; $i -lt $chain.Count; $i++) {
                $component = $chain[$i]
                $isLeaf = ($i -eq ($chain.Count - 1))
                $exists = $false
                try { $exists = [System.IO.Directory]::Exists($component) -or [System.IO.File]::Exists($component) }
                catch { $reason = 'component_stat_failed'; break }
                if (-not $exists) {
                    if ($isLeaf) { $reason = 'root_missing' } else { $reason = 'component_missing' }
                    break
                }
                $info = $null
                try { $info = Get-Item -LiteralPath $component -Force -ErrorAction Stop }
                catch { $reason = 'component_stat_failed'; break }
                if ($null -eq $info) { $reason = 'component_stat_failed'; break }
                if ($info -isnot [System.IO.DirectoryInfo]) {
                    if ($isLeaf) { $reason = 'root_not_directory' } else { $reason = 'component_not_directory' }
                    break
                }
                $attributes = $null
                try { $attributes = $info.Attributes } catch { $reason = 'component_stat_failed'; break }
                if ($attributes.HasFlag([System.IO.FileAttributes]::ReparsePoint)) {
                    # A junction, symbolic link or other redirected component. Reject WITHOUT
                    # following or resolving it.
                    if ($isLeaf) { $reason = 'root_reparse_point' } else { $reason = 'component_reparse_point' }
                    break
                }
            }
        }
    }
    $reasons = @()
    if ($reason -ne "") { $reasons = @($reason) }
    [pscustomobject]@{ trusted = ($reason -eq ""); reasons = $reasons }
}

# --------------------------------------------------------------------------- #
# Trusted state-root LEASE.
#
# Validating the root once and then using ordinary string paths is not enough: between the
# check and the irreversible SaveMember, a permitted rename or replacement of the root (or of
# any ancestor) could redirect a contender into a second backing claim namespace, defeating the
# global exactly-once boundary.
#
# The lease therefore PINS the namespace. It opens a Windows directory handle on every existing
# component from the local volume root through the canonical directory, parent before child,
# and RETAINS all of them. The handles are opened WITHOUT FILE_SHARE_DELETE, so while the lease
# is held no other process can rename, delete or replace any leased component. Handles are
# released, leaf first, only after terminal evidence handling has finished.
#
# Nothing here creates, repairs, migrates, cleans or deletes a directory, and no component is
# followed: FILE_FLAG_OPEN_REPARSE_POINT opens the link itself so a redirection is detected
# rather than traversed.
# --------------------------------------------------------------------------- #
function Initialize-ExpiryProbeNativeDirectoryApi {
    # Compiled lazily and only on the Windows active path, so dot-sourcing the library for the
    # pure/portable helpers never pays for (or depends on) native interop.
    if ('XbExpiryProbe.NativeDirectory' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace XbExpiryProbe {
    [StructLayout(LayoutKind.Sequential)]
    public struct BY_HANDLE_FILE_INFORMATION {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    public class DirectoryIdentity {
        public bool Ok;
        public uint Attributes;
        public uint VolumeSerialNumber;
        public ulong FileIndex;
    }

    public static class NativeDirectory {
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern SafeFileHandle CreateFileW(
            string lpFileName, uint dwDesiredAccess, uint dwShareMode, IntPtr lpSecurityAttributes,
            uint dwCreationDisposition, uint dwFlagsAndAttributes, IntPtr hTemplateFile);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(
            SafeFileHandle hFile, out BY_HANDLE_FILE_INFORMATION lpFileInformation);

        private const uint FILE_READ_ATTRIBUTES = 0x0080;
        private const uint FILE_LIST_DIRECTORY  = 0x0001;
        private const uint SYNCHRONIZE          = 0x00100000;
        private const uint FILE_SHARE_READ      = 0x00000001;
        private const uint FILE_SHARE_WRITE     = 0x00000002;
        private const uint OPEN_EXISTING        = 3;
        private const uint FILE_FLAG_BACKUP_SEMANTICS   = 0x02000000;
        private const uint FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000;

        public static SafeFileHandle OpenDirectoryNoDeleteShare(string path) {
            // Share read and write so ordinary claim/staging/result file operations inside the
            // directory keep working, but deliberately WITHOUT FILE_SHARE_DELETE: renaming or
            // deleting a directory needs DELETE access, so every such attempt by another
            // process fails with a sharing violation while this handle is retained.
            return CreateFileW(
                path,
                FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY | SYNCHRONIZE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                IntPtr.Zero,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                IntPtr.Zero);
        }

        public static DirectoryIdentity GetIdentity(SafeFileHandle handle) {
            // Inspect the OPENED OBJECT itself, not the path that was used to reach it.
            DirectoryIdentity identity = new DirectoryIdentity();
            BY_HANDLE_FILE_INFORMATION info;
            identity.Ok = GetFileInformationByHandle(handle, out info);
            if (identity.Ok) {
                identity.Attributes = info.FileAttributes;
                identity.VolumeSerialNumber = info.VolumeSerialNumber;
                identity.FileIndex = ((ulong)info.FileIndexHigh << 32) | (ulong)info.FileIndexLow;
            }
            return identity;
        }
    }
}
'@
}

function New-ExpiryProbeTrustedRootLease {
    # Acquire a trusted state-root lease. Production always passes
    # (Get-ExpiryProbeCanonicalStateRoot); the -Root parameter exists ONLY so pure unit tests
    # can pin a temporary directory chain. No executable script parameter reaches it.
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Root,
        [switch]$RequireWindows
    )
    $reasons = [System.Collections.Generic.List[string]]::new()
    $handles = [System.Collections.Generic.List[object]]::new()
    $identities = [System.Collections.Generic.List[string]]::new()
    $chain = @()
    $failed = $false

    # The lease is inherently Win32. Fail closed BEFORE any native interop is even compiled.
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        $reasons.Add('platform_not_windows')
        $failed = $true
    }
    if (-not $failed) {
        # Reuse the single fail-closed validator so lease and validation cannot diverge.
        $verdict = Test-ExpiryProbeTrustedStateRoot -Root $Root -RequireWindows:$RequireWindows
        if (-not $verdict.trusted) {
            foreach ($reason in @($verdict.reasons)) { $reasons.Add($reason) }
            $failed = $true
        }
    }
    if (-not $failed) {
        try { $chain = @(Get-ExpiryProbePathComponents -Path $Root) }
        catch { $reasons.Add('root_unresolvable'); $failed = $true }
    }
    if (-not $failed) {
        Initialize-ExpiryProbeNativeDirectoryApi
        foreach ($component in $chain) {
            # Parent-before-child: the parent's handle is already retained in $handles before
            # its child is opened, so no ancestor can be swapped mid-walk.
            $handle = $null
            try { $handle = [XbExpiryProbe.NativeDirectory]::OpenDirectoryNoDeleteShare($component) }
            catch { $reasons.Add('component_open_failed'); $failed = $true; break }
            if ($null -eq $handle) { $reasons.Add('component_open_failed'); $failed = $true; break }
            if ($handle.IsInvalid) {
                $handle.Dispose()
                $reasons.Add('component_open_failed'); $failed = $true; break
            }
            $handles.Add($handle)
            $identity = [XbExpiryProbe.NativeDirectory]::GetIdentity($handle)
            if (-not $identity.Ok) { $reasons.Add('component_identity_unavailable'); $failed = $true; break }
            if (($identity.Attributes -band [uint32]0x00000010) -eq 0) { $reasons.Add('component_not_directory'); $failed = $true; break }
            if (($identity.Attributes -band [uint32]0x00000400) -ne 0) { $reasons.Add('component_reparse_point'); $failed = $true; break }
            # Stable volume/file identity, retained internally only. Never emitted: it is not a
            # path, but it still describes private machine state.
            $identities.Add(('vol{0:x8}:idx{1:x16}' -f $identity.VolumeSerialNumber, $identity.FileIndex))
        }
    }
    if ($failed) {
        # Partial acquisition: dispose every handle already opened, in reverse order, so a
        # failed lease never leaves the chain pinned.
        for ($i = $handles.Count - 1; $i -ge 0; $i--) {
            try { $handles[$i].Dispose() } catch { }
        }
        $handles.Clear()
        $identities.Clear()
    }
    [pscustomobject]@{
        acquired        = (-not $failed)
        reasons         = @($reasons.ToArray())
        component_count = @($chain).Count
        identity_count  = $identities.Count
        held_count      = $handles.Count
        handles         = $handles
        identities      = @($identities.ToArray())
    }
}

function Close-ExpiryProbeTrustedRootLease {
    # Dispose every retained handle in reverse order (leaf first, volume root last). The caller
    # must invoke this ONLY from the outer cleanup path, after terminal evidence handling has
    # finished, so the namespace stays pinned across the whole irreversible operation.
    param([Parameter(Mandatory)][AllowNull()]$Lease)
    if ($null -eq $Lease) { return }
    $handles = Get-ExpiryProbeFlag $Lease 'handles' $null
    if ($null -eq $handles) { return }
    for ($i = $handles.Count - 1; $i -ge 0; $i--) {
        try { $handles[$i].Dispose() } catch { }
    }
    $handles.Clear()
    $Lease.held_count = 0
}

# --------------------------------------------------------------------------- #
# Durable single-use attempt claim (P1) and path-bound no-clobber result publication (P2).
# --------------------------------------------------------------------------- #
function New-ExpiryProbeDurableArtifact {
    # Exclusive-create a durable artefact. Fails closed if it already exists so a prior
    # claim/result is never overwritten. WriteThrough + Flush(true) push the bytes past
    # OS caches where the platform supports it. Never deletes anything.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Content)
    # CreateNew throws IOException if the file already exists; that propagates unwrapped
    # so the caller treats it as a conflict (the file was NOT created by us).
    $stream = [System.IO.FileStream]::new(
        $Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None, 4096, [System.IO.FileOptions]::WriteThrough)
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Content)
        $stream.Write($bytes, 0, $bytes.Length)
        # Flush(true) forces the data to the storage device. Only fall back to the
        # ordinary Flush() for a runtime that genuinely does not support the durable
        # overload (NotSupportedException); any other flush failure (e.g. an I/O error
        # that cannot confirm durability) must propagate so the caller fails closed
        # BEFORE the irreversible save rather than proceeding on a non-durable claim.
        try { $stream.Flush($true) } catch [System.NotSupportedException] { $stream.Flush() }
    }
    catch {
        # The file was exclusively created but its content could not be durably
        # persisted. Tag the failure so the caller distinguishes this storage error
        # (a distinct pre-write failure) from a pre-existing/concurrent claim, while the
        # partial file remains on disk as a fail-closed marker (never deleted here).
        throw ($script:ExpiryProbePostCreatePersistTag + ": " + $_.Exception.Message)
    }
    finally { $stream.Dispose() }
}

function New-ExpiryProbePublicationContract {
    # The content-borne publication contract embedded in the staged bytes. It binds the
    # artefact to ONE authoritative basename, so the same bytes sitting at a staging path
    # (or any other name) are self-invalidating.
    param([Parameter(Mandatory)][string]$OperationId)
    [ordered]@{
        publication_contract_version  = $script:ExpiryProbePublicationContractVersion
        authoritative_result_basename = (Get-ExpiryProbeResultBasename -OperationId $OperationId)
        authority_rule                = $script:ExpiryProbeAuthorityRule
    }
}

function Publish-ExpiryProbeResultAtomic {
    # Path-bound, no-clobber result publication:
    #   1. exclusive-create the same-directory staging artefact (never overwritten, never
    #      truncated, never deleted, even when it already exists from an earlier attempt);
    #   2. durably write and flush it (inside New-ExpiryProbeDurableArtifact);
    #   3. atomically publish it with a NO-REPLACE move to the final result path.
    #
    # If the move fails the staging artefact is deliberately LEFT EXACTLY AS WRITTEN: it is
    # not deleted, quarantined, renamed or rewritten, and no alternative publication route is
    # attempted. It cannot be mistaken for authoritative evidence because its own bytes bind
    # it to the final basename it never reached (see Test-ExpiryProbeAuthoritativeResult).
    param(
        [Parameter(Mandatory)][string]$StagingPath,
        [Parameter(Mandatory)][string]$FinalPath,
        [Parameter(Mandatory)][string]$Content,
        # Pure dependency injection for deterministic move-failure unit tests ONLY. No
        # executable script parameter reaches this, so there is no live bypass.
        [scriptblock]$MoveAction
    )
    if (Test-Path -LiteralPath $FinalPath) { throw "Terminal result artefact already exists; refusing to overwrite evidence." }
    # CreateNew: a pre-existing staging artefact is a conflict that propagates untouched.
    New-ExpiryProbeDurableArtifact -Path $StagingPath -Content $Content
    if ($null -eq $MoveAction) {
        # File.Move is no-replace: it throws when the destination exists, so a result created
        # by a racing process is never clobbered.
        [System.IO.File]::Move($StagingPath, $FinalPath)
    }
    else {
        & $MoveAction $StagingPath $FinalPath
    }
}

function New-ExpiryProbeClaimContent {
    # Sanitised, non-secret claim/evidence metadata only. Never the raw server,
    # database, credentials, password, user, synthetic name, or synthetic email.
    param(
        [Parameter(Mandatory)][string]$OperationId,
        [Parameter(Mandatory)][string]$ApprovalReference,
        [Parameter(Mandatory)][string]$TargetFingerprint,
        [Parameter(Mandatory)][string]$SyntheticFingerprint,
        [Parameter(Mandatory)][string]$AttemptFingerprint,
        [Parameter(Mandatory)][string]$IntendedExpiry
    )
    [ordered]@{
        schema_version        = $script:ExpiryProbeSchemaVersion
        operation_id          = $OperationId
        approval_reference    = $ApprovalReference
        target_fingerprint    = $TargetFingerprint
        synthetic_fingerprint = $SyntheticFingerprint
        attempt_fingerprint   = $AttemptFingerprint
        intended_expiry_date  = $IntendedExpiry
        created_at_utc        = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    } | ConvertTo-Json -Depth 4
}

# --------------------------------------------------------------------------- #
# Centralised, pure terminal-state derivation (P1).
# --------------------------------------------------------------------------- #
function Get-ExpiryProbeFlag { param($Flags, [string]$Name, $Default = $null)
    if ($Flags -is [System.Collections.IDictionary]) {
        if ($Flags.Contains($Name)) { return $Flags[$Name] }
        return $Default
    }
    $p = $Flags.PSObject.Properties[$Name]
    if ($null -ne $p) { return $p.Value }
    return $Default
}

function Get-ExpiryProbeStateContradictions {
    # Return a list of impossible-flag reasons; empty means internally consistent.
    param([Parameter(Mandatory)]$Flags)
    $reasons = [System.Collections.Generic.List[string]]::new()
    $attempted = [bool](Get-ExpiryProbeFlag $Flags 'save_member_attempted' $false)
    $confirmed = [bool](Get-ExpiryProbeFlag $Flags 'save_member_confirmed' $false)
    $outcome = Get-ExpiryProbeFlag $Flags 'save_outcome' 'not_attempted'
    $rbFound = [bool](Get-ExpiryProbeFlag $Flags 'readback_found' $false)
    $match = [bool](Get-ExpiryProbeFlag $Flags 'expiry_match' $false)
    $claimConflict = [bool](Get-ExpiryProbeFlag $Flags 'claim_conflict' $false)
    $activated = [bool](Get-ExpiryProbeFlag $Flags 'activated' $false)
    $recheck = [bool](Get-ExpiryProbeFlag $Flags 'member_exists_recheck' $false)
    $contacted = [bool](Get-ExpiryProbeFlag $Flags 'autocount_contacted' $false)
    $initialRead = [bool](Get-ExpiryProbeFlag $Flags 'initial_member_read_attempted' $false)
    $recheckRead = [bool](Get-ExpiryProbeFlag $Flags 'member_recheck_attempted' $false)
    $claimLost = [bool](Get-ExpiryProbeFlag $Flags 'claim_lost_after_contact' $false)
    $rootUnavailable = [bool](Get-ExpiryProbeFlag $Flags 'claim_root_unavailable' $false)
    $claimCreated = [bool](Get-ExpiryProbeFlag $Flags 'claim_created' $false)
    $initialExists = [bool](Get-ExpiryProbeFlag $Flags 'member_exists_initial' $false)

    if ($script:ExpiryProbeSaveOutcomes -notcontains $outcome) { $reasons.Add('save_outcome_invalid') }
    if ($confirmed -and -not $attempted) { $reasons.Add('confirmed_without_attempt') }
    if ($outcome -eq 'confirmed' -and -not $confirmed) { $reasons.Add('outcome_confirmed_without_confirmed_flag') }
    if ($outcome -eq 'not_attempted' -and $attempted) { $reasons.Add('not_attempted_but_attempted') }
    if ($outcome -eq 'uncertain' -and -not $attempted) { $reasons.Add('uncertain_without_attempt') }
    if ($rbFound -and $outcome -ne 'confirmed') { $reasons.Add('readback_without_confirmed_save') }
    if ($match -and -not $rbFound) { $reasons.Add('match_without_found') }
    if ($attempted -and -not $activated) { $reasons.Add('attempt_without_activation') }
    if ($attempted -and $claimConflict) { $reasons.Add('attempt_with_claim_conflict') }
    if ($attempted -and $recheck) { $reasons.Add('recheck_block_after_attempt') }
    $claimPersistFailed = [bool](Get-ExpiryProbeFlag $Flags 'claim_persist_failed' $false)
    if ($claimPersistFailed -and $attempted) { $reasons.Add('claim_persist_failed_after_attempt') }
    if ($claimPersistFailed -and $claimConflict) { $reasons.Add('claim_persist_failed_and_conflict') }

    # ---- Truthful live-contact and claim-race state ---- #
    # ATTEMPT_ALREADY_CLAIMED is a PRE-CONTACT refusal by definition, so a pre-contact claim
    # conflict can never coexist with live AutoCount contact; that case is the distinct
    # ATTEMPT_CLAIM_LOST_AFTER_CONTACT instead.
    if ($claimConflict -and $contacted) { $reasons.Add('already_claimed_after_contact') }
    if ($claimLost -and -not $contacted) { $reasons.Add('claim_lost_without_contact') }
    if ($claimLost -and $claimConflict) { $reasons.Add('claim_lost_and_pre_contact_conflict') }
    if ($claimLost -and $attempted) { $reasons.Add('claim_lost_but_save_attempted') }
    if ($claimLost -and $claimCreated) { $reasons.Add('claim_lost_but_claim_created') }
    # CLAIM_ROOT_UNAVAILABLE is decided before any assembly load, authentication or live
    # read, so no contact, claim or save flag may accompany it.
    if ($rootUnavailable -and $contacted) { $reasons.Add('claim_root_unavailable_after_contact') }
    if ($rootUnavailable -and $attempted) { $reasons.Add('claim_root_unavailable_with_save_attempt') }
    if ($rootUnavailable -and $claimCreated) { $reasons.Add('claim_root_unavailable_with_claim') }
    if ($rootUnavailable -and ($claimConflict -or $claimLost -or $claimPersistFailed)) { $reasons.Add('claim_root_unavailable_with_claim_state') }
    if ($rootUnavailable -and ($initialRead -or $recheckRead)) { $reasons.Add('claim_root_unavailable_with_live_read') }
    # A live read or an irreversible save cannot precede live contact, and an existence
    # verdict cannot exist without the read that produced it.
    if ($initialRead -and -not $contacted) { $reasons.Add('initial_read_without_contact') }
    if ($recheckRead -and -not $contacted) { $reasons.Add('recheck_read_without_contact') }
    if ($attempted -and -not $contacted) { $reasons.Add('save_without_contact') }
    if ($initialExists -and -not $initialRead) { $reasons.Add('member_exists_without_read_attempt') }
    if ($recheck -and -not $recheckRead) { $reasons.Add('recheck_without_read_attempt') }
    return $reasons.ToArray()
}

function Get-ExpiryProbeTerminalOutcome {
    # The single source of truth for the terminal outcome. Ordered so that once a save
    # was attempted (confirmed or uncertain) the result can ONLY be a post-save code:
    # FAILED_BEFORE_WRITE and BLOCKED_MEMBER_EXISTS are unreachable after SaveMember
    # begins. A confirmed save whose read-back throws or is not found becomes
    # WRITE_CONFIRMED_READBACK_FAILED, never FAILED_BEFORE_WRITE.
    param([Parameter(Mandatory)]$Flags)
    if (-not [bool](Get-ExpiryProbeFlag $Flags 'activated' $false)) { return 'REFUSED' }
    # Decided before any live access, so it precedes every claim/contact state.
    if ([bool](Get-ExpiryProbeFlag $Flags 'claim_root_unavailable' $false)) { return 'CLAIM_ROOT_UNAVAILABLE' }
    # A claim race lost AFTER live contact is never reported as a pre-contact refusal.
    if ([bool](Get-ExpiryProbeFlag $Flags 'claim_lost_after_contact' $false)) { return 'ATTEMPT_CLAIM_LOST_AFTER_CONTACT' }
    if ([bool](Get-ExpiryProbeFlag $Flags 'claim_conflict' $false)) { return 'ATTEMPT_ALREADY_CLAIMED' }
    if ([bool](Get-ExpiryProbeFlag $Flags 'claim_persist_failed' $false)) { return 'CLAIM_PERSISTENCE_FAILED' }
    $outcome = Get-ExpiryProbeFlag $Flags 'save_outcome' 'not_attempted'
    if ($outcome -eq 'confirmed') {
        if (-not [bool](Get-ExpiryProbeFlag $Flags 'readback_found' $false)) { return 'WRITE_CONFIRMED_READBACK_FAILED' }
        if ([bool](Get-ExpiryProbeFlag $Flags 'expiry_match' $false)) { return 'EXPIRY_VERIFIED' }
        return 'EXPIRY_READBACK_MISMATCH'
    }
    if ($outcome -eq 'uncertain') { return 'WRITE_OUTCOME_UNCERTAIN' }
    if ([bool](Get-ExpiryProbeFlag $Flags 'member_exists_initial' $false) -or
        [bool](Get-ExpiryProbeFlag $Flags 'member_exists_recheck' $false)) { return 'BLOCKED_MEMBER_EXISTS' }
    return 'FAILED_BEFORE_WRITE'
}

function Get-ExpiryProbeFinalOutcome {
    # Apply the durable-evidence override to the underlying (run) outcome. When durable
    # evidence was required but the authoritative result could not be persisted, the
    # honest final outcome is EVIDENCE_PERSISTENCE_FAILED (the capability is NOT proven),
    # regardless of what the read-back showed. Otherwise the final outcome is the
    # underlying outcome unchanged.
    param(
        [Parameter(Mandatory)][string]$UnderlyingOutcome,
        [bool]$DurableRequired = $false,
        [bool]$EvidencePersisted = $true
    )
    if ($DurableRequired -and -not $EvidencePersisted) { return 'EVIDENCE_PERSISTENCE_FAILED' }
    return $UnderlyingOutcome
}

function Get-ExpiryProbeExitCode {
    # Truthful process exit status: 0 ONLY for a final outcome of EXPIRY_VERIFIED (which,
    # because EVIDENCE_PERSISTENCE_FAILED overrides an unpersisted verified run, means a
    # durably persisted verified result). Every other terminal outcome, including
    # REFUSED, CLAIM_ROOT_UNAVAILABLE, ATTEMPT_CLAIM_LOST_AFTER_CONTACT,
    # CLAIM_PERSISTENCE_FAILED, and EVIDENCE_PERSISTENCE_FAILED, is nonzero.
    param([Parameter(Mandatory)][string]$TerminalOutcome)
    if ($TerminalOutcome -eq 'EXPIRY_VERIFIED') { return 0 }
    return 1
}

# --------------------------------------------------------------------------- #
# Pure authoritative-result validation (P2).
#
# Operators and recovery tooling must be able to decide, from an artefact alone, whether it
# is the authoritative terminal evidence for a run. The decision is content-bound, not
# filename-pattern-bound: the record carries the ONE basename it is authoritative under, and
# the artefact's CURRENT basename must equal it exactly. A staged artefact therefore fails
# even when its bytes contain a candidate EXPIRY_VERIFIED with exit_code 0.
# --------------------------------------------------------------------------- #
function Test-ExpiryProbeAuthoritativeResult {
    param(
        # The path (or basename) the artefact is currently stored under.
        [Parameter(Mandatory)][AllowEmptyString()][string]$Path,
        # The parsed artefact record (ConvertFrom-Json output or an ordered dictionary).
        [Parameter(Mandatory)]$Record
    )
    $reasons = [System.Collections.Generic.List[string]]::new()
    $basename = ""
    try { $basename = [System.IO.Path]::GetFileName($Path) } catch { $reasons.Add('basename_unresolvable') }
    if ([string]::IsNullOrWhiteSpace($basename)) { $reasons.Add('basename_unresolvable') }

    # ---- The content-borne publication contract ---- #
    $contract = Get-ExpiryProbeFlag $Record 'publication_contract' $null
    if ($null -eq $contract) { $reasons.Add('publication_contract_missing') }
    else {
        $contractVersion = [string](Get-ExpiryProbeFlag $contract 'publication_contract_version' '')
        if ($contractVersion -ne $script:ExpiryProbePublicationContractVersion) { $reasons.Add('publication_contract_version_mismatch') }
        $boundBasename = [string](Get-ExpiryProbeFlag $contract 'authoritative_result_basename' '')
        if ([string]::IsNullOrWhiteSpace($boundBasename)) { $reasons.Add('authoritative_basename_missing') }
        elseif (-not $basename.Equals($boundBasename, [System.StringComparison]::Ordinal)) {
            # THE decisive check: the artefact is not stored under the basename its own bytes
            # bind it to, so it is not authoritative whatever it claims to contain.
            $reasons.Add('basename_not_authoritative')
        }
        if ([string]::IsNullOrWhiteSpace([string](Get-ExpiryProbeFlag $contract 'authority_rule' ''))) { $reasons.Add('authority_rule_missing') }
    }

    # ---- Schema and run identity ---- #
    if ([string](Get-ExpiryProbeFlag $Record 'schema_version' '') -ne $script:ExpiryProbeSchemaVersion) { $reasons.Add('schema_version_mismatch') }
    $operationId = [string](Get-ExpiryProbeFlag $Record 'operation_id' '')
    if ([string]::IsNullOrWhiteSpace($operationId)) { $reasons.Add('operation_id_missing') }
    else {
        $expectedBasename = Get-ExpiryProbeResultBasename -OperationId $operationId
        if (-not $basename.Equals($expectedBasename, [System.StringComparison]::Ordinal)) { $reasons.Add('operation_id_basename_mismatch') }
        if ([string](Get-ExpiryProbeFlag $Record 'result_basename' '') -ne $expectedBasename) { $reasons.Add('result_basename_inconsistent') }
        # An artefact still stored under its own declared staging basename is never
        # authoritative, independent of the basename-binding check above.
        $declaredStaging = [string](Get-ExpiryProbeFlag $Record 'staging_basename' '')
        if ([string]::IsNullOrWhiteSpace($declaredStaging)) { $reasons.Add('staging_basename_missing') }
        elseif ($basename.Equals($declaredStaging, [System.StringComparison]::Ordinal)) { $reasons.Add('artefact_is_staging') }
    }

    # ---- Terminal-state truth, DERIVED from the recorded runtime flags ---- #
    # A record's declared outcome is never trusted. The underlying outcome is recomputed with
    # the SAME pure derivation the runtime uses, and the final outcome is recomputed from that
    # plus the record's own durable-publication fact. A fabricated or corrupted artefact whose
    # flags show no contact, claim or save therefore cannot declare itself verified.
    if (@(Get-ExpiryProbeStateContradictions -Flags $Record).Count -gt 0) { $reasons.Add('state_contradiction') }
    $declaredTerminal = [string](Get-ExpiryProbeFlag $Record 'terminal_outcome' '')
    $declaredUnderlying = [string](Get-ExpiryProbeFlag $Record 'underlying_terminal_outcome' '')
    $persisted = [bool](Get-ExpiryProbeFlag $Record 'evidence_persisted' $false)
    if ($script:ExpiryProbeTerminalCodes -notcontains $declaredTerminal) { $reasons.Add('terminal_outcome_unknown') }
    if ($script:ExpiryProbeTerminalCodes -notcontains $declaredUnderlying) { $reasons.Add('underlying_outcome_unknown') }

    $derivedUnderlying = Get-ExpiryProbeTerminalOutcome -Flags $Record
    if ($declaredUnderlying -ne $derivedUnderlying) { $reasons.Add('underlying_outcome_not_derived_from_flags') }
    # Use the RECORDED persistence fact, not an assumption that persistence succeeded merely
    # because the artefact reached this validator.
    $derivedFinal = Get-ExpiryProbeFinalOutcome -UnderlyingOutcome $derivedUnderlying -DurableRequired $true -EvidencePersisted $persisted
    if ($declaredTerminal -ne $derivedFinal) { $reasons.Add('terminal_outcome_inconsistent') }
    # A published authoritative result is, by construction, one whose durable write succeeded.
    if (-not $persisted) { $reasons.Add('evidence_not_persisted') }
    $exitCode = Get-ExpiryProbeFlag $Record 'exit_code' $null
    if ($null -eq $exitCode) { $reasons.Add('exit_code_missing') }
    elseif ([int]$exitCode -ne (Get-ExpiryProbeExitCode -TerminalOutcome $derivedFinal)) { $reasons.Add('exit_code_inconsistent') }

    # ---- Runtime-fact prerequisites, independent of any declared outcome ---- #
    $attempted = [bool](Get-ExpiryProbeFlag $Record 'save_member_attempted' $false)
    $claimCreated = [bool](Get-ExpiryProbeFlag $Record 'claim_created' $false)
    $confirmed = [bool](Get-ExpiryProbeFlag $Record 'save_member_confirmed' $false)
    $saveOutcome = [string](Get-ExpiryProbeFlag $Record 'save_outcome' 'not_attempted')
    $readbackFound = [bool](Get-ExpiryProbeFlag $Record 'readback_found' $false)
    # The irreversible save is only ever reached through an exclusively created durable claim.
    if ($attempted -and -not $claimCreated) { $reasons.Add('save_attempted_without_claim') }
    if (($confirmed -or $saveOutcome -eq 'confirmed') -and (-not $attempted -or -not $claimCreated)) {
        $reasons.Add('save_success_without_attempt_or_claim')
    }
    if ($readbackFound -and -not $confirmed) { $reasons.Add('readback_without_confirmed_save') }
    if ([bool](Get-ExpiryProbeFlag $Record 'expiry_match' $false) -and -not $readbackFound) { $reasons.Add('match_without_readback') }
    # An authoritative EXPIRY_VERIFIED requires the COMPLETE runtime path to have happened.
    if ($declaredTerminal -eq 'EXPIRY_VERIFIED' -or $derivedFinal -eq 'EXPIRY_VERIFIED') {
        $requiredTrueFlags = @(
            'activated', 'autocount_contacted', 'initial_member_read_attempted',
            'member_recheck_attempted', 'claim_created', 'save_member_attempted',
            'save_member_confirmed', 'readback_found', 'expiry_match', 'evidence_persisted'
        )
        foreach ($flagName in $requiredTrueFlags) {
            if (-not [bool](Get-ExpiryProbeFlag $Record $flagName $false)) {
                $reasons.Add('verified_without_required_runtime_state')
                break
            }
        }
    }

    $unique = @($reasons.ToArray() | Select-Object -Unique)
    [pscustomobject]@{ authoritative = ($unique.Count -eq 0); reasons = $unique }
}
