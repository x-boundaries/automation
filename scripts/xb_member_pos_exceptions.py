"""Monthly in-store member discount exception report (#268 Q7). Read-only. Built, switched off.

Finds till receipts of one month that carry the member discount rate (default 15% or 20%) for a
customer whose AutoCount member record does not qualify for it:

* guest         the member's type today is GUEST (or Default): registered, not qualified;
* expired       type MEMBER, but the membership ExpiryDate was before the receipt day;
* same_receipt  type MEMBER, the record changed on or after the receipt day and its ExpiryDate is
                the receipt day + 24 months (+/- 1 day): the discount went on the receipt that made
                the customer a member (the rate starts from the next purchase).

Counted, never listed: the member rate with no MemberNo on the receipt (no_member_on_receipt),
a MemberNo with no member row (member_not_found), a type outside MEMBER/GUEST/Default.

Facts behind the rule (AutoCount book read-only check of 24 Nov 2026, counts only):
* the member discount is keyed by hand on the receipt line as text (`PosDtl.Discount`, for
  example "15.00%"); the receipt header has no discount text and member types carry no price
  or discount rule, so any type can get the rate;
* `Pos.Cancelled` 'T' is a cancelled receipt; there is no cashier column on `Pos`;
* `Member.LastModified` moves on bulk edits as well as type changes, so a GUEST row changed after
  the sale is still listed, marked "type changed after the sale" (`changed_after_sale`).

Safety contract (docs/autocount2-automation/member_pos_exceptions_runbook.md):

* Read-only. Fixed SELECT texts with `?` parameters only (no comments, no ";"), pinned by
  SHA-256 in `check-settings`. READ COMMITTED, SET LOCK_TIMEOUT and a query timeout. No
  AutoCount sign-in, no SDK, no Shopify, no write of any kind to AutoCount.
* The stock exporter's identity check on every connection before any business query: the sa
  login is always refused; sysadmin or db_owner only with identity.allow_privileged_identity;
  the expected login, user and database (AUTOCOUNT_EXPECTED_SQL_*) must match.
* Every outlet in rule.outlets must have finished a POS sync after the month ended, or the run
  reports pos_sync_stale and no exception report (a partial month is never reported).
* Output privacy: counts plus a receipt list of DocNo, outlet, day, rate, reason and the
  MemberNo masked to its last 3 characters. No name, phone, email, full MemberNo or amount is
  read into the output, the console, the run record or the email.
* Email (off by default): one sanitized xb.member.pos_exceptions.v1 event to an n8n webhook,
  which emails it through the shared XB Ops Alert Email sub-workflow (system MEMBER-POS), and
  only when there is something to act on: exceptions, a stale sync, or a failed run. Nothing
  to report means no email. Each month's outcome is sent once (alert_state.json).

pyodbc is imported only inside SqlServerSource so the tests run without it.
"""

import argparse
import copy
import hashlib
import http.client
import json
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Reuse, unchanged: settings helpers, identity check, SQL error classes, credential and alert
# secret readers, the bounded alert POST and atomic file writes.
import autocount_stock_balance_hourly as stock  # noqa: E402
from autocount_stock_extract import sanitize_error  # noqa: E402

ConfigError = stock.ConfigError
TimezoneUnavailableError = stock.TimezoneUnavailableError
RunFailure = stock.RunFailure
SourceLockTimeout = stock.SourceLockTimeout
SourceReadError = stock.SourceReadError
CredentialUnavailableError = stock.CredentialUnavailableError

JOB = "member_pos_exceptions"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_WARN = 3

FAILURE_CONFIG = stock.FAILURE_CONFIG
FAILURE_TIMEZONE = stock.FAILURE_TIMEZONE
FAILURE_IDENTITY = stock.FAILURE_IDENTITY
FAILURE_CREDENTIAL = stock.FAILURE_CREDENTIAL
FAILURE_LOCK_TIMEOUT = stock.FAILURE_LOCK_TIMEOUT
FAILURE_SOURCE = stock.FAILURE_SOURCE
FAILURE_WRITE = stock.FAILURE_WRITE
FAILURE_INTERNAL = stock.FAILURE_INTERNAL
FAILURE_CLASSES = (
    FAILURE_CONFIG, FAILURE_TIMEZONE, FAILURE_IDENTITY, FAILURE_CREDENTIAL, FAILURE_LOCK_TIMEOUT,
    FAILURE_SOURCE, FAILURE_WRITE, FAILURE_INTERNAL,
)

# Run outcomes. report = the month was read and judged (with or without exceptions).
STATUS_REPORT = "report"
STATUS_SYNC_STALE = "pos_sync_stale"
STATUS_FAILED = "failed"
STATUS_NOT_DUE = "not_due"
STATUS_ALREADY_DONE = "already_reported"

