"""#155 DirectLocal real-session bridge path under real PowerShell (offline).

No AutoCount assembly, login or account book is reached: every case stops at
a guard before any session, or exercises the guard functions against a fake
DBSetting. The Unicode case runs the real bridge against the fake
MemberCommand host used by ``test_directlocal_bridge_ps``.
"""

from __future__ import annotations

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
sys.path.insert(0, str(ROOT / "tests"))

import xb_directlocal as dl  # noqa: E402
import test_directlocal_bridge_ps as bridge_fx  # noqa: E402

BRIDGE = ROOT / "scripts" / "ac2_directlocal_member_bridge.ps1"
GUARD = ROOT / "scripts" / "ac2_directlocal_book_guard.ps1"
ADAPTER = ROOT / "scripts" / "ac2_member_gateway_autocount_adapter.ps1"
CREDENTIAL_ENV = ("AC2_PROBE_USER_ID", "AC2_PROBE_PASSWORD", "XB_AC2_USER_ID", "XB_AC2_PASSWORD_ENV_VAR", "XB_AC2_SESSION_FACTORY")

GUARD_HARNESS = r"""
$ErrorActionPreference = "Stop"
. $args[0]
. $args[1]
function New-FakeDb($Name, $Values, [switch]$Throws) {
    $db = [pscustomobject]@{ DBName = $Name; Values = $Values; Throws = [bool]$Throws; Calls = 0 }
    $db | Add-Member -MemberType ScriptMethod -Name ExecuteScalar -Value {
        param([string]$Sql, [object[]]$Params)
        $this.Calls++
        if ($this.Throws) { throw "vendor failure" }
        foreach ($key in $this.Values.Keys) { if ($Sql -like "*$key*") { return $this.Values[$key] } }
        throw "unexpected sql"
    }
    return $db
}
function Try-Code([scriptblock]$Block) { try { & $Block; return "ok" } catch { return $_.Exception.Message } }
$good = @{ "DB_NAME()" = "AED_TESTINGAPI"; "MachineName" = "DESKTOP-4I042L6"; "UserScript" = 0; "MailQueue" = 0; "EInvoiceQueue" = 0 }
$out = [ordered]@{}
$out.identity_ok = Try-Code { Assert-XbDlBookIdentity -Session @{ DBSetting = (New-FakeDb "AED_TESTINGAPI" $good) } -Book "AED_TESTINGAPI" -ExpectedHost "DESKTOP-4I042L6" }
$prod = @{} + $good; $prod["DB_NAME()"] = "AED_XBOUNDARIES"
$out.identity_production = Try-Code { Assert-XbDlBookIdentity -Session @{ DBSetting = (New-FakeDb "AED_XBOUNDARIES" $prod) } -Book "AED_TESTINGAPI" -ExpectedHost "DESKTOP-4I042L6" }
$out.identity_ambiguous = Try-Code { Assert-XbDlBookIdentity -Session @{ DBSetting = (New-FakeDb "AED_STAGINGAPI" $good) } -Book "AED_TESTINGAPI" -ExpectedHost "DESKTOP-4I042L6" }
$wrong = @{} + $good; $wrong["DB_NAME()"] = "AED_STAGINGAPI"
$out.identity_wrong = Try-Code { Assert-XbDlBookIdentity -Session @{ DBSetting = (New-FakeDb "AED_STAGINGAPI" $wrong) } -Book "AED_TESTINGAPI" -ExpectedHost "DESKTOP-4I042L6" }
$out.identity_host = Try-Code { Assert-XbDlBookIdentity -Session @{ DBSetting = (New-FakeDb "AED_TESTINGAPI" $good) } -Book "AED_TESTINGAPI" -ExpectedHost "OTHER-HOST" }
$out.identity_unverifiable = Try-Code { Assert-XbDlBookIdentity -Session @{ DBSetting = (New-FakeDb "AED_TESTINGAPI" $good -Throws) } -Book "AED_TESTINGAPI" -ExpectedHost "DESKTOP-4I042L6" }
$out.quiet_ok = Try-Code { Assert-XbDlOutboundQuiet -Counts (Get-XbDlOutboundCounts -DBSetting (New-FakeDb "x" $good)) }
$busy = @{} + $good; $busy["MailQueue"] = 1
$out.quiet_mail = Try-Code { Assert-XbDlOutboundQuiet -Counts (Get-XbDlOutboundCounts -DBSetting (New-FakeDb "x" $busy)) }
$script = @{} + $good; $script["UserScript"] = 2
$out.quiet_script = Try-Code { Assert-XbDlOutboundQuiet -Counts (Get-XbDlOutboundCounts -DBSetting (New-FakeDb "x" $script)) }
$out.changed_none = Test-XbDlOutboundChanged -DBSetting (New-FakeDb "x" $good)
$out.changed_mail = Test-XbDlOutboundChanged -DBSetting (New-FakeDb "x" $busy)
$out.changed_unreadable = Test-XbDlOutboundChanged -DBSetting (New-FakeDb "x" $good -Throws)
$out.not_read_only = Try-Code { Get-XbDlScalar -DBSetting (New-FakeDb "x" $good) -Sql "DELETE FROM Member" }
$out.select_into = Try-Code { Get-XbDlScalar -DBSetting (New-FakeDb "x" $good) -Sql "SELECT * INTO X FROM Member" }
$out.book_prod = Try-Code { Assert-XbDlAuthorisedBook -Book "AED_XBOUNDARIES" }
$out.book_other = Try-Code { Assert-XbDlAuthorisedBook -Book "aed_testingapi" }
$out.book_test = Try-Code { Assert-XbDlAuthorisedBook -Book "AED_TESTINGAPI" }
$pos = @{ "FOR JSON" = '[{"location":"XB01","sync_name":"XBOUNDARIES_FE","last_finish_sync_local":"2026-10-15T23:19:47","last_alive_local":"2026-10-15T23:23:06"}]'; "GETDATE()" = "2026-10-15T23:24:00"; "ChangeCount" = 2826 }
$state = Get-XbDlPosSyncState -DBSetting (New-FakeDb "x" $pos)
$out.pos_outlets = @($state.outlets).Count
$out.pos_location = [string]@($state.outlets)[0].location
$out.pos_counter = $state.member_change_counter
[Console]::Out.WriteLine(($out | ConvertTo-Json -Compress))
"""


