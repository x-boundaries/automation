"""#288 slice 3: the #155 member sync daily summary, one email each morning (read-only).

Runs once a day as its own scheduled task (\\XB\\XB-MemberSync-DailySummary, 06:40 SGT) on the
AC2 VM, as the sync's task account. It automates the manual daily check of the member sync:

1. the sync task's state (Get-ScheduledTask | Get-ScheduledTaskInfo, read only);
2. ``xb_member_sync.py check-config`` and ``xb_member_sync.py digest --hours 24`` as child
   processes (the only two sync commands it may run: neither takes the ledger lock, signs in or
   writes; ``status`` is never run because it takes the lock, G0 #288 section 6.2);
3. two counts-only SELECTs (members per type; new Member rows in ChangeLog in 24 hours) with
   Windows authentication, read-only application intent, on the sync's own book, after the same
   identity guard the stock export uses (never the sa login, database pinned);
4. a CLEAN or STOP decision from a closed list of stop codes, and the auto-off switch in OBSERVE
   mode only: it reports ``would_switch`` and never writes the sync config;
5. one closed event ``xb.member_sync.daily_summary.v1`` POSTed to n8n with a header secret read
   from a Windows Credential Manager target (name in the settings; the value never leaves memory);
6. a local record ``daily_summary_<date>.json`` in its own output folder, written before and
   after the send.

An email failure never fails the task: it exits 0 and the record says ``send_failed``. The event
carries counts, closed codes, booleans and MemberNo refs masked to their last 3 characters only.
Standard library only (pyodbc is imported only when the SQL lines are enabled).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping

SETTINGS_SCHEMA = "xb.member_sync.daily_summary.settings.v1"
EVENT_SCHEMA = "xb.member_sync.daily_summary.v1"
EVENT_NAME = "member_sync_daily_summary"
RECORD_SCHEMA = "xb.member_sync.daily_summary.record.v1"
RECORD_PREFIX = "daily_summary_"
SGT = timezone(timedelta(hours=8))

# The sync script is always the sibling file of this one (never a configured command).
SYNC_SCRIPT = Path(__file__).resolve().parent / "xb_member_sync.py"
# The only sync commands this script may start. Both are read-only by design: check-config
# reads the config file; digest opens the ledger read-only with no lock, migration or login.
SYNC_COMMANDS = ("check-config", "digest")
DIGEST_HOURS = 24
AUTHORISED_BOOKS = ("AED_TESTINGAPI", "AED_XBOUNDARIES")

# Read only: the state of the sync task. No other cmdlet may appear in this text (static test).
TASK_STATE_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$t = Get-ScheduledTask -TaskPath '\\XB\\' -TaskName 'XB-MemberSync'; "
    "$i = $t | Get-ScheduledTaskInfo; "
    "$c = [Globalization.CultureInfo]::InvariantCulture; "
    "[pscustomobject]@{ "
    "state = [string]$t.State; "
    "last_run = $i.LastRunTime.ToString('yyyy-MM-ddTHH:mm', $c); "
    "last_result = [int64]$i.LastTaskResult; "
    "next_run = $(if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-ddTHH:mm', $c) } else { $null }); "
    "missed_runs = [int64]$i.NumberOfMissedRuns "
    "} | ConvertTo-Json -Compress"
)
TASK_STATES = ("Ready", "Running", "Disabled", "Queued", "Unknown")
TASK_RESULTS_OK = (0, 3)  # 3: the run completed with alerts (the sync's own exit code)
TASK_RESULT_RUNNING = 267009  # 0x41301: a run is in progress right now
RUNS_PER_HOUR = 4  # the sync's 15-minute schedule
# The summary task's own ExecutionTimeLimit (PT5M in the installer). The settings are refused
# when the worst case of every wait (three children, the SQL part, the POST) does not fit inside
# it with a margin: a run killed by the limit leaves no record and no email.
TASK_TIME_LIMIT_SECONDS = 300
TASK_TIME_MARGIN_SECONDS = 15
CHILD_COUNT = 3  # the task-state read, check-config, digest
SQL_WAITS = 5  # the connect, then four statements, each bounded by sql.timeout_seconds
# Every outcome code of the sync's one Shopify mutation (customerCreate) that means a write was
# sent: created, or outcome unknown (#155 BF2a: the "separate customer without the phone" path
# is gone). Pinned against the sync's own source in a test, so a new code there fails CI.
SHOPIFY_WRITE_CODES = ("shopify_customer_created", "shopify_create_uncertain")

# SQL: fixed text, counts only. The identity row comes from the same connection.
IDENTITY_QUERY = (
    "SELECT SUSER_SNAME() AS actual_login, DB_NAME() AS actual_database, "
    "IS_SRVROLEMEMBER(N'sysadmin') AS is_sysadmin, IS_MEMBER(N'db_owner') AS is_db_owner"
)
SESSION_SETUP = "SET TRANSACTION ISOLATION LEVEL READ COMMITTED; SET LOCK_TIMEOUT 5000;"
MEMBERS_PER_TYPE_QUERY = "SELECT MemberType, COUNT_BIG(*) AS member_count FROM dbo.Member GROUP BY MemberType"
NEW_MEMBERS_QUERY = (
    "SELECT COUNT_BIG(*) AS new_members FROM dbo.ChangeLog "
    "WHERE TableName = N'Member' AND ChangeType = N'I' AND ChangeTime >= DATEADD(hour, -24, GETDATE())"
)
SQL_TEXTS = (IDENTITY_QUERY, SESSION_SETUP, MEMBERS_PER_TYPE_QUERY, NEW_MEMBERS_QUERY)
ODBC_DRIVERS = ("ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server")
SQL_REASONS = ("disabled", "driver_missing", "connect_failed", "identity_refused", "book_mismatch",
               "permission_denied", "query_failed", "config_unavailable")

# The closed STOP list (pinned in tests). Order is the order in the email.
STOP_CODES = (
    "task_state_unavailable",     # the sync task could not be read
    "task_last_result",           # LastTaskResult not 0 or 3 (and no run in progress)
    "task_disabled",              # the sync task is Disabled (a dead sync must not read CLEAN)
    "check_config_failed",        # check-config refused or did not answer
    "config_unexpected",          # shopify creates not off, another member type, or another state root
    "digest_failed",              # digest refused or did not answer
    "no_runs",                    # no completed run in 24 hours (a dead sync must not read CLEAN)
    "unfinished_runs",            # more than 1 unfinished run in 24 hours
    "shopify_write",              # any Shopify write (SHOPIFY_WRITE_CODES)
    "uncertain_writes",           # uncertain writes open
    "possible_duplicate_member",  # the alert, any count
    "member_type_unknown",        # the alert, any count
    "login_refused",              # an autocount_* or bridge_read_failed* alert
    "writes_over_cap",            # more writes in a clock hour than the run caps allow
    "alert_unexpected",           # an alert code not on the expected list
)
# Alerts the manual check expects (not a stop): exact codes plus the memberno_* family.
EXPECTED_ALERTS = ("waiting_for_staff", "created_after_wait", "no_phone_sign_up_needs_staff", "max_writes_per_run_reached")
EXPECTED_ALERT_PREFIXES = ("memberno_",)
LOGIN_ALERT_PREFIXES = ("autocount_", "bridge_read_failed")
STOP_ALERTS = ("possible_duplicate_member", "member_type_unknown")
# Auto off (G0 6.4): observe mode only in this round. The three clearest conditions.
AUTO_OFF_CODES = ("shopify_write_while_disabled", "uncertain_writes", "possible_duplicate_member")
AUTO_OFF_MODES = ("off", "would_switch")

SEND_STATUSES = ("pending", "sent", "send_failed", "secret_unavailable", "disabled", "not_sent_check_only")
CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
MEMBER_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
MASKED_RE = re.compile(r"^\*{3}[A-Za-z0-9]{1,3}$")
LOCAL_MINUTE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
HEADER_RE = re.compile(r"^X-[A-Za-z0-9-]{1,60}$")
CREDENTIAL_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,127}$")
SERVER_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}(\\[A-Za-z0-9_$-]{1,63})?(,[0-9]{1,5})?$")
COUNT_MAX = 10 ** 9
MAX_ALERT_CODES = 20
MAX_ALERT_REFS = 5
MAX_MEMBER_TYPES = 12
UNTYPED = "-"
UNREADABLE_ALERT = "unreadable_alert_code"
OTHER_ALERTS = "other_alert_codes"

_SETTINGS_KEYS = {"schema", "sync_config_path", "sync_state_root", "output_dir", "expected_member_type",
                  "first_create_already_seen", "child_timeout_seconds", "sql", "send"}
# #155 BF2b (design 2.12): optional, so a settings file without them reads exactly as before.
# shopify_creates_expected (default false): S6 creates are on (the sitting is done), so
# shopify_create_enabled true is not config_unexpected and a live create is counted, not a stop.
# backfill_members_window (default null): the dates (Singapore, inclusive) of the attended
# backfill-members sittings and the caps declared for them; backfill creates are judged against
# it, and are a stop outside it.
_SETTINGS_OPTIONAL = {"shopify_creates_expected", "backfill_members_window"}
_WINDOW_KEYS = {"from", "to", "max_per_call", "max_per_hour"}
BACKFILL_WINDOW_MAX_DAYS = 14
BACKFILL_MAX_PER_CALL = 25  # the sync's own bounds (design 2.7)
BACKFILL_MAX_PER_HOUR = 60
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The outcome codes of the sync's one Shopify mutation that the creates-on rules read: a create
# that landed (counted), and the two a person must look at (a stop).
SHOPIFY_CREATED = "shopify_customer_created"
SHOPIFY_UNCERTAIN = "shopify_create_uncertain"
SHOPIFY_REJECTED = "shopify_create_rejected"
# Shown in the email's alert line (the n8n event is closed: no new key) when a backfill call ran.
BACKFILL_SHOWN = "backfill_members_creates"
_SQL_KEYS = {"enabled", "server", "odbc_driver", "encrypt", "trust_server_certificate", "allow_privileged_identity",
             "timeout_seconds"}
_SEND_KEYS = {"enabled", "webhook_url", "auth_header_name", "secret_credential_target", "timeout_seconds"}


class SummaryError(Exception):
    """A refusal with a fixed code (printed; never a path, name or secret)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------

