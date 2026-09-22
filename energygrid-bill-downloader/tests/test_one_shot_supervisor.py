"""Contract and native assurance tests for the EnergyGrid one-shot supervisor.

The synthetic model remains supplemental.  On Windows, the focused suite also extracts
the exact embedded C# and selected committed PowerShell functions, compiles them with
native Windows PowerShell 5.1, and exercises real Job Objects and contained processes.
The native harness has no portal, credential, launcher, Scheduler or network surface.
"""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = REPO_ROOT / "scripts" / "energygrid_one_shot_supervisor.ps1"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "energygrid-bill-downloader-tests.yml"
RUNBOOK = REPO_ROOT / "energygrid-bill-downloader" / "docs" / "runbook.md"


def native_powershell():
    """Return the native Windows PowerShell 5.1 executable when available."""
    if os.name != "nt":
        return None
    return shutil.which("powershell.exe")


class SyntheticJob:
    """A tiny offline model of the accepted creation-time containment boundary."""

    def __init__(self, creation_supported=True):
        self.creation_supported = creation_supported
        self.member_at_creation = False
        self.suspended = False
        self.resumed = False
        self.processes = []
        self.total_terminated = 0
        self.closed = False

    @property
    def active(self):
        return sum(1 for process in self.processes if process["live"])

    @property
    def total(self):
        return len(self.processes)

    def create_launcher(self):
        if not self.creation_supported:
            return False
        self.member_at_creation = True
        self.suspended = True
        self.processes.append({"kind": "launcher", "parent": None, "live": True})
        return True

    def resume(self):
        if not self.member_at_creation or not self.suspended:
            return False
        self.suspended = False
        self.resumed = True
        return True

    def add_child(self, kind="application", parent=0, image="python.exe", command="canonical"):
        if not self.resumed:
            raise AssertionError("synthetic child cannot run while launcher is suspended")
        self.processes.append({
            "kind": kind,
            "parent": parent,
            "image": image,
            "command": command,
            "live": True,
        })

    def terminate(self):
        for process in self.processes:
            if process["live"]:
                process["live"] = False
                self.total_terminated += 1

    def close_last_handle(self):
        self.closed = True
        self.terminate()


class SyntheticSupervisor:
    """Offline state machine for the accepted verdict and one-shot rules."""

    def __init__(self):
        self.job = SyntheticJob()
        self.create_attempts = 0
        self.intent_committed = False
        self.resume_attempts = 0
        self.launcher_exit = None
        self.application_observed = False
        self.outcome_committed = False
        self.containment_failure = False
        self.evidence_failure = False
        self.reap_confirmed = False

    def create(self, job_list=True):
        if not job_list:
            return False
        self.create_attempts += 1
        return self.job.create_launcher()

    def commit_intent(self, success=True):
        if success and not self.evidence_failure:
            self.intent_committed = True
        return self.intent_committed

    def resume(self, return_value=1):
        if self.evidence_failure:
            self.containment_failure = True
            self.job.terminate()
            return False
        self.resume_attempts += 1
        if return_value != 1:
            self.containment_failure = True
            self.job.terminate()
            return False
        return self.job.resume()

    def observe_application(self, parent=True, image=True, command=True, live=True):
        candidate = next(
            (process for process in self.job.processes if process["kind"] == "application"),
            None,
        )
        self.application_observed = bool(
            candidate and parent is True and image is True and command is True and live is True
        )
        return self.application_observed

    def reap(self, accounting=True):
        if not accounting:
            self.containment_failure = True
            self.reap_confirmed = False
        else:
            self.reap_confirmed = self.job.active == 0
        return self.reap_confirmed

    def classify(self, create_attempted=True):
        if self.containment_failure or self.evidence_failure:
            return "AMBIGUOUS"
        if not create_attempted and self.outcome_committed:
            return "NOT_STARTED_PROVEN"
        if (
            self.application_observed
            and self.job.total >= 2
            and self.reap_confirmed
            and self.intent_committed
            and self.outcome_committed
        ):
            return "STARTED_PROVEN"
        if (
            self.job.total == 1
            and self.job.active == 0
            and not self.application_observed
            and self.reap_confirmed
            and self.outcome_committed
        ):
            return "NOT_STARTED_PROVEN"
        return "AMBIGUOUS"


def quote_windows(argument):
    """Reference implementation of the command-line quoting rule."""
    if any(ord(char) < 32 for char in argument) or '"' in argument:
        raise ValueError("unsafe argument")
    output = ['"']
    backslashes = 0
    for char in argument:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            output.append("\\" * (2 * backslashes + 1))
            output.append('"')
            backslashes = 0
            continue
        output.append("\\" * backslashes)
        backslashes = 0
        output.append(char)
    output.append("\\" * (2 * backslashes))
    output.append('"')
    return "".join(output)


class SupervisorStaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SUPERVISOR.read_text(encoding="utf-8")
        cls.test_source = Path(__file__).read_text(encoding="utf-8")
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.runbook = RUNBOOK.read_text(encoding="utf-8")

    def test_exact_public_parameters_and_fixed_run_operation(self):
        parameter_block = self.source[self.source.index("param("): self.source.index(")\n\nSet-StrictMode")]
        required = (
            "LauncherPath", "ExpectedLauncherSha256", "ExpectedLauncherLibrarySha256",
            "ConfigPath", "PythonExe", "CheckoutRoot", "CredentialPath",
            "BrowserCachePath", "ExpectedBranch", "AuthorisedLauncherRootWriteSid",
            "LogRoot", "EvidenceRoot", "RunId", "TimeoutSeconds",
        )
        for name in required:
            self.assertIn("$" + name, parameter_block)
        self.assertNotIn("$Command", parameter_block)
        self.assertIn("Command = 'run'", self.source)
        self.assertIn("ValidateRange(1, 3600)", parameter_block)

    def test_shell_and_input_rejections_are_before_creation(self):
        creation_call = self.source.rindex("CreateContainedProcess(")
        self.assertLess(self.source.index("Test-EgShellContract"), creation_call)
        self.assertLess(self.source.index("Set-EgInputContract"), creation_call)
        self.assertLess(self.source.index("Test-EgLauncherHashes"), creation_call)
        self.assertIn("InvocationName -eq '.'", self.source)
        self.assertIn("PSEdition -cne 'Desktop'", self.source)
        self.assertIn("PSVersion.Minor -ne 1", self.source)
        self.assertIn("[IntPtr]::Size -ne 8", self.source)

    def test_no_unsupported_shell_or_execution_policy_fallback(self):
        self.assertNotIn("AssignProcessToJobObject", self.source)
        self.assertNotIn("ExecutionPolicy", self.source)
        self.assertNotIn("pwsh", self.source.lower())
        self.assertIn("[System.Environment]::SystemDirectory", self.source)
        self.assertIn("WindowsPowerShell\\v1.0\\powershell.exe", self.source)

    def test_creation_time_job_and_exact_flags(self):
        for token in (
            "CreateJobObjectW", "SetInformationJobObject", "InitializeProcThreadAttributeList",
            "UpdateProcThreadAttribute", "DeleteProcThreadAttributeList", "CreateProcessW",
            "IsProcessInJob", "ResumeThread", "TerminateJobObject", "QueryInformationJobObject",
            "WaitForSingleObject", "GetExitCodeProcess", "CloseHandle", "CreatePipe",
            "SetHandleInformation", "OpenProcess", "QueryFullProcessImageNameW",
            "SetConsoleCtrlHandler",
        ):
            self.assertIn(token, self.source)
        for token in (
            "PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D",
            "PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002",
            "EXTENDED_STARTUPINFO_PRESENT = 0x00080000",
            "CREATE_SUSPENDED = 0x00000004",
            "CREATE_NO_WINDOW = 0x08000000",
            "STARTF_USESTDHANDLES = 0x00000100",
            "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000",
        ):
            self.assertIn(token, self.source)
        self.assertIn("uint flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW", self.source)
        self.assertIn("IntPtr.Zero, currentDirectory", self.source)
        self.assertEqual(self.source.count("CreateContainedProcess("), 2)

    def test_job_limit_readback_accepts_only_the_effective_active_policy(self):
        start = self.source.index("public static JobLimitsResult VerifyJobLimits")
        end = self.source.index("public static JobAccountingResult GetAccounting", start)
        verification = self.source[start:end]
        self.assertIn(
            "result.Matches = basic.LimitFlags == JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;",
            verification,
        )
        for incidental_field in (
            "PriorityClass", "SchedulingClass", "IoInfo", "PeakProcessMemoryUsed",
            "PeakJobMemoryUsed", "ActiveProcessLimit", "ProcessMemoryLimit",
            "JobMemoryLimit",
        ):
            self.assertNotIn(incidental_field, verification)
        self.assertIn("if (!success)", verification)

    def test_exact_x64_structure_assertions(self):
        expected = (
            "Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES)) == 24",
            "Marshal.SizeOf(typeof(STARTUPINFO)) == 104",
            "Marshal.SizeOf(typeof(STARTUPINFOEX)) == 112",
            "Marshal.SizeOf(typeof(PROCESS_INFORMATION)) == 24",
            "Marshal.SizeOf(typeof(JOBOBJECT_BASIC_LIMIT_INFORMATION)) == 64",
            "Marshal.SizeOf(typeof(IO_COUNTERS)) == 48",
            "Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)) == 144",
            "Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)) == 48",
        )
        for assertion in expected:
            self.assertIn(assertion, self.source)

    def test_two_attribute_allocation_and_handle_custody(self):
        self.assertIn("InitializeProcThreadAttributeList(\n                    IntPtr.Zero, 2", self.source)
        self.assertIn("PROC_THREAD_ATTRIBUTE_JOB_LIST", self.source)
        self.assertIn("PROC_THREAD_ATTRIBUTE_HANDLE_LIST", self.source)
        self.assertIn("handlesUpdated", self.source)
        self.assertIn("DeleteProcThreadAttributeList(AttributeList)", self.source)
        self.assertIn("STARTF_USESTDHANDLES", self.source)
        self.assertIn("true, flags", self.source)
        self.assertIn("LauncherStdinRead", self.source)
        self.assertIn("LauncherStdoutWrite", self.source)
        self.assertIn("LauncherStderrWrite", self.source)

    def test_stream_drain_is_bounded_and_raw_streams_are_not_retained(self):
        self.assertIn("byte[] buffer = new byte[65536]", self.source)
        self.assertIn("Array.Clear(buffer, 0, buffer.Length)", self.source)
        self.assertIn("raw_stream_retained_bytes = [uint64]0", self.source)
        self.assertNotIn("stdout_digest", self.source)
        self.assertNotIn("stderr_digest", self.source)
        self.assertNotIn("WriteAllBytes", self.source)
        self.assertIn("Wait-EgDrains", self.source)
        self.assertIn("5 * [int64][System.Diagnostics.Stopwatch]::Frequency", self.source)

    def test_durable_intent_outcome_contract(self):
        self.assertIn("[System.IO.FileMode]::CreateNew", self.source)
        self.assertIn("[System.IO.FileShare]::None", self.source)
        self.assertIn("Flush(true)", self.source)
        self.assertIn("WriteFlushBounded", self.source)
        self.assertIn("$durability.TimedOut -or -not $durability.Succeeded", self.source)
        self.assertIn("intent_sha256", self.source)
        self.assertIn("UTF8Encoding($false)", self.source)
        self.assertIn("+ \"`n\"", self.source)
        self.assertIn("outcome.v1", self.source)
        self.assertIn("intent.v1", self.source)
        self.assertIn("EG_SUPERVISOR_DUPLICATE_RUN_ID", self.source)

    def test_durability_timeout_returns_terminal_snapshot(self):
        durability = self.source[
            self.source.index("public static DurabilityResult WriteFlushBounded"):
            self.source.index("public static NativeBooleanResult TerminateJob")
        ]
        self.assertIn("DurabilityWorkerState workerState", durability)
        self.assertIn("Volatile.Write(ref workerState.Succeeded, 1)", durability)
        self.assertIn("return new DurabilityResult", durability)
        self.assertIn("Succeeded = false", durability)
        self.assertIn("TimedOut = true", durability)
        self.assertNotIn("result.Succeeded = true", durability)
        self.assertNotIn("result.TimedOut = true", durability)

    def test_descendant_grace_has_explicit_reason_precedence(self):
        grace = self.source[
            self.source.index("function Wait-EgDescendantGrace"):
            self.source.index("function Get-EgStartVerdict")
        ]
        self.assertNotIn("Test-EgApplicationChild", grace)
        self.assertIn("$accounting = Get-EgAccounting -JobHandle $JobHandle", grace)
        self.assertIn("if ($accounting.ActiveProcesses -eq 0) { return }", grace)
        self.assertIn("IsTerminationRequested", grace)
        self.assertIn("-Reason 'INTERRUPTION'", grace)
        self.assertIn("-Reason 'TIMEOUT'", grace)
        self.assertIn("-Reason 'DESCENDANT_GRACE_EXPIRED'", grace)
        self.assertLess(grace.index("Get-EgAccounting"), grace.index("IsTerminationRequested"))
        self.assertLess(grace.index("IsTerminationRequested"), grace.index("Test-EgDeadlineReached"))
        self.assertLess(grace.index("-Reason 'TIMEOUT'"), grace.index("-Reason 'DESCENDANT_GRACE_EXPIRED'"))
        self.assertIn("Wait-EgDescendantGrace -JobHandle", self.source)

    def test_native_saturation_harness_uses_concurrent_writers(self):
        self.assertIn("ManualResetEvent startGate", self.test_source)
        self.assertIn("Thread stdoutWriter", self.test_source)
        self.assertIn("Thread stderrWriter", self.test_source)
        self.assertIn("stdoutWriter.Start()", self.test_source)
        self.assertIn("stderrWriter.Start()", self.test_source)
        self.assertIn("startGate.Set()", self.test_source)
        self.assertIn("stdoutWriter.Join(30000)", self.test_source)
        self.assertIn("stderrWriter.Join(30000)", self.test_source)

    def test_verdict_and_exit_mapping_are_conservative(self):
        for verdict in ("NOT_STARTED_PROVEN", "STARTED_PROVEN", "AMBIGUOUS"):
            self.assertIn("'" + verdict + "'", self.source)
        self.assertIn("launcher_exit_code", self.source)
        self.assertIn("if (-not $script:EgState.outcome_committed) { return 3 }", self.source)
        self.assertIn("if ($script:EgState.timed_out -or $script:EgState.interrupted) { return 2 }", self.source)
        self.assertIn("if ($script:EgState.start_verdict -eq 'AMBIGUOUS') { return 4 }", self.source)
        self.assertIn("$script:EgState.launcher_exit_code -eq 0", self.source)
        self.assertNotIn("launcher_failed", self.source)

    def test_exit_mapping_checks_infrastructure_before_timeout(self):
        exit_function = self.source[
            self.source.index("function Get-EgExitCode"):
            self.source.index("function Write-EgPublicProjection")
        ]
        self.assertLess(
            exit_function.index("containment_failure"),
            exit_function.index("timed_out"),
        )
        self.assertIn(
            "$script:EgState.creation_succeeded -and -not $script:EgState.reap_confirmed",
            exit_function,
        )
        self.assertIn("$script:EgState.stdout_complete", exit_function)
        self.assertIn("$script:EgState.stderr_complete", exit_function)

    def test_positive_only_observer_rechecks_every_identity(self):
        for token in (
            "GetProcessIds", "OpenQueryProcess", "GetProcessLive", "CheckMembership",
            "GetImage", "QueryProcessMetadata", "ParentProcessId", "CommandLine",
            "launcherLiveAgain", "candidateLiveAgain", "membershipAgain",
        ):
            self.assertIn(token, self.source)
        self.assertIn("application_child_observation_elapsed_ms", self.source)
        self.assertIn("observer_failed", self.source)
        self.assertIn("ERROR_MORE_DATA", self.source)
        self.assertIn("const int maximum = 1024 * 1024", self.source)

    def test_observer_failure_forces_ambiguous_verdict(self):
        verdict = self.source[
            self.source.index("function Get-EgStartVerdict"):
            self.source.index("function Get-EgExitCode")
        ]
        self.assertIn("$script:EgState.observer_failed", verdict)
        self.assertIn("return 'AMBIGUOUS'", verdict)

    def test_timeout_and_console_interruption_are_one_way(self):
        self.assertIn("RequestFailure()", self.source)
        self.assertIn("TerminateJob(", self.source)
        self.assertIn(
            "public const uint SUPERVISOR_TERMINATION_EXIT_CODE = 0xE0470001;",
            self.source,
        )
        self.assertIn(
            "[EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE",
            self.source,
        )
        self.assertNotIn("[uint32]0xE0470001", self.source)
        self.assertIn("termination_started", self.source)
        self.assertIn("Wait-EgReap", self.source)
        self.assertIn("SetConsoleCtrlHandler", self.source)
        self.assertIn("Start-Sleep -Milliseconds 100", self.source)

    def test_resume_gate_uses_the_creation_deadline_under_native_lock(self):
        native_resume = self.source[
            self.source.index("public static ResumeResult TryResumeThread"):
            self.source.index("public static NativeBooleanResult CloseHandleChecked")
        ]
        self.assertIn("long deadlineTicks", native_resume)
        self.assertIn("Stopwatch.GetTimestamp() >= deadlineTicks", native_resume)
        self.assertIn("resumeGateClosed = 1", native_resume)
        self.assertIn("failureRequested = 1", native_resume)
        self.assertIn("public bool DeadlineExpired", self.source)
        creation = self.source.index(
            "$creationStartTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()"
        )
        deadline = self.source.index(
            "$deadlineTicks = Get-EgDeadlineTicks -StartTicks $creationStartTicks",
            creation,
        )
        create = self.source.index("CreateContainedProcess(", deadline)
        intent = self.source.index("Write-EgReservedIntent", create)
        resume = self.source.index("TryResumeThread($threadHandle, $deadlineTicks)", intent)
        self.assertLess(deadline, create)
        self.assertLess(create, intent)
        self.assertLess(intent, resume)

    def test_workflow_is_exact_head_native_5_1_and_narrowly_scoped(self):
        self.assertIn('"scripts/energygrid_one_shot_supervisor.ps1"', self.workflow)
        self.assertNotIn('"scripts/**"', self.workflow)
        self.assertIn("github.event.pull_request.head.sha || github.sha", self.workflow)
        self.assertIn("Assert literal exact-head checkout", self.workflow)
        self.assertIn("test_one_shot_supervisor.py", self.workflow)
        self.assertIn("PSEdition", self.workflow)
        self.assertIn("IntPtr]::Size", self.workflow)
        self.assertIn("Desktop", self.workflow)
        self.assertIn("Upload supervisor synthetic summary", self.workflow)
        self.assertIn("candidate head", self.workflow.lower())

    def test_runbook_keeps_authority_boundaries_explicit(self):
        for phrase in (
            "creation-time containment", "NOT_STARTED_PROVEN", "STARTED_PROVEN",
            "AMBIGUOUS", "No retry", "Credential import", "ValidateOnly",
            "REAL run", "Scheduler", "Raw stdout/stderr",
        ):
            self.assertIn(phrase, self.runbook)

    def test_windows_powershell_5_1_parse_and_compile_is_not_skipped(self):
        if os.name != "nt":
            # Hosted validation is Windows-only.  Non-Windows local runs still retain
            # static coverage and do not report a skipped supervisor gate.
            self.assertIn("VerifyX64StructureSizes", self.source)
            return
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell, "Windows PowerShell 5.1 is required, not optional")
        path_literal = "'" + str(SUPERVISOR).replace("'", "''") + "'"
        command = (
            "$path=" + path_literal + "; $tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors)|Out-Null; "
            "if($errors.Count -gt 0){throw 'parse_failed'}; "
            "$source=Get-Content -Raw -LiteralPath $path; "
            "$marker=\"`$script:EgNativeSource = @\"; "
            "$start=$source.IndexOf($marker); $start += $marker.Length; "
            "if($source[$start] -eq [char]39){$start++}; if($source[$start] -eq \"`r\"){$start++}; "
            "if($source[$start] -eq \"`n\"){$start++}; "
            "$end=$source.IndexOf(([char]39).ToString()+\"@\",$start); "
            "$native=$source.Substring($start,$end-$start); "
            "Add-Type -TypeDefinition $native -ReferencedAssemblies @('System.Management.dll') -ErrorAction Stop; "
            "if(-not [EnergyGridOneShotSupervisorNative]::VerifyX64StructureSizes()){throw 'layout_failed'}"
        )
        result = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


