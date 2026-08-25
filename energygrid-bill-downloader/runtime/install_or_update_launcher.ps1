# Energy@Grid launcher installation and update entry script.
#
# Design lock: DL-XB-141-RUNTIME-005-SOURCE-DURABILITY.
# Controlling specification: energygrid-bill-downloader/docs/runtime_source_durability_design.md
#
# This script publishes the reviewed launcher from the deployed checkout into the launcher
# root. It is the only sanctioned way the installed launcher ever changes, and it is the
# SOLE ROLLBACK AUTHORITY in this design: neither publish primitive restores anything.
#
# The operator runs it FROM THE CHECKOUT. It is never itself deployed to the launcher root,
# because deploying an installer inside the surface it installs would be self-referential.
#
# What this script never does: it never writes into the Git checkout, never reads private
# configuration, never reads or decrypts credentials, never touches the archive, the SQLite
# state, the logs, or the Scheduled Task, and never mutates an access control list. There is
# no credential, browser-cache, scheduler, access-control, cleanup, or recovery parameter,
# and none may be added.

[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRoot,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRoot,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$AdmissionCommit,
    [switch]$ValidateOnly,
    [string]$LogRoot,
    [string]$RunId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'launcher_lib.ps1')

# --------------------------------------------------------------------------------------
# Bounded reporting
# --------------------------------------------------------------------------------------

$script:EgInstallerChecks = [ordered]@{}

function Set-EgInstallerCheck {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Name,
        [bool]$Pass
    )
    $outcome = 'FAIL'
    if ($Pass) { $outcome = 'PASS' }
    $script:EgInstallerChecks[$Name] = $outcome
}

function Write-EgInstallerStatus {
    # The installer's bounded real-path stdout shape (DD-06). It carries no path, no
    # environment value, no account identity, and no Git output text.
    param(
        [Parameter(Mandatory)]
        [ValidateSet('INSTALLED', 'ALREADY_CURRENT', 'FAILED_PREFLIGHT',
                     'FAILED_ROLLED_BACK', 'FAILED_ROLLBACK_INCOMPLETE')]
        [string]$Status,
        [AllowEmptyString()][string]$SupportRef = '',
        [int]$BackupsRemaining = 0
    )

    $payload = [ordered]@{}
    $payload['status'] = $Status
    $payload['support_ref'] = $SupportRef
    $payload['backups_remaining'] = $BackupsRemaining
    Write-Output ($payload | ConvertTo-Json -Depth 8 -Compress)
}