BUCKETS = ("guest", "expired", "same_receipt")
COUNT_KEYS = (
    "receipts_read", "cancelled", "not_positive", "member_rate", "guest", "expired", "same_receipt",
    "member_valid", "no_member_on_receipt", "member_not_found", "type_other", "changed_after_sale",
    "member_inactive",
)
OUTLET_KEYS = ("outlet",) + BUCKETS + ("no_member_on_receipt",)
ROW_KEYS = ("doc_no", "outlet", "day", "rate", "reason", "member", "changed_after_sale")

GUEST_TYPES = ("GUEST", "Default")
MEMBER_TYPE = "MEMBER"
NO_OUTLET = "UNKNOWN"
OUTLET_RE = re.compile(r"\A[A-Za-z0-9_-]{1,12}\Z")
DOC_NO_RE = re.compile(r"\A[A-Za-z0-9/_.-]{1,30}\Z")
RATE_TEXT_RE = re.compile(r"\A\s*([0-9]{1,2}(?:\.[0-9]{1,2})?)\s*%\s*\Z")
MONTH_RE = re.compile(r"\A(\d{4})-(\d{2})\Z")
TIME_RE = re.compile(r"\A([01]\d|2[0-3]):([0-5]\d)\Z")

RECORD_SCHEMA = "xb.member.pos_exceptions_record.v1"
ALERT_STATE_NAME = "alert_state.json"
ALERT_STATE_SCHEMA = "xb.member.pos_exceptions_alert_state.v1"
ALERT_SCHEMA = "xb.member.pos_exceptions.v1"
ALERT_EVENT = "member_pos_exceptions"
ALERT_USER_AGENT = "xb-member-pos-exceptions/1"
ALERT_PAYLOAD_KEYS = (
    "schema", "event", "run_id", "month", "status", "failure_class", "counts", "outlets", "rows",
    "rows_not_listed", "stale_outlets",
)
ALERT_DISABLED = stock.ALERT_DISABLED
ALERT_NONE = stock.ALERT_NONE
ALERT_SUPPRESSED = stock.ALERT_SUPPRESSED
ALERT_SENT = stock.ALERT_SENT
ALERT_SEND_FAILED = stock.ALERT_SEND_FAILED
ALERT_SECRET_UNAVAILABLE = stock.ALERT_SECRET_UNAVAILABLE
ALERT_ERROR = stock.ALERT_ERROR
ALERT_NOT_SENT = "not_sent_no_send"
COUNT_MAX = 1000000

# ---------------------------------------------------------------------------------------------
# SQL. Fixed strings with ? parameters. The one allowlisted terminal column is the only
# identifier ever inserted. Only these columns are read: no name, phone, email, address or amount
# beyond the sign of FinalTotal.
# ---------------------------------------------------------------------------------------------

IDENTITY_QUERY = stock.IDENTITY_QUERY
SESSION_SETUP = ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", "SET LOCK_TIMEOUT %d")
TERMINAL_COLUMNS = ("TerminalID", "LastModifiedTerminalID")


def receipts_query(terminal_column):
    """Every receipt of the window with each discounted line (one row per discounted line, or one
    row with Discount NULL). FinalTotal is reduced to its sign inside SQL."""
    if terminal_column not in TERMINAL_COLUMNS:
        raise ConfigError("source.terminal_column has an unsupported value")
    return (
        "SELECT p.DocKey, p.DocNo, p.DocDate, p.Cancelled, "
        "CASE WHEN p.FinalTotal > 0 THEN 1 ELSE 0 END AS IsPositive, "
        "p.MemberNo, t.Outlet, m.MemberNo AS MemberFound, m.MemberType, m.ExpiryDate, m.LastModified, "
        "m.IsActive, d.Discount "
        "FROM dbo.Pos p "
        "LEFT JOIN dbo.PosTerminal t ON t.TerminalID = p.[%s] "
        "LEFT JOIN dbo.Member m ON m.MemberNo = p.MemberNo "
        "LEFT JOIN dbo.PosDtl d ON d.DocKey = p.DocKey AND d.Discount IS NOT NULL AND LTRIM(RTRIM(d.Discount)) <> N'' "
        "WHERE p.DocDate >= ? AND p.DocDate < ?" % terminal_column
    )


SYNC_QUERY = "SELECT Location, MAX(LastFinishSyncTime) AS LastSyncTime FROM dbo.SyncRegistration GROUP BY Location"

FORBIDDEN_SQL_RE = re.compile(
    r"\b(WRITETEXT|UPDATETEXT|RECONFIGURE|CHECKPOINT|SETUSER|BEGIN\s+TRAN(SACTION)?|COMMIT|ROLLBACK|"
    r"XLOCK|TABLOCKX?|UPDLOCK|HOLDLOCK|NOLOCK|READPAST|READUNCOMMITTED)\b|"
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|DENY|EXEC|EXECUTE|INTO|BACKUP|"
    r"RESTORE|DBCC|SHUTDOWN|KILL|OPENROWSET|OPENQUERY|OPENDATASOURCE|WAITFOR|DECLARE|SET|USE)\b|;|--|/\*",
    re.IGNORECASE,
)


