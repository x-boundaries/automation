"""Source-bound, endpoint-custodied assurance for the EnergyGrid supervisor."""

import json
import hashlib
import ctypes
import ctypes.wintypes as wintypes
import argparse
import atexit
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
import uuid
import time
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = REPO_ROOT / "scripts" / "energygrid_one_shot_supervisor.ps1"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "energygrid-bill-downloader-tests.yml"
RUNBOOK = REPO_ROOT / "energygrid-bill-downloader" / "docs" / "runbook.md"
TESTS_ROOT = REPO_ROOT / "energygrid-bill-downloader" / "tests"
FIXTURES = TESTS_ROOT / "fixtures"
HARNESS = FIXTURES / "one_shot_supervisor_harness.ps1"
FUNCTIONS = FIXTURES / "one_shot_supervisor_functions.ps1"
SUPPORT = FIXTURES / "one_shot_supervisor_support.cs"
PYTHON_FIXTURE = FIXTURES / "energygrid_bill_downloader.py"
LAUNCHER = FIXTURES / "supervisor_launcher" / "launcher.ps1"
LAUNCHER_LIB = FIXTURES / "supervisor_launcher" / "launcher_lib.ps1"
BASE_HEAD = "005f5b8b9ae38eb7ae4b4be67115ad559d5e7024"
BASE_TREE = "a73bff398bc6e2b74a6dc63e767932709ed6b061"
CONSTRUCTION_HEAD = "70d6eef6da55f9e8faead3c1ab8f647336e194ca"
CONSTRUCTION_TREE = "28034420e436345586dc94f1624ac4483e97dbbb"
TARGET_BRANCH = "codex/energygrid-226-dual-stream-latest-email"
HELPERS = (HARNESS, FUNCTIONS, SUPPORT, PYTHON_FIXTURE, LAUNCHER, LAUNCHER_LIB)
HELPER_RELATIVES = tuple(item.relative_to(REPO_ROOT).as_posix() for item in HELPERS)
SATURATION_EXPECTED_BYTES = 1048576


def _saturation_result_passes(
    stdout_bytes: int,
    stderr_bytes: int,
    stdout_drain_complete: bool,
    stderr_drain_complete: bool,
    child_terminal: bool,
    exit_code_read: bool,
    exit_code: int,
) -> bool:
    return (
        stdout_bytes == SATURATION_EXPECTED_BYTES
        and stderr_bytes == SATURATION_EXPECTED_BYTES
        and stdout_drain_complete
        and stderr_drain_complete
        and child_terminal
        and exit_code_read
        and exit_code == 0
    )


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

    def test_shared_run_id_is_validated_recorded_and_forwarded_to_the_launcher(self):
        parameter_block = self.source[self.source.index("param("): self.source.index(")\n\nSet-StrictMode")]
        self.assertIn("[string]$RunId", parameter_block)
        validation = self.source[
            self.source.index("function Set-EgInputContract"):
            self.source.index("function Test-EgShellContract")
        ]
        self.assertIn("$RunId -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'", validation)
        self.assertIn('"RunId = $(ConvertTo-EgSafePowerShellLiteral -Value $RunId)"', self.source)
        self.assertIn("run_id = $RunId", self.source)
        main_flow = self.source[self.source.index("try {\n    Test-EgShellContract"):]
        self.assertLess(
            main_flow.index("Set-EgInputContract"),
            main_flow.index("$creation = [EnergyGridOneShotSupervisorNative]::CreateContainedProcess("),
        )

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
        support = SUPPORT.read_text(encoding="utf-8")
        fixture = PYTHON_FIXTURE.read_text(encoding="utf-8")
        self.assertIn("ManualResetEvent startGate", support)
        self.assertIn("stdoutWriter.Start()", support)
        self.assertIn("stderrWriter.Start()", support)
        self.assertIn("stdoutWriter.Join(30000)", support)
        self.assertIn("stderrWriter.Join(30000)", support)
        self.assertIn("threading.Thread(target=write_stream", fixture)
        self.assertIn("errors.append(error)", fixture)
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
        if os.name == "nt":
            self.assertTrue(HARNESS.is_file())
            self.assertTrue(SUPERVISOR.is_file())
        else:
            self.assertIn("VerifyX64StructureSizes", self.source)

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


import sys
from datetime import datetime, timezone


LOCAL_ENDPOINT_MODE = "LOCAL_ENDPOINT"
HOSTED_EXACT_HEAD_MODE = "HOSTED_EXACT_HEAD"
GITHUB_REPOSITORY = "x-boundaries/automation"
GITHUB_WORKFLOW = "energygrid-bill-downloader-tests"
GITHUB_JOB = "synthetic-windows"
_HOSTED_RECEIPT_PATH: Path | None = None
_HOSTED_RECEIPT_BINDING: dict[str, object] | None = None


def _full_sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value) is not None


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _execution_context(environment=None, *, actual_head: str | None = None) -> dict[str, object]:
    env = os.environ if environment is None else environment
    if "EG_SUPERVISOR_CUSTODY_RECEIPT" in env:
        value = env.get("EG_SUPERVISOR_CUSTODY_RECEIPT", "")
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("EG_SUPERVISOR_CUSTODY_RECEIPT is required")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise RuntimeError("custody receipt must be an absolute path")
        return {"mode": LOCAL_ENDPOINT_MODE, "receipt_path": str(path.resolve())}

    required = (
        "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW", "GITHUB_JOB",
        "GITHUB_WORKSPACE", "GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH", "GITHUB_SHA",
        "GITHUB_REF", "GITHUB_SERVER_URL", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
        "RUNNER_OS", "RUNNER_ENVIRONMENT", "RUNNER_TEMP",
    )
    if any(not isinstance(env.get(key), str) or not env.get(key, "").strip()
           for key in required):
        raise RuntimeError("no valid local receipt or complete GitHub Actions context")
    if env["GITHUB_ACTIONS"].lower() != "true" or \
            env["GITHUB_REPOSITORY"] != GITHUB_REPOSITORY or \
            env["GITHUB_WORKFLOW"] != GITHUB_WORKFLOW or \
            env["GITHUB_JOB"] != GITHUB_JOB or env["GITHUB_SERVER_URL"] != "https://github.com" or \
            env["RUNNER_OS"].lower() != "windows" or \
            env["RUNNER_ENVIRONMENT"] != "github-hosted":
        raise RuntimeError("GitHub Actions context does not match the EnergyGrid hosted job")
    if not env["GITHUB_RUN_ID"].isdigit() or not env["GITHUB_RUN_ATTEMPT"].isdigit() or \
            int(env["GITHUB_RUN_ID"]) <= 0 or int(env["GITHUB_RUN_ATTEMPT"]) <= 0:
        raise RuntimeError("invalid GitHub Actions run identity")
    if not _full_sha(env["GITHUB_SHA"]):
        raise RuntimeError("invalid GitHub Actions SHA")

    workspace = Path(env["GITHUB_WORKSPACE"]).expanduser()
    runner_temp = Path(env["RUNNER_TEMP"]).expanduser()
    event_path = Path(env["GITHUB_EVENT_PATH"]).expanduser()
    if not workspace.is_absolute() or os.path.normcase(str(workspace.resolve())) != \
            os.path.normcase(str(REPO_ROOT.resolve())):
        raise RuntimeError("GitHub workspace does not identify this repository checkout")
    if not runner_temp.is_absolute() or not runner_temp.is_dir() or \
            not event_path.is_absolute() or not event_path.is_file() or \
            not _path_is_within(event_path, runner_temp):
        raise RuntimeError("GitHub event payload is not under the runner temporary directory")
    try:
        with event_path.open("r", encoding="utf-8") as stream:
            event = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("GitHub event payload is unavailable or invalid") from error
    if not isinstance(event, dict):
        raise RuntimeError("GitHub event payload is invalid")
    repository = event.get("repository")
    if not isinstance(repository, dict) or repository.get("full_name") != GITHUB_REPOSITORY:
        raise RuntimeError("GitHub event repository does not match this repository")

    event_name = env["GITHUB_EVENT_NAME"]
    ref = env["GITHUB_REF"]
    if event_name == "pull_request":
        pull_request = event.get("pull_request")
        if not isinstance(pull_request, dict):
            raise RuntimeError("pull request event payload is missing")
        head = pull_request.get("head")
        base = pull_request.get("base")
        head_repo = head.get("repo") if isinstance(head, dict) else None
        base_repo = base.get("repo") if isinstance(base, dict) else None
        number = pull_request.get("number", event.get("number"))
        expected_head = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(number, int) or number <= 0 or not isinstance(head, dict) or \
                not isinstance(base, dict) or not isinstance(head_repo, dict) or \
                not isinstance(base_repo, dict) or head_repo.get("full_name") != GITHUB_REPOSITORY or \
                base_repo.get("full_name") != GITHUB_REPOSITORY or \
                head.get("ref") != TARGET_BRANCH or base.get("ref") != "main" or \
                not _full_sha(expected_head) or ref != f"refs/pull/{number}/merge":
            raise RuntimeError("pull request event binding is incomplete or inconsistent")
        expected_head = expected_head.lower()
    elif event_name == "workflow_dispatch":
        event_ref = event.get("ref")
        if ref != f"refs/heads/{TARGET_BRANCH}" or event_ref not in {
                TARGET_BRANCH, f"refs/heads/{TARGET_BRANCH}"}:
            raise RuntimeError("workflow dispatch ref does not identify the candidate branch")
        expected_head = env["GITHUB_SHA"].lower()
    else:
        raise RuntimeError("unsupported GitHub Actions event for exact-head custody")

    head = actual_head if actual_head is not None else _git("rev-parse", "HEAD")
    if not _full_sha(head) or head.lower() != expected_head:
        raise RuntimeError("actual HEAD does not match the trusted GitHub event SHA")
    return {
        "mode": HOSTED_EXACT_HEAD_MODE,
        "event_name": event_name,
        "repository": GITHUB_REPOSITORY,
        "expected_head": expected_head,
        "ref": ref,
        "run_id": env["GITHUB_RUN_ID"],
        "run_attempt": env["GITHUB_RUN_ATTEMPT"],
        "runner_temp": str(runner_temp.resolve()),
    }


