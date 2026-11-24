"""#268 slice E: the removals tool's PowerShell child under real PowerShell.

Synthetic only: a marked fake host (same marker rule as the #155, type move
and benefits bridge tests) whose MemberCommand updates members in place and
records which columns each SaveMember changed, and whose DBSetting answers the
removals scan and the book guard's pinned reads. No AutoCount assembly, login,
account book or network. Fake data only (example.com, fake 9-digit numbers).

The one-move rule is proven here, at the write boundary: removal-save refuses
any source but MEMBER, any target but GUEST, an expire that is not due and a
revoke whose basis is not the expected expiry, before any session or claim;
removal-restore refuses anything but GUEST -> MEMBER; only MemberType is ever
assigned, and a changed ExpiryDate reads back as a mismatch.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "directlocal"))
sys.path.insert(0, str(ROOT / "tests"))

import xb_directlocal as dl  # noqa: E402
import xb_member_removals as mr  # noqa: E402
from test_directlocal_bridge_ps import powershell  # noqa: E402

BRIDGE = ROOT / "scripts" / "ac2_member_removals_bridge.ps1"
LIBRARY = ROOT / "scripts" / "ac2_member_removals.ps1"
TYPE_MOVE_LIBRARY = ROOT / "scripts" / "ac2_member_type_move.ps1"
GUARD = ROOT / "scripts" / "ac2_directlocal_book_guard.ps1"
ADAPTER = ROOT / "scripts" / "ac2_member_gateway_autocount_adapter.ps1"
LIVE_BOOK = "AED_XBOUNDARIES"
TEST_BOOK = "AED_TESTINGAPI"
PINNED_HOST = "DESKTOP-4I042L6"
LM = "2026-10-01T09:00:00.000"
YESTERDAY = "2026-11-23T00:00:00.000"
RUN_DAY = "2026-11-24"
SERVER_NOW = "2026-11-24T12:00:00"

# Pins $script:XbMxSqlText in scripts/ac2_member_removals.ps1.
MX_SQL_TEXTS = {
    "expiring_members_page": (
        "SELECT (SELECT TOP 500 MemberNo AS member_no, IsActive AS is_active, "
        "CONVERT(varchar(23), ExpiryDate, 126) AS expiry_local, CONVERT(varchar(23), LastModified, 126) AS last_modified_local "
        "FROM Member WHERE MemberType = ? AND MemberNo > ? AND (ExpiryDate < ? OR ExpiryDate IS NULL OR ExpiryDate < ?) "
        "ORDER BY MemberNo FOR JSON PATH, INCLUDE_NULL_VALUES)"
    ),
}

MX_FAKE_HOST = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Bridge,
    [Parameter(Mandatory)][string]$StateFile,
    [Parameter(Mandatory)][string]$Operation,
    [switch]$EnableProductionAdapter,
    [string]$FenceId,
    [string]$ClaimDirectory,
    [string]$WriteDeadlineUtc,
    [ValidateSet("marked", "unmarked")][string]$HostMode = "marked"
)
$ErrorActionPreference = "Stop"
$fields = @("MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress", "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual", "LastModified", "LastModifiedUserID")
$dateFields = @("DOB", "RegisterDate", "ExpiryDate", "LastModified")
$inv = [Globalization.CultureInfo]::InvariantCulture
function Read-FakeState { return ([IO.File]::ReadAllText($StateFile) | ConvertFrom-Json) }
function Save-FakeState($State) { [IO.File]::WriteAllText($StateFile, ($State | ConvertTo-Json -Depth 8 -Compress)) }
function ConvertTo-FakeDate([string]$Text) {
    foreach ($format in @("yyyy-MM-ddTHH:mm:ss.fff", "yyyy-MM-ddTHH:mm:ss", "yyyy-MM-dd", "yyyyMMdd")) {
        $parsed = [datetime]::MinValue
        if ([datetime]::TryParseExact($Text, $format, $inv, [Globalization.DateTimeStyles]::None, [ref]$parsed)) { return $parsed }
    }
    throw "fake date"
}
function New-FakeEntity($Record) {
    $table = New-Object System.Data.DataTable
    foreach ($f in $fields) { [void]$table.Columns.Add($f, [object]) }
    $row = $table.NewRow()
    foreach ($f in $fields) {
        $row[$f] = [DBNull]::Value
        $p = $Record.PSObject.Properties[$f]
        if ($null -ne $p -and $null -ne $p.Value) { $row[$f] = $(if ($dateFields -contains $f) { ConvertTo-FakeDate ([string]$p.Value) } else { $p.Value }) }
    }
    [void]$table.Rows.Add($row)
    $table.AcceptChanges()
    return [pscustomobject]@{ Row = $row }
}
$command = [pscustomobject]@{ Name = "fake" }
$command | Add-Member -MemberType ScriptMethod -Name GetMember -Value {
    param([string]$MemberNo)
    $s = Read-FakeState
    $s.gets = [int]$s.gets + 1
    Save-FakeState $s
    $found = @(@($s.members) | Where-Object { $null -ne $_ -and $_.MemberNo -ceq $MemberNo })
    if ($found.Count -eq 0) { return $null }
    return (New-FakeEntity $found[0])
}
$command | Add-Member -MemberType ScriptMethod -Name SaveMember -Value {
    param($Entity)
    $s = Read-FakeState
    if ($s.save_throws) { throw "vendor failure for Synthetic Member Alpha" }
    $row = $Entity.Row
    $written = @()
    foreach ($f in $fields) {
        $old = $row[$f, [System.Data.DataRowVersion]::Original]
        $new = $row[$f]
        if (-not [object]::Equals($old, $new)) { $written += $f }
    }
    $s.written = @($s.written) + @(,@($written))
    $members = @()
    foreach ($m in @($s.members)) {
        if ($m.MemberNo -ceq [string]$row["MemberNo"]) {
            foreach ($f in $written) {
                $value = $row[$f]
                $m.$f = $(if ($value -is [datetime]) { $value.ToString("yyyy-MM-ddTHH:mm:ss.fff", $inv) } else { [string]$value })
            }
            $m.LastModified = (ConvertTo-FakeDate ([string]$m.LastModified)).AddSeconds(1).AddMilliseconds(7).ToString("yyyy-MM-ddTHH:mm:ss.fff", $inv)
            $m.LastModifiedUserID = "FAKEUSER"
            if ($s.save_mutates) { $m.($s.save_mutates) = "changed by the fake" }
            if ($s.save_wrong_expiry) { $m.ExpiryDate = "2030-01-01T00:00:00.000" }
            if ($s.save_wrong_type) { $m.MemberType = "Default" }
        }
        $members += $m
    }
    $s.members = $members
    $s.saves = [int]$s.saves + 1
    Save-FakeState $s
    if ([int]$s.hang_after_save_ms -gt 0) { Start-Sleep -Milliseconds ([int]$s.hang_after_save_ms) }
}
$db = [pscustomobject]@{ Name = "fake-db" }
$db | Add-Member -MemberType ScriptMethod -Name ExecuteScalar -Value {
    param([string]$Sql, [object[]]$Params)
    $s = Read-FakeState
    $s.sql = @($s.sql) + @($Sql)
    $s.params = @($s.params) + @(,(@($Params) | ForEach-Object { [string]$_ }))
    Save-FakeState $s
    if ($s.db_throws) { throw "vendor sql failure" }
    if ($Sql -like "*FROM MemberType WHERE*") { return [long]@($s.member_types | Where-Object { [string]::Equals([string]$_, [string]$Params[0], [StringComparison]::Ordinal) }).Count }
    if ($Sql -like "*GETDATE()*") { return [string]$s.server_now }
    if ($Sql -like "*FROM SyncRegistration*") { return $null }
    if ($Sql -like "*FROM ChangeCount*") { return [long]0 }
    if ($Sql -like "*AND (ExpiryDate < ? OR ExpiryDate IS NULL OR ExpiryDate < ?)*") {
        $day = ConvertTo-FakeDate ([string]$Params[2]); $floor = ConvertTo-FakeDate ([string]$Params[3])
        $rows = @(@($s.members) | Where-Object { $null -ne $_ -and [string]$_.MemberType -ceq [string]$Params[0] -and [string]::CompareOrdinal([string]$_.MemberNo, [string]$Params[1]) -gt 0 -and
            ($null -eq $_.ExpiryDate -or (ConvertTo-FakeDate ([string]$_.ExpiryDate)) -lt $day -or (ConvertTo-FakeDate ([string]$_.ExpiryDate)) -lt $floor) } |
            Sort-Object -Property @{ Expression = { [string]$_.MemberNo } } | Select-Object -First 500)
        if ($rows.Count -eq 0) { return $null }
        $out = @(); foreach ($m in $rows) { $out += [ordered]@{ member_no = $m.MemberNo; is_active = $m.IsActive; expiry_local = $m.ExpiryDate; last_modified_local = $m.LastModified } }
        return (ConvertTo-Json -InputObject @($out) -Compress)
    }
    throw "unexpected sql"
}
$factory = { param($Session) return $command }.GetNewClosure()
$marked = ($HostMode -ne "unmarked")
$sessionFactory = { [pscustomobject]@{ UserSession = "fake"; DBSetting = $db; MemberCommandFactory = $factory; XbDirectLocalSyntheticFake = $marked } }.GetNewClosure()
& $Bridge -Operation $Operation -EnableProductionAdapter:$EnableProductionAdapter -FenceId $FenceId -ClaimDirectory $ClaimDirectory -WriteDeadlineUtc $WriteDeadlineUtc -SessionFactory $sessionFactory
exit $LASTEXITCODE
"""


