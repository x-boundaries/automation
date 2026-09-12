# XB-141 EnergyGrid Run156 owner-operated package deployment helper
#
# This file is a manual server-side helper. It is designed and reviewed on the owner
# laptop, but it must only be run by the owner from an already-elevated, same-account,
# interactive Windows PowerShell 5.1 Desktop session on the EnergyGrid server.
#
# The final repository commit is deliberately not embedded here. The operator supplies
# the exact server checkout HEAD, tree, and sole parent at execution time.
#
# Closed surfaces: no self-elevation, no persistent execution-policy change, no repository
# write, no ACL write, no credential/config write, no launcher/browser/portal/Scheduler/
# n8n/AutoCount action. The canonical installer is reached only in a child PowerShell
# process. This helper has no direct exit statement, so installer exit cannot terminate the
# owner's shell.

[CmdletBinding()]
param(
    [string]$ExpectedHead,
    [string]$ExpectedTree,
    [string]$ExpectedParent
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$script:R156Run = '2026-09-12-xb-141-energygrid-server-package-deploy-helper-156'
$script:R156Lock = 'DL-XB-141-SERVER-PACKAGE-DEPLOY-HELPER-156'
$script:R156Checkout = 'C:\XB\automation'
$script:R156LocatorRelative = 'X-Boundaries\EnergyGrid\runtime_locator.json'
$script:R156LocatorSchema = 'xb.energygrid.runtime_locator.v1'
$script:R156ManifestSchema = 'eg_launcher_installation_manifest/v1'
$script:R156ExpectedBranch = 'main'
$script:R156RuntimeRelative = 'energygrid-bill-downloader\runtime'
$script:R156InstallerRelative = 'energygrid-bill-downloader\runtime\install_or_update_launcher.ps1'
$script:R156LibraryRelative = 'energygrid-bill-downloader\runtime\launcher_lib.ps1'
$script:R156LauncherRelative = 'energygrid-bill-downloader\runtime\launcher.ps1'
$script:R156LibraryGitBlob = 'de75302dfb7ce3b1b4b37919d6b7e734d0503018'
$script:R156LibraryGitBlobLength = [int64]141393
$script:R156InstallerGitBlob = 'a5b670e3b043a026af1d7f2086df03fdb1e7fa13'
$script:R156InstallerGitBlobLength = [int64]25643
$script:R156LauncherGitBlob = 'd632068bbd5832cc46971278ca0f4fba3bf7e8f3'
$script:R156LauncherGitBlobLength = [int64]21081
$script:R156ManifestFileName = 'installation_manifest.json'
$script:R156PackageNames = @('launcher.ps1', 'launcher_lib.ps1', 'installation_manifest.json')
$script:R156ManifestNames = @('launcher.ps1', 'launcher_lib.ps1')
$script:R156GitReadOnlySubcommands = @(
    'symbolic-ref',
    'rev-parse',
    'rev-list',
    'status',
    'ls-remote',
    'hash-object',
    'cat-file',
    'config'
)
$script:R156CanonicalOrigins = @(
    'https://github.com/x-boundaries/automation',
    'https://github.com/x-boundaries/automation.git',
    'git@github.com:x-boundaries/automation',
    'git@github.com:x-boundaries/automation.git',
    'ssh://git@github.com/x-boundaries/automation',
    'ssh://git@github.com/x-boundaries/automation.git'
)
$script:R156ValidationCheckNames = @(
    'checkout_root_absolute',
    'launcher_root_absolute',
    'checkout_root_exists',
    'launcher_root_exists',
    'launcher_root_outside_checkout',
    'admission_commit_matches_checkout_head',
    'source_members_present_and_parse_clean',
    'installation_manifest_shape',
    'installed_package_already_current'
)
$script:R156WriteMask = [uint32](
    0x00000002 -bor
    0x00000004 -bor
    0x00000010 -bor
    0x00000040 -bor
    0x00000100 -bor
    0x00010000 -bor
    0x00040000 -bor
    0x00080000
)
$script:R156AncestorMask = [uint32](
    0x00000040 -bor
    0x00010000 -bor
    0x00040000 -bor
    0x00080000
)
$script:R156GenericRead = [uint32]0x00120089
$script:R156GenericWrite = [uint32]0x00120116
$script:R156GenericExecute = [uint32]0x001200A0
$script:R156AllAccess = [uint32]0x001F01FF
$script:R156RealStarted = $false
$script:R156RealInstallerInvocations = 0
$script:R156AuthorityConsumed = 'NO'
$script:R156Validation = 'NOT_RUN'
$script:R156RaceDetected = 'NO'
$script:R156PackageMutation = 'NONE'
$script:R156InstallerStatus = 'NOT_RUN'
$script:R156SupportRef = 'EG_R156_UNEXPECTED_INFRASTRUCTURE'
$script:R156GithubAuth = 'FAIL'
$script:R156PostManifest = 'NOT_RUN'
$script:R156PostPackageManifest = 'NOT_RUN'
$script:R156CanonicalEquivalence = 'NOT_RUN'
$script:R156LauncherClassification = 'NOT_RUN'
$script:R156LocatorContinuity = 'NOT_RUN'
$script:R156RepositoryContinuity = 'NOT_RUN'

function Write-R156Terminal {
    param(
        [Parameter(Mandatory)][ValidateSet(
            'PASS',
            'CONTROLLER_REQUIRED_PRE_DISPATCH',
            'CONTROLLER_REQUIRED_POST_DISPATCH'
        )][string]$Terminal,
        [Parameter(Mandatory)][ValidateSet(
            'PASS',
            'CONTROLLER_REQUIRED',
            'POST_DISPATCH_UNCERTAIN'
        )][string]$Disposition
    )

    $fields = [ordered]@{}
    $fields['terminal'] = $Terminal
    $fields['RUN'] = $script:R156Run
    $fields['LOCK'] = $script:R156Lock
    $fields['github_auth'] = $script:R156GithubAuth
    $fields['disposition'] = $Disposition
    $fields['support_ref'] = [string]$script:R156SupportRef
    $fields['validate_only'] = $script:R156Validation
    $fields['race_detected'] = $script:R156RaceDetected
    $fields['real_installer_invocations'] = [int]$script:R156RealInstallerInvocations
    $fields['authority_consumed'] = $script:R156AuthorityConsumed
    $fields['package_mutation'] = $script:R156PackageMutation
    $fields['installer_status'] = $script:R156InstallerStatus
    $fields['post_manifest'] = $script:R156PostManifest
    $fields['post_package_to_manifest'] = $script:R156PostPackageManifest
    $fields['canonical_equivalence'] = $script:R156CanonicalEquivalence
    $fields['launcher_classification'] = $script:R156LauncherClassification
    $fields['locator_continuity'] = $script:R156LocatorContinuity
    $fields['repository_continuity'] = $script:R156RepositoryContinuity
    $fields['retry_allowed'] = 'NO'
    $fields['live_energygrid_activity'] = 'NONE'
    $fields['browser_portal_activity'] = 'NONE'
    $fields['scheduler_n8n_autocount_activity'] = 'NONE'
    $fields['secret_exposure'] = 'none'
    foreach ($name in $fields.Keys) {
        Write-Output ($name + '=' + [string]$fields[$name])
    }
}

function Stop-R156Gate {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SupportRef)

    $script:R156SupportRef = $SupportRef
    throw 'bounded gate stop'
}

function Test-R156CommitText {
    param([AllowEmptyString()][string]$Value)

    if ($null -eq $Value) {
        return $false
    }
    return ($Value -cmatch '^[0-9a-f]{40}$')
}

function Convert-R156BytesToHex {
    param([Parameter(Mandatory)][byte[]]$Bytes)

    return ([BitConverter]::ToString($Bytes).Replace('-', '').ToLowerInvariant())
}

function Get-R156Sha256ForBytes {
    param([Parameter(Mandatory)][byte[]]$Bytes)

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return (Convert-R156BytesToHex -Bytes $algorithm.ComputeHash($Bytes))
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-R156Properties {
    param([Parameter(Mandatory)]$Object)

    $names = @()
    foreach ($property in @($Object.PSObject.Properties)) {
        $names = $names + [string]$property.Name
    }
    return $names
}

function Test-R156ExactNameMultiset {
    param(
        [AllowEmptyCollection()][string[]]$Actual,
        [AllowEmptyCollection()][string[]]$Expected
    )

    $left = @($Actual)
    $right = @($Expected)
    if ($left.Count -ne $right.Count) {
        return $false
    }
    foreach ($name in $right) {
        $matches = 0
        foreach ($candidate in $left) {
            if ([string]::Equals([string]$candidate, [string]$name, [StringComparison]::Ordinal)) {
                $matches++
            }
        }
        $expectedMatches = 0
        foreach ($candidate in $right) {
            if ([string]::Equals([string]$candidate, [string]$name, [StringComparison]::Ordinal)) {
                $expectedMatches++
            }
        }
        if ($matches -ne $expectedMatches) {
            return $false
        }
    }
    return $true
}

function Test-R156ExactPropertySet {
    param(
        [Parameter(Mandatory)]$Object,
        [Parameter(Mandatory)][string[]]$Expected
    )

    return (Test-R156ExactNameMultiset -Actual (Get-R156Properties -Object $Object) -Expected $Expected)
}

function Get-R156FullPath {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    return [System.IO.Path]::GetFullPath($Path)
}

function Test-R156SamePath {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Left,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Right
    )

    return [string]::Equals(
        (Get-R156FullPath -Path $Left),
        (Get-R156FullPath -Path $Right),
        [StringComparison]::OrdinalIgnoreCase)
}

