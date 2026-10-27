"""Hourly AutoCount (AC2) stock balance export (#245).

Reads the stored on-hand balance (dbo.ItemBatchBalQty) read-only, once per hour, and writes an
immutable hourly CSV plus a manifest, a verified "latest" pair, and a last_run_status.json that
also records failed hours. Delivery to Google Drive is a plain copy into a local folder that
Google Drive for desktop syncs; nothing here calls a Drive API.

Safety contract (see docs/autocount2-automation/stock_balance_hourly_runbook.md):

* Read-only. Fixed SELECT statements, READ COMMITTED, SET LOCK_TIMEOUT, no NOLOCK, no snapshot
  isolation, no database setting changes. No cost column is ever selected.
* Identity is checked on every connection before any business query: the "sa" login is always
  refused; sysadmin / db_owner are refused unless allow_privileged_identity is true; the
  AUTOCOUNT_EXPECTED_SQL_* context and the database pin must match. Messages carry contract
  classes only, never observed or expected values.
* Nothing is ever overwritten: hourly files use exclusive create, a rerun makes a new folder,
  and the latest pair is replaced (temp file + os.replace) only after the new hourly CSV
  re-reads to the hash that was written.
* Every outcome writes a manifest. A failed run writes no CSV and never touches the latest pair.
* Passwords only come from Windows Credential Manager at runtime. Connection strings, logins,
  host names and row data are never logged or written to a manifest, except that a successful
  run's manifest lists at most 20 large-balance rows (ItemCode, Location, BalQty), the same
  rows its CSV already holds.

pyodbc is imported only inside SqlServerSource so the tests run without it.
"""

import argparse
import copy
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from autocount_stock_extract import (  # noqa: E402 - reuse, the daily extractor stays unchanged.
    SqlExecutionContextError,
    load_timezone,
    resolve_expected_sql_context,
    sanitize_error,
)
from csv_safety import safe_csv_row  # noqa: E402

JOB = "ac2_hourly_stock_balance"
SCHEMA_VERSION = "1"
SOURCE_SURFACE = "dbo.ItemBatchBalQty"

CSV_NAME = "stock_balance.csv"
MANIFEST_NAME = "manifest.json"
LATEST_CSV_NAME = "stock_balance_latest.csv"
LATEST_MANIFEST_NAME = "latest_manifest.json"
LAST_RUN_STATUS_NAME = "last_run_status.json"
LOCK_NAME = ".ac2_stockbal.lock"
HOURLY_DIR = "hourly"
# The scheduled task stops a run after this many minutes (installer ExecutionTimeLimit).
TASK_EXECUTION_LIMIT_MINUTES = 15
# A lock older than the execution limit plus this margin is cleared automatically, but only when
# it was written before the last boot, or the process it records is no longer running (a run
# killed by the time limit or a reboot). A post-boot lock whose process is still alive is always
# respected. The clearing run records a warning.
LOCK_CLEAR_MARGIN_MINUTES = 15
LOCK_STALE_AFTER_MINUTES = TASK_EXECUTION_LIMIT_MINUTES + LOCK_CLEAR_MARGIN_MINUTES
# A process created more than this after the lock was written reused the PID; it is not the
# lock owner. The tolerance only absorbs small clock adjustments.
LOCK_PID_START_TOLERANCE_SECONDS = 60
# A lock counts as written before the last boot only when it predates the boot time by more
# than this, so a small clock adjustment never makes a live run's lock look pre-boot.
LOCK_BOOT_TOLERANCE_SECONDS = 300

NO_BALANCE_ROW_LABEL = "(no balance row)"
ROW_SOURCE_BALANCE = "balance"
ROW_SOURCE_NO_BALANCE_ROW = "no_balance_row"

SYNC_TIME_COLUMNS = ("LastFinishSyncTime", "LastTransFinishSyncTime")
ZERO_ROW_MODES = ("keep_rows_and_add_items_without_rows", "keep_rows", "drop_zero_rows")
AUTH_MODES = ("windows_integrated", "sql_login")
LOGON_TYPES = ("S4U", "Password")
# The only supported auth mode / scheduled-task logon pairs (runbook "Scheduled identity"). An
# S4U task stores no password and cannot read Credential Manager secrets, so a SQL login
# password can only be read by a task registered with a Password logon.
SUPPORTED_LOGON_PAIRS = {"windows_integrated": "S4U", "sql_login": "Password"}
UOM_GUARD_MODES = ("fail",)
SCHEDULE_FREQUENCIES = ("hourly",)
# Large-balance warning (owner decision 27 Oct 2026): a quantity keyed into the wrong field can
# give a balance in the billions. Rows with |BalQty| above the threshold are listed in the
# manifest and the run still succeeds. No off switch: the check never blocks an export, and a
# threshold above a billion units could only hide such errors.
LARGE_BALANCE_THRESHOLD_MAX = 1000000000
LARGE_BALANCE_LIST_CAP = 20
LARGE_BALANCE_WARNING_PREFIX = "Large balance:"
AVAILABLE_COLUMNS = (
    "as_of_local",
    "ItemCode",
    "ItemDescription",
    "ItemType",
    "Location",
    "LocationLabel",
    "UOM",
    "BalQty",
    "IsActive",
    "StockControl",
    "RowSource",
)
REQUIRED_COLUMNS = ("ItemCode", "Location", "UOM", "BalQty")
DEFAULT_COLUMNS = list(AVAILABLE_COLUMNS)

# Exit codes.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_STALE = 2
EXIT_MISSING = 3

# Failure classes written to manifests and last_run_status.json.
FAILURE_CONFIG = "config_invalid"
FAILURE_TIMEZONE = "timezone_unavailable"
FAILURE_ALREADY_RUNNING = "already_running"
FAILURE_STALE_LOCK = "stale_lock"
FAILURE_IDENTITY = "identity_refused"
FAILURE_CREDENTIAL = "credential_unavailable"
FAILURE_LOCK_TIMEOUT = "lock_timeout"
FAILURE_UNSTABLE = "unstable_snapshot"
FAILURE_UOM = "uom_guard"
FAILURE_SYNC = "sync_read_failed"
FAILURE_SOURCE = "source_error"
FAILURE_WRITE = "write_failed"
FAILURE_LATEST = "latest_publish_failed"
FAILURE_INTERNAL = "internal_error"

DELIVERY_NOT_CONFIGURED = "not_configured"
DELIVERY_HANDED = "handed_to_drive_sync_folder"
DELIVERY_VERIFY_FAILED = "verify_failed"
DELIVERY_COPY_FAILED = "copy_failed"
DELIVERY_SKIPPED = "skipped_failed_run"

# ---------------------------------------------------------------------------------------------
# SQL. Fixed strings: parameters only, and the one allowlisted SyncRegistration column is the
# only identifier ever inserted. No cost column, no free-text column from a transaction table.
# ---------------------------------------------------------------------------------------------