# Classification of a failed reviewed login. Fake AutoCount types are compiled
# into this throwaway process only; New-XbAutoCountSession and the assembly pin
# check are stubbed so New-XbDlRealSession reaches its login catch offline.
STAGE_HARNESS = r"""
$ErrorActionPreference = "Stop"
. $args[0]
. $args[1]
function Try-Code([scriptblock]$Block) { try { & $Block; return "ok" } catch { return $_.Exception.Message } }
$out = [ordered]@{}
$out.no_types = Get-XbDlSessionFailureStage -Book "AED_TESTINGAPI"
Add-Type -TypeDefinition @'
namespace AutoCount.Data {
    public class DBSetting {
        public static string Mode = "ok";
        public static string LastServer;
        public string DBName;
        public static DBSetting CreateAutoCountDefaultDBSetting(string server, string database) {
            LastServer = server;
            if (Mode == "throw") { throw new System.Exception("Login failed for user 'XB' on HQ\\SQL"); }
            DBSetting value = new DBSetting();
            value.DBName = Mode == "other" ? "AED_OTHER" : database;
            return value;
        }
        public object ExecuteScalar(string sql, object[] parameters) {
            if (Mode == "scalar_throw") { throw new System.Exception("vendor detail"); }
            return DBName;
        }
    }
}
namespace AutoCount.Authentication { public class UserSession { } }
'@
[Environment]::SetEnvironmentVariable("AC2_PROBE_SERVER_NAME", "probe-server", "Process")
foreach ($mode in @("ok", "throw", "scalar_throw", "other")) {
    [AutoCount.Data.DBSetting]::Mode = $mode
    $out["stage_$mode"] = Get-XbDlSessionFailureStage -Book "AED_TESTINGAPI"
}
$out.server_source = [AutoCount.Data.DBSetting]::LastServer
[AutoCount.Data.DBSetting]::Mode = "ok"
function Assert-XbDlQualifiedAssemblies { param($Root) }
$env:COMPUTERNAME = "DESKTOP-4I042L6"
function New-XbAutoCountSession { param([switch]$EnableProductionAdapter) throw "autocount_session_initialization_failed" }
$out.session_login = Try-Code { New-XbDlRealSession -Book "AED_TESTINGAPI" -AcRoot "unused" }
function New-XbAutoCountSession { param([switch]$EnableProductionAdapter) throw "autocount_password_missing" }
$out.session_password = Try-Code { New-XbDlRealSession -Book "AED_TESTINGAPI" -AcRoot "unused" }
$out.session_prod = Try-Code { New-XbDlRealSession -Book "AED_XBOUNDARIES" -AcRoot "unused" }
[Console]::Out.WriteLine(($out | ConvertTo-Json -Compress))
"""


