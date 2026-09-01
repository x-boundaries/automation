# Energy@Grid runtime launcher library.
#
# Design lock: DL-XB-141-RUNTIME-005-SOURCE-DURABILITY.
# Controlling specification: energygrid-bill-downloader/docs/runtime_source_durability_design.md
# Controlling plan: energygrid-bill-downloader/docs/runtime_source_durability_implementation_plan.md
#
# This file is a PURE, DOT-SOURCEABLE LIBRARY (EGRT-I01). It performs no filesystem
# mutation at import time, no network access, no Git invocation at load, and no live
# action. Every function is deterministic given its inputs and its explicitly supplied
# paths, which is what makes the offline test suite possible. It mirrors the separation
# already established by scripts/member_create_uat_runner_lib.ps1.
#
# The dependency direction is one-way: entry scripts dot-source this library, and this
# library never dot-sources an entry script and never reads a caller's $PSScriptRoot.
#
# Compatibility boundary: every construct here must execute correctly under Windows
# PowerShell 5.1 on .NET Framework 4.x (PSEdition Desktop), which is the production
# runtime. The prohibited PowerShell 7 only constructs are guarded statically by the
# project test suite.
#
# Nothing private is committed here. No credential value, account identity, private
# absolute path, UNC path, host identity, or principal identity appears in this file.

Set-StrictMode -Version Latest

# --------------------------------------------------------------------------------------
# Native no-replace publication interop (design section 7.4)
# --------------------------------------------------------------------------------------
# Windows MoveFileExW WITHOUT MOVEFILE_REPLACE_EXISTING is the primitive that fails rather
# than overwrites if the destination has appeared in the meantime. It is the same
# no-replace move discipline the application already uses when publishing a validated PDF
# (energygrid_bill_downloader/publication.py) and the AutoCount capability probe
# (scripts/member_expiry_capability_probe_lib.ps1).
#
# This is a TYPE DEFINITION emitted once, guarded by a type-presence test so a second
# dot-source in the same session does not throw a duplicate-type error. It performs no
# filesystem action, so the pure-library rule in EGRT-I01 is preserved.
$script:EgNativePublicationInteropSource = @'
using System;
using System.Runtime.InteropServices;

namespace EgRuntime {
    public class NativeMoveResult {
        public bool Ok;
        public int LastError;
    }

    public static class NativePublication {
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode, EntryPoint = "MoveFileExW")]
        private static extern bool MoveFileExW(string lpExistingFileName, string lpNewFileName, uint dwFlags);

        // Write-through ONLY. Replacement, cross-volume copying, and reboot-delayed
        // scheduling are never requested, so the rename is no-replace, same-volume, and
        // synchronous through the documented write-through completion boundary. A losing
        // race is reported to the caller, never resolved by replacing.
        private const uint MOVEFILE_WRITE_THROUGH = 0x00000008;

        public static NativeMoveResult MoveNoReplaceWriteThrough(string source, string destination) {
            NativeMoveResult result = new NativeMoveResult();
            result.Ok = MoveFileExW(source, destination, MOVEFILE_WRITE_THROUGH);
            result.LastError = 0;
            if (result.Ok == false) {
                result.LastError = Marshal.GetLastWin32Error();
            }
            return result;
        }
    }
}
'@

function Initialize-EgNativePublicationInterop {
    # Compile the no-replace move interop on FIRST USE ONLY, guarded by a type-presence
    # test so repeat calls compile nothing and a second dot-source in the same session does
    # not throw a duplicate-type error.
    #
    # Declaring the here-string above is not a side effect, and compiling on first use
    # writes only into the host's own temporary compilation location, never into the
    # launcher root, the deployed checkout, the configuration directory, the browser cache,
    # or the log root, which is the exact domain the zero-mutation contract snapshots. This
    # follows the same lazy-compilation pattern the plan's DD-13 prescribes for the Win32
    # security interop, so EGRT-I01 holds and dot-sourcing the library stays cheap.
    [CmdletBinding()]
    param()

    if (-not ([System.Management.Automation.PSTypeName]'EgRuntime.NativePublication').Type) {
        Add-Type -TypeDefinition $script:EgNativePublicationInteropSource
    }
}

# --------------------------------------------------------------------------------------
# Bounded support-reference vocabulary (design section 11)
# --------------------------------------------------------------------------------------
# The vocabulary is BOUNDED and CLOSED. It uses an EG_LAUNCHER_ prefix so it cannot collide
# with the application's own EG_LOGIN_ and APP_ERROR_ vocabularies.
#
# What may be reported: the support reference, the phase that failed, the exception type
# name, the HRESULT as 0x%08X, and boolean or integer outcome fields.
#
# What may NEVER be reported: any path, any file name, any environment variable value, any
# credential value, length, prefix, or hash, any account identity, any raw Git output, any
# exception message text, and any host or principal identity.
#
# Retiring a reference means MOVING it to the retired set rather than deleting it, so
# evidence written by an earlier build stays readable. An unrecognised failure records
# EG_LAUNCHER_UNCLASSIFIED rather than leaking detail.
$script:EgLauncherSupportRefs = @(
    'EG_LAUNCHER_REPLACE_ARGUMENT_INVALID',
    'EG_LAUNCHER_REPLACE_SHARING_VIOLATION',
    'EG_LAUNCHER_REPLACE_ACCESS_DENIED',
    'EG_LAUNCHER_REPLACE_POSTIMAGE_MISMATCH',
    'EG_LAUNCHER_REPLACE_PREIMAGE_UNRECOVERABLE',
    'EG_LAUNCHER_PUBLISH_DESTINATION_UNEXPECTEDLY_PRESENT',
    'EG_LAUNCHER_PUBLISH_RACE_LOST',
    'EG_LAUNCHER_PUBLISH_POSTIMAGE_MISMATCH',
    'EG_LAUNCHER_CREDENTIAL_ARTEFACT_MISSING',
    'EG_LAUNCHER_CREDENTIAL_IMPORT_FAILED',
    'EG_LAUNCHER_CREDENTIAL_INCOMPLETE',
    'EG_LAUNCHER_CREDENTIAL_RESTORE_FAILED',
    'EG_LAUNCHER_BROWSER_CACHE_UNRESOLVED',
    'EG_LAUNCHER_BROWSER_CACHE_NOT_READY',
    'EG_LAUNCHER_BROWSER_CACHE_BIND_FAILED',
    'EG_LAUNCHER_SOURCE_BINDING_FAILED',
    'EG_LAUNCHER_SOURCE_BRANCH_MISMATCH',
    'EG_LAUNCHER_SOURCE_PATH_MISSING',
    'EG_LAUNCHER_SOURCE_PATH_UNTRACKED',
    'EG_LAUNCHER_SOURCE_STAGED_MODIFICATION',
    'EG_LAUNCHER_SOURCE_UNSTAGED_MODIFICATION',
    'EG_LAUNCHER_SOURCE_DELETED',
    'EG_LAUNCHER_SOURCE_UNTRACKED_OVERLAY',
    'EG_LAUNCHER_GIT_INVOCATION_FAILED',
    'EG_LAUNCHER_GIT_SUBCOMMAND_FORBIDDEN',
    'EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY',
    'EG_LAUNCHER_PACKAGE_MEMBER_MISSING',
    'EG_LAUNCHER_PACKAGE_PARSE_FAILED',
    'EG_LAUNCHER_MANIFEST_MISSING',
    'EG_LAUNCHER_MANIFEST_UNPARSABLE',
    'EG_LAUNCHER_MANIFEST_MISMATCH',
    'EG_LAUNCHER_PATH_NOT_ABSOLUTE',
    'EG_LAUNCHER_PATH_MISSING',
    'EG_LAUNCHER_CONFIG_INSIDE_CHECKOUT',
    'EG_LAUNCHER_CONFIG_UNPARSABLE',
    'EG_LAUNCHER_CONFIG_KEY_MISSING',
    'EG_LAUNCHER_PYTHON_VERSION_UNSUPPORTED',
    'EG_LAUNCHER_ROOT_INSIDE_CHECKOUT',
    'EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE',
    'EG_LAUNCHER_ROOT_ACL_WRITE_TRUSTEE_UNAUTHORISED',
    'EG_LAUNCHER_ROOT_AUTHORISED_SID_SET_INVALID',
    'EG_LAUNCHER_ROOT_REPARSE_POINT',
    'EG_LAUNCHER_FILE_UNEXPECTEDLY_READONLY',
    'EG_LAUNCHER_INSTALL_ADMISSION_INVALID',
    'EG_LAUNCHER_INSTALL_STAGING_FAILED',
    'EG_LAUNCHER_INSTALL_MANIFEST_VERIFY_FAILED',
    'EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE',
    'EG_LAUNCHER_INSTALL_BACKUP_CLEANUP_INCOMPLETE',
    'EG_LAUNCHER_UNCLASSIFIED'
)

# Empty at first implementation (DD-08). The reachability assertion covers both halves, so
# the retired half is vacuously satisfied until a reference is actually retired, and the
# first retirement is regressed the moment it happens.
$script:EgLauncherRetiredSupportRefs = @()

function Get-EgLauncherSupportRefs {
    # The live set. The vocabulary is bounded and closed.
    [CmdletBinding()]
    param()

    @($script:EgLauncherSupportRefs)
}

function Get-EgLauncherRetiredSupportRefs {
    # Retiring a reference means MOVING it here, never deleting it, so evidence written by
    # an earlier build stays readable.
    [CmdletBinding()]
    param()

    @($script:EgLauncherRetiredSupportRefs)
}

function Test-EgLauncherSupportRefLive {
    # Exact membership of the live set. An unrecognised reference is NEVER emitted:
    # callers substitute EG_LAUNCHER_UNCLASSIFIED rather than leaking detail.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$SupportRef)

    foreach ($live in $script:EgLauncherSupportRefs) {
        if ($live -ceq $SupportRef) {
            return $true
        }
    }
    return $false
}

# --------------------------------------------------------------------------------------
# Launcher-root entry classes (design section 6.7)
# --------------------------------------------------------------------------------------
# Every entry in the launcher root belongs to exactly one of three classes, and the
# classification is deterministic:
#
#   Class A  the three deployed package members, at fixed names. The set is EXACT.
#   Class B  recognised installer-owned transaction residue, recognised ONLY by the exact
#            reserved-name contract below. A generic *.bak or *.tmp rule is explicitly NOT
#            sufficient and is never used: it would wave through any file dropped into the
#            launcher root, which is the opposite of the intent.
#   Class C  everything else. Fails closed. The rule is never relaxed to "ignore extras".
#
# Security boundary: package-member paths are produced ONLY by joining the launcher root
# with a fixed Class A name. The root is enumerated to CLASSIFY, never to FIND a script or
# library, and recognised residue is never dot-sourced, invoked, imported, or selected as
# a fallback, and can never satisfy a missing Class A member.
$script:EgDeployedPackageMemberNames = @('launcher.ps1', 'launcher_lib.ps1', 'installation_manifest.json')
$script:EgResidueKinds = @('staging', 'backup', 'rollback')
$script:EgResiduePrefix = '.eglauncher-'
$script:EgResidueFieldDelimiter = '--'
$script:EgCanonicalGuidPattern = '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

function Get-EgLauncherLibraryContract {
    # The library's own bounded self-description. Pure; no side effect.
    [CmdletBinding()]
    param()

    [pscustomobject]@{
        SchemaVersion       = 'eg_launcher_lib/v1'
        DeployedMemberNames = @($script:EgDeployedPackageMemberNames)
    }
}

function Get-EgDeployedPackageMemberNames {
    # The exact three fixed Class A names, enumerated explicitly and NEVER derived from a
    # directory listing, so a file added to the runtime directory later cannot become
    # deployable by accident (design section 6.2, asserted by EGRT-T47).
    [CmdletBinding()]
    param()

    @($script:EgDeployedPackageMemberNames)
}

function Get-EgResidueKinds {
    # The fixed residue-kind vocabulary.
    [CmdletBinding()]
    param()

    @($script:EgResidueKinds)
}

function Test-EgResidueName {
    # Parse a launcher-root entry name against the exact reserved residue contract:
    #
    #   .eglauncher-<kind>--<member>--<operation-id>
    #
    # ALL of the following must hold, and any single failure means the name is not
    # residue: the reserved prefix is present; the remainder splits on the two-hyphen
    # delimiter into EXACTLY three fields; the kind is one of the fixed vocabulary; the
    # member is one of the three Class A fixed names; and the operation identifier is a
    # canonical LOWERCASE GUID. Package member names contain a dot but never the two-hyphen
    # delimiter, so the split is unambiguous.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Name)

    $notResidue = [pscustomobject]@{
        IsResidue   = $false
        Kind        = ''
        Member      = ''
        OperationId = ''
    }

    if ([string]::IsNullOrEmpty($Name)) {
        return $notResidue
    }
    if (-not $Name.StartsWith($script:EgResiduePrefix, [System.StringComparison]::Ordinal)) {
        return $notResidue
    }

    $remainder = $Name.Substring($script:EgResiduePrefix.Length)
    $fields = @($remainder -split $script:EgResidueFieldDelimiter)
    if ($fields.Count -ne 3) {
        return $notResidue
    }

    $kind = $fields[0]
    $member = $fields[1]
    $operationId = $fields[2]

    $kindRecognised = $false
    foreach ($candidate in $script:EgResidueKinds) {
        if ($candidate -ceq $kind) { $kindRecognised = $true }
    }
    if (-not $kindRecognised) {
        return $notResidue
    }

    $memberRecognised = $false
    foreach ($candidate in $script:EgDeployedPackageMemberNames) {
        if ($candidate -ceq $member) { $memberRecognised = $true }
    }
    if (-not $memberRecognised) {
        return $notResidue
    }

    if ($operationId -cnotmatch $script:EgCanonicalGuidPattern) {
        return $notResidue
    }

    [pscustomobject]@{
        IsResidue   = $true
        Kind        = $kind
        Member      = $member
        OperationId = $operationId
    }
}

function New-EgResidueName {
    # The installer's ONLY sanctioned residue-name source. The name carries no private
    # path, host name, principal, or other private value: the member names are public, the
    # kinds are a fixed vocabulary, and the operation identifier is random. It also cannot
    # be executed by accident, because it begins with a dot and ends in no executable
    # extension.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateSet('staging', 'backup', 'rollback')][string]$Kind,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Member,
        [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')][string]$OperationId
    )

    $memberRecognised = $false
    foreach ($candidate in $script:EgDeployedPackageMemberNames) {
        if ($candidate -ceq $Member) { $memberRecognised = $true }
    }
    if (-not $memberRecognised) {
        throw 'a residue name may only be constructed for a deployed package member'
    }

    return ($script:EgResiduePrefix + $Kind + $script:EgResidueFieldDelimiter + $Member +
        $script:EgResidueFieldDelimiter + $OperationId)
}