function Exit-EgInstallerPreflightFailure {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SupportRef)

    if ($ValidateOnly) {
        Write-Output (ConvertTo-EgValidationJson -Checks $script:EgInstallerChecks `
            -Status 'FAIL' -SupportRef $SupportRef)
    }
    else {
        Write-EgInstallerStatus -Status 'FAILED_PREFLIGHT' -SupportRef $SupportRef
    }
    exit $script:EgLauncherExitCodes['InstallPreMutationFailed']
}

# --------------------------------------------------------------------------------------
# Phase 1 - admission and prepare. NO DESTINATION MUTATION WHATSOEVER.
# --------------------------------------------------------------------------------------
# Any failure in this phase leaves the destination package byte-identical to its
# pre-transaction state and exits 71.

# Step 1a. The supplied roots resolve and are absolute.
$checkoutAbsolute = [System.IO.Path]::IsPathRooted($CheckoutRoot)
Set-EgInstallerCheck -Name 'checkout_root_absolute' -Pass $checkoutAbsolute
if (-not $checkoutAbsolute) {
    Exit-EgInstallerPreflightFailure -SupportRef 'EG_LAUNCHER_PATH_NOT_ABSOLUTE'
}

$launcherRootAbsolute = [System.IO.Path]::IsPathRooted($LauncherRoot)
Set-EgInstallerCheck -Name 'launcher_root_absolute' -Pass $launcherRootAbsolute
if (-not $launcherRootAbsolute) {
    Exit-EgInstallerPreflightFailure -SupportRef 'EG_LAUNCHER_PATH_NOT_ABSOLUTE'
}

$checkoutExists = (Test-Path -LiteralPath $CheckoutRoot -PathType Container)
Set-EgInstallerCheck -Name 'checkout_root_exists' -Pass $checkoutExists
if (-not $checkoutExists) {
    Exit-EgInstallerPreflightFailure -SupportRef 'EG_LAUNCHER_PATH_MISSING'
}

$launcherRootExists = (Test-Path -LiteralPath $LauncherRoot -PathType Container)
Set-EgInstallerCheck -Name 'launcher_root_exists' -Pass $launcherRootExists
if (-not $launcherRootExists) {
    Exit-EgInstallerPreflightFailure -SupportRef 'EG_LAUNCHER_PATH_MISSING'
}

# The launcher root must resolve outside the deployed checkout: the installed package is
# deployment state, not source (design section 17.2).
$resolvedCheckout = [System.IO.Path]::GetFullPath($CheckoutRoot).TrimEnd('\', '/')
$resolvedLauncherRoot = [System.IO.Path]::GetFullPath($LauncherRoot).TrimEnd('\', '/')
$launcherRootOutside = (-not (Test-EgPathIsWithin -CandidatePath $resolvedLauncherRoot -ContainerPath $resolvedCheckout))
Set-EgInstallerCheck -Name 'launcher_root_outside_checkout' -Pass $launcherRootOutside
if (-not $launcherRootOutside) {
    Exit-EgInstallerPreflightFailure -SupportRef 'EG_LAUNCHER_ROOT_INSIDE_CHECKOUT'
}

# Step 1b. Admission. -AdmissionCommit is the operator-controlled admission lane, and it
# is where an exact reviewed source commit legitimately belongs. It is verified against the
# deployed checkout's HEAD through a governed read-only Git invocation. This requirement
# stops at this lane: it is NOT inherited by normal unattended execution, which uses the
# path-scoped governed integrity contract instead.
$headRead = Invoke-GovernedGit -RepositoryRootPath $resolvedCheckout -Arguments @('rev-parse', 'HEAD')
$admissionOk = $false
if ($headRead.Success) {
    if ($headRead.Lines.Count -eq 1) {
        if ($headRead.Lines[0].Trim().ToLowerInvariant() -ceq $AdmissionCommit) {
            $admissionOk = $true
        }
    }
}
Set-EgInstallerCheck -Name 'admission_commit_matches_checkout_head' -Pass $admissionOk
if (-not $admissionOk) {
    Exit-EgInstallerPreflightFailure -SupportRef 'EG_LAUNCHER_INSTALL_ADMISSION_INVALID'
}

# Step 2. Resolve the exact deployable source set from the explicit enumeration.
$runtimeSourceDirectory = Join-Path (Join-Path $resolvedCheckout 'energygrid-bill-downloader') 'runtime'
$deployable = @(Get-EgDeployableSourceSet -RuntimeSourceDirectory $runtimeSourceDirectory)

# Step 3. Parse-check and SHA-256 every source file.
$sourceHashes = [ordered]@{}
$sourceOk = $true
foreach ($member in $deployable) {
    if (-not (Test-Path -LiteralPath $member.SourcePath -PathType Leaf)) {
        $sourceOk = $false
        break
    }
    if (-not (Test-EgPowerShellFileParsesCleanly -Path $member.SourcePath)) {
        $sourceOk = $false
        break
    }
    $sourceHashes[$member.Name] = (Get-EgFileSha256 -Path $member.SourcePath)
}
Set-EgInstallerCheck -Name 'source_members_present_and_parse_clean' -Pass $sourceOk
if (-not $sourceOk) {
    Exit-EgInstallerPreflightFailure -SupportRef 'EG_LAUNCHER_PACKAGE_PARSE_FAILED'
}

# Step 4. Classify every destination as Existing or Absent, BEFORE anything is written.
$preimages = [ordered]@{}
foreach ($member in $deployable) {
    $preimages[$member.Name] = Get-EgDestinationPreimage `
        -DestinationPath (Join-Path $resolvedLauncherRoot $member.Name)
}
$manifestDestination = Join-Path $resolvedLauncherRoot $script:EgManifestFileName
$preimages[$script:EgManifestFileName] = Get-EgDestinationPreimage -DestinationPath $manifestDestination

# Step 7. Construct the candidate manifest from the admitted source set and hashes.
$manifestEntries = @()
foreach ($member in $deployable) {
    $manifestEntries = $manifestEntries + ([ordered]@{
        name        = $member.Name
        sha256      = $sourceHashes[$member.Name]
        byte_length = [int](Get-Item -LiteralPath $member.SourcePath).Length
    })
}
$candidateManifest = New-EgInstallationManifestObject -MemberEntries $manifestEntries `
    -AdmissionCommit $AdmissionCommit
$candidateManifestJson = ConvertTo-EgManifestJson -ManifestObject $candidateManifest

# Step 8. Validate the candidate manifest against its shape rules.
$manifestShape = Test-EgInstallationManifestShape -ManifestObject $candidateManifest
Set-EgInstallerCheck -Name 'installation_manifest_shape' -Pass $manifestShape.Pass
if (-not $manifestShape.Pass) {
    Exit-EgInstallerPreflightFailure -SupportRef $manifestShape.SupportRef
}

# Step 5. Idempotency by HASH, not by timestamp. If every deployed member already matches
# its source hash and the manifest already agrees, mutate nothing.
$alreadyCurrent = $true
foreach ($member in $deployable) {
    if ($preimages[$member.Name].PreimageState -cne 'Existing') {
        $alreadyCurrent = $false
    }
    elseif ($preimages[$member.Name].PreimageSha256 -cne $sourceHashes[$member.Name]) {
        $alreadyCurrent = $false
    }
}
if ($alreadyCurrent) {
    if ($preimages[$script:EgManifestFileName].PreimageState -cne 'Existing') {
        $alreadyCurrent = $false
    }
    else {
        # The manifest AGREES when its parsed content matches the candidate. Agreement is
        # decided on content rather than on bytes, so a manifest serialised by a different
        # PowerShell edition does not force a needless republication.
        $installedComparison = Compare-EgInstalledPackageToManifest -LauncherRootPath $resolvedLauncherRoot
        if (-not $installedComparison.Pass) {
            $alreadyCurrent = $false
        }
        else {
            $installedManifest = $null
            try {
                $installedManifest = [System.IO.File]::ReadAllText($manifestDestination) | ConvertFrom-Json
            }
            catch {
                $alreadyCurrent = $false
            }
            if ($null -eq $installedManifest) {
                $alreadyCurrent = $false
            }
            elseif (([string]$installedManifest.admission_commit) -cne $AdmissionCommit) {
                $alreadyCurrent = $false
            }
        }
    }
}
Set-EgInstallerCheck -Name 'installed_package_already_current' -Pass $alreadyCurrent

if ($ValidateOnly) {
    # -ValidateOnly stops here. Every check above is read-only, so nothing has been
    # created, modified, deleted, or renamed: no staging file, no backup file, no log file,
    # no directory, and no transaction identifier (DD-09). ALREADY_CURRENT is a real-path
    # status and never appears as a validation check outcome (DD-06), so the check above
    # reports only whether a mutating run would have work to do.
    Write-Output (ConvertTo-EgValidationJson -Checks $script:EgInstallerChecks `
        -Status 'PASS' -SupportRef '')
    exit 0
}