def all_query_texts(terminal_column="TerminalID"):
    return {"identity": IDENTITY_QUERY, "receipts": receipts_query(terminal_column), "sync": SYNC_QUERY}


def queries_sha256(terminal_column="TerminalID"):
    return {name: stock.query_sha256(text) for name, text in sorted(all_query_texts(terminal_column).items())}


def assert_read_only(text):
    if not text.lstrip().upper().startswith("SELECT") or FORBIDDEN_SQL_RE.search(text):
        raise ConfigError("query text is not a single read-only SELECT")


for _column in TERMINAL_COLUMNS:
    for _text in all_query_texts(_column).values():
        assert_read_only(_text)


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "timezone": "Asia/Singapore",
    # A daily task; the script itself reports the previous month once, on or after run_day.
    "schedule": {"run_day": 2, "run_time": "09:15", "logon_type": "S4U"},
    "source": {
        "server": None,
        "odbc_driver": "ODBC Driver 17 for SQL Server",
        "expected_database": None,
        "allowed_databases": None,
        "encrypt": False,
        "trust_server_certificate": False,
        "terminal_column": "TerminalID",
    },
    "identity": {
        "auth_mode": "windows_integrated",
        "sql_login": None,
        "credential_target": None,
        "allow_privileged_identity": False,
    },
    "read": {"lock_timeout_ms": 5000, "query_timeout_seconds": 60, "max_attempts": 3, "retry_backoff_seconds": 2},
    "output": {"record_dir": None},
    "rule": {"member_rates": [15, 20], "outlets": [], "max_listed": 12, "expiry_months": 24},
    "alert": copy.deepcopy(stock.DEFAULT_SETTINGS["alert"]),
}
CLOSED_SECTIONS = ("schedule", "source", "identity", "read", "output", "rule", "alert")

_require_str = stock._require_str
_require_enum = stock._require_enum
_require_bool = stock._require_bool
_require_int = stock._require_int
_require_number = stock._require_number
_safe_key = stock._safe_key


