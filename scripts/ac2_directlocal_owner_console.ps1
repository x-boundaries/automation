# AC2 #155 DirectLocal Owner console: the only way a real-book DirectLocal
# command receives AutoCount credentials.
#
# Run in an interactive, elevated Windows PowerShell console on DESKTOP-4I042L6
# (elevation is needed for the complete isolation inventory). It:
#   - refuses redirected stdin/stdout, a non-interactive session, another host,
#     stale credential or git override variables;
#   - before it runs git or Python, proves code custody (G2 A2): the code root,
#     the Python interpreter tree and the git tree, and every ancestor of each,
#     must not be writable by anyone but the current user, SYSTEM,
#     Administrators or TrustedInstaller, with no reparse point
#     (ac2_directlocal_code_custody.ps1). A broadly writable development
#     checkout therefore always stops here; only a protected runtime checkout
#     (a separate Owner host decision) can pass;
#   - refuses another source head and any modified, untracked or ignored file;
#   - runs Python isolated: -I (no script directory, user site or PYTHON*
#     variables), -B (no bytecode written) and, for the core, -S (no
#     site-packages);
#   - collects fresh read-only isolation evidence for AED_TESTINGAPI into
#     <StateRoot>\evidence and stops unless the verdict is ISOLATED;
#   - for -Command Process with -EnableWrite, requires typing AED_TESTINGAPI;
#   - prompts for the AutoCount user ID and (as a SecureString) the password
#     and passes them only in the child Python process's environment block
#     (the bridge children inherit it); the BSTR is zeroed and freed and the
#     SecureString disposed in finally; this console's environment never
#     holds them;
#   - prints only the core's single JSON line (codes, states, keyed refs).
# Limitation: the adapter reads the password from an environment variable, so
# managed copies exist briefly in the child processes.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$StateRoot,
    [Parameter(Mandatory)][ValidateSet("BookCheck", "PosSyncStatus", "Process", "Reconcile")][string]$Command,
    [string]$Capture,
    [switch]$EnableWrite,
    # The exact source head reported for this procedure; any other checkout stops.
    [string]$ExpectedHead,
    [string]$Python = "python",
    # git runs only from this tree, after its custody is proven.
    [string]$GitRoot = "C:\Program Files\Git"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "ac2_directlocal_code_custody.ps1")

function Stop-XbDlOwner {
    param([Parameter(Mandatory)][string]$Code)
    [Console]::Out.WriteLine((@{ status = "OWNER_PROCEDURE_STOPPED"; code = $Code } | ConvertTo-Json -Compress))
    exit 1
}

if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected -or -not [Environment]::UserInteractive) { Stop-XbDlOwner "owner_console_required" }
if ($env:COMPUTERNAME -ine "DESKTOP-4I042L6") { Stop-XbDlOwner "host_not_authorised" }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Stop-XbDlOwner "elevated_console_required" }
foreach ($name in @("XB_AC2_USER_ID", "XB_AC2_PASSWORD_ENV_VAR", "XB_AC2_SESSION_FACTORY", "AC2_PROBE_USER_ID", "AC2_PROBE_PASSWORD")) {
    if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name, "Process"))) { Stop-XbDlOwner "stale_credential_environment" }
}
if (@(Get-ChildItem Env: | Where-Object { $_.Name -like "GIT_*" }).Count -gt 0) { Stop-XbDlOwner "git_environment_override" }
if ($Command -in @("Process", "Reconcile") -and [string]::IsNullOrWhiteSpace($Capture)) { Stop-XbDlOwner "capture_required" }
if ($EnableWrite -and $Command -ne "Process") { Stop-XbDlOwner "write_only_for_process" }

