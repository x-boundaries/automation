# Pinned clone check for scheduled tasks on the AC2 server (#117, 24 Nov 2026).
#
# Dot-source this file. Windows PowerShell 5.1 compatible. Reads git only; changes nothing.
#
# Why: a scheduled task that runs from a shared, auto-updated checkout (for example the
# C:\XB\automation folder that a chat start-up hook fast-forwards to origin/main) runs every
# merge as soon as that checkout moves, with no approval step. Each task must instead run from
# its own fixed clone, C:\XB\golive\automation-<purpose>-<sha7>, updated only by an approved
# step (docs/autocount2-automation/vm_pinned_clones_runbook.md).
#
# Get-XbPinnedCloneProblem returns "" when RepoRoot is such a clone, else one plain sentence:
#   1. the folder is named automation-<purpose>-<sha7> (lowercase letters, digits and -) and
#      sits in a folder named golive;
#   2. git reads its HEAD, and HEAD starts with that sha7;
#   3. HEAD is detached (no branch checked out), so nothing that pulls or fast-forwards a
#      branch can move it;
#   4. no tracked file is changed, and none is hidden from git status by assume-unchanged or
#      skip-worktree (untracked files, such as Python caches, are not checked);
#   5. its parent folder is exactly GoliveRoot (default C:\XB\golive).

$XbPinnedCloneNamePattern = '\Aautomation-[a-z0-9]+(-[a-z0-9]+)*-([0-9a-f]{7})\z'