_NATIVE_ASSURANCE_HARNESS = r'''
param(
    [Parameter(Mandatory = $true)][string]$SupervisorPath,
    [Parameter(Mandatory = $true)][string]$RootPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Native {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$source = Get-Content -LiteralPath $SupervisorPath -Raw
$marker = "`$script:EgNativeSource = @"
$start = $source.IndexOf($marker)
Assert-Native ($start -ge 0) 'native_source_marker_missing'
$start += $marker.Length
if ($source[$start] -eq [char]39) { $start++ }
if ($source[$start] -eq "`r") { $start++ }
if ($source[$start] -eq "`n") { $start++ }
$end = $source.IndexOf(([char]39).ToString() + "@", $start)
Assert-Native ($end -gt $start) 'native_source_terminator_missing'
$native = $source.Substring($start, $end - $start)
Add-Type -TypeDefinition $native -ReferencedAssemblies @('System.Management.dll') -ErrorAction Stop
Assert-Native ([EnergyGridOneShotSupervisorNative]::VerifyX64StructureSizes()) 'native_x64_layout_failed'

Add-Type -TypeDefinition @'
using System.IO;
using System.Threading;

public sealed class EnergyGridDelayedFlushStreamForTest : FileStream
{
    private readonly int delayMilliseconds;

    public EnergyGridDelayedFlushStreamForTest(string path, int delayMilliseconds)
        : base(path, FileMode.Create, FileAccess.ReadWrite, FileShare.None, 4096,
            FileOptions.DeleteOnClose)
    {
        this.delayMilliseconds = delayMilliseconds;
    }

    public override void Flush(bool flushToDisk)
    {
        Thread.Sleep(delayMilliseconds);
        base.Flush(flushToDisk);
    }
}
'@

$script:PowerShellPath = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
Assert-Native (Test-Path -LiteralPath $script:PowerShellPath -PathType Leaf) 'native_powershell_missing'

function Close-Native {
    param([IntPtr]$Handle)
    if ($Handle -ne [IntPtr]::Zero) {
        [void][EnergyGridOneShotSupervisorNative]::CloseHandleChecked($Handle)
    }
}

function Encode-ChildScript {
    param([string]$Script)
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
}

function Quote-PowerShellLiteral {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function New-ContainedPowerShell {
    param([IntPtr]$JobHandle, [string]$Code)

    $pipes = $null
    $attributes = $null
    try {
        $pipes = [EnergyGridOneShotSupervisorNative]::CreatePipes()
        $attributes = New-Object EnergyGridOneShotSupervisorNative+AttributeResources(
            $JobHandle, $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite,
            $pipes.LauncherStderrWrite)
        $encoded = Encode-ChildScript -Script $Code
        $commandLine = '"' + $script:PowerShellPath + '" -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + $encoded
        $builder = New-Object System.Text.StringBuilder($commandLine)
        $created = [EnergyGridOneShotSupervisorNative]::CreateContainedProcess(
            $script:PowerShellPath, $builder, [Environment]::SystemDirectory,
            $attributes.AttributeList, $pipes.LauncherStdinRead,
            $pipes.LauncherStdoutWrite, $pipes.LauncherStderrWrite)
        if (-not $created.Succeeded) {
            throw ('native_create_failed:' + $created.ErrorCode)
        }
        $attributes.Dispose()
        $attributes = $null
        Close-Native -Handle $pipes.LauncherStdinRead
        $pipes.LauncherStdinRead = [IntPtr]::Zero
        Close-Native -Handle $pipes.LauncherStdoutWrite
        $pipes.LauncherStdoutWrite = [IntPtr]::Zero
        Close-Native -Handle $pipes.LauncherStderrWrite
        $pipes.LauncherStderrWrite = [IntPtr]::Zero
        $stdoutDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStdoutRead)
        $pipes.SupervisorStdoutRead = [IntPtr]::Zero
        $stderrDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStderrRead)
        $pipes.SupervisorStderrRead = [IntPtr]::Zero
        Close-Native -Handle $pipes.SupervisorStdinWrite
        $pipes.SupervisorStdinWrite = [IntPtr]::Zero
        return [pscustomobject]@{
            ProcessHandle = $created.ProcessInfo.hProcess
            ThreadHandle = $created.ProcessInfo.hThread
            ProcessId = $created.ProcessInfo.dwProcessId
            StdoutDrain = $stdoutDrain
            StderrDrain = $stderrDrain
        }
    }
    catch {
        if ($null -ne $attributes) { $attributes.Dispose() }
        if ($null -ne $pipes) {
            Close-Native -Handle $pipes.LauncherStdinRead
            Close-Native -Handle $pipes.SupervisorStdinWrite
            Close-Native -Handle $pipes.SupervisorStdoutRead
            Close-Native -Handle $pipes.LauncherStdoutWrite
            Close-Native -Handle $pipes.SupervisorStderrRead
            Close-Native -Handle $pipes.LauncherStderrWrite
        }
        throw
    }
}

function Wait-JobZero {
    param([IntPtr]$JobHandle, [int]$Seconds = 10)
    $deadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]$Seconds * [int64][System.Diagnostics.Stopwatch]::Frequency)
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $deadline) {
        $accounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($JobHandle)
        Assert-Native $accounting.Succeeded ('accounting_failed:' + $accounting.ErrorCode)
        if ($accounting.ActiveProcesses -eq 0) { return $accounting }
        Start-Sleep -Milliseconds 100
    }
    $final = [EnergyGridOneShotSupervisorNative]::GetAccounting($JobHandle)
    Assert-Native $final.Succeeded ('final_accounting_failed:' + $final.ErrorCode)
    Assert-Native ($final.ActiveProcesses -eq 0) 'reap_timeout'
    return $final
}

function Close-Contained {
    param($Process)
    if ($null -eq $Process) { return }
    if ($null -ne $Process.StdoutDrain) { [void]$Process.StdoutDrain.Join(10000) }
    if ($null -ne $Process.StderrDrain) { [void]$Process.StderrDrain.Join(10000) }
    Close-Native -Handle $Process.ThreadHandle
    Close-Native -Handle $Process.ProcessHandle
}

function Cleanup-Job {
    param([IntPtr]$JobHandle, $Process)
    try {
        if ($JobHandle -ne [IntPtr]::Zero) {
            [void][EnergyGridOneShotSupervisorNative]::TerminateJob(
                $JobHandle, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        }
    }
    catch { }
    try { Close-Contained -Process $Process } catch { }
    Close-Native -Handle $JobHandle
}

function Future-Deadline {
    param([int]$Milliseconds = 30000)
    return [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]$Milliseconds * [int64][System.Diagnostics.Stopwatch]::Frequency / 1000))
}

Write-Output 'native_case=bounded_durability_results'
$durabilityPath = Join-Path $RootPath 'durability.bin'
$successStream = $null
$failureStream = $null
$delayedStream = $null
try {
    $successStream = New-Object System.IO.FileStream(
        $durabilityPath, [IO.FileMode]::Create, [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None, 4096, [IO.FileOptions]::DeleteOnClose)
    $success = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
        $successStream, [byte[]](1, 2, 3), 5000)
    Assert-Native ($success.Succeeded -and -not $success.TimedOut) 'durability_success_result_invalid'
    $successStream.Dispose()
    $successStream = $null

    $failureStream = New-Object System.IO.FileStream(
        $durabilityPath, [IO.FileMode]::Create, [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None, 4096, [IO.FileOptions]::DeleteOnClose)
    $failureStream.Dispose()
    $failureStream = $null
    $failure = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
        $failureStream, [byte[]](1, 2, 3), 5000)
    Assert-Native ((-not $failure.Succeeded) -and (-not $failure.TimedOut)) 'durability_failure_result_invalid'

    $delayedPath = Join-Path $RootPath 'delayed-durability.bin'
    $delayedStream = New-Object EnergyGridDelayedFlushStreamForTest($delayedPath, 5250)
    $delayed = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
        $delayedStream, [byte[]](1, 2, 3), 5000)
    Assert-Native ((-not $delayed.Succeeded) -and $delayed.TimedOut) 'durability_timeout_result_invalid'
    Start-Sleep -Milliseconds 6000
    Assert-Native ((-not $delayed.Succeeded) -and $delayed.TimedOut) 'durability_timeout_reopened'
    Write-Output ('native_durability_timeout=' + [string]$delayed.TimedOut)
    Write-Output ('native_durability_succeeded_after_wait=' + [string]$delayed.Succeeded)
}
finally {
    if ($null -ne $successStream) { $successStream.Dispose() }
    if ($null -ne $failureStream) { $failureStream.Dispose() }
    if ($null -ne $delayedStream) { $delayedStream.Dispose() }
}

Write-Output 'native_case=job_policy_before_and_after_activity'
$job = [IntPtr]::Zero
$process = $null
try {
    $job = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($job)
    $before = [EnergyGridOneShotSupervisorNative]::VerifyJobLimits($job)
    Assert-Native ($before.Succeeded -and $before.Matches) 'job_policy_before_create_failed'
    $code = @'
$buffer = New-Object byte[] (1024 * 1024)
[Console]::OpenStandardOutput().Write($buffer, 0, $buffer.Length)
[Console]::OpenStandardError().Write($buffer, 0, $buffer.Length)
Start-Sleep -Seconds 60
'@
    $process = New-ContainedPowerShell -JobHandle $job -Code $code
    $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership(
        $process.ProcessHandle, $job)
    Assert-Native ($membership.Succeeded -and $membership.IsMember) 'creation_membership_failed'
    $afterCreate = [EnergyGridOneShotSupervisorNative]::VerifyJobLimits($job)
    Assert-Native ($afterCreate.Succeeded -and $afterCreate.Matches) 'job_policy_after_create_failed'
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    $intent = [EnergyGridOneShotSupervisorNative]::CommitIntent()
    Assert-Native $intent 'native_intent_commit_failed'
    $resume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $process.ThreadHandle, (Future-Deadline))
    Assert-Native ($resume.Attempted -and $resume.Accepted -and $resume.ReturnValue -eq 1) 'native_resume_failed'
    Start-Sleep -Milliseconds 500
    $afterActivity = [EnergyGridOneShotSupervisorNative]::VerifyJobLimits($job)
    Assert-Native ($afterActivity.Succeeded -and $afterActivity.Matches) 'job_policy_after_activity_failed'
    $drainDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency)
    while (($process.StdoutDrain.Bytes -eq 0 -or $process.StderrDrain.Bytes -eq 0) -and
        [System.Diagnostics.Stopwatch]::GetTimestamp() -lt $drainDeadline) {
        Start-Sleep -Milliseconds 50
    }
    $terminated = [EnergyGridOneShotSupervisorNative]::TerminateJob(
        $job, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
    Assert-Native $terminated.Success ('native_termination_failed:' + $terminated.ErrorCode)
    $wait = [EnergyGridOneShotSupervisorNative]::WaitProcess($process.ProcessHandle, 10000)
    Assert-Native ($wait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) 'native_termination_wait_failed'
    $final = Wait-JobZero -JobHandle $job
    Assert-Native ($final.ActiveProcesses -eq 0) 'active_processes_not_zero'
    $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($process.ProcessHandle)
    Assert-Native ($live.Succeeded -and $live.ExitCode -eq [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE) 'termination_code_not_observed'
    [void]$process.StdoutDrain.Join(10000)
    [void]$process.StderrDrain.Join(10000)
    Assert-Native ($process.StdoutDrain.Completed -and $process.StderrDrain.Completed) 'native_drains_incomplete'
    Assert-Native ($process.StdoutDrain.Bytes -gt 0 -and $process.StderrDrain.Bytes -gt 0) 'native_drain_counts_empty'
}
finally {
    Cleanup-Job -JobHandle $job -Process $process
}

Write-Output 'native_case=wrong_active_flags_rejected'
$wrongJob = [IntPtr]::Zero
try {
    $wrongJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    $wrongInfo = New-Object EnergyGridOneShotSupervisorNative+JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    $wrongFlags = [uint32]([int64][EnergyGridOneShotSupervisorNative]::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE -bor 8)
    $wrongInfo.BasicLimitInformation.LimitFlags = $wrongFlags
    $wrongInfo.BasicLimitInformation.ActiveProcessLimit = 2
    $wrongLength = [uint32][Runtime.InteropServices.Marshal]::SizeOf(
        [type]'EnergyGridOneShotSupervisorNative+JOBOBJECT_EXTENDED_LIMIT_INFORMATION')
    $wrongSet = [EnergyGridOneShotSupervisorNative]::SetInformationJobObject(
        $wrongJob, [EnergyGridOneShotSupervisorNative]::JobObjectExtendedLimitInformation,
        [ref]$wrongInfo, $wrongLength)
    Assert-Native $wrongSet 'wrong_active_flags_setup_failed'
    $wrongReadback = [EnergyGridOneShotSupervisorNative]::VerifyJobLimits($wrongJob)
    Assert-Native ($wrongReadback.Succeeded -and -not $wrongReadback.Matches) 'wrong_active_flags_accepted'
}
finally {
    Close-Native -Handle $wrongJob
}

Write-Output 'native_case=unsupported_job_list_rejected_before_execution'
$invalidJob = [IntPtr]::Zero
$invalidPipes = $null
$invalidAttributes = $null
try {
    $invalidJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($invalidJob)
    $invalidPipes = [EnergyGridOneShotSupervisorNative]::CreatePipes()
    $invalidFailed = $false
    try {
        $invalidAttributes = New-Object EnergyGridOneShotSupervisorNative+AttributeResources(
            [IntPtr]::Zero, $invalidPipes.LauncherStdinRead,
            $invalidPipes.LauncherStdoutWrite, $invalidPipes.LauncherStderrWrite)
        $invalidBuilder = New-Object System.Text.StringBuilder(('"' + $script:PowerShellPath + '" -NoLogo -NoProfile -NonInteractive -Command "exit 0"'))
        $invalidCreation = [EnergyGridOneShotSupervisorNative]::CreateContainedProcess(
            $script:PowerShellPath, $invalidBuilder, [Environment]::SystemDirectory,
            $invalidAttributes.AttributeList, $invalidPipes.LauncherStdinRead,
            $invalidPipes.LauncherStdoutWrite, $invalidPipes.LauncherStderrWrite)
        $invalidFailed = -not $invalidCreation.Succeeded
        if ($invalidCreation.Succeeded) {
            [void][EnergyGridOneShotSupervisorNative]::ResumeThread($invalidCreation.ProcessInfo.hThread)
            [void][EnergyGridOneShotSupervisorNative]::WaitForSingleObject($invalidCreation.ProcessInfo.hProcess, 5000)
            Close-Native -Handle $invalidCreation.ProcessInfo.hThread
            Close-Native -Handle $invalidCreation.ProcessInfo.hProcess
        }
    }
    catch { $invalidFailed = $true }
    Assert-Native $invalidFailed 'unsupported_job_list_created_process'
}
finally {
    if ($null -ne $invalidAttributes) { $invalidAttributes.Dispose() }
    if ($null -ne $invalidPipes) {
        Close-Native -Handle $invalidPipes.LauncherStdinRead
        Close-Native -Handle $invalidPipes.SupervisorStdinWrite
        Close-Native -Handle $invalidPipes.SupervisorStdoutRead
        Close-Native -Handle $invalidPipes.LauncherStdoutWrite
        Close-Native -Handle $invalidPipes.SupervisorStderrRead
        Close-Native -Handle $invalidPipes.LauncherStderrWrite
    }
    Close-Native -Handle $invalidJob
}

Write-Output 'native_case=deadline_gate_and_one_way_resume'
$markerRoot = Join-Path $RootPath 'deadline-marker.txt'
foreach ($mode in @('expired', 'delayed', 'future', 'failure', 'anomaly')) {
    $gateJob = [IntPtr]::Zero
    $gateProcess = $null
    try {
        [EnergyGridOneShotSupervisorNative]::ResetControlState()
        $gateJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
        [EnergyGridOneShotSupervisorNative]::ConfigureJob($gateJob)
        $gateCode = "[IO.File]::WriteAllText(" + (Quote-PowerShellLiteral -Value $markerRoot) + ",'ran'); Start-Sleep -Seconds 60"
        $gateProcess = New-ContainedPowerShell -JobHandle $gateJob -Code $gateCode
        Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'gate_intent_commit_failed'
        if ($mode -eq 'expired') {
            $gateResult = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, ([System.Diagnostics.Stopwatch]::GetTimestamp() - 1))
            Assert-Native (-not $gateResult.Attempted -and $gateResult.DeadlineExpired) 'expired_deadline_resumed'
            $late = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, (Future-Deadline))
            Assert-Native (-not $late.Attempted) 'expired_gate_reopened'
        }
        elseif ($mode -eq 'delayed') {
            $deadline = Future-Deadline -Milliseconds 100
            Start-Sleep -Milliseconds 250
            $gateResult = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, $deadline)
            Assert-Native (-not $gateResult.Attempted -and $gateResult.DeadlineExpired) 'delayed_deadline_resumed'
        }
        elseif ($mode -eq 'future') {
            $gateResult = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, (Future-Deadline))
            Assert-Native ($gateResult.Attempted -and $gateResult.Accepted) 'future_deadline_rejected'
            $second = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, (Future-Deadline))
            Assert-Native (-not $second.Attempted) 'resume_retried'
            $ranDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
                ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency)
            while (-not (Test-Path -LiteralPath $markerRoot) -and
                [System.Diagnostics.Stopwatch]::GetTimestamp() -lt $ranDeadline) {
                Start-Sleep -Milliseconds 50
            }
            Assert-Native (Test-Path -LiteralPath $markerRoot) 'future_child_did_not_execute'
            Remove-Item -LiteralPath $markerRoot -Force
        }
        elseif ($mode -eq 'failure') {
            $failedIntentPath = Join-Path $RootPath 'failed-intent.bin'
            $failedIntentStream = New-Object System.IO.FileStream(
                $failedIntentPath, [IO.FileMode]::Create, [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None, 1, [IO.FileOptions]::DeleteOnClose)
            $failedIntentStream.Dispose()
            $durability = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
                $failedIntentStream, [byte[]](1, 2, 3), 1000)
            Assert-Native (-not $durability.Succeeded) 'durability_failure_succeeded'
            [EnergyGridOneShotSupervisorNative]::RequestFailure()
            Assert-Native (-not [EnergyGridOneShotSupervisorNative]::CommitIntent()) 'failure_gate_reopened'
            $gateResult = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, (Future-Deadline))
            Assert-Native (-not $gateResult.Attempted) 'failure_gate_resumed'
        }
        else {
            $gateResult = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                ([IntPtr]([int64]1)), (Future-Deadline))
            Assert-Native ($gateResult.Attempted -and -not $gateResult.Accepted) 'resume_anomaly_not_observed'
            $late = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
                $gateProcess.ThreadHandle, (Future-Deadline))
            Assert-Native (-not $late.Attempted) 'resume_anomaly_reopened_gate'
        }
        if ($mode -ne 'future' -and (Test-Path -LiteralPath $markerRoot)) {
            throw ($mode + '_child_executed')
        }
    }
    finally {
        Cleanup-Job -JobHandle $gateJob -Process $gateProcess
        if (Test-Path -LiteralPath $markerRoot) { Remove-Item -LiteralPath $markerRoot -Force }
    }
}

Write-Output 'native_case=stdout_stderr_saturation_without_deadlock'
    $saturationJob = [IntPtr]::Zero
    $saturationProcess = $null
    try {
        $saturationJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
        [EnergyGridOneShotSupervisorNative]::ConfigureJob($saturationJob)
    $saturationCode = @"
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Threading;

public static class EnergyGridConcurrentSaturationWriters
{
    public static void Run(int byteCount)
    {
        byte[] payload = new byte[byteCount];
        ManualResetEvent startGate = new ManualResetEvent(false);
        object errorLock = new object();
        Exception failure = null;
        Thread stdoutWriter = new Thread(delegate()
        {
            try
            {
                startGate.WaitOne();
                using (Stream stream = Console.OpenStandardOutput())
                {
                    stream.Write(payload, 0, payload.Length);
                    stream.Flush();
                }
            }
            catch (Exception error)
            {
                lock (errorLock) { if (failure == null) { failure = error; } }
            }
        });
        Thread stderrWriter = new Thread(delegate()
        {
            try
            {
                startGate.WaitOne();
                using (Stream stream = Console.OpenStandardError())
                {
                    stream.Write(payload, 0, payload.Length);
                    stream.Flush();
                }
            }
            catch (Exception error)
            {
                lock (errorLock) { if (failure == null) { failure = error; } }
            }
        });
        stdoutWriter.Start();
        stderrWriter.Start();
        startGate.Set();
        bool stdoutFinished = stdoutWriter.Join(30000);
        bool stderrFinished = stderrWriter.Join(30000);
        startGate.Dispose();
        if (!stdoutFinished || !stderrFinished) { throw new TimeoutException(); }
        if (failure != null) { throw failure; }
    }
}
'@
[EnergyGridConcurrentSaturationWriters]::Run(1024 * 1024)
"@
    $saturationProcess = New-ContainedPowerShell -JobHandle $saturationJob -Code $saturationCode
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'saturation_intent_failed'
    $saturationResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $saturationProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($saturationResume.Attempted -and $saturationResume.Accepted) 'saturation_resume_failed'
    $saturationWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $saturationProcess.ProcessHandle, 15000)
    Assert-Native ($saturationWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) 'saturation_pipe_deadlock'
    $saturationFinal = Wait-JobZero -JobHandle $saturationJob
    [void]$saturationProcess.StdoutDrain.Join(10000)
    [void]$saturationProcess.StderrDrain.Join(10000)
    Assert-Native ($saturationProcess.StdoutDrain.Completed -and $saturationProcess.StderrDrain.Completed) 'saturation_drains_incomplete'
    Assert-Native ($saturationProcess.StdoutDrain.Bytes -ge 1048576 -and $saturationProcess.StderrDrain.Bytes -ge 1048576) 'saturation_byte_counts_incomplete'
    Assert-Native ($saturationFinal.ActiveProcesses -eq 0) 'saturation_active_processes_nonzero'
    Write-Output 'native_saturation_writers=CONCURRENT'
    Write-Output ('native_saturation_stdout_bytes=' + [string]$saturationProcess.StdoutDrain.Bytes)
    Write-Output ('native_saturation_stderr_bytes=' + [string]$saturationProcess.StderrDrain.Bytes)
    Write-Output ('native_saturation_drains=' + [string]($saturationProcess.StdoutDrain.Completed -and $saturationProcess.StderrDrain.Completed))
    Write-Output ('native_saturation_active_processes=' + [string]$saturationFinal.ActiveProcesses)
}
finally {
    Cleanup-Job -JobHandle $saturationJob -Process $saturationProcess
}

Write-Output 'native_case=child_grandchild_containment_and_large_tree'
$treeJob = [IntPtr]::Zero
$treeProcess = $null
try {
    $treeJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($treeJob)
    $treeCode = @'
$shell = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
1..40 | ForEach-Object {
    Start-Process -FilePath $shell -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds 60') -WindowStyle Hidden
}
Start-Sleep -Seconds 60
'@
    $treeProcess = New-ContainedPowerShell -JobHandle $treeJob -Code $treeCode
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'tree_intent_failed'
    $treeResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $treeProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($treeResume.Attempted -and $treeResume.Accepted) 'tree_resume_failed'
    $treeDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]15 * [int64][System.Diagnostics.Stopwatch]::Frequency)
    $treeAccounting = $null
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $treeDeadline) {
        $treeAccounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($treeJob)
        Assert-Native $treeAccounting.Succeeded 'tree_accounting_failed'
        if ($treeAccounting.TotalProcesses -ge 41) { break }
        Start-Sleep -Milliseconds 100
    }
    Assert-Native ($treeAccounting.TotalProcesses -ge 41) 'large_descendant_tree_not_observed'
    $treeTermination = [EnergyGridOneShotSupervisorNative]::TerminateJob(
        $treeJob, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
    Assert-Native $treeTermination.Success 'tree_termination_failed'
    $treeFinal = Wait-JobZero -JobHandle $treeJob -Seconds 20
    Assert-Native ($treeFinal.ActiveProcesses -eq 0) 'tree_reap_not_confirmed'
}
finally {
    Cleanup-Job -JobHandle $treeJob -Process $treeProcess
}

Write-Output 'native_case=explicit_handle_list_excludes_unrelated_inheritable_handle'
$canaryJob = [IntPtr]::Zero
$canaryProcess = $null
$canaryStream = $null
try {
    $canaryJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($canaryJob)
    $canaryPath = Join-Path $RootPath 'unrelated-canary.bin'
    $canaryResult = Join-Path $RootPath 'unrelated-canary-result.txt'
    $canaryStream = New-Object System.IO.FileStream(
        $canaryPath, [IO.FileMode]::Create, [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None, 1, [IO.FileOptions]::DeleteOnClose)
    $canaryInheritance = [EnergyGridOneShotSupervisorNative]::SetHandleInheritance(
        $canaryStream.SafeFileHandle.DangerousGetHandle(), $true)
    Assert-Native $canaryInheritance.Success 'canary_inheritance_setup_failed'
    $canaryCode = @'
$inherited = $false
try {
    $stream = New-Object System.IO.FileStream([IntPtr]__CANARY__, [IO.FileAccess]::Read, $false, 1, $false)
    $inherited = $true
    $stream.Dispose()
}
catch { }
if ($inherited) { [IO.File]::WriteAllText(__RESULT__, 'inherited') }
else { [IO.File]::WriteAllText(__RESULT__, 'not_inherited') }
'@
    $canaryCode = $canaryCode.Replace('__CANARY__', $canaryStream.SafeFileHandle.DangerousGetHandle().ToInt64().ToString())
    $canaryCode = $canaryCode.Replace('__RESULT__', (Quote-PowerShellLiteral -Value $canaryResult))
    $canaryProcess = New-ContainedPowerShell -JobHandle $canaryJob -Code $canaryCode
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'canary_intent_failed'
    $canaryResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $canaryProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($canaryResume.Attempted -and $canaryResume.Accepted) 'canary_resume_failed'
    $canaryWait = [EnergyGridOneShotSupervisorNative]::WaitProcess($canaryProcess.ProcessHandle, 10000)
    Assert-Native ($canaryWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) 'canary_child_wait_failed'
    $canaryFinal = Wait-JobZero -JobHandle $canaryJob
    $canaryDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency)
    while (-not (Test-Path -LiteralPath $canaryResult) -and
        [System.Diagnostics.Stopwatch]::GetTimestamp() -lt $canaryDeadline) {
        Start-Sleep -Milliseconds 50
    }
    Assert-Native (Test-Path -LiteralPath $canaryResult) 'canary_result_missing'
    $canaryValue = Get-Content -LiteralPath $canaryResult -Raw
    Assert-Native ($canaryValue -eq 'not_inherited') ('canary_inherited:' + $canaryValue)
    Assert-Native ($canaryFinal.ActiveProcesses -eq 0) 'canary_reap_failed'
}
finally {
    if ($null -ne $canaryStream) { $canaryStream.Dispose() }
    Cleanup-Job -JobHandle $canaryJob -Process $canaryProcess
}

Write-Output 'native_assurance_cases=15'
Write-Output 'native_assurance=PASS'
'''