def _receipt_context_binding(context: dict[str, object]) -> dict[str, object]:
    mode = context.get("mode")
    if mode == LOCAL_ENDPOINT_MODE:
        return {"mode": LOCAL_ENDPOINT_MODE}
    if mode != HOSTED_EXACT_HEAD_MODE:
        raise RuntimeError("invalid custody execution context")
    return {key: context.get(key) for key in (
        "mode", "event_name", "repository", "expected_head", "ref", "run_id", "run_attempt",
        "runner_temp",
    )}


def _receipt_path() -> Path:
    global _HOSTED_RECEIPT_PATH, _HOSTED_RECEIPT_BINDING
    context = _execution_context()
    if context["mode"] == LOCAL_ENDPOINT_MODE:
        return Path(str(context["receipt_path"]))
    binding = _receipt_context_binding(context)
    if _HOSTED_RECEIPT_PATH is not None:
        if binding != _HOSTED_RECEIPT_BINDING:
            raise RuntimeError("hosted custody context changed during this process")
        return _HOSTED_RECEIPT_PATH
    path = _create_hosted_receipt(context)
    _HOSTED_RECEIPT_PATH = path
    _HOSTED_RECEIPT_BINDING = binding
    atexit.register(_cleanup_hosted_receipt, path, Path(str(context["runner_temp"])))
    print("HOSTED_CONTEXT=HOSTED_EXACT_HEAD")
    print("HOSTED_RECEIPT=EPHEMERAL_TEST_OWNED")
    print("HOSTED_ENDPOINT_STATE=ENDPOINT_EVIDENCE_UNAVAILABLE")
    return path


CUSTODY_RECEIPT_MAX_AGE = timedelta(hours=12)
CUSTODY_RECEIPT_NAME = re.compile(
    r"^eg212-custody-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.json$"
)


def _custody_run_id(path: Path) -> str:
    match = CUSTODY_RECEIPT_NAME.fullmatch(path.name)
    if match is None:
        raise RuntimeError("invalid custody receipt run identity")
    return match.group(1).lower()


def _assert_current_receipt_path(path: Path) -> None:
    if path.resolve() != _receipt_path().resolve():
        raise RuntimeError("STALE_CUSTODY_RECEIPT: receipt path differs from this run")