function Get-XbPinnedCloneProblem {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$RepoRoot,
        [string]$GoliveRoot = "C:\XB\golive"
    )

    # Windows PowerShell 5.1 turns a native command's stderr into a terminating error under
    # "Stop"; here a git failure must come back as a sentence instead.
    $ErrorActionPreference = "Continue"
    if ([string]::IsNullOrWhiteSpace($RepoRoot) -or -not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
        return "the code folder does not exist."
    }
    $full = (Resolve-Path -LiteralPath $RepoRoot).Path.TrimEnd('\')
    $leaf = Split-Path -Leaf $full
    $parent = Split-Path -Leaf (Split-Path -Parent $full)
    $match = [regex]::Match($leaf, $XbPinnedCloneNamePattern)
    if (-not $match.Success -or -not [string]::Equals($parent, "golive", [StringComparison]::OrdinalIgnoreCase)) {
        return "the code folder $full is not a pinned clone (expected ...\golive\automation-<purpose>-<sha7>)."
    }
    $sha7 = $match.Groups[2].Value
    if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
        return "git is not on PATH, so the pinned clone cannot be checked."
    }
    $head = & git -C $full rev-parse --verify HEAD 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$head)) {
        return "git cannot read HEAD in $full."
    }
    $head = ([string]$head).Trim()
    if (-not $head.StartsWith($sha7, [StringComparison]::Ordinal)) {
        return "HEAD in $full is $($head.Substring(0, 7)), not $sha7 as its folder name says."
    }
    & git -C $full symbolic-ref -q HEAD 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        return "$full has a branch checked out; a pinned clone has a detached HEAD (git checkout --detach $sha7)."
    }
    $changed = & git -C $full status --porcelain --untracked-files=no 2>$null
    if ($LASTEXITCODE -ne 0) {
        return "git status failed in $full."
    }
    if (-not [string]::IsNullOrWhiteSpace(($changed | Out-String))) {
        return "$full has changed tracked files."
    }
    # ls-files -v marks assume-unchanged files with a lowercase tag and skip-worktree files with
    # S; git status does not show changes to either.
    $tags = & git -C $full ls-files -v 2>$null
    if ($LASTEXITCODE -ne 0) {
        return "git ls-files failed in $full."
    }
    if (@($tags | Where-Object { $_ -cmatch '\A[a-zS] ' }).Count -gt 0) {
        return "$full has tracked files hidden from git status (assume-unchanged or skip-worktree)."
    }
    # Checked last, so a clone elsewhere that passes everything else names only this.
    $root = $GoliveRoot.TrimEnd('\')
    if (-not [string]::Equals((Split-Path -Parent $full).TrimEnd('\'), $root, [StringComparison]::OrdinalIgnoreCase)) {
        return "the code folder $full is not in $root."
    }
    return ""
}

# The installers' dry-run line for the result above.
function Format-XbPinnedCloneLine {
    param([AllowEmptyString()][string]$Problem)
    if ([string]::IsNullOrEmpty($Problem)) {
        return "Pinned clone: yes."
    }
    return "Pinned clone: NO, so registering would be refused: $Problem"
}

# The installers call this after the dry run and before any password prompt or registration.
function Assert-XbPinnedClone {
    param([AllowEmptyString()][string]$Problem)
    if (-not [string]::IsNullOrEmpty($Problem)) {
        throw "Refusing to register: $Problem Run this installer from a pinned clone (docs/autocount2-automation/vm_pinned_clones_runbook.md)."
    }
}

# Get-XbRepointedAction works out a task action that runs the same script from NewRepoRoot.
# Only two things change: the quoted script path at the start of the arguments (same file,
# other clone) and the working folder. The program and every other argument stay byte for
# byte. It throws, changing nothing, when that cannot be done without changing behaviour:
#   - the program is a bare name (Windows may look it up in the working folder first);
#   - the working folder is empty or not a full path;
#   - the arguments do not start with "<working folder>\<script>" in double quotes;
#   - the script is not in NewRepoRoot;
#   - another argument is a relative path (its meaning would move with the working folder).
# An argument that still names a file under the old folder (for example a settings file kept
# there) is kept as is and reported in Notes.
function Get-XbRepointedAction {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Execute,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Arguments,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$NewRepoRoot
    )

    if ([string]::IsNullOrWhiteSpace($Execute) -or -not [System.IO.Path]::IsPathRooted($Execute.Trim('"'))) {
        throw "The task's program is not a full path; re-point it by reinstalling instead."
    }
    if ([string]::IsNullOrWhiteSpace($WorkingDirectory) -or -not [System.IO.Path]::IsPathRooted($WorkingDirectory)) {
        throw "The task has no full-path working folder, so its clone cannot be worked out."
    }
    $oldRoot = $WorkingDirectory.TrimEnd('\')
    $newRoot = $NewRepoRoot.TrimEnd('\')
    if ([string]::Equals($oldRoot, $newRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The task already runs from $newRoot."
    }
    $prefix = '"' + $oldRoot + '\'
    if (-not $Arguments.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The task's arguments do not start with the quoted script path under its working folder."
    }
    $close = $Arguments.IndexOf('"', $prefix.Length)
    if ($close -lt 0) {
        throw "The task's script path has no closing quote."
    }
    $relative = $Arguments.Substring($prefix.Length, $close - $prefix.Length)
    if ([string]::IsNullOrWhiteSpace($relative) -or $relative.Contains('..')) {
        throw "The task's script path is not a plain file under its working folder."
    }
    $newScript = Join-Path $newRoot $relative
    if (-not (Test-Path -LiteralPath $newScript -PathType Leaf)) {
        throw "The script $relative is not in $newRoot."
    }
    $rest = $Arguments.Substring($close + 1)
    $notes = @()
    foreach ($token in [regex]::Matches($rest, '"[^"]*"|\S+')) {
        $value = $token.Value.Trim('"')
        if ($value.StartsWith("-") -or $value.Length -eq 0) {
            continue
        }
        $looksLikePath = $value.Contains('\') -or $value.Contains('/') -or ($value -match '\.[A-Za-z0-9]{1,5}\z')
        if ($looksLikePath -and -not [System.IO.Path]::IsPathRooted($value)) {
            throw "The argument $value is a relative path; it would point somewhere else from the new working folder."
        }
        if ($value.StartsWith($oldRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            $notes += "The argument $value is under the old folder $oldRoot and is kept as is."
        }
    }
    return [pscustomobject]@{
        Execute          = $Execute
        Arguments        = '"' + $newScript + '"' + $rest
        WorkingDirectory = $newRoot
        Notes            = $notes
    }
}

# Write-XbTaskBackup saves an Export-ScheduledTask XML text as UTF-16 with a byte order mark,
# matching its encoding="UTF-16" declaration. It never overwrites a file.
function Write-XbTaskBackup {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$XmlText
    )
    if (Test-Path -LiteralPath $Path) {
        throw "The backup file already exists; nothing changed."
    }
    [System.IO.File]::WriteAllText($Path, $XmlText, (New-Object System.Text.UnicodeEncoding($false, $true)))
}

# Get-XbActionFromTaskBackup reads a backup written by Write-XbTaskBackup and returns the one
# program action in it (Execute, Arguments, WorkingDirectory), for the rollback. It throws,
# changing nothing, unless:
#   - the file's RegistrationInfo/URI names TaskUri (the task being rolled back);
#   - it has exactly one Exec action, with a Command;
#   - that Command equals CurrentExecute (the rollback never changes the program).
# Entity-encoded text such as &quot; comes back as plain characters.
function Get-XbActionFromTaskBackup {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$TaskUri,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$CurrentExecute
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "The backup file does not exist."
    }
    [xml]$doc = Get-Content -LiteralPath $Path -Raw
    $ns = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
    $ns.AddNamespace("t", "http://schemas.microsoft.com/windows/2004/02/mit/task")
    $uriNode = $doc.SelectSingleNode("/t:Task/t:RegistrationInfo/t:URI", $ns)
    if ($null -eq $uriNode -or [string]::IsNullOrWhiteSpace($uriNode.InnerText)) {
        throw "The backup has no task URI, so it cannot be matched to a task."
    }
    if (-not [string]::Equals($uriNode.InnerText, $TaskUri, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The backup is for $($uriNode.InnerText), not $TaskUri."
    }
    $execs = @($doc.SelectNodes("/t:Task/t:Actions/t:Exec", $ns))
    $others = @($doc.SelectNodes("/t:Task/t:Actions/*", $ns))
    if ($execs.Count -ne 1 -or $others.Count -ne 1) {
        throw "The backup must have exactly one action, a program action."
    }
    $field = {
        param([string]$Name)
        $node = $execs[0].SelectSingleNode("t:$Name", $ns)
        if ($null -eq $node) { "" } else { [string]$node.InnerText }
    }
    $action = [pscustomobject]@{
        Execute          = & $field "Command"
        Arguments        = & $field "Arguments"
        WorkingDirectory = & $field "WorkingDirectory"
        Notes            = @()
    }
    if ([string]::IsNullOrWhiteSpace($action.Execute)) {
        throw "The backup's action has no program."
    }
    if (-not [string]::Equals($action.Execute, $CurrentExecute, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The program would change ($CurrentExecute to $($action.Execute)); the rollback never changes the program."
    }
    return $action
}
