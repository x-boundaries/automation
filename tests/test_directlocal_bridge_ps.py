"""#155 DirectLocal AutoCount child bridge under real PowerShell (synthetic only).

The unchanged adapter runs against a fake MemberCommand whose state persists in
a JSON file, so separate child processes (and killed ones) share one fake
account book. No AutoCount assembly, login, account book or network is used.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "directlocal"))
sys.path.insert(0, str(ROOT / "tests"))

import xb_directlocal as dl  # noqa: E402

BRIDGE = ROOT / "scripts" / "ac2_directlocal_member_bridge.ps1"
PAYLOAD = {"email_address": "alpha@example.test", "expiry_date": "2028-10-14", "mobile_phone": "6580000001", "name": "Synthetic Member Alpha", "register_date": "2026-10-15"}
PII = ("Synthetic Member Alpha", "6580000001", "alpha@example.test")

FAKE_HOST = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Bridge,
    [Parameter(Mandatory)][string]$StateFile,
    [Parameter(Mandatory)][string]$Operation,
    [switch]$EnableProductionAdapter,
    [string]$FenceId,
    [string]$ClaimDirectory,
    [string]$WriteDeadlineUtc
)
$ErrorActionPreference = "Stop"
$fields = @("MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress", "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual")
function Read-FakeState { return ([IO.File]::ReadAllText($StateFile) | ConvertFrom-Json) }
function Save-FakeState($State) { [IO.File]::WriteAllText($StateFile, ($State | ConvertTo-Json -Depth 6 -Compress)) }
function New-FakeTable {
    $table = New-Object System.Data.DataTable
    foreach ($f in $fields) { [void]$table.Columns.Add($f, [object]) }
    return ,$table
}
function New-FakeEntity($Record) {
    $table = New-FakeTable
    $row = $table.NewRow()
    foreach ($f in $fields) {
        $row[$f] = [DBNull]::Value
        if ($null -ne $Record -and $null -ne $Record.PSObject.Properties[$f] -and $null -ne $Record.$f) { $row[$f] = $Record.$f }
    }
    [void]$table.Rows.Add($row)
    return [pscustomobject]@{ Row = $row }
}
$command = [pscustomobject]@{ Name = "fake" }
$command | Add-Member -MemberType ScriptMethod -Name NewMember -Value { param([bool]$Unused) return (New-FakeEntity $null) }
$command | Add-Member -MemberType ScriptMethod -Name SaveMember -Value {
    param($Entity)
    $s = Read-FakeState
    if ($s.save_throws) { throw "vendor failure for Synthetic Member Alpha" }
    $rec = [ordered]@{}
    foreach ($f in $fields) {
        $v = $Entity.Row[$f]
        $rec[$f] = if ($v -is [DBNull]) { $null } elseif ($v -is [datetime]) { $v.ToString("yyyy-MM-dd") } else { [string]$v }
    }
    $s.members = @($s.members) + @([pscustomobject]$rec)
    $s.saves = [int]$s.saves + 1
    Save-FakeState $s
    if ([int]$s.hang_after_save_ms -gt 0) { Start-Sleep -Milliseconds ([int]$s.hang_after_save_ms) }
}
$command | Add-Member -MemberType ScriptMethod -Name GetMember -Value {
    param([string]$MemberNo)
    $s = Read-FakeState
    $found = @(@($s.members) | Where-Object { $null -ne $_ -and $_.MemberNo -ceq $MemberNo })
    if ($found.Count -eq 0) { return $null }
    return (New-FakeEntity $found[0])
}
$command | Add-Member -MemberType ScriptMethod -Name GetNextMemberNo -Value {
    $s = Read-FakeState
    $value = @($s.next)[[int]$s.next_index]
    $s.next_index = [int]$s.next_index + 1
    Save-FakeState $s
    return $value
}
$command | Add-Member -MemberType ScriptMethod -Name LoadBrowseTable -Value {
    $s = Read-FakeState
    if ($s.browse_error) { throw "browse failed" }
    $table = New-FakeTable
    foreach ($r in (@($s.browse) + @($s.members))) {
        if ($null -eq $r) { continue }
        $row = $table.NewRow()
        foreach ($f in $fields) { $row[$f] = [DBNull]::Value }
        if ($null -ne $r.MobilePhone) { $row["MobilePhone"] = $r.MobilePhone }
        if ($null -ne $r.EmailAddress) { $row["EmailAddress"] = $r.EmailAddress }
        [void]$table.Rows.Add($row)
    }
    return ,$table
}
$factory = { param($Session) return $command }.GetNewClosure()
$sessionFactory = { [pscustomobject]@{ UserSession = "fake"; DBSetting = "fake"; MemberCommandFactory = $factory } }.GetNewClosure()
& $Bridge -Operation $Operation -EnableProductionAdapter:$EnableProductionAdapter -FenceId $FenceId -ClaimDirectory $ClaimDirectory -WriteDeadlineUtc $WriteDeadlineUtc -SessionFactory $sessionFactory
exit $LASTEXITCODE
"""


def powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class DirectLocalBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-directlocal-bridge-"))
        self.host = self.temp / "fake_host.ps1"
        self.host.write_text(FAKE_HOST, encoding="utf-8")
        self.state = self.temp / "fake_autocount.json"
        self.claims = self.temp / "claims"
        self.claims.mkdir()
        self.set_state()

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def set_state(self, **overrides):
        state = {"members": [], "browse": [], "saves": 0, "next": ["M000201", "M 1"], "next_index": 0, "browse_error": False, "save_throws": False, "hang_after_save_ms": 0}
        state.update(overrides)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def fake(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8-sig"))

    def command(self) -> list[str]:
        return [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.host), "-Bridge", str(BRIDGE), "-StateFile", str(self.state)]

    def run_bridge(self, operation: str, stdin: dict | None, *extra: str, enable: bool = True, timeout: int = 120):
        args = self.command() + ["-Operation", operation] + (["-EnableProductionAdapter"] if enable else []) + list(extra)
        data = b"" if stdin is None else (json.dumps(stdin) + "\n").encode("ascii")
        proc = subprocess.run(args, input=data, capture_output=True, timeout=timeout, check=False)
        out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        return proc.returncode, (json.loads(out[-1]) if out else None), proc

    def write_request(self, fence_id: str, *, salt: str = "a1" * 16, member_no: str = "M000201", tamper: bool = False) -> dict:
        text = dl.canonical_payload(PAYLOAD)
        digest = dl.payload_digest(salt, text)
        return {"fence_id": fence_id, "member_no": member_no, "salt": salt, "payload_canonical": text.replace("Alpha", "Omega") if tamper else text, "payload_digest": digest}

    def deadline(self, seconds: int = 60) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def write_args(self, fence_id: str, seconds: int = 60) -> list[str]:
        return ["-FenceId", fence_id, "-ClaimDirectory", str(self.claims), "-WriteDeadlineUtc", self.deadline(seconds)]

    def assert_clean(self, proc) -> None:
        text = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
        for value in PII:
            self.assertNotIn(value, text)

    def test_read_operations(self):
        self.assertEqual(self.run_bridge("probe", {"member_no": "M000201"})[:2], (0, {"status": "FREE"}))
        self.assertEqual(self.run_bridge("next-member-no", {})[:2], (0, {"member_no": "M000201"}))
        self.assertEqual(self.run_bridge("next-member-no", {})[:2], (1, {"error": "member_no_generator_failed"}))
        self.assertEqual(self.run_bridge("probe", {"member_no": "../x"})[:2], (1, {"error": "member_no_invalid"}))
        self.assertEqual(self.run_bridge("probe", {"member_no": "M000201"}, enable=False)[:2], (1, {"error": "autocount_adapter_disabled"}))

    def test_symmetric_legacy_precheck(self):
        keys_65 = list(dl.duplicate_phone_keys("6580000001"))
        keys_local = list(dl.duplicate_phone_keys("80000001"))
        cases = [
            ({"browse": [{"MobilePhone": "8000 0001", "EmailAddress": None}]}, keys_65, None, ("CANDIDATE_FOUND", False, True, False)),
            ({"browse": [{"MobilePhone": "+65 8000 0001", "EmailAddress": None}]}, keys_local, None, ("CANDIDATE_FOUND", False, True, False)),
            ({"members": [{"MemberNo": "80000001", "MobilePhone": None, "EmailAddress": None}]}, keys_65, None, ("CANDIDATE_FOUND", True, False, False)),
            ({"browse": [{"MobilePhone": None, "EmailAddress": " ALPHA@example.TEST "}]}, [], "alpha@example.test", ("CANDIDATE_FOUND", False, False, True)),
            ({"browse": [{"MobilePhone": "6580000002", "EmailAddress": "other@example.test"}]}, keys_65, "alpha@example.test", ("NO_CANDIDATE", False, False, False)),
            ({"browse_error": True}, keys_65, "alpha@example.test", ("LOOKUP_FAILED", False, False, False)),
        ]
        for state, keys, email, expected in cases:
            with self.subTest(state=state, keys=keys):
                self.set_state(**state)
                code, out, proc = self.run_bridge("legacy-precheck", {"phone_keys": keys, "email_address": email})
                self.assertEqual(code, 0)
                self.assertEqual((out["outcome"], out["phone_member_no_hit"], out["mobile_phone_hit"], out["email_hit"]), expected)
                self.assertEqual(out["lookups"], max(1, len(keys)))
                self.assert_clean(proc)
        self.assertEqual(self.run_bridge("legacy-precheck", {"phone_keys": ["+6580000001"], "email_address": None})[:2], (1, {"error": "phone_keys_invalid"}))

    def test_write_once_claims_saves_once_and_refuses_replay(self):
        fence = "fence-" + "1" * 32
        code, out, proc = self.run_bridge("write-once", self.write_request(fence), *self.write_args(fence))
        self.assertEqual((code, out), (0, {"save_invocation_count": 1, "readback_found": True, "readback_match": True}))
        self.assert_clean(proc)
        self.assertTrue((self.claims / f"{fence}.claim").exists())
        self.assertEqual(json.loads((self.claims / f"{fence}.outcome").read_text(encoding="ascii"))["readback_match"], True)
        member = self.fake()["members"][0]
        self.assertEqual((member["MemberNo"], member["MobilePhone"], member["MemberType"], member["DOB"], member["RegisterDate"], member["ExpiryDate"]), ("M000201", "6580000001", "Default", None, "2026-10-15", "2028-10-14"))
        code, out, _ = self.run_bridge("write-once", self.write_request(fence), *self.write_args(fence))
        self.assertEqual((code, out), (3, {"error": "fence_already_claimed"}))
        self.assertEqual(self.fake()["saves"], 1)
        self.assertEqual(self.run_bridge("reconcile", {"member_no": "M000201", "payload_canonical": dl.canonical_payload(PAYLOAD)})[:2], (0, {"found": True, "match": True}))
        self.assertEqual(self.run_bridge("reconcile", {"member_no": "M000209", "payload_canonical": dl.canonical_payload(PAYLOAD)})[:2], (0, {"found": False, "match": False}))

    def test_write_once_refusals_never_save_or_claim(self):
        fence = "fence-" + "2" * 32
        cases = [
            ("payload_digest_mismatch", self.write_request(fence, tamper=True), self.write_args(fence), True),
            ("fence_binding_mismatch", self.write_request("fence-" + "3" * 32), self.write_args(fence), True),
            ("writer_deadline_expired", self.write_request(fence), ["-FenceId", fence, "-ClaimDirectory", str(self.claims), "-WriteDeadlineUtc", self.deadline(-5)], True),
            ("fence_id_invalid", self.write_request(fence), ["-FenceId", "fence-1", "-ClaimDirectory", str(self.claims), "-WriteDeadlineUtc", self.deadline()], True),
            ("claim_directory_invalid", self.write_request(fence), ["-FenceId", fence, "-ClaimDirectory", str(self.temp / "absent"), "-WriteDeadlineUtc", self.deadline()], True),
            ("autocount_adapter_disabled", self.write_request(fence), self.write_args(fence), False),
            ("member_no_invalid", self.write_request(fence, member_no="M 1"), self.write_args(fence), True),
        ]
        for code_name, request, args, enable in cases:
            with self.subTest(code=code_name):
                code, out, _ = self.run_bridge("write-once", request, *args, enable=enable)
                self.assertEqual((code, out), (1, {"error": code_name}))
                self.assertEqual(self.fake()["saves"], 0)
                self.assertFalse((self.claims / f"{fence}.claim").exists())

    def test_vendor_failure_after_claim_is_sanitised(self):
        self.set_state(save_throws=True)
        fence = "fence-" + "4" * 32
        code, out, proc = self.run_bridge("write-once", self.write_request(fence), *self.write_args(fence))
        self.assertEqual((code, out), (1, {"error": "bridge_failed"}))
        self.assert_clean(proc)
        self.assertTrue((self.claims / f"{fence}.claim").exists())  # parent classifies this as uncertain

    def test_watchdog_terminates_hung_writer_after_save(self):
        self.set_state(hang_after_save_ms=60000)
        fence = "fence-" + "5" * 32
        started = time.monotonic()
        code, out, _ = self.run_bridge("write-once", self.write_request(fence), *self.write_args(fence, seconds=6), timeout=50)
        self.assertLess(time.monotonic() - started, 45)
        self.assertNotEqual(code, 0)
        self.assertIsNone(out)
        self.assertEqual(self.fake()["saves"], 1)
        self.assertTrue((self.claims / f"{fence}.claim").exists())
        self.assertFalse((self.claims / f"{fence}.outcome").exists())


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class DirectLocalEndToEndTests(unittest.TestCase):
    """Processor -> BridgeClient -> real PowerShell bridge -> fake MemberCommand."""

    def setUp(self):
        import test_member_directlocal as fixtures

        self.fx = fixtures
        self.bridge_tests = DirectLocalBridgeTests("test_read_operations")
        self.bridge_tests.setUp()
        self.root = self.bridge_tests.temp / "state"
        self.root.mkdir()
        # Real time: the child's watchdog deadline is wall-clock.
        self.ledger = dl.Ledger(self.root)
        self.processor = dl.Processor(self.ledger, alive=lambda pid: False, writer_deadline_seconds=6, writer_grace_seconds=3)
        self.processor.init_ledger(mode="synthetic", store_gid=fixtures.STORE, shop_domain=fixtures.DOMAIN)
        self.processor.begin_run("BASELINE")
        cutover = dl.parse_ts(self.ledger.conn.execute("SELECT cutover_at FROM runs").fetchone()[0])
        page = fixtures.record("toolu_page", fixtures.page_input(None), fixtures.page_data([fixtures.node(fixtures.NEW, cutover + timedelta(minutes=1), ["member-mg"])], False, None), cutover + timedelta(seconds=1))
        (self.root / "inbox" / "toolu_page.json").write_text(json.dumps(page), encoding="utf-8")
        self.processor.ingest_run()
        self.client = dl.BridgeClient(self.bridge_tests.command())
        self.created = cutover + timedelta(minutes=1)
        self.counter = 0

    def tearDown(self):
        self.ledger.close()
        self.bridge_tests.tearDown()

    def capture(self) -> Path:
        self.counter += 1
        customer = self.fx.profile(self.fx.NEW, created=self.created, phone="+65 8000 0001", email="alpha@example.test", first="Synthetic", last="Member Alpha")
        path = self.root / "inbox" / f"toolu_profile_{self.counter}.json"
        path.write_text(json.dumps(self.fx.profile_record(f"toolu_profile_{self.counter}", customer, self.fx.NEW, datetime.now(timezone.utc))), encoding="utf-8")
        return path

    def admit(self) -> None:
        self.assertEqual(self.processor.process(self.capture())["customer_state"], "ADMITTED")
        self.processor.approve_write(self.fx.NEW, confirm_suffix="000002")

    def test_full_synthetic_write_then_replay_is_refused(self):
        self.admit()
        result = self.processor.process(self.capture(), bridge=self.client, enable_write=True)
        self.assertEqual(result["fence_state"], "CREATED_VERIFIED")
        self.assertEqual(self.bridge_tests.fake()["saves"], 1)
        with self.assertRaises(dl.DirectLocalError):
            self.processor.process(self.capture(), bridge=self.client, enable_write=True)
        self.assertEqual(self.bridge_tests.fake()["saves"], 1)

    def test_hung_writer_is_uncertain_then_reconciled_read_only(self):
        self.bridge_tests.set_state(hang_after_save_ms=60000)
        self.admit()
        result = self.processor.process(self.capture(), bridge=self.client, enable_write=True)
        self.assertEqual(result["fence_state"], "WRITE_OUTCOME_UNCERTAIN")
        self.assertEqual(self.bridge_tests.fake()["saves"], 1)
        state = self.bridge_tests.fake()
        state["hang_after_save_ms"] = 0
        self.bridge_tests.state.write_text(json.dumps(state), encoding="utf-8")
        result = self.processor.reconcile(self.capture(), bridge=self.client)
        self.assertEqual(result["fence_state"], "RECONCILED_VERIFIED")
        self.assertEqual(self.bridge_tests.fake()["saves"], 1)


if __name__ == "__main__":
    unittest.main()
