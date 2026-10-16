# AC2 #155 E7 Owner console procedure: fresh isolation evidence, then the E7
# test-book script with credentials typed locally by the Owner.
#
# Run in an interactive, elevated Windows PowerShell console on DESKTOP-4I042L6
# (elevation is needed for a complete scheduled-task/service inventory).
# After code custody passes and before its first native command it makes the
# verified code root its working directory (G2 A4), so the launch directory
# can never supply an executable to an elevated child.
# Credential handling:
#   - refuses redirected stdin/stdout, so secrets can only be typed at the
#     console and nothing a tool pipes in is accepted;
#   - the password is read with Read-Host -AsSecureString and decrypted into a
#     BSTR only to fill the E7 child process's private environment block; the
#     BSTR is zeroed and freed in finally (ZeroFreeBSTR), the SecureString is
#     disposed, and the managed copy is dropped;
#   - this console's own environment is never given the user ID or password;
#     the child environment is discarded when the child exits;
#   - only the E7 JSON line (no secrets, no MemberNo) is printed.
# Limitation: the adapter reads the password from an environment variable, so
# a managed string copy exists briefly in this process and in the child.
[CmdletBinding()]
param(
    [ValidateSet("DryRun", "Write")][string]$Mode = "DryRun",
    # The exact source head reported for this procedure; any other checkout stops.
    [string]$ExpectedHead,
    [string]$Workspace = "C:\XB\xb155-e7-testingapi-uat",
    [string]$Python = "python",
    # git runs only from this tree, after its custody is proven.
    [string]$GitRoot = "C:\Program Files\Git"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "ac2_directlocal_code_custody.ps1")

function Stop-E7Owner {
    param([Parameter(Mandatory)][string]$Code)
    [Console]::Out.WriteLine((@{ status = "OWNER_PROCEDURE_STOPPED"; code = $Code } | ConvertTo-Json -Compress))
    exit 1
}

if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected -or -not [Environment]::UserInteractive) { Stop-E7Owner "owner_console_required" }
if ($env:COMPUTERNAME -ine "DESKTOP-4I042L6") { Stop-E7Owner "host_not_authorised" }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Stop-E7Owner "elevated_console_required" }
foreach ($name in @("XB_AC2_USER_ID", "XB_AC2_PASSWORD_ENV_VAR", "XB_AC2_SESSION_FACTORY", "AC2_PROBE_USER_ID", "AC2_PROBE_PASSWORD")) {
    # Stale credentials in the console environment would bypass the prompt.
    if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name, "Process"))) { Stop-E7Owner "stale_credential_environment" }
}
if (@(Get-ChildItem Env: | Where-Object { $_.Name -like "GIT_*" }).Count -gt 0) { Stop-E7Owner "git_environment_override" }
# Code custody (G2 A2, 2026-10-16) before any git or Python runs and before
# any prompt; see ac2_directlocal_code_custody.ps1.
try { $pythonExe = [string](Get-Command $Python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source } catch { Stop-E7Owner "interpreter_path_unsafe" }
$custody = Get-XbDlOwnerRuntimeIssue -CodeRoot $repo -PythonExe $pythonExe -GitRoot $GitRoot
if ($custody) { Stop-E7Owner $custody }
# G2 A4: the verified code root is the working directory before any native
# command, and NoDefaultCurrentDirectoryInExePath is set for every child.
$pinned = Enter-XbDlTrustedWorkingDirectory -Root $repo
if ($pinned) { Stop-E7Owner $pinned }
$git = Join-Path $GitRoot "cmd\git.exe"
$head = [string](& $git -c core.fsmonitor=false -C $repo rev-parse HEAD 2>$null)
if ($ExpectedHead -notmatch '^[0-9a-f]{40}$' -or $head.Trim() -cne $ExpectedHead) { Stop-E7Owner "source_head_mismatch" }
$dirty = [string](& $git -c core.fsmonitor=false -C $repo status --porcelain --untracked-files=all --ignored 2>$null)
if ($LASTEXITCODE -ne 0 -or -not [string]::IsNullOrEmpty($dirty)) { Stop-E7Owner "source_tree_not_exact" }
Write-Host "Code custody and source head verified: $ExpectedHead"

# 1. Fresh isolation evidence (read-only). Its printed SHA-256 is the custody link.
$collector = & $pythonExe -I -B (Join-Path $repo "scripts\ac2_directlocal_e7_isolation_check.py") (Join-Path $Workspace "evidence") | Select-Object -Last 1
$iso = $collector | ConvertFrom-Json
Write-Host ("Isolation: {0} blockers={1} failed={2}" -f $iso.verdict, (@($iso.blockers) -join ","), (@($iso.failed_inspections) -join ","))
if ($iso.verdict -cne "ISOLATED") { Stop-E7Owner "isolation_not_proven" }

if ($Mode -eq "Write") {
    $typed = Read-Host "Type AED_TESTINGAPI to perform the single synthetic member write"
    if ($typed -cne "AED_TESTINGAPI") { Stop-E7Owner "write_not_confirmed" }
}

# 2. Credentials typed locally; passed only to the child process environment.
$userId = Read-Host "AutoCount user ID"
$secure = Read-Host -AsSecureString "AutoCount password"
$bstr = [IntPtr]::Zero
$process = $null
try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $PSHOME "powershell.exe"
    $psi.WorkingDirectory = $repo
    $arguments = @("-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
        ('"{0}"' -f (Join-Path $repo "scripts\ac2_directlocal_e7_testbook_create.ps1")),
        "-StateDir", ('"{0}"' -f (Join-Path $Workspace "state")),
        "-IsolationEvidencePath", ('"{0}"' -f $iso.evidence),
        "-IsolationEvidenceSha256", $iso.sha256)
    if ($Mode -eq "Write") { $arguments += @("-EnableTestBookWrite", "-ConfirmTargetAedTestingApi", "-ConfirmOwnerRiskAccepted") }
    $psi.Arguments = $arguments -join " "
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables["AC2_PROBE_USER_ID"] = $userId
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $psi.EnvironmentVariables["AC2_PROBE_PASSWORD"] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $process = [System.Diagnostics.Process]::Start($psi)
    # The child has its own copy now; drop every copy held here.
    $psi.EnvironmentVariables.Remove("AC2_PROBE_PASSWORD")
    $psi.EnvironmentVariables.Remove("AC2_PROBE_USER_ID")
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $stdout = $process.StandardOutput.ReadToEnd()
    $process.WaitForExit()
    $stderrLines = @($stderrTask.Result -split "`r?`n" | Where-Object { $_ }).Count
    $line = @($stdout -split "`r?`n" | Where-Object { $_ -like "{*" } | Select-Object -Last 1)
    if ($line.Count -eq 1) { [Console]::Out.WriteLine($line[0]) } else { Stop-E7Owner "e7_output_missing" }
    Write-Host ("E7 exit code {0}; stderr lines suppressed: {1}" -f $process.ExitCode, $stderrLines)
}
finally {
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $secure.Dispose()
    $userId = $null
    if ($null -ne $process) { $process.Dispose() }
    [GC]::Collect()
}
