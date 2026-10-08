# Energy@Grid bounded Claude supervisor (#226 G3).
#
# Windows Task Scheduler -> this supervisor -> pinned standalone Claude Code CLI
# -> egcore.cmd (exact eleven commands) -> this supervisor's -CoreDispatch mode
# -> installed launcher.ps1 -> deterministic Python core.
#
# Claude is a bounded orchestrator only. Business success is never inferred from
# Claude: the supervisor runs its own final `status` through the core and maps
# that deterministic document to the exit code. Claude exit 0 alone is never
# success.
#
# Run mode (the Scheduled Task action), in order:
#   1. load the private settings document (strict shape);
#   2. check the SHA-256 of the prompt, the installed Claude settings, the MCP
#      config, egcore.cmd and the launcher installation manifest;
#   3. check the Claude executable SHA-256 and its exact --version output;
#   4. decrypt the DPAPI CurrentUser token (model-only setup token);
#   5. create the run ID;
#   6. run `status` through the core as a pre-check;
#   7. start Claude inside a kill-on-close Windows Job Object with a hard
#      timeout, the reviewed prompt on stdin and the token only in Claude's
#      process environment;
#   8. validate Claude's stream-json transcript: every tool call must be Bash
#      with one of the eleven exact command strings, no permission denials,
#      one success result within the turn and budget limits;
#   9. run the final `status` through the core;
#  10. write a privacy-minimal receipt and, on a non-zero exit, send the
#      existing energygrid.alert.v1 alert.
#
# CoreDispatch mode (reached only through egcore.cmd): accept exactly one of the
# eleven reviewed command forms, then start the installed launcher with fixed
# private parameters from the settings document. CLAUDE_CODE_OAUTH_TOKEN and
# every ANTHROPIC_* variable are removed from the core's environment.
#
# Exit codes (separate from the core's 0/10/20/64 and the launcher's 70-73).
# When several apply the most severe wins: 88 > 87 > 89 > 85 > 86 > 84 > 83 > 81 > 82.
#   0  NO_WORK or COMPLETED          84 Drive upload uncertain
#   81 HOLD / action required        85 Drive conflict or Drive HOLD
#   82 source failure, retryable     86 email uncertain
#   83 source failure, action req.   87 Claude / runtime failure (incl. timeout)
#   88 supervisor preflight failed   89 final status invalid or incomplete
#
# Nothing private is committed here: no credential, token, private path, host or
# principal identity appears in this file.