function Get-EgLauncherRootClassification {
    # Classify every entry in the launcher root. Enumeration is for CLASSIFICATION ONLY:
    # this function never returns a path to be dot-sourced, invoked, or imported, and
    # never selects a substitute for a package member.
    #
    # A Class A name is satisfied only by a FILE, so a directory cannot impersonate a
    # package member: it is reported both as Class C and as a missing member.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRootPath)

    $classA = @()
    $classB = @()
    $classC = @()

    if (Test-Path -LiteralPath $LauncherRootPath -PathType Container) {
        $entries = @(Get-ChildItem -LiteralPath $LauncherRootPath -Force)
        foreach ($entry in $entries) {
            $isFile = (-not $entry.PSIsContainer)
            $isMember = $false
            if ($isFile) {
                foreach ($candidate in $script:EgDeployedPackageMemberNames) {
                    if ($candidate -ceq $entry.Name) { $isMember = $true }
                }
            }
            if ($isMember) {
                $classA = $classA + $entry.Name
                continue
            }
            $residue = Test-EgResidueName -Name $entry.Name
            if ($isFile -and $residue.IsResidue) {
                $classB = $classB + $entry.Name
                continue
            }
            $classC = $classC + $entry.Name
        }
    }

    $missingMembers = @()
    foreach ($candidate in $script:EgDeployedPackageMemberNames) {
        $present = $false
        foreach ($found in $classA) {
            if ($found -ceq $candidate) { $present = $true }
        }
        if (-not $present) {
            $missingMembers = $missingMembers + $candidate
        }
    }

    $supportRef = ''
    if (@($classC).Count -gt 0) {
        $supportRef = 'EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY'
    }
    elseif (@($missingMembers).Count -gt 0) {
        $supportRef = 'EG_LAUNCHER_PACKAGE_MEMBER_MISSING'
    }

    [pscustomobject]@{
        ClassA         = [string[]]@($classA)
        ClassB         = [string[]]@($classB)
        ClassC         = [string[]]@($classC)
        MissingMembers = [string[]]@($missingMembers)
        Pass           = ((@($classC).Count -eq 0) -and (@($missingMembers).Count -eq 0))
        SupportRef     = $supportRef
    }
}

# --------------------------------------------------------------------------------------
# Exit bands (design section 5.3)
# --------------------------------------------------------------------------------------
# The application returns 0, 10, 20, or 64. The runtime layer's own failures therefore use
# a DISJOINT band, so a launcher or installer failure can never be mistaken for an
# application status. The disjointness is asserted by a test, not left to convention.
$script:EgLauncherExitCodes = [ordered]@{
    PreflightFailed           = 70
    InstallPreMutationFailed  = 71
    InstallRolledBack         = 72
    InstallRollbackIncomplete = 73
}

# --------------------------------------------------------------------------------------
# Deployable source set and transaction state (design sections 6.2 and 6.3)
# --------------------------------------------------------------------------------------

# The fixed recorded publication order (DD-10). The library is published BEFORE the entry
# script that dot-sources it, so a mid-transaction crash leaves an old entry script with a
# new library rather than a new entry script calling a missing library function. Rollback
# therefore walks this order in reverse.
$script:EgPublicationOrder = @('launcher_lib.ps1', 'launcher.ps1')

function Get-EgPublicationOrder {
    # The recorded publication order, which must be a permutation of the manifest member
    # set. The two constants are cross-checked here so they cannot drift apart.
    [CmdletBinding()]
    param()

    if (@($script:EgPublicationOrder).Count -ne @($script:EgManifestMemberNames).Count) {
        throw 'the publication order must cover exactly the manifest member set'
    }
    foreach ($name in $script:EgPublicationOrder) {
        $known = $false
        foreach ($candidate in $script:EgManifestMemberNames) {
            if ($candidate -ceq $name) { $known = $true }
        }
        if (-not $known) {
            throw 'the publication order must cover exactly the manifest member set'
        }
    }
    @($script:EgPublicationOrder)
}

function Get-EgDeployableSourceSet {
    # The ordered deployable source set, ENUMERATED EXPLICITLY from the recorded
    # publication order and never derived from a directory listing, so a file added to the
    # runtime directory later cannot become deployable by accident (design section 6.2,
    # asserted by EGRT-T47).
    #
    # The manifest is not in this set: it is generated during installation rather than
    # copied from source, and it is published in Phase 3.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RuntimeSourceDirectory)

    $ordered = @()
    foreach ($name in (Get-EgPublicationOrder)) {
        $ordered = $ordered + ([pscustomobject]@{
            Name       = $name
            SourcePath = (Join-Path $RuntimeSourceDirectory $name)
        })
    }
    return $ordered
}

function Get-EgDestinationPreimage {
    # Establish a destination's preimage state BEFORE anything is written. This
    # classification selects the publish primitive and is what makes the two failure
    # states of design section 7.2.1 decidable.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath)

    if (Test-Path -LiteralPath $DestinationPath -PathType Leaf) {
        $item = Get-Item -LiteralPath $DestinationPath
        return [pscustomobject]@{
            PreimageState  = 'Existing'
            PreimageSha256 = (Get-EgFileSha256 -Path $DestinationPath)
            ByteLength     = [int64]$item.Length
        }
    }
    return [pscustomobject]@{
        PreimageState  = 'Absent'
        PreimageSha256 = ''
        ByteLength     = [int64](-1)
    }
}

function New-EgTransactionState {
    # The transaction record Phase 4 and rollback both depend on. Members are appended in
    # publication order as they are prepared.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
        [string]$OperationId
    )

    [pscustomobject]@{
        OperationId = $OperationId
        Members     = @()
    }
}

function New-EgTransactionMember {
    # One member entry of the transaction record.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Name,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath,
        [Parameter(Mandatory)][ValidateSet('Existing', 'Absent')][string]$PreimageState,
        [AllowEmptyString()][string]$PreimageSha256 = '',
        [AllowEmptyString()][string]$StagingPath = '',
        [AllowEmptyString()][string]$BackupPath = '',
        [AllowEmptyString()][string]$SourceSha256 = ''
    )

    [pscustomobject]@{
        Name                = $Name
        DestinationPath     = $DestinationPath
        PreimageState       = $PreimageState
        PreimageSha256      = $PreimageSha256
        StagingPath         = $StagingPath
        BackupPath          = $BackupPath
        SourceSha256        = $SourceSha256
        PublicationOccurred = $false
        BackupCreated       = $false
    }
}

function Get-EgTouchedSet {
    # The member entries whose publication ADVANCED the destination, in REVERSE
    # publication order.
    #
    # Membership is decided by PublicationOccurred, NOT by which member failed. The member
    # whose failure ended the transaction is included only if it actually advanced: a Case
    # A failure (threw before publication) did not advance and is not restored, while a
    # Case B failure (returned, postimage verification failed) did advance and is the most
    # recent entry, so it is restored first.
    [CmdletBinding()]
    param([Parameter(Mandatory)]$TransactionState)

    $advanced = @()
    foreach ($entry in @($TransactionState.Members)) {
        if ($entry.PublicationOccurred) {
            $advanced = $advanced + $entry
        }
    }
    if (@($advanced).Count -le 1) {
        return $advanced
    }
    $reversed = @()
    for ($index = @($advanced).Count - 1; $index -ge 0; $index--) {
        $reversed = $reversed + $advanced[$index]
    }
    return $reversed
}