def fake_no(n: int) -> str:
    """A fake 9-digit member number (never a real one)."""
    return f"9{n:08d}"


def member(n: int, *, member_type: str = "MEMBER", active: str = "T", expiry: str | None = YESTERDAY, last_modified: str = LM) -> dict:
    return {
        "MemberNo": fake_no(n), "MemberType": member_type, "Name": f"Fake Member {n}", "MobilePhone": None,
        "EmailAddress": f"fake{n}@example.com", "DOB": None, "RegisterDate": "2026-07-01T00:00:00.000", "ExpiryDate": expiry,
        "OpeningPoints": "0", "IsActive": active, "Individual": "T", "LastModified": last_modified, "LastModifiedUserID": "STAFF",
    }


def payload(n: int, *, kind: str = "expire", from_type: str = "MEMBER", to_type: str = "GUEST", expected: str = LM,
            expected_expiry: str | None = YESTERDAY, run_day: str | None = "default", basis: str | None = "default", **extra) -> dict:
    value = {"kind": kind, "member_no": fake_no(n), "from_type": from_type, "to_type": to_type, "expected_last_modified_local": expected,
             "expected_expiry_local": expected_expiry,
             "run_day_local": (RUN_DAY if kind == "expire" else None) if run_day == "default" else run_day,
             "basis_expiry_local": (expected_expiry if kind == "revoke" else None) if basis == "default" else basis,
             "salt": "c3" * 16}
    value.update(extra)
    return value


def restore_payload(n: int, *, expected: str, expected_expiry: str = YESTERDAY, **extra) -> dict:
    value = {"kind": "restore", "member_no": fake_no(n), "from_type": "GUEST", "to_type": "MEMBER", "expected_last_modified_local": expected,
             "expected_expiry_local": expected_expiry, "removal_id": "rm-" + "1" * 32, "salt": "c3" * 16}
    value.update(extra)
    return value


