"""#155 E7 test-book create path under real PowerShell (fakes only).

The unchanged adapter runs against a fake DBSetting and MemberCommand reached
only through the test-only -SessionFactory seam. No AutoCount assembly, login,
account book or network is touched. Also covers the fail-closed isolation
collector, the E7 script's independent evidence re-validation, the private
workspace ACL guard and the Owner console procedure's refusals.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
E7 = ROOT / "scripts" / "ac2_directlocal_e7_testbook_create.ps1"
OWNER = ROOT / "scripts" / "ac2_directlocal_e7_owner_console.ps1"
CHECKER = ROOT / "scripts" / "ac2_directlocal_e7_isolation_check.py"
RUN = "2026-10-14-xb-155-directlocal-aed-testingapi-single-member-uat-001"
LOCK = "DL-XB-155-DIRECTLOCAL-AED_TESTINGAPI-ONE-SAVE-UAT-001"

HARNESS = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$E7,
    [Parameter(Mandatory)][string]$StateDir,
    [Parameter(Mandatory)][string]$Evidence,
    [Parameter(Mandatory)][string]$EvidenceSha,
    [Parameter(Mandatory)][string]$FakeFile,
    [Parameter(Mandatory)][string]$AcRoot,
    [Parameter(Mandatory)][string]$PinsJson,
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
    $s = Read-Fake; $s.get_member_calls = [int]$s.get_member_calls + 1; Save-Fake $s
    $m = @(@($s.members) | Where-Object { $null -ne $_ -and $_.MemberNo -ceq $No })
    if ($m.Count -eq 0) { return $null }; return (New-FakeEntity $m[0])
}
$command | Add-Member ScriptMethod GetNextMemberNo { $s = Read-Fake; $v = @($s.next)[[int]$s.next_index]; $s.next_index = [int]$s.next_index + 1; Save-Fake $s; return $v }
$command | Add-Member ScriptMethod LoadBrowseTable { $s = Read-Fake; $s.browse_calls = [int]$s.browse_calls + 1; Save-Fake $s; throw "browse table must not be loaded" }
$fake = Read-Fake
$db = [pscustomobject]@{ DBName = $fake.declared_db; ServerName = "(local)\A2006" }
if ($fake.execute_scalar) {
    $db | Add-Member ScriptMethod ExecuteScalar {
        param($Sql, $Params)
        $s = Read-Fake
        $members = @(@($s.members) | Where-Object { $null -ne $_ })
        if ($Sql -like "*DB_NAME()*") { return $s.vendor_db }
        if ($Sql -like "*MachineName*") { return $s.vendor_machine }
        if ($Sql -like "*ChangeCount*") { return 100 + $members.Count }
        if ($Sql -like "*ChangeLog*") { return 5000 + $members.Count }
        if ($Sql -like "*EventLog*") { return 40000 + $members.Count }
        if ($Sql -like "*FROM Activity*") { return 0 }
        if ($Sql -like "*MailQueue*") { if ([int]$s.save_calls -gt 0) { return $s.mail_queue_after_save }; return $s.mail_queue }
        if ($Sql -like "*EInvoiceQueue*") { return $s.einvoice_queue }
        if ($Sql -like "*UserScript*") { return $s.user_scripts }
        if ($Sql -like "*WHERE MemberNo = ?*") { $s.member_probe_queries = [int]$s.member_probe_queries + 1; Save-Fake $s; return @($members | Where-Object { $_.MemberNo -ceq [string]$Params[0] }).Count }
        if ($Sql -like "*WHERE Name LIKE ?*") { $p = ([string]$Params[0]).TrimEnd("%"); return @($members | Where-Object { ([string]$_.Name).StartsWith($p, [StringComparison]::OrdinalIgnoreCase) }).Count }
        if ($Sql -like "*FROM Member*") { return $members.Count }
        if ($Sql -like "*SyncRegistration*") { return "2026-09-24T17:40:01" }
        throw "unexpected fake query"
    }
}
$sessionFactory = {
    $s = Read-Fake; $s.session_calls = [int]$s.session_calls + 1; Save-Fake $s
    [pscustomobject]@{ UserSession = "fake"; DBSetting = $db; MemberCommandFactory = { param($Session) return $command }.GetNewClosure() }
}.GetNewClosure()
$pins = @{}
($PinsJson | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $pins[$_.Name] = [string]$_.Value }
$switches = @{}
if ($Write) { $switches = @{ EnableTestBookWrite = $true; ConfirmTargetAedTestingApi = $true; ConfirmOwnerRiskAccepted = $true } }
& $E7 -StateDir $StateDir -IsolationEvidencePath $Evidence -IsolationEvidenceSha256 $EvidenceSha -AcRoot $AcRoot -TestAssemblyPins $pins -SessionFactory $sessionFactory -TestExpectedHost $env:COMPUTERNAME -TestDelayBeforeClaimMs $DelayMs -FenceTtlSeconds $FenceTtl @switches
exit $LASTEXITCODE
"""


def powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def load_checker():
    spec = importlib.util.spec_from_file_location("e7_isolation", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def full_evidence(host_name: str, pins: dict[str, str], /, **overrides) -> dict:
    """A complete, ISOLATED v2 evidence document; overrides replace top-level keys."""

    now = datetime.now(timezone.utc).isoformat()
    evidence = {
        "schema": "xb155-e7-isolation-v2", "book": "AED_TESTINGAPI", "host": host_name, "run_at": now, "completed_at": now,
        "collector_sha256": "0" * 64, "elevated": True, "samples": 6, "sample_failures": 0,
        "collection": {name: {"ok": True} for name in ("host", "processes", "sessions", "sql_capabilities", "book_channels", "scheduled_tasks", "services", "assemblies")},
        "touching_processes": [{"process": "Accounting.exe", "client": "<local machine>", "observations": 6},
                               {"process": "AutoCountServerService.exe", "client": "<local machine>", "observations": 6}],
        "acserver_book_tables": ["mailqueue", "sys"],
        "member_triggers": [{"name": "MemberChangeLog", "external_markers": [], "writes_tables": ["ChangeLog"]},
                            {"name": "UpdateMemberChangeCount", "external_markers": [], "writes_tables": ["ChangeCount"]}],
        "instance": {"linked_servers": 0, "xp_cmdshell": 0, "server_triggers": 0, "book_broker_enabled": 0, "database_mail_xps": 0},
        "sql_agent_jobs_for_book": 0, "book_user_assemblies": 0, "book_database_triggers": 0, "scheduled_tasks_mentioning_book": 0,
        "services_mentioning_book": [], "pos_registrations_active_after_clone": 0, "plugins_allowing_sync": 0, "mail_queue_rows": 0,
        "einvoice_queue_rows": 0, "user_script_rows": 0, "do_email_smtp_configured": 0, "onerewards_members": 0,
        "assemblies": dict(pins), "blockers": [], "verdict": "ISOLATED",
    }
    evidence.update(overrides)
    return evidence


def make_private(path: Path) -> None:
    """Protect a test directory like the real workspace (no inheritance, owner/SYSTEM/Admins)."""

    sid = subprocess.run(["powershell", "-NoProfile", "-Command", "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
                   capture_output=True, check=True)


@unittest.skipUnless(powershell() and os.name == "nt", "Windows PowerShell required")
class E7TestBookCreateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="xb-e7-"))
        make_private(self.root)
        self.state = self.root / "state"
        self.state.mkdir()
        self.evidence_dir = self.root / "evidence"
        self.evidence_dir.mkdir()
        self.acroot = self.root / "acroot"
        self.acroot.mkdir()
        self.pins = {}
        for name in ("AutoCount.dll", "AutoCount.Invoicing.dll"):
            (self.acroot / name).write_bytes(f"fake {name}".encode())
            self.pins[name] = hashlib.sha256(f"fake {name}".encode()).hexdigest()
        (self.root / "XB_E7_UAT_OWNER.json").write_text(json.dumps({"run": RUN, "lock": LOCK, "target_book": "AED_TESTINGAPI"}), encoding="utf-8")
        self.harness = self.root / "harness.ps1"
        self.harness.write_text(HARNESS, encoding="utf-8")
        self.fake_file = self.root / "fake.json"
        self.host = os.environ.get("COMPUTERNAME", "")
        self.evidence = self.evidence_dir / "isolation_test.json"
        self.set_evidence()
        self.set_fake()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def set_fake(self, **overrides):
        fake = {"members": [], "next": ["M000901", "M000902", "M000903", "M000904"], "next_index": 0, "save_calls": 0, "save_throws": False,
                "session_calls": 0, "get_member_calls": 0, "browse_calls": 0, "member_probe_queries": 0, "declared_db": "AED_TESTINGAPI",
                "vendor_db": "AED_TESTINGAPI", "vendor_machine": self.host, "execute_scalar": True,
                "mail_queue": 0, "mail_queue_after_save": 0, "einvoice_queue": 0, "user_scripts": 0}
        fake.update(overrides)
        self.fake_file.write_text(json.dumps(fake), encoding="utf-8")

    def set_evidence(self, raw: bytes | None = None, **overrides):
        data = raw if raw is not None else json.dumps(full_evidence(self.host, self.pins, **overrides)).encode("utf-8")
        if self.evidence.exists():
            self.evidence.unlink()
        self.evidence.write_bytes(data)
        self.evidence_sha = hashlib.sha256(data).hexdigest()

    def fake(self) -> dict:
        return json.loads(self.fake_file.read_text(encoding="utf-8-sig"))

    def run_e7(self, write=False, delay_ms=0, fence_ttl=120, evidence: Path | None = None, sha: str | None = None):
        args = [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.harness),
                "-E7", str(E7), "-StateDir", str(self.state), "-Evidence", str(evidence or self.evidence), "-EvidenceSha", sha or self.evidence_sha,
                "-FakeFile", str(self.fake_file), "-AcRoot", str(self.acroot), "-PinsJson", json.dumps(self.pins),
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
        self.assertEqual((out["isolation"]["revalidated"], out["private_paths"], out["vendor_error_telemetry"]), (True, "verified", "test_seam"))
        self.assertEqual((self.fake()["get_member_calls"], self.fake()["browse_calls"]), (0, 0))
        self.assert_untouched()

    def test_single_verified_write_then_replay_refused(self):
        code, out, proc = self.run_e7(write=True)
        self.assertEqual((code, out["status"], out["get_next_member_no_count"], out["save_member_invocations"]), (0, "CREATED_VERIFIED", 1, 1))
        self.assertEqual((out["readback_found"], out["readback_match"]), (True, True))
        member = self.fake()["members"][0]
        self.assertTrue(member["Name"].startswith("XB DIRECTLOCAL E7 SYNTHETIC "))
        self.assertEqual((member["MemberType"], member["MobilePhone"], member["EmailAddress"], member["DOB"], member["IsActive"], member["Individual"], member["OpeningPoints"]),
                         ("Default", None, None, None, "T", "T", "0"))
        self.assertEqual(datetime.strptime(member["ExpiryDate"], "%Y-%m-%d") - datetime.strptime(member["RegisterDate"], "%Y-%m-%d") >= timedelta(days=729), True)
        self.assertEqual(out["audit_after"]["member_change_counter"] - out["audit_before"]["member_change_counter"], 1)
        self.assertEqual(self.files(), ["e7.claim", "e7.fence.json", "e7.intent.json", "e7.outcome.json"])
        self.assertNotIn(b"vendor failure", proc.stdout)
        # Only the single adapter readback loads a member; no browse table.
        self.assertEqual((self.fake()["get_member_calls"], self.fake()["browse_calls"]), (1, 0))
        self.replay_refused()

    def replay_refused(self):
        code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["status"], out["code"]), (1, "REFUSED", "e7_already_executed"))
        self.assertEqual(self.fake()["save_calls"], 1)

    def test_member_no_private_and_records_hash_chained(self):
        code, out, proc = self.run_e7(write=True)
        self.assertEqual(code, 0)
        self.assertNotIn(b"M000901", proc.stdout)
        self.assertNotIn("member_no", out)
        self.assertEqual(out["member_ref"], "e7m-" + hashlib.sha256(b"M000901").hexdigest()[:12])
        outcome = json.loads((self.state / "e7.outcome.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((outcome["member_no"], outcome["member_ref"]), ("M000901", out["member_ref"]))
        sha = lambda name: hashlib.sha256((self.state / name).read_bytes()).hexdigest()  # noqa: E731
        fence = json.loads((self.state / "e7.fence.json").read_text(encoding="utf-8-sig"))
        claim = json.loads((self.state / "e7.claim").read_text(encoding="utf-8-sig"))
        intent = json.loads((self.state / "e7.intent.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((fence["intent_sha256"], claim["fence_sha256"]), (sha("e7.intent.json"), sha("e7.fence.json")))
        self.assertEqual((intent["isolation_sha256"], out["records"]["intent"], out["records"]["claim"]), (self.evidence_sha, sha("e7.intent.json"), sha("e7.claim")))

    def test_occupied_candidates_use_at_most_three_numbering_calls(self):
        occupied = [{"MemberNo": no, "Name": "Existing"} for no in ("M000901", "M000902")]
        self.set_fake(members=occupied)
        code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["member_ref"], out["get_next_member_no_count"]), (0, "e7m-" + hashlib.sha256(b"M000903").hexdigest()[:12], 3))
        self.assertEqual(self.fake()["get_member_calls"], 1)
        self.set_fake(members=occupied + [{"MemberNo": "M000903", "Name": "Existing"}])
        shutil.rmtree(self.state)
        self.state.mkdir()
        code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["code"], out["get_next_member_no_count"]), (1, "member_no_candidates_exhausted", 3))
        self.assertEqual((self.fake()["save_calls"], self.fake()["next_index"], self.fake()["get_member_calls"]), (0, 3, 0))
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

    def test_isolation_evidence_independently_revalidated_before_any_login(self):
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        incomplete = {name: {"ok": True} for name in ("host", "processes", "sessions", "sql_capabilities", "book_channels", "scheduled_tasks", "services")}
        failed = dict(incomplete, assemblies={"ok": False, "code": "assembly_unreadable"})
        cases = {
            # A verdict of ISOLATED is not trusted when any finding contradicts it.
            "isolation_not_proven": {"verdict": "NOT_PROVEN"},
            "isolation_not_proven ": {"mail_queue_rows": 1},
            "isolation_not_proven  ": {"touching_processes": [{"process": "TechDevs.Unicart+.Service.exe", "client": "<local machine>"}]},
            "isolation_not_proven   ": {"touching_processes": [{"process": "unattributed", "client": "<local machine>"}]},
            "isolation_not_proven    ": {"acserver_book_tables": ["mailqueue", "member", "sys"]},
            "isolation_not_proven     ": {"member_triggers": [{"external_markers": ["http"], "writes_tables": ["ChangeLog"]}]},
            "isolation_not_proven      ": {"services_mentioning_book": ["X"]},
            "isolation_not_proven       ": {"blockers": ["plugin_sync_enabled"]},
            "isolation_evidence_identity_wrong": {"book": "AED_XBOUNDARIES"},
            "isolation_evidence_identity_wrong ": {"schema": "xb155-e7-isolation-v1"},
            "isolation_evidence_identity_wrong  ": {"host": "OTHER-HOST"},
            "isolation_evidence_stale": {"run_at": stale},
            "isolation_evidence_stale ": {"completed_at": stale},
            "isolation_collection_incomplete": {"collection": incomplete},
            "isolation_collection_incomplete ": {"collection": failed},
            "isolation_collection_incomplete  ": {"sample_failures": 1},
            "isolation_collection_incomplete   ": {"samples": 2},
            "isolation_evidence_field_missing": {"user_script_rows": None, "touching_processes": None},
            "isolation_build_not_qualified": {"assemblies": {"AutoCount.dll": "0" * 64, "AutoCount.Invoicing.dll": "1" * 64}},
        }
        for code_name, evidence in cases.items():
            with self.subTest(code=code_name.strip(), evidence=evidence):
                self.set_evidence(**evidence)
                code, out, _ = self.run_e7(write=True)
                self.assertEqual((code, out["code"]), (1, code_name.strip()))
                self.assert_untouched(sessions=0)
        missing = full_evidence(self.host, self.pins)
        del missing["einvoice_queue_rows"]
        self.set_evidence(raw=json.dumps(missing).encode())
        self.assertEqual(self.run_e7(write=True)[1]["code"], "isolation_evidence_field_missing")
        self.assert_untouched(sessions=0)

    def test_isolation_evidence_custody(self):
        self.assertEqual(self.run_e7(write=True, sha="a" * 64)[1]["code"], "isolation_evidence_hash_mismatch")
        self.assertEqual(self.run_e7(write=True, sha="not-a-hash")[1]["code"], "isolation_evidence_hash_required")
        outside = self.root / "isolation_outside.json"
        outside.write_bytes(self.evidence.read_bytes())
        self.assertEqual(self.run_e7(write=True, evidence=outside)[1]["code"], "isolation_evidence_outside_custody")
        # Tampering after collection breaks the printed hash.
        self.evidence.write_bytes(self.evidence.read_bytes().replace(b'"mail_queue_rows": 0', b'"mail_queue_rows": 0 '))
        self.assertEqual(self.run_e7(write=True)[1]["code"], "isolation_evidence_hash_mismatch")
        self.evidence.unlink()
        self.assertEqual(self.run_e7(write=True)[1]["code"], "isolation_evidence_missing")
        self.assert_untouched(sessions=0)

    def test_live_assemblies_must_match_qualified_build(self):
        (self.acroot / "AutoCount.Invoicing.dll").write_bytes(b"patched build")
        code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["code"]), (1, "autocount_build_not_qualified"))
        self.assert_untouched(sessions=0)

    def test_workspace_acl_must_be_private(self):
        subprocess.run(["icacls", str(self.state), "/grant", "*S-1-5-32-545:(R)"], capture_output=True, check=True)
        self.assertEqual(self.run_e7(write=True)[1]["code"], "state_acl_unsafe")
        subprocess.run(["icacls", str(self.state), "/remove:g", "*S-1-5-32-545"], capture_output=True, check=True)
        subprocess.run(["icacls", str(self.evidence_dir), "/grant", "*S-1-5-11:(OI)(CI)(M)"], capture_output=True, check=True)
        self.assertEqual(self.run_e7(write=True)[1]["code"], "evidence_acl_unsafe")
        subprocess.run(["icacls", str(self.evidence_dir), "/remove:g", "*S-1-5-11"], capture_output=True, check=True)
        subprocess.run(["icacls", str(self.root), "/inheritance:e"], capture_output=True, check=True)
        self.assertEqual(self.run_e7(write=True)[1]["code"], "workspace_acl_inherited")
        self.assert_untouched(sessions=0)

    def test_outbound_channels_must_be_quiet(self):
        for code_name, fake in {"user_script_present": {"user_scripts": 1}, "outbound_queue_not_empty": {"mail_queue": 2},
                                "outbound_queue_not_empty ": {"einvoice_queue": 1}}.items():
            with self.subTest(code=code_name.strip()):
                self.set_fake(**fake)
                for write in (False, True):
                    code, out, _ = self.run_e7(write=write)
                    self.assertEqual((code, out["code"]), (1, code_name.strip()))
                    self.assert_untouched()

    def test_mail_queue_change_after_save_holds(self):
        self.set_fake(mail_queue_after_save=1)
        code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["status"], out["save_member_invocations"]), (3, "CREATED_OUTBOUND_QUEUE_CHANGED_HOLD", 1))
        self.replay_refused()

    def test_existing_synthetic_member_blocks_duplicate(self):
        self.set_fake(members=[{"MemberNo": "Z1", "Name": "XB DIRECTLOCAL E7 SYNTHETIC 20261014"}])
        code, out, _ = self.run_e7(write=True)
        self.assertEqual((code, out["code"]), (1, "synthetic_member_already_present"))
        self.assertEqual((self.fake()["save_calls"], self.fake()["next_index"], self.fake()["get_member_calls"], self.fake()["browse_calls"]), (0, 0, 0, 0))
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
                "-StateDir", str(self.state), "-IsolationEvidencePath", str(self.root / "absent.json"), "-IsolationEvidenceSha256", "a" * 64,
                "-EnableTestBookWrite", "-ConfirmTargetAedTestingApi", "-ConfirmOwnerRiskAccepted"]
        out = json.loads(subprocess.run(args + ["-TestExpectedHost", "ANY"], capture_output=True, timeout=120).stdout.decode().strip().splitlines()[-1])
        self.assertEqual(out["code"], "test_seam_without_test_session")
        out = json.loads(subprocess.run(args, capture_output=True, timeout=120).stdout.decode().strip().splitlines()[-1])
        expected = "isolation_evidence_missing" if self.host.upper() == "DESKTOP-4I042L6" else "host_not_authorised"
        self.assertEqual(out["code"], expected)
        self.assert_untouched()

    def test_owner_console_refuses_non_interactive_input_before_any_prompt(self):
        proc = subprocess.run([powershell(), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(OWNER), "-Mode", "Write", "-ExpectedHead", "0" * 40],
                              input=b"AED_TESTINGAPI\nuser\nsecret\n", capture_output=True, timeout=120, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout.decode().strip().splitlines()[-1]), {"status": "OWNER_PROCEDURE_STOPPED", "code": "owner_console_required"})
        self.assertNotIn(b"secret", proc.stdout + proc.stderr)


