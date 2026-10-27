"""Hourly AutoCount (AC2) stock balance export with a daily close (#245).

Reads the stored on-hand balance (dbo.ItemBatchBalQty) and the stock ledger (dbo.StockDTL)
read-only, once per hour, and writes an immutable local hourly CSV plus a manifest, a verified
"latest" pair, a compact dashboard feed and a last_run_status.json that also records failed
hours. The first run after local midnight also publishes the previous day's closing balance,
rebuilt from the ledger, as one daily file (round 6, accepted design 27 Oct 2026). Delivery to
Google Drive is a plain copy into a local folder that Google Drive for desktop syncs; only the
latest pair, the feed, the run status and the daily files go there, never hourly folders.
Nothing here calls a Drive API.

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
* Every run checks that the ledger summed over all dates equals the stored balance per key, read
  inside the same ChangeCount window. A mismatch fails the run (ledger_mismatch): no latest pair,
  no daily close, no feed.
* Daily files may be replaced only inside the restate window (default 7 days); a change to an
  older day is counted and flagged, never written. Pruning (off by default) deletes only the
  exporter's own file names inside its own archive folders.
* Passwords only come from Windows Credential Manager at runtime. Connection strings, logins,
  host names and row data are never logged or written to a manifest, except that a successful
  run's manifest lists at most 20 large-balance rows (ItemCode, Location, BalQty), the same
  rows its CSV already holds.

pyodbc is imported only inside SqlServerSource so the tests run without it.
"""

import argparse
import copy
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
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
# 2: round 6 adds the ledger check, the daily close, the feed and retention blocks.
SCHEMA_VERSION = "2"
SOURCE_SURFACE = "dbo.ItemBatchBalQty"
LEDGER_SURFACE = "dbo.StockDTL"

CSV_NAME = "stock_balance.csv"
MANIFEST_NAME = "manifest.json"
LATEST_CSV_NAME = "stock_balance_latest.csv"
LATEST_MANIFEST_NAME = "latest_manifest.json"
LAST_RUN_STATUS_NAME = "last_run_status.json"
LOCK_NAME = ".ac2_stockbal.lock"
HOURLY_DIR = "hourly"
DAILY_DIR = "daily"
# Local only (never delivered, never pruned): what the daily files were built from.
CLOSE_STATE_NAME = "close_state.json.gz"
CLOSE_STATE_SCHEMA = "xb.ac2.stock_close_state.v1"
FEED_NAME = "stock_dashboard_feed.json.gz"
FEED_SCHEMA = "xb.ac2.stock_dashboard_feed"
FEED_SCHEMA_VERSION = 1
DAILY_JOB = "ac2_daily_stock_close"
DAILY_COLUMNS = (
    "close_date",
    "ItemCode",
    "ItemDescription",
    "ItemType",
    "Location",
    "LocationLabel",
    "UOM",
    "CloseQty",
    "IsActive",
    "StockControl",
)
# Exporter-owned names. Pruning matches these exactly and deletes nothing else.
HOURLY_DATE_DIR_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
RUN_FOLDER_RE = re.compile(r"\Aac2_stockbal_(\d{8}T\d{6}[+-]\d{4})_[0-9a-f]{8}\Z")
DAILY_MONTH_DIR_RE = re.compile(r"\A\d{4}-\d{2}\Z")
DAILY_FILE_RE = re.compile(r"\Astock_close_(\d{4}-\d{2}-\d{2})\.(csv|manifest\.json)\Z")
# Lists in the manifest and feed are capped; the counts are always complete.
CHANGED_DAYS_LIST_CAP = 31
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
FAILURE_LEDGER_MISMATCH = "ledger_mismatch"
# Daily close and feed outcomes (the hourly snapshot itself can still be a success).
FAILURE_CLOSE_WRITE = "daily_close_write_failed"
FAILURE_FEED = "feed_publish_failed"

CLOSE_NOT_DUE = "not_due"
CLOSE_PUBLISHED = "published"
CLOSE_FAILED = "failed"
CLOSE_SKIPPED = "skipped_failed_run"

