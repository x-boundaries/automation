"""#268 member type move: the PowerShell child under real PowerShell.

Synthetic only: a marked fake host (same marker rule as the #155 bridge
tests) whose MemberCommand updates members in place and records which
columns each SaveMember changed, and whose DBSetting answers the type move's
two pinned reads and the book guard's pinned reads. No AutoCount assembly,
login, account book or network is used. Fake data only (example.com, fake
9-digit numbers).
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
import xb_member_type_move as tm  # noqa: E402
from test_directlocal_bridge_ps import powershell  # noqa: E402

BRIDGE = ROOT / "scripts" / "ac2_member_type_move_bridge.ps1"
LIBRARY = ROOT / "scripts" / "ac2_member_type_move.ps1"
GUARD = ROOT / "scripts" / "ac2_directlocal_book_guard.ps1"
ADAPTER = ROOT / "scripts" / "ac2_member_gateway_autocount_adapter.ps1"
LIVE_BOOK = "AED_XBOUNDARIES"
TEST_BOOK = "AED_TESTINGAPI"
PINNED_HOST = "DESKTOP-4I042L6"

# Pins $script:XbTmSqlText in scripts/ac2_member_type_move.ps1.
TM_SQL_TEXTS = {
    "members_by_type_page": (
        "SELECT (SELECT TOP 500 MemberNo AS member_no, IsActive AS is_active, "
        "CONVERT(varchar(23), ExpiryDate, 126) AS expiry_local, CONVERT(varchar(23), LastModified, 126) AS last_modified_local "
        "FROM Member WHERE MemberType = ? AND MemberNo > ? ORDER BY MemberNo FOR JSON PATH, INCLUDE_NULL_VALUES)"
    ),
    "member_counts_by_type": (
        "SELECT (SELECT MemberType AS member_type, IsActive AS is_active, COUNT_BIG(*) AS members "
        "FROM Member GROUP BY MemberType, IsActive ORDER BY MemberType, IsActive FOR JSON PATH, INCLUDE_NULL_VALUES)"
    ),
}

TM_FAKE_HOST = r"""
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
    foreach ($format in @("yyyy-MM-ddTHH:mm:ss.fff", "yyyy-MM-ddTHH:mm:ss", "yyyy-MM-dd")) {
        $parsed = [datetime]::MinValue
        if ([datetime]::TryParseExact($Text, $format, $inv, [Globalization.DateTimeStyles]::None, [ref]$parsed)) { return $parsed }
    }
    throw "fake date"
}
function ConvertFrom-FakeDate($Value) {
    # As SQL Server CONVERT(..., 126): the fraction only when it is not zero.
    if ($Value.Millisecond -eq 0) { return $Value.ToString("yyyy-MM-ddTHH:mm:ss", $inv) }
    return $Value.ToString("yyyy-MM-ddTHH:mm:ss.fff", $inv)
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
            foreach ($f in $written) { $m.$f = [string]$row[$f] }
            $m.LastModified = (ConvertTo-FakeDate ([string]$m.LastModified)).AddSeconds(1).AddMilliseconds(7).ToString("yyyy-MM-ddTHH:mm:ss.fff", $inv)
            $m.LastModifiedUserID = "FAKEUSER"
            if ($s.save_mutates) { $m.($s.save_mutates) = "changed by the fake" }
        }
        $members += $m
    }
    $s.members = $members
    $s.saves = [int]$s.saves + 1
    $s.change_counter = [int]$s.change_counter + 1
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
    if ($Sql -like "*WHERE MemberType = ? AND MemberNo > ?*") {
        $rows = @(@($s.members) | Where-Object { $null -ne $_ -and [string]$_.MemberType -ceq [string]$Params[0] -and [string]::CompareOrdinal([string]$_.MemberNo, [string]$Params[1]) -gt 0 } |
            Sort-Object -Property @{ Expression = { [string]$_.MemberNo } } | Select-Object -First 500)
        if ($rows.Count -eq 0) { return $null }
        $out = @(); foreach ($m in $rows) {
            $out += [ordered]@{
                member_no = $m.MemberNo; is_active = $m.IsActive
                expiry_local = $(if ($null -eq $m.ExpiryDate) { $null } else { ConvertFrom-FakeDate (ConvertTo-FakeDate ([string]$m.ExpiryDate)) })
                last_modified_local = $(if ($null -eq $m.LastModified) { $null } else { ConvertFrom-FakeDate (ConvertTo-FakeDate ([string]$m.LastModified)) })
            }
        }
        return (ConvertTo-Json -InputObject @($out) -Compress)
    }
    if ($Sql -like "*GROUP BY MemberType, IsActive*") {
        $groups = @(@($s.members) | Group-Object -Property MemberType, IsActive | Sort-Object Name)
        $out = @(); foreach ($g in $groups) { $out += [ordered]@{ member_type = $g.Group[0].MemberType; is_active = $g.Group[0].IsActive; members = $g.Count } }
        return (ConvertTo-Json -InputObject @($out) -Compress)
    }
    if ($Sql -like "*FROM SyncRegistration*") { return (ConvertTo-Json -InputObject @($s.outlets) -Compress) }
    if ($Sql -like "*GETDATE()*") { return [string]$s.server_now }
    if ($Sql -like "*FROM ChangeCount*") { return [long]$s.change_counter }
    throw "unexpected sql"
}
$factory = { param($Session) return $command }.GetNewClosure()
$marked = ($HostMode -ne "unmarked")
$sessionFactory = { [pscustomobject]@{ UserSession = "fake"; DBSetting = $db; MemberCommandFactory = $factory; XbDirectLocalSyntheticFake = $marked } }.GetNewClosure()
& $Bridge -Operation $Operation -EnableProductionAdapter:$EnableProductionAdapter -FenceId $FenceId -ClaimDirectory $ClaimDirectory -WriteDeadlineUtc $WriteDeadlineUtc -SessionFactory $sessionFactory
exit $LASTEXITCODE
"""

SNAPSHOT_AT = "2026-11-06T20:00:00"


def fake_no(n: int) -> str:
    """A fake 9-digit member number (never a real one)."""
    return f"9{n:08d}"


def member(n: int, *, member_type: str = "Default", active: str = "T", expiry: str | None = "2027-06-30T00:00:00", last_modified: str = "2026-10-01T09:00:00.000", **extra) -> dict:
    record = {
        "MemberNo": fake_no(n), "MemberType": member_type, "Name": f"Fake Member {n}", "MobilePhone": None,
        "EmailAddress": f"fake{n}@example.com", "DOB": None, "RegisterDate": "2024-07-01T00:00:00.000", "ExpiryDate": expiry,
        "OpeningPoints": "0", "IsActive": active, "Individual": "T", "LastModified": last_modified, "LastModifiedUserID": "STAFF",
    }
    record.update(extra)
    return record


# The four L2 fixture kinds (amendment 6) and their targets.
FIXTURE_KINDS = {
    "active_future_expiry": ({"active": "T", "expiry": "2027-06-30T00:00:00"}, "MEMBER"),
    "active_past_expiry": ({"active": "T", "expiry": "2025-01-31T00:00:00"}, "GUEST"),
    "active_null_expiry": ({"active": "T", "expiry": None}, "GUEST"),
    "inactive": ({"active": "F", "expiry": "2027-06-30T00:00:00"}, "GUEST"),
}


class StaticTests(unittest.TestCase):
    def test_only_member_type_is_assigned_and_saved_once(self):
        text = LIBRARY.read_text(encoding="utf-8")
        self.assertEqual(text.count(".SaveMember("), 1)
        assignments = re.findall(r"Set-XbAutoCountMemberRowValue[^\n]*", text)
        self.assertEqual(assignments, ['Set-XbAutoCountMemberRowValue -Row $row -Field "MemberType" -Value $to'])
        for field in ("ExpiryDate", "RegisterDate"):
            self.assertNotRegex(text, rf'\$row\[\s*"{field}"\s*\]\s*=')
        self.assertNotIn(".SaveMember(", BRIDGE.read_text(encoding="utf-8"))
        self.assertNotIn("NewMember", text + BRIDGE.read_text(encoding="utf-8"))

    def test_sql_texts_are_pinned_ascii_and_placeholders_only(self):
        text = LIBRARY.read_text(encoding="utf-8")
        for name, sql in TM_SQL_TEXTS.items():
            with self.subTest(name=name):
                self.assertRegex(sql, r"\A[\x20-\x7E]+\Z")
                self.assertNotRegex(sql, r"'[^']*\?[^']*'")
                self.assertIsNone(re.search(dl_denied_pattern(), sql))
        # The scripts never send SQL through the #155 helper or any other text.
        for path in (LIBRARY, BRIDGE):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.lstrip().startswith("#"):
                    self.assertNotIn("Get-XbDlScalar", line)
                if "Get-XbTmScalar" in line and "-Sql" in line and "function " not in line:
                    self.assertRegex(line, r"-Sql \$script:XbTmSqlText\.(members_by_type_page|member_counts_by_type)[ )]")
        self.assertEqual(text.count("ExecuteScalar("), 1)

    def test_types_are_pinned(self):
        text = LIBRARY.read_text(encoding="utf-8")
        self.assertIn('$script:XbTmFromType = "Default"', text)
        self.assertIn('$script:XbTmTargetTypes = @("MEMBER", "GUEST")', text)
        self.assertIn('$script:XbTmScanTypes = @("Default", "MEMBER", "GUEST")', text)
        self.assertEqual((tm.FROM_TYPE, tm.TARGET_TYPES, tm.SCAN_TYPES), ("Default", ("MEMBER", "GUEST"), ("Default", "MEMBER", "GUEST")))

    def test_child_codes_are_allowlisted(self):
        found = set()
        for path in (LIBRARY, BRIDGE):
            found |= set(re.findall(r'throw\s+"([a-z0-9_]{1,64})"', path.read_text(encoding="utf-8")))
        self.assertLessEqual(found, tm.CHILD_ERROR_CODES)
        self.assertLessEqual(tm.TYPE_MOVE_CHILD_CODES, found)
        skips = set(re.findall(r'"((?:not|changed|target|member)_[a-z_]+)"', LIBRARY.read_text(encoding="utf-8")))
        self.assertLessEqual(tm.MOVE_SKIP_CODES | tm.ROLLBACK_SKIP_CODES, skips)

    def test_sources_stay_ascii(self):
        for path in (LIBRARY, BRIDGE, GUARD, Path(__file__)):
            path.read_bytes().decode("ascii")


def dl_denied_pattern() -> str:
    text = GUARD.read_text(encoding="utf-8")
    pattern = re.search(r"^\$script:XbDlDeniedSqlPattern = '(.*)'$", text, re.M).group(1).replace("''", "'")
    return pattern.removeprefix("(?i)")


TM_GUARD_HARNESS = r"""
$ErrorActionPreference = "Stop"
. $args[0]
. $args[1]
. $args[2]
$doc = ConvertFrom-Json -InputObject (Get-Content -LiteralPath $args[3] -Raw -Encoding UTF8)
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
            $code = Try-Code { Get-XbTmScalar -DBSetting $db -Sql ([string]$c.sql) -Params $params }
            $results += "$code|$($db.Calls -gt $before)"
        }
        "target" {
            $expiry = if ($null -eq $c.expiry) { [DBNull]::Value } else { [datetime]::ParseExact([string]$c.expiry, "yyyy-MM-ddTHH:mm:ss", [Globalization.CultureInfo]::InvariantCulture) }
            $active = if ($null -eq $c.active) { [DBNull]::Value } else { [string]$c.active }
            $at = [datetime]::ParseExact([string]$c.at, "yyyy-MM-ddTHH:mm:ss", [Globalization.CultureInfo]::InvariantCulture)
            $results += [string](Get-XbTmTarget -IsActive $active -ExpiryDate $expiry -SnapshotAt $at)
        }
        default { $results += "unknown_case" }
    }
}
$texts = Get-Variable -Name XbTmSqlText -Scope Script -ValueOnly
[Console]::Out.WriteLine((ConvertTo-Json -InputObject ([ordered]@{ results = @($results); texts = $texts }) -Compress -Depth 4))
"""


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class GuardAndRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-type-move-guard-"))
        self.addCleanup(shutil.rmtree, self.temp, True)

    def run_cases(self, cases):
        (self.temp / "harness.ps1").write_text(TM_GUARD_HARNESS, encoding="utf-8")
        (self.temp / "cases.json").write_text(json.dumps({"cases": cases}), encoding="utf-8")
        proc = subprocess.run([powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.temp / "harness.ps1"),
                               str(ADAPTER), str(GUARD), str(LIBRARY), str(self.temp / "cases.json")], capture_output=True, timeout=180, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[-2000:])
        out = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
        results = out["results"] if isinstance(out["results"], list) else [out["results"]]
        self.assertEqual(len(results), len(cases))
        return results, out["texts"]

    def test_live_book_admits_the_type_move_operations_only_with_their_profile(self):
        ops = ("type-move-scan", "type-move-counts", "type-move-member", "type-move-save")
        cases = [{"kind": "book", "book": LIVE_BOOK, "operation": op, "profile": "type_move_p1"} for op in ops]
        refused = [(op, profile) for op in ops for profile in (None, "", "sync_p1", "TYPE_MOVE_P1", "type_move_p1 ", "type_move_p2")]
        cases += [{"kind": "book", "book": LIVE_BOOK, "operation": op, "profile": p} for op, p in refused]
        cases += [{"kind": "book_not_yet", "book": LIVE_BOOK, "operation": op} for op in ops]
        # The sync's operations never take the type move's profile; write-once stays sync_p1 only.
        cases += [{"kind": "book", "book": LIVE_BOOK, "operation": "write-once", "profile": "type_move_p1"}]
        cases += [{"kind": "book", "book": LIVE_BOOK, "operation": op, "profile": "type_move_p1"} for op in ("probe", "reconcile", "next-member-no", "Type-Move-Save", "type-move-delete")]
        # Unchanged: the sync's own rows.
        cases += [{"kind": "book", "book": LIVE_BOOK, "operation": "write-once", "profile": "sync_p1"}, {"kind": "book", "book": LIVE_BOOK, "operation": "sync-lookup", "profile": None}]
        cases += [{"kind": "book", "book": TEST_BOOK, "operation": op, "profile": "type_move_p1"} for op in ops]
        cases += [{"kind": "book", "book": "aed_xboundaries", "operation": "type-move-scan", "profile": "type_move_p1"}]
        expected = ["ok"] * 4 + ["production_book_not_authorised"] * (len(refused) + 4 + 1 + 5) + ["ok", "ok"] + ["ok"] * 4 + ["target_book_not_authorised"]
        results, _ = self.run_cases(cases)
        for case, got, want in zip(cases, results, expected):
            with self.subTest(case=case):
                self.assertEqual(got, want)

    def test_only_the_pinned_texts_reach_execute_scalar(self):
        page, counts = TM_SQL_TEXTS["members_by_type_page"], TM_SQL_TEXTS["member_counts_by_type"]
        cases = [
            {"kind": "sql", "sql": page, "params": ["Default", ""]},
            {"kind": "sql", "sql": counts, "params": []},
            {"kind": "sql", "sql": page, "params": ["Default"]},
            {"kind": "sql", "sql": page.replace("TOP 500", "TOP 5000"), "params": ["Default", ""]},
            {"kind": "sql", "sql": page + " ", "params": ["Default", ""]},
            {"kind": "sql", "sql": "SELECT DB_NAME()", "params": []},  # the guard's texts are not this helper's
            {"kind": "sql", "sql": "UPDATE Member SET MemberType = ? WHERE MemberNo = ?", "params": ["MEMBER", "x"]},
            {"kind": "sql", "sql": counts + "; DELETE FROM Member", "params": []},
            {"kind": "sql", "sql": counts.replace("Member ", "Mem" + chr(0xAD) + "ber "), "params": []},
        ]
        results, texts = self.run_cases(cases)
        self.assertEqual(results, ["ok|True", "ok|True", "vendor_query_not_allowlisted|False", "vendor_query_not_allowlisted|False",
                                   "vendor_query_not_allowlisted|False", "vendor_query_not_allowlisted|False", "vendor_query_not_read_only|False",
                                   "vendor_query_not_read_only|False", "vendor_query_not_ascii|False"])
        self.assertEqual(texts, TM_SQL_TEXTS)

    def test_the_pinned_rule_in_powershell_matches_python(self):
        at = SNAPSHOT_AT
        rows = [
            ("T", "2027-06-30T00:00:00"), ("T", "2025-01-31T00:00:00"), ("T", None), ("F", "2027-06-30T00:00:00"), ("F", None),
            ("T", at), ("T", "2026-11-06T20:00:01"), ("T", "2026-11-06T19:59:59"), ("X", "2027-06-30T00:00:00"), (None, "2027-06-30T00:00:00"),
        ]
        results, _ = self.run_cases([{"kind": "target", "active": a, "expiry": e, "at": at} for a, e in rows])
        snapshot_at = tm.parse_local(at)
        python = [tm.target_for(a, e, snapshot_at)[0] or "" for a, e in rows]
        self.assertEqual(results, python)
        self.assertEqual(results, ["MEMBER", "GUEST", "GUEST", "GUEST", "GUEST", "GUEST", "MEMBER", "GUEST", "", ""])
        self.assertLessEqual(set(results), {"MEMBER", "GUEST", ""})


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class TypeMoveBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-type-move-bridge-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.host = self.temp / "fake_type_move_host.ps1"
        self.host.write_text(TM_FAKE_HOST, encoding="utf-8")
        self.state = self.temp / "fake_autocount.json"
        self.claims = self.temp / "claims"
        self.claims.mkdir()
        self.set_state()

    def set_state(self, **overrides):
        state = {"members": [], "saves": 0, "gets": 0, "written": [], "sql": [], "params": [], "db_throws": False, "save_throws": False,
                 "save_mutates": None, "hang_after_save_ms": 0, "member_types": ["Default", "MEMBER", "GUEST"], "server_now": SNAPSHOT_AT,
                 "change_counter": 100, "outlets": [{"location": "OUT1", "sync_name": "POS", "last_finish_sync_local": "2026-11-06T19:45:00", "last_alive_local": "2026-11-06T19:59:00"}]}
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

    def save_request(self, fence: str, payload: dict) -> dict:
        text = dl.canonical_payload(payload)
        salt = "5a" * 16
        return {"fence_id": fence, "payload_canonical": text, "payload_digest": dl.payload_digest(salt, text), "digest_salt": salt}

    def save_args(self, fence: str, seconds: int = 60) -> list[str]:
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return ["-FenceId", fence, "-ClaimDirectory", str(self.claims), "-WriteDeadlineUtc", deadline]

    @staticmethod
    def move_payload(n: int, to_type: str, *, expected: str = "2026-10-01T09:00:00.000", kind: str = "move", from_type: str = "Default") -> dict:
        return {"kind": kind, "member_no": fake_no(n), "from_type": from_type, "to_type": to_type, "expected_last_modified_local": expected,
                "snapshot_at_local": SNAPSHOT_AT if kind == "move" else None, "salt": "c3" * 16}

    def client(self, deadline_seconds: int = 60) -> tm.TypeMoveBridgeClient:
        return tm.TypeMoveBridgeClient(self.command(), autocount_book=None, claims=self.claims, deadline_seconds=deadline_seconds)

    def test_scan_is_parameterised_and_pages_by_member_no(self):
        self.set_state(members=[member(3), member(1), member(2, member_type="GUEST"), member(4, expiry=None)])
        code, out, _ = self.run_bridge("type-move-scan", {"member_type": "Default", "after_member_no": ""})
        self.assertEqual(code, 0)
        self.assertEqual([r["member_no"] for r in out["members"]], [fake_no(1), fake_no(3), fake_no(4)])
        self.assertEqual(out["members"][0], {"member_no": fake_no(1), "is_active": "T", "expiry_local": "2027-06-30T00:00:00", "last_modified_local": "2026-10-01T09:00:00"})
        self.assertIsNone(out["members"][2]["expiry_local"])
        state = self.fake()
        self.assertEqual(state["sql"], [TM_SQL_TEXTS["members_by_type_page"]])
        self.assertEqual(state["params"][0], ["Default", ""])
        code, out, _ = self.run_bridge("type-move-scan", {"member_type": "Default", "after_member_no": fake_no(3)})
        self.assertEqual([r["member_no"] for r in out["members"]], [fake_no(4)])
        self.assertEqual(self.fake()["saves"], 0)

    def test_scan_refusals(self):
        for request, error in (
            ({"member_type": "VIP", "after_member_no": ""}, "type_move_type_not_allowed"),
            ({"member_type": "default", "after_member_no": ""}, "type_move_type_not_allowed"),
            ({"member_type": "Default", "after_member_no": None}, "type_move_request_invalid"),
            ({"member_type": "Default", "after_member_no": "x" * 41}, "type_move_request_invalid"),
            ({"member_type": "Default", "after_member_no": "", "extra": 1}, "type_move_request_invalid"),
        ):
            with self.subTest(error=error, request=request):
                self.assertEqual(self.run_bridge("type-move-scan", request)[:2], (1, {"error": error}))
        self.set_state(db_throws=True)
        code, out, proc = self.run_bridge("type-move-scan", {"member_type": "Default", "after_member_no": ""})
        self.assertEqual((code, out), (1, {"error": "type_move_scan_failed"}))
        self.assertNotIn(b"vendor sql failure", proc.stdout)
        self.assertEqual(self.run_bridge("type-move-scan", {"member_type": "Default", "after_member_no": ""}, host_mode="unmarked")[:2],
                         (1, {"error": "synthetic_session_marker_missing"}))

    def test_counts_report_types_and_pos_sync(self):
        self.set_state(members=[member(1), member(2, active="F"), member(3, member_type="MEMBER")], member_types=["Default", "MEMBER"])
        code, out, _ = self.run_bridge("type-move-counts", {})
        self.assertEqual(code, 0)
        self.assertEqual(out["types_exist"], {"Default": True, "MEMBER": True, "GUEST": False})
        self.assertEqual(out["server_now_local"], SNAPSHOT_AT)
        self.assertEqual(out["member_change_counter"], 100)
        self.assertEqual(sorted((r["member_type"], r["is_active"], r["members"]) for r in out["counts"]), [("Default", "F", 1), ("Default", "T", 1), ("MEMBER", "T", 1)])
        self.assertEqual(self.fake()["saves"], 0)

    def test_save_moves_each_fixture_kind_to_its_target_and_writes_only_member_type(self):
        members = []
        for index, (kind, (spec, _target)) in enumerate(FIXTURE_KINDS.items(), 1):
            members.append(member(index, active=spec["active"], expiry=spec["expiry"]))
        self.set_state(members=members)
        for index, (kind, (_spec, target)) in enumerate(FIXTURE_KINDS.items(), 1):
            with self.subTest(kind=kind):
                fence = "fence-" + f"{index:x}" * 32
                code, out, _ = self.run_bridge("type-move-save", self.save_request(fence, self.move_payload(index, target)), *self.save_args(fence))
                self.assertEqual(code, 0, out)
                self.assertEqual({k: out[k] for k in ("status", "save_invocation_count", "readback_found", "readback_type_match", "fields_unchanged", "outbound_queue_changed")},
                                 {"status": "saved", "save_invocation_count": 1, "readback_found": True, "readback_type_match": True, "fields_unchanged": True, "outbound_queue_changed": False})
                self.assertEqual(out["post_last_modified_local"], "2026-10-01T09:00:01.007")
                self.assertTrue((self.claims / f"{fence}.claim").exists())
                self.assertTrue((self.claims / f"{fence}.outcome").exists())
                # A move to the other target is refused at the pre-check: target_changed.
        state = self.fake()
        self.assertEqual(state["saves"], 4)
        self.assertEqual(state["written"], [["MemberType"]] * 4)  # ExpiryDate and RegisterDate never written
        after = {m["MemberNo"]: m for m in state["members"]}
        for index, (kind, (spec, target)) in enumerate(FIXTURE_KINDS.items(), 1):
            self.assertEqual(after[fake_no(index)]["MemberType"], target)
            self.assertEqual(after[fake_no(index)]["ExpiryDate"], spec["expiry"])
            self.assertEqual(after[fake_no(index)]["RegisterDate"], "2024-07-01T00:00:00.000")

    def test_pre_check_skips_are_proven_not_written(self):
        self.set_state(members=[
            member(1, member_type="GUEST"),  # moved by staff since the snapshot
            member(2, last_modified="2026-11-06T19:30:00.000"),  # edited after the snapshot
            member(3, expiry="2026-11-06T20:00:00"),  # expiry now at the snapshot time: GUEST, not MEMBER
            member(4, active="F"),  # deactivated after the snapshot: GUEST, not MEMBER
            member(5, expiry=None),  # expiry cleared: GUEST, not MEMBER
        ])
        cases = [(1, "MEMBER", "2026-10-01T09:00:00.000", "not_default_now"), (2, "MEMBER", "2026-10-01T09:00:00.000", "changed_since_snapshot"),
                 (3, "MEMBER", "2026-10-01T09:00:00.000", "target_changed"), (4, "MEMBER", "2026-10-01T09:00:00.000", "target_changed"),
                 (5, "MEMBER", "2026-10-01T09:00:00.000", "target_changed"), (9, "GUEST", "2026-10-01T09:00:00.000", "member_not_found")]
        for n, target, expected, skip in cases:
            with self.subTest(skip=skip, n=n):
                fence = "fence-" + f"{n:x}" * 32
                code, out, _ = self.run_bridge("type-move-save", self.save_request(fence, self.move_payload(n, target, expected=expected)), *self.save_args(fence))
                self.assertEqual((code, out), (0, {"status": "skipped", "code": skip}))
                self.assertFalse((self.claims / f"{fence}.claim").exists())
        self.assertEqual(self.fake()["saves"], 0)

    def test_no_third_target_and_no_other_source_type(self):
        self.set_state(members=[member(1)], member_types=["Default", "MEMBER", "GUEST", "VIP"])
        bad = [
            self.move_payload(1, "VIP"), self.move_payload(1, "Default"), self.move_payload(1, "member"), self.move_payload(1, "GUEST "),
            self.move_payload(1, "MEMBER", from_type="GUEST"), self.move_payload(1, "GUEST", from_type="REGISTERED"),
            self.move_payload(1, "VIP", kind="rollback", from_type="MEMBER"), self.move_payload(1, "Default", kind="rollback", from_type="VIP"),
            self.move_payload(1, "Default", kind="rollback", from_type="Default"),
        ]
        for index, payload in enumerate(bad):
            with self.subTest(payload=payload):
                fence = "fence-" + f"{index:x}" * 32
                self.assertEqual(self.run_bridge("type-move-save", self.save_request(fence, payload), *self.save_args(fence))[:2], (1, {"error": "type_move_type_not_allowed"}))
        unknown_kind = dict(self.move_payload(1, "MEMBER"), kind="delete")
        extra_key = dict(self.move_payload(1, "MEMBER"), ExpiryDate="2030-01-01")
        for index, payload in enumerate((unknown_kind, extra_key)):
            fence = "fence-" + f"{index + 10:x}"[-1] * 32
            self.assertEqual(self.run_bridge("type-move-save", self.save_request(fence, payload), *self.save_args(fence))[:2], (1, {"error": "type_move_request_invalid"}))
        self.assertEqual((self.fake()["saves"], self.fake()["sql"]), (0, []))
        self.assertEqual(list(self.claims.iterdir()), [])

    def test_both_types_must_exist_before_any_write(self):
        self.set_state(members=[member(1)], member_types=["Default", "MEMBER"])
        fence = "fence-" + "e" * 32
        self.assertEqual(self.run_bridge("type-move-save", self.save_request(fence, self.move_payload(1, "GUEST", )), *self.save_args(fence))[:2],
                         (1, {"error": "member_type_unknown"}))
        self.assertEqual(self.fake()["saves"], 0)
        self.assertFalse((self.claims / f"{fence}.claim").exists())

    def test_fence_digest_and_deadline_are_checked_before_any_session(self):
        self.set_state(members=[member(1)])
        fence = "fence-" + "a" * 32
        request = self.save_request(fence, self.move_payload(1, "MEMBER"))
        cases = [
            (dict(request, fence_id="fence-" + "b" * 32), self.save_args(fence), "fence_binding_mismatch"),
            (dict(request, payload_digest="sha256:" + "0" * 64), self.save_args(fence), "payload_digest_mismatch"),
            (request, ["-FenceId", "fence-x", "-ClaimDirectory", str(self.claims), "-WriteDeadlineUtc", self.save_args(fence)[-1]], "fence_id_invalid"),
            (request, self.save_args(fence, seconds=-5), "writer_deadline_expired"),
        ]
        for stdin, args, error in cases:
            with self.subTest(error=error):
                self.assertEqual(self.run_bridge("type-move-save", stdin, *args)[:2], (1, {"error": error}))
        self.assertEqual((self.fake()["saves"], self.fake()["gets"]), (0, 0))

    def test_read_back_mismatch_is_reported_by_column_name_only(self):
        self.set_state(members=[member(1)], save_mutates="Name")
        fence = "fence-" + "d" * 32
        code, out, proc = self.run_bridge("type-move-save", self.save_request(fence, self.move_payload(1, "MEMBER")), *self.save_args(fence))
        self.assertEqual(code, 0)
        self.assertEqual((out["readback_type_match"], out["fields_unchanged"]), (True, False))
        self.assertEqual(out["changed_fields"] if isinstance(out["changed_fields"], list) else [out["changed_fields"]], ["Name"])
        self.assertNotIn(b"changed by the fake", proc.stdout)
        self.assertNotIn(b"Fake Member", proc.stdout)
        self.assertEqual(tm.classify_save(dl.WriteRun(started=True, exit_code=code, stdout=proc.stdout.decode()), claim_exists=True, kind="move").state, "HELD")

    def test_save_exception_after_the_claim_is_uncertain_for_the_parent(self):
        self.set_state(members=[member(1)], save_throws=True)
        fence = "fence-" + "9" * 32
        code, out, proc = self.run_bridge("type-move-save", self.save_request(fence, self.move_payload(1, "MEMBER")), *self.save_args(fence))
        self.assertEqual((code, out), (1, {"error": "bridge_failed"}))
        self.assertNotIn(b"Synthetic Member Alpha", proc.stdout)
        self.assertTrue((self.claims / f"{fence}.claim").exists())
        self.assertEqual(tm.classify_save(dl.WriteRun(started=True, exit_code=code, stdout=proc.stdout.decode()), claim_exists=True, kind="move"),
                         tm.SaveOutcome("UNCERTAIN", "writer_failed_after_claim"))

    def test_existing_claim_exits_3_without_a_save(self):
        self.set_state(members=[member(1)])
        fence = "fence-" + "8" * 32
        (self.claims / f"{fence}.claim").write_text("{}", encoding="ascii")
        self.assertEqual(self.run_bridge("type-move-save", self.save_request(fence, self.move_payload(1, "MEMBER")), *self.save_args(fence))[:2],
                         (3, {"error": "fence_already_claimed"}))
        self.assertEqual(self.fake()["saves"], 0)

    def test_rollback_restores_default_for_both_targets(self):
        self.set_state(members=[member(1), member(2, active="F")])
        client = self.client()
        moved = {}
        for n, target in ((1, "MEMBER"), (2, "GUEST")):
            outcome = client.save(self.move_payload(n, target), move_id="move-" + f"{n}" * 32)
            self.assertEqual(outcome.state, "DONE")
            moved[n] = outcome.post_last_modified_local
        # A rollback that expects the wrong type or an old LastModified is skipped.
        self.assertEqual(client.save(self.move_payload(1, "Default", kind="rollback", from_type="GUEST", expected=moved[1]), move_id="move-" + "3" * 32),
                         tm.SaveOutcome("SKIPPED", "not_moved_type_now"))
        self.assertEqual(client.save(self.move_payload(1, "Default", kind="rollback", from_type="MEMBER", expected="2026-10-01T09:00:00.000"), move_id="move-" + "4" * 32),
                         tm.SaveOutcome("SKIPPED", "changed_since_move"))
        for n, target in ((1, "MEMBER"), (2, "GUEST")):
            outcome = client.save(self.move_payload(n, "Default", kind="rollback", from_type=target, expected=moved[n]), move_id="move-" + f"{n + 4}" * 32)
            self.assertEqual(outcome.state, "DONE")
        state = self.fake()
        self.assertEqual([m["MemberType"] for m in state["members"]], ["Default", "Default"])
        self.assertEqual(state["written"], [["MemberType"]] * 4)
        self.assertEqual(state["saves"], 4)

    def test_kill_after_save_is_resolved_by_read_back_without_a_second_save(self):
        self.set_state(members=[member(1)], hang_after_save_ms=60000)
        client = self.client(deadline_seconds=30)
        intent_salt = "c3" * 16
        outcome = client.save(self.move_payload(1, "MEMBER"), move_id="move-" + "f" * 32)
        self.assertEqual(outcome.state, "UNCERTAIN")
        self.assertEqual(self.fake()["saves"], 1)
        claim = json.loads((self.claims / ("fence-" + "f" * 32 + ".claim")).read_text(encoding="ascii"))
        now = client.member(fake_no(1), intent_salt)
        self.assertEqual((now["found"], now["member_type"], now["fields_digest"]), (True, "MEMBER", claim["before_digest"]))
        self.assertEqual(now["last_modified_local"], "2026-10-01T09:00:01.007")
        self.assertEqual(self.fake()["saves"], 1)

    def test_member_read_refusals_and_not_found(self):
        self.set_state(members=[member(1)])
        self.assertEqual(self.run_bridge("type-move-member", {"member_no": fake_no(7), "salt": "1" * 32})[:2], (0, {"found": False}))
        self.assertEqual(self.run_bridge("type-move-member", {"member_no": "bad no", "salt": "1" * 32})[:2], (1, {"error": "member_no_invalid"}))
        self.assertEqual(self.run_bridge("type-move-member", {"member_no": fake_no(1), "salt": "short"})[:2], (1, {"error": "type_move_request_invalid"}))


def run_direct_offline(operation: str, stdin: dict, *extra: str, temp: Path, host: str = "XB-CI-NOT-THE-VM"):
    """The real entry point, never able to log in: COMPUTERNAME is not the
    pinned host (or -AcRoot holds no assemblies). No credential env."""

    import os

    ac_root = temp / "no-autocount"
    ac_root.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("AC2_PROBE_USER_ID", "AC2_PROBE_PASSWORD", "XB_AC2_USER_ID", "XB_AC2_PASSWORD_ENV_VAR", "XB_AC2_SESSION_FACTORY", "XB_AC2_SYNC_PASSWORD")}
    env["COMPUTERNAME"] = host
    command = [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(BRIDGE),
               "-Operation", operation, "-EnableProductionAdapter", "-AcRoot", str(ac_root), *extra]
    proc = subprocess.run(command, input=(json.dumps(stdin) + "\n").encode("ascii"), capture_output=True, timeout=120, check=False, env=env)
    lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    return proc.returncode, (json.loads(lines[-1]) if lines else None)


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class RealEntryOfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-type-move-real-"))
        self.addCleanup(shutil.rmtree, self.temp, True)

    def test_without_a_session_source_or_switch_nothing_runs(self):
        self.assertEqual(run_direct_offline("type-move-counts", {}, temp=self.temp), (1, {"error": "autocount_real_bridge_disabled"}))
        self.assertEqual(run_direct_offline("type-move-counts", {}, "-TargetBook", "AED_OTHER", temp=self.temp), (1, {"error": "target_book_not_authorised"}))

    def test_live_book_reaches_the_host_and_assembly_pins_only(self):
        request = {"member_type": "Default", "after_member_no": ""}
        self.assertEqual(run_direct_offline("type-move-scan", request, "-TargetBook", LIVE_BOOK, temp=self.temp), (1, {"error": "host_not_authorised"}))
        self.assertEqual(run_direct_offline("type-move-scan", request, "-TargetBook", LIVE_BOOK, temp=self.temp, host=PINNED_HOST),
                         (1, {"error": "autocount_build_not_qualified"}))

    def test_the_sync_bridge_does_not_offer_the_type_move(self):
        sync_bridge = ROOT / "scripts" / "ac2_directlocal_member_bridge.ps1"
        text = sync_bridge.read_text(encoding="utf-8")
        self.assertNotIn("type-move", text)
        self.assertNotIn("type_move", text)


if __name__ == "__main__":
    unittest.main()