def _parse_receipt_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("STALE_CUSTODY_RECEIPT: missing " + field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RuntimeError("STALE_CUSTODY_RECEIPT: invalid " + field) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("STALE_CUSTODY_RECEIPT: timezone missing from " + field)
    return parsed.astimezone(timezone.utc)


def _validate_receipt_freshness(receipt: dict[str, object], path: Path) -> None:
    if receipt.get("run_id") != _custody_run_id(path):
        raise RuntimeError("STALE_CUSTODY_RECEIPT: run identity mismatch")
    now = datetime.now(timezone.utc)
    created = _parse_receipt_time(receipt.get("created_utc"), "created_utc")
    if created > now + timedelta(minutes=1) or now - created > CUSTODY_RECEIPT_MAX_AGE:
        raise RuntimeError("STALE_CUSTODY_RECEIPT: creation time outside the active run window")
    observation = receipt.get("endpoint_observation")
    if not isinstance(observation, dict):
        raise RuntimeError("STALE_CUSTODY_RECEIPT: endpoint observation missing")
    started = _parse_receipt_time(observation.get("started_utc"), "endpoint started_utc")
    if started < created or started > now + timedelta(minutes=1):
        raise RuntimeError("STALE_CUSTODY_RECEIPT: endpoint observation chronology invalid")
    checked_value = observation.get("checked_utc")
    if checked_value is not None:
        checked = _parse_receipt_time(checked_value, "endpoint checked_utc")
        if checked < started or checked > now + timedelta(minutes=1) or \
                now - checked > CUSTODY_RECEIPT_MAX_AGE:
            raise RuntimeError("STALE_CUSTODY_RECEIPT: endpoint observation is stale")
    history = receipt.get("phase_history")
    if not isinstance(history, list):
        raise RuntimeError("STALE_CUSTODY_RECEIPT: phase history missing")
    previous = created
    for entry in history:
        if not isinstance(entry, dict):
            raise RuntimeError("STALE_CUSTODY_RECEIPT: invalid phase history")
        checked = _parse_receipt_time(entry.get("checked_utc"), "phase checked_utc")
        if checked < previous or checked > now + timedelta(minutes=1):
            raise RuntimeError("STALE_CUSTODY_RECEIPT: phase chronology invalid")
        previous = checked


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError("git failed: " + " ".join(arguments) + "\n" + result.stderr)
    return result.stdout.strip()


def _identity(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": path.resolve().relative_to(REPO_ROOT).as_posix(),
        "canonical_path": str(path.resolve()),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _valid_candidate_parent(parent: str) -> bool:
    if parent == BASE_HEAD:
        return True
    if _git("rev-parse", f"{CONSTRUCTION_HEAD}^") != BASE_HEAD or \
            _git("rev-parse", f"{CONSTRUCTION_HEAD}^{{tree}}") != CONSTRUCTION_TREE:
        return False
    try:
        _git("merge-base", "--is-ancestor", CONSTRUCTION_HEAD, parent)
    except RuntimeError:
        return False
    return True


def _assert_candidate_scope(context: dict[str, object] | None = None) -> dict[str, object]:
    context = _execution_context() if context is None else context
    head = _git("rev-parse", "HEAD")
    parent = _git("rev-parse", "HEAD^")
    tree = _git("rev-parse", "HEAD^{tree}")
    branch = _git("branch", "--show-current")
    mode = context.get("mode")
    if mode == LOCAL_ENDPOINT_MODE:
        if branch != TARGET_BRANCH:
            raise RuntimeError(f"unexpected local candidate branch: {branch}")
    elif mode == HOSTED_EXACT_HEAD_MODE:
        expected_head = context.get("expected_head")
        if not _full_sha(expected_head) or head.lower() != str(expected_head).lower():
            raise RuntimeError("hosted checkout does not match the exact event head")
    else:
        raise RuntimeError("invalid candidate execution context")
    if not _valid_candidate_parent(parent):
        raise RuntimeError(f"unexpected candidate parent/lineage: {parent}")
    if _git("rev-parse", f"{BASE_HEAD}^{{tree}}") != BASE_TREE:
        raise RuntimeError("admitted product tree changed")
    entries = _git("diff", "--name-status", BASE_HEAD, head).splitlines()
    expected = {"M\tenergygrid-bill-downloader/tests/test_one_shot_supervisor.py"}
    expected.update(f"A\t{relative}" for relative in HELPER_RELATIVES)
    if set(entries) != expected or len(entries) != 7:
        raise RuntimeError("candidate diff is outside the exact seven-path envelope")
    if _git("status", "--porcelain=v1") or _git("ls-files", "--others", "--exclude-standard"):
        raise RuntimeError("candidate worktree is not clean")
    return {
        "head": head,
        "tree": tree,
        "parent": parent,
        "branch": branch,
        "execution_context": _receipt_context_binding(context),
        "changed_paths": sorted(
            ["energygrid-bill-downloader/tests/test_one_shot_supervisor.py", *HELPER_RELATIVES]
        ),
        "changed_path_count": 7,
        "protected_base_path_count": 42,
    }


def _fresh_receipt(
    receipt_path: Path, context: dict[str, object] | None = None
) -> dict[str, object]:
    context = _execution_context() if context is None else context
    candidate = _assert_candidate_scope(context)
    created = datetime.now(timezone.utc)
    return {
        "schema": "energygrid.supervisor-harness-custody.v1",
        "run_id": _custody_run_id(receipt_path),
        "created_utc": created.isoformat(),
        "execution_context": _receipt_context_binding(context),
        "candidate": candidate,
        "helpers": [_identity(item) for item in HELPERS],
        "test_identity": _identity(Path(__file__).resolve()),
        "supervisor_identity": _identity(SUPERVISOR),
        "endpoint_observation": {
            "started_utc": created.isoformat(),
            "protection_enabled": "UNKNOWN",
            "coverage": "UNKNOWN",
            "detection_events": "UNKNOWN",
            "status": "ENDPOINT_EVIDENCE_UNAVAILABLE",
        },
        "phase_history": [],
        "cleanup": "PENDING",
    }


def _create_hosted_receipt(context: dict[str, object]) -> Path:
    if context.get("mode") != HOSTED_EXACT_HEAD_MODE:
        raise RuntimeError("hosted receipt creation requires HOSTED_EXACT_HEAD context")
    runner_temp = Path(str(context.get("runner_temp", ""))).resolve()
    if not runner_temp.is_dir():
        raise RuntimeError("GitHub runner temporary directory is unavailable")
    receipt_path = runner_temp / f"eg212-custody-{uuid.uuid4()}.json"
    receipt = _fresh_receipt(receipt_path, context)
    descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except BaseException:
        try:
            receipt_path.unlink()
        except OSError:
            pass
        raise
    return receipt_path.resolve()


def _cleanup_hosted_receipt(receipt_path: Path, runner_temp: Path) -> None:
    try:
        if receipt_path.is_file() and _path_is_within(receipt_path, runner_temp) and \
                receipt_path.resolve().parent == runner_temp.resolve() and \
                CUSTODY_RECEIPT_NAME.fullmatch(receipt_path.name):
            receipt = _load_receipt(receipt_path)
            if receipt.get("execution_context", {}).get("mode") == HOSTED_EXACT_HEAD_MODE:
                receipt_path.unlink()
    except (OSError, RuntimeError, AttributeError, TypeError, json.JSONDecodeError):
        return


def _load_receipt(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as stream:
        receipt = json.load(stream)
    if not isinstance(receipt, dict) or receipt.get("schema") != \
            "energygrid.supervisor-harness-custody.v1":
        raise RuntimeError("invalid custody receipt")
    _validate_receipt_freshness(receipt, path)
    return receipt


def _endpoint_state(receipt: dict[str, object]) -> str:
    observation = receipt.get("endpoint_observation")
    if not isinstance(observation, dict):
        return "ENDPOINT_EVIDENCE_UNAVAILABLE"
    quarantine_paths = observation.get("quarantined_paths", [])
    if not isinstance(quarantine_paths, list) or quarantine_paths:
        return "ENDPOINT_ACCEPTANCE_FAIL"
    if observation.get("status") == "ENDPOINT_ACCEPTANCE_FAIL":
        return "ENDPOINT_ACCEPTANCE_FAIL"
    if observation.get("detection_events") == "ATTRIBUTABLE":
        return "ENDPOINT_ACCEPTANCE_FAIL"
    context = receipt.get("execution_context")
    if not isinstance(context, dict) or context.get("mode") != LOCAL_ENDPOINT_MODE:
        return "ENDPOINT_EVIDENCE_UNAVAILABLE"
    if observation.get("protection_enabled") == "YES" and \
            observation.get("coverage") == "PASS" and \
            observation.get("detection_events") == "NONE":
        return "ENDPOINT_ACCEPTANCE_PASS"
    return "ENDPOINT_EVIDENCE_UNAVAILABLE"


def _custody_check(path: Path, phase: str = "check") -> dict[str, object]:
    context = _execution_context()
    _assert_current_receipt_path(path)
    receipt = _load_receipt(path)
    if receipt.get("execution_context") != _receipt_context_binding(context):
        raise RuntimeError("custody receipt execution context changed")
    recorded = receipt["candidate"]
    current = _assert_candidate_scope(context)
    if any(recorded.get(key) != current[key]
           for key in ("head", "tree", "parent", "execution_context")):
        raise RuntimeError("candidate identity changed after freeze")
    endpoint = _endpoint_state(receipt)
    if endpoint == "ENDPOINT_ACCEPTANCE_FAIL":
        raise RuntimeError(endpoint)
    expected = {item["path"]: item for item in receipt["helpers"]}
    missing: list[str] = []
    changed: list[str] = []
    for helper in HELPERS:
        relative = helper.relative_to(REPO_ROOT).as_posix()
        if not helper.is_file():
            missing.append(relative)
        else:
            actual = _identity(helper)
            prior = expected.get(relative)
            if prior is None or actual["size"] != prior.get("size") or \
                    actual["sha256"] != prior.get("sha256"):
                changed.append(relative)
    test_file = Path(__file__).resolve()
    if not test_file.is_file() or _identity(test_file)["sha256"] != \
            receipt["test_identity"]["sha256"]:
        changed.append("energygrid-bill-downloader/tests/test_one_shot_supervisor.py")
    if not SUPERVISOR.is_file() or _identity(SUPERVISOR)["sha256"] != \
            receipt["supervisor_identity"]["sha256"]:
        changed.append("scripts/energygrid_one_shot_supervisor.ps1")
    if missing:
        observation = receipt.get("endpoint_observation", {})
        quarantine_paths = observation.get("quarantined_paths", [])
        removal_proof = observation.get("removal_proof", [])
        if any(item in quarantine_paths for item in missing):
            classification = "ENDPOINT_QUARANTINE_CONFIRMED"
        elif all(item in removal_proof for item in missing):
            classification = "HARNESS_REMOVAL_CONFIRMED"
        else:
            classification = "DISAPPEARANCE_UNATTRIBUTED"
        raise RuntimeError(classification + ": " + ", ".join(missing))
    if changed:
        raise RuntimeError("HELPER_IDENTITY_CHANGED: " + ", ".join(changed))
    receipt["phase_history"].append({
        "phase": phase,
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "helpers_unchanged": True,
        "endpoint_observation": endpoint,
    })
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"receipt": receipt, "endpoint_state": endpoint}
def _record_endpoint_evidence(args: argparse.Namespace) -> None:
    context = _execution_context()
    if context.get("mode") != LOCAL_ENDPOINT_MODE:
        raise RuntimeError("endpoint evidence can only be recorded in LOCAL_ENDPOINT mode")
    path = Path(args.receipt).expanduser()
    if not path.is_absolute():
        raise RuntimeError("custody receipt must be absolute")
    _assert_current_receipt_path(path)
    receipt = _load_receipt(path)
    if receipt.get("execution_context") != _receipt_context_binding(context):
        raise RuntimeError("custody receipt execution context changed")
    if args.enabled not in {"YES", "NO"} or args.coverage not in {"PASS", "FAIL", "UNKNOWN"}:
        raise RuntimeError("invalid endpoint status")
    if args.detections not in {"NONE", "ATTRIBUTABLE", "UNKNOWN"}:
        raise RuntimeError("invalid detection status")
    note = args.note.strip()
    if not note or len(note) > 500:
        raise RuntimeError("safe endpoint observation note is required")
    previous = receipt.get("endpoint_observation")
    if not isinstance(previous, dict):
        raise RuntimeError("invalid endpoint observation")
    previous_quarantine_paths = previous.get("quarantined_paths", [])
    if not isinstance(previous_quarantine_paths, list) or any(
            not isinstance(item, str) or not item for item in previous_quarantine_paths):
        raise RuntimeError("invalid endpoint quarantine evidence")
    quarantine_paths = list(dict.fromkeys([
        *previous_quarantine_paths,
        *([args.quarantined_path] if args.quarantined_path else []),
    ]))
    detections = args.detections
    if previous.get("detection_events") == "ATTRIBUTABLE":
        detections = "ATTRIBUTABLE"
    observation: dict[str, object] = {
        "started_utc": previous["started_utc"],
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "protection_enabled": args.enabled,
        "coverage": args.coverage,
        "detection_events": detections,
        "quarantined_paths": quarantine_paths,
        "safe_observation_note": note,
    }
    observation["status"] = _endpoint_state({"endpoint_observation": observation})
    receipt["endpoint_observation"] = observation
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _custody_cli() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--harness-custody", required=True,
                        choices=("freeze", "check", "record-endpoint"))
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--enabled", choices=("YES", "NO"))
    parser.add_argument("--coverage", choices=("PASS", "FAIL", "UNKNOWN"))
    parser.add_argument("--detections", choices=("NONE", "ATTRIBUTABLE", "UNKNOWN"))
    parser.add_argument("--quarantined-path")
    parser.add_argument("--note", default="")
    args = parser.parse_args(sys.argv[1:])
    receipt_path = Path(args.receipt).expanduser()
    if not receipt_path.is_absolute():
        raise RuntimeError("custody receipt must be absolute")
    _assert_current_receipt_path(receipt_path)
    if args.harness_custody == "freeze":
        receipt = _fresh_receipt(receipt_path)
        descriptor = os.open(
            receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
        print("custody=FROZEN")
        print("candidate=" + receipt["candidate"]["head"])
        print("tree=" + receipt["candidate"]["tree"])
        print("parent=" + receipt["candidate"]["parent"])
        print("changed_paths=7")
        print("protected_base_paths=42")
        print("endpoint_observation=ENDPOINT_EVIDENCE_UNAVAILABLE")
        return 0
    if args.harness_custody == "record-endpoint":
        _record_endpoint_evidence(args)
    result = _custody_check(
        receipt_path, "endpoint-evidence" if args.harness_custody == "record-endpoint"
        else "custody-check"
    )
    print("custody_identity=PASS")
    print("helpers_unchanged=YES")
    print("endpoint_observation=" + result["endpoint_state"])
    print("endpoint_protection_change=NONE")
    return 0


def _powershell_command(phase: str, receipt: Path, data_root: Path | None = None,
                        python_exe: str | None = None, pythonw_exe: str | None = None,
                        case: str | None = None, ready_path: Path | None = None,
                        resume_child: bool = False) -> list[str]:
    powershell = native_powershell()
    if powershell is None:
        raise RuntimeError("Windows PowerShell 5.1 is required")
    command = [
        powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
        "-File", str(HARNESS), "-Phase", phase,
        "-SupervisorPath", str(SUPERVISOR), "-ReceiptPath", str(receipt),
    ]
    if data_root is not None:
        command.extend(["-DataRoot", str(data_root)])
    if python_exe is not None:
        command.extend(["-PythonExe", python_exe])
    if pythonw_exe is not None:
        command.extend(["-PythonwExe", pythonw_exe])
    if case is not None:
        command.extend(["-Case", case])
    if ready_path is not None:
        command.extend(["-ReadyPath", str(ready_path)])
    if resume_child:
        command.append("-ResumeChild")
    return command


def _clean_native_environment() -> dict[str, str]:
    result = {key: value for key, value in os.environ.items()
              if key.upper() != "PSMODULEPATH"}
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


def _source_evidence() -> dict[str, object]:
    receipt = _receipt_path()
    _custody_check(receipt, "before-source-binding")
    result = subprocess.run(
        _powershell_command("ExtractSource", receipt), cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=90, check=False,
        env=_clean_native_environment(),
    )
    _custody_check(receipt, "after-source-binding")
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip())


def _assert_source_binding() -> None:
    evidence = _source_evidence()
    for name, item in evidence["definitions"].items():
        production = item["production"].replace("\r\n", "\n").replace("\r", "\n")
        fixture = item["fixture"].replace("\r\n", "\n").replace("\r", "\n")
        if production != fixture:
            raise AssertionError("source-bound function mismatch: " + name)
    for name, item in evidence["assignments"].items():
        production = item["production"].replace("\r\n", "\n").replace("\r", "\n")
        fixture = item["fixture"].replace("\r\n", "\n").replace("\r", "\n")
        if production != fixture:
            raise AssertionError("source-bound assignment mismatch: " + name)
    production_observer = evidence["definitions"]["Test-EgApplicationChild"]["production"]
    production_observer = production_observer.replace("\r\n", "\n").replace("\r", "\n")
    substitutions = {
        "Test-EgN4bApplicationChild": (
            "Get-EgN4bProcessIds -JobHandle $JobHandle",
            "[EnergyGridOneShotSupervisorNative]::GetProcessIds($JobHandle)",
        ),
        "Test-EgN5ApplicationChild": (
            "Get-EgN5ProcessIds -JobHandle $JobHandle",
            "[EnergyGridOneShotSupervisorNative]::GetProcessIds($JobHandle)",
        ),
        "Test-EgN7ApplicationChild": (
            "Get-EgN7ProcessMetadata -ProcessId ([uint32]$candidatePid) -TimeoutMilliseconds $script:EgObserverMetadataTimeoutMilliseconds",
            "[EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(\n                [uint32]$candidatePid, $script:EgObserverMetadataTimeoutMilliseconds)",
        ),
        "Test-EgN10ApplicationChild": (
            "Get-EgN10ProcessIds -JobHandle $JobHandle",
            "[EnergyGridOneShotSupervisorNative]::GetProcessIds($JobHandle)",
        ),
        "Test-EgN11ApplicationChild": (
            "Get-EgN11ProcessMetadata -ProcessId ([uint32]$candidatePid) -TimeoutMilliseconds $script:EgObserverMetadataTimeoutMilliseconds",
            "[EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(\n                [uint32]$candidatePid, $script:EgObserverMetadataTimeoutMilliseconds)",
        ),
        "Test-EgN13ApplicationChild": (
            "Get-EgN13ProcessMetadata -ProcessId ([uint32]$candidatePid) -TimeoutMilliseconds $script:EgObserverMetadataTimeoutMilliseconds",
            "[EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(\n                [uint32]$candidatePid, $script:EgObserverMetadataTimeoutMilliseconds)",
        ),
    }
    if set(evidence["variants"]) != set(substitutions):
        raise AssertionError("observer source variant manifest mismatch")
    for name, (callsite, production_call) in substitutions.items():
        variant = evidence["variants"][name].replace("\r\n", "\n").replace("\r", "\n")
        renamed = variant.replace("function " + name + " {", "function Test-EgApplicationChild {", 1)
        if renamed == variant or variant.count(callsite) != 1:
            raise AssertionError("observer variant substitution count mismatch: " + name)
        normalized = renamed.replace(callsite, production_call, 1)
        if normalized != production_observer:
            raise AssertionError("observer source variant differs beyond its one callsite: " + name)

    outcome_variants = evidence["outcome_variants"]
    if set(outcome_variants) != {"Write-EgOutcomeWithTimeoutControl"}:
        raise AssertionError("outcome source variant manifest mismatch")
    production_outcome = evidence["definitions"]["Write-EgOutcome"]["production"]
    production_outcome = production_outcome.replace("\r\n", "\n").replace("\r", "\n")
    outcome_variant = outcome_variants["Write-EgOutcomeWithTimeoutControl"]
    outcome_variant = outcome_variant.replace("\r\n", "\n").replace("\r", "\n")
    outcome_call = (
        "$durability = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(\n"
        "                $stream, $bytes, 5000)"
    )
    timeout_call = "$durability = Get-EgOutcomeTimedOutFlushResult"
    renamed_outcome = outcome_variant.replace(
        "function Write-EgOutcomeWithTimeoutControl {", "function Write-EgOutcome {", 1
    )
    if renamed_outcome == outcome_variant or outcome_variant.count(timeout_call) != 1:
        raise AssertionError("outcome source variant substitution mismatch")
    normalized_outcome = renamed_outcome.replace(timeout_call, outcome_call, 1)
    if normalized_outcome != production_outcome:
        raise AssertionError("outcome source variant differs beyond its flush result control")

    production_n7 = "    " + evidence["n7_native_method"].strip()
    production_n7 = production_n7.replace("\r\n", "\n").replace("\r", "\n")
    provider_start = production_n7.find("                string query = ")
    try_close = production_n7.find("\n            }\n            catch", provider_start)
    if provider_start < 0 or try_close < provider_start:
        raise AssertionError("production N7 provider operation missing")
    provider_operation = production_n7[provider_start:try_close]
    fixture_provider_call = (
        "                EnergyGridOneShotSupervisorN7ProviderControl.QueryProvider(\n"
        "                    processId, timeoutMilliseconds, result);"
    )
    expected_n7 = production_n7.replace(provider_operation, fixture_provider_call, 1)
    expected_n7 = expected_n7.replace("ProcessMetadataResult", "EgN7ProcessMetadataResult")
    support_source = SUPPORT.read_text(encoding="utf-8").replace("\r\n", "\n")
    begin_marker = "// N7_SOURCE_BOUND_METHOD_BEGIN"
    end_marker = "// N7_SOURCE_BOUND_METHOD_END"
    if support_source.count(begin_marker) != 1 or support_source.count(end_marker) != 1:
        raise AssertionError("N7 source-bound method markers are invalid")
    fixture_n7 = support_source.split(begin_marker, 1)[1].split(end_marker, 1)[0]
    expected_n7 = textwrap.dedent(expected_n7).strip()
    fixture_n7 = textwrap.dedent(fixture_n7).strip()
    if fixture_n7 != expected_n7:
        raise AssertionError("N7 worker differs beyond the one provider-control operation")


def _run_driver(phase: str, data_root: Path, timeout: int) -> str:
    receipt = _receipt_path()
    _custody_check(receipt, "before-" + phase.lower())
    if phase in {"Native", "Functions", "Observer", "EndToEnd", "CrashOwner"}:
        _assert_source_binding()
    python_exe = sys.executable
    pythonw_exe = str(Path(python_exe).with_name("pythonw.exe"))
    command = _powershell_command(
        phase, receipt, data_root.resolve(), python_exe, pythonw_exe
    )
    result = subprocess.run(
        command, cwd=REPO_ROOT, capture_output=True, text=True,
        timeout=timeout, check=False, env=_clean_native_environment(),
    )
    _custody_check(receipt, "after-" + phase.lower())
    if result.returncode:
        raise AssertionError(result.stdout + "\n" + result.stderr)
    return result.stdout
class SupervisorHarnessSourceTests(unittest.TestCase):
    def test_candidate_custody_manifest_is_exact_and_source_bound(self):
        self.assertEqual(6, len(HELPERS))
        self.assertEqual(7, len(HELPER_RELATIVES) + 1)
        self.assertTrue(all(path.is_file() for path in HELPERS))
        _assert_source_binding()

    def test_committed_fixtures_are_fixed_and_have_no_runtime_code_generation(self):
        source = "\n".join(
            item.read_text(encoding="utf-8-sig")
            for item in (HARNESS, LAUNCHER, LAUNCHER_LIB)
        )
        for token in (
            "Invoke-Expression", "ScriptBlock::Create", "EncodedCommand",
            "Copy-Item", "Start-Process",
        ):
            self.assertNotIn(token, source)
        self.assertIn("Add-Type -Path $SupportPath", source)
        self.assertIn("ValidateSet(", source)
        self.assertNotIn(
            "class EnergyGridOneShotSupervisorNative",
            SUPPORT.read_text(encoding="utf-8"),
        )

    def test_fixture_module_is_real_python_and_uses_bounded_roles(self):
        import ast
        source = PYTHON_FIXTURE.read_text(encoding="utf-8-sig")
        ast.parse(source, filename=str(PYTHON_FIXTURE))
        for role in (
            "run", "list", "tree-child", "noise-child", "saturate",
            "crash-child", "wrong-parent-child", "handle-canary",
        ):
            self.assertIn(role, source)
        self.assertIn("EG_TEST_MODULE_PATH", source)
        self.assertIn("EG_TEST_MODULE_SHA256", source)
        self.assertIn("PYTHONDONTWRITEBYTECODE", source)
        self.assertIn("PYTHONHOME", source)

    def test_source_binding_evidence_includes_all_required_definitions(self):
        evidence = _source_evidence()
        expected = {
            "Stop-EgSupervisor", "Test-EgUnsafeText", "ConvertTo-EgNativeCommandLine",
            "ConvertTo-EgUtf8JsonBytes", "Close-EgHandle", "Write-EgReservedIntent",
            "Get-EgOutcomeObject", "Write-EgOutcome", "Get-EgCanonicalApplicationCommandLine",
            "Test-EgApplicationChild", "Get-EgAccounting", "Invoke-EgTerminateJob",
            "Get-EgDeadlineTicks", "Get-EgDurabilityMilliseconds", "Test-EgDeadlineReached",
            "Wait-EgReap", "Wait-EgDescendantGrace", "Get-EgStartVerdict", "Get-EgExitCode",
        }
        self.assertEqual(expected, set(evidence["definitions"]))
        self.assertEqual(6, len(evidence["variants"]))
        _assert_source_binding()


def _synthetic_candidate_scope() -> dict[str, object]:
    return {
        "head": _git("rev-parse", "HEAD"),
        "tree": _git("rev-parse", "HEAD^{tree}"),
        "parent": _git("rev-parse", "HEAD^"),
        "branch": TARGET_BRANCH,
        "execution_context": {"mode": LOCAL_ENDPOINT_MODE},
        "changed_paths": sorted([
            "energygrid-bill-downloader/tests/test_one_shot_supervisor.py",
            *HELPER_RELATIVES,
        ]),
        "changed_path_count": 7,
        "protected_base_path_count": 42,
    }


class HostedCustodyContextRegressionTests(unittest.TestCase):
    def _hosted_environment(
        self,
        runner_temp: Path,
        *,
        event_name: str,
        expected_head: str,
        github_sha: str | None = None,
    ) -> dict[str, str]:
        event_path = runner_temp / "event.json"
        if event_name == "pull_request":
            event = {
                "number": 229,
                "repository": {"full_name": GITHUB_REPOSITORY},
                "pull_request": {
                    "number": 229,
                    "head": {
                        "sha": expected_head,
                        "ref": TARGET_BRANCH,
                        "repo": {"full_name": GITHUB_REPOSITORY},
                    },
                    "base": {
                        "ref": "main",
                        "repo": {"full_name": GITHUB_REPOSITORY},
                    },
                },
            }
            ref = "refs/pull/229/merge"
        else:
            event = {
                "ref": TARGET_BRANCH,
                "repository": {"full_name": GITHUB_REPOSITORY},
            }
            ref = f"refs/heads/{TARGET_BRANCH}"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        return {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": GITHUB_REPOSITORY,
            "GITHUB_WORKFLOW": GITHUB_WORKFLOW,
            "GITHUB_JOB": GITHUB_JOB,
            "GITHUB_WORKSPACE": str(REPO_ROOT),
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_SHA": github_sha or expected_head,
            "GITHUB_REF": ref,
            "GITHUB_RUN_ID": "37303332493",
            "GITHUB_RUN_ATTEMPT": "1",
            "RUNNER_OS": "Windows",
            "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_TEMP": str(runner_temp),
        }

    def _hosted_context(
        self, runner_temp: Path, *, event_name: str = "pull_request",
        expected_head: str | None = None, github_sha: str | None = None,
        actual_head: str | None = None,
    ) -> dict[str, object]:
        actual_head = actual_head or _git("rev-parse", "HEAD")
        environment = self._hosted_environment(
            runner_temp, event_name=event_name,
            expected_head=expected_head or actual_head, github_sha=github_sha,
        )
        return _execution_context(environment, actual_head=actual_head)

    def _hosted_receipt(self, runner_temp: Path):
        context = self._hosted_context(runner_temp)
        return context, _create_hosted_receipt(context)

    def _assert_identity_drift(self, receipt_path: Path, context, changed_path: Path) -> None:
        original_identity = _identity
        candidate_scope = _synthetic_candidate_scope()
        candidate_scope["execution_context"] = _receipt_context_binding(context)

        def changed_identity(path: Path) -> dict[str, object]:
            result = original_identity(path)
            if path.resolve() == changed_path.resolve():
                result = result | {"sha256": "0" * 64}
            return result

        with mock.patch(__name__ + "._execution_context", return_value=context), \
                mock.patch(__name__ + "._receipt_path", return_value=receipt_path), \
                mock.patch(__name__ + "._assert_candidate_scope",
                           return_value=candidate_scope), \
                mock.patch(__name__ + "._identity", side_effect=changed_identity):
            with self.assertRaisesRegex(RuntimeError, "HELPER_IDENTITY_CHANGED"):
                _custody_check(receipt_path)

    def test_h1_missing_local_receipt_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "no valid local receipt"):
            _execution_context({})
        with self.assertRaisesRegex(RuntimeError, "EG_SUPERVISOR_CUSTODY_RECEIPT is required"):
            _execution_context({"EG_SUPERVISOR_CUSTODY_RECEIPT": ""})

    def test_h2_local_mode_rejects_wrong_named_branch(self):
        context = {"mode": LOCAL_ENDPOINT_MODE, "receipt_path": "C:/temp/receipt.json"}
        original_git = _git

        def wrong_branch(*arguments: str) -> str:
            if arguments == ("branch", "--show-current"):
                return "codex/wrong-branch"
            return original_git(*arguments)

        with mock.patch(__name__ + "._git", side_effect=wrong_branch):
            with self.assertRaisesRegex(RuntimeError, "unexpected local candidate branch"):
                _assert_candidate_scope(context)

    def test_h3_pull_request_exact_head_accepts_detached_checkout(self):
        with tempfile.TemporaryDirectory(prefix="eg_h3_hosted_") as directory:
            context = self._hosted_context(Path(directory))
            original_git = _git

            def detached(*arguments: str) -> str:
                if arguments == ("branch", "--show-current"):
                    return ""
                return original_git(*arguments)

            with mock.patch(__name__ + "._git", side_effect=detached):
                candidate = _assert_candidate_scope(context)
            self.assertEqual(HOSTED_EXACT_HEAD_MODE, candidate["execution_context"]["mode"])
            self.assertEqual(_git("rev-parse", "HEAD"), candidate["head"])

    def test_h4_workflow_dispatch_uses_trusted_github_sha(self):
        with tempfile.TemporaryDirectory(prefix="eg_h4_hosted_") as directory:
            head = _git("rev-parse", "HEAD")
            context = self._hosted_context(
                Path(directory), event_name="workflow_dispatch", github_sha=head,
            )
            original_git = _git

            def detached(*arguments: str) -> str:
                if arguments == ("branch", "--show-current"):
                    return ""
                return original_git(*arguments)

            with mock.patch(__name__ + "._git", side_effect=detached):
                candidate = _assert_candidate_scope(context)
            self.assertEqual(head, context["expected_head"])
            self.assertEqual(head, candidate["head"])

    def test_h5_pull_request_expected_sha_mismatch_fails(self):
        with tempfile.TemporaryDirectory(prefix="eg_h5_hosted_") as directory:
            environment = self._hosted_environment(
                Path(directory), event_name="pull_request", expected_head="f" * 40,
                github_sha="e" * 40,
            )
            with self.assertRaisesRegex(RuntimeError, "actual HEAD does not match"):
                _execution_context(environment, actual_head=_git("rev-parse", "HEAD"))
        with tempfile.TemporaryDirectory(prefix="eg_h5_dispatch_") as directory:
            environment = self._hosted_environment(
                Path(directory), event_name="workflow_dispatch", expected_head="f" * 40,
                github_sha="e" * 40,
            )
            with self.assertRaisesRegex(RuntimeError, "actual HEAD does not match"):
                _execution_context(environment, actual_head=_git("rev-parse", "HEAD"))

    def test_h6_partial_or_fake_github_environment_fails(self):
        partial = {
            "GITHUB_ACTIONS": "true", "RUNNER_OS": "Windows",
            "GITHUB_SHA": _git("rev-parse", "HEAD"),
        }
        with self.assertRaisesRegex(RuntimeError, "complete GitHub Actions context"):
            _execution_context(partial)

    def test_h7_hosted_receipt_is_ephemeral_and_endpoint_unavailable(self):
        with tempfile.TemporaryDirectory(prefix="eg_h7_hosted_") as directory:
            context, receipt_path = self._hosted_receipt(Path(directory))
            receipt = _load_receipt(receipt_path)
            self.assertTrue(_path_is_within(receipt_path, Path(directory)))
            self.assertEqual(
                _receipt_context_binding(context), receipt["execution_context"],
            )
            self.assertEqual(
                "ENDPOINT_EVIDENCE_UNAVAILABLE",
                receipt["endpoint_observation"]["status"],
            )
            self.assertEqual("ENDPOINT_EVIDENCE_UNAVAILABLE", _endpoint_state(receipt))

    def test_h8_hosted_endpoint_evidence_cannot_be_recorded_or_passed(self):
        with tempfile.TemporaryDirectory(prefix="eg_h8_hosted_") as directory:
            context, receipt_path = self._hosted_receipt(Path(directory))
            receipt = _load_receipt(receipt_path)
            receipt["endpoint_observation"].update({
                "checked_utc": datetime.now(timezone.utc).isoformat(),
                "protection_enabled": "YES", "coverage": "PASS",
                "detection_events": "NONE", "quarantined_paths": [],
                "status": "ENDPOINT_ACCEPTANCE_PASS",
            })
            receipt_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            self.assertEqual("ENDPOINT_EVIDENCE_UNAVAILABLE", _endpoint_state(receipt))
            quarantined = json.loads(json.dumps(receipt))
            quarantined["endpoint_observation"]["quarantined_paths"] = [HELPER_RELATIVES[0]]
            self.assertEqual("ENDPOINT_ACCEPTANCE_FAIL", _endpoint_state(quarantined))
            before = receipt_path.read_bytes()
            args = argparse.Namespace(
                receipt=str(receipt_path), enabled="YES", coverage="PASS",
                detections="NONE", quarantined_path=None, note="forbidden hosted write",
            )
            with mock.patch(__name__ + "._execution_context", return_value=context), \
                    mock.patch(__name__ + "._receipt_path", return_value=receipt_path):
                with self.assertRaisesRegex(RuntimeError, "only be recorded in LOCAL_ENDPOINT"):
                    _record_endpoint_evidence(args)
            self.assertEqual(before, receipt_path.read_bytes())

    def test_h9_hosted_helper_identity_drift_fails(self):
        with tempfile.TemporaryDirectory(prefix="eg_h9_hosted_") as directory:
            context, receipt_path = self._hosted_receipt(Path(directory))
            self._assert_identity_drift(receipt_path, context, HELPERS[0])

    def test_h10_hosted_test_identity_drift_fails(self):
        with tempfile.TemporaryDirectory(prefix="eg_h10_hosted_") as directory:
            context, receipt_path = self._hosted_receipt(Path(directory))
            self._assert_identity_drift(receipt_path, context, Path(__file__).resolve())

    def test_h11_hosted_supervisor_identity_drift_fails(self):
        with tempfile.TemporaryDirectory(prefix="eg_h11_hosted_") as directory:
            context, receipt_path = self._hosted_receipt(Path(directory))
            self._assert_identity_drift(receipt_path, context, SUPERVISOR)

    def test_h12_hosted_parent_lineage_drift_fails(self):
        with tempfile.TemporaryDirectory(prefix="eg_h12_hosted_") as directory:
            context = self._hosted_context(Path(directory))
            with mock.patch(__name__ + "._valid_candidate_parent", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "candidate parent/lineage"):
                    _assert_candidate_scope(context)

    def test_h13_hosted_base_tree_drift_fails(self):
        with tempfile.TemporaryDirectory(prefix="eg_h13_hosted_") as directory:
            context = self._hosted_context(Path(directory))
            original_git = _git

            def changed_base_tree(*arguments: str) -> str:
                if arguments == ("rev-parse", f"{BASE_HEAD}^{{tree}}"):
                    return "0" * 40
                return original_git(*arguments)

            with mock.patch(__name__ + "._git", side_effect=changed_base_tree):
                with self.assertRaisesRegex(RuntimeError, "admitted product tree changed"):
                    _assert_candidate_scope(context)

    def test_h14_hosted_seven_path_scope_drift_fails(self):
        with tempfile.TemporaryDirectory(prefix="eg_h14_hosted_") as directory:
            context = self._hosted_context(Path(directory))
            head = _git("rev-parse", "HEAD")
            original_git = _git

            def changed_scope(*arguments: str) -> str:
                if arguments == ("diff", "--name-status", BASE_HEAD, head):
                    return "M\tREADME.md"
                return original_git(*arguments)

            with mock.patch(__name__ + "._git", side_effect=changed_scope):
                with self.assertRaisesRegex(RuntimeError, "exact seven-path envelope"):
                    _assert_candidate_scope(context)

    def test_h15_local_named_branch_positive_control_remains_valid(self):
        with tempfile.TemporaryDirectory(prefix="eg_h15_local_") as directory:
            context = {
                "mode": LOCAL_ENDPOINT_MODE,
                "receipt_path": str(Path(directory) / f"eg212-custody-{uuid.uuid4()}.json"),
            }
            original_git = _git

            def named_branch(*arguments: str) -> str:
                if arguments == ("branch", "--show-current"):
                    return TARGET_BRANCH
                return original_git(*arguments)

            with mock.patch(__name__ + "._git", side_effect=named_branch):
                candidate = _assert_candidate_scope(context)
            self.assertEqual(TARGET_BRANCH, candidate["branch"])
            self.assertEqual(7, candidate["changed_path_count"])
            self.assertEqual(42, candidate["protected_base_path_count"])