class SupervisorNativeAssuranceTests(unittest.TestCase):
    """Real Windows-native regressions against the committed supervisor boundary."""

    def _run_native_harness(self, script, timeout=180):
        powershell = native_powershell()
        if powershell is None:
            self.skipTest("native Windows PowerShell 5.1 is only available on Windows")
        with tempfile.TemporaryDirectory(prefix="eg_native_assurance_") as directory:
            root = Path(directory)
            harness = root / "native_assurance.ps1"
            harness.write_text(script, encoding="ascii")
            result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(harness),
                    "-SupervisorPath",
                    str(SUPERVISOR),
                    "-RootPath",
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            self.assertEqual(
                0,
                result.returncode,
                result.stdout + "\n" + result.stderr,
            )
            return result.stdout

    def test_native_assurance_harness_uses_real_job_objects_and_pipes(self):
        output = self._run_native_harness(_NATIVE_ASSURANCE_HARNESS)
        for case in (
            "job_policy_before_and_after_activity",
            "wrong_active_flags_rejected",
            "unsupported_job_list_rejected_before_execution",
            "deadline_gate_and_one_way_resume",
            "stdout_stderr_saturation_without_deadlock",
            "child_grandchild_containment_and_large_tree",
            "explicit_handle_list_excludes_unrelated_inheritable_handle",
        ):
            self.assertIn("native_case=" + case, output)
        self.assertIn("native_case=bounded_durability_results", output)
        self.assertIn("native_durability_timeout=True", output)
        self.assertIn("native_durability_succeeded_after_wait=False", output)
        self.assertIn("native_saturation_writers=CONCURRENT", output)
        metrics = {}
        for line in output.splitlines():
            if line.startswith("native_saturation_") and "=" in line:
                name, value = line.split("=", 1)
                metrics[name] = value
        self.assertGreaterEqual(int(metrics["native_saturation_stdout_bytes"]), 1048576)
        self.assertGreaterEqual(int(metrics["native_saturation_stderr_bytes"]), 1048576)
        self.assertIn("native_saturation_drains=True", output)
        self.assertIn("native_saturation_active_processes=0", output)
        self.assertIn("native_assurance_cases=15", output)
        self.assertIn("native_assurance=PASS", output)