function Invoke-EgPackageRollback {
    # The SOLE rollback authority in this design (design section 6.5). Neither publish
    # primitive restores anything, so only this function undoes a transaction, and it
    # walks the touched set in reverse publication order.
    #
    # After the walk it re-verifies that the ENTIRE installed package equals its
    # pre-transaction state: every previously present member restored to its preimage hash,
    # and every member that was absent beforehand absent again.
    [CmdletBinding()]
    param([Parameter(Mandatory)]$TransactionState)

    $verified = $true
    $supportRef = ''

    foreach ($entry in @(Get-EgTouchedSet -TransactionState $TransactionState)) {
        if ($entry.PreimageState -ceq 'Existing') {
            # A destination that advanced with no preimage backup on disk is the one state
            # this design cannot restore. It is reported rather than left implicit.
            if (-not (Test-Path -LiteralPath $entry.BackupPath -PathType Leaf)) {
                $verified = $false
                $supportRef = 'EG_LAUNCHER_REPLACE_PREIMAGE_UNRECOVERABLE'
                continue
            }
            # Restore through the explicit-backup replacement semantics of section 7.2. The
            # advanced bytes are retained as recognised rollback residue, so the evidence
            # survives for inspection and the launcher's next preflight still passes.
            $rollbackResiduePath = Join-Path `
                ([System.IO.Path]::GetDirectoryName($entry.DestinationPath)) `
                (New-EgResidueName -Kind 'rollback' -Member $entry.Name `
                    -OperationId $TransactionState.OperationId)
            $restored = Invoke-AtomicFileReplace -SourcePath $entry.BackupPath `
                -DestinationPath $entry.DestinationPath -BackupPath $rollbackResiduePath `
                -ExpectedSha256 $entry.PreimageSha256
            if (-not $restored.Success) {
                $verified = $false
                if ([string]::IsNullOrEmpty($supportRef)) {
                    $supportRef = 'EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE'
                }
                continue
            }
            # A restoration that cannot be positively verified is not treated as successful.
            if ((Get-EgFileSha256 -Path $entry.DestinationPath) -cne $entry.PreimageSha256) {
                $verified = $false
                if ([string]::IsNullOrEmpty($supportRef)) {
                    $supportRef = 'EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE'
                }
            }
            continue
        }

        # Preimage Absent: remove exactly the destination THIS transaction created, then
        # positively confirm it is absent again. A destination whose current content no
        # longer matches what this transaction published means something else has taken
        # ownership, so it is never removed.
        if (-not (Test-Path -LiteralPath $entry.DestinationPath -PathType Leaf)) {
            continue
        }
        if ((Get-EgFileSha256 -Path $entry.DestinationPath) -cne $entry.SourceSha256) {
            $verified = $false
            if ([string]::IsNullOrEmpty($supportRef)) {
                $supportRef = 'EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE'
            }
            continue
        }
        try {
            Remove-Item -LiteralPath $entry.DestinationPath -Force
        }
        catch {
            $verified = $false
            if ([string]::IsNullOrEmpty($supportRef)) {
                $supportRef = 'EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE'
            }
            continue
        }
        if (Test-Path -LiteralPath $entry.DestinationPath -PathType Leaf) {
            $verified = $false
            if ([string]::IsNullOrEmpty($supportRef)) {
                $supportRef = 'EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE'
            }
        }
    }

    # Whole-package re-verification over every prepared member, advanced or not.
    foreach ($entry in @($TransactionState.Members)) {
        if ($entry.PreimageState -ceq 'Existing') {
            if ((Get-EgFileSha256 -Path $entry.DestinationPath) -cne $entry.PreimageSha256) {
                $verified = $false
                if ([string]::IsNullOrEmpty($supportRef)) {
                    $supportRef = 'EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE'
                }
            }
        }
        elseif (Test-Path -LiteralPath $entry.DestinationPath -PathType Leaf) {
            $verified = $false
            if ([string]::IsNullOrEmpty($supportRef)) {
                $supportRef = 'EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE'
            }
        }
    }

    if ($verified) {
        $supportRef = ''
    }
    elseif ([string]::IsNullOrEmpty($supportRef)) {
        $supportRef = 'EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE'
    }

    [pscustomobject]@{
        Verified   = $verified
        SupportRef = $supportRef
    }
}

function Invoke-EgPostAcceptanceBackupCleanup {
    # Reap the retained preimage backups of an ACCEPTED transaction (design section 6.6).
    #
    # Only a backup THIS transaction created is ever reaped, identified by the exact
    # reserved name recorded in the transaction state. A file this transaction did not
    # create is never deleted, including a validly named backup left by a different
    # transaction.
    #
    # This is the design's single sanctioned narrow exception to the fail-closed default,
    # and it is bounded exactly as written. If a delete fails the package is already
    # committed, verified, and correct on disk, so rolling it back over a now-redundant
    # artefact would replace a good outcome with a worse one. Instead the accepted
    # installation is kept, the undeleted backup is retained as inert residue that still
    # satisfies the Class B contract, and the caller exits 0.
    #
    # The failure is NEVER swallowed. Absence is positively verified after every delete
    # attempt, and whatever remains is counted and reported under a bounded,
    # operator-visible reference. A caught delete error is therefore not a silent
    # continue: it is converted into observed filesystem state that the caller reports.
    [CmdletBinding()]
    param([Parameter(Mandatory)]$TransactionState)

    $remaining = 0
    foreach ($entry in @($TransactionState.Members)) {
        if ([string]::IsNullOrEmpty($entry.BackupPath)) {
            continue
        }
        # The recorded path must be exactly the name this transaction would have produced.
        $expectedName = New-EgResidueName -Kind 'backup' -Member $entry.Name `
            -OperationId $TransactionState.OperationId
        if ([System.IO.Path]::GetFileName($entry.BackupPath) -cne $expectedName) {
            continue
        }
        if (-not (Test-Path -LiteralPath $entry.BackupPath -PathType Leaf)) {
            continue
        }
        try {
            Remove-Item -LiteralPath $entry.BackupPath -Force -ErrorAction Stop
        }
        catch {
            # Deliberately not rethrown; the outcome is decided by the absence check below.
        }
        if (Test-Path -LiteralPath $entry.BackupPath -PathType Leaf) {
            $remaining++
        }
    }

    $supportRef = ''
    if ($remaining -gt 0) {
        $supportRef = 'EG_LAUNCHER_INSTALL_BACKUP_CLEANUP_INCOMPLETE'
    }

    [pscustomobject]@{
        BackupsRemaining = [int]$remaining
        SupportRef       = $supportRef
    }
}

function New-EgOperationId {
    # The installer transaction identifier used in the Class B operation-id field. One per
    # installer invocation. -ValidateOnly generates none, because it creates no residue and
    # must stay byte-deterministic (DD-09).
    [CmdletBinding()]
    param()

    return ([guid]::NewGuid().ToString('D').ToLowerInvariant())
}

# --------------------------------------------------------------------------------------
# Installation-integrity manifest (design section 6.4)
# --------------------------------------------------------------------------------------
# Git is canonical for the manifest's SHAPE and for the comparison rules: which fields
# exist, how the hash is computed, and that a missing, unparsable, or non-matching manifest
# is terminal rather than a warning. The manifest RECORD is private deployment state,
# generated by the installer from reviewed source, and is never committed.
#
# Completeness is evaluated over the DEPLOYED PACKAGE-MEMBER DOMAIN ONLY, never over every
# file present in the launcher root. Unexpected files are caught by launcher-root
# classification instead. Splitting the two concerns is what lets the manifest stay an
# exact description of the deployed package while arbitrary extra files still fail closed.
#
# The manifest carries NO DATE-SHAPED FIELD. ConvertFrom-Json on PowerShell 7 coerces
# ISO-date-shaped strings to a date type while Windows PowerShell 5.1 keeps them as
# strings, so a date-shaped field would parse differently on the two editions.
$script:EgManifestFileName = 'installation_manifest.json'
$script:EgManifestSchema = 'eg_launcher_installation_manifest/v1'
$script:EgManifestMemberNames = @('launcher.ps1', 'launcher_lib.ps1')
$script:EgSha256Pattern = '^[0-9a-f]{64}$'
$script:EgCommitPattern = '^[0-9a-f]{40}$'

function Test-EgPathIsWithin {
    # Whether a candidate path resolves inside a container path. Comparison is on fully
    # resolved paths with a trailing separator, so a sibling whose name merely starts with
    # the container's name is not treated as being inside it.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CandidatePath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ContainerPath
    )

    $candidate = [System.IO.Path]::GetFullPath($CandidatePath).TrimEnd('\', '/')
    $container = [System.IO.Path]::GetFullPath($ContainerPath).TrimEnd('\', '/')
    if ([string]::Equals($candidate, $container, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $container + [System.IO.Path]::DirectorySeparatorChar
    return $candidate.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function ConvertTo-EgValidationJson {
    # The single deterministic validation output shape (design section 8). It carries no
    # timestamp and no generated identifier, so two consecutive runs against unchanged
    # state produce byte-identical standard output. support_ref is OMITTED on a pass.
    #
    # Nothing private reaches this surface: no path, no environment value, no credential
    # value, no account identity, no security identifier, no trustee or owner name, and no
    # Git output text.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Checks,
        [Parameter(Mandatory)][ValidateSet('PASS', 'FAIL')][string]$Status,
        [Parameter(Mandatory)][AllowEmptyString()][string]$SupportRef
    )

    $payload = [ordered]@{}
    $payload['checks'] = $Checks
    $payload['status'] = $Status
    if ($Status -cne 'PASS') {
        $payload['support_ref'] = $SupportRef
    }
    return ($payload | ConvertTo-Json -Depth 8 -Compress)
}

function Write-EgUtf8NoBomText {
    # The single sanctioned text write. Set-Content and Add-Content are never used, because
    # they default to the system ANSI code page on the Windows PowerShell 5.1 boundary.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Text
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Sort-EgOrdinalStringList {
    # Sort strings by ORDINAL comparison.
    #
    # Sort-Object uses culture-aware comparison, which orders punctuation differently
    # between PowerShell editions: 'launcher.ps1' and 'launcher_lib.ps1' swap places
    # between Windows PowerShell 5.1 and PowerShell 7. Manifest member order is part of the
    # serialised bytes, so a culture-dependent order would make identical input produce
    # different bytes on different editions and break the hash-idempotency contract that
    # installation determinism rests on.
    #
    # An explicit insertion sort over [string]::CompareOrdinal is used rather than a
    # comparer-based sort so the ordering is identical on both editions by construction and
    # depends on no collection type, culture, or cmdlet behaviour. The member set is two
    # entries, so the algorithm's cost is irrelevant.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Value)

    $items = @($Value)
    for ($index = 1; $index -lt $items.Count; $index++) {
        $current = $items[$index]
        $scan = $index - 1
        while ($scan -ge 0 -and [string]::CompareOrdinal([string]$items[$scan], $current) -gt 0) {
            $items[$scan + 1] = $items[$scan]
            $scan--
        }
        $items[$scan + 1] = $current
    }
    return $items
}

function Sort-EgOrdinalByName {
    # Sort member entries by their name field, ordinally. Same reasoning as above; this is
    # the ordering that reaches the serialised manifest.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()]$Entry)

    $items = @($Entry)
    for ($index = 1; $index -lt $items.Count; $index++) {
        $current = $items[$index]
        $scan = $index - 1
        while ($scan -ge 0 -and
               [string]::CompareOrdinal([string]$items[$scan].name, [string]$current.name) -gt 0) {
            $items[$scan + 1] = $items[$scan]
            $scan--
        }
        $items[$scan + 1] = $current
    }
    return $items
}

function New-EgInstallationManifestObject {
    # Build the manifest from admitted source hashes and the admitted commit. The members
    # array is sorted by name ascending and describes exactly the two EXECUTABLE members,
    # never the manifest itself and never residue.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$MemberEntries,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$AdmissionCommit
    )

    $sorted = @(Sort-EgOrdinalByName -Entry @($MemberEntries))
    $members = @()
    foreach ($entry in $sorted) {
        $members = $members + ([ordered]@{
            name        = [string]$entry.name
            sha256      = [string]$entry.sha256
            byte_length = [int]$entry.byte_length
        })
    }

    $manifest = [ordered]@{}
    $manifest['schema_version'] = $script:EgManifestSchema
    $manifest['admission_commit'] = $AdmissionCommit
    $manifest['members'] = @($members)
    return $manifest
}

function ConvertTo-EgManifestJson {
    # Deterministic serialisation: identical input yields identical bytes, which is what
    # makes installation idempotency by hash decidable.
    [CmdletBinding()]
    param([Parameter(Mandatory)]$ManifestObject)

    return ($ManifestObject | ConvertTo-Json -Depth 8)
}

function New-EgCheckResult {
    # Internal factory for the CheckResult shape consumed by preflight and validation.
    [CmdletBinding()]
    param(
        [bool]$Pass,
        [AllowEmptyString()][string]$SupportRef = '',
        [Parameter(Mandatory)]$Checks
    )

    [pscustomobject]@{
        Pass       = $Pass
        SupportRef = $SupportRef
        Checks     = $Checks
    }
}

function New-EgSingleCheckResult {
    # A CheckResult carrying exactly one named check.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckName,
        [bool]$Pass,
        [AllowEmptyString()][string]$SupportRef = ''
    )

    $checks = [ordered]@{}
    $outcome = 'FAIL'
    if ($Pass) { $outcome = 'PASS' }
    $checks[$CheckName] = $outcome

    $reportedRef = $SupportRef
    if ($Pass) { $reportedRef = '' }
    return (New-EgCheckResult -Pass $Pass -SupportRef $reportedRef -Checks $checks)
}

function Test-EgInstallationManifestShape {
    # Validate the manifest's shape rules. Every failure is EG_LAUNCHER_MANIFEST_MISMATCH.
    [CmdletBinding()]
    param([Parameter(Mandatory)]$ManifestObject)

    $checkName = 'installation_manifest_shape'
    $mismatch = 'EG_LAUNCHER_MANIFEST_MISMATCH'

    $schemaVersion = ''
    $admissionCommit = ''
    $members = @()
    try {
        $schemaVersion = [string]$ManifestObject.schema_version
        $admissionCommit = [string]$ManifestObject.admission_commit
        $members = @($ManifestObject.members)
    }
    catch {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
    }

    if ($schemaVersion -cne $script:EgManifestSchema) {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
    }
    if ($admissionCommit -cnotmatch $script:EgCommitPattern) {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
    }

    $observedNames = @()
    foreach ($member in $members) {
        $name = ''
        $sha256 = ''
        $byteLength = -1
        try {
            $name = [string]$member.name
            $sha256 = [string]$member.sha256
            $byteLength = [int]$member.byte_length
        }
        catch {
            return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
        }
        if ($sha256 -cnotmatch $script:EgSha256Pattern) {
            return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
        }
        if ($byteLength -lt 0) {
            return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
        }
        $observedNames = $observedNames + $name
    }

    # Ordinal on both sides, so the name-set comparison cannot depend on culture either.
    $expectedNames = @(Sort-EgOrdinalStringList -Value @($script:EgManifestMemberNames))
    $sortedObserved = @(Sort-EgOrdinalStringList -Value @($observedNames))
    if ($sortedObserved.Count -ne $expectedNames.Count) {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
    }
    for ($index = 0; $index -lt $expectedNames.Count; $index++) {
        if ($sortedObserved[$index] -cne $expectedNames[$index]) {
            return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
        }
    }

    return (New-EgSingleCheckResult -CheckName $checkName -Pass $true)
}

function Compare-EgInstalledPackageToManifest {
    # Read the manifest from the launcher root and compare it to the two installed
    # executable members, in both directions: a manifest entry with no corresponding
    # package member, and a package member with no manifest entry, each fail closed.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRootPath)

    $checkName = 'launcher_package_manifest_match'
    $manifestPath = Join-Path $LauncherRootPath $script:EgManifestFileName

    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
            -SupportRef 'EG_LAUNCHER_MANIFEST_MISSING')
    }

    $manifest = $null
    try {
        $raw = [System.IO.File]::ReadAllText($manifestPath)
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
                -SupportRef 'EG_LAUNCHER_MANIFEST_UNPARSABLE')
        }
        $manifest = $raw | ConvertFrom-Json
    }
    catch {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
            -SupportRef 'EG_LAUNCHER_MANIFEST_UNPARSABLE')
    }
    # The manifest document is defined as a single JSON OBJECT. A document that does not
    # parse to exactly one object is not a readable manifest at all, and is reported as
    # unparsable rather than as a content mismatch.
    #
    # This is decided from the parse RESULT rather than from whether ConvertFrom-Json threw,
    # because the two editions disagree about that: Windows PowerShell 5.1 throws on a
    # truncated document while PowerShell 7 can accept it and yield a value. Relying on the
    # throw alone therefore classified the same malformed file differently on each edition.
    # Checking the shape of the result gives one answer on both.
    #
    # The distinction is preserved in full and both outcomes stay terminal: a syntactically
    # unreadable document is UNPARSABLE, while a syntactically valid object whose fields do
    # not match the accepted shape or the installed bytes is MISMATCH.
    $parsed = @($manifest)
    if ($parsed.Count -ne 1) {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
            -SupportRef 'EG_LAUNCHER_MANIFEST_UNPARSABLE')
    }
    $manifest = $parsed[0]
    if ($null -eq $manifest) {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
            -SupportRef 'EG_LAUNCHER_MANIFEST_UNPARSABLE')
    }
    if ($manifest -isnot [System.Management.Automation.PSCustomObject]) {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
            -SupportRef 'EG_LAUNCHER_MANIFEST_UNPARSABLE')
    }

    $shape = Test-EgInstallationManifestShape -ManifestObject $manifest
    if (-not $shape.Pass) {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
            -SupportRef 'EG_LAUNCHER_MANIFEST_MISMATCH')
    }

    $mismatch = 'EG_LAUNCHER_MANIFEST_MISMATCH'
    $describedNames = @()
    foreach ($entry in @($manifest.members)) {
        $describedNames = $describedNames + ([string]$entry.name)
        $memberPath = Join-Path $LauncherRootPath ([string]$entry.name)
        if (-not (Test-Path -LiteralPath $memberPath -PathType Leaf)) {
            return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
        }
        if ((Get-EgFileSha256 -Path $memberPath) -cne ([string]$entry.sha256)) {
            return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
        }
        $observedLength = (Get-Item -LiteralPath $memberPath).Length
        if ([int64]$observedLength -ne [int64]$entry.byte_length) {
            return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
        }
    }

    # The other direction: an installed executable member the manifest does not describe.
    foreach ($expected in $script:EgManifestMemberNames) {
        $described = $false
        foreach ($name in $describedNames) {
            if ($name -ceq $expected) { $described = $true }
        }
        if (-not $described) {
            return (New-EgSingleCheckResult -CheckName $checkName -Pass $false -SupportRef $mismatch)
        }
    }

    return (New-EgSingleCheckResult -CheckName $checkName -Pass $true)
}

# --------------------------------------------------------------------------------------
# Hashing and bounded exception classification
# --------------------------------------------------------------------------------------

function Get-EgFileSha256 {
    # Lowercase hexadecimal SHA-256 of a file, or the empty string when the path does not
    # exist as a file. Every hash in this library is normalised the same way, so a hash
    # comparison never depends on the casing a provider happened to return.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ''
    }
    try {
        # Hashed through .NET directly rather than through Get-FileHash. Hashing is on the
        # critical path of every integrity, idempotency, and rollback decision, so it must
        # not depend on a module being autoloadable: a host whose module path does not
        # resolve the Windows PowerShell module directory would otherwise turn every hash
        # into the empty string and every verification into a failure.
        $algorithm = [System.Security.Cryptography.SHA256]::Create()
        try {
            $stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::Read)
            try {
                $digest = $algorithm.ComputeHash($stream)
            }
            finally {
                $stream.Dispose()
            }
        }
        finally {
            $algorithm.Dispose()
        }
        return ([System.BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant())
    }
    catch {
        # The file exists but could not be opened for reading, which is what a destination
        # held with an exclusive share looks like. This function must not throw, because
        # both publish primitives are contractually required to RETURN a structured result
        # rather than raise (design section 7.1), and a primitive that threw here would
        # never surface the real File.Replace exception type and HRESULT.
        #
        # The empty string is a fail-closed signal, not a swallowed failure: it can never
        # equal an expected 64-character hash, so every verification that consumes it
        # fails. It is also never mistaken for a matching preimage, because an unreadable
        # preimage compares equal only to an equally unreadable postimage, which is
        # exactly the case where the destination provably did not advance.
        return ''
    }
}

function Get-EgUnderlyingException {
    # Unwrap the exception PowerShell wraps around a failed .NET method invocation, so
    # classification sees the real type and HRESULT rather than the wrapper's.
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Exception]$Exception)

    $candidate = $Exception
    while ($candidate -is [System.Management.Automation.MethodInvocationException]) {
        if ($null -eq $candidate.InnerException) {
            break
        }
        $candidate = $candidate.InnerException
    }
    return $candidate
}

function Get-EgExceptionHResultString {
    # The HRESULT in exact 0x%08X form. Never any message text (design section 11.2).
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Exception]$Exception)

    return [string]::Format('0x{0:X8}', $Exception.HResult)
}

function Get-EgReplaceSupportRef {
    # Map a replacement failure to its bounded support reference by exception TYPE and
    # HRESULT ONLY. Exception message text is never read, and no control-flow decision
    # anywhere in this library is made by reading it (design section 7.2 rule 5).
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Exception]$Exception)

    if ($Exception -is [System.UnauthorizedAccessException]) {
        return 'EG_LAUNCHER_REPLACE_ACCESS_DENIED'
    }
    if ($Exception -is [System.ArgumentException]) {
        return 'EG_LAUNCHER_REPLACE_ARGUMENT_INVALID'
    }
    if ($Exception -is [System.IO.IOException]) {
        if ((Get-EgExceptionHResultString -Exception $Exception) -eq '0x80070020') {
            return 'EG_LAUNCHER_REPLACE_SHARING_VIOLATION'
        }
    }
    return 'EG_LAUNCHER_UNCLASSIFIED'
}

# --------------------------------------------------------------------------------------
# Installer staging phase vocabulary (design sections 11.1 and 17.3)
# --------------------------------------------------------------------------------------
# Section 7.2 rule 5 requires every failure to record the exception type name and the
# HRESULT mapped to a bounded support reference, and section 17.3 states that the support
# reference, the FAILING PHASE, the type name, and the HRESULT together distinguish every
# failure class. Phase 1 staging has five distinguishable substeps that previously all
# collapsed into one support reference, which is exactly the evidence-dead-end this
# vocabulary removes.
#
# The vocabulary is BOUNDED and CLOSED, like the support references. Every value is a
# public phase label: it carries no path, no file name, no operation identifier, and no
# identity. The empty string means no staging phase failed.
$script:EgInstallerStagingPhases = @(
    'staging_write_executable',
    'staging_hash_executable',
    'staging_parse_executable',
    'staging_write_manifest',
    'staging_hash_manifest'
)

function Get-EgInstallerStagingPhases {
    # The bounded staging phase vocabulary, enumerated explicitly.
    [CmdletBinding()]
    param()

    @($script:EgInstallerStagingPhases)
}

function Test-EgInstallerStagingPhase {
    # Exact, case-sensitive membership. An unrecognised phase is never treated as live.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Phase)

    foreach ($known in $script:EgInstallerStagingPhases) {
        if ($known -ceq $Phase) {
            return $true
        }
    }
    return $false
}

