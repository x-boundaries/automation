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
    if ($Sql -like "*FROM MemberType WHERE*") { return [long]@($s.member_types | Where-Object { [string]::Equals([string]$_, [string]$Params[0], [StringComparison]::Ordinal) }).Count }
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
    # Go-live R7 (Owner Q7, SCOPE-CONFIRMED 29 Oct 2026): exactly one parameter.
    "member_type_exists": "SELECT COUNT_BIG(*) FROM MemberType WHERE MemberType = ?",
}


def changelog_sql(limit, column: str = "Content") -> str:
    return (f"SELECT (SELECT TOP ({limit}) ID AS id, CAST([{column}] AS nvarchar(max)) AS xml FROM ChangeLog "
            "WHERE TableName = 'Member' AND ChangeType = 'I' AND ID > ? ORDER BY ID FOR JSON PATH)")


LOOKUP_KEY = "UPPER(LTRIM(RTRIM(MemberNo))) = ?"
LOOKUP_MOBILE = "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(ISNULL(MobilePhone, ''), ' ', ''), '-', ''), '+', ''), '(', ''), ')', ''), '.', '') = ?"
LOOKUP_EMBEDDED = "CHARINDEX(?, MemberNo) > 0"
LOOKUP_NUMBER = "UPPER(LTRIM(RTRIM(MemberNo))) = UPPER(LTRIM(RTRIM(?)))"


# Round 7 (Owner-approved, 22 Oct 2026): the one change to the book guard is
# CreatedTime in this lookup (Singapore local time, no offset, VM fact G0).
LOOKUP_PREFIX = ("SELECT (SELECT TOP 50 MemberNo AS member_no, Name AS name, MobilePhone AS mobile_phone, EmailAddress AS email_address, "
                 "CONVERT(varchar(10), DOB, 23) AS dob, CONVERT(varchar(23), CreatedTime, 126) AS created_time FROM Member WHERE ")
LOOKUP_PREFIX_BEFORE_R7 = ("SELECT (SELECT TOP 50 MemberNo AS member_no, Name AS name, MobilePhone AS mobile_phone, EmailAddress AS email_address, "
                           "CONVERT(varchar(10), DOB, 23) AS dob FROM Member WHERE ")


def lookup_sql(conditions, prefix: str = LOOKUP_PREFIX) -> str:
    return prefix + " OR ".join(conditions) + " ORDER BY MemberNo FOR JSON PATH, INCLUDE_NULL_VALUES)"


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
            # Round 7: the lookup without CreatedTime is no longer allowlisted.
            (lookup_sql([LOOKUP_KEY, LOOKUP_MOBILE], LOOKUP_PREFIX_BEFORE_R7), ("1", "2")),
            (lookup_sql([LOOKUP_NUMBER], LOOKUP_PREFIX_BEFORE_R7), ("1",)),
        ], "vendor_query_not_allowlisted")

    def test_every_allowlisted_text_runs(self):
        phone, sg_local = [LOOKUP_KEY, LOOKUP_MOBILE], [LOOKUP_KEY, LOOKUP_MOBILE, LOOKUP_EMBEDDED]
        cases = [(text, ("1",) * text.count("?")) for text in ALLOWED_SQL_TEXTS.values()]
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
        bases = [(text, ("1",) * text.count("?")) for text in ALLOWED_SQL_TEXTS.values()]
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


# The guard's ChangeLog template line, unchanged since round 3.
CHANGELOG_PATTERN_LINE = "$script:XbDlChangeLogSqlPattern = '^SELECT \\(SELECT TOP \\(([1-9][0-9]{0,3})\\) ID AS id, CAST\\(\\[[A-Za-z][A-Za-z0-9_]{0,63}\\] AS nvarchar\\(max\\)\\) AS xml FROM ChangeLog WHERE TableName = ''Member'' AND ChangeType = ''I'' AND ID > \\? ORDER BY ID FOR JSON PATH\\)\\z'"