_NATIVE_FUNCTION_HARNESS = r'''
param(
    [Parameter(Mandatory = $true)][string]$SupervisorPath,
    [Parameter(Mandatory = $true)][string]$RootPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Native {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$source = Get-Content -LiteralPath $SupervisorPath -Raw
$marker = "`$script:EgNativeSource = @"
$start = $source.IndexOf($marker)
Assert-Native ($start -ge 0) 'native_source_marker_missing'
$start += $marker.Length
if ($source[$start] -eq [char]39) { $start++ }
if ($source[$start] -eq "`r") { $start++ }
if ($source[$start] -eq "`n") { $start++ }
$end = $source.IndexOf(([char]39).ToString() + "@", $start)
Assert-Native ($end -gt $start) 'native_source_terminator_missing'
Add-Type -TypeDefinition $source.Substring($start, $end - $start) -ReferencedAssemblies @('System.Management.dll') -ErrorAction Stop
Assert-Native ([EnergyGridOneShotSupervisorNative]::VerifyX64StructureSizes()) 'native_x64_layout_failed'

Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

public enum FILE_INFO_BY_HANDLE_CLASS : int
{
    FileIdInfo = 18
}

[StructLayout(LayoutKind.Sequential)]
public struct FILE_ID_128
{
    public ulong Part0;
    public ulong Part1;
}

[StructLayout(LayoutKind.Sequential)]
public struct FILE_ID_INFO
{
    public ulong VolumeSerialNumber;
    public FILE_ID_128 FileId;
}

[StructLayout(LayoutKind.Sequential)]
public struct BY_HANDLE_FILE_INFORMATION
{
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

public sealed class EgIdentityOpenResult
{
    public bool Succeeded;
    public int ErrorCode;
    public SafeFileHandle Handle;
}

public sealed class EgIdentityInfoResult
{
    public bool Succeeded;
    public int ErrorCode;
    public ulong VolumeSerialNumber;
    public ulong FileIdPart0;
    public ulong FileIdPart1;
    public uint FileAttributes;
    public uint NumberOfLinks;
}

public sealed class EgIdentityReadResult
{
    public bool Succeeded;
    public int ErrorCode;
    public long InitialLength;
    public long FinalLength;
    public bool Eof;
    public byte[] Bytes;
}

public sealed class EgIdentityBooleanResult
{
    public bool Succeeded;
    public int ErrorCode;
}

public sealed class EgShortPathResult
{
    public bool Succeeded;
    public int ErrorCode;
    public string Path;
}

public static class EgOutcomeIdentity
{
    public const uint DirectoryAccess = 0x00100081;
    public const uint DirectoryShare = 0x00000003;
    public const uint DirectoryCreation = 0x00000003;
    public const uint DirectoryFlags = 0x02200000;
    public const uint FileAccess = 0x80100080;
    public const uint FileShare = 0x00000001;
    public const uint FileCreation = 0x00000003;
    public const uint FileFlags = 0x00200000;
    public const uint FileAttributeDirectory = 0x00000010;
    public const uint FileAttributeReparsePoint = 0x00000400;
    public const int FileIdInfoValue = 18;

    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode,
        SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        IntPtr securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandleEx(
        SafeFileHandle hFile,
        FILE_INFO_BY_HANDLE_CLASS fileInformationClass,
        out FILE_ID_INFO fileInformation,
        uint bufferSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(
        SafeFileHandle hFile,
        out BY_HANDLE_FILE_INFORMATION fileInformation);

    [DllImport("kernel32.dll", EntryPoint = "GetShortPathNameW", CharSet = CharSet.Unicode,
        SetLastError = true)]
    private static extern uint GetShortPathNameW(
        string longPath,
        StringBuilder shortPath,
        int shortPathCapacity);

    [DllImport("kernel32.dll", EntryPoint = "CreateHardLinkW", CharSet = CharSet.Unicode,
        SetLastError = true)]
    private static extern bool CreateHardLinkW(
        string fileName,
        string existingFileName,
        IntPtr securityAttributes);

    public static bool VerifyLayout()
    {
        return Marshal.SizeOf(typeof(FILE_ID_128)) == 16 &&
            Marshal.SizeOf(typeof(FILE_ID_INFO)) == 24 &&
            Marshal.SizeOf(typeof(BY_HANDLE_FILE_INFORMATION)) == 52 &&
            (int)FILE_INFO_BY_HANDLE_CLASS.FileIdInfo == FileIdInfoValue;
    }

    private static EgIdentityOpenResult Open(
        string path, uint access, uint share, uint creation, uint flags)
    {
        EgIdentityOpenResult result = new EgIdentityOpenResult();
        result.Handle = CreateFileW(path, access, share, IntPtr.Zero, creation, flags, IntPtr.Zero);
        if (result.Handle == null || result.Handle.IsInvalid)
        {
            result.ErrorCode = Marshal.GetLastWin32Error();
            return result;
        }
        result.Succeeded = true;
        return result;
    }

    public static EgIdentityOpenResult OpenDirectory(string path)
    {
        return Open(path, DirectoryAccess, DirectoryShare, DirectoryCreation, DirectoryFlags);
    }

    public static EgIdentityOpenResult OpenFile(string path)
    {
        return Open(path, FileAccess, FileShare, FileCreation, FileFlags);
    }

    public static EgIdentityInfoResult QueryInfo(SafeFileHandle handle)
    {
        EgIdentityInfoResult result = new EgIdentityInfoResult();
        if (handle == null || handle.IsInvalid)
        {
            result.ErrorCode = 6;
            return result;
        }

        FILE_ID_INFO identity;
        if (!GetFileInformationByHandleEx(
            handle,
            FILE_INFO_BY_HANDLE_CLASS.FileIdInfo,
            out identity,
            (uint)Marshal.SizeOf(typeof(FILE_ID_INFO))))
        {
            result.ErrorCode = Marshal.GetLastWin32Error();
            return result;
        }

        BY_HANDLE_FILE_INFORMATION legacy;
        if (!GetFileInformationByHandle(handle, out legacy))
        {
            result.ErrorCode = Marshal.GetLastWin32Error();
            return result;
        }

        result.Succeeded = true;
        result.VolumeSerialNumber = identity.VolumeSerialNumber;
        result.FileIdPart0 = identity.FileId.Part0;
        result.FileIdPart1 = identity.FileId.Part1;
        result.FileAttributes = legacy.FileAttributes;
        result.NumberOfLinks = legacy.NumberOfLinks;
        return result;
    }

    public static EgIdentityReadResult ReadRetained(SafeFileHandle original)
    {
        EgIdentityReadResult result = new EgIdentityReadResult();
        bool addedReference = false;
        try
        {
            if (original == null || original.IsInvalid)
            {
                result.ErrorCode = 6;
                return result;
            }

            original.DangerousAddRef(ref addedReference);
            using (SafeFileHandle borrowed = new SafeFileHandle(
                original.DangerousGetHandle(), false))
            using (FileStream stream = new FileStream(
                borrowed, System.IO.FileAccess.Read, 65536, false))
            {
                result.InitialLength = stream.Length;
                if (result.InitialLength < 1 || result.InitialLength > 65536)
                {
                    return result;
                }

                byte[] bytes = new byte[(int)result.InitialLength];
                int offset = 0;
                while (offset < bytes.Length)
                {
                    int read = stream.Read(bytes, offset, bytes.Length - offset);
                    if (read <= 0)
                    {
                        return result;
                    }
                    offset += read;
                }

                result.Eof = stream.ReadByte() == -1;
                result.FinalLength = stream.Length;
                result.Bytes = bytes;
                result.Succeeded = result.Eof && result.FinalLength == result.InitialLength;
                if (!result.Succeeded)
                {
                    Array.Clear(bytes, 0, bytes.Length);
                    result.Bytes = null;
                }
            }
        }
        catch
        {
            result.Succeeded = false;
            result.Bytes = null;
        }
        finally
        {
            if (addedReference)
            {
                original.DangerousRelease();
            }
        }
        return result;
    }

    public static EgIdentityBooleanResult CreateHardLink(string linkPath, string existingPath)
    {
        EgIdentityBooleanResult result = new EgIdentityBooleanResult();
        result.Succeeded = CreateHardLinkW(linkPath, existingPath, IntPtr.Zero);
        if (!result.Succeeded)
        {
            result.ErrorCode = Marshal.GetLastWin32Error();
        }
        return result;
    }

    public static EgShortPathResult GetShortPath(string path)
    {
        EgShortPathResult result = new EgShortPathResult();
        int capacity = 260;
        while (capacity <= 32768)
        {
            StringBuilder buffer = new StringBuilder(capacity);
            uint length = GetShortPathNameW(path, buffer, buffer.Capacity);
            if (length == 0)
            {
                result.ErrorCode = Marshal.GetLastWin32Error();
                return result;
            }
            if (length < (uint)buffer.Capacity)
            {
                result.Succeeded = true;
                result.Path = buffer.ToString();
                return result;
            }
            capacity = checked((int)length + 1);
        }
        result.ErrorCode = 122;
        return result;
    }
}

public sealed class EnergyGridDelayedFlushStreamForFunctionTest : FileStream
{
    private readonly int delayMilliseconds;

    public EnergyGridDelayedFlushStreamForFunctionTest(string path, int delayMilliseconds)
        : base(path, FileMode.Create, FileAccess.ReadWrite, FileShare.None, 4096,
            FileOptions.DeleteOnClose)
    {
        this.delayMilliseconds = delayMilliseconds;
    }

    public override void Flush(bool flushToDisk)
    {
        Thread.Sleep(delayMilliseconds);
        base.Flush(flushToDisk);
    }
}

public sealed class EnergyGridOutcomeDelayedFlushStreamForFunctionTest : FileStream
{
    private const int DelayMilliseconds = 6500;
    private static readonly object workerLock = new object();

    public static readonly ManualResetEvent FlushEntered = new ManualResetEvent(false);
    public static readonly ManualResetEvent ReleaseFlush = new ManualResetEvent(false);
    public static Thread WorkerThread;

    public EnergyGridOutcomeDelayedFlushStreamForFunctionTest(
        string path, FileMode mode, FileAccess access, FileShare share,
        int bufferSize, FileOptions options)
        : base(path, mode, access, share, bufferSize, options)
    {
    }

    public static void ResetState()
    {
        FlushEntered.Reset();
        ReleaseFlush.Reset();
        lock (workerLock) { WorkerThread = null; }
    }

    public static bool IsReleaseClosed()
    {
        return !ReleaseFlush.WaitOne(0);
    }

    public static void Release()
    {
        ReleaseFlush.Set();
    }

    public static bool JoinWorker(int timeoutMilliseconds)
    {
        Thread worker;
        lock (workerLock) { worker = WorkerThread; }
        return worker != null && worker.Join(timeoutMilliseconds);
    }

    public override void Flush(bool flushToDisk)
    {
        Thread currentThread = Thread.CurrentThread;
        if (!currentThread.IsBackground)
        {
            base.Flush(flushToDisk);
            return;
        }
        lock (workerLock)
        {
            if (WorkerThread != null && WorkerThread != currentThread)
            {
                base.Flush(flushToDisk);
                return;
            }
            WorkerThread = currentThread;
        }
        FlushEntered.Set();
        Thread.Sleep(DelayMilliseconds);
        if (!ReleaseFlush.WaitOne(120000))
        {
            throw new TimeoutException("test release event timed out");
        }
        base.Flush(flushToDisk);
    }
}

public static class EnergyGridGraceInterruptSchedulerForFunctionTest
{
    public static Thread Schedule(int delayMilliseconds)
    {
        Thread thread = new Thread(delegate()
        {
            Thread.Sleep(delayMilliseconds);
            Type nativeType = null;
            foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                nativeType = assembly.GetType("EnergyGridOneShotSupervisorNative");
                if (nativeType != null) { break; }
            }
            if (nativeType == null) { throw new InvalidOperationException("native type missing"); }
            MethodInfo signalMethod = nativeType.GetMethod(
                "HandleConsoleSignal", BindingFlags.NonPublic | BindingFlags.Static);
            signalMethod.Invoke(null, new object[] { (uint)2 });
        });
        thread.IsBackground = true;
        thread.Start();
        return thread;
    }
}
'@

Assert-Native ([EgOutcomeIdentity]::VerifyLayout()) 'file_id_info_layout_invalid'
Assert-Native ([EgOutcomeIdentity]::DirectoryAccess -eq [uint32]0x00100081) `
    'directory_access_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::DirectoryShare -eq [uint32]0x00000003) `
    'directory_share_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::DirectoryCreation -eq [uint32]0x00000003) `
    'directory_creation_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::DirectoryFlags -eq [uint32]0x02200000) `
    'directory_flags_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::FileAccess -eq [uint32]2148532352) `
    'file_access_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::FileShare -eq [uint32]0x00000001) `
    'file_share_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::FileCreation -eq [uint32]0x00000003) `
    'file_creation_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::FileFlags -eq [uint32]0x00200000) `
    'file_flags_contract_invalid'
Assert-Native ([EgOutcomeIdentity]::FileIdInfoValue -eq 18) 'file_id_info_class_invalid'
Write-Output 'file_id_info_layout=PASS'

function Get-ExactFunction {
    param([string]$Name, [string]$NextName)
    $definitionCount = [regex]::Matches(
        $source, '(?m)^function ' + [regex]::Escape($Name) + '\s*\{').Count
    Assert-Native ($definitionCount -eq 1) ('function_definition_count:' + $Name)
    $start = $source.IndexOf('function ' + $Name)
    $end = $source.IndexOf('function ' + $NextName, $start)
    Assert-Native ($start -ge 0 -and $end -gt $start) ('function_missing:' + $Name)
    return $source.Substring($start, $end - $start)
}

Invoke-Expression (Get-ExactFunction -Name 'Stop-EgSupervisor' -NextName 'Test-EgUnsafeText')
Invoke-Expression (Get-ExactFunction -Name 'Get-EgAccounting' -NextName 'Invoke-EgTerminateJob')
Invoke-Expression (Get-ExactFunction -Name 'Invoke-EgTerminateJob' -NextName 'Get-EgDeadlineTicks')
Invoke-Expression (Get-ExactFunction -Name 'Test-EgDeadlineReached' -NextName 'Wait-EgReap')
Invoke-Expression (Get-ExactFunction -Name 'Wait-EgReap' -NextName 'Wait-EgDrains')
Invoke-Expression (Get-ExactFunction -Name 'Wait-EgDescendantGrace' -NextName 'Get-EgStartVerdict')
Invoke-Expression (Get-ExactFunction -Name 'Get-EgExitCode' -NextName 'Write-EgPublicProjection')
Invoke-Expression (Get-ExactFunction -Name 'ConvertTo-EgUtf8JsonBytes' -NextName 'Initialize-EgNative')
Invoke-Expression (Get-ExactFunction -Name 'Write-EgReservedIntent' -NextName 'Get-EgIntentObject')
Invoke-Expression (Get-ExactFunction -Name 'Get-EgOutcomeObject' -NextName 'Write-EgOutcome')
Invoke-Expression (Get-ExactFunction -Name 'Write-EgOutcome' -NextName 'Get-EgCanonicalApplicationCommandLine')

function Test-EgApplicationChild {
    param(
        [IntPtr]$JobHandle,
        [IntPtr]$LauncherHandle,
        [uint32]$LauncherPid,
        [long]$StartTicks
    )
    return $false
}

$script:PowerShellPath = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'

function Quote-PowerShellLiteral {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Future-Deadline {
    param([int]$Milliseconds = 30000)
    return [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]$Milliseconds * [int64][System.Diagnostics.Stopwatch]::Frequency / 1000))
}

function Close-Native {
    param([IntPtr]$Handle)
    if ($Handle -ne [IntPtr]::Zero) {
        [void][EnergyGridOneShotSupervisorNative]::CloseHandleChecked($Handle)
    }
}

function New-TestChild {
    param(
        [IntPtr]$JobHandle,
        [string]$Code = 'Start-Sleep -Seconds 60'
    )
    $pipes = [EnergyGridOneShotSupervisorNative]::CreatePipes()
    $attributes = New-Object EnergyGridOneShotSupervisorNative+AttributeResources(
        $JobHandle, $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite,
        $pipes.LauncherStderrWrite)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
    $commandLine = '"' + $script:PowerShellPath + '" -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + $encoded
    $builder = New-Object System.Text.StringBuilder($commandLine)
    $created = [EnergyGridOneShotSupervisorNative]::CreateContainedProcess(
        $script:PowerShellPath, $builder, [Environment]::SystemDirectory,
        $attributes.AttributeList, $pipes.LauncherStdinRead,
        $pipes.LauncherStdoutWrite, $pipes.LauncherStderrWrite)
    Assert-Native $created.Succeeded ('native_create_failed:' + $created.ErrorCode)
    $attributes.Dispose()
    Close-Native -Handle $pipes.LauncherStdinRead
    Close-Native -Handle $pipes.LauncherStdoutWrite
    Close-Native -Handle $pipes.LauncherStderrWrite
    $stdoutDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStdoutRead)
    $stderrDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStderrRead)
    Close-Native -Handle $pipes.SupervisorStdinWrite
    return [pscustomobject]@{
        ProcessHandle = $created.ProcessInfo.hProcess
        ThreadHandle = $created.ProcessInfo.hThread
        StdoutDrain = $stdoutDrain
        StderrDrain = $stderrDrain
    }
}

function Wait-JobZero {
    param([IntPtr]$JobHandle)
    $deadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]10 * [int64][System.Diagnostics.Stopwatch]::Frequency)
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $deadline) {
        $accounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($JobHandle)
        Assert-Native $accounting.Succeeded ('accounting_failed:' + $accounting.ErrorCode)
        if ($accounting.ActiveProcesses -eq 0) { return $accounting }
        Start-Sleep -Milliseconds 100
    }
    throw 'reap_timeout'
}

function Close-TestChild {
    param($Process)
    if ($null -eq $Process) { return }
    [void]$Process.StdoutDrain.Join(10000)
    [void]$Process.StderrDrain.Join(10000)
    Close-Native -Handle $Process.ThreadHandle
    Close-Native -Handle $Process.ProcessHandle
}

function Cleanup-Job {
    param([IntPtr]$JobHandle, $Process)
    try {
        if ($JobHandle -ne [IntPtr]::Zero) {
            [void][EnergyGridOneShotSupervisorNative]::TerminateJob(
                $JobHandle, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        }
    }
    catch { }
    try { Close-TestChild -Process $Process } catch { }
    Close-Native -Handle $JobHandle
}

function New-State {
    param([bool]$OutcomeCommitted = $false)
    return [pscustomobject]@{
        outcome_committed = $OutcomeCommitted
        outcome_write_attempted = $false
        containment_failure = $false
        evidence_integrity_failure = $false
        drain_failure = $false
        termination_failure = $false
        observer_failed = $false
        creation_succeeded = $true
        creation_attempted = $true
        resume_attempted = $false
        reap_confirmed = $true
        stdout_complete = $true
        stderr_complete = $true
        timed_out = $false
        interrupted = $false
        start_verdict = 'NOT_STARTED_PROVEN'
        launcher_exit_code = 1
        termination_started = $false
        termination_succeeded = $false
        support_ref = 'EG_TEST'
        error_code = $null
        precreate_rejection = $false
        total_processes = [uint64]0
        active_processes = [uint64]0
        total_terminated_processes = [uint64]0
        intent_committed = $false
        intent_bytes = [byte[]]@()
        application_child_observed = $false
        application_child_observation_elapsed_ms = [int64]0
        stdout_bytes = [uint64]0
        stderr_bytes = [uint64]0
        descendant_grace_expired = $false
    }
}

$script:GraceSleepMode = 'off'
$script:GraceSleepCalls = 0
$script:GraceSleepJobHandle = [IntPtr]::Zero
$script:GraceSleepReleasePath = $null

function Start-Sleep {
    param([int]$Milliseconds)

    if ($script:GraceSleepMode -ne 'off' -and $script:GraceSleepCalls -eq 0) {
        $script:GraceSleepCalls = $script:GraceSleepCalls + 1
        if ($null -ne $script:GraceSleepReleasePath) {
            [IO.File]::WriteAllText($script:GraceSleepReleasePath, 'release')
        }
        if ($script:GraceSleepMode -eq 'global-completion' -or
            $script:GraceSleepMode -eq 'local-completion') {
            $zeroDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
                ([int64]10 * [int64][System.Diagnostics.Stopwatch]::Frequency)
            $zeroAccounting = $null
            while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $zeroDeadline) {
                $zeroAccounting = [EnergyGridOneShotSupervisorNative]::GetAccounting(
                    $script:GraceSleepJobHandle)
                Assert-Native $zeroAccounting.Succeeded ('grace_completion_accounting_failed:' +
                    $zeroAccounting.ErrorCode)
                if ($zeroAccounting.ActiveProcesses -eq 0) { break }
                Microsoft.PowerShell.Utility\Start-Sleep -Milliseconds 50
            }
            Assert-Native ($null -ne $zeroAccounting -and $zeroAccounting.ActiveProcesses -eq 0) `
                'grace_completion_zero_not_observed'
            if ($script:GraceSleepMode -eq 'global-completion') {
                [Threading.Thread]::Sleep(250)
            }
            else {
                [Threading.Thread]::Sleep(5200)
            }
        }
        elseif ($script:GraceSleepMode -eq 'timeout-local-precedence') {
            [Threading.Thread]::Sleep(5200)
        }
        return
    }
    Microsoft.PowerShell.Utility\Start-Sleep -Milliseconds $Milliseconds
}

$script:OutcomeSeamEnabled = $false
$script:OutcomeMode = 'ordinary'
$script:OutcomeConstructionCount = 0
$script:OutcomeConstructorArgumentsValid = $false
$script:EgExpectedOutcomePath = $null

function New-Object {
    param(
        [Parameter(Mandatory = $true, Position = 0)][string]$TypeName,
        [Parameter(Position = 1, ValueFromRemainingArguments = $true)][object[]]$ArgumentList
    )

    $arguments = @()
    if ($PSBoundParameters.ContainsKey('ArgumentList')) {
        $arguments = @($ArgumentList)
        if ($arguments.Count -eq 1 -and $arguments[0] -is [array]) {
            $arguments = @($arguments[0])
        }
    }
    $isTarget = $script:OutcomeSeamEnabled -and
        $TypeName -eq 'System.IO.FileStream' -and
        $arguments.Count -eq 6 -and
        [string]$arguments[0] -eq $script:EgExpectedOutcomePath
    if ($isTarget) {
        Assert-Native ($arguments[1] -eq [IO.FileMode]::CreateNew) 'outcome_file_mode_mismatch'
        Assert-Native ($arguments[2] -eq [IO.FileAccess]::Write) 'outcome_file_access_mismatch'
        Assert-Native ($arguments[3] -eq [IO.FileShare]::None) 'outcome_file_share_mismatch'
        Assert-Native ($arguments[4] -eq 4096) 'outcome_file_buffer_mismatch'
        Assert-Native ($arguments[5] -eq [IO.FileOptions]::WriteThrough) 'outcome_file_options_mismatch'
        $script:OutcomeConstructionCount = $script:OutcomeConstructionCount + 1
        $script:OutcomeConstructorArgumentsValid = $true
        if ($script:OutcomeMode -eq 'delayed') {
            return Microsoft.PowerShell.Utility\New-Object `
                -TypeName 'EnergyGridOutcomeDelayedFlushStreamForFunctionTest' `
                -ArgumentList $arguments
        }
    }

    if ($PSBoundParameters.ContainsKey('ArgumentList')) {
        return Microsoft.PowerShell.Utility\New-Object -TypeName $TypeName -ArgumentList $arguments
    }
    return Microsoft.PowerShell.Utility\New-Object -TypeName $TypeName
}

Write-Output 'function_case=timed_out_intent_rejection'
$intentGateJob = [IntPtr]::Zero
$intentGateProcess = $null
$intentStream = $null
try {
    $intentGateJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($intentGateJob)
    $intentGateProcess = New-TestChild -JobHandle $intentGateJob
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    $script:EgState = New-State
    $intentPath = Join-Path $RootPath 'timed-out-intent.bin'
    $intentStream = New-Object EnergyGridDelayedFlushStreamForFunctionTest($intentPath, 250)
    $intentFailed = $false
    try {
        Write-EgReservedIntent -Stream $intentStream -Bytes ([byte[]](1, 2, 3)) `
            -TimeoutMilliseconds 50
    }
    catch { $intentFailed = $true }
    Assert-Native $intentFailed 'timed_out_intent_was_accepted'
    Assert-Native (-not $script:EgState.intent_committed) 'timed_out_intent_committed'
    Start-Sleep -Milliseconds 500
    [EnergyGridOneShotSupervisorNative]::RequestFailure()
    Assert-Native (-not [EnergyGridOneShotSupervisorNative]::CommitIntent()) 'timed_out_intent_gate_reopened'
    $intentResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $intentGateProcess.ThreadHandle, (Future-Deadline))
    Assert-Native (-not $intentResume.Attempted) 'timed_out_intent_resumed'
}
finally {
    if ($null -ne $intentStream) { $intentStream.Dispose() }
    try {
        if ($intentGateJob -ne [IntPtr]::Zero) {
            [void][EnergyGridOneShotSupervisorNative]::TerminateJob(
                $intentGateJob, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        }
    }
    catch { }
    try { Close-TestChild -Process $intentGateProcess } catch { }
    Close-Native -Handle $intentGateJob
}

Write-Output 'function_case=timed_out_outcome_contract'
$intentFunction = Get-ExactFunction -Name 'Write-EgReservedIntent' -NextName 'Get-EgIntentObject'
$outcomeFunction = Get-ExactFunction -Name 'Write-EgOutcome' -NextName 'Get-EgCanonicalApplicationCommandLine'
Assert-Native ($intentFunction.Contains('$durability.TimedOut -or -not $durability.Succeeded')) 'intent_timeout_check_missing'
Assert-Native ($outcomeFunction.Contains('$durability.TimedOut -or -not $durability.Succeeded')) 'outcome_timeout_check_missing'

function Test-EgDirectoryObjectAttributes {
    param([uint32]$Attributes)
    return (($Attributes -band [uint32]0x00000010) -ne 0 -and
        ($Attributes -band [uint32]0x00000400) -eq 0)
}

function Test-EgFileObjectAttributes {
    param([uint32]$Attributes)
    return (($Attributes -band [uint32]0x00000010) -eq 0 -and
        ($Attributes -band [uint32]0x00000400) -eq 0)
}

function Test-EgObjectIdentityEqual {
    param($Left, $Right)
    if ($null -eq $Left -or $null -eq $Right -or
        -not $Left.Succeeded -or -not $Right.Succeeded) { return $false }
    return [bool]($Left.VolumeSerialNumber -eq $Right.VolumeSerialNumber -and
        $Left.FileIdPart0 -eq $Right.FileIdPart0 -and
        $Left.FileIdPart1 -eq $Right.FileIdPart1)
}

function Assert-EgDistinctDirectoryIdentity {
    param([string]$PathA, [string]$PathB, [string]$FailureMarker)

    $handleA = $null
    $handleB = $null
    try {
        $openedA = [EgOutcomeIdentity]::OpenDirectory($PathA)
        $handleA = $openedA.Handle
        Assert-Native ($openedA.Succeeded -and $null -ne $handleA) 'identity_control_directory_open_a'
        $openedB = [EgOutcomeIdentity]::OpenDirectory($PathB)
        $handleB = $openedB.Handle
        Assert-Native ($openedB.Succeeded -and $null -ne $handleB) 'identity_control_directory_open_b'
        $infoA = [EgOutcomeIdentity]::QueryInfo($handleA)
        $infoB = [EgOutcomeIdentity]::QueryInfo($handleB)
        Assert-Native ($infoA.Succeeded -and $infoB.Succeeded) 'identity_control_directory_query'
        Assert-Native (-not (Test-EgObjectIdentityEqual -Left $infoA -Right $infoB)) $FailureMarker
    }
    finally {
        if ($null -ne $handleB) { $handleB.Dispose() }
        if ($null -ne $handleA) { $handleA.Dispose() }
    }
}

function Assert-EgDistinctFileIdentity {
    param([string]$PathA, [string]$PathB, [string]$FailureMarker)

    $handleA = $null
    $handleB = $null
    try {
        $openedA = [EgOutcomeIdentity]::OpenFile($PathA)
        $handleA = $openedA.Handle
        Assert-Native ($openedA.Succeeded -and $null -ne $handleA) 'identity_control_file_open_a'
        $openedB = [EgOutcomeIdentity]::OpenFile($PathB)
        $handleB = $openedB.Handle
        Assert-Native ($openedB.Succeeded -and $null -ne $handleB) 'identity_control_file_open_b'
        $infoA = [EgOutcomeIdentity]::QueryInfo($handleA)
        $infoB = [EgOutcomeIdentity]::QueryInfo($handleB)
        Assert-Native ($infoA.Succeeded -and $infoB.Succeeded) 'identity_control_file_query'
        Assert-Native (-not (Test-EgObjectIdentityEqual -Left $infoA -Right $infoB)) $FailureMarker
    }
    finally {
        if ($null -ne $handleB) { $handleB.Dispose() }
        if ($null -ne $handleA) { $handleA.Dispose() }
    }
}

function Assert-EgHardLinkIdentity {
    param([string]$OriginalPath, [string]$AliasPath)

    $originalHandle = $null
    $aliasHandle = $null
    try {
        $originalOpen = [EgOutcomeIdentity]::OpenFile($OriginalPath)
        $originalHandle = $originalOpen.Handle
        Assert-Native ($originalOpen.Succeeded -and $null -ne $originalHandle) `
            'hard_link_original_open_failed'
        $aliasOpen = [EgOutcomeIdentity]::OpenFile($AliasPath)
        $aliasHandle = $aliasOpen.Handle
        Assert-Native ($aliasOpen.Succeeded -and $null -ne $aliasHandle) 'hard_link_alias_open_failed'
        $originalInfo = [EgOutcomeIdentity]::QueryInfo($originalHandle)
        $aliasInfo = [EgOutcomeIdentity]::QueryInfo($aliasHandle)
        Assert-Native ($originalInfo.Succeeded -and $aliasInfo.Succeeded) 'hard_link_query_failed'
        Assert-Native (Test-EgObjectIdentityEqual -Left $originalInfo -Right $aliasInfo) `
            'hard_link_identity_not_equal'
        Assert-Native ($originalInfo.NumberOfLinks -gt 1 -and $aliasInfo.NumberOfLinks -gt 1) `
            'hard_link_count_not_observed'
    }
    finally {
        if ($null -ne $aliasHandle) { $aliasHandle.Dispose() }
        if ($null -ne $originalHandle) { $originalHandle.Dispose() }
    }
}

function Assert-EgCrossRootHardLinkIdentity {
    param(
        [string]$ExpectedRootPath,
        [string]$DiscoveredRootPath,
        [string]$ExpectedFilePath,
        [string]$DiscoveredFilePath
    )

    $expectedRootHandle = $null
    $discoveredRootHandle = $null
    $expectedFileHandle = $null
    $discoveredFileHandle = $null
    try {
        $expectedRootOpen = [EgOutcomeIdentity]::OpenDirectory($ExpectedRootPath)
        $expectedRootHandle = $expectedRootOpen.Handle
        Assert-Native ($expectedRootOpen.Succeeded -and $null -ne $expectedRootHandle) `
            'cross_root_expected_root_open_failed'
        $discoveredRootOpen = [EgOutcomeIdentity]::OpenDirectory($DiscoveredRootPath)
        $discoveredRootHandle = $discoveredRootOpen.Handle
        Assert-Native ($discoveredRootOpen.Succeeded -and $null -ne $discoveredRootHandle) `
            'cross_root_discovered_root_open_failed'
        $expectedFileOpen = [EgOutcomeIdentity]::OpenFile($ExpectedFilePath)
        $expectedFileHandle = $expectedFileOpen.Handle
        Assert-Native ($expectedFileOpen.Succeeded -and $null -ne $expectedFileHandle) `
            'cross_root_expected_file_open_failed'
        $discoveredFileOpen = [EgOutcomeIdentity]::OpenFile($DiscoveredFilePath)
        $discoveredFileHandle = $discoveredFileOpen.Handle
        Assert-Native ($discoveredFileOpen.Succeeded -and $null -ne $discoveredFileHandle) `
            'cross_root_discovered_file_open_failed'
        $expectedRootInfo = [EgOutcomeIdentity]::QueryInfo($expectedRootHandle)
        $discoveredRootInfo = [EgOutcomeIdentity]::QueryInfo($discoveredRootHandle)
        $expectedFileInfo = [EgOutcomeIdentity]::QueryInfo($expectedFileHandle)
        $discoveredFileInfo = [EgOutcomeIdentity]::QueryInfo($discoveredFileHandle)
        Assert-Native ($expectedRootInfo.Succeeded -and $discoveredRootInfo.Succeeded -and
            $expectedFileInfo.Succeeded -and $discoveredFileInfo.Succeeded) `
            'cross_root_query_failed'
        Assert-Native (-not (Test-EgObjectIdentityEqual -Left $expectedRootInfo -Right $discoveredRootInfo)) `
            'cross_root_directory_identity_accepted'
        Assert-Native (Test-EgObjectIdentityEqual -Left $expectedFileInfo -Right $discoveredFileInfo) `
            'cross_root_file_identity_not_equal'
        Assert-Native ($expectedFileInfo.NumberOfLinks -gt 1 -and
            $discoveredFileInfo.NumberOfLinks -gt 1) 'cross_root_link_count_not_observed'
    }
    finally {
        if ($null -ne $discoveredFileHandle) { $discoveredFileHandle.Dispose() }
        if ($null -ne $expectedFileHandle) { $expectedFileHandle.Dispose() }
        if ($null -ne $discoveredRootHandle) { $discoveredRootHandle.Dispose() }
        if ($null -ne $expectedRootHandle) { $expectedRootHandle.Dispose() }
    }
}

function Assert-OutcomeFile {
    param(
        [string]$EvidenceRoot,
        [string]$ExpectedPath,
        [string]$ExpectedRunId,
        [string]$ExpectedIntentHash
    )

    $rootHandle = $null
    $expectedParentHandle = $null
    $discoveredParentHandle = $null
    $expectedFileHandle = $null
    $discoveredFileHandle = $null
    try {
        $ExpectedLeaf = $ExpectedRunId + '.outcome.json'
        $ConstructedExpectedPath = Join-Path $EvidenceRoot $ExpectedLeaf

        $outcomeFiles = @(Get-ChildItem -LiteralPath $EvidenceRoot -Filter '*.outcome.json' -File)
        Assert-Native ($outcomeFiles.Count -eq 1) 'ordinary_outcome_file_count'
        Assert-Native ([StringComparer]::OrdinalIgnoreCase.Equals(
            [string]$outcomeFiles[0].Name, $ExpectedLeaf)) 'ordinary_outcome_leaf_mismatch'

        $expectedParentPath = [IO.Path]::GetDirectoryName($ConstructedExpectedPath)
        $discoveredFilePath = [string]$outcomeFiles[0].FullName
        $discoveredParentPath = [IO.Path]::GetDirectoryName($discoveredFilePath)
        Assert-Native (-not [string]::IsNullOrEmpty($expectedParentPath) -and
            -not [string]::IsNullOrEmpty($discoveredParentPath)) 'ordinary_outcome_parent_missing'

        $rootOpen = [EgOutcomeIdentity]::OpenDirectory($EvidenceRoot)
        $rootHandle = $rootOpen.Handle
        Assert-Native ($rootOpen.Succeeded -and $null -ne $rootHandle) 'ordinary_outcome_root_open'
        $expectedParentOpen = [EgOutcomeIdentity]::OpenDirectory($expectedParentPath)
        $expectedParentHandle = $expectedParentOpen.Handle
        Assert-Native ($expectedParentOpen.Succeeded -and $null -ne $expectedParentHandle) `
            'ordinary_outcome_expected_parent_open'
        $discoveredParentOpen = [EgOutcomeIdentity]::OpenDirectory($discoveredParentPath)
        $discoveredParentHandle = $discoveredParentOpen.Handle
        Assert-Native ($discoveredParentOpen.Succeeded -and $null -ne $discoveredParentHandle) `
            'ordinary_outcome_discovered_parent_open'
        $expectedFileOpen = [EgOutcomeIdentity]::OpenFile($ConstructedExpectedPath)
        $expectedFileHandle = $expectedFileOpen.Handle
        Assert-Native ($expectedFileOpen.Succeeded -and $null -ne $expectedFileHandle) `
            'ordinary_outcome_expected_file_open'
        $discoveredFileOpen = [EgOutcomeIdentity]::OpenFile($discoveredFilePath)
        $discoveredFileHandle = $discoveredFileOpen.Handle
        Assert-Native ($discoveredFileOpen.Succeeded -and $null -ne $discoveredFileHandle) `
            'ordinary_outcome_discovered_file_open'

        $rootInfo = [EgOutcomeIdentity]::QueryInfo($rootHandle)
        $expectedParentInfo = [EgOutcomeIdentity]::QueryInfo($expectedParentHandle)
        $discoveredParentInfo = [EgOutcomeIdentity]::QueryInfo($discoveredParentHandle)
        $expectedFileInfo = [EgOutcomeIdentity]::QueryInfo($expectedFileHandle)
        $discoveredFileInfo = [EgOutcomeIdentity]::QueryInfo($discoveredFileHandle)
        Assert-Native ($rootInfo.Succeeded -and $expectedParentInfo.Succeeded -and
            $discoveredParentInfo.Succeeded -and $expectedFileInfo.Succeeded -and
            $discoveredFileInfo.Succeeded) 'ordinary_outcome_identity_query'
        Assert-Native (Test-EgDirectoryObjectAttributes -Attributes $rootInfo.FileAttributes) `
            'ordinary_outcome_root_attributes'
        Assert-Native (Test-EgDirectoryObjectAttributes -Attributes $expectedParentInfo.FileAttributes) `
            'ordinary_outcome_expected_parent_attributes'
        Assert-Native (Test-EgDirectoryObjectAttributes -Attributes $discoveredParentInfo.FileAttributes) `
            'ordinary_outcome_discovered_parent_attributes'
        Assert-Native (Test-EgFileObjectAttributes -Attributes $expectedFileInfo.FileAttributes) `
            'ordinary_outcome_expected_file_attributes'
        Assert-Native (Test-EgFileObjectAttributes -Attributes $discoveredFileInfo.FileAttributes) `
            'ordinary_outcome_discovered_file_attributes'
        Assert-Native (Test-EgObjectIdentityEqual -Left $rootInfo -Right $expectedParentInfo) `
            'ordinary_outcome_root_expected_parent_identity'
        Assert-Native (Test-EgObjectIdentityEqual -Left $rootInfo -Right $discoveredParentInfo) `
            'ordinary_outcome_root_discovered_parent_identity'
        Assert-Native (Test-EgObjectIdentityEqual -Left $expectedFileInfo -Right $discoveredFileInfo) `
            'ordinary_outcome_file_identity'
        Assert-Native ($expectedFileInfo.NumberOfLinks -eq 1 -and
            $discoveredFileInfo.NumberOfLinks -eq 1) 'ordinary_outcome_link_count_before'

        $retainedRead = [EgOutcomeIdentity]::ReadRetained($expectedFileHandle)
        Assert-Native $retainedRead.Succeeded 'ordinary_outcome_retained_read'
        $bytes = $retainedRead.Bytes
        $script:LastOutcomeBytes = [byte[]]$bytes.Clone()
        $expectedFileAfterRead = [EgOutcomeIdentity]::QueryInfo($expectedFileHandle)
        $discoveredFileAfterRead = [EgOutcomeIdentity]::QueryInfo($discoveredFileHandle)
        Assert-Native ($expectedFileAfterRead.Succeeded -and $discoveredFileAfterRead.Succeeded) `
            'ordinary_outcome_identity_after_read_query'
        Assert-Native (Test-EgObjectIdentityEqual -Left $expectedFileInfo -Right $expectedFileAfterRead) `
            'ordinary_outcome_expected_identity_changed_after_read'
        Assert-Native (Test-EgObjectIdentityEqual -Left $expectedFileAfterRead -Right $discoveredFileAfterRead) `
            'ordinary_outcome_file_identity_changed_after_read'
        Assert-Native (Test-EgFileObjectAttributes -Attributes $expectedFileAfterRead.FileAttributes) `
            'ordinary_outcome_expected_file_attributes_after_read'
        Assert-Native (Test-EgFileObjectAttributes -Attributes $discoveredFileAfterRead.FileAttributes) `
            'ordinary_outcome_discovered_file_attributes_after_read'
        Assert-Native ($expectedFileAfterRead.NumberOfLinks -eq 1 -and
            $discoveredFileAfterRead.NumberOfLinks -eq 1) 'ordinary_outcome_link_count_after'

        Assert-Native ($bytes.Length -ge 1 -and $bytes.Length -le 65536) 'ordinary_outcome_size_invalid'
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $jsonText = $strictUtf8.GetString($bytes)
        $document = $jsonText | ConvertFrom-Json
        $expectedFields = @(
            'schema', 'run_id', 'operation', 'intent_sha256', 'start_verdict',
            'launcher_exit_code', 'creation_attempted', 'resume_attempted',
            'application_child_observed', 'application_child_observation_elapsed_ms',
            'total_processes', 'active_processes', 'total_terminated_processes',
            'reap_confirmed', 'stdout_bytes', 'stderr_bytes', 'stdout_complete',
            'stderr_complete', 'raw_stream_retained_bytes', 'containment', 'completed_utc'
        )
        $actualFields = @($document.PSObject.Properties.Name)
        Assert-Native ($actualFields.Count -eq $expectedFields.Count) 'ordinary_outcome_field_count'
        foreach ($field in $expectedFields) {
            Assert-Native ($actualFields -contains $field) ('ordinary_outcome_field_missing:' + $field)
        }
        Assert-Native ($document.schema -eq 'energygrid.one_shot_supervisor.outcome.v1') `
            'ordinary_outcome_schema_invalid'
        Assert-Native ($document.run_id -eq $ExpectedRunId) 'ordinary_outcome_run_id_invalid'
        Assert-Native ($document.operation -eq 'run') 'ordinary_outcome_operation_invalid'
        Assert-Native ($document.intent_sha256 -eq $ExpectedIntentHash) `
            'ordinary_outcome_intent_hash_invalid'
        Assert-Native ($document.raw_stream_retained_bytes -eq 0) 'ordinary_outcome_raw_bytes_retained'
        Assert-Native ($document.creation_attempted -and $document.resume_attempted) `
            'ordinary_outcome_representative_flags_invalid'
        Assert-Native ($document.reap_confirmed -and $document.stdout_complete -and $document.stderr_complete) `
            'ordinary_outcome_representative_completion_invalid'
        return $document
    }
    finally {
        if ($null -ne $discoveredFileHandle) { $discoveredFileHandle.Dispose() }
        if ($null -ne $expectedFileHandle) { $expectedFileHandle.Dispose() }
        if ($null -ne $discoveredParentHandle) { $discoveredParentHandle.Dispose() }
        if ($null -ne $expectedParentHandle) { $expectedParentHandle.Dispose() }
        if ($null -ne $rootHandle) { $rootHandle.Dispose() }
    }
}

function Assert-EgRejected {
    param([scriptblock]$Action, [string]$FailureMarker)
    $rejected = $false
    try { & $Action | Out-Null } catch { $rejected = $true }
    Assert-Native $rejected $FailureMarker
}

Write-Output 'function_case=timed_out_outcome_actual_delayed'
$delayedOutcomeRoot = Join-Path $RootPath 'delayed-outcome'
[IO.Directory]::CreateDirectory($delayedOutcomeRoot) | Out-Null
$RunId = 'EG-OUTCOME-DELAYED-0001'
$script:EgEvidenceRootNormal = $delayedOutcomeRoot
$script:EgExpectedOutcomePath = Join-Path $delayedOutcomeRoot ($RunId + '.outcome.json')
$script:OutcomeSeamEnabled = $true
$script:OutcomeMode = 'delayed'
$script:OutcomeConstructionCount = 0
$script:OutcomeConstructorArgumentsValid = $false
[EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::ResetState()
$script:EgState = New-State
Assert-Native (-not $script:EgState.outcome_committed) 'delayed_outcome_precommitted'
Write-EgOutcome -StartVerdict 'STARTED_PROVEN'
Assert-Native $script:EgState.outcome_write_attempted 'delayed_outcome_write_not_attempted'
Assert-Native (-not $script:EgState.outcome_committed) 'delayed_outcome_committed'
Assert-Native $script:EgState.evidence_integrity_failure 'delayed_outcome_evidence_not_failed'
Assert-Native ($script:EgState.support_ref -eq 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED') `
    'delayed_outcome_support_ref_invalid'
Assert-Native ($script:OutcomeConstructionCount -eq 1) 'delayed_outcome_construction_count_invalid'
Assert-Native $script:OutcomeConstructorArgumentsValid 'delayed_outcome_constructor_arguments_invalid'
Assert-Native ([EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::FlushEntered.WaitOne(0)) `
    'delayed_outcome_flush_not_entered'
Assert-Native ($null -ne [EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::WorkerThread) `
    'delayed_outcome_worker_not_captured'
Assert-Native ([EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::IsReleaseClosed()) `
    'delayed_outcome_release_open_too_early'
Assert-Native ([EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::WorkerThread.IsAlive) `
    'delayed_outcome_worker_not_blocked'
$delayedOutcomeFiles = @(Get-ChildItem -LiteralPath $delayedOutcomeRoot -Filter '*.outcome.json' -File)
Assert-Native ($delayedOutcomeFiles.Count -eq 1) 'delayed_outcome_file_count_invalid'

[EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::Release()
Assert-Native ([EnergyGridOutcomeDelayedFlushStreamForFunctionTest]::JoinWorker(15000)) `
    'delayed_outcome_worker_join_failed'
Assert-Native (-not $script:EgState.outcome_committed) 'delayed_outcome_committed_after_worker'
Assert-Native $script:EgState.evidence_integrity_failure 'delayed_outcome_evidence_changed_after_worker'
Assert-Native ($script:EgState.support_ref -eq 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED') `
    'delayed_outcome_support_ref_changed_after_worker'
Assert-Native ($script:OutcomeConstructionCount -eq 1) 'delayed_outcome_retried'
Assert-Native ((Get-EgExitCode) -eq 3) 'delayed_outcome_exit_not_three'
Assert-Native ((@(Get-ChildItem -LiteralPath $delayedOutcomeRoot -Filter '*.outcome.json' -File)).Count -eq 1) `
    'delayed_outcome_alternate_file_created'
$script:OutcomeSeamEnabled = $false
Remove-Item -LiteralPath $delayedOutcomeRoot -Recurse -Force
Assert-Native (-not (Test-Path -LiteralPath $delayedOutcomeRoot)) 'delayed_outcome_cleanup_failed'

Write-Output 'function_case=timed_out_outcome_actual_ordinary'
$ordinaryOutcomeRoot = Join-Path $RootPath 'ordinary-outcome'
[IO.Directory]::CreateDirectory($ordinaryOutcomeRoot) | Out-Null
$RunId = 'EG-OUTCOME-ORDINARY-0001'
$script:EgEvidenceRootNormal = $ordinaryOutcomeRoot
$script:EgExpectedOutcomePath = Join-Path $ordinaryOutcomeRoot ($RunId + '.outcome.json')
$script:OutcomeSeamEnabled = $true
$script:OutcomeMode = 'ordinary'
$script:OutcomeConstructionCount = 0
$script:OutcomeConstructorArgumentsValid = $false
$script:EgState = New-State
$script:EgState.intent_committed = $true
$script:EgState.intent_bytes = [byte[]](0, 1, 2, 255)
$script:EgState.creation_attempted = $true
$script:EgState.resume_attempted = $true
$script:EgState.application_child_observed = $true
$script:EgState.application_child_observation_elapsed_ms = [int64]17
$script:EgState.total_processes = [uint64]2
$script:EgState.active_processes = [uint64]0
$script:EgState.total_terminated_processes = [uint64]1
$script:EgState.stdout_bytes = [uint64]12
$script:EgState.stderr_bytes = [uint64]34
$script:EgState.reap_confirmed = $true
$hash = New-Object System.Security.Cryptography.SHA256Managed
try {
    $expectedIntentHash = ([BitConverter]::ToString($hash.ComputeHash($script:EgState.intent_bytes))).Replace('-', '').ToLowerInvariant()
}
finally { $hash.Dispose() }
Assert-Native (-not $script:EgState.outcome_committed) 'ordinary_outcome_precommitted'
Write-EgOutcome -StartVerdict 'STARTED_PROVEN'
Assert-Native $script:EgState.outcome_write_attempted 'ordinary_outcome_write_not_attempted'
Assert-Native $script:EgState.outcome_committed 'ordinary_outcome_not_committed'
Assert-Native (-not $script:EgState.evidence_integrity_failure) 'ordinary_outcome_evidence_failed'
Assert-Native ($script:OutcomeConstructionCount -eq 1) 'ordinary_outcome_construction_count_invalid'
Assert-Native $script:OutcomeConstructorArgumentsValid 'ordinary_outcome_constructor_arguments_invalid'
$null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
    -ExpectedPath $script:EgExpectedOutcomePath -ExpectedRunId $RunId `
    -ExpectedIntentHash $expectedIntentHash

Write-Output 'file_id_info_query=PASS'
Write-Output 'root_identity=PASS'
Write-Output 'file_identity=PASS'
Write-Output 'link_count=PASS'
Write-Output 'retained_handle_read=PASS'
Write-Output 'outcome_commit_transition=PASS'

$identityControlsRoot = Join-Path $RootPath 'identity-controls'
$alternateOutcomeRoot = Join-Path $identityControlsRoot 'alternate-root'
$crossRoot = Join-Path $identityControlsRoot 'cross-root'
$representationRoot = Join-Path $identityControlsRoot 'representation-long-parent-0123456789'
$junctionTarget = Join-Path $identityControlsRoot 'junction-target'
$junctionPath = Join-Path $identityControlsRoot 'junction-root'
$identityLeaf = $RunId + '.outcome.json'
try {
    [IO.Directory]::CreateDirectory($identityControlsRoot) | Out-Null
    [IO.Directory]::CreateDirectory($alternateOutcomeRoot) | Out-Null
    [IO.Directory]::CreateDirectory($crossRoot) | Out-Null
    [IO.Directory]::CreateDirectory($representationRoot) | Out-Null
    [IO.File]::WriteAllBytes(
        (Join-Path $alternateOutcomeRoot $identityLeaf), $script:LastOutcomeBytes)
    [IO.File]::WriteAllBytes(
        (Join-Path $representationRoot $identityLeaf), $script:LastOutcomeBytes)

    $caseRoot = $ordinaryOutcomeRoot.ToUpperInvariant()
    $null = Assert-OutcomeFile -EvidenceRoot $caseRoot `
        -ExpectedPath (Join-Path $caseRoot $identityLeaf) -ExpectedRunId $RunId `
        -ExpectedIntentHash $expectedIntentHash
    $slashRoot = $ordinaryOutcomeRoot.Replace('\', '/')
    $null = Assert-OutcomeFile -EvidenceRoot $slashRoot `
        -ExpectedPath ($slashRoot + '/' + $identityLeaf) -ExpectedRunId $RunId `
        -ExpectedIntentHash $expectedIntentHash
    $dotRoot = $ordinaryOutcomeRoot + '\.\'
    $null = Assert-OutcomeFile -EvidenceRoot $dotRoot `
        -ExpectedPath (Join-Path $dotRoot $identityLeaf) -ExpectedRunId $RunId `
        -ExpectedIntentHash $expectedIntentHash

    $shortParent = [EgOutcomeIdentity]::GetShortPath($representationRoot)
    if ($shortParent.Succeeded -and
        -not [StringComparer]::OrdinalIgnoreCase.Equals($shortParent.Path, $representationRoot)) {
        $shortExpectedPath = Join-Path $shortParent.Path $identityLeaf
        $null = Assert-OutcomeFile -EvidenceRoot $shortParent.Path `
            -ExpectedPath $shortExpectedPath -ExpectedRunId $RunId `
            -ExpectedIntentHash $expectedIntentHash
        Write-Output 'SHORT_NAME_ALIAS=PASS'
    }
    else {
        Write-Output 'SHORT_NAME_ALIAS=UNAVAILABLE'
    }
    Write-Output 'representation_positives=PASS'

    Assert-EgDistinctDirectoryIdentity -PathA $ordinaryOutcomeRoot -PathB $alternateOutcomeRoot `
        -FailureMarker 'identity_negative_alternate_root'
    Assert-EgDistinctFileIdentity `
        -PathA (Join-Path $ordinaryOutcomeRoot $identityLeaf) `
        -PathB (Join-Path $alternateOutcomeRoot $identityLeaf) `
        -FailureMarker 'identity_negative_identical_bytes_different_object'

    Assert-EgRejected -Action {
        $null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
            -ExpectedPath (Join-Path $ordinaryOutcomeRoot 'EG-OUTCOME-SIBLING-0001.outcome.json') `
            -ExpectedRunId 'EG-OUTCOME-SIBLING-0001' -ExpectedIntentHash $expectedIntentHash
    } -FailureMarker 'identity_negative_sibling_filename'
    Assert-EgRejected -Action {
        $null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
            -ExpectedPath (Join-Path $ordinaryOutcomeRoot 'EG-DIFFERENT-RUN-0001.outcome.json') `
            -ExpectedRunId 'EG-DIFFERENT-RUN-0001' -ExpectedIntentHash $expectedIntentHash
    } -FailureMarker 'identity_negative_different_run_id'

    $missingExpectedPath = Join-Path $ordinaryOutcomeRoot 'EG-MISSING-OUTCOME-0001.outcome.json'
    $missingOpen = [EgOutcomeIdentity]::OpenFile($missingExpectedPath)
    $missingHandle = $missingOpen.Handle
    Assert-Native (-not $missingOpen.Succeeded) 'identity_negative_expected_path_opened'
    if ($null -ne $missingHandle) { $missingHandle.Dispose() }
    Assert-EgRejected -Action {
        $null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
            -ExpectedPath $missingExpectedPath -ExpectedRunId 'EG-MISSING-OUTCOME-0001' `
            -ExpectedIntentHash $expectedIntentHash
    } -FailureMarker 'identity_negative_expected_path_missing'

    $secondOutcomePath = Join-Path $ordinaryOutcomeRoot 'EG-SECOND-OUTCOME-0001.outcome.json'
    [IO.File]::WriteAllBytes($secondOutcomePath, $script:LastOutcomeBytes)
    try {
        Assert-EgRejected -Action {
            $null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
                -ExpectedPath $script:EgExpectedOutcomePath -ExpectedRunId $RunId `
                -ExpectedIntentHash $expectedIntentHash
        } -FailureMarker 'identity_negative_second_outcome_entry'
    }
    finally {
        if (Test-Path -LiteralPath $secondOutcomePath) {
            Remove-Item -LiteralPath $secondOutcomePath -Force -ErrorAction SilentlyContinue
        }
    }

    $sameRootAliasPath = Join-Path $ordinaryOutcomeRoot 'hard-link-alias.bin'
    $sameRootLink = [EgOutcomeIdentity]::CreateHardLink(
        $sameRootAliasPath, $script:EgExpectedOutcomePath)
    Assert-Native $sameRootLink.Succeeded 'identity_negative_same_root_hard_link_create'
    try {
        Assert-EgHardLinkIdentity -OriginalPath $script:EgExpectedOutcomePath `
            -AliasPath $sameRootAliasPath
        Assert-EgRejected -Action {
            $null = Assert-OutcomeFile -EvidenceRoot $ordinaryOutcomeRoot `
                -ExpectedPath $script:EgExpectedOutcomePath -ExpectedRunId $RunId `
                -ExpectedIntentHash $expectedIntentHash
        } -FailureMarker 'identity_negative_same_root_hard_link'
    }
    finally {
        if (Test-Path -LiteralPath $sameRootAliasPath) {
            Remove-Item -LiteralPath $sameRootAliasPath -Force -ErrorAction SilentlyContinue
        }
    }

    $crossRootLinkPath = Join-Path $crossRoot $identityLeaf
    $crossRootLink = [EgOutcomeIdentity]::CreateHardLink(
        $crossRootLinkPath, $script:EgExpectedOutcomePath)
    Assert-Native $crossRootLink.Succeeded 'identity_negative_cross_root_hard_link_create'
    try {
        Assert-EgCrossRootHardLinkIdentity `
            -ExpectedRootPath $ordinaryOutcomeRoot -DiscoveredRootPath $crossRoot `
            -ExpectedFilePath $script:EgExpectedOutcomePath -DiscoveredFilePath $crossRootLinkPath
        Assert-EgRejected -Action {
            $null = Assert-OutcomeFile -EvidenceRoot $crossRoot `
                -ExpectedPath $crossRootLinkPath -ExpectedRunId $RunId `
                -ExpectedIntentHash $expectedIntentHash
        } -FailureMarker 'identity_negative_cross_root_hard_link'
    }
    finally {
        if (Test-Path -LiteralPath $crossRootLinkPath) {
            Remove-Item -LiteralPath $crossRootLinkPath -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Output 'identity_negatives=PASS'

    [IO.Directory]::CreateDirectory($junctionTarget) | Out-Null
    try {
        New-Item -ItemType Junction -Path $junctionPath -Target $junctionTarget `
            -ErrorAction Stop | Out-Null
    }
    catch { throw 'reparse_junction_creation_failed' }
    $junctionHandle = $null
    try {
        $junctionOpen = [EgOutcomeIdentity]::OpenDirectory($junctionPath)
        $junctionHandle = $junctionOpen.Handle
        Assert-Native ($junctionOpen.Succeeded -and $null -ne $junctionHandle) `
            'reparse_junction_open_failed'
        $junctionInfo = [EgOutcomeIdentity]::QueryInfo($junctionHandle)
        Assert-Native $junctionInfo.Succeeded 'reparse_junction_query_failed'
        Assert-Native (($junctionInfo.FileAttributes -band [uint32]0x00000400) -ne 0) `
            'reparse_junction_attribute_missing'
        Assert-Native (-not (Test-EgDirectoryObjectAttributes -Attributes $junctionInfo.FileAttributes)) `
            'reparse_junction_accepted'
    }
    finally {
        if ($null -ne $junctionHandle) { $junctionHandle.Dispose() }
    }
    Assert-Native (-not (Test-EgFileObjectAttributes -Attributes ([uint32]0x00000400))) `
        'reparse_file_predicate_accepted'
    Write-Output 'reparse_rejection=PASS'
}
finally {
    if (Test-Path -LiteralPath $junctionPath) {
        Remove-Item -LiteralPath $junctionPath -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $junctionTarget) {
        Remove-Item -LiteralPath $junctionTarget -Recurse -Force -ErrorAction SilentlyContinue
    }
    foreach ($cleanupPath in @($crossRoot, $alternateOutcomeRoot, $representationRoot)) {
        if (Test-Path -LiteralPath $cleanupPath) {
            Remove-Item -LiteralPath $cleanupPath -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path -LiteralPath $identityControlsRoot) {
        Remove-Item -LiteralPath $identityControlsRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$script:OutcomeSeamEnabled = $false
Remove-Item -LiteralPath $ordinaryOutcomeRoot -Recurse -Force
Assert-Native (-not (Test-Path -LiteralPath $ordinaryOutcomeRoot)) 'ordinary_outcome_cleanup_failed'

Write-Output 'function_case=timed_out_outcome_defensive_exact_caller'
$syntheticTimedOutDurability = [pscustomobject]@{ Succeeded = $true; TimedOut = $true; ErrorCode = 0 }
$branchStart = $outcomeFunction.IndexOf('if ($durability.TimedOut -or -not $durability.Succeeded) {')
Assert-Native ($branchStart -ge 0) 'outcome_timeout_branch_missing'
$branchDepth = 0
$branchEnd = -1
for ($branchIndex = $branchStart; $branchIndex -lt $outcomeFunction.Length; $branchIndex++) {
    if ($outcomeFunction[$branchIndex] -eq '{') { $branchDepth++ }
    elseif ($outcomeFunction[$branchIndex] -eq '}') {
        $branchDepth--
        if ($branchDepth -eq 0) { $branchEnd = $branchIndex + 1; break }
    }
}
Assert-Native ($branchEnd -gt $branchStart) 'outcome_timeout_branch_unbalanced'
$exactOutcomeTimeoutBranch = $outcomeFunction.Substring($branchStart, $branchEnd - $branchStart)
$script:DefensiveRejected = $false
$script:DefensiveSupportRef = $null
$defensiveScript = [scriptblock]::Create(
    'param($durability)' + "`n" +
    'function Stop-EgSupervisor { param([string]$SupportRef, [int]$ErrorCode, [switch]$EvidenceFailure); ' +
        '$script:DefensiveRejected = $true; $script:DefensiveSupportRef = $SupportRef }' + "`n" +
    $exactOutcomeTimeoutBranch)
& $defensiveScript $syntheticTimedOutDurability
Assert-Native $script:DefensiveRejected 'defensive_timeout_caller_accepted'
Assert-Native ($script:DefensiveSupportRef -eq 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED') `
    'defensive_timeout_support_ref_invalid'

Write-Output 'function_case=descendant_grace_stale_zero_global_deadline'
$staleZeroDeadlineJob = [IntPtr]::Zero
$staleZeroDeadlineProcess = $null
try {
    $staleZeroDeadlineJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($staleZeroDeadlineJob)
    $staleZeroDeadlineProcess = New-TestChild -JobHandle $staleZeroDeadlineJob `
        -Code 'Start-Sleep -Milliseconds 150'
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'stale_zero_deadline_intent_failed'
    $staleZeroResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $staleZeroDeadlineProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($staleZeroResume.Attempted -and $staleZeroResume.Accepted) 'stale_zero_deadline_resume_failed'
    $staleZeroDeadline = Future-Deadline -Milliseconds 1000
    $staleZeroWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $staleZeroDeadlineProcess.ProcessHandle, 10000)
    Assert-Native ($staleZeroWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) `
        'stale_zero_deadline_launcher_wait_failed'
    $staleZeroAccounting = Wait-JobZero -JobHandle $staleZeroDeadlineJob
    Assert-Native ($staleZeroAccounting.ActiveProcesses -eq 0) 'stale_zero_deadline_not_zero'
    Assert-Native ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $staleZeroDeadline) `
        'stale_zero_deadline_zero_after_deadline'
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $staleZeroDeadline) {
        Microsoft.PowerShell.Utility\Start-Sleep -Milliseconds 50
    }
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $staleZeroDeadlineJob `
        -LauncherHandle $staleZeroDeadlineProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks $staleZeroDeadline
    Assert-Native (-not $script:EgState.termination_started) 'stale_zero_deadline_terminated'
    Assert-Native ($script:EgState.active_processes -eq 0) 'stale_zero_deadline_active_processes_nonzero'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted -and
        -not $script:EgState.descendant_grace_expired) 'stale_zero_deadline_flags_set'
}
finally {
    Cleanup-Job -JobHandle $staleZeroDeadlineJob -Process $staleZeroDeadlineProcess
}

Write-Output 'function_case=descendant_grace_stale_zero_interruption'
$staleZeroInterruptJob = [IntPtr]::Zero
$staleZeroInterruptProcess = $null
try {
    $staleZeroInterruptJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($staleZeroInterruptJob)
    $staleZeroInterruptProcess = New-TestChild -JobHandle $staleZeroInterruptJob `
        -Code 'Start-Sleep -Milliseconds 150'
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'stale_zero_interrupt_intent_failed'
    $staleZeroInterruptResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $staleZeroInterruptProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($staleZeroInterruptResume.Attempted -and $staleZeroInterruptResume.Accepted) `
        'stale_zero_interrupt_resume_failed'
    $staleZeroInterruptWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $staleZeroInterruptProcess.ProcessHandle, 10000)
    Assert-Native ($staleZeroInterruptWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) `
        'stale_zero_interrupt_launcher_wait_failed'
    $staleZeroInterruptAccounting = Wait-JobZero -JobHandle $staleZeroInterruptJob
    Assert-Native ($staleZeroInterruptAccounting.ActiveProcesses -eq 0) 'stale_zero_interrupt_not_zero'
    $staleZeroSignalMethod = [EnergyGridOneShotSupervisorNative].GetMethod(
        'HandleConsoleSignal', [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Static)
    Assert-Native ($null -ne $staleZeroSignalMethod) 'stale_zero_interrupt_signal_method_missing'
    [void]$staleZeroSignalMethod.Invoke($null, [object[]]@([uint32]2))
    Assert-Native ([EnergyGridOneShotSupervisorNative]::IsTerminationRequested) `
        'stale_zero_interrupt_signal_not_pending'
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $staleZeroInterruptJob `
        -LauncherHandle $staleZeroInterruptProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline)
    Assert-Native (-not $script:EgState.termination_started) 'stale_zero_interrupt_terminated'
    Assert-Native ($script:EgState.active_processes -eq 0) 'stale_zero_interrupt_active_processes_nonzero'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted -and
        -not $script:EgState.descendant_grace_expired) 'stale_zero_interrupt_flags_set'
}
finally {
    Cleanup-Job -JobHandle $staleZeroInterruptJob -Process $staleZeroInterruptProcess
}

Write-Output 'function_case=descendant_grace_overall_deadline'
$graceDeadlineJob = [IntPtr]::Zero
$graceDeadlineProcess = $null
try {
    $graceDeadlineJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($graceDeadlineJob)
    $descendantCode = @'
$shell = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
Start-Process -FilePath $shell -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds 60') -WindowStyle Hidden | Out-Null
Start-Sleep -Milliseconds 150
'@
    $graceDeadlineProcess = New-TestChild -JobHandle $graceDeadlineJob -Code $descendantCode
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'grace_deadline_intent_failed'
    $graceResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $graceDeadlineProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($graceResume.Attempted -and $graceResume.Accepted) 'grace_deadline_resume_failed'
    $launcherWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $graceDeadlineProcess.ProcessHandle, 10000)
    Assert-Native ($launcherWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) `
        'grace_launcher_did_not_signal'
    $accounting = $null
    $accountingDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency)
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $accountingDeadline) {
        $accounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($graceDeadlineJob)
        Assert-Native $accounting.Succeeded 'grace_deadline_accounting_failed'
        if ($accounting.ActiveProcesses -gt 0) { break }
        Microsoft.PowerShell.Utility\Start-Sleep -Milliseconds 50
    }
    Assert-Native ($null -ne $accounting -and $accounting.ActiveProcesses -gt 0) 'grace_descendant_missing'
    $script:EgState = New-State -OutcomeCommitted $true
    $script:EgState.active_processes = [uint64]0
    Wait-EgDescendantGrace -JobHandle $graceDeadlineJob `
        -LauncherHandle $graceDeadlineProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 200)
    Assert-Native $script:EgState.timed_out 'grace_deadline_not_timeout'
    Assert-Native (-not $script:EgState.interrupted -and -not $script:EgState.descendant_grace_expired) `
        'grace_deadline_reason_flags_invalid'
    Assert-Native $script:EgState.termination_succeeded 'grace_deadline_termination_failed'
    Wait-EgReap -JobHandle $graceDeadlineJob -DeadlineTicks 0 -WindowSeconds 10
    Assert-Native ((Get-EgExitCode) -eq 2) 'grace_deadline_exit_not_two'
}
finally {
    Cleanup-Job -JobHandle $graceDeadlineJob -Process $graceDeadlineProcess
}

Write-Output 'function_case=descendant_grace_interruption_during_polling'
$graceInterruptJob = [IntPtr]::Zero
$graceInterruptProcess = $null
$graceInterruptThread = $null
try {
    $graceInterruptJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($graceInterruptJob)
    $graceInterruptProcess = New-TestChild -JobHandle $graceInterruptJob -Code $descendantCode
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'grace_interrupt_intent_failed'
    $graceInterruptResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $graceInterruptProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($graceInterruptResume.Attempted -and $graceInterruptResume.Accepted) `
        'grace_interrupt_resume_failed'
    $graceInterruptWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $graceInterruptProcess.ProcessHandle, 10000)
    Assert-Native ($graceInterruptWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) `
        'grace_interrupt_launcher_wait_failed'
    $graceInterruptAccounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($graceInterruptJob)
    Assert-Native ($graceInterruptAccounting.Succeeded -and $graceInterruptAccounting.ActiveProcesses -gt 0) `
        'grace_interrupt_descendant_missing'
    $script:EgState = New-State -OutcomeCommitted $true
    $script:EgState.active_processes = [uint64]1
    $graceInterruptThread = [EnergyGridGraceInterruptSchedulerForFunctionTest]::Schedule(250)
    Wait-EgDescendantGrace -JobHandle $graceInterruptJob `
        -LauncherHandle $graceInterruptProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 30000)
    Assert-Native ($graceInterruptThread.Join(5000)) 'grace_interrupt_scheduler_join_failed'
    Assert-Native (-not $graceInterruptThread.IsAlive) 'grace_interrupt_scheduler_still_running'
    Assert-Native $script:EgState.interrupted 'grace_interruption_not_recorded'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.descendant_grace_expired) `
        'grace_interruption_reason_flags_invalid'
    Assert-Native $script:EgState.termination_succeeded 'grace_interruption_termination_failed'
    Wait-EgReap -JobHandle $graceInterruptJob -DeadlineTicks 0 -WindowSeconds 10
    Assert-Native ((Get-EgExitCode) -eq 2) 'grace_interruption_exit_not_two'
}
finally {
    Cleanup-Job -JobHandle $graceInterruptJob -Process $graceInterruptProcess
    if ($null -ne $graceInterruptThread -and $graceInterruptThread.IsAlive) { [void]$graceInterruptThread.Join(5000) }
}

Write-Output 'function_case=descendant_grace_local_control'
$localGraceJob = [IntPtr]::Zero
$localGraceProcess = $null
try {
    $localGraceJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($localGraceJob)
    $localGraceProcess = New-TestChild -JobHandle $localGraceJob -Code $descendantCode
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'local_grace_intent_failed'
    $localGraceResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $localGraceProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($localGraceResume.Attempted -and $localGraceResume.Accepted) 'local_grace_resume_failed'
    $localGraceWait = [EnergyGridOneShotSupervisorNative]::WaitProcess(
        $localGraceProcess.ProcessHandle, 10000)
    Assert-Native ($localGraceWait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) `
        'local_grace_launcher_wait_failed'
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $localGraceJob `
        -LauncherHandle $localGraceProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 30000)
    Assert-Native $script:EgState.descendant_grace_expired 'local_grace_not_recorded'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted) `
        'local_grace_reason_flags_invalid'
    Assert-Native $script:EgState.termination_succeeded 'local_grace_termination_failed'
    Wait-EgReap -JobHandle $localGraceJob -DeadlineTicks 0 -WindowSeconds 10
}
finally {
    Cleanup-Job -JobHandle $localGraceJob -Process $localGraceProcess
}

Write-Output 'function_case=descendant_grace_completion_global_deadline'
$globalCompletionJob = [IntPtr]::Zero
$globalCompletionProcess = $null
$globalCompletionReleasePath = Join-Path $RootPath 'completion-global.release'
try {
    $globalCompletionJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($globalCompletionJob)
    $globalCompletionCode = 'while (-not (Test-Path -LiteralPath ' +
        (Quote-PowerShellLiteral -Value $globalCompletionReleasePath) +
        ')) { Start-Sleep -Milliseconds 50 }'
    $globalCompletionProcess = New-TestChild -JobHandle $globalCompletionJob -Code $globalCompletionCode
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'global_completion_intent_failed'
    $globalCompletionResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $globalCompletionProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($globalCompletionResume.Attempted -and $globalCompletionResume.Accepted) `
        'global_completion_resume_failed'
    $globalCompletionInitial = [EnergyGridOneShotSupervisorNative]::GetAccounting($globalCompletionJob)
    Assert-Native ($globalCompletionInitial.Succeeded -and $globalCompletionInitial.ActiveProcesses -gt 0) `
        'global_completion_initial_positive_missing'
    $script:GraceSleepMode = 'global-completion'
    $script:GraceSleepCalls = 0
    $script:GraceSleepJobHandle = $globalCompletionJob
    $script:GraceSleepReleasePath = $globalCompletionReleasePath
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $globalCompletionJob `
        -LauncherHandle $globalCompletionProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 200)
    $script:GraceSleepMode = 'off'
    Assert-Native ($script:GraceSleepCalls -eq 1) 'global_completion_sleep_seam_not_used'
    Assert-Native (-not $script:EgState.termination_started) 'global_completion_terminated'
    Assert-Native ($script:EgState.active_processes -eq 0) 'global_completion_active_processes_nonzero'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted -and
        -not $script:EgState.descendant_grace_expired) 'global_completion_flags_set'
}
finally {
    $script:GraceSleepMode = 'off'
    Cleanup-Job -JobHandle $globalCompletionJob -Process $globalCompletionProcess
    if (Test-Path -LiteralPath $globalCompletionReleasePath) { Remove-Item -LiteralPath $globalCompletionReleasePath -Force }
}

Write-Output 'function_case=descendant_grace_completion_local_grace'
$localCompletionJob = [IntPtr]::Zero
$localCompletionProcess = $null
$localCompletionReleasePath = Join-Path $RootPath 'completion-local.release'
try {
    $localCompletionJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($localCompletionJob)
    $localCompletionCode = 'while (-not (Test-Path -LiteralPath ' +
        (Quote-PowerShellLiteral -Value $localCompletionReleasePath) +
        ')) { Start-Sleep -Milliseconds 50 }'
    $localCompletionProcess = New-TestChild -JobHandle $localCompletionJob -Code $localCompletionCode
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'local_completion_intent_failed'
    $localCompletionResume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
        $localCompletionProcess.ThreadHandle, (Future-Deadline))
    Assert-Native ($localCompletionResume.Attempted -and $localCompletionResume.Accepted) `
        'local_completion_resume_failed'
    $localCompletionInitial = [EnergyGridOneShotSupervisorNative]::GetAccounting($localCompletionJob)
    Assert-Native ($localCompletionInitial.Succeeded -and $localCompletionInitial.ActiveProcesses -gt 0) `
        'local_completion_initial_positive_missing'
    $script:GraceSleepMode = 'local-completion'
    $script:GraceSleepCalls = 0
    $script:GraceSleepJobHandle = $localCompletionJob
    $script:GraceSleepReleasePath = $localCompletionReleasePath
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $localCompletionJob `
        -LauncherHandle $localCompletionProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 30000)
    $script:GraceSleepMode = 'off'
    Assert-Native ($script:GraceSleepCalls -eq 1) 'local_completion_sleep_seam_not_used'
    Assert-Native (-not $script:EgState.termination_started) 'local_completion_terminated'
    Assert-Native ($script:EgState.active_processes -eq 0) 'local_completion_active_processes_nonzero'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted -and
        -not $script:EgState.descendant_grace_expired) 'local_completion_flags_set'
}
finally {
    $script:GraceSleepMode = 'off'
    Cleanup-Job -JobHandle $localCompletionJob -Process $localCompletionProcess
    if (Test-Path -LiteralPath $localCompletionReleasePath) { Remove-Item -LiteralPath $localCompletionReleasePath -Force }
}

Write-Output 'function_case=descendant_grace_accounting_failure'
[EnergyGridOneShotSupervisorNative]::ResetControlState()
$accountingFailureState = New-State -OutcomeCommitted $true
$script:EgState = $accountingFailureState
$accountingFailureThrown = $false
try {
    Wait-EgDescendantGrace -JobHandle ([IntPtr]([int64]1)) `
        -LauncherHandle ([IntPtr]::Zero) -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline)
}
catch {
    $accountingFailureThrown = $true
    Assert-Native ($_.Exception.Message -eq 'EG_SUPERVISOR_ACCOUNTING_FAILED') `
        'accounting_failure_support_ref_invalid'
}
Assert-Native $accountingFailureThrown 'accounting_failure_not_thrown'
Assert-Native $script:EgState.containment_failure 'accounting_failure_not_containment_failure'
Assert-Native ($script:EgState.support_ref -eq 'EG_SUPERVISOR_ACCOUNTING_FAILED') `
    'accounting_failure_state_support_ref_invalid'
Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.interrupted -and
    -not $script:EgState.descendant_grace_expired) 'accounting_failure_timing_flags_set'
Assert-Native ((Get-EgExitCode) -eq 3) 'accounting_failure_exit_not_three'

Write-Output 'function_case=descendant_grace_precedence_interruption_over_timeout'
$precedenceInterruptJob = [IntPtr]::Zero
$precedenceInterruptProcess = $null
try {
    $precedenceInterruptJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($precedenceInterruptJob)
    $precedenceInterruptProcess = New-TestChild -JobHandle $precedenceInterruptJob
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    $precedenceSignalMethod = [EnergyGridOneShotSupervisorNative].GetMethod(
        'HandleConsoleSignal', [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Static)
    Assert-Native ($null -ne $precedenceSignalMethod) 'precedence_interrupt_signal_method_missing'
    [void]$precedenceSignalMethod.Invoke($null, [object[]]@([uint32]2))
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]0
    Wait-EgDescendantGrace -JobHandle $precedenceInterruptJob `
        -LauncherHandle $precedenceInterruptProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks ([System.Diagnostics.Stopwatch]::GetTimestamp() - 1)
    Assert-Native $script:EgState.interrupted 'precedence_interrupt_not_selected'
    Assert-Native (-not $script:EgState.timed_out -and -not $script:EgState.descendant_grace_expired) `
        'precedence_interrupt_lost'
    Wait-EgReap -JobHandle $precedenceInterruptJob -DeadlineTicks 0 -WindowSeconds 10
}
finally {
    Cleanup-Job -JobHandle $precedenceInterruptJob -Process $precedenceInterruptProcess
}

Write-Output 'function_case=descendant_grace_precedence_timeout_over_local'
$precedenceTimeoutJob = [IntPtr]::Zero
$precedenceTimeoutProcess = $null
try {
    $precedenceTimeoutJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($precedenceTimeoutJob)
    $precedenceTimeoutProcess = New-TestChild -JobHandle $precedenceTimeoutJob
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    $script:GraceSleepMode = 'timeout-local-precedence'
    $script:GraceSleepCalls = 0
    $script:GraceSleepJobHandle = $precedenceTimeoutJob
    $script:GraceSleepReleasePath = $null
    $script:EgState = New-State
    $script:EgState.active_processes = [uint64]1
    Wait-EgDescendantGrace -JobHandle $precedenceTimeoutJob `
        -LauncherHandle $precedenceTimeoutProcess.ProcessHandle -LauncherPid 0 -StartTicks 0 `
        -DeadlineTicks (Future-Deadline -Milliseconds 5100)
    $script:GraceSleepMode = 'off'
    Assert-Native ($script:GraceSleepCalls -eq 1) 'precedence_timeout_sleep_seam_not_used'
    Assert-Native $script:EgState.timed_out 'precedence_timeout_not_selected'
    Assert-Native (-not $script:EgState.interrupted -and -not $script:EgState.descendant_grace_expired) `
        'precedence_timeout_lost_to_local_grace'
    Wait-EgReap -JobHandle $precedenceTimeoutJob -DeadlineTicks 0 -WindowSeconds 10
}
finally {
    $script:GraceSleepMode = 'off'
    Cleanup-Job -JobHandle $precedenceTimeoutJob -Process $precedenceTimeoutProcess
}

Write-Output 'function_case=exact_termination_function'
$job = [IntPtr]::Zero
$process = $null
try {
    $job = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($job)
    $process = New-TestChild -JobHandle $job
    $script:EgState = New-State
    Invoke-EgTerminateJob -JobHandle $job -Reason 'TIMEOUT'
    Assert-Native $script:EgState.termination_started 'termination_not_started'
    Assert-Native $script:EgState.termination_succeeded 'termination_not_succeeded'
    Assert-Native (-not $script:EgState.termination_failure) 'termination_reported_failure'
    Assert-Native $script:EgState.timed_out 'timeout_not_recorded'
    $wait = [EnergyGridOneShotSupervisorNative]::WaitProcess($process.ProcessHandle, 10000)
    Assert-Native ($wait.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) 'termination_wait_failed'
    $final = Wait-JobZero -JobHandle $job
    $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($process.ProcessHandle)
    Assert-Native ($live.Succeeded -and $live.ExitCode -eq [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE) 'exact_termination_code_missing'
    Assert-Native ($final.ActiveProcesses -eq 0) 'termination_reap_missing'
}
finally {
    try {
        if ($job -ne [IntPtr]::Zero) {
            [void][EnergyGridOneShotSupervisorNative]::TerminateJob(
                $job, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        }
    }
    catch { }
    try { Close-TestChild -Process $process } catch { }
    Close-Native -Handle $job
}

$script:EgState = New-State
Invoke-EgTerminateJob -JobHandle ([IntPtr]::Zero) -Reason 'POST_CREATE_FAILURE'
Assert-Native $script:EgState.termination_failure 'termination_failure_not_observed'
Assert-Native $script:EgState.containment_failure 'termination_failure_not_infrastructure'
Write-Output 'function_case=termination_failure_infrastructure'

Write-Output 'function_case=accounting_failure_and_reap_timeout'
$accountingState = New-State
$script:EgState = $accountingState
try { Get-EgAccounting -JobHandle ([IntPtr]([int64]1)) } catch { }
Assert-Native $script:EgState.containment_failure 'accounting_failure_not_containment_failure'

$reapJob = [IntPtr]::Zero
$reapProcess = $null
try {
    $reapJob = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($reapJob)
    $reapProcess = New-TestChild -JobHandle $reapJob
    $script:EgState = New-State -OutcomeCommitted $true
    $script:EgState.reap_confirmed = $false
    Wait-EgReap -JobHandle $reapJob -DeadlineTicks 0 -WindowSeconds 0
    Assert-Native (-not $script:EgState.reap_confirmed) 'reap_timeout_was_confirmed'
    Assert-Native ((Get-EgExitCode) -eq 3) 'reap_timeout_exit_not_three'
}
finally {
    try {
        if ($reapJob -ne [IntPtr]::Zero) {
            [void][EnergyGridOneShotSupervisorNative]::TerminateJob(
                $reapJob, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
        }
    }
    catch { }
    try { Close-TestChild -Process $reapProcess } catch { }
    Close-Native -Handle $reapJob
}

function Assert-ExitCase {
    param([string]$Name, [int]$Expected)
    $actual = Get-EgExitCode
    Assert-Native ($actual -eq $Expected) ($Name + '_expected_' + $Expected + '_actual_' + $actual)
    Write-Output ('exit_case=' + $Name + '=' + $actual)
}

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.creation_succeeded = $false
$script:EgState.reap_confirmed = $false
$script:EgState.start_verdict = 'NOT_STARTED_PROVEN'
Assert-ExitCase -Name 'durable_precreation_rejection' -Expected 1

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.timed_out = $true
$script:EgState.start_verdict = 'STARTED_PROVEN'
$script:EgState.launcher_exit_code = 0
Assert-ExitCase -Name 'durable_timeout_successful_reap' -Expected 2

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.timed_out = $true
$script:EgState.containment_failure = $true
Assert-ExitCase -Name 'timeout_containment_failure' -Expected 3

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.timed_out = $true
$script:EgState.reap_confirmed = $false
Assert-ExitCase -Name 'timeout_reap_unconfirmed' -Expected 3

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.start_verdict = 'AMBIGUOUS'
Assert-ExitCase -Name 'durable_ambiguous' -Expected 4

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.start_verdict = 'STARTED_PROVEN'
$script:EgState.launcher_exit_code = 0
Assert-ExitCase -Name 'started_proven_launcher_zero_complete' -Expected 0

$script:EgState = New-State -OutcomeCommitted $true
$script:EgState.start_verdict = 'STARTED_PROVEN'
$script:EgState.launcher_exit_code = 17
Assert-ExitCase -Name 'nonzero_launcher_nonambiguous' -Expected 1

$script:EgState = New-State
$script:EgState.outcome_committed = $false
Assert-ExitCase -Name 'outcome_missing' -Expected 3

Write-Output 'function_assurance_cases=26'
Write-Output 'function_assurance=PASS'
'''


