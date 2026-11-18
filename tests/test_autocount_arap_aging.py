"""Offline tests for the AC2 AR/AP aging export, latest snapshot only (#321 slice A1).

In-memory fakes and temporary folders only. No SQL Server, AutoCount, Drive, n8n or Task
Scheduler is touched. Every identity, host, party code, name, document number and amount here is
synthetic (codes like DEBT-A01 / CRED-B02, names like "Example Trading A").
"""

import contextlib
import copy
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import autocount_arap_aging as arap  # noqa: E402

SGT = ZoneInfo("Asia/Singapore")
FIXED_NOW = datetime(2026, 11, 17, 14, 30, 5, tzinfo=SGT)
SQL_NOW = datetime(2026, 11, 17, 14, 30, 6, tzinfo=SGT)
AS_AT = date(2026, 11, 17)
# JSON content. The extension is .fixture because the repo guardrail
# (test_member_intake_validate) allows no *.json file under tests/.
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "arap_dashboard_feed.fake.fixture"
FIXTURE_RUN_ID = "0" * 32

FAKE_DB = "AED_EXAMPLE_BOOK"
FAKE_LOGIN = "fake_reader_login"
FAKE_USER = "fake_reader_user"
FAKE_SERVER = "FAKE-SQL-HOST\\FAKEINST"
ENV = {
    "AUTOCOUNT_EXPECTED_SQL_LOGIN": FAKE_LOGIN,
    "AUTOCOUNT_EXPECTED_SQL_USER": FAKE_USER,
    "AUTOCOUNT_EXPECTED_SQL_DATABASE": FAKE_DB,
}

# Pinned query hashes: a changed query text must be reviewed (G2) and this table updated with it.
PINNED_QUERY_SHA256 = {
    "ap_credit_note": "caf2dcfbeb625d74ca253ba8952adee785259dee4abdc6bd3640000dda552b34",
    "ap_debit_note": "9b358b3cc5a0678735c4889728bf9d5a3f207cf7108b1a52afd99778a6c5411b",
    "ap_deposit": "5390c6f6c329619a0def1a5b2fd0650795e73ef0faced80ee57c39141bd5f1c5",
    "ap_invoice": "238695d1b62894080fe8757b3cb36bb75e88bf94c4d2b3d6273cbd3bf92e7bf1",
    "ap_knockoffs": "faf9b9e5e3781503901e6f69278bd9d7790f6471fd54c1aad8f26285c225579b",
    "ap_parties": "b5a32a02792bae7de2567b8bec6fcd522298e1d263de2e778fb0acdfba5490f0",
    "ap_payment": "4321022ad7464e248786ef9f5d6deecffa7fa56c2083bcb419b239b0934fe54d",
    "ar_credit_note": "1078bb384f091a373bedb680d5660acf39154d9e6ec75dd4c23523bfd5042381",
    "ar_debit_note": "74536231e5649d75a150adf146667033e43697835761e26146438e752afaf0ec",
    "ar_deposit": "12a4bf2f5a68bfa12d1a0a783d6dfd9ceea6ac8ac67f5590b271d94b27e7299b",
    "ar_invoice": "32d555d06fbdec868dfc9368da928ce9bc17e3e559cc30ba65e59c36f1cd4e6d",
    "ar_knockoffs": "945ff326f8841cf94b3cc447ed57c7ba60040189210a982766ae8119cc2a75d0",
    "ar_parties": "052ba916fb6f6be4815e9a6a38c600a6261746689ffcb7c19bd6b1489fc82f78",
    "ar_payment": "a28ab6e2f2d6e0aa5ff014e29ef2ce329414f9d2ad5ee594dfc30160511d7d90",
    "change_count": "3850a407d60499abce7344bd47075ba967e771eef5d5b0c86a30b66c7100493a",
    "identity": "5429df38941a267a8ece8d3c8e3a6b4b9543974ee4e11d13d0386b1d17ba36f6",
    "sql_time": "55ea2791766679fd010ed952b66c92c99af438d8d8b1f972a256b83a251217bd",
}

D = Decimal


def dt(value):
    return datetime.fromisoformat(value) if value else None


# ---------------------------------------------------------------------------------------------
# Fake rows (column names are the SQL aliases of the exporter's queries)
# ---------------------------------------------------------------------------------------------


def party(code, name, active="T", ptype="FAKE-TYPE", terms="C.O.D.", ccy="SGD", limit=None, aging="I"):
    return {
        "PartyCode": code, "PartyName": name, "PartyType": ptype, "AreaCode": "FAKE-AREA", "Agent": "FAKE-AGENT",
        "DisplayTerm": terms, "CurrencyCode": ccy, "CreditLimit": None if limit is None else D(limit),
        "IsActive": active, "AgingOn": aging,
    }


def debit(key, code, doc_date, due, net, out=None, ccy="SGD", rate="1", cancelled="F", prefix="IV", terms="C.O.D."):
    net = D(net)
    out = net if out is None else D(out)
    rate = D(rate)
    paid = net - out
    return {
        "DocKey": key, "DocNo": "%s-%04d" % (prefix, key), "PartyCode": code, "DocDate": dt(doc_date),
        "DueDate": dt(due), "DisplayTerm": terms, "CurrencyCode": ccy, "CurrencyRate": rate,
        "OriginalAmt": net, "LocalOriginalAmt": (net * rate).quantize(D("0.01")),
        "KnockedOffAmt": paid, "LocalKnockedOffAmt": (paid * rate).quantize(D("0.01")),
        "Outstanding": out, "Cancelled": cancelled, "Agent": "FAKE-AGENT",
        "Description": "Example order %d" % key, "RefText": "REF-%d" % key,
    }


def payment(key, code, doc_date, amount, ko="0", refund="0", local_unapplied=None, cancelled="F", handover=None):
    amount, ko, refund = D(amount), D(ko), D(refund)
    unapplied = amount - ko - refund
    return {
        "DocKey": key, "DocNo": "PM-%04d" % key, "PartyCode": code, "DocDate": dt(doc_date), "CurrencyCode": "SGD",
        "CurrencyRate": D("1"), "OriginalAmt": amount, "LocalOriginalAmt": amount, "KnockOffAmt": ko,
        "RefundAmt": refund, "LocalUnappliedAmount": unapplied if local_unapplied is None else D(local_unapplied),
        "HandOverDate": dt(handover), "Cancelled": cancelled, "Description": "Example receipt", "RefText": "",
    }


def credit_note(key, code, doc_date, net, ko="0", refund="0", cancelled="F", ccy="SGD", rate="1"):
    net, rate = D(net), D(rate)
    return {
        "DocKey": key, "DocNo": "CN-%04d" % key, "PartyCode": code, "DocDate": dt(doc_date), "CurrencyCode": ccy,
        "CurrencyRate": rate, "OriginalAmt": net, "LocalOriginalAmt": (net * rate).quantize(D("0.01")),
        "KnockOffAmt": D(ko), "RefundAmt": D(refund), "Cancelled": cancelled,
        "Description": "Example return", "RefText": "RET-%d" % key,
    }


def deposit(key, code, doc_date, amount, outstanding, cancelled="F"):
    return {
        "DocKey": key, "DocNo": "DP-%04d" % key, "PartyCode": code, "DocDate": dt(doc_date), "CurrencyCode": "SGD",
        "CurrencyRate": D("1"), "OriginalAmt": D(amount), "TransferedAmt": D(amount) - D(outstanding),
        "Outstanding": D(outstanding), "Cancelled": cancelled, "Description": "Example deposit",
    }


def knockoff(target_type, target_key, amount, payer_date, discount="0", payer="PM"):
    return {
        "PayerType": payer, "KnockOffDocType": target_type, "KnockOffDocKey": target_key,
        "Amount": D(amount), "DiscountAmt": D(discount), "PayerDate": dt(payer_date),
    }


def empty_data():
    return {key: [] for key in arap.BUSINESS_QUERY_KEYS}


def fixture_data():
    """The rich fake book behind tests/fixtures/arap_dashboard_feed.fake.fixture (as at 17 Nov 2026).

    AR: open invoices in several buckets, a part-paid invoice, unapplied payments and a credit
    note, an old-dated (January) invoice and payment, an inactive zero-balance debtor and the
    online-shop debtor. AP: supplier invoices only (no payments), in JPY, USD and SGD, one of them
    old-dated, due across every schedule band.
    """
    data = empty_data()
    data["ar_parties"] = [
        party("DEBT-A01", "Example Trading A", limit="5000"),
        party("DEBT-A02", "Example Outdoor Club B", terms="Net 30 days"),
        party("DEBT-A03", "Example School C", active="F"),
        party("DEBT-A04", "Example Retail D"),
        party("DEBT-WEB", "Example Online Shop"),
    ]
    data["ar_invoice"] = [
        debit(101, "DEBT-A01", "2026-10-01", "2026-10-31", "1200.00", terms="Net 30 days"),
        debit(102, "DEBT-A01", "2026-08-10", "2026-08-10", "450.00", out="250.00"),
        debit(103, "DEBT-A02", "2026-11-10", "2026-12-10", "800.00", terms="Net 30 days"),
        debit(104, "DEBT-A04", "2026-01-15", "2026-01-15", "300.00"),
        debit(105, "DEBT-A02", "2026-09-01", "2026-10-01", "640.00", out="0", terms="Net 30 days"),
        debit(106, "DEBT-WEB", "2026-11-16", "2026-11-16", "95.50"),
        debit(107, "DEBT-A01", "2026-11-01", "2026-11-01", "999.00", cancelled="T"),
    ]
    data["ar_payment"] = [
        payment(201, "DEBT-A01", "2026-09-15", "200.00", ko="200.00"),
        payment(202, "DEBT-A02", "2026-10-05", "700.00", ko="640.00"),
        payment(203, "DEBT-WEB", "2026-11-17", "120.00"),
        payment(204, "DEBT-A04", "2026-02-01", "50.00"),
    ]
    data["ar_credit_note"] = [credit_note(301, "DEBT-A01", "2026-11-02", "80.00")]
    data["ar_knockoffs"] = [
        knockoff("RI", 102, "200.00", "2026-09-15"),
        knockoff("RI", 105, "640.00", "2026-10-05"),
    ]
    data["ap_parties"] = [
        party("CRED-B01", "Example Supplier Japan", ccy="JPY", terms="Net 45 days"),
        party("CRED-B02", "Example Supplier US", ccy="USD", terms="Net 45 days"),
        party("CRED-B03", "Example Supplier Local"),
        party("CRED-B04", "Example Freight E", active="F"),
    ]
    data["ap_invoice"] = [
        debit(501, "CRED-B01", "2026-09-20", "2026-11-04", "150000", ccy="JPY", rate="0.0089", prefix="PI"),
        debit(502, "CRED-B02", "2026-11-01", "2026-12-16", "2400.00", ccy="USD", rate="1.3", prefix="PI"),
        debit(503, "CRED-B03", "2026-11-14", "2026-11-20", "560.00", prefix="PI"),
        debit(504, "CRED-B03", "2026-11-17", "2026-11-24", "210.00", prefix="PI"),
        debit(505, "CRED-B02", "2026-03-05", "2026-04-19", "500.00", ccy="USD", rate="1.34", prefix="PI"),
    ]
    return data


# ---------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------


def full_counters(value=5):
    return [(name, value) for name in arap.CHANGE_COUNT_TABLES]


class FakeSession:
    def __init__(self, data_seq, identity=None, counters=None, lock_timeout=False):
        self.data_seq = data_seq
        self.identity = identity or {
            "login": FAKE_LOGIN, "user": FAKE_USER, "database": FAKE_DB, "is_sysadmin": 0, "is_db_owner": 0,
        }
        self.counters = list(counters) if counters is not None else None
        self.lock_timeout = lock_timeout
        self.business_reads = -1
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def read_identity(self):
        return self.identity

    def read_change_counts(self):
        if self.counters is None:
            return full_counters()
        return self.counters.pop(0)

    def read_sql_time(self):
        return SQL_NOW

    def read_rows(self, key):
        if self.lock_timeout:
            raise arap.SourceLockTimeout("lock")
        if key == arap.BUSINESS_QUERY_KEYS[0]:
            self.business_reads += 1
        data = self.data_seq[min(self.business_reads, len(self.data_seq) - 1)]
        return copy.deepcopy(data.get(key, []))


