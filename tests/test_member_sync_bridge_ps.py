"""#155 phase 1 sync operations of the DirectLocal bridge under real PowerShell.

Synthetic only: the existing marked fake host (``test_directlocal_bridge_ps``)
gets a fake DBSetting whose ExecuteScalar records the SQL and parameters and
returns canned FOR JSON text. No AutoCount assembly, login, account book or
network is used. Phones are built at runtime from fragments.
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
import xb_member_sync as sync  # noqa: E402
from test_directlocal_bridge_ps import BRIDGE, FAKE_HOST, powershell  # noqa: E402

FAKE_DB = r"""
$db = [pscustomobject]@{ Name = "fake-db" }
$db | Add-Member -MemberType ScriptMethod -Name ExecuteScalar -Value {
    param([string]$Sql, [object[]]$Params)
    $s = Read-FakeState
    $s.sql = @($s.sql) + @($Sql)
    $s.params = @($s.params) + @(,(@($Params) | ForEach-Object { [string]$_ }))
    Save-FakeState $s
    if ($s.db_throws) { throw "vendor sql failure" }
    if ($Sql -like "*MAX(Id)*") { return [long]$s.max_id }
    if ($Sql -like "*FROM ChangeLog*") { return [string]$s.changelog_json }
    if ($Sql -like "*FROM Member*") { return [string]$s.lookup_json }
    throw "unexpected sql"
}
"""
SYNC_HOST = FAKE_HOST.replace("$factory = { param($Session) return $command }.GetNewClosure()", FAKE_DB + "$factory = { param($Session) return $command }.GetNewClosure()", 1).replace('DBSetting = "fake"', "DBSetting = $db", 1)


def sg(n: int) -> str:
    return "65" + "8000" + f"{n:04d}"


class SyncScriptStaticTests(unittest.TestCase):
    def test_sync_create_is_separate_from_the_pinned_adapter(self):
        sync_script = (ROOT / "scripts" / "ac2_member_sync_member.ps1").read_text(encoding="utf-8")
        adapter = (ROOT / "scripts" / "ac2_member_gateway_autocount_adapter.ps1").read_text(encoding="utf-8")
        bridge = BRIDGE.read_text(encoding="utf-8")
        self.assertEqual(sync_script.count(".SaveMember("), 1)
        self.assertNotIn("sync_p1", adapter)
        self.assertNotIn("REGISTERED", adapter)
        self.assertIn('. (Join-Path $PSScriptRoot "ac2_member_sync_member.ps1")', bridge)


SQL_SCRIPTS = ("ac2_directlocal_member_bridge.ps1", "ac2_directlocal_book_guard.ps1", "ac2_member_sync_member.ps1")
_PS_STRING_RE = re.compile(r'"(?:[^"\n`]|`.)*"')
_SQL_HINT_RE = re.compile(r"\b(SELECT|FROM|WHERE|CHARINDEX|COUNT_BIG|ISNULL)\b|\$keyExpr|\$mobileExpr|= \?")
_PLACEHOLDER_RE = re.compile(r"(?<=[\s(,])\?(?=[\s),]|$)")


class SqlPlaceholderStaticTests(unittest.TestCase):
    """VM fact r5-5: AutoCount's ExecuteScalar rewrites every "?" in the SQL
    text into a parameter, so a "?" may appear only as a placeholder."""

    def sql_literals(self):
        for name in SQL_SCRIPTS:
            for number, line in enumerate((ROOT / "scripts" / name).read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for literal in _PS_STRING_RE.findall(line):
                    if _SQL_HINT_RE.search(literal):
                        yield name, number, line, literal[1:-1]

    def test_question_marks_are_placeholders_only(self):
        literals = list(self.sql_literals())
        self.assertGreater(len(literals), 10)
        for name, number, _line, text in literals:
            with self.subTest(at=f"{name}:{number}"):
                for quoted in re.findall(r"'[^']*'", text):
                    self.assertNotIn("?", quoted)  # inside an SQL string literal
                self.assertNotIn("--", text)  # no SQL comment at all
                outside = re.sub(r"'[^']*'", "''", text)
                self.assertEqual(outside.count("?"), len(_PLACEHOLDER_RE.findall(outside)))

    def test_placeholder_count_matches_parameters(self):
        bridge = BRIDGE.read_text(encoding="utf-8")
        # sync-lookup: each condition adds exactly as many parameters as "?".
        lines = [line for line in bridge.splitlines() if "$conditions +=" in line]
        self.assertEqual(len(lines), 6)
        for line in lines:
            with self.subTest(line=line.strip()):
                literal = _PS_STRING_RE.search(line).group(0)
                self.assertEqual(literal.count("?"), line.count("$params +="))
        # sync-changelog: one "?" and one parameter.
        body = bridge.split("function Invoke-XbDirectLocalSyncChangeLog", 1)[1].split("\nfunction ", 1)[0]
        query = "".join(literal for literal in _PS_STRING_RE.findall(body) if "ChangeLog " in literal or "ORDER BY ID" in literal)
        self.assertEqual(query.count("?"), 1)
        self.assertEqual(body.count("-Params @([long]$afterId)"), 1)
        # Every call without -Params passes no "?".
        for name in SQL_SCRIPTS:
            for line in (ROOT / "scripts" / name).read_text(encoding="utf-8").splitlines():
                if "Get-XbDlScalar" in line and "-Sql" in line and "-Params" not in line and "function " not in line:
                    with self.subTest(line=line.strip()):
                        self.assertNotIn("?", "".join(_PS_STRING_RE.findall(line)))

    def test_python_builds_no_sql_for_execute_scalar(self):
        source = (ROOT / "directlocal" / "xb_member_sync.py").read_text(encoding="utf-8")
        self.assertNotIn("ExecuteScalar", source)
        # Its own SQL is SQLite on the private ledger; no AutoCount table is named.
        self.assertNotRegex(source, r"(?i)\bFROM\s+(dbo\.)?(Member|ChangeLog)\b")


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class SyncBridgeTests(unittest.TestCase):
    def setUp(self):
        assert SYNC_HOST != FAKE_HOST and "DBSetting = $db" in SYNC_HOST
        self.temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-bridge-"))
        self.host = self.temp / "fake_sync_host.ps1"
        self.host.write_text(SYNC_HOST, encoding="utf-8")
        self.state = self.temp / "fake_autocount.json"
        self.claims = self.temp / "claims"
        self.claims.mkdir()
        self.set_state()

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def set_state(self, **overrides):
        state = {"members": [], "browse": [], "saves": 0, "next": [], "next_index": 0, "browse_error": False, "save_throws": False, "hang_after_save_ms": 0,
                 "sql": [], "params": [], "db_throws": False, "lookup_json": "", "changelog_json": "", "max_id": 0}
        state.update(overrides)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def fake(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8-sig"))

    def command(self) -> list[str]:
        return [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.host), "-Bridge", str(BRIDGE), "-StateFile", str(self.state)]

    def run_bridge(self, operation: str, stdin: dict, *extra: str, command: list[str] | None = None):
        args = (command or self.command()) + ["-Operation", operation, "-EnableProductionAdapter"] + list(extra)
        proc = subprocess.run(args, input=(json.dumps(stdin) + "\n").encode("ascii"), capture_output=True, timeout=120, check=False)
        out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        return proc.returncode, (json.loads(out[-1]) if out else None), proc

    def test_lookup_is_parameterised_and_returns_rows(self):
        rows = [{"member_no": sg(1), "name": "Alpha Tester", "mobile_phone": None, "email_address": None, "dob": None}]
        self.set_state(lookup_json=json.dumps(rows))
        code, out, _ = self.run_bridge("sync-lookup", {"phone_digits": sg(1), "sg_local": sg(1)[2:], "member_nos": ["ECHOTESTER"]})
        self.assertEqual((code, out), (0, {"members": rows}))
        state = self.fake()
        sql, params = state["sql"][0], state["params"][0]
        self.assertEqual(sql.count("?"), len(params))
        self.assertEqual(params, [sg(1), sg(1), sg(1)[2:], sg(1)[2:], sg(1)[2:], "ECHOTESTER"])
        self.assertNotIn(sg(1)[2:], sql)
        self.assertNotIn("EmailAddress =", sql)
        self.assertTrue(sql.startswith("SELECT (SELECT TOP 50 "))

    def test_lookup_single_row_and_empty(self):
        row = {"member_no": sg(2), "name": "Bravo", "mobile_phone": None, "email_address": None, "dob": "2000-03-01"}
        self.set_state(lookup_json=json.dumps([row]))
        self.assertEqual(self.run_bridge("sync-lookup", {"phone_digits": sg(2), "sg_local": None, "member_nos": []})[:2], (0, {"members": [row]}))
        self.set_state(lookup_json="")
        self.assertEqual(self.run_bridge("sync-lookup", {"phone_digits": sg(2), "sg_local": None, "member_nos": []})[:2], (0, {"members": []}))
        self.assertEqual(self.run_bridge("sync-lookup", {"phone_digits": None, "sg_local": None, "member_nos": []})[:2], (0, {"members": []}))

    def test_lookup_refusals(self):
        cases = [
            ({"phone_digits": "+" + sg(3), "sg_local": None, "member_nos": []}, "phone_keys_invalid"),
            ({"phone_digits": sg(3), "sg_local": "12", "member_nos": []}, "phone_keys_invalid"),
            ({"phone_digits": None, "sg_local": None, "member_nos": ["x" * 41]}, "member_no_invalid"),
        ]
        for request, error in cases:
            with self.subTest(error=error):
                self.assertEqual(self.run_bridge("sync-lookup", request)[:2], (1, {"error": error}))
        self.set_state(db_throws=True)
        code, out, proc = self.run_bridge("sync-lookup", {"phone_digits": sg(3), "sg_local": None, "member_nos": []})
        self.assertEqual((code, out), (1, {"error": "sync_lookup_failed"}))
        self.assertNotIn(b"vendor sql failure", proc.stdout)

    def test_changelog_reads_inserts_after_watermark(self):
        rows = [{"id": 1007, "xml": "<Member><MemberNo>A1</MemberNo></Member>"}, {"id": 1021, "xml": "<Member><MemberNo>A2</MemberNo></Member>"}]
        self.set_state(changelog_json=json.dumps(rows), max_id=1030)
        code, out, _ = self.run_bridge("sync-changelog", {"after_id": 1000, "limit": 200, "xml_column": "XmlData"})
        self.assertEqual((code, out), (0, {"rows": rows, "max_id": 1030}))
        sql = self.fake()["sql"][0]
        self.assertIn("TOP (200)", sql)
        self.assertIn("CAST([XmlData] AS nvarchar(max))", sql)
        self.assertIn("FROM ChangeLog WHERE TableName = 'Member' AND ChangeType = 'I' AND ID > ? ORDER BY ID", sql)
        params = self.fake()["params"][0]  # the fake's JSON flattens a one-item array
        self.assertEqual(params if isinstance(params, list) else [params], ["1000"])

    def test_changelog_refusals(self):
        for request in (
            {"after_id": 0, "limit": 200, "xml_column": "Xml;DROP"},
            {"after_id": 0, "limit": 0, "xml_column": "XmlData"},
            {"after_id": 0, "limit": 1001, "xml_column": "XmlData"},
            {"after_id": -2, "limit": 10, "xml_column": "XmlData"},
            {"after_id": "1", "limit": 10, "xml_column": "XmlData"},
        ):
            with self.subTest(request=request):
                self.assertEqual(self.run_bridge("sync-changelog", request)[:2], (1, {"error": "sync_changelog_request_invalid"}))
        self.assertEqual(self.fake()["sql"], [])

    def write_request(self, fence_id: str, payload: dict, member_no: str) -> dict:
        salt = "b2" * 16
        text = dl.canonical_payload(payload)
        return {"fence_id": fence_id, "member_no": member_no, "salt": salt, "payload_canonical": text, "payload_digest": dl.payload_digest(salt, text)}

    def write_args(self, fence_id: str) -> list[str]:
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return ["-FenceId", fence_id, "-ClaimDirectory", str(self.claims), "-WriteDeadlineUtc", deadline]

    def test_sync_p1_create_registered_no_expiry_month_dob(self):
        fence = "fence-" + "a" * 32
        payload = {"profile": "sync_p1", "name": "Alpha Tester", "mobile_phone": sg(4), "email_address": "alpha@example.com", "register_date": "2026-10-21", "dob": "2000-07-01"}
        code, out, proc = self.run_bridge("write-once", self.write_request(fence, payload, sg(4)), *self.write_args(fence))
        self.assertEqual((code, out), (0, {"save_invocation_count": 1, "readback_found": True, "readback_match": True, "outbound_queue_changed": False}))
        member = self.fake()["members"][0]
        self.assertEqual(
            (member["MemberNo"], member["MemberType"], member["ExpiryDate"], member["DOB"], member["RegisterDate"], member["IsActive"], member["MobilePhone"]),
            (sg(4), "REGISTERED", None, "2000-07-01", "2026-10-21", "T", sg(4)),
        )
        self.assertNotIn(sg(4)[2:].encode(), proc.stdout)

    def test_sync_p1_without_birthday_or_phone(self):
        fence = "fence-" + "b" * 32
        payload = {"profile": "sync_p1", "name": "Echo Tester", "mobile_phone": None, "email_address": None, "register_date": "2026-10-21", "dob": None}
        code, out, _ = self.run_bridge("write-once", self.write_request(fence, payload, "ECHOTESTER"), *self.write_args(fence))
        self.assertEqual(code, 0, out)
        self.assertTrue(out["readback_match"])
        member = self.fake()["members"][0]
        self.assertEqual((member["DOB"], member["ExpiryDate"], member["MobilePhone"], member["EmailAddress"]), (None, None, None, None))

    def test_sync_p1_refuses_a_held_number_before_the_claim(self):
        self.set_state(members=[{"MemberNo": sg(8), "Name": "Kilo Other"}])
        fence = "fence-" + "e" * 32
        payload = {"profile": "sync_p1", "name": "Alpha Tester", "mobile_phone": sg(8), "email_address": None, "register_date": "2026-10-21", "dob": None}
        code, out, _ = self.run_bridge("write-once", self.write_request(fence, payload, sg(8)), *self.write_args(fence))
        self.assertEqual((code, out), (1, {"error": "member_no_taken"}))
        self.assertEqual(self.fake()["saves"], 0)
        self.assertFalse((self.claims / f"{fence}.claim").exists())  # parent proves NOT_WRITTEN

    def test_sync_p1_refuses_a_full_birth_date_and_unknown_profile(self):
        for letter, payload, error in (
            ("c", {"profile": "sync_p1", "name": "Alpha", "mobile_phone": None, "email_address": None, "register_date": "2026-10-21", "dob": "1990-07-15"}, "member_date_invalid"),
            ("d", {"profile": "other", "name": "Alpha", "register_date": "2026-10-21"}, "member_field_not_allowed"),
        ):
            fence = "fence-" + letter * 32
            code, out, _ = self.run_bridge("write-once", self.write_request(fence, payload, "ALPHA"), *self.write_args(fence))
            self.assertEqual((code, out), (1, {"error": error}))
        self.assertEqual(self.fake()["saves"], 0)

    def test_production_book_refused_by_entrypoint(self):
        direct = [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(BRIDGE)]
        for operation, request in (("sync-lookup", {"phone_digits": sg(5), "sg_local": None, "member_nos": []}), ("sync-changelog", {"after_id": 0, "limit": 1, "xml_column": "XmlData"})):
            with self.subTest(operation=operation):
                code, out, _ = self.run_bridge(operation, request, "-TargetBook", "AED_XBOUNDARIES", command=direct)
                self.assertEqual((code, out), (1, {"error": "production_book_not_authorised"}))

    def test_python_client_end_to_end(self):
        rows = [{"member_no": sg(6), "name": "Alpha Tester", "mobile_phone": sg(6), "email_address": None, "dob": None}]
        self.set_state(lookup_json=json.dumps(rows), changelog_json=json.dumps([{"id": 5, "xml": "<Member/>"}]), max_id=9)
        client = sync.SyncBridgeClient(self.command(), autocount_book="AED_TESTINGAPI", claims=self.claims, deadline_seconds=60)
        client.autocount_book = None  # the marked fake host takes no -TargetBook (test-only)
        self.assertEqual([m.member_no for m in client.lookup(sg(6), [])], [sg(6)])
        self.assertEqual(client.changelog_inserts(None, 10, "XmlData"), ([(5, "<Member/>")], 9))
        payload = {"profile": "sync_p1", "name": "Bravo Tester", "mobile_phone": sg(7), "email_address": None, "register_date": "2026-10-21", "dob": "2000-01-01"}
        self.assertEqual(client.create_member(sg(7), payload, write_id="write-" + "e" * 32), ("CREATED_VERIFIED", None))
        self.assertEqual(self.fake()["saves"], 1)


if __name__ == "__main__":
    unittest.main()
