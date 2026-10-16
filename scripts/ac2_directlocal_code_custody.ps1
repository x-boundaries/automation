# AC2 #155 DirectLocal code custody guard (G2 A2, 16 October 2026).
#
# Dot-sourced by the credential-bearing Owner consoles. They call it BEFORE
# they run git or Python and before any credential prompt, and stop on any
# returned code. Read-only: it reads owner and DACL descriptors and file
# attributes, and never changes an ACL, a file or a path. The consoles then
# pin their working directory to the verified code root
# (Enter-XbDlTrustedWorkingDirectory, G2 A4) before any native command.
#
# A tree is in custody when no principal other than the current user, SYSTEM,
# Administrators or TrustedInstaller owns, or is granted any write-class right
# on, the tree root or anything below it, and no entry is a reparse point
# (symlink, junction or app-execution alias). Read and execute grants (for
# example BUILTIN\Users under C:\Program Files) are allowed: code holds no
# secret, but nobody else may change what runs. CREATOR OWNER and OWNER
# RIGHTS entries are allowed because they only ever apply to an owner, and
# every owner must already be trusted.
#
# Every ancestor of the tree root must also be a non-reparse directory on
# which no other principal can delete, take ownership of or rewrite the DACL
# of the path, or delete its children; that is what replacing or redirecting
# the tree would need. Creating a NEW entry beside an ancestor (for example
# Authenticated Users' create-folder right on C:\) does not change what runs.
# One exception (G2 A5): CPython reads pyvenv.cfg beside python.exe and in
# its parent, so no pyvenv.cfg may exist in either place, and nobody else
# may add a file to the interpreter directory's immediate parent. A venv
# interpreter is therefore refused: its standard library lies outside the
# verified tree.
#
# Limitation: this proves custody at the moment it runs; it is not a
# trusted bootstrap. It protects only when the console itself is installed
# in a tree that passes it (a protected runtime checkout, an Owner host
# decision). In a broadly writable development checkout it fails closed.
# Windows PowerShell 5.1 (.NET Framework) only.

Set-StrictMode -Version Latest

# SYSTEM, Administrators and NT SERVICE\TrustedInstaller; the current user is added per call.
$script:XbDlCustodyTrustedSids = @("S-1-5-18", "S-1-5-32-544", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
# CREATOR OWNER, OWNER RIGHTS.
$script:XbDlCustodyOwnerSids = @("S-1-3-0", "S-1-3-4")
# Write-class rights inside a tree: write data/add file (0x2), append/add
# subdirectory (0x4), write EA (0x10), delete child (0x40), write attributes
# (0x100), DELETE (0x10000), WRITE_DAC (0x40000), WRITE_OWNER (0x80000),
# GENERIC_ALL (0x10000000), GENERIC_WRITE (0x40000000).
$script:XbDlCustodyTreeMask = [int64](0x2 -bor 0x4 -bor 0x10 -bor 0x40 -bor 0x100 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000)
# Replacement or takeover rights on an ancestor: delete child, DELETE,
# WRITE_DAC, WRITE_OWNER, GENERIC_ALL.
$script:XbDlCustodyAncestorMask = [int64](0x40 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000)
# The interpreter directory's immediate parent (G2 A5): CPython reads
# pyvenv.cfg there, also under -I, and its "home" redirects the standard
# library. So, beyond the ancestor rights, nobody else may add a file
# there: write data/add file (0x2) or GENERIC_WRITE (0x40000000).
$script:XbDlCustodyInterpreterParentMask = [int64]($script:XbDlCustodyAncestorMask -bor 0x2 -bor 0x40000000)
$script:XbDlCustodyMaxEntries = 25000

function Get-XbDlSddlWriteRisk {
    # Pure check of one owner + DACL descriptor in SDDL form. Returns $null,
    # "owner_unsafe", "writable" or "acl_unverifiable".
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Sddl,
        [Parameter(Mandatory)][string]$UserSid,
        [switch]$Ancestor,
        # Ancestor semantics with the interpreter-parent mask (G2 A5).
        [switch]$InterpreterParent
    )
    try { $descriptor = New-Object Security.AccessControl.RawSecurityDescriptor -ArgumentList $Sddl } catch { return "acl_unverifiable" }
    $trusted = @($script:XbDlCustodyTrustedSids) + $UserSid
    if ($null -eq $descriptor.Owner -or $trusted -notcontains $descriptor.Owner.Value) { return "owner_unsafe" }
    # A null DACL grants everyone everything.
    if ($null -eq $descriptor.DiscretionaryAcl) { return "acl_unverifiable" }
    if ($InterpreterParent) { $Ancestor = [switch]$true }
    $mask = if ($InterpreterParent) { $script:XbDlCustodyInterpreterParentMask } elseif ($Ancestor) { $script:XbDlCustodyAncestorMask } else { $script:XbDlCustodyTreeMask }
    foreach ($ace in $descriptor.DiscretionaryAcl) {
        if ($ace -isnot [Security.AccessControl.CommonAce]) { return "acl_unverifiable" }  # object, compound or unknown ACE
        if ($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessDenied) { continue }
        if ($ace.AceQualifier -ne [Security.AccessControl.AceQualifier]::AccessAllowed) { return "acl_unverifiable" }
        if (([int64]$ace.AccessMask -band $mask) -eq 0) { continue }
        $sid = $ace.SecurityIdentifier.Value
        if ($trusted -contains $sid -or $script:XbDlCustodyOwnerSids -contains $sid) { continue }
        # An inherit-only entry on an ancestor never applies to the ancestor
        # itself; inside the tree it is materialised on new children, so it
        # is a risk there.
        if ($Ancestor -and (([int]$ace.AceFlags -band [int][Security.AccessControl.AceFlags]::InheritOnly) -ne 0)) { continue }
        return "writable"
    }
    return $null
}