function Test-R156PathWithin {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Candidate,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Container
    )

    $candidateFull = (Get-R156FullPath -Path $Candidate).TrimEnd([char[]]"\/")
    $containerFull = (Get-R156FullPath -Path $Container).TrimEnd([char[]]"\/")
    if ([string]::Equals($candidateFull, $containerFull, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $containerFull + [System.IO.Path]::DirectorySeparatorChar
    return $candidateFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Test-R156NormalDirectory {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    try {
        $item = Get-Item -LiteralPath $Path -Force
        return (
            $item.PSIsContainer -and
            (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0)
        )
    }
    catch {
        return $false
    }
}

function Test-R156NormalFile {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    try {
        $item = Get-Item -LiteralPath $Path -Force
        return (
            (-not $item.PSIsContainer) -and
            (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0)
        )
    }
    catch {
        return $false
    }
}

function Get-R156Metadata {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    $item = Get-Item -LiteralPath $Path -Force
    $isFile = -not $item.PSIsContainer
    $length = $null
    if ($isFile) {
        $length = [int64]$item.Length
    }
    return [pscustomobject]@{
        IsFile = $isFile
        IsDirectory = [bool]$item.PSIsContainer
        Length = $length
        LastWriteTimeUtcTicks = $item.LastWriteTimeUtc.Ticks
        Attributes = [int64]$item.Attributes
        IsReparsePoint = (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
    }
}

function Test-R156MetadataEqual {
    param(
        [Parameter(Mandatory)]$Before,
        [Parameter(Mandatory)]$After
    )

    if ([bool]$Before.IsFile -ne [bool]$After.IsFile) { return $false }
    if ([bool]$Before.IsDirectory -ne [bool]$After.IsDirectory) { return $false }
    if ([bool]$Before.IsReparsePoint -ne [bool]$After.IsReparsePoint) { return $false }
    if ([int64]$Before.LastWriteTimeUtcTicks -ne [int64]$After.LastWriteTimeUtcTicks) { return $false }
    if ([int64]$Before.Attributes -ne [int64]$After.Attributes) { return $false }
    if ($Before.IsFile -and ([int64]$Before.Length -ne [int64]$After.Length)) { return $false }
    return $true
}

function Get-R156ParentDirectory {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    $full = Get-R156FullPath -Path $Path
    $parent = [System.IO.Directory]::GetParent($full)
    if ($null -eq $parent) {
        return $null
    }
    return $parent.FullName
}

function Get-R156AncestorDirectories {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    $full = Get-R156FullPath -Path $Path
    $item = Get-Item -LiteralPath $full -Force
    $current = $full
    if (-not $item.PSIsContainer) {
        $current = Get-R156ParentDirectory -Path $full
    }
    $result = @()
    while ($null -ne $current) {
        $result = $result + $current
        $root = [System.IO.Path]::GetPathRoot($current)
        if ([string]::Equals($current, $root, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $next = Get-R156ParentDirectory -Path $current
        if ($null -eq $next) {
            break
        }
        $current = $next
    }
    return $result
}

function ConvertTo-R156NativeArgument {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-R156Git {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RepositoryRoot,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Arguments
    )

    if (@($Arguments).Count -eq 0 -or
        $script:R156GitReadOnlySubcommands -notcontains ([string]$Arguments[0])) {
        return [pscustomobject]@{
            Success = $false
            ExitCode = -1
            Lines = [string[]]@()
        }
    }

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = 'git.exe'
    $quoted = @()
    foreach ($argument in @('--no-optional-locks') + @($Arguments)) {
        $quoted = $quoted + (ConvertTo-R156NativeArgument -Value ([string]$argument))
    }
    $startInfo.Arguments = [string]::Join(' ', $quoted)
    $startInfo.WorkingDirectory = (Get-R156FullPath -Path $RepositoryRoot)
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $inheritedGitNames = @($startInfo.EnvironmentVariables.Keys |
        Where-Object { ([string]$_) -like 'GIT_*' })
    foreach ($name in $inheritedGitNames) {
        [void]$startInfo.EnvironmentVariables.Remove([string]$name)
    }
    $startInfo.EnvironmentVariables['GIT_TERMINAL_PROMPT'] = '0'
    $startInfo.EnvironmentVariables['GIT_OPTIONAL_LOCKS'] = '0'
    $startInfo.EnvironmentVariables['GIT_CONFIG_NOSYSTEM'] = '1'
    $startInfo.EnvironmentVariables['GIT_CONFIG_GLOBAL'] = 'NUL'

    $process = $null
    try {
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        [void]$process.Start()
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = [string]$stdoutTask.Result
        $null = $stderrTask.Result
        $lines = @()
        if (-not [string]::IsNullOrEmpty($stdout)) {
            $lines = @($stdout -split '\r?\n')
            if ($lines.Count -gt 0 -and [string]::IsNullOrEmpty($lines[$lines.Count - 1])) {
                $lines = @($lines[0..($lines.Count - 2)])
            }
        }
        return [pscustomobject]@{
            Success = ($process.ExitCode -eq 0)
            ExitCode = [int]$process.ExitCode
            Lines = [string[]]$lines
        }
    }
    catch {
        return [pscustomobject]@{
            Success = $false
            ExitCode = -1
            Lines = [string[]]@()
        }
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
    }
}

function Get-R156RepositoryState {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RepositoryRoot)

    $branch = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @('symbolic-ref', '--short', '-q', 'HEAD')
    $head = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @('rev-parse', '--verify', 'HEAD')
    $tree = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @('rev-parse', '--verify', 'HEAD^{tree}')
    $parents = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @('rev-list', '--parents', '-n', '1', 'HEAD')
    $status = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @(
        'status', '--porcelain=v1', '--untracked-files=all'
    )
    $topLevel = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @(
        'rev-parse', '--show-toplevel'
    )
    $insideWorkTree = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @(
        'rev-parse', '--is-inside-work-tree'
    )
    $gitDirectory = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @(
        'rev-parse', '--git-dir'
    )
    $commonDirectory = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @(
        'rev-parse', '--git-common-dir'
    )
    $origin = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @(
        'config', '--no-includes', '--local', '--get-all', 'remote.origin.url'
    )

    $branchValue = ''
    if ($branch.Success -and $branch.Lines.Count -eq 1) {
        $branchValue = $branch.Lines[0].Trim()
    }
    $headValue = ''
    if ($head.Success -and $head.Lines.Count -eq 1) {
        $headValue = $head.Lines[0].Trim().ToLowerInvariant()
    }
    $treeValue = ''
    if ($tree.Success -and $tree.Lines.Count -eq 1) {
        $treeValue = $tree.Lines[0].Trim().ToLowerInvariant()
    }
    $parentValue = ''
    $parentCount = -1
    if ($parents.Success -and $parents.Lines.Count -eq 1) {
        $parts = @($parents.Lines[0].Trim() -split '\s+')
        if ($parts.Count -ge 1) {
            $parentCount = $parts.Count - 1
        }
        if ($parts.Count -eq 2) {
            $parentValue = $parts[1].ToLowerInvariant()
        }
    }
    $topLevelValue = ''
    if ($topLevel.Success -and $topLevel.Lines.Count -eq 1) {
        $topLevelValue = $topLevel.Lines[0].Trim()
    }
    $insideWorkTreeValue = ''
    if ($insideWorkTree.Success -and $insideWorkTree.Lines.Count -eq 1) {
        $insideWorkTreeValue = $insideWorkTree.Lines[0].Trim().ToLowerInvariant()
    }
    $gitDirectoryValue = ''
    if ($gitDirectory.Success -and $gitDirectory.Lines.Count -eq 1) {
        $gitDirectoryValue = Resolve-R156RepositoryPath `
            -RepositoryRoot $RepositoryRoot -Value $gitDirectory.Lines[0].Trim()
    }
    $commonDirectoryValue = ''
    if ($commonDirectory.Success -and $commonDirectory.Lines.Count -eq 1) {
        $commonDirectoryValue = Resolve-R156RepositoryPath `
            -RepositoryRoot $RepositoryRoot -Value $commonDirectory.Lines[0].Trim()
    }
    $originValues = @()
    if ($origin.Success) {
        foreach ($line in @($origin.Lines)) {
            $originValues = $originValues + ([string]$line).Trim()
        }
    }
    return [pscustomobject]@{
        Branch = $branchValue
        Head = $headValue
        Tree = $treeValue
        Parent = $parentValue
        ParentCount = $parentCount
        TopLevel = $topLevelValue
        InsideWorkTree = $insideWorkTreeValue
        GitDirectory = $gitDirectoryValue
        CommonDirectory = $commonDirectoryValue
        Origin = [string[]]$originValues
        Clean = (
            $status.Success -and
            $status.Lines.Count -eq 0
        )
        ReadOk = (
            $branch.Success -and
            $head.Success -and
            $tree.Success -and
            $parents.Success -and
            $status.Success -and
            $topLevel.Success -and
            $insideWorkTree.Success -and
            $gitDirectory.Success -and
            $commonDirectory.Success -and
            $origin.Success
        )
    }
}

function Resolve-R156RepositoryPath {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RepositoryRoot,
        [AllowEmptyString()][string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ''
    }
    try {
        $root = Get-R156FullPath -Path $RepositoryRoot
        if ([System.IO.Path]::IsPathRooted($Value)) {
            return Get-R156FullPath -Path $Value
        }
        return Get-R156FullPath -Path (Join-Path $root $Value)
    }
    catch {
        return ''
    }
}

function Test-R156CanonicalOrigin {
    param([AllowEmptyCollection()][string[]]$Origin)

    $values = @($Origin)
    if ($values.Count -ne 1) {
        return $false
    }
    foreach ($expected in @($script:R156CanonicalOrigins)) {
        if ([string]::Equals(
                ([string]$values[0]),
                $expected,
                [StringComparison]::Ordinal)) {
            return $true
        }
    }
    return $false
}

function Test-R156RepositoryIdentity {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RepositoryRoot
    )

    if (-not $State.ReadOk -or
        [string]::IsNullOrWhiteSpace([string]$State.TopLevel) -or
        [string]::IsNullOrWhiteSpace([string]$State.GitDirectory) -or
        [string]::IsNullOrWhiteSpace([string]$State.CommonDirectory)) {
        return $false
    }
    try {
        $expectedRoot = Get-R156FullPath -Path $RepositoryRoot
        $expectedGitMetadata = Get-R156FullPath -Path (Join-Path $expectedRoot '.git')
        if (-not (Test-R156SamePath -Left $State.TopLevel -Right $expectedRoot)) {
            return $false
        }
        if ([string]$State.InsideWorkTree -cne 'true') {
            return $false
        }
        if (-not (Test-R156NormalFile -Path $expectedGitMetadata) -and
            -not (Test-R156NormalDirectory -Path $expectedGitMetadata)) {
            return $false
        }
        if (-not (Test-R156SamePath -Left $State.CommonDirectory -Right $expectedGitMetadata)) {
            return $false
        }
        if (-not (Test-R156NormalDirectory -Path $State.CommonDirectory) -or
            -not (Test-R156NormalDirectory -Path $State.GitDirectory)) {
            return $false
        }
        if (-not (Test-R156PathWithin -Candidate $State.GitDirectory -Container $State.CommonDirectory)) {
            return $false
        }
        return (Test-R156CanonicalOrigin -Origin $State.Origin)
    }
    catch {
        return $false
    }
}

function Test-R156RepositoryFence {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$ExpectedHeadValue,
        [Parameter(Mandatory)][string]$ExpectedTreeValue,
        [Parameter(Mandatory)][string]$ExpectedParentValue
    )

    return (
        $State.ReadOk -and
        (Test-R156RepositoryIdentity -State $State -RepositoryRoot $RepositoryRoot) -and
        $State.Branch -ceq $script:R156ExpectedBranch -and
        $State.Head -ceq $ExpectedHeadValue -and
        $State.Tree -ceq $ExpectedTreeValue -and
        $State.Parent -ceq $ExpectedParentValue -and
        $State.ParentCount -eq 1 -and
        $State.Clean
    )
}

function Test-R156RemoteHead {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RepositoryRoot,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedHeadValue
    )

    $remote = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @(
        'ls-remote', 'origin', 'refs/heads/main'
    )
    if (-not $remote.Success -or $remote.Lines.Count -ne 1) {
        return $false
    }
    $parts = @($remote.Lines[0].Trim() -split '[ \t]+')
    if ($parts.Count -ne 2) {
        return $false
    }
    return (
        ($parts[0] -cmatch '^[0-9a-f]{40}$') -and
        $parts[0].ToLowerInvariant() -ceq $ExpectedHeadValue -and
        $parts[1] -ceq 'refs/heads/main'
    )
}

function Get-R156SourceBytes {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    if (-not (Test-R156NormalFile -Path $Path)) {
        return $null
    }
    return [System.IO.File]::ReadAllBytes($Path)
}

function Test-R156PowerShellParse {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path,
        [Parameter(Mandatory)][byte[]]$Bytes
    )

    try {
        $encoding = New-Object System.Text.UTF8Encoding($false, $true)
        $text = $encoding.GetString($Bytes)
        $tokens = $null
        $errors = $null
        [void][System.Management.Automation.Language.Parser]::ParseInput(
            $text,
            [ref]$tokens,
            [ref]$errors
        )
        return (@($errors).Count -eq 0)
    }
    catch {
        return $false
    }
}