if ($alreadyCurrent) {
    Write-EgInstallerStatus -Status 'ALREADY_CURRENT' -SupportRef '' -BackupsRemaining 0
    exit 0
}

# From here the run is a mutating transaction.
$operationId = New-EgOperationId
$transaction = New-EgTransactionState -OperationId $operationId

# Step 6. Write and fully verify EVERY staging file, for all changed members, BEFORE
# publishing any of them. Staging files are created in the destination directory only,
# never in a shared temporary directory, and are named exclusively by the reserved Class B
# contract, which is what makes them recognisable to the launcher later.
$stagingOk = $true
$stagingFailureRef = 'EG_LAUNCHER_INSTALL_STAGING_FAILED'
foreach ($member in $deployable) {
    $destination = Join-Path $resolvedLauncherRoot $member.Name
    $stagingPath = Join-Path $resolvedLauncherRoot (New-EgResidueName -Kind 'staging' `
        -Member $member.Name -OperationId $operationId)
    $backupPath = Join-Path $resolvedLauncherRoot (New-EgResidueName -Kind 'backup' `
        -Member $member.Name -OperationId $operationId)

    try {
        [System.IO.File]::WriteAllBytes($stagingPath, [System.IO.File]::ReadAllBytes($member.SourcePath))
    }
    catch {
        $stagingOk = $false
        break
    }
    if ((Get-EgFileSha256 -Path $stagingPath) -cne $sourceHashes[$member.Name]) {
        $stagingOk = $false
        break
    }
    if (-not (Test-EgPowerShellFileParsesCleanly -Path $stagingPath)) {
        $stagingOk = $false
        break
    }

    $entry = New-EgTransactionMember -Name $member.Name -DestinationPath $destination `
        -PreimageState $preimages[$member.Name].PreimageState `
        -PreimageSha256 $preimages[$member.Name].PreimageSha256 `
        -StagingPath $stagingPath -BackupPath $backupPath `
        -SourceSha256 $sourceHashes[$member.Name]
    $transaction.Members = $transaction.Members + $entry
}