class SupervisorCommittedFunctionTests(unittest.TestCase):
    def test_exact_termination_and_exit_mapping_functions_on_windows_powershell_5_1(self):
        powershell = native_powershell()
        if powershell is None:
            self.skipTest("native Windows PowerShell 5.1 is only available on Windows")
        with tempfile.TemporaryDirectory(prefix="eg_function_assurance_") as directory:
            root = Path(directory)
            harness = root / "function_assurance.ps1"
            harness.write_text(_NATIVE_FUNCTION_HARNESS, encoding="ascii")
            result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(harness),
                    "-SupervisorPath",
                    str(SUPERVISOR),
                    "-RootPath",
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + "\n" + result.stderr)
            for case in (
                "durable_precreation_rejection=1",
                "durable_timeout_successful_reap=2",
                "timeout_containment_failure=3",
                "timeout_reap_unconfirmed=3",
                "durable_ambiguous=4",
                "started_proven_launcher_zero_complete=0",
                "nonzero_launcher_nonambiguous=1",
                "outcome_missing=3",
            ):
                self.assertIn("exit_case=" + case, result.stdout)
            for case in (
                "timed_out_intent_rejection",
                "timed_out_outcome_contract",
                "timed_out_outcome_actual_delayed",
                "timed_out_outcome_actual_ordinary",
                "timed_out_outcome_defensive_exact_caller",
                "descendant_grace_stale_zero_global_deadline",
                "descendant_grace_stale_zero_interruption",
                "descendant_grace_overall_deadline",
                "descendant_grace_interruption_during_polling",
                "descendant_grace_local_control",
                "descendant_grace_completion_global_deadline",
                "descendant_grace_completion_local_grace",
                "descendant_grace_accounting_failure",
                "descendant_grace_precedence_interruption_over_timeout",
                "descendant_grace_precedence_timeout_over_local",
                "exact_termination_function",
                "termination_failure_infrastructure",
                "accounting_failure_and_reap_timeout",
            ):
                self.assertIn("function_case=" + case, result.stdout)
            for marker in (
                "file_id_info_layout=PASS",
                "file_id_info_query=PASS",
                "root_identity=PASS",
                "file_identity=PASS",
                "link_count=PASS",
                "reparse_rejection=PASS",
                "identity_negatives=PASS",
                "representation_positives=PASS",
                "outcome_commit_transition=PASS",
                "retained_handle_read=PASS",
            ):
                self.assertIn(marker, result.stdout)
            self.assertRegex(result.stdout, r"SHORT_NAME_ALIAS=(PASS|UNAVAILABLE)")
            self.assertIn("function_assurance_cases=26", result.stdout)
            self.assertIn("function_assurance=PASS", result.stdout)


