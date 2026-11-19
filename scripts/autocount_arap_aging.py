"""Hourly AutoCount (AC2) AR/AP aging export, latest snapshot only (#321 slice A1).

Reads open debtor (AR) and creditor (AP) documents, their knock-offs and the debtor/creditor
masters read-only, once per hour, and replaces one "latest" set: four CSVs, a compact dashboard
feed and latest_manifest.json. last_run_status.json is written on every run, failed or not.
There are no hourly folders, no daily files, no history and no retention: each successful run
replaces the set, and a failed run leaves the previous set in place. Delivery to Google Drive is
a plain verified copy into a local folder that Google Drive for desktop syncs (delivery_root,
default null). Nothing here calls a Drive API and nothing in delivery_root is ever deleted.

Safety contract (see docs/autocount2-automation/arap_aging_runbook.md):

* Read-only. Fixed SELECT texts only (no parameters, no comments, no ";"), pinned by SHA-256 in
  the manifest. READ COMMITTED, SET LOCK_TIMEOUT, a query timeout and one connection per attempt.
* The identity is checked on every connection before any business query, with the stock
  exporter's check_identity: "sa" is always refused, sysadmin / db_owner only with
  allow_privileged_identity, and DB_NAME() must equal source.expected_database.
* Every read sits inside one dbo.ChangeCount window over the AR/AP document and master tables.
  When those counter rows are not usable, the business reads run twice and their row counts and
  per-column control sums must match. Otherwise the run retries, then fails unstable_snapshot.
* Outstanding is AutoCount's stored figure. For invoices and debit notes it must equal the
  document amount minus the knock-offs from non-cancelled payments, credit notes and contras,
  or the run fails outstanding_mismatch. Unapplied payments, credit notes and (optionally)
  deposits are their own open rows with negative amounts; nothing is netted.
* Self-checks (row identity, party totals, buckets on both bases, AP schedule, feed against the
  CSVs) fail closed: nothing is published and only counts are recorded.
* Console output, the manifest and last_run_status.json carry counts, hashes and flags only:
  never a party name or code, a document number or an amount.
* Error alerts (#321 A3) are off by default. When on, a failed run, a failed Drive delivery or the
  first sight of AP payments POSTs at most one sanitized xb.ac2.arap_alert.v1 event (closed codes
  only) to an n8n webhook, which emails it through the shared XB Ops Alert Email sub-workflow. A
  still-open problem repeats at most once a day. The outcome is recorded as alert_status and
  alert_http_status in last_run_status.json and never changes the run's result or exit code.

pyodbc is imported only inside SqlServerSource so the tests run without it.
"""

import argparse
import copy
import csv
import hashlib
import http.client
import io
import json
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Reuse, unchanged: settings helpers, identity check, SQL error classes, verified publish and the
# lock helpers. A shared module for both exporters is POST_SHIP.
import autocount_stock_balance_hourly as stock  # noqa: E402
from autocount_stock_extract import sanitize_error  # noqa: E402
from csv_safety import safe_csv_row  # noqa: E402

ConfigError = stock.ConfigError
TimezoneUnavailableError = stock.TimezoneUnavailableError
RunFailure = stock.RunFailure
SourceLockTimeout = stock.SourceLockTimeout
SourceReadError = stock.SourceReadError
CredentialUnavailableError = stock.CredentialUnavailableError
VerifyError = stock.VerifyError

JOB = "ac2_arap_aging"
SCHEMA_VERSION = "1"
FEED_SCHEMA = "xb.ac2.arap_dashboard_feed"
FEED_SCHEMA_VERSION = 1
LOCAL_CURRENCY = "SGD"
CADENCE_MINUTES = 60

AR_ITEMS_NAME = "ar_open_items_latest.csv"
AP_ITEMS_NAME = "ap_open_items_latest.csv"
AR_PARTIES_NAME = "ar_parties_latest.csv"
AP_PARTIES_NAME = "ap_parties_latest.csv"
FEED_NAME = "arap_dashboard_feed.json.gz"
LATEST_MANIFEST_NAME = "latest_manifest.json"
LAST_RUN_STATUS_NAME = "last_run_status.json"
LOCK_NAME = ".ac2_arap.lock"
# The published set, in move order: data first, the manifest that lists their hashes last.
PUBLISHED_FILES = (AR_ITEMS_NAME, AP_ITEMS_NAME, AR_PARTIES_NAME, AP_PARTIES_NAME, FEED_NAME, LATEST_MANIFEST_NAME)

EXIT_OK = stock.EXIT_OK
EXIT_FAILED = stock.EXIT_FAILED
EXIT_STALE = stock.EXIT_STALE
EXIT_MISSING = stock.EXIT_MISSING

FAILURE_CONFIG = stock.FAILURE_CONFIG
FAILURE_ALREADY_RUNNING = stock.FAILURE_ALREADY_RUNNING
FAILURE_STALE_LOCK = stock.FAILURE_STALE_LOCK
FAILURE_CREDENTIAL = stock.FAILURE_CREDENTIAL
FAILURE_LOCK_TIMEOUT = stock.FAILURE_LOCK_TIMEOUT
FAILURE_UNSTABLE = stock.FAILURE_UNSTABLE
FAILURE_SOURCE = stock.FAILURE_SOURCE
FAILURE_LATEST = stock.FAILURE_LATEST
FAILURE_INTERNAL = stock.FAILURE_INTERNAL
FAILURE_OUTSTANDING = "outstanding_mismatch"
FAILURE_UNKNOWN_KNOCKOFF = "unknown_knockoff_target"
FAILURE_SELF_CHECK = "self_check_failed"

DELIVERY_NOT_CONFIGURED = stock.DELIVERY_NOT_CONFIGURED
DELIVERY_HANDED = stock.DELIVERY_HANDED
DELIVERY_VERIFY_FAILED = stock.DELIVERY_VERIFY_FAILED
DELIVERY_COPY_FAILED = stock.DELIVERY_COPY_FAILED
DELIVERY_SKIPPED = stock.DELIVERY_SKIPPED

# Error alerts (#321 A3): email through the n8n webhook "AC2 ARAP Export - Error Alert" and the
# shared "XB Ops Alert Email" sub-workflow (system AC2-ARAP). No heartbeat, no success message.
# The secret, settings checks, de-dup rule and the POST itself are the stock exporter's helpers.
ALERT_STATE_NAME = "alert_state.json"
ALERT_STATE_SCHEMA = "xb.ac2.arap_alert_state.v1"
ALERT_SCHEMA = "xb.ac2.arap_alert.v1"
ALERT_EVENT = "ac2_arap_alert"
ALERT_USER_AGENT = "xb-ac2-arap-alert/1"
# The only keys a payload may carry, in this order. No party, document, amount, path or message.
ALERT_PAYLOAD_KEYS = (
    "schema",
    "event",
    "run_id",
    "run_hour_local",
    "alert_kind",
    "status",
    "failure_class",
    "conditions",
    "delivery_status",
    "ap_payments_recorded",
)
ALERT_CONDITIONS = ("run_failed", "delivery_failed", "ap_payments_recorded")
# Notices are sent once when they appear and never repeated daily: there is nothing to retry,
# only a one-time owner action (runbook "ap_payments_recorded").
ALERT_NOTICE_CONDITIONS = ("ap_payments_recorded",)
ALERT_KINDS = stock.ALERT_KINDS
# Every failure class run_once and its settings path can record (runbook "Failure classes").
ALERT_FAILURE_CLASSES = (
    FAILURE_CONFIG,
    stock.FAILURE_TIMEZONE,
    FAILURE_ALREADY_RUNNING,
    FAILURE_STALE_LOCK,
    stock.FAILURE_IDENTITY,
    FAILURE_CREDENTIAL,
    FAILURE_LOCK_TIMEOUT,
    FAILURE_UNSTABLE,
    FAILURE_SOURCE,
    FAILURE_OUTSTANDING,
    FAILURE_UNKNOWN_KNOCKOFF,
    FAILURE_SELF_CHECK,
    FAILURE_LATEST,
    FAILURE_INTERNAL,
)
ALERT_DELIVERY_STATUSES = stock.ALERT_DELIVERY_STATUSES
ALERT_DELIVERY_FAILURES = (DELIVERY_VERIFY_FAILED, DELIVERY_COPY_FAILED)
# alert_status in last_run_status.json (same values and meanings as the stock export).
ALERT_DISABLED = stock.ALERT_DISABLED
ALERT_NONE = stock.ALERT_NONE
ALERT_SUPPRESSED = stock.ALERT_SUPPRESSED
ALERT_SENT = stock.ALERT_SENT
ALERT_SEND_FAILED = stock.ALERT_SEND_FAILED
ALERT_SECRET_UNAVAILABLE = stock.ALERT_SECRET_UNAVAILABLE
ALERT_ERROR = stock.ALERT_ERROR
ALERT_SKIPPED_CONFIG = stock.ALERT_SKIPPED_CONFIG

AUTH_MODES = stock.AUTH_MODES
LOGON_TYPES = stock.LOGON_TYPES
SUPPORTED_LOGON_PAIRS = stock.SUPPORTED_LOGON_PAIRS
SCHEDULE_FREQUENCIES = ("hourly",)
ISO_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
TYPE_CODE_RE = re.compile(r"\A[A-Z]{1,4}\Z")
PARTY_CODE_MAX = 24

SIDES = ("ar", "ap")
DEBIT_TYPES = {"ar": ("IV", "DN"), "ap": ("PI", "SD")}
UNAPPLIED_BUCKET = "unapplied"
AGING_BASES = ("due_date", "document_date")