function Get-XbDlPathSddl {
    param([Parameter(Mandatory)][IO.FileSystemInfo]$Item)
    $sections = [Security.AccessControl.AccessControlSections]"Owner, Access"
    if ($Item -is [IO.DirectoryInfo]) { $security = [IO.Directory]::GetAccessControl($Item.FullName, $sections) }
    else { $security = [IO.File]::GetAccessControl($Item.FullName, $sections) }
    return $security.GetSecurityDescriptorSddlForm($sections)
}

function Test-XbDlReparsePoint {
    param([Parameter(Mandatory)][IO.FileSystemInfo]$Item)
    return (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Get-XbDlAncestorCustodyIssue {
    # Every ancestor of -Directory, nearest first, up to the drive root.
    param([Parameter(Mandatory)][IO.DirectoryInfo]$Directory, [Parameter(Mandatory)][string]$Label)
    try {
        $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $parent = $Directory.Parent
        while ($null -ne $parent) {
            if (Test-XbDlReparsePoint $parent) { return "${Label}_ancestor_reparse_point" }
            $issue = Get-XbDlSddlWriteRisk -Sddl (Get-XbDlPathSddl $parent) -UserSid $userSid -Ancestor
            if ($issue) { return "${Label}_ancestor_$issue" }
            $parent = $parent.Parent
        }
    }
    catch { return "${Label}_acl_unverifiable" }
    return $null
}

function Get-XbDlEntriesCustodyIssue {
    # -Root and every entry below it, without following any reparse point.
    param(
        [Parameter(Mandatory)][IO.DirectoryInfo]$Root,
        [Parameter(Mandatory)][string]$Label,
        [int]$MaxEntries = $script:XbDlCustodyMaxEntries
    )
    try {
        $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $pending = New-Object System.Collections.Stack
        $pending.Push($Root)
        $count = 0
        while ($pending.Count -gt 0) {
            $item = $pending.Pop()
            $count++
            if ($count -gt $MaxEntries) { return "${Label}_too_large_to_verify" }
            if (Test-XbDlReparsePoint $item) { return "${Label}_reparse_point" }
            $issue = Get-XbDlSddlWriteRisk -Sddl (Get-XbDlPathSddl $item) -UserSid $userSid
            if ($issue) { return "${Label}_$issue" }
            if ($item -is [IO.DirectoryInfo]) {
                foreach ($child in $item.GetFileSystemInfos()) { $pending.Push($child) }
            }
        }
    }
    catch { return "${Label}_acl_unverifiable" }
    return $null
}

function Get-XbDlTreeCustodyIssue {
    # Returns $null when the tree and its ancestors are in custody, else
    # "<Label>_<reason>". Ancestors are checked first.
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][ValidatePattern('^[a-z_]{1,32}$')][string]$Label,
        [int]$MaxEntries = $script:XbDlCustodyMaxEntries
    )
    # A local drive path only: no UNC, device or relative path.
    if ($Root -notmatch '^[A-Za-z]:\\') { return "${Label}_path_unsafe" }
    try { $info = New-Object IO.DirectoryInfo -ArgumentList ([IO.Path]::GetFullPath($Root)) } catch { return "${Label}_path_unsafe" }
    if (-not $info.Exists) { return "${Label}_missing" }
    $issue = Get-XbDlAncestorCustodyIssue -Directory $info -Label $Label
    if ($issue) { return $issue }
    return (Get-XbDlEntriesCustodyIssue -Root $info -Label $Label -MaxEntries $MaxEntries)
}