def dl_denied_pattern() -> str:
    text = GUARD.read_text(encoding="utf-8")
    pattern = re.search(r"^\$script:XbDlDeniedSqlPattern = '(.*)'$", text, re.M).group(1).replace("''", "'")
    return pattern.removeprefix("(?i)")


class StaticTests(unittest.TestCase):
    def test_only_member_type_is_assigned_and_each_write_saves_once(self):
        """Mutation target 3 (ExpiryDate assigned)."""
        text = LIBRARY.read_text(encoding="utf-8")
        self.assertEqual(text.count(".SaveMember("), 2)
        assignments = re.findall(r"Set-XbAutoCountMemberRowValue[^\n]*", text)
        self.assertEqual(assignments, [
            'Set-XbAutoCountMemberRowValue -Row $row -Field "MemberType" -Value $script:XbMxToType',
            'Set-XbAutoCountMemberRowValue -Row $row -Field "MemberType" -Value $script:XbMxFromType',
        ])
        self.assertNotRegex(text, r'\$row\[\s*"[A-Za-z]+"\s*\]\s*=')
        self.assertIn('$script:XbMxFromType = "MEMBER"', text)
        self.assertIn('$script:XbMxToType = "GUEST"', text)
        self.assertIn('$script:XbMxKinds = @("expire", "revoke")', text)
        bridge = BRIDGE.read_text(encoding="utf-8")
        self.assertNotIn(".SaveMember(", bridge)
        self.assertNotIn("NewMember", text + bridge)
        self.assertNotIn("Remove", re.sub(r"#.*", "", text))

    def test_expiry_date_is_inside_the_read_back_digest(self):
        """Mutation target 3 (ExpiryDate added to the read-back exclusions): the
        child reuses the type move's digest, whose exclusions are exactly these."""
        text = TYPE_MOVE_LIBRARY.read_text(encoding="utf-8")
        self.assertIn('$script:XbTmUnchangedExclusions = @("MemberType", "LastModified", "LastModifiedUserID")', text)
        library = LIBRARY.read_text(encoding="utf-8")
        self.assertNotIn("UnchangedExclusions", re.sub(r"#.*", "", library))
        self.assertIn("Get-XbTmFieldsDigest", library)

    def test_sql_text_is_pinned_ascii_and_placeholders_only(self):
        """Mutation target 14 (a pinned SQL text changed, or a third added)."""
        text = LIBRARY.read_text(encoding="utf-8")
        for name, sql in MX_SQL_TEXTS.items():
            with self.subTest(name=name):
                self.assertRegex(sql, r"\A[\x20-\x7E]+\Z")
                self.assertIsNone(re.search(dl_denied_pattern(), sql))
        for path in (LIBRARY, BRIDGE):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.lstrip().startswith("#"):
                    self.assertNotIn("Get-XbTmScalar", line)
                    self.assertNotIn("Get-XbMbScalar", line)
                    self.assertNotIn("Get-XbDlScalar", line)  # the #155 allowlist test pins its callers
                if "Get-XbMxScalar" in line and "-Sql" in line and "function " not in line:
                    self.assertRegex(line, r"-Sql \$script:XbMxSqlText\.expiring_members_page[ )]")
        self.assertEqual(text.count("ExecuteScalar("), 1)

    def test_child_codes_are_allowlisted(self):
        found = set()
        for path in (LIBRARY, BRIDGE):
            found |= set(re.findall(r'throw\s+"([a-z0-9_]{1,64})"', path.read_text(encoding="utf-8")))
        self.assertLessEqual(found, mr.CHILD_ERROR_CODES)
        skips = set(re.findall(r'code = "([a-z_]+)"', LIBRARY.read_text(encoding="utf-8")))
        self.assertEqual(skips, mr.REMOVAL_SKIP_CODES | mr.RESTORE_SKIP_CODES)

    def test_other_entry_points_do_not_offer_the_removal_operations(self):
        for name in ("ac2_member_type_move_bridge.ps1", "ac2_member_type_move.ps1", "ac2_directlocal_member_bridge.ps1",
                     "ac2_member_benefits_bridge.ps1", "ac2_member_benefits.ps1"):
            text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotIn("removal", text)
            self.assertNotIn("benefit_x1", text)

    def test_sources_stay_ascii(self):
        for path in (LIBRARY, BRIDGE, GUARD, ROOT / "scripts" / "install_member_removals_task.ps1", Path(__file__)):
            path.read_bytes().decode("ascii")