# ---------------------------------------------------------------------------------------------
# SQL. Fixed texts built once from constants: no parameters, no comments, no ";", SELECT only.
# Column roles come from the VM read-only check (#321). Money is decimal(19,2), flags are T/F.
# ---------------------------------------------------------------------------------------------

IDENTITY_QUERY = stock.IDENTITY_QUERY
SQL_TIME_QUERY = stock.SQL_TIME_QUERY
SESSION_SETUP = ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", "SET LOCK_TIMEOUT %d")

# Tables whose dbo.ChangeCount rows bound the read window. Knock-off tables have no counter row;
# a knock-off changes its paying document (payment, credit note, contra), which has one.
CHANGE_COUNT_TABLES = (
    "ARInvoice", "ARDN", "ARCN", "ARPayment", "ARDeposit", "ARRefund", "ARAPContra",
    "APInvoice", "APDN", "APCN", "APPayment", "APDeposit", "APRefund",
    "Debtor", "Creditor",
)
CHANGE_COUNT_QUERY = (
    "SELECT c.TableName, c.Counter FROM dbo.ChangeCount AS c WHERE c.TableName IN ("
    + ", ".join("N'%s'" % name for name in CHANGE_COUNT_TABLES)
    + ")"
)


def _party_query(table, type_column, agent_column):
    return (
        "SELECT p.AccNo AS PartyCode, p.CompanyName AS PartyName, p.%s AS PartyType, p.AreaCode, "
        "p.%s AS Agent, p.DisplayTerm, p.CurrencyCode, p.CreditLimit, p.IsActive, p.AgingOn "
        "FROM dbo.%s AS p" % (type_column, agent_column, table)
    )


def _debit_query(table, party_column, agent_column):
    # Invoices and debit notes: stored Outstanding, PaymentAmt = knocked off to date.
    return (
        "SELECT x.DocKey, x.DocNo, x.%s AS PartyCode, x.DocDate, x.DueDate, x.DisplayTerm, x.CurrencyCode, "
        "x.CurrencyRate, x.NetTotal AS OriginalAmt, x.LocalNetTotal AS LocalOriginalAmt, "
        "x.PaymentAmt AS KnockedOffAmt, x.LocalPaymentAmt AS LocalKnockedOffAmt, x.Outstanding, x.Cancelled, "
        "x.%s AS Agent, x.Description, x.RefNo2 AS RefText FROM dbo.%s AS x" % (party_column, agent_column, table)
    )


def _credit_note_query(table, party_column):
    # Unapplied part = NetTotal - KnockOffAmt - RefundAmt (VM check: formula to prove at V3).
    return (
        "SELECT x.DocKey, x.DocNo, x.%s AS PartyCode, x.DocDate, x.CurrencyCode, x.CurrencyRate, "
        "x.NetTotal AS OriginalAmt, x.LocalNetTotal AS LocalOriginalAmt, x.KnockOffAmt, x.RefundAmt, x.Cancelled, "
        "x.Description, x.Ref AS RefText FROM dbo.%s AS x" % (party_column, table)
    )


def _payment_query(table, party_column):
    # Unapplied part = PaymentAmt - KnockOffAmt - RefundAmt; LocalUnappliedAmount is stored.
    # ToHomeRate and HandOverDate are VM-check guesses: used only when their setting is on.
    return (
        "SELECT x.DocKey, x.DocNo, x.%s AS PartyCode, x.DocDate, x.CurrencyCode, x.ToHomeRate AS CurrencyRate, "
        "x.PaymentAmt AS OriginalAmt, x.LocalPaymentAmt AS LocalOriginalAmt, x.KnockOffAmt, x.RefundAmt, "
        "x.LocalUnappliedAmount, x.HandOverDate, x.Cancelled, x.Description, x.DocNo2 AS RefText "
        "FROM dbo.%s AS x" % (party_column, table)
    )


def _deposit_query(table, party_column):
    # Every deposit column is a VM-check guess: exported only with guess_sources.*_deposits on.
    return (
        "SELECT x.DocKey, x.DocNo, x.%s AS PartyCode, x.DocDate, x.CurrencyCode, x.ToHomeRate AS CurrencyRate, "
        "x.PaymentAmt AS OriginalAmt, x.TransferedAmt, x.Outstanding, x.Cancelled, x.Description "
        "FROM dbo.%s AS x" % (party_column, table)
    )


def _knockoff_query(prefix, payment_table, cn_table):
    # One row per knock-off line from a non-cancelled paying document. PayerDate is the paying
    # document's date (VM check: GainLossDate equals it on every row; there is no KO date column).
    return (
        "SELECT N'PM' AS PayerType, k.KnockOffDocType, k.KnockOffDocKey, k.Amount, k.DiscountAmt, "
        "p.DocDate AS PayerDate FROM dbo.%(p)sPaymentKnockOff AS k INNER JOIN dbo.%(pay)s AS p "
        "ON p.DocKey = k.DocKey WHERE p.Cancelled = N'F' "
        "UNION ALL SELECT N'CN' AS PayerType, k.KnockOffDocType, k.KnockOffDocKey, k.Amount, "
        "CAST(0 AS decimal(19, 2)) AS DiscountAmt, c.DocDate AS PayerDate FROM dbo.%(p)sCNKnockOff AS k "
        "INNER JOIN dbo.%(cn)s AS c ON c.DocKey = k.DocKey WHERE c.Cancelled = N'F' "
        "UNION ALL SELECT N'CT' AS PayerType, k.KnockOffDocType, k.KnockOffDocKey, k.Amount, "
        "CAST(0 AS decimal(19, 2)) AS DiscountAmt, t.DocDate AS PayerDate FROM dbo.%(p)sContraKnockOff AS k "
        "INNER JOIN dbo.ARAPContra AS t ON t.DocKey = k.DocKey WHERE t.Cancelled = N'F'"
        % {"p": prefix, "pay": payment_table, "cn": cn_table}
    )


# key -> (side, document type, kind, guess setting or None). Kinds: debit, payment, credit_note,
# deposit. Document type codes are the VM check's: AR IV, DN, CN, PM, DP; AP PI, SD, SC, PV, DP.
DOC_SOURCES = {
    "ar_invoice": ("ar", "IV", "debit", None),
    "ar_debit_note": ("ar", "DN", "debit", "ar_debit_notes"),
    "ar_credit_note": ("ar", "CN", "credit_note", None),
    "ar_payment": ("ar", "PM", "payment", None),
    "ar_deposit": ("ar", "DP", "deposit", "ar_deposits"),
    "ap_invoice": ("ap", "PI", "debit", None),
    "ap_debit_note": ("ap", "SD", "debit", "ap_debit_notes"),
    "ap_credit_note": ("ap", "SC", "credit_note", None),
    "ap_payment": ("ap", "PV", "payment", None),
    "ap_deposit": ("ap", "DP", "deposit", "ap_deposits"),
}

QUERIES = {
    "ar_parties": _party_query("Debtor", "DebtorType", "SalesAgent"),
    "ap_parties": _party_query("Creditor", "CreditorType", "PurchaseAgent"),
    "ar_invoice": _debit_query("ARInvoice", "DebtorCode", "SalesAgent"),
    "ar_debit_note": _debit_query("ARDN", "DebtorCode", "SalesAgent"),
    "ar_credit_note": _credit_note_query("ARCN", "DebtorCode"),
    "ar_payment": _payment_query("ARPayment", "DebtorCode"),
    "ar_deposit": _deposit_query("ARDeposit", "DebtorCode"),
    "ar_knockoffs": _knockoff_query("AR", "ARPayment", "ARCN"),
    "ap_invoice": _debit_query("APInvoice", "CreditorCode", "PurchaseAgent"),
    "ap_debit_note": _debit_query("APDN", "CreditorCode", "PurchaseAgent"),
    "ap_credit_note": _credit_note_query("APCN", "CreditorCode"),
    "ap_payment": _payment_query("APPayment", "CreditorCode"),
    "ap_deposit": _deposit_query("APDeposit", "CreditorCode"),
    "ap_knockoffs": _knockoff_query("AP", "APPayment", "APCN"),
}
BUSINESS_QUERY_KEYS = tuple(QUERIES)

# Contract: business queries are one SELECT statement each. No write or DDL keyword, no INTO,
# EXEC, statement separator or comment. The tests apply this to every query text.
FORBIDDEN_SQL_RE = re.compile(
    # Text, trigger, sequence, Service Broker, server and transaction statements, and lock hints.
    r"\b(WRITETEXT|UPDATETEXT|ENABLE\s+TRIGGER|DISABLE\s+TRIGGER|NEXT\s+VALUE\s+FOR|RECEIVE|SEND|"
    r"END\s+CONVERSATION|BEGIN\s+DIALOG|RECONFIGURE|CHECKPOINT|SETUSER|BEGIN\s+TRAN(SACTION)?|COMMIT|ROLLBACK|"
    r"SAVE\s+TRAN(SACTION)?|XLOCK|TABLOCKX?|UPDLOCK|HOLDLOCK|PAGLOCK|ROWLOCK|NOLOCK|READPAST|READUNCOMMITTED)\b|"
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|DENY|EXEC|EXECUTE|INTO|BACKUP|"
    r"RESTORE|DBCC|SHUTDOWN|KILL|OPENROWSET|OPENQUERY|OPENDATASOURCE|WAITFOR|DECLARE|SET|USE)\b|;|--|/\*",
    re.IGNORECASE,
)


def all_query_texts():
    """name -> text of every query sent to the server (setup statements excluded)."""
    texts = {"identity": IDENTITY_QUERY, "sql_time": SQL_TIME_QUERY, "change_count": CHANGE_COUNT_QUERY}
    texts.update(QUERIES)
    return texts