function Read-R156TrustedSource {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RepositoryRoot,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RelativePath,
        [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedGitBlob,
        [Parameter(Mandatory)][ValidateRange(1, [int64]::MaxValue)][int64]$ExpectedGitBlobLength
    )

    $path = Join-Path $RepositoryRoot ($RelativePath.Replace('/', '\'))
    $bytes = Get-R156SourceBytes -Path $path
    if ($null -eq $bytes) {
        return $null
    }
    $blob = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @(
        'hash-object', ('--path=' + $RelativePath.Replace('\', '/')),
        $RelativePath.Replace('\', '/')
    )
    $blobValue = ''
    if ($blob.Success -and $blob.Lines.Count -eq 1) {
        $blobValue = $blob.Lines[0].Trim().ToLowerInvariant()
    }
    if ($blobValue -notmatch '^[0-9a-f]{40}$') {
        return $null
    }
    if ($blobValue -cne $ExpectedGitBlob) {
        return $null
    }
    $blobSize = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @(
        'cat-file', '-s', ('HEAD:' + $RelativePath.Replace('\', '/'))
    )
    $blobSizeValue = ''
    if ($blobSize.Success -and $blobSize.Lines.Count -eq 1) {
        $blobSizeValue = $blobSize.Lines[0].Trim()
    }
    if ($blobSizeValue -notmatch '^[0-9]+$') {
        return $null
    }
    try {
        if ([int64]$blobSizeValue -ne $ExpectedGitBlobLength) {
            return $null
        }
    }
    catch {
        return $null
    }
    $headBlob = Invoke-R156Git -RepositoryRoot $RepositoryRoot -Arguments @(
        'rev-parse', ('HEAD:' + $RelativePath.Replace('\', '/'))
    )
    if (-not $headBlob.Success -or $headBlob.Lines.Count -ne 1) {
        return $null
    }
    if ($blobValue -cne $headBlob.Lines[0].Trim().ToLowerInvariant()) {
        return $null
    }
    if (-not (Test-R156PowerShellParse -Path $path -Bytes $bytes)) {
        return $null
    }
    return [pscustomobject]@{
        Path = $path
        RelativePath = $RelativePath
        Bytes = $bytes
        Sha256 = Get-R156Sha256ForBytes -Bytes $bytes
        ByteLength = [int64]$bytes.Length
    }
}

function Read-R156StrictJsonObject {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    if (-not (Test-R156NormalFile -Path $Path)) {
        return $null
    }
    try {
        $raw = [System.IO.File]::ReadAllText($Path)
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $null
        }
        $parsed = @($raw | ConvertFrom-Json)
        if ($parsed.Count -ne 1) {
            return $null
        }
        if ($parsed[0] -isnot [System.Management.Automation.PSCustomObject]) {
            return $null
        }
        return $parsed[0]
    }
    catch {
        return $null
    }
}

function Read-R156Locator {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LocatorPath)

    $locator = Read-R156StrictJsonObject -Path $LocatorPath
    if ($null -eq $locator) {
        return $null
    }
    if (-not (Test-R156ExactPropertySet -Object $locator -Expected @('schema', 'runtime_root'))) {
        return $null
    }
    if ($locator.schema -isnot [string] -or $locator.runtime_root -isnot [string]) {
        return $null
    }
    if ([string]$locator.schema -cne $script:R156LocatorSchema) {
        return $null
    }
    $rootValue = [string]$locator.runtime_root
    if ([string]::IsNullOrWhiteSpace($rootValue)) {
        return $null
    }
    if (-not [System.IO.Path]::IsPathRooted($rootValue)) {
        return $null
    }
    if (-not (Test-R156NormalDirectory -Path $rootValue)) {
        return $null
    }
    return [pscustomobject]@{
        RuntimeRoot = Get-R156FullPath -Path $rootValue
        Metadata = Get-R156Metadata -Path $LocatorPath
    }
}

$script:R156NativeSource = @'
using System;
using System.Runtime.InteropServices;
using System.Text;

namespace EgR156 {
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
    public struct SID_AND_ATTRIBUTES {
        public IntPtr Sid;
        public uint Attributes;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct TOKEN_USER_LAYOUT {
        public SID_AND_ATTRIBUTES User;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct TOKEN_OWNER_LAYOUT {
        public IntPtr Owner;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct TOKEN_GROUPS_LAYOUT {
        public uint GroupCount;
        public SID_AND_ATTRIBUTES Groups;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct TOKEN_PRIVILEGES_LAYOUT {
        public uint PrivilegeCount;
        public LUID_AND_ATTRIBUTES Privileges;
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

    public sealed class TokenGroupRecord {
        public byte[] Sid;
        public uint Attributes;
    }

    public sealed class AccessCheckOutcome {
        public bool Evaluated;
        public uint GrantedAccess;
        public bool AccessStatus;
        public int LastError;
    }

    public static class Native {
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr GetCurrentProcess();

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool OpenProcessToken(
            IntPtr processHandle,
            uint desiredAccess,
            out IntPtr tokenHandle);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool DuplicateTokenEx(
            IntPtr existingToken,
            uint desiredAccess,
            IntPtr tokenAttributes,
            int impersonationLevel,
            int tokenType,
            out IntPtr newToken);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool GetTokenInformation(
            IntPtr tokenHandle,
            int tokenInformationClass,
            IntPtr tokenInformation,
            int tokenInformationLength,
            out int returnLength);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool AccessCheck(
            byte[] securityDescriptor,
            IntPtr clientToken,
            uint desiredAccess,
            ref GENERIC_MAPPING genericMapping,
            IntPtr privilegeSet,
            ref int privilegeSetLength,
            out uint grantedAccess,
            out bool accessStatus);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern void MapGenericMask(
            ref uint accessMask,
            ref GENERIC_MAPPING genericMapping);

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true,
            EntryPoint = "LookupPrivilegeNameW")]
        private static extern bool LookupPrivilegeNameW(
            [MarshalAs(UnmanagedType.LPWStr)] string systemName,
            ref LUID luid,
            StringBuilder privilegeName,
            ref int cchName);

        private const uint TOKEN_DUPLICATE = 0x0002;
        private const uint TOKEN_QUERY = 0x0008;
        private const uint MAXIMUM_ALLOWED = 0x02000000;
        private const int SECURITY_IDENTIFICATION = 2;
        private const int TOKEN_IMPERSONATION = 2;
        private const int TOKEN_USER_CLASS = 1;
        private const int TOKEN_GROUPS_CLASS = 2;
        private const int TOKEN_PRIVILEGES_CLASS = 3;
        private const int TOKEN_OWNER_CLASS = 4;
        private const int TOKEN_ELEVATION_TYPE_CLASS = 18;
        private const int TOKEN_LINKED_TOKEN_CLASS = 19;
        private const int ERROR_INSUFFICIENT_BUFFER = 122;
        private const int PRIVILEGE_SET_SLACK_ENTRIES = 64;

        public static void CloseToken(IntPtr tokenHandle) {
            if (tokenHandle != IntPtr.Zero) {
                CloseHandle(tokenHandle);
            }
        }

        public static IntPtr OpenCurrentProcessToken() {
            IntPtr token = IntPtr.Zero;
            if (!OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_QUERY, out token)) {
                return IntPtr.Zero;
            }
            return token;
        }

        private static bool HasBufferRange(
            int bufferLength,
            long offset,
            long length) {
            if (bufferLength <= 0 || offset < 0 || length < 0) {
                return false;
            }
            if (offset > (long)bufferLength) {
                return false;
            }
            return length <= ((long)bufferLength - offset);
        }

        private static bool TryGetTokenInformationBuffer(
            IntPtr token,
            int informationClass,
            out IntPtr buffer,
            out int returnedLength) {
            buffer = IntPtr.Zero;
            returnedLength = 0;
            IntPtr allocated = IntPtr.Zero;
            try {
                if (token == IntPtr.Zero) {
                    return false;
                }
                int required = 0;
                bool first = GetTokenInformation(
                    token,
                    informationClass,
                    IntPtr.Zero,
                    0,
                    out required);
                int firstError = Marshal.GetLastWin32Error();
                if (first ||
                    firstError != ERROR_INSUFFICIENT_BUFFER ||
                    required <= 0) {
                    return false;
                }
                allocated = Marshal.AllocHGlobal(required);
                int returned = 0;
                if (!GetTokenInformation(
                        token,
                        informationClass,
                        allocated,
                        required,
                        out returned) ||
                    returned <= 0 ||
                    returned > required) {
                    return false;
                }
                buffer = allocated;
                returnedLength = returned;
                allocated = IntPtr.Zero;
                return true;
            }
            catch {
                buffer = IntPtr.Zero;
                returnedLength = 0;
                return false;
            }
            finally {
                if (allocated != IntPtr.Zero) {
                    Marshal.FreeHGlobal(allocated);
                }
            }
        }

        private static bool TryGetBufferOffset(
            IntPtr buffer,
            int bufferLength,
            IntPtr target,
            int minimumLength,
            out int offset) {
            offset = 0;
            if (buffer == IntPtr.Zero ||
                target == IntPtr.Zero ||
                bufferLength <= 0 ||
                minimumLength < 0 ||
                minimumLength > bufferLength) {
                return false;
            }
            long bufferAddress = buffer.ToInt64();
            long targetAddress = target.ToInt64();
            if (bufferAddress <= 0 || targetAddress <= 0) {
                return false;
            }
            long bufferEnd;
            try {
                bufferEnd = checked(bufferAddress + (long)bufferLength);
            }
            catch {
                return false;
            }
            if (bufferEnd <= bufferAddress ||
                targetAddress < bufferAddress ||
                targetAddress > bufferEnd - (long)minimumLength) {
                return false;
            }
            long difference = targetAddress - bufferAddress;
            if (difference < 0 || difference > Int32.MaxValue) {
                return false;
            }
            offset = (int)difference;
            return true;
        }

        private static bool TryCopySidFromBuffer(
            IntPtr buffer,
            int bufferLength,
            IntPtr sid,
            out byte[] sidBytes) {
            sidBytes = null;
            const int SID_HEADER_LENGTH = 8;
            const int SID_MAX_SUB_AUTHORITIES = 15;
            int sidOffset = 0;
            if (!TryGetBufferOffset(
                    buffer,
                    bufferLength,
                    sid,
                    SID_HEADER_LENGTH,
                    out sidOffset)) {
                return false;
            }
            try {
                int revision = Marshal.ReadByte(buffer, sidOffset);
                int subAuthorityCount = Marshal.ReadByte(buffer, sidOffset + 1);
                if (revision != 1 ||
                    subAuthorityCount < 0 ||
                    subAuthorityCount > SID_MAX_SUB_AUTHORITIES) {
                    return false;
                }
                long sidLength = SID_HEADER_LENGTH +
                    (4L * (long)subAuthorityCount);
                if (!HasBufferRange(bufferLength, sidOffset, sidLength)) {
                    return false;
                }
                byte[] copy = new byte[(int)sidLength];
                Marshal.Copy(
                    IntPtr.Add(buffer, sidOffset),
                    copy,
                    0,
                    copy.Length);
                sidBytes = copy;
                return true;
            }
            catch {
                sidBytes = null;
                return false;
            }
        }

        public static byte[] ReadTokenUserSid(IntPtr token) {
            IntPtr buffer = IntPtr.Zero;
            int returnedLength = 0;
            if (!TryGetTokenInformationBuffer(
                    token,
                    TOKEN_USER_CLASS,
                    out buffer,
                    out returnedLength)) {
                return null;
            }
            try {
                int structureLength = Marshal.SizeOf(typeof(TOKEN_USER_LAYOUT));
                long sidOffset = Marshal.OffsetOf(
                    typeof(TOKEN_USER_LAYOUT), "User").ToInt64();
                if (structureLength <= 0 ||
                    returnedLength < structureLength ||
                    sidOffset < 0 ||
                    sidOffset > Int32.MaxValue ||
                    !HasBufferRange(returnedLength, sidOffset, IntPtr.Size)) {
                    return null;
                }
                IntPtr sid = Marshal.ReadIntPtr(buffer, (int)sidOffset);
                byte[] sidBytes = null;
                if (!TryCopySidFromBuffer(
                        buffer,
                        returnedLength,
                        sid,
                        out sidBytes)) {
                    return null;
                }
                return sidBytes;
            }
            catch {
                return null;
            }
            finally {
                Marshal.FreeHGlobal(buffer);
            }
        }

        public static byte[] ReadTokenOwnerSid(IntPtr token) {
            IntPtr buffer = IntPtr.Zero;
            int returnedLength = 0;
            if (!TryGetTokenInformationBuffer(
                    token,
                    TOKEN_OWNER_CLASS,
                    out buffer,
                    out returnedLength)) {
                return null;
            }
            try {
                int structureLength = Marshal.SizeOf(typeof(TOKEN_OWNER_LAYOUT));
                long sidOffset = Marshal.OffsetOf(
                    typeof(TOKEN_OWNER_LAYOUT), "Owner").ToInt64();
                if (structureLength <= 0 ||
                    returnedLength < structureLength ||
                    sidOffset < 0 ||
                    sidOffset > Int32.MaxValue ||
                    !HasBufferRange(returnedLength, sidOffset, IntPtr.Size)) {
                    return null;
                }
                IntPtr sid = Marshal.ReadIntPtr(buffer, (int)sidOffset);
                byte[] sidBytes = null;
                if (!TryCopySidFromBuffer(
                        buffer,
                        returnedLength,
                        sid,
                        out sidBytes)) {
                    return null;
                }
                return sidBytes;
            }
            catch {
                return null;
            }
            finally {
                Marshal.FreeHGlobal(buffer);
            }
        }

        public static TokenGroupRecord[] ReadTokenGroups(IntPtr token) {
            IntPtr buffer = IntPtr.Zero;
            int returnedLength = 0;
            if (!TryGetTokenInformationBuffer(
                    token,
                    TOKEN_GROUPS_CLASS,
                    out buffer,
                    out returnedLength)) {
                return null;
            }
            try {
                long countOffset = Marshal.OffsetOf(
                    typeof(TOKEN_GROUPS_LAYOUT), "GroupCount").ToInt64();
                long firstOffset = Marshal.OffsetOf(
                    typeof(TOKEN_GROUPS_LAYOUT), "Groups").ToInt64();
                int entrySize = Marshal.SizeOf(typeof(SID_AND_ATTRIBUTES));
                if (entrySize <= 0 ||
                    countOffset < 0 ||
                    countOffset > Int32.MaxValue ||
                    firstOffset < 0 ||
                    firstOffset > Int32.MaxValue ||
                    !HasBufferRange(returnedLength, countOffset, 4)) {
                    return null;
                }
                int countValue = Marshal.ReadInt32(buffer, (int)countOffset);
                if (countValue < 0) {
                    return null;
                }
                uint count = (uint)countValue;
                if (count > 65536) {
                    return null;
                }
                long entriesLength = (long)entrySize * (long)count;
                if (count > 0 &&
                    !HasBufferRange(returnedLength, firstOffset, entriesLength)) {
                    return null;
                }
                TokenGroupRecord[] result = new TokenGroupRecord[(int)count];
                for (int index = 0; index < (int)count; index++) {
                    long entryOffset = firstOffset +
                        ((long)entrySize * (long)index);
                    if (!HasBufferRange(returnedLength, entryOffset, entrySize)) {
                        return null;
                    }
                    IntPtr entry = IntPtr.Add(buffer, (int)entryOffset);
                    IntPtr sid = Marshal.ReadIntPtr(entry);
                    uint attributes = (uint)Marshal.ReadInt32(
                        entry,
                        IntPtr.Size);
                    byte[] sidBytes = null;
                    if (!TryCopySidFromBuffer(
                            buffer,
                            returnedLength,
                            sid,
                            out sidBytes)) {
                        return null;
                    }
                    result[index] = new TokenGroupRecord();
                    result[index].Sid = sidBytes;
                    result[index].Attributes = attributes;
                }
                return result;
            }
            catch {
                return null;
            }
            finally {
                Marshal.FreeHGlobal(buffer);
            }
        }

        // Scalar and inline-value token classes are safe to copy. Pointer-bearing token
        // classes use their dedicated readers above so no embedded target survives a free.
        private static byte[] ReadTokenInformationBytes(IntPtr token, int informationClass) {
            IntPtr buffer = IntPtr.Zero;
            int returnedLength = 0;
            if (!TryGetTokenInformationBuffer(
                    token,
                    informationClass,
                    out buffer,
                    out returnedLength)) {
                return null;
            }
            try {
                byte[] bytes = new byte[returnedLength];
                Marshal.Copy(buffer, bytes, 0, returnedLength);
                return bytes;
            }
            catch {
                return null;
            }
            finally {
                Marshal.FreeHGlobal(buffer);
            }
        }

        public static int GetTokenElevationType(IntPtr token) {
            byte[] bytes = ReadTokenInformationBytes(token, TOKEN_ELEVATION_TYPE_CLASS);
            if (bytes == null || bytes.Length != 4) {
                return 0;
            }
            return BitConverter.ToInt32(bytes, 0);
        }

        public static IntPtr GetLinkedToken(IntPtr token) {
            // TOKEN_LINKED_TOKEN contains a HANDLE value, not a SID pointer into the
            // returned allocation. Copying the value is safe; the caller owns the returned
            // handle and closes it exactly once after all filtered-token reads complete.
            byte[] bytes = ReadTokenInformationBytes(token, TOKEN_LINKED_TOKEN_CLASS);
            if (bytes == null || bytes.Length != IntPtr.Size) {
                return IntPtr.Zero;
            }
            try {
                if (IntPtr.Size == 8) {
                    return new IntPtr(BitConverter.ToInt64(bytes, 0));
                }
                return new IntPtr(BitConverter.ToInt32(bytes, 0));
            }
            catch {
                return IntPtr.Zero;
            }
        }

        public static string[] ReadTokenPrivilegeNames(IntPtr token) {
            byte[] bytes = ReadTokenInformationBytes(token, TOKEN_PRIVILEGES_CLASS);
            if (bytes == null || bytes.Length < 4) {
                return null;
            }
            IntPtr buffer = Marshal.AllocHGlobal(bytes.Length);
            try {
                Marshal.Copy(bytes, 0, buffer, bytes.Length);
                uint count = (uint)Marshal.ReadInt32(buffer);
                if (count > 65536) {
                    return null;
                }
                int firstOffset = (int)Marshal.OffsetOf(
                    typeof(TOKEN_PRIVILEGES_LAYOUT), "Privileges");
                int entrySize = Marshal.SizeOf(typeof(LUID_AND_ATTRIBUTES));
                if (firstOffset < 0 ||
                    firstOffset + (long)entrySize * count > bytes.Length) {
                    return null;
                }
                string[] names = new string[(int)count];
                for (int index = 0; index < (int)count; index++) {
                    IntPtr entry = IntPtr.Add(buffer, firstOffset + (entrySize * index));
                    LUID_AND_ATTRIBUTES entryValue = (LUID_AND_ATTRIBUTES)Marshal.PtrToStructure(
                        entry, typeof(LUID_AND_ATTRIBUTES));
                    LUID luid = entryValue.Luid;
                    int required = 0;
                    if (LookupPrivilegeNameW(null, ref luid, null, ref required) == false &&
                        Marshal.GetLastWin32Error() != ERROR_INSUFFICIENT_BUFFER) {
                        return null;
                    }
                    if (required <= 0) {
                        return null;
                    }
                    StringBuilder builder = new StringBuilder(required + 1);
                    int capacity = required + 1;
                    if (!LookupPrivilegeNameW(null, ref luid, builder, ref capacity)) {
                        return null;
                    }
                    names[index] = builder.ToString();
                }
                return names;
            }
            finally {
                Marshal.FreeHGlobal(buffer);
            }
        }

        public static bool TokenHasPrivilege(IntPtr token, string name) {
            string[] names = ReadTokenPrivilegeNames(token);
            if (names == null) {
                return false;
            }
            for (int index = 0; index < names.Length; index++) {
                if (String.Equals(names[index], name,
                    StringComparison.OrdinalIgnoreCase)) {
                    return true;
                }
            }
            return false;
        }

        private static GENERIC_MAPPING BuildMapping(
            uint genericRead,
            uint genericWrite,
            uint genericExecute,
            uint genericAll) {
            GENERIC_MAPPING mapping = new GENERIC_MAPPING();
            mapping.GenericRead = genericRead;
            mapping.GenericWrite = genericWrite;
            mapping.GenericExecute = genericExecute;
            mapping.GenericAll = genericAll;
            return mapping;
        }

        public static uint MapMask(
            uint mask,
            uint genericRead,
            uint genericWrite,
            uint genericExecute,
            uint genericAll) {
            GENERIC_MAPPING mapping = BuildMapping(
                genericRead, genericWrite, genericExecute, genericAll);
            uint working = mask;
            MapGenericMask(ref working, ref mapping);
            return working;
        }

        public static AccessCheckOutcome CheckMaximumAllowed(
            byte[] descriptor,
            IntPtr primaryToken,
            uint genericRead,
            uint genericWrite,
            uint genericExecute,
            uint genericAll) {
            AccessCheckOutcome outcome = new AccessCheckOutcome();
            outcome.Evaluated = false;
            outcome.GrantedAccess = 0;
            outcome.AccessStatus = false;
            outcome.LastError = 0;
            IntPtr impersonation = IntPtr.Zero;
            IntPtr privilegeSet = IntPtr.Zero;
            try {
                if (!DuplicateTokenEx(
                    primaryToken,
                    TOKEN_QUERY,
                    IntPtr.Zero,
                    SECURITY_IDENTIFICATION,
                    TOKEN_IMPERSONATION,
                    out impersonation)) {
                    outcome.LastError = Marshal.GetLastWin32Error();
                    return outcome;
                }
                GENERIC_MAPPING mapping = BuildMapping(
                    genericRead, genericWrite, genericExecute, genericAll);
                int privilegeSetLength =
                    Marshal.SizeOf(typeof(PRIVILEGE_SET)) +
                    (PRIVILEGE_SET_SLACK_ENTRIES *
                        Marshal.SizeOf(typeof(LUID_AND_ATTRIBUTES)));
                privilegeSet = Marshal.AllocHGlobal(privilegeSetLength);
                uint granted = 0;
                bool status = false;
                if (!AccessCheck(
                    descriptor,
                    impersonation,
                    MAXIMUM_ALLOWED,
                    ref mapping,
                    privilegeSet,
                    ref privilegeSetLength,
                    out granted,
                    out status)) {
                    outcome.LastError = Marshal.GetLastWin32Error();
                    return outcome;
                }
                outcome.Evaluated = true;
                outcome.GrantedAccess = granted;
                outcome.AccessStatus = status;
                return outcome;
            }
            finally {
                if (privilegeSet != IntPtr.Zero) {
                    Marshal.FreeHGlobal(privilegeSet);
                }
                if (impersonation != IntPtr.Zero) {
                    CloseHandle(impersonation);
                }
            }
        }
    }
}
'@

function Initialize-R156Native {
    if (-not ([System.Management.Automation.PSTypeName]'EgR156.Native').Type) {
        Add-Type -TypeDefinition $script:R156NativeSource
    }
}

function New-R156WellKnownSid {
    param([Parameter(Mandatory)][System.Security.Principal.WellKnownSidType]$SidType)

    return New-Object System.Security.Principal.SecurityIdentifier($SidType, $null)
}

function Convert-R156SidBytesToSid {
    param([Parameter(Mandatory)][byte[]]$Bytes)

    return New-Object System.Security.Principal.SecurityIdentifier($Bytes, 0)
}

function Test-R156SidBytesEqual {
    param(
        [Parameter(Mandatory)][byte[]]$Left,
        [Parameter(Mandatory)][byte[]]$Right
    )

    if ($Left.Length -ne $Right.Length) {
        return $false
    }
    for ($index = 0; $index -lt $Left.Length; $index++) {
        if ($Left[$index] -ne $Right[$index]) {
            return $false
        }
    }
    return $true
}

function Test-R156SidInSet {
    param(
        [Parameter(Mandatory)][System.Security.Principal.SecurityIdentifier]$Candidate,
        [Parameter(Mandatory)][System.Security.Principal.SecurityIdentifier[]]$Set
    )

    foreach ($sid in @($Set)) {
        if ($Candidate.Equals($sid)) {
            return $true
        }
    }
    return $false
}

function Get-R156TokenContext {
    Initialize-R156Native
    $full = [IntPtr]::Zero
    $linked = [IntPtr]::Zero
    $success = $false
    try {
        $full = [EgR156.Native]::OpenCurrentProcessToken()
        if ($full -eq [IntPtr]::Zero) {
            Stop-R156Gate -SupportRef 'EG_R156_TOKEN_READ_FAILED'
        }
        $fullUserBytes = [EgR156.Native]::ReadTokenUserSid($full)
        $fullOwnerBytes = [EgR156.Native]::ReadTokenOwnerSid($full)
        $fullGroups = [EgR156.Native]::ReadTokenGroups($full)
        if ($null -eq $fullUserBytes -or $null -eq $fullOwnerBytes -or $null -eq $fullGroups) {
            Stop-R156Gate -SupportRef 'EG_R156_TOKEN_READ_FAILED'
        }
        $systemSid = New-R156WellKnownSid -SidType ([System.Security.Principal.WellKnownSidType]::LocalSystemSid)
        $systemBytes = New-Object byte[] ($systemSid.BinaryLength)
        $systemSid.GetBinaryForm($systemBytes, 0)
        if (Test-R156SidBytesEqual -Left $fullUserBytes -Right $systemBytes) {
            Stop-R156Gate -SupportRef 'EG_R156_SYSTEM_CONTEXT'
        }

        if ([Environment]::UserInteractive -ne $true) {
            Stop-R156Gate -SupportRef 'EG_R156_NON_INTERACTIVE_CONTEXT'
        }
        $elevationType = [EgR156.Native]::GetTokenElevationType($full)
        if ($elevationType -ne 2) {
            Stop-R156Gate -SupportRef 'EG_R156_NOT_ELEVATED'
        }

        $adminSid = New-R156WellKnownSid -SidType ([System.Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid)
        $adminBytes = New-Object byte[] ($adminSid.BinaryLength)
        $adminSid.GetBinaryForm($adminBytes, 0)
        $fullAdminMatches = 0
        foreach ($group in @($fullGroups)) {
            if (Test-R156SidBytesEqual -Left $group.Sid -Right $adminBytes) {
                $fullAdminMatches++
                if (($group.Attributes -band 0x00000004) -eq 0) {
                    Stop-R156Gate -SupportRef 'EG_R156_ADMIN_TOKEN_INVALID'
                }
                if (($group.Attributes -band 0x00000010) -ne 0) {
                    Stop-R156Gate -SupportRef 'EG_R156_ADMIN_TOKEN_INVALID'
                }
            }
        }
        if ($fullAdminMatches -ne 1) {
            Stop-R156Gate -SupportRef 'EG_R156_ADMIN_TOKEN_INVALID'
        }

        $linked = [EgR156.Native]::GetLinkedToken($full)
        if ($linked -eq [IntPtr]::Zero -or $linked -eq $full) {
            Stop-R156Gate -SupportRef 'EG_R156_FILTERED_TOKEN_UNAVAILABLE'
        }
        $linkedUserBytes = [EgR156.Native]::ReadTokenUserSid($linked)
        $linkedGroups = [EgR156.Native]::ReadTokenGroups($linked)
        if ($null -eq $linkedUserBytes -or $null -eq $linkedGroups) {
            Stop-R156Gate -SupportRef 'EG_R156_FILTERED_TOKEN_READ_FAILED'
        }
        if (-not (Test-R156SidBytesEqual -Left $fullUserBytes -Right $linkedUserBytes)) {
            Stop-R156Gate -SupportRef 'EG_R156_FILTERED_TOKEN_ACCOUNT_MISMATCH'
        }
        if ([EgR156.Native]::GetTokenElevationType($linked) -ne 3) {
            Stop-R156Gate -SupportRef 'EG_R156_FILTERED_TOKEN_NOT_LIMITED'
        }
        $linkedAdminMatches = 0
        foreach ($group in @($linkedGroups)) {
            if (Test-R156SidBytesEqual -Left $group.Sid -Right $adminBytes) {
                $linkedAdminMatches++
                if (($group.Attributes -band 0x00000010) -eq 0) {
                    Stop-R156Gate -SupportRef 'EG_R156_ADMIN_DENY_ONLY_INVALID'
                }
                if (($group.Attributes -band 0x00000004) -ne 0) {
                    Stop-R156Gate -SupportRef 'EG_R156_ADMIN_DENY_ONLY_INVALID'
                }
            }
        }
        if ($linkedAdminMatches -ne 1) {
            Stop-R156Gate -SupportRef 'EG_R156_ADMIN_DENY_ONLY_INVALID'
        }
        $filteredPrivileges = [EgR156.Native]::ReadTokenPrivilegeNames($linked)
        if ($null -eq $filteredPrivileges) {
            Stop-R156Gate -SupportRef 'EG_R156_FILTERED_TOKEN_PRIVILEGE_READ_FAILED'
        }
        foreach ($privilege in @($filteredPrivileges)) {
            if ([string]::Equals(
                    [string]$privilege,
                    'SeTakeOwnershipPrivilege',
                    [StringComparison]::OrdinalIgnoreCase)) {
                Stop-R156Gate -SupportRef 'EG_R156_FILTERED_TOKEN_BYPASS_PRIVILEGE'
            }
            if ([string]::Equals(
                    [string]$privilege,
                    'SeRestorePrivilege',
                    [StringComparison]::OrdinalIgnoreCase)) {
                Stop-R156Gate -SupportRef 'EG_R156_FILTERED_TOKEN_BYPASS_PRIVILEGE'
            }
        }
        $success = $true
        return [pscustomobject]@{
            FullToken = $full
            FilteredToken = $linked
            FullUser = Convert-R156SidBytesToSid -Bytes $fullUserBytes
            FullOwner = Convert-R156SidBytesToSid -Bytes $fullOwnerBytes
            Administrators = $adminSid
            LocalSystem = $systemSid
        }
    }
    finally {
        if (-not $success) {
            if ($linked -ne [IntPtr]::Zero) {
                [EgR156.Native]::CloseToken($linked)
            }
            if ($full -ne [IntPtr]::Zero) {
                [EgR156.Native]::CloseToken($full)
            }
        }
    }
}

function Get-R156RawSecurityDescriptor {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    try {
        $sections = [System.Security.AccessControl.AccessControlSections]::Owner -bor
            [System.Security.AccessControl.AccessControlSections]::Group -bor
            [System.Security.AccessControl.AccessControlSections]::Access
        if ((Get-Item -LiteralPath $Path -Force).PSIsContainer) {
            $descriptor = New-Object System.Security.AccessControl.DirectorySecurity($Path, $sections)
        }
        else {
            $descriptor = New-Object System.Security.AccessControl.FileSecurity($Path, $sections)
        }
        $binary = $descriptor.GetSecurityDescriptorBinaryForm()
        $raw = New-Object System.Security.AccessControl.RawSecurityDescriptor($binary, 0)
        return [pscustomobject]@{
            Binary = [byte[]]$binary
            Raw = $raw
        }
    }
    catch {
        return $null
    }
}

function Get-R156AceRecord {
    param([Parameter(Mandatory)]$Ace)

    try {
        $aceType = [int]$Ace.AceType
        $allowedType = (
            $aceType -eq [int][System.Security.AccessControl.AceType]::AccessAllowed -or
            $aceType -eq [int][System.Security.AccessControl.AceType]::AccessAllowedObject
        )
        $deniedType = (
            $aceType -eq [int][System.Security.AccessControl.AceType]::AccessDenied -or
            $aceType -eq [int][System.Security.AccessControl.AceType]::AccessDeniedObject
        )
        if (-not $allowedType -and -not $deniedType) {
            return $null
        }
        $sid = [string]$Ace.SecurityIdentifier.Value
        $isCallback = [bool]$Ace.IsCallback
        if ($isCallback) {
            return $null
        }
        $objectFlags = -1
        $objectType = ''
        $inheritedObjectType = ''
        if ($Ace -is [System.Security.AccessControl.ObjectAce]) {
            $objectFlags = [int]$Ace.ObjectAceFlags
            if (($objectFlags -band 0x00000001) -ne 0) {
                $objectType = [string]$Ace.ObjectAceType
            }
            if (($objectFlags -band 0x00000002) -ne 0) {
                $inheritedObjectType = [string]$Ace.InheritedObjectAceType
            }
        }
        return [pscustomobject]@{
            Type = $aceType
            Allow = $allowedType
            Flags = [int]$Ace.AceFlags
            Mask = [int64]$Ace.AccessMask
            Sid = $sid
            ObjectFlags = $objectFlags
            ObjectType = $objectType
            InheritedObjectType = $inheritedObjectType
        }
    }
    catch {
        return $null
    }
}

function Get-R156DaclRecords {
    param([Parameter(Mandatory)]$Descriptor)

    $raw = $Descriptor.Raw
    if (($raw.ControlFlags -band
            [System.Security.AccessControl.ControlFlags]::DiscretionaryAclPresent) -eq 0) {
        return $null
    }
    if ($null -eq $raw.DiscretionaryAcl) {
        return $null
    }
    $records = @()
    for ($index = 0; $index -lt $raw.DiscretionaryAcl.Count; $index++) {
        $record = Get-R156AceRecord -Ace $raw.DiscretionaryAcl[$index]
        if ($null -eq $record) {
            return $null
        }
        $records = $records + $record
    }
    return $records
}

function Get-R156AceSortKey {
    param([Parameter(Mandatory)]$Record)

    return [string]::Join(
        '|',
        @(
            [string]$Record.Type,
            [string]$Record.Allow,
            [string]$Record.Flags,
            [string]$Record.Mask,
            [string]$Record.Sid,
            [string]$Record.ObjectFlags,
            [string]$Record.ObjectType,
            [string]$Record.InheritedObjectType
        )
    )
}

function Sort-R156AceRecords {
    param([AllowEmptyCollection()]$Records)

    $items = @($Records)
    for ($index = 1; $index -lt $items.Count; $index++) {
        $current = $items[$index]
        $currentKey = Get-R156AceSortKey -Record $current
        $scan = $index - 1
        while ($scan -ge 0) {
            $scanKey = Get-R156AceSortKey -Record $items[$scan]
            if ([string]::CompareOrdinal($scanKey, $currentKey) -le 0) {
                break
            }
            $items[$scan + 1] = $items[$scan]
            $scan--
        }
        $items[$scan + 1] = $current
    }
    return $items
}

function Test-R156AceRecordMultiset {
    param(
        [AllowEmptyCollection()]$Actual,
        [AllowEmptyCollection()]$Expected
    )

    $left = @(Sort-R156AceRecords -Records @($Actual))
    $right = @(Sort-R156AceRecords -Records @($Expected))
    if ($left.Count -ne $right.Count) {
        return $false
    }
    for ($index = 0; $index -lt $left.Count; $index++) {
        if ((Get-R156AceSortKey -Record $left[$index]) -cne
            (Get-R156AceSortKey -Record $right[$index])) {
            return $false
        }
    }
    return $true
}

function Get-R156MappedMask {
    param([Parameter(Mandatory)][uint32]$Mask)

    Initialize-R156Native
    return [uint32][EgR156.Native]::MapMask(
        $Mask,
        $script:R156GenericRead,
        $script:R156GenericWrite,
        $script:R156GenericExecute,
        $script:R156AllAccess
    )
}

function Get-R156AccessOutcome {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path,
        [Parameter(Mandatory)][IntPtr]$Token
    )

    $descriptor = Get-R156RawSecurityDescriptor -Path $Path
    if ($null -eq $descriptor) {
        return $null
    }
    try {
        Initialize-R156Native
        return [EgR156.Native]::CheckMaximumAllowed(
            $descriptor.Binary,
            $Token,
            $script:R156GenericRead,
            $script:R156GenericWrite,
            $script:R156GenericExecute,
            $script:R156AllAccess
        )
    }
    catch {
        return $null
    }
}

function Test-R156FullInstallerWrite {
    param(
        [Parameter(Mandatory)][string[]]$ObjectPath,
        [Parameter(Mandatory)][IntPtr]$FullToken
    )

    $mappedAll = Get-R156MappedMask -Mask $script:R156AllAccess
    foreach ($path in @($ObjectPath)) {
        $outcome = Get-R156AccessOutcome -Path $path -Token $FullToken
        if ($null -eq $outcome -or -not $outcome.Evaluated) {
            return $false
        }
        if (([uint32]$outcome.GrantedAccess -band $mappedAll) -ne $mappedAll) {
            return $false
        }
    }
    return $true
}

function Test-R156FilteredNoWrite {
    param(
        [Parameter(Mandatory)][string[]]$ObjectPath,
        [Parameter(Mandatory)][IntPtr]$FilteredToken
    )

    $mappedWrite = Get-R156MappedMask -Mask $script:R156WriteMask
    foreach ($path in @($ObjectPath)) {
        $outcome = Get-R156AccessOutcome -Path $path -Token $FilteredToken
        if ($null -eq $outcome -or -not $outcome.Evaluated) {
            return $false
        }
        if (([uint32]$outcome.GrantedAccess -band $mappedWrite) -ne 0) {
            return $false
        }
    }
    return $true
}

function Test-R156FilteredRead {
    param(
        [Parameter(Mandatory)][string[]]$ObjectPath,
        [Parameter(Mandatory)][IntPtr]$FilteredToken
    )

    $mappedRead = Get-R156MappedMask -Mask $script:R156GenericRead
    foreach ($path in @($ObjectPath)) {
        $outcome = Get-R156AccessOutcome -Path $path -Token $FilteredToken
        if ($null -eq $outcome -or -not $outcome.Evaluated) {
            return $false
        }
        if (([uint32]$outcome.GrantedAccess -band $mappedRead) -ne $mappedRead) {
            return $false
        }
    }
    return $true
}

function Test-R156DaclWriterModel {
    param(
        [Parameter(Mandatory)][string[]]$ObjectPath,
        [Parameter(Mandatory)][System.Security.Principal.SecurityIdentifier[]]$AuthorisedSid
    )

    $mappedWrite = Get-R156MappedMask -Mask $script:R156WriteMask
    $creatorOwner = New-R156WellKnownSid -SidType ([System.Security.Principal.WellKnownSidType]::CreatorOwnerSid)
    $creatorGroup = New-R156WellKnownSid -SidType ([System.Security.Principal.WellKnownSidType]::CreatorGroupSid)
    foreach ($path in @($ObjectPath)) {
        $descriptor = Get-R156RawSecurityDescriptor -Path $path
        if ($null -eq $descriptor -or $null -eq $descriptor.Raw.Owner) {
            return $false
        }
        if (-not (Test-R156SidInSet -Candidate $descriptor.Raw.Owner -Set $AuthorisedSid)) {
            return $false
        }
        $records = Get-R156DaclRecords -Descriptor $descriptor
        if ($null -eq $records) {
            return $false
        }
        foreach ($record in @($records)) {
            $mapped = Get-R156MappedMask -Mask ([uint32]$record.Mask)
            $sid = New-Object System.Security.Principal.SecurityIdentifier($record.Sid)
            $hasWrite = (($mapped -band $mappedWrite) -ne 0)
            if ($hasWrite -and $sid.Equals($creatorGroup)) {
                return $false
            }
            if (([int]$record.Flags -band 0x00000008) -ne 0) {
                continue
            }
            if (-not $record.Allow -or -not $hasWrite) {
                continue
            }
            if ($sid.Equals($creatorOwner) -or $sid.Equals($creatorGroup)) {
                return $false
            }
            if (-not (Test-R156SidInSet -Candidate $sid -Set $AuthorisedSid)) {
                return $false
            }
        }
    }
    return $true
}

function Get-R156StagingModel {
    param(
        [Parameter(Mandatory)]$RootDescriptor,
        [Parameter(Mandatory)][System.Security.Principal.SecurityIdentifier]$DefaultOwner
    )

    $rootRecords = Get-R156DaclRecords -Descriptor $RootDescriptor
    if ($null -eq $rootRecords) {
        return $null
    }
    $objectInherit = 0x00000001
    $containerInherit = 0x00000002
    $noPropagate = 0x00000004
    $inheritOnly = 0x00000008
    $inherited = 0x00000010
    $inheritanceMask = $objectInherit -bor $containerInherit -bor $noPropagate -bor $inheritOnly
    $creatorGroup = New-R156WellKnownSid -SidType ([System.Security.Principal.WellKnownSidType]::CreatorGroupSid)
    $creatorOwner = New-R156WellKnownSid -SidType ([System.Security.Principal.WellKnownSidType]::CreatorOwnerSid)
    $expected = @()
    foreach ($record in @($rootRecords)) {
        $flags = [int]$record.Flags
        if (($flags -band ($objectInherit -bor $containerInherit)) -ne 0) {
            $sid = New-Object System.Security.Principal.SecurityIdentifier($record.Sid)
            if ($sid.Equals($creatorGroup)) {
                return $null
            }
        }
        if (($flags -band $objectInherit) -eq 0) {
            continue
        }
        if ($record.ObjectFlags -ne -1) {
            return $null
        }
        $childSid = $record.Sid
        if ($sid.Equals($creatorOwner)) {
            $childSid = $DefaultOwner.Value
        }
        $childFlags = ([int]$flags -band (-bnot [int]$inheritanceMask)) -bor $inherited
        $expected = $expected + [pscustomobject]@{
            Type = [int]$record.Type
            Allow = [bool]$record.Allow
            Flags = [int]$childFlags
            Mask = [int64]$record.Mask
            Sid = [string]$childSid
            ObjectFlags = -1
            ObjectType = ''
            InheritedObjectType = ''
        }
    }
    return $expected
}

function Test-R156StagingInheritanceModel {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRoot,
        [Parameter(Mandatory)][string[]]$ClassAPath,
        [Parameter(Mandatory)][System.Security.Principal.SecurityIdentifier]$DefaultOwner
    )

    $rootDescriptor = Get-R156RawSecurityDescriptor -Path $LauncherRoot
    if ($null -eq $rootDescriptor) {
        return $false
    }
    $expected = Get-R156StagingModel -RootDescriptor $rootDescriptor -DefaultOwner $DefaultOwner
    if ($null -eq $expected) {
        return $false
    }
    foreach ($path in @($ClassAPath)) {
        $descriptor = Get-R156RawSecurityDescriptor -Path $path
        if ($null -eq $descriptor) {
            return $false
        }
        $actual = Get-R156DaclRecords -Descriptor $descriptor
        if ($null -eq $actual) {
            return $false
        }
        if (-not (Test-R156AceRecordMultiset -Actual $actual -Expected $expected)) {
            return $false
        }
    }
    return $true
}

function Get-R156AuthorisedWriterSet {
    param([Parameter(Mandatory)]$TokenContext)

    $set = @(
        $TokenContext.FullUser,
        $TokenContext.Administrators,
        $TokenContext.LocalSystem
    )
    if ($set.Count -ne 3) {
        return $null
    }
    for ($left = 0; $left -lt $set.Count; $left++) {
        for ($right = $left + 1; $right -lt $set.Count; $right++) {
            if ($set[$left].Equals($set[$right])) {
                return $null
            }
        }
    }
    return [System.Security.Principal.SecurityIdentifier[]]$set
}

function Test-R156AncestorReplacement {
    param(
        [Parameter(Mandatory)][string[]]$TargetPath,
        [Parameter(Mandatory)][IntPtr]$FilteredToken
    )

    $mappedAncestor = Get-R156MappedMask -Mask $script:R156AncestorMask
    foreach ($target in @($TargetPath)) {
        $ancestors = Get-R156AncestorDirectories -Path $target
        if (@($ancestors).Count -eq 0) {
            return $false
        }
        foreach ($ancestor in @($ancestors)) {
            if (-not (Test-R156NormalDirectory -Path $ancestor)) {
                return $false
            }
            $outcome = Get-R156AccessOutcome -Path $ancestor -Token $FilteredToken
            if ($null -eq $outcome -or -not $outcome.Evaluated) {
                return $false
            }
            if (([uint32]$outcome.GrantedAccess -band $mappedAncestor) -ne 0) {
                return $false
            }
        }
    }
    return $true
}

function Test-R156ProtectedDacl {
    param([Parameter(Mandatory)][string[]]$ObjectPath)

    foreach ($path in @($ObjectPath)) {
        $descriptor = Get-R156RawSecurityDescriptor -Path $path
        if ($null -eq $descriptor) {
            return $false
        }
        if (($descriptor.Raw.ControlFlags -band
                [System.Security.AccessControl.ControlFlags]::DiscretionaryAclProtected) -eq 0) {
            return $false
        }
    }
    return $true
}

function Test-R156LauncherSecurity {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRoot,
        [Parameter(Mandatory)][string[]]$ClassAPath,
        [Parameter(Mandatory)]$TokenContext
    )

    $objects = @($LauncherRoot) + @($ClassAPath)
    $writers = Get-R156AuthorisedWriterSet -TokenContext $TokenContext
    if ($null -eq $writers) {
        return $false
    }
    if (-not (Test-R156FullInstallerWrite -ObjectPath $objects -FullToken $TokenContext.FullToken)) {
        return $false
    }
    if (-not (Test-R156FilteredNoWrite -ObjectPath $objects -FilteredToken $TokenContext.FilteredToken)) {
        return $false
    }
    if (-not (Test-R156DaclWriterModel -ObjectPath $objects -AuthorisedSid $writers)) {
        return $false
    }
    if (-not (Test-R156StagingInheritanceModel -LauncherRoot $LauncherRoot -ClassAPath $ClassAPath -DefaultOwner $TokenContext.FullOwner)) {
        return $false
    }
    if (-not (Test-R156AncestorReplacement -TargetPath $objects -FilteredToken $TokenContext.FilteredToken)) {
        return $false
    }
    return $true
}

function Test-R156LocatorSecurity {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ProgramData,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LocatorPath,
        [Parameter(Mandatory)]$TokenContext
    )

    $xb = Join-Path $ProgramData 'X-Boundaries'
    $energy = Join-Path $xb 'EnergyGrid'
    $dedicated = @($xb, $energy, $LocatorPath)
    foreach ($path in $dedicated) {
        if (-not (Test-R156NormalDirectory -Path $path) -and
            -not (Test-R156NormalFile -Path $path)) {
            return $false
        }
    }
    if (-not (Test-R156ProtectedDacl -ObjectPath $dedicated)) {
        return $false
    }
    $writers = Get-R156AuthorisedWriterSet -TokenContext $TokenContext
    if ($null -eq $writers) {
        return $false
    }
    if (-not (Test-R156FullInstallerWrite -ObjectPath $dedicated -FullToken $TokenContext.FullToken)) {
        return $false
    }
    if (-not (Test-R156FilteredRead -ObjectPath $dedicated -FilteredToken $TokenContext.FilteredToken)) {
        return $false
    }
    if (-not (Test-R156FilteredNoWrite -ObjectPath $dedicated -FilteredToken $TokenContext.FilteredToken)) {
        return $false
    }
    if (-not (Test-R156DaclWriterModel -ObjectPath $dedicated -AuthorisedSid $writers)) {
        return $false
    }
    if (-not (Test-R156AncestorReplacement -TargetPath $dedicated -FilteredToken $TokenContext.FilteredToken)) {
        return $false
    }
    $volumeRoot = [System.IO.Path]::GetPathRoot((Get-R156FullPath -Path $ProgramData))
    if ([string]::IsNullOrEmpty($volumeRoot)) {
        return $false
    }
    if (-not (Test-R156SamePath -Left $volumeRoot -Right ([System.IO.Path]::GetPathRoot((Get-R156FullPath -Path $LocatorPath))))) {
        return $false
    }
    return $true
}

function Test-R156ClassAClassification {
    param([Parameter(Mandatory)]$Classification)

    return (
        $Classification.Pass -and
        @($Classification.ClassA).Count -eq 3 -and
        @($Classification.ClassB).Count -eq 0 -and
        @($Classification.ClassC).Count -eq 0 -and
        @($Classification.MissingMembers).Count -eq 0
    )
}

function Get-R156ClassAPaths {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRoot)

    $paths = @()
    foreach ($name in @($script:R156PackageNames)) {
        $path = Join-Path $LauncherRoot $name
        if (-not (Test-R156NormalFile -Path $path)) {
            return $null
        }
        $paths = $paths + $path
    }
    return [string[]]$paths
}

function Read-R156ManifestState {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRoot,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedAdmission,
        [Parameter(Mandatory)][System.Collections.IDictionary]$CanonicalSources
    )

    $classification = Get-EgLauncherRootClassification -LauncherRootPath $LauncherRoot
    $classPass = Test-R156ClassAClassification -Classification $classification
    $classAPaths = Get-R156ClassAPaths -LauncherRoot $LauncherRoot
    if (-not $classPass -or $null -eq $classAPaths) {
        return $null
    }
    $manifestPath = Join-Path $LauncherRoot $script:R156ManifestFileName
    $manifest = Read-R156StrictJsonObject -Path $manifestPath
    if ($null -eq $manifest) {
        return $null
    }
    $shape = Test-EgInstallationManifestShape -ManifestObject $manifest
    if ($null -eq $shape -or -not $shape.Pass) {
        return $null
    }
    if ([string]$manifest.schema_version -cne $script:R156ManifestSchema) {
        return $null
    }
    if ([string]$manifest.admission_commit -cne $ExpectedAdmission) {
        return $null
    }
    $packageManifest = Compare-EgInstalledPackageToManifest -LauncherRootPath $LauncherRoot
    if ($null -eq $packageManifest -or -not $packageManifest.Pass) {
        return $null
    }
    foreach ($name in @($script:R156ManifestNames)) {
        $installedPath = Join-Path $LauncherRoot $name
        if (-not (Test-R156NormalFile -Path $installedPath)) {
            return $null
        }
        $installedBytes = [System.IO.File]::ReadAllBytes($installedPath)
        if (-not (Test-R156PowerShellParse -Path $installedPath -Bytes $installedBytes)) {
            return $null
        }
        if (-not $CanonicalSources.Contains($name)) {
            return $null
        }
        $source = $CanonicalSources[$name]
        if ((Get-R156Sha256ForBytes -Bytes $installedBytes) -cne $source.Sha256) {
            return $null
        }
        if ([int64]$installedBytes.Length -ne [int64]$source.ByteLength) {
            return $null
        }
    }
    return [pscustomobject]@{
        Classification = $classification
        ClassAPaths = [string[]]$classAPaths
        ManifestPath = $manifestPath
        Manifest = $manifest
        PackageToManifest = $true
        Admission = [string]$manifest.admission_commit
        CanonicalEquivalence = $true
    }
}

function Get-R156DirectChildren {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    try {
        return @(Get-ChildItem -LiteralPath $Path -Force)
    }
    catch {
        return $null
    }
}

function Test-R156NameIsFixedTopologyDirectory {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string[]]$FixedName
    )

    foreach ($fixed in @($FixedName)) {
        if ([string]::Equals($Name, $fixed, [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Resolve-R156Topology {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RuntimeRoot,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRoot
    )

    if (-not (Test-R156NormalDirectory -Path $RuntimeRoot)) {
        return $null
    }
    if (Test-R156PathWithin -Candidate $RuntimeRoot -Container $CheckoutRoot) {
        return $null
    }
    $children = Get-R156DirectChildren -Path $RuntimeRoot
    if ($null -eq $children) {
        return $null
    }
    $directPackageMembers = @()
    $candidateRoots = @()
    foreach ($entry in @($children)) {
        if (-not $entry.PSIsContainer) {
            foreach ($name in @($script:R156PackageNames)) {
                if ([string]::Equals($entry.Name, $name, [StringComparison]::Ordinal)) {
                    $directPackageMembers = $directPackageMembers + $entry.Name
                }
            }
            continue
        }
        if (-not (Test-R156NormalDirectory -Path $entry.FullName)) {
            return $null
        }
        $classification = Get-EgLauncherRootClassification -LauncherRootPath $entry.FullName
        if (Test-R156ClassAClassification -Classification $classification) {
            $candidateRoots = $candidateRoots + [pscustomobject]@{
                Path = Get-R156FullPath -Path $entry.FullName
                Classification = $classification
            }
        }
    }
    if (@($directPackageMembers).Count -ne 0 -or @($candidateRoots).Count -ne 1) {
        return $null
    }
    $candidate = $candidateRoots[0]
    $candidatePath = [string]$candidate.Path
    $candidateName = [System.IO.Path]::GetFileName($candidatePath.TrimEnd('\'))
    if (Test-R156NameIsFixedTopologyDirectory -Name $candidateName -FixedName @('config', 'credentials', 'venv')) {
        return $null
    }

    $configDirectory = Join-Path $RuntimeRoot 'config'
    $credentialDirectory = Join-Path $RuntimeRoot 'credentials'
    $venvDirectory = Join-Path $RuntimeRoot 'venv'
    if (-not (Test-R156NormalDirectory -Path $configDirectory) -or
        -not (Test-R156NormalDirectory -Path $credentialDirectory) -or
        -not (Test-R156NormalDirectory -Path $venvDirectory)) {
        return $null
    }
    $configChildren = Get-R156DirectChildren -Path $configDirectory
    $credentialChildren = Get-R156DirectChildren -Path $credentialDirectory
    if ($null -eq $configChildren -or $null -eq $credentialChildren) {
        return $null
    }
    $configJson = @($configChildren | Where-Object {
        (-not $_.PSIsContainer) -and $_.Name -match '\.json$'
    })
    if ($configJson.Count -ne 1 -or -not (Test-R156NormalFile -Path $configJson[0].FullName)) {
        return $null
    }
    $credentialLeaves = @($credentialChildren | Where-Object { -not $_.PSIsContainer })
    if ($credentialLeaves.Count -ne 1 -or
        -not (Test-R156NormalFile -Path $credentialLeaves[0].FullName)) {
        return $null
    }
    $pythonExe = Join-Path $venvDirectory 'Scripts\python.exe'
    if (-not (Test-R156NormalFile -Path $pythonExe)) {
        return $null
    }

    $excluded = @(
        (Get-R156FullPath -Path $candidatePath),
        (Get-R156FullPath -Path $configDirectory),
        (Get-R156FullPath -Path $credentialDirectory),
        (Get-R156FullPath -Path $venvDirectory)
    )
    $cacheCandidates = @()
    foreach ($entry in @($children)) {
        $isExcluded = $false
        foreach ($path in $excluded) {
            if (Test-R156SamePath -Left $entry.FullName -Right $path) {
                $isExcluded = $true
            }
        }
        if ($isExcluded) {
            continue
        }
        if (-not $entry.PSIsContainer -or
            -not (Test-R156NormalDirectory -Path $entry.FullName)) {
            return $null
        }
        $ready = Test-EgBrowserCacheReady -BrowserCachePath $entry.FullName
        if ($null -ne $ready -and $ready.Pass) {
            $cacheCandidates = $cacheCandidates + $entry.FullName
        }
    }
    if (@($cacheCandidates).Count -ne 1) {
        return $null
    }

    $configPath = Get-R156FullPath -Path $configJson[0].FullName
    $credentialPath = Get-R156FullPath -Path $credentialLeaves[0].FullName
    $pythonPath = Get-R156FullPath -Path $pythonExe
    $cachePath = Get-R156FullPath -Path $cacheCandidates[0]
    $configResult = Test-EgLauncherConfigContract -ConfigPath $configPath -CheckoutRootPath $CheckoutRoot
    if ($null -eq $configResult -or -not $configResult.Pass) {
        return $null
    }
    $metadata = [ordered]@{}
    foreach ($name in @(
            'runtime_root',
            'config_directory',
            'config',
            'credential_directory',
            'credential',
            'venv_directory',
            'python',
            'browser_cache'
        )) {
        $path = switch ($name) {
            'runtime_root' { $RuntimeRoot }
            'config_directory' { $configDirectory }
            'config' { $configPath }
            'credential_directory' { $credentialDirectory }
            'credential' { $credentialPath }
            'venv_directory' { $venvDirectory }
            'python' { $pythonPath }
            'browser_cache' { $cachePath }
        }
        $metadata[$name] = Get-R156Metadata -Path $path
    }
    return [pscustomobject]@{
        RuntimeRoot = Get-R156FullPath -Path $RuntimeRoot
        Candidate = $candidatePath
        Classification = $candidate.Classification
        ConfigDirectory = Get-R156FullPath -Path $configDirectory
        ConfigPath = $configPath
        CredentialDirectory = Get-R156FullPath -Path $credentialDirectory
        CredentialPath = $credentialPath
        VenvDirectory = Get-R156FullPath -Path $venvDirectory
        PythonExe = $pythonPath
        BrowserCachePath = $cachePath
        Metadata = $metadata
    }
}

function Test-R156TopologyContinuity {
    param(
        [Parameter(Mandatory)]$Before,
        [Parameter(Mandatory)]$After
    )

    $pathPairs = @(
        @($Before.RuntimeRoot, $After.RuntimeRoot),
        @($Before.Candidate, $After.Candidate),
        @($Before.ConfigDirectory, $After.ConfigDirectory),
        @($Before.ConfigPath, $After.ConfigPath),
        @($Before.CredentialDirectory, $After.CredentialDirectory),
        @($Before.CredentialPath, $After.CredentialPath),
        @($Before.VenvDirectory, $After.VenvDirectory),
        @($Before.PythonExe, $After.PythonExe),
        @($Before.BrowserCachePath, $After.BrowserCachePath)
    )
    foreach ($pair in @($pathPairs)) {
        if (-not (Test-R156SamePath -Left $pair[0] -Right $pair[1])) {
            return $false
        }
    }
    foreach ($name in @(
            'runtime_root',
            'config_directory',
            'config',
            'credential_directory',
            'credential',
            'venv_directory',
            'python',
            'browser_cache'
        )) {
        if (-not (Test-R156MetadataEqual -Before $Before.Metadata[$name] -After $After.Metadata[$name])) {
            return $false
        }
    }
    return $true
}

$script:R156ChildScript = @'
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-R156ChildLine {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Line)

    [Console]::Out.WriteLine($Line)
    [Console]::Out.Flush()
}

function Get-R156ChildProperties {
    param([Parameter(Mandatory)]$Object)

    return @($Object.PSObject.Properties | ForEach-Object { [string]$_.Name })
}

function Test-R156ChildExactSet {
    param(
        [AllowEmptyCollection()][string[]]$Actual,
        [AllowEmptyCollection()][string[]]$Expected
    )

    $left = @($Actual | Sort-Object)
    $right = @($Expected | Sort-Object)
    if ($left.Count -ne $right.Count) { return $false }
    for ($index = 0; $index -lt $left.Count; $index++) {
        if ([string]::CompareOrdinal([string]$left[$index], [string]$right[$index]) -ne 0) {
            return $false
        }
    }
    return $true
}

function Invoke-R156ContainedInstaller {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$InstallerPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRoot,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRoot,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$AdmissionCommit,
        [bool]$ValidateOnly
    )

    $inner = [PowerShell]::Create()
    try {
        [void]$inner.Runspace.SessionStateProxy.SetVariable('r156_installer_path', $InstallerPath)
        [void]$inner.Runspace.SessionStateProxy.SetVariable('r156_checkout_root', $CheckoutRoot)
        [void]$inner.Runspace.SessionStateProxy.SetVariable('r156_launcher_root', $LauncherRoot)
        [void]$inner.Runspace.SessionStateProxy.SetVariable('r156_admission_commit', $AdmissionCommit)
        [void]$inner.Runspace.SessionStateProxy.SetVariable('r156_validate_only', $ValidateOnly)
        [void]$inner.AddScript({
            Set-StrictMode -Version Latest
            $ErrorActionPreference = 'Stop'
            $path = Get-Variable -Name r156_installer_path -ValueOnly
            $checkout = Get-Variable -Name r156_checkout_root -ValueOnly
            $launcher = Get-Variable -Name r156_launcher_root -ValueOnly
            $admission = Get-Variable -Name r156_admission_commit -ValueOnly
            $validate = Get-Variable -Name r156_validate_only -ValueOnly
            if ($validate) {
                & $path -CheckoutRoot $checkout -LauncherRoot $launcher -AdmissionCommit $admission -ValidateOnly
            }
            else {
                & $path -CheckoutRoot $checkout -LauncherRoot $launcher -AdmissionCommit $admission
            }
        })
        $items = @($inner.Invoke())
        if ($inner.InvocationStateInfo.State -ne [System.Management.Automation.PSInvocationState]::Completed -or
            $inner.HadErrors -or
            @($inner.Streams.Error).Count -ne 0) {
            return [pscustomobject]@{
                Completed = $false
                Lines = [string[]]@()
            }
        }
        $lines = @()
        foreach ($item in @($items)) {
            if ($item -is [string]) {
                $lines = $lines + [string]$item
            }
            else {
                $lines = $lines + ([string]$item)
            }
        }
        return [pscustomobject]@{
            Completed = $true
            Lines = [string[]]$lines
        }
    }
    catch {
        return [pscustomobject]@{
            Completed = $false
            Lines = [string[]]@()
        }
    }
    finally {
        $inner.Dispose()
    }
}

function Get-R156ChildJsonObject {
    param([Parameter(Mandatory)][string[]]$Lines)

    if (@($Lines).Count -ne 1) {
        return $null
    }
    try {
        $object = @($Lines[0] | ConvertFrom-Json)
        if ($object.Count -ne 1) {
            return $null
        }
        if ($object[0] -isnot [System.Management.Automation.PSCustomObject]) {
            return $null
        }
        return $object[0]
    }
    catch {
        return $null
    }
}

function Test-R156ChildValidationResult {
    param([Parameter(Mandatory)]$Object)

    $names = Get-R156ChildProperties -Object $Object
    $exactTwo = Test-R156ChildExactSet -Actual $names -Expected @('checks', 'status')
    $exactThree = Test-R156ChildExactSet -Actual $names -Expected @(
        'checks', 'status', 'support_ref'
    )
    if (-not $exactTwo -and -not $exactThree) {
        return $null
    }
    if ($exactThree -and -not $exactTwo -and
        -not [string]::IsNullOrEmpty([string]$Object.support_ref)) {
        return $null
    }
    if ([string]$Object.status -cne 'PASS') {
        return $null
    }
    if ($Object.checks -isnot [System.Management.Automation.PSCustomObject]) {
        return $null
    }
    if (-not (Test-R156ChildExactSet -Actual (Get-R156ChildProperties -Object $Object.checks) -Expected @('checkout_root_absolute','launcher_root_absolute','checkout_root_exists','launcher_root_exists','launcher_root_outside_checkout','admission_commit_matches_checkout_head','source_members_present_and_parse_clean','installation_manifest_shape','installed_package_already_current'))) {
        return $null
    }
    foreach ($name in @(
            'checkout_root_absolute',
            'launcher_root_absolute',
            'checkout_root_exists',
            'launcher_root_exists',
            'launcher_root_outside_checkout',
            'admission_commit_matches_checkout_head',
            'source_members_present_and_parse_clean',
            'installation_manifest_shape'
        )) {
        if ([string]$Object.checks.$name -cne 'PASS') {
            return $null
        }
    }
    if ([string]$Object.checks.installed_package_already_current -notin @('PASS', 'FAIL')) {
        return $null
    }
    return [pscustomobject]@{
        Valid = $true
        ValidationStatus = 'PASS'
        ValidationCurrent = [string]$Object.checks.installed_package_already_current
    }
}

function Test-R156ChildRealResult {
    param([Parameter(Mandatory)]$Object)

    if (-not (Test-R156ChildExactSet -Actual (Get-R156ChildProperties -Object $Object) -Expected @('status','support_ref','backups_remaining','phase','exception_type','hresult'))) {
        return $null
    }
    $status = [string]$Object.status
    if ($status -notin @(
            'INSTALLED',
            'ALREADY_CURRENT',
            'FAILED_PREFLIGHT',
            'FAILED_ROLLED_BACK',
            'FAILED_ROLLBACK_INCOMPLETE'
        )) {
        return $null
    }
    $backups = -1
    try {
        $backups = [int64]$Object.backups_remaining
    }
    catch {
        return $null
    }
    if ($backups -lt 0) {
        return $null
    }
    foreach ($name in @('support_ref', 'phase', 'exception_type', 'hresult')) {
        if ($Object.$name -is [System.Array]) {
            return $null
        }
        [void][string]$Object.$name
    }
    $successShape = (
        $status -ceq 'INSTALLED' -and
        $backups -eq 0 -and
        [string]::IsNullOrEmpty([string]$Object.support_ref) -and
        [string]::IsNullOrEmpty([string]$Object.phase) -and
        [string]::IsNullOrEmpty([string]$Object.exception_type) -and
        [string]::IsNullOrEmpty([string]$Object.hresult)
    )
    return [pscustomobject]@{
        Valid = $true
        RealStatus = $status
        RealBackupsRemaining = $backups
        RealSuccessShape = $successShape
    }
}

function Get-R156ChildPacket {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Mode,
        [Parameter(Mandatory)][string[]]$Lines
    )

    $packet = Get-R156ChildJsonObject -Lines $Lines
    if ($null -eq $packet) {
        return $null
    }
    if (-not (Test-R156ChildExactSet -Actual (Get-R156ChildProperties -Object $packet) -Expected @('protocol','mode','canonical_valid','validation_status','validation_current','real_status','real_backups_remaining','real_success_shape'))) {
        return $null
    }
    if ([string]$packet.protocol -cne 'xb-r156-child/v1' -or
        [string]$packet.mode -cne $Mode) {
        return $null
    }
    if ($Mode -ceq 'VALIDATE_ONLY') {
        if ($packet.canonical_valid -isnot [bool] -or
            -not [bool]$packet.canonical_valid -or
            $packet.validation_status -isnot [string] -or
            $packet.validation_current -isnot [string] -or
            [string]$packet.validation_status -cne 'PASS' -or
            [string]$packet.validation_current -notin @('PASS', 'FAIL')) {
            return $null
        }
    }
    else {
        if ($packet.canonical_valid -isnot [bool] -or
            -not [bool]$packet.canonical_valid -or
            $packet.real_status -isnot [string] -or
            $packet.real_success_shape -isnot [bool] -or
            [string]$packet.real_status -notin @(
                'INSTALLED',
                'ALREADY_CURRENT',
                'FAILED_PREFLIGHT',
                'FAILED_ROLLED_BACK',
                'FAILED_ROLLBACK_INCOMPLETE'
            ) -or
            [string]::IsNullOrEmpty([string]$packet.real_status)) {
            return $null
        }
        try {
            if ([int64]$packet.real_backups_remaining -lt 0) {
                return $null
            }
        }
        catch {
            return $null
        }
    }
    return $packet
}

$mode = [Environment]::GetEnvironmentVariable('EG_R156_MODE', 'Process')
$checkout = [Environment]::GetEnvironmentVariable('EG_R156_CHECKOUT_ROOT', 'Process')
$installer = [Environment]::GetEnvironmentVariable('EG_R156_INSTALLER_PATH', 'Process')
$launcher = [Environment]::GetEnvironmentVariable('EG_R156_LAUNCHER_ROOT', 'Process')
$admission = [Environment]::GetEnvironmentVariable('EG_R156_ADMISSION_COMMIT', 'Process')
$packet = [ordered]@{
    protocol = 'xb-r156-child/v1'
    mode = [string]$mode
    canonical_valid = $false
    validation_status = ''
    validation_current = ''
    real_status = ''
    real_backups_remaining = -1
    real_success_shape = $false
}

try {
    if ($mode -notin @('VALIDATE_ONLY', 'REAL')) {
        throw 'invalid child mode'
    }
    if ([string]::IsNullOrWhiteSpace($checkout) -or
        [string]::IsNullOrWhiteSpace($installer) -or
        [string]::IsNullOrWhiteSpace($launcher) -or
        [string]::IsNullOrWhiteSpace($admission)) {
        throw 'missing child binding'
    }
    Write-R156ChildLine -Line ('R156|BEGIN|' + $mode)
    if ($mode -ceq 'REAL') {
        Write-R156ChildLine -Line 'R156|DISPATCH|REAL'
    }
    $contained = Invoke-R156ContainedInstaller -InstallerPath $installer -CheckoutRoot $checkout -LauncherRoot $launcher -AdmissionCommit $admission -ValidateOnly ($mode -ceq 'VALIDATE_ONLY')
    if (-not $contained.Completed) {
        throw 'contained installer invocation did not complete'
    }
    $canonical = Get-R156ChildJsonObject -Lines $contained.Lines
    if ($null -eq $canonical) {
        throw 'canonical installer output was not one JSON object'
    }
    if ($mode -ceq 'VALIDATE_ONLY') {
        $validation = Test-R156ChildValidationResult -Object $canonical
        if ($null -eq $validation) {
            throw 'canonical validation object failed contract'
        }
        $packet.canonical_valid = $true
        $packet.validation_status = $validation.ValidationStatus
        $packet.validation_current = $validation.ValidationCurrent
    }
    else {
        $real = Test-R156ChildRealResult -Object $canonical
        if ($null -eq $real) {
            throw 'canonical real object failed contract'
        }
        $packet.canonical_valid = $true
        $packet.real_status = $real.RealStatus
        $packet.real_backups_remaining = $real.RealBackupsRemaining
        $packet.real_success_shape = $real.RealSuccessShape
    }
}
catch {
    $packet.canonical_valid = $false
}
finally {
    Write-R156ChildLine -Line ('R156|PACKET|' + ($packet | ConvertTo-Json -Depth 8 -Compress))
    Write-R156ChildLine -Line ('R156|END|' + [string]$mode)
}
'@

function Test-R156ChildScriptParse {
    try {
        $tokens = $null
        $errors = $null
        [void][System.Management.Automation.Language.Parser]::ParseInput(
            $script:R156ChildScript,
            [ref]$tokens,
            [ref]$errors
        )
        return (@($errors).Count -eq 0)
    }
    catch {
        return $false
    }
}

function ConvertTo-R156EncodedCommand {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ScriptText)

    $encoding = New-Object System.Text.UnicodeEncoding($false, $true)
    return [Convert]::ToBase64String($encoding.GetBytes($ScriptText))
}

function Get-R156ChildProtocol {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Mode,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Stdout
    )

    $lines = @()
    if (-not [string]::IsNullOrEmpty($Stdout)) {
        $lines = @($Stdout -split '\r?\n')
        if ($lines.Count -gt 0 -and [string]::IsNullOrEmpty($lines[$lines.Count - 1])) {
            $lines = @($lines[0..($lines.Count - 2)])
        }
    }
    $expectedCount = 3
    if ($Mode -ceq 'REAL') {
        $expectedCount = 4
    }
    if ($lines.Count -ne $expectedCount) {
        return $null
    }
    if ($lines[0] -cne ('R156|BEGIN|' + $Mode)) {
        return $null
    }
    if ($Mode -ceq 'REAL') {
        if ($lines[1] -cne 'R156|DISPATCH|REAL') {
            return $null
        }
        $packetLine = $lines[2]
        if ($lines[3] -cne 'R156|END|REAL') {
            return $null
        }
    }
    else {
        $packetLine = $lines[1]
        if ($lines[2] -cne 'R156|END|VALIDATE_ONLY') {
            return $null
        }
    }
    $prefix = 'R156|PACKET|'
    if (-not $packetLine.StartsWith($prefix, [StringComparison]::Ordinal)) {
        return $null
    }
    $json = $packetLine.Substring($prefix.Length)
    $packet = $null
    try {
        $packet = @($json | ConvertFrom-Json)
    }
    catch {
        return $null
    }
    if ($packet.Count -ne 1 -or
        $packet[0] -isnot [System.Management.Automation.PSCustomObject]) {
        return $null
    }
    $packetObject = $packet[0]
    if (-not (Test-R156ExactPropertySet -Object $packetObject -Expected @(
            'protocol',
            'mode',
            'canonical_valid',
            'validation_status',
            'validation_current',
            'real_status',
            'real_backups_remaining',
            'real_success_shape'
        ))) {
        return $null
    }
    if ([string]$packetObject.protocol -cne 'xb-r156-child/v1' -or
        [string]$packetObject.mode -cne $Mode) {
        return $null
    }
    if ($Mode -ceq 'VALIDATE_ONLY') {
        if ($packetObject.canonical_valid -isnot [bool] -or
            -not [bool]$packetObject.canonical_valid -or
            $packetObject.validation_status -isnot [string] -or
            $packetObject.validation_current -isnot [string] -or
            [string]$packetObject.validation_status -cne 'PASS' -or
            [string]$packetObject.validation_current -notin @('PASS', 'FAIL')) {
            return $null
        }
    }
    else {
        if ($packetObject.canonical_valid -isnot [bool] -or
            -not [bool]$packetObject.canonical_valid -or
            $packetObject.real_status -isnot [string] -or
            $packetObject.real_success_shape -isnot [bool] -or
            [string]$packetObject.real_status -notin @(
                'INSTALLED',
                'ALREADY_CURRENT',
                'FAILED_PREFLIGHT',
                'FAILED_ROLLED_BACK',
                'FAILED_ROLLBACK_INCOMPLETE'
            ) -or
            [string]::IsNullOrEmpty([string]$packetObject.real_status)) {
            return $null
        }
        try {
            if ([int64]$packetObject.real_backups_remaining -lt 0) {
                return $null
            }
        }
        catch {
            return $null
        }
    }
    return $packetObject
}

function Test-R156DispatchMarkerObserved {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Stdout)

    if ([string]::IsNullOrEmpty($Stdout)) {
        return $false
    }
    $lines = @($Stdout -split '\r?\n')
    $matches = @($lines | Where-Object { $_ -ceq 'R156|DISPATCH|REAL' })
    return ($matches.Count -eq 1)
}

function Invoke-R156Transport {
    param(
        [Parameter(Mandatory)][ValidateSet('VALIDATE_ONLY', 'REAL')][string]$Mode,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRoot,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$InstallerPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRoot,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$AdmissionCommit
    )

    $encoded = ConvertTo-R156EncodedCommand -ScriptText $script:R156ChildScript
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = Join-Path $PSHOME 'powershell.exe'
    $startInfo.Arguments = (
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ' + $encoded
    )
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $startInfo.EnvironmentVariables['EG_R156_MODE'] = $Mode
    $startInfo.EnvironmentVariables['EG_R156_CHECKOUT_ROOT'] = $CheckoutRoot
    $startInfo.EnvironmentVariables['EG_R156_INSTALLER_PATH'] = $InstallerPath
    $startInfo.EnvironmentVariables['EG_R156_LAUNCHER_ROOT'] = $LauncherRoot
    $startInfo.EnvironmentVariables['EG_R156_ADMISSION_COMMIT'] = $AdmissionCommit

    $process = $null
    $started = $false
    try {
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        $started = [bool]$process.Start()
        if (-not $started) {
            return [pscustomobject]@{
                Started = $false
                SupervisorComplete = $false
                ChildExitCode = -1
                Packet = $null
            }
        }
        if ($Mode -ceq 'REAL') {
            $script:R156RealStarted = $true
            $script:R156RealInstallerInvocations = 1
            $script:R156AuthorityConsumed = 'YES'
            $script:R156PackageMutation = 'UNKNOWN'
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = [string]$stdoutTask.Result
        $null = $stderrTask.Result
        $exitCode = [int]$process.ExitCode
        if ($Mode -ceq 'REAL' -and (Test-R156DispatchMarkerObserved -Stdout $stdout)) {
            $script:R156PackageMutation = 'CANONICAL_TRANSACTION_ATTEMPTED'
        }
        return [pscustomobject]@{
            Started = $true
            SupervisorComplete = $true
            ChildExitCode = $exitCode
            Packet = Get-R156ChildProtocol -Mode $Mode -Stdout $stdout
        }
    }
    catch {
        $terminated = $false
        if ($started) {
            try {
                $terminated = [bool]$process.HasExited
            }
            catch {
                $terminated = $false
            }
        }
        return [pscustomobject]@{
            Started = $started
            SupervisorComplete = $false
            ChildExitCode = -1
            Packet = $null
            ChildTerminatedKnown = $terminated
        }
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
    }
}

function Test-R156PrivateBindingsOutsideCheckout {
    param(
        [Parameter(Mandatory)]$Topology,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRoot
    )

    foreach ($path in @(
            $Topology.RuntimeRoot,
            $Topology.Candidate,
            $Topology.ConfigDirectory,
            $Topology.ConfigPath,
            $Topology.CredentialDirectory,
            $Topology.CredentialPath,
            $Topology.VenvDirectory,
            $Topology.PythonExe,
            $Topology.BrowserCachePath
        )) {
        if (Test-R156PathWithin -Candidate $path -Container $CheckoutRoot) {
            return $false
        }
    }
    return $true
}

function Test-R156LocatorContinuity {
    param(
        [Parameter(Mandatory)]$Before,
        [Parameter(Mandatory)]$After
    )

    return (
        (Test-R156SamePath -Left $Before.RuntimeRoot -Right $After.RuntimeRoot) -and
        (Test-R156MetadataEqual -Before $Before.Metadata -After $After.Metadata)
    )
}

function Assert-R156PostProof {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LocatorPath,
        [Parameter(Mandatory)]$LocatorBefore,
        [Parameter(Mandatory)]$TopologyBefore,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRoot,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedHeadValue,
        [Parameter(Mandatory)][System.Collections.IDictionary]$CanonicalSources,
    [Parameter(Mandatory)]$TokenContext
    )

    $script:R156PostManifest = 'FAIL'
    $script:R156PostPackageManifest = 'FAIL'
    $locatorAfter = Read-R156Locator -LocatorPath $LocatorPath
    if ($null -eq $locatorAfter) {
        $script:R156LocatorContinuity = 'FAIL'
        Stop-R156Gate -SupportRef 'EG_R156_LOCATOR_READBACK_FAILED'
    }
    if (-not (Test-R156LocatorContinuity -Before $LocatorBefore -After $locatorAfter)) {
        $script:R156LocatorContinuity = 'FAIL'
        Stop-R156Gate -SupportRef 'EG_R156_LOCATOR_TARGET_CHANGED'
    }
    $script:R156LocatorContinuity = 'PASS'

    $topologyAfter = Resolve-R156Topology -RuntimeRoot $locatorAfter.RuntimeRoot -CheckoutRoot $CheckoutRoot
    if ($null -eq $topologyAfter) {
        Stop-R156Gate -SupportRef 'EG_R156_PRIVATE_TOPOLOGY_CHANGED'
    }
    if (-not (Test-R156TopologyContinuity -Before $TopologyBefore -After $topologyAfter)) {
        Stop-R156Gate -SupportRef 'EG_R156_PRIVATE_METADATA_CHANGED'
    }
    if (-not (Test-R156PrivateBindingsOutsideCheckout -Topology $topologyAfter -CheckoutRoot $CheckoutRoot)) {
        Stop-R156Gate -SupportRef 'EG_R156_PRIVATE_BINDING_INSIDE_CHECKOUT'
    }

    $manifestState = Read-R156ManifestState -LauncherRoot $topologyAfter.Candidate -ExpectedAdmission $ExpectedHeadValue -CanonicalSources $CanonicalSources
    if ($null -eq $manifestState) {
        Stop-R156Gate -SupportRef 'EG_R156_POST_PACKAGE_PROOF_FAILED'
    }
    $script:R156PostManifest = 'PASS'
    $script:R156PostPackageManifest = 'PASS'
    $script:R156CanonicalEquivalence = 'PASS'
    $script:R156LauncherClassification = 'A3/B0/C0/M0'

    if (-not (Test-R156LocatorSecurity -ProgramData $script:R156ProgramData -LocatorPath $LocatorPath -TokenContext $TokenContext)) {
        Stop-R156Gate -SupportRef 'EG_R156_LOCATOR_SECURITY_FAILED'
    }
    if (-not (Test-R156LauncherSecurity -LauncherRoot $topologyAfter.Candidate -ClassAPath $manifestState.ClassAPaths -TokenContext $TokenContext)) {
        Stop-R156Gate -SupportRef 'EG_R156_LAUNCHER_SECURITY_FAILED'
    }

    $state = Get-R156RepositoryState -RepositoryRoot $CheckoutRoot
    if (-not (Test-R156RepositoryFence -State $state -RepositoryRoot $CheckoutRoot -ExpectedHeadValue $ExpectedHeadValue -ExpectedTreeValue $script:R156ExpectedTreeAtExecution -ExpectedParentValue $script:R156ExpectedParentAtExecution)) {
        $script:R156RepositoryContinuity = 'FAIL'
        Stop-R156Gate -SupportRef 'EG_R156_REPOSITORY_CHANGED'
    }
    $script:R156RepositoryContinuity = 'PASS'
}

$tokenContext = $null
try {
    if (-not (Test-R156CommitText -Value $ExpectedHead) -or
        -not (Test-R156CommitText -Value $ExpectedTree) -or
        -not (Test-R156CommitText -Value $ExpectedParent)) {
        Stop-R156Gate -SupportRef 'EG_R156_EXPECTED_FENCE_INVALID'
    }
    $script:R156ExpectedHeadAtExecution = $ExpectedHead.ToLowerInvariant()
    $script:R156ExpectedTreeAtExecution = $ExpectedTree.ToLowerInvariant()
    $script:R156ExpectedParentAtExecution = $ExpectedParent.ToLowerInvariant()

    if ([string]$PSVersionTable.PSEdition -cne 'Desktop' -or
        [int]$PSVersionTable.PSVersion.Major -ne 5 -or
        [int]$PSVersionTable.PSVersion.Minor -ne 1) {
        Stop-R156Gate -SupportRef 'EG_R156_WINDOWS_POWERSHELL_51_REQUIRED'
    }
    if ([Environment]::UserInteractive -ne $true) {
        Stop-R156Gate -SupportRef 'EG_R156_NON_INTERACTIVE_CONTEXT'
    }
    $checkout = Get-R156FullPath -Path $script:R156Checkout
    if (-not (Test-R156SamePath -Left $checkout -Right 'C:\XB\automation') -or
        -not (Test-R156NormalDirectory -Path $checkout)) {
        Stop-R156Gate -SupportRef 'EG_R156_CHECKOUT_BINDING_INVALID'
    }

    $initialState = Get-R156RepositoryState -RepositoryRoot $checkout
    if (-not (Test-R156RepositoryFence -State $initialState -RepositoryRoot $checkout -ExpectedHeadValue $script:R156ExpectedHeadAtExecution -ExpectedTreeValue $script:R156ExpectedTreeAtExecution -ExpectedParentValue $script:R156ExpectedParentAtExecution)) {
        Stop-R156Gate -SupportRef 'EG_R156_REPOSITORY_FENCE_FAILED'
    }
    if (-not (Test-R156RemoteHead -RepositoryRoot $checkout -ExpectedHeadValue $script:R156ExpectedHeadAtExecution)) {
        $script:R156GithubAuth = 'FAIL'
        Stop-R156Gate -SupportRef 'EG_R156_GITHUB_HEAD_FAILED'
    }
    $script:R156GithubAuth = 'PASS'

    $canonicalLibrary = Read-R156TrustedSource -RepositoryRoot $checkout -RelativePath $script:R156LibraryRelative -ExpectedGitBlob $script:R156LibraryGitBlob -ExpectedGitBlobLength $script:R156LibraryGitBlobLength
    $canonicalLauncher = Read-R156TrustedSource -RepositoryRoot $checkout -RelativePath $script:R156LauncherRelative -ExpectedGitBlob $script:R156LauncherGitBlob -ExpectedGitBlobLength $script:R156LauncherGitBlobLength
    $canonicalInstaller = Read-R156TrustedSource -RepositoryRoot $checkout -RelativePath $script:R156InstallerRelative -ExpectedGitBlob $script:R156InstallerGitBlob -ExpectedGitBlobLength $script:R156InstallerGitBlobLength
    if ($null -eq $canonicalLibrary -or
        $null -eq $canonicalLauncher -or
        $null -eq $canonicalInstaller) {
        Stop-R156Gate -SupportRef 'EG_R156_CANONICAL_SOURCE_FAILED'
    }
    $canonicalSources = [ordered]@{
        'launcher.ps1' = $canonicalLauncher
        'launcher_lib.ps1' = $canonicalLibrary
    }
    $sourceRoot = Join-Path $checkout $script:R156RuntimeRelative
    $canonicalLibraryPath = Join-Path $sourceRoot 'launcher_lib.ps1'
    try {
        $ignoredDotSourceOutput = . $canonicalLibraryPath
    }
    catch {
        Stop-R156Gate -SupportRef 'EG_R156_LIBRARY_LOAD_FAILED'
    }
    if (-not (Test-R156ChildScriptParse)) {
        Stop-R156Gate -SupportRef 'EG_R156_CHILD_SCRIPT_PARSE_FAILED'
    }

    $tokenContext = Get-R156TokenContext
    if ($null -eq $tokenContext) {
        Stop-R156Gate -SupportRef 'EG_R156_TOKEN_CONTEXT_FAILED'
    }
    $programData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    if ([string]::IsNullOrWhiteSpace($programData) -or
        -not (Test-R156NormalDirectory -Path $programData)) {
        Stop-R156Gate -SupportRef 'EG_R156_PROGRAMDATA_BINDING_FAILED'
    }
    $script:R156ProgramData = Get-R156FullPath -Path $programData
    $locatorPath = Join-Path $script:R156ProgramData $script:R156LocatorRelative
    $locatorBefore = Read-R156Locator -LocatorPath $locatorPath
    if ($null -eq $locatorBefore) {
        Stop-R156Gate -SupportRef 'EG_R156_LOCATOR_READ_FAILED'
    }
    if (-not (Test-R156LocatorSecurity -ProgramData $script:R156ProgramData -LocatorPath $locatorPath -TokenContext $tokenContext)) {
        Stop-R156Gate -SupportRef 'EG_R156_LOCATOR_SECURITY_FAILED'
    }
    $locatorAfterSecurity = Read-R156Locator -LocatorPath $locatorPath
    if ($null -eq $locatorAfterSecurity -or
        -not (Test-R156LocatorContinuity -Before $locatorBefore -After $locatorAfterSecurity)) {
        Stop-R156Gate -SupportRef 'EG_R156_LOCATOR_TARGET_CHANGED'
    }

    $topology = Resolve-R156Topology -RuntimeRoot $locatorAfterSecurity.RuntimeRoot -CheckoutRoot $checkout
    if ($null -eq $topology) {
        Stop-R156Gate -SupportRef 'EG_R156_PRIVATE_TOPOLOGY_FAILED'
    }
    if (-not (Test-R156PrivateBindingsOutsideCheckout -Topology $topology -CheckoutRoot $checkout)) {
        Stop-R156Gate -SupportRef 'EG_R156_PRIVATE_BINDING_INSIDE_CHECKOUT'
    }
    $manifestState = Read-R156ManifestState -LauncherRoot $topology.Candidate -ExpectedAdmission $script:R156ExpectedParentAtExecution -CanonicalSources $canonicalSources
    if ($null -eq $manifestState) {
        Stop-R156Gate -SupportRef 'EG_R156_STALE_PREIMAGE_FAILED'
    }
    $script:R156CanonicalEquivalence = 'PASS'
    $script:R156PostManifest = 'NOT_RUN'
    $script:R156PostPackageManifest = 'NOT_RUN'
    $script:R156LauncherClassification = 'A3/B0/C0/M0'
    if (-not (Test-R156LauncherSecurity -LauncherRoot $topology.Candidate -ClassAPath $manifestState.ClassAPaths -TokenContext $tokenContext)) {
        Stop-R156Gate -SupportRef 'EG_R156_LAUNCHER_SECURITY_FAILED'
    }

    $preValidateState = Get-R156RepositoryState -RepositoryRoot $checkout
    if (-not (Test-R156RepositoryFence -State $preValidateState -RepositoryRoot $checkout -ExpectedHeadValue $script:R156ExpectedHeadAtExecution -ExpectedTreeValue $script:R156ExpectedTreeAtExecution -ExpectedParentValue $script:R156ExpectedParentAtExecution)) {
        Stop-R156Gate -SupportRef 'EG_R156_REPOSITORY_CHANGED'
    }
    $script:R156Validation = 'FAIL'
    $validationResult = Invoke-R156Transport -Mode 'VALIDATE_ONLY' -CheckoutRoot $checkout -InstallerPath $canonicalInstaller.Path -LauncherRoot $topology.Candidate -AdmissionCommit $script:R156ExpectedHeadAtExecution
    if ($null -eq $validationResult -or
        -not $validationResult.Started -or
        -not $validationResult.SupervisorComplete -or
        [int]$validationResult.ChildExitCode -ne 0 -or
        $null -eq $validationResult.Packet) {
        Stop-R156Gate -SupportRef 'EG_R156_VALIDATEONLY_TRANSPORT_FAILED'
    }
    $script:R156Validation = 'PASS'
    if ([string]$validationResult.Packet.validation_current -cne 'FAIL') {
        $script:R156RaceDetected = 'YES'
        $script:R156InstallerStatus = 'ALREADY_CURRENT'
        Stop-R156Gate -SupportRef 'EG_R156_VALIDATEONLY_ALREADY_CURRENT'
    }

    $locatorBeforeReal = Read-R156Locator -LocatorPath $locatorPath
    if ($null -eq $locatorBeforeReal -or
        -not (Test-R156LocatorContinuity -Before $locatorBefore -After $locatorBeforeReal)) {
        Stop-R156Gate -SupportRef 'EG_R156_LOCATOR_TARGET_CHANGED'
    }
    $topologyBeforeReal = Resolve-R156Topology -RuntimeRoot $locatorBeforeReal.RuntimeRoot -CheckoutRoot $checkout
    if ($null -eq $topologyBeforeReal -or
        -not (Test-R156TopologyContinuity -Before $topology -After $topologyBeforeReal)) {
        Stop-R156Gate -SupportRef 'EG_R156_PRIVATE_STATE_CHANGED'
    }
    $manifestBeforeReal = Read-R156ManifestState -LauncherRoot $topologyBeforeReal.Candidate -ExpectedAdmission $script:R156ExpectedParentAtExecution -CanonicalSources $canonicalSources
    if ($null -eq $manifestBeforeReal) {
        Stop-R156Gate -SupportRef 'EG_R156_STALE_PREIMAGE_MOVED'
    }
    if (-not (Test-R156LocatorSecurity -ProgramData $script:R156ProgramData -LocatorPath $locatorPath -TokenContext $tokenContext)) {
        Stop-R156Gate -SupportRef 'EG_R156_LOCATOR_SECURITY_FAILED'
    }
    if (-not (Test-R156LauncherSecurity -LauncherRoot $topologyBeforeReal.Candidate -ClassAPath $manifestBeforeReal.ClassAPaths -TokenContext $tokenContext)) {
        Stop-R156Gate -SupportRef 'EG_R156_LAUNCHER_SECURITY_FAILED'
    }
    $beforeRealState = Get-R156RepositoryState -RepositoryRoot $checkout
    if (-not (Test-R156RepositoryFence -State $beforeRealState -RepositoryRoot $checkout -ExpectedHeadValue $script:R156ExpectedHeadAtExecution -ExpectedTreeValue $script:R156ExpectedTreeAtExecution -ExpectedParentValue $script:R156ExpectedParentAtExecution)) {
        Stop-R156Gate -SupportRef 'EG_R156_REPOSITORY_CHANGED'
    }
    $canonicalLibraryBeforeReal = Read-R156TrustedSource -RepositoryRoot $checkout -RelativePath $script:R156LibraryRelative -ExpectedGitBlob $script:R156LibraryGitBlob -ExpectedGitBlobLength $script:R156LibraryGitBlobLength
    $canonicalLauncherBeforeReal = Read-R156TrustedSource -RepositoryRoot $checkout -RelativePath $script:R156LauncherRelative -ExpectedGitBlob $script:R156LauncherGitBlob -ExpectedGitBlobLength $script:R156LauncherGitBlobLength
    $canonicalInstallerBeforeReal = Read-R156TrustedSource -RepositoryRoot $checkout -RelativePath $script:R156InstallerRelative -ExpectedGitBlob $script:R156InstallerGitBlob -ExpectedGitBlobLength $script:R156InstallerGitBlobLength
    if ($null -eq $canonicalLibraryBeforeReal -or
        $null -eq $canonicalLauncherBeforeReal -or
        $null -eq $canonicalInstallerBeforeReal) {
        Stop-R156Gate -SupportRef 'EG_R156_CANONICAL_SOURCE_FAILED'
    }
    $canonicalSources['launcher.ps1'] = $canonicalLauncherBeforeReal
    $canonicalSources['launcher_lib.ps1'] = $canonicalLibraryBeforeReal
    $canonicalInstaller = $canonicalInstallerBeforeReal
    $manifestBeforeReal = Read-R156ManifestState -LauncherRoot $topologyBeforeReal.Candidate -ExpectedAdmission $script:R156ExpectedParentAtExecution -CanonicalSources $canonicalSources
    if ($null -eq $manifestBeforeReal) {
        Stop-R156Gate -SupportRef 'EG_R156_STALE_PREIMAGE_MOVED'
    }
    if (-not (Test-R156RemoteHead -RepositoryRoot $checkout -ExpectedHeadValue $script:R156ExpectedHeadAtExecution)) {
        $script:R156GithubAuth = 'FAIL'
        Stop-R156Gate -SupportRef 'EG_R156_GITHUB_HEAD_FAILED'
    }
    $script:R156GithubAuth = 'PASS'
    $realResult = Invoke-R156Transport -Mode 'REAL' -CheckoutRoot $checkout -InstallerPath $canonicalInstaller.Path -LauncherRoot $topologyBeforeReal.Candidate -AdmissionCommit $script:R156ExpectedHeadAtExecution
    if ($null -eq $realResult -or -not $realResult.Started) {
        Stop-R156Gate -SupportRef 'EG_R156_REAL_CHILD_START_FAILED'
    }
    if (-not $realResult.SupervisorComplete -or $null -eq $realResult.Packet) {
        $script:R156InstallerStatus = 'UNKNOWN'
        Stop-R156Gate -SupportRef 'EG_R156_POST_DISPATCH_UNCERTAIN'
    }
    $script:R156InstallerStatus = [string]$realResult.Packet.real_status
    if ([int]$realResult.ChildExitCode -ne 0) {
        Stop-R156Gate -SupportRef 'EG_R156_POST_DISPATCH_RESULT_FAILED'
    }
    if ($script:R156InstallerStatus -ceq 'ALREADY_CURRENT') {
        $script:R156RaceDetected = 'YES'
        Stop-R156Gate -SupportRef 'EG_R156_POST_DISPATCH_ALREADY_CURRENT'
    }
    if ($script:R156InstallerStatus -cne 'INSTALLED' -or
        -not [bool]$realResult.Packet.real_success_shape) {
        Stop-R156Gate -SupportRef 'EG_R156_POST_DISPATCH_INSTALL_FAILED'
    }
    $script:R156PackageMutation = 'CANONICAL_STAGED_AND_PUBLISHED'

    Assert-R156PostProof -LocatorPath $locatorPath -LocatorBefore $locatorBefore -TopologyBefore $topologyBeforeReal -CheckoutRoot $checkout -ExpectedHeadValue $script:R156ExpectedHeadAtExecution -CanonicalSources $canonicalSources -TokenContext $tokenContext
    $script:R156SupportRef = ''
    Write-R156Terminal -Terminal 'PASS' -Disposition 'PASS'
    return
}
catch {
    if ($script:R156RealStarted) {
        if ($script:R156SupportRef -eq 'EG_R156_UNEXPECTED_INFRASTRUCTURE') {
            $script:R156SupportRef = 'EG_R156_POST_DISPATCH_UNCERTAIN'
        }
        Write-R156Terminal -Terminal 'CONTROLLER_REQUIRED_POST_DISPATCH' -Disposition 'POST_DISPATCH_UNCERTAIN'
    }
    else {
        if ($script:R156SupportRef -eq 'EG_R156_UNEXPECTED_INFRASTRUCTURE') {
            $script:R156SupportRef = 'EG_R156_PRE_DISPATCH_GATE_FAILED'
        }
        Write-R156Terminal -Terminal 'CONTROLLER_REQUIRED_PRE_DISPATCH' -Disposition 'CONTROLLER_REQUIRED'
    }
}
finally {
    if ($null -ne $tokenContext) {
        if ($tokenContext.FilteredToken -ne [IntPtr]::Zero) {
            [EgR156.Native]::CloseToken($tokenContext.FilteredToken)
        }
        if ($tokenContext.FullToken -ne [IntPtr]::Zero) {
            [EgR156.Native]::CloseToken($tokenContext.FullToken)
        }
    }
}