class E7StaticTests(unittest.TestCase):
    def test_script_has_no_outbound_channels_and_pins_the_book(self):
        text = E7.read_text(encoding="utf-8")
        for forbidden in ("Invoke-WebRequest", "Invoke-RestMethod", "Net.WebClient", "HttpClient", "Send-MailMessage", "SmtpClient", "Start-Process",
                          "AC2_PROBE_DATABASE_NAME", "LoadBrowseTable", "Get-XbAutoCountMember "):
            self.assertNotIn(forbidden, text)
        self.assertIn('$script:E7Book = "AED_TESTINGAPI"', text)
        self.assertIn('"XB_AC2_DATABASE_NAME", $script:E7Book', text)
        self.assertEqual(text.count("= New-XbAutoCountMember "), 1)
        self.assertNotIn("SaveMember(", text)

    def test_assembly_pins_agree_between_collector_and_e7(self):
        checker = load_checker()
        text = E7.read_text(encoding="utf-8")
        for name, digest in checker.QUALIFIED_ASSEMBLIES.items():
            self.assertIn(f'"{name}" = "{digest}"', text)
        for name in checker.INSPECTIONS + checker.ZERO_COUNTS:
            self.assertIn(f'"{name}"', text)

    def test_owner_console_secret_handling(self):
        text = OWNER.read_text(encoding="utf-8")
        self.assertIn("Read-Host -AsSecureString", text)
        self.assertIn("SecureStringToBSTR", text)
        finally_block = text[text.index("finally {"):]
        for required in ("ZeroFreeBSTR($bstr)", "$secure.Dispose()"):
            self.assertIn(required, finally_block)
        self.assertIn('$psi.EnvironmentVariables["AC2_PROBE_PASSWORD"]', text)
        self.assertIn('$psi.EnvironmentVariables.Remove("AC2_PROBE_PASSWORD")', text)
        self.assertNotIn("SetEnvironmentVariable", text)
        self.assertNotIn("$env:AC2_PROBE_PASSWORD", text)
        self.assertNotIn("ConvertFrom-SecureString", text)
        self.assertNotIn("Start-Transcript", text)
        self.assertIn("IsInputRedirected", text)
        self.assertIn("source_head_mismatch", text)

    def test_directlocal_bridge_still_hard_disarmed(self):
        bridge = (ROOT / "scripts" / "ac2_directlocal_member_bridge.ps1").read_text(encoding="utf-8")
        self.assertIn('if ($null -eq $SessionFactory) { throw "autocount_real_bridge_disabled" }', bridge)
        self.assertNotIn("e7", bridge.lower())