[CmdletBinding(DefaultParameterSetName = 'Run')]
param(
    [Parameter(Mandatory)][AllowEmptyString()][string]$SettingsPath,
    [Parameter(ParameterSetName = 'Dispatch', Mandatory)][switch]$CoreDispatch,
    [Parameter(ParameterSetName = 'Dispatch')][AllowEmptyString()][string]$CoreArg1 = '',
    [Parameter(ParameterSetName = 'Dispatch')][AllowEmptyString()][string]$CoreArg2 = '',
    [Parameter(ParameterSetName = 'Dispatch')][AllowEmptyString()][string]$CoreArg3 = '',
    [Parameter(ParameterSetName = 'Dispatch')][AllowEmptyString()][string]$CoreArg4 = '',
    [Parameter(ParameterSetName = 'Dispatch')][AllowEmptyString()][string]$CoreArgOverflow = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:EgSettingsSchema = 'energygrid.claude_supervisor_settings.v1'
$script:EgReceiptSchema = 'energygrid.supervisor_receipt.v1'
$script:EgStatusSchema = 'energygrid.core.status.v3'
$script:EgAllowedCoreLines = @(
    'egcore.cmd plan',
    'egcore.cmd status',
    'egcore.cmd acquire',
    'egcore.cmd drive-intent --stream EB_BILL',
    'egcore.cmd drive-intent --stream TENANT_BILL',
    'egcore.cmd drive-reconcile --stream EB_BILL',
    'egcore.cmd drive-reconcile --stream TENANT_BILL',
    'egcore.cmd drive-upload --stream EB_BILL',
    'egcore.cmd drive-upload --stream TENANT_BILL',
    'egcore.cmd deliver --stream EB_BILL',
    'egcore.cmd deliver --stream TENANT_BILL'
)
$script:EgExit = [ordered]@{
    Ok = 0; Hold = 81; SourceRetryable = 82; SourceFailure = 83; DriveUncertain = 84
    DriveConflict = 85; EmailUncertain = 86; ClaudeFailure = 87; PreflightFailed = 88; StatusInvalid = 89
}
$script:EgSeverity = @(88, 87, 89, 85, 86, 84, 83, 81, 82, 0)
$script:EgOutcomeExit = @{
    'NO_WORK' = 0; 'COMPLETED' = 0; 'HOLD' = 81; 'SOURCE_FAILURE_RETRYABLE' = 82; 'SOURCE_FAILURE' = 83
    'DRIVE_UNCERTAIN' = 84; 'DRIVE_CONFLICT' = 85; 'EMAIL_UNCERTAIN' = 86; 'INCOMPLETE' = 89
}
$script:EgSettingsKeys = @(
    'schema', 'claude_exe_path', 'claude_exe_sha256', 'claude_version', 'claude_model', 'token_path', 'git_bash_path',
    'work_dir', 'prompt_path', 'claude_settings_path', 'mcp_config_path', 'egcore_bin_dir',
    'expected_sha256', 'launcher', 'receipt_root', 'claude_timeout_seconds', 'max_turns', 'max_budget_usd', 'alert'
)
$script:EgHashKeys = @('prompt', 'claude_settings', 'mcp_config', 'egcore_cmd', 'launcher_manifest')
$script:EgLauncherKeys = @(
    'path', 'manifest_path', 'config_path', 'python_exe', 'checkout_root', 'credential_path',
    'browser_cache_path', 'expected_branch', 'authorised_launcher_root_write_sid', 'log_root'
)

function Get-EgPropertyNames {
    param($Object)
    return @($Object.PSObject.Properties | ForEach-Object { $_.Name })
}

function Test-EgExactKeys {
    param($Object, [string[]]$Keys)
    if ($null -eq $Object -or $Object -isnot [System.Management.Automation.PSCustomObject]) { return $false }
    $names = @(Get-EgPropertyNames -Object $Object | Sort-Object)
    $expected = @($Keys | Sort-Object)
    return (($names -join "`n") -ceq ($expected -join "`n"))
}

function Read-EgSettings {
    param([string]$Path)
    if (-not [System.IO.Path]::IsPathRooted($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'EG_SUPERVISOR_SETTINGS_UNAVAILABLE'
    }
    $settings = (Get-Content -LiteralPath $Path -Raw -Encoding UTF8) | ConvertFrom-Json
    if (-not (Test-EgExactKeys -Object $settings -Keys $script:EgSettingsKeys)) { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
    if ($settings.schema -cne $script:EgSettingsSchema) { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
    if (-not (Test-EgExactKeys -Object $settings.expected_sha256 -Keys $script:EgHashKeys)) { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
    if (-not (Test-EgExactKeys -Object $settings.launcher -Keys $script:EgLauncherKeys)) { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
    foreach ($name in @('claude_exe_path', 'token_path', 'git_bash_path', 'work_dir', 'prompt_path', 'claude_settings_path', 'mcp_config_path', 'egcore_bin_dir', 'receipt_root')) {
        $value = $settings.$name
        if ($value -isnot [string] -or -not [System.IO.Path]::IsPathRooted($value)) { throw 'EG_SUPERVISOR_SETTINGS_PATH' }
    }
    if ($settings.claude_exe_path -match '\\WindowsApps\\' -or $settings.claude_exe_path -match '\\\.local\\bin\\') {
        throw 'EG_SUPERVISOR_CLAUDE_NOT_PINNED'
    }
    foreach ($name in $script:EgHashKeys) {
        if ([string]$settings.expected_sha256.$name -cnotmatch '\A[0-9a-f]{64}\z') { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
    }
    if ([string]$settings.claude_exe_sha256 -cnotmatch '\A[0-9a-f]{64}\z') { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
    if ([string]$settings.claude_model -cnotmatch '\A[a-z0-9][a-z0-9.-]{2,63}\z') { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
    $timeout = $settings.claude_timeout_seconds
    if ($timeout -isnot [int] -or $timeout -lt 5 -or $timeout -gt 1200) { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
    if ($settings.max_turns -isnot [int] -or $settings.max_turns -lt 1 -or $settings.max_turns -gt 40) { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
    if ([string]$settings.max_budget_usd -cnotmatch '\A(0\.[0-9]{2}|1\.00)\z') { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
    if ($null -ne $settings.alert) {
        if (-not (Test-EgExactKeys -Object $settings.alert -Keys @('url', 'auth_header_name', 'auth_token_env'))) { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
        $uri = [System.Uri]([string]$settings.alert.url)
        if ($uri.Scheme -cne 'http' -or @('127.0.0.1', 'localhost', '[::1]') -notcontains $uri.Host) { throw 'EG_SUPERVISOR_SETTINGS_SHAPE' }
    }
    return $settings
}

function Get-EgSha256 {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function ConvertTo-EgArgumentString {
    # Windows CommandLineToArgvW quoting for a fixed argument list.
    param([string[]]$Argument)
    $parts = foreach ($value in $Argument) {
        if ($value.Length -gt 0 -and $value -notmatch '[\s"]') { $value; continue }
        $builder = New-Object System.Text.StringBuilder
        [void]$builder.Append('"')
        $slashes = 0
        foreach ($char in $value.ToCharArray()) {
            if ($char -eq '\') { $slashes++; continue }
            if ($char -eq '"') { [void]$builder.Append(('\' * ($slashes * 2 + 1))); $slashes = 0; [void]$builder.Append('"'); continue }
            if ($slashes -gt 0) { [void]$builder.Append(('\' * $slashes)); $slashes = 0 }
            [void]$builder.Append($char)
        }
        if ($slashes -gt 0) { [void]$builder.Append(('\' * ($slashes * 2))) }
        [void]$builder.Append('"')
        $builder.ToString()
    }
    return ($parts -join ' ')
}

function Remove-EgClaudeEnvironment {
    param([System.Collections.Specialized.StringDictionary]$Environment)
    foreach ($name in @($Environment.Keys)) {
        if ($name -ieq 'CLAUDE_CODE_OAUTH_TOKEN' -or $name -like 'ANTHROPIC_*' -or $name -ieq 'EGCORE_SUPERVISOR_SETTINGS') {
            $Environment.Remove($name)
        }
    }
}

function Invoke-EgProcess {
    param(
        [string]$FileName,
        [string[]]$Argument,
        [hashtable]$Set = @{},
        [switch]$ScrubClaude,
        [string]$StdIn = $null,
        [string]$WorkingDirectory = $null,
        [int]$TimeoutSeconds = 0,
        [switch]$UseJob
    )
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FileName
    $startInfo.Arguments = ConvertTo-EgArgumentString -Argument $Argument
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    if ($WorkingDirectory) { $startInfo.WorkingDirectory = $WorkingDirectory }
    if ($ScrubClaude) { Remove-EgClaudeEnvironment -Environment $startInfo.EnvironmentVariables }
    foreach ($key in $Set.Keys) { $startInfo.EnvironmentVariables[$key] = [string]$Set[$key] }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $job = [IntPtr]::Zero
    try {
        if ($UseJob) { $job = [EnergyGridSupervisorJob]::Create() }
        [void]$process.Start()
        if ($UseJob) { [void][EnergyGridSupervisorJob]::Assign($job, $process.Handle) }
        $outTask = $process.StandardOutput.ReadToEndAsync()
        $errTask = $process.StandardError.ReadToEndAsync()
        if ($null -ne $StdIn) { $process.StandardInput.Write($StdIn) }
        $process.StandardInput.Close()
        $timedOut = $false
        if ($TimeoutSeconds -gt 0) {
            if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
                $timedOut = $true
                if ($UseJob) { [void][EnergyGridSupervisorJob]::Terminate($job) } else { $process.Kill() }
                [void]$process.WaitForExit(30000)
            }
        }
        $process.WaitForExit()
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            StdOut   = [string]$outTask.GetAwaiter().GetResult()
            StdErr   = [string]$errTask.GetAwaiter().GetResult()
            TimedOut = $timedOut
        }
    }
    finally {
        if ($job -ne [IntPtr]::Zero) { [EnergyGridSupervisorJob]::Close($job) }
        $process.Dispose()
    }
}

function Get-EgCoreArgumentVector {
    param([string]$Line)
    if ($script:EgAllowedCoreLines -cnotcontains $Line) { return $null }
    $parts = $Line.Split(' ')
    $stream = 'NONE'
    if ($parts.Count -eq 4) { $stream = $parts[3] }
    return [pscustomobject]@{ Command = $parts[1]; Stream = $stream }
}

function Invoke-EgCore {
    param($Settings, [string]$Line, [string]$RunId)
    $vector = Get-EgCoreArgumentVector -Line $Line
    if ($null -eq $vector) { throw 'EG_CORE_COMMAND_NOT_ALLOWED' }
    $launcher = $Settings.launcher
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $argumentList = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', [string]$launcher.path,
        '-ConfigPath', [string]$launcher.config_path, '-PythonExe', [string]$launcher.python_exe,
        '-CheckoutRoot', [string]$launcher.checkout_root, '-CredentialPath', [string]$launcher.credential_path,
        '-BrowserCachePath', [string]$launcher.browser_cache_path, '-ExpectedBranch', [string]$launcher.expected_branch,
        '-AuthorisedLauncherRootWriteSid', ((@($launcher.authorised_launcher_root_write_sid) | ForEach-Object { [string]$_ }) -join ','),
        '-Command', $vector.Command, '-Stream', $vector.Stream, '-LogRoot', [string]$launcher.log_root, '-RunId', $RunId
    )
    return Invoke-EgProcess -FileName $powershell -Argument $argumentList -ScrubClaude
}

function Read-EgJsonLine {
    param([string]$Text)
    $lines = @($Text -split "`r?`n" | Where-Object { $_.Trim().Length -gt 0 })
    if ($lines.Count -ne 1) { return $null }
    try { return ($lines[0] | ConvertFrom-Json) } catch { return $null }
}

function Test-EgStatusDocument {
    param($Status)
    if ($null -eq $Status) { return $false }
    $names = @(Get-EgPropertyNames -Object $Status)
    foreach ($name in @('schema', 'run', 'streams', 'terminal', 'uncertainty_outstanding', 'business_outcome')) {
        if ($names -cnotcontains $name) { return $false }
    }
    if ($Status.schema -cne $script:EgStatusSchema) { return $false }
    if ($Status.terminal -isnot [bool] -or $Status.uncertainty_outstanding -isnot [bool]) { return $false }
    return $script:EgOutcomeExit.ContainsKey([string]$Status.business_outcome)
}

function Get-EgStatusExitCode {
    param($Status)
    if (-not (Test-EgStatusDocument -Status $Status)) { return $script:EgExit.StatusInvalid }
    # A non-terminal status is incomplete whatever its business outcome says, so an
    # unfinished run can never collapse onto the ordinary HOLD code.
    if (-not $Status.terminal) { return $script:EgExit.StatusInvalid }
    $code = [int]$script:EgOutcomeExit[[string]$Status.business_outcome]
    if ($code -eq 0 -and $Status.uncertainty_outstanding) { return $script:EgExit.StatusInvalid }
    return $code
}

function Test-EgClaudeTranscript {
    # Returns '' when Claude's part is acceptable, otherwise a fixed reason.
    param([string]$StdOut, $Settings)
    $lines = @($StdOut -split "`r?`n" | Where-Object { $_.Trim().Length -gt 0 })
    if ($lines.Count -eq 0) { return 'EG_CLAUDE_NO_OUTPUT' }
    $results = @()
    foreach ($line in $lines) {
        try { $event = $line | ConvertFrom-Json } catch { return 'EG_CLAUDE_OUTPUT_MALFORMED' }
        if ($null -eq $event -or $event -isnot [System.Management.Automation.PSCustomObject]) { return 'EG_CLAUDE_OUTPUT_MALFORMED' }
        $names = @(Get-EgPropertyNames -Object $event)
        if ($names -cnotcontains 'type') { return 'EG_CLAUDE_OUTPUT_MALFORMED' }
        if ($event.type -ceq 'system' -and $names -ccontains 'tools') {
            $tools = @($event.tools | ForEach-Object { [string]$_ })
            if (@($tools | Where-Object { $_ -cne 'Bash' }).Count -gt 0) { return 'EG_CLAUDE_TOOL_SURFACE' }
            if ($names -ccontains 'mcp_servers' -and @($event.mcp_servers).Count -gt 0) { return 'EG_CLAUDE_TOOL_SURFACE' }
        }
        if ($event.type -ceq 'assistant' -and $names -ccontains 'message') {
            foreach ($block in @($event.message.content)) {
                if ($null -eq $block -or @(Get-EgPropertyNames -Object $block) -cnotcontains 'type') { continue }
                if ($block.type -cne 'tool_use') { continue }
                if ($block.name -cne 'Bash') { return 'EG_CLAUDE_TOOL_NOT_ALLOWED' }
                $command = $null
                if (@(Get-EgPropertyNames -Object $block.input) -ccontains 'command') { $command = $block.input.command }
                if ($command -isnot [string] -or $script:EgAllowedCoreLines -cnotcontains $command) {
                    return 'EG_CLAUDE_COMMAND_NOT_ALLOWED'
                }
            }
        }
        if ($event.type -ceq 'result') { $results += $event }
    }
    if ($results.Count -ne 1) { return 'EG_CLAUDE_RESULT_MISSING' }
    $result = $results[0]
    $resultNames = @(Get-EgPropertyNames -Object $result)
    foreach ($name in @('subtype', 'is_error', 'num_turns', 'total_cost_usd', 'permission_denials')) {
        if ($resultNames -cnotcontains $name) { return 'EG_CLAUDE_RESULT_MALFORMED' }
    }
    if ($result.subtype -cne 'success' -or $result.is_error -isnot [bool] -or $result.is_error) { return 'EG_CLAUDE_RESULT_ERROR' }
    if (@($result.permission_denials).Count -ne 0) { return 'EG_CLAUDE_PERMISSION_DENIED' }
    if ([int]$result.num_turns -gt [int]$Settings.max_turns) { return 'EG_CLAUDE_TURN_LIMIT' }
    if ([double]$result.total_cost_usd -gt [double]$Settings.max_budget_usd) { return 'EG_CLAUDE_BUDGET_LIMIT' }
    return ''
}

function Select-EgExitCode {
    param([int[]]$Codes)
    foreach ($candidate in $script:EgSeverity) {
        if ($Codes -contains $candidate) { return $candidate }
    }
    return $script:EgExit.StatusInvalid
}

function Write-EgReceipt {
    param($Settings, [string]$RunId, [string]$StartedAt, [int]$ExitCode, [string]$ClaudeReason, $Status)
    try {
        $root = [string]$Settings.receipt_root
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { [void](New-Item -ItemType Directory -Path $root) }
        $statusSummary = $null
        if (Test-EgStatusDocument -Status $Status) {
            $statusSummary = [ordered]@{
                terminal = [bool]$Status.terminal; uncertainty_outstanding = [bool]$Status.uncertainty_outstanding
                business_outcome = [string]$Status.business_outcome
            }
        }
        $reason = $ClaudeReason
        if ([string]::IsNullOrEmpty($reason)) { $reason = $null }
        $document = [ordered]@{
            schema = $script:EgReceiptSchema; run_id = $RunId; started_at_utc = $StartedAt
            finished_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'); exit_code = $ExitCode
            claude_reason = $reason; status = $statusSummary
        }
        $path = Join-Path $root ('receipt-' + $RunId + '.json')
        $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
        try {
            $bytes = [System.Text.Encoding]::ASCII.GetBytes(($document | ConvertTo-Json -Compress -Depth 4))
            $stream.Write($bytes, 0, $bytes.Length)
        }
        finally { $stream.Dispose() }
    }
    catch { }
}

function Send-EgAlert {
    param($Settings, [string]$RunId, [int]$ExitCode)
    if ($null -eq $Settings.alert) { return }
    try {
        $token = [Environment]::GetEnvironmentVariable([string]$Settings.alert.auth_token_env)
        if ([string]::IsNullOrEmpty($token)) { return }
        $payload = [ordered]@{
            schema = 'energygrid.alert.v1'; event = 'energygrid_run_failed'
            timestamp = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'); run_id = $RunId; stage = 'run'
            status = 'ACTION_REQUIRED'; support_ref = ('EG_SUPERVISOR_EXIT_' + $ExitCode); exit_code = $ExitCode
            counts = [ordered]@{ inventory = 0; downloaded = 0; present = 0; failure = 0 }; attention_required = $true
        }
        $headers = @{ ([string]$Settings.alert.auth_header_name) = $token }
        [void](Invoke-WebRequest -UseBasicParsing -Method Post -Uri ([string]$Settings.alert.url) -Headers $headers `
            -ContentType 'application/json' -Body ($payload | ConvertTo-Json -Compress -Depth 3) -TimeoutSec 5 -MaximumRedirection 0)
    }
    catch { }
}

$script:EgJobSource = @'
using System;
using System.Runtime.InteropServices;
public static class EnergyGridSupervisorJob {
    [StructLayout(LayoutKind.Sequential)]
    struct BASIC { public long PerProcessUserTimeLimit; public long PerJobUserTimeLimit; public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize; public UIntPtr MaximumWorkingSetSize; public uint ActiveProcessLimit;
        public UIntPtr Affinity; public uint PriorityClass; public uint SchedulingClass; }
    [StructLayout(LayoutKind.Sequential)]
    struct IO { public ulong a; public ulong b; public ulong c; public ulong d; public ulong e; public ulong f; }
    [StructLayout(LayoutKind.Sequential)]
    struct EXTENDED { public BASIC Basic; public IO Io; public UIntPtr ProcessMemoryLimit; public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed; public UIntPtr PeakJobMemoryUsed; }
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr CreateJobObject(IntPtr a, string n);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetInformationJobObject(IntPtr j, int c, ref EXTENDED i, uint l);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool TerminateJobObject(IntPtr j, uint c);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr h);
    public static IntPtr Create() {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new InvalidOperationException("job");
        EXTENDED info = new EXTENDED();
        info.Basic.LimitFlags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (!SetInformationJobObject(job, 9, ref info, (uint)Marshal.SizeOf(typeof(EXTENDED)))) { CloseHandle(job); throw new InvalidOperationException("job"); }
        return job;
    }
    public static bool Assign(IntPtr job, IntPtr process) { return AssignProcessToJobObject(job, process); }
    public static bool Terminate(IntPtr job) { return TerminateJobObject(job, 87); }
    public static void Close(IntPtr job) { CloseHandle(job); }
}
'@

# --------------------------------------------------------------------------------------
# CoreDispatch mode (egcore.cmd)
# --------------------------------------------------------------------------------------
if ($CoreDispatch) {
    $refusal = '{"command":null,"mutated":false,"outcome":"REFUSED","schema":"energygrid.core.result.v3","stream":null,"support_ref":"EG_CORE_ARGUMENTS_INVALID"}'
    $parts = @($CoreArg1, $CoreArg2, $CoreArg3, $CoreArg4) | Where-Object { $_.Length -gt 0 }
    $line = 'egcore.cmd ' + ($parts -join ' ')
    $runId = [string]$env:ENERGYGRID_RUN_ID
    if ($CoreArgOverflow.Length -gt 0 -or $script:EgAllowedCoreLines -cnotcontains $line -or
        $runId -cnotmatch '\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\z') {
        [Console]::Out.WriteLine($refusal)
        exit 64
    }
    try { $settings = Read-EgSettings -Path $SettingsPath }
    catch { [Console]::Out.WriteLine($refusal); exit 64 }
    $core = Invoke-EgCore -Settings $settings -Line $line -RunId $runId
    if ($core.StdOut.Length -gt 0) { [Console]::Out.Write($core.StdOut) }
    if ($core.StdErr.Length -gt 0) { [Console]::Error.Write($core.StdErr) }
    exit $core.ExitCode
}

# --------------------------------------------------------------------------------------
# Run mode (Scheduled Task action)
# --------------------------------------------------------------------------------------
$startedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
$runId = [guid]::NewGuid().ToString('D').ToLowerInvariant()
$settings = $null
$status = $null
$claudeReason = ''
$codes = @()

try {
    $settings = Read-EgSettings -Path $SettingsPath
    $hashTargets = [ordered]@{
        prompt = $settings.prompt_path; claude_settings = $settings.claude_settings_path
        mcp_config = $settings.mcp_config_path; egcore_cmd = (Join-Path $settings.egcore_bin_dir 'egcore.cmd')
        launcher_manifest = $settings.launcher.manifest_path
    }
    foreach ($key in $hashTargets.Keys) {
        if ((Get-EgSha256 -Path ([string]$hashTargets[$key])) -cne [string]$settings.expected_sha256.$key) { throw 'EG_SUPERVISOR_HASH_MISMATCH' }
    }
    if (@(Get-ChildItem -LiteralPath $settings.egcore_bin_dir -Force).Count -ne 1) { throw 'EG_SUPERVISOR_BIN_NOT_EXCLUSIVE' }
    if (Test-Path -LiteralPath (Join-Path $settings.work_dir 'CLAUDE.md')) { throw 'EG_SUPERVISOR_WORKDIR_INSTRUCTIONS' }
    if ((Get-EgSha256 -Path $settings.claude_exe_path) -cne [string]$settings.claude_exe_sha256) { throw 'EG_SUPERVISOR_CLAUDE_HASH' }
    $version = Invoke-EgProcess -FileName $settings.claude_exe_path -Argument @('--version') -ScrubClaude -TimeoutSeconds 60
    if ($version.ExitCode -ne 0 -or $version.StdOut.Trim() -cne [string]$settings.claude_version) { throw 'EG_SUPERVISOR_CLAUDE_VERSION' }
    if (-not (Test-Path -LiteralPath $settings.token_path -PathType Leaf)) { throw 'EG_SUPERVISOR_TOKEN_UNAVAILABLE' }
    $secure = Import-Clixml -LiteralPath $settings.token_path
    if ($secure -isnot [System.Security.SecureString] -or $secure.Length -eq 0) { throw 'EG_SUPERVISOR_TOKEN_UNAVAILABLE' }
    $pre = Invoke-EgCore -Settings $settings -Line 'egcore.cmd status' -RunId $runId
    if ($pre.ExitCode -ne 0 -or -not (Test-EgStatusDocument -Status (Read-EgJsonLine -Text $pre.StdOut))) { throw 'EG_SUPERVISOR_PRE_STATUS' }
}
catch {
    $codes += $script:EgExit.PreflightFailed
    $claudeReason = 'EG_SUPERVISOR_PREFLIGHT'
}

if ($codes.Count -eq 0) {
    Add-Type -TypeDefinition $script:EgJobSource -ErrorAction Stop
    $plain = ''
    $pointer = [IntPtr]::Zero
    try {
        $pointer = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        $claudeArguments = @(
            '-p', '--output-format', 'stream-json', '--verbose', '--model', [string]$settings.claude_model,
            '--permission-mode', 'dontAsk', '--tools', 'Bash', '--allowedTools'
        ) + @($script:EgAllowedCoreLines | ForEach-Object { 'Bash(' + $_ + ')' }) + @(
            '--disallowedTools', 'mcp__*', '--strict-mcp-config', '--mcp-config', [string]$settings.mcp_config_path,
            '--setting-sources', 'project', '--settings', [string]$settings.claude_settings_path,
            '--no-session-persistence', '--max-turns', [string]$settings.max_turns, '--max-budget-usd', [string]$settings.max_budget_usd
        )
        $environment = @{
            'CLAUDE_CODE_OAUTH_TOKEN'     = $plain
            'DISABLE_AUTOUPDATER'         = '1'
            'ENERGYGRID_RUN_ID'           = $runId
            'EGCORE_SUPERVISOR_SETTINGS'  = $SettingsPath
            'CLAUDE_CODE_GIT_BASH_PATH'   = [string]$settings.git_bash_path
            'PATH'                        = ([string]$settings.egcore_bin_dir + ';' + (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot + ';' + (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0'))
        }
        $prompt = Get-Content -LiteralPath $settings.prompt_path -Raw -Encoding UTF8
        $claude = Invoke-EgProcess -FileName $settings.claude_exe_path -Argument $claudeArguments -Set $environment `
            -StdIn $prompt -WorkingDirectory $settings.work_dir -TimeoutSeconds ([int]$settings.claude_timeout_seconds) -UseJob
        if ($claude.TimedOut) { $claudeReason = 'EG_CLAUDE_TIMEOUT' }
        elseif ($claude.ExitCode -ne 0) { $claudeReason = 'EG_CLAUDE_EXIT_NONZERO' }
        else { $claudeReason = Test-EgClaudeTranscript -StdOut $claude.StdOut -Settings $settings }
    }
    catch { $claudeReason = 'EG_CLAUDE_RUNTIME_FAILURE' }
    finally {
        $plain = ''
        if ($pointer -ne [IntPtr]::Zero) { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    }
    if (-not [string]::IsNullOrEmpty($claudeReason)) { $codes += $script:EgExit.ClaudeFailure }
    try {
        $final = Invoke-EgCore -Settings $settings -Line 'egcore.cmd status' -RunId $runId
        if ($final.ExitCode -eq 0) { $status = Read-EgJsonLine -Text $final.StdOut }
    }
    catch { $status = $null }
    $codes += (Get-EgStatusExitCode -Status $status)
}

$exitCode = Select-EgExitCode -Codes $codes
if ($null -ne $settings) {
    Write-EgReceipt -Settings $settings -RunId $runId -StartedAt $startedAt -ExitCode $exitCode -ClaudeReason $claudeReason -Status $status
    if ($exitCode -ne 0) { Send-EgAlert -Settings $settings -RunId $runId -ExitCode $exitCode }
}
exit $exitCode
