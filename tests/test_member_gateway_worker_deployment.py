"""Deployment-tooling split and immutable worker blob contracts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_MISSING = object()


PROTECTED_WORKER_BLOBS = {
    "scripts/install_ac2_member_gateway_worker.ps1": "fee4fac43185eaf893966e176d23216ac9544b7f",
    "scripts/ac2_member_gateway_worker.ps1": "27f0a3f8c78ba9b391b1a09ad33fe5805f723cc1",
    "scripts/ac2_member_gateway_worker_lib.ps1": "332af5a25f2be996694fdb6cef085139192ebf19",
    "scripts/ac2_member_gateway_autocount_adapter.ps1": "37ea54fea46c57671b02cbc15a138791a2977244",
}

WORKER_SCRIPTS = tuple(PROTECTED_WORKER_BLOBS) + (
    "scripts/launch_ac2_member_gateway_worker.ps1",
    "scripts/test_ac2_member_gateway_autocount_dependencies.ps1",
)

ALLOWED_FILES = {
    ".github/workflows/member-gateway-tests.yml",
    "config/ac2_member_gateway_worker.production.example.json",
    "config/member_forms_gateway_bounded_import.v2.template.json",
    "docs/autocount2-automation/member_gateway_production_runbook.md",
    "n8n-workflows/README.md",
    "n8n-workflows/scripts/README.md",
    "n8n-workflows/scripts/import-member-forms-gateway-bounded.ps1",
    "scripts/install_ac2_member_gateway_worker.ps1",
    "scripts/launch_ac2_member_gateway_worker.ps1",
    "scripts/test_ac2_member_gateway_autocount_dependencies.ps1",
    "tests/test_member_gateway_worker_deployment.py",
    "tests/test_member_gateway_bounded_import_security.py",
    "tests/test_member_gateway_ci.py",
}


def _assert_autocount_probe_contract(probe: str) -> None:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    require(
        'if ($PSVersionTable.PSEdition -ne "Desktop" -or $PSVersionTable.PSVersion.Major -ne 5) {' in probe,
        "PowerShell Desktop/version gate is missing",
    )
    require(
        'if (-not [Environment]::Is64BitProcess) { throw "autocount_64bit_powershell_required" }' in probe,
        "64-bit gate is missing",
    )

    required_assembly_match = re.search(
        r"\$requiredAssemblies\s*=\s*@\(\n(?P<body>.*?)^\)",
        probe,
        re.MULTILINE | re.DOTALL,
    )
    require(required_assembly_match is not None, "required assembly set is missing")
    required_assemblies = re.findall(r'"([^"\r\n]+\.dll)"', required_assembly_match.group("body"))
    require(
        required_assemblies
        == [
            "AutoCount.dll",
            "AutoCount.Accounting.dll",
            "AutoCount.Invoicing.dll",
            "AutoCount.ImportExport.dll",
            "AutoCount.Tools.dll",
        ],
        "required assembly set/order is not the accepted five-assembly contract",
    )

    require(
        probe.count("[Reflection.Assembly]::ReflectionOnlyLoadFrom($path)") == 1,
        "ReflectionOnlyLoadFrom loading contract is missing or duplicated",
    )
    require(
        '$loaded[$name] = [Reflection.Assembly]::ReflectionOnlyLoadFrom($path)' in probe,
        "required assemblies are not loaded into the named map",
    )
    require(
        probe.count('$invoicing = $loaded["AutoCount.Invoicing.dll"]') == 1,
        "invoicing binding is not exact",
    )

    member_command_lines = [
        line.strip() for line in probe.splitlines() if "AutoCount.BonusPoint.Member.MemberCommand" in line
    ]
    require(
        member_command_lines
        == [
            '@($invoicing, "AutoCount.BonusPoint.Member.MemberCommand")',
            '$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)',
        ],
        "MemberCommand has an alternate binding, scan, or fallback",
    )

    required_methods_match = re.search(
        r"foreach \(\$name in @\((?P<body>[^)]*)\)\)\s*\{\s*"
        r"if \(\$methodNames -notcontains \$name\)",
        probe,
        re.DOTALL,
    )
    require(required_methods_match is not None, "required MemberCommand method check is missing")
    required_methods = re.findall(r'"([^"\r\n]+)"', required_methods_match.group("body"))
    require(
        required_methods == ["Create", "GetMember", "NewMember", "SaveMember"],
        "required MemberCommand method set is incomplete or changed",
    )
    require(
        "$memberCommand.GetMethods() | ForEach-Object Name" in probe,
        "MemberCommand method enumeration is missing",
    )

    for forbidden in (
        "AssemblyResolve",
        "GetFiles(",
        "EnumerateFiles(",
        "GetFileSystemEntries(",
        "Get-ChildItem",
        "[IO.Directory]::",
        "[IO.DirectoryInfo]::",
    ):
        require(forbidden not in probe, f"forbidden broad/fallback resolver token: {forbidden}")


class MemberGatewayWorkerDeploymentTests(unittest.TestCase):
    @staticmethod
    def _blob(path: str) -> str:
        return subprocess.run(
            ["git", "hash-object", path],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @classmethod
    def setUpClass(cls) -> None:
        cls.pwsh = shutil.which("powershell")
        if not cls.pwsh:
            windows_root = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
            if windows_root:
                desktop_powershell = Path(windows_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
                if desktop_powershell.is_file():
                    cls.pwsh = str(desktop_powershell)
        cls.pwsh = cls.pwsh or shutil.which("pwsh")

    @staticmethod
    def _desktop_powershell_environment() -> dict[str, str]:
        env = os.environ.copy()
        module_path_key = next((key for key in env if key.casefold() == "psmodulepath"), None)
        module_path = env.get(module_path_key) if module_path_key else None
        if module_path and module_path_key:
            env[module_path_key] = os.pathsep.join(
                path
                for path in module_path.split(os.pathsep)
                if "native\\powershell\\modules" not in path.casefold()
            )
        return env

    def _run_powershell_harness(self, script: str, *arguments: str, env: dict[str, str] | None = None):
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-harness-") as temp_dir:
            harness = Path(temp_dir) / "harness.ps1"
            harness.write_text(script, encoding="utf-8", newline="\n")
            return subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(harness),
                    *arguments,
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )

    def test_protected_worker_blobs_are_unchanged(self) -> None:
        for path, expected in PROTECTED_WORKER_BLOBS.items():
            with self.subTest(path=path):
                self.assertEqual(self._blob(path), expected)

    def test_approved_worker_scripts_parse_without_execution(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for parser validation")
        for relative in WORKER_SCRIPTS:
            command = (
                "$errors = $null; $tokens = $null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path '{relative}'), "
                "[ref]$tokens, [ref]$errors); "
                "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
            )
            with self.subTest(path=relative):
                completed = subprocess.run(
                    [self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_autocount_probe_binds_member_command_to_invoicing(self) -> None:
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        _assert_autocount_probe_contract(probe)

        rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Tools.dll"]',
            1,
        )
        alternate_scan = probe.replace(
            '$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)',
            '$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)\n'
            '$alternateMemberCommand = @($loaded.Values | ForEach-Object { $_.GetType("AutoCount.BonusPoint.Member.MemberCommand", $false, $false) } | Where-Object { $null -ne $_ } | Select-Object -First 1)\n'
            'if ($null -eq $memberCommand) { $memberCommand = $alternateMemberCommand }',
            1,
        )
        sixth_assembly = probe.replace(
            '    "AutoCount.Tools.dll"\n)',
            '    "AutoCount.Tools.dll",\n    "AutoCount.Extended.dll"\n)',
            1,
        )

        for name, counterexample in (
            ("wrong invoicing binding", rebind),
            ("alternate assembly MemberCommand scan", alternate_scan),
            ("sixth required assembly", sixth_assembly),
        ):
            with self.subTest(counterexample=name):
                with self.assertRaises(AssertionError):
                    _assert_autocount_probe_contract(counterexample)

    def test_production_launcher_accepts_exact_strings_and_preserves_dpapi_mapping(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-runtime-") as temp_dir:
            runtime = Path(temp_dir)
            (runtime / "config").mkdir()
            (runtime / "secrets").mkdir()
            config = {
                "gateway_base_url": "https://gateway.example.test",
                "worker_host_binding": "host-ac2-worker",
                "autocount_assembly_path": r"C:\\AutoCount",
                "autocount_server_name": "  server exact  ",
                "autocount_database_name": "database exact",
                "autocount_user_id": "user exact",
            }
            (runtime / "config" / "worker.config.json").write_text(json.dumps(config), encoding="utf-8")
            launcher_text = (ROOT / "scripts/launch_ac2_member_gateway_worker.ps1").read_text(encoding="utf-8")
            self.assertIn("Import-Clixml -LiteralPath $Path", launcher_text)
            self.assertIn("$secureValue -isnot [Security.SecureString]", launcher_text)
            harness = r'''
$launcher = Get-Content -Raw -LiteralPath $args[0]
$prefix = $launcher.Substring(0, $launcher.IndexOf('$runId = '))
Invoke-Expression $prefix
function Read-XbCurrentUserSecretArtifact {
    param([Parameter(Mandatory)][string]$Path)
    if ($Path -like '*worker-token.clixml') { return 'token' }
    return 'password'
}
$info = New-XbWorkerProcessStartInfo -WorkerScript $args[1] -LauncherMode Production -RuntimeRootPath $args[2]
[ordered]@{
    server = $info.EnvironmentVariables['XB_AC2_SERVER_NAME']
    database = $info.EnvironmentVariables['XB_AC2_DATABASE_NAME']
    user = $info.EnvironmentVariables['XB_AC2_USER_ID']
    probe_server = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_SERVER_NAME')
    probe_database = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_DATABASE_NAME')
    probe_user = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_USER_ID')
    probe_password = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_PASSWORD')
    session_factory = $info.EnvironmentVariables.ContainsKey('XB_AC2_SESSION_FACTORY')
    password = $info.EnvironmentVariables['XB_AC2_PASSWORD']
    password_env = $info.EnvironmentVariables['XB_AC2_PASSWORD_ENV_VAR']
} | ConvertTo-Json -Compress
'''
            env = os.environ.copy()
            env.update(
                {
                    "AC2_PROBE_SERVER_NAME": "legacy-server",
                    "AC2_PROBE_DATABASE_NAME": "legacy-database",
                    "AC2_PROBE_USER_ID": "legacy-user",
                    "AC2_PROBE_PASSWORD": "legacy-password",
                    "XB_AC2_SESSION_FACTORY": "legacy-factory",
                }
            )
            completed = self._run_powershell_harness(
                harness,
                str(ROOT / "scripts/launch_ac2_member_gateway_worker.ps1"),
                r"C:\synthetic-worker.ps1",
                str(runtime),
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            observed = json.loads(completed.stdout)
            self.assertEqual(observed["server"], "  server exact  ")
            self.assertEqual(observed["database"], "database exact")
            self.assertEqual(observed["user"], "user exact")
            for name in ("probe_server", "probe_database", "probe_user", "probe_password", "session_factory"):
                self.assertFalse(observed[name], name)
            self.assertEqual(observed["password"], "password")
            self.assertEqual(observed["password_env"], "XB_AC2_PASSWORD")

    def test_invalid_production_identity_cannot_start_synthetic_child(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        invalid_values = (_MISSING, None, 7, [], "", "   ")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-invalid-") as temp_dir:
            root = Path(temp_dir)
            install = root / "install"
            runtime = root / "runtime"
            (runtime / "config").mkdir(parents=True)
            (runtime / "secrets").mkdir()
            install.mkdir()
            marker = root / "child-started.txt"
            (install / "ac2_member_gateway_worker.ps1").write_text(
                "param([switch]$EnableProductionWorker, [switch]$EnableProductionAdapter)\n"
                "$marker = [Environment]::GetEnvironmentVariable('XB_TEST_CHILD_MARKER', 'Process')\n"
                "if ([string]::IsNullOrWhiteSpace($marker)) { throw 'synthetic_marker_missing' }\n"
                "[IO.File]::WriteAllText($marker, 'started')\n"
                "[pscustomobject]@{ status = 'completed'; writes = 0 } | ConvertTo-Json -Compress\n",
                encoding="utf-8",
            )
            base_config = {
                "gateway_base_url": "https://gateway.example.test",
                "worker_host_binding": "host-ac2-worker",
                "autocount_assembly_path": r"C:\\AutoCount",
                "autocount_server_name": "synthetic-server",
                "autocount_database_name": "synthetic-database",
                "autocount_user_id": "synthetic-user",
            }
            (runtime / "config" / "worker.config.json").write_text(
                json.dumps(base_config), encoding="utf-8"
            )
            secret_environment = self._desktop_powershell_environment()
            secret_environment["XB_TEST_WORKER_TOKEN_PATH"] = str(
                runtime / "secrets" / "worker-token.clixml"
            )
            secret_environment["XB_TEST_PASSWORD_PATH"] = str(
                runtime / "secrets" / "autocount-password.clixml"
            )
            secrets = subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$ErrorActionPreference = 'Stop'; "
                    "ConvertTo-SecureString -String 'synthetic-worker-token' -AsPlainText -Force | "
                    "Export-Clixml -LiteralPath $env:XB_TEST_WORKER_TOKEN_PATH; "
                    "ConvertTo-SecureString -String 'synthetic-autocount-password' -AsPlainText -Force | "
                    "Export-Clixml -LiteralPath $env:XB_TEST_PASSWORD_PATH",
                ],
                cwd=ROOT,
                env=secret_environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(secrets.returncode, 0, secrets.stdout + secrets.stderr)
            self.assertTrue((runtime / "secrets" / "worker-token.clixml").is_file())
            self.assertTrue((runtime / "secrets" / "autocount-password.clixml").is_file())
            env = self._desktop_powershell_environment()
            env["XB_TEST_CHILD_MARKER"] = str(marker)
            launcher = ROOT / "scripts/launch_ac2_member_gateway_worker.ps1"

            control = subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(launcher),
                    "-Mode",
                    "Production",
                    "-InstallRoot",
                    str(install),
                    "-RuntimeRoot",
                    str(runtime),
                    "-ExecutionTimeoutMilliseconds",
                    "5000",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
            control_result = json.loads(control.stdout)
            self.assertEqual(control_result["exit_code"], 0)
            self.assertEqual(control_result["terminal_status"], "worker_completed")
            self.assertTrue(marker.exists(), control.stdout + control.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "started")

            for field in ("autocount_server_name", "autocount_database_name", "autocount_user_id"):
                for value in invalid_values:
                    with self.subTest(field=field, value=value):
                        config = dict(base_config)
                        if value is _MISSING:
                            config.pop(field)
                        else:
                            config[field] = value
                        (runtime / "config" / "worker.config.json").write_text(
                            json.dumps(config), encoding="utf-8"
                        )
                        if marker.exists():
                            marker.unlink()
                        completed = subprocess.run(
                            [
                                self.pwsh,
                                "-ExecutionPolicy",
                                "Bypass",
                                "-NoLogo",
                                "-NoProfile",
                                "-NonInteractive",
                                "-File",
                                str(launcher),
                                "-Mode",
                                "Production",
                                "-InstallRoot",
                                str(install),
                                "-RuntimeRoot",
                                str(runtime),
                                "-ExecutionTimeoutMilliseconds",
                                "5000",
                            ],
                            cwd=ROOT,
                            env=env,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                        result = json.loads(completed.stdout)
                        self.assertEqual(result["exit_code"], 1)
                        self.assertEqual(result["terminal_status"], "launcher_failed")
                        self.assertFalse(marker.exists(), completed.stdout + completed.stderr)

    def test_disabled_proof_does_not_read_production_config_or_secrets(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-disabled-") as temp_dir:
            root = Path(temp_dir)
            install = root / "install"
            runtime = root / "runtime"
            install.mkdir()
            (install / "ac2_member_gateway_worker.ps1").write_text(
                "[pscustomobject]@{ status = 'disabled'; writes = 0; dispatch_fence = $false } | ConvertTo-Json -Compress\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(ROOT / "scripts/launch_ac2_member_gateway_worker.ps1"),
                    "-Mode",
                    "DisabledProof",
                    "-InstallRoot",
                    str(install),
                    "-RuntimeRoot",
                    str(runtime),
                    "-ExecutionTimeoutMilliseconds",
                    "5000",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            observed = json.loads(completed.stdout)
            self.assertEqual(observed["terminal_status"], "disabled_proof_pass")

    def test_production_launcher_binds_exact_identity_and_removes_legacy_environment(self) -> None:
        launcher = (ROOT / "scripts/launch_ac2_member_gateway_worker.ps1").read_text(encoding="utf-8")
        fields = {
            "autocount_server_name": "launcher_autocount_server_name_invalid",
            "autocount_database_name": "launcher_autocount_database_name_invalid",
            "autocount_user_id": "launcher_autocount_user_id_invalid",
        }
        for field, error_id in fields.items():
            with self.subTest(field=field):
                self.assertIn(f'PropertyName "{field}"', launcher)
                self.assertIn(error_id, launcher)
        for environment_name in (
            "XB_AC2_SERVER_NAME",
            "XB_AC2_DATABASE_NAME",
            "XB_AC2_USER_ID",
        ):
            self.assertIn(f'EnvironmentVariables["{environment_name}"]', launcher)
        for environment_name in (
            "AC2_PROBE_SERVER_NAME",
            "AC2_PROBE_DATABASE_NAME",
            "AC2_PROBE_USER_ID",
            "AC2_PROBE_PASSWORD",
            "XB_AC2_SESSION_FACTORY",
        ):
            self.assertIn(f'EnvironmentVariables.Remove("{environment_name}")', launcher)
        self.assertIn('$value -isnot [string]', launcher)
        self.assertIn('[string]::IsNullOrWhiteSpace($value)', launcher)
        self.assertNotIn('[string]$config.autocount_server_name', launcher)
        self.assertNotIn('[string]$config.autocount_database_name', launcher)
        self.assertNotIn('[string]$config.autocount_user_id', launcher)
        self.assertLess(
            launcher.index('$autocountServerName = Get-XbRequiredProductionConfigString'),
            launcher.index('if (-not $process.Start())'),
        )

    def test_worker_production_example_has_unbound_identity_placeholders(self) -> None:
        example = json.loads(
            (ROOT / "config/ac2_member_gateway_worker.production.example.json").read_text(encoding="utf-8")
        )
        for field in (
            "gateway_base_url",
            "worker_host_binding",
            "autocount_assembly_path",
            "autocount_server_name",
            "autocount_database_name",
            "autocount_user_id",
        ):
            self.assertIn(field, example)
        for field in ("autocount_server_name", "autocount_database_name", "autocount_user_id"):
            self.assertIsNone(example[field])

    def test_worktree_change_allowlist_is_narrow(self) -> None:
        status = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        for line in status:
            self.assertGreaterEqual(len(line), 4, line)
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            self.assertIn(path.replace("\\", "/"), ALLOWED_FILES, line)


if __name__ == "__main__":
    unittest.main()
