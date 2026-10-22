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


GUARD = ROOT / "scripts" / "ac2_directlocal_book_guard.ps1"

# G2 H1: the exact SQL texts Get-XbDlScalar may send (every AutoCount session
# runs as SQL login sa, so the guard is the only database-side limit). Pins
# $script:XbDlSqlText in the book guard.
ALLOWED_SQL_TEXTS = {
    "db_name": "SELECT DB_NAME()",
    "machine_name": "SELECT CAST(SERVERPROPERTY('MachineName') AS nvarchar(128))",
    "user_script_rows": "SELECT COUNT_BIG(*) FROM UserScript",
    "mail_queue_rows": "SELECT COUNT_BIG(*) FROM MailQueue",
    "einvoice_queue_rows": "SELECT COUNT_BIG(*) FROM EInvoiceQueue",
    "pos_sync_registrations": (
        "SELECT (SELECT Location AS location, SyncName AS sync_name, "
        "CONVERT(varchar(19), LastFinishSyncTime, 126) AS last_finish_sync_local, CONVERT(varchar(19), LastAliveTime, 126) AS last_alive_local "
        "FROM SyncRegistration ORDER BY Location, SyncName FOR JSON PATH, INCLUDE_NULL_VALUES)"
    ),
    "server_now_local": "SELECT CONVERT(varchar(19), GETDATE(), 126)",
    "member_change_counter": "SELECT ISNULL(MAX(Counter), 0) FROM ChangeCount WHERE TableName = 'Member'",
    "changelog_max_id": "SELECT ISNULL(MAX(ID), 0) FROM ChangeLog",
}


def changelog_sql(limit, column: str = "Content") -> str:
    return (f"SELECT (SELECT TOP ({limit}) ID AS id, CAST([{column}] AS nvarchar(max)) AS xml FROM ChangeLog "
            "WHERE TableName = 'Member' AND ChangeType = 'I' AND ID > ? ORDER BY ID FOR JSON PATH)")


LOOKUP_KEY = "UPPER(LTRIM(RTRIM(MemberNo))) = ?"
LOOKUP_MOBILE = "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(ISNULL(MobilePhone, ''), ' ', ''), '-', ''), '+', ''), '(', ''), ')', ''), '.', '') = ?"
LOOKUP_EMBEDDED = "CHARINDEX(?, MemberNo) > 0"
LOOKUP_NUMBER = "UPPER(LTRIM(RTRIM(MemberNo))) = UPPER(LTRIM(RTRIM(?)))"


def lookup_sql(conditions) -> str:
    return ("SELECT (SELECT TOP 50 MemberNo AS member_no, Name AS name, MobilePhone AS mobile_phone, EmailAddress AS email_address, "
            "CONVERT(varchar(10), DOB, 23) AS dob FROM Member WHERE " + " OR ".join(conditions) + " ORDER BY MemberNo FOR JSON PATH, INCLUDE_NULL_VALUES)")


DENIED_WORDS = ("EXEC", "EXECUTE", "TRUNCATE", "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "CREATE", "GRANT", "REVOKE", "DENY",
                "INTO", "DBCC", "SHUTDOWN", "KILL", "BACKUP", "RESTORE", "BULK", "OPENROWSET", "OPENDATASOURCE", "OPENQUERY", "WAITFOR",
                "DECLARE", "SET", "USE")