function Get-XbDlInterpreterConfigIssue {
    # G2 A5. -InterpreterDir is the directory holding python.exe, whose tree
    # and ancestors were already verified. Checks only that directory and its
    # immediate parent for pyvenv.cfg, and the parent's own descriptor; no
    # other entry of the parent is read.
    param([Parameter(Mandatory)][IO.DirectoryInfo]$InterpreterDir)
    try {
        $parent = $InterpreterDir.Parent
        foreach ($dir in @($InterpreterDir, $parent)) {
            if ($null -eq $dir) { continue }
            $config = Join-Path $dir.FullName "pyvenv.cfg"
            # Any entry, including a dangling link, counts as present.
            if ([IO.File]::Exists($config) -or [IO.Directory]::Exists($config)) { return "interpreter_pyvenv_cfg_present" }
        }
        if ($null -eq $parent) { return $null }
        $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $issue = Get-XbDlSddlWriteRisk -Sddl (Get-XbDlPathSddl $parent) -UserSid $userSid -InterpreterParent
        if ($issue) { return "interpreter_parent_$issue" }
    }
    catch { return "interpreter_parent_acl_unverifiable" }
    return $null
}

function Enter-XbDlTrustedWorkingDirectory {
    # G2 A4 (16 October 2026). The Owner consoles call this once the code
    # root's custody is proven and before their first native command. A bare
    # executable name is searched in the current directory before PATH, and
    # native children start in the console's location, so an attacker-writable
    # launch directory could otherwise supply tasklist.exe, powershell.exe or
    # a DLL to an elevated child. This changes only this process's location
    # and environment, never a file or an ACL. Returns $null or a stop code.
    param([Parameter(Mandatory)][string]$Root)
    # Defence in depth for every child: CreateProcess and cmd then skip the
    # current directory for bare names.
    $env:NoDefaultCurrentDirectoryInExePath = "1"
    try {
        $full = [IO.Path]::GetFullPath($Root)
        Set-Location -LiteralPath $full -ErrorAction Stop
        [Environment]::CurrentDirectory = $full
        if ([Environment]::CurrentDirectory -ine $full -or (Get-Location).ProviderPath -ine $full) { return "working_directory_unpinned" }
    }
    catch { return "working_directory_unpinned" }
    return $null
}

function Get-XbDlOwnerRuntimeIssue {
    # Code root, interpreter and git custody for an Owner console, in that
    # order. Returns $null or the first code. -PythonExe and -GitExe must be
    # absolute paths to python.exe / git.exe directly inside their trees.
    param(
        [Parameter(Mandatory)][string]$CodeRoot,
        [Parameter(Mandatory)][string]$PythonExe,
        [Parameter(Mandatory)][string]$GitRoot
    )
    $issue = Get-XbDlTreeCustodyIssue -Root $CodeRoot -Label "code_root"
    if ($issue) { return $issue }
    # A standalone checkout: a worktree's .git file points at a git
    # directory (config, hooks) outside the verified tree.
    if (-not [IO.Directory]::Exists((Join-Path $CodeRoot ".git"))) { return "code_root_not_standalone_checkout" }
    if ($PythonExe -notmatch '^[A-Za-z]:\\' -or [IO.Path]::GetFileName($PythonExe) -ine "python.exe" -or -not [IO.File]::Exists($PythonExe)) { return "interpreter_path_unsafe" }
    $interpreterDir = [IO.Path]::GetDirectoryName($PythonExe)
    $issue = Get-XbDlTreeCustodyIssue -Root $interpreterDir -Label "interpreter"
    if ($issue) { return $issue }
    $issue = Get-XbDlInterpreterConfigIssue -InterpreterDir (New-Object IO.DirectoryInfo -ArgumentList ([IO.Path]::GetFullPath($interpreterDir)))
    if ($issue) { return $issue }
    if (-not [IO.File]::Exists((Join-Path $GitRoot "cmd\git.exe"))) { return "git_path_unsafe" }
    $issue = Get-XbDlTreeCustodyIssue -Root $GitRoot -Label "git"
    if ($issue) { return $issue }
    return $null
}
