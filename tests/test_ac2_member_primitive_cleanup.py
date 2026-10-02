"""Test-book cleanup / absence check (W-G2-149 section 5, T-17).

DeleteMember is reachable only for fully synthetic rows created by the
integration user in an allowlisted, non-production test book with an
explicit -Book test and -ConfirmDelete. -VerifyAbsent never writes.
Synthetic data only, against the in-memory AutoCount double.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLEANUP = ROOT / "scripts/ac2_member_test_cleanup.ps1"
FAKE = ROOT / "tests/fixtures/ac2_member_primitive/fake_autocount.ps1"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
IU = "IU_PLACEHOLDER"
TEST_CONFIG = {"DatabaseName": "BOOK_TEST_PLACEHOLDER", "LoginUserId": IU, "IntegrationUserId": IU, "ProductionBook": "BOOK_PROD_PLACEHOLDER", "TestBookAllowlist": ["BOOK_TEST_PLACEHOLDER"]}
ROWS = [
    {"MemberNo": "00090000001", "Name": "ZZTEST One", "EmailAddress": "zztest.one@example.invalid", "CreatedUserID": IU},
    {"MemberNo": "00090000002", "Name": "ZZTEST Two", "EmailAddress": "zztest.two@EXAMPLE.INVALID", "CreatedUserID": IU},
    {"MemberNo": "00090000003", "Name": "ZZTEST Staff", "EmailAddress": "zztest.staff@example.invalid", "CreatedUserID": "STAFF_PLACEHOLDER"},
    {"MemberNo": "00090000004", "Name": "Real Looking", "EmailAddress": "zztest.half@example.invalid", "CreatedUserID": IU},
    {"MemberNo": "00090000005", "Name": "ZZTEST Half", "EmailAddress": "half@example.test", "CreatedUserID": IU},
    {"MemberNo": "91234567", "Name": "Tan Ah Kow", "EmailAddress": "member.one@example.test", "CreatedUserID": IU},
]
CASES = [
    {"name": "delete_only_fully_synthetic_iu_rows", "book": "test", "confirm": True, "config": TEST_CONFIG, "rows": ROWS},
    {"name": "verify_absent_reports_present_and_writes_nothing", "book": "test", "verify": True, "config": TEST_CONFIG, "rows": ROWS},
    {"name": "verify_absent_clean_book", "book": "test", "verify": True, "config": TEST_CONFIG, "rows": ROWS[5:]},
    {"name": "production_book_refused", "book": "production", "confirm": True, "config": TEST_CONFIG, "rows": ROWS},
    {"name": "test_book_equal_to_production_refused", "book": "test", "confirm": True, "config": {**TEST_CONFIG, "DatabaseName": "BOOK_PROD_PLACEHOLDER", "TestBookAllowlist": ["BOOK_PROD_PLACEHOLDER"]}, "rows": ROWS},
    {"name": "not_allowlisted_refused", "book": "test", "confirm": True, "config": {**TEST_CONFIG, "TestBookAllowlist": []}, "rows": ROWS},
    {"name": "login_user_not_iu_refused", "book": "test", "confirm": True, "config": {**TEST_CONFIG, "LoginUserId": "ADMIN_PLACEHOLDER"}, "rows": ROWS},
    {"name": "missing_confirmation_refused", "book": "test", "config": TEST_CONFIG, "rows": ROWS},
    {"name": "session_bound_elsewhere_refused", "book": "test", "confirm": True, "config": TEST_CONFIG, "rows": ROWS, "session_database": "BOOK_OTHER_PLACEHOLDER"},
]

HARNESS = r"""
[CmdletBinding()]
param([string]$Cleanup, [string]$Fake, [string]$CasesPath, [string]$OutPath)
$ErrorActionPreference = "Stop"
. $Cleanup -LibraryOnly
. $Fake
$cases = [IO.File]::ReadAllText($CasesPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
$request = [pscustomobject]@{ first_claimed_at = "2026-09-30T02:00:00Z"; name = "x"; base_member_no = "91234567"; email = "x@example.test"; DOB = "2000-01-01"; RegisterDate = "2026-01-01"; ExpiryDate = "2027-12-31" }
$out = [ordered]@{}
foreach ($case in $cases) {
    $cfg = $case.config
    $config = [pscustomobject]@{
        DatabaseName = [string]$cfg.DatabaseName; LoginUserId = [string]$cfg.LoginUserId; IntegrationUserId = [string]$cfg.IntegrationUserId
        ProductionBook = [string]$cfg.ProductionBook; TestBookAllowlist = [string[]]@($cfg.TestBookAllowlist); Fault = ""; PasswordEnvironmentVariable = ""
    }
    $fakeBook = New-XbFakeAutoCountBook -Case $case -Request $request -ServerNow ([DateTimeOffset]::UtcNow) -IntegrationUserId $IU -DatabaseName ([string]$cfg.DatabaseName)
    $command = & $fakeBook.MemberCommandFactory $null
    $command.State.deletes = 0
    [void]($command | Add-Member -MemberType ScriptMethod -Name DeleteMember -Value {
        param([string]$MemberNo)
        $this.State.deletes = [int]$this.State.deletes + 1
        $target = @($this.State.table.Rows | Where-Object { [string]$_["MemberNo"] -eq $MemberNo })
        foreach ($row in $target) { $this.State.table.Rows.Remove($row) }
        return $true
    })
    $report = Invoke-XbAc2MemberTestCleanup -Book ([string]$case.book) -VerifyAbsent:([bool]($case.PSObject.Properties["verify"])) -ConfirmDelete:([bool]($case.PSObject.Properties["confirm"])) -Config $config -EnableProductionAdapter -SessionFactory $fakeBook.SessionFactory -MemberCommandFactory $fakeBook.MemberCommandFactory -MutexName ("Global\XB-AC2-MemberCreate-test-" + [Guid]::NewGuid().ToString("N"))
    $out[[string]$case.name] = [ordered]@{
        report = $report
        deletes = [int]$command.State.deletes
        saves = [int]$command.State.saves
        remaining = @($command.State.table.Rows | ForEach-Object { [string]$_["MemberNo"] })
        session_opened = [int]$fakeBook.State.session_opened
    }
}
[IO.File]::WriteAllText($OutPath, ($out | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
""".replace("$IU", '"' + IU + '"')


@unittest.skipUnless(Path(POWERSHELL).exists(), "Windows PowerShell 5.1 is required")
class Ac2MemberTestCleanupTests(unittest.TestCase):
    report: dict

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory(prefix="xb-ac2-cleanup-") as temp:
            root = Path(temp)
            (root / "h.ps1").write_text(HARNESS, encoding="utf-8")
            (root / "c.json").write_text(json.dumps(CASES), encoding="utf-8")
            completed = subprocess.run(
                [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(root / "h.ps1"),
                 "-Cleanup", str(CLEANUP), "-Fake", str(FAKE), "-CasesPath", str(root / "c.json"), "-OutPath", str(root / "o.json")],
                capture_output=True, text=True, timeout=600,
            )
            if completed.returncode != 0 or not (root / "o.json").exists():
                raise AssertionError(completed.stdout[-4000:] + completed.stderr[-4000:])
            cls.report = json.loads((root / "o.json").read_text(encoding="utf-8-sig"))

    def test_delete_is_limited_to_fully_synthetic_iu_rows(self):
        entry = self.report["delete_only_fully_synthetic_iu_rows"]
        self.assertEqual(entry["report"]["status"], "cleaned")
        self.assertEqual(entry["report"]["deleted"], 2)
        self.assertEqual(entry["report"]["kept_not_created_by_iu"], 1)
        self.assertEqual(entry["report"]["remaining_after"], 0)
        self.assertEqual(entry["deletes"], 2)
        self.assertEqual(entry["saves"], 0)
        self.assertEqual(sorted(entry["remaining"]), ["00090000003", "00090000004", "00090000005", "91234567"])

    def test_verify_absent_never_writes(self):
        present = self.report["verify_absent_reports_present_and_writes_nothing"]
        self.assertEqual(present["report"]["status"], "present")
        self.assertEqual(present["report"]["synthetic_rows"], 3)
        self.assertEqual((present["deletes"], present["saves"]), (0, 0))
        self.assertEqual(len(present["remaining"]), len(ROWS))
        clean = self.report["verify_absent_clean_book"]
        self.assertEqual(clean["report"]["status"], "absent")
        self.assertEqual((clean["deletes"], clean["saves"]), (0, 0))

    def test_guards_refuse_before_any_session_or_delete(self):
        expected = {
            "production_book_refused": ("test_book_required", 0),
            "test_book_equal_to_production_refused": ("book_binding_mismatch", 0),
            "not_allowlisted_refused": ("book_binding_mismatch", 0),
            "login_user_not_iu_refused": ("integration_user_mismatch", 0),
            "missing_confirmation_refused": ("confirm_delete_required", 0),
            "session_bound_elsewhere_refused": ("book_binding_mismatch", 1),
        }
        for name, (reason, sessions) in expected.items():
            with self.subTest(name=name):
                entry = self.report[name]
                self.assertEqual(entry["report"]["status"], "refused")
                self.assertEqual(entry["report"]["reason"], reason)
                self.assertEqual(entry["deletes"], 0)
                self.assertEqual(entry["session_opened"], sessions)
                self.assertEqual(len(entry["remaining"]), len(ROWS))

    def test_cleanup_source_contract(self):
        source = CLEANUP.read_text(encoding="utf-8")
        self.assertEqual(source.count("DeleteMember("), 1)
        self.assertNotIn("SaveMember(", source)
        self.assertIn('[ValidateSet("test")][string]$Book', source)
        self.assertTrue(CLEANUP.read_bytes().isascii())


if __name__ == "__main__":
    unittest.main()
