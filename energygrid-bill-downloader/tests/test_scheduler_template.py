"""Inert Task Scheduler template contract (DL-XB-199 G3-101). Registers nothing."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET


TEMPLATE = Path(__file__).resolve().parents[1] / "task-scheduler" / "energygrid_daily.task.example.xml"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


class SchedulerTemplateContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = TEMPLATE.read_text(encoding="utf-8")
        cls.root = ET.fromstring(cls.text.encode("utf-8"))

    def value(self, path: str) -> str:
        element = self.root.find(path, NS)
        self.assertIsNotNone(element, path)
        return (element.text or "").strip()

    def test_overlap_timeout_and_missed_start_policy(self) -> None:
        self.assertEqual("IgnoreNew", self.value("t:Settings/t:MultipleInstancesPolicy"))
        self.assertEqual("true", self.value("t:Settings/t:StartWhenAvailable"))
        limit = self.value("t:Settings/t:ExecutionTimeLimit")
        self.assertRegex(limit, r"\APT([1-9]\d*M|[1-9]\d*H)\Z", "a hard, non-zero execution ceiling")
        self.assertEqual("true", self.value("t:Settings/t:AllowHardTerminate"))

    def test_template_is_inert(self) -> None:
        self.assertEqual("false", self.value("t:Settings/t:Enabled"))

    def test_one_daily_calendar_trigger_has_fixed_singapore_time_and_date_placeholder(self) -> None:
        triggers = self.root.findall("t:Triggers/t:CalendarTrigger", NS)
        self.assertEqual(1, len(triggers))
        boundary = self.value("t:Triggers/t:CalendarTrigger/t:StartBoundary")
        self.assertEqual("REPLACE_WITH_START_DATET08:00:00+08:00", boundary)
        self.assertEqual(
            "2026-10-03T08:00:00+08:00",
            boundary.replace("REPLACE_WITH_START_DATE", "2026-10-03"),
        )
        self.assertEqual("1", self.value("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval"))

    def test_non_elevated_run_principal(self) -> None:
        self.assertEqual("LeastPrivilege", self.value("t:Principals/t:Principal/t:RunLevel"))
        self.assertEqual("Password", self.value("t:Principals/t:Principal/t:LogonType"))
        self.assertTrue(self.value("t:Principals/t:Principal/t:UserId").startswith("REPLACE_WITH_"))

    def test_action_is_the_absolute_powershell_supervisor_invocation(self) -> None:
        """#226 G3: the task runs the bounded Claude supervisor, never the launcher."""
        execs = self.root.findall("t:Actions/t:Exec", NS)
        self.assertEqual(1, len(execs), "exactly one action")
        self.assertEqual(POWERSHELL, self.value("t:Actions/t:Exec/t:Command"))
        arguments = self.value("t:Actions/t:Exec/t:Arguments")
        self.assertEqual(
            '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "REPLACE_WITH_RUNTIME_ROOT\\claude_supervisor.ps1" '
            '-SettingsPath "REPLACE_WITH_PRIVATE_SUPERVISOR_SETTINGS_JSON"',
            arguments,
        )
        for forbidden in ("launcher.ps1", "-Command", "python", "energygrid_bill_downloader", "claude.exe", "-ValidateOnly", "token"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden.lower(), arguments.lower())
        self.assertIsNone(self.root.find("t:Actions/t:Exec/t:WorkingDirectory", NS))

    def test_thirty_minute_ceiling_and_never_local_system(self) -> None:
        self.assertEqual("PT30M", self.value("t:Settings/t:ExecutionTimeLimit"))
        user = self.value("t:Principals/t:Principal/t:UserId")
        self.assertNotIn(user.upper(), {"SYSTEM", "LOCALSYSTEM", "NT AUTHORITY\\SYSTEM", "S-1-5-18"})
        self.assertNotIn("HighestAvailable", self.text)
        self.assertNotIn("S4U", self.text)

    def test_register_example_is_inert_and_names_the_supervisor(self) -> None:
        example = (TEMPLATE.parent / "register_task.example.ps1").read_text(encoding="utf-8")
        self.assertIn("claude_supervisor.ps1", example)
        code = [line for line in example.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual([], code, "the register example contains comments only")

    def test_no_private_value_is_committed(self) -> None:
        self.assertIsNone(re.search(r"S-1-(?:\d+-)+\d+", self.text))
        self.assertIsNone(re.search(r"(?i)[A-Z]:\\(?!Windows\\System32\\WindowsPowerShell)", self.text))
        for token in re.findall(r"REPLACE_WITH_[A-Z0-9_]+", self.text):
            self.assertRegex(token, r"\AREPLACE_WITH_[A-Z0-9_]+\Z")


@unittest.skipUnless(Path(POWERSHELL).exists(), "Windows PowerShell 5.1 is required")
class FileInvocationPreservesExitCode(unittest.TestCase):
    """The template's -File form returns the script's own exit code."""

    def test_script_exit_code_survives_file_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "exit_probe.ps1"
            script.write_text("param([string[]]$X)\nexit ([int]$X[0])\n", encoding="utf-8")
            for code in (0, 10, 20, 64, 70):
                with self.subTest(code=code):
                    completed = subprocess.run(
                        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(script), "-X", str(code)],
                        capture_output=True, text=True, timeout=120,
                    )
                    self.assertEqual(code, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
