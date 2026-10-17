"""Claude-free daily run scheduler surface (#226 V3 G1): inert task template and the runner script.

The task XML contract is parsed offline. The PowerShell runner template is rendered into a throwaway
EG_HOME and executed for real by Windows PowerShell 5.1 against a disposable venv, a fake Core module
that prints a chosen result line, and a fake monitor hook that records its calls. Nothing registers a
task, touches the real host or contacts n8n.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

PROJECT = Path(__file__).resolve().parents[1]
TASK = PROJECT / "task-scheduler" / "energygrid_daily_native.task.example.xml"
TEMPLATE = PROJECT / "runtime" / "scheduler" / "egdaily.ps1.template"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
COMMIT = "0123456789abcdef0123456789abcdef01234567"

FAKE_CLI = r'''
import json, pathlib, sys
home = pathlib.Path(__file__).resolve().parents[4]
mode = (home / "fake_mode.txt").read_text(encoding="utf-8").strip()
(home / "cli_args.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
runs = home / "data" / "runs"
run_dir = runs / "20261020-113000"
if mode in ("SUBMIT", "WITHHELD"):
    run_dir.mkdir(parents=True, exist_ok=True)
result = {"schema": "energygrid.native.v4.result.v1", "command": "daily", "run_dir": str(run_dir),
          "monitor_report": mode, "withheld_reasons": [] if mode == "SUBMIT" else ["EG_DAILY_FAILURE_NOTICE_UNDELIVERED"],
          "exit_code": 0 if mode == "SUBMIT" else 20}
if mode == "OUTSIDE":
    result.update(monitor_report="SUBMIT", run_dir=str(home))
if mode == "GARBAGE":
    print("not json at all")
    sys.exit(20)
print("noise line before the result")
print(json.dumps(result))
sys.exit(result["exit_code"])
'''


class TaskTemplateContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = ET.fromstring(TASK.read_bytes())

    def value(self, path: str) -> str:
        element = self.root.find(path, NS)
        self.assertIsNotNone(element, path)
        return (element.text or "").strip()

    def test_inert_daily_1130_singapore_trigger(self) -> None:
        self.assertEqual("false", self.value("t:Settings/t:Enabled"))
        self.assertEqual(1, len(self.root.findall("t:Triggers/t:CalendarTrigger", NS)))
        self.assertEqual("REPLACE_WITH_START_DATET11:30:00+08:00", self.value("t:Triggers/t:CalendarTrigger/t:StartBoundary"))
        self.assertEqual("1", self.value("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval"))

    def test_same_interactive_user_non_elevated(self) -> None:
        self.assertEqual("InteractiveToken", self.value("t:Principals/t:Principal/t:LogonType"))
        self.assertEqual("LeastPrivilege", self.value("t:Principals/t:Principal/t:RunLevel"))
        self.assertTrue(self.value("t:Principals/t:Principal/t:UserId").startswith("REPLACE_WITH_"))

    def test_single_writer_and_bounded_time(self) -> None:
        self.assertEqual("IgnoreNew", self.value("t:Settings/t:MultipleInstancesPolicy"))
        self.assertEqual("true", self.value("t:Settings/t:StartWhenAvailable"))
        self.assertEqual("PT2H", self.value("t:Settings/t:ExecutionTimeLimit"))
        self.assertEqual("true", self.value("t:Settings/t:AllowHardTerminate"))

    def test_action_is_the_locked_no_argument_runner_only(self) -> None:
        self.assertEqual(1, len(self.root.findall("t:Actions/t:Exec", NS)))
        self.assertEqual(POWERSHELL, self.value("t:Actions/t:Exec/t:Command"))
        self.assertEqual('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "REPLACE_WITH_EG_HOME\\runtime\\egdaily.ps1"',
                         self.value("t:Actions/t:Exec/t:Arguments"))
        text = TASK.read_text(encoding="utf-8").lower()
        for forbidden in ("claude", "python", "launcher.ps1", "egnative"):
            self.assertNotIn(forbidden + ".", text.split("-->", 1)[1], forbidden)


class RunnerTemplateStatic(unittest.TestCase):
    def test_fixed_paths_no_parameters_and_no_model(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        self.assertIn("param()", text)
        self.assertIn("if ($args.Count -ne 0)", text)
        self.assertIn("-m energygrid_bill_downloader.native_cli daily --config $Config", text)
        self.assertIn("-E -s -X utf8", text)
        self.assertEqual({"{{EG_HOME}}", "{{PINNED_COMMIT}}"},
                         {token for token in ("{{EG_HOME}}", "{{PINNED_COMMIT}}", "{{LAUNCHER}}") if token in text})
        lowered = "\n".join(line for line in text.lower().splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ("claude", "invoke-expression", "iex ", "start-process", "-command"):
            self.assertNotIn(forbidden, lowered, forbidden)


@unittest.skipUnless(os.name == "nt" and Path(POWERSHELL).exists(), "Windows PowerShell 5.1 is required")
class RunnerTemplateBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls._tmp.name) / "EnergyGrid"
        venv = cls.home / "runtime" / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=180)
        package = cls.home / "app" / COMMIT / "energygrid-bill-downloader" / "energygrid_bill_downloader"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "native_cli.py").write_text(FAKE_CLI, encoding="utf-8")
        (cls.home / "runtime" / "config").mkdir(parents=True)
        monitor = cls.home / "monitor"
        monitor.mkdir()
        (monitor / "egmonitor.cmd").write_text(
            '@echo off\r\necho %~1>>"%~dp0..\\monitor_calls.txt"\r\nexit /b 0\r\n', encoding="ascii")
        script = cls.home / "runtime" / "egdaily.ps1"
        script.write_text(TEMPLATE.read_text(encoding="utf-8").replace("{{EG_HOME}}", str(cls.home))
                          .replace("{{PINNED_COMMIT}}", COMMIT), encoding="utf-8")
        cls.script = script

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        for name in ("monitor_calls.txt", "cli_args.json"):
            (self.home / name).unlink(missing_ok=True)
        shutil.rmtree(self.home / "data", ignore_errors=True)
        (self.home / "data").mkdir()

    def run_script(self, mode: str, *extra: str) -> subprocess.CompletedProcess:
        (self.home / "fake_mode.txt").write_text(mode, encoding="utf-8")
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                               str(self.script), *extra], capture_output=True, text=True, timeout=120, check=False)

    def monitor_calls(self) -> list[str]:
        path = self.home / "monitor_calls.txt"
        return path.read_text(encoding="utf-8").split() if path.exists() else []

    def test_submit_reports_the_run_folder_once_and_passes_the_exit_code(self) -> None:
        completed = self.run_script("SUBMIT")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([str(self.home / "data" / "runs" / "20261020-113000")], self.monitor_calls())
        self.assertEqual(["daily", "--config", str(self.home / "runtime" / "config" / "native.json")],
                         json.loads((self.home / "cli_args.json").read_text(encoding="utf-8")))
        log = (self.home / "data" / "egdaily.log").read_text(encoding="utf-8-sig")
        self.assertIn("exit=0 monitor_report=SUBMIT submitted=True", log)

    def test_withheld_never_runs_the_monitor(self) -> None:
        completed = self.run_script("WITHHELD")
        self.assertEqual(20, completed.returncode, completed.stderr)
        self.assertEqual([], self.monitor_calls())
        self.assertIn("withheld=EG_DAILY_FAILURE_NOTICE_UNDELIVERED",
                      (self.home / "data" / "egdaily.log").read_text(encoding="utf-8-sig"))

    def test_unreadable_result_or_foreign_folder_never_runs_the_monitor(self) -> None:
        for mode in ("GARBAGE", "OUTSIDE"):
            with self.subTest(mode=mode):
                self.setUp()
                completed = self.run_script(mode)
                self.assertNotEqual(64, completed.returncode)
                self.assertEqual([], self.monitor_calls())

    def test_any_argument_is_refused_before_the_core_runs(self) -> None:
        completed = self.run_script("SUBMIT", "--config", "elsewhere.json")
        self.assertEqual(64, completed.returncode, completed.stderr)
        self.assertIn("EG_DAILY_ARGUMENTS_REFUSED", completed.stdout)
        self.assertFalse((self.home / "cli_args.json").exists())
        self.assertEqual([], self.monitor_calls())


if __name__ == "__main__":
    unittest.main()