# The manifest staging file is prepared in the same phase, so Phase 2 and Phase 3 both
# publish from fully verified staging bytes.
$manifestStagingPath = Join-Path $resolvedLauncherRoot (New-EgResidueName -Kind 'staging' `
    -Member $script:EgManifestFileName -OperationId $operationId)
$manifestBackupPath = Join-Path $resolvedLauncherRoot (New-EgResidueName -Kind 'backup' `
    -Member $script:EgManifestFileName -OperationId $operationId)
if ($stagingOk) {
    try {
        Write-EgUtf8NoBomText -Path $manifestStagingPath -Text $candidateManifestJson
    }
    catch {
        $stagingOk = $false
    }
}
$manifestStagingSha = ''
if ($stagingOk) {
    $manifestStagingSha = Get-EgFileSha256 -Path $manifestStagingPath
    if ([string]::IsNullOrEmpty($manifestStagingSha)) {
        $stagingOk = $false
    }
}

if (-not $stagingOk) {
    # A staging failure is still pre-mutation with respect to the installed package: no
    # destination has advanced. Staging residue is retained for inspection and carries a
    # reserved Class B name, so it does not fail the launcher's next preflight.
    Write-EgInstallerStatus -Status 'FAILED_PREFLIGHT' -SupportRef $stagingFailureRef `
        -BackupsRemaining 0
    exit $script:EgLauncherExitCodes['InstallPreMutationFailed']
}

# --------------------------------------------------------------------------------------
# Phase 2 - publish the executable members, in the recorded DD-10 order.
# --------------------------------------------------------------------------------------
# NO BACKUP IS DELETED IN THIS PHASE. The installer records, for every touched
# destination, its preimage state, its preimage hash where one existed, its backup path
# where one exists, and whether it has been published. That record is the transaction state
# Phase 4 and rollback both depend on.

$failureRef = ''
$transactionFailed = $false

foreach ($entry in $transaction.Members) {
    $published = $null
    if ($entry.PreimageState -ceq 'Existing') {
        $published = Invoke-AtomicFileReplace -SourcePath $entry.StagingPath `
            -DestinationPath $entry.DestinationPath -BackupPath $entry.BackupPath `
            -ExpectedSha256 $entry.SourceSha256
    }
    else {
        $published = Invoke-PublishToAbsentDestination -SourcePath $entry.StagingPath `
            -DestinationPath $entry.DestinationPath -ExpectedSha256 $entry.SourceSha256
    }

    $entry.PublicationOccurred = $published.PublicationOccurred
    $entry.BackupCreated = $published.BackupCreated

    if (-not $published.Success) {
        $transactionFailed = $true
        $failureRef = $published.SupportRef
        break
    }
    # The destination hash is verified immediately after each publication.
    if ((Get-EgFileSha256 -Path $entry.DestinationPath) -cne $entry.SourceSha256) {
        $transactionFailed = $true
        $failureRef = 'EG_LAUNCHER_REPLACE_POSTIMAGE_MISMATCH'
        break
    }
}

# --------------------------------------------------------------------------------------
# Phase 3 - publish and verify the manifest, INSIDE the same transaction.
# --------------------------------------------------------------------------------------
# The manifest is a package member, not an epilogue. It is published only after every
# executable member has individually verified, and its backup is retained exactly like any
# other member's.