IDENTITY_QUERY = (
    "SELECT SUSER_SNAME() AS actual_login, USER_NAME() AS actual_user, DB_NAME() AS actual_database, "
    "IS_SRVROLEMEMBER(N'sysadmin') AS is_sysadmin, IS_MEMBER(N'db_owner') AS is_db_owner"
)
CHANGE_COUNT_QUERY = "SELECT Counter FROM dbo.ChangeCount WHERE TableName = ?"
CHANGE_COUNT_TABLE = "ItemBatchBalQty"
# pyodbc cannot fetch datetimeoffset natively, so the server clock is read as a local datetime2
# plus its offset in minutes (SYSDATETIMEOFFSET is evaluated once per statement).
SQL_TIME_QUERY = (
    "SELECT CONVERT(datetime2(0), SYSDATETIMEOFFSET()) AS sql_local, "
    "DATEPART(TZOFFSET, SYSDATETIMEOFFSET()) AS offset_minutes"
)
BALANCE_QUERY = """SELECT
    b.ItemCode,
    b.UOM,
    b.Location,
    SUM(b.BalQty) AS BalQty,
    COUNT_BIG(*) AS SourceRows,
    SUM(CASE WHEN b.BalQty IS NULL THEN 1 ELSE 0 END) AS NullBalQtyRows,
    SUM(CASE WHEN b.BatchNo IS NOT NULL AND b.BatchNo <> N'' THEN 1 ELSE 0 END) AS NonEmptyBatchRows,
    i.Description AS ItemDescription,
    i.ItemType,
    i.IsActive,
    i.StockControl,
    i.BaseUOM,
    l.Description AS LocationDescription
FROM dbo.ItemBatchBalQty AS b
LEFT JOIN dbo.Item AS i ON i.ItemCode = b.ItemCode
LEFT JOIN dbo.Location AS l ON l.Location = b.Location
GROUP BY b.ItemCode, b.UOM, b.Location, i.Description, i.ItemType, i.IsActive, i.StockControl,
    i.BaseUOM, l.Description"""
MULTI_UOM_QUERY = (
    "SELECT COUNT_BIG(*) AS multi_uom_items FROM "
    "(SELECT ItemCode FROM dbo.ItemUOM GROUP BY ItemCode HAVING COUNT_BIG(*) > 1) AS m"
)
ITEMS_WITHOUT_ROWS_QUERY = """SELECT
    i.ItemCode,
    i.Description AS ItemDescription,
    i.ItemType,
    i.IsActive,
    i.StockControl,
    i.BaseUOM
FROM dbo.Item AS i
WHERE i.IsActive = N'T' AND i.StockControl = N'T'
    AND NOT EXISTS (SELECT 1 FROM dbo.ItemBatchBalQty AS b WHERE b.ItemCode = i.ItemCode)"""


def sync_query(column):
    """SyncRegistration read for the allowlisted time column. Location and time only."""
    if column not in SYNC_TIME_COLUMNS:
        raise ConfigError("source.sync_registration_time_column has an unsupported value")
    return "SELECT Location, [%s] AS LastSyncTime FROM dbo.SyncRegistration" % column


def query_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------------


class ConfigError(ValueError):
    """Invalid settings. Messages name the setting, never its value."""

    failure_class = FAILURE_CONFIG


class TimezoneUnavailableError(ConfigError):
    """No time zone database at all (Windows Python without tzdata), not just a bad zone name."""

    failure_class = FAILURE_TIMEZONE


class RunFailure(RuntimeError):
    """A classified, sanitized failure that ends the run."""

    def __init__(self, failure_class, message):
        super().__init__(message)
        self.failure_class = failure_class


class SourceLockTimeout(RuntimeError):
    """SET LOCK_TIMEOUT expired while reading."""


class SourceReadError(RuntimeError):
    """Any other source read failure."""


class SyncReadError(RuntimeError):
    """The SyncRegistration read failed. Fails the hour; there is no silent fallback."""


class CredentialUnavailableError(RuntimeError):
    """The SQL login password could not be read from Windows Credential Manager."""


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "timezone": "Asia/Singapore",
    "schedule": {"frequency": "hourly", "run_minute": 0, "logon_type": "S4U"},
    "stale_after_minutes": 70,
    "source": {
        "server": None,
        "odbc_driver": "ODBC Driver 17 for SQL Server",
        "expected_database": None,
        "allowed_databases": None,
        "encrypt": False,
        "trust_server_certificate": False,
        "sync_registration_time_column": "LastFinishSyncTime",
    },
    "identity": {
        "auth_mode": "windows_integrated",
        "sql_login": None,
        "credential_target": None,
        "allow_privileged_identity": False,
    },
    "read": {
        "lock_timeout_ms": 5000,
        "query_timeout_seconds": 60,
        "max_attempts": 3,
        "retry_backoff_seconds": 2,
    },
    "rows": {
        "zero_row_mode": "keep_rows_and_add_items_without_rows",
        "drop_zero_rows_for_inactive_items": True,
        "uom_guard": "fail",
    },
    "location_labels": {},
    "output": {
        "archive_root": None,
        "delivery_root": None,
        "columns": DEFAULT_COLUMNS,
        "large_balance_threshold": 100000,
    },
}
# Sections whose keys are a closed set. location_labels is an open code -> label map.
CLOSED_SECTIONS = ("schedule", "source", "identity", "read", "rows", "output")


