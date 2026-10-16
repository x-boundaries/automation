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
TI = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
MODIFY = "0x1301bf"  # icacls (M)
READ_EXECUTE = "0x1200a9"  # icacls (RX)


def windows_powershell() -> str | None:
    if os.name != "nt":
        return None
    path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(path) if path.exists() else None


def run_harness(body: str, *args: str) -> dict:
    """Dot-source the guard, run ``body`` and return its single JSON line."""

    script = Path(tempfile.mkdtemp(prefix="xb-custody-harness-")) / "harness.ps1"
    try:
        script.write_text('$ErrorActionPreference = "Stop"\n. $args[0]\n' + body, encoding="utf-8")
        proc = subprocess.run(
            [windows_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), str(CUSTODY), *args],
            capture_output=True, timeout=180, check=False,
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