class LookupCreatedTimeStaticTests(unittest.TestCase):
    """Round 7: the guard's lookup prefix and the bridge's lookup text both
    read CreatedTime; nothing else in the guard's SQL changed."""

    def test_guard_and_bridge_pin_the_lookup_with_created_time(self):
        guard = GUARD.read_text(encoding="utf-8")
        self.assertEqual(guard.count('$script:XbDlLookupSqlPrefix = "' + LOOKUP_PREFIX + '"'), 1)
        self.assertNotIn(LOOKUP_PREFIX_BEFORE_R7, guard)
        bridge = BRIDGE.read_text(encoding="utf-8")
        body = bridge.split("function Invoke-XbDirectLocalSyncLookup", 1)[1].split("\nfunction ", 1)[0]
        self.assertEqual("".join(literal[1:-1] for literal in _PS_STRING_RE.findall(body) if "AS member_no" in literal or "AS dob" in literal), LOOKUP_PREFIX)
        self.assertEqual(guard.count("CreatedTime"), 1)
        self.assertEqual(guard.count(CHANGELOG_PATTERN_LINE + "\n"), 1)  # the ChangeLog read is unchanged


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


def run_direct_offline(operation: str, stdin: dict, *extra: str, temp: Path, host: str = "XB-CI-NOT-THE-VM"):
    """The real bridge entry point, never able to log in: COMPUTERNAME is not
    the pinned AC2 host and -AcRoot is an empty folder (no assemblies), so a
    request that passes the book guard stops at the host pin (or, with the
    pinned host name, at the assembly pin). No credential env is passed."""

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
                 "sql": [], "params": [], "db_throws": False, "lookup_json": "", "changelog_json": "", "max_id": 0,
                 "member_types": ["REGISTERED", "GUEST"]}
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
        rows = [{"member_no": sg(1), "name": "Alpha Tester", "mobile_phone": None, "email_address": None, "dob": None, "created_time": "2026-10-21T10:02:00.123"}]
        self.set_state(lookup_json=json.dumps(rows))
        code, out, _ = self.run_bridge("sync-lookup", {"phone_digits": sg(1), "sg_local": sg(1)[2:], "member_nos": ["ECHOTESTER"]})
        self.assertEqual((code, out), (0, {"members": rows}))
        state = self.fake()
        sql, params = state["sql"][0], state["params"][0]
        self.assertEqual(sql.count("?"), len(params))
        self.assertEqual(params, [sg(1), sg(1), sg(1)[2:], sg(1)[2:], sg(1)[2:], "ECHOTESTER"])
        self.assertNotIn(sg(1)[2:], sql)
        self.assertNotIn("EmailAddress =", sql)
        self.assertTrue(sql.startswith(LOOKUP_PREFIX))

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
        # Go-live R3/R4 (moved refusal): the sync's reads on the live book now
        # pass the book guard and stop at the next guard (the host pin; this
        # is never the AC2 VM); any other operation is still refused by book.
        for operation, request, error in (
            ("sync-lookup", {"phone_digits": sg(5), "sg_local": None, "member_nos": []}, "host_not_authorised"),
            ("sync-changelog", {"after_id": 0, "limit": 1, "xml_column": "XmlData"}, "host_not_authorised"),
            ("probe", {"member_no": "ALPHA"}, "production_book_not_authorised"),
            ("next-member-no", {}, "production_book_not_authorised"),
        ):
            with self.subTest(operation=operation):
                self.assertEqual(run_direct_offline(operation, request, "-TargetBook", "AED_XBOUNDARIES", temp=self.temp), (1, {"error": error}))

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


# --------------------------------------------------------------------------
# Go-live closure matrix, PowerShell rows (G0 go-live design 4.4; the Python
# rows and the row coverage test are in tests/test_member_sync.py). Offline:
# fake DBSettings, the marked synthetic host, and the real entry point only
# where it cannot log in (run_direct_offline). Lookalike names are built at
# runtime so this file stays ASCII.
# --------------------------------------------------------------------------

