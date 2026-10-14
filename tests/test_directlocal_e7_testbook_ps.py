"""#155 E7 test-book create path under real PowerShell (fakes only).

The unchanged adapter runs against a fake DBSetting and MemberCommand reached
only through the test-only -SessionFactory seam. No AutoCount assembly, login,
account book or network is touched. Also covers the pure isolation classifier.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
E7 = ROOT / "scripts" / "ac2_directlocal_e7_testbook_create.ps1"
CHECKER = ROOT / "scripts" / "ac2_directlocal_e7_isolation_check.py"
RUN = "2026-10-14-xb-155-directlocal-aed-testingapi-single-member-uat-001"
LOCK = "DL-XB-155-DIRECTLOCAL-AED_TESTINGAPI-ONE-SAVE-UAT-001"

HARNESS = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$E7,
    [Parameter(Mandatory)][string]$StateDir,
    [Parameter(Mandatory)][string]$Evidence,
    [Parameter(Mandatory)][string]$FakeFile,
    [switch]$Write,
    [int]$DelayMs = 0,
    [int]$FenceTtl = 120
)
$ErrorActionPreference = "Stop"
$fields = @("MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress", "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual")
function Read-Fake { return ([IO.File]::ReadAllText($FakeFile) | ConvertFrom-Json) }
function Save-Fake($S) { [IO.File]::WriteAllText($FakeFile, ($S | ConvertTo-Json -Depth 6 -Compress)) }
function New-FakeTable { $t = New-Object System.Data.DataTable; foreach ($f in $fields) { [void]$t.Columns.Add($f, [object]) }; return ,$t }
function New-FakeEntity($Record) {
    $t = New-FakeTable; $r = $t.NewRow()
    foreach ($f in $fields) { $r[$f] = [DBNull]::Value; if ($null -ne $Record -and $null -ne $Record.PSObject.Properties[$f] -and $null -ne $Record.$f) { $r[$f] = $Record.$f } }
    [void]$t.Rows.Add($r); return [pscustomobject]@{ Row = $r }
}
$command = [pscustomobject]@{ Name = "fake" }
$command | Add-Member ScriptMethod NewMember { param([bool]$U) return (New-FakeEntity $null) }
$command | Add-Member ScriptMethod SaveMember {
    param($Entity)
    $s = Read-Fake; $s.save_calls = [int]$s.save_calls + 1; Save-Fake $s
    if ($s.save_throws) { throw "vendor failure detail" }
    $rec = [ordered]@{}
    foreach ($f in $fields) { $v = $Entity.Row[$f]; $rec[$f] = if ($v -is [DBNull]) { $null } elseif ($v -is [datetime]) { $v.ToString("yyyy-MM-dd") } else { [string]$v } }
    $s = Read-Fake; $s.members = @($s.members) + @([pscustomobject]$rec); Save-Fake $s
}
$command | Add-Member ScriptMethod GetMember {
    param([string]$No)
    $s = Read-Fake; $m = @(@($s.members) | Where-Object { $null -ne $_ -and $_.MemberNo -ceq $No })
    if ($m.Count -eq 0) { return $null }; return (New-FakeEntity $m[0])
}
$command | Add-Member ScriptMethod GetNextMemberNo { $s = Read-Fake; $v = @($s.next)[[int]$s.next_index]; $s.next_index = [int]$s.next_index + 1; Save-Fake $s; return $v }
$command | Add-Member ScriptMethod LoadBrowseTable {
    $s = Read-Fake; $t = New-FakeTable
    foreach ($m in @($s.members)) { if ($null -eq $m) { continue }; $r = $t.NewRow(); foreach ($f in $fields) { $r[$f] = [DBNull]::Value }; $r["Name"] = $m.Name; [void]$t.Rows.Add($r) }
    return ,$t
}
$fake = Read-Fake
$db = [pscustomobject]@{ DBName = $fake.declared_db; ServerName = "(local)\A2006" }
if ($fake.execute_scalar) {
    $db | Add-Member ScriptMethod ExecuteScalar {
        param($Sql, $Params)
        $s = Read-Fake
        if ($Sql -like "*DB_NAME()*") { return $s.vendor_db }
        if ($Sql -like "*MachineName*") { return $s.vendor_machine }
        if ($Sql -like "*ChangeCount*") { return 100 + @($s.members).Count }
        if ($Sql -like "*ChangeLog*") { return 5000 + @($s.members).Count }
        if ($Sql -like "*MailQueue*") { return 0 }
        if ($Sql -like "*FROM Member*") { return @($s.members).Count }
        if ($Sql -like "*SyncRegistration*") { return "2026-09-24T17:40:01" }
        throw "unexpected fake query"
    }
}
$sessionFactory = {
    $s = Read-Fake; $s.session_calls = [int]$s.session_calls + 1; Save-Fake $s
    [pscustomobject]@{ UserSession = "fake"; DBSetting = $db; MemberCommandFactory = { param($Session) return $command }.GetNewClosure() }
}.GetNewClosure()
$switches = @{}
if ($Write) { $switches = @{ EnableTestBookWrite = $true; ConfirmTargetAedTestingApi = $true; ConfirmOwnerRiskAccepted = $true } }
& $E7 -StateDir $StateDir -IsolationEvidencePath $Evidence -SessionFactory $sessionFactory -TestExpectedHost $env:COMPUTERNAME -TestDelayBeforeClaimMs $DelayMs -FenceTtlSeconds $FenceTtl @switches
exit $LASTEXITCODE
"""


def powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def load_checker():
    spec = importlib.util.spec_from_file_location("e7_isolation", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class E7TestBookCreateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="xb-e7-"))
        self.state = self.root / "state"
        self.state.mkdir()
        (self.root / "XB_E7_UAT_OWNER.json").write_text(json.dumps({"run": RUN, "lock": LOCK, "target_book": "AED_TESTINGAPI"}), encoding="utf-8")
        self.harness = self.root / "harness.ps1"
        self.harness.write_text(HARNESS, encoding="utf-8")
        self.fake_file = self.root / "fake.json"
        self.evidence = self.root / "isolation.json"
        self.set_evidence()
        self.set_fake()
        import os

        self.host = os.environ.get("COMPUTERNAME", "")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def set_fake(self, **overrides):
        import os

        fake = {"members": [], "next": ["M000901", "M000902", "M000903", "M000904"], "next_index": 0, "save_calls": 0, "save_throws": False,
                "session_calls": 0, "declared_db": "AED_TESTINGAPI", "vendor_db": "AED_TESTINGAPI", "vendor_machine": os.environ.get("COMPUTERNAME", ""), "execute_scalar": True}
        fake.update(overrides)
        self.fake_file.write_text(json.dumps(fake), encoding="utf-8")

    def set_evidence(self, **overrides):
        evidence = {"book": "AED_TESTINGAPI", "verdict": "ISOLATED", "run_at": datetime.now(timezone.utc).isoformat(), "touching_processes": [{"process": "Accounting.exe"}], "blockers": []}
        evidence.update(overrides)
        self.evidence.write_text(json.dumps(evidence), encoding="utf-8")

    def fake(self) -> dict:
        return json.loads(self.fake_file.read_text(encoding="utf-8-sig"))

    def run_e7(self, write=False, delay_ms=0, fence_ttl=120):
        args = [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.harness),
                "-E7", str(E7), "-StateDir", str(self.state), "-Evidence", str(self.evidence), "-FakeFile", str(self.fake_file),
                "-DelayMs", str(delay_ms), "-FenceTtl", str(fence_ttl)] + (["-Write"] if write else [])
        proc = subprocess.run(args, capture_output=True, timeout=180, check=False)
        lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        return proc.returncode, json.loads(lines[-1]), proc

    def files(self) -> list[str]:
        return sorted(p.name for p in self.state.iterdir() if p.name != "e7.lock")

    def assert_untouched(self, *, sessions: int | None = None):
        fake = self.fake()
        self.assertEqual((fake["save_calls"], fake["next_index"]), (0, 0))
        self.assertEqual(self.files(), [])
        if sessions is not None:
            self.assertEqual(fake["session_calls"], sessions)

    def test_dry_run_proves_identity_without_numbering_or_write(self):
        code, out, _ = self.run_e7()
        self.assertEqual((code, out["status"], out["mode"]), (0, "DRY_RUN_PASSED", "dry_run"))
        self.assertEqual(out["book_identity"]["vendor_db"], "AED_TESTINGAPI")
        self.assert_untouched()

    def test_single_verified_write_then_replay_refused(self):
        code, out, proc = self.run_e7(write=True)
        self.assertEqual((code, out["status"], out["get_next_member_no_count"], out["save_member_invocations"]), (0, "CREATED_VERIFIED", 1, 1))
        self.assertEqual((out["member_no"], out["readback_found"], out["readback_match"]), ("M000901", True, True))
        member = self.fake()["members"][0]
        self.assertTrue(member["Name"].startswith("XB DIRECTLOCAL E7 SYNTHETIC "))
        self.assertEqual((member["MemberType"], member["MobilePhone"], member["EmailAddress"], member["DOB"], member["IsActive"], member["Individual"], member["OpeningPoints"]),
                         ("Default", None, None, None, "T", "T", "0"))
        self.assertEqual(datetime.strptime(member["ExpiryDate"], "%Y-%m-%d") - datetime.strptime(member["RegisterDate"], "%Y-%m-%d") >= timedelta(days=729), True)
        self.assertEqual(out["audit_after"]["member_change_counter"] - out["audit_before"]["member_change_counter"], 1)
        self.assertEqual(self.files(), ["e7.claim", "e7.fence.json", "e7.intent.json", "e7.outcome.json"])
        self.assertNotIn(b"vendor failure", proc.stdout)
        code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["status"], out["code"]), (1, "REFUSED", "e7_already_executed"))
        self.assertEqual(self.fake()["save_calls"], 1)

    def test_occupied_candidates_use_at_most_three_numbering_calls(self):
        occupied = [{"MemberNo": no, "Name": "Existing"} for no in ("M000901", "M000902")]
        self.set_fake(members=occupied)
        code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["member_no"], out["get_next_member_no_count"]), (0, "M000903", 3))
        self.set_fake(members=occupied + [{"MemberNo": "M000903", "Name": "Existing"}])
        shutil.rmtree(self.state)
        self.state.mkdir()
        code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["code"], out["get_next_member_no_count"]), (1, "member_no_candidates_exhausted", 3))
        self.assertEqual((self.fake()["save_calls"], self.fake()["next_index"]), (0, 3))
        self.assertIn("e7.outcome.json", self.files())
        self.assertEqual(self.run_e7(write=True)[1]["code"], "e7_already_executed")
        self.assertEqual(self.fake()["next_index"], 3)

    def test_generator_repeating_a_candidate_stops(self):
        self.set_fake(members=[{"MemberNo": "M000901", "Name": "Existing"}], next=["M000901", "M000901", "M000903"])
        code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["code"]), (1, "member_no_generator_not_advancing"))
        self.assertEqual(self.fake()["save_calls"], 0)

    def test_book_identity_refusals(self):
        cases = {
            "book_identity_wrong": {"declared_db": "AED_STAGINGAPI", "vendor_db": "AED_STAGINGAPI"},
            "production_book_refused": {"declared_db": "AED_XBOUNDARIES", "vendor_db": "AED_XBOUNDARIES"},
            "production_book_refused ": {"declared_db": "AED_TESTINGAPI", "vendor_db": "AED_XBOUNDARIES"},
            "book_identity_ambiguous": {"declared_db": "AED_TESTINGAPI", "vendor_db": "AED_STAGINGAPI"},
            "book_identity_missing": {"declared_db": "", "vendor_db": ""},
            "book_identity_unverifiable": {"execute_scalar": False},
            "book_host_wrong": {"vendor_machine": "SOME-OTHER-HOST"},
        }
        for code_name, fake in cases.items():
            with self.subTest(code=code_name.strip()):
                self.set_fake(**fake)
                for write in (False, True):
                    code, out, _ = self.run_e7(write=write)
                    self.assertEqual((code, out["status"], out["code"]), (1, "REFUSED", code_name.strip()))
                    self.assert_untouched()

    def test_isolation_evidence_required_before_any_login(self):
        cases = {
            "isolation_not_proven": {"verdict": "NOT_PROVEN"},
            "isolation_not_proven ": {"book": "AED_XBOUNDARIES"},
            "isolation_evidence_stale": {"run_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()},
        }
        for code_name, evidence in cases.items():
            with self.subTest(code=code_name.strip()):
                self.set_evidence(**evidence)
                code, out, _ = self.run_e7(write=True)
                self.assertEqual((code, out["code"]), (1, code_name.strip()))
                self.assert_untouched(sessions=0)
        self.evidence.unlink()
        self.assertEqual(self.run_e7(write=True)[1]["code"], "isolation_evidence_missing")
        self.assert_untouched(sessions=0)

    def test_existing_synthetic_member_blocks_duplicate(self):
        self.set_fake(members=[{"MemberNo": "Z1", "Name": "XB DIRECTLOCAL E7 SYNTHETIC 20261014"}])
        code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["code"]), (1, "synthetic_member_already_present"))
        self.assertEqual((self.fake()["save_calls"], self.fake()["next_index"]), (0, 0))
        self.assertEqual(self.files(), [])

    def test_concurrent_run_refused_by_lock(self):
        with open(self.state / "e7.lock", "a+b"):
            code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["code"]), (1, "e7_lock_held"))
        self.assertEqual(self.fake()["save_calls"], 0)

    def test_expired_fence_never_claims_or_saves(self):
        code, out, _ = self.run_e7(write=True, delay_ms=2500, fence_ttl=1)
        self.assertEqual((code, out["code"]), (1, "e7_fence_expired"))
        self.assertEqual(self.fake()["save_calls"], 0)
        self.assertNotIn("e7.claim", self.files())
        self.assertEqual(self.run_e7(write=True)[1]["code"], "e7_already_executed")

    def test_unknown_save_outcome_holds_without_retry(self):
        self.set_fake(save_throws=True)
        code, out, proc = self.run_e7(write=True)
        self.assertEqual((code, out["status"], out["code"], out["save_member_invocations"]), (3, "WRITE_OUTCOME_UNKNOWN_HOLD", "e7_failed", 1))
        self.assertEqual(self.fake()["save_calls"], 1)
        self.assertEqual(out["reconcile"]["found"], False)
        self.assertNotIn(b"vendor failure", proc.stdout)
        self.assertEqual(self.run_e7(write=True)[1]["code"], "e7_already_executed")
        self.assertEqual(self.fake()["save_calls"], 1)

    def test_direct_file_invocation_cannot_use_test_seams_or_other_hosts(self):
        args = [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(E7),
                "-StateDir", str(self.state), "-IsolationEvidencePath", str(self.root / "absent.json"), "-EnableTestBookWrite", "-ConfirmTargetAedTestingApi", "-ConfirmOwnerRiskAccepted"]
        out = json.loads(subprocess.run(args + ["-TestExpectedHost", "ANY"], capture_output=True, timeout=120).stdout.decode().strip().splitlines()[-1])
        self.assertEqual(out["code"], "test_seam_without_test_session")
        out = json.loads(subprocess.run(args, capture_output=True, timeout=120).stdout.decode().strip().splitlines()[-1])
        import os

        expected = "isolation_evidence_missing" if os.environ.get("COMPUTERNAME", "").upper() == "DESKTOP-4I042L6" else "host_not_authorised"
        self.assertEqual(out["code"], expected)
        self.assert_untouched()