class IsolationClassifierTests(unittest.TestCase):
    def setUp(self):
        self.checker = load_checker()
        self.base = full_evidence("DESKTOP-4I042L6", self.checker.QUALIFIED_ASSEMBLIES)

    def test_base_is_isolated_and_each_channel_blocks(self):
        self.assertEqual(self.checker.classify(self.base), [])
        mutations = {
            "unexpected_process_touches_book": lambda e: e["touching_processes"].append({"process": "TechDevs.Unicart+.Service.exe", "client": "<local machine>"}),
            "unexpected_process_touches_book ": lambda e: e["touching_processes"].__setitem__(0, {"process": "Accounting.exe", "client": "10.0.0.5"}),
            "unexpected_process_touches_book  ": lambda e: e["touching_processes"].append({"process": "unattributed", "client": "<local machine>"}),
            "acserver_unqualified_book_activity": lambda e: e.__setitem__("acserver_book_tables", ["changelog", "mailqueue"]),
            "sql_agent_jobs_reference_book": lambda e: e.__setitem__("sql_agent_jobs_for_book", 1),
            "member_trigger_not_internal_only": lambda e: e["member_triggers"].append({"external_markers": ["sp_send_dbmail"], "writes_tables": []}),
            "instance_external_capability_present": lambda e: e["instance"].__setitem__("linked_servers", 1),
            "book_external_capability_present": lambda e: e.__setitem__("book_user_assemblies", 1),
            "host_job_references_book": lambda e: e.__setitem__("services_mentioning_book", ["TD Unicart+ Service - AED_TESTINGAPI"]),
            "pos_sync_active_on_book": lambda e: e.__setitem__("pos_registrations_active_after_clone", 1),
            "plugin_sync_enabled": lambda e: e.__setitem__("plugins_allowing_sync", 1),
            "mail_or_rewards_channel_active": lambda e: e.__setitem__("einvoice_queue_rows", 1),
            "user_script_present": lambda e: e.__setitem__("user_script_rows", 1),
            "autocount_build_not_qualified": lambda e: e["assemblies"].__setitem__("AutoCount.dll", "0" * 64),
            "collection_incomplete": lambda e: e["collection"].__setitem__("scheduled_tasks", {"ok": False, "code": "task_listing_requires_elevation"}),
            "collection_incomplete ": lambda e: e["collection"].pop("services"),
            "collection_incomplete  ": lambda e: e.__setitem__("sample_failures", 1),
            "evidence_identity_wrong": lambda e: e.__setitem__("host", "OTHER"),
            "evidence_field_missing": lambda e: e.pop("mail_queue_rows"),
            "evidence_field_missing ": lambda e: e["instance"].pop("xp_cmdshell"),
        }
        for code, mutate in mutations.items():
            with self.subTest(code=code.strip()):
                evidence = copy.deepcopy(self.base)
                mutate(evidence)
                self.assertIn(code.strip(), self.checker.classify(evidence))


class IsolationCollectorFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.checker = load_checker()

    def completed(self, stdout="", returncode=0):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")

    def test_tool_failures_and_untrustworthy_listings_raise(self):
        c = self.checker
        with mock.patch.object(c.subprocess, "run", return_value=self.completed(returncode=1)):
            with self.assertRaisesRegex(c.InspectionError, "tasklist_exit_nonzero"):
                c.processes()
        with mock.patch.object(c.subprocess, "run", side_effect=subprocess.TimeoutExpired("tasklist", 60)):
            with self.assertRaisesRegex(c.InspectionError, "tasklist_failed"):
                c.processes()
        rows = "".join(f'"p{i}.exe","{4000000 + i}","Console","1","1 K"\n' for i in range(30))
        with mock.patch.object(c.subprocess, "run", return_value=self.completed(rows)):
            with self.assertRaisesRegex(c.InspectionError, "process_listing_untrustworthy"):
                c.processes()  # this process is absent from the listing
        with mock.patch.object(c, "is_elevated", return_value=False):
            for step, code in ((c.scheduled_task_hits, "task_listing_requires_elevation"), (c.service_hits, "service_listing_requires_elevation")):
                with self.assertRaisesRegex(c.InspectionError, code):
                    step()
        with mock.patch.object(c, "is_elevated", return_value=True):
            with mock.patch.object(c.subprocess, "run", return_value=self.completed('"\\a","x"\n')):
                with self.assertRaisesRegex(c.InspectionError, "task_listing_untrustworthy"):
                    c.scheduled_task_hits()
            with mock.patch.object(c.subprocess, "run", return_value=self.completed("not json")):
                with self.assertRaisesRegex(c.InspectionError, "service_listing_unparseable"):
                    c.service_hits()
            services = [{"Name": f"s{i}", "PathName": "C:\\x.exe"} for i in range(60)] + [{"Name": "hidden", "PathName": None}]
            with mock.patch.object(c.subprocess, "run", return_value=self.completed(json.dumps(services))):
                with self.assertRaisesRegex(c.InspectionError, "service_listing_untrustworthy"):
                    c.service_hits()
        with self.assertRaisesRegex(c.InspectionError, "assembly_unreadable"):
            c.assembly_hashes(Path(tempfile.gettempdir()) / "xb-e7-no-such-acroot")

    def test_collect_fails_closed_when_sql_and_host_inventory_unavailable(self):
        c = self.checker
        broken = types.ModuleType("pyodbc")

        def refuse(*_args, **_kwargs):
            raise RuntimeError("no server")

        broken.connect = refuse
        with mock.patch.dict(sys.modules, {"pyodbc": broken}), mock.patch.dict(os.environ, {"AUTOCOUNT_READONLY_SQL_CONNECTION_STRING": "x"}), \
                mock.patch.object(c, "is_elevated", return_value=False), mock.patch.object(c, "processes", side_effect=c.InspectionError("tasklist_failed")):
            evidence = c.collect(samples=1, interval=0)
        self.assertEqual(evidence["verdict"], "NOT_PROVEN")
        self.assertIn("collection_incomplete", evidence["blockers"])
        for name, code in (("sessions", "sql_connection_failed"), ("sql_capabilities", "sql_connection_failed"), ("book_channels", "sql_connection_failed"),
                           ("scheduled_tasks", "task_listing_requires_elevation"), ("services", "service_listing_requires_elevation"), ("processes", "tasklist_failed")):
            self.assertEqual(evidence["collection"][name], {"ok": False, "code": code})

    def test_evidence_written_once_with_matching_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, digest = self.checker.write_evidence(Path(tmp), {"verdict": "NOT_PROVEN"})
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            with self.assertRaises(FileExistsError):
                open(path, "xb").close()


if __name__ == "__main__":
    unittest.main()