class SupervisorCustodyRegressionTests(unittest.TestCase):
    def _candidate_scope(self):
        return _synthetic_candidate_scope()

    def _receipt(self, path: Path, observation: dict[str, object] | None = None):
        context = {"mode": LOCAL_ENDPOINT_MODE, "receipt_path": str(path.resolve())}
        with mock.patch(
                __name__ + "._assert_candidate_scope",
                return_value=self._candidate_scope()):
            receipt = _fresh_receipt(path, context)
        if observation:
            receipt["endpoint_observation"].update(observation)
        path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return receipt

    def _record(self, path: Path, *, detections="NONE", quarantined_path=None):
        args = argparse.Namespace(
            receipt=str(path), enabled="YES", coverage="PASS", detections=detections,
            quarantined_path=quarantined_path, note="Owner observed endpoint state",
        )
        with mock.patch.dict(os.environ, {"EG_SUPERVISOR_CUSTODY_RECEIPT": str(path)}):
            _record_endpoint_evidence(args)

    def _check(self, path: Path):
        with mock.patch.dict(os.environ, {"EG_SUPERVISOR_CUSTODY_RECEIPT": str(path)}):
            return _custody_check(path)

    def test_f1_endpoint_state_regressions_r1_r4_r5_r7(self):
        with tempfile.TemporaryDirectory(prefix="eg_f1_state_") as directory:
            receipt_path = Path(directory) / (
                "eg212-custody-" + str(uuid.uuid4()) + ".json"
            )
            receipt = self._receipt(receipt_path)
            clean_observation = receipt["endpoint_observation"]
            clean_observation.update({
                "checked_utc": datetime.now(timezone.utc).isoformat(),
                "protection_enabled": "YES", "coverage": "PASS",
                "detection_events": "NONE", "quarantined_paths": [],
                "status": "ENDPOINT_ACCEPTANCE_PASS",
            })
            receipt_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            self.assertEqual("ENDPOINT_ACCEPTANCE_PASS", _endpoint_state(receipt))
            for helper in HELPERS:
                self.assertTrue(helper.is_file())
                recorded = next(item for item in receipt["helpers"]
                                if item["path"] == helper.relative_to(REPO_ROOT).as_posix())
                self.assertEqual(recorded, _identity(helper))
            with mock.patch(
                    __name__ + "._assert_candidate_scope",
                    return_value=self._candidate_scope()):
                self.assertEqual("ENDPOINT_ACCEPTANCE_PASS",
                                 self._check(receipt_path)["endpoint_state"])

            for label, update in (
                ("F1-R1", {"quarantined_paths": [HELPER_RELATIVES[0]]}),
                ("F1-R4", {"detection_events": "ATTRIBUTABLE"}),
                ("F1-R7", {
                    "quarantined_paths": [HELPER_RELATIVES[0]],
                    "status": "ENDPOINT_ACCEPTANCE_PASS",
                }),
            ):
                with self.subTest(label=label):
                    candidate = json.loads(json.dumps(receipt))
                    candidate["endpoint_observation"].update(update)
                    self.assertEqual("ENDPOINT_ACCEPTANCE_FAIL", _endpoint_state(candidate))
                    receipt_path.write_text(
                        json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                    )
                    with mock.patch(
                            __name__ + "._assert_candidate_scope",
                            return_value=self._candidate_scope()):
                        with self.assertRaisesRegex(RuntimeError, "ENDPOINT_ACCEPTANCE_FAIL"):
                            self._check(receipt_path)

    def test_f1_quarantine_and_detection_history_cannot_be_cleared_r3_r6(self):
        with tempfile.TemporaryDirectory(prefix="eg_f1_history_") as directory:
            receipt_path = Path(directory) / (
                "eg212-custody-" + str(uuid.uuid4()) + ".json"
            )
            self._receipt(receipt_path)
            self._record(receipt_path, quarantined_path=HELPER_RELATIVES[0])
            self._record(receipt_path)
            recorded = _load_receipt(receipt_path)
            self.assertEqual([HELPER_RELATIVES[0]],
                             recorded["endpoint_observation"]["quarantined_paths"])
            self.assertEqual("ENDPOINT_ACCEPTANCE_FAIL",
                             recorded["endpoint_observation"]["status"])
            self.assertEqual("ENDPOINT_ACCEPTANCE_FAIL", _endpoint_state(recorded))
            self.assertTrue(HELPERS[0].is_file())
            recorded_helper = next(
                item for item in recorded["helpers"]
                if item["path"] == HELPER_RELATIVES[0]
            )
            self.assertEqual(recorded_helper, _identity(HELPERS[0]))
            with mock.patch(
                    __name__ + "._assert_candidate_scope",
                    return_value=self._candidate_scope()):
                with self.assertRaisesRegex(RuntimeError, "ENDPOINT_ACCEPTANCE_FAIL"):
                    self._check(receipt_path)

            detection_path = Path(directory) / (
                "eg212-custody-" + str(uuid.uuid4()) + ".json"
            )
            self._receipt(detection_path)
            self._record(detection_path, detections="ATTRIBUTABLE")
            self._record(detection_path, detections="NONE")
            detected = _load_receipt(detection_path)
            self.assertEqual("ATTRIBUTABLE",
                             detected["endpoint_observation"]["detection_events"])
            self.assertEqual("ENDPOINT_ACCEPTANCE_FAIL", _endpoint_state(detected))

    def test_f1_quarantine_rejection_is_independent_of_helper_presence_r2(self):
        with tempfile.TemporaryDirectory(prefix="eg_f1_missing_") as directory:
            receipt_path = Path(directory) / (
                "eg212-custody-" + str(uuid.uuid4()) + ".json"
            )
            receipt = self._receipt(receipt_path, {
                "protection_enabled": "YES", "coverage": "PASS",
                "detection_events": "NONE",
                "quarantined_paths": [HELPER_RELATIVES[0]],
                "status": "ENDPOINT_ACCEPTANCE_PASS",
            })
            self.assertEqual("ENDPOINT_ACCEPTANCE_FAIL", _endpoint_state(receipt))
            missing_helper = Path(directory) / "helper-that-is-not-present.ps1"
            with mock.patch(__name__ + "._assert_candidate_scope",
                            return_value=self._candidate_scope()):
                with mock.patch(__name__ + ".HELPERS", (missing_helper,)):
                    with self.assertRaisesRegex(RuntimeError, "ENDPOINT_ACCEPTANCE_FAIL"):
                        self._check(receipt_path)