def validate_settings(raw):
    """Return the complete settings (defaults applied) or raise ConfigError naming the setting."""
    if not isinstance(raw, dict):
        raise ConfigError("settings must be a JSON object")
    unknown = sorted(set(raw) - set(DEFAULT_SETTINGS))
    if unknown:
        raise ConfigError("unknown setting(s): " + ", ".join(_safe_key(k) for k in unknown))
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    for key, value in raw.items():
        if key in CLOSED_SECTIONS:
            if not isinstance(value, dict):
                raise ConfigError("%s must be an object" % key)
            extra = sorted(set(value) - set(DEFAULT_SETTINGS[key]))
            if extra:
                raise ConfigError("unknown setting(s): " + ", ".join("%s.%s" % (key, _safe_key(k)) for k in extra))
            settings[key].update(value)
        else:
            settings[key] = value

    try:
        stock.load_zone(_require_str(settings, "timezone"))
    except RuntimeError as exc:
        if not stock.timezone_database_available():
            raise TimezoneUnavailableError("time zone data is not installed (install tzdata)") from exc
        raise ConfigError("timezone is not available") from exc

    schedule = settings["schedule"]
    _require_int(schedule["run_day"], "schedule.run_day", 1, 28)
    if not isinstance(schedule["run_time"], str) or not TIME_RE.match(schedule["run_time"]):
        raise ConfigError("schedule.run_time must be HH:MM")
    _require_enum(schedule["logon_type"], stock.LOGON_TYPES, "schedule.logon_type")

    source = settings["source"]
    _require_str(source, "server", "source.")
    expected_db = source["expected_database"]
    if not isinstance(expected_db, str) or not expected_db.strip():
        raise ConfigError("source.expected_database is required")
    if source["allowed_databases"] is None:
        source["allowed_databases"] = [expected_db]
    allowed = source["allowed_databases"]
    if not isinstance(allowed, list) or not allowed or not all(isinstance(d, str) and d for d in allowed):
        raise ConfigError("source.allowed_databases must be a non-empty list of names")
    if expected_db not in allowed:
        raise ConfigError("source.expected_database must be listed in source.allowed_databases")
    _require_str(source, "odbc_driver", "source.")
    _require_bool(source["encrypt"], "source.encrypt")
    _require_bool(source["trust_server_certificate"], "source.trust_server_certificate")
    _require_enum(source["terminal_column"], TERMINAL_COLUMNS, "source.terminal_column")

    identity = settings["identity"]
    _require_enum(identity["auth_mode"], stock.AUTH_MODES, "identity.auth_mode")
    _require_bool(identity["allow_privileged_identity"], "identity.allow_privileged_identity")
    if identity["auth_mode"] == "sql_login":
        _require_str(identity, "sql_login", "identity.")
        _require_str(identity, "credential_target", "identity.")
        if identity["sql_login"].strip().lower() == "sa":
            raise ConfigError("identity.sql_login: the sa login is never allowed")
    else:
        for key in ("sql_login", "credential_target"):
            if identity[key] not in (None, ""):
                raise ConfigError("identity.%s is only used with auth_mode sql_login" % key)
    required_logon = stock.SUPPORTED_LOGON_PAIRS[identity["auth_mode"]]
    if schedule["logon_type"] != required_logon:
        raise ConfigError(
            "identity.auth_mode %s is only supported with schedule.logon_type %s" % (identity["auth_mode"], required_logon)
        )

    read = settings["read"]
    _require_int(read["lock_timeout_ms"], "read.lock_timeout_ms", 1, 600000)
    _require_int(read["query_timeout_seconds"], "read.query_timeout_seconds", 1, 3600)
    _require_int(read["max_attempts"], "read.max_attempts", 1, 10)
    _require_number(read["retry_backoff_seconds"], "read.retry_backoff_seconds", 0, 300)

    _require_str(settings["output"], "record_dir", "output.")

    rule = settings["rule"]
    rates = rule["member_rates"]
    if (
        not isinstance(rates, list) or not 1 <= len(rates) <= 5
        or any(isinstance(r, bool) or not isinstance(r, (int, float)) or not 0 < r < 100 for r in rates)
    ):
        raise ConfigError("rule.member_rates must be 1 to 5 percentages between 0 and 100")
    outlets = rule["outlets"]
    if (
        not isinstance(outlets, list) or not 1 <= len(outlets) <= 10
        or not all(isinstance(o, str) and OUTLET_RE.match(o) for o in outlets)
        or len(set(outlets)) != len(outlets)
    ):
        raise ConfigError("rule.outlets must list 1 to 10 outlet codes (letters, digits, _ and -)")
    _require_int(rule["max_listed"], "rule.max_listed", 1, 12)
    _require_int(rule["expiry_months"], "rule.expiry_months", 1, 120)

    # The stock export's alert check, unchanged: https URL without credentials, query or fragment;
    # a plain header name; exactly one secret source when enabled.
    stock._validate_alert_settings(settings["alert"], schedule["logon_type"])
    return settings


def settings_sha256(settings):
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Months and values
# ---------------------------------------------------------------------------------------------


def parse_month(text):
    match = MONTH_RE.match(text or "")
    if not match or not 1 <= int(match.group(2)) <= 12 or not 2020 <= int(match.group(1)) <= 2099:
        raise ConfigError("--month must be YYYY-MM")
    return date(int(match.group(1)), int(match.group(2)), 1)


