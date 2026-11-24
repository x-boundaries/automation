"""#117: scheduled tasks on the AC2 server run only from pinned clones.

Failure this prevents: a task that runs from a shared checkout which a chat start-up hook
fast-forwards to origin/main (the VM's C:\\XB\\automation, 22-24 Nov 2026) runs every merge as
soon as that checkout moves, with no approval step.

Covers scripts/xb_pinned_clone.ps1 (the check and the action re-point), every VM installer's
use of it, and scripts/xb_repoint_task_clone.ps1. Runs under Windows PowerShell 5.1 with real
git in temporary folders. Nothing is registered or changed in Task Scheduler. Fake data only.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "xb_pinned_clone.ps1"
REPOINT = ROOT / "scripts" / "xb_repoint_task_clone.ps1"
HOURLY = ROOT / "scripts" / "install_autocount_stock_balance_hourly_task.ps1"

# Every installer of a task that runs on the AC2 server. The server-PC tasks (daily health, Drive
# mirror) and the retired member gateway worker are not in this list.
VM_INSTALLERS = (
    "install_autocount_stock_balance_hourly_task.ps1",
    "install_autocount_arap_aging_task.ps1",
    "install_autocount_stock_extract_task.ps1",
    "install_member_pos_exceptions_task.ps1",
    "install_member_sync_task.ps1",
    "install_member_sync_summary_task.ps1",
    "install_member_benefits_task.ps1",
    "install_member_tags_task.ps1",
    "install_member_removals_task.ps1",
)

POWERSHELL = shutil.which("powershell")
GIT = shutil.which("git")


def ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def strip_comments(text: str) -> str:
    code = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))


class StaticInstallerTests(unittest.TestCase):
    """Each VM installer checks its clone and refuses to register from anything else."""

    def test_every_vm_installer_refuses_an_unpinned_clone_before_registering(self):
        for name in VM_INSTALLERS:
            with self.subTest(installer=name):
                code = strip_comments((ROOT / "scripts" / name).read_text(encoding="utf-8"))
                self.assertIn('. (Join-Path $PSScriptRoot "xb_pinned_clone.ps1")', code)
                self.assertIn("Get-XbPinnedCloneProblem -RepoRoot $RepoRoot", code)
                self.assertEqual(code.count("Assert-XbPinnedClone "), 1)
                check = code.index("Assert-XbPinnedClone ")
                self.assertLess(code.index("Get-XbPinnedCloneProblem"), check)
                self.assertLess(check, code.index("Register-ScheduledTask"))
                if "Get-Credential" in code:
                    self.assertLess(check, code.index("Get-Credential"))
                for exit_word in ("exit 0",):
                    if exit_word in code:
                        # The dry run ends before the refusal, so a dry run still prints everything.
                        self.assertLess(code.index(exit_word), check)
                if "exit 0" in code:
                    self.assertIn("Write-Host (Format-XbPinnedCloneLine $pinProblem)", code)

    def test_every_installer_that_registers_a_vm_task_is_listed(self):
        server_pc_or_retired = {
            "install_daily_health_task.ps1",
            "install_mirror_main_to_drive_task.ps1",
            "install_ac2_member_gateway_worker.ps1",
        }
        found = {path.name for path in (ROOT / "scripts").glob("install_*.ps1")}
        self.assertEqual(found - server_pc_or_retired, set(VM_INSTALLERS))


class StaticRepointTests(unittest.TestCase):
    def setUp(self):
        self.code = strip_comments(REPOINT.read_text(encoding="utf-8"))

    def test_dry_run_is_the_default_and_stops_before_any_change(self):
        self.assertIn("[switch]$Apply", self.code)
        dry = self.code.index("if (-not $Apply) {")
        for later in ("Export-ScheduledTask", "Set-ScheduledTask", "Get-Credential", "WriteAllText"):
            self.assertLess(dry, self.code.index(later), later)
        self.assertIn('Write-Host "Dry run: nothing changed."', self.code)

    def test_backup_before_change_and_never_replace_or_remove(self):
        self.assertLess(self.code.index("WriteAllText($backupPath"), self.code.index("Set-ScheduledTask"))
        self.assertLess(self.code.index('"Running"'), self.code.index("Set-ScheduledTask"))
        for forbidden in ("Unregister-ScheduledTask", "Register-ScheduledTask", "-Force", "Disable-ScheduledTask",
                          "Start-ScheduledTask", "Start-Transcript", "ConvertFrom-SecureString", "Out-File"):
            self.assertNotIn(forbidden, self.code)

    def test_readback_compares_triggers_account_and_settings(self):
        self.assertIn('foreach ($part in @("Triggers", "Principals", "Settings"))', self.code)
        self.assertIn("-RestoreFromXml", self.code)
        self.assertIn("exit 1", self.code)

    def test_new_clone_must_pass_the_pinned_check(self):
        self.assertIn("Get-XbPinnedCloneProblem -RepoRoot $NewRepoRoot", self.code)
        self.assertIn('throw "Refusing: $pinProblem"', self.code)


@unittest.skipUnless(POWERSHELL and GIT, "Windows PowerShell 5.1 and git are needed")
class PowerShellTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xb-pinned-clone-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # git never looks above the temporary folder for a repository.
        self.env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(self.tmp.parent))

    def git(self, cwd, *args) -> str:
        done = subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd,
                              capture_output=True, text=True, env=self.env, check=True)
        return done.stdout.strip()

    def make_clone(self, purpose="test", files=None, detach=True, golive="golive", name=None) -> Path:
        """A git repo renamed to <golive>\\automation-<purpose>-<sha7> after its one commit."""
        work = self.tmp / ("work-" + purpose)
        work.mkdir()
        self.git(work, "init", "-q")
        for rel, body in (files or {"scripts/example.py": "print('example')\n"}).items():
            target = work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        self.git(work, "add", "-A")
        self.git(work, "commit", "-q", "-m", "fixture")
        sha = self.git(work, "rev-parse", "HEAD")
        if detach:
            self.git(work, "checkout", "-q", "--detach", sha)
        final = self.tmp / golive / (name or f"automation-{purpose}-{sha[:7]}")
        final.parent.mkdir(parents=True, exist_ok=True)
        work.rename(final)
        return final

    def ps(self, script: str):
        done = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                               "$ErrorActionPreference = 'Stop'; . " + ps_quote(LIB) + "; " + script],
                              capture_output=True, text=True, timeout=120, env=self.env)
        return done.returncode, done.stdout.strip(), done.stderr.strip()

    def problem(self, root) -> str:
        code, out, err = self.ps("Get-XbPinnedCloneProblem -RepoRoot " + ps_quote(root))
        self.assertEqual(code, 0, err)
        return out


class PinnedCheckTests(PowerShellTestCase):
    def test_a_detached_clean_clone_named_for_its_head_passes(self):
        clone = self.make_clone()
        self.assertEqual(self.problem(clone), "")
        self.assertEqual(self.problem(str(clone) + "\\"), "", "a trailing backslash is the same folder")
        code, out, _ = self.ps("Format-XbPinnedCloneLine ''")
        self.assertEqual(out, "Pinned clone: yes.")

    def test_untracked_files_are_allowed(self):
        clone = self.make_clone()
        (clone / "scripts" / "__pycache__").mkdir()
        (clone / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"\0")
        self.assertEqual(self.problem(clone), "")

    def test_a_branch_checkout_is_refused(self):
        # The start-up hook fast-forwards a branch; a detached HEAD has no branch to move.
        clone = self.make_clone(detach=False)
        self.assertIn("has a branch checked out", self.problem(clone))

    def test_a_changed_tracked_file_is_refused(self):
        clone = self.make_clone()
        (clone / "scripts" / "example.py").write_text("print('changed')\n", encoding="utf-8")
        self.assertIn("has changed tracked files", self.problem(clone))

    def test_a_head_that_does_not_match_the_folder_name_is_refused(self):
        clone = self.make_clone()
        (clone / "scripts" / "more.py").write_text("x = 1\n", encoding="utf-8")
        self.git(clone, "add", "-A")
        self.git(clone, "commit", "-q", "-m", "moved on")
        out = self.problem(clone)
        self.assertIn("HEAD in", out)
        self.assertIn("not " + clone.name[-7:], out)

    def test_the_shared_checkout_and_other_names_are_refused(self):
        cases = {
            "the shared checkout": dict(golive="XB", name="automation"),
            "not in a golive folder": dict(golive="build"),
            "no purpose in the name": dict(name="automation-abcdef1"),
            "uppercase sha": dict(name="automation-test-ABCDEF1"),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                clone = self.make_clone(purpose=re.sub(r"\W", "", label)[:12].lower(), **kwargs)
                self.assertIn("is not a pinned clone", self.problem(clone))

    def test_a_missing_folder_or_a_folder_without_git_is_refused(self):
        self.assertIn("does not exist", self.problem(self.tmp / "golive" / "automation-x-abcdef1"))
        bare = self.tmp / "golive" / "automation-nogit-abcdef1"
        bare.mkdir(parents=True)
        self.assertIn("git cannot read HEAD", self.problem(bare))

    def test_assert_refuses_with_the_reason_and_the_runbook(self):
        code, out, err = self.ps("Assert-XbPinnedClone 'the code folder X is not a pinned clone.'")
        self.assertNotEqual(code, 0)
        self.assertIn("Refusing to register: the code folder X is not a pinned clone.", err)
        self.assertIn("vm_pinned_clones_runbook.md", err)
        code, _, err = self.ps("Assert-XbPinnedClone ''")
        self.assertEqual(code, 0, err)


class RepointedActionTests(PowerShellTestCase):
    OLD = "C:\\XB\\automation"
    PY = "C:\\Python311\\python.exe"

    def repoint(self, arguments, new_root, execute=None, working=None):
        script = ("$r = Get-XbRepointedAction -Execute {e} -Arguments {a} -WorkingDirectory {w} -NewRepoRoot {n}; "
                  "Write-Output $r.Arguments; Write-Output $r.WorkingDirectory; $r.Notes | ForEach-Object {{ Write-Output ('NOTE ' + $_) }}").format(
            e=ps_quote(self.PY if execute is None else execute), a=ps_quote(arguments),
            w=ps_quote(self.OLD if working is None else working), n=ps_quote(new_root))
        return self.ps(script)

    def test_only_the_script_path_and_the_folder_change(self):
        clone = self.make_clone(files={"scripts/autocount_stock_balance_hourly.py": "x = 1\n"})
        args = '"C:\\XB\\automation\\scripts\\autocount_stock_balance_hourly.py" --config "D:\\private\\settings.local.json"'
        code, out, err = self.repoint(args, clone)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], f'"{clone}\\scripts\\autocount_stock_balance_hourly.py" --config "D:\\private\\settings.local.json"')
        self.assertEqual(lines[1], str(clone))
        self.assertEqual(len(lines), 2, "no notes")

    def test_subcommands_and_flags_are_kept(self):
        clone = self.make_clone(files={"scripts/xb_member_pos_exceptions.py": "x = 1\n"})
        args = '"C:\\XB\\automation\\scripts\\xb_member_pos_exceptions.py" run --scheduled --config "D:\\p\\s.json"'
        code, out, err = self.repoint(args, clone)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.splitlines()[0].endswith('" run --scheduled --config "D:\\p\\s.json"'), out)

    def test_a_settings_file_left_in_the_old_folder_is_kept_and_noted(self):
        clone = self.make_clone(files={"scripts/autocount_arap_aging.py": "x = 1\n"})
        args = '"C:\\XB\\automation\\scripts\\autocount_arap_aging.py" --config "C:\\XB\\automation\\config\\arap.local.json"'
        code, out, err = self.repoint(args, clone)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertTrue(lines[0].endswith('--config "C:\\XB\\automation\\config\\arap.local.json"'), out)
        self.assertIn("NOTE The argument C:\\XB\\automation\\config\\arap.local.json is under the old folder", out)

    def test_changes_that_would_change_behaviour_are_refused(self):
        clone = self.make_clone(files={"scripts/autocount_stock_extract.py": "x = 1\n"})
        good = '"C:\\XB\\automation\\scripts\\autocount_stock_extract.py" --config "D:\\p\\s.json"'
        cases = {
            "relative settings path": (dict(arguments='"C:\\XB\\automation\\scripts\\autocount_stock_extract.py" --config "config\\x.local.json"'), "relative path"),
            "relative unquoted": (dict(arguments='"C:\\XB\\automation\\scripts\\autocount_stock_extract.py" --config x.json'), "relative path"),
            "bare program name": (dict(arguments=good, execute="python.exe"), "not a full path"),
            "no working folder": (dict(arguments=good, working=""), "no full-path working folder"),
            "script not under the folder": (dict(arguments='"D:\\other\\scripts\\autocount_stock_extract.py"'), "do not start with"),
            "script missing in the new clone": (dict(arguments='"C:\\XB\\automation\\scripts\\missing.py"'), "is not in"),
            "parent folder step": (dict(arguments='"C:\\XB\\automation\\..\\x\\scripts\\autocount_stock_extract.py"'), "not a plain file"),
            "already there": (dict(arguments='"%s\\scripts\\autocount_stock_extract.py"' % clone, working=str(clone)), "already runs from"),
        }
        for label, (kwargs, expected) in cases.items():
            with self.subTest(label):
                code, out, err = self.repoint(kwargs.pop("arguments"), clone, **kwargs)
                self.assertNotEqual(code, 0, out)
                self.assertIn(expected, err)


class InstallerDryRunTests(PowerShellTestCase):
    """The hourly installer's dry run reports the check, from this checkout and from a pinned clone."""

    def run_hourly(self, *extra):
        stub = self.tmp / "stub"
        stub.mkdir(exist_ok=True)
        (stub / "pyodbc.py").write_text("# test stub\n", encoding="utf-8")
        sys.path.insert(0, str(ROOT / "tests"))
        try:
            from test_autocount_stock_balance_hourly import raw_settings  # noqa: WPS433 (shared fixture)
        finally:
            sys.path.pop(0)
        import json

        config = self.tmp / "settings.local.json"
        config.write_text(json.dumps(raw_settings(str(self.tmp))), encoding="utf-8")
        env = dict(self.env, PYTHONPATH=os.pathsep.join(p for p in (str(stub), os.environ.get("PYTHONPATH", "")) if p))
        done = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(HOURLY),
                               "-UserId", "EXAMPLE-PC\\example-user", "-ConfigPath", str(config), "-PythonExe", sys.executable,
                               "-TaskName", "XB pinned clone test (never registered)", "-DryRun", *extra],
                              capture_output=True, text=True, timeout=180, env=env, cwd=self.tmp)
        return done.returncode, done.stdout + done.stderr

    def test_dry_run_from_this_checkout_says_registering_would_be_refused(self):
        code, out = self.run_hourly()
        self.assertEqual(code, 0, out)
        self.assertIn("Pinned clone: NO, so registering would be refused: the code folder", out)
        self.assertIn("Nothing was registered.", out)

    def test_dry_run_from_a_pinned_clone_passes_the_check(self):
        files = {str(p.relative_to(ROOT)).replace("\\", "/"): p.read_text(encoding="utf-8")
                 for p in (ROOT / "scripts").glob("*.py")}
        clone = self.make_clone(purpose="hourly", files=files)
        code, out = self.run_hourly("-RepoRoot", str(clone))
        self.assertEqual(code, 0, out)
        self.assertIn("Repo root: " + str(clone), out)
        self.assertIn("Pinned clone: yes.", out)