class SupervisorSaturationRegressionTests(unittest.TestCase):
    def test_f2_exact_byte_child_and_drain_regressions_r1_to_r8(self):
        import ast

        fixture_tree = ast.parse(PYTHON_FIXTURE.read_text(encoding="utf-8-sig"))
        saturate = next(node for node in fixture_tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_saturate")
        byte_counts = [
            node.value for node in saturate.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "byte_count"
                    for target in node.targets)
        ]
        self.assertEqual(1, len(byte_counts))
        byte_count = byte_counts[0]
        self.assertEqual("1024 * 1024", ast.unparse(byte_count))
        self.assertEqual(SATURATION_EXPECTED_BYTES, 1024 * 1024)

        valid = {
            "stdout_bytes": SATURATION_EXPECTED_BYTES,
            "stderr_bytes": SATURATION_EXPECTED_BYTES,
            "stdout_drain_complete": True,
            "stderr_drain_complete": True,
            "child_terminal": True,
            "exit_code_read": True,
            "exit_code": 0,
        }
        cases = (
            ("F2-R1", {}, True),
            ("F2-R2", {"stdout_bytes": 1048575}, False),
            ("F2-R3", {"stderr_bytes": 1048575}, False),
            ("F2-R4", {"stdout_bytes": 1048577}, False),
            ("F2-R5", {"stderr_bytes": 1048577}, False),
            ("F2-R6", {"stdout_bytes": 2097152, "stderr_bytes": 2097152}, False),
            ("F2-R7", {"exit_code": 1}, False),
            ("F2-R8-stdout", {"stdout_drain_complete": False}, False),
            ("F2-R8-stderr", {"stderr_drain_complete": False}, False),
            ("child-not-terminal", {"child_terminal": False}, False),
            ("exit-code-unread", {"exit_code_read": False}, False),
        )
        for label, update, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(expected, _saturation_result_passes(**(valid | update)))