DELIVERY_NOT_CONFIGURED = "not_configured"
DELIVERY_HANDED = "handed_to_drive_sync_folder"
DELIVERY_VERIFY_FAILED = "verify_failed"
DELIVERY_COPY_FAILED = "copy_failed"
DELIVERY_SKIPPED = "skipped_failed_run"
# Written into the run manifest before delivery; the result is in last_run_status.json.
DELIVERY_PENDING = "pending_see_last_run_status"

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
# The whole stock ledger, summed per key and business day. DocDay is the date part of DocDate
# (AutoCount stores local wall-clock time; one document type carries real times), so a close
# "as at the end of day D" sums every group with DocDay < D + 1, which is exactly
# DocDate < next local midnight. Never filtered by date: every close sums from the first row.
LEDGER_QUERY = """SELECT
    d.ItemCode,
    d.UOM,
    d.Location,
    CAST(d.DocDate AS date) AS DocDay,
    SUM(d.Qty) AS Qty,
    COUNT_BIG(*) AS LedgerRows,
    SUM(CASE WHEN d.Qty IS NULL THEN 1 ELSE 0 END) AS NullQtyRows,
    SUM(CASE WHEN d.BatchNo IS NOT NULL AND d.BatchNo <> N'' THEN 1 ELSE 0 END) AS NonEmptyBatchRows
FROM dbo.StockDTL AS d
GROUP BY d.ItemCode, d.UOM, d.Location, CAST(d.DocDate AS date)"""
ITEM_MASTER_QUERY = """SELECT
    i.ItemCode,
    i.Description AS ItemDescription,
    i.ItemType,
    i.IsActive,
    i.StockControl,
    i.BaseUOM
FROM dbo.Item AS i"""
LOCATION_QUERY = "SELECT l.Location, l.Description AS LocationDescription FROM dbo.Location AS l"
# Reorder levels live in dbo.ItemUOM (not dbo.Item). Read for information only: the low-stock
# rule is the global feed.low_stock_threshold (owner, 28 Oct 2026).
REORDER_QUERY = "SELECT u.ItemCode, u.UOM, u.ReOLevel FROM dbo.ItemUOM AS u WHERE u.ReOLevel > 0"


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
        "exclude_non_stock_items": True,
    },
    "location_labels": {},
    "output": {
        "archive_root": None,
        "delivery_root": None,
        "columns": DEFAULT_COLUMNS,
        "large_balance_threshold": 100000,
    },
    "daily_close": {
        # Owner decision 27 Oct 2026: the later of 1 Aug 2026 and the opening-stock date (R1: 31 Jul).
        "backfill_start": "2026-08-01",
        "restate_days": 7,
    },
    "retention": {
        "prune_enabled": False,
        "hourly_keep_hours": 48,
        "daily_keep_days": 90,
    },
    "feed": {
        "low_stock_threshold": 2,
        "list_cap": 200,
        "movers_top": 20,
    },
}
# Sections whose keys are a closed set. location_labels is an open code -> label map.
CLOSED_SECTIONS = ("schedule", "source", "identity", "read", "rows", "output", "daily_close", "retention", "feed")


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
    _require_bool(rows["exclude_non_stock_items"], "rows.exclude_non_stock_items")

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

    close = settings["daily_close"]
    start = close["backfill_start"]
    try:
        # Python 3.11 fromisoformat also takes other ISO forms (week dates), so match first.
        if not isinstance(start, str) or not HOURLY_DATE_DIR_RE.match(start):
            raise ValueError("not YYYY-MM-DD")
        date.fromisoformat(start)
    except ValueError as exc:
        raise ConfigError("daily_close.backfill_start must be a date as YYYY-MM-DD") from exc
    _require_int(close["restate_days"], "daily_close.restate_days", 1, 31)

    retention = settings["retention"]
    _require_bool(retention["prune_enabled"], "retention.prune_enabled")
    _require_int(retention["hourly_keep_hours"], "retention.hourly_keep_hours", 2, 24 * 366)
    # Daily files inside the restate window are never pruned.
    _require_int(retention["daily_keep_days"], "retention.daily_keep_days", close["restate_days"] + 1, 3660)

    feed = settings["feed"]
    _require_int(feed["low_stock_threshold"], "feed.low_stock_threshold", 0, 1000000)
    _require_int(feed["list_cap"], "feed.list_cap", 1, 5000)
    _require_int(feed["movers_top"], "feed.movers_top", 1, 200)
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

    def read_ledger(self):
        return self._fetch_dicts(LEDGER_QUERY)

    def read_item_master(self):
        return self._fetch_dicts(ITEM_MASTER_QUERY)

    def read_locations(self):
        return self._fetch_dicts(LOCATION_QUERY)

    def read_reorder_levels(self):
        return self._fetch_dicts(REORDER_QUERY)

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
    dbo.ChangeCount has no row for the ledger (R1), so the ledger read sits inside the same
    balance-table window: a stock posting writes both tables, so it moves the balance counter.
    The ledger-equals-balance check after the read then catches anything the counter missed.
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
                ledger = session.read_ledger()
                item_master = session.read_item_master()
                location_rows = session.read_locations()
                reorder_rows = session.read_reorder_levels()
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
                "ledger": ledger,
                "item_master": item_master,
                "location_rows": location_rows,
                "reorder_rows": reorder_rows,
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


def check_uom_guard(balances, multi_uom_items, ledger_non_base_groups=0):
    mismatched = sum(1 for b in balances if _clean(b.get("UOM")) != _clean(b.get("BaseUOM")) or not _clean(b.get("BaseUOM")))
    result = {
        "mode": "fail",
        "non_base_uom_rows": mismatched,
        "multi_uom_items": int(multi_uom_items),
        "ledger_non_base_uom_groups": int(ledger_non_base_groups),
    }
    clean = mismatched == 0 and multi_uom_items == 0 and ledger_non_base_groups == 0
    result["result"] = "pass" if clean else "fail"
    return result


def is_non_stock_item(item, rows_cfg):
    """True for an item AutoCount marks as not stock-controlled (StockControl 'F'), when excluded.

    Only an explicit 'F' counts. An item missing from the item master (no StockControl) is kept,
    so nothing unknown is silently hidden.
    """
    return bool(rows_cfg["exclude_non_stock_items"]) and _clean(item.get("StockControl")) == "F"


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
        "non_stock_excluded": 0,
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
        if is_non_stock_item(b, rows_cfg):
            # Non-stock-controlled codes (for example marketplace fee codes) are not stock.
            counts["non_stock_excluded"] += 1
            continue
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
# Ledger, daily closes and history (pure; no I/O)
# ---------------------------------------------------------------------------------------------


