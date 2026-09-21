"""Deterministic contract tests for the EnergyGrid one-shot supervisor.

The production supervisor is intentionally exercised through static source-shape checks
and this small synthetic containment model.  The model has no portal, credential,
launcher, Scheduler or network surface.  Windows PowerShell 5.1 parsing and native C#
compilation are run as a real gate on Windows; a Windows host without that exact shell is
an error, never a skipped pass.
"""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = REPO_ROOT / "scripts" / "energygrid_one_shot_supervisor.ps1"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "energygrid-bill-downloader-tests.yml"
RUNBOOK = REPO_ROOT / "energygrid-bill-downloader" / "docs" / "runbook.md"


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
        self.assertIn("intent_sha256", self.source)
        self.assertIn("UTF8Encoding($false)", self.source)
        self.assertIn("+ \"`n\"", self.source)
        self.assertIn("outcome.v1", self.source)
        self.assertIn("intent.v1", self.source)
        self.assertIn("EG_SUPERVISOR_DUPLICATE_RUN_ID", self.source)

    def test_verdict_and_exit_mapping_are_conservative(self):
        for verdict in ("NOT_STARTED_PROVEN", "STARTED_PROVEN", "AMBIGUOUS"):
            self.assertIn("'" + verdict + "'", self.source)
        self.assertIn("launcher_exit_code", self.source)
        self.assertIn("if (-not $script:EgState.outcome_committed) { return 3 }", self.source)
        self.assertIn("if ($script:EgState.timed_out -or $script:EgState.interrupted) { return 2 }", self.source)
        self.assertIn("if ($script:EgState.start_verdict -eq 'AMBIGUOUS') { return 4 }", self.source)
        self.assertIn("$script:EgState.launcher_exit_code -eq 0", self.source)
        self.assertNotIn("launcher_failed", self.source)

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
        self.assertIn("[uint32]0xE0470001", self.source)
        self.assertIn("termination_started", self.source)
        self.assertIn("Wait-EgReap", self.source)
        self.assertIn("SetConsoleCtrlHandler", self.source)
        self.assertIn("Start-Sleep -Milliseconds 100", self.source)

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