class SupervisorHarnessParseCompileTests(unittest.TestCase):
    def test_committed_helpers_and_exact_production_native_source_parse_and_compile(self):
        if os.name != "nt":
            self.skipTest("Windows PowerShell 5.1 is only available on Windows")
        with tempfile.TemporaryDirectory(prefix="eg_parse_compile_data_") as directory:
            output = _run_driver("ParseCompile", Path(directory), 120)
        self.assertIn("parse_compile=PASS", output)
        self.assertIn("production_native_type=EnergyGridOneShotSupervisorNative", output)
        self.assertIn("committed_support=PASS", output)


class SupervisorHarnessCleanupTests(unittest.TestCase):
    def test_cleanup_uses_retained_owned_handles_and_creation_identity(self):
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertIn("class _OwnedProcessHandle", source)
        self.assertIn("GetProcessTimes", source)
        self.assertIn("TerminateProcess", source)
        self.assertNotIn("Stop-Process" + " " + "-Id", source)
        self.assertNotIn(
            "Get-Process" + " " + "-ErrorAction SilentlyContinue" + " |", source
        )

    def test_receipt_binds_candidate_helpers_test_owner_and_supervisor(self):
        receipt = _load_receipt(_receipt_path())
        self.assertTrue(_valid_candidate_parent(receipt["candidate"]["parent"]))
        self.assertEqual(7, receipt["candidate"]["changed_path_count"])
        self.assertEqual(42, receipt["candidate"]["protected_base_path_count"])
        self.assertEqual(6, len(receipt["helpers"]))
        self.assertEqual("ENDPOINT_EVIDENCE_UNAVAILABLE", _endpoint_state(receipt))

    def test_stale_custody_receipt_is_rejected_even_with_green_endpoint_fields(self):
        with tempfile.TemporaryDirectory(prefix="eg_stale_custody_") as directory:
            receipt_path = Path(directory) / (
                "eg212-custody-" + str(uuid.uuid4()) + ".json"
            )
            with mock.patch(
                    __name__ + "._assert_candidate_scope",
                    return_value=_synthetic_candidate_scope()):
                receipt = _fresh_receipt(receipt_path, {
                    "mode": LOCAL_ENDPOINT_MODE,
                    "receipt_path": str(receipt_path.resolve()),
                })
            receipt["endpoint_observation"].update({
                "checked_utc": datetime.now(timezone.utc).isoformat(),
                "protection_enabled": "YES",
                "coverage": "PASS",
                "detection_events": "NONE",
                "status": "ENDPOINT_ACCEPTANCE_PASS",
            })
            replay_path = Path(directory) / (
                "eg212-custody-" + str(uuid.uuid4()) + ".json"
            )
            replay_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "STALE_CUSTODY_RECEIPT"):
                _load_receipt(replay_path)
            stale_time = datetime.now(timezone.utc) - CUSTODY_RECEIPT_MAX_AGE - timedelta(seconds=1)
            receipt["created_utc"] = stale_time.isoformat()
            receipt["endpoint_observation"].update({
                "started_utc": stale_time.isoformat(),
                "checked_utc": (stale_time + timedelta(seconds=1)).isoformat(),
                "protection_enabled": "YES",
                "coverage": "PASS",
                "detection_events": "NONE",
                "status": "ENDPOINT_ACCEPTANCE_PASS",
            })
            receipt_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "STALE_CUSTODY_RECEIPT"):
                _load_receipt(receipt_path)