class FakeSource:
    def __init__(self, data=None, sessions=None):
        self.sessions = sessions
        self.data = data
        self.opened = 0

    def open_session(self):
        self.opened += 1
        if self.sessions is not None:
            return self.sessions.pop(0)
        return FakeSession([self.data])


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="arap_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def settings(self, delivery=False, **overrides):
        raw = {
            "source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
            "output": {
                "archive_root": str(self.tmp / "archive"),
                "delivery_root": str(self.tmp / "drive") if delivery else None,
            },
            "parties": {"online_shop_debtor_codes": ["DEBT-WEB"]},
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(raw.get(key), dict):
                raw[key].update(value)
            else:
                raw[key] = value
        return arap.validate_settings(raw)

    def run_export(self, data=None, settings=None, source=None, **kwargs):
        settings = settings or self.settings()
        source = source or FakeSource(data if data is not None else fixture_data())
        with contextlib.redirect_stdout(io.StringIO()):
            return arap.run_once(settings, source=source, environ=ENV, clock=lambda: FIXED_NOW,
                                 sleep=lambda _s: None, **kwargs)

    def view(self, data, settings=None):
        return arap.build_view(data, settings or self.settings(), AS_AT)


def read_csv(path):
    return list(csv.DictReader(io.StringIO(Path(path).read_text(encoding="utf-8-sig"))))


def item(view, side, doc_no):
    return next(i for i in view[side]["items"] if i["doc_no"] == doc_no)


def fixture_feed(settings):
    view = arap.build_view(fixture_data(), settings, AS_AT)
    return arap.build_feed(settings, view, FIXTURE_RUN_ID, FIXED_NOW, SQL_NOW.isoformat())


# ---------------------------------------------------------------------------------------------
# AC1 read safety
# ---------------------------------------------------------------------------------------------


class QueryContractTests(unittest.TestCase):
    def test_every_query_is_one_read_only_select(self):
        for name, text in arap.all_query_texts().items():
            self.assertTrue(text.lstrip().upper().startswith("SELECT"), name)
            self.assertIsNone(arap.FORBIDDEN_SQL_RE.search(text), name)
            self.assertNotIn("NOLOCK", text.upper(), name)
            self.assertNotIn("?", text, name)

    def test_forbidden_pattern_catches_writes_ddl_into_exec_separators_and_comments(self):
        for bad in (
            "SELECT 1; SELECT 2", "SELECT a INTO t FROM b", "EXEC sp_who", "SELECT 1 -- note", "SELECT /* x */ 1",
            "UPDATE dbo.ARInvoice SET Outstanding = 0", "DELETE FROM x", "INSERT INTO x VALUES (1)", "DROP TABLE x",
            "ALTER TABLE x ADD y int", "CREATE TABLE x (y int)", "MERGE x USING y ON 1 = 1", "TRUNCATE TABLE x",
            "SELECT * FROM OPENROWSET(1)", "WAITFOR DELAY '0:0:1'", "DECLARE @x int",
        ):
            with self.subTest(bad=bad):
                self.assertIsNotNone(arap.FORBIDDEN_SQL_RE.search(bad))
                with self.assertRaises(arap.ConfigError):
                    arap.assert_read_only(bad)

    def test_column_names_containing_keywords_are_not_flagged(self):
        arap.assert_read_only("SELECT x.LastUpdate, x.TransferedAmt, x.Description FROM dbo.Debtor AS x")

    def test_session_setup_is_isolation_and_lock_timeout_only(self):
        self.assertEqual(arap.SESSION_SETUP, ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", "SET LOCK_TIMEOUT %d"))

        class Cursor:
            def __init__(self, log):
                self.log = log
                self.description = []

            def execute(self, sql):
                self.log.append(sql)

            def fetchall(self):
                return []

        class Connection:
            def __init__(self):
                self.log = []
                self.timeout = None

            def cursor(self):
                return Cursor(self.log)

        connection = Connection()
        settings = arap.validate_settings({
            "source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
            "output": {"archive_root": "C:\\EXAMPLE\\archive"},
        })
        arap.SqlServerSession(connection, settings, RuntimeError)
        self.assertEqual(connection.log, ["SET TRANSACTION ISOLATION LEVEL READ COMMITTED", "SET LOCK_TIMEOUT 5000"])
        self.assertEqual(connection.timeout, 60)

    def test_query_hashes_pinned(self):
        self.assertEqual(arap.queries_sha256(), PINNED_QUERY_SHA256)

    def test_knockoff_reads_exclude_cancelled_paying_documents(self):
        for side in ("ar", "ap"):
            text = arap.QUERIES["%s_knockoffs" % side]
            self.assertEqual(text.count("Cancelled = N'F'"), 3)
            self.assertEqual(text.count("UNION ALL"), 2)

    def test_connection_string_never_carries_sa_and_names_this_job(self):
        settings = arap.validate_settings({
            "source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
            "output": {"archive_root": "C:\\EXAMPLE\\archive"},
        })
        text = arap.build_connection_string(settings)
        self.assertIn("APP=xb_ac2_arap_aging", text)
        self.assertIn("Trusted_Connection=yes", text)


class IdentityTests(TempDirTestCase):
    def failing_identity(self, **identity):
        base = {"login": FAKE_LOGIN, "user": FAKE_USER, "database": FAKE_DB, "is_sysadmin": 0, "is_db_owner": 0}
        base.update(identity)
        source = FakeSource(sessions=[FakeSession([fixture_data()], identity=base)])
        return self.run_export(source=source)

    def test_sa_login_refused(self):
        result = self.failing_identity(login="sa")
        self.assertEqual(result["failure_class"], "identity_refused")

    def test_wrong_database_refused(self):
        result = self.failing_identity(database="AED_OTHER_BOOK")
        self.assertEqual(result["failure_class"], "identity_refused")

    def test_privileged_identity_needs_the_setting(self):
        result = self.failing_identity(is_sysadmin=1)
        self.assertEqual(result["failure_class"], "identity_refused")
        settings = self.settings(identity={"allow_privileged_identity": True})
        source = FakeSource(sessions=[FakeSession([fixture_data()], identity={
            "login": FAKE_LOGIN, "user": FAKE_USER, "database": FAKE_DB, "is_sysadmin": 1, "is_db_owner": 0})])
        result = self.run_export(settings=settings, source=source)
        self.assertEqual(result["status"], "success")
        self.assertTrue(any("Privileged" in w for w in result["_warnings"]))


# ---------------------------------------------------------------------------------------------
# AC9 settings
# ---------------------------------------------------------------------------------------------


class SettingsTests(TempDirTestCase):
    def test_example_config_is_valid_and_fake(self):
        raw = json.loads((ROOT / "config" / "autocount_arap_aging.example.json").read_text(encoding="utf-8"))
        settings = arap.validate_settings(raw)
        self.assertEqual(settings["schedule"]["run_minute"], 30)
        self.assertEqual(settings["stale_after_minutes"], 70)
        self.assertIsNone(settings["output"]["delivery_root"])
        self.assertIsNone(settings["reconciled_on"])
        self.assertIn("EXAMPLE", settings["source"]["expected_database"])

    def test_defaults(self):
        settings = self.settings()
        self.assertEqual(settings["schedule"]["run_minute"], 30)
        self.assertEqual(settings["stale_after_minutes"], 70)
        self.assertEqual(settings["aging"]["boundaries"], [0, 30, 60, 90, 120])
        self.assertIsNone(settings["output"]["delivery_root"])
        self.assertFalse(any(settings["guess_sources"].values()))
        self.assertEqual(settings["knockoff_target_types"], {"ar": {"RI": "IV"}, "ap": {}})

    def assert_invalid(self, **overrides):
        with self.assertRaises(arap.ConfigError):
            self.settings(**overrides)

    def test_unknown_keys_fail(self):
        self.assert_invalid(extra_setting=1)
        self.assert_invalid(output={"retention_days": 7})
        self.assert_invalid(read={"nolock": True})

    def test_no_history_retention_or_start_date_settings(self):
        # Latest snapshot only (owner, 17 Nov) and start dates live in the tracker page (owner, 17 Nov).
        for key in ("retention", "daily_close", "history", "ar_start_date", "ap_start_date"):
            self.assertNotIn(key, arap.DEFAULT_SETTINGS)
            with self.subTest(key=key):
                self.assert_invalid(**{key: None})

    def test_missing_expected_database_fails(self):
        with self.assertRaises(arap.ConfigError):
            arap.validate_settings({"source": {"server": FAKE_SERVER}, "output": {"archive_root": str(self.tmp)}})

    def test_schedule_must_be_hourly(self):
        self.assert_invalid(schedule={"frequency": "daily"})
        self.assert_invalid(schedule={"run_minute": 60})

    def test_bad_values_fail(self):
        self.assert_invalid(aging={"boundaries": [30, 60]})
        self.assert_invalid(aging={"boundaries": [0, 60, 30]})
        self.assert_invalid(aging={"boundaries": [0, True]})
        self.assert_invalid(aging={"boundaries": []})
        self.assert_invalid(parties={"online_shop_debtor_codes": "DEBT-WEB"})
        self.assert_invalid(knockoff_target_types={"ar": {"RI": "PI"}})
        self.assert_invalid(knockoff_target_types={"ap": {"bad code": "PI"}})
        self.assert_invalid(guess_sources={"ar_deposits": "yes"})
        self.assert_invalid(reconciled_on="17/11/2026")
        self.assert_invalid(stale_after_minutes=0)
        self.assert_invalid(read={"max_attempts": 0})

    def test_sa_and_logon_pairs(self):
        self.assert_invalid(identity={"auth_mode": "sql_login", "sql_login": "sa", "credential_target": "FAKE"},
                            schedule={"logon_type": "Password"})
        self.assert_invalid(schedule={"logon_type": "Password"})

    def test_delivery_root_must_be_separate(self):
        archive = str(self.tmp / "archive")
        with self.assertRaises(arap.ConfigError):
            arap.validate_settings({"source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
                                    "output": {"archive_root": archive, "delivery_root": archive}})
        with self.assertRaises(arap.ConfigError):
            arap.validate_settings({"source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
                                    "output": {"archive_root": archive, "delivery_root": archive + "\\drive"}})

    def test_reconciled_on_reaches_the_feed(self):
        settings = self.settings(reconciled_on="2026-11-20")
        self.assertEqual(fixture_feed(settings)["reconciled_on"], "2026-11-20")
        self.assertIsNone(fixture_feed(self.settings())["reconciled_on"])

    def test_invalid_settings_fail_before_any_read(self):
        config = self.tmp / "settings.json"
        config.write_text(json.dumps({
            "schedule": {"frequency": "daily"},
            "source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
            "output": {"archive_root": str(self.tmp / "archive")},
        }), encoding="utf-8")
        source = FakeSource(fixture_data())
        with contextlib.redirect_stdout(io.StringIO()):
            code = arap.main(["--config", str(config)], source=source, environ=ENV, clock=lambda: FIXED_NOW)
        self.assertEqual(code, arap.EXIT_FAILED)
        self.assertEqual(source.opened, 0)
        status = json.loads((self.tmp / "archive" / arap.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["failure_class"], "config_invalid")
        self.assertEqual(sorted(p.name for p in (self.tmp / "archive").iterdir()), [arap.LAST_RUN_STATUS_NAME])

    def test_dry_run_config(self):
        config = self.tmp / "settings.json"
        config.write_text(json.dumps({
            "source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
            "output": {"archive_root": str(self.tmp / "archive")},
        }), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = arap.main(["--config", str(config), "--dry-run-config"])
        self.assertEqual(code, arap.EXIT_OK)
        printed = json.loads(out.getvalue())
        self.assertEqual(printed["status"], "valid")
        self.assertEqual(printed["run_minute"], 30)
        self.assertFalse((self.tmp / "archive").exists())


# ---------------------------------------------------------------------------------------------
# AC3 / AC4 open items, knock-offs, buckets, flags
# ---------------------------------------------------------------------------------------------


class BucketTests(TempDirTestCase):
    def test_labels(self):
        self.assertEqual(arap.bucket_labels([0, 30, 60, 90, 120]),
                         ["current", "1-30", "31-60", "61-90", "91-120", "over_120"])
        self.assertEqual(arap.bucket_labels([0]), ["current", "over_0"])

    def test_every_boundary_edge_on_both_bases(self):
        expected = {
            -1: "current", 0: "current", 1: "1-30", 30: "1-30", 31: "31-60", 60: "31-60", 61: "61-90",
            90: "61-90", 91: "91-120", 120: "91-120", 121: "over_120", 400: "over_120",
        }
        data = empty_data()
        data["ar_parties"] = [party("DEBT-A01", "Example Trading A")]
        rows = []
        for index, days in enumerate(sorted(expected)):
            when = date.fromordinal(AS_AT.toordinal() - days).isoformat()
            # Due basis: due date "days" ago, document dated 200 days earlier than that.
            doc_day = date.fromordinal(AS_AT.toordinal() - days - 200).isoformat()
            rows.append(debit(1000 + index, "DEBT-A01", doc_day, when, "10.00"))
            # Document basis: document "days" ago (future-dated for -1), due on the same day.
            rows.append(debit(2000 + index, "DEBT-A01", when, when, "10.00"))
        data["ar_invoice"] = rows
        view = self.view(data)
        for index, days in enumerate(sorted(expected)):
            with self.subTest(days=days):
                due_item = item(view, "ar", "IV-%04d" % (1000 + index))
                doc_item = item(view, "ar", "IV-%04d" % (2000 + index))
                self.assertEqual(due_item["days_past_due"], days)
                self.assertEqual(due_item["bucket_due"], expected[days])
                self.assertEqual(doc_item["days_since_doc"], days)
                self.assertEqual(doc_item["bucket_doc"], expected[days])

    def test_custom_boundaries_are_a_settings_change_only(self):
        settings = self.settings(aging={"boundaries": [0, 7, 14]})
        feed = fixture_feed(settings)
        self.assertEqual(feed["aging"]["bucket_labels"], ["current", "1-7", "8-14", "over_14"])
        self.assertEqual(len(feed["ar"]["totals"]["by_bucket_due"]), 4)
        result = self.run_export(settings=settings)
        self.assertEqual(result["status"], "success")
        header = read_csv(self.tmp / "archive" / arap.AR_PARTIES_NAME)[0]
        self.assertIn("due_8_14", header)
        self.assertIn("doc_over_14", header)


class OpenItemTests(TempDirTestCase):
    def test_knockoffs_outstanding_and_last_knockoff_date(self):
        data = empty_data()
        data["ar_parties"] = [party("DEBT-A01", "Example Trading A")]
        data["ar_invoice"] = [debit(1, "DEBT-A01", "2026-10-01", "2026-10-31", "100.00", out="30.00")]
        data["ar_payment"] = [payment(11, "DEBT-A01", "2026-10-20", "60.00", ko="60.00")]
        data["ar_credit_note"] = [credit_note(12, "DEBT-A01", "2026-11-03", "10.00", ko="10.00")]
        data["ar_knockoffs"] = [
            knockoff("RI", 1, "55.00", "2026-10-20", discount="5.00"),
            knockoff("RI", 1, "10.00", "2026-11-03", payer="CN"),
        ]
        view = self.view(data)
        row = item(view, "ar", "IV-0001")
        self.assertEqual(row["outstanding_doc"], D("30.00"))
        self.assertEqual(row["knocked_off_doc"], D("70.00"))  # 55 paid + 5 discount + 10 credit note
        self.assertEqual(row["last_knockoff_date"], date(2026, 11, 3))
        self.assertEqual(len(view["ar"]["items"]), 1)  # the fully applied payment and CN are not open
        self.assertEqual(view["checks"]["ar"]["outstanding_mismatch"], 0)

    def test_stored_outstanding_mismatch_fails_and_publishes_nothing(self):
        data = fixture_data()
        data["ar_invoice"][1]["Outstanding"] = D("249.00")
        with self.assertRaises(arap.RunFailure) as ctx:
            self.view(data)
        self.assertEqual(ctx.exception.failure_class, "outstanding_mismatch")
        result = self.run_export(data)
        self.assertEqual(result["failure_class"], "outstanding_mismatch")
        self.assertFalse((self.tmp / "archive" / arap.LATEST_MANIFEST_NAME).exists())
        self.assertNotIn("249", result["message"])

    def test_orphan_knockoff_fails(self):
        data = fixture_data()
        data["ar_knockoffs"].append(knockoff("RI", 9999, "1.00", "2026-11-01"))
        with self.assertRaises(arap.RunFailure) as ctx:
            self.view(data)
        self.assertEqual(ctx.exception.failure_class, "outstanding_mismatch")

    def test_unknown_knockoff_target_type_fails(self):
        data = fixture_data()
        data["ap_knockoffs"] = [knockoff("PI", 501, "1.00", "2026-11-01")]  # AP mapping is empty by default
        with self.assertRaises(arap.RunFailure) as ctx:
            self.view(data)
        self.assertEqual(ctx.exception.failure_class, "unknown_knockoff_target")

    def test_foreign_currency_keeps_document_and_local_amounts(self):
        view = self.view(fixture_data())
        jpy = item(view, "ap", "PI-0501")
        self.assertEqual(jpy["currency"], "JPY")
        self.assertEqual(jpy["outstanding_doc"], D("150000"))
        self.assertEqual(jpy["outstanding_local"], D("1335.00"))
        self.assertEqual(jpy["currency_rate"], D("0.0089"))
        usd = item(view, "ap", "PI-0502")
        self.assertEqual(usd["outstanding_local"], D("3120.00"))
        feed_doc = next(d for d in fixture_feed(self.settings())["ap"]["docs"] if d["doc_no"] == "PI-0501")
        self.assertEqual(feed_doc["outstanding_doc"], 150000.0)
        self.assertEqual(feed_doc["outstanding"], 1335.0)

    def test_unapplied_payments_and_credit_notes_are_their_own_credit_rows(self):
        view = self.view(fixture_data())
        pm = item(view, "ar", "PM-0202")
        self.assertTrue(pm["unapplied"])
        self.assertEqual(pm["outstanding_doc"], D("-60.00"))
        self.assertEqual(pm["outstanding_local"], D("-60.00"))
        self.assertEqual(pm["bucket_due"], "unapplied")
        self.assertIsNone(pm["days_past_due"])
        cn = item(view, "ar", "CN-0301")
        self.assertEqual(cn["outstanding_local"], D("-80.00"))
        totals = view["ar"]["totals"]
        self.assertEqual(totals["unapplied"], D("-310.00"))  # -60 -120 -50 -80
        self.assertEqual(sum(totals["by_bucket_due"]) + totals["unapplied"], totals["outstanding"])
        # Never netted: DEBT-A01 keeps its invoices and its credit note as separate rows.
        a01 = [i["doc_no"] for i in view["ar"]["items"] if i["party_code"] == "DEBT-A01"]
        self.assertEqual(sorted(a01), ["CN-0301", "IV-0101", "IV-0102"])

    def test_credit_note_larger_than_invoices(self):
        data = empty_data()
        data["ar_parties"] = [party("DEBT-A01", "Example Trading A")]
        data["ar_invoice"] = [debit(1, "DEBT-A01", "2026-11-01", "2026-11-01", "100.00")]
        data["ar_credit_note"] = [credit_note(2, "DEBT-A01", "2026-11-05", "300.00")]
        view = self.view(data)
        totals = view["ar"]["parties"]["DEBT-A01"]["totals"]
        self.assertEqual(totals["outstanding"], D("-200.00"))
        self.assertEqual(totals["unapplied"], D("-300.00"))
        self.assertEqual(totals["overdue"], D("100.00"))
        result = self.run_export(data)
        self.assertEqual(result["status"], "success")

    def test_post_dated_payment_is_a_flag_not_a_filter(self):
        data = fixture_data()
        data["ar_payment"].append(payment(205, "DEBT-A02", "2026-11-20", "40.00", handover="2026-11-21"))
        view = self.view(data)
        row = item(view, "ar", "PM-0205")
        self.assertTrue(row["post_dated"])
        self.assertIsNone(row["cheque_date"])  # HandOverDate is a guess column: off by default
        self.assertEqual(view["counts"]["ar"]["post_dated"], 1)
        view = self.view(data, self.settings(guess_sources={"payment_handover_date": True}))
        self.assertEqual(item(view, "ar", "PM-0205")["cheque_date"], date(2026, 11, 21))

    def test_payment_home_rate_only_with_its_setting(self):
        self.assertIsNone(item(self.view(fixture_data()), "ar", "PM-0203")["currency_rate"])
        view = self.view(fixture_data(), self.settings(guess_sources={"payment_home_rate": True}))
        self.assertEqual(item(view, "ar", "PM-0203")["currency_rate"], D("1"))

    def test_due_date_before_document_date(self):
        data = fixture_data()
        data["ar_invoice"].append(debit(108, "DEBT-A02", "2026-11-10", "2026-11-01", "70.00"))
        view = self.view(data)
        row = item(view, "ar", "IV-0108")
        self.assertEqual(row["days_past_due"], 16)
        self.assertEqual(row["days_since_doc"], 7)
        self.assertEqual(view["counts"]["ar"]["due_before_doc_date"], 1)
        self.assertTrue(any("due date before the document date" in w for w in view["warnings"]))

    def test_cancelled_documents_ignored(self):
        data = fixture_data()
        data["ar_invoice"][6]["Outstanding"] = D("1.23")  # a cancelled invoice is never checked or exported
        data["ar_payment"].append(payment(206, "DEBT-A01", "2026-11-01", "77.00", cancelled="T"))
        view = self.view(data)
        numbers = [i["doc_no"] for i in view["ar"]["items"]]
        self.assertNotIn("IV-0107", numbers)
        self.assertNotIn("PM-0206", numbers)
        self.assertEqual(view["counts"]["ar"]["cancelled_skipped"], 2)

    def test_unexpected_flag_value_fails(self):
        data = fixture_data()
        data["ar_invoice"][0]["Cancelled"] = None
        with self.assertRaises(arap.RunFailure) as ctx:
            self.view(data)
        self.assertEqual(ctx.exception.failure_class, "source_error")

    def test_guess_sources_off_are_counted_not_exported(self):
        data = fixture_data()
        data["ar_debit_note"] = [debit(401, "DEBT-A01", "2026-11-01", "2026-11-15", "30.00", prefix="DN")]
        data["ar_deposit"] = [deposit(402, "DEBT-A02", "2026-11-01", "100.00", "100.00")]
        view = self.view(data)
        numbers = [i["doc_no"] for i in view["ar"]["items"]]
        self.assertNotIn("DN-0401", numbers)
        self.assertNotIn("DP-0402", numbers)
        self.assertEqual(view["counts"]["ar"]["guess_rows_skipped"], 2)
        self.assertTrue(any("guess_sources" in w for w in view["warnings"]))
        settings = self.settings(guess_sources={"ar_debit_notes": True, "ar_deposits": True},
                                 knockoff_target_types={"ar": {"RI": "IV", "RD": "DN"}})
        view = self.view(data, settings)
        self.assertEqual(item(view, "ar", "DN-0401")["outstanding_local"], D("30.00"))
        self.assertEqual(item(view, "ar", "DP-0402")["outstanding_local"], D("-100.00"))
        self.assertTrue(item(view, "ar", "DP-0402")["unapplied"])

    def test_every_item_carries_what_the_page_needs_for_start_dates(self):
        # Start dates are a tracker-page setting: items carry side, type, dates, currency, amounts.
        feed = fixture_feed(self.settings())
        for side in ("ar", "ap"):
            for doc in feed[side]["docs"]:
                for key in ("side", "type", "doc_date", "due_date", "currency", "outstanding"):
                    self.assertIn(key, doc)
                self.assertEqual(doc["side"], side)
        old = [d for d in feed["ar"]["docs"] + feed["ap"]["docs"] if d["doc_date"] < "2026-04-01"]
        self.assertGreaterEqual(len(old), 3)  # old-dated items stay in, and in every total
        self.assertIn(-50.0, [d["outstanding"] for d in feed["ar"]["docs"]])


class PartyTests(TempDirTestCase):
    def test_every_party_present_with_flags(self):
        view = self.view(fixture_data())
        parties = view["ar"]["parties"]
        self.assertEqual(sorted(parties), ["DEBT-A01", "DEBT-A02", "DEBT-A03", "DEBT-A04", "DEBT-WEB"])
        self.assertFalse(parties["DEBT-A03"]["active"])
        self.assertEqual(parties["DEBT-A03"]["totals"]["docs_open"], 0)
        self.assertEqual(parties["DEBT-A03"]["totals"]["outstanding"], D(0))
        self.assertTrue(parties["DEBT-WEB"]["online_shop"])
        self.assertFalse(parties["DEBT-A01"]["online_shop"])
        self.assertFalse(view["ap"]["parties"]["CRED-B04"]["active"])
        self.assertEqual(view["ar"]["totals"]["parties_open"], 4)

    def test_last_payment_date_and_amount(self):
        data = fixture_data()
        data["ar_payment"].append(payment(207, "DEBT-A01", "2026-09-15", "15.00", ko="0", cancelled="F"))
        parties = self.view(data)["ar"]["parties"]
        self.assertEqual(parties["DEBT-A01"]["last_payment_date"], date(2026, 9, 15))
        self.assertEqual(parties["DEBT-A01"]["last_payment_amount"], D("215.00"))
        self.assertIsNone(parties["DEBT-A03"]["last_payment_date"])

    def test_item_naming_a_missing_party_fails_closed(self):
        data = fixture_data()
        data["ar_invoice"].append(debit(109, "DEBT-ZZ9", "2026-11-01", "2026-11-01", "5.00"))
        with self.assertRaises(arap.RunFailure) as ctx:
            self.view(data)
        self.assertEqual(ctx.exception.failure_class, "self_check_failed")


class ApTests(TempDirTestCase):
    def test_ap_payments_recorded_false_without_payments_or_knockoffs(self):
        view = self.view(fixture_data())
        self.assertFalse(view["ap_payments_recorded"])
        self.assertFalse(fixture_feed(self.settings())["ap_payments_recorded"])
        self.assertEqual(item(view, "ap", "PI-0501")["outstanding_doc"], D("150000"))  # stored figure

    def test_ap_payments_recorded_true_with_a_payment(self):
        data = fixture_data()
        data["ap_payment"] = [payment(601, "CRED-B03", "2026-11-16", "100.00")]
        view = self.view(data)
        self.assertTrue(view["ap_payments_recorded"])
        self.assertEqual(item(view, "ap", "PM-0601")["outstanding_local"], D("-100.00"))
        self.assertEqual(item(view, "ap", "PM-0601")["doc_type"], "PV")

    def test_ap_payments_recorded_true_with_knockoffs_only(self):
        data = fixture_data()
        data["ap_invoice"][2] = debit(503, "CRED-B03", "2026-11-14", "2026-11-20", "560.00", out="500.00", prefix="PI")
        data["ap_knockoffs"] = [knockoff("PX", 503, "60.00", "2026-11-15", payer="CN")]
        view = self.view(data, self.settings(knockoff_target_types={"ap": {"PX": "PI"}}))
        self.assertTrue(view["ap_payments_recorded"])
        self.assertEqual(item(view, "ap", "PI-0503")["outstanding_local"], D("500.00"))

    def test_ap_payments_recorded_ignores_cancelled_payments(self):
        data = fixture_data()
        data["ap_payment"] = [payment(602, "CRED-B03", "2026-11-16", "100.00", cancelled="T")]
        self.assertFalse(self.view(data)["ap_payments_recorded"])

    def test_schedule_weeks_are_monday_to_sunday(self):
        self.assertEqual(AS_AT.weekday(), 1)  # Tuesday 17 Nov 2026
        schedule = self.view(fixture_data())["ap"]["schedule"]
        self.assertEqual(schedule["week_starts"], date(2026, 11, 16))
        bands = {name: band["total"] for name, band in schedule["bands"].items()}
        self.assertEqual(bands["overdue"], D("1335.00") + D("670.00"))
        self.assertEqual(bands["this_week"], D("560.00"))
        self.assertEqual(bands["next_week"], D("210.00"))
        self.assertEqual(bands["later"], D("3120.00"))


# ---------------------------------------------------------------------------------------------
# AC2 stable read
# ---------------------------------------------------------------------------------------------


class StableReadTests(TempDirTestCase):
    def test_counter_window_moves_then_settles(self):
        moved = FakeSession([fixture_data()], counters=[full_counters(5), full_counters(6)])
        settled = FakeSession([fixture_data()], counters=[full_counters(6), full_counters(6)])
        source = FakeSource(sessions=[moved, settled])
        result = self.run_export(source=source)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["_manifest"]["attempts"], 2)
        self.assertEqual(result["_manifest"]["read_mode"], "change_count")
        self.assertEqual(source.opened, 2)

    def test_unstable_on_every_attempt_fails(self):
        sessions = [FakeSession([fixture_data()], counters=[full_counters(i), full_counters(i + 1)]) for i in range(3)]
        result = self.run_export(source=FakeSource(sessions=sessions))
        self.assertEqual(result["failure_class"], "unstable_snapshot")
        self.assertFalse((self.tmp / "archive" / arap.LATEST_MANIFEST_NAME).exists())

    def test_missing_counter_rows_use_the_double_read(self):
        partial = full_counters()[:-1]
        stable = FakeSession([fixture_data()], counters=[partial, partial])
        result = self.run_export(source=FakeSource(sessions=[stable]))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["_manifest"]["read_mode"], "double_read")
        changed = fixture_data()
        changed["ar_payment"][2]["OriginalAmt"] = D("121.00")
        sessions = [FakeSession([fixture_data(), changed], counters=[partial, partial]) for _ in range(3)]
        result = self.run_export(source=FakeSource(sessions=sessions))
        self.assertEqual(result["failure_class"], "unstable_snapshot")

    def test_duplicate_counter_rows_are_not_usable(self):
        rows = full_counters() + [("ARInvoice", 5)]
        self.assertIsNone(arap.counters_usable(rows))
        self.assertIsNotNone(arap.counters_usable(full_counters()))

    def test_lock_timeout_retries_then_fails(self):
        sessions = [FakeSession([fixture_data()], lock_timeout=True) for _ in range(3)]
        result = self.run_export(source=FakeSource(sessions=sessions))
        self.assertEqual(result["failure_class"], "lock_timeout")


# ---------------------------------------------------------------------------------------------
# AC5 feed contract and the committed fake fixture
# ---------------------------------------------------------------------------------------------


EXPECTED_FEED_KEYS = {
    "schema", "schema_version", "job", "run_id", "as_at_local", "generated_at", "timezone", "cadence_minutes",
    "next_run_local", "stale_after_minutes", "currency_local", "aging", "reconciled_on", "ap_payments_recorded",
    "ar", "ap", "health",
}
EXPECTED_TOTALS_KEYS = {"outstanding", "overdue", "unapplied", "by_bucket_due", "by_bucket_doc", "parties_open", "docs_open"}
EXPECTED_PARTY_KEYS = {
    "code", "name", "type", "terms", "currency", "credit_limit", "flags", "outstanding", "overdue", "unapplied",
    "by_bucket_due", "by_bucket_doc", "docs_open", "oldest_open_doc_date", "oldest_overdue_days",
    "last_payment_date", "last_payment_amount",
}
EXPECTED_DOC_KEYS = {
    "side", "party", "type", "doc_no", "doc_date", "due_date", "currency", "original_doc", "outstanding_doc",
    "outstanding", "days_past_due", "days_since_doc", "bucket_due", "bucket_doc", "flags",
}


def assert_feed_contract(test, feed):
    test.assertEqual(set(feed), EXPECTED_FEED_KEYS)
    test.assertEqual(feed["schema"], "xb.ac2.arap_dashboard_feed")
    test.assertEqual(feed["schema_version"], 1)
    test.assertEqual(feed["cadence_minutes"], 60)
    test.assertEqual(feed["currency_local"], "SGD")
    test.assertEqual(set(feed["aging"]), {"bases", "boundaries", "bucket_labels"})
    test.assertEqual(feed["aging"]["bases"], ["due_date", "document_date"])
    test.assertIsInstance(feed["ap_payments_recorded"], bool)
    labels = feed["aging"]["bucket_labels"]
    test.assertEqual(set(feed["ar"]), {"totals", "parties", "docs"})
    test.assertEqual(set(feed["ap"]), {"totals", "parties", "docs", "schedule"})
    test.assertEqual(set(feed["ap"]["schedule"]), {"week_starts", "overdue", "this_week", "next_week", "later"})
    for band in ("overdue", "this_week", "next_week", "later"):
        test.assertEqual(set(feed["ap"]["schedule"][band]), {"total", "parties"})
        for entry in feed["ap"]["schedule"][band]["parties"]:
            test.assertEqual(set(entry), {"code", "amount"})
    test.assertEqual(set(feed["health"]), {"status", "failure_class", "self_checks", "counts"})
    for side in ("ar", "ap"):
        totals = feed[side]["totals"]
        test.assertEqual(set(totals), EXPECTED_TOTALS_KEYS)
        for basis in ("by_bucket_due", "by_bucket_doc"):
            test.assertEqual(len(totals[basis]), len(labels))
            test.assertAlmostEqual(sum(totals[basis]) + totals["unapplied"], totals["outstanding"], places=2)
        test.assertAlmostEqual(sum(d["outstanding"] for d in feed[side]["docs"]), totals["outstanding"], places=2)
        test.assertAlmostEqual(sum(p["outstanding"] for p in feed[side]["parties"]), totals["outstanding"], places=2)
        for party_entry in feed[side]["parties"]:
            test.assertEqual(set(party_entry), EXPECTED_PARTY_KEYS)
            test.assertEqual(set(party_entry["flags"]), {"active", "online_shop"})
        for doc in feed[side]["docs"]:
            test.assertEqual(set(doc), EXPECTED_DOC_KEYS)
            test.assertEqual(set(doc["flags"]), {"post_dated", "unapplied"})
            test.assertIn(doc["bucket_due"], labels + ["unapplied"])
    for forbidden in ("history", "closed_since_prev", "ar_start_date", "ap_start_date", "before_start_date"):
        test.assertNotIn(forbidden, json.dumps(feed))


class FeedContractTests(TempDirTestCase):
    def test_built_feed_has_exactly_the_contract_keys(self):
        assert_feed_contract(self, fixture_feed(self.settings()))

    def test_committed_fixture_passes_the_contract(self):
        assert_feed_contract(self, json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))

    def test_committed_fixture_is_this_exporters_output(self):
        built = json.loads(json.dumps(fixture_feed(self.settings()), sort_keys=True, default=str))
        self.assertEqual(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")), built)

    def test_fixture_is_fake_and_rich_enough_for_the_tracker(self):
        feed = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        codes = [p["code"] for p in feed["ar"]["parties"] + feed["ap"]["parties"]]
        self.assertTrue(all(re.match(r"\A(DEBT|CRED)-[A-Z0-9]{2,3}\Z", c) for c in codes), codes)
        self.assertTrue(all(p["name"].startswith("Example ") for p in feed["ar"]["parties"] + feed["ap"]["parties"]))
        self.assertFalse(feed["ap_payments_recorded"])
        self.assertTrue(any(d["flags"]["unapplied"] for d in feed["ar"]["docs"]))
        self.assertEqual({d["currency"] for d in feed["ap"]["docs"]}, {"JPY", "USD", "SGD"})
        self.assertTrue(any(d["doc_date"] < "2026-04-01" for d in feed["ar"]["docs"]))
        self.assertTrue(any(d["doc_date"] < "2026-04-01" for d in feed["ap"]["docs"]))

    def test_published_feed_matches_the_csvs(self):
        result = self.run_export()
        self.assertEqual(result["status"], "success")
        archive = self.tmp / "archive"
        feed = json.loads(gzip.decompress((archive / arap.FEED_NAME).read_bytes()))
        assert_feed_contract(self, feed)
        items = read_csv(archive / arap.AR_ITEMS_NAME)
        self.assertEqual(len(items), feed["ar"]["totals"]["docs_open"])
        total = sum(D(r["outstanding_local"]) for r in items)
        self.assertEqual(total, D(str(feed["ar"]["totals"]["outstanding"])).quantize(D("0.01")))


# ---------------------------------------------------------------------------------------------
# AC7 self-checks
# ---------------------------------------------------------------------------------------------


class SelfCheckTests(TempDirTestCase):
    def assert_self_check_failure(self, name, **patches):
        archive = self.tmp / "archive"
        self.assertEqual(self.run_export()["status"], "success")
        before = {n: (archive / n).read_bytes() for n in arap.PUBLISHED_FILES}
        with contextlib.ExitStack() as stack:
            for target, replacement in patches.items():
                stack.enter_context(mock.patch.object(arap, target, replacement))
            result = self.run_export()
        self.assertEqual(result["failure_class"], "self_check_failed")
        self.assertIn(name, result["message"])
        self.assertEqual({n: (archive / n).read_bytes() for n in arap.PUBLISHED_FILES}, before)

    def test_csv_missing_a_row(self):
        real = arap.render_open_items

        def drop_last(items, as_at_local):
            return real(items[:-1], as_at_local)

        self.assert_self_check_failure("feed_matches_csv", render_open_items=drop_last)

    def test_party_totals_disagree(self):
        real = arap.summarise

        def skewed(items, labels):
            result = real(items, labels)
            if items and items[0]["party_code"] == "DEBT-A01" and len({i["party_code"] for i in items}) == 1:
                result["outstanding"] += D("0.01")
            return result

        self.assert_self_check_failure("party_totals", summarise=skewed)

    def test_buckets_disagree(self):
        real = arap.summarise

        def skewed(items, labels):
            result = real(items, labels)
            result["by_bucket_doc"][0] += D("1.00")
            return result

        self.assert_self_check_failure("buckets", summarise=skewed)

    def test_ap_schedule_disagrees(self):
        real = arap.ap_schedule

        def skewed(items, as_at_date):
            result = real(items, as_at_date)
            result["bands"]["later"]["total"] += D("1.00")
            return result

        self.assert_self_check_failure("ap_schedule", ap_schedule=skewed)


# ---------------------------------------------------------------------------------------------
# AC6 latest only, publish, delivery
# ---------------------------------------------------------------------------------------------


class PublishTests(TempDirTestCase):
    def test_success_writes_exactly_the_latest_set_and_status(self):
        result = self.run_export()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["delivery_status"], "not_configured")
        names = sorted(p.name for p in (self.tmp / "archive").iterdir())
        self.assertEqual(names, sorted(list(arap.PUBLISHED_FILES) + [arap.LAST_RUN_STATUS_NAME]))
        self.assertFalse(any(p.is_dir() for p in (self.tmp / "archive").iterdir()))
        manifest = json.loads((self.tmp / "archive" / arap.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        for name, meta in manifest["files"].items():
            data = (self.tmp / "archive" / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), meta["sha256"])
        self.assertEqual(manifest["source"]["queries_sha256"], arap.queries_sha256())

    def test_next_run_replaces_the_set_without_extra_files(self):
        self.run_export()
        data = fixture_data()
        data["ar_payment"].append(payment(208, "DEBT-A02", "2026-11-17", "10.00"))
        self.run_export(data)
        names = sorted(p.name for p in (self.tmp / "archive").iterdir())
        self.assertEqual(names, sorted(list(arap.PUBLISHED_FILES) + [arap.LAST_RUN_STATUS_NAME]))
        self.assertIn("PM-0208", [r["doc_no"] for r in read_csv(self.tmp / "archive" / arap.AR_ITEMS_NAME)])

    def test_failed_run_keeps_the_previous_set_and_records_the_failure(self):
        settings = self.settings(delivery=True)
        self.assertEqual(self.run_export(settings=settings)["status"], "success")
        roots = [self.tmp / "archive", self.tmp / "drive"]
        before = {(r, n): (r / n).read_bytes() for r in roots for n in arap.PUBLISHED_FILES}
        bad = fixture_data()
        bad["ar_invoice"][0]["Outstanding"] = D("1.00")
        result = self.run_export(bad, settings=settings)
        self.assertEqual(result["status"], "failed")
        self.assertEqual({(r, n): (r / n).read_bytes() for r in roots for n in arap.PUBLISHED_FILES}, before)
        for root in roots:
            status = json.loads((root / arap.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
            self.assertEqual(status["failure_class"], "outstanding_mismatch")
            self.assertEqual(status["run_id"], result["run_id"])

    def test_delivery_gets_the_same_verified_set(self):
        result = self.run_export(settings=self.settings(delivery=True))
        self.assertEqual(result["delivery_status"], "handed_to_drive_sync_folder")
        for name in arap.PUBLISHED_FILES:
            self.assertEqual((self.tmp / "drive" / name).read_bytes(), (self.tmp / "archive" / name).read_bytes())
        names = sorted(p.name for p in (self.tmp / "drive").iterdir())
        self.assertEqual(names, sorted(list(arap.PUBLISHED_FILES) + [arap.LAST_RUN_STATUS_NAME]))

    def test_bad_delivery_copy_keeps_the_previous_drive_set(self):
        settings = self.settings(delivery=True)
        self.run_export(settings=settings)
        before = {n: (self.tmp / "drive" / n).read_bytes() for n in arap.PUBLISHED_FILES}
        real = arap.stock.copy_bytes_exclusive

        def corrupt_drive(path, data):
            real(path, data + b"x" if "drive" in str(path) and path.name.startswith(".ar_parties") else data)

        data = fixture_data()
        data["ar_payment"].append(payment(209, "DEBT-A02", "2026-11-17", "10.00"))
        with mock.patch.object(arap.stock, "copy_bytes_exclusive", corrupt_drive):
            result = self.run_export(data, settings=settings)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["delivery_status"], "verify_failed")
        self.assertEqual({n: (self.tmp / "drive" / n).read_bytes() for n in arap.PUBLISHED_FILES}, before)
        self.assertFalse(any(p.name.endswith(".tmp") for p in (self.tmp / "drive").iterdir()))

    def test_archive_publish_failure_fails_the_run_and_skips_delivery(self):
        settings = self.settings(delivery=True)

        def refuse(path, data):
            raise OSError("disk full")

        with mock.patch.object(arap.stock, "copy_bytes_exclusive", refuse):
            result = self.run_export(settings=settings)
        self.assertEqual(result["failure_class"], "latest_publish_failed")
        self.assertFalse((self.tmp / "drive" / arap.LATEST_MANIFEST_NAME).exists())
        status = json.loads((self.tmp / "drive" / arap.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["failure_class"], "latest_publish_failed")

    def test_never_deletes_in_delivery_root(self):
        settings = self.settings(delivery=True)
        drive = self.tmp / "drive"
        drive.mkdir(parents=True)
        (drive / "owner_notes.txt").write_text("keep", encoding="utf-8")
        (drive / "ar_open_items_2026-11-01.csv").write_text("old", encoding="utf-8")
        self.run_export(settings=settings)
        self.assertEqual((drive / "owner_notes.txt").read_text(encoding="utf-8"), "keep")
        self.assertTrue((drive / "ar_open_items_2026-11-01.csv").exists())

    def test_lock_held_gives_already_running(self):
        archive = self.tmp / "archive"
        archive.mkdir(parents=True)
        (archive / arap.LOCK_NAME).write_text(json.dumps(
            {"run_id": "f" * 32, "pid": 4242, "started_at": FIXED_NOW.isoformat()}), encoding="utf-8")
        result = self.run_export()
        self.assertEqual(result["failure_class"], "already_running")
        self.assertTrue((archive / arap.LOCK_NAME).exists())


class CheckLatestTests(TempDirTestCase):
    def check(self, now):
        return arap.check_latest(self.settings(), clock=lambda: now)

    def test_fresh_stale_missing_and_tampered(self):
        self.assertEqual(self.check(FIXED_NOW)[0], arap.EXIT_MISSING)
        self.run_export()
        self.assertEqual(self.check(FIXED_NOW)[0], arap.EXIT_OK)
        self.assertEqual(self.check(datetime(2026, 11, 17, 15, 40, 6, tzinfo=SGT))[0], arap.EXIT_OK)  # 70 minutes
        later = datetime(2026, 11, 17, 15, 41, 6, tzinfo=SGT)  # 71 minutes
        code, result = self.check(later)
        self.assertEqual((code, result["status"]), (arap.EXIT_STALE, "stale"))
        (self.tmp / "archive" / arap.AP_ITEMS_NAME).write_bytes(b"tampered")
        self.assertEqual(self.check(FIXED_NOW)[0], arap.EXIT_MISSING)


# ---------------------------------------------------------------------------------------------
# AC8 CSV safety and privacy
# ---------------------------------------------------------------------------------------------


class CsvAndPrivacyTests(TempDirTestCase):
    def test_csv_cells_are_formula_safe_and_amounts_numeric(self):
        data = fixture_data()
        data["ar_parties"][0]["PartyName"] = "=HYPERLINK(\"http://example.com\")"
        data["ar_invoice"][0]["Description"] = "+cmd"
        self.run_export(data)
        archive = self.tmp / "archive"
        parties = {r["code"]: r for r in read_csv(archive / arap.AR_PARTIES_NAME)}
        self.assertTrue(parties["DEBT-A01"]["name"].startswith("'="))
        items = read_csv(archive / arap.AR_ITEMS_NAME)
        first = next(r for r in items if r["doc_no"] == "IV-0101")
        self.assertEqual(first["description"], "'+cmd")
        self.assertEqual(first["party_name"], "'=HYPERLINK(\"http://example.com\")")
        credit = next(r for r in items if r["doc_no"] == "PM-0202")
        self.assertEqual(credit["outstanding_local"], "-60.00")
        self.assertEqual(credit["unapplied"], "true")
        self.assertEqual(list(items[0]), list(arap.OPEN_ITEM_COLUMNS))

    def test_status_manifest_and_console_carry_no_names_codes_documents_or_amounts(self):
        config = self.tmp / "settings.json"
        config.write_text(json.dumps({
            "source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
            "output": {"archive_root": str(self.tmp / "archive"), "delivery_root": str(self.tmp / "drive")},
            "parties": {"online_shop_debtor_codes": ["DEBT-WEB"]},
        }), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = arap.main(["--config", str(config)], source=FakeSource(fixture_data()), environ=ENV,
                             clock=lambda: FIXED_NOW)
        self.assertEqual(code, arap.EXIT_OK)
        texts = [
            out.getvalue(),
            (self.tmp / "archive" / arap.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"),
            (self.tmp / "archive" / arap.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"),
        ]
        forbidden = ["DEBT-", "CRED-", "Example ", "IV-0", "PI-0", "PM-0", "CN-0", "1200", "1335", "150000", "95.5",
                     FAKE_SERVER, "FAKE-SQL-HOST", FAKE_LOGIN, "Trusted_Connection"]
        for text in texts:
            for value in forbidden:
                self.assertNotIn(value, text)

    def test_failure_messages_carry_counts_only(self):
        data = fixture_data()
        data["ar_invoice"][0]["Outstanding"] = D("1199.99")
        result = self.run_export(data)
        self.assertRegex(result["message"], r"\AAR stored outstanding disagrees with the knock-offs: 1 document")
        self.assertNotIn("1199", result["message"])
        self.assertNotIn("DEBT", result["message"])


# ---------------------------------------------------------------------------------------------
# Live-check SQL template and repo hygiene
# ---------------------------------------------------------------------------------------------


class LiveCheckSqlTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "sql" / "autocount2" / "arap_aging_live_check.readonly.template.sql").read_text(encoding="utf-8")

    def test_read_only(self):
        body = re.sub(r"/\*.*?\*/", "", self.text, flags=re.S)
        body = re.sub(r"--[^\n]*", "", body)
        self.assertIsNone(re.search(
            r"\b(INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|EXEC|EXECUTE|TRUNCATE|GRANT|INTO|DBCC)\b", body, re.I))
        self.assertNotIn("NOLOCK", body.upper())
        self.assertIn("IF DB_NAME() <> N'$(ExpectedDb)'", body)
        self.assertIn("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", body)

    def test_aggregates_only(self):
        body = re.sub(r"/\*.*?\*/", "", self.text, flags=re.S)
        for column in ("CompanyName", "DocNo", "Description", "Note", "Address", "Phone", "EmailAddress"):
            self.assertNotIn(column, body)


class RepoHygieneTests(unittest.TestCase):
    def test_runbook_names_every_failure_class_and_proof_step(self):
        text = (ROOT / "docs" / "autocount2-automation" / "arap_aging_runbook.md").read_text(encoding="utf-8")
        for needle in ("outstanding_mismatch", "unknown_knockoff_target", "self_check_failed", "unstable_snapshot",
                       "latest_publish_failed", "V1", "V2", "V3", "ap_payments_recorded", "Debtor Aging Report",
                       "Creditor Aging Report", "knockoff_target_types"):
            self.assertIn(needle, text)

    def test_new_files_are_ascii(self):
        for rel in ("scripts/autocount_arap_aging.py", "config/autocount_arap_aging.example.json",
                    "sql/autocount2/arap_aging_live_check.readonly.template.sql",
                    "docs/autocount2-automation/arap_aging_runbook.md"):
            (ROOT / rel).read_bytes().decode("ascii")


# ---------------------------------------------------------------------------------------------
# Fix round 1 (G2 on PR #322): blockers 1-5 and LATER L1-L3
# ---------------------------------------------------------------------------------------------


class ReadOnlyContractFix1Tests(unittest.TestCase):
    """L1: every keyword on its own (R04), the SELECT-start rule (R05), the import-time check (R06)."""

    KEYWORDS = (
        "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "CREATE", "TRUNCATE", "GRANT", "REVOKE", "DENY",
        "EXEC", "EXECUTE", "INTO", "BACKUP", "RESTORE", "DBCC", "SHUTDOWN", "KILL", "OPENROWSET", "OPENQUERY",
        "OPENDATASOURCE", "WAITFOR", "DECLARE", "SET", "USE", "WRITETEXT", "UPDATETEXT", "ENABLE TRIGGER",
        "DISABLE TRIGGER", "NEXT VALUE FOR", "RECEIVE", "END CONVERSATION", "RECONFIGURE", "CHECKPOINT",
        "SETUSER", "BEGIN TRAN", "BEGIN TRANSACTION", "XLOCK", "TABLOCKX", "UPDLOCK", "HOLDLOCK", "NOLOCK",
    )

    def test_each_keyword_is_rejected_on_its_own(self):
        for keyword in self.KEYWORDS:
            for probe in ("SELECT a FROM b %s c" % keyword, "SELECT a FROM b %s" % keyword.lower()):
                with self.subTest(probe=probe):
                    self.assertIsNotNone(arap.FORBIDDEN_SQL_RE.search(probe))
                    with self.assertRaises(arap.ConfigError):
                        arap.assert_read_only(probe)

    def test_g2_slip_through_forms_are_rejected(self):
        for probe in (
            "SELECT 1 FROM x WITH (XLOCK)", "SELECT 1 FROM x WITH (TABLOCKX)", "SELECT 1 FROM x WITH (UPDLOCK)",
            "SELECT 1 FROM x WITH (HOLDLOCK)", "SELECT NEXT VALUE FOR dbo.Seq", "WRITETEXT x.y @p 'z'",
            "UPDATETEXT x.y @p 0 0 'z'", "SELECT 1 BEGIN TRANSACTION", "SELECT 1 RECEIVE TOP (1) * FROM q",
            "SELECT 1 END CONVERSATION @h", "SELECT 1 RECONFIGURE", "SELECT 1 CHECKPOINT", "SELECT 1 SETUSER",
            "SELECT 1 DISABLE TRIGGER t ON x", "SELECT 1 ENABLE  TRIGGER t ON x",
        ):
            with self.subTest(probe=probe):
                with self.assertRaises(arap.ConfigError):
                    arap.assert_read_only(probe)

    def test_update_statement_without_set_is_rejected(self):
        # R04: no SET in the probe, so only the UPDATE keyword can catch it.
        self.assertIsNotNone(arap.FORBIDDEN_SQL_RE.search("UPDATE STATISTICS dbo.ARInvoice"))

    def test_non_select_start_is_rejected(self):
        # R05: no forbidden word, so only the SELECT-start rule rejects it.
        probe = "WITH c AS (SELECT 1 AS a) SELECT a FROM c"
        self.assertIsNone(arap.FORBIDDEN_SQL_RE.search(probe))
        with self.assertRaises(arap.ConfigError):
            arap.assert_read_only(probe)
        with self.assertRaises(arap.ConfigError):
            arap.assert_read_only("  VALUES (1)")

    def test_the_17_pinned_query_texts_still_pass_the_stricter_check(self):
        texts = arap.all_query_texts()
        self.assertEqual(len(texts), 17)
        self.assertEqual(set(texts), set(PINNED_QUERY_SHA256))
        for name, text in texts.items():
            with self.subTest(name=name):
                arap.assert_read_only(text)

    def test_import_time_check_rejects_a_bad_query_text(self):
        # R06: the module checks every query text when it is imported.
        import importlib.util

        spec = importlib.util.spec_from_file_location("arap_import_check", ROOT / "scripts" / "autocount_arap_aging.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(arap.stock, "IDENTITY_QUERY", "SELECT SUSER_SNAME() UPDATE STATISTICS dbo.Debtor"):
            with self.assertRaises(arap.ConfigError):
                spec.loader.exec_module(module)

    def test_connection_string_pins_the_database(self):
        # L2 (R12).
        settings = arap.validate_settings({
            "source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
            "output": {"archive_root": "C:\\EXAMPLE\\archive"},
        })
        parts = arap.build_connection_string(settings).split(";")
        self.assertIn("DATABASE=%s" % FAKE_DB, parts)
        self.assertIn("SERVER=%s" % FAKE_SERVER, parts)


class MoneyFix1Tests(TempDirTestCase):
    """Blockers 1-3: refunds, foreign-currency local amounts, due-today and week edges."""

    def base(self):
        data = empty_data()
        data["ar_parties"] = [party("DEBT-A01", "Example Trading A"), party("DEBT-A02", "Example Outdoor Club B")]
        data["ap_parties"] = [party("CRED-B01", "Example Supplier US", ccy="USD"), party("CRED-B02", "Example Supplier Local")]
        return data

    def test_refund_reduces_unapplied_payments_and_credit_notes(self):
        # Blocker 1 (B04).
        data = self.base()
        data["ar_payment"] = [payment(1, "DEBT-A01", "2026-11-02", "100.00", ko="30.00", refund="20.00")]
        data["ar_credit_note"] = [credit_note(2, "DEBT-A01", "2026-11-03", "200.00", ko="50.00", refund="30.00",
                                              ccy="USD", rate="1.35")]
        view = self.view(data)
        pm = item(view, "ar", "PM-0001")
        self.assertEqual((pm["original_doc"], pm["knocked_off_doc"], pm["outstanding_doc"]),
                         (D("-100.00"), D("-50.00"), D("-50.00")))
        self.assertEqual(pm["outstanding_local"], D("-50.00"))
        cn = item(view, "ar", "CN-0002")
        self.assertEqual((cn["original_doc"], cn["knocked_off_doc"], cn["outstanding_doc"]),
                         (D("-200.00"), D("-80.00"), D("-120.00")))
        self.assertEqual(cn["outstanding_local"], D("-162.00"))  # cents(120 x 1.35)
        self.assertEqual(view["ar"]["totals"]["unapplied"], D("-212.00"))

    def test_foreign_currency_local_amounts(self):
        # Blocker 2 (B05, B06, B07, B15).
        data = self.base()
        invoice = debit(11, "CRED-B01", "2026-10-01", "2026-11-15", "1000.00", out="600.00", ccy="USD", rate="1.30",
                        prefix="PI")
        invoice["LocalKnockedOffAmt"] = D("528.00")  # paid 400 USD at 1.32, not at the invoice rate
        data["ap_invoice"] = [invoice]
        data["ap_knockoffs"] = [knockoff("PX", 11, "400.00", "2026-10-20")]
        data["ap_credit_note"] = [credit_note(12, "CRED-B01", "2026-11-05", "50.00", ccy="USD", rate="1.40")]
        pay = payment(13, "CRED-B01", "2026-11-06", "500.00", ko="100.00", local_unapplied="536.00")
        pay.update(CurrencyCode="USD", LocalOriginalAmt=D("675.00"))
        data["ap_payment"] = [pay]
        view = self.view(data, self.settings(knockoff_target_types={"ap": {"PX": "PI"}}))
        pi = item(view, "ap", "PI-0011")
        self.assertEqual((pi["original_local"], pi["knocked_off_local"], pi["outstanding_local"]),
                         (D("1300.00"), D("528.00"), D("772.00")))
        self.assertEqual(pi["outstanding_doc"], D("600.00"))
        sc = item(view, "ap", "CN-0012")
        self.assertEqual(sc["doc_type"], "SC")
        self.assertEqual((sc["original_local"], sc["knocked_off_local"], sc["outstanding_local"]),
                         (D("-70.00"), D("0.00"), D("-70.00")))
        pv = item(view, "ap", "PM-0013")
        self.assertEqual(pv["doc_type"], "PV")
        self.assertEqual((pv["original_local"], pv["knocked_off_local"], pv["outstanding_local"]),
                         (D("-675.00"), D("-139.00"), D("-536.00")))
        self.assertEqual(pv["outstanding_doc"], D("-400.00"))
        self.assertEqual(view["ap"]["totals"]["outstanding"], D("772.00") - D("70.00") - D("536.00"))

    def test_due_today_is_current_and_not_overdue(self):
        # Blocker 3 (B03).
        data = self.base()
        data["ar_invoice"] = [debit(21, "DEBT-A01", "2026-11-17", "2026-11-17", "40.00"),
                              debit(22, "DEBT-A02", "2026-11-16", "2026-11-16", "15.00")]
        view = self.view(data)
        today = item(view, "ar", "IV-0021")
        self.assertEqual((today["days_past_due"], today["bucket_due"]), (0, "current"))
        self.assertEqual(view["ar"]["parties"]["DEBT-A01"]["totals"]["overdue"], D(0))
        self.assertIsNone(view["ar"]["parties"]["DEBT-A01"]["totals"]["oldest_overdue_days"])
        self.assertEqual(view["ar"]["totals"]["overdue"], D("15.00"))

    def test_ap_schedule_edges(self):
        # Blocker 3 (B12, B13): this week Mon 16 - Sun 22 Nov, next week Mon 23 - Sun 29 Nov.
        data = self.base()
        data["ap_invoice"] = [
            debit(31, "CRED-B02", "2026-11-01", "2026-11-16", "1.00", prefix="PI"),   # yesterday: overdue
            debit(32, "CRED-B02", "2026-11-01", "2026-11-17", "2.00", prefix="PI"),   # today: this week
            debit(33, "CRED-B02", "2026-11-01", "2026-11-22", "4.00", prefix="PI"),   # Sunday: this week
            debit(34, "CRED-B02", "2026-11-01", "2026-11-23", "8.00", prefix="PI"),   # Monday: next week
            debit(35, "CRED-B02", "2026-11-01", "2026-11-29", "16.00", prefix="PI"),  # next Sunday: next week
            debit(36, "CRED-B02", "2026-11-01", "2026-11-30", "32.00", prefix="PI"),  # Monday after: later
        ]
        bands = {name: band["total"] for name, band in self.view(data)["ap"]["schedule"]["bands"].items()}
        self.assertEqual(bands, {"overdue": D("1.00"), "this_week": D("6.00"), "next_week": D("24.00"),
                                 "later": D("32.00")})


class SelfCheckFix1Tests(TempDirTestCase):
    def test_over_applied_payment_fails_row_identity_and_publishes_nothing(self):
        # Blocker 4 (C03).
        archive = self.tmp / "archive"
        self.assertEqual(self.run_export()["status"], "success")
        before = {n: (archive / n).read_bytes() for n in arap.PUBLISHED_FILES}
        data = fixture_data()
        data["ar_payment"].append(payment(220, "DEBT-A02", "2026-11-12", "100.00", ko="120.00"))
        result = self.run_export(data)
        self.assertEqual(result["failure_class"], "self_check_failed")
        self.assertIn("row_identity", result["message"])
        self.assertEqual({n: (archive / n).read_bytes() for n in arap.PUBLISHED_FILES}, before)

    def test_party_csv_amounts_are_checked_against_the_feed(self):
        # L3 (C02): same row count, one party amount off by a cent.
        archive = self.tmp / "archive"
        self.assertEqual(self.run_export()["status"], "success")
        before = {n: (archive / n).read_bytes() for n in arap.PUBLISHED_FILES}
        real = arap.render_parties

        def skewed(parties, side, labels, as_at_local):
            parties = copy.deepcopy(parties)
            first = sorted(parties)[0]
            parties[first]["totals"]["outstanding"] += D("0.01")
            return real(parties, side, labels, as_at_local)

        with mock.patch.object(arap, "render_parties", skewed):
            result = self.run_export()
        self.assertEqual(result["failure_class"], "self_check_failed")
        self.assertIn("feed_matches_csv", result["message"])
        self.assertEqual({n: (archive / n).read_bytes() for n in arap.PUBLISHED_FILES}, before)


PRIVATE_VALUES = (
    # Amounts with their decimal point: bare digit runs also occur inside hex hashes and run ids.
    "DEBT-", "CRED-", "Example ", "IV-0", "PI-0", "PM-0", "CN-0", "DN-0", "DP-0", "REF-", "4321.0", "8765.4",
    "1234.5", "1200.0", "1335.0", "150000.0", "95.5", FAKE_SERVER, "FAKE-SQL-HOST", FAKE_LOGIN, FAKE_USER,
    "Trusted_Connection",
)


class PrivacyFix1Tests(TempDirTestCase):
    """Blocker 5 (V04, V07, V08): every warning and every count-only failure carries no private value."""

    def assert_clean(self, *texts):
        for text in texts:
            for value in PRIVATE_VALUES:
                self.assertNotIn(value, text)

    def warning_data(self):
        data = fixture_data()
        data["ar_invoice"].append(debit(111, "DEBT-A02", "2026-11-10", "2026-11-01", "4321.09"))   # due before doc
        data["ar_invoice"].append(debit(112, "DEBT-A02", "2026-11-01", "2026-11-30", "100.00", out="60.00"))
        data["ar_knockoffs"].append(knockoff("RI", 112, "40.00", "2026-11-25"))                      # payer after run
        data["ar_knockoffs"].append(knockoff("RI", 107, "8765.43", "2026-11-02"))                    # cancelled target
        data["ar_debit_note"] = [debit(113, "DEBT-A01", "2026-11-01", "2026-11-15", "1234.56", prefix="DN")]  # guess off
        data["ar_payment"].append(payment(214, "DEBT-A02", "2026-11-10", "30.00", local_unapplied="0"))  # disagree
        return data

    def test_every_warning_path_is_counts_only(self):
        config = self.tmp / "settings.json"
        config.write_text(json.dumps({
            "source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
            "identity": {"allow_privileged_identity": True},
            "output": {"archive_root": str(self.tmp / "archive"), "delivery_root": str(self.tmp / "drive")},
            "parties": {"online_shop_debtor_codes": ["DEBT-WEB"]},
        }), encoding="utf-8")
        privileged = {"login": FAKE_LOGIN, "user": FAKE_USER, "database": FAKE_DB, "is_sysadmin": 1, "is_db_owner": 0}
        source = FakeSource(sessions=[FakeSession([self.warning_data()], identity=privileged)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = arap.main(["--config", str(config)], source=source, environ=ENV, clock=lambda: FIXED_NOW)
        self.assertEqual(code, arap.EXIT_OK)
        manifest_text = (self.tmp / "archive" / arap.LATEST_MANIFEST_NAME).read_text(encoding="utf-8")
        warnings = json.loads(manifest_text)["warnings"]
        for needle in ("Privileged", "due date before the document date", "guess_sources", "cancelled document",
                       "dated after the run date", "disagree on zero"):
            self.assertTrue(any(needle in w for w in warnings), needle)
        status_texts = [(root / arap.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8")
                        for root in (self.tmp / "archive", self.tmp / "drive")]
        self.assert_clean(out.getvalue(), manifest_text, json.dumps(warnings), *status_texts)

    def test_view_warnings_are_counts_only(self):
        view = self.view(self.warning_data())
        self.assertGreaterEqual(len(view["warnings"]), 5)
        self.assert_clean(json.dumps(view["warnings"]), json.dumps(view["checks"]), json.dumps(view["counts"]))

    def failing_runs(self):
        missing = fixture_data()
        missing["ar_invoice"].append(debit(115, "DEBT-ZZ9", "2026-11-01", "2026-11-01", "4321.09"))
        orphan = fixture_data()
        orphan["ar_knockoffs"].append(knockoff("RI", 9999, "8765.43", "2026-11-01"))
        unknown = fixture_data()
        unknown["ap_knockoffs"] = [knockoff("PI", 501, "8765.43", "2026-11-01")]
        mismatch = fixture_data()
        mismatch["ar_invoice"][0]["Outstanding"] = D("4321.09")
        unstable = [FakeSession([fixture_data()], counters=[full_counters(i), full_counters(i + 1)]) for i in range(3)]
        return {
            "self_check_failed": FakeSource(missing),
            "outstanding_mismatch": FakeSource(orphan),
            "unknown_knockoff_target": FakeSource(unknown),
            "outstanding_mismatch ": FakeSource(mismatch),
            "unstable_snapshot": FakeSource(sessions=unstable),
        }

    def test_every_count_only_failure_path(self):
        for expected, source in self.failing_runs().items():
            with self.subTest(failure=expected.strip()):
                settings = self.settings(delivery=True)
                result = self.run_export(settings=settings, source=source)
                self.assertEqual(result["failure_class"], expected.strip())
                texts = [json.dumps({k: v for k, v in result.items() if k != "_manifest"})]
                for root in (self.tmp / "archive", self.tmp / "drive"):
                    texts.append((root / arap.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
                self.assert_clean(*texts)


# ---------------------------------------------------------------------------------------------
# Slice A2: Task Scheduler installer for \XB\AC2 ARAP Aging (copy of the stock installer pattern)
# ---------------------------------------------------------------------------------------------

INSTALLER_PATH = ROOT / "scripts" / "install_autocount_arap_aging_task.ps1"
STOCK_INSTALLER_PATH = ROOT / "scripts" / "install_autocount_stock_balance_hourly_task.ps1"


def strip_ps_comments(text):
    code = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))


class ArapInstallerStaticTests(unittest.TestCase):
    def setUp(self):
        self.text = INSTALLER_PATH.read_text(encoding="utf-8")
        self.code = strip_ps_comments(self.text)

    def test_ascii(self):
        INSTALLER_PATH.read_bytes().decode("ascii")

    def test_task_name_folder_and_script(self):
        self.assertRegex(self.text, r'\[string\]\$TaskName = "AC2 ARAP Aging"\s*,')
        self.assertRegex(self.text, r'\[string\]\$TaskPath = "\\XB\\"\s*,')
        self.assertIn('Join-Path $RepoRoot "scripts\\autocount_arap_aging.py"', self.code)
        self.assertNotIn("stock_balance_hourly.py", self.code)
        self.assertIn("separate owner OK", self.text)

    def test_static_contract(self):
        self.assertIn("-MultipleInstances IgnoreNew", self.code)
        self.assertNotIn("-Force", self.text)
        self.assertIn("-ExecutionTimeLimit (New-TimeSpan -Minutes 15)", self.code)
        self.assertEqual(self.code.count("-RunLevel Limited"), 2, "both registration paths")
        self.assertIn("-RepetitionInterval (New-TimeSpan -Hours 1)", self.code)
        self.assertNotIn("GetCurrent()", self.text, "no default Windows user")
        self.assertRegex(self.text, r"\[Parameter\(Mandatory = \$true\)\]\s*\[string\]\$UserId")
        self.assertRegex(self.text, r"\[Parameter\(Mandatory = \$true\)\]\s*\[string\]\$ConfigPath")
        self.assertRegex(self.text, r"\[Parameter\(Mandatory = \$true\)\]\s*\[string\]\$PythonExe\s*,")
        self.assertIn("[System.IO.Path]::IsPathRooted($PythonExe)", self.code)
        self.assertIn("Test-Path -LiteralPath $PythonExe -PathType Leaf", self.code)

    def test_execution_limit_matches_the_shared_lock_rules(self):
        limit = re.search(r"-ExecutionTimeLimit \(New-TimeSpan -Minutes (\d+)\)", self.code)
        self.assertEqual(int(limit.group(1)), arap.stock.TASK_EXECUTION_LIMIT_MINUTES)
        self.assertGreater(arap.stock.LOCK_STALE_AFTER_MINUTES, arap.stock.TASK_EXECUTION_LIMIT_MINUTES)

    def test_single_hourly_trigger_without_daily_start(self):
        self.assertEqual(self.code.count("New-ScheduledTaskTrigger"), 1, "exactly one trigger")
        self.assertIn(
            "$trigger = New-ScheduledTaskTrigger -Once -At $startAt -RepetitionInterval (New-TimeSpan -Hours 1)\n",
            self.code,
        )
        self.assertIn("$trigger.Repetition.StopAtDurationEnd = $false", self.code)
        for forbidden in ("-Daily", "-RepetitionDuration", "MaxValue", "StopAtDurationEnd = $true"):
            self.assertNotIn(forbidden, self.code)
        self.assertEqual(self.code.count("-Trigger $trigger "), 2, "both registration paths use the one trigger")
        self.assertLess(self.code.index('$repeatEvery -cne "PT1H"'), self.code.index("if ($DryRun) {"))

    def test_run_minute_and_logon_type_come_from_the_arap_settings_check(self):
        self.assertIn("& $PythonExe $scriptPath --config $ConfigPath --dry-run-config", self.code)
        self.assertIn("$RunMinute = $validated.run_minute", self.code)
        self.assertIn("$RunMinute -lt 0 -or $RunMinute -gt 59", self.code)
        self.assertIn("AddMinutes($RunMinute)", self.code)
        self.assertNotIn("$LogonType =", self.text, "no free LogonType parameter")
        self.assertNotIn("[int]$RunMinute", self.text.split("$ErrorActionPreference", 1)[0], "no RunMinute parameter")
        self.assertIn("$validated.scheduled_logon_type", self.code)
        self.assertIn('($authMode -ceq "windows_integrated") -and ($logonType -ceq "S4U")', self.code)
        self.assertIn('($authMode -ceq "sql_login") -and ($logonType -ceq "Password")', self.code)
        self.assertIn("-LogonType S4U", self.code)
        self.assertEqual(sorted(arap.SUPPORTED_LOGON_PAIRS.items()), [("sql_login", "Password"), ("windows_integrated", "S4U")])

    def test_password_logon_prompts_and_never_echoes(self):
        self.assertIn("Get-Credential -UserName $UserId", self.code)
        self.assertIn("-User $UserId", self.code)
        self.assertIn("-Password $credential.GetNetworkCredential().Password", self.code)
        for forbidden in ("ConvertFrom-SecureString", "Out-File", "Set-Content", "Add-Content", "Export-Clixml",
                          "Start-Transcript", "Write-Output", "-Verbose"):
            self.assertNotIn(forbidden, self.text)
        for line in self.text.splitlines():
            if "Write-Host" in line:
                self.assertNotIn("$credential", line)
                self.assertNotIn("Password", line.replace("LogonType Password", ""))
        self.assertEqual(self.text.count("GetNetworkCredential()"), 1)

    def test_repo_root_is_worked_out_in_the_body(self):
        # $PSScriptRoot is empty in param() defaults under Windows PowerShell 5.1.
        block = re.search(r"^param\((.*?)^\)", self.text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(block)
        self.assertNotIn("$PSScriptRoot", block.group(1))
        self.assertRegex(block.group(1), r'\[string\]\$RepoRoot = ""\s*,')
        body = self.text[block.end():]
        self.assertIn("if ([string]::IsNullOrWhiteSpace($RepoRoot)) {", body)
        self.assertIn('$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path', body)

    def test_refuses_same_name_in_folder_or_root(self):
        folder = 'Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue'
        root = 'Get-ScheduledTask -TaskName $TaskName -TaskPath "\\" -ErrorAction SilentlyContinue'
        self.assertIn(folder, self.code)
        self.assertIn(root, self.code)
        after_root = self.code[self.code.index(root):]
        self.assertLess(after_root.index("throw \"Refusing:"), after_root.index("& $PythonExe"))
        self.assertNotIn("Write-Warning", self.code, "a root task is refused, not warned about")
        self.assertNotIn("Unregister-ScheduledTask", self.code)
        self.assertEqual(self.text.count("Register-ScheduledTask `"), 2)
        self.assertEqual(self.text.count("-TaskPath $TaskPath `"), 2, "both registrations use the folder")

    def test_dry_run_stops_before_prompt_and_register(self):
        dry = self.text.index("if ($DryRun) {")
        self.assertLess(self.text.index("--dry-run-config"), dry)
        self.assertLess(dry, self.text.index("Get-Credential -UserName"))
        self.assertLess(dry, self.text.index("Register-ScheduledTask `"))
        self.assertIn("Nothing was registered.", self.text[dry:self.text.index("Register-ScheduledTask `")])

    def test_supervised_run_and_off_switch_are_named(self):
        self.assertIn("Start-ScheduledTask", self.text)
        self.assertIn("last_run_status.json", self.text)
        self.assertIn("Disable-ScheduledTask", self.text)

    def test_differs_from_the_stock_installer_only_where_intended(self):
        # Every executable line of the stock installer is kept, except the named differences.
        stock = [line.strip() for line in strip_ps_comments(STOCK_INSTALLER_PATH.read_text(encoding="utf-8")).splitlines()]
        mine = {line.strip() for line in self.code.splitlines()}
        intended = ("$TaskName", "$scriptPath = Join-Path", "Write-Warning", "$description =", "Write-Host \"Next")
        dropped = [line for line in stock if line and line not in mine and not any(k in line for k in intended)]
        self.assertEqual(dropped, [])

    def test_powershell_parse_only(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("Neither pwsh nor powershell is available for the parse-only check")
        command = (
            "$errs = $null; "
            "[System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$null, [ref]$errs) | Out-Null; "
            "if (@($errs).Count -gt 0) { $errs | ForEach-Object { Write-Output $_.Message }; exit 1 } else { exit 0 }"
        ) % str(INSTALLER_PATH).replace("'", "''")
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runbook_has_the_a2_section(self):
        text = (ROOT / "docs" / "autocount2-automation" / "arap_aging_runbook.md").read_text(encoding="utf-8")
        section = text.split("## A2: hourly task", 1)[1].split("\n## ", 1)[0]
        for needle in ("install_autocount_arap_aging_task.ps1", "-DryRun", "\\XB\\", "AC2 ARAP Aging",
                       "Start-ScheduledTask", "--check-latest", "Disable-ScheduledTask", ":07/:22/:37/:52",
                       "owner"):
            self.assertIn(needle, section)


class ArapInstallerPowerShell51Tests(TempDirTestCase):
    """Runs the installer under Windows PowerShell 5.1 with -File and -DryRun only.

    -DryRun stops before the password prompt and Register-ScheduledTask, and the task name is a
    test-only name, so nothing is ever registered. No stand-in for any ScheduledTasks command is
    used (Windows PowerShell 5.1 autoloads the real module over a stand-in). The refusal tests
    only read Task Scheduler (Get-ScheduledTask) to borrow the name of a task that already exists.
    A stub pyodbc module on PYTHONPATH stands in for the driver (the installer only imports it).
    Fake user name and fake settings only.
    """

    TASK_NAME = "XB AC2 ARAP installer test (never registered)"

    def setUp(self):
        super().setUp()
        if sys.platform != "win32":
            self.skipTest("Windows PowerShell 5.1 runs only on Windows")
        self.shell = shutil.which("powershell")
        if self.shell is None:
            self.skipTest("Windows PowerShell is not installed")
        stub = self.tmp / "stub"
        stub.mkdir()
        (stub / "pyodbc.py").write_text("# test stub: the installer only imports pyodbc\n", encoding="utf-8")
        # Keep any PYTHONPATH already set (a local tzdata copy, for example) after the stub.
        self.env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in (str(stub), os.environ.get("PYTHONPATH")) if p))
        self.config = self.write_config()

    def write_config(self, name="settings.local.json", **overrides):
        raw = {
            "source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
            "output": {"archive_root": str(self.tmp / "archive"), "delivery_root": None},
        }
        for key, value in overrides.items():
            raw.setdefault(key, {}).update(value)
        path = self.tmp / name
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def run_installer(self, *extra, config=None, task_name=None, python_exe=None):
        args = [self.shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(INSTALLER_PATH), "-UserId", "EXAMPLE-PC\\example-user",
                "-ConfigPath", str(config or self.config), "-PythonExe", python_exe or sys.executable,
                "-TaskName", task_name or self.TASK_NAME, "-DryRun", *extra]
        done = subprocess.run(args, capture_output=True, text=True, timeout=180, env=self.env, cwd=str(self.tmp))
        return done.returncode, done.stdout + done.stderr

    def existing_task(self, where):
        """Path and name of a task that already exists on this host (read-only lookup), or None."""
        command = (
            "Get-ScheduledTask -TaskPath '%s' -ErrorAction SilentlyContinue | "
            "Where-Object { $_.TaskPath -cmatch '\\A(\\\\[A-Za-z0-9_-]+)*\\\\\\z' -and "
            "$_.TaskName -cmatch '\\A[A-Za-z0-9_-][A-Za-z0-9 ._-]*\\z' } | "
            "Select-Object -First 1 | ForEach-Object { $_.TaskPath + '|' + $_.TaskName }"
        ) % where
        done = subprocess.run([self.shell, "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, text=True, timeout=180)
        line = done.stdout.strip()
        return tuple(line.split("|", 1)) if "|" in line else None

    def test_runs_under_windows_powershell_5_1(self):
        done = subprocess.run([self.shell, "-NoProfile", "-NonInteractive", "-Command",
                               "$PSVersionTable.PSVersion.Major"],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.stdout.strip(), "5", done.stdout + done.stderr)

    def test_dry_run_default_settings(self):
        code, out = self.run_installer()
        self.assertEqual(code, 0, out)
        self.assertIn("Dry run: would register \\XB\\" + self.TASK_NAME, out)
        self.assertIn("Repo root: " + str(ROOT), out)
        self.assertIn("Runs hourly at minute 30, IgnoreNew, 15 minute limit, RunLevel Limited, "
                      "LogonType S4U (windows_integrated).", out)
        # The trigger object is really built on this host: one trigger, hourly from 00:30, no end.
        self.assertIn("Trigger: one trigger, starts 00:30, repeats every PT1H, no end", out)
        self.assertIn("Nothing was registered.", out)

    def test_dry_run_with_empty_repo_root_finds_this_clone(self):
        code, out = self.run_installer("-RepoRoot", "")
        self.assertEqual(code, 0, out)
        self.assertIn("Repo root: " + str(ROOT), out)

    def test_dry_run_takes_the_minute_from_the_settings(self):
        config = self.write_config("minute.local.json", schedule={"run_minute": 45})
        code, out = self.run_installer(config=config)
        self.assertEqual(code, 0, out)
        self.assertIn("Runs hourly at minute 45,", out)
        self.assertIn("starts 00:45, repeats every PT1H", out)

    def test_dry_run_password_pair_never_prompts(self):
        # Windows PowerShell 5.1 does not make Get-Credential fail under -NonInteractive: it waits
        # on the prompt. A regression past the dry run shows as a timeout, not a quick failure.
        config = self.write_config(
            "sql.local.json",
            identity={"auth_mode": "sql_login", "sql_login": "fake_sql_reader", "credential_target": "FAKE-CRED-TARGET"},
            schedule={"logon_type": "Password"},
        )
        code, out = self.run_installer(config=config)
        self.assertEqual(code, 0, out)
        self.assertIn("LogonType Password (sql_login)", out)
        self.assertIn("Nothing was registered.", out)

    def test_bad_settings_are_refused(self):
        cases = {
            "minute": {"schedule": {"run_minute": 75}},
            "pair_sql_s4u": {"identity": {"auth_mode": "sql_login", "sql_login": "fake_sql_reader",
                                          "credential_target": "FAKE-CRED-TARGET"}},
            "pair_windows_password": {"schedule": {"logon_type": "Password"}},
            "frequency": {"schedule": {"frequency": "daily"}},
            "no_database": {"source": {"expected_database": ""}},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                config = self.write_config(label + ".local.json", **overrides)
                code, out = self.run_installer(config=config)
                self.assertNotEqual(code, 0, out)
                self.assertIn("The settings are not valid for this python.exe", out)
                self.assertNotIn("Dry run:", out)

    def test_missing_settings_file_and_relative_python_are_refused(self):
        code, out = self.run_installer(config=self.tmp / "missing.local.json")
        self.assertNotEqual(code, 0, out)
        self.assertIn("Local settings file not found.", out)
        code, out = self.run_installer(python_exe="python.exe")
        self.assertNotEqual(code, 0, out)
        self.assertIn("PythonExe must be the full path", out)
        self.assertNotIn("Dry run:", out)

    def test_bad_task_path_is_refused(self):
        for bad in ("", "\\", "XB", "\\XB", "XB\\", "\\XB\\\\", "\\X;B\\", "\\..\\", "\\XB/\\", "\\ XB\\"):
            with self.subTest(task_path=bad):
                code, out = self.run_installer("-TaskPath", bad)
                self.assertNotEqual(code, 0, out)
                self.assertIn("TaskPath must look like", out)
                self.assertNotIn("Dry run:", out)

    def test_existing_task_name_in_the_folder_is_refused(self):
        # Borrow a task Windows itself ships (read-only lookup); the dry run refuses before any check.
        found = self.existing_task("\\Microsoft\\Windows\\*")
        if found is None:
            self.skipTest("No built-in Windows task with a plain folder and name found on this host")
        path, name = found
        code, out = self.run_installer("-TaskPath", path, task_name=name)
        self.assertNotEqual(code, 0, out)
        self.assertIn("Refusing: a scheduled task with this name already exists in " + path, out)
        self.assertNotIn("Dry run:", out)

    def test_existing_task_name_in_the_root_is_refused(self):
        found = self.existing_task("\\")
        if found is None:
            self.skipTest("No task in the Task Scheduler root on this host")
        _path, name = found
        code, out = self.run_installer("-TaskPath", "\\XB ARAP installer test never created\\", task_name=name)
        self.assertNotEqual(code, 0, out)
        self.assertIn("already exists in the Task Scheduler root", out)
        self.assertNotIn("Dry run:", out)


if __name__ == "__main__":
    unittest.main()