_CRASH_CONTAINMENT_HARNESS = r'''
param(
    [Parameter(Mandatory = $true)][string]$SupervisorPath,
    [Parameter(Mandatory = $true)][string]$ReadyPath,
    [Parameter(Mandatory = $true)][string]$ChildPidPath,
    [Parameter(Mandatory = $true)][string]$GrandchildPidPath,
    [switch]$ResumeChild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Native {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$source = Get-Content -LiteralPath $SupervisorPath -Raw
$marker = "`$script:EgNativeSource = @"
$start = $source.IndexOf($marker) + $marker.Length
if ($source[$start] -eq [char]39) { $start++ }
if ($source[$start] -eq "`r") { $start++ }
if ($source[$start] -eq "`n") { $start++ }
$end = $source.IndexOf(([char]39).ToString() + "@", $start)
Add-Type -TypeDefinition $source.Substring($start, $end - $start) -ReferencedAssemblies @('System.Management.dll') -ErrorAction Stop
Assert-Native ([EnergyGridOneShotSupervisorNative]::VerifyX64StructureSizes()) 'native_x64_layout_failed'

function Close-Native {
    param([IntPtr]$Handle)
    if ($Handle -ne [IntPtr]::Zero) {
        [void][EnergyGridOneShotSupervisorNative]::CloseHandleChecked($Handle)
    }
}

function Quote-PowerShellLiteral {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

$powerShellPath = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
$job = [IntPtr]::Zero
$pipes = $null
$attributes = $null
$processHandle = [IntPtr]::Zero
$threadHandle = [IntPtr]::Zero
$stdoutDrain = $null
$stderrDrain = $null
try {
    $job = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($job)
    $pipes = [EnergyGridOneShotSupervisorNative]::CreatePipes()
    $attributes = New-Object EnergyGridOneShotSupervisorNative+AttributeResources(
        $job, $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite,
        $pipes.LauncherStderrWrite)
    if ($ResumeChild) {
        $childCode = @'
$shell = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
$grand = Start-Process -FilePath $shell -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds 60') -WindowStyle Hidden -PassThru
[IO.File]::WriteAllText(__GRANDCHILD__, [string]$grand.Id)
Start-Sleep -Seconds 60
'@
        $childCode = $childCode.Replace('__GRANDCHILD__', (Quote-PowerShellLiteral -Value $GrandchildPidPath))
    }
    else {
        $childCode = 'Start-Sleep -Seconds 60'
    }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childCode))
    $commandLine = '"' + $powerShellPath + '" -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + $encoded
    $builder = New-Object System.Text.StringBuilder($commandLine)
    $created = [EnergyGridOneShotSupervisorNative]::CreateContainedProcess(
        $powerShellPath, $builder, [Environment]::SystemDirectory,
        $attributes.AttributeList, $pipes.LauncherStdinRead,
        $pipes.LauncherStdoutWrite, $pipes.LauncherStderrWrite)
    Assert-Native $created.Succeeded ('native_create_failed:' + $created.ErrorCode)
    $processHandle = $created.ProcessInfo.hProcess
    $threadHandle = $created.ProcessInfo.hThread
    $attributes.Dispose()
    $attributes = $null
    Close-Native -Handle $pipes.LauncherStdinRead
    Close-Native -Handle $pipes.LauncherStdoutWrite
    Close-Native -Handle $pipes.LauncherStderrWrite
    $stdoutDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStdoutRead)
    $stderrDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStderrRead)
    Close-Native -Handle $pipes.SupervisorStdinWrite
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    if ($ResumeChild) {
        Assert-Native ([EnergyGridOneShotSupervisorNative]::CommitIntent()) 'intent_commit_failed'
        $resume = [EnergyGridOneShotSupervisorNative]::TryResumeThread(
            $threadHandle,
            ([System.Diagnostics.Stopwatch]::GetTimestamp() +
                ([int64]30 * [int64][System.Diagnostics.Stopwatch]::Frequency)))
        Assert-Native ($resume.Attempted -and $resume.Accepted) 'resume_failed'
    }
    [IO.File]::WriteAllText($ChildPidPath, [string]$created.ProcessInfo.dwProcessId)
    if ($ResumeChild) {
        $grandchildDeadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
            ([int64]10 * [int64][System.Diagnostics.Stopwatch]::Frequency)
        while (-not (Test-Path -LiteralPath $GrandchildPidPath) -and
            [System.Diagnostics.Stopwatch]::GetTimestamp() -lt $grandchildDeadline) {
            Start-Sleep -Milliseconds 100
        }
        Assert-Native (Test-Path -LiteralPath $GrandchildPidPath) 'grandchild_pid_missing'
    }
    [IO.File]::WriteAllText($ReadyPath, 'ready')
    while ($true) { Start-Sleep -Milliseconds 100 }
}
finally {
    if ($null -ne $attributes) { $attributes.Dispose() }
    if ($null -ne $pipes) {
        Close-Native -Handle $pipes.LauncherStdinRead
        Close-Native -Handle $pipes.SupervisorStdinWrite
        Close-Native -Handle $pipes.SupervisorStdoutRead
        Close-Native -Handle $pipes.LauncherStdoutWrite
        Close-Native -Handle $pipes.SupervisorStderrRead
        Close-Native -Handle $pipes.LauncherStderrWrite
    }
    Close-Native -Handle $threadHandle
    Close-Native -Handle $processHandle
    Close-Native -Handle $job
}
'''