def next_month(first):
    return date(first.year + (first.month // 12), first.month % 12 + 1, 1)


def previous_month(day):
    first = day.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


def add_months(day, months):
    """day + months, clamped to the last day of the target month (29 Feb + 12 months -> 28 Feb)."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    last = (date(year + (month // 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def as_day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def clean(value):
    return "" if value is None else str(value).strip()


def mask_member(member_no):
    """The last 3 letters or digits of a MemberNo, as ***123 (no '+', space or '-')."""
    tail = re.sub(r"[^A-Za-z0-9]", "", clean(member_no))[-3:]
    return "***" + (tail or "?")


def rate_of(text, member_rates):
    """'15.00%' -> Decimal('15') when it is one of the member rates, else None."""
    match = RATE_TEXT_RE.match(clean(text))
    if not match:
        return None
    try:
        value = Decimal(match.group(1))
    except InvalidOperation:
        return None
    return value if value in member_rates else None


def rate_label(value):
    return "%s%%" % (value.quantize(Decimal(1)) if value == value.to_integral_value() else value.normalize())


# ---------------------------------------------------------------------------------------------
# The rule (pure)
# ---------------------------------------------------------------------------------------------


def group_receipts(rows):
    """Rows of receipts_query -> one dict per receipt with all its discount texts."""
    receipts = {}
    for row in rows:
        key = row["DocKey"]
        receipt = receipts.get(key)
        if receipt is None:
            receipt = dict(row)
            receipt["discounts"] = []
            receipts[key] = receipt
        if row.get("Discount") is not None:
            receipt["discounts"].append(row["Discount"])
    return list(receipts.values())


def classify(rows, settings):
    """Judge every receipt of the window. Returns (counts, outlets, listed rows). Pure.

    Exceptions (listed): guest, expired, same_receipt. Everything else is a count only.
    """
    rule = settings["rule"]
    member_rates = {Decimal(str(r)) for r in rule["member_rates"]}
    counts = dict.fromkeys(COUNT_KEYS, 0)
    per_outlet = {}
    listed = []
    for receipt in sorted(group_receipts(rows), key=lambda r: (as_day(r.get("DocDate")) or date.min, clean(r.get("DocNo")))):
        counts["receipts_read"] += 1
        if clean(receipt.get("Cancelled")) != "F":
            counts["cancelled"] += 1  # 'T' = cancelled; any other flag is treated the same (excluded)
            continue
        if receipt.get("IsPositive") != 1:
            counts["not_positive"] += 1  # returns and zero receipts
            continue
        rates = [rate for rate in (rate_of(text, member_rates) for text in receipt["discounts"]) if rate is not None]
        if not rates:
            continue
        counts["member_rate"] += 1
        outlet = clean(receipt.get("Outlet"))
        outlet = outlet if OUTLET_RE.match(outlet) else NO_OUTLET
        tally = per_outlet.setdefault(outlet, dict.fromkeys(OUTLET_KEYS[1:], 0))
        day = as_day(receipt.get("DocDate"))
        member_no = clean(receipt.get("MemberNo"))
        if not member_no:
            counts["no_member_on_receipt"] += 1
            tally["no_member_on_receipt"] += 1
            continue
        if receipt.get("MemberFound") is None:
            counts["member_not_found"] += 1
            continue
        if clean(receipt.get("IsActive")) not in ("T", "1", "True"):
            counts["member_inactive"] += 1
        member_type = clean(receipt.get("MemberType"))
        changed = as_day(receipt.get("LastModified"))
        changed_after = changed is None or day is None or changed >= day
        expiry = as_day(receipt.get("ExpiryDate"))
        if member_type in GUEST_TYPES:
            reason = "guest"
        elif member_type == MEMBER_TYPE:
            if expiry is not None and day is not None and expiry < day:
                reason = "expired"
            elif (
                expiry is not None and day is not None and changed_after
                and abs((expiry - add_months(day, rule["expiry_months"])).days) <= 1
            ):
                reason = "same_receipt"
            else:
                counts["member_valid"] += 1
                continue
        else:
            counts["type_other"] += 1
            continue
        counts[reason] += 1
        tally[reason] += 1
        if reason == "guest" and changed_after:
            counts["changed_after_sale"] += 1
        doc_no = clean(receipt.get("DocNo"))
        listed.append({
            "doc_no": doc_no if DOC_NO_RE.match(doc_no) else "UNREADABLE",
            "outlet": outlet,
            "day": day.isoformat() if day else "0000-00-00",
            "rate": rate_label(max(rates)),
            "reason": reason,
            "member": mask_member(member_no),
            "changed_after_sale": reason == "guest" and changed_after,
        })
    outlets = [dict({"outlet": code}, **values) for code, values in sorted(per_outlet.items())]
    return counts, outlets, listed


def exceptions_total(counts):
    return sum(counts[b] for b in BUCKETS)


def stale_outlets(sync_rows, settings, month_first, tz):
    """Outlets in rule.outlets whose last finished POS sync is not after the month ended."""
    month_end = datetime.combine(next_month(month_first), datetime.min.time()).replace(tzinfo=tz)
    latest = {}
    for row in sync_rows:
        code = clean(row.get("Location"))
        value = row.get("LastSyncTime")
        if not isinstance(value, datetime):
            continue
        value = value if value.tzinfo else value.replace(tzinfo=tz)
        if code not in latest or value > latest[code]:
            latest[code] = value
    return [code for code in settings["rule"]["outlets"] if code not in latest or latest[code] <= month_end]


# ---------------------------------------------------------------------------------------------
# Source (real SQL Server). Not exercised by the offline tests beyond its query texts.
# ---------------------------------------------------------------------------------------------


def build_connection_string(settings, password=None):
    """ODBC connection string. Never logged, printed or written to any file."""
    source = settings["source"]
    parts = [
        "DRIVER={%s}" % source["odbc_driver"],
        "SERVER=%s" % source["server"],
        "DATABASE=%s" % source["expected_database"],
        "APP=xb_member_pos_exceptions",
        "ApplicationIntent=ReadOnly",
        "Encrypt=%s" % ("yes" if source["encrypt"] else "no"),
        "TrustServerCertificate=%s" % ("yes" if source["trust_server_certificate"] else "no"),
    ]
    if settings["identity"]["auth_mode"] == "windows_integrated":
        parts.append("Trusted_Connection=yes")
    else:
        if password is None:
            raise CredentialUnavailableError("No password for the SQL login")
        parts.append("UID=%s" % stock._odbc_value(settings["identity"]["sql_login"]))
        parts.append("PWD=%s" % stock._odbc_value(password))
    return ";".join(parts) + ";"


class SqlServerSource:
    """Opens one pyodbc connection per attempt."""

    def __init__(self, settings, credential_reader=stock.read_credential_password):
        self.settings = settings
        self.credential_reader = credential_reader

    def open_session(self):
        password = None
        if self.settings["identity"]["auth_mode"] == "sql_login":
            password = self.credential_reader(self.settings["identity"]["credential_target"])
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise SourceReadError("pyodbc is not installed") from exc
        try:
            connection = pyodbc.connect(build_connection_string(self.settings, password), autocommit=True, timeout=15)
        except pyodbc.Error as exc:
            raise SourceReadError("Could not connect to the configured SQL Server") from exc
        finally:
            password = None
        return SqlServerSession(connection, self.settings, pyodbc.Error)


class SqlServerSession:
    def __init__(self, connection, settings, error_type):
        self.connection = connection
        self.error_type = error_type
        self.terminal_column = settings["source"]["terminal_column"]
        read = settings["read"]
        connection.timeout = int(read["query_timeout_seconds"])
        self._run(SESSION_SETUP[0])
        self._run(SESSION_SETUP[1] % int(read["lock_timeout_ms"]))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        try:
            self.connection.close()
        except Exception:  # noqa: BLE001 - closing a broken connection must not mask the result.
            pass

    def read_identity(self):
        row = self._rows(IDENTITY_QUERY)[0]
        return {"login": row[0], "user": row[1], "database": row[2], "is_sysadmin": row[3], "is_db_owner": row[4]}

    def read_receipts(self, start, end):
        return self._dicts(receipts_query(self.terminal_column), start, end)

    def read_sync(self):
        return self._dicts(SYNC_QUERY)

    def _run(self, sql, *params):
        try:
            cursor = self.connection.cursor()
            cursor.execute(sql, *params)
            return cursor
        except self.error_type as exc:
            raise stock._classify_sql_error(exc) from exc

    def _rows(self, sql, *params):
        cursor = self._run(sql, *params)
        try:
            return cursor.fetchall()
        except self.error_type as exc:
            raise stock._classify_sql_error(exc) from exc

    def _dicts(self, sql, *params):
        cursor = self._run(sql, *params)
        try:
            columns = [c[0] for c in cursor.description or []]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except self.error_type as exc:
            raise stock._classify_sql_error(exc) from exc


def read_month(source, settings, month_first, environ=None, sleep=time.sleep):
    """Identity check, then the sync times and the month's receipts on one connection."""
    read = settings["read"]
    start = datetime.combine(month_first, datetime.min.time())
    end = datetime.combine(next_month(month_first), datetime.min.time())
    for attempt in range(1, read["max_attempts"] + 1):
        if attempt > 1:
            sleep(read["retry_backoff_seconds"])
        try:
            session = source.open_session()
        except CredentialUnavailableError as exc:
            raise RunFailure(FAILURE_CREDENTIAL, "The SQL login password could not be read") from exc
        except SourceReadError as exc:
            raise RunFailure(FAILURE_SOURCE, sanitize_error(str(exc))) from exc
        try:
            with session:
                identity = stock.check_identity(session.read_identity(), settings, environ)
                sync_rows = session.read_sync()
                rows = session.read_receipts(start, end)
        except SourceLockTimeout:
            continue
        except SourceReadError as exc:
            raise RunFailure(FAILURE_SOURCE, sanitize_error(str(exc))) from exc
        return {"identity": identity, "sync_rows": sync_rows, "rows": rows, "attempts": attempt}
    raise RunFailure(FAILURE_LOCK_TIMEOUT, "Lock timeout on every attempt")


# ---------------------------------------------------------------------------------------------
# Run record and alert
# ---------------------------------------------------------------------------------------------


def record_path(settings, month_first):
    return Path(settings["output"]["record_dir"]) / ("pos_exceptions_%s.json" % month_first.strftime("%Y-%m"))


def load_alert_state(record_dir):
    try:
        raw = json.loads(Path(record_dir, ALERT_STATE_NAME).read_text(encoding="utf-8"))
        if raw.get("schema") != ALERT_STATE_SCHEMA or not isinstance(raw.get("sent"), dict):
            return {}
        return {str(k): v for k, v in raw["sent"].items() if isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def alert_key(result):
    """One key per month and outcome: each is emailed once."""
    if result["status"] == STATUS_FAILED:
        return "%s:run_failed:%s" % (result["month"], result["failure_class"])
    return "%s:%s" % (result["month"], result["status"])


def needs_alert(result):
    if result["status"] == STATUS_REPORT:
        return exceptions_total(result["counts"]) > 0
    return result["status"] in (STATUS_SYNC_STALE, STATUS_FAILED)


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= COUNT_MAX else 0


def build_alert_payload(result, max_listed):
    """The sanitized event: closed keys, closed codes, counts, masked refs. Never a name, phone,
    email, full MemberNo, amount, path, host or message, whatever the result holds."""
    run_id = result.get("run_id")
    status = result["status"]
    report = status == STATUS_REPORT
    rows = [
        {key: row[key] for key in ROW_KEYS}
        for row in (result.get("rows") or [])[:max_listed]
    ] if report else []
    total_rows = len(result.get("rows") or []) if report else 0
    return {
        "schema": ALERT_SCHEMA,
        "event": ALERT_EVENT,
        "run_id": run_id if isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id) else "unknown",
        "month": result["month"],
        "status": status,
        "failure_class": (result.get("failure_class") if result.get("failure_class") in FAILURE_CLASSES else FAILURE_INTERNAL)
        if status == STATUS_FAILED else None,
        "counts": {key: _count((result.get("counts") or {}).get(key)) for key in COUNT_KEYS} if report else None,
        "outlets": [
            {key: (o[key] if key == "outlet" else _count(o[key])) for key in OUTLET_KEYS}
            for o in (result.get("outlets") or [])[:10]
        ] if report else [],
        "rows": rows,
        "rows_not_listed": total_rows - len(rows),
        "stale_outlets": list(result.get("stale_outlets") or [])[:10] if status == STATUS_SYNC_STALE else [],
    }


def send_alert(settings, result, environ=None, transport=None, credential_reader=None):
    """At most one bounded POST. Returns (alert_status, http_status). Never raises, never returns
    the URL, headers, secret or body. A failed send leaves the state, so the next day retries."""
    alert = settings["alert"]
    if not alert["webhook_enabled"]:
        return ALERT_DISABLED, None
    if not needs_alert(result):
        return ALERT_NONE, None
    record_dir = settings["output"]["record_dir"]
    http_status = None
    try:
        key = alert_key(result)
        sent = load_alert_state(record_dir)
        if key in sent:
            return ALERT_SUPPRESSED, None
        secret = stock.read_alert_secret(alert, environ, credential_reader)
        if secret is None:
            return ALERT_SECRET_UNAVAILABLE, None
        payload = build_alert_payload(result, settings["rule"]["max_listed"])
        if tuple(payload) != ALERT_PAYLOAD_KEYS:
            return ALERT_ERROR, None
        body = json.dumps(payload, ensure_ascii=True).encode("ascii")
        headers = {"Content-Type": "application/json", "User-Agent": ALERT_USER_AGENT, alert["auth_header_name"]: secret}
        try:
            code = (transport or stock.post_alert)(alert["webhook_url"], headers, body, alert["timeout_seconds"])
        except (OSError, ValueError, http.client.HTTPException):
            code = None
        http_status = stock._http_status(code)
        if http_status is None or not 200 <= http_status < 300:
            return ALERT_SEND_FAILED, http_status
        sent[key] = result["finished_at"]
        try:
            stock.replace_atomic(
                Path(record_dir) / ALERT_STATE_NAME,
                stock.json_bytes({"schema": ALERT_STATE_SCHEMA, "sent": dict(sorted(sent.items()))}),
                result["run_id"][:8],
            )
        except OSError:
            pass  # The email went out; without the state the next day may send it once more.
        return ALERT_SENT, http_status
    except Exception:  # noqa: BLE001 - an alert bug must never change the run's result.
        return ALERT_ERROR, http_status


# ---------------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------------


def run_month(settings, month_first, send=True, source=None, environ=None, clock=None, sleep=time.sleep,
              transport=None, credential_reader=None):
    """Judge one month. Writes the private run record; emails when needed. Returns the result."""
    tz = stock.load_zone(settings["timezone"])
    started = stock._now(tz, clock)
    run_id = uuid.uuid4().hex
    result = {
        "job": JOB, "record_schema": RECORD_SCHEMA, "run_id": run_id, "month": month_first.strftime("%Y-%m"),
        "status": None, "failure_class": None, "message": None, "started_at": started.isoformat(),
        "finished_at": None, "counts": None, "outlets": [], "rows": [], "stale_outlets": [],
        "identity": None, "queries_sha256": queries_sha256(settings["source"]["terminal_column"]),
        "settings_sha256": settings_sha256(settings), "alert_status": None, "alert_http_status": None,
    }
    if next_month(month_first) > started.date():
        raise ConfigError("the month has not ended yet")
    try:
        source = source or SqlServerSource(settings, credential_reader or stock.read_credential_password)
        snapshot = read_month(source, settings, month_first, environ, sleep)
        result["identity"] = snapshot["identity"]
        stale = stale_outlets(snapshot["sync_rows"], settings, month_first, tz)
        if stale:
            result.update(status=STATUS_SYNC_STALE, stale_outlets=stale,
                          message="POS sync not finished after the month ended for %d outlet(s)" % len(stale))
        else:
            counts, outlets, rows = classify(snapshot["rows"], settings)
            result.update(status=STATUS_REPORT, counts=counts, outlets=outlets, rows=rows)
    except RunFailure as exc:
        result.update(status=STATUS_FAILED, failure_class=exc.failure_class,
                      message="%s (run %s)" % (sanitize_error(str(exc)), run_id[:8]))
    except Exception:  # noqa: BLE001 - an unexpected error is recorded, never a traceback with data.
        result.update(status=STATUS_FAILED, failure_class=FAILURE_INTERNAL,
                      message="Unexpected error (run %s)" % run_id[:8])
    result["finished_at"] = stock._now(tz, clock).isoformat()
    try:
        Path(settings["output"]["record_dir"]).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # The record write below reports it.
    if send:
        result["alert_status"], result["alert_http_status"] = send_alert(settings, result, environ, transport, credential_reader)
    else:
        result["alert_status"] = ALERT_NOT_SENT
    try:
        stock.replace_atomic(record_path(settings, month_first), stock.json_bytes(result), run_id[:8])
    except OSError:
        if result["status"] != STATUS_FAILED:
            result.update(status=STATUS_FAILED, failure_class=FAILURE_WRITE,
                          message="The run record could not be written (run %s)" % run_id[:8])
    return result


def scheduled_month(settings, clock=None):
    """(month to report, why) for the daily task: the previous month, once, on or after run_day."""
    tz = stock.load_zone(settings["timezone"])
    today = stock._now(tz, clock).date()
    month = previous_month(today)
    if today.day < settings["schedule"]["run_day"]:
        return month, STATUS_NOT_DUE
    try:
        done = json.loads(record_path(settings, month).read_text(encoding="utf-8"))
        if done.get("status") == STATUS_REPORT and done.get("alert_status") in (ALERT_SENT, ALERT_NONE, ALERT_SUPPRESSED, ALERT_DISABLED):
            return month, STATUS_ALREADY_DONE
    except (OSError, ValueError, AttributeError):
        pass
    return month, None


def console_summary(result):
    """What the console and the task log show: counts and codes only (no DocNo, no member ref)."""
    keys = ("job", "run_id", "month", "status", "failure_class", "message", "counts", "outlets", "stale_outlets",
            "alert_status", "alert_http_status", "settings_sha256")
    summary = {key: result.get(key) for key in keys}
    summary["rows_listed"] = len(result.get("rows") or [])
    return summary


def exit_code(result):
    status = result["status"]
    if status == STATUS_FAILED:
        return EXIT_FAILED
    if status == STATUS_SYNC_STALE:
        return EXIT_WARN
    return EXIT_OK


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def main(argv=None, source=None, environ=None, clock=None, transport=None, credential_reader=None):
    parser = argparse.ArgumentParser(description="Monthly read-only in-store member discount exception report (#268).")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-settings", help="Validate the settings only (reads no data).")
    check.add_argument("--config", required=True)
    run = sub.add_parser("run", help="Report one month (--month YYYY-MM) or, with --scheduled, the previous month when due.")
    run.add_argument("--config", required=True)
    which = run.add_mutually_exclusive_group(required=True)
    which.add_argument("--month")
    which.add_argument("--scheduled", action="store_true")
    run.add_argument("--no-send", action="store_true", help="Read and record, never email.")
    args = parser.parse_args(argv)

    try:
        settings = validate_settings(stock.load_settings_file(args.config))
    except ConfigError as exc:
        _print({"status": STATUS_FAILED, "failure_class": exc.failure_class, "message": "Settings invalid: %s" % exc})
        return EXIT_FAILED

    if args.command == "check-settings":
        _print({
            "status": "valid",
            "settings_sha256": settings_sha256(settings),
            "auth_mode": settings["identity"]["auth_mode"],
            "scheduled_logon_type": settings["schedule"]["logon_type"],
            "run_day": settings["schedule"]["run_day"],
            "run_time": settings["schedule"]["run_time"],
            "alert_enabled": settings["alert"]["webhook_enabled"],
            "queries_sha256": queries_sha256(settings["source"]["terminal_column"]),
        })
        return EXIT_OK

    try:
        if args.scheduled:
            month, skip = scheduled_month(settings, clock)
            if skip:
                _print({"job": JOB, "month": month.strftime("%Y-%m"), "status": skip})
                return EXIT_OK
        else:
            month = parse_month(args.month)
        result = run_month(settings, month, send=not args.no_send, source=source, environ=environ, clock=clock,
                           transport=transport, credential_reader=credential_reader)
    except ConfigError as exc:
        _print({"status": STATUS_FAILED, "failure_class": exc.failure_class, "message": str(exc)})
        return EXIT_FAILED
    _print(console_summary(result))
    return exit_code(result)


def _print(payload):
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    sys.exit(main())