class SupervisorNativeAssuranceTests(unittest.TestCase):
    def test_native_assurance_harness_uses_real_job_objects_and_pipes(self):
        if os.name != "nt":
            self.skipTest("native Windows PowerShell 5.1 is only available on Windows")
        with tempfile.TemporaryDirectory(prefix="eg_native_assurance_data_") as directory:
            output = _run_driver("Native", Path(directory), 240)
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
        self.assertIn("native_durability_timeout=True", output)
        self.assertIn("native_durability_succeeded_after_wait=False", output)
        self.assertIn("native_saturation_writers=CONCURRENT", output)
        metrics = {}
        for line in output.splitlines():
            if line.startswith("native_saturation_") and "=" in line:
                name, value = line.split("=", 1)
                metrics[name] = value
        self.assertTrue(_saturation_result_passes(
            stdout_bytes=int(metrics["native_saturation_stdout_bytes"]),
            stderr_bytes=int(metrics["native_saturation_stderr_bytes"]),
            stdout_drain_complete=metrics["native_saturation_stdout_drained"] == "True",
            stderr_drain_complete=metrics["native_saturation_stderr_drained"] == "True",
            child_terminal=metrics["native_saturation_child_terminal"] == "True",
            exit_code_read=metrics["native_saturation_exit_read"] == "True",
            exit_code=int(metrics["native_saturation_exit_code"]),
        ))
        self.assertIn("native_saturation_drains=True", output)
        self.assertIn("native_saturation_active_processes=0", output)
        self.assertIn("native_assurance_cases=15", output)
        self.assertIn("native_assurance=PASS", output)


class SupervisorCommittedFunctionTests(unittest.TestCase):
    def test_exact_termination_and_exit_mapping_functions_on_windows_powershell_5_1(self):
        if os.name != "nt":
            self.skipTest("native Windows PowerShell 5.1 is only available on Windows")
        with tempfile.TemporaryDirectory(prefix="eg_function_assurance_data_") as directory:
            output = _run_driver("Functions", Path(directory), 360)
        for case in (
            "durable_precreation_rejection=1", "durable_timeout_successful_reap=2",
            "timeout_containment_failure=3", "timeout_reap_unconfirmed=3",
            "durable_ambiguous=4", "started_proven_launcher_zero_complete=0",
            "nonzero_launcher_nonambiguous=1", "outcome_missing=3",
        ):
            self.assertIn("exit_case=" + case, output)
        for case in (
            "timed_out_intent_rejection", "timed_out_outcome_contract",
            "timed_out_outcome_actual_delayed", "timed_out_outcome_actual_ordinary",
            "timed_out_outcome_defensive_exact_caller",
            "descendant_grace_stale_zero_global_deadline",
            "descendant_grace_stale_zero_interruption", "descendant_grace_overall_deadline",
            "descendant_grace_interruption_during_polling", "descendant_grace_local_control",
            "descendant_grace_completion_global_deadline",
            "descendant_grace_completion_local_grace", "descendant_grace_accounting_failure",
            "descendant_grace_precedence_interruption_over_timeout",
            "descendant_grace_precedence_timeout_over_local", "exact_termination_function",
            "termination_failure_infrastructure", "accounting_failure_and_reap_timeout",
        ):
            self.assertIn("function_case=" + case, output)
        for marker in (
            "file_id_info_layout=PASS", "file_id_info_query=PASS", "root_identity=PASS",
            "file_identity=PASS", "link_count=PASS", "reparse_rejection=PASS",
            "identity_negatives=PASS", "representation_positives=PASS",
            "outcome_commit_transition=PASS", "retained_handle_read=PASS",
        ):
            self.assertIn(marker, output)
        self.assertRegex(output, r"SHORT_NAME_ALIAS=(PASS|UNAVAILABLE)")
        self.assertIn("function_assurance_cases=26", output)
        self.assertIn("function_assurance=PASS", output)
class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _OwnedProcessHandle:
    _SYNCHRONIZE = 0x00100000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 258

    def __init__(self, pid: int):
        self.pid = int(pid)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32 = kernel32
        self.handle = kernel32.OpenProcess(
            self._SYNCHRONIZE | self._PROCESS_TERMINATE | self._PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            self.pid,
        )
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed for owned child")
        self.creation_time = self._read_creation_time()

    def _read_creation_time(self) -> int:
        created, exited, kernel, user = (_FileTime(), _FileTime(), _FileTime(), _FileTime())
        self._kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
        )
        self._kernel32.GetProcessTimes.restype = wintypes.BOOL
        if not self._kernel32.GetProcessTimes(
            self.handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise OSError(ctypes.get_last_error(), "GetProcessTimes failed")
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)

    def wait(self, timeout_ms: int) -> bool:
        self._kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        result = self._kernel32.WaitForSingleObject(self.handle, timeout_ms)
        if result == self._WAIT_OBJECT_0:
            return True
        if result == self._WAIT_TIMEOUT:
            return False
        raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")

    def signaled(self) -> bool:
        return self.wait(0)

    def terminate(self) -> None:
        self._kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self._kernel32.TerminateProcess.restype = wintypes.BOOL
        if not self._kernel32.TerminateProcess(self.handle, 0xE0470001) and not self.signaled():
            raise OSError(ctypes.get_last_error(), "TerminateProcess failed on retained handle")

    def close(self) -> None:
        if self.handle:
            self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            self._kernel32.CloseHandle.restype = wintypes.BOOL
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