LIVE_BOOK = "AED_XBOUNDARIES"
TEST_BOOK = "AED_TESTINGAPI"
PINNED_HOST = "DESKTOP-4I042L6"
ADAPTER = ROOT / "scripts" / "ac2_member_gateway_autocount_adapter.ps1"
ALL_OPERATIONS = ("book-check", "pos-sync-status", "legacy-precheck", "next-member-no", "probe", "reconcile", "write-once", "sync-lookup", "sync-changelog")


def lookalikes(name: str) -> list[str]:
    middle = len(name) // 2
    return [name[:middle] + chr(0xAD) + name[middle:], name + chr(0x200B), chr(0xFEFF) + name, name.replace("_", chr(0xA0)), name + " ", name.replace("A", chr(0x410), 1)]


GOLIVE_GUARD_HARNESS = r"""
$ErrorActionPreference = "Stop"
. $args[0]
. $args[1]
$doc = ConvertFrom-Json -InputObject (Get-Content -LiteralPath $args[2] -Raw -Encoding UTF8)
function New-FakeDb($Name, $Values) {
    $db = [pscustomobject]@{ DBName = $Name; Values = $Values; Sent = @() }
    $db | Add-Member -MemberType ScriptMethod -Name ExecuteScalar -Value {
        param([string]$Sql, [object[]]$Params)
        $this.Sent += $Sql
        foreach ($key in $this.Values.Keys) { if ($Sql -like "*$key*") { return $this.Values[$key] } }
        throw "unexpected sql"
    }
    return $db
}
function Try-Code([scriptblock]$Block) { try { [void](& $Block); return "ok" } catch { return [string]$_.Exception.Message } }
$results = @()
foreach ($c in @($doc.cases)) {
    switch ([string]$c.kind) {
        "book" {
            $code = Try-Code {
                if ($c.not_yet) { Assert-XbDlAuthorisedBook -Book $c.book -Operation $c.operation -ProfileNotYetRead }
                else { Assert-XbDlAuthorisedBook -Book $c.book -Operation $c.operation -PayloadProfile $c.profile }
            }
        }
        "identity" {
            $values = @{ "DB_NAME()" = [string]$c.vendor; "MachineName" = "DESKTOP-4I042L6" }
            $code = Try-Code { Assert-XbDlBookIdentity -Session @{ DBSetting = (New-FakeDb ([string]$c.declared) $values) } -Book ([string]$c.book) -ExpectedHost "DESKTOP-4I042L6" }
        }
        "outbound" {
            $values = @{ "UserScript" = [long]$c.user_script; "MailQueue" = [long]$c.mail; "EInvoiceQueue" = [long]$c.einvoice }
            $code = Try-Code { Assert-XbDlOutboundQuiet -Counts (Get-XbDlOutboundCounts -DBSetting (New-FakeDb "x" $values)) }
        }
        "sql" {
            $params = @(); foreach ($p in @($c.params)) { if ($null -ne $p) { $params += [string]$p } }
            $code = Try-Code { Get-XbDlScalar -DBSetting (New-FakeDb "x" @{ "FROM" = 1 }) -Sql ([string]$c.sql) -Params $params }
        }
        "member_type" {
            $db = New-FakeDb "x" @{ "FROM MemberType" = [long]$c.count }
            $code = Try-Code { Assert-XbDlMemberTypeExists -DBSetting $db -MemberType ([string]$c.type) }
            $code = "$code|$(@($db.Sent).Count)"
        }
        "session" {
            $env:COMPUTERNAME = [string]$c.host
            if ($c.factory) { $env:XB_AC2_SESSION_FACTORY = "New-Anything" } else { Remove-Item Env:XB_AC2_SESSION_FACTORY -ErrorAction SilentlyContinue }
            $code = Try-Code { New-XbDlRealSession -Book ([string]$c.book) -AcRoot ([string]$c.acroot) -Operation $c.operation -PayloadProfile $c.profile }
        }
        default { $code = "unknown_case" }
    }
    $results += $code
}
[Console]::Out.WriteLine((ConvertTo-Json -InputObject @($results) -Compress))
"""