function Get-EgStagingWriteSupportRef {
    # Map a staging WRITE failure to its bounded support reference by exception TYPE and
    # HRESULT ONLY. Exception message text is never read, exactly as Get-EgReplaceSupportRef
    # does for replacement, and no control-flow decision is made by reading it.
    #
    # This deliberately adds NO new vocabulary. Every write failure recognised here is a
    # staging failure and keeps the existing bounded reference; the class that distinguishes
    # one cause from another is carried by the exception type name and the HRESULT, which is
    # what section 17.3 already requires. Minting a per-cause reference would fabricate
    # causal specificity that the type and HRESULT already express exactly.
    #
    # An unrecognised class records EG_LAUNCHER_UNCLASSIFIED rather than being absorbed into
    # the staging reference, so a genuinely unexpected failure stays visible as unexpected
    # while still surfacing only its type and HRESULT.
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Exception]$Exception)

    if ($Exception -is [System.UnauthorizedAccessException]) {
        return 'EG_LAUNCHER_INSTALL_STAGING_FAILED'
    }
    if ($Exception -is [System.Security.SecurityException]) {
        return 'EG_LAUNCHER_INSTALL_STAGING_FAILED'
    }
    if ($Exception -is [System.NotSupportedException]) {
        return 'EG_LAUNCHER_INSTALL_STAGING_FAILED'
    }
    if ($Exception -is [System.ArgumentException]) {
        return 'EG_LAUNCHER_INSTALL_STAGING_FAILED'
    }
    # PathTooLongException, DirectoryNotFoundException, FileNotFoundException and the
    # sharing-violation class all derive from IOException, so the one test covers them.
    if ($Exception -is [System.IO.IOException]) {
        return 'EG_LAUNCHER_INSTALL_STAGING_FAILED'
    }
    return 'EG_LAUNCHER_UNCLASSIFIED'
}

# --------------------------------------------------------------------------------------
# Publication result shape (design section 7.1)
# --------------------------------------------------------------------------------------

function New-EgPublicationResult {
    # Internal factory. Guarantees all ten PublicationResult fields are ALWAYS present,
    # in a fixed order, on every path including failure. ExceptionTypeName and HResult are
    # the empty string when no exception occurred; there is no RolledBack field, because
    # neither publish primitive ever rolls back.
    [CmdletBinding()]
    param(
        [bool]$Success,
        [AllowEmptyString()][string]$SupportRef = '',
        [AllowEmptyString()][string]$ExceptionTypeName = '',
        [AllowEmptyString()][string]$HResult = '',
        [Parameter(Mandatory)][ValidateSet('Existing', 'Absent')][string]$PreimageState,
        [AllowEmptyString()][string]$PreimageSha256 = '',
        [AllowEmptyString()][string]$PostimageSha256 = '',
        [bool]$PublicationOccurred,
        [bool]$BackupCreated,
        [bool]$BackupRetained
    )

    [pscustomobject]@{
        Success             = $Success
        SupportRef          = $SupportRef
        ExceptionTypeName   = $ExceptionTypeName
        HResult             = $HResult
        PreimageState       = $PreimageState
        PreimageSha256      = $PreimageSha256
        PostimageSha256     = $PostimageSha256
        PublicationOccurred = $PublicationOccurred
        BackupCreated       = $BackupCreated
        BackupRetained      = $BackupRetained
    }
}

function Test-EgSameDirectory {
    # Whether two paths resolve to the same containing directory. The same-directory rule
    # is stronger than the same-volume requirement ReplaceFileW imposes, and is enforced
    # because it is mechanically checkable (design section 7.2 rule 1).
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$FirstPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SecondPath
    )

    $firstDirectory = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($FirstPath))
    $secondDirectory = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($SecondPath))
    return ([string]::Equals($firstDirectory, $secondDirectory, [System.StringComparison]::OrdinalIgnoreCase))
}

function Invoke-AtomicFileReplace {
    # Publish a staging file over an EXISTING destination, through
    # [System.IO.File]::Replace with a mandatory explicit same-directory backup path.
    #
    # This primitive NEVER rolls back and NEVER deletes a backup. It publishes and
    # reports; restoring a preimage is the installer transaction's exclusive
    # responsibility (design sections 6.5 and 7.2 rules 3 and 4), because only the
    # installer knows which other package members have already advanced and in what order
    # they must be undone.
    #
    # The mandatory, non-empty -BackupPath is the structural closure of the Run119
    # null-backup defect (design section 18.2). The prohibition does not rely on any
    # runtime rejecting a null argument, because the parameter contract refuses it first.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SourcePath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$BackupPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedSha256
    )

    # Step 1. A backup path outside the destination directory is refused before any call
    # reaches the filesystem, so the destination is byte-identical afterwards.
    if (-not (Test-EgSameDirectory -FirstPath $BackupPath -SecondPath $DestinationPath)) {
        return New-EgPublicationResult -Success $false `
            -SupportRef 'EG_LAUNCHER_REPLACE_ARGUMENT_INVALID' `
            -PreimageState 'Existing' `
            -PreimageSha256 (Get-EgFileSha256 -Path $DestinationPath) `
            -PostimageSha256 (Get-EgFileSha256 -Path $DestinationPath) `
            -PublicationOccurred $false -BackupCreated $false -BackupRetained $false
    }

    # Step 2. The preimage hash is recorded before anything is attempted. It is what makes
    # the two failure states of design section 7.2.1 decidable from observed state.
    $preimageSha = Get-EgFileSha256 -Path $DestinationPath

    $publicationOccurred = $false
    $exceptionTypeName = ''
    $hresult = ''
    $supportRef = ''
    $threw = $false

    try {
        [System.IO.File]::Replace($SourcePath, $DestinationPath, $BackupPath)
        # Set immediately after the call returns and before any verification work.
        $publicationOccurred = $true
    }
    catch {
        $threw = $true
        $underlying = Get-EgUnderlyingException -Exception $_.Exception
        $exceptionTypeName = $underlying.GetType().FullName
        $hresult = Get-EgExceptionHResultString -Exception $underlying
        $supportRef = Get-EgReplaceSupportRef -Exception $underlying
        # Design section 7.2.1 determination: the call threw, so the destination advanced
        # only if its observed hash no longer equals the recorded preimage.
        if ((Get-EgFileSha256 -Path $DestinationPath) -ne $preimageSha) {
            $publicationOccurred = $true
        }
    }

    # Observed on disk, never inferred from the requested path: supplying a backup path is
    # not evidence that a backup file exists.
    $backupCreated = (Test-Path -LiteralPath $BackupPath -PathType Leaf)
    $postimageSha = Get-EgFileSha256 -Path $DestinationPath

    if (-not $threw) {
        # Step 6. Success is positively verified, never assumed. A returned call is not a
        # successful replacement until the destination hash matches.
        if ($postimageSha -ceq $ExpectedSha256) {
            return New-EgPublicationResult -Success $true `
                -PreimageState 'Existing' -PreimageSha256 $preimageSha `
                -PostimageSha256 $postimageSha -PublicationOccurred $true `
                -BackupCreated $backupCreated -BackupRetained $backupCreated
        }
        # Case B: the destination advanced and verification failed. The backup is retained
        # and the primitive performs no self-rollback.
        return New-EgPublicationResult -Success $false `
            -SupportRef 'EG_LAUNCHER_REPLACE_POSTIMAGE_MISMATCH' `
            -PreimageState 'Existing' -PreimageSha256 $preimageSha `
            -PostimageSha256 $postimageSha -PublicationOccurred $true `
            -BackupCreated $backupCreated -BackupRetained $backupCreated
    }

    return New-EgPublicationResult -Success $false -SupportRef $supportRef `
        -ExceptionTypeName $exceptionTypeName -HResult $hresult `
        -PreimageState 'Existing' -PreimageSha256 $preimageSha `
        -PostimageSha256 $postimageSha -PublicationOccurred $publicationOccurred `
        -BackupCreated $backupCreated -BackupRetained $backupCreated
}

function Test-EgPowerShellFileParsesCleanly {
    # Whether a .ps1 file parses with zero errors. Read-only; the file is never executed,
    # dot-sourced, or imported by this check.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $parseErrors = $null
    $parseTokens = $null
    try {
        [void][System.Management.Automation.Language.Parser]::ParseFile(
            $Path, [ref]$parseTokens, [ref]$parseErrors)
    }
    catch {
        return $false
    }
    if ($null -eq $parseErrors) {
        return $true
    }
    return (@($parseErrors).Count -eq 0)
}

function Invoke-EgNoReplaceMove {
    # Same-directory move that will NOT clobber. Returns the observed native outcome; the
    # caller decides what a failure means.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SourcePath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath
    )

    Initialize-EgNativePublicationInterop
    $native = [EgRuntime.NativePublication]::MoveNoReplaceWriteThrough($SourcePath, $DestinationPath)
    [pscustomobject]@{
        Ok        = $native.Ok
        LastError = [int]$native.LastError
    }
}