if (-not $transactionFailed) {
    $manifestEntry = New-EgTransactionMember -Name $script:EgManifestFileName `
        -DestinationPath $manifestDestination `
        -PreimageState $preimages[$script:EgManifestFileName].PreimageState `
        -PreimageSha256 $preimages[$script:EgManifestFileName].PreimageSha256 `
        -StagingPath $manifestStagingPath -BackupPath $manifestBackupPath `
        -SourceSha256 $manifestStagingSha
    $transaction.Members = $transaction.Members + $manifestEntry

    $publishedManifest = $null
    if ($manifestEntry.PreimageState -ceq 'Existing') {
        $publishedManifest = Invoke-AtomicFileReplace -SourcePath $manifestEntry.StagingPath `
            -DestinationPath $manifestEntry.DestinationPath `
            -BackupPath $manifestEntry.BackupPath -ExpectedSha256 $manifestEntry.SourceSha256
    }
    else {
        $publishedManifest = Invoke-PublishToAbsentDestination `
            -SourcePath $manifestEntry.StagingPath `
            -DestinationPath $manifestEntry.DestinationPath `
            -ExpectedSha256 $manifestEntry.SourceSha256
    }
    $manifestEntry.PublicationOccurred = $publishedManifest.PublicationOccurred
    $manifestEntry.BackupCreated = $publishedManifest.BackupCreated

    if (-not $publishedManifest.Success) {
        $transactionFailed = $true
        $failureRef = $publishedManifest.SupportRef
    }
    else {
        # Re-read the published manifest from disk and verify the exact expected entry
        # set, hashes, byte lengths, admitted commit, and no extra or missing deployed
        # member. Then re-verify every deployed executable against the manifest just read
        # back, so the installed bytes and the record of them are proven mutually
        # consistent rather than assumed to be.
        $readBack = Compare-EgInstalledPackageToManifest -LauncherRootPath $resolvedLauncherRoot
        if (-not $readBack.Pass) {
            $transactionFailed = $true
            $failureRef = 'EG_LAUNCHER_INSTALL_MANIFEST_VERIFY_FAILED'
        }
        else {
            $reread = $null
            try {
                $reread = [System.IO.File]::ReadAllText($manifestDestination) | ConvertFrom-Json
            }
            catch {
                $reread = $null
            }
            if ($null -eq $reread) {
                $transactionFailed = $true
                $failureRef = 'EG_LAUNCHER_INSTALL_MANIFEST_VERIFY_FAILED'
            }
            elseif (([string]$reread.admission_commit) -cne $AdmissionCommit) {
                $transactionFailed = $true
                $failureRef = 'EG_LAUNCHER_INSTALL_MANIFEST_VERIFY_FAILED'
            }
            else {
                foreach ($entry in $transaction.Members) {
                    if ($entry.Name -ceq $script:EgManifestFileName) { continue }
                    if ((Get-EgFileSha256 -Path $entry.DestinationPath) -cne $entry.SourceSha256) {
                        $transactionFailed = $true
                        $failureRef = 'EG_LAUNCHER_INSTALL_MANIFEST_VERIFY_FAILED'
                    }
                }
            }
        }
    }
}

# --------------------------------------------------------------------------------------
# Failure before acceptance
# --------------------------------------------------------------------------------------
# Installer-owned package rollback is added by the next task. Until then a pre-acceptance
# failure is reported fail-closed as requiring manual owner action, and every artefact is
# retained for inspection. It is never reported as a success and never silently abandoned.

if ($transactionFailed) {
    if ([string]::IsNullOrEmpty($failureRef)) {
        $failureRef = 'EG_LAUNCHER_UNCLASSIFIED'
    }
    $retained = @(Get-ChildItem -LiteralPath $resolvedLauncherRoot -Force |
        Where-Object { (Test-EgResidueName -Name $_.Name).Kind -ceq 'backup' })
    Write-EgInstallerStatus -Status 'FAILED_ROLLBACK_INCOMPLETE' -SupportRef $failureRef `
        -BackupsRemaining @($retained).Count
    exit $script:EgLauncherExitCodes['InstallRollbackIncomplete']
}

# --------------------------------------------------------------------------------------
# Phase 4 - commit
# --------------------------------------------------------------------------------------
# The transaction is accepted only when every executable member has verified, the manifest
# has been published and read back correctly, and the full package re-verification has
# passed. Only AFTER acceptance may retained backups be reaped, which the next task
# implements. Staging files this transaction created are consumed by publication.

$remainingBackups = @(Get-ChildItem -LiteralPath $resolvedLauncherRoot -Force |
    Where-Object { (Test-EgResidueName -Name $_.Name).Kind -ceq 'backup' })
Write-EgInstallerStatus -Status 'INSTALLED' -SupportRef '' `
    -BackupsRemaining @($remainingBackups).Count
exit 0