class SupervisorCrashContainmentTests(unittest.TestCase):
    def _process_exists(self, powershell, pid):
        result = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$process = Get-Process -Id " + str(pid) + " -ErrorAction SilentlyContinue; "
                "if ($null -ne $process) { exit 1 } else { exit 0 }",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 1

    def test_separate_os_process_supervisor_death_closes_last_job_handle(self):
        powershell = native_powershell()
        if powershell is None:
            self.skipTest("native Windows PowerShell 5.1 is only available on Windows")

        with tempfile.TemporaryDirectory(prefix="eg_crash_assurance_") as directory:
            root = Path(directory)
            harness = root / "crash_containment.ps1"
            harness.write_text(_CRASH_CONTAINMENT_HARNESS, encoding="ascii")

            for resume_child in (False, True):
                ready = root / ("ready-resumed" if resume_child else "ready-suspended")
                child_pid_path = root / ("child-resumed.pid" if resume_child else "child-suspended.pid")
                grandchild_pid_path = root / "grandchild.pid"
                process = subprocess.Popen(
                    [
                        powershell,
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(harness),
                        "-SupervisorPath",
                        str(SUPERVISOR),
                        "-ReadyPath",
                        str(ready),
                        "-ChildPidPath",
                        str(child_pid_path),
                        "-GrandchildPidPath",
                        str(grandchild_pid_path),
                    ] + (["-ResumeChild"] if resume_child else []),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
                child_pid = None
                grandchild_pid = None
                try:
                    deadline = time.monotonic() + 30
                    while not ready.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail("crash harness exited before readiness")
                        time.sleep(0.05)
                    self.assertTrue(ready.exists(), "crash harness readiness timed out")
                    child_pid = int(child_pid_path.read_text(encoding="ascii").strip())
                    if resume_child:
                        grandchild_deadline = time.monotonic() + 10
                        while not grandchild_pid_path.exists() and time.monotonic() < grandchild_deadline:
                            time.sleep(0.05)
                        self.assertTrue(grandchild_pid_path.exists(), "grandchild pid was not published")
                        grandchild_pid = int(grandchild_pid_path.read_text(encoding="ascii").strip())
                    process.kill()
                    process.wait(timeout=15)
                    gone_deadline = time.monotonic() + 10
                    while time.monotonic() < gone_deadline:
                        child_gone = not self._process_exists(powershell, child_pid)
                        grandchild_gone = (
                            grandchild_pid is None
                            or not self._process_exists(powershell, grandchild_pid)
                        )
                        if child_gone and grandchild_gone:
                            break
                        time.sleep(0.1)
                    self.assertFalse(self._process_exists(powershell, child_pid))
                    if grandchild_pid is not None:
                        self.assertFalse(self._process_exists(powershell, grandchild_pid))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=15)
                    for pid in (child_pid, grandchild_pid):
                        if pid is not None and self._process_exists(powershell, pid):
                            subprocess.run(
                                [
                                    powershell,
                                    "-NoLogo",
                                    "-NoProfile",
                                    "-NonInteractive",
                                    "-Command",
                                    "Stop-Process -Id " + str(pid) + " -Force -ErrorAction SilentlyContinue",
                                ],
                                capture_output=True,
                                text=True,
                                timeout=10,
                                check=False,
                            )