class E7StaticTests(unittest.TestCase):
    def test_script_has_no_outbound_channels_and_pins_the_book(self):
        text = E7.read_text(encoding="utf-8")
        for forbidden in ("Invoke-WebRequest", "Invoke-RestMethod", "Net.WebClient", "HttpClient", "Send-MailMessage", "SmtpClient", "Start-Process", "AC2_PROBE_DATABASE_NAME"):
            self.assertNotIn(forbidden, text)
        self.assertIn('$script:E7Book = "AED_TESTINGAPI"', text)
        self.assertIn('"XB_AC2_DATABASE_NAME", $script:E7Book', text)
        self.assertEqual(text.count("= New-XbAutoCountMember "), 1)
        self.assertNotIn("SaveMember(", text)

    def test_directlocal_bridge_still_hard_disarmed(self):
        bridge = (ROOT / "scripts" / "ac2_directlocal_member_bridge.ps1").read_text(encoding="utf-8")
        self.assertIn('if ($null -eq $SessionFactory) { throw "autocount_real_bridge_disabled" }', bridge)
        self.assertNotIn("e7", bridge.lower())


class IsolationClassifierTests(unittest.TestCase):
    BASE = {
        "touching_processes": [{"process": "Accounting.exe", "client": "<local machine>"}, {"process": "AutoCountServerService.exe", "client": "<local machine>"}],
        "sql_agent_jobs_for_book": 0,
        "member_triggers": [{"external_markers": [], "writes_tables": ["ChangeLog"]}, {"external_markers": [], "writes_tables": ["ChangeCount"]}],
        "instance": {"linked_servers": 0, "xp_cmdshell": 0, "server_triggers": 0, "book_broker_enabled": 0, "database_mail_xps": 0},
        "book_user_assemblies": 0, "book_database_triggers": 0, "scheduled_tasks_mentioning_book": 0, "services_mentioning_book": [],
        "pos_registrations_active_after_clone": 0, "plugins_allowing_sync": 0, "mail_queue_rows": 0, "do_email_smtp_configured": 0, "onerewards_members": 0,
    }

    def test_base_is_isolated_and_each_channel_blocks(self):
        checker = load_checker()
        self.assertEqual(checker.classify(self.BASE), [])
        mutations = {
            "unexpected_process_touches_book": lambda e: e["touching_processes"].append({"process": "TechDevs.Unicart+.Service.exe", "client": "<local machine>"}),
            "unexpected_process_touches_book ": lambda e: e["touching_processes"].__setitem__(0, {"process": "Accounting.exe", "client": "10.0.0.5"}),
            "sql_agent_jobs_reference_book": lambda e: e.__setitem__("sql_agent_jobs_for_book", 1),
            "member_trigger_not_internal_only": lambda e: e["member_triggers"].append({"external_markers": ["sp_send_dbmail"], "writes_tables": []}),
            "instance_external_capability_present": lambda e: e["instance"].__setitem__("linked_servers", 1),
            "book_external_capability_present": lambda e: e.__setitem__("book_user_assemblies", 1),
            "host_job_references_book": lambda e: e.__setitem__("services_mentioning_book", ["TD Unicart+ Service - AED_TESTINGAPI"]),
            "pos_sync_active_on_book": lambda e: e.__setitem__("pos_registrations_active_after_clone", 1),
            "plugin_sync_enabled": lambda e: e.__setitem__("plugins_allowing_sync", 1),
            "mail_or_rewards_channel_active": lambda e: e.__setitem__("do_email_smtp_configured", 1),
        }
        for code, mutate in mutations.items():
            with self.subTest(code=code.strip()):
                evidence = copy.deepcopy(self.BASE)
                mutate(evidence)
                self.assertIn(code.strip(), checker.classify(evidence))


if __name__ == "__main__":
    unittest.main()
