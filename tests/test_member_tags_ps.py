"""#268 slice C1: the tagging tool's installer under Windows PowerShell 5.1.

Dry-run behaviour only (the default: no -Register). Nothing is registered;
Register-ScheduledTask is never reached and no stand-in is used (PS 5.1
ScheduledTasks module-shadow pitfall). Fake data only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_member_tags_task.ps1"
TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


@unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell 5.1 is not installed")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-tags-installer-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.config = self.temp / "member_tags.json"
        self.write_config(False)

    def write_config(self, write_enabled: bool) -> None:
        value = json.loads((ROOT / "config" / "member_tags.example.json").read_text(encoding="utf-8"))
        value["tags_write_enabled"] = write_enabled
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def run_installer(self, *extra: str, task_name: str = "XB-MemberTags-installer-test-never-registered"):
        command = [shutil.which("powershell"), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
                   "-UserId", "EXAMPLE-PC\\example-user", "-ConfigPath", str(self.config), "-PythonExe", sys.executable, "-TaskName", task_name, *extra]
        proc = subprocess.run(command, capture_output=True, timeout=180, check=False)
        return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")

    @staticmethod
    def task_xml(output: str):
        import xml.etree.ElementTree as ElementTree

        text = output.split("Task XML:", 1)[1].split("Nothing was registered.", 1)[0].strip()
        return ElementTree.fromstring(text.replace('encoding="UTF-16"', ""))

    def test_default_is_an_observe_dry_run_every_two_hours_from_0011(self):
        code, out = self.run_installer()
        self.assertEqual(code, 0, out)
        self.assertIn("Dry run (the default)", out)
        self.assertIn("Nothing was registered.", out)
        ns = TASK_NS
        task = self.task_xml(out)
        trigger = task.find(f"{ns}Triggers/{ns}TimeTrigger")
        self.assertEqual(trigger.findtext(f"{ns}Repetition/{ns}Interval"), "PT2H")
        self.assertTrue(trigger.findtext(f"{ns}StartBoundary").endswith("T00:11:00"))
        settings = task.find(f"{ns}Settings")
        self.assertEqual((settings.findtext(f"{ns}MultipleInstancesPolicy"), settings.findtext(f"{ns}ExecutionTimeLimit"), settings.findtext(f"{ns}Enabled")),
                         ("IgnoreNew", "PT20M", "true"))
        principal = task.find(f"{ns}Principals/{ns}Principal")
        self.assertEqual((principal.findtext(f"{ns}LogonType"), principal.findtext(f"{ns}RunLevel")), ("Password", "LeastPrivilege"))
        self.assertRegex(task.findtext(f"{ns}Actions/{ns}Exec/{ns}Arguments"), r'^"[^"]+\\directlocal\\xb_member_tags\.py" observe --config "[^"]+\\member_tags\.json"$')
        source = INSTALLER.read_text(encoding="utf-8")
        dry = source.index("if (-not $Register) {")
        self.assertLess(source.index("Get-ScheduledTask -TaskName $TaskName"), dry)  # an existing task is refused before the dry run too
        self.assertLess(dry, source.index("$credential = Get-Credential"))
        self.assertLess(dry, source.index("Register-ScheduledTask -TaskName"))
        self.assertEqual(source.count("Register-ScheduledTask -TaskName"), 1)
        code, out = self.run_installer("-Disabled", "-IntervalMinutes", "90")
        task = self.task_xml(out)
        self.assertEqual(task.findtext(f"{ns}Settings/{ns}Enabled"), "false")
        self.assertEqual(task.findtext(f"{ns}Triggers/{ns}TimeTrigger/{ns}Repetition/{ns}Interval"), "PT1H30M")

    def test_apply_needs_the_write_switch(self):
        code, out = self.run_installer("-Mode", "apply")
        self.assertNotEqual(code, 0)
        self.assertIn("tags_write_enabled true", out)
        self.write_config(True)
        code, out = self.run_installer("-Mode", "apply")
        self.assertEqual(code, 0, out)
        self.assertRegex(self.task_xml(out).findtext(f"{TASK_NS}Actions/{TASK_NS}Exec/{TASK_NS}Arguments"), r'" apply --config "')

    def test_refusals(self):
        cases = [
            (("-LogonType", "S4U"), "LogonType must be Password"),
            (("-Mode", "Apply"), "Mode must be observe or apply"),
            (("-Mode", "digest"), "Mode must be observe or apply"),
            (("-TaskPath", "\\xb\\"), "TaskPath is pinned"),
            (("-IntervalMinutes", "59"), "IntervalMinutes must be"),
            (("-IntervalMinutes", "241"), "IntervalMinutes must be"),
        ]
        for extra, message in cases:
            with self.subTest(extra=extra):
                code, out = self.run_installer(*extra)
                self.assertNotEqual(code, 0)
                self.assertIn(message, out)
                self.assertNotIn("Task XML:", out)
        for name in ("XB-MemberSync", "XB-MemberSync-Probe", "XB-TypeMove-L5", "XB-MemberBenefits", "XB-MemberBenefits-Apply"):
            with self.subTest(name=name):
                code, out = self.run_installer(task_name=name)
                self.assertNotEqual(code, 0)
                self.assertIn("may not be the #155 sync's, the type move's or the member core's", out)

    def test_a_refused_config_is_refused(self):
        self.config.write_text(json.dumps({"schema": "xb.directlocal.member_benefits.config.v1"}), encoding="utf-8")
        code, out = self.run_installer()
        self.assertNotEqual(code, 0)
        self.assertIn("refused by check-config", out)
        self.assertNotIn("Task XML:", out)


if __name__ == "__main__":
    unittest.main()