# 0. Code custody, before any git or Python runs and before any prompt.
try { $pythonExe = [string](Get-Command $Python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source } catch { Stop-XbDlOwner "interpreter_path_unsafe" }
$custody = Get-XbDlOwnerRuntimeIssue -CodeRoot $repo -PythonExe $pythonExe -GitRoot $GitRoot
if ($custody) { Stop-XbDlOwner $custody }
$git = Join-Path $GitRoot "cmd\git.exe"
$head = [string](& $git -c core.fsmonitor=false -C $repo rev-parse HEAD 2>$null)
if ($ExpectedHead -notmatch '^[0-9a-f]{40}$' -or $head.Trim() -cne $ExpectedHead) { Stop-XbDlOwner "source_head_mismatch" }
$dirty = [string](& $git -c core.fsmonitor=false -C $repo status --porcelain --untracked-files=all --ignored 2>$null)
if ($LASTEXITCODE -ne 0 -or -not [string]::IsNullOrEmpty($dirty)) { Stop-XbDlOwner "source_tree_not_exact" }
Write-Host "Code custody and source head verified: $ExpectedHead"

# 1. Fresh read-only isolation evidence for the bound test book.
$evidenceDir = Join-Path $StateRoot "evidence"
if (-not (Test-Path -LiteralPath $evidenceDir -PathType Container)) { Stop-XbDlOwner "evidence_directory_missing" }
# The collector needs pyodbc, so it keeps site-packages (inside the verified interpreter tree).
$collector = & $pythonExe -I -B (Join-Path $repo "scripts\ac2_directlocal_e7_isolation_check.py") $evidenceDir | Select-Object -Last 1
$iso = $collector | ConvertFrom-Json
Write-Host ("Isolation: {0} blockers={1} failed={2}" -f $iso.verdict, (@($iso.blockers) -join ","), (@($iso.failed_inspections) -join ","))
if ($iso.verdict -cne "ISOLATED") { Stop-XbDlOwner "isolation_not_proven" }

if ($EnableWrite) {
    $typed = Read-Host "Type AED_TESTINGAPI to allow at most one member create for this capture"
    if ($typed -cne "AED_TESTINGAPI") { Stop-XbDlOwner "write_not_confirmed" }
}

$core = @{ BookCheck = @("book-check"); PosSyncStatus = @("pos-sync-status"); Process = @("process", "--capture", $Capture); Reconcile = @("reconcile", "--capture", $Capture) }[$Command]
if ($EnableWrite) { $core += "--enable-autocount-write" }

# 2. Credentials typed locally; passed only to the child process environment.
$userId = Read-Host "AutoCount user ID"
$secure = Read-Host -AsSecureString "AutoCount password"
$bstr = [IntPtr]::Zero
$process = $null
try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $pythonExe
    $quoted = @((Join-Path $repo "directlocal\xb_directlocal.py"), "--state-root", $StateRoot) + $core | ForEach-Object { '"{0}"' -f ($_ -replace '"', '') }
    $psi.Arguments = "-I -S -B " + ($quoted -join " ")
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables["AC2_PROBE_USER_ID"] = $userId
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $psi.EnvironmentVariables["AC2_PROBE_PASSWORD"] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $process = [System.Diagnostics.Process]::Start($psi)
    $psi.EnvironmentVariables.Remove("AC2_PROBE_PASSWORD")
    $psi.EnvironmentVariables.Remove("AC2_PROBE_USER_ID")
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $stdout = $process.StandardOutput.ReadToEnd()
    $process.WaitForExit()
    $stderrLines = @($stderrTask.Result -split "`r?`n" | Where-Object { $_ }).Count
    $line = @($stdout -split "`r?`n" | Where-Object { $_ -like "{*" } | Select-Object -Last 1)
    if ($line.Count -eq 1) { [Console]::Out.WriteLine($line[0]) } else { Stop-XbDlOwner "core_output_missing" }
    Write-Host ("Core exit code {0}; stderr lines suppressed: {1}" -f $process.ExitCode, $stderrLines)
}
finally {
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $secure.Dispose()
    $userId = $null
    if ($null -ne $process) { $process.Dispose() }
    [GC]::Collect()
}