@unittest.skipUnless(powershell(), "no PowerShell executable available")
class GoLivePsMatrixTests(unittest.TestCase):
    """Go-live rows in the PowerShell layer: the book guard admits the live
    book for the sync's three operations only (write-once only with sync_p1),
    compares ordinally, keeps the identity proof, and the member type read is
    one pinned parameterised text."""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-golive-guard-"))
        self.addCleanup(shutil.rmtree, self.temp, True)

    def guard(self, cases: list[dict]) -> list[str]:
        harness = self.temp / "harness.ps1"
        harness.write_text(GOLIVE_GUARD_HARNESS, encoding="utf-8")
        (self.temp / "cases.json").write_text(json.dumps({"cases": cases}), encoding="utf-8")
        env = {k: v for k, v in __import__("os").environ.items() if k not in ("XB_AC2_SESSION_FACTORY", "XB_AC2_USER_ID", "AC2_PROBE_USER_ID", "AC2_PROBE_PASSWORD")}
        proc = subprocess.run([powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness), str(ADAPTER), str(GUARD), str(self.temp / "cases.json")],
                              capture_output=True, timeout=180, check=False, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[-2000:])
        out = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
        out = out if isinstance(out, list) else [out]
        self.assertEqual(len(out), len(cases))
        return out

    def assert_codes(self, cases: list[dict], expected: list[str]) -> None:
        for case, code, want in zip(cases, self.guard(cases), expected):
            with self.subTest(case=ascii(case)):
                self.assertEqual(code, want)

    @staticmethod
    def book(book, operation=None, profile=None, not_yet=False) -> dict:
        return {"kind": "book", "book": book, "operation": operation, "profile": profile, "not_yet": not_yet}

    def test_row_B1_test_book_keeps_every_operation(self):
        cases = [self.book(TEST_BOOK, op) for op in ALL_OPERATIONS] + [self.book(TEST_BOOK)]
        cases += [self.book(TEST_BOOK, "write-once", profile) for profile in ("shopify_m1", "sync_p1", None)] + [self.book(TEST_BOOK, "write-once", not_yet=True)]
        self.assert_codes(cases, ["ok"] * len(cases))

    def test_row_B2_live_book_sync_lookup_is_admitted(self):
        self.assert_codes([self.book(LIVE_BOOK, "sync-lookup")], ["ok"])

    def test_row_B3_live_book_sync_changelog_is_admitted(self):
        self.assert_codes([self.book(LIVE_BOOK, "sync-changelog")], ["ok"])

    def test_row_B4_live_book_write_once_sync_p1_is_admitted(self):
        # Before stdin the profile is not known yet; it is checked again once read.
        self.assert_codes([self.book(LIVE_BOOK, "write-once", "sync_p1"), self.book(LIVE_BOOK, "write-once", not_yet=True)], ["ok", "ok"])

    def test_row_B5_live_book_write_once_with_another_profile_is_refused_before_the_claim(self):
        profiles = ["shopify_m1", "", None, "SYNC_P1", "sync_p1 ", "sync" + chr(0xAD) + "_p1", "sync_p2"]
        self.assert_codes([self.book(LIVE_BOOK, "write-once", p) for p in profiles], ["production_book_not_authorised"] * len(profiles))
        # End to end: the Core's shopify_m1 payload through the real entry point stops at the book
        # guard once the payload is read, before any session, login or claim.
        claims = self.temp / "claims"
        claims.mkdir()
        fence = "fence-" + "7" * 32
        payload = {"name": "Alpha Tester", "register_date": "2026-10-21", "expiry_date": "2027-10-21", "mobile_phone": None, "email_address": None}
        text = dl.canonical_payload(payload)
        salt = "b2" * 16
        request = {"fence_id": fence, "member_no": "ALPHA", "salt": salt, "payload_canonical": text, "payload_digest": dl.payload_digest(salt, text)}
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        extra = ["-TargetBook", LIVE_BOOK, "-FenceId", fence, "-ClaimDirectory", str(claims), "-WriteDeadlineUtc", deadline]
        for host in ("XB-CI-NOT-THE-VM", PINNED_HOST):
            with self.subTest(host=host):
                self.assertEqual(run_direct_offline("write-once", request, *extra, temp=self.temp, host=host), (1, {"error": "production_book_not_authorised"}))
        self.assertEqual(list(claims.iterdir()), [])

    def test_row_B6_live_book_next_member_no_is_refused(self):
        # Owner Q2 = A: never GetNextMemberNo on the live book.
        self.assert_codes([self.book(LIVE_BOOK, "next-member-no")], ["production_book_not_authorised"])

    def test_row_B7_live_book_other_operations_and_other_callers_are_refused(self):
        operations = ["book-check", "pos-sync-status", "legacy-precheck", "probe", "reconcile", None, "", "Sync-Lookup", "sync-lookup "]
        self.assert_codes([self.book(LIVE_BOOK, op) for op in operations], ["production_book_not_authorised"] * len(operations))

    def test_row_B8_lookalike_book_names_are_refused_in_both_layers(self):
        names = lookalikes(LIVE_BOOK) + lookalikes(TEST_BOOK)
        self.assert_codes([self.book(name, "sync-lookup") for name in names], ["target_book_not_authorised"] * len(names))
        identity = [{"kind": "identity", "book": book, "declared": name, "vendor": name} for book in (LIVE_BOOK, TEST_BOOK) for name in lookalikes(book)]
        self.assert_codes(identity, ["book_identity_wrong"] * len(identity))
        for name in names:
            with self.subTest(name=ascii(name)), self.assertRaises(dl.DirectLocalError) as caught:
                sync.check_book(name)
            self.assertEqual(caught.exception.code, "autocount_book_not_authorised")

    def test_row_B9_empty_other_and_lower_case_names_are_refused_in_both_layers(self):
        names = ["", "aed_xboundaries", "aed_testingapi", "AED_OTHER", "AED_XBOUNDARIES2"]
        self.assert_codes([self.book(name, "sync-lookup") for name in names], ["target_book_not_authorised"] * len(names))
        for name in names + [None, 5, [LIVE_BOOK]]:
            with self.subTest(name=name), self.assertRaises(dl.DirectLocalError) as caught:
                sync.check_book(name)
            self.assertEqual(caught.exception.code, "autocount_book_not_authorised")

    def test_row_B10_session_on_the_other_book_is_refused(self):
        self.assert_codes([
            {"kind": "identity", "book": LIVE_BOOK, "declared": TEST_BOOK, "vendor": TEST_BOOK},
            {"kind": "identity", "book": TEST_BOOK, "declared": LIVE_BOOK, "vendor": LIVE_BOOK},  # unchanged for test-book callers
            {"kind": "identity", "book": LIVE_BOOK, "declared": LIVE_BOOK, "vendor": LIVE_BOOK},
        ], ["book_identity_wrong", "production_book_refused", "ok"])

    def test_row_B11_declared_and_vendor_book_differ(self):
        self.assert_codes([
            {"kind": "identity", "book": LIVE_BOOK, "declared": LIVE_BOOK, "vendor": TEST_BOOK},
            {"kind": "identity", "book": LIVE_BOOK, "declared": TEST_BOOK, "vendor": LIVE_BOOK},
        ], ["book_identity_ambiguous", "book_identity_ambiguous"])

    def test_row_B12_sql_naming_the_production_book_is_refused(self):
        texts = ["SELECT COUNT_BIG(*) FROM AED_XBOUNDARIES.dbo.Member", "SELECT DB_NAME() FROM AED_XBOUNDARIES", "SELECT COUNT_BIG(*) FROM MemberType WHERE MemberType = 'AED_XBOUNDARIES'"]
        self.assert_codes([{"kind": "sql", "sql": text, "params": []} for text in texts], ["vendor_query_not_read_only"] * len(texts))

    def test_row_B13_member_type_must_exist_before_the_claim(self):
        text = ALLOWED_SQL_TEXTS["member_type_exists"]
        self.assertEqual((text.count("?"), re.fullmatch(r"[\x20-\x7E]+", text) is not None), (1, True))
        self.assert_codes([
            {"kind": "member_type", "type": "Default", "count": 0},
            {"kind": "member_type", "type": "Default", "count": 1},
            {"kind": "sql", "sql": text, "params": []},
            {"kind": "sql", "sql": text, "params": ["Default", "x"]},
            {"kind": "sql", "sql": text, "params": ["Default"]},
        ], ["member_type_unknown|1", "ok|1", "vendor_query_not_allowlisted", "vendor_query_not_allowlisted", "ok"])
        # Through the bridge (synthetic session) and the Python client: refused before the claim,
        # proven not written, the code named.
        bridge = SyncBridgeTests("test_lookup_single_row_and_empty")
        bridge.setUp()
        try:
            bridge.set_state(member_types=["REGISTERED"])
            fence = "fence-" + "c" * 32
            payload = {"profile": "sync_p1", "member_type": "Default", "name": "Alpha Tester", "mobile_phone": sg(9), "email_address": None, "register_date": "2026-10-21", "dob": None}
            code, out, _ = bridge.run_bridge("write-once", bridge.write_request(fence, payload, sg(9)), *bridge.write_args(fence))
            self.assertEqual((code, out), (1, {"error": "member_type_unknown"}))
            self.assertEqual(bridge.fake()["saves"], 0)
            self.assertFalse((bridge.claims / f"{fence}.claim").exists())
            last = bridge.fake()["params"][-1]  # the fake's JSON flattens a one-item array
            self.assertEqual(last if isinstance(last, list) else [last], ["Default"])
            client = sync.SyncBridgeClient(bridge.command(), autocount_book=TEST_BOOK, claims=bridge.claims, deadline_seconds=60)
            client.autocount_book = None  # the marked fake host takes no -TargetBook (test-only)
            self.assertEqual(client.create_member(sg(9), payload, write_id="write-" + "d" * 32), ("NOT_WRITTEN_PROVEN", "member_type_unknown"))
            self.assertEqual(bridge.fake()["saves"], 0)
        finally:
            bridge.tearDown()

    def test_row_B14_member_no_taken_is_refused_before_the_type_check_and_the_claim(self):
        bridge = SyncBridgeTests("test_lookup_single_row_and_empty")
        bridge.setUp()
        try:
            bridge.set_state(members=[{"MemberNo": sg(10), "Name": "Kilo Other"}], member_types=["Default"])
            fence = "fence-" + "f" * 32
            payload = {"profile": "sync_p1", "member_type": "Default", "name": "Alpha Tester", "mobile_phone": sg(10), "email_address": None, "register_date": "2026-10-21", "dob": None}
            code, out, _ = bridge.run_bridge("write-once", bridge.write_request(fence, payload, sg(10)), *bridge.write_args(fence))
            self.assertEqual((code, out), (1, {"error": "member_no_taken"}))
            self.assertEqual((bridge.fake()["saves"], bridge.fake()["sql"]), (0, []))
            self.assertFalse((bridge.claims / f"{fence}.claim").exists())
        finally:
            bridge.tearDown()

    def test_row_B15_outbound_queue_or_user_script_refuses_the_session(self):
        self.assert_codes([
            {"kind": "outbound", "user_script": 1, "mail": 0, "einvoice": 0},
            {"kind": "outbound", "user_script": 0, "mail": 2, "einvoice": 0},
            {"kind": "outbound", "user_script": 0, "mail": 0, "einvoice": 3},
            {"kind": "outbound", "user_script": 0, "mail": 0, "einvoice": 0},
        ], ["user_script_present", "outbound_queue_not_empty", "outbound_queue_not_empty", "ok"])

    def test_row_B16_host_pin_and_session_factory_env_still_refuse_the_live_book(self):
        empty = str(self.temp / "no-autocount")
        Path(empty).mkdir()
        session = {"kind": "session", "book": LIVE_BOOK, "acroot": empty, "operation": "sync-lookup", "profile": None}
        self.assert_codes([
            {**session, "host": "XB-CI-NOT-THE-VM", "factory": False},
            {**session, "host": PINNED_HOST, "factory": True},
            {**session, "host": PINNED_HOST, "factory": False},  # stops at the assembly pin: no login
            {**session, "host": PINNED_HOST, "factory": False, "operation": "probe"},
            {**session, "host": PINNED_HOST, "factory": False, "operation": "write-once", "profile": "shopify_m1"},
        ], ["host_not_authorised", "session_factory_env_refused", "autocount_build_not_qualified", "production_book_not_authorised", "production_book_not_authorised"])

    def test_row_B18_core_owner_console_and_e7_stay_test_book_only(self):
        # Their call shape (no operation) never reaches the live book, and their sources are the
        # merged ones (blob IDs pinned when the go-live round was built on main ac2d1d4).
        empty = str(self.temp / "no-autocount")
        Path(empty).mkdir()
        self.assert_codes([
            self.book(LIVE_BOOK),
            {"kind": "session", "book": LIVE_BOOK, "acroot": empty, "operation": None, "profile": None, "host": PINNED_HOST, "factory": False},
        ], ["production_book_not_authorised", "production_book_not_authorised"])
        pins = {
            "scripts/ac2_directlocal_owner_console.ps1": "bd044be3b220b57306f7fa7115e3b6b5bc2f79db",
            "scripts/ac2_directlocal_e7_testbook_create.ps1": "fea238051ebd9e654b578f1c4e77ee8c3daea450",
            "scripts/ac2_directlocal_e7_owner_console.ps1": "8fcf4b65d7f48e99a90408cf10c994ef31ed3740",
            "scripts/ac2_member_gateway_autocount_adapter.ps1": "590f9e6f808d288da9da11edbbd2b030f871dc1e",
        }
        git = shutil.which("git")
        if git is None:
            self.skipTest("git is not available")
        for path, blob in pins.items():
            with self.subTest(path=path):
                found = subprocess.run([git, "hash-object", path], cwd=ROOT, capture_output=True, check=True).stdout.decode().strip()
                self.assertEqual(found, blob)
        self.assertEqual(dl.ENVIRONMENT_BOOKS["development"], TEST_BOOK)
        for name in ("ac2_directlocal_owner_console.ps1", "ac2_directlocal_e7_owner_console.ps1"):
            self.assertIn('-cne "AED_TESTINGAPI"', (ROOT / "scripts" / name).read_text(encoding="utf-8"))