SQL_GUARD_HARNESS = r"""
$ErrorActionPreference = "Stop"
. $args[0]
$doc = ConvertFrom-Json -InputObject (Get-Content -LiteralPath $args[1] -Raw -Encoding UTF8)
$db = [pscustomobject]@{ Calls = 0 }
$db | Add-Member -MemberType ScriptMethod -Name ExecuteScalar -Value { param([string]$Sql, [object[]]$Params) $this.Calls++; return "x" }
$results = @()
foreach ($case in $doc.cases) {
    $params = @()
    foreach ($value in @($case.params)) { if ($null -ne $value) { $params += [string]$value } }
    $before = $db.Calls
    try { [void](Get-XbDlScalar -DBSetting $db -Sql ([string]$case.sql) -Params $params); $code = "ok" } catch { $code = [string]$_.Exception.Message }
    $results += [ordered]@{ code = $code; executed = ($db.Calls -gt $before) }
}
$texts = Get-Variable -Name XbDlSqlText -Scope Script -ValueOnly -ErrorAction SilentlyContinue
$engine = "$($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion)"
[Console]::Out.WriteLine((ConvertTo-Json -InputObject ([ordered]@{ results = $results; texts = $texts; engine = $engine }) -Compress -Depth 4))
"""


class SqlAllowlistStaticTests(unittest.TestCase):
    """G2 H1: no PowerShell needed. Every Get-XbDlScalar call in the scripts
    sends a named allowlisted text, or (only in the two sync reads) the
    template the guard pins; Python builds no AutoCount SQL (see
    SqlPlaceholderStaticTests)."""

    def test_every_call_sends_an_allowlisted_text(self):
        calls = []
        for path in sorted((ROOT / "scripts").glob("*.ps1")):
            function = None
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                found = re.match(r"^function ([A-Za-z-]+)", line)
                function = found.group(1) if found else function
                if "Get-XbDlScalar" in line and not line.lstrip().startswith(("function ", "#")):
                    calls.append((path.name, number, function, line))
        self.assertGreaterEqual(len(calls), 12)
        for name, number, function, line in calls:
            with self.subTest(at=f"{name}:{number}"):
                self.assertIn(name, ("ac2_directlocal_book_guard.ps1", "ac2_directlocal_member_bridge.ps1"))
                argument = re.search(r"-Sql\s+(\S+)", line).group(1)
                if argument == "$sql":
                    self.assertIn(function, ("Invoke-XbDirectLocalSyncLookup", "Invoke-XbDirectLocalSyncChangeLog"))
                else:
                    self.assertRegex(argument, r"^\$script:XbDlSqlText\.[a-z_]+\)*$")
                    self.assertIn(argument.split(".", 1)[1].rstrip(")"), ALLOWED_SQL_TEXTS)


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class SqlAllowlistGuardTests(unittest.TestCase):
    """G2 H1 under real PowerShell: Get-XbDlScalar refuses every text that is
    not pinned, after a deny check on every text. Refused texts never reach
    ExecuteScalar."""

    def run_guard(self, cases, engine=None):
        temp = Path(tempfile.mkdtemp(prefix="xb-sql-guard-"))
        self.addCleanup(shutil.rmtree, temp, True)
        (temp / "harness.ps1").write_text(SQL_GUARD_HARNESS, encoding="utf-8")
        # ensure_ascii (the default): any non-ASCII or control character travels as a \u escape.
        (temp / "cases.json").write_text(json.dumps({"cases": [{"sql": sql, "params": list(params)} for sql, params in cases]}), encoding="utf-8")
        proc = subprocess.run([engine or powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(temp / "harness.ps1"), str(GUARD), str(temp / "cases.json")],
                              capture_output=True, timeout=120, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        out = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertEqual(len(out["results"]), len(cases))
        return out

    def assert_refused(self, cases, code):
        out = self.run_guard(cases)
        for (sql, _params), result in zip(cases, out["results"]):
            with self.subTest(sql=sql[:90]):
                self.assertEqual(result, {"code": code, "executed": False})

    def test_g2_probe_strings_are_refused(self):
        self.assert_refused([
            ("SELECT 1; EXECUTE sp_who", ()),
            ("SELECT 1; TRUNCATE TABLE Member", ()),
            ("SELECT 1; GRANT CONTROL TO public", ()),
            ("SELECT * FROM AED_XBOUNDARIES.dbo.Member", ()),
        ], "vendor_query_not_read_only")

    def test_denied_words_and_tokens(self):
        cases = [(f"SELECT {word} FROM Member", ()) for word in DENIED_WORDS]
        cases += [(f"select 1 {word.lower()} x", ()) for word in DENIED_WORDS]
        cases += [(text, ()) for text in (
            "SELECT 1;", "SELECT 1 -- x", "SELECT 1 /* x", "SELECT 1 */", "SELECT * FROM sp_helpdb", "SELECT xp_cmdshell",
            "SELECT COUNT_BIG(*) FROM aed_xboundaries", ALLOWED_SQL_TEXTS["db_name"] + ";",
        )]
        self.assert_refused(cases, "vendor_query_not_read_only")

    def test_three_and_four_part_names(self):
        self.assert_refused([(f"SELECT COUNT_BIG(*) FROM {name}", ()) for name in (
            "a.b.c", "[a].[b].[c]", "a..c", "srv.db.dbo.t", '"a"."b"."c"', "a . b . c", "[srv].[db]..[t]",
        )], "vendor_query_not_read_only")

    def test_unknown_or_altered_texts(self):
        db_name, max_id = ALLOWED_SQL_TEXTS["db_name"], ALLOWED_SQL_TEXTS["changelog_max_id"]
        self.assert_refused([
            ("SELECT 1", ()),
            ("SELECT  DB_NAME()", ()),
            (db_name + " ", ()),
            (" " + db_name, ()),
            ("select DB_NAME()", ()),
            (max_id + " WHERE ID > 0", ()),
            ("SELECT COUNT_BIG(*) FROM Member", ()),
            (db_name, ("x",)),  # a parameter the text has no placeholder for
            (changelog_sql(200), ()),  # its placeholder without the parameter
            (changelog_sql(0), ("1",)),
            (changelog_sql(1001), ("1",)),
            (changelog_sql("01"), ("1",)),
            (changelog_sql(200, "Con tent"), ("1",)),
            (changelog_sql(200, "1Content"), ("1",)),
            (changelog_sql(200) + " ", ("1",)),
            (lookup_sql([]), ()),
            (lookup_sql(["1 = 1"]), ()),
            (lookup_sql([LOOKUP_MOBILE, LOOKUP_KEY]), ("1", "2")),
            (lookup_sql([LOOKUP_KEY, LOOKUP_MOBILE] * 3), ("1",) * 6),
            (lookup_sql([LOOKUP_EMBEDDED]), ("1",)),
            (lookup_sql([LOOKUP_NUMBER] * 21), ("1",) * 21),
            (lookup_sql([LOOKUP_KEY, LOOKUP_MOBILE]).replace(" OR ", "  OR "), ("1", "2")),
        ], "vendor_query_not_allowlisted")

    def test_every_allowlisted_text_runs(self):
        phone, sg_local = [LOOKUP_KEY, LOOKUP_MOBILE], [LOOKUP_KEY, LOOKUP_MOBILE, LOOKUP_EMBEDDED]
        cases = [(text, ()) for text in ALLOWED_SQL_TEXTS.values()]
        cases += [(changelog_sql(limit, column), ("1",)) for limit in (1, 200, 1000) for column in ("Content", "XmlData")]
        cases += [(lookup_sql(conditions), ("1",) * sum(c.count("?") for c in conditions)) for conditions in (
            phone, sg_local, phone + sg_local, [LOOKUP_NUMBER], [LOOKUP_NUMBER] * 20, phone + sg_local + [LOOKUP_NUMBER] * 20,
        )]
        out = self.run_guard(cases)
        for (sql, _params), result in zip(cases, out["results"]):
            with self.subTest(sql=sql[:90]):
                self.assertEqual(result, {"code": "ok", "executed": True})
        self.assertEqual(out["texts"], ALLOWED_SQL_TEXTS)  # the guard's table is exactly the pin

    def test_non_ascii_texts_are_refused_on_every_engine(self):
        # G2 LOW-1: an allowlisted text with an invisible or non-ASCII
        # character (built at runtime; this file stays ASCII) is refused
        # before any other check, on Windows PowerShell 5.1 and pwsh alike.
        db_name = ALLOWED_SQL_TEXTS["db_name"]
        counter = ALLOWED_SQL_TEXTS["member_change_counter"]
        bases = [(text, ()) for text in ALLOWED_SQL_TEXTS.values()]
        bases += [(changelog_sql(200), ("1",)), (lookup_sql([LOOKUP_KEY, LOOKUP_MOBILE]), ("1", "2"))]
        cases = []
        for code in NON_ASCII_CODE_POINTS:
            for text, params in bases:
                middle = len(text) // 2
                cases += [(chr(code) + text, params), (text[:middle] + chr(code) + text[middle:], params), (text + chr(code), params)]
        cases += [
            ("SELECT DB_" + chr(0xAD) + "NAME()", ()),  # G2 probes
            (db_name + chr(0x00), ()),
            (db_name + chr(0x200D), ()),
            (counter.replace("'Member'", "'Mem" + chr(0xAD) + "ber'"), ()),
            (db_name + "\n", ()),  # moved here: a line break is outside the allowed set
            (db_name + "\r", ()),
            ("SELECT" + "\t" + "DB_NAME()", ()),
        ]
        engines = powershell_engines()
        self.assertTrue(engines)
        ran = []
        for engine in engines:
            with self.subTest(engine=Path(engine).name):
                out = self.run_guard(cases, engine)
                ran.append(f"{Path(engine).name} ({out['engine']})")
                for (sql, _params), result in zip(cases, out["results"]):
                    with self.subTest(engine=Path(engine).name, sql=ascii(sql[:60])):
                        self.assertEqual(result, {"code": "vendor_query_not_ascii", "executed": False})
        # G2 round 5 NOTE-1: the CI log shows which engines this test ran.
        sys.stderr.write("\n[sql-allowlist non-ASCII] engines run: " + "; ".join(ran) + "\n")


# U+00AD soft hyphen, NUL, U+200B zero-width space, U+200D zero-width joiner,
# U+FEFF byte-order mark, U+00A0 no-break space.
NON_ASCII_CODE_POINTS = (0xAD, 0x00, 0x200B, 0x200D, 0xFEFF, 0xA0)


def powershell_engines() -> list[str]:
    """Every PowerShell engine on this machine (Windows PowerShell 5.1 and/or pwsh)."""

    found = []
    for name in ("powershell", "pwsh"):
        path = shutil.which(name)
        if path and path not in found:
            found.append(path)
    return found


class SqlAllowlistAsciiStaticTests(unittest.TestCase):
    def test_allowlisted_texts_are_single_line_printable_ascii(self):
        # Fact behind the ASCII gate: every pinned text and both templates use
        # only U+0020-U+007E, so U+0020 is the only whitespace (no tab, CR or LF).
        texts = list(ALLOWED_SQL_TEXTS.values()) + [changelog_sql(1000, "Content")]
        texts.append(lookup_sql([LOOKUP_KEY, LOOKUP_MOBILE, LOOKUP_KEY, LOOKUP_MOBILE, LOOKUP_EMBEDDED] + [LOOKUP_NUMBER] * 20))
        for text in texts:
            with self.subTest(text=text[:60]):
                self.assertRegex(text, r"\A[\x20-\x7E]+\Z")

    def test_sources_stay_ascii(self):
        for path in (Path(__file__), GUARD):
            path.read_bytes().decode("ascii")


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
        payload = {"profile": "sync_p1", "member_type": "REGISTERED", "name": "Alpha Tester", "mobile_phone": sg(4), "email_address": "alpha@example.com", "register_date": "2026-10-21", "dob": "2000-07-01"}
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
        payload = {"profile": "sync_p1", "member_type": "REGISTERED", "name": "Echo Tester", "mobile_phone": None, "email_address": None, "register_date": "2026-10-21", "dob": None}
        code, out, _ = self.run_bridge("write-once", self.write_request(fence, payload, "ECHOTESTER"), *self.write_args(fence))
        self.assertEqual(code, 0, out)
        self.assertTrue(out["readback_match"])
        member = self.fake()["members"][0]
        self.assertEqual((member["DOB"], member["ExpiryDate"], member["MobilePhone"], member["EmailAddress"]), (None, None, None, None))

    def test_sync_p1_refuses_a_held_number_before_the_claim(self):
        self.set_state(members=[{"MemberNo": sg(8), "Name": "Kilo Other"}])
        fence = "fence-" + "e" * 32
        payload = {"profile": "sync_p1", "member_type": "REGISTERED", "name": "Alpha Tester", "mobile_phone": sg(8), "email_address": None, "register_date": "2026-10-21", "dob": None}
        code, out, _ = self.run_bridge("write-once", self.write_request(fence, payload, sg(8)), *self.write_args(fence))
        self.assertEqual((code, out), (1, {"error": "member_no_taken"}))
        self.assertEqual(self.fake()["saves"], 0)
        self.assertFalse((self.claims / f"{fence}.claim").exists())  # parent proves NOT_WRITTEN

    def test_sync_p1_refuses_a_full_birth_date_and_unknown_profile(self):
        for letter, payload, error in (
            ("c", {"profile": "sync_p1", "member_type": "REGISTERED", "name": "Alpha", "mobile_phone": None, "email_address": None, "register_date": "2026-10-21", "dob": "1990-07-15"}, "member_date_invalid"),
            ("d", {"profile": "other", "name": "Alpha", "register_date": "2026-10-21"}, "member_field_not_allowed"),
        ):
            fence = "fence-" + letter * 32
            code, out, _ = self.run_bridge("write-once", self.write_request(fence, payload, "ALPHA"), *self.write_args(fence))
            self.assertEqual((code, out), (1, {"error": error}))
        self.assertEqual(self.fake()["saves"], 0)

    def test_sync_p1_member_type_is_the_configured_code(self):
        # Round 6: the sync's MemberType comes from ac2_create_member_type
        # (validated here too); it is never a caller field on the member.
        base = {"profile": "sync_p1", "name": "Alpha Tester", "mobile_phone": None, "email_address": None, "register_date": "2026-10-21", "dob": None}
        for digit, member_type, error in (("1", "BAD TYPE", "member_type_invalid"), ("2", "GU" + chr(0xC9) + "ST", "member_type_invalid"), ("3", "X" * 21, "member_type_invalid"), ("4", None, "member_field_required")):
            with self.subTest(member_type=ascii(member_type)):
                fence = "fence-" + digit * 32
                payload = dict(base) if member_type is None else {**base, "member_type": member_type}
                code, out, _ = self.run_bridge("write-once", self.write_request(fence, payload, "ALPHA" + digit), *self.write_args(fence))
                self.assertEqual((code, out), (1, {"error": error}))
                self.assertFalse((self.claims / f"{fence}.claim").exists())  # refused before the claim
        self.assertEqual(self.fake()["saves"], 0)
        fence = "fence-" + "5" * 32
        code, out, _ = self.run_bridge("write-once", self.write_request(fence, {**base, "member_type": "GUEST"}, "ALPHA5"), *self.write_args(fence))
        self.assertEqual(code, 0, out)
        self.assertTrue(out["readback_match"])
        self.assertEqual(self.fake()["members"][0]["MemberType"], "GUEST")

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
        payload = {"profile": "sync_p1", "member_type": "REGISTERED", "name": "Bravo Tester", "mobile_phone": sg(7), "email_address": None, "register_date": "2026-10-21", "dob": "2000-01-01"}
        self.assertEqual(client.create_member(sg(7), payload, write_id="write-" + "e" * 32), ("CREATED_VERIFIED", None))
        self.assertEqual(self.fake()["saves"], 1)


if __name__ == "__main__":
    unittest.main()