def queries_sha256():
    return {name: stock.query_sha256(text) for name, text in sorted(all_query_texts().items())}


def assert_read_only(text):
    """Raise ConfigError when a query text breaks the read-only contract (checked before use)."""
    if not text.lstrip().upper().startswith("SELECT") or FORBIDDEN_SQL_RE.search(text):
        raise ConfigError("query text is not a single read-only SELECT")


for _name, _text in all_query_texts().items():
    assert_read_only(_text)


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "timezone": "Asia/Singapore",
    "schedule": {"frequency": "hourly", "run_minute": 30, "logon_type": "S4U"},
    "stale_after_minutes": 70,
    "source": {
        "server": None,
        "odbc_driver": "ODBC Driver 17 for SQL Server",
        "expected_database": None,
        "allowed_databases": None,
        "encrypt": False,
        "trust_server_certificate": False,
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
    "output": {"archive_root": None, "delivery_root": None},
    "aging": {"boundaries": [0, 30, 60, 90, 120]},
    # Debtor codes of the online-shop customer(s). Real codes live only in the VM's local settings.
    "parties": {"online_shop_debtor_codes": []},
    # KnockOffDocType code -> document type it points at. "RI" -> AR invoice is VM-proven. Any
    # other code seen on a knock-off fails the run until it is mapped here (runbook).
    "knockoff_target_types": {"ar": {"RI": "IV"}, "ap": {}},
    # Sources and columns the VM check could only guess. Off by default: an open row of a source
    # that is off is counted and warned about, never exported.
    "guess_sources": {
        "ar_debit_notes": False,
        "ap_debit_notes": False,
        "ar_deposits": False,
        "ap_deposits": False,
        "payment_home_rate": False,
        "payment_handover_date": False,
    },
    # Date of the last passed menu reconciliation (runbook V3); null until it passes.
    "reconciled_on": None,
    # Error alerts (A3). Off by default. Same keys and checks as the stock export's alert block:
    # the shared secret is never a setting, only the name of the variable or target holding it.
    "alert": copy.deepcopy(stock.DEFAULT_SETTINGS["alert"]),
}
CLOSED_SECTIONS = (
    "schedule", "source", "identity", "read", "output", "aging", "parties", "knockoff_target_types", "guess_sources",
    "alert",
)

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

    output = settings["output"]
    _require_str(output, "archive_root", "output.")
    if output["delivery_root"] is not None:
        _require_str(output, "delivery_root", "output.")
        if stock._same_path(output["delivery_root"], output["archive_root"]):
            raise ConfigError("output.delivery_root must differ from output.archive_root")
        if stock._nested_path(output["delivery_root"], output["archive_root"]):
            raise ConfigError("output.delivery_root and output.archive_root must not be inside each other")

    boundaries = settings["aging"]["boundaries"]
    if (
        not isinstance(boundaries, list)
        or not 1 <= len(boundaries) <= 12
        or any(isinstance(b, bool) or not isinstance(b, int) for b in boundaries)
        or boundaries[0] != 0
        or any(b <= a for a, b in zip(boundaries, boundaries[1:]))
        or boundaries[-1] > 3650
    ):
        raise ConfigError("aging.boundaries must be 1 to 12 increasing whole days from 0 to 3650, starting at 0")

    codes = settings["parties"]["online_shop_debtor_codes"]
    if not isinstance(codes, list) or not all(
        isinstance(c, str) and c.strip() and len(c) <= PARTY_CODE_MAX for c in codes
    ):
        raise ConfigError("parties.online_shop_debtor_codes must be a list of debtor codes")

    for side in SIDES:
        mapping = settings["knockoff_target_types"].get(side)
        if not isinstance(mapping, dict) or not all(
            isinstance(code, str) and TYPE_CODE_RE.match(code) and target in DEBIT_TYPES[side]
            for code, target in mapping.items()
        ):
            raise ConfigError(
                "knockoff_target_types.%s must map knock-off type codes to %s" % (side, " or ".join(DEBIT_TYPES[side]))
            )

    for key, value in settings["guess_sources"].items():
        _require_bool(value, "guess_sources.%s" % key)

    reconciled = settings["reconciled_on"]
    if reconciled is not None and not _iso_date_ok(reconciled):
        raise ConfigError("reconciled_on must be null or a date as YYYY-MM-DD")

    # The stock export's check, unchanged: https URL without credentials, query or fragment; a
    # plain header name; exactly one secret source when enabled; Credential Manager only with
    # the Password logon (an S4U task cannot read it).
    stock._validate_alert_settings(settings["alert"], schedule["logon_type"])
    return settings


def _iso_date_ok(value):
    if not isinstance(value, str) or not ISO_DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def settings_sha256(settings):
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Source (real SQL Server). Not exercised by the offline tests beyond its query texts.
# ---------------------------------------------------------------------------------------------


def build_connection_string(settings, password=None):
    """ODBC connection string. Never logged, printed or written to any file.

    A copy of the stock exporter's builder with this job's APP name (POST_SHIP: shared module).
    """
    source = settings["source"]
    parts = [
        "DRIVER={%s}" % source["odbc_driver"],
        "SERVER=%s" % source["server"],
        "DATABASE=%s" % source["expected_database"],
        "APP=xb_ac2_arap_aging",
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
        read = settings["read"]
        connection.timeout = int(read["query_timeout_seconds"])
        # Two fixed setup statements, sent one at a time. The lock timeout is a validated int.
        self._run(SESSION_SETUP[0])
        self._run(SESSION_SETUP[1] % int(read["lock_timeout_ms"]))

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
        row = self._fetch(IDENTITY_QUERY)[0]
        return {"login": row[0], "user": row[1], "database": row[2], "is_sysadmin": row[3], "is_db_owner": row[4]}

    def read_sql_time(self):
        local, offset_minutes = self._fetch(SQL_TIME_QUERY)[0][:2]
        if not isinstance(local, datetime) or not isinstance(offset_minutes, int):
            raise SourceReadError("SQL read time was not a timestamp")
        return local.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))

    def read_change_counts(self):
        return [(row[0], row[1]) for row in self._fetch(CHANGE_COUNT_QUERY)]

    def read_rows(self, key):
        cursor = self._run(QUERIES[key])
        try:
            columns = [c[0] for c in cursor.description or []]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except self.error_type as exc:
            raise stock._classify_sql_error(exc) from exc

    def _run(self, sql):
        try:
            cursor = self.connection.cursor()
            cursor.execute(sql)
            return cursor
        except self.error_type as exc:
            raise stock._classify_sql_error(exc) from exc

    def _fetch(self, sql):
        cursor = self._run(sql)
        try:
            return cursor.fetchall()
        except self.error_type as exc:
            raise stock._classify_sql_error(exc) from exc


# ---------------------------------------------------------------------------------------------
# Consistent read
# ---------------------------------------------------------------------------------------------


def counters_usable(rows):
    """{table: counter} when every expected ChangeCount row is present exactly once, else None."""
    names = [r[0] for r in rows]
    if len(names) != len(set(names)) or set(names) != set(CHANGE_COUNT_TABLES):
        return None
    if any(isinstance(r[1], bool) or not isinstance(r[1], int) for r in rows):
        return None
    return dict(rows)


def control_sums(data):
    """Per query: the row count and the sum of every numeric column (fallback stability check)."""
    sums = {}
    for key in sorted(data):
        columns = {}
        for row in data[key]:
            for column, value in row.items():
                if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
                    columns[column] = columns.get(column, Decimal(0)) + Decimal(value)
        sums[key] = (len(data[key]), sorted((c, str(v)) for c, v in columns.items()))
    return sums


def read_business(session):
    return {key: session.read_rows(key) for key in BUSINESS_QUERY_KEYS}


def read_snapshot(source, settings, environ=None, sleep=time.sleep):
    """Read every business query inside one stable window. Returns a snapshot dict.

    Primary: dbo.ChangeCount for CHANGE_COUNT_TABLES read before and after on the same
    connection; equal counters mean no AR/AP document or master write landed in between.
    Fallback (a counter row missing or doubled): the business reads run twice and must give the
    same row counts and control sums. A fresh connection is opened per attempt.
    """
    read = settings["read"]
    max_attempts = read["max_attempts"]
    last_failure = None
    identity = {"checked": False}
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
                identity = stock.check_identity(session.read_identity(), settings, environ)
                before = session.read_change_counts()
                sql_now = session.read_sql_time()
                data = read_business(session)
                after = session.read_change_counts()
                before_map, after_map = counters_usable(before), counters_usable(after)
                if before_map is not None and after_map is not None:
                    mode = "change_count"
                    stable = before_map == after_map
                else:
                    mode = "double_read"
                    stable = control_sums(data) == control_sums(read_business(session))
        except SourceLockTimeout:
            last_failure = (FAILURE_LOCK_TIMEOUT, "Lock timeout on every attempt")
            continue
        except SourceReadError as exc:
            raise RunFailure(FAILURE_SOURCE, sanitize_error(str(exc))) from exc
        if stable:
            return {"sql_now": sql_now, "data": data, "attempts": attempt, "identity": identity, "read_mode": mode}
        last_failure = (FAILURE_UNSTABLE, "AR/AP data kept changing during every attempt")
    failure = RunFailure(last_failure[0], last_failure[1])
    failure.attempts = max_attempts
    failure.identity = identity
    raise failure


# ---------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------

CENT = Decimal("0.01")


def money(value):
    """A decimal(19,2) value as Decimal (NULL -> 0). Anything non-numeric fails the run."""
    if value is None:
        return Decimal(0)
    if isinstance(value, bool):
        raise RunFailure(FAILURE_SOURCE, "An amount was not numeric")
    try:
        return Decimal(str(value)) if not isinstance(value, Decimal) else value
    except InvalidOperation as exc:
        raise RunFailure(FAILURE_SOURCE, "An amount was not numeric") from exc