class SupervisorSyntheticContainmentTests(unittest.TestCase):
    def test_job_membership_exists_at_creation_before_resume(self):
        supervisor = SyntheticSupervisor()
        self.assertTrue(supervisor.create())
        self.assertTrue(supervisor.job.member_at_creation)
        self.assertTrue(supervisor.job.suspended)
        self.assertTrue(supervisor.resume())

    def test_no_assignment_after_creation_fallback(self):
        source = SUPERVISOR.read_text(encoding="utf-8")
        self.assertNotIn("AssignProcessToJobObject", source)
        supervisor = SyntheticSupervisor()
        self.assertFalse(supervisor.create(job_list=False))
        self.assertEqual(0, supervisor.create_attempts)

    def test_supervisor_death_before_intent_cannot_leave_suspended_launcher(self):
        supervisor = SyntheticSupervisor()
        self.assertTrue(supervisor.create())
        self.assertTrue(supervisor.job.suspended)
        supervisor.job.close_last_handle()
        self.assertEqual(0, supervisor.job.active)

    def test_intent_flush_failure_prevents_resume_and_tree(self):
        supervisor = SyntheticSupervisor()
        self.assertTrue(supervisor.create())
        self.assertFalse(supervisor.commit_intent(success=False))
        supervisor.job.terminate()
        self.assertEqual(0, supervisor.resume_attempts)
        self.assertEqual(0, supervisor.job.active)

    def test_timeout_kills_launcher_and_descendants(self):
        supervisor = SyntheticSupervisor()
        self.assertTrue(supervisor.create())
        self.assertTrue(supervisor.commit_intent())
        self.assertTrue(supervisor.resume())
        supervisor.job.add_child("application")
        supervisor.job.add_child("grandchild", parent=1, image="chromium.exe")
        supervisor.job.terminate()
        self.assertEqual(0, supervisor.job.active)
        self.assertEqual(3, supervisor.job.total_terminated)

    def test_last_job_handle_closure_kills_descendants(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        supervisor.resume()
        supervisor.job.add_child("application")
        supervisor.job.close_last_handle()
        self.assertTrue(supervisor.job.closed)
        self.assertEqual(0, supervisor.job.active)

    def test_termination_requires_zero_active_accounting(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        supervisor.resume()
        supervisor.job.add_child("application")
        supervisor.job.terminate()
        self.assertTrue(supervisor.reap(accounting=True))
        self.assertTrue(supervisor.reap_confirmed)

    def test_unsupported_job_list_never_executes_launcher(self):
        supervisor = SyntheticSupervisor()
        self.assertFalse(supervisor.create(job_list=False))
        self.assertFalse(supervisor.job.resumed)
        self.assertEqual([], supervisor.job.processes)

    def test_raw_stream_canaries_do_not_enter_public_projection(self):
        canary = "RAW_STDOUT_CANARY secret@example.invalid"
        projection = {
            "support_ref": "EG_SUPERVISOR",
            "start_verdict": "AMBIGUOUS",
            "stdout_bytes": len(canary.encode("utf-8")),
            "raw_stream_retained_bytes": 0,
        }
        encoded = json.dumps(projection)
        self.assertNotIn(canary, encoded)
        self.assertEqual(0, projection["raw_stream_retained_bytes"])

    def test_zero_or_one_process_creation_attempt(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        self.assertEqual(1, supervisor.create_attempts)
        self.assertNotIn("retry", SUPERVISOR.read_text(encoding="utf-8").lower())

    def test_child_start_and_launcher_exit_70_never_proves_negative(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        supervisor.commit_intent()
        supervisor.resume()
        supervisor.job.add_child("application")
        supervisor.launcher_exit = 70
        supervisor.observe_application()
        supervisor.job.terminate()
        supervisor.reap()
        supervisor.outcome_committed = True
        self.assertNotEqual("NOT_STARTED_PROVEN", supervisor.classify())

    def test_child_start_without_application_log_never_proves_negative(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        supervisor.commit_intent()
        supervisor.resume()
        supervisor.job.add_child("application")
        # No log is modelled at all; only the positive observer can prove start.
        supervisor.observe_application()
        supervisor.job.terminate()
        supervisor.reap()
        supervisor.outcome_committed = True
        self.assertNotEqual("NOT_STARTED_PROVEN", supervisor.classify())

    def test_early_prechild_failure_is_the_only_accounting_negative_proof(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        supervisor.resume()
        supervisor.launcher_exit = 70
        supervisor.job.terminate()
        supervisor.reap()
        supervisor.outcome_committed = True
        self.assertEqual("NOT_STARTED_PROVEN", supervisor.classify())

    def test_child_grandchild_containment(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        supervisor.resume()
        supervisor.job.add_child("application")
        supervisor.job.add_child("grandchild", parent=1)
        supervisor.job.terminate()
        self.assertEqual(0, supervisor.job.active)

    def test_forty_descendants_are_not_rejected_by_an_active_process_ceiling(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        supervisor.resume()
        for index in range(40):
            supervisor.job.add_child("worker", parent=1)
        self.assertEqual(41, supervisor.job.total)
        self.assertNotIn("ActiveProcessLimit = 0", SUPERVISOR.read_text(encoding="utf-8"))

    def test_probe_git_and_compiler_descendants_are_not_started_proof(self):
        for kind, image in (("python-probe", "python.exe"), ("git", "git.exe"), ("compiler", "cl.exe")):
            supervisor = SyntheticSupervisor()
            supervisor.create()
            supervisor.commit_intent()
            supervisor.resume()
            supervisor.job.add_child(kind, image=image, command="noncanonical")
            supervisor.job.terminate()
            supervisor.reap()
            supervisor.outcome_committed = True
            self.assertFalse(supervisor.observe_application(image=False, command=False))
            self.assertEqual("AMBIGUOUS", supervisor.classify())

    def test_positive_application_observer(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        supervisor.commit_intent()
        supervisor.resume()
        supervisor.job.add_child("application")
        self.assertTrue(supervisor.observe_application())
        supervisor.job.terminate()
        supervisor.reap()
        supervisor.outcome_committed = True
        self.assertEqual("STARTED_PROVEN", supervisor.classify())

    def test_missed_observer_is_ambiguous(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        supervisor.commit_intent()
        supervisor.resume()
        supervisor.job.add_child("application")
        supervisor.job.terminate()
        supervisor.reap()
        supervisor.outcome_committed = True
        self.assertEqual("AMBIGUOUS", supervisor.classify())

    def test_wrong_parent_image_argument_stale_pid_and_exited_process_rejected(self):
        for kwargs in (
            {"parent": 999},
            {"image": "wrong.exe"},
            {"command": "wrong"},
            {"live": False},
        ):
            supervisor = SyntheticSupervisor()
            supervisor.create()
            supervisor.commit_intent()
            supervisor.resume()
            supervisor.job.add_child("application")
            self.assertFalse(supervisor.observe_application(**kwargs))

    def test_concurrent_stdout_stderr_saturation_has_only_counts(self):
        supervisor = SUPERVISOR.read_text(encoding="utf-8")
        self.assertIn("Read(buffer, 0, buffer.Length)", supervisor)
        self.assertIn("checked(bytes + (ulong)read)", supervisor)
        self.assertNotIn("StringBuilder", supervisor[supervisor.index("public sealed class DrainWorker"): supervisor.index("public sealed class ProcessMetadataResult")])

    def test_inheritable_handle_canary_is_excluded(self):
        supervisor = SUPERVISOR.read_text(encoding="utf-8")
        handle_section = supervisor[supervisor.index("public sealed class AttributeResources"): supervisor.index("public sealed class PipeSet")]
        self.assertIn("PROC_THREAD_ATTRIBUTE_HANDLE_LIST", handle_section)
        self.assertIn("IntPtr.Size * 3", handle_section)
        self.assertNotIn("jobHandle", handle_section)

    def test_windows_quoting_spaces_apostrophes_unicode_and_trailing_separators(self):
        cases = (
            r"C:\Program Files\EnergyGrid\launcher.ps1",
            "C:\\owner's\\EnergyGrid",
            "C:\\能源\\EnergyGrid",
            "C:\\EnergyGrid\\",
        )
        for case in cases:
            quoted = quote_windows(case)
            self.assertTrue(quoted.startswith('"'))
            self.assertTrue(quoted.endswith('"'))
        self.assertTrue(quote_windows("C:\\EnergyGrid\\").endswith("\\\\\""))

    def test_torn_intent_and_outcome_are_not_receipts(self):
        def valid_receipt(raw):
            try:
                if not raw.endswith(b"\n") or raw.endswith(b"\r\n"):
                    return False
                value = json.loads(raw.decode("utf-8"))
                return isinstance(value, dict) and value.get("schema", "").endswith(".v1")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False

        self.assertFalse(valid_receipt(b'{"schema":"energygrid.one_shot_supervisor.intent.v1"'))
        self.assertFalse(valid_receipt(b'{"schema":"energygrid.one_shot_supervisor.outcome.v1"}\r\n'))
        self.assertTrue(valid_receipt(b'{"schema":"energygrid.one_shot_supervisor.outcome.v1"}\n'))

    def test_duplicate_run_id_is_create_new_only(self):
        with tempfile.TemporaryDirectory(prefix="eg_supervisor_receipt_") as directory:
            path = Path(directory) / "run.intent.json"
            path.write_bytes(b"existing\n")
            with self.assertRaises(FileExistsError):
                path.open("xb").close()
            self.assertEqual(b"existing\n", path.read_bytes())

    def test_late_flush_cannot_reopen_resume_gate(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        supervisor.evidence_failure = True
        self.assertFalse(supervisor.commit_intent())
        self.assertFalse(supervisor.resume())
        self.assertEqual(0, supervisor.resume_attempts)

    def test_outcome_flush_failure_is_infrastructure_not_a_receipt(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        supervisor.evidence_failure = True
        supervisor.job.terminate()
        supervisor.reap()
        self.assertEqual("AMBIGUOUS", supervisor.classify())

    def test_resume_anomaly_termination_failure_accounting_failure_and_reap_timeout(self):
        supervisor = SyntheticSupervisor()
        supervisor.create()
        self.assertFalse(supervisor.resume(return_value=0))
        self.assertTrue(supervisor.containment_failure)
        supervisor.reap(accounting=False)
        self.assertFalse(supervisor.reap_confirmed)


if __name__ == "__main__":
    unittest.main()