MX_GUARD_HARNESS = r"""
$ErrorActionPreference = "Stop"
. $args[0]
. $args[1]
. $args[2]
. $args[3]
$doc = ConvertFrom-Json -InputObject (Get-Content -LiteralPath $args[4] -Raw -Encoding UTF8)
function Try-Code([scriptblock]$Block) { try { [void](& $Block); return "ok" } catch { return [string]$_.Exception.Message } }
$db = [pscustomobject]@{ Calls = 0 }
$db | Add-Member -MemberType ScriptMethod -Name ExecuteScalar -Value { param([string]$Sql, [object[]]$Params) $this.Calls++; return "x" }
$results = @()
foreach ($c in @($doc.cases)) {
    switch ([string]$c.kind) {
        "book" { $results += Try-Code { Assert-XbDlAuthorisedBook -Book $c.book -Operation $c.operation -PayloadProfile $c.profile } }
        "book_not_yet" { $results += Try-Code { Assert-XbDlAuthorisedBook -Book $c.book -Operation $c.operation -ProfileNotYetRead } }
        "sql" {
            $params = @(); foreach ($p in @($c.params)) { if ($null -ne $p) { $params += [string]$p } }
            $before = $db.Calls
            $code = Try-Code { Get-XbMxScalar -DBSetting $db -Sql ([string]$c.sql) -Params $params }
            $results += "$code|$($db.Calls -gt $before)"
        }
        default { $results += "unknown_case" }
    }
}
$texts = Get-Variable -Name XbMxSqlText -Scope Script -ValueOnly
[Console]::Out.WriteLine((ConvertTo-Json -InputObject ([ordered]@{ results = @($results); texts = $texts }) -Compress -Depth 4))
"""


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-removals-guard-"))
        self.addCleanup(shutil.rmtree, self.temp, True)

    def run_cases(self, cases):
        (self.temp / "harness.ps1").write_text(MX_GUARD_HARNESS, encoding="utf-8")
        (self.temp / "cases.json").write_text(json.dumps({"cases": cases}), encoding="utf-8")
        proc = subprocess.run([powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.temp / "harness.ps1"),
                               str(ADAPTER), str(GUARD), str(TYPE_MOVE_LIBRARY), str(LIBRARY), str(self.temp / "cases.json")],
                              capture_output=True, timeout=180, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[-2000:])
        out = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
        results = out["results"] if isinstance(out["results"], list) else [out["results"]]
        self.assertEqual(len(results), len(cases))
        return results, out["texts"]

    def test_live_book_admits_each_removal_operation_only_with_its_own_profile(self):
        """Mutation target 13 (guard admits removal-* with benefit_p1, benefit-save with benefit_x1, or a lookalike)."""
        cases, expected = [], []

        def add(case, want):
            cases.append({"kind": "book", **case})
            expected.append(want)
        for op, own in (("removal-scan", "benefit_x1"), ("removal-member", "benefit_x1"), ("removal-save", "benefit_x1"), ("removal-restore", "benefit_x1r")):
            add({"book": LIVE_BOOK, "operation": op, "profile": own}, "ok")
            for profile in (None, "", "sync_p1", "type_move_p1", "benefit_p1", "BENEFIT_X1", own + " ", own + "x",
                            "benefit_x1r" if own == "benefit_x1" else "benefit_x1"):
                add({"book": LIVE_BOOK, "operation": op, "profile": profile}, "production_book_not_authorised")
            add({"book": TEST_BOOK, "operation": op, "profile": own}, "ok")
            cases.append({"kind": "book_not_yet", "book": LIVE_BOOK, "operation": op})
            expected.append("production_book_not_authorised")
        for op in ("benefit-save", "benefit-member", "benefit-scan", "write-once", "type-move-save", "Removal-Save", "removal-delete", "removal_save"):
            for profile in ("benefit_x1", "benefit_x1r"):
                add({"book": LIVE_BOOK, "operation": op, "profile": profile}, "production_book_not_authorised")
        add({"book": LIVE_BOOK, "operation": "benefit-save", "profile": "benefit_p1"}, "ok")  # unchanged
        add({"book": LIVE_BOOK, "operation": "type-move-save", "profile": "type_move_p1"}, "ok")
        add({"book": LIVE_BOOK, "operation": "write-once", "profile": "sync_p1"}, "ok")
        add({"book": "aed_xboundaries", "operation": "removal-save", "profile": "benefit_x1"}, "target_book_not_authorised")
        results, _ = self.run_cases(cases)
        for case, got, want in zip(cases, results, expected):
            with self.subTest(case=case):
                self.assertEqual(got, want)

    def test_only_the_pinned_text_reaches_execute_scalar(self):
        page = MX_SQL_TEXTS["expiring_members_page"]
        cases = [
            {"kind": "sql", "sql": page, "params": ["MEMBER", "", "20261124", "20200101"]},
            {"kind": "sql", "sql": page, "params": ["MEMBER", ""]},
            {"kind": "sql", "sql": page.replace("TOP 500", "TOP 5000"), "params": ["MEMBER", "", "20261124", "20200101"]},
            {"kind": "sql", "sql": page.replace("ExpiryDate < ? OR", "ExpiryDate <= ? OR"), "params": ["MEMBER", "", "20261124", "20200101"]},
            {"kind": "sql", "sql": "SELECT DB_NAME()", "params": []},
            {"kind": "sql", "sql": "UPDATE Member SET MemberType = ? WHERE MemberNo = ?", "params": ["x", "y"]},
            {"kind": "sql", "sql": page.replace("Member ", "Mem" + chr(0xAD) + "ber "), "params": ["MEMBER", "", "20261124", "20200101"]},
        ]
        results, texts = self.run_cases(cases)
        self.assertEqual(results, ["ok|True", "vendor_query_not_allowlisted|False", "vendor_query_not_allowlisted|False", "vendor_query_not_allowlisted|False",
                                   "vendor_query_not_allowlisted|False", "vendor_query_not_read_only|False", "vendor_query_not_ascii|False"])
        self.assertEqual(texts, MX_SQL_TEXTS)


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class RemovalsBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-removals-bridge-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.host = self.temp / "fake_removals_host.ps1"
        self.host.write_text(MX_FAKE_HOST, encoding="utf-8")
        self.state = self.temp / "fake_autocount.json"
        self.claims = self.temp / "claims"
        self.claims.mkdir()
        self.set_state()

    def set_state(self, **overrides):
        state = {"members": [], "saves": 0, "gets": 0, "written": [], "sql": [], "params": [], "db_throws": False, "save_throws": False,
                 "save_mutates": None, "save_wrong_expiry": False, "save_wrong_type": False, "hang_after_save_ms": 0,
                 "member_types": ["Default", "MEMBER", "GUEST"], "server_now": SERVER_NOW}
        state.update(overrides)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def fake(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8-sig"))

    def command(self, host_mode: str = "marked") -> list[str]:
        return [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.host),
                "-Bridge", str(BRIDGE), "-StateFile", str(self.state), "-HostMode", host_mode]

    def run_bridge(self, operation: str, stdin, *extra: str, host_mode: str = "marked"):
        args = self.command(host_mode) + ["-Operation", operation, "-EnableProductionAdapter"] + list(extra)
        body = stdin if isinstance(stdin, str) else json.dumps(stdin)
        proc = subprocess.run(args, input=(body + "\n").encode("ascii"), capture_output=True, timeout=120, check=False)
        out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        return proc.returncode, (json.loads(out[-1]) if out else None), proc

    @staticmethod
    def save_request(fence: str, value: dict) -> dict:
        text = dl.canonical_payload(value)
        salt = "5a" * 16
        return {"fence_id": fence, "payload_canonical": text, "payload_digest": dl.payload_digest(salt, text), "digest_salt": salt}

    def save_args(self, fence: str, seconds: int = 60) -> list[str]:
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return ["-FenceId", fence, "-ClaimDirectory", str(self.claims), "-WriteDeadlineUtc", deadline]

    def save(self, index: int, value: dict, operation: str = "removal-save"):
        fence = "fence-" + f"{index:032x}"
        return (*self.run_bridge(operation, self.save_request(fence, value), *self.save_args(fence)), fence)

    def client(self, deadline_seconds: int = 60) -> mr.RemovalsBridgeClient:
        return mr.RemovalsBridgeClient(self.command(), autocount_book=None, claims=self.claims, deadline_seconds=deadline_seconds)

    def test_expire_and_revoke_write_member_type_only_and_read_back(self):
        basis = "2028-11-18T00:00:00.000"
        self.set_state(members=[member(1), member(2, expiry=basis)])
        for n, value in ((1, payload(1)), (2, payload(2, kind="revoke", expected_expiry=basis))):
            with self.subTest(n=n):
                code, out, _, fence = self.save(n, value)
                self.assertEqual(code, 0, out)
                self.assertEqual({k: out[k] for k in ("status", "save_invocation_count", "readback_found", "readback_type_match", "readback_expiry_match",
                                                      "fields_unchanged", "outbound_queue_changed")},
                                 {"status": "saved", "save_invocation_count": 1, "readback_found": True, "readback_type_match": True,
                                  "readback_expiry_match": True, "fields_unchanged": True, "outbound_queue_changed": False})
                self.assertEqual(out["post_last_modified_local"], "2026-10-01T09:00:01.007")
                self.assertTrue((self.claims / f"{fence}.claim").exists())
                self.assertEqual(mr.classify_save(dl.WriteRun(started=True, exit_code=code, stdout=json.dumps(out)), claim_exists=True).state, "DONE")
        state = self.fake()
        self.assertEqual(state["written"], [["MemberType"], ["MemberType"]])
        self.assertEqual([(m["MemberType"], m["ExpiryDate"]) for m in state["members"]], [("GUEST", YESTERDAY), ("GUEST", basis)])

    def test_boundary_refuses_every_other_move_before_any_session(self):
        """Mutation targets 1, 2, 5 and 6 at the write boundary."""
        self.set_state(members=[member(1), member(2, member_type="GUEST")], member_types=["Default", "MEMBER", "GUEST", "VIP"])
        cases = [
            (payload(1, to_type="MEMBER"), "removal_type_not_allowed"),
            (payload(1, to_type="Default"), "removal_type_not_allowed"),
            (payload(1, to_type="guest"), "removal_type_not_allowed"),
            (payload(1, to_type="GUEST "), "removal_type_not_allowed"),
            (payload(2, from_type="GUEST"), "removal_type_not_allowed"),
            (payload(1, from_type="Default"), "removal_type_not_allowed"),
            (payload(1, from_type="VIP"), "removal_type_not_allowed"),
            (payload(1, expected_expiry="2026-11-24T00:00:00.000"), "removal_not_due"),  # expiry today
            (payload(1, expected_expiry="2026-11-25T00:00:00.000"), "removal_not_due"),
            (payload(1, expected_expiry="2000-01-01T00:00:00.000"), "removal_not_due"),  # placeholder
            (payload(1, expected_expiry="2019-12-31T00:00:00.000"), "removal_not_due"),
            (payload(1, expected_expiry=None), "removal_request_invalid"),
            (payload(1, run_day=None), "removal_request_invalid"),
            (payload(1, run_day="24/11/2026"), "removal_request_invalid"),
            (payload(1, basis=YESTERDAY), "removal_request_invalid"),
            (payload(1, kind="revoke", basis="2028-01-01T00:00:00.000"), "removal_request_invalid"),
            (payload(1, kind="revoke", run_day=RUN_DAY), "removal_request_invalid"),
            (payload(1, kind="Expire"), "removal_request_invalid"),
            (payload(1, kind="restore"), "removal_request_invalid"),
            (payload(1, MemberType="GUEST"), "removal_request_invalid"),
            (payload(1, ExpiryDate="2030-01-01T00:00:00.000"), "removal_request_invalid"),
        ]
        for index, (value, error) in enumerate(cases):
            with self.subTest(value=value):
                code, out, _, fence = self.save(index + 1, value)
                self.assertEqual((code, out), (1, {"error": error}))
                self.assertFalse((self.claims / f"{fence}.claim").exists())
        state = self.fake()
        self.assertEqual((state["saves"], state["gets"], state["sql"]), (0, 0, []))

    def test_pre_check_skips_are_proven_not_written(self):
        """Mutation target 4 (LastModified or expiry pre-check removed)."""
        self.set_state(members=[
            member(1, member_type="GUEST"),  # already removed
            member(2, member_type="Default"),
            member(3, last_modified="2026-11-24T05:00:00.123"),  # edited since the read
            member(4, expiry="2026-11-20T00:00:00.000"),  # expiry changed since the read
            member(6, expiry=None),
        ])
        cases = [(payload(1), "type_changed_since_read"), (payload(2), "type_not_in_scope_now"), (payload(3), "changed_since_read"),
                 (payload(4), "changed_since_read"), (payload(5), "member_not_found"), (payload(6), "changed_since_read")]
        for index, (value, skip) in enumerate(cases):
            with self.subTest(skip=skip):
                code, out, _, fence = self.save(index + 1, value)
                self.assertEqual((code, out), (0, {"status": "skipped", "code": skip}))
                self.assertFalse((self.claims / f"{fence}.claim").exists())
        self.assertEqual(self.fake()["saves"], 0)

    def test_both_types_must_exist_before_any_write(self):
        self.set_state(members=[member(1)], member_types=["Default", "MEMBER"])
        code, out, _, fence = self.save(1, payload(1))
        self.assertEqual((code, out), (1, {"error": "member_type_unknown"}))
        self.assertFalse((self.claims / f"{fence}.claim").exists())

    def test_read_back_mismatches_are_held_by_column_name_only(self):
        self.set_state(members=[member(1)], save_wrong_expiry=True)
        code, out, proc, _ = self.save(1, payload(1))
        self.assertEqual((out["readback_type_match"], out["readback_expiry_match"], out["fields_unchanged"]), (True, False, False))
        changed = out["changed_fields"] if isinstance(out["changed_fields"], list) else [out["changed_fields"]]
        self.assertEqual(changed, ["ExpiryDate"])
        self.assertEqual(mr.classify_save(dl.WriteRun(started=True, exit_code=code, stdout=proc.stdout.decode()), claim_exists=True).code, "readback_expiry_changed")
        self.set_state(members=[member(2)], save_mutates="Name")
        code, out, proc, _ = self.save(2, payload(2))
        self.assertEqual((out["readback_expiry_match"], out["fields_unchanged"]), (True, False))
        self.assertNotIn(b"changed by the fake", proc.stdout)
        self.set_state(members=[member(3)], save_wrong_type=True)
        code, out, proc, _ = self.save(3, payload(3))
        self.assertEqual(mr.classify_save(dl.WriteRun(started=True, exit_code=code, stdout=proc.stdout.decode()), claim_exists=True).code, "readback_type_mismatch")

    def test_restore_is_bound_to_the_removed_state(self):
        """Mutation target 12 (restore skips the changed_since_removal check)."""
        post = "2026-11-24T12:00:01.007"
        self.set_state(members=[member(1, member_type="GUEST", last_modified=post), member(2, member_type="GUEST", last_modified="2026-11-24T13:00:00.000"),
                                member(3, member_type="MEMBER", last_modified=post), member(4, member_type="GUEST", last_modified=post, expiry=None)])
        code, out, _, _ = self.save(1, restore_payload(1, expected=post), "removal-restore")
        self.assertEqual((code, out["readback_type_match"], out["readback_expiry_match"], out["fields_unchanged"]), (0, True, True, True))
        for n in (2, 3, 4):
            with self.subTest(n=n):
                code, out, _, fence = self.save(n, restore_payload(n, expected=post), "removal-restore")
                self.assertEqual((code, out), (0, {"status": "skipped", "code": "changed_since_removal"}))
                self.assertFalse((self.claims / f"{fence}.claim").exists())
        for index, (value, error) in enumerate([
                (restore_payload(1, expected=post, to_type="GUEST"), "removal_type_not_allowed"),
                (restore_payload(1, expected=post, from_type="MEMBER"), "removal_type_not_allowed"),
                (restore_payload(1, expected=post, removal_id="rs-" + "1" * 32), "removal_request_invalid"),
                (restore_payload(1, expected=post, kind="expire"), "removal_request_invalid"),
                (payload(1), "removal_request_invalid")], start=10):
            with self.subTest(error=error):
                self.assertEqual(self.save(index, value, "removal-restore")[:2], (1, {"error": error}))
        state = self.fake()
        self.assertEqual(state["written"], [["MemberType"]])
        self.assertEqual(state["members"][0]["MemberType"], "MEMBER")

    def test_fence_binding_and_payload_digest_are_checked_before_any_session(self):
        self.set_state(members=[member(1)])
        fence = "fence-" + "a" * 32
        request = self.save_request(fence, payload(1))
        cases = [
            (dict(request, fence_id="fence-" + "b" * 32), "fence_binding_mismatch"),
            (dict(request, payload_digest="sha256:" + "0" * 64), "payload_digest_mismatch"),
            (dict(request, payload_canonical=request["payload_canonical"].replace("GUEST", "Default")), "payload_digest_mismatch"),
        ]
        for stdin, error in cases:
            with self.subTest(error=error):
                self.assertEqual(self.run_bridge("removal-save", stdin, *self.save_args(fence))[:2], (1, {"error": error}))
        state = self.fake()
        self.assertEqual((state["saves"], state["gets"], state["sql"]), (0, 0, []))

    def test_without_the_adapter_switch_nothing_runs(self):
        self.set_state(members=[member(1)])
        fence = "fence-" + "c" * 32
        for operation, stdin, extra in (("removal-save", self.save_request(fence, payload(1)), self.save_args(fence)),
                                        ("removal-member", {"member_no": fake_no(1), "salt": "1" * 32}, []),
                                        ("removal-scan", {"after_member_no": "", "run_day_local": RUN_DAY}, [])):
            with self.subTest(operation=operation):
                args = self.command() + ["-Operation", operation] + list(extra)
                proc = subprocess.run(args, input=(json.dumps(stdin) + "\n").encode("ascii"), capture_output=True, timeout=120, check=False)
                lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
                self.assertEqual((proc.returncode, json.loads(lines[-1])), (1, {"error": "autocount_adapter_disabled"}))
        state = self.fake()
        self.assertEqual((state["saves"], state["gets"], state["sql"]), (0, 0, []))

    def test_save_exception_after_the_claim_is_uncertain_and_vendor_text_never_leaves(self):
        self.set_state(members=[member(1)], save_throws=True)
        code, out, proc, fence = self.save(1, payload(1))
        self.assertEqual((code, out), (1, {"error": "bridge_failed"}))
        self.assertNotIn(b"Synthetic Member Alpha", proc.stdout)
        self.assertEqual(mr.classify_save(dl.WriteRun(started=True, exit_code=code, stdout=proc.stdout.decode()), claim_exists=True).state, "UNCERTAIN")

    def test_existing_claim_exits_3_without_a_save(self):
        self.set_state(members=[member(1)])
        fence = "fence-" + "8" * 32
        (self.claims / f"{fence}.claim").write_text("{}", encoding="ascii")
        code, out, _ = self.run_bridge("removal-save", self.save_request(fence, payload(1)), *self.save_args(fence))
        self.assertEqual((code, out), (3, {"error": "fence_already_claimed"}))
        self.assertEqual(self.fake()["saves"], 0)

    def test_python_client_round_trip_scan_member_save_restore(self):
        self.set_state(members=[member(1), member(2, expiry=None), member(3, expiry="2028-06-30T00:00:00.000"), member(4, member_type="GUEST"),
                                member(5, expiry="2000-01-01T00:00:00.000")])
        client = self.client()
        rows, server = client.scan("", __import__("datetime").date(2026, 11, 24))
        self.assertEqual(server, SERVER_NOW)
        self.assertEqual([r["member_no"] for r in rows], [fake_no(1), fake_no(2), fake_no(5)])
        state = self.fake()
        scans = [i for i, sql in enumerate(state["sql"]) if sql == MX_SQL_TEXTS["expiring_members_page"]]
        self.assertEqual([state["params"][i] for i in scans], [["MEMBER", "", "20261124", "20200101"]])
        read = client.member(fake_no(1), "1" * 32)
        self.assertEqual((read.member_type, read.is_active, read.expiry_local, read.last_modified_local, read.server_now_local),
                         ("MEMBER", "T", YESTERDAY, LM, SERVER_NOW))
        self.assertEqual(client.member(fake_no(9), "1" * 32), mr.MemberRead(False, server_now_local=SERVER_NOW))
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=60)).replace(microsecond=0)
        self.assertEqual(client.save(payload(1), move_id="rm-" + "1" * 32, deadline=deadline).state, "DONE")
        after = client.member(fake_no(1), "1" * 32)
        restore = restore_payload(1, expected=after.last_modified_local)
        self.assertEqual(client.restore(restore, move_id="rs-" + "2" * 32, deadline=deadline).state, "DONE")
        self.assertEqual(client.member(fake_no(1), "1" * 32).member_type, "MEMBER")
        with self.assertRaises(dl.DirectLocalError):
            client.save(payload(1, to_type="MEMBER"), move_id="rm-" + "3" * 32, deadline=deadline)  # Python refuses first

    def test_kill_after_save_resolves_by_read_back(self):
        self.set_state(members=[member(3)], hang_after_save_ms=60000)
        slow = self.client(deadline_seconds=30)
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).replace(microsecond=0)
        outcome = slow.save(payload(3, salt="d4" * 16), move_id="rm-" + "f" * 32, deadline=deadline)
        self.assertEqual(outcome.state, "UNCERTAIN")
        claim = json.loads((self.claims / ("fence-" + "f" * 32 + ".claim")).read_text(encoding="ascii"))
        now = slow.member(fake_no(3), "d4" * 16)
        self.assertEqual((now.member_type, now.expiry_local, now.fields_digest), ("GUEST", YESTERDAY, claim["before_digest"]))
        self.assertEqual(self.fake()["saves"], 1)

    def test_scan_and_read_refusals(self):
        self.set_state(members=[member(1)])
        for request, error in (({"after_member_no": "", "run_day_local": "24/11/2026"}, "removal_request_invalid"),
                               ({"after_member_no": None, "run_day_local": RUN_DAY}, "removal_request_invalid"),
                               ({"after_member_no": "", "run_day_local": RUN_DAY, "member_type": "GUEST"}, "removal_request_invalid")):
            with self.subTest(request=request):
                self.assertEqual(self.run_bridge("removal-scan", request)[:2], (1, {"error": error}))
        self.assertEqual(self.run_bridge("removal-member", {"member_no": "bad no", "salt": "1" * 32})[:2], (1, {"error": "member_no_invalid"}))
        self.assertEqual(self.run_bridge("removal-member", {"member_no": fake_no(1), "salt": "x"})[:2], (1, {"error": "removal_request_invalid"}))
        self.assertEqual(self.run_bridge("removal-member", {"member_no": fake_no(1), "salt": "1" * 32}, host_mode="unmarked")[:2],
                         (1, {"error": "synthetic_session_marker_missing"}))