def _as_day(value):
    """A ledger DocDay as a date. pyodbc returns datetime.date for a SQL date column."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise RunFailure(FAILURE_SOURCE, "A ledger row had no usable date")


def _key(row):
    return (_clean(row.get("ItemCode")), _clean(row.get("UOM")), _clean(row.get("Location")))


def build_ledger(snapshot, settings, today):
    """Group the ledger read by business day. Pure.

    Returns a dict:
      totals    {key: qty} over every row and every date, all items (for the balance check);
      movements {day: {key: qty}} for stock items only; non-stock codes are excluded and counted;
      items     {ItemCode: item master row};
      counts    ledger counts for the manifest (no item-level values).
    key = (ItemCode, UOM, Location).
    """
    rows_cfg = settings["rows"]
    items = {_clean(i.get("ItemCode")): i for i in snapshot["item_master"]}
    totals = {}
    movements = {}
    non_stock_items = set()
    missing_items = set()
    counts = {
        "ledger_groups": 0,
        "ledger_rows": 0,
        "null_qty_rows": 0,
        "non_empty_batch": 0,
        "future_dated_rows": 0,
        "non_base_uom_groups": 0,
        "non_stock_rows_excluded": 0,
        "non_stock_items_excluded": 0,
        "items_missing_from_master": 0,
        "first_day": None,
        "last_day": None,
    }
    days = set()
    for r in snapshot["ledger"]:
        counts["ledger_groups"] += 1
        ledger_rows = int(r.get("LedgerRows") or 0)
        counts["ledger_rows"] += ledger_rows
        counts["null_qty_rows"] += int(r.get("NullQtyRows") or 0)
        counts["non_empty_batch"] += int(r.get("NonEmptyBatchRows") or 0)
        day = _as_day(r.get("DocDay"))
        days.add(day)
        key = _key(r)
        qty = to_decimal(r.get("Qty"))
        totals[key] = totals.get(key, Decimal(0)) + qty
        if day > today:
            counts["future_dated_rows"] += ledger_rows
        item = items.get(key[0])
        if item is None:
            missing_items.add(key[0])
        # Same rule as the balance UOM guard: the ledger UOM must be the item's base UOM.
        base = _clean(item.get("BaseUOM")) if item is not None else ""
        if not base or key[1] != base:
            counts["non_base_uom_groups"] += 1
        if item is not None and is_non_stock_item(item, rows_cfg):
            counts["non_stock_rows_excluded"] += ledger_rows
            non_stock_items.add(key[0])
            continue
        day_moves = movements.setdefault(day, {})
        day_moves[key] = day_moves.get(key, Decimal(0)) + qty
    for day in list(movements):
        movements[day] = {k: q for k, q in movements[day].items() if q != 0}
        if not movements[day]:
            del movements[day]
    counts["non_stock_items_excluded"] = len(non_stock_items)
    counts["items_missing_from_master"] = len(missing_items)
    if days:
        counts["first_day"] = min(days).isoformat()
        counts["last_day"] = max(days).isoformat()
    return {"totals": totals, "movements": movements, "items": items, "counts": counts}


def check_ledger_identity(balances, totals):
    """The ledger summed over all dates must equal the stored balance for every key. Pure.

    Keys on both sides must match exactly; a balance-only key must be 0 and a ledger-only key
    must net 0 (R1 found both kinds, all zero). Counts only, never keys or quantities.
    """
    stored = {}
    for b in balances:
        key = _key(b)
        stored[key] = stored.get(key, Decimal(0)) + to_decimal(b.get("BalQty"))
    result = {
        "keys_compared": 0,
        "mismatched": 0,
        "balance_only": 0,
        "balance_only_non_zero": 0,
        "ledger_only": 0,
        "ledger_only_non_zero": 0,
    }
    for key in set(stored) | set(totals):
        if key in stored and key in totals:
            result["keys_compared"] += 1
            if stored[key] != totals[key]:
                result["mismatched"] += 1
        elif key in stored:
            result["balance_only"] += 1
            if stored[key] != 0:
                result["balance_only_non_zero"] += 1
        else:
            result["ledger_only"] += 1
            if totals[key] != 0:
                result["ledger_only_non_zero"] += 1
    bad = result["mismatched"] + result["balance_only_non_zero"] + result["ledger_only_non_zero"]
    result["result"] = "pass" if bad == 0 else "fail"
    return result


def iter_closes(movements, last_day, first_day=None):
    """Yield (day, close) for every day from first_day to last_day.

    close is the balance as at the end of that day: the sum of every movement whose business day
    is before the next local midnight (DocDay < day + 1, i.e. DocDate < next midnight), summed
    from the very first ledger row whatever first_day is. Zero balances are left out. The same
    dict is updated in place for the next day: a caller that keeps a close must copy it.
    """
    days = sorted(movements)
    if first_day is None:
        if not days:
            return
        first_day = days[0]
    current = {}
    index = 0
    day = first_day
    while day <= last_day:
        next_midnight = day + timedelta(days=1)
        while index < len(days) and days[index] < next_midnight:
            for key, qty in movements[days[index]].items():
                value = current.get(key, Decimal(0)) + qty
                if value == 0:
                    current.pop(key, None)
                else:
                    current[key] = value
            index += 1
        yield day, current
        day = next_midnight


def day_range(first, last):
    out = []
    day = first
    while day <= last:
        out.append(day)
        day += timedelta(days=1)
    return out


def changed_closes(old_movements, new_movements, days):
    """Close days whose close differs between two ledger reads. Pure.

    Returns {day: {"rows_changed": n, "net_qty_change": Decimal}} for the changed days in days.
    The difference ledger is summed with iter_closes, so a backdated movement changes every
    close from its day on, exactly as it changes the real closes.
    """
    if not days:
        return {}
    diff = {}
    for day in set(old_movements) | set(new_movements):
        old = old_movements.get(day, {})
        new = new_movements.get(day, {})
        for key in set(old) | set(new):
            delta = new.get(key, Decimal(0)) - old.get(key, Decimal(0))
            if delta != 0:
                diff.setdefault(day, {})[key] = delta
    wanted = set(days)
    out = {}
    for day, cumulative in iter_closes(diff, days[-1], first_day=min(days[0], min(diff) if diff else days[0])):
        if day in wanted and cumulative:
            out[day] = {"rows_changed": len(cumulative), "net_qty_change": sum(cumulative.values(), Decimal(0))}
    return out


def plan_daily_close(state, movements, today, settings, force):
    """Which daily files this run writes. Pure.

    The close of day D-1 is due when the published state has no close for it yet (the first run
    after local midnight, or the next run after a missed or failed one). A due run rebuilds every
    close from the ledger and compares it with what was published:
      * a day never published is written ("new");
      * a changed day inside the restate window (D-1 .. D-restate_days) is replaced ("revised");
      * a changed older day is left as published and flagged ("late").
    force (--backfill) or a missing state writes every day from backfill_start ("rebuild").
    """
    close_cfg = settings["daily_close"]
    start = date.fromisoformat(close_cfg["backfill_start"])
    last_day = today - timedelta(days=1)
    window_start = today - timedelta(days=close_cfg["restate_days"])
    plan = {
        "due": False,
        "mode": None,
        "close_day": last_day.isoformat(),
        "window_start": window_start,
        "write": {},
        "revised": {},
        "late": {},
    }
    if last_day < start:
        return plan
    days = day_range(start, last_day)
    if force or state is None:
        plan.update(due=True, mode="backfill" if force else "initial")
        plan["write"] = {day: "rebuild" for day in days}
        return plan
    last_published = state.get("last_close_day")
    if last_published is not None and date.fromisoformat(last_published) >= last_day:
        return plan
    plan.update(due=True, mode="nightly")
    published = state.get("published", {})
    changed = changed_closes(state.get("movements", {}), movements, days)
    for day in days:
        if day.isoformat() not in published:
            plan["write"][day] = "new"
        elif day in changed:
            if day >= window_start:
                plan["write"][day] = "revised"
                plan["revised"][day] = changed[day]
            else:
                plan["late"][day] = changed[day]
    return plan


def rebuild_history(movements, settings, last_day, on_close=None):
    """Walk every close from the first ledger day to last_day once. Pure apart from on_close.

    Returns per-location daily totals from backfill_start, the closes kept for the movers, and
    the day each currently negative key turned negative. on_close(day, close) is called for each
    day from backfill_start (the caller writes the due daily files there).
    """
    start = date.fromisoformat(settings["daily_close"]["backfill_start"])
    keep = {last_day - timedelta(days=n) for n in (0, 1, 7, 30)}
    per_day = []
    snapshots = {}
    negative_since = {}
    locations = set()
    if movements and min(movements) <= last_day:
        for day, close in iter_closes(movements, last_day):
            for key, qty in close.items():
                if qty < 0 and key not in negative_since:
                    negative_since[key] = day
            for key in [k for k in negative_since if close.get(k, Decimal(0)) >= 0]:
                del negative_since[key]
            if day in keep:
                snapshots[day] = dict(close)
            if day < start:
                continue
            totals = {}
            for (item, uom, loc), qty in close.items():
                entry = totals.setdefault(loc, [Decimal(0), 0, 0])
                entry[0] += qty
                if qty > 0:
                    entry[1] += 1
                else:
                    entry[2] += 1
                locations.add(loc)
            per_day.append((day, totals))
            if on_close is not None:
                on_close(day, close)
    # Days with no ledger movement at all before them still get an (empty) entry.
    seen = {day for day, _ in per_day}
    for day in day_range(start, last_day):
        if day not in seen:
            per_day.append((day, {}))
            if on_close is not None:
                on_close(day, {})
    per_day.sort(key=lambda entry: entry[0])
    series = {
        loc: {
            "units": [totals.get(loc, [Decimal(0), 0, 0])[0] for _, totals in per_day],
            "skus_in_stock": [totals.get(loc, [Decimal(0), 0, 0])[1] for _, totals in per_day],
            "negative": [totals.get(loc, [Decimal(0), 0, 0])[2] for _, totals in per_day],
        }
        for loc in sorted(locations)
    }
    return {
        "days": [day for day, _ in per_day],
        "per_location": series,
        "snapshots": snapshots,
        "negative_since": negative_since,
        "last_day": last_day,
    }


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
    _replace_with_retry(temp, path)


def _replace_with_retry(temp, path):
    for attempt in range(3):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 2:
                _remove_quietly(temp)
                raise
            time.sleep(1)


class VerifyError(OSError):
    """A written copy did not re-read to the hash of the data."""


def copy_bytes_exclusive(path, data):
    """Write one copy with exclusive create. Patched in tests to simulate a bad copy."""
    write_exclusive(path, data)


def publish_verified(path, data, tag):
    """Replace path with data only after a temp copy re-reads to the data's hash.

    Used for everything that goes to the Drive sync folder and for the daily files. On a hash
    mismatch the target is left as it was and VerifyError is raised. Returns the sha256.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".%s.%s.tmp" % (path.name, tag))
    expected = hashlib.sha256(data).hexdigest()
    copy_bytes_exclusive(temp, data)
    try:
        if file_sha256(temp) != expected:
            raise VerifyError("copy did not re-read to the written hash")
    except OSError:
        _remove_quietly(temp)
        raise
    _replace_with_retry(temp, path)
    return expected


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def json_bytes(payload):
    return (json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")


def gzip_json_bytes(payload):
    """Compact JSON, gzipped with a fixed header time so equal content gives equal bytes."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return gzip.compress(raw, compresslevel=9, mtime=0)


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
# Daily close files and the close state
# ---------------------------------------------------------------------------------------------


def close_state_path(archive_root):
    return Path(archive_root) / DAILY_DIR / CLOSE_STATE_NAME


def load_close_state(archive_root):
    """(state, "loaded") or (None, "missing" / "unreadable"). Local only; never in Drive.

    The state holds the ledger movements the published daily files were built from, plus one
    small entry per published day. A missing or unreadable state makes the next due run write
    every day again from the ledger (self-healing, with a warning).
    """
    try:
        raw = json.loads(gzip.decompress(close_state_path(archive_root).read_bytes()).decode("utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, EOFError, ValueError):
        return None, "unreadable"
    try:
        if raw.get("schema") != CLOSE_STATE_SCHEMA:
            raise ValueError("unknown state schema")
        movements = {
            date.fromisoformat(day): {(i, u, l): Decimal(q) for i, u, l, q in entries}
            for day, entries in raw["movements"].items()
        }
        published = raw["published"]
        if not isinstance(published, dict):
            raise ValueError("published is not a map")
        for day in published:
            date.fromisoformat(day)
        last_close_day = raw.get("last_close_day")
        if last_close_day is not None:
            date.fromisoformat(last_close_day)
    except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation):
        return None, "unreadable"
    return {
        "last_close_day": last_close_day,
        "published": published,
        "movements": movements,
        "last_close_run": raw.get("last_close_run"),
    }, "loaded"


def close_state_bytes(state):
    movements = {
        day.isoformat(): [[i, u, l, str(tidy_qty(q))] for (i, u, l), q in sorted(moves.items())]
        for day, moves in sorted(state["movements"].items())
    }
    return gzip_json_bytes(
        {
            "schema": CLOSE_STATE_SCHEMA,
            "last_close_day": state["last_close_day"],
            "published": state["published"],
            "movements": movements,
            "last_close_run": state.get("last_close_run"),
        }
    )


def daily_relative_paths(day):
    folder = Path(DAILY_DIR) / day.strftime("%Y-%m")
    base = "stock_close_%s" % day.isoformat()
    return folder / (base + ".csv"), folder / (base + ".manifest.json")


def render_daily_rows(day, close, items, location_names, labels):
    """Non-zero close rows for one day, plus per-location totals and unmapped codes. Pure."""
    rows = []
    per_location = {}
    unmapped = set()
    for (item_code, uom, loc), qty in close.items():
        item = items.get(item_code, {})
        label, mapped = location_label(loc, location_names.get(loc), labels)
        if not mapped:
            unmapped.add(loc)
        entry = per_location.setdefault(loc, {"rows": 0, "negative": 0, "sum_qty": Decimal(0)})
        entry["rows"] += 1
        entry["sum_qty"] += qty
        if qty < 0:
            entry["negative"] += 1
        rows.append(
            {
                "close_date": day.isoformat(),
                "ItemCode": item_code,
                "ItemDescription": _clean(item.get("ItemDescription")),
                "ItemType": _clean(item.get("ItemType")),
                "Location": loc,
                "LocationLabel": label,
                "UOM": uom,
                "CloseQty": tidy_qty(qty),
                "IsActive": _clean(item.get("IsActive")),
                "StockControl": _clean(item.get("StockControl")),
            }
        )
    rows.sort(key=lambda r: (r["ItemCode"], r["Location"], r["UOM"]))
    totals = {
        code: {"rows": v["rows"], "negative": v["negative"], "sum_qty": str(tidy_qty(v["sum_qty"]))}
        for code, v in sorted(per_location.items())
    }
    return rows, totals, sorted(unmapped)


def write_daily_day(roots, day, close, ctx, reason, previous):
    """Write one day's close CSV and manifest to every root (archive, then Drive sync folder).

    Each file goes through publish_verified. Raises OSError when any write or check fails.
    Returns the published-day entry for the close state.
    """
    rows, per_location, unmapped = render_daily_rows(day, close, ctx["items"], ctx["location_names"], ctx["labels"])
    data = render_csv(rows, list(DAILY_COLUMNS))
    revision = int((previous or {}).get("revision", 0)) + 1
    csv_rel, manifest_rel = daily_relative_paths(day)
    meta = {
        "job": DAILY_JOB,
        "schema_version": "1",
        "close_date": day.isoformat(),
        "close_rule": "Sum of every stock ledger row dated before %s 00:00 local, from the first ledger row"
        % (day + timedelta(days=1)).isoformat(),
        "rows_rule": "Non-zero balances only; non-stock-controlled items excluded",
        "write_reason": reason,
        "revision": revision,
        "run_id": ctx["run_id"],
        "written_at": ctx["written_at"],
        "ledger_read_as_of_local": ctx["as_of_local"],
        # Replaced only while the day is inside the restate window; from this date it is final.
        "final_from": (day + timedelta(days=ctx["restate_days"] + 1)).isoformat(),
        "row_count": len(rows),
        "csv": {"name": csv_rel.name, "sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data)},
        "per_location": per_location,
        "unmapped_location_codes": unmapped,
    }
    meta_data = json_bytes(meta)
    for root in roots:
        publish_verified(Path(root) / csv_rel, data, ctx["tag"])
        publish_verified(Path(root) / manifest_rel, meta_data, ctx["tag"])
    return {
        "revision": revision,
        "row_count": len(rows),
        "csv_sha256": meta["csv"]["sha256"],
        "written_at": ctx["written_at"],
    }, unmapped


def _day_change_list(changes):
    entries = [
        {"day": day.isoformat(), "rows_changed": v["rows_changed"], "net_qty_change": str(tidy_qty(v["net_qty_change"]))}
        for day, v in sorted(changes.items())
    ]
    return entries[:CHANGED_DAYS_LIST_CAP], len(entries) > CHANGED_DAYS_LIST_CAP


def run_daily_close(settings, archive_root, delivery_root, ledger, snapshot, today, ctx, force, warnings):
    """Rebuild every close from the ledger, write the due daily files, and return
    (close_result, history). History (for the feed) is built on every run, due or not.

    A write failure stops further writes and leaves the close state as it was, so the next run
    retries the same days. The feed still gets the full rebuilt history.
    """
    close_cfg = settings["daily_close"]
    state, state_status = load_close_state(archive_root)
    plan = plan_daily_close(state, ledger["movements"], today, settings, force)
    result = {
        "status": CLOSE_NOT_DUE,
        "failure_class": None,
        "mode": plan["mode"],
        "close_day": plan["close_day"],
        "restate_days": close_cfg["restate_days"],
        "provisional_from": plan["window_start"].isoformat(),
        "state": state_status,
        "days_written": 0,
        "new_days": 0,
        "rebuilt_days": 0,
        "revised_day_count": len(plan["revised"]),
        "revised_days": [],
        "late_change_day_count": len(plan["late"]),
        "late_changes_beyond_window": [],
        "unmapped_location_codes": [],
        "published_close_day": state.get("last_close_day") if state else None,
    }
    result["revised_days"], _ = _day_change_list(plan["revised"])
    result["late_changes_beyond_window"], result["late_changes_truncated"] = _day_change_list(plan["late"])
    if plan["due"] and plan["mode"] == "initial" and state_status != "missing":
        warnings.append("Close state was %s: every day from backfill_start is written again" % state_status)
    elif plan["due"] and plan["mode"] == "initial" and (Path(archive_root) / DAILY_DIR).exists():
        warnings.append("Close state was missing: every day from backfill_start is written again")
    if plan["late"]:
        warnings.append(
            "Late change: %d day(s) older than the %d-day restate window changed in the ledger; their daily "
            "files were left as published (daily_close.late_changes_beyond_window)"
            % (len(plan["late"]), close_cfg["restate_days"])
        )

    roots = [archive_root] + ([delivery_root] if delivery_root else [])
    published = dict(state["published"]) if state else {}
    unmapped = set()
    failed = []

    def on_close(day, close):
        reason = plan["write"].get(day)
        if reason is None or failed:
            return
        try:
            entry, codes = write_daily_day(roots, day, close, ctx, reason, published.get(day.isoformat()))
        except OSError:
            failed.append(day)
            return
        published[day.isoformat()] = entry
        unmapped.update(codes)
        result["days_written"] += 1
        if reason == "new":
            result["new_days"] += 1
        elif reason == "rebuild":
            result["rebuilt_days"] += 1

    history = rebuild_history(ledger["movements"], settings, today - timedelta(days=1), on_close if plan["write"] else None)
    result["unmapped_location_codes"] = sorted(unmapped)
    if not plan["due"]:
        return result, history
    if failed:
        result["status"] = CLOSE_FAILED
        result["failure_class"] = FAILURE_CLOSE_WRITE
        return result, history
    for day in plan["late"]:
        published[day.isoformat()] = dict(published[day.isoformat()], late_change_flagged=True)
    new_state = {
        "last_close_day": plan["close_day"],
        "published": published,
        "movements": ledger["movements"],
        "last_close_run": {
            "run_id": ctx["run_id"],
            "close_day": plan["close_day"],
            "mode": plan["mode"],
            "days_written": result["days_written"],
            "revised_day_count": result["revised_day_count"],
            "late_change_day_count": result["late_change_day_count"],
        },
    }
    try:
        replace_atomic(close_state_path(archive_root), close_state_bytes(new_state), ctx["tag"])
    except OSError:
        result["status"] = CLOSE_FAILED
        result["failure_class"] = FAILURE_CLOSE_WRITE
        return result, history
    result["status"] = CLOSE_PUBLISHED
    result["published_close_day"] = plan["close_day"]
    return result, history


# ---------------------------------------------------------------------------------------------
# Dashboard feed (pure)
# ---------------------------------------------------------------------------------------------


def _num(value):
    """A quantity as a JSON number: whole numbers as int, others as float."""
    value = tidy_qty(value)
    return int(value) if value == value.to_integral_value() else float(value)


def next_run_local(started, run_minute):
    nxt = started.replace(minute=run_minute, second=0, microsecond=0)
    while nxt <= started:
        nxt += timedelta(hours=1)
    return nxt.isoformat()


def build_feed(settings, manifest, rows, history, ledger, snapshot, close_result, started):
    """The compact dashboard feed (schema xb.ac2.stock_dashboard_feed, version 1). Pure.

    Quantities only: no cost, no sales value, no customer data. Item-level lists are capped at
    feed.list_cap; every count is complete.
    """
    feed_cfg = settings["feed"]
    labels = settings["location_labels"]
    cap = feed_cfg["list_cap"]
    top = feed_cfg["movers_top"]
    threshold = Decimal(feed_cfg["low_stock_threshold"])
    items = ledger["items"]
    location_names = {_clean(r.get("Location")): r.get("LocationDescription") for r in snapshot["location_rows"]}
    reorder = {
        (_clean(r.get("ItemCode")), _clean(r.get("UOM"))): to_decimal(r.get("ReOLevel")) for r in snapshot["reorder_rows"]
    }
    negative_since = history["negative_since"]

    def item_entry(item_code, uom, loc, **extra):
        entry = {
            "item": item_code,
            "description": _clean(items.get(item_code, {}).get("ItemDescription")),
            "location": loc,
            "uom": uom,
        }
        entry.update(extra)
        return entry

    latest = {}
    negatives = []
    low = []
    for r in rows:
        if r["RowSource"] != ROW_SOURCE_BALANCE:
            continue
        loc, qty = r["Location"], r["BalQty"]
        entry = latest.setdefault(loc, {"units": Decimal(0), "skus_in_stock": 0, "zero": 0, "negative": 0, "low_stock": 0})
        entry["units"] += qty
        if qty > 0:
            entry["skus_in_stock"] += 1
        elif qty == 0:
            entry["zero"] += 1
        else:
            entry["negative"] += 1
            negatives.append(r)
        if 0 < qty <= threshold and r["IsActive"] == "T":
            entry["low_stock"] += 1
            low.append(r)
    negatives.sort(key=lambda r: (r["BalQty"], r["ItemCode"], r["Location"]))
    low.sort(key=lambda r: (r["BalQty"], r["ItemCode"], r["Location"]))

    def since(r):
        day = negative_since.get((r["ItemCode"], r["UOM"], r["Location"]))
        return day.isoformat() if day else None

    def reorder_level(r):
        level = reorder.get((r["ItemCode"], r["UOM"]))
        return _num(level) if level is not None else None

    last = history["last_day"]
    snapshots = history["snapshots"]
    current = snapshots.get(last, {})
    movers = {}
    for name, days_back in (("1d", 1), ("7d", 7), ("30d", 30)):
        earlier_day = last - timedelta(days=days_back)
        earlier = snapshots.get(earlier_day, {})
        changes = []
        for key in set(current) | set(earlier):
            delta = current.get(key, Decimal(0)) - earlier.get(key, Decimal(0))
            if delta != 0:
                changes.append((delta, key))
        up = sorted((c for c in changes if c[0] > 0), key=lambda c: (-c[0], c[1]))[:top]
        down = sorted((c for c in changes if c[0] < 0), key=lambda c: (c[0], c[1]))[:top]
        movers[name] = {
            "from": earlier_day.isoformat(),
            "to": last.isoformat(),
            "changed_keys": len(changes),
            "up": [item_entry(*key, change=_num(d), close=_num(current.get(key, Decimal(0)))) for d, key in up],
            "down": [item_entry(*key, change=_num(d), close=_num(current.get(key, Decimal(0)))) for d, key in down],
        }

    codes = set(latest) | set(history["per_location"]) | set(labels)
    locations = []
    for code in sorted(codes):
        label, mapped = location_label(code, location_names.get(code), labels)
        locations.append({"code": code, "label": label, "mapped": mapped})
    large = manifest.get("large_balances") or {}
    ledger_counts = ledger["counts"]
    return {
        "schema": FEED_SCHEMA,
        "schema_version": FEED_SCHEMA_VERSION,
        "job": JOB,
        "run_id": manifest["run_id"],
        "as_of_local": manifest["as_of_local"],
        "timezone": settings["timezone"],
        "cadence_minutes": 60,
        "next_run_local": next_run_local(started, settings["schedule"]["run_minute"]),
        "stale_after_minutes": settings["stale_after_minutes"],
        "backfill_start": settings["daily_close"]["backfill_start"],
        "last_close_day": last.isoformat(),
        "published_close_day": close_result["published_close_day"],
        "provisional_from": close_result["provisional_from"],
        "quantity_rule": "Units only; non-stock-controlled items excluded",
        "locations": locations,
        "latest": {
            loc: {k: (_num(v) if k == "units" else v) for k, v in entry.items()} for loc, entry in sorted(latest.items())
        },
        "history": {
            "days": [day.isoformat() for day in history["days"]],
            "per_location": {
                loc: {
                    "units": [_num(v) for v in series["units"]],
                    "skus_in_stock": series["skus_in_stock"],
                    "negative": series["negative"],
                }
                for loc, series in history["per_location"].items()
            },
        },
        "movers": movers,
        "negatives": {
            "count": len(negatives),
            "rows": [
                item_entry(r["ItemCode"], r["UOM"], r["Location"], qty=_num(r["BalQty"]), negative_since=since(r))
                for r in negatives[:cap]
            ],
            "truncated": len(negatives) > cap,
        },
        "low_stock": {
            "rule": "0 < quantity <= threshold, active items",
            "threshold": feed_cfg["low_stock_threshold"],
            "count": len(low),
            "items_with_reorder_level": len(reorder),
            "rows": [
                item_entry(r["ItemCode"], r["UOM"], r["Location"], qty=_num(r["BalQty"]), reorder_level=reorder_level(r))
                for r in low[:cap]
            ],
            "truncated": len(low) > cap,
        },
        "anomalies": {
            "ledger_check": (manifest.get("ledger_check") or {}).get("result"),
            "large_balance_count": large.get("count"),
            "negative_rows": len(negatives),
            "future_dated_ledger_rows": ledger_counts["future_dated_rows"],
            "unmapped_location_count": sum(1 for loc in locations if not loc["mapped"]),
            "non_stock_items_excluded": ledger_counts["non_stock_items_excluded"],
            "daily_close_status": close_result["status"],
            "revised_day_count": close_result["revised_day_count"],
            "late_change_day_count": close_result["late_change_day_count"],
        },
        "pos_sync": manifest.get("pos_sync", []),
    }


# ---------------------------------------------------------------------------------------------
# Retention (local archive only)
# ---------------------------------------------------------------------------------------------


def _is_link(path):
    """True for a symlink or any reparse point (junction), or when it cannot be inspected."""
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return True


def _children(folder):
    try:
        return sorted(Path(folder).iterdir())
    except OSError:
        return []


def _rmdir_if_empty(folder):
    try:
        os.rmdir(folder)
    except OSError:
        pass


def prune_archive(archive_root, settings, now, today, keep_run_dir):
    """Delete the exporter's own expired files from the local archive. Never raises.

    Only hourly run folders (archive_root/hourly/<YYYY-MM-DD>/ac2_stockbal_...) older than
    retention.hourly_keep_hours and daily files (archive_root/daily/<YYYY-MM>/stock_close_...)
    older than retention.daily_keep_days. Inside them only the exporter's own file names are
    removed; anything else (other names, links, junctions) is kept and counted. The close state,
    the latest pair, the feed and the Drive sync folder are never touched.
    """
    retention = settings["retention"]
    result = {
        "prune_enabled": retention["prune_enabled"],
        "hourly_folders_removed": 0,
        "daily_files_removed": 0,
        "kept_unexpected": 0,
        "errors": 0,
    }
    if not retention["prune_enabled"]:
        return result
    root = Path(archive_root)
    hourly_cutoff = now - timedelta(hours=retention["hourly_keep_hours"])
    daily_cutoff = today - timedelta(days=retention["daily_keep_days"])

    def remove(path):
        if _is_link(path) or not path.is_file() or not _inside(path, root):
            result["kept_unexpected"] += 1
            return False
        try:
            os.remove(path)
        except OSError:
            result["errors"] += 1
            return False
        return True

    hourly_root = root / HOURLY_DIR
    if hourly_root.is_dir() and not _is_link(hourly_root):
        for date_dir in _children(hourly_root):
            if not HOURLY_DATE_DIR_RE.match(date_dir.name) or _is_link(date_dir) or not date_dir.is_dir():
                result["kept_unexpected"] += 1
                continue
            for run_dir in _children(date_dir):
                match = RUN_FOLDER_RE.match(run_dir.name)
                if not match or _is_link(run_dir) or not run_dir.is_dir():
                    result["kept_unexpected"] += 1
                    continue
                try:
                    run_started = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%z")
                except ValueError:
                    result["kept_unexpected"] += 1
                    continue
                if run_started >= hourly_cutoff or (keep_run_dir is not None and _same_path(run_dir, keep_run_dir)):
                    continue
                for child in _children(run_dir):
                    if child.name in (CSV_NAME, MANIFEST_NAME):
                        remove(child)
                    else:
                        result["kept_unexpected"] += 1
                if not _children(run_dir):
                    _rmdir_if_empty(run_dir)
                    result["hourly_folders_removed"] += 1
            _rmdir_if_empty(date_dir)

    daily_root = root / DAILY_DIR
    if daily_root.is_dir() and not _is_link(daily_root):
        for month_dir in _children(daily_root):
            if month_dir.name == CLOSE_STATE_NAME:
                continue
            if not DAILY_MONTH_DIR_RE.match(month_dir.name) or _is_link(month_dir) or not month_dir.is_dir():
                result["kept_unexpected"] += 1
                continue
            for child in _children(month_dir):
                match = DAILY_FILE_RE.match(child.name)
                if not match:
                    result["kept_unexpected"] += 1
                    continue
                try:
                    day = date.fromisoformat(match.group(1))
                except ValueError:
                    result["kept_unexpected"] += 1
                    continue
                if day < daily_cutoff and remove(child):
                    result["daily_files_removed"] += 1
            _rmdir_if_empty(month_dir)
    return result


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


def run_hourly(settings, source=None, environ=None, clock=None, sleep=time.sleep, force_close=False):
    """Run one hourly export. Always returns the manifest dict (success or failed).

    force_close (--backfill) writes every daily file from daily_close.backfill_start again.
    """
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
            "ledger_surface": LEDGER_SURFACE,
            "database": settings["source"]["expected_database"],
            "query_sha256": query_sha256(BALANCE_QUERY),
            "queries_sha256": {
                "balance": query_sha256(BALANCE_QUERY),
                "items_without_rows": query_sha256(ITEMS_WITHOUT_ROWS_QUERY),
                "multi_uom": query_sha256(MULTI_UOM_QUERY),
                "ledger": query_sha256(LEDGER_QUERY),
                "item_master": query_sha256(ITEM_MASTER_QUERY),
                "locations": query_sha256(LOCATION_QUERY),
                "reorder_levels": query_sha256(REORDER_QUERY),
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
        "ledger": None,
        "ledger_check": None,
        "daily_close": {"status": CLOSE_SKIPPED, "mode": "backfill" if force_close else None},
        "feed": None,
        "retention": None,
        "pos_sync": [],
        # Drive gets the latest pair, the feed, the run status and the daily files only. The
        # final delivery result is delivery_status in last_run_status.json.
        "delivery": {
            "mode": "latest_only",
            "status": DELIVERY_SKIPPED if delivery_root else DELIVERY_NOT_CONFIGURED,
            "files": [],
        },
        "warnings": [],
    }

    lock_path = None
    publish_failure = None
    work = None
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
        today = datetime.fromisoformat(as_of_local).date()

        ledger = build_ledger(snapshot, settings, today)
        manifest["ledger"] = ledger["counts"]
        uom = check_uom_guard(snapshot["balances"], snapshot["multi_uom_items"], ledger["counts"]["non_base_uom_groups"])
        manifest["uom_guard"] = uom
        if uom["result"] != "pass":
            raise RunFailure(FAILURE_UOM, "Non-base UOM found; no CSV until the UOM is mapped")
        ledger_check = check_ledger_identity(snapshot["balances"], ledger["totals"])
        manifest["ledger_check"] = ledger_check
        if ledger_check["result"] != "pass":
            bad = ledger_check["mismatched"] + ledger_check["balance_only_non_zero"] + ledger_check["ledger_only_non_zero"]
            raise RunFailure(
                FAILURE_LEDGER_MISMATCH,
                "The stock ledger does not add up to the stored balance for %d key(s); nothing published" % bad,
            )

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
        work = {"snapshot": snapshot, "ledger": ledger, "rows": rows, "today": today}
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

    # The daily close and the feed come from the same verified read. Their failures are
    # recorded separately: the hourly snapshot above is still good.
    feed_data = None
    if manifest["status"] == "success" and work is not None:
        ctx = {
            "items": work["ledger"]["items"],
            "location_names": {
                _clean(r.get("Location")): r.get("LocationDescription") for r in work["snapshot"]["location_rows"]
            },
            "labels": settings["location_labels"],
            "run_id": run_id,
            "tag": run_id[:8],
            "written_at": started.isoformat(),
            "as_of_local": manifest["as_of_local"],
            "restate_days": settings["daily_close"]["restate_days"],
        }
        history = None
        try:
            close_result, history = run_daily_close(
                settings, archive_root, delivery_root, work["ledger"], work["snapshot"], work["today"], ctx,
                force_close, manifest["warnings"],
            )
        except Exception:  # noqa: BLE001 - a close bug must not hide the good hourly snapshot.
            close_result = {"status": CLOSE_FAILED, "failure_class": FAILURE_INTERNAL, "mode": None}
        manifest["daily_close"] = close_result
        try:
            if history is None:
                raise RuntimeError("no history")
            feed = build_feed(
                settings, manifest, work["rows"], history, work["ledger"], work["snapshot"], close_result, started
            )
            feed_data = gzip_json_bytes(feed)
            manifest["feed"] = {
                "status": "built",
                "name": FEED_NAME,
                "schema": FEED_SCHEMA,
                "schema_version": FEED_SCHEMA_VERSION,
                "sha256": hashlib.sha256(feed_data).hexdigest(),
                "byte_size": len(feed_data),
            }
        except Exception:  # noqa: BLE001 - the feed is derived data; its failure is reported, not raised.
            manifest["feed"] = {"status": "failed", "failure_class": FAILURE_FEED}
        # Never raises; only expired exporter-owned files, never this run's folder.
        manifest["retention"] = prune_archive(archive_root, settings, started, work["today"], run_dir)

    manifest["finished_at"] = _now(tz, clock).isoformat()
    if manifest["status"] != "success":
        manifest["csv"] = None
        manifest["row_count"] = 0
        manifest["large_balances"] = None
        # A failed run lists no large balances, so it carries no large-balance warning either.
        manifest["warnings"] = [w for w in manifest["warnings"] if not w.startswith(LARGE_BALANCE_WARNING_PREFIX)]
    elif delivery_root:
        manifest["delivery"]["status"] = DELIVERY_PENDING
        manifest["delivery"]["files"] = [LATEST_CSV_NAME, LATEST_MANIFEST_NAME] + ([FEED_NAME] if feed_data else [])
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest_sha, _ = write_exclusive(run_dir / MANIFEST_NAME, json_bytes(manifest))
    except OSError:
        manifest_sha = None
        if manifest["status"] == "success":
            manifest["status"] = "failed"
            manifest["failure_class"] = FAILURE_WRITE
            manifest["message"] = "Could not write the manifest (run %s)" % run_id[:8]

    final_delivery = manifest["delivery"]["status"]
    if manifest["status"] == "success":
        try:
            publish_latest(archive_root, run_dir, manifest, run_id)
        except (OSError, RunFailure):
            publish_failure = FAILURE_LATEST
        if feed_data is not None:
            try:
                publish_verified(archive_root / FEED_NAME, feed_data, run_id[:8])
            except OSError:
                manifest["feed"]["status"] = "failed"
                manifest["feed"]["failure_class"] = FAILURE_FEED
        if delivery_root:
            final_delivery = deliver_latest(run_dir, delivery_root, manifest, manifest_sha, feed_data, run_id)
        if manifest["feed"] and manifest["feed"]["status"] == "built":
            manifest["feed"]["status"] = "published"
    elif manifest["status"] != "success" and delivery_root and manifest["delivery"]["status"] == DELIVERY_PENDING:
        final_delivery = DELIVERY_SKIPPED

    close = manifest["daily_close"]
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
        "daily_close_status": close.get("status"),
        "daily_close_failure_class": close.get("failure_class"),
        "published_close_day": close.get("published_close_day"),
        "revised_day_count": close.get("revised_day_count"),
        "late_change_day_count": close.get("late_change_day_count"),
        "feed_status": (manifest["feed"] or {}).get("status"),
        "next_run_local": next_run_local(started, settings["schedule"]["run_minute"]),
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


def deliver_latest(run_dir, delivery_root, manifest, manifest_sha, feed_data, run_id):
    """Replace the Drive latest pair and the feed, each only after its copy verifies.

    No hourly folder goes to Drive. The CSV goes first: if it does not verify, the latest
    manifest is not replaced either, so Drive keeps the previous verified pair.
    """
    tag = run_id[:8]
    try:
        data = Path(run_dir, CSV_NAME).read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest["csv"]["sha256"]:
            return DELIVERY_VERIFY_FAILED
        if manifest_sha is None or file_sha256(Path(run_dir, MANIFEST_NAME)) != manifest_sha:
            return DELIVERY_VERIFY_FAILED
        latest = {key: value for key, value in manifest.items() if not key.startswith("_")}
        publish_verified(delivery_root / LATEST_CSV_NAME, data, tag)
        publish_verified(delivery_root / LATEST_MANIFEST_NAME, json_bytes(latest), tag)
        if feed_data is not None:
            publish_verified(delivery_root / FEED_NAME, feed_data, tag)
    except VerifyError:
        return DELIVERY_VERIFY_FAILED
    except OSError:
        return DELIVERY_COPY_FAILED
    return DELIVERY_HANDED


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
    parser = argparse.ArgumentParser(description="Hourly read-only AC2 stock balance export with a daily close (#245).")
    parser.add_argument("--config", required=True, help="Local settings JSON (config/*.local.json, never committed).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-latest", action="store_true", help="Read-only stale check of the latest pair.")
    mode.add_argument("--dry-run-config", action="store_true", help="Validate the settings only.")
    mode.add_argument(
        "--backfill",
        action="store_true",
        help="A normal run that also writes every daily close from daily_close.backfill_start again.",
    )
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

    manifest = run_hourly(settings, source=source, environ=environ, clock=clock, force_close=args.backfill)
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
            "daily_close_status": status["daily_close_status"],
            "published_close_day": status["published_close_day"],
            "feed_status": status["feed_status"],
            "message": manifest["message"],
        }
    )
    if status["status"] != "success":
        return EXIT_FAILED
    if status["delivery_status"] not in (DELIVERY_HANDED, DELIVERY_NOT_CONFIGURED):
        return EXIT_FAILED
    if status["daily_close_status"] == CLOSE_FAILED or status["feed_status"] != "published":
        return EXIT_FAILED
    return EXIT_OK


def _print(payload):
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
