"""#226 G3 bounded Claude supervisor, egcore.cmd shim and reviewed Claude envelope.

Synthetic only. A compiled fake `claude.exe` replays scripted stream-json
transcripts and may invoke the real `egcore.cmd`; a fake launcher records what
reaches the core. No Claude token, model, n8n, Google or EnergyGrid is used.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from energygrid_bill_downloader.orchestration import ALLOWED_COMMAND_LINES


PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = PROJECT / "runtime"
SUPERVISOR = RUNTIME / "claude_supervisor.ps1"
EGCORE = RUNTIME / "bin" / "egcore.cmd"
CLAUDE_DIR = RUNTIME / "claude"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
WINDOWS = sys.platform == "win32" and POWERSHELL.exists()
VERSION = "9.9.9 (Claude Code)"

FAKE_CLAUDE_SOURCE = r"""
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
public static class FakeClaude {
    public static int Main(string[] args) {
        string dir = Environment.GetEnvironmentVariable("FAKE_CLAUDE_DIR");
        if (args.Length == 1 && args[0] == "--version") { Console.Out.Write(File.ReadAllText(Path.Combine(dir, "version.txt"))); return 0; }
        string prompt = Console.In.ReadToEnd();
        StringBuilder record = new StringBuilder();
        record.AppendLine("ARGS=" + string.Join("\u001f", args));
        record.AppendLine("TOKEN=" + (Environment.GetEnvironmentVariable("CLAUDE_CODE_OAUTH_TOKEN") ?? ""));
        record.AppendLine("PATH0=" + (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';')[0]);
        record.AppendLine("CWD=" + Environment.CurrentDirectory);
        record.AppendLine("PROMPT_SHA=" + BitConverter.ToString(System.Security.Cryptography.SHA256.Create().ComputeHash(Encoding.UTF8.GetBytes(prompt))).Replace("-", "").ToLowerInvariant());
        int exitCode = 0;
        foreach (string line in File.ReadAllLines(Path.Combine(dir, "scenario.txt"))) {
            if (line.StartsWith("EXIT:")) { exitCode = int.Parse(line.Substring(5)); }
            else if (line.StartsWith("SLEEP:")) { Thread.Sleep(int.Parse(line.Substring(6)) * 1000); }
            else if (line.StartsWith("RUN:")) {
                ProcessStartInfo info = new ProcessStartInfo("cmd.exe", "/d /c " + line.Substring(4));
                info.UseShellExecute = false; info.RedirectStandardOutput = true; info.RedirectStandardError = true;
                Process child = Process.Start(info);
                string output = child.StandardOutput.ReadToEnd(); child.StandardError.ReadToEnd(); child.WaitForExit();
                record.AppendLine("RAN=" + line.Substring(4) + "\u001f" + child.ExitCode + "\u001f" + output.Trim());
            }
            else if (line.StartsWith("OUT:")) { Console.Out.WriteLine(line.Substring(4)); }
        }
        File.WriteAllText(Path.Combine(dir, "record.txt"), record.ToString());
        return exitCode;
    }
}
"""

FAKE_LAUNCHER = r"""
param(
    [string]$ConfigPath, [string]$PythonExe, [string]$CheckoutRoot, [string]$CredentialPath,
    [string]$BrowserCachePath, [string]$ExpectedBranch, [string[]]$AuthorisedLauncherRootWriteSid,
    [string]$Command, [string]$Stream, [string]$LogRoot, [string]$RunId
)
$dir = Split-Path -Parent $PSCommandPath
$entry = [ordered]@{
    command = $Command; stream = $Stream; run_id = $RunId
    token = [bool][Environment]::GetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN')
    anthropic = [bool][Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY')
    settings_var = [bool][Environment]::GetEnvironmentVariable('EGCORE_SUPERVISOR_SETTINGS')
}
Add-Content -LiteralPath (Join-Path $dir 'calls.jsonl') -Value ($entry | ConvertTo-Json -Compress) -Encoding ascii
$responses = Get-Content -LiteralPath (Join-Path $dir 'responses.json') -Raw | ConvertFrom-Json
$statusCalls = @(Get-Content -LiteralPath (Join-Path $dir 'calls.jsonl') | Where-Object { $_ -like '*"command":"status"*' }).Count
$response = $responses.$Command
if ($Command -eq 'status' -and $statusCalls -ge 2 -and $null -ne $responses.final_status) { $response = $responses.final_status }
if ($null -eq $response) { $response = $responses.default }
[Console]::Out.WriteLine([string]$response.stdout)
exit ([int]$response.exit)
"""


def status_document(outcome: str = "COMPLETED", *, terminal: bool = True, uncertainty: bool = False) -> str:
    return json.dumps({
        "schema": "energygrid.core.status.v3", "run": {"acquire": "COMPLETED", "acquire_exit_code": 0},
        "streams": {}, "terminal": terminal, "uncertainty_outstanding": uncertainty, "business_outcome": outcome,
    }, separators=(",", ":"))


def result_line(**overrides) -> str:
    document = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 6, "total_cost_usd": 0.12,
                "permission_denials": [], "result": '{"schema":"energygrid.claude_orchestrator.v1"}'}
    document.update(overrides)
    return json.dumps(document)


def tool_use(command: str, name: str = "Bash") -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_synthetic", "name": name, "input": {"command": command}}]}})


INIT = json.dumps({"type": "system", "subtype": "init", "tools": ["Bash"], "mcp_servers": []})

_FAKE_EXE: Path | None = None
_BUILD_DIR: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _FAKE_EXE, _BUILD_DIR
    if not WINDOWS:
        return
    _BUILD_DIR = tempfile.TemporaryDirectory()
    source = Path(_BUILD_DIR.name) / "FakeClaude.cs"
    source.write_text(FAKE_CLAUDE_SOURCE, encoding="utf-8")
    exe = Path(_BUILD_DIR.name) / "claude.exe"
    script = (
        f"Add-Type -TypeDefinition ([IO.File]::ReadAllText('{source}')) -OutputAssembly '{exe}' "
        "-OutputType ConsoleApplication -ReferencedAssemblies System.dll"
    )
    subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", script], check=True, capture_output=True, timeout=120)
    _FAKE_EXE = exe


def tearDownModule() -> None:
    if _BUILD_DIR is not None:
        _BUILD_DIR.cleanup()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(WINDOWS, "the supervisor runs on native Windows PowerShell 5.1")
class SupervisorHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.runtime = root / "runtime"
        (self.runtime / "bin").mkdir(parents=True)
        (self.runtime / "claude" / "workspace" / ".claude").mkdir(parents=True)
        shutil.copy2(SUPERVISOR, self.runtime / "claude_supervisor.ps1")
        shutil.copy2(EGCORE, self.runtime / "bin" / "egcore.cmd")
        shutil.copy2(CLAUDE_DIR / "energygrid_orchestrator.prompt.md", self.runtime / "claude" / "energygrid_orchestrator.prompt.md")
        shutil.copy2(CLAUDE_DIR / "mcp.empty.json", self.runtime / "claude" / "mcp.empty.json")
        shutil.copy2(CLAUDE_DIR / "claude.settings.json", self.runtime / "claude" / "workspace" / ".claude" / "settings.json")
        self.claude_dir = root / "claude-install"
        self.claude_dir.mkdir()
        shutil.copy2(_FAKE_EXE, self.claude_dir / "claude.exe")
        (self.claude_dir / "version.txt").write_text(VERSION, encoding="ascii")
        self.launcher_dir = root / "launcher"
        self.launcher_dir.mkdir()
        (self.launcher_dir / "launcher.ps1").write_text(FAKE_LAUNCHER, encoding="ascii")
        (self.launcher_dir / "installation_manifest.json").write_text('{"synthetic":true}', encoding="ascii")
        self.responses({"default": {"exit": 0, "stdout": status_document()}})
        self.token = root / "token.clixml"
        subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command",
                        f"ConvertTo-SecureString 'synthetic-setup-token' -AsPlainText -Force | Export-Clixml -LiteralPath '{self.token}'"],
                       check=True, capture_output=True, timeout=60)
        self.receipts = root / "receipts"
        self.settings_path = root / "supervisor.settings.json"
        self.settings = self.default_settings()
        self.write_settings()
        self.scenario([INIT, result_line()])

    # -- fixture helpers ----------------------------------------------------
    def default_settings(self) -> dict:
        runtime = self.runtime
        return {
            "schema": "energygrid.claude_supervisor_settings.v1",
            "claude_exe_path": str(self.claude_dir / "claude.exe"),
            "claude_exe_sha256": sha(self.claude_dir / "claude.exe"),
            "claude_version": VERSION,
            "claude_model": "claude-synthetic-model",
            "token_path": str(self.token),
            "git_bash_path": r"C:\Program Files\Git\bin\bash.exe",
            "work_dir": str(runtime / "claude" / "workspace"),
            "prompt_path": str(runtime / "claude" / "energygrid_orchestrator.prompt.md"),
            "claude_settings_path": str(runtime / "claude" / "workspace" / ".claude" / "settings.json"),
            "mcp_config_path": str(runtime / "claude" / "mcp.empty.json"),
            "egcore_bin_dir": str(runtime / "bin"),
            "expected_sha256": {
                "prompt": sha(runtime / "claude" / "energygrid_orchestrator.prompt.md"),
                "claude_settings": sha(runtime / "claude" / "workspace" / ".claude" / "settings.json"),
                "mcp_config": sha(runtime / "claude" / "mcp.empty.json"),
                "egcore_cmd": sha(runtime / "bin" / "egcore.cmd"),
                "launcher_manifest": sha(self.launcher_dir / "installation_manifest.json"),
            },
            "launcher": {
                "path": str(self.launcher_dir / "launcher.ps1"),
                "manifest_path": str(self.launcher_dir / "installation_manifest.json"),
                "config_path": "C:\\synthetic\\config.json", "python_exe": "C:\\synthetic\\python.exe",
                "checkout_root": "C:\\synthetic\\checkout", "credential_path": "C:\\synthetic\\credential.clixml",
                "browser_cache_path": "C:\\synthetic\\browsers", "expected_branch": "ANY_BRANCH",
                "authorised_launcher_root_write_sid": ["S-1-5-32-544"], "log_root": "C:\\synthetic\\logs",
            },
            "receipt_root": str(self.receipts),
            "claude_timeout_seconds": 60,
            "max_turns": 40,
            "max_budget_usd": "1.00",
            "alert": None,
        }

    def write_settings(self) -> None:
        self.settings_path.write_text(json.dumps(self.settings), encoding="utf-8")

    def scenario(self, outputs: list[str], *, runs: tuple[str, ...] = (), exit_code: int = 0, sleep: int = 0) -> None:
        lines = [f"EXIT:{exit_code}"]
        if sleep:
            lines.append(f"SLEEP:{sleep}")
        lines += [f"RUN:{command}" for command in runs]
        lines += [f"OUT:{item}" for item in outputs]
        (self.claude_dir / "scenario.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def responses(self, mapping: dict) -> None:
        (self.launcher_dir / "responses.json").write_text(json.dumps(mapping), encoding="utf-8")

    def run_supervisor(self) -> int:
        environment = dict(os.environ)
        environment["FAKE_CLAUDE_DIR"] = str(self.claude_dir)
        environment["ANTHROPIC_API_KEY"] = "synthetic-anthropic-canary"
        completed = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
             str(self.runtime / "claude_supervisor.ps1"), "-SettingsPath", str(self.settings_path)],
            capture_output=True, text=True, timeout=300, env=environment,
        )
        return completed.returncode

    def calls(self) -> list[dict]:
        path = self.launcher_dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines() if line.strip()]

    def record(self) -> dict[str, list[str]]:
        path = self.claude_dir / "record.txt"
        if not path.exists():
            return {}
        result: dict[str, list[str]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            result.setdefault(key, []).append(value)
        return result

    def receipt(self) -> dict:
        files = list(self.receipts.glob("receipt-*.json"))
        self.assertEqual(1, len(files))
        return json.loads(files[0].read_text(encoding="ascii"))


class SupervisorRunTests(SupervisorHarness):
    def test_success_runs_exact_envelope_and_success_comes_from_final_status(self) -> None:
        self.scenario([INIT, tool_use("egcore.cmd plan"), tool_use("egcore.cmd acquire"), result_line()],
                      runs=("egcore.cmd plan", "egcore.cmd drive-upload --stream EB_BILL"))
        self.assertEqual(0, self.run_supervisor())
        record = self.record()
        args = record["ARGS"][0].split("\u001f")
        self.assertEqual(["-p", "--output-format", "stream-json", "--verbose"], args[:4])
        self.assertEqual(["--permission-mode", "dontAsk"], args[6:8])
        self.assertEqual(["--tools", "Bash", "--allowedTools"], args[8:11])
        self.assertEqual([f"Bash({line})" for line in ALLOWED_COMMAND_LINES], args[11:22])
        for flag in ("--strict-mcp-config", "--no-session-persistence", "--disallowedTools"):
            self.assertIn(flag, args)
        self.assertEqual(["40", "1.00"], [args[args.index("--max-turns") + 1], args[args.index("--max-budget-usd") + 1]])
        self.assertEqual(["synthetic-setup-token"], record["TOKEN"], "token only in Claude's environment")
        self.assertEqual([str(self.runtime / "bin")], record["PATH0"])
        self.assertEqual([sha(self.runtime / "claude" / "energygrid_orchestrator.prompt.md")], record["PROMPT_SHA"])
        calls = self.calls()
        self.assertEqual(["status", "plan", "drive-upload", "status"], [call["command"] for call in calls])
        self.assertEqual("EB_BILL", calls[2]["stream"])
        self.assertEqual(1, len({call["run_id"] for call in calls}), "one run ID across every core call")
        for call in calls:
            self.assertEqual((False, False, False), (call["token"], call["anthropic"], call["settings_var"]),
                             "the core never sees the Claude token, ANTHROPIC_* or the settings pointer")
        receipt = self.receipt()
        self.assertEqual((0, "COMPLETED", None), (receipt["exit_code"], receipt["status"]["business_outcome"], receipt["claude_reason"]))
        self.assertNotIn("synthetic-setup-token", json.dumps(receipt))

    def test_business_outcomes_map_to_exit_codes_and_claude_exit_zero_is_not_success(self) -> None:
        cases = {
            ("HOLD", True, False): 81, ("SOURCE_FAILURE_RETRYABLE", True, False): 82, ("SOURCE_FAILURE", True, False): 83,
            ("DRIVE_UNCERTAIN", True, True): 84, ("DRIVE_CONFLICT", True, False): 85, ("EMAIL_UNCERTAIN", True, True): 86,
            ("INCOMPLETE", False, False): 89, ("COMPLETED", False, False): 89, ("NO_WORK", True, True): 89,
            ("NO_WORK", True, False): 0, ("COMPLETED", True, False): 0,
            # M1: a non-terminal status is incomplete (89) even when its outcome claims an
            # ordinary HOLD or a lower-severity business code.
            ("HOLD", False, False): 89, ("HOLD", False, True): 89, ("DRIVE_CONFLICT", False, False): 89,
            ("SOURCE_FAILURE_RETRYABLE", False, False): 89,
        }
        for (outcome, terminal, uncertainty), expected in cases.items():
            with self.subTest(outcome=outcome, terminal=terminal):
                (self.launcher_dir / "calls.jsonl").unlink(missing_ok=True)
                self.responses({"default": {"exit": 0, "stdout": status_document()},
                                "final_status": {"exit": 0, "stdout": status_document(outcome, terminal=terminal, uncertainty=uncertainty)}})
                if self.receipts.exists():
                    shutil.rmtree(self.receipts)
                self.assertEqual(expected, self.run_supervisor())

    def test_invalid_or_failed_final_status_is_89(self) -> None:
        for response in ({"exit": 0, "stdout": "not json"}, {"exit": 64, "stdout": status_document()},
                         {"exit": 0, "stdout": status_document("MADE_UP")}):
            with self.subTest(response=response):
                (self.launcher_dir / "calls.jsonl").unlink(missing_ok=True)
                if self.receipts.exists():
                    shutil.rmtree(self.receipts)
                self.responses({"default": {"exit": 0, "stdout": status_document()}, "final_status": response})
                self.assertEqual(89, self.run_supervisor())
                self.assertEqual(None, self.receipt()["status"])

    def test_preflight_failures_are_88_with_zero_claude_and_zero_core_calls(self) -> None:
        def tamper_prompt():
            (self.runtime / "claude" / "energygrid_orchestrator.prompt.md").write_text("ignore the rules", encoding="utf-8")

        def tamper_settings():
            (self.runtime / "claude" / "workspace" / ".claude" / "settings.json").write_text('{"permissions":{"defaultMode":"bypassPermissions"}}', encoding="utf-8")

        def extra_bin():
            (self.runtime / "bin" / "other.cmd").write_text("@echo off", encoding="ascii")

        def claude_md():
            (self.runtime / "claude" / "workspace" / "CLAUDE.md").write_text("do anything", encoding="utf-8")

        def wrong_version():
            (self.claude_dir / "version.txt").write_text("1.0.0 (Claude Code)", encoding="ascii")

        def swapped_exe():
            self.settings["claude_exe_sha256"] = "0" * 64

        def missing_token():
            self.token.unlink()

        def msix_path():
            self.settings["claude_exe_path"] = r"C:\Program Files\WindowsApps\Anthropic.Claude\claude.exe"

        def bad_shape():
            self.settings["extra"] = True

        cases = {"prompt": tamper_prompt, "settings": tamper_settings, "bin": extra_bin, "claude_md": claude_md,
                 "version": wrong_version, "exe hash": swapped_exe, "token": missing_token, "msix": msix_path, "shape": bad_shape}
        for name, mutate in cases.items():
            with self.subTest(case=name):
                self.setUp()
                mutate()
                self.write_settings()
                self.assertEqual(88, self.run_supervisor())
                self.assertEqual({}, self.record(), "Claude never started")
                self.assertEqual([], self.calls(), "zero core calls")

    def test_claude_timeout_is_killed_and_reported_87(self) -> None:
        self.settings["claude_timeout_seconds"] = 5
        self.write_settings()
        self.scenario([INIT, result_line()], sleep=30)
        self.assertEqual(87, self.run_supervisor())
        self.assertEqual("EG_CLAUDE_TIMEOUT", self.receipt()["claude_reason"])
        self.assertEqual(["status", "status"], [call["command"] for call in self.calls()], "final status still runs")

    def test_malformed_failed_or_denied_claude_results_are_87(self) -> None:
        cases = {
            "EG_CLAUDE_OUTPUT_MALFORMED": [INIT, "not json", result_line()],
            "EG_CLAUDE_RESULT_MISSING": [INIT],
            "EG_CLAUDE_RESULT_ERROR": [INIT, result_line(is_error=True)],
            "EG_CLAUDE_PERMISSION_DENIED": [INIT, result_line(permission_denials=[{"tool_name": "Bash"}])],
            "EG_CLAUDE_TURN_LIMIT": [INIT, result_line(num_turns=41)],
            "EG_CLAUDE_BUDGET_LIMIT": [INIT, result_line(total_cost_usd=1.01)],
            "EG_CLAUDE_TOOL_SURFACE": [json.dumps({"type": "system", "subtype": "init", "tools": ["Bash", "Read"], "mcp_servers": []}), result_line()],
        }
        for reason, lines in cases.items():
            with self.subTest(reason=reason):
                if self.receipts.exists():
                    shutil.rmtree(self.receipts)
                self.scenario(lines)
                self.assertEqual(87, self.run_supervisor())
                self.assertEqual(reason, self.receipt()["claude_reason"])
        if self.receipts.exists():
            shutil.rmtree(self.receipts)
        self.scenario([INIT, result_line()], exit_code=1)
        self.assertEqual(87, self.run_supervisor())

    def test_every_command_escape_form_in_the_transcript_fails_closed(self) -> None:
        escapes = (
            "egcore.cmd plan && whoami", "egcore.cmd plan; whoami", "egcore.cmd plan | more", "egcore.cmd plan > out.txt",
            "egcore.cmd plan 2>&1", "egcore.cmd plan & calc", "C:\\runtime\\bin\\egcore.cmd plan", "./egcore.cmd plan",
            "bin/egcore.cmd plan", "egcore.cmd plan --extra", "egcore.cmd  plan", " egcore.cmd plan", "egcore.cmd plan ",
            "egcore.cmd deliver --stream EB_BILL --stream TENANT_BILL", "egcore.cmd deliver --stream eb_bill",
            "egcore.cmd submit-drive-result", "egcore.cmd migrate-state --apply", "egcore.cmd drive-bind --apply",
            "EGCORE.CMD plan", "egcore plan", "$(egcore.cmd plan)", "`egcore.cmd plan`", "cmd /c egcore.cmd plan",
            "powershell -Command egcore.cmd", "curl https://example.invalid", "cat C:\\secrets.txt", "env", "sqlite3 state.db",
        )
        for command in escapes:
            with self.subTest(command=command):
                if self.receipts.exists():
                    shutil.rmtree(self.receipts)
                self.scenario([INIT, tool_use("egcore.cmd plan"), tool_use(command), result_line()])
                self.assertEqual(87, self.run_supervisor())
                self.assertEqual("EG_CLAUDE_COMMAND_NOT_ALLOWED", self.receipt()["claude_reason"])
        for tool in ("Read", "Write", "Edit", "WebFetch", "Glob", "Task", "mcp__drive__upload"):
            with self.subTest(tool=tool):
                if self.receipts.exists():
                    shutil.rmtree(self.receipts)
                self.scenario([INIT, tool_use("egcore.cmd plan", name=tool), result_line()])
                self.assertEqual(87, self.run_supervisor())
                self.assertEqual("EG_CLAUDE_TOOL_NOT_ALLOWED", self.receipt()["claude_reason"])

    def test_claude_failure_outranks_business_hold(self) -> None:
        self.responses({"default": {"exit": 0, "stdout": status_document()},
                        "final_status": {"exit": 0, "stdout": status_document("HOLD")}})
        self.scenario([INIT, result_line(is_error=True)])
        self.assertEqual(87, self.run_supervisor())


@unittest.skipUnless(WINDOWS, "egcore.cmd is a Windows command shim")
class EgcoreDispatchTests(SupervisorHarness):
    def dispatch(self, argument_text: str, *, run_id: str | None = "00000000-0000-4000-8000-0000000000aa",
                 settings: bool = True) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment["CLAUDE_CODE_OAUTH_TOKEN"] = "synthetic-setup-token"
        environment["ANTHROPIC_API_KEY"] = "synthetic-anthropic-canary"
        environment.pop("ENERGYGRID_RUN_ID", None)
        if run_id is not None:
            environment["ENERGYGRID_RUN_ID"] = run_id
        if settings:
            environment["EGCORE_SUPERVISOR_SETTINGS"] = str(self.settings_path)
        return subprocess.run(f'cmd.exe /d /c ""{self.runtime / "bin" / "egcore.cmd"}" {argument_text}"',
                              capture_output=True, text=True, timeout=120, env=environment, shell=False)

    def test_the_eleven_exact_forms_reach_the_launcher_with_a_scrubbed_environment(self) -> None:
        for line in ALLOWED_COMMAND_LINES:
            with self.subTest(line=line):
                completed = self.dispatch(line.split(" ", 1)[1])
                self.assertEqual(0, completed.returncode, completed.stderr)
        calls = self.calls()
        self.assertEqual(11, len(calls))
        expected = [(line.split()[1], line.split()[3] if len(line.split()) == 4 else "NONE") for line in ALLOWED_COMMAND_LINES]
        self.assertEqual(expected, [(call["command"], call["stream"]) for call in calls])
        self.assertTrue(all(not call["token"] and not call["anthropic"] and not call["settings_var"] for call in calls))

    def test_anything_else_is_refused_with_64_and_never_reaches_the_launcher(self) -> None:
        for text in ("", "plan extra", "plan --stream EB_BILL", "deliver", "deliver --stream", "deliver --stream OTHER",
                     "deliver --stream EB_BILL x", "deliver --stream EB_BILL x y", "drive-bind --apply", "migrate-state",
                     "submit-drive-result", "PLAN", "--stream EB_BILL deliver", '"plan --x"'):
            with self.subTest(text=text):
                completed = self.dispatch(text)
                self.assertEqual(64, completed.returncode)
                self.assertEqual("REFUSED", json.loads(completed.stdout)["outcome"])
        self.assertEqual(64, self.dispatch("plan", run_id=None).returncode)
        self.assertEqual(64, self.dispatch("plan", run_id="not-a-uuid").returncode)
        self.assertEqual(64, self.dispatch("plan", settings=False).returncode)
        self.assertEqual([], self.calls())


class EnvelopeStaticTests(unittest.TestCase):
    def test_single_source_allowlist_matches_settings_prompt_and_supervisor(self) -> None:
        settings = json.loads((CLAUDE_DIR / "claude.settings.json").read_text(encoding="utf-8"))
        self.assertEqual([f"Bash({line})" for line in ALLOWED_COMMAND_LINES], settings["permissions"]["allow"])
        self.assertTrue(all("*" not in rule and ":" not in rule for rule in settings["permissions"]["allow"]))
        prompt = (CLAUDE_DIR / "energygrid_orchestrator.prompt.md").read_text(encoding="utf-8")
        listed = [line.strip()[2:] for line in prompt.splitlines() if line.strip().startswith("- egcore.cmd")]
        self.assertEqual(list(ALLOWED_COMMAND_LINES), listed)
        supervisor = SUPERVISOR.read_text(encoding="utf-8")
        for line in ALLOWED_COMMAND_LINES:
            self.assertIn(f"'{line}'", supervisor)
        self.assertEqual(11, len(ALLOWED_COMMAND_LINES))

    def test_settings_deny_every_other_tool_and_disable_bypass_hooks_and_mcp(self) -> None:
        settings = json.loads((CLAUDE_DIR / "claude.settings.json").read_text(encoding="utf-8"))
        permissions = settings["permissions"]
        self.assertEqual(("dontAsk", "disable"), (permissions["defaultMode"], permissions["disableBypassPermissionsMode"]))
        for tool in ("Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "NotebookEdit", "mcp__*"):
            self.assertIn(tool, permissions["deny"])
        self.assertNotIn("Bash", permissions["deny"])
        self.assertIs(True, settings["disableAllHooks"])
        self.assertIs(False, settings["enableAllProjectMcpServers"])
        self.assertEqual("1", settings["env"]["DISABLE_AUTOUPDATER"])
        self.assertEqual({"mcpServers": {}}, json.loads((CLAUDE_DIR / "mcp.empty.json").read_text(encoding="utf-8")))
        self.assertFalse((CLAUDE_DIR / "CLAUDE.md").exists())

    def test_egcore_shim_forwards_only_fixed_slots_to_the_sibling_supervisor(self) -> None:
        text = EGCORE.read_text(encoding="ascii")
        self.assertIn('-File "%~dp0..\\claude_supervisor.ps1"', text)
        self.assertIn("-CoreDispatch", text)
        self.assertIn('-CoreArgOverflow "%~5"', text)
        self.assertNotIn("%*", text)
        self.assertNotIn("EnableDelayedExpansion", text.replace("DisableDelayedExpansion", ""))
        self.assertEqual(["egcore.cmd"], [path.name for path in (RUNTIME / "bin").iterdir()])

    def test_supervisor_never_logs_or_passes_the_token_on_a_command_line(self) -> None:
        text = SUPERVISOR.read_text(encoding="utf-8")
        self.assertIn("'CLAUDE_CODE_OAUTH_TOKEN'     = $plain", text)
        self.assertIn("$name -ieq 'CLAUDE_CODE_OAUTH_TOKEN' -or $name -like 'ANTHROPIC_*'", text)
        self.assertNotIn("Write-Host", text)
        self.assertNotIn("--dangerously-skip-permissions", text)
        self.assertNotIn("bypassPermissions", text)
        self.assertIn("ZeroFreeBSTR", text)
        self.assertIn("0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", text)

    def test_settings_example_is_placeholders_only(self) -> None:
        example = json.loads((RUNTIME / "supervisor.settings.example.json").read_text(encoding="utf-8"))
        flat = json.dumps(example)
        self.assertNotRegex(flat, r"[0-9a-f]{64}")
        self.assertNotIn("WindowsApps", flat)
        self.assertEqual(1200, example["claude_timeout_seconds"])
        self.assertEqual(40, example["max_turns"])
        self.assertEqual("1.00", example["max_budget_usd"])

    @unittest.skipUnless(WINDOWS, "parser is Windows PowerShell 5.1")
    def test_supervisor_parses_cleanly_in_windows_powershell_5_1(self) -> None:
        script = (
            "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{SUPERVISOR}',[ref]$null,[ref]$e); $e.Count"
        )
        completed = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", script],
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual("0", completed.stdout.strip())



class SupervisorAlertCompatibilityTests(unittest.TestCase):
    """G2: the accepted alert-ingress workflow stays unchanged only if its committed
    validator accepts the supervisor's energygrid.alert.v1 values. Node executes it."""

    def test_committed_ingress_validator_accepts_every_supervisor_alert(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is required to execute the committed Code node")
        workflow = PROJECT.parent / "n8n-workflows" / "energygrid_alert_ingress.workflow.json"
        supervisor = SUPERVISOR.read_text(encoding="utf-8")
        for literal in ("schema = 'energygrid.alert.v1'", "event = 'energygrid_run_failed'", "stage = 'run'",
                        "status = 'ACTION_REQUIRED'", "support_ref = ('EG_SUPERVISOR_EXIT_' + $ExitCode)",
                        "attention_required = $true", "ToString('yyyy-MM-ddTHH:mm:ssZ')"):
            self.assertIn(literal, supervisor)
        payloads = [{
            "schema": "energygrid.alert.v1", "event": "energygrid_run_failed", "timestamp": "2026-10-06T00:00:00Z",
            "run_id": "00000000-0000-4000-8000-000000000001", "stage": "run", "status": "ACTION_REQUIRED",
            "support_ref": f"EG_SUPERVISOR_EXIT_{code}", "exit_code": code,
            "counts": {"inventory": 0, "downloaded": 0, "present": 0, "failure": 0}, "attention_required": True,
        } for code in (81, 82, 83, 84, 85, 86, 87, 88, 89)]
        harness = r"""
const fs = require('fs');
const [workflowPath, payloadsPath] = process.argv.slice(2);
const wf = JSON.parse(fs.readFileSync(workflowPath, 'utf8'));
const code = wf.nodes.find((n) => n.name === 'Validate Alert Payload').parameters.jsCode;
const results = [];
for (const body of JSON.parse(fs.readFileSync(payloadsPath, 'utf8'))) {
  try { new Function('$input', code)({ all: () => [{ json: { body } }] }); results.push('ok'); }
  catch (error) { results.push(String(error.message)); }
}
process.stdout.write(JSON.stringify(results));
"""
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "h.js"
            script.write_text(harness, encoding="utf-8")
            data = Path(temp) / "p.json"
            data.write_text(json.dumps(payloads), encoding="utf-8")
            completed = subprocess.run([node, str(script), str(workflow), str(data)], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(["ok"] * 9, json.loads(completed.stdout))


if __name__ == "__main__":
    unittest.main()