def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SummaryError(code)


def _bool(value: Any, code: str) -> bool:
    _require(isinstance(value, bool), code)
    return value


def _int(value: Any, low: int, high: int, code: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool) and low <= value <= high, code)
    return value


def _absolute(value: Any, code: str) -> str:
    _require(isinstance(value, str) and value.strip() == value and value != "", code)
    _require(PureWindowsPath(value).is_absolute() or Path(value).is_absolute(), code)
    return value


def _pure(value: str) -> PureWindowsPath | PurePosixPath:
    """The path as written (no file is touched): a Windows path compares without case."""

    return PureWindowsPath(value) if PureWindowsPath(value).is_absolute() else PurePosixPath(value)


def _folder(value: Any, code: str) -> str:
    """An absolute folder path with no ``..`` part, so it can be compared as written."""

    _absolute(value, code)
    _require(".." not in _pure(value).parts, code)
    return value


def _written_inside(folder: str, root: str) -> bool:
    child, parent = _pure(folder), _pure(root)
    return type(child) is type(parent) and (child == parent or parent in child.parents)


def parse_settings(value: Any) -> dict[str, Any]:
    """Structural check of the settings file. No file, network or process is touched."""

    _require(isinstance(value, dict) and _SETTINGS_KEYS <= set(value) <= _SETTINGS_KEYS | _SETTINGS_OPTIONAL, "settings_keys_invalid")
    _require(value["schema"] == SETTINGS_SCHEMA, "settings_schema_invalid")
    settings = {
        "sync_config_path": _absolute(value["sync_config_path"], "settings_sync_config_path_invalid"),
        "sync_state_root": _folder(value["sync_state_root"], "settings_sync_state_root_invalid"),
        "output_dir": _folder(value["output_dir"], "settings_output_dir_invalid"),
        "first_create_already_seen": _bool(value["first_create_already_seen"], "settings_first_create_invalid"),
        "child_timeout_seconds": _int(value["child_timeout_seconds"], 10, 600, "settings_child_timeout_invalid"),
        "shopify_creates_expected": _bool(value.get("shopify_creates_expected", False), "settings_shopify_creates_expected_invalid"),
        "backfill_members_window": _window(value.get("backfill_members_window")),
    }
    # The summary never writes into the sync's private state: the record folder is checked
    # against the state root named here, not against the live sync config (a broken live config
    # must still give a STOP email, not a refusal before anything is read).
    _require(not _written_inside(settings["output_dir"], settings["sync_state_root"]), "output_dir_inside_sync_state")
    expected = value["expected_member_type"]
    _require(isinstance(expected, str) and MEMBER_TYPE_RE.fullmatch(expected) is not None, "settings_expected_member_type_invalid")
    settings["expected_member_type"] = expected

    sql = value["sql"]
    _require(isinstance(sql, dict) and set(sql) == _SQL_KEYS, "settings_sql_keys_invalid")
    _require(isinstance(sql["server"], str) and SERVER_RE.fullmatch(sql["server"]) is not None, "settings_sql_server_invalid")
    _require(sql["odbc_driver"] in ODBC_DRIVERS, "settings_sql_driver_invalid")
    settings["sql"] = {
        "enabled": _bool(sql["enabled"], "settings_sql_enabled_invalid"),
        "server": sql["server"],
        "odbc_driver": sql["odbc_driver"],
        "encrypt": _bool(sql["encrypt"], "settings_sql_encrypt_invalid"),
        "trust_server_certificate": _bool(sql["trust_server_certificate"], "settings_sql_trust_invalid"),
        "allow_privileged_identity": _bool(sql["allow_privileged_identity"], "settings_sql_privileged_invalid"),
        "timeout_seconds": _int(sql["timeout_seconds"], 5, 120, "settings_sql_timeout_invalid"),
    }

    send = value["send"]
    _require(isinstance(send, dict) and set(send) == _SEND_KEYS, "settings_send_keys_invalid")
    enabled = _bool(send["enabled"], "settings_send_enabled_invalid")
    url = send["webhook_url"]
    _require(isinstance(url, str) and re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/webhook/[A-Za-z0-9_/-]{1,200}", url) is not None,
             "settings_send_url_invalid")
    _require(not enabled or "REPLACE_WITH_" not in url, "settings_send_url_placeholder")
    _require(isinstance(send["auth_header_name"], str) and HEADER_RE.fullmatch(send["auth_header_name"]) is not None,
             "settings_send_header_invalid")
    target = send["secret_credential_target"]
    _require(isinstance(target, str) and CREDENTIAL_TARGET_RE.fullmatch(target) is not None, "settings_send_target_invalid")
    settings["send"] = {
        "enabled": enabled, "webhook_url": url, "auth_header_name": send["auth_header_name"],
        "secret_credential_target": target,
        "timeout_seconds": _int(send["timeout_seconds"], 5, 60, "settings_send_timeout_invalid"),
    }
    _require(worst_case_seconds(settings) <= TASK_TIME_LIMIT_SECONDS - TASK_TIME_MARGIN_SECONDS, "settings_timeouts_exceed_task_limit")
    return settings


