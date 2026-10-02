# Test-only stand-in for the primitive child. Behaviour is selected through
# its environment (XB_TEST_CHILD_MODE) and it records what it received into
# XB_TEST_CHILD_TRACE. Synthetic data only.
[CmdletBinding()]
param(
    [string]$Book,
    [switch]$EnableProductionAdapter
)
$ErrorActionPreference = "Stop"
$line = [Console]::In.ReadLine()
$mode = [Environment]::GetEnvironmentVariable("XB_TEST_CHILD_MODE", "Process")
$tracePath = [Environment]::GetEnvironmentVariable("XB_TEST_CHILD_TRACE", "Process")
$passwordVariable = [Environment]::GetEnvironmentVariable("XB_AC2_PASSWORD_ENV_VAR", "Process")
$password = $(if ([string]::IsNullOrWhiteSpace($passwordVariable)) { $null } else { [Environment]::GetEnvironmentVariable($passwordVariable, "Process") })
$decodedName = $null
try { $decodedName = ($line | ConvertFrom-Json).name } catch { $decodedName = $null }
if (-not [string]::IsNullOrWhiteSpace($tracePath)) {
    $trace = [ordered]@{
        pid = $PID
        interpreter = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
        command_line = [Environment]::CommandLine
        book = $Book
        production_adapter = [bool]$EnableProductionAdapter
        request_line = $line
        request_line_ascii = (-not ($line.ToCharArray() | Where-Object { [int]$_ -gt 127 }))
        decoded_name = $decodedName
        password_present = (-not [string]::IsNullOrEmpty($password))
        password_on_command_line = ((-not [string]::IsNullOrEmpty($password)) -and [Environment]::CommandLine.Contains($password))
        worker_fault_present = (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("XB_WORKER_FAULT", "Process")))
    }
    [IO.File]::WriteAllText($tracePath, ($trace | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
}
if ($mode -eq "sleep") { Start-Sleep -Seconds 120; exit 0 }
if ($mode -eq "garbage") { [Console]::Out.WriteLine("{not json"); exit 0 }
if ($mode -eq "silent_exit") { exit 97 }
if ($mode -eq "two_lines") {
    [Console]::Out.WriteLine([Environment]::GetEnvironmentVariable("XB_TEST_CHILD_OUTPUT", "Process"))
    [Console]::Out.WriteLine([Environment]::GetEnvironmentVariable("XB_TEST_CHILD_OUTPUT", "Process"))
    exit 0
}
[Console]::Out.WriteLine([Environment]::GetEnvironmentVariable("XB_TEST_CHILD_OUTPUT", "Process"))
exit 0