def run_direct_offline(operation: str, stdin: dict, *extra: str, temp: Path, host: str = "XB-CI-NOT-THE-VM", switch: bool = True):
    """The real entry point, never able to log in: COMPUTERNAME is not the
    pinned host (or -AcRoot holds no assemblies). No credential env."""

    import os

    ac_root = temp / "no-autocount"
    ac_root.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("AC2_PROBE_USER_ID", "AC2_PROBE_PASSWORD", "XB_AC2_USER_ID", "XB_AC2_PASSWORD_ENV_VAR", "XB_AC2_SESSION_FACTORY", "XB_AC2_SYNC_PASSWORD")}
    env["COMPUTERNAME"] = host
    command = [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(BRIDGE),
               "-Operation", operation, *(["-EnableProductionAdapter"] if switch else []), "-AcRoot", str(ac_root), *extra]
    proc = subprocess.run(command, input=(json.dumps(stdin) + "\n").encode("ascii"), capture_output=True, timeout=120, check=False, env=env)
    lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    return proc.returncode, (json.loads(lines[-1]) if lines else None)


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class RealEntryOfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-removals-real-"))
        self.addCleanup(shutil.rmtree, self.temp, True)

    def test_without_a_session_source_nothing_runs(self):
        self.assertEqual(run_direct_offline("removal-member", {}, temp=self.temp), (1, {"error": "autocount_real_bridge_disabled"}))
        self.assertEqual(run_direct_offline("removal-member", {}, "-TargetBook", "AED_OTHER", temp=self.temp), (1, {"error": "target_book_not_authorised"}))

    def test_the_real_entry_is_inert_without_the_adapter_switch(self):
        request = {"member_no": fake_no(1), "salt": "1" * 32}
        self.assertEqual(run_direct_offline("removal-member", request, "-TargetBook", LIVE_BOOK, temp=self.temp, host=PINNED_HOST, switch=False),
                         (1, {"error": "autocount_adapter_disabled"}))

    def test_live_book_reaches_the_host_and_assembly_pins_only(self):
        request = {"member_no": fake_no(1), "salt": "1" * 32}
        self.assertEqual(run_direct_offline("removal-member", request, "-TargetBook", LIVE_BOOK, temp=self.temp), (1, {"error": "host_not_authorised"}))
        self.assertEqual(run_direct_offline("removal-member", request, "-TargetBook", LIVE_BOOK, temp=self.temp, host=PINNED_HOST),
                         (1, {"error": "autocount_build_not_qualified"}))