def _window(value: Any) -> dict[str, Any] | None:
    """#155 BF2b: null, or the declared backfill window: ``from`` and ``to`` (Singapore dates,
    inclusive, at most BACKFILL_WINDOW_MAX_DAYS days) and the caps declared for it (as the
    backfill config copy's ``backfill_members_max_per_call`` / ``_per_hour``)."""

    code = "settings_backfill_members_window_invalid"
    if value is None:
        return None
    _require(isinstance(value, dict) and set(value) == _WINDOW_KEYS, code)
    days = []
    for key in ("from", "to"):
        _require(isinstance(value[key], str) and DATE_RE.fullmatch(value[key]) is not None, code)
        try:
            days.append(date.fromisoformat(value[key]))
        except ValueError:
            raise SummaryError(code) from None
    _require(days[0] <= days[1] and (days[1] - days[0]).days < BACKFILL_WINDOW_MAX_DAYS, code)
    per_call = _int(value["max_per_call"], 1, BACKFILL_MAX_PER_CALL, code)
    per_hour = _int(value["max_per_hour"], 1, BACKFILL_MAX_PER_HOUR, code)
    _require(per_call <= per_hour, code)
    return {"from": days[0].isoformat(), "to": days[1].isoformat(), "max_per_call": per_call, "max_per_hour": per_hour}


def window_in_effect(window: Mapping[str, Any] | None, today: str) -> bool:
    """The 24 hours before this morning's summary (yesterday and today, Singapore) touch the
    declared window."""

    if window is None:
        return False
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    return window["from"] <= today and yesterday <= window["to"]


def worst_case_seconds(settings: Mapping[str, Any]) -> int:
    """The longest one run can wait: every child, the SQL part (when enabled) and the POST."""

    sql = settings["sql"]["timeout_seconds"] * SQL_WAITS if settings["sql"]["enabled"] else 0
    return settings["child_timeout_seconds"] * CHILD_COUNT + sql + settings["send"]["timeout_seconds"]


