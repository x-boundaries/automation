"""#155 G2 A2 (2026-10-16): code custody for the credential-bearing Owner
consoles, and Python import isolation for the core.

Offline. The custody guard is exercised against SDDL strings and against
temporary directories whose DACLs the test sets itself; no host ACL outside
the test's own temporary tree is changed. No console reaches a prompt.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "directlocal"))

import xb_directlocal as dl  # noqa: E402

CUSTODY = ROOT / "scripts" / "ac2_directlocal_code_custody.ps1"
CONSOLES = (ROOT / "scripts" / "ac2_directlocal_owner_console.ps1", ROOT / "scripts" / "ac2_directlocal_e7_owner_console.ps1")
CHECKER = ROOT / "scripts" / "ac2_directlocal_e7_isolation_check.py"
TI = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
MODIFY = "0x1301bf"  # icacls (M)
READ_EXECUTE = "0x1200a9"  # icacls (RX)


def windows_powershell() -> str | None:
    if os.name != "nt":
        return None
    path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(path) if path.exists() else None


def run_harness(body: str, *args: str, cwd: Path | None = None, env: dict | None = None) -> dict:
    """Dot-source the guard, run ``body`` and return its single JSON line."""

    script = Path(tempfile.mkdtemp(prefix="xb-custody-harness-")) / "harness.ps1"
    try:
        script.write_text('$ErrorActionPreference = "Stop"\n. $args[0]\n' + body, encoding="utf-8")
        proc = subprocess.run(
            [windows_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), str(CUSTODY), *args],
            capture_output=True, timeout=180, check=False, cwd=None if cwd is None else str(cwd), env=env,
        )
        lines = [line for line in proc.stdout.decode("utf-8", "replace").splitlines() if line.startswith("{")]
        if proc.returncode != 0 or not lines:
            raise AssertionError(f"harness failed: rc={proc.returncode} stderr={proc.stderr.decode('utf-8', 'replace')[-800:]}")
        return json.loads(lines[-1])
    finally:
        shutil.rmtree(script.parent, ignore_errors=True)


def set_dacl(path: Path, sddl: str) -> None:
    """Replace the whole DACL of a test-owned path (protected, from SDDL)."""

    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG))
    advapi32.GetSecurityDescriptorDacl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL))
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = (wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise OSError("sddl_invalid")
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise OSError("dacl_missing")
        if advapi32.SetNamedSecurityInfoW(str(path), 1, 0x4 | 0x80000000, None, None, dacl, None) != 0:
            raise OSError("set_dacl_failed")
    finally:
        kernel32.LocalFree(descriptor)


@unittest.skipUnless(windows_powershell(), "Windows PowerShell 5.1 required")
class CustodySddlTests(unittest.TestCase):
    """Pure descriptor rules (Get-XbDlSddlWriteRisk)."""

    def verdicts(self, cases: dict[str, tuple[str, bool]], user: str) -> dict:
        source = Path(tempfile.mkdtemp(prefix="xb-custody-cases-")) / "cases.json"
        try:
            source.write_text(json.dumps({name: {"sddl": sddl, "ancestor": ancestor} for name, (sddl, ancestor) in cases.items()}), encoding="utf-8")
            body = (
                "$cases = Get-Content -Raw -LiteralPath $args[1] | ConvertFrom-Json\n"
                "$out = [ordered]@{}\n"
                "foreach ($p in $cases.PSObject.Properties) {\n"
                "  $r = Get-XbDlSddlWriteRisk -Sddl $p.Value.sddl -UserSid $args[2] -Ancestor:([bool]$p.Value.ancestor)\n"
                "  $out[$p.Name] = if ($r) { $r } else { 'ok' }\n"
                "}\n"
                "[Console]::Out.WriteLine(($out | ConvertTo-Json -Compress))\n"
            )
            return run_harness(body, str(source), user)
        finally:
            shutil.rmtree(source.parent, ignore_errors=True)

    def test_descriptor_rules(self):
        user = "S-1-5-21-1-2-3-1001"
        other = "S-1-5-21-1-2-3-1002"
        private = f"D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{user})"
        cases = {
            "private": (f"O:{user}{private}", False),
            "program_files_like": (f"O:{TI}D:PAI(A;OICI;FA;;;{TI})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICIIO;GA;;;CO)(A;OICI;{READ_EXECUTE};;;BU)(A;OICI;{READ_EXECUTE};;;AC)", False),
            "owner_rights_entry": (f"O:BA{private}(A;;FA;;;OW)", False),
            "deny_entry_ignored": (f"O:SY{private}(D;OICI;FA;;;AU)", False),
            "empty_dacl": (f"O:{user}D:P", False),
            "authenticated_users_modify_inherited": (f"O:BAD:AI(A;OICIID;{MODIFY};;;AU)(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)", False),
            "users_write_data_only": (f"O:BA{private}(A;;0x2;;;BU)", False),
            "users_delete_only": (f"O:BA{private}(A;;SD;;;BU)", False),
            "users_generic_write": (f"O:BA{private}(A;;GW;;;BU)", False),
            "app_package_full_control": (f"O:BA{private}(A;;FA;;;S-1-15-2-1)", False),
            "other_account_write_dac": (f"O:BA{private}(A;;WD;;;{other})", False),
            "inherit_only_write_in_tree": (f"O:BA{private}(A;OICIIO;{MODIFY};;;AU)", False),
            "owner_untrusted": (f"O:{other}{private}", False),
            "owner_authenticated_users": (f"O:AU{private}", False),
            "null_dacl": (f"O:{user}D:NO_ACCESS_CONTROL", False),
            "unparseable": ("not-sddl", False),
            "object_ace": (f"O:BAD:(OA;;FA;bf967aba-0de6-11d0-a285-00aa003049e2;;AU)", False),
            # Ancestor rules: only replacement or takeover rights matter.
            "ancestor_drive_root_like": (f"O:{TI}D:PAI(A;;LC;;;AU)(A;OICIIO;SDGXGWGR;;;AU)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;{READ_EXECUTE};;;BU)(A;CI;0x4;;;BU)", True),
            "ancestor_inherit_only_modify": (f"O:BA{private}(A;OICIIO;{MODIFY};;;AU)", True),
            "ancestor_authenticated_users_modify": (f"O:BAD:AI(A;OICIID;{MODIFY};;;AU)(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)", True),
            "ancestor_delete_child": (f"O:BA{private}(A;;0x40;;;BU)", True),
            "ancestor_app_package_full_control": (f"O:BA{private}(A;;FA;;;S-1-15-2-1)", True),
            "ancestor_owner_untrusted": (f"O:{other}{private}", True),
        }
        expected = {
            "private": "ok", "program_files_like": "ok", "owner_rights_entry": "ok", "deny_entry_ignored": "ok", "empty_dacl": "ok",
            "authenticated_users_modify_inherited": "writable", "users_write_data_only": "writable", "users_delete_only": "writable",
            "users_generic_write": "writable", "app_package_full_control": "writable", "other_account_write_dac": "writable",
            "inherit_only_write_in_tree": "writable", "owner_untrusted": "owner_unsafe", "owner_authenticated_users": "owner_unsafe",
            "null_dacl": "acl_unverifiable", "unparseable": "acl_unverifiable", "object_ace": "acl_unverifiable",
            "ancestor_drive_root_like": "ok", "ancestor_inherit_only_modify": "ok", "ancestor_authenticated_users_modify": "writable",
            "ancestor_delete_child": "writable", "ancestor_app_package_full_control": "writable", "ancestor_owner_untrusted": "owner_unsafe",
        }
        self.assertEqual(self.verdicts(cases, user), expected)

    def test_interpreter_parent_rules(self):
        # G2 A5: the parent may not let anyone else add a file (pyvenv.cfg),
        # on top of the ancestor rules; read/execute and create-folder stay allowed.
        user = "S-1-5-21-1-2-3-1001"
        other = "S-1-5-21-1-2-3-1002"
        private = f"D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{user})"
        cases = {
            "program_files_like": f"O:{TI}D:PAI(A;;FA;;;{TI})(A;CIIO;GA;;;{TI})(A;;{MODIFY};;;SY)(A;OICIIO;GA;;;SY)(A;;{MODIFY};;;BA)(A;OICIIO;GA;;;BA)(A;;{READ_EXECUTE};;;BU)(A;OICIIO;GXGR;;;BU)(A;OICIIO;GA;;;CO)(A;;{READ_EXECUTE};;;AC)(A;OICIIO;GXGR;;;AC)",
            "drive_root_like": f"O:{TI}D:PAI(A;;LC;;;AU)(A;OICIIO;SDGXGWGR;;;AU)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;{READ_EXECUTE};;;BU)(A;CI;0x4;;;BU)",
            "inherit_only_modify": f"O:BA{private}(A;OICIIO;{MODIFY};;;AU)",
            "users_add_file": f"O:BA{private}(A;;0x2;;;BU)",
            "users_generic_write": f"O:BA{private}(A;;GW;;;BU)",
            "authenticated_users_modify_inherited": f"O:BAD:AI(A;OICIID;{MODIFY};;;AU)(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)",
            "other_account_add_file": f"O:BA{private}(A;CI;0x2;;;{other})",
            "delete_child": f"O:BA{private}(A;;0x40;;;BU)",
            "owner_untrusted": f"O:{other}{private}",
            "null_dacl": f"O:{user}D:NO_ACCESS_CONTROL",
        }
        source = Path(tempfile.mkdtemp(prefix="xb-custody-cases-")) / "cases.json"
        try:
            source.write_text(json.dumps(cases), encoding="utf-8")
            body = (
                "$cases = Get-Content -Raw -LiteralPath $args[1] | ConvertFrom-Json\n"
                "$out = [ordered]@{}\n"
                "foreach ($p in $cases.PSObject.Properties) {\n"
                "  $r = Get-XbDlSddlWriteRisk -Sddl $p.Value -UserSid $args[2] -InterpreterParent\n"
                "  $a = Get-XbDlSddlWriteRisk -Sddl $p.Value -UserSid $args[2] -Ancestor\n"
                "  $out[$p.Name] = @($(if ($r) { $r } else { 'ok' }), $(if ($a) { $a } else { 'ok' }))\n"
                "}\n"
                "[Console]::Out.WriteLine(($out | ConvertTo-Json -Compress))\n"
            )
            verdicts = run_harness(body, str(source), user)
        finally:
            shutil.rmtree(source.parent, ignore_errors=True)
        # [interpreter parent, ordinary ancestor]: add-file is the A5 difference.
        self.assertEqual(verdicts, {
            "program_files_like": ["ok", "ok"], "drive_root_like": ["ok", "ok"], "inherit_only_modify": ["ok", "ok"],
            "users_add_file": ["writable", "ok"], "users_generic_write": ["writable", "ok"],
            "authenticated_users_modify_inherited": ["writable", "writable"], "other_account_add_file": ["writable", "ok"],
            "delete_child": ["writable", "writable"], "owner_untrusted": ["owner_unsafe", "owner_unsafe"],
            "null_dacl": ["acl_unverifiable", "acl_unverifiable"],
        })

    def test_real_program_files_is_an_accepted_interpreter_parent(self):
        # Read-only: the host's own C:\Program Files descriptor (the parent of
        # an all-users C:\Program Files\PythonXY install) must not false-block.
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        body = (
            "$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value\n"
            "$r = Get-XbDlSddlWriteRisk -Sddl (Get-XbDlPathSddl (New-Object IO.DirectoryInfo $args[1])) -UserSid $sid -InterpreterParent\n"
            "[Console]::Out.WriteLine((@{ r = $(if ($r) { $r } else { 'ok' }) } | ConvertTo-Json -Compress))\n"
        )
        self.assertEqual(run_harness(body, program_files)["r"], "ok")


@unittest.skipUnless(windows_powershell(), "Windows PowerShell 5.1 required")
class CustodyFilesystemTests(unittest.TestCase):
    """The walkers against test-owned temporary trees."""

    def setUp(self):
        self.holder = Path(tempfile.mkdtemp(prefix="xb-custody-tree-"))
        self.user = dl._win_current_user_sid()
        self.private = f"D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{self.user})"
        self.root = self.holder / "code"
        self.root.mkdir()
        set_dacl(self.root, self.private)
        (self.root / "directlocal").mkdir()
        (self.root / "directlocal" / "core.py").write_text("print('x')\n", encoding="utf-8")
        (self.root / "README.md").write_text("x\n", encoding="utf-8")

    def tearDown(self):
        subprocess.run(["cmd", "/c", "rmdir", str(self.root / "link")], capture_output=True, check=False)
        shutil.rmtree(self.holder, ignore_errors=True)

    def entries(self, root: Path, max_entries: int = 25000) -> str:
        body = (
            "$r = Get-XbDlEntriesCustodyIssue -Root (New-Object IO.DirectoryInfo $args[1]) -Label 'code_root' -MaxEntries ([int]$args[2])\n"
            "[Console]::Out.WriteLine((@{ r = $(if ($r) { $r } else { 'ok' }) } | ConvertTo-Json -Compress))\n"
        )
        return run_harness(body, str(root), str(max_entries))["r"]

    def tree(self, root: Path) -> str:
        body = (
            "$r = Get-XbDlTreeCustodyIssue -Root $args[1] -Label 'code_root'\n"
            "[Console]::Out.WriteLine((@{ r = $(if ($r) { $r } else { 'ok' }) } | ConvertTo-Json -Compress))\n"
        )
        return run_harness(body, str(root))["r"]

    def test_private_tree_is_in_custody(self):
        self.assertEqual(self.entries(self.root), "ok")

    def test_one_writable_file_fails_closed(self):
        set_dacl(self.root / "directlocal" / "core.py", self.private + f"(A;;{MODIFY};;;AU)")
        self.assertEqual(self.entries(self.root), "code_root_writable")

    def test_inheritable_write_grant_on_a_subdirectory_fails_closed(self):
        set_dacl(self.root / "directlocal", self.private + f"(A;OICIIO;{MODIFY};;;BU)")
        self.assertEqual(self.entries(self.root), "code_root_writable")

    def test_read_only_grant_to_others_is_allowed(self):
        set_dacl(self.root, self.private + f"(A;OICI;{READ_EXECUTE};;;BU)")
        self.assertEqual(self.entries(self.root), "ok")

    def test_junction_inside_tree_is_refused_not_followed(self):
        target = self.holder / "elsewhere"
        target.mkdir()
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.root / "link"), str(target)], capture_output=True, check=False)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(self.entries(self.root), "code_root_reparse_point")

    def test_entry_cap_fails_closed(self):
        self.assertEqual(self.entries(self.root, max_entries=2), "code_root_too_large_to_verify")

    def test_writable_ancestor_fails_before_the_tree(self):
        # A broadly writable parent (the reported C:\XB shape): Authenticated Users Modify, inherited.
        parent = self.holder / "xb"
        parent.mkdir()
        set_dacl(parent, self.private + f"(A;OICI;{MODIFY};;;AU)")
        child = parent / "automation"
        child.mkdir()
        set_dacl(child, self.private)
        self.assertEqual(self.tree(child), "code_root_ancestor_writable")

    def test_junction_ancestor_is_refused(self):
        target = self.holder / "real"
        (target / "automation").mkdir(parents=True)
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.root / "link"), str(target)], capture_output=True, check=False)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(self.tree(self.root / "link" / "automation"), "code_root_ancestor_reparse_point")

    def test_path_shapes(self):
        self.assertEqual(self.tree(Path("relative") / "x"), "code_root_path_unsafe")
        self.assertEqual(self.tree(Path(r"\\server\share\automation")), "code_root_path_unsafe")
        self.assertEqual(self.tree(self.holder / "absent"), "code_root_missing")

    def test_owner_runtime_stops_on_the_code_root_first(self):
        parent = self.holder / "xb"
        parent.mkdir()
        set_dacl(parent, self.private + f"(A;OICI;{MODIFY};;;AU)")
        (parent / "automation" / ".git").mkdir(parents=True)
        body = (
            "$r = Get-XbDlOwnerRuntimeIssue -CodeRoot $args[1] -PythonExe 'C:\\absent\\python.exe' -GitRoot 'C:\\absent'\n"
            "[Console]::Out.WriteLine((@{ r = $r } | ConvertTo-Json -Compress))\n"
        )
        self.assertEqual(run_harness(body, str(parent / "automation"))["r"], "code_root_ancestor_writable")

    def interpreter_config(self, interpreter: Path) -> str:
        body = (
            "$r = Get-XbDlInterpreterConfigIssue -InterpreterDir (New-Object IO.DirectoryInfo $args[1])\n"
            "[Console]::Out.WriteLine((@{ r = $(if ($r) { $r } else { 'ok' }) } | ConvertTo-Json -Compress))\n"
        )
        return run_harness(body, str(interpreter))["r"]

    def interpreter_layout(self, parent_sddl: str) -> tuple[Path, Path]:
        parent = self.holder / "pyparent"
        interpreter = parent / "Python312"
        interpreter.mkdir(parents=True)
        set_dacl(parent, parent_sddl)
        set_dacl(interpreter, self.private)
        (interpreter / "python.exe").write_bytes(b"synthetic")
        return parent, interpreter

    def test_protected_interpreter_parent_is_accepted(self):
        # The Program Files shape: others may read and execute only.
        _, interpreter = self.interpreter_layout(self.private + f"(A;OICI;{READ_EXECUTE};;;BU)")
        self.assertEqual(self.interpreter_config(interpreter), "ok")

    def test_interpreter_parent_others_can_add_a_file_is_refused(self):
        # G2 A5 reproduction shape: add-file on the parent lets anyone plant pyvenv.cfg.
        _, interpreter = self.interpreter_layout(self.private + "(A;;0x2;;;AU)")
        self.assertEqual(self.interpreter_config(interpreter), "interpreter_parent_writable")

    def test_pyvenv_cfg_beside_or_above_the_interpreter_is_refused(self):
        parent, interpreter = self.interpreter_layout(self.private)
        for where in (parent, interpreter):
            with self.subTest(where=where.name):
                config = where / "pyvenv.cfg"
                config.write_text("home = C:\\elsewhere\n", encoding="utf-8")
                set_dacl(config, self.private)  # even a trusted-owned, protected one
                self.assertEqual(self.interpreter_config(interpreter), "interpreter_pyvenv_cfg_present")
                config.unlink()
        (parent / "pyvenv.cfg").mkdir()
        self.assertEqual(self.interpreter_config(interpreter), "interpreter_pyvenv_cfg_present")

    def test_owner_runtime_checks_interpreter_config_before_git(self):
        text = CUSTODY.read_text(encoding="utf-8")
        runtime = text[text.index("function Get-XbDlOwnerRuntimeIssue"):]
        tree = runtime.index('-Label "interpreter"')
        config = runtime.index("Get-XbDlInterpreterConfigIssue -InterpreterDir")
        self.assertLess(tree, config)
        self.assertLess(config, runtime.index('-Label "git"'))


class OwnerConsoleShapeTests(unittest.TestCase):
    """Both credential-bearing consoles prove custody before git, Python or a prompt."""

    def test_custody_precedes_git_python_and_prompts(self):
        for console in CONSOLES:
            with self.subTest(console=console.name):
                text = console.read_text(encoding="utf-8")
                self.assertIn('. (Join-Path $PSScriptRoot "ac2_directlocal_code_custody.ps1")', text)
                guard = text.index("$custody = Get-XbDlOwnerRuntimeIssue")
                for later in ("& $git ", "& $pythonExe ", "= Read-Host"):
                    self.assertLess(guard, text.index(later), later)
                for removed in ("& git ", "& $Python ", "--untracked-files=no"):
                    self.assertNotIn(removed, text)
                self.assertIn("--untracked-files=all --ignored", text)
                self.assertIn("core.fsmonitor=false", text)
                self.assertIn("& $pythonExe -I -B ", text)
                self.assertIn('"git_environment_override"', text)
        core = CONSOLES[0].read_text(encoding="utf-8")
        self.assertIn('$psi.Arguments = "-I -S -B " + ($quoted -join " ")', core)
        self.assertIn("$psi.FileName = $pythonExe", core)

    def test_working_directory_pinned_before_the_first_native_command(self):
        # G2 A4: custody, then the pin, then git, the collector and the child.
        for console in CONSOLES:
            with self.subTest(console=console.name):
                text = console.read_text(encoding="utf-8")
                guard = text.index("$custody = Get-XbDlOwnerRuntimeIssue")
                pin = text.index("$pinned = Enter-XbDlTrustedWorkingDirectory -Root $repo")
                self.assertLess(guard, pin)
                self.assertIn("if ($pinned) { Stop-", text)
                for later in ("& $git ", "& $pythonExe ", "= Read-Host", "$psi = New-Object System.Diagnostics.ProcessStartInfo"):
                    self.assertLess(pin, text.index(later), later)
                # No native command anywhere before the pin.
                self.assertNotRegex(text[:pin], r"(?m)^\s*[^#\n]*(&\s*\$|\bStart-Process\b|::Start\()")
                self.assertLess(text.index("$psi.WorkingDirectory = $repo"), text.index("[System.Diagnostics.Process]::Start($psi)"))
                # Every native child is started by an absolute path.
                for bare in ("& python", "& powershell", "& cmd", "& git", "tasklist", "schtasks"):
                    self.assertNotIn(bare, text)
        custody = CUSTODY.read_text(encoding="utf-8")
        function = custody[custody.index("function Enter-XbDlTrustedWorkingDirectory"):custody.index("function Get-XbDlOwnerRuntimeIssue")]
        for required in ('$env:NoDefaultCurrentDirectoryInExePath = "1"', "Set-Location -LiteralPath $full", "[Environment]::CurrentDirectory = $full", '"working_directory_unpinned"'):
            self.assertIn(required, function)

    def test_collector_starts_system_tools_by_absolute_path_only(self):
        text = CHECKER.read_text(encoding="utf-8")
        self.assertEqual(text.count("subprocess.run("), 1)
        self.assertIn("subprocess.run([str(executable), *args]", text)
        self.assertIn("cwd=str(system)", text)
        self.assertIn("GetSystemDirectoryW", text)
        self.assertEqual(sorted(re.findall(r'_run\("(\w+)"', text)), ["powershell", "schtasks", "tasklist"])
        self.assertNotRegex(text, r'_run\(\[')
        self.assertNotIn("shell=True", text)

    @unittest.skipUnless(windows_powershell(), "Windows PowerShell 5.1 required")
    def test_console_refuses_non_interactive_input_before_any_prompt(self):
        proc = subprocess.run(
            [windows_powershell(), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CONSOLES[0]),
             "-StateRoot", str(ROOT / "absent"), "-Command", "BookCheck", "-ExpectedHead", "0" * 40],
            input=b"AED_TESTINGAPI\nuser\nsecret\n", capture_output=True, timeout=120, check=False,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout.decode().strip().splitlines()[-1]), {"status": "OWNER_PROCEDURE_STOPPED", "code": "owner_console_required"})
        self.assertNotIn(b"secret", proc.stdout + proc.stderr)


@unittest.skipUnless(windows_powershell(), "Windows PowerShell 5.1 required")
class LaunchDirectoryHijackTests(unittest.TestCase):
    """G2 A4 reproduction, inverted: a harmless planted ``tasklist.exe`` (a
    copy of hostname.exe) in the directory an elevated console was started
    from must never run. No console, credential or AutoCount effect: only the
    collector's process listing and the guard's pin function are exercised."""

    def setUp(self):
        self.holder = Path(tempfile.mkdtemp(prefix="xb-a4-plant-"))
        self.plant = self.holder / "launch"
        self.plant.mkdir()
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        shutil.copy2(system / "hostname.exe", self.plant / "tasklist.exe")
        self.safe = self.holder / "runtime"
        self.safe.mkdir()
        self.probe = self.holder / "probe" / "bare_tasklist.py"
        self.probe.parent.mkdir()
        # A child that still uses a bare name; it reports which tasklist answered.
        self.probe.write_text(
            "import subprocess, sys\n"
            "out = subprocess.run(['tasklist'], capture_output=True, text=True).stdout\n"
            "print('PLANTED' if out.strip().lower() == sys.argv[1].lower() else 'REAL' if out.count('\\n') >= 20 else 'OTHER')\n",
            encoding="utf-8")
        self.host = subprocess.run([str(system / "hostname.exe")], capture_output=True, text=True, check=True).stdout.strip()
        self.env = {k: v for k, v in os.environ.items() if k.lower() != "nodefaultcurrentdirectoryinexepath"}
        self.env["PATH"] = os.pathsep.join([str(self.plant), self.env.get("PATH", "")])

    def tearDown(self):
        shutil.rmtree(self.holder, ignore_errors=True)

    def test_control_planted_tasklist_runs_from_an_unpinned_launch_directory(self):
        # Proves the plant is live: the reviewed hazard without any defence.
        proc = subprocess.run([sys.executable, "-I", "-B", str(self.probe), self.host], capture_output=True, text=True,
                              cwd=str(self.plant), env=self.env, timeout=120, check=False)
        self.assertEqual(proc.stdout.strip(), "PLANTED", proc.stderr[-400:])

    def test_collector_never_runs_the_planted_tasklist(self):
        # The collector itself, started where the plant is and with the plant first on PATH.
        script = ("import importlib.util, json, sys\n"
                  "spec = importlib.util.spec_from_file_location('e7_isolation', sys.argv[1])\n"
                  "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                  "print(json.dumps({'rows': len(m.processes())}))\n")
        proc = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(CHECKER)], capture_output=True, text=True,
                              cwd=str(self.plant), env=self.env, timeout=120, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
        self.assertGreaterEqual(json.loads(proc.stdout.strip().splitlines()[-1])["rows"], 20)

    def pinned_probe(self, setup: str) -> str:
        body = setup + (
            "$py = $args[1]\n"
            "$out = & $py -I -B $args[2] $args[3]\n"
            "[Console]::Out.WriteLine((@{ r = [string]($out | Select-Object -Last 1); cwd = [Environment]::CurrentDirectory } | ConvertTo-Json -Compress))\n"
        )
        result = run_harness(body, sys.executable, str(self.probe), self.host, str(self.safe), cwd=self.plant, env=self.env)
        return result["r"].strip()

    def test_console_pin_keeps_even_a_bare_child_name_off_the_launch_directory(self):
        self.assertEqual(self.pinned_probe(""), "PLANTED")  # harness control: started in the plant
        self.assertEqual(self.pinned_probe("$p = Enter-XbDlTrustedWorkingDirectory -Root $args[4]; if ($p) { throw $p }\n"), "REAL")
        # Each defence alone is enough: the environment flag with the location left in the plant...
        self.assertEqual(self.pinned_probe('$env:NoDefaultCurrentDirectoryInExePath = "1"\n'), "REAL")
        # ...and the pinned location without the flag.
        self.assertEqual(self.pinned_probe(
            "$p = Enter-XbDlTrustedWorkingDirectory -Root $args[4]; if ($p) { throw $p }\n"
            "Remove-Item Env:\\NoDefaultCurrentDirectoryInExePath\n"), "REAL")

    def test_pin_reports_the_trusted_directory(self):
        body = ("$p = Enter-XbDlTrustedWorkingDirectory -Root $args[1]\n"
                "[Console]::Out.WriteLine((@{ r = $(if ($p) { $p } else { 'ok' }); cwd = [Environment]::CurrentDirectory; loc = (Get-Location).ProviderPath; flag = $env:NoDefaultCurrentDirectoryInExePath } | ConvertTo-Json -Compress))\n")
        result = run_harness(body, str(self.safe), cwd=self.plant, env=self.env)
        self.assertEqual(result, {"r": "ok", "cwd": str(self.safe), "loc": str(self.safe), "flag": "1"})
        self.assertEqual(run_harness(body, str(self.holder / "absent"), cwd=self.plant, env=self.env)["r"], "working_directory_unpinned")


class CoreImportIsolationTests(unittest.TestCase):
    """G2 reproduction: a planted module beside the core or in the gateway
    source must not shadow the standard library when the core runs the way
    the Owner console runs it (``python -I -S -B``)."""

    def setUp(self):
        self.tree = Path(tempfile.mkdtemp(prefix="xb-core-isolation-"))
        (self.tree / "directlocal").mkdir()
        shutil.copy2(ROOT / "directlocal" / "xb_directlocal.py", self.tree / "directlocal" / "xb_directlocal.py")
        shutil.copytree(ROOT / "member_gateway" / "src" / "xb_member_gateway", self.tree / "member_gateway" / "src" / "xb_member_gateway",
                        ignore=shutil.ignore_patterns("__pycache__"))
        planted = "raise SystemExit('planted_module_imported')\n"
        (self.tree / "directlocal" / "json.py").write_text(planted, encoding="utf-8")
        # decimal is first imported by the gateway package, after the path change.
        (self.tree / "member_gateway" / "src" / "decimal.py").write_text(planted, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tree, ignore_errors=True)

    def run_core(self, *flags: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *flags, str(self.tree / "directlocal" / "xb_directlocal.py"), "--state-root", "x", "--help"],
                              capture_output=True, timeout=120, check=False, cwd=str(self.tree))

    def test_isolated_core_ignores_planted_modules(self):
        proc = self.run_core("-I", "-S", "-B")
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertIn(b"DirectLocal #155 member-sync core", proc.stdout)
        self.assertNotIn(b"planted_module_imported", proc.stdout + proc.stderr)
        self.assertEqual(list(self.tree.rglob("__pycache__")), [])

    def test_controls_show_each_defence_is_needed(self):
        # Without -I the script's own directory is searched first.
        self.assertIn(b"planted_module_imported", self.run_core("-S", "-B").stderr)
        # A prepended gateway path would shadow the standard library.
        core = self.tree / "directlocal" / "xb_directlocal.py"
        core.write_text(core.read_text(encoding="utf-8").replace("sys.path.append(_GATEWAY_SRC)", "sys.path.insert(0, _GATEWAY_SRC)"), encoding="utf-8")
        self.assertIn(b"planted_module_imported", self.run_core("-I", "-S", "-B").stderr)


if __name__ == "__main__":
    unittest.main()