INSTALLER = ROOT / "scripts" / "install_member_removals_task.ps1"
TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


@unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell 5.1 is not installed")
class InstallerTests(unittest.TestCase):
    """Windows PowerShell 5.1, dry-run behaviour only (the default: no
    -Register). Nothing is registered; Register-ScheduledTask is never reached
    and no stand-in is used (PS 5.1 module-shadow pitfall)."""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-removals-installer-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.core = self.temp / "member_benefits.json"
        self.core.write_text((ROOT / "config" / "member_benefits.example.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.config = self.temp / "member_removals.json"
        self.write_config()

    def write_config(self, **switches) -> None:
        value = json.loads((ROOT / "config" / "member_removals.example.json").read_text(encoding="utf-8"))
        value["member_core_config"] = str(self.core)
        value.update(switches)
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def run_installer(self, *extra: str, task_name: str = "XB-MemberRemovals-installer-test-never-registered"):
        command = [shutil.which("powershell"), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
                   "-UserId", "EXAMPLE-PC\\example-user", "-ConfigPath", str(self.config), "-PythonExe", sys.executable, "-TaskName", task_name, *extra]
        proc = subprocess.run(command, capture_output=True, timeout=180, check=False)
        return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")

    @staticmethod
    def task_xml(output: str):
        import xml.etree.ElementTree as ElementTree

        text = output.split("Task XML:", 1)[1].split("Nothing was registered.", 1)[0].strip()
        return ElementTree.fromstring(text.replace('encoding="UTF-16"', ""))

    def test_default_is_an_hourly_observe_dry_run(self):
        code, out = self.run_installer()
        self.assertEqual(code, 0, out)
        self.assertIn("Dry run (the default)", out)
        self.assertIn("Nothing was registered.", out)
        ns = TASK_NS
        task = self.task_xml(out)
        trigger = task.find(f"{ns}Triggers/{ns}TimeTrigger")
        self.assertEqual(trigger.findtext(f"{ns}Repetition/{ns}Interval"), "PT1H")
        self.assertTrue(trigger.findtext(f"{ns}StartBoundary").endswith("T00:40:00"))
        settings = task.find(f"{ns}Settings")
        self.assertEqual((settings.findtext(f"{ns}MultipleInstancesPolicy"), settings.findtext(f"{ns}ExecutionTimeLimit"), settings.findtext(f"{ns}Enabled")),
                         ("IgnoreNew", "PT10M", "true"))
        principal = task.find(f"{ns}Principals/{ns}Principal")
        self.assertEqual((principal.findtext(f"{ns}LogonType"), principal.findtext(f"{ns}RunLevel")), ("Password", "LeastPrivilege"))
        self.assertRegex(task.findtext(f"{ns}Actions/{ns}Exec/{ns}Arguments"),
                         r'^"[^"]+\\directlocal\\xb_member_removals\.py" scheduled --config "[^"]+\\member_removals\.json"$')
        source = INSTALLER.read_text(encoding="utf-8")
        dry = source.index("if (-not $Register) {")
        self.assertLess(dry, source.index("$credential = Get-Credential"))
        self.assertLess(dry, source.index("Register-ScheduledTask -TaskName"))
        self.assertEqual(source.count("Register-ScheduledTask -TaskName"), 1)

    def test_apply_needs_a_write_switch(self):
        code, out = self.run_installer("-Mode", "apply")
        self.assertNotEqual(code, 0)
        self.assertIn("expire_write_enabled or revoke_write_enabled true", out)
        self.write_config(expire_write_enabled=True)
        code, out = self.run_installer("-Mode", "apply")
        self.assertEqual(code, 0, out)
        self.assertRegex(self.task_xml(out).findtext(f"{TASK_NS}Actions/{TASK_NS}Exec/{TASK_NS}Arguments"), r'" scheduled --config "[^"]+" --apply$')

    def test_refusals(self):
        cases = [
            (("-LogonType", "S4U"), "LogonType must be Password"),
            (("-Mode", "Apply"), "Mode must be observe or apply"),
            (("-TaskPath", "\\xb\\"), "TaskPath is pinned"),
            (("-OffsetMinutes", "19"), "OffsetMinutes must be"),
            (("-OffsetMinutes", "51"), "OffsetMinutes must be"),
        ]
        for extra, message in cases:
            with self.subTest(extra=extra):
                code, out = self.run_installer(*extra)
                self.assertNotEqual(code, 0)
                self.assertIn(message, out)
                self.assertNotIn("Task XML:", out)
        for name in ("XB-MemberSync", "XB-TypeMove-L5", "XB-MemberBenefits", "XB-MemberTags"):
            with self.subTest(name=name):
                code, out = self.run_installer(task_name=name)
                self.assertNotEqual(code, 0)
                self.assertIn("may not be the #155 sync's", out)


if __name__ == "__main__":
    unittest.main()