function Invoke-PublishToAbsentDestination {
    # Publish a staging file to a destination whose ABSENCE has been positively
    # established. This is the clean-first-install path and it never calls
    # [System.IO.File]::Replace, which requires the destination to already exist.
    #
    # There is no backup, because there was nothing to back up. This primitive never rolls
    # back: undoing a publication is the installer transaction's responsibility, and means
    # returning the destination to absence (design section 7.4 step 6).
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SourcePath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedSha256
    )

    # Step 1. Absence is established first, not assumed. An unexpectedly present
    # destination file means the caller's preimage classification was wrong, and that is
    # terminal rather than something to recover from by overwriting.
    if (Test-Path -LiteralPath $DestinationPath -PathType Leaf) {
        return New-EgPublicationResult -Success $false `
            -SupportRef 'EG_LAUNCHER_PUBLISH_DESTINATION_UNEXPECTEDLY_PRESENT' `
            -PreimageState 'Absent' `
            -PostimageSha256 (Get-EgFileSha256 -Path $DestinationPath) `
            -PublicationOccurred $false -BackupCreated $false -BackupRetained $false
    }

    # Step 2. The staging file is complete before publication: it exists, parses cleanly
    # when it is a PowerShell file, and hashes to the expected value. Nothing partially
    # written is ever published. A staging file that fails any of these is a staging
    # failure, reported as such rather than attempted.
    $stagingOk = (Test-Path -LiteralPath $SourcePath -PathType Leaf)
    if ($stagingOk) {
        if ([System.IO.Path]::GetExtension($SourcePath) -ieq '.ps1') {
            $stagingOk = (Test-EgPowerShellFileParsesCleanly -Path $SourcePath)
        }
    }
    if ($stagingOk) {
        $stagingOk = ((Get-EgFileSha256 -Path $SourcePath) -ceq $ExpectedSha256)
    }
    if (-not $stagingOk) {
        return New-EgPublicationResult -Success $false `
            -SupportRef 'EG_LAUNCHER_INSTALL_STAGING_FAILED' `
            -PreimageState 'Absent' `
            -PublicationOccurred $false -BackupCreated $false -BackupRetained $false
    }

    # Step 3. A same-directory move that will not clobber. A losing race is reported,
    # never resolved by replacing.
    $moved = Invoke-EgNoReplaceMove -SourcePath $SourcePath -DestinationPath $DestinationPath
    if (-not $moved.Ok) {
        if (Test-Path -LiteralPath $DestinationPath) {
            return New-EgPublicationResult -Success $false `
                -SupportRef 'EG_LAUNCHER_PUBLISH_RACE_LOST' `
                -PreimageState 'Absent' `
                -PostimageSha256 (Get-EgFileSha256 -Path $DestinationPath) `
                -PublicationOccurred $false -BackupCreated $false -BackupRetained $false
        }
        return New-EgPublicationResult -Success $false `
            -SupportRef 'EG_LAUNCHER_PUBLISH_POSTIMAGE_MISMATCH' `
            -PreimageState 'Absent' `
            -PublicationOccurred $false -BackupCreated $false -BackupRetained $false
    }

    # Step 4. The postimage is verified. Publication is not treated as successful until
    # the destination hash matches.
    $postimageSha = Get-EgFileSha256 -Path $DestinationPath
    if ($postimageSha -ceq $ExpectedSha256) {
        return New-EgPublicationResult -Success $true `
            -PreimageState 'Absent' -PostimageSha256 $postimageSha `
            -PublicationOccurred $true -BackupCreated $false -BackupRetained $false
    }
    return New-EgPublicationResult -Success $false `
        -SupportRef 'EG_LAUNCHER_PUBLISH_POSTIMAGE_MISMATCH' `
        -PreimageState 'Absent' -PostimageSha256 $postimageSha `
        -PublicationOccurred $true -BackupCreated $false -BackupRetained $false
}

# --------------------------------------------------------------------------------------
# Process-scope environment snapshot and exact restoration
# --------------------------------------------------------------------------------------

# The two credential variable names the application reads from its environment. Declared
# once here because Restore-EgProcessEnvironmentSnapshot needs them to select its bounded
# support reference; the credential import path consumes the same constant.
$script:EgCredentialVariableNames = @('ENERGYGRID_USERNAME', 'ENERGYGRID_PASSWORD')

function Get-EgProcessEnvironmentSnapshot {
    # Record the exact process-scope state of the named variables.
    #
    # Present is $false for a variable that does not exist, which is a DIFFERENT state
    # from a variable that exists and holds an empty string. Restoration depends on the
    # distinction (design sections 9.2 and 10.2).
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Names)

    $snapshot = [ordered]@{}
    foreach ($name in $Names) {
        $observed = [System.Environment]::GetEnvironmentVariable($name, 'Process')
        $snapshot[$name] = [pscustomobject]@{
            Present = ($null -ne $observed)
            Value   = ([string]$observed)
        }
    }
    return $snapshot
}

function Restore-EgProcessEnvironmentSnapshot {
    # Restore a snapshot EXACTLY. A name recorded Present $false is REMOVED, never set to
    # an empty string. A failed restoration is terminal rather than silent.
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Snapshot)

    $checks = [ordered]@{}
    $pass = $true
    $supportRef = ''

    $names = @($Snapshot.Keys)
    foreach ($name in $names) {
        $entry = $Snapshot[$name]
        $desired = $null
        if ($entry.Present) {
            $desired = $entry.Value
        }
        try {
            Set-EgProcessEnvironmentVariable -Name $name -Value $desired
            $observed = [System.Environment]::GetEnvironmentVariable($name, 'Process')
            if ($entry.Present) {
                if ($null -eq $observed -or $observed -ne $entry.Value) {
                    $pass = $false
                }
            }
            else {
                # Absence is verified through BOTH the framework getter and the environment
                # provider. A variable left present with an empty value can read back as
                # $null through the getter while still occupying the process environment
                # block that child processes inherit, and an empty GIT_DIR is not the same
                # thing as an absent one: it breaks every subsequent Git invocation.
                if ($null -ne $observed) {
                    $pass = $false
                }
                elseif (Test-Path -LiteralPath ('Env:\' + $name)) {
                    $pass = $false
                }
            }
        }
        catch {
            $pass = $false
        }
    }

    if (-not $pass) {
        $supportRef = 'EG_LAUNCHER_UNCLASSIFIED'
        foreach ($credentialName in $script:EgCredentialVariableNames) {
            if ($names -contains $credentialName) {
                $supportRef = 'EG_LAUNCHER_CREDENTIAL_RESTORE_FAILED'
            }
        }
    }

    $outcome = 'FAIL'
    if ($pass) { $outcome = 'PASS' }
    $checks['process_environment_restored'] = $outcome

    [pscustomobject]@{
        Pass       = $pass
        SupportRef = $supportRef
        Checks     = $checks
    }
}

# --------------------------------------------------------------------------------------
# Governed Git invocation (design sections 10.1, 10.2, 10.3)
# --------------------------------------------------------------------------------------

function Set-EgProcessEnvironmentVariable {
    # PROCESS SCOPE ONLY. The User and Machine scopes are never used anywhere in the
    # committed runtime, so no persistent EnergyGrid credential variable is ever created on
    # the host and the existing absence of those persistent variables is preserved
    # (EGRT-T25). A $null value REMOVES the variable rather than setting it to an empty
    # string, which the exact-restoration contract in design sections 9.2 and 10.2 depends
    # on: a variable that was absent before a call must be absent after it, not present and
    # empty.
    #
    # $Value is deliberately UNTYPED. Declaring it [string] lets the parameter binder
    # convert $null into an empty string on some PowerShell editions, which silently turns
    # "remove this variable" into "set it to empty" and breaks that contract. This was
    # observed as a real divergence: Windows PowerShell 5.1 preserved the null while
    # PowerShell 7 did not, so every neutralised ambient variable came back present and
    # empty on a PowerShell 7 host.
    #
    # Removal is then completed through the environment provider when the framework call
    # has not produced absence. That is not a fallback that hides a failure: absence is the
    # contracted post-condition, it is checked here, and every caller independently verifies
    # the resulting state and reports a bounded failure reference when it is wrong.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Name,
        [Parameter(Mandatory)][AllowEmptyString()][AllowNull()]$Value
    )

    if ($null -eq $Value) {
        [System.Environment]::SetEnvironmentVariable($Name, $null, 'Process')
        # Checked through the PROVIDER unconditionally. A variable left present with an
        # empty value reads back as $null through the framework getter while still
        # occupying the process environment block a child inherits, so gating this on the
        # getter would leave exactly the state the contract forbids.
        $providerPath = 'Env:\' + $Name
        if (Test-Path -LiteralPath $providerPath) {
            Remove-Item -LiteralPath $providerPath -Force
        }
        return
    }

    [System.Environment]::SetEnvironmentVariable($Name, [string]$Value, 'Process')
}

function Invoke-EgWithInjectedProcessEnvironment {
    # Snapshot the named variables, set them at PROCESS SCOPE ONLY for the bounded body,
    # then restore the exact prior process state on a finally-equivalent path reached
    # whether the body succeeded, failed, or never started.
    #
    # This is the single implementation of the injection sequence, so the launcher entry
    # script and the offline tests exercise the same code rather than two copies of it.
    #
    # Values passed in may be credential-derived. They exist in memory only: this function
    # never writes them to disk, never logs them, never places them in the returned object,
    # and never derives any reportable quantity from them.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Variables,
        [Parameter(Mandatory)][scriptblock]$Body
    )

    $names = @($Variables.Keys)
    $snapshot = Get-EgProcessEnvironmentSnapshot -Names $names
    $bodyResult = $null
    $restore = $null
    try {
        foreach ($name in $names) {
            Set-EgProcessEnvironmentVariable -Name $name -Value $Variables[$name]
        }
        $bodyResult = & $Body
    }
    finally {
        $restore = Restore-EgProcessEnvironmentSnapshot -Snapshot $snapshot
    }

    [pscustomobject]@{
        BodyResult = $bodyResult
        Restore    = $restore
    }
}

# --------------------------------------------------------------------------------------
# Launcher preflight predicates (design sections 5.2, 11.3, 17.2)
# --------------------------------------------------------------------------------------

# The application's own exit codes. The runtime band 70 to 73 is disjoint from these, so a
# launcher or installer failure can never be mistaken for an application status.
$script:EgApplicationExitCodes = @(0, 10, 20, 64)

# The configuration keys the launcher checks for presence and non-emptiness (DD-05). The
# launcher does NOT re-validate the application's own path rules: the application already
# enforces them, and duplicating them here would create two sources of truth.
$script:EgRequiredConfigKeys = @(
    'portal_url', 'account_identity', 'archive_root', 'state_path', 'temp_root', 'log_root'
)

$script:EgPythonVersionPattern = '^Python 3\.14\.'
$script:EgTerminalEventFileName = 'launcher_failed.jsonl'

function Invoke-EgReadOnlyProcessProbe {
    # Run a read-only external probe and capture both streams. Used only for the
    # interpreter version probe, which is the sole non-Git child a validation run starts.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$FilePath,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Arguments
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = ConvertTo-EgNativeArgumentString -Argument $Arguments
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true

    $exitCode = -1
    $combined = ''
    $process = $null
    try {
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        [void]$process.Start()
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $combined = [string]$outputTask.Result + [string]$errorTask.Result
        $exitCode = $process.ExitCode
    }
    catch {
        return [pscustomobject]@{ Started = $false; ExitCode = -1; Output = '' }
    }
    finally {
        if ($null -ne $process) { $process.Dispose() }
    }

    [pscustomobject]@{
        Started  = $true
        ExitCode = [int]$exitCode
        Output   = [string]$combined
    }
}

function Test-EgPythonVersionSupported {
    # Read-only interpreter version probe. Returns a CheckResult named
    # python_version_is_3_14. No interpreter output text reaches any surface.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$PythonExe)

    $checkName = 'python_version_is_3_14'
    $probe = Invoke-EgReadOnlyProcessProbe -FilePath $PythonExe -Arguments @('--version')
    if (-not $probe.Started) {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
            -SupportRef 'EG_LAUNCHER_PYTHON_VERSION_UNSUPPORTED')
    }
    if ($probe.ExitCode -ne 0) {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
            -SupportRef 'EG_LAUNCHER_PYTHON_VERSION_UNSUPPORTED')
    }
    foreach ($line in @(Split-EgProcessOutputLines -Text $probe.Output)) {
        if ($line.Trim() -match $script:EgPythonVersionPattern) {
            return (New-EgSingleCheckResult -CheckName $checkName -Pass $true)
        }
    }
    return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
        -SupportRef 'EG_LAUNCHER_PYTHON_VERSION_UNSUPPORTED')
}

function Test-EgLauncherConfigContract {
    # The private configuration must resolve OUTSIDE the deployed checkout, parse as JSON,
    # and carry every key the application requires. No configuration VALUE is ever emitted,
    # logged, or placed in a result object.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ConfigPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRootPath
    )

    $checks = [ordered]@{}
    $checks['config_path_outside_checkout'] = 'FAIL'
    $checks['config_parses_json'] = 'FAIL'
    $checks['config_required_keys_present'] = 'FAIL'

    if (Test-EgPathIsWithin -CandidatePath $ConfigPath -ContainerPath $CheckoutRootPath) {
        return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_CONFIG_INSIDE_CHECKOUT' -Checks $checks)
    }
    $checks['config_path_outside_checkout'] = 'PASS'

    $parsed = $null
    try {
        $raw = [System.IO.File]::ReadAllText($ConfigPath)
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_CONFIG_UNPARSABLE' -Checks $checks)
        }
        $parsed = $raw | ConvertFrom-Json
    }
    catch {
        return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_CONFIG_UNPARSABLE' -Checks $checks)
    }
    if ($null -eq $parsed) {
        return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_CONFIG_UNPARSABLE' -Checks $checks)
    }
    $checks['config_parses_json'] = 'PASS'

    $propertyNames = @()
    foreach ($property in @($parsed.PSObject.Properties)) {
        $propertyNames = $propertyNames + $property.Name
    }
    foreach ($required in $script:EgRequiredConfigKeys) {
        $present = $false
        foreach ($name in $propertyNames) {
            if ($name -ceq $required) { $present = $true }
        }
        if (-not $present) {
            return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_CONFIG_KEY_MISSING' -Checks $checks)
        }
        $value = $parsed.$required
        if ($null -eq $value) {
            return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_CONFIG_KEY_MISSING' -Checks $checks)
        }
        if ([string]::IsNullOrWhiteSpace([string]$value)) {
            return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_CONFIG_KEY_MISSING' -Checks $checks)
        }
    }
    $checks['config_required_keys_present'] = 'PASS'

    return (New-EgCheckResult -Pass $true -SupportRef '' -Checks $checks)
}

function Test-EgLauncherRootSecurity {
    # Ordered preflight positions 14 to 18. The examined object set is the launcher root
    # plus every Class A member joined to it (DD-01), and BOTH write checks are evaluated
    # against every member of that set: one failing object fails the check.
    #
    # launcher_root_entries_classified is deliberately NOT re-evaluated here. It is
    # computed once, at preflight position 1 (DD-04).
    #
    # No principal name, identifier, owner identity, path, or count derived from them is
    # ever emitted. Only the check name and the bounded support reference reach any surface.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRootPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRootPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string[]]$AuthorisedLauncherRootWriteSid
    )

    $checks = [ordered]@{}
    $checks['launcher_root_outside_checkout'] = 'FAIL'
    $checks['launcher_root_not_writable_by_run_principal'] = 'FAIL'
    $checks['launcher_root_write_trustees_authorised'] = 'FAIL'
    $checks['launcher_files_not_reparse_points'] = 'FAIL'
    $checks['launcher_files_not_unexpectedly_readonly'] = 'FAIL'

    if (Test-EgPathIsWithin -CandidatePath $LauncherRootPath -ContainerPath $CheckoutRootPath) {
        return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_ROOT_INSIDE_CHECKOUT' -Checks $checks)
    }
    $checks['launcher_root_outside_checkout'] = 'PASS'

    $examined = @($LauncherRootPath)
    foreach ($memberName in (Get-EgDeployedPackageMemberNames)) {
        $examined = $examined + (Join-Path $LauncherRootPath $memberName)
    }

    # Position 15. The privilege read happens ONCE. A failed read is terminal under this
    # same check and reference: the run does not continue to the privilege predicate or to
    # any remaining access check, and the empty name list is never read as absence of a
    # bypass privilege.
    $privileges = Get-EgTokenPrivilegeNames
    if (-not $privileges.ReadOk) {
        return (New-EgCheckResult -Pass $false `
            -SupportRef 'EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE' -Checks $checks)
    }
    if (Test-EgBypassPrivilegePresent -PrivilegeName $privileges.PrivilegeNames) {
        return (New-EgCheckResult -Pass $false `
            -SupportRef 'EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE' -Checks $checks)
    }
    foreach ($objectPath in $examined) {
        $tokenCheck = Test-EgTokenWriteAccessToPath -Path $objectPath
        if (-not $tokenCheck.Evaluated) {
            return (New-EgCheckResult -Pass $false `
                -SupportRef 'EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE' -Checks $checks)
        }
        if ($tokenCheck.AnyWriteGranted) {
            return (New-EgCheckResult -Pass $false `
                -SupportRef 'EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE' -Checks $checks)
        }
    }
    $checks['launcher_root_not_writable_by_run_principal'] = 'PASS'

    # Position 16. The supplied set is admitted once, and a refused set is reported as a
    # failure of this same check under its own bounded reference (DD-12).
    $admission = Test-EgAuthorisedWriteSidSet -AuthorisedSid $AuthorisedLauncherRootWriteSid
    if (-not $admission.Pass) {
        return (New-EgCheckResult -Pass $false `
            -SupportRef 'EG_LAUNCHER_ROOT_AUTHORISED_SID_SET_INVALID' -Checks $checks)
    }
    foreach ($objectPath in $examined) {
        $trusteeCheck = Test-EgPathWriteTrusteesAuthorised -Path $objectPath `
            -AuthorisedSid $admission.Sids
        if (-not $trusteeCheck.Evaluated) {
            return (New-EgCheckResult -Pass $false `
                -SupportRef 'EG_LAUNCHER_ROOT_ACL_WRITE_TRUSTEE_UNAUTHORISED' -Checks $checks)
        }
        if (-not $trusteeCheck.Authorised) {
            return (New-EgCheckResult -Pass $false `
                -SupportRef 'EG_LAUNCHER_ROOT_ACL_WRITE_TRUSTEE_UNAUTHORISED' -Checks $checks)
        }
    }
    $checks['launcher_root_write_trustees_authorised'] = 'PASS'

    # Position 17. Neither installed file, nor the destination directory, is a symlink,
    # junction, or other reparse point.
    foreach ($objectPath in $examined) {
        if (-not (Test-Path -LiteralPath $objectPath)) {
            return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_ROOT_REPARSE_POINT' -Checks $checks)
        }
        $item = $null
        try {
            $item = Get-Item -LiteralPath $objectPath -Force
        }
        catch {
            return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_ROOT_REPARSE_POINT' -Checks $checks)
        }
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_ROOT_REPARSE_POINT' -Checks $checks)
        }
    }
    $checks['launcher_files_not_reparse_points'] = 'PASS'

    # Position 18. No installed file carries an unexplained read-only attribute.
    foreach ($memberName in (Get-EgDeployedPackageMemberNames)) {
        $memberPath = Join-Path $LauncherRootPath $memberName
        $item = $null
        try {
            $item = Get-Item -LiteralPath $memberPath -Force
        }
        catch {
            return (New-EgCheckResult -Pass $false `
                -SupportRef 'EG_LAUNCHER_FILE_UNEXPECTEDLY_READONLY' -Checks $checks)
        }
        if (($item.Attributes -band [System.IO.FileAttributes]::ReadOnly) -ne 0) {
            return (New-EgCheckResult -Pass $false `
                -SupportRef 'EG_LAUNCHER_FILE_UNEXPECTEDLY_READONLY' -Checks $checks)
        }
    }
    $checks['launcher_files_not_unexpectedly_readonly'] = 'PASS'

    return (New-EgCheckResult -Pass $true -SupportRef '' -Checks $checks)
}

function Write-EgLauncherTerminalEvent {
    # Append EXACTLY ONE launcher_failed event carrying run_id, phase, status, and
    # support_ref, and nothing else.
    #
    # The event is EVIDENCE, not a result: a failure to write it never changes the exit
    # code, and a failure raised before the log root is resolved simply has no event. This
    # mirrors log_terminal_failure in energygrid_bill_downloader/cli.py.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$LogRoot,
        [Parameter(Mandatory)][AllowEmptyString()][string]$RunId,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Phase,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Status,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SupportRef
    )

    if ([string]::IsNullOrWhiteSpace($LogRoot)) {
        return
    }
    if (-not (Test-Path -LiteralPath $LogRoot -PathType Container)) {
        return
    }

    $event = [ordered]@{}
    $event['run_id'] = $RunId
    $event['phase'] = $Phase
    $event['status'] = $Status
    $event['support_ref'] = $SupportRef

    try {
        $line = ($event | ConvertTo-Json -Depth 8 -Compress)
        $encoding = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::AppendAllText(
            (Join-Path $LogRoot $script:EgTerminalEventFileName),
            ($line + "`r`n"),
            $encoding)
    }
    catch {
        # Evidence, not a result. A failed write never changes the exit code.
        return
    }
}

# --------------------------------------------------------------------------------------
# Launcher-root write authority (design section 17.2.1, DD-02, DD-03, DD-12, DD-13)
# --------------------------------------------------------------------------------------
# TWO different questions are asked about the launcher root, and Windows answers them by
# two different mechanisms. Conflating them is the defect this section exists to prevent.
#
#   launcher_root_not_writable_by_run_principal  asks Windows to evaluate the RUNNING TOKEN
#   launcher_root_write_trustees_authorised      inspects the DISCRETIONARY ACCESS LIST
#
# Neither implies the other and both are terminal. Where installer and run principal are
# the same Windows account, binding write authority to that account's user SID separates
# nothing, because normal split-token behaviour carries the same user SID enabled in both
# contexts; binding to an administrative group identity separates them only while the host
# keeps producing a filtered token, which the access list cannot show. The trustee binding
# is kept because it stops an unexpected writer, and the run-token check is what makes the
# residual case observable rather than asserted.
#
# Neither check ever emits a security identifier, a trustee name, an owner identity, a
# path, or any count derived from them. Only the check name, the pass or fail outcome, and
# the bounded support reference reach any surface.

# Rights treated as write-capable, declared ONCE and the sole source for both checks and
# for every test that asserts against the mask. In declared order: FILE_WRITE_DATA /
# FILE_ADD_FILE, FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY, FILE_WRITE_EA,
# FILE_DELETE_CHILD, FILE_WRITE_ATTRIBUTES, DELETE, WRITE_DAC, WRITE_OWNER.
#
# FILE_ADD_FILE and FILE_ADD_SUBDIRECTORY are the directory readings of the same bits as
# FILE_WRITE_DATA and FILE_APPEND_DATA, so the mask is bit-identical for both object kinds
# and the object type changes only how a granted bit is described. WRITE_DAC and
# WRITE_OWNER are included deliberately: Windows defines them as the right to modify the
# access list and the right to change the owner, so a trustee holding either can grant
# itself every other right at will, and treating them as read-level rights would make both
# checks decorative.
$script:EgWriteCapableAccessMask =
    0x00000002 -bor `
    0x00000004 -bor `
    0x00000010 -bor `
    0x00000040 -bor `
    0x00000100 -bor `
    0x00010000 -bor `
    0x00040000 -bor `
    0x00080000

# A token holding either privilege can reach the object whatever the access list says, so
# presence alone fails the run-principal check, whether enabled or disabled. This is a
# bounded rule and not an exhaustive one: no discretionary-access-list check can fully
# constrain a principal granted list-bypassing privileges, and claiming otherwise would be
# dishonest. LocalSystem holds them by construction and therefore fails by construction.
$script:EgBypassPrivilegeNames = @('SeTakeOwnershipPrivilege', 'SeRestorePrivilege')

# CREATOR OWNER. Windows replaces this placeholder on inheritance with the security
# identifier of whoever created the new object, so an inheritable write-capable entry for it
# describes an unbounded future write set rather than a principal. It is terminal as an
# entry and refused in the supplied authorised set.
$script:EgRefusedAuthorisedSid = 'S-1-3-0'

# Documented file-system GENERIC_MAPPING values (design section 17.2.1 step 4).
$script:EgFileGenericRead = 0x00120089
$script:EgFileGenericWrite = 0x00120116
$script:EgFileGenericExecute = 0x001200A0
$script:EgFileAllAccess = 0x001F01FF

# The Win32 calls DD-02 requires have no managed equivalent, so the interop is declared
# here as a constant and compiled lazily on first use (DD-13). Declaring a here-string is
# not a side effect, and compiling on first use writes only into the host's own temporary
# compilation location, never into the launcher root, the deployed checkout, the
# configuration directory, the browser cache, or the log root, which is the exact domain
# the zero-mutation contract snapshots.
#
# The DllImport surface is COMPLETE AND CLOSED by contract: every native function any
# security helper in this library calls appears below, and nothing appears below that no
# helper calls. LookupPrivilegeNameW is required because GetTokenInformation with
# TOKEN_PRIVILEGES returns locally unique identifiers rather than names, and neither the
# translation nor the enumeration has a managed equivalent.
#
# No security-descriptor import is declared. The descriptor is read through managed .NET as
# GetSecurityDescriptorBinaryForm() over Owner, Group, and Access sections, which is what
# design section 17.2.1 step 3 requires and is why AccessCheck does not fail with
# ERROR_INVALID_SECURITY_DESCR.
$script:EgWin32SecurityInteropSource = @'
using System;
using System.Runtime.InteropServices;
using System.Text;

namespace EgWin32 {

    [StructLayout(LayoutKind.Sequential)]
    public struct LUID {
        public uint LowPart;
        public int HighPart;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct LUID_AND_ATTRIBUTES {
        public LUID Luid;
        public uint Attributes;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct GENERIC_MAPPING {
        public uint GenericRead;
        public uint GenericWrite;
        public uint GenericExecute;
        public uint GenericAll;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PRIVILEGE_SET {
        public uint PrivilegeCount;
        public uint Control;
        public LUID_AND_ATTRIBUTES Privilege;
    }

    public class TokenPrivilegeReadResult {
        public bool ReadOk;
        public string[] PrivilegeNames;
    }

    public class AccessCheckOutcome {
        public bool Evaluated;
        public uint GrantedAccess;
        public bool AccessStatus;
        public int LastError;
    }

    public static class Security {

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr GetCurrentProcess();

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool OpenProcessToken(IntPtr ProcessHandle, uint DesiredAccess, out IntPtr TokenHandle);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool DuplicateTokenEx(IntPtr hExistingToken, uint dwDesiredAccess, IntPtr lpTokenAttributes, int ImpersonationLevel, int TokenType, out IntPtr phNewToken);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr hObject);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool GetTokenInformation(IntPtr TokenHandle, int TokenInformationClass, IntPtr TokenInformation, int TokenInformationLength, out int ReturnLength);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool AccessCheck(byte[] pSecurityDescriptor, IntPtr ClientToken, uint DesiredAccess, ref GENERIC_MAPPING GenericMapping, IntPtr PrivilegeSet, ref int PrivilegeSetLength, out uint GrantedAccess, out bool AccessStatus);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern void MapGenericMask(ref uint AccessMask, ref GENERIC_MAPPING GenericMapping);

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "LookupPrivilegeNameW")]
        private static extern bool LookupPrivilegeNameW([MarshalAs(UnmanagedType.LPWStr)] string lpSystemName, ref LUID lpLuid, StringBuilder lpName, ref int cchName);

        private const uint TOKEN_DUPLICATE = 0x0002;
        private const uint TOKEN_QUERY = 0x0008;
        private const uint MAXIMUM_ALLOWED = 0x02000000;
        private const int SECURITY_IDENTIFICATION = 2;
        private const int TOKEN_IMPERSONATION = 2;
        private const int TOKEN_PRIVILEGES_CLASS = 3;
        private const int ERROR_INSUFFICIENT_BUFFER = 122;
        private const int PRIVILEGE_SET_SLACK_ENTRIES = 32;

        private static GENERIC_MAPPING BuildMapping(uint genericRead, uint genericWrite, uint genericExecute, uint genericAll) {
            GENERIC_MAPPING mapping = new GENERIC_MAPPING();
            mapping.GenericRead = genericRead;
            mapping.GenericWrite = genericWrite;
            mapping.GenericExecute = genericExecute;
            mapping.GenericAll = genericAll;
            return mapping;
        }

        // Apply MapGenericMask so the compared mask carries no generic rights.
        public static uint MapMask(uint mask, uint genericRead, uint genericWrite, uint genericExecute, uint genericAll) {
            GENERIC_MAPPING mapping = BuildMapping(genericRead, genericWrite, genericExecute, genericAll);
            uint working = mask;
            MapGenericMask(ref working, ref mapping);
            return working;
        }

        // The prescribed run-token access check. DesiredAccess is MAXIMUM_ALLOWED, so
        // Windows returns the maximum access the descriptor allows that token, and the
        // caller intersects the returned mask. Requesting the union of every write-capable
        // right and reading a denied access status as safe is a false negative by
        // construction and is never done.
        //
        // PrivilegeSet and PrivilegeSetLength are call mechanics, not part of any verdict.
        // A correctly sized buffer is supplied; an insufficient or invalid buffer is an API
        // failure, so it sets Evaluated false and is terminal in the caller.
        public static AccessCheckOutcome CheckMaximumAllowed(byte[] securityDescriptor, uint genericRead, uint genericWrite, uint genericExecute, uint genericAll) {
            AccessCheckOutcome outcome = new AccessCheckOutcome();
            outcome.Evaluated = false;
            outcome.GrantedAccess = 0;
            outcome.AccessStatus = false;
            outcome.LastError = 0;

            IntPtr processToken = IntPtr.Zero;
            IntPtr impersonationToken = IntPtr.Zero;
            IntPtr privilegeSet = IntPtr.Zero;
            try {
                if (OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_QUERY, out processToken) == false) {
                    outcome.LastError = Marshal.GetLastWin32Error();
                    return outcome;
                }
                // AccessCheck is documented to take an IMPERSONATION token, so the primary
                // token is never passed to it. SecurityIdentification is the least
                // privileged level Windows documents as sufficient for a server to make
                // access-validation decisions. The duplicate exists only to be evaluated.
                if (DuplicateTokenEx(processToken, TOKEN_QUERY, IntPtr.Zero, SECURITY_IDENTIFICATION, TOKEN_IMPERSONATION, out impersonationToken) == false) {
                    outcome.LastError = Marshal.GetLastWin32Error();
                    return outcome;
                }

                GENERIC_MAPPING mapping = BuildMapping(genericRead, genericWrite, genericExecute, genericAll);
                int privilegeSetLength = Marshal.SizeOf(typeof(PRIVILEGE_SET)) + (PRIVILEGE_SET_SLACK_ENTRIES * Marshal.SizeOf(typeof(LUID_AND_ATTRIBUTES)));
                privilegeSet = Marshal.AllocHGlobal(privilegeSetLength);

                uint granted = 0;
                bool status = false;
                if (AccessCheck(securityDescriptor, impersonationToken, MAXIMUM_ALLOWED, ref mapping, privilegeSet, ref privilegeSetLength, out granted, out status) == false) {
                    outcome.LastError = Marshal.GetLastWin32Error();
                    return outcome;
                }
                outcome.Evaluated = true;
                outcome.GrantedAccess = granted;
                outcome.AccessStatus = status;
                return outcome;
            }
            finally {
                if (privilegeSet != IntPtr.Zero) { Marshal.FreeHGlobal(privilegeSet); }
                if (impersonationToken != IntPtr.Zero) { CloseHandle(impersonationToken); }
                if (processToken != IntPtr.Zero) { CloseHandle(processToken); }
            }
        }

        // Read the privilege names PRESENT in the running process token.
        //
        // ReadOk true means the information was read and PrivilegeNames carries exactly
        // the names OBSERVED. ReadOk false means the read FAILED and PrivilegeNames is
        // empty. One unresolved locally unique identifier fails the WHOLE read: no entry is
        // ever silently skipped, no partial list is ever returned, and a lookup error is
        // never read as that privilege being absent. No privilege name is ever fabricated.
        public static TokenPrivilegeReadResult ReadTokenPrivilegeNames() {
            TokenPrivilegeReadResult result = new TokenPrivilegeReadResult();
            result.ReadOk = false;
            result.PrivilegeNames = new string[0];

            IntPtr processToken = IntPtr.Zero;
            IntPtr buffer = IntPtr.Zero;
            try {
                if (OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_QUERY, out processToken) == false) {
                    return result;
                }
                int required = 0;
                if (GetTokenInformation(processToken, TOKEN_PRIVILEGES_CLASS, IntPtr.Zero, 0, out required) == false) {
                    if (Marshal.GetLastWin32Error() != ERROR_INSUFFICIENT_BUFFER) {
                        return result;
                    }
                }
                if (required <= 0) {
                    return result;
                }
                buffer = Marshal.AllocHGlobal(required);
                int returned = 0;
                if (GetTokenInformation(processToken, TOKEN_PRIVILEGES_CLASS, buffer, required, out returned) == false) {
                    return result;
                }

                int count = Marshal.ReadInt32(buffer);
                if (count < 0) {
                    return result;
                }
                string[] names = new string[count];
                int entrySize = Marshal.SizeOf(typeof(LUID_AND_ATTRIBUTES));
                for (int index = 0; index < count; index++) {
                    IntPtr entryPointer = new IntPtr(buffer.ToInt64() + 4 + (index * entrySize));
                    LUID_AND_ATTRIBUTES entry = (LUID_AND_ATTRIBUTES)Marshal.PtrToStructure(entryPointer, typeof(LUID_AND_ATTRIBUTES));
                    LUID luid = entry.Luid;

                    // Two-call sizing. The first call reports the required length; the
                    // retry is given a buffer of that length plus one, and cchName is set
                    // to the real capacity so Windows is told the true buffer size.
                    int cchName = 0;
                    if (LookupPrivilegeNameW(null, ref luid, null, ref cchName) == false) {
                        if (Marshal.GetLastWin32Error() != ERROR_INSUFFICIENT_BUFFER) {
                            return result;
                        }
                    }
                    if (cchName <= 0) {
                        return result;
                    }
                    int capacity = cchName + 1;
                    StringBuilder builder = new StringBuilder(capacity);
                    int cchRetry = capacity;
                    if (LookupPrivilegeNameW(null, ref luid, builder, ref cchRetry) == false) {
                        return result;
                    }
                    names[index] = builder.ToString();
                }
                result.ReadOk = true;
                result.PrivilegeNames = names;
                return result;
            }
            finally {
                if (buffer != IntPtr.Zero) { Marshal.FreeHGlobal(buffer); }
                if (processToken != IntPtr.Zero) { CloseHandle(processToken); }
            }
        }
    }
}
'@