def cents(value):
    return money(value).quantize(CENT, rounding=ROUND_HALF_UP)


def rate(value):
    return None if value is None else money(value)


def day(value):
    """A datetime/date column as a date (AutoCount stores local wall-clock dates)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise RunFailure(FAILURE_SOURCE, "A date column was not a date")


def flag(value):
    """AutoCount d_Boolean: 'T' or 'F'. Anything else fails the run (never guessed)."""
    text = "" if value is None else str(value).strip().upper()
    if text == "T":
        return True
    if text == "F":
        return False
    raise RunFailure(FAILURE_SOURCE, "A T/F flag column held another value")


def text(value):
    return "" if value is None else str(value).strip()


def bucket_labels(boundaries):
    labels = ["current"]
    for low, high in zip(boundaries, boundaries[1:]):
        labels.append("%d-%d" % (low + 1, high))
    labels.append("over_%d" % boundaries[-1])
    return labels


def bucket_index(days, boundaries):
    for index, limit in enumerate(boundaries):
        if days <= limit:
            return index
    return len(boundaries)


def label_slug(label):
    return label.replace("-", "_")


def json_money(value):
    return float(cents(value))


# ---------------------------------------------------------------------------------------------
# Build: open items, parties, totals
# ---------------------------------------------------------------------------------------------


def build_view(data, settings, as_at_date):
    """Open items, parties and totals for both sides from one snapshot. Pure.

    Raises RunFailure(outstanding_mismatch / unknown_knockoff_target / source_error).
    Returns {"ar": side, "ap": side, "counts": ..., "checks": ..., "warnings": [...],
    "ap_payments_recorded": bool}.
    """
    boundaries = settings["aging"]["boundaries"]
    labels = bucket_labels(boundaries)
    guess = settings["guess_sources"]
    online_codes = set(settings["parties"]["online_shop_debtor_codes"])
    warnings = []
    view = {"labels": labels, "warnings": warnings, "checks": {}, "counts": {}}

    for side in SIDES:
        parties = _parties(data["%s_parties" % side], side, online_codes)
        docs = {}  # (doc_type, DocKey) -> raw row with "_cancelled"
        items = []
        counts = {
            "parties": len(parties), "documents_read": 0, "cancelled_skipped": 0, "docs_open": 0,
            "credit_rows": 0, "guess_rows_skipped": 0, "post_dated": 0, "due_before_doc_date": 0,
        }
        for key, (src_side, doc_type, kind, guess_key) in DOC_SOURCES.items():
            if src_side != side:
                continue
            for row in data[key]:
                counts["documents_read"] += 1
                cancelled = flag(row.get("Cancelled"))
                docs[(doc_type, row.get("DocKey"))] = {"row": row, "cancelled": cancelled, "kind": kind,
                                                      "enabled": guess_key is None or guess[guess_key]}
        knockoffs, ko_checks = _knockoffs(data["%s_knockoffs" % side], side, docs, settings)
        if ko_checks["unknown_target_type"]:
            view["checks"][side] = ko_checks
            raise RunFailure(
                FAILURE_UNKNOWN_KNOCKOFF,
                "%d %s knock-off line(s) point at a document type not in knockoff_target_types; nothing published"
                % (ko_checks["unknown_target_type"], side.upper()),
            )
        mismatch = 0
        unapplied_local_disagree = 0
        for (doc_type, _key), doc in sorted(docs.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
            row, kind = doc["row"], doc["kind"]
            if doc["cancelled"]:
                counts["cancelled_skipped"] += 1
                continue
            if kind == "debit" and doc["enabled"]:
                ko_sum = knockoffs.get((doc_type, _key), {}).get("amount", Decimal(0))
                if money(row.get("Outstanding")) != money(row.get("OriginalAmt")) - ko_sum:
                    mismatch += 1
            item = _item(side, doc_type, kind, row, knockoffs.get((doc_type, _key)), settings, as_at_date, labels)
            if item is None:
                continue
            if kind == "payment" and item.pop("_local_disagree"):
                unapplied_local_disagree += 1
            else:
                item.pop("_local_disagree", None)
            if not doc["enabled"]:
                counts["guess_rows_skipped"] += 1
                continue
            items.append(item)
        ko_checks["outstanding_mismatch"] = mismatch
        ko_checks["unapplied_local_disagree"] = unapplied_local_disagree
        ko_checks["after_as_at"] = sum(
            1 for i in items if i["last_knockoff_date"] and i["last_knockoff_date"] > as_at_date
        )
        view["checks"][side] = ko_checks
        if mismatch or ko_checks["orphan_target"]:
            raise RunFailure(
                FAILURE_OUTSTANDING,
                "%s stored outstanding disagrees with the knock-offs: %d document(s), %d orphan knock-off line(s); "
                "nothing published" % (side.upper(), mismatch, ko_checks["orphan_target"]),
            )
        if unapplied_local_disagree:
            warnings.append("%s: %d payment(s) where the document and local unapplied amounts disagree on zero"
                            % (side.upper(), unapplied_local_disagree))
        if ko_checks["to_cancelled_target"]:
            warnings.append("%s: %d knock-off line(s) point at a cancelled document" % (side.upper(), ko_checks["to_cancelled_target"]))
        if ko_checks["after_as_at"]:
            warnings.append("%s: %d document(s) knocked off by a paying document dated after the run date"
                            % (side.upper(), ko_checks["after_as_at"]))
        if counts["guess_rows_skipped"]:
            warnings.append("%s: %d open row(s) of a guess_sources source that is off were not exported"
                            % (side.upper(), counts["guess_rows_skipped"]))

        items.sort(key=lambda i: (i["party_code"], i["doc_date"] or date.min, i["doc_type"], i["doc_no"]))
        missing_party = sorted({i["party_code"] for i in items} - set(parties))
        if missing_party:
            raise RunFailure(FAILURE_SELF_CHECK, "%d %s open item(s) name a party missing from the master; nothing published"
                             % (sum(1 for i in items if i["party_code"] in missing_party), side.upper()))
        for item in items:
            item["party_name"] = parties[item["party_code"]]["name"]
        counts["docs_open"] = len(items)
        counts["credit_rows"] = sum(1 for i in items if i["unapplied"])
        counts["post_dated"] = sum(1 for i in items if i["post_dated"])
        counts["due_before_doc_date"] = sum(
            1 for i in items if i["due_date"] and i["doc_date"] and i["due_date"] < i["doc_date"]
        )
        if counts["due_before_doc_date"]:
            warnings.append("%s: %d open document(s) have a due date before the document date"
                            % (side.upper(), counts["due_before_doc_date"]))
        _last_payments(parties, data["%s_payment" % side])
        for code, party in parties.items():
            party["totals"] = summarise([i for i in items if i["party_code"] == code], labels)
        totals = summarise(items, labels)
        totals["parties_open"] = sum(1 for p in parties.values() if p["totals"]["docs_open"])
        counts["parties_open"] = totals["parties_open"]
        view[side] = {"parties": parties, "items": items, "totals": totals}
        view["counts"][side] = counts

    ap_payments = sum(1 for r in data["ap_payment"] if not flag(r.get("Cancelled")))
    view["ap_payments_recorded"] = bool(ap_payments or data["ap_knockoffs"])
    view["ap"]["schedule"] = ap_schedule(view["ap"]["items"], as_at_date)
    return view


def _parties(rows, side, online_codes):
    parties = {}
    for row in rows:
        code = text(row.get("PartyCode"))
        if not code:
            raise RunFailure(FAILURE_SOURCE, "A party row has no code")
        if code in parties:
            raise RunFailure(FAILURE_SOURCE, "A party code appears twice in the master")
        parties[code] = {
            "code": code,
            "name": text(row.get("PartyName")),
            "type": text(row.get("PartyType")),
            "area": text(row.get("AreaCode")),
            "agent": text(row.get("Agent")),
            "terms": text(row.get("DisplayTerm")),
            "currency": text(row.get("CurrencyCode")),
            "credit_limit": None if row.get("CreditLimit") is None else cents(row.get("CreditLimit")),
            "aging_on": text(row.get("AgingOn")),
            "active": flag(row.get("IsActive")),
            "online_shop": side == "ar" and code in online_codes,
            "last_payment_date": None,
            "last_payment_amount": None,
        }
    return parties


def _knockoffs(rows, side, docs, settings):
    """Knock-off lines summed per target document, with integrity counts."""
    mapping = settings["knockoff_target_types"][side]
    sums = {}
    checks = {"knockoff_lines": 0, "unknown_target_type": 0, "orphan_target": 0, "to_cancelled_target": 0}
    for row in rows:
        checks["knockoff_lines"] += 1
        target_type = mapping.get(text(row.get("KnockOffDocType")))
        if target_type is None:
            checks["unknown_target_type"] += 1
            continue
        key = (target_type, row.get("KnockOffDocKey"))
        target = docs.get(key)
        if target is None:
            checks["orphan_target"] += 1
            continue
        if target["cancelled"]:
            checks["to_cancelled_target"] += 1
            continue
        entry = sums.setdefault(key, {"amount": Decimal(0), "last_date": None})
        entry["amount"] += money(row.get("Amount")) + money(row.get("DiscountAmt"))
        payer_date = day(row.get("PayerDate"))
        if payer_date and (entry["last_date"] is None or payer_date > entry["last_date"]):
            entry["last_date"] = payer_date
    return sums, checks


def _item(side, doc_type, kind, row, knockoff, settings, as_at_date, labels):
    """One open item, or None when the document is fully settled."""
    guess = settings["guess_sources"]
    boundaries = settings["aging"]["boundaries"]
    doc_date = day(row.get("DocDate"))
    item = {
        "side": side,
        "party_code": text(row.get("PartyCode")),
        "doc_type": doc_type,
        "doc_no": text(row.get("DocNo")),
        "doc_date": doc_date,
        "due_date": None,
        "terms": text(row.get("DisplayTerm")),
        "currency": text(row.get("CurrencyCode")),
        "currency_rate": rate(row.get("CurrencyRate")),
        "last_knockoff_date": None,
        "unapplied": kind != "debit",
        "post_dated": False,
        "cheque_date": None,
        "agent": text(row.get("Agent")),
        "description": text(row.get("Description")),
        "ref": text(row.get("RefText")),
        "_local_disagree": False,
    }
    if kind == "debit":
        original, outstanding = money(row.get("OriginalAmt")), money(row.get("Outstanding"))
        if outstanding == 0:
            return None
        local_original = money(row.get("LocalOriginalAmt"))
        local_outstanding = local_original - money(row.get("LocalKnockedOffAmt"))
        item["due_date"] = day(row.get("DueDate"))
        item["last_knockoff_date"] = knockoff["last_date"] if knockoff else None
    elif kind in ("payment", "credit_note"):
        paid = money(row.get("OriginalAmt"))
        unapplied = paid - money(row.get("KnockOffAmt")) - money(row.get("RefundAmt"))
        if kind == "payment":
            local_unapplied = money(row.get("LocalUnappliedAmount"))
            item["_local_disagree"] = (unapplied == 0) != (local_unapplied == 0)
            if not guess["payment_home_rate"]:
                item["currency_rate"] = None
            if guess["payment_handover_date"]:
                item["cheque_date"] = day(row.get("HandOverDate"))
            item["post_dated"] = bool(doc_date and doc_date > as_at_date)
        else:
            local_unapplied = cents(unapplied * (item["currency_rate"] if item["currency_rate"] is not None else 1))
        if unapplied == 0:
            return None
        original, outstanding = -paid, -unapplied
        local_original, local_outstanding = -money(row.get("LocalOriginalAmt")), -local_unapplied
    else:  # deposit (guess source)
        paid, remaining = money(row.get("OriginalAmt")), money(row.get("Outstanding"))
        if remaining == 0:
            return None
        deposit_rate = item["currency_rate"] if item["currency_rate"] is not None else Decimal(1)
        original, outstanding = -paid, -remaining
        local_original, local_outstanding = -cents(paid * deposit_rate), -cents(remaining * deposit_rate)
    item.update(
        original_doc=original,
        knocked_off_doc=original - outstanding,
        outstanding_doc=outstanding,
        original_local=local_original,
        knocked_off_local=local_original - local_outstanding,
        outstanding_local=local_outstanding,
        days_since_doc=(as_at_date - doc_date).days if doc_date else None,
    )
    if kind == "debit":
        aged_from = item["due_date"] or doc_date
        item["days_past_due"] = (as_at_date - aged_from).days if aged_from else None
        item["bucket_due"] = labels[bucket_index(item["days_past_due"], boundaries)] if aged_from else labels[0]
        item["bucket_doc"] = labels[bucket_index(item["days_since_doc"], boundaries)] if doc_date else labels[0]
    else:
        item["days_past_due"] = None
        item["bucket_due"] = item["bucket_doc"] = UNAPPLIED_BUCKET
    return item


def _last_payments(parties, payment_rows):
    """Last non-cancelled payment date per party and the local total paid on that date."""
    latest = {}
    for row in payment_rows:
        if flag(row.get("Cancelled")):
            continue
        code, paid_on = text(row.get("PartyCode")), day(row.get("DocDate"))
        if code not in parties or paid_on is None:
            continue
        current = latest.get(code)
        amount = money(row.get("LocalOriginalAmt"))
        if current is None or paid_on > current[0]:
            latest[code] = [paid_on, amount]
        elif paid_on == current[0]:
            current[1] += amount
    for code, (paid_on, amount) in latest.items():
        parties[code]["last_payment_date"] = paid_on
        parties[code]["last_payment_amount"] = cents(amount)


def summarise(items, labels):
    """Totals in local currency over the given items. Buckets cover debit documents only;
    unapplied credits are their own figure, so buckets + unapplied = outstanding."""
    debits = [i for i in items if not i["unapplied"]]
    overdue = [i for i in debits if i["days_past_due"] is not None and i["days_past_due"] > 0]
    dated = [i["doc_date"] for i in items if i["doc_date"]]
    return {
        "outstanding": sum((i["outstanding_local"] for i in items), Decimal(0)),
        "overdue": sum((i["outstanding_local"] for i in overdue), Decimal(0)),
        "unapplied": sum((i["outstanding_local"] for i in items if i["unapplied"]), Decimal(0)),
        "by_bucket_due": [sum((i["outstanding_local"] for i in debits if i["bucket_due"] == lb), Decimal(0)) for lb in labels],
        "by_bucket_doc": [sum((i["outstanding_local"] for i in debits if i["bucket_doc"] == lb), Decimal(0)) for lb in labels],
        "docs_open": len(items),
        "oldest_open_doc_date": min(dated) if dated else None,
        "oldest_overdue_days": max((i["days_past_due"] for i in overdue), default=None),
    }


SCHEDULE_BANDS = ("overdue", "this_week", "next_week", "later")


def ap_schedule(items, as_at_date):
    """AP debit documents by due date: overdue, this week and next week (Mon-Sun, local), later."""
    monday = as_at_date - timedelta(days=as_at_date.weekday())
    next_monday, after_next = monday + timedelta(days=7), monday + timedelta(days=14)
    bands = {name: {} for name in SCHEDULE_BANDS}
    for item in items:
        if item["unapplied"]:
            continue
        due = item["due_date"] or item["doc_date"]
        if due is None or due < as_at_date:
            band = "overdue"
        elif due < next_monday:
            band = "this_week"
        elif due < after_next:
            band = "next_week"
        else:
            band = "later"
        bands[band][item["party_code"]] = bands[band].get(item["party_code"], Decimal(0)) + item["outstanding_local"]
    return {
        "week_starts": monday,
        "bands": {
            name: {
                "total": sum(parties.values(), Decimal(0)),
                "parties": sorted(parties.items(), key=lambda kv: (-kv[1], kv[0])),
            }
            for name, parties in bands.items()
        },
    }


# ---------------------------------------------------------------------------------------------
# Outputs: CSVs and the feed
# ---------------------------------------------------------------------------------------------

OPEN_ITEM_COLUMNS = (
    "as_at_local", "side", "party_code", "party_name", "doc_type", "doc_no", "doc_date", "due_date", "terms",
    "currency", "currency_rate", "original_doc", "original_local", "knocked_off_doc", "knocked_off_local",
    "outstanding_doc", "outstanding_local", "last_knockoff_date", "unapplied", "post_dated", "cheque_date",
    "days_past_due", "days_since_doc", "bucket_due", "bucket_doc", "agent", "description", "ref",
)
MONEY_FIELDS = (
    "original_doc", "original_local", "knocked_off_doc", "knocked_off_local", "outstanding_doc", "outstanding_local",
)


def party_columns(labels):
    return (
        ("as_at_local", "side", "code", "name", "type", "area", "agent", "terms", "currency", "credit_limit",
         "aging_on", "active", "online_shop", "outstanding", "overdue", "unapplied")
        + tuple("due_%s" % label_slug(lb) for lb in labels)
        + tuple("doc_%s" % label_slug(lb) for lb in labels)
        + ("docs_open", "oldest_open_doc_date", "oldest_overdue_days", "last_payment_date", "last_payment_amount")
    )


def _cell(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return value
    if isinstance(value, date):
        return value.isoformat()
    return value


def _render(columns, rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(safe_csv_row({k: _cell(row.get(k)) for k in columns}, list(columns)))
    return buffer.getvalue().encode("utf-8-sig")


def render_open_items(items, as_at_local):
    rows = []
    for item in items:
        row = dict(item, as_at_local=as_at_local)
        for field in MONEY_FIELDS:
            row[field] = cents(item[field])
        rows.append(row)
    return _render(OPEN_ITEM_COLUMNS, rows)


def render_parties(parties, side, labels, as_at_local):
    rows = []
    for code in sorted(parties):
        party = parties[code]
        totals = party["totals"]
        row = dict(party, side=side, as_at_local=as_at_local)
        row.update(
            outstanding=cents(totals["outstanding"]), overdue=cents(totals["overdue"]),
            unapplied=cents(totals["unapplied"]), docs_open=totals["docs_open"],
            oldest_open_doc_date=totals["oldest_open_doc_date"], oldest_overdue_days=totals["oldest_overdue_days"],
        )
        for lb, due_amt, doc_amt in zip(labels, totals["by_bucket_due"], totals["by_bucket_doc"]):
            row["due_%s" % label_slug(lb)] = cents(due_amt)
            row["doc_%s" % label_slug(lb)] = cents(doc_amt)
        rows.append(row)
    return _render(party_columns(labels), rows)


FEED_KEYS = (
    "schema", "schema_version", "job", "run_id", "as_at_local", "generated_at", "timezone",
    "cadence_minutes", "next_run_local", "stale_after_minutes", "currency_local", "aging", "reconciled_on",
    "ap_payments_recorded", "ar", "ap", "health",
)
FEED_AGING_KEYS = ("bases", "boundaries", "bucket_labels")
FEED_SIDE_KEYS = ("totals", "parties", "docs")
FEED_AP_EXTRA_KEYS = ("schedule",)
FEED_TOTALS_KEYS = (
    "outstanding", "overdue", "unapplied", "by_bucket_due", "by_bucket_doc", "parties_open", "docs_open",
)
FEED_PARTY_KEYS = (
    "code", "name", "type", "terms", "currency", "credit_limit", "flags", "outstanding", "overdue", "unapplied",
    "by_bucket_due", "by_bucket_doc", "docs_open", "oldest_open_doc_date", "oldest_overdue_days",
    "last_payment_date", "last_payment_amount",
)
FEED_PARTY_FLAG_KEYS = ("active", "online_shop")
FEED_DOC_KEYS = (
    "side", "party", "type", "doc_no", "doc_date", "due_date", "currency", "original_doc", "outstanding_doc",
    "outstanding", "days_past_due", "days_since_doc", "bucket_due", "bucket_doc", "flags",
)
FEED_DOC_FLAG_KEYS = ("post_dated", "unapplied")
FEED_SCHEDULE_KEYS = ("week_starts",) + SCHEDULE_BANDS
FEED_BAND_KEYS = ("total", "parties")
FEED_HEALTH_KEYS = ("status", "failure_class", "self_checks", "counts")
SELF_CHECK_NAMES = ("row_identity", "party_totals", "buckets", "ap_schedule", "feed_matches_csv")


def _iso(value):
    return value.isoformat() if isinstance(value, date) else None


def _feed_totals(totals):
    return {
        "outstanding": json_money(totals["outstanding"]),
        "overdue": json_money(totals["overdue"]),
        "unapplied": json_money(totals["unapplied"]),
        "by_bucket_due": [json_money(v) for v in totals["by_bucket_due"]],
        "by_bucket_doc": [json_money(v) for v in totals["by_bucket_doc"]],
        "parties_open": totals.get("parties_open", 0),
        "docs_open": totals["docs_open"],
    }


def _feed_side(side_view):
    parties = []
    for code in sorted(side_view["parties"]):
        party = side_view["parties"][code]
        totals = party["totals"]
        parties.append({
            "code": code,
            "name": party["name"],
            "type": party["type"],
            "terms": party["terms"],
            "currency": party["currency"],
            "credit_limit": None if party["credit_limit"] is None else json_money(party["credit_limit"]),
            "flags": {"active": party["active"], "online_shop": party["online_shop"]},
            "outstanding": json_money(totals["outstanding"]),
            "overdue": json_money(totals["overdue"]),
            "unapplied": json_money(totals["unapplied"]),
            "by_bucket_due": [json_money(v) for v in totals["by_bucket_due"]],
            "by_bucket_doc": [json_money(v) for v in totals["by_bucket_doc"]],
            "docs_open": totals["docs_open"],
            "oldest_open_doc_date": _iso(totals["oldest_open_doc_date"]),
            "oldest_overdue_days": totals["oldest_overdue_days"],
            "last_payment_date": _iso(party["last_payment_date"]),
            "last_payment_amount": None if party["last_payment_amount"] is None else json_money(party["last_payment_amount"]),
        })
    docs = [
        {
            "side": i["side"],
            "party": i["party_code"],
            "type": i["doc_type"],
            "doc_no": i["doc_no"],
            "doc_date": _iso(i["doc_date"]),
            "due_date": _iso(i["due_date"]),
            "currency": i["currency"],
            "original_doc": json_money(i["original_doc"]),
            "outstanding_doc": json_money(i["outstanding_doc"]),
            "outstanding": json_money(i["outstanding_local"]),
            "days_past_due": i["days_past_due"],
            "days_since_doc": i["days_since_doc"],
            "bucket_due": i["bucket_due"],
            "bucket_doc": i["bucket_doc"],
            "flags": {"post_dated": i["post_dated"], "unapplied": i["unapplied"]},
        }
        for i in side_view["items"]
    ]
    return {"totals": _feed_totals(side_view["totals"]), "parties": parties, "docs": docs}


def build_feed(settings, view, run_id, started, as_at_local):
    """The dashboard feed (schema xb.ac2.arap_dashboard_feed, version 1). Pure.

    Money is a JSON number in local currency (2 decimals) unless named _doc. Every total covers
    every open item: start dates are a tracker-page setting (owner, 17 Nov 2026).
    """
    ap = _feed_side(view["ap"])
    schedule = view["ap"]["schedule"]
    ap["schedule"] = {"week_starts": _iso(schedule["week_starts"])}
    for name in SCHEDULE_BANDS:
        band = schedule["bands"][name]
        ap["schedule"][name] = {
            "total": json_money(band["total"]),
            "parties": [{"code": code, "amount": json_money(amount)} for code, amount in band["parties"]],
        }
    return {
        "schema": FEED_SCHEMA,
        "schema_version": FEED_SCHEMA_VERSION,
        "job": JOB,
        "run_id": run_id,
        "as_at_local": as_at_local,
        "generated_at": started.isoformat(),
        "timezone": settings["timezone"],
        "cadence_minutes": CADENCE_MINUTES,
        "next_run_local": stock.next_run_local(started, settings["schedule"]["run_minute"]),
        "stale_after_minutes": settings["stale_after_minutes"],
        "currency_local": LOCAL_CURRENCY,
        "aging": {
            "bases": list(AGING_BASES),
            "boundaries": list(settings["aging"]["boundaries"]),
            "bucket_labels": list(view["labels"]),
        },
        "reconciled_on": settings["reconciled_on"],
        "ap_payments_recorded": view["ap_payments_recorded"],
        "ar": _feed_side(view["ar"]),
        "ap": ap,
        # Only a feed whose self-checks all passed is ever published.
        "health": {
            "status": "success",
            "failure_class": None,
            "self_checks": {name: "pass" for name in SELF_CHECK_NAMES},
            "counts": view["counts"],
        },
    }


# ---------------------------------------------------------------------------------------------
# Self-checks (fail closed, counts only)
# ---------------------------------------------------------------------------------------------


def run_self_checks(view, csv_files, feed):
    """Raise RunFailure(self_check_failed) naming the failed checks; return the counts otherwise."""
    failed = {}
    for side in SIDES:
        side_view = view[side]
        items, parties, totals = side_view["items"], side_view["parties"], side_view["totals"]
        bad_rows = sum(
            1 for i in items
            if i["original_doc"] - i["knocked_off_doc"] != i["outstanding_doc"]
            or i["original_local"] - i["knocked_off_local"] != i["outstanding_local"]
            or (i["unapplied"] and i["outstanding_local"] > 0)
        )
        if bad_rows:
            failed["row_identity"] = failed.get("row_identity", 0) + bad_rows
        party_bad = 0
        for code, party in parties.items():
            own = [i for i in items if i["party_code"] == code]
            if sum((i["outstanding_local"] for i in own), Decimal(0)) != party["totals"]["outstanding"]:
                party_bad += 1
            if len(own) != party["totals"]["docs_open"]:
                party_bad += 1
        if sum((p["totals"]["outstanding"] for p in parties.values()), Decimal(0)) != totals["outstanding"]:
            party_bad += 1
        if party_bad:
            failed["party_totals"] = failed.get("party_totals", 0) + party_bad
        bucket_bad = 0
        for summary in [totals] + [p["totals"] for p in parties.values()]:
            for basis in ("by_bucket_due", "by_bucket_doc"):
                if sum(summary[basis], Decimal(0)) + summary["unapplied"] != summary["outstanding"]:
                    bucket_bad += 1
        if bucket_bad:
            failed["buckets"] = failed.get("buckets", 0) + bucket_bad
        csv_bad = _csv_vs_feed(side, csv_files, feed, len(parties))
        if csv_bad:
            failed["feed_matches_csv"] = failed.get("feed_matches_csv", 0) + csv_bad
    schedule = view["ap"]["schedule"]["bands"]
    ap_debits = sum((i["outstanding_local"] for i in view["ap"]["items"] if not i["unapplied"]), Decimal(0))
    if sum((schedule[b]["total"] for b in SCHEDULE_BANDS), Decimal(0)) != ap_debits:
        failed["ap_schedule"] = 1
    if failed:
        raise RunFailure(
            FAILURE_SELF_CHECK,
            "Self-check failed: %s; nothing published" % ", ".join("%s (%d)" % kv for kv in sorted(failed.items())),
        )
    return {name: "pass" for name in SELF_CHECK_NAMES}


def _csv_rows(data):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def _csv_vs_feed(side, csv_files, feed, party_count):
    """Re-read the rendered CSVs and compare their totals with the feed, to the cent."""
    bad = 0
    items_csv = _csv_rows(csv_files[AR_ITEMS_NAME if side == "ar" else AP_ITEMS_NAME])
    parties_csv = _csv_rows(csv_files[AR_PARTIES_NAME if side == "ar" else AP_PARTIES_NAME])
    feed_totals = feed[side]["totals"]
    item_sum = sum((Decimal(r["outstanding_local"]) for r in items_csv), Decimal(0))
    party_sum = sum((Decimal(r["outstanding"]) for r in parties_csv), Decimal(0))
    feed_total = cents(Decimal(str(feed_totals["outstanding"])))
    if item_sum != feed_total or party_sum != feed_total:
        bad += 1
    if len(items_csv) != feed_totals["docs_open"] or len(items_csv) != len(feed[side]["docs"]):
        bad += 1
    if len(parties_csv) != party_count or len(parties_csv) != len(feed[side]["parties"]):
        bad += 1
    return bad


# ---------------------------------------------------------------------------------------------
# Lock (a copy of the stock exporter's acquire_lock with this job's lock file; POST_SHIP shared)
# ---------------------------------------------------------------------------------------------


def acquire_lock(archive_root, run_id, now, warnings):
    path = Path(archive_root) / LOCK_NAME
    payload = stock.json_bytes({"run_id": run_id, "pid": os.getpid(), "started_at": now.isoformat()})
    try:
        stock.write_exclusive(path, payload)
        return path
    except FileExistsError:
        pass
    held = stock._read_lock(path)
    if held is None:
        raise RunFailure(FAILURE_STALE_LOCK, "The lock file is unreadable; check no run is active, then remove it")
    age = now - held["started_at"]
    if age <= timedelta(minutes=stock.LOCK_STALE_AFTER_MINUTES):
        raise RunFailure(FAILURE_ALREADY_RUNNING, "Another run holds the lock")
    boot = stock._boot_time()
    before_boot = boot is not None and held["started_at"] < boot - timedelta(seconds=stock.LOCK_BOOT_TOLERANCE_SECONDS)
    if not before_boot and stock._pid_running(held["pid"], held["started_at"]):
        raise RunFailure(FAILURE_ALREADY_RUNNING, "Another run holds the lock")
    try:
        if path.read_bytes() != held["raw"]:
            raise RunFailure(FAILURE_ALREADY_RUNNING, "Another run holds the lock")
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RunFailure(FAILURE_STALE_LOCK, "A lock left by a killed run could not be removed") from exc
    try:
        stock.write_exclusive(path, payload)
    except FileExistsError as exc:
        raise RunFailure(FAILURE_ALREADY_RUNNING, "Another run holds the lock") from exc
    warnings.append("Cleared a lock left by run %s (lock age %d minutes)" % (held["run_id"][:8], int(age.total_seconds() // 60)))
    return path


# ---------------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------------


def run_once(settings, source=None, environ=None, clock=None, sleep=time.sleep, alert_transport=None,
             credential_reader=None):
    """One export. Returns the run status dict (also written as last_run_status.json).

    The manifest of a successful run is under the "_manifest" key (not written to the status).
    alert_transport and credential_reader replace the real POST and Credential Manager read in
    the offline tests only.
    """
    tz = stock.load_zone(settings["timezone"])
    run_id = uuid.uuid4().hex
    started = stock._now(tz, clock)
    archive_root = Path(settings["output"]["archive_root"])
    delivery_root = settings["output"]["delivery_root"]
    delivery_root = Path(delivery_root) if delivery_root else None
    warnings = []
    status = {
        "job": JOB,
        "run_id": run_id,
        "status": "failed",
        "failure_class": None,
        "message": None,
        "started_at": started.isoformat(),
        "finished_at": None,
        "as_at_local": None,
        "delivery_status": DELIVERY_SKIPPED if delivery_root else DELIVERY_NOT_CONFIGURED,
        "ap_payments_recorded": None,
        "docs_open": None,
        "warning_count": 0,
        "next_run_local": stock.next_run_local(started, settings["schedule"]["run_minute"]),
    }
    lock_path = None
    entries = None
    manifest = None
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
        lock_path = acquire_lock(archive_root, run_id, started, warnings)
        source = source or SqlServerSource(settings)
        snapshot = read_snapshot(source, settings, environ, sleep)
        if snapshot["identity"].get("privileged"):
            warnings.append("Privileged read identity (sysadmin or db_owner) allowed by allow_privileged_identity")
        as_at_local = stock._as_local(snapshot["sql_now"], tz)
        as_at_date = datetime.fromisoformat(as_at_local).date()
        status["as_at_local"] = as_at_local
        view = build_view(snapshot["data"], settings, as_at_date)
        warnings.extend(view["warnings"])
        csv_files = {
            AR_ITEMS_NAME: render_open_items(view["ar"]["items"], as_at_local),
            AP_ITEMS_NAME: render_open_items(view["ap"]["items"], as_at_local),
            AR_PARTIES_NAME: render_parties(view["ar"]["parties"], "ar", view["labels"], as_at_local),
            AP_PARTIES_NAME: render_parties(view["ap"]["parties"], "ap", view["labels"], as_at_local),
        }
        feed = build_feed(settings, view, run_id, started, as_at_local)
        self_checks = run_self_checks(view, csv_files, feed)
        files = dict(csv_files)
        files[FEED_NAME] = stock.gzip_json_bytes(feed)
        manifest = {
            "job": JOB,
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "status": "success",
            "started_at": started.isoformat(),
            "finished_at": stock._now(tz, clock).isoformat(),
            "as_at_local": as_at_local,
            "as_at_sql": snapshot["sql_now"].isoformat(),
            "timezone": settings["timezone"],
            "source": {
                "database": settings["source"]["expected_database"],
                "queries_sha256": queries_sha256(),
                "settings_sha256": settings_sha256(settings),
                "git_commit": stock.git_commit(),
            },
            "identity": snapshot["identity"],
            "attempts": snapshot["attempts"],
            "read_mode": snapshot["read_mode"],
            "counts": view["counts"],
            "checks": {"knockoffs": view["checks"], "self_checks": self_checks},
            "ap_payments_recorded": view["ap_payments_recorded"],
            "files": {
                name: {"sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data)} for name, data in files.items()
            },
            "warnings": list(warnings),
        }
        entries = [(name, files[name]) for name in PUBLISHED_FILES[:-1]]
        entries.append((LATEST_MANIFEST_NAME, stock.json_bytes(manifest)))
        status["ap_payments_recorded"] = view["ap_payments_recorded"]
        status["docs_open"] = {side: view["counts"][side]["docs_open"] for side in SIDES}
        status["status"] = "success"
    except RunFailure as exc:
        status["failure_class"] = exc.failure_class
        status["message"] = "%s (run %s)" % (sanitize_error(str(exc)), run_id[:8])
    except Exception:  # noqa: BLE001 - every outcome writes a status; details stay out of it.
        status["failure_class"] = FAILURE_INTERNAL
        status["message"] = "Unexpected error (run %s)" % run_id[:8]

    if status["status"] == "success":
        tag = run_id[:8]
        try:
            stock.publish_verified_set([(archive_root / name, data) for name, data in entries], tag)
        except OSError:
            status.update(status="failed", failure_class=FAILURE_LATEST,
                          message="Could not publish the latest set (run %s)" % tag)
        else:
            if delivery_root:
                try:
                    stock.publish_verified_set([(delivery_root / name, data) for name, data in entries], tag)
                    status["delivery_status"] = DELIVERY_HANDED
                except VerifyError:
                    status["delivery_status"] = DELIVERY_VERIFY_FAILED
                except OSError:
                    status["delivery_status"] = DELIVERY_COPY_FAILED
    if status["status"] != "success":
        status["ap_payments_recorded"] = None
        status["docs_open"] = None
    status["warning_count"] = len(warnings)
    status["finished_at"] = stock._now(tz, clock).isoformat()
    # Last step before the status file, still under the lock: the alert reads the final status, and
    # its own outcome is recorded beside it. It never changes the status, the exit code or a file
    # of the latest set.
    status["alert_status"], status["alert_http_status"] = send_run_alert(
        settings, archive_root, status, started, run_id, environ, alert_transport, credential_reader
    )
    for root in (archive_root, delivery_root):
        if root is None:
            continue
        try:
            root.mkdir(parents=True, exist_ok=True)
            stock.replace_atomic(root / LAST_RUN_STATUS_NAME, stock.json_bytes(status), run_id[:8])
        except OSError:
            pass
    if lock_path is not None:
        stock.release_lock(lock_path, run_id)
    result = dict(status)
    result["_manifest"] = manifest if status["status"] == "success" else None
    result["_warnings"] = warnings
    return result


# ---------------------------------------------------------------------------------------------
# Error alerts (A3). Errors and the one-time AP notice only; never changes a run's outcome.
# ---------------------------------------------------------------------------------------------


def alert_conditions(status):
    """The alert condition keys a last_run_status dict raises. Pure.

    A failed run raises only run_failed, keyed by its failure class so a different failure is a
    new condition. A successful run can raise delivery_failed and the ap_payments_recorded notice.
    """
    if status.get("status") != "success":
        failure_class = status.get("failure_class")
        return ["run_failed:%s" % (failure_class if failure_class in ALERT_FAILURE_CLASSES else "unknown")]
    keys = []
    if status.get("delivery_status") in ALERT_DELIVERY_FAILURES:
        keys.append("delivery_failed")
    if status.get("ap_payments_recorded") is True:
        keys.append("ap_payments_recorded")
    return keys


def plan_alert(alerted, active, now, repeat_hours):
    """De-duplication rule. Pure. Returns (alert_kind or None, state to keep if nothing is sent).

    The stock export's rule (stock.plan_alert): send when a key is new or was last sent
    repeat_hours or more ago, drop keys a successful run saw cleared, and keep the success-side
    keys through failed runs. One addition: a notice key that was already sent is left out of
    the decision, so it is never repeated daily, but is kept in the state while it stays active.
    """
    quiet = [key for key in active if key in ALERT_NOTICE_CONDITIONS and key in alerted]
    kind, kept = stock.plan_alert(alerted, [key for key in active if key not in quiet], now, repeat_hours)
    kept.update((key, alerted[key]) for key in quiet)
    return kind, kept


def load_alert_state(archive_root):
    """The alert de-dup state ({condition key: last sent time}); empty when missing or unreadable."""
    try:
        raw = json.loads(Path(archive_root, ALERT_STATE_NAME).read_text(encoding="utf-8"))
        if raw.get("schema") != ALERT_STATE_SCHEMA or not isinstance(raw.get("alerted"), dict):
            return {}
        return {str(k): v for k, v in raw["alerted"].items() if isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_alert_state(archive_root, alerted, tag):
    stock.replace_atomic(
        Path(archive_root) / ALERT_STATE_NAME,
        stock.json_bytes({"schema": ALERT_STATE_SCHEMA, "alerted": dict(sorted(alerted.items()))}),
        tag,
    )


def _keep_alert_state(archive_root, kept, alerted, tag):
    """Drop cleared conditions when nothing was sent. A write error is ignored: stale entries only
    make a recurrence look like a repeat instead of new."""
    if kept != alerted:
        try:
            save_alert_state(archive_root, kept, tag)
        except OSError:
            pass


def build_alert_payload(status, active, kind, started):
    """The sanitized event. Closed keys and closed values only: no party, document, amount,
    path, message or host, whatever the status dict holds."""
    run_id = status.get("run_id")
    succeeded = status.get("status") == "success"
    failure_class = status.get("failure_class")
    ap_recorded = status.get("ap_payments_recorded")
    return {
        "schema": ALERT_SCHEMA,
        "event": ALERT_EVENT,
        "run_id": run_id if isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id) else "unknown",
        "run_hour_local": started.replace(minute=0, second=0, microsecond=0).isoformat(),
        "alert_kind": kind if kind in ALERT_KINDS else "new",
        "status": "success" if succeeded else "failed",
        # A failed run always names a class (unknown when it is outside the closed set).
        "failure_class": None if succeeded else (
            failure_class if failure_class in ALERT_FAILURE_CLASSES else "unknown"
        ),
        "conditions": [c for c in ALERT_CONDITIONS if c in {key.split(":", 1)[0] for key in active}],
        "delivery_status": stock._alert_enum(status.get("delivery_status"), ALERT_DELIVERY_STATUSES),
        "ap_payments_recorded": ap_recorded if succeeded and isinstance(ap_recorded, bool) else None,
    }


def send_run_alert(settings, archive_root, status, started, run_id, environ=None, transport=None,
                   credential_reader=None):
    """Alert step of one run: one bounded POST at most. Returns (alert_status, http_status).

    The stock export's step with this job's event (stock.send_run_alert): the secret is checked
    on every enabled run, a failed send leaves the de-dup state as it was so the next hour tries
    again, and http_status is the answer's status or None. Never raises and never returns the
    URL, headers, secret or body.
    """
    alert = settings["alert"]
    if not alert["webhook_enabled"]:
        return ALERT_DISABLED, None
    http_status = None
    tag = run_id[:8]
    try:
        active = alert_conditions(status)
        alerted = load_alert_state(archive_root)
        kind, kept = plan_alert(alerted, active, started, alert["repeat_hours"])
        secret = stock.read_alert_secret(alert, environ, credential_reader)
        if secret is None:
            _keep_alert_state(archive_root, kept, alerted, tag)
            return ALERT_SECRET_UNAVAILABLE, None
        if kind is None:
            _keep_alert_state(archive_root, kept, alerted, tag)
            return (ALERT_NONE if not active else ALERT_SUPPRESSED), None
        payload = build_alert_payload(status, active, kind, started)
        if tuple(payload) != ALERT_PAYLOAD_KEYS:
            return ALERT_ERROR, None
        body = json.dumps(payload, ensure_ascii=True).encode("ascii")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": ALERT_USER_AGENT,
            alert["auth_header_name"]: secret,
        }
        try:
            code = (transport or stock.post_alert)(alert["webhook_url"], headers, body, alert["timeout_seconds"])
        except (OSError, ValueError, http.client.HTTPException):
            code = None
        http_status = stock._http_status(code)
        if http_status is None or not 200 <= http_status < 300:
            _keep_alert_state(archive_root, kept, alerted, tag)
            return ALERT_SEND_FAILED, http_status
        sent_at = started.isoformat()
        sent_state = dict(kept)
        sent_state.update((key, sent_at) for key in active)
        try:
            save_alert_state(archive_root, sent_state, tag)
        except OSError:
            pass  # The alert went out; without the state the next hour may repeat it once.
        return ALERT_SENT, http_status
    except Exception:  # noqa: BLE001 - an alert bug must never change the run's result.
        return ALERT_ERROR, http_status


def check_latest(settings, clock=None):
    """Read-only freshness check of the latest set. Returns (exit_code, status dict)."""
    tz = stock.load_zone(settings["timezone"])
    root = Path(settings["output"]["delivery_root"] or settings["output"]["archive_root"])
    now = stock._now(tz, clock)
    try:
        latest = json.loads((root / LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return EXIT_MISSING, {"status": "missing"}
    except (OSError, ValueError):
        return EXIT_MISSING, {"status": "invalid"}
    try:
        if latest.get("job") != JOB or latest.get("status") != "success":
            raise ValueError("not a successful manifest")
        listed = latest["files"]
        if set(listed) != set(PUBLISHED_FILES[:-1]):
            raise ValueError("unexpected file list")
        for name, meta in listed.items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != meta["sha256"]:
                raise ValueError("file does not match the manifest")
        as_at = datetime.fromisoformat(latest["as_at_local"])
        age_minutes = int((now - as_at).total_seconds() // 60)
    except (KeyError, TypeError, ValueError, OSError):
        return EXIT_MISSING, {"status": "invalid"}
    result = {"as_at_local": latest["as_at_local"], "age_minutes": age_minutes, "run_id": latest.get("run_id")}
    if age_minutes > settings["stale_after_minutes"]:
        return EXIT_STALE, dict(result, status="stale")
    return EXIT_OK, dict(result, status="fresh")


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def config_failure_status(raw, message, failure_class, clock=None):
    """Best-effort failed last_run_status.json for invalid settings, when an archive root is readable."""
    output = raw.get("output") if isinstance(raw, dict) else None
    root = output.get("archive_root") if isinstance(output, dict) else None
    if not isinstance(root, str) or not root.strip():
        return None
    tz, tz_name = timezone.utc, "UTC"
    try:
        tz, tz_name = stock.load_zone(DEFAULT_SETTINGS["timezone"]), DEFAULT_SETTINGS["timezone"]
    except RuntimeError:
        pass
    run_id = uuid.uuid4().hex
    now = stock._now(tz, clock)
    status = {
        "job": JOB, "run_id": run_id, "status": "failed", "failure_class": failure_class,
        "message": "%s (run %s)" % (message, run_id[:8]), "started_at": now.isoformat(),
        "finished_at": now.isoformat(), "as_at_local": None, "timezone": tz_name,
        "delivery_status": DELIVERY_SKIPPED, "ap_payments_recorded": None, "docs_open": None,
        # The alert settings cannot be trusted when the settings are invalid: no alert is attempted.
        "alert_status": ALERT_SKIPPED_CONFIG, "alert_http_status": None,
    }
    roots = [root]
    delivery = output.get("delivery_root")
    if isinstance(delivery, str) and delivery.strip() and not stock._same_path(delivery, root):
        roots.append(delivery)
    for status_root in roots:
        try:
            Path(status_root).mkdir(parents=True, exist_ok=True)
            stock.replace_atomic(Path(status_root) / LAST_RUN_STATUS_NAME, stock.json_bytes(status), run_id[:8])
        except (OSError, ValueError):
            pass
    return status


def main(argv=None, source=None, environ=None, clock=None, alert_transport=None, credential_reader=None):
    parser = argparse.ArgumentParser(description="Hourly read-only AC2 AR/AP aging export, latest snapshot only (#321).")
    parser.add_argument("--config", required=True, help="Local settings JSON (config/*.local.json, never committed).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-latest", action="store_true", help="Read-only stale check of the latest set.")
    mode.add_argument("--dry-run-config", action="store_true", help="Validate the settings only.")
    check_mode = "--check-latest" in (sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if check_mode and exc.code not in (0, None):
            return EXIT_MISSING
        raise

    raw = None
    try:
        raw = stock.load_settings_file(args.config)
        settings = validate_settings(raw)
    except Exception as exc:  # noqa: BLE001 - a settings failure leaves a status, never a traceback.
        if isinstance(exc, ConfigError):
            failure_class, message = exc.failure_class, "Settings invalid: %s" % exc
        else:
            failure_class, message = FAILURE_INTERNAL, "Settings could not be validated (unexpected error)"
        if args.check_latest:
            _print({"status": "invalid", "failure_class": failure_class, "message": message})
            return EXIT_MISSING
        if not args.dry_run_config and raw is not None:
            config_failure_status(raw, message, failure_class, clock)
        _print({"status": "failed", "failure_class": failure_class, "message": message})
        return EXIT_FAILED

    if args.dry_run_config:
        _print({
            "status": "valid",
            "settings_sha256": settings_sha256(settings),
            "auth_mode": settings["identity"]["auth_mode"],
            "scheduled_logon_type": settings["schedule"]["logon_type"],
            "run_minute": settings["schedule"]["run_minute"],
            "queries_sha256": queries_sha256(),
        })
        return EXIT_OK
    if args.check_latest:
        try:
            code, result = check_latest(settings, clock)
        except Exception:  # noqa: BLE001 - --check-latest exits only 0, 2 or 3.
            code, result = EXIT_MISSING, {"status": "invalid", "failure_class": FAILURE_INTERNAL}
        _print(result)
        return code

    result = run_once(settings, source=source, environ=environ, clock=clock, alert_transport=alert_transport,
                      credential_reader=credential_reader)
    _print({key: value for key, value in result.items() if not key.startswith("_")})
    if result["status"] != "success":
        return EXIT_FAILED
    if result["delivery_status"] not in (DELIVERY_HANDED, DELIVERY_NOT_CONFIGURED):
        return EXIT_FAILED
    # alert_status is deliberately not read: an alert outcome never changes the exit code.
    return EXIT_OK


def _print(payload):
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