@unittest.skipUnless(POWERSHELL, "Windows PowerShell 5.1 is needed")
class RepointScriptTests(unittest.TestCase):
    def run_repoint(self, *args):
        done = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(REPOINT), *args],
                              capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout + done.stderr

    def test_exactly_one_mode(self):
        for args in ((), ("-NewRepoRoot", "C:\\x", "-RestoreFromXml", "C:\\y.xml")):
            with self.subTest(args=args):
                code, out = self.run_repoint("-TaskName", "XB pinned clone test (never registered)", *args)
                self.assertNotEqual(code, 0)
                self.assertIn("Give exactly one of -NewRepoRoot", out)

    def test_a_missing_task_is_refused(self):
        code, out = self.run_repoint("-TaskName", "XB pinned clone test (never registered)", "-NewRepoRoot", "C:\\x")
        self.assertNotEqual(code, 0)
        self.assertIn("No scheduled task", out)

    def test_parse_only(self):
        for path in (REPOINT, LIB):
            with self.subTest(path=path.name):
                command = ("$errs = $null; [System.Management.Automation.Language.Parser]::ParseFile(%s, [ref]$null, [ref]$errs) | Out-Null; "
                           "if (@($errs).Count -gt 0) { $errs | ForEach-Object { Write-Output $_.Message }; exit 1 }") % ps_quote(path)
                done = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