function Initialize-EgWin32SecurityInterop {
    # Compile the security interop on FIRST USE ONLY, guarded by a type-presence test so
    # repeat calls compile nothing. Never invoked at load, so the library stays pure and
    # dot-sourceable (EGRT-I01, DD-13).
    [CmdletBinding()]
    param()

    if (-not ([System.Management.Automation.PSTypeName]'EgWin32.Security').Type) {
        Add-Type -TypeDefinition $script:EgWin32SecurityInteropSource
    }
}

function Get-EgMappedWriteCapableMask {
    # The write-capable mask AFTER generic mapping, so the compared mask carries no generic
    # rights (design section 17.2.1 steps 4 and 7, EGRT-T71).
    [CmdletBinding()]
    param()

    Initialize-EgWin32SecurityInterop
    return [int][EgWin32.Security]::MapMask(
        [uint32]$script:EgWriteCapableAccessMask,
        [uint32]$script:EgFileGenericRead,
        [uint32]$script:EgFileGenericWrite,
        [uint32]$script:EgFileGenericExecute,
        [uint32]$script:EgFileAllAccess)
}

function Test-EgAuthorisedWriteSidSet {
    # Admit the value supplied on the authorised-write-trustee parameter (DD-12). Pure.
    #
    # Refuses an empty set; refuses any element that does not construct a
    # SecurityIdentifier from its standard textual form, which is what refuses an account
    # name WITHOUT performing a name-resolution lookup; and refuses the CREATOR OWNER
    # placeholder. No supplied value is echoed into the result, a log, or an exception
    # surface.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string[]]$AuthorisedSid)

    $refused = [pscustomobject]@{
        Pass       = $false
        SupportRef = 'EG_LAUNCHER_ROOT_AUTHORISED_SID_SET_INVALID'
        Sids       = @()
    }

    $supplied = @($AuthorisedSid)
    if ($supplied.Count -eq 0) {
        return $refused
    }

    $admitted = @()
    foreach ($candidate in $supplied) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            return $refused
        }
        if ($candidate -ceq $script:EgRefusedAuthorisedSid) {
            return $refused
        }
        $parsed = $null
        try {
            $parsed = New-Object System.Security.Principal.SecurityIdentifier($candidate)
        }
        catch {
            return $refused
        }
        if ($null -eq $parsed) {
            return $refused
        }
        # The textual form must round-trip, so a value that merely happens to construct is
        # not admitted as a security identifier.
        if ($parsed.Value -cne $candidate.Trim()) {
            return $refused
        }
        $admitted = $admitted + $parsed
    }

    [pscustomobject]@{
        Pass       = $true
        SupportRef = ''
        Sids       = @($admitted)
    }
}