def load_settings_file(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError("settings file could not be read as JSON") from exc
    return raw


def validate_settings(raw):
    """Return the complete settings (defaults applied) or raise ConfigError.

    Unknown keys, bad enums, a bad SyncRegistration column and a missing expected_database all
    fail. Messages name the setting path only.
    """
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
        load_zone(_require_str(settings, "timezone"))
    except RuntimeError as exc:
        if not timezone_database_available():
            raise TimezoneUnavailableError("time zone data is not installed (install tzdata)") from exc
        raise ConfigError("timezone is not available") from exc

    schedule = settings["schedule"]
    _require_enum(schedule["frequency"], SCHEDULE_FREQUENCIES, "schedule.frequency")
    _require_int(schedule["run_minute"], "schedule.run_minute", 0, 59)
    _require_enum(schedule["logon_type"], LOGON_TYPES, "schedule.logon_type")
    _require_int(settings["stale_after_minutes"], "stale_after_minutes", 1, 24 * 60)

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
    _require_enum(source["sync_registration_time_column"], SYNC_TIME_COLUMNS, "source.sync_registration_time_column")

    identity = settings["identity"]
    _require_enum(identity["auth_mode"], AUTH_MODES, "identity.auth_mode")
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
    required_logon = SUPPORTED_LOGON_PAIRS[identity["auth_mode"]]
    if schedule["logon_type"] != required_logon:
        raise ConfigError(
            "identity.auth_mode %s is only supported with schedule.logon_type %s"
            % (identity["auth_mode"], required_logon)
        )

    read = settings["read"]
    _require_int(read["lock_timeout_ms"], "read.lock_timeout_ms", 1, 600000)
    _require_int(read["query_timeout_seconds"], "read.query_timeout_seconds", 1, 3600)
    _require_int(read["max_attempts"], "read.max_attempts", 1, 10)
    _require_number(read["retry_backoff_seconds"], "read.retry_backoff_seconds", 0, 300)

    rows = settings["rows"]
    _require_enum(rows["zero_row_mode"], ZERO_ROW_MODES, "rows.zero_row_mode")
    _require_bool(rows["drop_zero_rows_for_inactive_items"], "rows.drop_zero_rows_for_inactive_items")
    _require_enum(rows["uom_guard"], UOM_GUARD_MODES, "rows.uom_guard")

    labels = settings["location_labels"]
    if not isinstance(labels, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) and v for k, v in labels.items()
    ):
        raise ConfigError("location_labels must map location codes to non-empty labels")

    output = settings["output"]
    _require_str(output, "archive_root", "output.")
    if output["delivery_root"] is not None:
        _require_str(output, "delivery_root", "output.")
        if _same_path(output["delivery_root"], output["archive_root"]):
            raise ConfigError("output.delivery_root must differ from output.archive_root")
    columns = output["columns"]
    if not isinstance(columns, list) or not columns or not all(isinstance(c, str) for c in columns):
        raise ConfigError("output.columns must be a non-empty list")
    if len(set(columns)) != len(columns):
        raise ConfigError("output.columns has duplicates")
    if set(columns) - set(AVAILABLE_COLUMNS):
        raise ConfigError("output.columns has unsupported column(s)")
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise ConfigError("output.columns must include: " + ", ".join(missing))
    _require_int(output["large_balance_threshold"], "output.large_balance_threshold", 1, LARGE_BALANCE_THRESHOLD_MAX)
    return settings


def load_zone(name):
    """The daily extractor's load_timezone, with every load failure raised as RuntimeError.

    load_timezone converts only ZoneInfoNotFoundError. zoneinfo raises ValueError for a
    malformed or absolute key ("Asia/Singapore/", "/Asia/Singapore", "Asia//Singapore"), and the
    tzdata package can raise OSError for a folder-like key. Any of these is a bad setting, never
    a crash, so callers map RuntimeError to config_invalid or timezone_unavailable.
    """
    try:
        return load_timezone(name)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - any zone load failure is a config failure, not a crash.
        raise RuntimeError("time zone could not be loaded") from exc


def timezone_database_available():
    """False when not even the default zone loads, i.e. no zone database is installed."""
    try:
        load_zone(DEFAULT_SETTINGS["timezone"])
    except RuntimeError:
        return False
    return True


def settings_sha256(settings):
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode("utf-8")).hexdigest()


def _safe_key(key):
    text = str(key)
    return text if text.isidentifier() and len(text) <= 64 else "<invalid key>"


def _require_str(mapping, key, prefix=""):
    value = mapping[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s%s is required" % (prefix, key))
    return value


def _require_enum(value, allowed, name):
    if value not in allowed:
        raise ConfigError("%s has an unsupported value" % name)


def _require_bool(value, name):
    if not isinstance(value, bool):
        raise ConfigError("%s must be true or false" % name)


def _require_int(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError("%s must be a whole number from %s to %s" % (name, low, high))


def _require_number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ConfigError("%s must be a number from %s to %s" % (name, low, high))


def _same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ---------------------------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------------------------


def check_identity(observed, settings, environ=None):
    """Fail-closed identity checks for ONE connection. Returns flags only, never names.

    observed: dict with login, user, database, is_sysadmin, is_db_owner as read by
    IDENTITY_QUERY on the same connection that will run the business queries.
    """
    login = _clean(observed.get("login"))
    if login.lower() == "sa":
        # Not configurable: the sa login is refused even with allow_privileged_identity true.
        raise RunFailure(FAILURE_IDENTITY, "Refused: the sa login is never used")
    try:
        expected = resolve_expected_sql_context(environ)
    except SqlExecutionContextError as exc:
        raise RunFailure(FAILURE_IDENTITY, sanitize_error(str(exc))) from exc
    actual = {"login": login, "user": _clean(observed.get("user")), "database": _clean(observed.get("database"))}
    mismatched = sorted(field for field, value in expected.items() if actual[field] != value)
    if mismatched:
        raise RunFailure(FAILURE_IDENTITY, "SQL execution context mismatch: " + ", ".join(mismatched))
    source = settings["source"]
    if actual["database"] != source["expected_database"] or actual["database"] not in source["allowed_databases"]:
        raise RunFailure(FAILURE_IDENTITY, "Database pin mismatch: database is not the expected book")
    # IS_SRVROLEMEMBER / IS_MEMBER return NULL when undeterminable: treat as privileged.
    sysadmin = observed.get("is_sysadmin")
    db_owner = observed.get("is_db_owner")
    privileged = sysadmin != 0 or db_owner != 0
    if privileged and not settings["identity"]["allow_privileged_identity"]:
        raise RunFailure(
            FAILURE_IDENTITY,
            "Refused: the identity is sysadmin or db_owner and allow_privileged_identity is false",
        )
    return {
        "checked": True,
        "privileged": privileged,
        "sysadmin": sysadmin != 0,
        "db_owner": db_owner != 0,
        "expected_context_match": True,
        "database_pinned": True,
    }


def _clean(value):
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------------------------------------
# Source (real SQL Server). Not exercised by the offline tests beyond its query text.
# ---------------------------------------------------------------------------------------------


def read_credential_password(target):
    """Read a generic credential's password from Windows Credential Manager (CredReadW)."""
    if os.name != "nt":
        raise CredentialUnavailableError("Windows Credential Manager is only available on Windows")
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    pointer = ctypes.POINTER(CREDENTIALW)()
    cred_type_generic = 1
    if not cred_read(target, cred_type_generic, 0, ctypes.byref(pointer)):
        raise CredentialUnavailableError("The configured credential could not be read")
    try:
        cred = pointer.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        cred_free(pointer)
    try:
        return blob.decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise CredentialUnavailableError("The configured credential is not a text password") from exc


def build_connection_string(settings, password=None):
    """ODBC connection string. Never logged, printed or written to any file."""
    source = settings["source"]
    parts = [
        "DRIVER={%s}" % source["odbc_driver"],
        "SERVER=%s" % source["server"],
        "DATABASE=%s" % source["expected_database"],
        "APP=xb_ac2_stock_balance_hourly",
        "Encrypt=%s" % ("yes" if source["encrypt"] else "no"),
        "TrustServerCertificate=%s" % ("yes" if source["trust_server_certificate"] else "no"),
    ]
    if settings["identity"]["auth_mode"] == "windows_integrated":
        parts.append("Trusted_Connection=yes")
    else:
        if password is None:
            raise CredentialUnavailableError("No password for the SQL login")
        parts.append("UID=%s" % _odbc_value(settings["identity"]["sql_login"]))
        parts.append("PWD=%s" % _odbc_value(password))
    return ";".join(parts) + ";"


def _odbc_value(value):
    return "{" + value.replace("}", "}}") + "}"


class SqlServerSource:
    """Opens one pyodbc connection per attempt."""

    def __init__(self, settings, credential_reader=read_credential_password):
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
        self.sync_column = settings["source"]["sync_registration_time_column"]
        read = settings["read"]
        connection.timeout = int(read["query_timeout_seconds"])
        # Validated whole number; SET LOCK_TIMEOUT does not take a parameter marker.
        self._run("SET TRANSACTION ISOLATION LEVEL READ COMMITTED; SET LOCK_TIMEOUT %d;" % int(read["lock_timeout_ms"]))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        try:
            self.connection.close()
        except Exception:  # noqa: BLE001 - closing a broken connection must not mask the result.
            pass

    def read_identity(self):
        row = self._fetchall(IDENTITY_QUERY)[0]
        return {
            "login": row[0],
            "user": row[1],
            "database": row[2],
            "is_sysadmin": row[3],
            "is_db_owner": row[4],
        }

    def read_change_count(self):
        rows = self._fetchall(CHANGE_COUNT_QUERY, [CHANGE_COUNT_TABLE])
        if len(rows) != 1:
            raise SourceReadError("ChangeCount row for the balance table is missing")
        return int(rows[0][0])

    def read_sql_time(self):
        local, offset_minutes = self._fetchall(SQL_TIME_QUERY)[0][:2]
        if not isinstance(local, datetime) or not isinstance(offset_minutes, int):
            raise SourceReadError("SQL read time was not a timestamp")
        return local.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))

    def read_balances(self):
        return self._fetch_dicts(BALANCE_QUERY)

    def read_multi_uom_item_count(self):
        return int(self._fetchall(MULTI_UOM_QUERY)[0][0])

    def read_items_without_rows(self):
        return self._fetch_dicts(ITEMS_WITHOUT_ROWS_QUERY)

    def read_sync(self):
        try:
            return [{"Location": r[0], "LastSyncTime": r[1]} for r in self._fetchall(sync_query(self.sync_column))]
        except (SourceReadError, SourceLockTimeout) as exc:
            raise SyncReadError("SyncRegistration read failed") from exc

    def _run(self, sql, params=None):
        try:
            cursor = self.connection.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            return cursor
        except self.error_type as exc:
            raise _classify_sql_error(exc) from exc

    def _fetchall(self, sql, params=None):
        cursor = self._run(sql, params)
        try:
            return cursor.fetchall()
        except self.error_type as exc:
            raise _classify_sql_error(exc) from exc

    def _fetch_dicts(self, sql):
        cursor = self._run(sql)
        try:
            columns = [c[0] for c in cursor.description or []]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except self.error_type as exc:
            raise _classify_sql_error(exc) from exc