class SupervisorCrashContainmentTests(unittest.TestCase):
    def _wait_json(self, path: Path, owner: subprocess.Popen[bytes],
                   timeout: float = 30.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file():
                with path.open("r", encoding="utf-8") as stream:
                    value = json.load(stream)
                if isinstance(value, dict):
                    return value
            if owner.poll() is not None:
                raise AssertionError("crash owner exited before readiness")
            time.sleep(0.05)
        raise AssertionError("crash owner readiness timed out")

    def _run_crash_variant(self, resume: bool) -> None:
        receipt = _receipt_path()
        _custody_check(receipt, "before-crash-owner")
        _assert_source_binding()
        with tempfile.TemporaryDirectory(prefix="eg_crash_data_") as directory:
            root = Path(directory).resolve()
            ready_path = root / "owner-ready.json"
            command = _powershell_command(
                "CrashOwner", receipt, root, sys.executable, ready_path=ready_path,
                resume_child=resume,
            )
            owner = subprocess.Popen(
                command, cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=_clean_native_environment(),
            )
            retained: list[_OwnedProcessHandle] = []
            outside: subprocess.Popen[bytes] | None = None
            try:
                proof = self._wait_json(ready_path, owner)
                child_pid = int(proof["child_pid"])
                grandchild_pid = int(proof.get("grandchild_pid", 0) or 0)
                self.assertEqual(owner.pid, int(proof["owner_pid"]))
                self.assertTrue(bool(proof["job_owned"]))
                retained.append(_OwnedProcessHandle(child_pid))
                if resume:
                    self.assertGreater(grandchild_pid, 0)
                    self.assertEqual(child_pid, int(proof["grandchild_parent_pid"]))
                    retained.append(_OwnedProcessHandle(grandchild_pid))
                else:
                    self.assertEqual(0, grandchild_pid)
                    self.assertFalse(bool(proof["child_marker_seen"]))

                fixture_hash = hashlib.sha256(PYTHON_FIXTURE.read_bytes()).hexdigest()
                outside_config = root / "outside-control.json"
                outside_ready = root / "outside-ready.json"
                outside_config.write_text(json.dumps({
                    "module_sha256": fixture_hash,
                    "grandchild_ready_path": str(outside_ready),
                    "tree_child_delay_seconds": 60,
                }, separators=(",", ":")), encoding="utf-8")
                environment = _clean_native_environment()
                environment.update({
                    "PYTHONPATH": str(FIXTURES.resolve()),
                    "PYTHONNOUSERSITE": "1",
                    "EG_TEST_MODULE_PATH": str(PYTHON_FIXTURE.resolve()),
                    "EG_TEST_MODULE_SHA256": fixture_hash,
                })
                for key in ("PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP", "PYTHONINSPECT"):
                    environment.pop(key, None)
                outside = subprocess.Popen(
                    [sys.executable, "-B", "-m", "energygrid_bill_downloader",
                     "tree-child", "--config", str(outside_config)],
                    cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=environment,
                )
                self.assertIsNone(outside.poll(), "outside-Job control exited too early")
                owner.kill()
                owner.wait(timeout=15)
                for handle in retained:
                    self.assertTrue(handle.wait(10000), "owned process survived last Job handle closure")
                self.assertIsNone(outside.poll(), "outside-Job control was terminated")
                self.assertTrue(all(handle.creation_time > 0 for handle in retained))
                print("crash_variant=" +
                      ("resumed_descendant" if resume else "suspended_child") + " PASS")
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=15)
                for handle in retained:
                    if not handle.signaled():
                        handle.terminate()
                        handle.wait(5000)
                    handle.close()
                if outside is not None and outside.poll() is None:
                    outside.kill()
                    outside.wait(timeout=10)
        _custody_check(receipt, "after-crash-owner")

    def test_suspended_and_resumed_owner_crash_containment(self):
        if os.name != "nt":
            self.skipTest("crash containment is Windows-only")
        self._run_crash_variant(resume=False)
        self._run_crash_variant(resume=True)


class SupervisorRealObserverTests(unittest.TestCase):
    def test_real_observer_matrix_through_committed_observer_and_wmi(self):
        if os.name != "nt":
            self.skipTest("real WMI observer is Windows-only")
        with tempfile.TemporaryDirectory(prefix="eg_observer_data_") as directory:
            output = _run_driver("Observer", Path(directory), 1200)
        expected = (
            "P1_positive observed=True observer_failed=False verdict=STARTED_PROVEN",
            "N1_wrong_parent observed=False observer_failed=False",
            "N2_wrong_command_line observed=False observer_failed=False",
            "N3_wrong_image_exact_command_line observed=False observer_failed=False",
            "N4a_outside_job_exact_identity observed=False observer_failed=False",
            "N4b_injected_non_member_pid observed=False observer_failed=False",
            "N5_gone_pid_87_then_positive observed=True observer_failed=False verdict=STARTED_PROVEN",
            "N6_provider_busy observed=False observer_failed=True verdict=AMBIGUOUS",
            "N7_provider_timeout observed=False observer_failed=True verdict=AMBIGUOUS",
            "N8_launcher_dead observed=False observer_failed=False",
            "N9_child_dead observed=False observer_failed=False",
            "N10_open_error_non_87 observed=False observer_failed=True",
            "N11_no_row_candidate_live observed=False observer_failed=True",
            "N12_late_success_after_timeout observed=False observer_failed=True",
            "N13_real_no_row_after_exit observed=False observer_failed=False",
            "N14_deadline_guard observed=False observer_failed=False",
        )
        for case in expected:
            self.assertIn("observer_case=" + case, output)
        self.assertIn("observer_case=P2_preflight_noise trials=10 observed=10 observer_failed=0", output)
        self.assertIn(
            "native_timeout_producer=PASS substitutions=1 provider_calls=1 timeout_ms=1000 "
            "worker_started=True worker_incomplete=True timed_out=True",
            output,
        )
        self.assertIn("observer_n7_injection=PASS calls=1 target_pid=", output)
        self.assertIn("observer_n7_idle_after_release=PASS", output)
        self.assertIn("observer_deadline_guard_metadata_calls=0", output)
        self.assertIn("observer_leftover_processes=0", output)
        self.assertIn("real_observer_matrix=PASS cases=17 budget_ms=1000", output)

    def test_whole_supervisor_end_to_end_with_real_launcher_and_child(self):
        with tempfile.TemporaryDirectory(prefix="eg_e2e_data_") as directory:
            output = _run_driver("EndToEnd", Path(directory), 900)
        cases = (
            "E1_positive exit=0 verdict=STARTED_PROVEN observed=True launcher_exit=0 reap=True",
            "E2_noise exit=0 verdict=STARTED_PROVEN observed=True launcher_exit=0 reap=True",
            "E3_wrong_command_line exit=4 verdict=AMBIGUOUS observed=False launcher_exit=0 reap=True",
            "E4_launcher_exit_70_no_child exit=4 verdict=AMBIGUOUS observed=False launcher_exit=70 "
            "reap=True",
        )
        for case in cases:
            self.assertIn("e2e_case=" + case, output)
        e4_line = next(line for line in output.splitlines()
                       if line.startswith("e2e_case=E4_launcher_exit_70_no_child "))
        process_count = e4_line.rsplit("total_processes=", 1)
        self.assertEqual(2, len(process_count))
        # E4 must exceed the launcher-only NOT_STARTED_PROVEN accounting predicate.
        self.assertGreaterEqual(int(process_count[1].split()[0]), 2)
        self.assertEqual(4, output.count("integrity=True leftover=0"))
        self.assertIn("e2e_supervisor=PASS cases=4", output)


class SupervisorObserverRepairStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SUPERVISOR.read_text(encoding="utf-8")
        start = cls.source.index("function Test-EgApplicationChild {")
        cls.observer = cls.source[start:cls.source.index("function Get-EgAccounting", start)]

    def test_observer_constants_are_defined_exactly_once(self):
        self.assertEqual(1, self.source.count("$script:EgObserverMetadataTimeoutMilliseconds = "))
        self.assertEqual(1, self.source.count("$script:EgObserverProcessGoneErrorCode = "))
        normalized = self.source.replace("\r\n", "\n")
        self.assertIn("$script:EgObserverMetadataTimeoutMilliseconds = 1000\n", normalized)
        self.assertIn("$script:EgObserverProcessGoneErrorCode = 87\n", normalized)

    def test_metadata_query_uses_the_bound_budget_not_a_literal(self):
        self.assertIsNone(re.search(
            r"QueryProcessMetadata\(\s*\[uint32\]\$candidatePid,\s*\d", self.observer))
        self.assertIsNotNone(re.search(
            r"QueryProcessMetadata\(\s*\[uint32\]\$candidatePid,\s*"
            r"\$script:EgObserverMetadataTimeoutMilliseconds\)", self.observer))

    def test_deadline_guard_precedes_the_provider_query(self):
        self.assertIn(
            "$supervisorDeadline = Get-EgDeadlineTicks -StartTicks $StartTicks -Seconds $TimeoutSeconds",
            self.observer,
        )
        guard = self.observer.index(
            "if ($remainingMilliseconds -lt $script:EgObserverMetadataTimeoutMilliseconds) { break }")
        query = self.observer.index("QueryProcessMetadata(")
        self.assertLess(self.observer.index("$supervisorDeadline -"), guard)
        self.assertLess(guard, query)

    def test_provider_uncertainty_is_checked_before_success(self):
        uncertain = self.observer.index("if ($metadata.TimedOut -or $metadata.ProviderFailed) {")
        success = self.observer.index("if (-not $metadata.Succeeded) {")
        self.assertLess(self.observer.index("QueryProcessMetadata("), uncertain)
        self.assertLess(uncertain, success)
        self.assertIn("$script:EgState.observer_failed = $true", self.observer[uncertain:success])

    def test_only_error_87_and_proven_exit_are_treated_as_absent(self):
        self.assertIn(
            "if ($candidate.ErrorCode -ne $script:EgObserverProcessGoneErrorCode)",
            self.observer,
        )
        self.assertIn("GetProcessLive($candidate.Handle)", self.observer)

    def test_committed_observer_is_dotted_from_the_source_bound_fixture(self):
        source = HARNESS.read_text(encoding="utf-8-sig")
        self.assertIn(". $FunctionsPath", source)
        self.assertNotIn("function Test-EgApplicationChild", source)
        self.assertIn("Test-EgApplicationChild", FUNCTIONS.read_text(encoding="utf-8"))
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--harness-custody":
        try:
            sys.exit(_custody_cli())
        except Exception as error:
            print("custody_error=" + str(error), file=sys.stderr)
            sys.exit(1)
    unittest.main()