# E5 root cause: the adapter's scriptblock AssemblyResolve handler re-enters on
# PowerShell's own missing resource satellite until the stack overflows. Same
# handler shape, no AutoCount: "guard" must survive, "none" is the control.
RECURSION_HARNESS = r"""
$ErrorActionPreference = "Stop"
. $args[0]
. $args[1]
if ($args[2] -eq "guard") { Install-XbDlResolveGuard; Install-XbDlResolveGuard }
$root = $args[3]
[System.AppDomain]::CurrentDomain.add_AssemblyResolve([System.ResolveEventHandler] {
    param($sender, $eventArgs)
    $assemblyName = [System.Reflection.AssemblyName]::new($eventArgs.Name)
    $candidate = Join-Path $root ($assemblyName.Name + ".dll")
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return [System.Reflection.Assembly]::LoadFrom($candidate) }
    return $null
})
# A vendor login may also change the thread UI culture.
[System.Threading.Thread]::CurrentThread.CurrentUICulture = [System.Globalization.CultureInfo]::GetCultureInfo("en-SG")
try { Get-Item -LiteralPath (Join-Path $root "definitely-missing") } catch { }
[Console]::Out.WriteLine("survived")
"""


def powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class RealBridgeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-directlocal-realbridge-"))
        self.claims = self.temp / "claims"
        self.claims.mkdir()
        self.empty_ac_root = self.temp / "no-autocount"
        self.empty_ac_root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def env(self, **overrides) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_ENV}
        env.update(overrides)
        return env

    def run_direct(self, operation: str, stdin: dict, *extra: str, env: dict):
        command = [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(BRIDGE), "-Operation", operation, "-EnableProductionAdapter", *extra]
        proc = subprocess.run(command, input=(json.dumps(stdin) + "\n").encode("ascii"), capture_output=True, timeout=120, check=False, env=env)
        lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        return proc.returncode, (json.loads(lines[-1]) if lines else None)

    def write_args(self, fence: str) -> list[str]:
        return ["-FenceId", fence, "-ClaimDirectory", str(self.claims), "-WriteDeadlineUtc", bridge_fx.DirectLocalBridgeTests.deadline(None, 60)]

    def write_request(self, fence: str) -> dict:
        text = dl.canonical_payload(bridge_fx.PAYLOAD)
        salt = "b2" * 16
        return {"fence_id": fence, "member_no": "M000201", "salt": salt, "payload_canonical": text, "payload_digest": dl.payload_digest(salt, text)}

    def test_real_path_refuses_before_any_session(self):
        fence = "fence-" + "8" * 32
        host_ok = self.env(COMPUTERNAME="DESKTOP-4I042L6")
        cases = [
            ("production_book_not_authorised", ["-TargetBook", "AED_XBOUNDARIES"], host_ok),
            ("target_book_not_authorised", ["-TargetBook", "AED_STAGINGAPI"], host_ok),
            ("host_not_authorised", ["-TargetBook", "AED_TESTINGAPI", "-AcRoot", str(self.empty_ac_root)], self.env(COMPUTERNAME="SOME-OTHER-HOST")),
            ("session_factory_env_refused", ["-TargetBook", "AED_TESTINGAPI", "-AcRoot", str(self.empty_ac_root)], self.env(COMPUTERNAME="DESKTOP-4I042L6", XB_AC2_SESSION_FACTORY="New-Anything")),
            ("autocount_build_not_qualified", ["-TargetBook", "AED_TESTINGAPI", "-AcRoot", str(self.empty_ac_root)], host_ok),
        ]
        for code, extra, env in cases:
            for operation, request, more in (("probe", {"member_no": "M000201"}, []), ("book-check", {}, []), ("write-once", self.write_request(fence), self.write_args(fence))):
                with self.subTest(code=code, operation=operation):
                    self.assertEqual(self.run_direct(operation, request, *more, *extra, env=env), (1, {"error": code}))
        self.assertEqual(list(self.claims.iterdir()), [])  # no claim: the parent proves NOT_WRITTEN

    def test_synthetic_session_cannot_be_combined_with_a_book_or_real_only_operations(self):
        tests = bridge_fx.DirectLocalBridgeTests("test_read_operations")
        tests.setUp()
        try:
            booked = tests.temp / "fake_host_booked.ps1"
            booked.write_text(bridge_fx.FAKE_HOST.replace("-SessionFactory $sessionFactory", "-SessionFactory $sessionFactory -TargetBook AED_TESTINGAPI"), encoding="utf-8")
            command = [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(booked), "-Bridge", str(BRIDGE), "-StateFile", str(tests.state)]
            self.assertEqual(tests.run_bridge("probe", {"member_no": "M000201"}, command=command)[:2], (1, {"error": "synthetic_session_with_target_book"}))
            self.assertEqual(tests.run_bridge("book-check", {})[:2], (1, {"error": "book_check_requires_real_session"}))
            self.assertEqual(tests.run_bridge("pos-sync-status", {})[:2], (1, {"error": "pos_sync_status_requires_real_session"}))
        finally:
            tests.tearDown()

    def test_guard_functions(self):
        harness = self.temp / "guard_harness.ps1"
        harness.write_text(GUARD_HARNESS, encoding="utf-8")
        proc = subprocess.run([powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness), str(ADAPTER), str(GUARD)], capture_output=True, timeout=120, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[-2000:])
        out = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertEqual(out, {
            "identity_ok": "ok", "identity_production": "production_book_refused", "identity_ambiguous": "book_identity_ambiguous",
            "identity_wrong": "book_identity_wrong", "identity_host": "book_host_wrong", "identity_unverifiable": "book_identity_unverifiable",
            "quiet_ok": "ok", "quiet_mail": "outbound_queue_not_empty", "quiet_script": "user_script_present",
            "changed_none": False, "changed_mail": True, "changed_unreadable": True,
            "not_read_only": "vendor_query_not_read_only", "select_into": "vendor_query_not_read_only",
            "book_prod": "production_book_not_authorised", "book_other": "target_book_not_authorised", "book_test": "ok",
            "pos_outlets": 1, "pos_location": "XB01", "pos_counter": 2826,
        })

    def test_resolve_guard_prevents_scriptblock_handler_recursion(self):
        harness = self.temp / "recursion_harness.ps1"
        harness.write_text(RECURSION_HARNESS, encoding="utf-8")

        def run(mode: str):
            return subprocess.run([powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness), str(ADAPTER), str(GUARD), mode, str(self.temp)], capture_output=True, timeout=120, check=False)

        guarded = run("guard")
        self.assertEqual((guarded.returncode, guarded.stdout.decode("utf-8", "replace").strip()), (0, "survived"), guarded.stderr.decode("utf-8", "replace")[-2000:])
        control = run("none")  # proves the harness reproduces the E5 failure
        self.assertNotEqual(control.returncode, 0)
        self.assertNotIn(b"survived", control.stdout)

    def test_failed_login_stage_is_classified_without_vendor_text(self):
        harness = self.temp / "stage_harness.ps1"
        harness.write_text(STAGE_HARNESS, encoding="utf-8")
        env = self.env()
        env.pop("XB_AC2_SERVER_NAME", None)
        proc = subprocess.run([powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness), str(ADAPTER), str(GUARD)], capture_output=True, timeout=120, check=False, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[-2000:])
        out = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertEqual(out, {
            "no_types": "autocount_assembly_load_failed",
            "stage_ok": "autocount_login_rejected", "stage_throw": "autocount_sql_connection_failed",
            "stage_scalar_throw": "autocount_sql_connection_failed", "stage_other": "autocount_sql_book_mismatch",
            "server_source": "probe-server",
            "session_login": "autocount_login_rejected", "session_password": "autocount_password_missing",
            "session_prod": "production_book_not_authorised",
        })
        for key, value in out.items():
            if key != "server_source":
                self.assertEqual(dl.bridge_failure_code(json.dumps({"error": value}).encode(), 1), f"bridge_read_failed:{value}")
            self.assertNotIn("Login failed", str(value))


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class UnicodeRoundTripTests(unittest.TestCase):
    def test_cjk_and_astral_name_round_trips_exactly(self):
        tests = bridge_fx.DirectLocalBridgeTests("test_read_operations")
        tests.setUp()
        try:
            payload = {**bridge_fx.PAYLOAD, "name": "沈小明 Synthetic \U0001F600 Member"}
            self.assertIsNone(dl.autocount_text_issue(payload))
            text = dl.canonical_payload(payload)
            self.assertTrue(text.isascii())
            fence = "fence-" + "9" * 32
            salt = "c3" * 16
            request = {"fence_id": fence, "member_no": "M000201", "salt": salt, "payload_canonical": text, "payload_digest": dl.payload_digest(salt, text)}
            code, out, _ = tests.run_bridge("write-once", request, *tests.write_args(fence))
            self.assertEqual((code, out), (0, {"save_invocation_count": 1, "readback_found": True, "readback_match": True, "outbound_queue_changed": False}))
            self.assertEqual(tests.fake()["members"][0]["Name"], payload["name"])
            self.assertEqual(tests.run_bridge("reconcile", {"member_no": "M000201", "payload_canonical": text})[:2], (0, {"found": True, "match": True}))
        finally:
            tests.tearDown()


if __name__ == "__main__":
    unittest.main()