def _classify_sql_error(exc):
    text = " ".join(str(a) for a in getattr(exc, "args", ()))
    if "1222" in text or "Lock request time out" in text:
        return SourceLockTimeout("Lock timeout while reading")
    return SourceReadError("SQL read failed")


# ---------------------------------------------------------------------------------------------
# Consistent read
# ---------------------------------------------------------------------------------------------


def read_snapshot(source, settings, environ=None, sleep=time.sleep):
    """Read a consistent snapshot. Returns (snapshot, attempts, identity).

    ChangeCount is read before and after the business reads on the same connection; equal
    values mean no balance write landed in between. A fresh connection is opened per attempt.
    """
    read = settings["read"]
    max_attempts = read["max_attempts"]
    zero_mode = settings["rows"]["zero_row_mode"]
    last_failure = None
    identity = {"checked": False}
    changecounts = (None, None)
    for attempt in range(1, max_attempts + 1):
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
                identity = check_identity(session.read_identity(), settings, environ)
                before = session.read_change_count()
                sql_now = session.read_sql_time()
                balances = session.read_balances()
                multi_uom = session.read_multi_uom_item_count()
                no_row_items = (
                    session.read_items_without_rows() if zero_mode == "keep_rows_and_add_items_without_rows" else []
                )
                sync_rows = session.read_sync()
                after = session.read_change_count()
        except SyncReadError as exc:
            raise RunFailure(FAILURE_SYNC, "SyncRegistration read failed") from exc
        except SourceLockTimeout:
            last_failure = (FAILURE_LOCK_TIMEOUT, "Lock timeout on every attempt")
            continue
        except SourceReadError as exc:
            raise RunFailure(FAILURE_SOURCE, sanitize_error(str(exc))) from exc
        if before == after:
            snapshot = {
                "changecount_before": before,
                "changecount_after": after,
                "sql_now": sql_now,
                "balances": balances,
                "multi_uom_items": multi_uom,
                "no_row_items": no_row_items,
                "sync_rows": sync_rows,
            }
            return snapshot, attempt, identity
        last_failure = (FAILURE_UNSTABLE, "Balances kept changing during every attempt")
        changecounts = (before, after)
    failure = RunFailure(last_failure[0], last_failure[1])
    failure.attempts = max_attempts
    failure.identity = identity
    failure.changecounts = changecounts
    raise failure


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------


def to_decimal(value):
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise RunFailure(FAILURE_SOURCE, "A balance quantity was not numeric") from exc