function Test-EgBypassPrivilegePresent {
    # Pure predicate over ACTUALLY OBSERVED privilege names. Presence alone is sufficient;
    # enabled state is irrelevant.
    #
    # The supplied list is only ever the observed names of a successful token read. This
    # predicate has no knowledge of read failure, never receives a fabricated name, and
    # never manufactures failure state: an empty list is simply false, and the terminal
    # handling of a failed read belongs to the caller.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$PrivilegeName)

    foreach ($observed in @($PrivilegeName)) {
        foreach ($bypass in $script:EgBypassPrivilegeNames) {
            if ([string]::Equals($observed, $bypass, [System.StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
    }
    return $false
}

function Get-EgTokenPrivilegeNames {
    # Read the privilege names present in the running process token.
    #
    # ReadOk true means the information was read and PrivilegeNames carries exactly the
    # names OBSERVED. ReadOk false means the read FAILED and PrivilegeNames is empty. The
    # two are DISTINCT states and are never collapsed: the caller must treat a failed read
    # as terminal and must never read the empty array as evidence that no bypass privilege
    # is present.
    #
    # This function deliberately does not consume the bypass-privilege name constant, so a
    # read failure can never be expressed as a synthesised privilege membership.
    [CmdletBinding()]
    param()

    $failed = [pscustomobject]@{
        ReadOk         = $false
        PrivilegeNames = [string[]]@()
    }

    try {
        Initialize-EgWin32SecurityInterop
    }
    catch {
        return $failed
    }

    $read = $null
    try {
        $read = [EgWin32.Security]::ReadTokenPrivilegeNames()
    }
    catch {
        return $failed
    }
    if ($null -eq $read) {
        return $failed
    }
    if (-not $read.ReadOk) {
        return $failed
    }

    [pscustomobject]@{
        ReadOk         = $true
        PrivilegeNames = [string[]]@($read.PrivilegeNames)
    }
}

function Get-EgSecurityDescriptorForPath {
    # Read a file-system object's security descriptor including its OWNER, its GROUP, and
    # its discretionary access list. All three are required, because AccessCheck fails with
    # ERROR_INVALID_SECURITY_DESCR when the descriptor carries no owner and group.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    $sections = [System.Security.AccessControl.AccessControlSections]::Owner -bor
        [System.Security.AccessControl.AccessControlSections]::Group -bor
        [System.Security.AccessControl.AccessControlSections]::Access

    $descriptor = $null
    try {
        if (Test-Path -LiteralPath $Path -PathType Container) {
            $descriptor = New-Object System.Security.AccessControl.DirectorySecurity($Path, $sections)
        }
        else {
            $descriptor = New-Object System.Security.AccessControl.FileSecurity($Path, $sections)
        }
    }
    catch {
        return $null
    }
    return $descriptor
}

function Test-EgTokenWriteAccessToPath {
    # DD-02 steps 1 to 8 against exactly ONE object.
    #
    # Evaluated false is TERMINAL. The caller must never read it as a pass, must not retry
    # at another impersonation level, and must not fall back to another method.
    #
    # A null discretionary access list needs no special case: Windows grants all access when
    # an object has none, so GrantedAccess returns carrying every right and the intersection
    # is non-zero, which fails the check.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    $terminal = [pscustomobject]@{
        AnyWriteGranted = $false
        Evaluated       = $false
        SupportRef      = 'EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE'
    }

    $descriptor = Get-EgSecurityDescriptorForPath -Path $Path
    if ($null -eq $descriptor) {
        return $terminal
    }

    $binaryForm = $null
    try {
        $binaryForm = $descriptor.GetSecurityDescriptorBinaryForm()
    }
    catch {
        return $terminal
    }
    if ($null -eq $binaryForm) {
        return $terminal
    }

    $mappedMask = 0
    $outcome = $null
    try {
        Initialize-EgWin32SecurityInterop
        $mappedMask = Get-EgMappedWriteCapableMask
        $outcome = [EgWin32.Security]::CheckMaximumAllowed(
            $binaryForm,
            [uint32]$script:EgFileGenericRead,
            [uint32]$script:EgFileGenericWrite,
            [uint32]$script:EgFileGenericExecute,
            [uint32]$script:EgFileAllAccess)
    }
    catch {
        return $terminal
    }
    if ($null -eq $outcome) {
        return $terminal
    }
    if (-not $outcome.Evaluated) {
        return $terminal
    }

    # ANY-BIT intersection of the returned maximum access with the mapped write-capable
    # mask. AccessStatus is deliberately not consulted for the verdict: under
    # MAXIMUM_ALLOWED the granted mask IS the answer.
    $anyWriteGranted = ((([int]$outcome.GrantedAccess) -band $mappedMask) -ne 0)

    $supportRef = ''
    if ($anyWriteGranted) {
        $supportRef = 'EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE'
    }

    [pscustomobject]@{
        AnyWriteGranted = $anyWriteGranted
        Evaluated       = $true
        SupportRef      = $supportRef
    }
}

function Test-EgPathWriteTrusteesAuthorised {
    # DD-03 steps 1 to 8 against exactly ONE object.
    #
    # Comparison is EXACT SecurityIdentifier equality only. No prefix, pattern, range, or
    # wildcard match against a trustee identifier appears anywhere. Access-denied entries
    # are ignored, because a deny entry can only reduce access and whether it neutralises a
    # given allow depends on list order, so cancelling one against the other here would let
    # a badly ordered list conceal a real grant. An inherited allow entry is treated exactly
    # as an explicit one, because inheritance describes where an entry came from, not how
    # much access it grants.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path,
        [Parameter(Mandatory)][ValidateNotNull()][System.Security.Principal.SecurityIdentifier[]]$AuthorisedSid
    )

    $terminal = [pscustomobject]@{
        Authorised = $false
        Evaluated  = $false
        SupportRef = 'EG_LAUNCHER_ROOT_ACL_WRITE_TRUSTEE_UNAUTHORISED'
    }

    $descriptor = Get-EgSecurityDescriptorForPath -Path $Path
    if ($null -eq $descriptor) {
        return $terminal
    }

    $binaryForm = $null
    $raw = $null
    try {
        $binaryForm = $descriptor.GetSecurityDescriptorBinaryForm()
        $raw = New-Object System.Security.AccessControl.RawSecurityDescriptor($binaryForm, 0)
    }
    catch {
        return $terminal
    }
    if ($null -eq $raw) {
        return $terminal
    }

    # An object with NO discretionary access list fails, because Windows grants all access
    # in that case.
    $listPresent = (($raw.ControlFlags -band
        [System.Security.AccessControl.ControlFlags]::DiscretionaryAclPresent) -ne 0)
    if (-not $listPresent) {
        return [pscustomobject]@{
            Authorised = $false
            Evaluated  = $true
            SupportRef = 'EG_LAUNCHER_ROOT_ACL_WRITE_TRUSTEE_UNAUTHORISED'
        }
    }
    if ($null -eq $raw.DiscretionaryAcl) {
        return [pscustomobject]@{
            Authorised = $false
            Evaluated  = $true
            SupportRef = 'EG_LAUNCHER_ROOT_ACL_WRITE_TRUSTEE_UNAUTHORISED'
        }
    }

    $mappedMask = 0
    try {
        $mappedMask = Get-EgMappedWriteCapableMask
    }
    catch {
        return $terminal
    }

    $unauthorised = [pscustomobject]@{
        Authorised = $false
        Evaluated  = $true
        SupportRef = 'EG_LAUNCHER_ROOT_ACL_WRITE_TRUSTEE_UNAUTHORISED'
    }

    # Every examined object's OWNER must also be in the supplied set, because an owner
    # implicitly holds WRITE_DAC and can restore write access to itself at will. An owner
    # outside the set fails even where no explicit write-capable entry exists.
    $owner = $null
    try {
        $owner = $raw.Owner
    }
    catch {
        return $terminal
    }
    if ($null -eq $owner) {
        return $terminal
    }
    if (-not (Test-EgSidInSet -Candidate $owner -AuthorisedSid $AuthorisedSid)) {
        return $unauthorised
    }

    foreach ($ace in @($raw.DiscretionaryAcl)) {
        if ($ace -isnot [System.Security.AccessControl.CommonAce]) {
            continue
        }
        if ($ace.AceType -ne [System.Security.AccessControl.AceType]::AccessAllowed) {
            if ($ace.AceType -ne [System.Security.AccessControl.AceType]::AccessAllowedObject) {
                continue
            }
        }
        if ((([int]$ace.AccessMask) -band $mappedMask) -eq 0) {
            continue
        }
        # A write-capable CREATOR OWNER entry describes an unbounded future write set
        # rather than a principal, so it is terminal.
        if ($ace.SecurityIdentifier.Value -ceq $script:EgRefusedAuthorisedSid) {
            return $unauthorised
        }
        if (-not (Test-EgSidInSet -Candidate $ace.SecurityIdentifier -AuthorisedSid $AuthorisedSid)) {
            return $unauthorised
        }
    }

    [pscustomobject]@{
        Authorised = $true
        Evaluated  = $true
        SupportRef = ''
    }
}

function Test-EgSidInSet {
    # Exact SecurityIdentifier equality against the supplied exhaustive set. No prefix,
    # pattern, range, or wildcard form is accepted, and well-known identifiers carry no
    # implicit authority: they are accepted only where the operator supplied that exact
    # value.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNull()][System.Security.Principal.SecurityIdentifier]$Candidate,
        [Parameter(Mandatory)][ValidateNotNull()][System.Security.Principal.SecurityIdentifier[]]$AuthorisedSid
    )

    foreach ($authorised in @($AuthorisedSid)) {
        if ($Candidate.Equals($authorised)) {
            return $true
        }
    }
    return $false
}

# --------------------------------------------------------------------------------------
# Path-scoped governed source integrity (design section 10.3)
# --------------------------------------------------------------------------------------
# The unit of protection is the EnergyGrid runtime-critical surface, NOT the repository.
# This repository is a monorepo: requiring the deployed checkout to sit at one permanently
# fixed whole-repository HEAD would mean any accepted, unrelated merge stops the daily job,
# and any unrelated dirty file elsewhere in the tree does the same. That converts routine
# repository activity into an outage, which is what DL-XB-141-SCHEDULER-005 forbids.
#
# Exactly two paths are governed. runtime/ is deliberately EXCLUDED: the launcher executes
# from the launcher root outside the checkout, so its integrity is covered by the
# installation manifest and the checkout copy is install-source rather than execution
# surface. tests/, docs/, task-scheduler/, and the example configuration are excluded
# because a normal run neither executes nor reads them. Broadening to the whole monorepo
# because it is easier is exactly the failure this section exists to prevent.
$script:EgGovernedSourcePaths = @(
    'energygrid-bill-downloader/energygrid_bill_downloader',
    'energygrid-bill-downloader/requirements.txt'
)
$script:EgGovernedExecutableSurface = 'energygrid-bill-downloader/energygrid_bill_downloader'
$script:EgAnyBranchSentinel = 'ANY_BRANCH'

function Get-EgGovernedSourcePaths {
    # The exact two governed paths, enumerated explicitly.
    [CmdletBinding()]
    param()

    @($script:EgGovernedSourcePaths)
}

function Test-EgIsSanctionedBytecodeArtefact {
    # The ONE sanctioned overlay exception. Python bytecode is created by normal execution
    # and must not fail the run. EVERY other untracked entry inside the governed executable
    # surface, ignored or not, is a substitution and is terminal.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$RelativePath)

    if ([string]::IsNullOrEmpty($RelativePath)) {
        return $false
    }
    $normalised = $RelativePath.Replace('\', '/')
    foreach ($segment in @($normalised -split '/')) {
        if ($segment -ceq '__pycache__') {
            return $true
        }
    }
    return $normalised.EndsWith('.pyc', [System.StringComparison]::Ordinal)
}

function Test-EgGovernedSourceIntegrity {
    # The eight ordered, read-only, path-scoped integrity checks. Every read goes through
    # Invoke-GovernedGit, so ambient variable neutralisation and the read-only subcommand
    # allowlist both apply, and every check is scoped with an explicit Git pathspec.
    #
    # Evaluation stops at the first failing check and the remaining checks are reported
    # FAIL, because a governed read taken after a failed repository binding would be
    # evaluating something other than what the caller asked about.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRootPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedBranch
    )

    $orderedNames = @(
        'source_repository_binding',
        'source_branch_binding',
        'source_paths_exist',
        'source_paths_tracked',
        'source_no_staged_modification',
        'source_no_unstaged_modification',
        'source_no_tracked_deletion',
        'source_no_untracked_overlay'
    )
    $checks = [ordered]@{}
    foreach ($name in $orderedNames) {
        $checks[$name] = 'FAIL'
    }

    $governedPaths = @(Get-EgGovernedSourcePaths)
    $resolvedRoot = [System.IO.Path]::GetFullPath($CheckoutRootPath).TrimEnd('\', '/')

    # Check 1. Repository binding. This also proves ambient redirection did not silently
    # move the checks to another tree.
    $topLevel = Invoke-GovernedGit -RepositoryRootPath $resolvedRoot -Arguments @('rev-parse', '--show-toplevel')
    $bound = $false
    if ($topLevel.Success) {
        if ($topLevel.Lines.Count -eq 1) {
            $reported = [System.IO.Path]::GetFullPath($topLevel.Lines[0].Trim()).TrimEnd('\', '/')
            $bound = [string]::Equals($reported, $resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)
        }
    }
    if (-not $bound) {
        return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_SOURCE_BINDING_FAILED' -Checks $checks)
    }
    $checks['source_repository_binding'] = 'PASS'

    # Check 2. Branch binding, unless the explicit sentinel is supplied. The sentinel means
    # binding is never disabled by omitting an argument.
    if ($ExpectedBranch -ceq $script:EgAnyBranchSentinel) {
        $checks['source_branch_binding'] = 'PASS'
    }
    else {
        $branch = Invoke-GovernedGit -RepositoryRootPath $resolvedRoot -Arguments @('symbolic-ref', '--short', 'HEAD')
        $branchOk = $false
        if ($branch.Success) {
            if ($branch.Lines.Count -eq 1) {
                $branchOk = ($branch.Lines[0].Trim() -ceq $ExpectedBranch)
            }
        }
        if (-not $branchOk) {
            return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_SOURCE_BRANCH_MISMATCH' -Checks $checks)
        }
        $checks['source_branch_binding'] = 'PASS'
    }

    # Check 3. Each governed path exists on disk.
    foreach ($governed in $governedPaths) {
        $onDisk = Join-Path $resolvedRoot ($governed.Replace('/', [string][System.IO.Path]::DirectorySeparatorChar))
        if (-not (Test-Path -LiteralPath $onDisk)) {
            return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_SOURCE_PATH_MISSING' -Checks $checks)
        }
    }
    $checks['source_paths_exist'] = 'PASS'

    # Check 4. Each governed path is tracked, proven by a non-empty tracked-file listing
    # scoped to that path.
    foreach ($governed in $governedPaths) {
        $tracked = Invoke-GovernedGit -RepositoryRootPath $resolvedRoot -Arguments (@('ls-files', '--') + @($governed))
        if (-not $tracked.Success) {
            return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_SOURCE_PATH_UNTRACKED' -Checks $checks)
        }
        if ($tracked.Lines.Count -eq 0) {
            return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_SOURCE_PATH_UNTRACKED' -Checks $checks)
        }
    }
    $checks['source_paths_tracked'] = 'PASS'

    # Checks 5 and 6. Staged and unstaged modification, scoped to modification and type
    # change only. Deletion is a DISTINCT terminal condition with its own reference, so it
    # is excluded here rather than reported as a modification.
    $staged = Invoke-GovernedGit -RepositoryRootPath $resolvedRoot -Arguments (@('diff', '--cached', '--name-only', '--diff-filter=MT', '--') + $governedPaths)
    if ((-not $staged.Success) -or ($staged.Lines.Count -ne 0)) {
        return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_SOURCE_STAGED_MODIFICATION' -Checks $checks)
    }
    $checks['source_no_staged_modification'] = 'PASS'

    $unstaged = Invoke-GovernedGit -RepositoryRootPath $resolvedRoot -Arguments (@('diff', '--name-only', '--diff-filter=MT', '--') + $governedPaths)
    if ((-not $unstaged.Success) -or ($unstaged.Lines.Count -ne 0)) {
        return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_SOURCE_UNSTAGED_MODIFICATION' -Checks $checks)
    }
    $checks['source_no_unstaged_modification'] = 'PASS'

    # Check 7. No deletion of a tracked file, staged or unstaged.
    $deletedWorktree = Invoke-GovernedGit -RepositoryRootPath $resolvedRoot -Arguments (@('diff', '--name-only', '--diff-filter=D', '--') + $governedPaths)
    if ((-not $deletedWorktree.Success) -or ($deletedWorktree.Lines.Count -ne 0)) {
        return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_SOURCE_DELETED' -Checks $checks)
    }
    $deletedStaged = Invoke-GovernedGit -RepositoryRootPath $resolvedRoot -Arguments (@('diff', '--cached', '--name-only', '--diff-filter=D', '--') + $governedPaths)
    if ((-not $deletedStaged.Success) -or ($deletedStaged.Lines.Count -ne 0)) {
        return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_SOURCE_DELETED' -Checks $checks)
    }
    $checks['source_no_tracked_deletion'] = 'PASS'

    # Check 8. No untracked substitution or overlay inside the governed executable surface.
    # An untracked file that shadows a module name is a real substitution risk, so the
    # listing must see untracked files even when .gitignore would hide them (DD-11). The
    # ONLY sanctioned exception is Python bytecode.
    $overlaySurface = @($script:EgGovernedExecutableSurface)
    $untracked = Invoke-GovernedGit -RepositoryRootPath $resolvedRoot -Arguments (@('ls-files', '--others', '--exclude-standard', '--') + $overlaySurface)
    $ignored = Invoke-GovernedGit -RepositoryRootPath $resolvedRoot -Arguments (@('ls-files', '--others', '--ignored', '--exclude-standard', '--') + $overlaySurface)
    if ((-not $untracked.Success) -or (-not $ignored.Success)) {
        return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_SOURCE_UNTRACKED_OVERLAY' -Checks $checks)
    }
    $observedOverlays = @($untracked.Lines) + @($ignored.Lines)
    foreach ($candidate in $observedOverlays) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }
        if (Test-EgIsSanctionedBytecodeArtefact -RelativePath $candidate) {
            continue
        }
        return (New-EgCheckResult -Pass $false -SupportRef 'EG_LAUNCHER_SOURCE_UNTRACKED_OVERLAY' -Checks $checks)
    }
    $checks['source_no_untracked_overlay'] = 'PASS'

    return (New-EgCheckResult -Pass $true -SupportRef '' -Checks $checks)
}

# --------------------------------------------------------------------------------------
# Private browser-cache binding (design section 9.3)
# --------------------------------------------------------------------------------------
# The launcher must POSITIVELY bind the approved private browser cache and fail closed. It
# must never silently fall through to an ambient or default cache, because a run that
# quietly uses an unreviewed browser cache is a run whose behaviour nobody approved.
#
# The launcher never installs, updates, repairs, or downloads into the cache. Provisioning
# stays an operator action, exactly as the project runbook already requires. There is no
# fallback: a cache that is missing, unreadable, or not provisioned fails the run.
$script:EgBrowserCacheVariableName = 'PLAYWRIGHT_BROWSERS_PATH'
$script:EgBrowserCacheChildPattern = '^chromium(-|_).+'
$script:EgBrowserCacheExecutableNames = @('chrome.exe', 'headless_shell.exe')

function Test-EgBrowserCacheReady {
    # A POSITIVE readiness check, not merely a directory-existence test: the path must
    # contain at least one immediate child directory whose name matches the Chromium
    # pattern, and that child must contain a recognised browser executable at any depth.
    #
    # The probe is READ-ONLY. Nothing is created, downloaded, or repaired.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$BrowserCachePath)

    $checkName = 'browser_cache_ready'

    if (-not (Test-Path -LiteralPath $BrowserCachePath -PathType Container)) {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
            -SupportRef 'EG_LAUNCHER_BROWSER_CACHE_UNRESOLVED')
    }

    $children = @()
    try {
        $children = @(Get-ChildItem -LiteralPath $BrowserCachePath -Directory)
    }
    catch {
        return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
            -SupportRef 'EG_LAUNCHER_BROWSER_CACHE_UNRESOLVED')
    }

    foreach ($child in $children) {
        if ($child.Name -notmatch $script:EgBrowserCacheChildPattern) {
            continue
        }
        foreach ($executableName in $script:EgBrowserCacheExecutableNames) {
            $found = @()
            try {
                $found = @(Get-ChildItem -LiteralPath $child.FullName -Filter $executableName `
                    -File -Recurse -ErrorAction SilentlyContinue)
            }
            catch {
                $found = @()
            }
            if (@($found).Count -gt 0) {
                return (New-EgSingleCheckResult -CheckName $checkName -Pass $true)
            }
        }
    }

    return (New-EgSingleCheckResult -CheckName $checkName -Pass $false `
        -SupportRef 'EG_LAUNCHER_BROWSER_CACHE_NOT_READY')
}

# --------------------------------------------------------------------------------------
# DPAPI credential import and viability (design section 9.2)
# --------------------------------------------------------------------------------------
# What stays private: the DPAPI artefact itself, its absolute path, the Windows user
# identity it is bound to, and every value it yields. The artefact is never committed,
# never copied into the checkout, and never reconstructed by the repository. Its location
# reaches the launcher only through a parameter, supplied from private host settings.
#
# What Git owns: the import, injection, and cleanup behaviour, expressed without any
# private value.
#
# This design makes no claim of cryptographic erasure of managed memory. .NET string
# interning and garbage collection make that claim false, and a false guarantee is worse
# than a bounded one. What is guaranteed is bounded lifetime, exact environment
# restoration, and no persistence to disk or to any durable environment scope.

function Import-EgLauncherCredential {
    # Import the private DPAPI PSCredential CLIXML artefact.
    #
    # Uses Import-Clixml ONLY, and passes NO -Key and NO -SecureKey argument. A keyed
    # export would silently convert the artefact into something portable between users and
    # void the DPAPI CurrentUser binding, which is the property that makes cross-user
    # rejection a construction guarantee rather than a check this code has to write.
    #
    # No exception message text is recorded anywhere, and nothing is emitted on any path.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CredentialPath)

    if (-not (Test-Path -LiteralPath $CredentialPath -PathType Leaf)) {
        return [pscustomobject]@{
            Success    = $false
            SupportRef = 'EG_LAUNCHER_CREDENTIAL_ARTEFACT_MISSING'
            Credential = $null
        }
    }

    $imported = $null
    try {
        $imported = Import-Clixml -LiteralPath $CredentialPath
    }
    catch {
        return [pscustomobject]@{
            Success    = $false
            SupportRef = 'EG_LAUNCHER_CREDENTIAL_IMPORT_FAILED'
            Credential = $null
        }
    }

    if ($imported -isnot [System.Management.Automation.PSCredential]) {
        return [pscustomobject]@{
            Success    = $false
            SupportRef = 'EG_LAUNCHER_CREDENTIAL_IMPORT_FAILED'
            Credential = $null
        }
    }

    [pscustomobject]@{
        Success    = $true
        SupportRef = ''
        Credential = $imported
    }
}

function Test-EgCredentialViability {
    # Require a non-empty username and a non-empty password. Either being empty is
    # terminal, and this happens BEFORE the child process starts, so there is no path on
    # which the application is launched with absent, partial, or unverified credentials.
    #
    # Password non-emptiness is derived by marshalling the SecureString inside a
    # try/finally that ALWAYS zeroes and frees the unmanaged buffer, testing only whether
    # the length exceeds zero. The length itself is never recorded, returned, logged, or
    # emitted in any form: only the boolean leaves this function.
    #
    # Dispose is NEVER called on the PSCredential. PSCredential does not implement
    # IDisposable and the call would throw at runtime.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowNull()]$Credential)

    if ($null -eq $Credential) {
        return [pscustomobject]@{
            CredentialImportOk = $false
            UsernameNonEmpty   = $false
            PasswordNonEmpty   = $false
            SupportRef         = 'EG_LAUNCHER_CREDENTIAL_INCOMPLETE'
        }
    }

    $usernameNonEmpty = (-not [string]::IsNullOrEmpty($Credential.UserName))

    $passwordNonEmpty = $false
    $secure = $Credential.Password
    if ($null -ne $secure) {
        $unmanaged = [System.IntPtr]::Zero
        try {
            $unmanaged = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($unmanaged)
            if ($null -ne $plain) {
                $passwordNonEmpty = ($plain.Length -gt 0)
            }
            $plain = ''
        }
        finally {
            if ($unmanaged -ne [System.IntPtr]::Zero) {
                [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($unmanaged)
            }
        }
    }

    $supportRef = ''
    if (-not ($usernameNonEmpty -and $passwordNonEmpty)) {
        $supportRef = 'EG_LAUNCHER_CREDENTIAL_INCOMPLETE'
    }

    [pscustomobject]@{
        CredentialImportOk = $true
        UsernameNonEmpty   = $usernameNonEmpty
        PasswordNonEmpty   = $passwordNonEmpty
        SupportRef         = $supportRef
    }
}

function Get-EgGovernedGitEnvironmentNames {
    # The exact nineteen ambient Git variables design section 10.2 names, in a fixed
    # order. Each is neutralised before a governed read and exactly restored afterwards.
    [CmdletBinding()]
    param()

    @(
        'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
        'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_CONFIG',
        'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_NOSYSTEM',
        'GIT_CEILING_DIRECTORIES', 'GIT_NAMESPACE', 'GIT_ATTR_NOSYSTEM', 'GIT_PAGER',
        'GIT_EDITOR', 'GIT_ASKPASS', 'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_TERMINAL_PROMPT'
    )
}

# The exhaustive READ-ONLY Git subcommand allowlist (design section 10.3). Every Git
# operation that fetches, updates, moves, or rewrites state is prohibited outright: the
# runtime layer performs zero Git network operations and zero Git state mutations.
# Bringing the deployed checkout to a newer source revision is a separately controlled
# operator or repository action, never something the unattended job does to itself.
$script:EgGitAllowedSubcommands = @('rev-parse', 'symbolic-ref', 'ls-files', 'diff', 'status')

function Get-EgGitAllowedSubcommands {
    # The allowlist, enumerated explicitly.
    [CmdletBinding()]
    param()

    @($script:EgGitAllowedSubcommands)
}

function Test-EgGitSubcommandAllowed {
    # Exact, case-sensitive membership of the allowlist. A prefix, pattern, or
    # case-insensitive match is deliberately NOT accepted.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Subcommand)

    foreach ($allowed in $script:EgGitAllowedSubcommands) {
        if ($allowed -ceq $Subcommand) {
            return $true
        }
    }
    return $false
}

function ConvertTo-EgNativeArgumentString {
    # Build a Windows command line from an argument vector.
    #
    # ProcessStartInfo.ArgumentList does not exist on .NET Framework 4.x, so the argument
    # string is composed here rather than delegated to the runtime. Every argument is
    # quoted and any trailing backslash run is doubled, which is the documented Windows
    # command-line rule and is what keeps a path ending in a separator from escaping the
    # closing quote. A double quote inside an argument is not produced by any caller in
    # this library and is not accommodated by a fallback.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Argument)

    $parts = New-Object 'System.Collections.Generic.List[string]'
    foreach ($item in $Argument) {
        $trailingBackslashes = 0
        for ($index = $item.Length - 1; $index -ge 0; $index--) {
            if ($item[$index] -eq '\') {
                $trailingBackslashes++
            }
            else {
                break
            }
        }
        $escaped = $item
        if ($trailingBackslashes -gt 0) {
            $escaped = $item + ('\' * $trailingBackslashes)
        }
        $parts.Add('"' + $escaped + '"')
    }
    return ($parts -join ' ')
}

function Split-EgProcessOutputLines {
    # Split captured standard output into lines, dropping trailing empty entries.
    #
    # PowerShell unrolls a collection returned from a function, so this function emits its
    # lines and EVERY call site forces the collection shape with the array subexpression.
    # Pipeline scalar collapse is the direct cause of the Run119 output-shape defect
    # (design section 18.1): a one-line result must not collapse to a bare string and a
    # zero-line result must not collapse to $null.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][AllowNull()][string]$Text)

    if ([string]::IsNullOrEmpty($Text)) {
        return
    }

    $split = $Text -split "`r`n|`n"
    $last = $split.Length - 1
    while ($last -ge 0 -and $split[$last] -eq '') {
        $last--
    }
    if ($last -lt 0) {
        return
    }
    return $split[0..$last]
}

function Invoke-GovernedGit {
    # The single governed Git read. Returns a GovernedGitResult and never throws for a
    # non-zero Git exit.
    #
    # Standard output and standard error are captured through System.Diagnostics.Process
    # with redirected streams. Native stderr is never merged inline, because on Windows
    # PowerShell 5.1 that wraps each line in an ErrorRecord and falsifies the success
    # variable even for a process that exited zero (design section 10.1).
    #
    # Raw Git output text never reaches a log, a console summary, or any result field
    # other than Lines, and Lines is consumed by this library rather than emitted.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RepositoryRootPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string[]]$Arguments
    )

    # The allowlist gate runs first and starts no process. A prohibited subcommand can
    # therefore never reach the filesystem or the network, whatever else the caller passed.
    if (-not (Test-EgGitSubcommandAllowed -Subcommand $Arguments[0])) {
        return [pscustomobject]@{
            Success    = $false
            ExitCode   = [int](-1)
            Lines      = [string[]]@()
            SupportRef = 'EG_LAUNCHER_GIT_SUBCOMMAND_FORBIDDEN'
        }
    }

    $fullArguments = @('-C', $RepositoryRootPath, '--no-optional-locks') + $Arguments

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = 'git'
    $startInfo.Arguments = ConvertTo-EgNativeArgumentString -Argument $fullArguments
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true

    # Ambient Git variables are neutralised for the duration of the read and restored
    # exactly in the finally block. The repository is selected explicitly with -C, and
    # --no-optional-locks keeps a governed read from mutating the repository.
    $governedNames = @(Get-EgGovernedGitEnvironmentNames)
    $environmentSnapshot = Get-EgProcessEnvironmentSnapshot -Names $governedNames

    $exitCode = -1
    $standardOutput = ''
    $process = $null
    try {
        foreach ($governedName in $governedNames) {
            Set-EgProcessEnvironmentVariable -Name $governedName -Value $null
        }
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        [void]$process.Start()
        # Both streams are drained asynchronously before the wait, so neither can fill its
        # buffer and deadlock the child.
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $standardOutput = $outputTask.Result
        # The captured error stream is drained and discarded. It is deliberately never
        # placed in the result object, a log, or a console surface (design section 11.2).
        [void]$errorTask.Result
        $exitCode = $process.ExitCode
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
        [void](Restore-EgProcessEnvironmentSnapshot -Snapshot $environmentSnapshot)
    }

    $lines = @(Split-EgProcessOutputLines -Text $standardOutput)
    $supportRef = ''
    if ($exitCode -ne 0) {
        $supportRef = 'EG_LAUNCHER_GIT_INVOCATION_FAILED'
    }

    [pscustomobject]@{
        Success    = ($exitCode -eq 0)
        ExitCode   = [int]$exitCode
        Lines      = [string[]]$lines
        SupportRef = $supportRef
    }
}