def load_settings(path: str | Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SummaryError("settings_unreadable") from None
    return parse_settings(raw)


def _resolved_inside(folder: Path, root: Path) -> bool:
    return folder == root or root in folder.parents


def check_output_dir(settings: Mapping[str, Any]) -> Path:
    """The record folder must exist and must not sit inside the sync's state root named in the
    settings (links resolved). The live sync config is not read here."""

    out = Path(settings["output_dir"])
    _require(out.is_dir(), "output_dir_missing")
    resolved = out.resolve()
    _require(not _resolved_inside(resolved, Path(settings["sync_state_root"]).resolve()), "output_dir_inside_sync_state")
    return resolved


def read_live_state_root(settings: Mapping[str, Any]) -> Path | None:
    """The ``state_root`` of the live sync config, read the way the sync reads it (read only).
    None when the file is unreadable, not JSON or has no state root: check-config then refuses
    and the email says STOP check_config_failed."""

    try:
        value = json.loads(Path(settings["sync_config_path"]).read_bytes().decode("utf-8"))
        state_root = value["state_root"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None
    if not isinstance(state_root, str) or state_root == "":
        return None
    try:
        return Path(state_root).resolve()
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------------------------
# Read-only sources (each injectable for the offline tests)
# --------------------------------------------------------------------------------------------

def run_child(argv: list[str], timeout: int) -> tuple[int, str]:
    """One child process, no shell, output captured. Returns (exit code, stdout)."""

    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False, check=False)
    return done.returncode, done.stdout


def _last_json_line(text: str) -> Any:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def run_sync(command: str, settings: Mapping[str, Any], runner: Callable[[list[str], int], tuple[int, str]]) -> dict[str, Any] | None:
    """Runs one allowlisted sync command. Returns its JSON answer, or None when it refused,
    failed or did not answer in time."""

    if command not in SYNC_COMMANDS:
        raise SummaryError("sync_command_not_allowed")
    argv = [sys.executable, str(SYNC_SCRIPT), command, "--config", settings["sync_config_path"]]
    if command == "digest":
        argv += ["--hours", str(DIGEST_HOURS)]
    try:
        code, out = runner(argv, settings["child_timeout_seconds"])
    except (OSError, subprocess.SubprocessError):
        return None
    answer = _last_json_line(out)
    if code != 0 or not isinstance(answer, dict) or answer.get("status") != "ok":
        return None
    return answer


def read_task_state(settings: Mapping[str, Any], runner: Callable[[list[str], int], tuple[int, str]]) -> dict[str, Any] | None:
    shell = str(Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    try:
        code, out = runner([shell, "-NoProfile", "-NonInteractive", "-Command", TASK_STATE_SCRIPT], settings["child_timeout_seconds"])
    except (OSError, subprocess.SubprocessError):
        return None
    answer = _last_json_line(out)
    return answer if code == 0 and isinstance(answer, dict) else None


def build_connection_string(sql: Mapping[str, Any], book: str) -> str:
    """Windows authentication only, read-only intent, the sync's own book. No user or password."""

    return ";".join([
        "DRIVER={%s}" % sql["odbc_driver"],
        "SERVER=%s" % sql["server"],
        "DATABASE=%s" % book,
        "Trusted_Connection=yes",
        "ApplicationIntent=ReadOnly",
        "APP=xb_member_sync_daily_summary",
        "Encrypt=%s" % ("yes" if sql["encrypt"] else "no"),
        "TrustServerCertificate=%s" % ("yes" if sql["trust_server_certificate"] else "no"),
    ]) + ";"


def check_sql_identity(row: Any, book: str, allow_privileged: bool) -> str | None:
    """The stock export's identity guard, reduced to flags: returns a refusal reason or None."""

    login, database, sysadmin, db_owner = (list(row) + [None] * 4)[:4]
    if str(login or "").strip().lower() == "sa":
        return "identity_refused"  # never the sa login, not configurable
    if str(database or "").strip() != book:
        return "book_mismatch"
    privileged = sysadmin != 0 or db_owner != 0  # NULL (undeterminable) counts as privileged
    if privileged and not allow_privileged:
        return "identity_refused"
    return None


def _sql_error_reason(exc: BaseException) -> str:
    text = " ".join(str(a) for a in getattr(exc, "args", ()))
    return "permission_denied" if ("229" in text or "permission was denied" in text.lower()) else "query_failed"


def read_sql_counts(sql: Mapping[str, Any], book: str, connect: Callable[[str, int], Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two counts-only SELECTs. Returns (members_per_type, new_members_24h) sections; a part
    that cannot be read is ``not_available`` with a closed reason, and the summary still sends."""

    def unavailable(reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return members_section(None, reason), new_members_section(None, None, reason)

    if not sql["enabled"]:
        return unavailable("disabled")
    if book not in AUTHORISED_BOOKS:
        return unavailable("book_mismatch")
    error_type: type[BaseException] = Exception
    if connect is None:
        try:
            import pyodbc  # type: ignore
        except ImportError:
            return unavailable("driver_missing")
        error_type = pyodbc.Error

        def connect(conn_str: str, timeout: int) -> Any:
            opened = pyodbc.connect(conn_str, autocommit=True, timeout=timeout)
            opened.timeout = timeout  # the query timeout of every statement (as the stock export sets it)
            return opened
    try:
        connection = connect(build_connection_string(sql, book), sql["timeout_seconds"])
    except error_type:
        return unavailable("connect_failed")
    try:
        try:
            cursor = connection.cursor()
            cursor.execute(SESSION_SETUP)
            cursor.execute(IDENTITY_QUERY)
            refusal = check_sql_identity(cursor.fetchone(), book, sql["allow_privileged_identity"])
        except error_type as exc:
            return unavailable(_sql_error_reason(exc))
        if refusal is not None:
            return unavailable(refusal)
        try:
            cursor.execute(MEMBERS_PER_TYPE_QUERY)
            members = members_section(cursor.fetchall(), None)
        except error_type as exc:
            members = members_section(None, _sql_error_reason(exc))
        try:
            cursor.execute(NEW_MEMBERS_QUERY)
            new_total = int(cursor.fetchone()[0])
            new_reason = None
        except error_type as exc:
            new_total, new_reason = None, _sql_error_reason(exc)
        return members, new_members_section(new_total, None, new_reason)
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing a broken connection must not mask the result
            pass


def members_section(rows: Any, reason: str | None) -> dict[str, Any]:
    if rows is None:
        return {"available": False, "reason": reason, "counts": None}
    counts: dict[str, int] = {}
    for member_type, number in rows:
        key = UNTYPED if member_type is None or str(member_type).strip() == "" else str(member_type).strip()
        if key != UNTYPED and not _safe_code(key, MEMBER_TYPE_RE):
            key = "other"
        counts[key] = min(counts.get(key, 0) + int(number), COUNT_MAX)
    if len(counts) > MAX_MEMBER_TYPES:
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        counts = dict(ordered[:MAX_MEMBER_TYPES - 1])
        counts["other"] = min(counts.get("other", 0) + sum(n for _, n in ordered[MAX_MEMBER_TYPES - 1:]), COUNT_MAX)
    return {"available": True, "reason": None, "counts": dict(sorted(counts.items()))}


def new_members_section(total: int | None, by_sync: int | None, reason: str | None) -> dict[str, Any]:
    if total is None:
        return {"available": False, "reason": reason, "total": None, "by_sync": None, "by_staff": None}
    return {"available": True, "reason": None, "total": min(total, COUNT_MAX), "by_sync": by_sync, "by_staff": None}


# --------------------------------------------------------------------------------------------
# Decision and event
# --------------------------------------------------------------------------------------------

def _safe_code(value: Any, pattern: re.Pattern[str]) -> bool:
    """A closed-format code with no phone-length digit run (6 or more digits in a row)."""

    return isinstance(value, str) and pattern.fullmatch(value) is not None and re.search(r"[0-9]{6,}", value) is None


def _count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value:
        return min(value, COUNT_MAX)
    return None


def _local_minute(value: Any) -> str | None:
    if isinstance(value, str) and LOCAL_MINUTE_RE.fullmatch(value[:16]):
        return value[:16]
    return None


def task_section(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {"available": False, "state": None, "last_run": None, "last_result": None, "next_run": None, "missed_runs": None}
    state = raw.get("state")
    result = raw.get("last_result")
    return {
        "available": True,
        "state": state if state in TASK_STATES else "Unknown",
        "last_run": _local_minute(raw.get("last_run")),
        "last_result": result if isinstance(result, int) and not isinstance(result, bool) and 0 <= result < 2 ** 32 else None,
        "next_run": _local_minute(raw.get("next_run")),
        "missed_runs": _count(raw.get("missed_runs")),
    }


def config_section(raw: Mapping[str, Any] | None, state_root_ok: bool | None) -> dict[str, Any]:
    """``state_root_ok``: whether the live config's state root is the one in the settings (None
    when it could not be read). Only the flag enters the event, never a path."""

    keys = ("ac2_create_enabled", "shopify_create_enabled", "immediate_create_enabled")
    if raw is None:
        return {"available": False, "autocount_book": None, **{k: None for k in keys}, "max_writes_per_run": None,
                "max_creates_per_hour": None, "ac2_create_member_type": None, "state_root_ok": None}
    member_type = raw.get("ac2_create_member_type")
    book = raw.get("autocount_book")
    return {
        "available": True,
        "autocount_book": book if book in AUTHORISED_BOOKS else None,
        **{k: raw.get(k) if isinstance(raw.get(k), bool) else None for k in keys},
        "max_writes_per_run": _count(raw.get("max_writes_per_run")),
        "max_creates_per_hour": _count(raw.get("max_creates_per_hour")),
        "ac2_create_member_type": member_type if _safe_code(member_type, MEMBER_TYPE_RE) else None,
        "state_root_ok": state_root_ok if isinstance(state_root_ok, bool) else None,
    }


def _masked(value: Any) -> str | None:
    """The sync prints a MemberNo as stars plus its last 3 characters. Re-masked here to exactly
    ``***`` plus those 3, so no longer value can ever pass through."""

    if not isinstance(value, str) or not re.fullmatch(r"\*{3,}[^*]{1,3}", value):
        return None
    masked = "***" + value.lstrip("*")[-3:]
    return masked if MASKED_RE.fullmatch(masked) else None


def digest_section(raw: Mapping[str, Any] | None, links_yesterday: int | None) -> dict[str, Any]:
    fields = ("runs_completed", "runs_unfinished", "duration_max_s", "duration_median_s", "ac2_creates", "created_immediately",
              "created_after_wait", "shopify_writes", "possible_duplicate_member", "writes_per_hour_max", "links",
              "ac2_pending", "ac2_pending_would_create", "shopify_pending", "uncertain_writes")
    if raw is None:
        return {"available": False, **{f: None for f in fields}, "links_yesterday": links_yesterday, "alerts": None, "alert_refs": None}

    def get(*path: str) -> Any:
        value: Any = raw
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        return value

    counts = get("counts") if isinstance(get("counts"), dict) else {}
    alerts_raw = get("alerts") if isinstance(get("alerts"), dict) else {}
    # Every alert code is kept for the decision (an unreadable one is counted under its own code,
    # which is not on the expected list, so it still stops). The email list is bounded later.
    alerts: dict[str, int] = {}
    for code, number in sorted(alerts_raw.items()):
        if _count(number) is None:
            continue
        key = code if _safe_code(code, CODE_RE) else UNREADABLE_ALERT
        alerts[key] = min(alerts.get(key, 0) + _count(number), COUNT_MAX)
    refs = []
    for entry in get("alert_refs") if isinstance(get("alert_refs"), list) else []:
        if len(refs) >= MAX_ALERT_REFS:
            break
        if not isinstance(entry, dict) or not _safe_code(entry.get("code"), CODE_RE):
            continue
        masked = _masked(entry.get("member_no_masked"))
        if masked is not None:
            refs.append({"code": entry["code"], "member_no": masked})

    def counted(code: str) -> int:
        return _count(counts.get(code)) or 0

    return {
        "available": True,
        "runs_completed": _count(get("runs", "completed")),
        "runs_unfinished": _count(get("runs", "unfinished")),
        "duration_max_s": _count(get("runs", "duration_seconds_max")),
        "duration_median_s": _count(get("runs", "duration_seconds_median")),
        "ac2_creates": counted("ac2_member_created"),
        "created_immediately": counted("created_immediately"),
        "created_after_wait": counted("created_after_wait"),
        "shopify_writes": min(sum(counted(code) for code in SHOPIFY_WRITE_CODES), COUNT_MAX),
        "possible_duplicate_member": alerts.get("possible_duplicate_member", 0),
        "writes_per_hour_max": _count(get("creates", "writes_per_hour_max")),
        "links": _count(get("open", "links")),
        "links_yesterday": links_yesterday,
        "ac2_pending": _count(get("open", "ac2_pending")),
        "ac2_pending_would_create": _count(get("open", "ac2_pending_would_create")),
        "shopify_pending": _count(get("open", "shopify_pending")),
        "uncertain_writes": _count(get("open", "uncertain_writes")),
        "alerts": alerts,
        "alert_refs": refs,
    }


def creates_section(raw: Mapping[str, Any] | None, *, expected: bool, window: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """#155 BF2b (design 2.12): the Shopify create outcomes split into the live runs' and the
    backfill calls' (the digest's ``backfill_members`` part, present only when a call ran).
    ``window``: the declared backfill window when it is in effect for this summary, else None.
    None (the BF2a rules, unchanged) when creates are not expected and no window is in effect,
    or when the digest could not be read. Never enters the event (its shape is closed)."""

    if (not expected and window is None) or not isinstance(raw, dict):
        return None
    counts = raw.get("counts") if isinstance(raw.get("counts"), dict) else {}
    total = {code: _count(counts.get(code)) or 0 for code in (SHOPIFY_CREATED, SHOPIFY_UNCERTAIN, SHOPIFY_REJECTED)}
    part = raw.get("backfill_members")
    backfill = None
    if isinstance(part, dict):
        backfill = {key: _count(part.get(key)) or 0 for key in ("calls", "creates", "uncertain", "rejected", "writes",
                                                                 "writes_per_call_max", "writes_per_hour_max", "live_writes_per_hour_max")}
    # Inside a window the backfill's outcomes are judged apart; outside one they count as live.
    apart = backfill if window is not None else None
    live = {
        "created": max(total[SHOPIFY_CREATED] - (apart["creates"] if apart else 0), 0),
        "uncertain": max(total[SHOPIFY_UNCERTAIN] - (apart["uncertain"] if apart else 0), 0),
        "rejected": max(total[SHOPIFY_REJECTED] - (apart["rejected"] if apart else 0), 0),
    }
    return {"expected": expected, "window": dict(window) if window is not None else None, "live": live, "backfill": backfill}


def _bounded(alerts: Mapping[str, int] | None) -> dict[str, int] | None:
    """At most MAX_ALERT_CODES entries in the email; the rest are summed as other_alert_codes."""

    if alerts is None or len(alerts) <= MAX_ALERT_CODES:
        return None if alerts is None else dict(alerts)
    items = sorted(alerts.items())
    kept = dict(items[:MAX_ALERT_CODES - 1])
    kept[OTHER_ALERTS] = min(sum(n for _, n in items[MAX_ALERT_CODES - 1:]), COUNT_MAX)
    return kept


def _alert_is_expected(code: str) -> bool:
    return code in EXPECTED_ALERTS or code.startswith(EXPECTED_ALERT_PREFIXES)


def decide(task: Mapping[str, Any], config: Mapping[str, Any], digest: Mapping[str, Any], expected_member_type: str,
           creates: Mapping[str, Any] | None = None) -> tuple[list[str], list[str]]:
    """Returns (stop_reasons, auto_off_reasons), each in the pinned order of its closed list.
    ``creates`` (#155 BF2b, ``creates_section``): None keeps the BF2a rules exactly."""

    stops: set[str] = set()
    auto_off: set[str] = set()
    expected = creates is not None and creates["expected"]
    if not task["available"]:
        stops.add("task_state_unavailable")
    else:
        if task["last_result"] not in TASK_RESULTS_OK and task["last_result"] != TASK_RESULT_RUNNING:
            stops.add("task_last_result")
        if task["state"] == "Disabled":
            stops.add("task_disabled")
    if not config["available"]:
        stops.add("check_config_failed")
    elif (not (config["shopify_create_enabled"] is False or (expected and config["shopify_create_enabled"] is True))
          or config["ac2_create_member_type"] != expected_member_type or config["state_root_ok"] is not True):
        # BF2b: with shopify_creates_expected, true is expected too (false is the rollback).
        stops.add("config_unexpected")
    if not digest["available"]:
        stops.add("digest_failed")
    else:
        if (digest["runs_completed"] or 0) < 1:
            stops.add("no_runs")
        if (digest["runs_unfinished"] or 0) > 1:
            stops.add("unfinished_runs")
        if creates is None:
            if digest["shopify_writes"] > 0:
                stops.add("shopify_write")
                if config["shopify_create_enabled"] is False:
                    auto_off.add("shopify_write_while_disabled")
        else:
            stops |= _create_stops(creates, config, auto_off)
        if (digest["uncertain_writes"] or 0) > 0:
            stops.add("uncertain_writes")
            auto_off.add("uncertain_writes")
        if digest["possible_duplicate_member"] > 0:
            stops.add("possible_duplicate_member")
            auto_off.add("possible_duplicate_member")
        for code in digest["alerts"]:
            if code in STOP_ALERTS:
                stops.add(code)
            elif code.startswith(LOGIN_ALERT_PREFIXES):
                stops.add("login_refused")
            elif not _alert_is_expected(code):
                stops.add("alert_unexpected")
        per_run = config["max_writes_per_run"] if config["available"] else None
        live_hour_max = digest["writes_per_hour_max"]
        if creates is not None and creates["window"] is not None and creates["backfill"] is not None:
            live_hour_max = creates["backfill"]["live_writes_per_hour_max"]  # BF2b: the backfill's hours apart
        if per_run is not None and live_hour_max is not None:
            # The digest has no per-run write count; a clock hour holds at most RUNS_PER_HOUR
            # scheduled runs (more only on the capped immediate path).
            hour_cap = max(per_run * RUNS_PER_HOUR, config["max_creates_per_hour"] or 0)
            if live_hour_max > hour_cap:
                stops.add("writes_over_cap")
    return [c for c in STOP_CODES if c in stops], [c for c in AUTO_OFF_CODES if c in auto_off]


def _create_stops(creates: Mapping[str, Any], config: Mapping[str, Any], auto_off: set[str]) -> set[str]:
    """#155 BF2b (design 2.12), with creates expected or a backfill window in effect:
    - live S6 creates are counted, not a stop, only when creates are expected; an uncertain or
      rejected live create always stops (shopify_write), and so does any live create while the
      config says creates are off (and the auto-off reason, as before);
    - backfill calls' creates are counted inside the declared window; outside one any backfill
      write stops; an uncertain or rejected one stops; more writes in one call or one clock hour
      than the window declares is writes_over_cap."""

    stops: set[str] = set()
    live, backfill, window = creates["live"], creates["backfill"], creates["window"]
    if live["created"] + live["uncertain"] > 0 and (not creates["expected"] or config["shopify_create_enabled"] is False):
        stops.add("shopify_write")
        if config["shopify_create_enabled"] is False:
            auto_off.add("shopify_write_while_disabled")
    if live["uncertain"] > 0 or live["rejected"] > 0:
        stops.add("shopify_write")
    if backfill is not None:
        sent = backfill["creates"] + backfill["uncertain"] + backfill["rejected"] + backfill["writes"]
        if window is None:
            if sent > 0:
                stops.add("shopify_write")  # a backfill write outside a declared window
        else:
            if backfill["uncertain"] > 0 or backfill["rejected"] > 0:
                stops.add("shopify_write")
            if backfill["writes_per_call_max"] > window["max_per_call"] or backfill["writes_per_hour_max"] > window["max_per_hour"]:
                stops.add("writes_over_cap")
    return stops


def build_event(*, now: datetime, task: Mapping[str, Any], config: Mapping[str, Any], digest: Mapping[str, Any],
                members: Mapping[str, Any], new_members: Mapping[str, Any], first_create: bool,
                expected_member_type: str, creates: Mapping[str, Any] | None = None) -> dict[str, Any]:
    stops, auto_off = decide(task, config, digest, expected_member_type, creates)
    local = now.astimezone(SGT)
    new_members = dict(new_members)
    if new_members["available"] and digest["available"]:
        new_members["by_sync"] = digest["ac2_creates"]
        new_members["by_staff"] = max(new_members["total"] - digest["ac2_creates"], 0)
    alerts = digest["alerts"]
    if creates is not None and creates["backfill"] is not None and alerts is not None:
        # BF2b: the backfill's creates shown apart in the email (its alert line; the event's
        # shape and the n8n validator are unchanged). Added after the decision: not an alert.
        alerts = {**alerts, BACKFILL_SHOWN: creates["backfill"]["creates"]}
    return {
        "schema": EVENT_SCHEMA,
        "event": EVENT_NAME,
        "summary_date": local.strftime("%Y-%m-%d"),
        "generated_at": local.strftime("%Y-%m-%dT%H:%M+08:00"),
        "result": "STOP" if stops else "CLEAN",
        "stop_reasons": stops,
        "auto_off": "would_switch" if auto_off else "off",
        "auto_off_reasons": auto_off,
        "task": dict(task),
        "config": dict(config),
        "digest": {**digest, "alerts": _bounded(alerts)},
        "members_per_type": dict(members),
        "new_members_24h": new_members,
        "first_create": first_create,
        "owner_session": "not_checked",
    }


EVENT_KEYS = ("schema", "event", "summary_date", "generated_at", "result", "stop_reasons", "auto_off", "auto_off_reasons",
              "task", "config", "digest", "members_per_type", "new_members_24h", "first_create", "owner_session")


def check_event(event: Mapping[str, Any]) -> None:
    """Last guard before the POST: the closed shape, and no value that could be personal data."""

    _require(tuple(event) == EVENT_KEYS, "event_keys_invalid")
    _require(event["result"] in ("CLEAN", "STOP") and (event["result"] == "STOP") == bool(event["stop_reasons"]), "event_result_invalid")
    _require(all(code in STOP_CODES for code in event["stop_reasons"]), "event_stop_reason_invalid")
    _require(event["auto_off"] in AUTO_OFF_MODES and all(code in AUTO_OFF_CODES for code in event["auto_off_reasons"]), "event_auto_off_invalid")
    for ref in event["digest"]["alert_refs"] or []:
        _require(set(ref) == {"code", "member_no"} and MASKED_RE.fullmatch(ref["member_no"]) is not None, "event_ref_invalid")
    # No text anywhere (key or value) may hold a phone-length digit run or an @: dates and times
    # have at most 4 digits in a row, codes and member types have none that long.
    for text in _strings(event):
        _require(re.search(r"[0-9]{6,}", text) is None and "@" not in text, "event_personal_data_like")


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for key, child in value.items() for s in [str(key), *_strings(child)]]
    if isinstance(value, list):
        return [s for child in value for s in _strings(child)]
    return [value] if isinstance(value, str) else []


# --------------------------------------------------------------------------------------------
# History, record and send
# --------------------------------------------------------------------------------------------

def record_path(out_dir: Path, day: str) -> Path:
    return out_dir / f"{RECORD_PREFIX}{day}.json"


def read_history(out_dir: Path, today: str) -> tuple[int | None, bool]:
    """(links in yesterday's record, whether any earlier record saw an AC2 create)."""

    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    links_yesterday, seen_create = None, False
    for path in sorted(out_dir.glob(RECORD_PREFIX + "*.json")):
        day = path.stem[len(RECORD_PREFIX):]
        if day >= today:
            continue
        try:
            digest = json.loads(path.read_text(encoding="utf-8"))["event"]["digest"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(digest, dict) or not digest.get("available"):
            continue
        if (_count(digest.get("ac2_creates")) or 0) > 0:
            seen_create = True
        if day == yesterday:
            links_yesterday = _count(digest.get("links"))
    return links_yesterday, seen_create


def write_record(out_dir: Path, event: Mapping[str, Any], send_status: str, http_status: int | None, send_error: str | None,
                 creates: Mapping[str, Any] | None = None) -> None:
    """Writes daily_summary_<date>.json inside the output folder only (temp file, then replace).
    #155 BF2b: with creates expected or a backfill window in effect, the split the decision used
    (counts, dates and caps only) is kept as ``creates``; otherwise the record is as before."""

    _require(send_status in SEND_STATUSES, "record_status_invalid")
    target = record_path(out_dir, event["summary_date"])
    _require(target.parent == out_dir, "record_path_invalid")
    temp = target.with_name(target.name + ".tmp")
    record = {"schema": RECORD_SCHEMA, "email_status": send_status, "http_status": http_status, "send_error": send_error,
              "event": event, **({"creates": dict(creates)} if creates is not None else {})}
    try:
        temp.unlink(missing_ok=True)
        with open(temp, "x", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=False)
        os.replace(temp, target)
    except OSError:
        raise SummaryError("record_write_failed") from None


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401 - urllib hook
        return None


def post_event(url: str, headers: Mapping[str, str], body: bytes, timeout: int) -> int:
    """POST once: no proxy, no redirect, no retry. Returns the HTTP status; raises OSError when
    there is no answer. The response body is never read."""

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirect())
    request = urllib.request.Request(url, data=body, method="POST", headers=dict(headers))
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code
    with response:
        return response.status


class CredentialUnavailable(Exception):
    pass


def read_credential_secret(target: str) -> str:
    """The generic credential's secret from Windows Credential Manager (CredReadW). Only the
    value is returned; it is never printed, logged or written."""

    if os.name != "nt":
        raise CredentialUnavailable()
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR), ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    pointer = ctypes.POINTER(CREDENTIALW)()
    if not cred_read(target, 1, 0, ctypes.byref(pointer)):  # 1: CRED_TYPE_GENERIC
        raise CredentialUnavailable()
    try:
        blob = ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
    finally:
        cred_free(pointer)
    try:
        secret = blob.decode("utf-16-le")
    except UnicodeDecodeError:
        raise CredentialUnavailable() from None
    if not secret:
        raise CredentialUnavailable()
    return secret


def send_event(event: Mapping[str, Any], send: Mapping[str, Any], *, cred_reader: Callable[[str], str],
               poster: Callable[[str, Mapping[str, str], bytes, int], int]) -> tuple[str, int | None, str | None]:
    """(send_status, http_status, send_error). Never raises: an email failure never fails the task."""

    if not send["enabled"]:
        return "disabled", None, None
    try:
        secret = cred_reader(send["secret_credential_target"])
    except Exception:  # noqa: BLE001 - any credential failure is the same closed outcome
        return "secret_unavailable", None, "credential_unreadable"
    body = json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json", send["auth_header_name"]: secret}
    try:
        status = poster(send["webhook_url"], headers, body, send["timeout_seconds"])
    except Exception:  # noqa: BLE001 - network, TLS or timeout: no answer from n8n
        return "send_failed", None, "no_answer"
    finally:
        secret = ""
        headers.clear()
    if isinstance(status, int) and status == 200:
        return "sent", 200, None
    return "send_failed", status if isinstance(status, int) else None, "http_status"


# --------------------------------------------------------------------------------------------
# The daily run
# --------------------------------------------------------------------------------------------

def daily(settings: Mapping[str, Any], *, now: datetime, no_send: bool = False,
          runner: Callable[[list[str], int], tuple[int, str]] | None = None,
          sql_reader: Callable[[Mapping[str, Any], str], tuple[dict[str, Any], dict[str, Any]]] | None = None,
          cred_reader: Callable[[str], str] | None = None,
          poster: Callable[[str, Mapping[str, str], bytes, int], int] | None = None) -> dict[str, Any]:
    runner = runner or run_child
    sql_reader = sql_reader or read_sql_counts
    cred_reader = cred_reader or read_credential_secret
    poster = poster or post_event
    out_dir = check_output_dir(settings)
    # The live config's state root, when it can be read: the record folder is never inside it
    # either, and a root that is not the one in the settings stops (config_unexpected). An
    # unreadable live config is not a refusal here: check-config refuses and the email says so.
    live_root = read_live_state_root(settings)
    _require(live_root is None or not _resolved_inside(out_dir, live_root), "output_dir_inside_sync_state")
    state_root_ok = None if live_root is None else live_root == Path(settings["sync_state_root"]).resolve()
    today = now.astimezone(SGT).strftime("%Y-%m-%d")
    links_yesterday, seen_create = read_history(out_dir, today)

    task = task_section(read_task_state(settings, runner))
    config = config_section(run_sync("check-config", settings, runner), state_root_ok)
    raw_digest = run_sync("digest", settings, runner)
    digest = digest_section(raw_digest, links_yesterday)
    window = settings["backfill_members_window"] if window_in_effect(settings["backfill_members_window"], today) else None
    creates = creates_section(raw_digest, expected=settings["shopify_creates_expected"], window=window)
    if config["available"] and config["autocount_book"] is not None:
        members, new_members = sql_reader(settings["sql"], config["autocount_book"])
    else:
        members, new_members = members_section(None, "config_unavailable"), new_members_section(None, None, "config_unavailable")
    first_create = bool(digest["available"] and digest["ac2_creates"] > 0 and not seen_create and not settings["first_create_already_seen"])
    event = build_event(now=now, task=task, config=config, digest=digest, members=members, new_members=new_members,
                        first_create=first_create, expected_member_type=settings["expected_member_type"], creates=creates)
    check_event(event)

    write_record(out_dir, event, "pending", None, None, creates)
    if no_send:
        status, http_status, error = "not_sent_check_only", None, None
    else:
        status, http_status, error = send_event(event, settings["send"], cred_reader=cred_reader, poster=poster)
    write_record(out_dir, event, status, http_status, error, creates)
    return {"status": "ok", "result": event["result"], "stop_reasons": event["stop_reasons"], "auto_off": event["auto_off"],
            "email_status": status, "http_status": http_status, "record": record_path(out_dir, today).name}


def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#288 slice 3: #155 member sync daily summary (read-only)")
    parser.add_argument("command", choices=("daily", "check-settings"))
    parser.add_argument("--settings", required=True)
    parser.add_argument("--no-send", action="store_true", help="daily only: build and record the summary, do not POST it")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.settings)
        if args.command == "check-settings":
            _emit({"status": "ok", "send_enabled": settings["send"]["enabled"], "sql_enabled": settings["sql"]["enabled"]})
            return 0
        _emit(daily(settings, now=datetime.now(timezone.utc), no_send=args.no_send))
        return 0
    except SummaryError as exc:
        _emit({"status": "refused", "code": exc.code})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