def tidy_qty(value):
    """Decimal without trailing zeros or exponent; stays numeric so CSV safety never prefixes it."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return Decimal(text)


def check_uom_guard(balances, multi_uom_items):
    mismatched = sum(1 for b in balances if _clean(b.get("UOM")) != _clean(b.get("BaseUOM")) or not _clean(b.get("BaseUOM")))
    result = {
        "mode": "fail",
        "non_base_uom_rows": mismatched,
        "multi_uom_items": int(multi_uom_items),
    }
    result["result"] = "pass" if mismatched == 0 and multi_uom_items == 0 else "fail"
    return result


def location_label(code, description, labels):
    if code in labels:
        return labels[code], True
    description = _clean(description)
    return (code + " - " + description if description else code), False


def build_rows(snapshot, settings, as_of_local):
    """Turn the snapshot into CSV rows plus counts. Pure; no I/O."""
    rows_cfg = settings["rows"]
    labels = settings["location_labels"]
    zero_mode = rows_cfg["zero_row_mode"]
    counts = {
        "balance_groups_read": 0,
        "source_balance_rows": 0,
        "zero": 0,
        "negative": 0,
        "no_balance_row": 0,
        "non_empty_batch": 0,
        "null_balqty_rows": 0,
        "inactive_dropped": 0,
        "zero_dropped": 0,
    }
    per_location = {}
    unmapped = set()
    out = []
    for b in snapshot["balances"]:
        counts["balance_groups_read"] += 1
        counts["source_balance_rows"] += int(b.get("SourceRows") or 0)
        counts["non_empty_batch"] += int(b.get("NonEmptyBatchRows") or 0)
        counts["null_balqty_rows"] += int(b.get("NullBalQtyRows") or 0)
        code = _clean(b.get("Location"))
        qty = to_decimal(b.get("BalQty"))
        active = _clean(b.get("IsActive")) == "T"
        loc = per_location.setdefault(code, {"rows": 0, "non_zero": 0, "negative": 0, "sum_qty": Decimal(0)})
        if qty == 0 and not active and rows_cfg["drop_zero_rows_for_inactive_items"]:
            counts["inactive_dropped"] += 1
            continue
        if qty == 0 and zero_mode == "drop_zero_rows":
            counts["zero_dropped"] += 1
            continue
        label, mapped = location_label(code, b.get("LocationDescription"), labels)
        if not mapped:
            unmapped.add(code)
        if qty == 0:
            counts["zero"] += 1
        if qty < 0:
            counts["negative"] += 1
            loc["negative"] += 1
        if qty != 0:
            loc["non_zero"] += 1
        loc["rows"] += 1
        loc["sum_qty"] += qty
        out.append(_row(as_of_local, b, code, label, b.get("UOM"), tidy_qty(qty), ROW_SOURCE_BALANCE))
    if zero_mode == "keep_rows_and_add_items_without_rows":
        seen = {_clean(b.get("ItemCode")) for b in snapshot["balances"]}
        for item in snapshot["no_row_items"]:
            if _clean(item.get("ItemCode")) in seen:
                continue
            seen.add(_clean(item.get("ItemCode")))
            counts["no_balance_row"] += 1
            out.append(_row(as_of_local, item, "", NO_BALANCE_ROW_LABEL, item.get("BaseUOM"), Decimal(0), ROW_SOURCE_NO_BALANCE_ROW))
    out.sort(key=lambda r: (r["ItemCode"], r["Location"], r["UOM"], r["RowSource"]))
    locations = {
        code: {
            "rows": v["rows"],
            "non_zero": v["non_zero"],
            "negative": v["negative"],
            "sum_qty": str(tidy_qty(v["sum_qty"])),
        }
        for code, v in sorted(per_location.items())
    }
    return out, counts, locations, sorted(unmapped)


def find_large_balances(rows, threshold):
    """Exported rows with |BalQty| strictly above threshold, largest first, listed up to the cap. Pure."""
    large = sorted(
        (r for r in rows if abs(r["BalQty"]) > threshold),
        key=lambda r: (-abs(r["BalQty"]), r["ItemCode"], r["Location"]),
    )
    return {
        "threshold": threshold,
        "count": len(large),
        "rows": [
            {"ItemCode": r["ItemCode"], "Location": r["Location"], "BalQty": str(r["BalQty"])}
            for r in large[:LARGE_BALANCE_LIST_CAP]
        ],
        "rows_truncated": len(large) > LARGE_BALANCE_LIST_CAP,
    }


def _row(as_of_local, src, code, label, uom, qty, row_source):
    return {
        "as_of_local": as_of_local,
        "ItemCode": _clean(src.get("ItemCode")),
        "ItemDescription": _clean(src.get("ItemDescription")),
        "ItemType": _clean(src.get("ItemType")),
        "Location": code,
        "LocationLabel": label,
        "UOM": _clean(uom),
        "BalQty": qty,
        "IsActive": _clean(src.get("IsActive")),
        "StockControl": _clean(src.get("StockControl")),
        "RowSource": row_source,
    }


def render_csv(rows, columns):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(safe_csv_row(row, columns) for row in rows)
    return buffer.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------------


def write_exclusive(path, data):
    """Exclusive create + fsync. Never overwrites. Returns (sha256, size)."""
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return hashlib.sha256(data).hexdigest(), len(data)


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replace_atomic(path, data, tag):
    """Write a temp file, then os.replace. A sync client holding the target briefly is retried."""
    path = Path(path)
    temp = path.with_name(".%s.%s.tmp" % (path.name, tag))
    write_exclusive(temp, data)
    for attempt in range(3):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 2:
                _remove_quietly(temp)
                raise
            time.sleep(1)


def copy_exclusive(src, dst):
    """Copy one file with exclusive create. Patched in tests to simulate a bad copy."""
    write_exclusive(dst, Path(src).read_bytes())


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def json_bytes(payload):
    return (json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")


def git_commit():
    """The repository HEAD commit when readable from the checkout, else None. No subprocess."""
    git_dir = SCRIPT_DIR.parent / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head or None
        ref = head[5:]
        ref_file = git_dir / ref
        if ref_file.exists():
            return ref_file.read_text(encoding="utf-8").strip() or None
        packed = git_dir / "packed-refs"
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------------------------


def acquire_lock(archive_root, run_id, now, warnings):
    """Take the lock, clearing one left by a killed run. Appends a warning when it clears one.

    A lock younger than LOCK_STALE_AFTER_MINUTES gives already_running. An older lock written
    before the last boot is dead (a reboot ends every run), whatever process now has its PID.
    Otherwise a lock whose process is still running gives already_running. An unreadable lock
    gives stale_lock and is kept for the owner to check.
    """
    path = Path(archive_root) / LOCK_NAME
    payload = json_bytes({"run_id": run_id, "pid": os.getpid(), "started_at": now.isoformat()})
    try:
        write_exclusive(path, payload)
        return path
    except FileExistsError:
        pass
    held = _read_lock(path)
    if held is None:
        raise RunFailure(FAILURE_STALE_LOCK, "The lock file is unreadable; check no run is active, then remove it")
    age = now - held["started_at"]
    if age <= timedelta(minutes=LOCK_STALE_AFTER_MINUTES):
        raise RunFailure(FAILURE_ALREADY_RUNNING, "Another run holds the lock")
    boot = _boot_time()
    before_boot = boot is not None and held["started_at"] < boot - timedelta(seconds=LOCK_BOOT_TOLERANCE_SECONDS)
    if not before_boot and _pid_running(held["pid"], held["started_at"]):
        raise RunFailure(FAILURE_ALREADY_RUNNING, "Another run holds the lock")
    reason = "written before the last restart" if before_boot else "whose process is no longer running"
    try:
        # Remove only the lock that was inspected, never one another run has just taken.
        if path.read_bytes() != held["raw"]:
            raise RunFailure(FAILURE_ALREADY_RUNNING, "Another run holds the lock")
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RunFailure(FAILURE_STALE_LOCK, "A lock left by a killed run could not be removed") from exc
    try:
        write_exclusive(path, payload)
    except FileExistsError as exc:
        raise RunFailure(FAILURE_ALREADY_RUNNING, "Another run holds the lock") from exc
    warnings.append(
        "Cleared a lock left by run %s, %s (lock age %d minutes)"
        % (held["run_id"][:8], reason, int(age.total_seconds() // 60))
    )
    return path


def _boot_time():
    """When Windows last started (aware UTC), or None when unknown. None keeps the PID check.

    Uses GetTickCount64 (milliseconds since start, stdlib ctypes only). Not used off Windows.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetTickCount64.argtypes = []
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        uptime_ms = int(kernel32.GetTickCount64())
    except (OSError, AttributeError, ValueError):
        return None
    return datetime.now(timezone.utc) - timedelta(milliseconds=uptime_ms)