INSTALLER = ROOT / "scripts" / "install_member_sync_task.ps1"
TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


@unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell 5.1 is not installed")
class GoLiveInstallerTests(unittest.TestCase):
    """T1-T3 under Windows PowerShell 5.1, -DryRun behaviour only (the default:
    no -Register). Nothing is registered; Register-ScheduledTask is never
    reached (PS 5.1 module-shadow pitfall: no stand-in is used either). Fake
    account and a fake test-book config only."""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-installer-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.config = self.temp / "member_sync.json"
        self.config.write_text(json.dumps({
            "schema": sync.CONFIG_SCHEMA, "state_root": "C:\\XB\\xb155-member-sync-example", "autocount_book": TEST_BOOK,
            "shopify": {"store_gid": "gid://shopify/Shop/900000001", "shop_domain": "xb-sync-test.synthetic.invalid", "api_version": "2026-07",
                        "auth": {"method": "admin_api_access_token", "token_env": "XB_SHOPIFY_SYNC_TOKEN"}},
            "shopify_cutover_utc": "2026-11-01T00:00:00Z",
        }), encoding="utf-8")

    def run_installer(self, *extra: str):
        command = [shutil.which("powershell"), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
                   "-UserId", "EXAMPLE-PC\\example-user", "-ConfigPath", str(self.config), "-PythonExe", sys.executable,
                   "-TaskName", "XB-MemberSync-installer-test-never-registered", *extra]
        proc = subprocess.run(command, capture_output=True, timeout=180, check=False)
        return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")

    def task_xml(self, output: str):
        import xml.etree.ElementTree as ElementTree

        text = output.split("Task XML:", 1)[1].split("Nothing was registered.", 1)[0].strip()
        return ElementTree.fromstring(text.replace('encoding="UTF-16"', ""))

    def test_row_T1_s4u_and_other_logons_are_refused(self):
        for logon in ("S4U", "s4u", "Interactive", "InteractiveOrPassword", "password"):
            with self.subTest(logon=logon):
                code, out = self.run_installer("-LogonType", logon)
                self.assertNotEqual(code, 0)
                self.assertIn("LogonType must be Password", out)
                self.assertNotIn("Task XML:", out)

    def test_row_T2_default_is_a_dry_run_that_registers_nothing(self):
        code, out = self.run_installer()
        self.assertEqual(code, 0, out)
        self.assertIn("Dry run (the default)", out)
        self.assertIn("Nothing was registered.", out)
        source = INSTALLER.read_text(encoding="utf-8")
        dry = source.index("if (-not $Register) {")
        self.assertLess(dry, source.index("$credential = Get-Credential"))
        self.assertLess(dry, source.index("Register-ScheduledTask -TaskName"))
        self.assertEqual(source.count("Register-ScheduledTask -TaskName"), 1)
        self.assertIn("-Xml $taskXml", source)  # what -Register registers is the XML printed here

    def test_row_T3_task_shape(self):
        code, out = self.run_installer()
        self.assertEqual(code, 0, out)
        task = self.task_xml(out)
        ns = TASK_NS
        self.assertEqual(task.findtext(f"{ns}RegistrationInfo/{ns}URI"), "\\XB\\XB-MemberSync-installer-test-never-registered")
        principal = task.find(f"{ns}Principals/{ns}Principal")
        self.assertEqual((principal.findtext(f"{ns}LogonType"), principal.findtext(f"{ns}RunLevel"), principal.findtext(f"{ns}UserId")),
                         ("Password", "LeastPrivilege", "EXAMPLE-PC\\example-user"))
        settings = task.find(f"{ns}Settings")
        self.assertEqual((settings.findtext(f"{ns}MultipleInstancesPolicy"), settings.findtext(f"{ns}ExecutionTimeLimit")), ("IgnoreNew", "PT10M"))
        triggers = list(task.find(f"{ns}Triggers"))
        self.assertEqual([t.tag for t in triggers], [f"{ns}TimeTrigger"])
        self.assertEqual((triggers[0].findtext(f"{ns}Repetition/{ns}Interval"), triggers[0].findtext(f"{ns}Repetition/{ns}Duration")), ("PT15M", None))
        exec_ = task.find(f"{ns}Actions/{ns}Exec")
        self.assertEqual(Path(exec_.findtext(f"{ns}Command")), Path(sys.executable))
        arguments = exec_.findtext(f"{ns}Arguments")
        # The runner's temp folder may be an 8.3 short path, so only the shape is compared.
        self.assertRegex(arguments, r'^"[^"]+\\directlocal\\xb_member_sync\.py" run --config "[^"]+\\member_sync\.json" --direction both$')
        self.assertNotRegex(out, r"(?i)password\s*[:=]")
        # The interval: 15 by default, the safe minimum 10, nothing lower.
        code, out = self.run_installer("-IntervalMinutes", "10")
        self.assertEqual((code, self.task_xml(out).findtext(f"{ns}Triggers/{ns}TimeTrigger/{ns}Repetition/{ns}Interval")), (0, "PT10M"))
        for bad in ("9", "5", "0", "1441"):
            with self.subTest(interval=bad):
                code, out = self.run_installer("-IntervalMinutes", bad)
                self.assertNotEqual(code, 0)
                self.assertIn("safe minimum", out)

    def test_refuses_a_config_check_config_refuses(self):
        self.config.write_text(json.dumps({"schema": sync.CONFIG_SCHEMA}), encoding="utf-8")
        code, out = self.run_installer()
        self.assertNotEqual(code, 0)
        self.assertIn("refused by check-config", out)


if __name__ == "__main__":
    unittest.main()