def _read_lock(path):
    """The lock's run_id, pid, started_at and raw bytes, or None when it cannot be trusted."""
    try:
        raw = path.read_bytes()
        content = json.loads(raw.decode("utf-8"))
        started = datetime.fromisoformat(content["started_at"])
        pid = content["pid"]
        run_id = content["run_id"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if started.tzinfo is None or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(run_id, str):
        return None
    return {"run_id": run_id, "pid": pid, "started_at": started, "raw": raw}


def _pid_running(pid, started_at):
    """True when the lock's process may still be running. Any doubt counts as running."""
    if os.name == "nt":
        return _windows_pid_running(pid, started_at)
    # POSIX only: signal 0 checks existence. Never used on Windows, where os.kill terminates.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _windows_pid_running(pid, started_at):
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    process_query_limited_information = 0x1000
    error_invalid_parameter = 87
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Invalid parameter: no process has this PID. Anything else (for example access denied)
        # means a process exists that cannot be inspected, so the lock is respected.
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        if exit_code.value != still_active:
            return False
        times = [FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return True
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        created = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
        return created <= started_at + timedelta(seconds=LOCK_PID_START_TOLERANCE_SECONDS)
    finally:
        kernel32.CloseHandle(handle)


def release_lock(path, run_id):
    try:
        if json.loads(Path(path).read_text(encoding="utf-8")).get("run_id") == run_id:
            os.remove(path)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------------


def _now(tz, clock):
    value = clock() if clock else datetime.now(tz)
    return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)


def run_folder_name(started, run_id):
    return "ac2_stockbal_%s_%s" % (started.strftime("%Y%m%dT%H%M%S%z"), run_id[:8])


def run_hourly(settings, source=None, environ=None, clock=None, sleep=time.sleep):
    """Run one hourly export. Always returns the manifest dict (success or failed)."""
    tz = load_zone(settings["timezone"])
    run_id = uuid.uuid4().hex
    started = _now(tz, clock)
    archive_root = Path(settings["output"]["archive_root"])
    delivery_root = settings["output"]["delivery_root"]
    delivery_root = Path(delivery_root) if delivery_root else None
    relative_folder = Path(HOURLY_DIR) / started.strftime("%Y-%m-%d") / run_folder_name(started, run_id)
    run_dir = archive_root / relative_folder

    manifest = {
        "job": JOB,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "failed",
        "failure_class": None,
        "message": None,
        "started_at": started.isoformat(),
        "finished_at": None,
        "as_of_local": None,
        "as_of_sql": None,
        "timezone": settings["timezone"],
        "hourly_folder": relative_folder.as_posix(),
        "source": {
            "surface": SOURCE_SURFACE,
            "database": settings["source"]["expected_database"],
            "query_sha256": query_sha256(BALANCE_QUERY),
            "queries_sha256": {
                "balance": query_sha256(BALANCE_QUERY),
                "items_without_rows": query_sha256(ITEMS_WITHOUT_ROWS_QUERY),
                "multi_uom": query_sha256(MULTI_UOM_QUERY),
                "sync": query_sha256(sync_query(settings["source"]["sync_registration_time_column"])),
                "identity": query_sha256(IDENTITY_QUERY),
                "change_count": query_sha256(CHANGE_COUNT_QUERY),
            },
            "settings_sha256": settings_sha256(settings),
            "git_commit": git_commit(),
        },
        "identity": {"checked": False},
        "attempts": 0,
        "changecount_before": None,
        "changecount_after": None,
        "row_count": 0,
        "csv": None,
        "counts": None,
        "per_location": None,
        "unmapped_location_codes": [],
        "large_balances": None,
        "uom_guard": None,
        "pos_sync": [],
        "delivery": {"status": DELIVERY_SKIPPED if delivery_root else DELIVERY_NOT_CONFIGURED, "files": []},
        "warnings": [],
    }

    lock_path = None
    final_delivery = manifest["delivery"]["status"]
    publish_failure = None
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
        lock_path = acquire_lock(archive_root, run_id, started, manifest["warnings"])
        source = source or SqlServerSource(settings)
        snapshot, attempts, identity = read_snapshot(source, settings, environ, sleep)
        manifest["attempts"] = attempts
        manifest["identity"] = identity
        if identity.get("privileged"):
            manifest["warnings"].append(
                "Privileged read identity (sysadmin or db_owner) allowed by allow_privileged_identity"
            )
        manifest["changecount_before"] = snapshot["changecount_before"]
        manifest["changecount_after"] = snapshot["changecount_after"]
        as_of_sql = snapshot["sql_now"]
        as_of_local = _as_local(as_of_sql, tz)
        manifest["as_of_sql"] = as_of_sql.isoformat() if hasattr(as_of_sql, "isoformat") else None
        manifest["as_of_local"] = as_of_local
        manifest["pos_sync"] = _pos_sync(snapshot["sync_rows"], tz)

        uom = check_uom_guard(snapshot["balances"], snapshot["multi_uom_items"])
        manifest["uom_guard"] = uom
        if uom["result"] != "pass":
            raise RunFailure(FAILURE_UOM, "Non-base UOM found; no CSV until the UOM is mapped")

        rows, counts, per_location, unmapped = build_rows(snapshot, settings, as_of_local)
        manifest["counts"] = counts
        manifest["per_location"] = per_location
        manifest["unmapped_location_codes"] = unmapped
        manifest["row_count"] = len(rows)
        large = find_large_balances(rows, settings["output"]["large_balance_threshold"])
        manifest["large_balances"] = large
        if large["count"]:
            manifest["warnings"].append(
                "%s %d exported row(s) with |BalQty| above %d; check the AutoCount document behind each "
                "listed item (large_balances)" % (LARGE_BALANCE_WARNING_PREFIX, large["count"], large["threshold"])
            )
        data = render_csv(rows, settings["output"]["columns"])

        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            csv_sha, csv_size = write_exclusive(run_dir / CSV_NAME, data)
        except OSError as exc:
            raise RunFailure(FAILURE_WRITE, "Could not write the hourly CSV") from exc
        if file_sha256(run_dir / CSV_NAME) != csv_sha:
            raise RunFailure(FAILURE_WRITE, "Hourly CSV did not re-read to the written hash")
        manifest["csv"] = {"name": CSV_NAME, "sha256": csv_sha, "byte_size": csv_size}
        manifest["status"] = "success"

        if delivery_root:
            manifest["delivery"] = _deliver_csv(run_dir, delivery_root / relative_folder, relative_folder, csv_sha)
        final_delivery = manifest["delivery"]["status"]
    except RunFailure as exc:
        manifest["status"] = "failed"
        manifest["failure_class"] = exc.failure_class
        manifest["message"] = "%s (run %s)" % (sanitize_error(str(exc)), run_id[:8])
        manifest["attempts"] = getattr(exc, "attempts", manifest["attempts"])
        if getattr(exc, "identity", None):
            manifest["identity"] = exc.identity
        if getattr(exc, "changecounts", None):
            manifest["changecount_before"], manifest["changecount_after"] = exc.changecounts
    except Exception:  # noqa: BLE001 - every outcome must leave a manifest; details stay out of it.
        manifest["status"] = "failed"
        manifest["failure_class"] = FAILURE_INTERNAL
        manifest["message"] = "Unexpected error (run %s)" % run_id[:8]

    manifest["finished_at"] = _now(tz, clock).isoformat()
    if manifest["status"] != "success":
        manifest["csv"] = None
        manifest["row_count"] = 0
        manifest["large_balances"] = None
        # A failed run lists no large balances, so it carries no large-balance warning either.
        manifest["warnings"] = [w for w in manifest["warnings"] if not w.startswith(LARGE_BALANCE_WARNING_PREFIX)]
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest_sha, _ = write_exclusive(run_dir / MANIFEST_NAME, json_bytes(manifest))
    except OSError:
        manifest_sha = None
        if manifest["status"] == "success":
            manifest["status"] = "failed"
            manifest["failure_class"] = FAILURE_WRITE
            manifest["message"] = "Could not write the manifest (run %s)" % run_id[:8]

    if manifest["status"] == "success":
        try:
            publish_latest(archive_root, run_dir, manifest, run_id)
        except (OSError, RunFailure):
            publish_failure = FAILURE_LATEST
        if delivery_root and final_delivery == DELIVERY_HANDED:
            final_delivery = _deliver_manifest_and_latest(run_dir, delivery_root / relative_folder, manifest_sha, manifest, run_id, delivery_root)

    status = {
        "job": JOB,
        "run_id": run_id,
        "status": "failed" if publish_failure else manifest["status"],
        "failure_class": publish_failure or manifest["failure_class"],
        "as_of_local": manifest["as_of_local"],
        "started_at": manifest["started_at"],
        "finished_at": manifest["finished_at"],
        "hourly_folder": manifest["hourly_folder"],
        "delivery_status": final_delivery,
        "large_balance_count": (
            _large_balance_count(manifest) if manifest["status"] == "success" and not publish_failure else None
        ),
    }
    manifest["_last_run_status"] = status
    for root in (archive_root, delivery_root):
        if root is None:
            continue
        try:
            root.mkdir(parents=True, exist_ok=True)
            replace_atomic(root / LAST_RUN_STATUS_NAME, json_bytes(status), run_id[:8])
        except OSError:
            pass
    if lock_path is not None:
        release_lock(lock_path, run_id)
    return manifest


def _large_balance_count(manifest):
    """The large-balance count of a manifest, or None when it has none (failed, or written before the check)."""
    large = manifest.get("large_balances")
    count = large.get("count") if isinstance(large, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) else None


def _as_local(value, tz):
    if not hasattr(value, "astimezone"):
        raise RunFailure(FAILURE_SOURCE, "SQL read time was not a timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(tz).isoformat()


def _pos_sync(rows, tz):
    out = []
    for r in rows:
        value = r.get("LastSyncTime")
        if value is not None and hasattr(value, "isoformat"):
            # AutoCount stores local wall-clock time without an offset.
            value = (value if value.tzinfo else value.replace(tzinfo=tz)).astimezone(tz).isoformat()
        else:
            value = None
        out.append({"Location": _clean(r.get("Location")), "last_sync_local": value})
    out.sort(key=lambda r: (r["Location"], r["last_sync_local"] or ""))
    return out


def _deliver_csv(run_dir, target_dir, relative_folder, csv_sha):
    files = [(relative_folder / CSV_NAME).as_posix()]
    try:
        target_dir.mkdir(parents=True, exist_ok=False)
        copy_exclusive(run_dir / CSV_NAME, target_dir / CSV_NAME)
    except OSError:
        return {"status": DELIVERY_COPY_FAILED, "files": files}
    try:
        verified = file_sha256(target_dir / CSV_NAME) == csv_sha
    except OSError:
        verified = False
    return {"status": DELIVERY_HANDED if verified else DELIVERY_VERIFY_FAILED, "files": files}


def _deliver_manifest_and_latest(run_dir, target_dir, manifest_sha, manifest, run_id, delivery_root):
    """Copy the manifest, verify it, then advance the delivery latest pair."""
    try:
        copy_exclusive(run_dir / MANIFEST_NAME, target_dir / MANIFEST_NAME)
    except OSError:
        return DELIVERY_COPY_FAILED
    try:
        verified = manifest_sha is not None and file_sha256(target_dir / MANIFEST_NAME) == manifest_sha
    except OSError:
        verified = False
    if not verified:
        return DELIVERY_VERIFY_FAILED
    try:
        publish_latest(delivery_root, target_dir, manifest, run_id)
    except (OSError, RunFailure):
        return DELIVERY_VERIFY_FAILED
    return DELIVERY_HANDED


def publish_latest(root, run_dir, manifest, run_id):
    """Replace the latest pair only after the hourly CSV re-reads to the manifest hash."""
    data = Path(run_dir, CSV_NAME).read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest["csv"]["sha256"]:
        raise RunFailure(FAILURE_LATEST, "Hourly CSV hash changed before publishing latest")
    latest = {key: value for key, value in manifest.items() if not key.startswith("_")}
    replace_atomic(Path(root) / LATEST_CSV_NAME, data, run_id[:8])
    replace_atomic(Path(root) / LATEST_MANIFEST_NAME, json_bytes(latest), run_id[:8])


# ---------------------------------------------------------------------------------------------
# Stale check
# ---------------------------------------------------------------------------------------------


def check_latest(settings, clock=None):
    """Read-only freshness check of the latest pair. Returns (exit_code, status dict)."""
    tz = load_zone(settings["timezone"])
    root = Path(settings["output"]["delivery_root"] or settings["output"]["archive_root"])
    now = _now(tz, clock)
    try:
        latest = json.loads((root / LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        data = (root / LATEST_CSV_NAME).read_bytes()
    except FileNotFoundError:
        return EXIT_MISSING, {"status": "missing"}
    except (OSError, ValueError):
        return EXIT_MISSING, {"status": "invalid"}
    try:
        if latest.get("job") != JOB or latest.get("status") != "success":
            raise ValueError("not a successful hourly manifest")
        if hashlib.sha256(data).hexdigest() != latest["csv"]["sha256"]:
            raise ValueError("latest CSV does not match latest manifest")
        as_of = datetime.fromisoformat(latest["as_of_local"])
        age_minutes = int((now - as_of).total_seconds() // 60)
    except (KeyError, TypeError, ValueError):
        return EXIT_MISSING, {"status": "invalid"}
    result = {
        "as_of_local": latest["as_of_local"],
        "age_minutes": age_minutes,
        "run_id": latest.get("run_id"),
        "large_balance_count": _large_balance_count(latest),
    }
    if age_minutes > settings["stale_after_minutes"]:
        return EXIT_STALE, dict(result, status="stale")
    return EXIT_OK, dict(result, status="fresh")


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def config_failure_manifest(raw, message, failure_class=FAILURE_CONFIG, clock=None):
    """Best-effort FAILED manifest for an invalid config, when an archive root is readable.

    Never raises. Without any zone database the times are stamped in UTC with a visible note,
    and last_run_status.json also goes to delivery_root when one is readable, so a stale
    success status is not left in Drive.
    """
    output = raw.get("output") if isinstance(raw, dict) else None
    root = output.get("archive_root") if isinstance(output, dict) else None
    if not isinstance(root, str) or not root.strip():
        return None
    delivery = output.get("delivery_root")
    tz_names = [DEFAULT_SETTINGS["timezone"]]
    if isinstance(raw.get("timezone"), str):
        tz_names.insert(0, raw["timezone"])
    tz, tz_name, note = timezone.utc, "UTC", "Time zone data unavailable: times are UTC"
    for name in tz_names:
        try:
            tz, tz_name, note = load_zone(name), name, None
            break
        except RuntimeError:
            continue
    run_id = uuid.uuid4().hex
    now = _now(tz, clock)
    relative = Path(HOURLY_DIR) / now.strftime("%Y-%m-%d") / run_folder_name(now, run_id)
    manifest = {
        "job": JOB,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "failed",
        "failure_class": failure_class,
        "message": "%s (run %s)" % (message, run_id[:8]),
        "started_at": now.isoformat(),
        "finished_at": now.isoformat(),
        "as_of_local": None,
        "timezone": tz_name,
        "note": note,
        "hourly_folder": relative.as_posix(),
    }
    status = {k: manifest[k] for k in ("job", "run_id", "status", "failure_class", "as_of_local", "started_at", "finished_at", "hourly_folder", "note")}
    status["delivery_status"] = DELIVERY_SKIPPED
    status["large_balance_count"] = None
    try:
        run_dir = Path(root) / relative
        run_dir.mkdir(parents=True, exist_ok=False)
        write_exclusive(run_dir / MANIFEST_NAME, json_bytes(manifest))
    except (OSError, ValueError):
        manifest = None
    roots = [root]
    if isinstance(delivery, str) and delivery.strip():
        roots.append(delivery)
    for index, status_root in enumerate(roots):
        try:
            if index and _same_path(status_root, root):
                continue
            Path(status_root).mkdir(parents=True, exist_ok=True)
            replace_atomic(Path(status_root) / LAST_RUN_STATUS_NAME, json_bytes(status), run_id[:8])
        except (OSError, ValueError):
            pass
    return manifest


def main(argv=None, source=None, environ=None, clock=None):
    parser = argparse.ArgumentParser(description="Hourly read-only AC2 stock balance export (#245).")
    parser.add_argument("--config", required=True, help="Local settings JSON (config/*.local.json, never committed).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-latest", action="store_true", help="Read-only stale check of the latest pair.")
    mode.add_argument("--dry-run-config", action="store_true", help="Validate the settings only.")
    # --check-latest exits only 0, 2 or 3, so a usage error there is 3, never argparse's 2.
    check_mode = "--check-latest" in (sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if check_mode and exc.code not in (0, None):
            return EXIT_MISSING
        raise

    raw = None
    try:
        raw = load_settings_file(args.config)
        settings = validate_settings(raw)
    except Exception as exc:  # noqa: BLE001 - a settings failure must leave a manifest, never a traceback.
        if isinstance(exc, ConfigError):
            failure_class, message = exc.failure_class, "Settings invalid: %s" % exc
        else:
            # validate_settings raises only ConfigError; anything else is a bug, reported generically.
            failure_class, message = FAILURE_INTERNAL, "Settings could not be validated (unexpected error)"
        if args.check_latest:
            _print({"status": "invalid", "failure_class": failure_class, "message": message})
            return EXIT_MISSING
        if not args.dry_run_config and raw is not None:
            config_failure_manifest(raw, message, failure_class, clock)
        _print({"status": "failed", "failure_class": failure_class, "message": message})
        return EXIT_FAILED

    if args.dry_run_config:
        # The installer reads auth_mode, scheduled_logon_type and run_minute to register the task.
        _print(
            {
                "status": "valid",
                "settings_sha256": settings_sha256(settings),
                "auth_mode": settings["identity"]["auth_mode"],
                "scheduled_logon_type": settings["schedule"]["logon_type"],
                "run_minute": settings["schedule"]["run_minute"],
            }
        )
        return EXIT_OK
    if args.check_latest:
        try:
            code, result = check_latest(settings, clock)
        except Exception:  # noqa: BLE001 - --check-latest exits only 0, 2 or 3; 3 means invalid.
            code, result = EXIT_MISSING, {"status": "invalid", "failure_class": FAILURE_INTERNAL}
        _print(result)
        return code

    manifest = run_hourly(settings, source=source, environ=environ, clock=clock)
    status = manifest["_last_run_status"]
    _print(
        {
            "status": status["status"],
            "failure_class": status["failure_class"],
            "run_id": manifest["run_id"],
            "as_of_local": manifest["as_of_local"],
            "row_count": manifest["row_count"],
            "delivery_status": status["delivery_status"],
            "large_balance_count": status["large_balance_count"],
            "message": manifest["message"],
        }
    )
    if status["status"] != "success":
        return EXIT_FAILED
    if status["delivery_status"] not in (DELIVERY_HANDED, DELIVERY_NOT_CONFIGURED):
        return EXIT_FAILED
    return EXIT_OK


def _print(payload):
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
