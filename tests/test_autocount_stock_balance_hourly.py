"""Offline tests for the hourly AC2 stock balance export (#245).

In-memory fakes and temporary folders only. No SQL Server, AutoCount, Drive, n8n or Task
Scheduler is touched. Every identity, host, item code and location code here is synthetic.
No fixture files: data is built in memory.
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
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_stock_balance_hourly as hourly  # noqa: E402

SGT = ZoneInfo("Asia/Singapore")
FIXED_NOW = datetime(2026, 10, 23, 14, 0, 5, tzinfo=SGT)
SQL_NOW = datetime(2026, 10, 23, 14, 0, 6, tzinfo=SGT)

FAKE_DB = "AED_EXAMPLE_BOOK"
FAKE_LOGIN = "fake_reader_login"
FAKE_USER = "fake_reader_user"
FAKE_SERVER = "FAKE-SQL-HOST\\FAKEINST"
FAKE_CRED_TARGET = "FAKE-CRED-TARGET"
FAKE_SQL_LOGIN = "fake_sql_reader"
ENV = {
    "AUTOCOUNT_EXPECTED_SQL_LOGIN": FAKE_LOGIN,
    "AUTOCOUNT_EXPECTED_SQL_USER": FAKE_USER,
    "AUTOCOUNT_EXPECTED_SQL_DATABASE": FAKE_DB,
}


def observed_identity(**overrides):
    value = {
        "login": FAKE_LOGIN,
        "user": FAKE_USER,
        "database": FAKE_DB,
        "is_sysadmin": 0,
        "is_db_owner": 0,
    }
    value.update(overrides)
    return value


def bal(item, loc, qty, uom="PCS", base="PCS", active="T", stock="T", desc="Fake item",
        loc_desc="Fake location", batch=0, null_rows=0, rows=1, item_type="FAKE-BRAND"):
    return {
        "ItemCode": item,
        "UOM": uom,
        "Location": loc,
        "BalQty": None if qty is None else Decimal(str(qty)),
        "SourceRows": rows,
        "NullBalQtyRows": null_rows,
        "NonEmptyBatchRows": batch,
        "ItemDescription": desc,
        "ItemType": item_type,
        "IsActive": active,
        "StockControl": stock,
        "BaseUOM": base,
        "LocationDescription": loc_desc,
    }


def no_row_item(item, base="PCS"):
    return {
        "ItemCode": item,
        "ItemDescription": "Fake item without rows",
        "ItemType": "FAKE-BRAND",
        "IsActive": "T",
        "StockControl": "T",
        "BaseUOM": base,
    }


# Default business day of the fake ledger rows that back the fake balances.
LEDGER_DAY = date(2026, 10, 1)


def led(item, loc, qty, day=LEDGER_DAY, uom="PCS", rows=1, batch=0, null_rows=0):
    return {
        "ItemCode": item,
        "UOM": uom,
        "Location": loc,
        "DocDay": day,
        "Qty": Decimal(str(qty)),
        "LedgerRows": rows,
        "NullQtyRows": null_rows,
        "NonEmptyBatchRows": batch,
    }


def master(item, base="PCS", active="T", stock="T", desc="Fake item", item_type="FAKE-BRAND", group="FAKE-GRP",
           brand="FAKE-BRD"):
    return {
        "ItemCode": item,
        "ItemDescription": desc,
        "ItemType": item_type,
        "IsActive": active,
        "StockControl": stock,
        "BaseUOM": base,
        "ItemGroup": group,
        "ItemBrand": brand,
    }


def item_uom(item, uom="PCS", barcode=None, reorder=None, min_qty=None):
    return {
        "ItemCode": item,
        "UOM": uom,
        "BarCode": barcode,
        "ReOLevel": None if reorder is None else Decimal(str(reorder)),
        "MinQty": None if min_qty is None else Decimal(str(min_qty)),
    }


def default_balances():
    return [
        bal("FAKE-0001", "LOC-A", "5.00000000"),
        bal("FAKE-0001", "LOC-B", "-2.00000000", loc_desc="Fake outlet B"),
        bal("FAKE-0002", "LOC-A", "0.00000000"),
        bal("FAKE-0003", "LOC-A", "0.00000000", active="F"),
        bal("FAKE-0004", "LOC-GIT", "3.50000000"),
    ]


class FakeSession:
    def __init__(self, source):
        self.source = source
        self.pair = source.changecounts.pop(0) if source.changecounts else (7, 7)
        self.cc_calls = 0
        self.lock_timeout = bool(source.lock_timeouts and source.lock_timeouts.pop(0))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.source.closed += 1

    def read_identity(self):
        self.source.identity_reads += 1
        return dict(self.source.identity)

    def read_change_count(self):
        value = self.pair[min(self.cc_calls, 1)]
        self.cc_calls += 1
        return value

    def read_sql_time(self):
        return self.source.sql_now

    def read_balances(self):
        if self.lock_timeout:
            raise hourly.SourceLockTimeout("Lock timeout while reading")
        return copy.deepcopy(self.source.balances)

    def read_multi_uom_item_count(self):
        return self.source.multi_uom

    def read_items_without_rows(self):
        self.source.no_row_reads += 1
        return copy.deepcopy(self.source.no_row_items)

    def read_ledger(self):
        self.source.ledger_reads += 1
        if self.source.ledger is not None:
            return copy.deepcopy(self.source.ledger)
        # By default the ledger backs every fake balance exactly, on one early business day.
        return [
            led(b["ItemCode"], b["Location"], b["BalQty"], uom=b["UOM"])
            for b in self.source.balances
            if b["BalQty"] is not None
        ]

    def read_item_master(self):
        if self.source.item_master is not None:
            return copy.deepcopy(self.source.item_master)
        items = {}
        for b in self.source.balances:
            items.setdefault(b["ItemCode"], master(b["ItemCode"], base=b["BaseUOM"], active=b["IsActive"],
                                                   stock=b["StockControl"], desc=b["ItemDescription"],
                                                   item_type=b["ItemType"]))
        for item in self.source.no_row_items:
            items.setdefault(item["ItemCode"], dict(item))
        return list(items.values())

    def read_locations(self):
        if self.source.locations is not None:
            return copy.deepcopy(self.source.locations)
        codes = {}
        for b in self.source.balances:
            codes.setdefault(b["Location"], b["LocationDescription"])
        return [{"Location": code, "LocationDescription": desc} for code, desc in codes.items()]

    def read_item_uoms(self):
        return copy.deepcopy(self.source.item_uoms)

    def read_sync(self):
        if self.source.sync_error:
            raise hourly.SyncReadError("SyncRegistration read failed")
        return copy.deepcopy(self.source.sync_rows)


class FakeSource:
    def __init__(self, balances=None, no_row_items=None, sync_rows=None, identity=None,
                 changecounts=None, multi_uom=0, lock_timeouts=None, sync_error=False, ledger=None,
                 item_master=None, locations=None, item_uoms=None, sql_now=SQL_NOW):
        self.sql_now = sql_now
        self.balances = default_balances() if balances is None else balances
        self.ledger = ledger
        self.item_master = item_master
        self.locations = locations
        self.item_uoms = list(item_uoms or [])
        self.ledger_reads = 0
        self.no_row_items = [no_row_item("FAKE-0009")] if no_row_items is None else no_row_items
        self.sync_rows = (
            [{"Location": "LOC-A", "LastSyncTime": datetime(2026, 10, 23, 13, 50, 0)}]
            if sync_rows is None else sync_rows
        )
        self.identity = observed_identity() if identity is None else identity
        self.changecounts = list(changecounts or [])
        self.multi_uom = multi_uom
        self.lock_timeouts = list(lock_timeouts or [])
        self.sync_error = sync_error
        self.sessions = 0
        self.closed = 0
        self.identity_reads = 0
        self.no_row_reads = 0

    def open_session(self):
        self.sessions += 1
        return FakeSession(self)


def raw_settings(tmp, **overrides):
    raw = {
        "source": {"server": FAKE_SERVER, "expected_database": FAKE_DB},
        "location_labels": {"LOC-A": "LOC-A - Example outlet", "LOC-GIT": "LOC-GIT - Example in transit"},
        "output": {"archive_root": str(Path(tmp) / "archive"), "delivery_root": None},
        "read": {"retry_backoff_seconds": 0},
        # Three close days before FIXED_NOW keep each test run small.
        "daily_close": {"backfill_start": "2026-10-20"},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = dict(raw[key], **value)
        else:
            raw[key] = value
    return raw


def settings_for(tmp, **overrides):
    return hourly.validate_settings(raw_settings(tmp, **overrides))


def clock_at(value=FIXED_NOW):
    return lambda: value


def run(settings, source=None, environ=None, clock=None):
    return hourly.run_hourly(
        settings,
        source=source or FakeSource(),
        environ=ENV if environ is None else environ,
        clock=clock or clock_at(),
        sleep=lambda seconds: None,
    )


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def tree_bytes(root):
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xb245_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @property
    def archive(self):
        return Path(self.tmp) / "archive"

    @property
    def delivery(self):
        return Path(self.tmp) / "drive_sync"

    def run_dir(self, manifest, root=None):
        return Path(root or self.archive) / manifest["hourly_folder"]


# ---------------------------------------------------------------------------------------------


class ConfigValidationTests(TempDirTestCase):
    def assert_config_error(self, raw, fragment):
        with self.assertRaises(hourly.ConfigError) as ctx:
            hourly.validate_settings(raw)
        self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    def test_example_config_is_valid_and_fake(self):
        path = ROOT / "config" / "autocount_stock_balance_hourly.example.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        settings = hourly.validate_settings(raw)
        self.assertEqual(settings["source"]["expected_database"], FAKE_DB)
        self.assertEqual(settings["timezone"], "Asia/Singapore")
        self.assertEqual(settings["schedule"]["run_minute"], 0)
        self.assertEqual(settings["schedule"]["logon_type"], "S4U")
        self.assertEqual(settings["stale_after_minutes"], 70)
        self.assertEqual(settings["rows"]["zero_row_mode"], "keep_rows_and_add_items_without_rows")
        self.assertFalse(settings["identity"]["allow_privileged_identity"])
        self.assertEqual(settings["output"]["columns"], hourly.DEFAULT_COLUMNS)
        self.assertEqual(settings["output"]["large_balance_threshold"], 100000)
        for code in raw["location_labels"]:
            self.assertTrue(code.startswith("LOC-"), "example labels must use fake codes")
        self.assertIn("EXAMPLE", raw["output"]["delivery_root"])
        # Drive folder naming: <Feed name (cadence)>, daily since round 6 (no hourly folders in Drive).
        self.assertTrue(raw["output"]["delivery_root"].endswith("AC2 Stock Balance (daily)"))
        self.assertNotIn("hourly", raw["output"]["delivery_root"].lower())
        self.assertEqual(settings["daily_close"], {"backfill_start": "2026-08-01", "restate_days": 7})
        self.assertFalse(settings["retention"]["prune_enabled"], "pruning is switched on at the VM step only")
        self.assertTrue(settings["rows"]["exclude_non_stock_items"])

    def test_defaults_apply(self):
        settings = settings_for(self.tmp)
        self.assertEqual(settings["source"]["allowed_databases"], [FAKE_DB])
        self.assertEqual(settings["source"]["sync_registration_time_column"], "LastFinishSyncTime")
        self.assertEqual(settings["read"]["lock_timeout_ms"], 5000)
        self.assertEqual(settings["read"]["query_timeout_seconds"], 60)
        self.assertEqual(settings["read"]["max_attempts"], 3)
        self.assertEqual(settings["rows"]["uom_guard"], "fail")
        self.assertTrue(settings["rows"]["drop_zero_rows_for_inactive_items"])
        self.assertEqual(settings["identity"]["auth_mode"], "windows_integrated")

    def test_bad_sync_column_fails(self):
        message = self.assert_config_error(
            raw_settings(self.tmp, source={"sync_registration_time_column": "HostName; DROP"}),
            "source.sync_registration_time_column",
        )
        self.assertNotIn("DROP", message)

    def test_both_sync_columns_are_allowed(self):
        for column in ("LastFinishSyncTime", "LastTransFinishSyncTime"):
            settings = settings_for(self.tmp, source={"sync_registration_time_column": column})
            self.assertIn("[%s]" % column, hourly.sync_query(column))
            self.assertEqual(settings["source"]["sync_registration_time_column"], column)
        with self.assertRaises(hourly.ConfigError):
            hourly.sync_query("HostName")

    def test_bad_zero_row_mode_fails(self):
        self.assert_config_error(raw_settings(self.tmp, rows={"zero_row_mode": "sometimes"}), "rows.zero_row_mode")

    def test_unknown_keys_fail(self):
        raw = raw_settings(self.tmp)
        raw["surprise"] = 1
        self.assert_config_error(raw, "unknown setting")
        self.assert_config_error(raw_settings(self.tmp, read={"nolock": True}), "read.nolock")

    def test_missing_expected_database_fails(self):
        raw = raw_settings(self.tmp)
        del raw["source"]["expected_database"]
        self.assert_config_error(raw, "source.expected_database is required")

    def test_expected_database_must_be_allowed(self):
        self.assert_config_error(
            raw_settings(self.tmp, source={"allowed_databases": ["AED_OTHER_BOOK"]}),
            "allowed_databases",
        )

    def test_sa_sql_login_rejected_in_settings(self):
        message = self.assert_config_error(
            raw_settings(self.tmp, identity={"auth_mode": "sql_login", "sql_login": "SA", "credential_target": FAKE_CRED_TARGET}),
            "sa login is never allowed",
        )
        self.assertNotIn(FAKE_CRED_TARGET, message)

    def test_sql_login_needs_credential_target(self):
        self.assert_config_error(
            raw_settings(self.tmp, identity={"auth_mode": "sql_login", "sql_login": FAKE_SQL_LOGIN}),
            "identity.credential_target",
        )

    def test_bad_enums_and_ranges(self):
        self.assert_config_error(raw_settings(self.tmp, identity={"auth_mode": "kerberos"}), "identity.auth_mode")
        self.assert_config_error(raw_settings(self.tmp, rows={"uom_guard": "ignore"}), "rows.uom_guard")
        self.assert_config_error(raw_settings(self.tmp, schedule={"run_minute": 60}), "schedule.run_minute")
        self.assert_config_error(raw_settings(self.tmp, read={"max_attempts": 0}), "read.max_attempts")
        self.assert_config_error(raw_settings(self.tmp, identity={"allow_privileged_identity": "yes"}), "allow_privileged_identity")
        self.assert_config_error(raw_settings(self.tmp, schedule={"logon_type": "Interactive"}), "schedule.logon_type")

    def test_only_the_two_documented_identity_logon_pairs_are_supported(self):
        sql_identity = {"auth_mode": "sql_login", "sql_login": FAKE_SQL_LOGIN, "credential_target": FAKE_CRED_TARGET}
        self.assertEqual(settings_for(self.tmp)["schedule"]["logon_type"], "S4U")
        settings = settings_for(self.tmp, identity=sql_identity, schedule={"logon_type": "Password"})
        self.assertEqual((settings["identity"]["auth_mode"], settings["schedule"]["logon_type"]), ("sql_login", "Password"))
        # An S4U task cannot read Credential Manager, so sql_login under S4U is refused.
        message = self.assert_config_error(
            raw_settings(self.tmp, identity=sql_identity), "sql_login is only supported with schedule.logon_type Password"
        )
        self.assertNotIn(FAKE_SQL_LOGIN, message)
        self.assertNotIn(FAKE_CRED_TARGET, message)
        self.assert_config_error(
            raw_settings(self.tmp, schedule={"logon_type": "Password"}),
            "windows_integrated is only supported with schedule.logon_type S4U",
        )

    def test_dry_run_reports_auth_mode_and_scheduled_logon_type(self):
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw_settings(
            self.tmp,
            identity={"auth_mode": "sql_login", "sql_login": FAKE_SQL_LOGIN, "credential_target": FAKE_CRED_TARGET},
            schedule={"logon_type": "Password"},
        )), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = hourly.main(["--config", str(path), "--dry-run-config"])
        self.assertEqual(code, 0)
        printed = json.loads(out.getvalue())
        self.assertEqual((printed["auth_mode"], printed["scheduled_logon_type"]), ("sql_login", "Password"))
        self.assertEqual(printed["run_minute"], 0, "the installer reads schedule.run_minute from here")
        for value in (FAKE_SQL_LOGIN, FAKE_CRED_TARGET, FAKE_SERVER):
            self.assertNotIn(value, out.getvalue())

    def test_columns_validation(self):
        self.assert_config_error(raw_settings(self.tmp, output={"columns": ["ItemCode", "Cost"]}), "unsupported column")
        self.assert_config_error(raw_settings(self.tmp, output={"columns": ["ItemCode", "Location", "UOM"]}), "BalQty")
        self.assert_config_error(
            raw_settings(self.tmp, output={"columns": ["ItemCode", "ItemCode", "Location", "UOM", "BalQty"]}),
            "duplicates",
        )

    def test_delivery_root_must_differ_from_archive(self):
        archive = str(Path(self.tmp) / "archive")
        self.assert_config_error(raw_settings(self.tmp, output={"delivery_root": archive}), "delivery_root")

    def test_roots_must_not_be_nested(self):
        # G2 r6 N20: an archive inside the Drive folder would sync the hourly folders to Drive.
        drive = Path(self.tmp) / "drive_sync"
        nested = {"archive_root": str(drive / "archive"), "delivery_root": str(drive)}
        self.assert_config_error(raw_settings(self.tmp, output=nested), "inside each other")
        nested = {"archive_root": str(drive), "delivery_root": str(drive / "Report" / "AC2")}
        self.assert_config_error(raw_settings(self.tmp, output=nested), "inside each other")
        # Siblings with a shared name prefix are not nested.
        siblings = {"archive_root": str(drive), "delivery_root": str(Path(self.tmp) / "drive_sync_2")}
        self.assertEqual(hourly.validate_settings(raw_settings(self.tmp, output=siblings))["output"]["archive_root"], str(drive))

    def test_dry_run_config_cli(self):
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw_settings(self.tmp)), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = hourly.main(["--config", str(path), "--dry-run-config"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "valid")
        self.assertFalse(self.archive.exists(), "dry run must not create output")
        path.write_text(json.dumps(raw_settings(self.tmp, rows={"zero_row_mode": "bad"})), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = hourly.main(["--config", str(path), "--dry-run-config"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["failure_class"], "config_invalid")
        self.assertFalse(self.archive.exists())

    def test_invalid_config_run_writes_failed_manifest(self):
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw_settings(self.tmp, rows={"zero_row_mode": "bad"})), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = hourly.main(["--config", str(path)], source=FakeSource(), environ=ENV, clock=clock_at())
        self.assertEqual(code, 1)
        manifests = list(self.archive.rglob(hourly.MANIFEST_NAME))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["failure_class"], "config_invalid")
        self.assertEqual(list(self.archive.rglob("*.csv")), [])
        status = json.loads((self.archive / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "failed")


@contextlib.contextmanager
def no_zone_database():
    """Simulate Windows Python without tzdata: every zone lookup fails, as ZoneInfo does there."""
    def missing(key):
        raise ZoneInfoNotFoundError("No time zone found with key %s" % key)

    with mock.patch.dict(hourly.load_timezone.__globals__, {"ZoneInfo": missing}):
        yield


class TimezoneFailureTests(TempDirTestCase):
    def write_settings(self, **overrides):
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw_settings(self.tmp, **overrides)), encoding="utf-8")
        return path

    def main(self, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = hourly.main(list(args), **kwargs)
        return code, out.getvalue()

    def test_missing_zone_database_run_writes_utc_manifest_and_status(self):
        path = self.write_settings(output={"delivery_root": str(self.delivery)})
        code, _ = self.main("--config", str(path), source=FakeSource(), environ=ENV, clock=clock_at())
        self.assertEqual(code, 0)
        source = FakeSource()
        with no_zone_database():
            code, printed = self.main("--config", str(path), source=source, environ=ENV, clock=clock_at())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(printed)["failure_class"], "timezone_unavailable")
        self.assertEqual(source.sessions, 0)
        manifests = [json.loads(p.read_text(encoding="utf-8")) for p in self.archive.rglob(hourly.MANIFEST_NAME)]
        failed = [m for m in manifests if m["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        manifest = failed[0]
        self.assertEqual(manifest["failure_class"], "timezone_unavailable")
        self.assertEqual(manifest["timezone"], "UTC")
        self.assertIn("UTC", manifest["note"])
        self.assertEqual(manifest["started_at"], "2026-10-23T06:00:05+00:00")
        self.assertIn("+0000", manifest["hourly_folder"])
        # The earlier success status is replaced in the archive AND in Drive, never left stale.
        for root in (self.archive, self.delivery):
            status = json.loads((root / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
            self.assertEqual((status["status"], status["failure_class"]), ("failed", "timezone_unavailable"))
            self.assertIn("UTC", status["note"])
            self.assertEqual(status["run_id"], manifest["run_id"])
        self.assertEqual(len(list(self.archive.rglob(hourly.CSV_NAME))), 1, "only the earlier success wrote a CSV")

    def test_bad_zone_name_with_zone_database_is_config_invalid(self):
        path = self.write_settings(timezone="Mars/Olympus_Mons")
        code, printed = self.main("--config", str(path), source=FakeSource(), environ=ENV, clock=clock_at())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(printed)["failure_class"], "config_invalid")
        manifest = json.loads(next(self.archive.rglob(hourly.MANIFEST_NAME)).read_text(encoding="utf-8"))
        self.assertEqual(manifest["failure_class"], "config_invalid")
        self.assertEqual((manifest["timezone"], manifest["note"]), ("Asia/Singapore", None))
        self.assertTrue(manifest["started_at"].endswith("+08:00"))

    def test_config_failure_path_never_raises(self):
        with no_zone_database():
            self.assertIsNone(hourly.config_failure_manifest({"output": {"archive_root": "bad\0root"}}, "x"))
            self.assertIsNone(hourly.config_failure_manifest({"output": "not an object"}, "x"))

    def test_dry_run_without_zone_database_fails_cleanly(self):
        path = self.write_settings()
        with no_zone_database():
            code, printed = self.main("--config", str(path), "--dry-run-config")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(printed)["failure_class"], "timezone_unavailable")
        self.assertFalse(self.archive.exists(), "dry run must not create output")

    def test_check_latest_only_exits_0_2_or_3(self):
        path = self.write_settings()
        run(settings_for(self.tmp))
        code, printed = self.main("--config", str(path), "--check-latest", clock=clock_at(FIXED_NOW + timedelta(minutes=5)))
        self.assertEqual((code, json.loads(printed)["status"]), (0, "fresh"))
        with no_zone_database():
            code, printed = self.main("--config", str(path), "--check-latest")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(printed)["failure_class"], "timezone_unavailable")
        bad = self.write_settings(rows={"zero_row_mode": "bad"})
        self.assertEqual(self.main("--config", str(bad), "--check-latest")[0], 3)
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.main("--config", str(bad), "--check-latest")[0], 3)
        self.assertEqual(self.main("--check-latest")[0], 3, "a usage error is 3, never argparse's 2")
        path = self.write_settings()
        with mock.patch.object(hourly, "check_latest", side_effect=RuntimeError("boom")):
            self.assertEqual(self.main("--config", str(path), "--check-latest")[0], 3)
        latest = self.archive / hourly.LATEST_MANIFEST_NAME
        content = json.loads(latest.read_text(encoding="utf-8"))
        content["as_of_local"] = "2026-10-23T14:00:06"  # no offset
        latest.write_text(json.dumps(content), encoding="utf-8")
        self.assertEqual(hourly.check_latest(settings_for(self.tmp), clock=clock_at())[0], 3)

    MALFORMED_ZONES = ("Asia/Singapore/", "/Asia/Singapore", "Asia//Singapore")

    def assert_config_failure_written(self, failure_class):
        """One failed manifest (default zone stamps) and a failed status in archive AND delivery."""
        manifests = [json.loads(p.read_text(encoding="utf-8")) for p in self.archive.rglob(hourly.MANIFEST_NAME)]
        failed = [m for m in manifests if m["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["failure_class"], failure_class)
        self.assertEqual((failed[0]["timezone"], failed[0]["note"]), ("Asia/Singapore", None))
        for root in (self.archive, self.delivery):
            status = json.loads((root / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
            self.assertEqual((status["status"], status["failure_class"]), ("failed", failure_class))
            self.assertEqual(status["run_id"], failed[0]["run_id"])

    def test_malformed_zone_names_never_crash_any_mode(self):
        # zoneinfo raises ValueError (not ZoneInfoNotFoundError) for these keys.
        for name in self.MALFORMED_ZONES:
            with self.subTest(timezone=name):
                with self.assertRaises(ValueError):
                    ZoneInfo(name)
                shutil.rmtree(self.archive, ignore_errors=True)
                shutil.rmtree(self.delivery, ignore_errors=True)
                good = self.write_settings(output={"delivery_root": str(self.delivery)})
                self.assertEqual(self.main("--config", str(good), source=FakeSource(), environ=ENV, clock=clock_at())[0], 0)
                path = self.write_settings(timezone=name, output={"delivery_root": str(self.delivery)})
                source = FakeSource()
                code, printed = self.main("--config", str(path), source=source, environ=ENV, clock=clock_at())
                self.assertEqual((code, json.loads(printed)["failure_class"]), (1, "config_invalid"))
                self.assertEqual(source.sessions, 0)
                self.assert_config_failure_written("config_invalid")
                code, printed = self.main("--config", str(path), "--check-latest")
                self.assertEqual((code, json.loads(printed)["failure_class"]), (3, "config_invalid"))
                code, printed = self.main("--config", str(path), "--dry-run-config")
                self.assertEqual((code, json.loads(printed)["failure_class"]), (1, "config_invalid"))
                with self.assertRaises(hourly.ConfigError):
                    hourly.validate_settings(raw_settings(self.tmp, timezone=name))

    def test_any_zone_load_error_is_a_config_failure(self):
        # For example the tzdata package raising OSError for a folder-like key such as "Asia".
        real_zone_info = hourly.load_timezone.__globals__["ZoneInfo"]
        for error in (OSError("is a directory"), IsADirectoryError("Asia"), PermissionError("denied"), ValueError("bad key")):
            def zone_info(key, error=error):
                if key == "Asia":
                    raise error
                return real_zone_info(key)

            with self.subTest(error=type(error).__name__):
                path = self.write_settings(timezone="Asia")
                with mock.patch.dict(hourly.load_timezone.__globals__, {"ZoneInfo": zone_info}):
                    code, printed = self.main("--config", str(path), source=FakeSource(), environ=ENV, clock=clock_at())
                    self.assertEqual((code, json.loads(printed)["failure_class"]), (1, "config_invalid"))
                    self.assertEqual(self.main("--config", str(path), "--check-latest")[0], 3)
                    self.assertEqual(self.main("--config", str(path), "--dry-run-config")[0], 1)
                    self.assertIsInstance(hourly.load_zone("Asia/Singapore"), type(SGT))
                    with self.assertRaises(RuntimeError):
                        hourly.load_zone("Asia")

    def test_malformed_zone_without_zone_database_is_timezone_unavailable(self):
        def missing_or_malformed(key):
            raise ValueError("bad key") if key != key.strip("/") else ZoneInfoNotFoundError(key)

        path = self.write_settings(timezone="Asia/Singapore/")
        with mock.patch.dict(hourly.load_timezone.__globals__, {"ZoneInfo": missing_or_malformed}):
            code, printed = self.main("--config", str(path), source=FakeSource(), environ=ENV, clock=clock_at())
            self.assertEqual(self.main("--config", str(path), "--check-latest")[0], 3)
        self.assertEqual((code, json.loads(printed)["failure_class"]), (1, "timezone_unavailable"))
        manifest = json.loads(next(self.archive.rglob(hourly.MANIFEST_NAME)).read_text(encoding="utf-8"))
        self.assertEqual((manifest["failure_class"], manifest["timezone"]), ("timezone_unavailable", "UTC"))

    def test_unexpected_validation_error_still_writes_status_and_exits_cleanly(self):
        path = self.write_settings(output={"delivery_root": str(self.delivery)})
        self.assertEqual(self.main("--config", str(path), source=FakeSource(), environ=ENV, clock=clock_at())[0], 0)
        with mock.patch.object(hourly, "validate_settings", side_effect=KeyError("boom")):
            code, printed = self.main("--config", str(path), source=FakeSource(), environ=ENV, clock=clock_at())
            self.assertEqual((code, json.loads(printed)["failure_class"]), (1, "internal_error"))
            self.assertNotIn("boom", printed)
            self.assertEqual(self.main("--config", str(path), "--check-latest")[0], 3)
            self.assertEqual(self.main("--config", str(path), "--dry-run-config")[0], 1)
        self.assert_config_failure_written("internal_error")


class IdentityTests(TempDirTestCase):
    def test_sa_refused_even_when_privileged_allowed(self):
        settings = settings_for(self.tmp, identity={"allow_privileged_identity": True})
        for login in ("sa", "SA", " Sa "):
            with self.assertRaises(hourly.RunFailure) as ctx:
                hourly.check_identity(observed_identity(login=login, is_sysadmin=1), settings, ENV)
            self.assertEqual(ctx.exception.failure_class, "identity_refused")
            self.assertIn("sa login", str(ctx.exception))
        manifest = run(settings, FakeSource(identity=observed_identity(login="sa")))
        self.assertEqual(manifest["failure_class"], "identity_refused")
        self.assertEqual(list(self.archive.rglob("*.csv")), [])

    def test_sysadmin_refused_when_not_allowed(self):
        settings = settings_for(self.tmp)
        for flags in ({"is_sysadmin": 1}, {"is_db_owner": 1}, {"is_sysadmin": None}, {"is_db_owner": None}):
            with self.assertRaises(hourly.RunFailure) as ctx:
                hourly.check_identity(observed_identity(**flags), settings, ENV)
            self.assertEqual(ctx.exception.failure_class, "identity_refused")

    def test_privileged_allowed_with_flag_and_warning(self):
        settings = settings_for(self.tmp, identity={"allow_privileged_identity": True})
        manifest = run(settings, FakeSource(identity=observed_identity(is_sysadmin=1, is_db_owner=1)))
        self.assertEqual(manifest["status"], "success")
        self.assertTrue(manifest["identity"]["privileged"])
        self.assertTrue(manifest["warnings"])
        on_disk = json.loads((self.run_dir(manifest) / hourly.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertTrue(on_disk["identity"]["privileged"])
        self.assertIn("Privileged", on_disk["warnings"][0])

    def test_non_privileged_identity_flags(self):
        flags = hourly.check_identity(observed_identity(), settings_for(self.tmp), ENV)
        self.assertEqual(flags["privileged"], False)
        self.assertTrue(flags["checked"])

    def test_expected_context_mismatch_fails_closed_without_values(self):
        settings = settings_for(self.tmp)
        for field, value in (("login", "other_login"), ("user", "other_user"), ("database", "OTHER_DB")):
            with self.assertRaises(hourly.RunFailure) as ctx:
                hourly.check_identity(observed_identity(**{field: value}), settings, ENV)
            message = str(ctx.exception)
            self.assertIn("mismatch", message)
            self.assertIn(field, message)
            for secret in (value, FAKE_LOGIN, FAKE_USER, FAKE_DB):
                self.assertNotIn(secret, message)

    def test_missing_expected_context_env_fails_closed(self):
        with self.assertRaises(hourly.RunFailure) as ctx:
            hourly.check_identity(observed_identity(), settings_for(self.tmp), {})
        self.assertEqual(ctx.exception.failure_class, "identity_refused")
        self.assertIn("AUTOCOUNT_EXPECTED_SQL_LOGIN", str(ctx.exception))
        manifest = run(settings_for(self.tmp), environ={})
        self.assertEqual(manifest["failure_class"], "identity_refused")

    def test_database_pin_mismatch(self):
        env = dict(ENV, AUTOCOUNT_EXPECTED_SQL_DATABASE="AED_OTHER_BOOK")
        with self.assertRaises(hourly.RunFailure) as ctx:
            hourly.check_identity(observed_identity(database="AED_OTHER_BOOK"), settings_for(self.tmp), env)
        self.assertIn("Database pin", str(ctx.exception))
        self.assertNotIn("AED_OTHER_BOOK", str(ctx.exception))

    def test_identity_checked_on_every_attempt(self):
        source = FakeSource(changecounts=[(1, 2), (2, 2)])
        manifest = run(settings_for(self.tmp), source)
        self.assertEqual(manifest["attempts"], 2)
        self.assertEqual(source.sessions, 2)
        self.assertEqual(source.identity_reads, 2)
        self.assertEqual(source.closed, 2)

    def test_credential_failure_is_classified(self):
        settings = settings_for(
            self.tmp,
            identity={"auth_mode": "sql_login", "sql_login": FAKE_SQL_LOGIN, "credential_target": FAKE_CRED_TARGET},
            schedule={"logon_type": "Password"},
        )
        calls = []

        def failing_reader(target):
            calls.append(target)
            raise hourly.CredentialUnavailableError("The configured credential could not be read")

        manifest = run(settings, hourly.SqlServerSource(settings, credential_reader=failing_reader))
        self.assertEqual(calls, [FAKE_CRED_TARGET])
        self.assertEqual(manifest["failure_class"], "credential_unavailable")
        text = (self.run_dir(manifest) / hourly.MANIFEST_NAME).read_text(encoding="utf-8")
        self.assertNotIn(FAKE_CRED_TARGET, text)
        self.assertNotIn(FAKE_SQL_LOGIN, text)


class ConsistencyTests(TempDirTestCase):
    def test_changecount_moves_then_settles(self):
        manifest = run(settings_for(self.tmp), FakeSource(changecounts=[(10, 11), (11, 11)]))
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["attempts"], 2)
        self.assertEqual(manifest["changecount_before"], 11)
        self.assertEqual(manifest["changecount_after"], 11)

    def test_changecount_keeps_moving_fails_unstable(self):
        settings = settings_for(self.tmp)
        first = run(settings)
        latest_before = {
            name: (self.archive / name).read_bytes() for name in (hourly.LATEST_CSV_NAME, hourly.LATEST_MANIFEST_NAME)
        }
        manifest = run(settings, FakeSource(changecounts=[(1, 2), (2, 3), (3, 4)]))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_class"], "unstable_snapshot")
        self.assertEqual(manifest["attempts"], 3)
        self.assertEqual((manifest["changecount_before"], manifest["changecount_after"]), (3, 4))
        run_dir = self.run_dir(manifest)
        self.assertTrue((run_dir / hourly.MANIFEST_NAME).exists())
        self.assertFalse((run_dir / hourly.CSV_NAME).exists())
        for name, data in latest_before.items():
            self.assertEqual((self.archive / name).read_bytes(), data)
        self.assertEqual(json.loads((self.archive / hourly.LATEST_MANIFEST_NAME).read_text())["run_id"], first["run_id"])

    def test_lock_timeout_retries_then_fails(self):
        manifest = run(settings_for(self.tmp), FakeSource(lock_timeouts=[True, False]))
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["attempts"], 2)
        manifest = run(settings_for(self.tmp), FakeSource(lock_timeouts=[True, True, True]))
        self.assertEqual(manifest["failure_class"], "lock_timeout")
        self.assertFalse((self.run_dir(manifest) / hourly.CSV_NAME).exists())

    def test_sync_failure_fails_the_hour(self):
        manifest = run(settings_for(self.tmp), FakeSource(sync_error=True))
        self.assertEqual(manifest["failure_class"], "sync_read_failed")
        self.assertFalse((self.run_dir(manifest) / hourly.CSV_NAME).exists())

    def test_pos_sync_in_manifest(self):
        manifest = run(settings_for(self.tmp))
        self.assertEqual(manifest["pos_sync"], [{"Location": "LOC-A", "last_sync_local": "2026-10-23T13:50:00+08:00"}])
        self.assertEqual(manifest["as_of_local"], "2026-10-23T14:00:06+08:00")
        self.assertEqual(manifest["as_of_sql"], SQL_NOW.isoformat())


class UomGuardTests(TempDirTestCase):
    def test_non_base_uom_row_fails_without_csv(self):
        balances = default_balances() + [bal("FAKE-0005", "LOC-A", 2, uom="BOX", base="PCS")]
        manifest = run(settings_for(self.tmp), FakeSource(balances=balances))
        self.assertEqual(manifest["failure_class"], "uom_guard")
        self.assertEqual(manifest["uom_guard"]["non_base_uom_rows"], 1)
        self.assertFalse((self.run_dir(manifest) / hourly.CSV_NAME).exists())
        self.assertFalse((self.archive / hourly.LATEST_CSV_NAME).exists())

    def test_multi_uom_item_fails(self):
        manifest = run(settings_for(self.tmp), FakeSource(multi_uom=2))
        self.assertEqual(manifest["failure_class"], "uom_guard")
        self.assertEqual(manifest["uom_guard"]["multi_uom_items"], 2)

    def test_guard_passes_for_base_uom(self):
        manifest = run(settings_for(self.tmp))
        self.assertEqual(manifest["uom_guard"]["result"], "pass")


class RowRuleTests(TempDirTestCase):
    def build(self, balances=None, no_rows=None, **overrides):
        settings = settings_for(self.tmp, **overrides)
        snapshot = {
            "balances": default_balances() if balances is None else balances,
            "no_row_items": [no_row_item("FAKE-0009")] if no_rows is None else no_rows,
        }
        return hourly.build_rows(snapshot, settings, "2026-10-23T14:00:06+08:00")

    def keys(self, rows):
        return [(r["ItemCode"], r["Location"], r["RowSource"]) for r in rows]

    def test_default_mode(self):
        rows, counts, per_location, _ = self.build()
        keys = self.keys(rows)
        self.assertIn(("FAKE-0002", "LOC-A", "balance"), keys, "active zero row kept")
        self.assertNotIn(("FAKE-0003", "LOC-A", "balance"), keys, "inactive zero row dropped")
        self.assertIn(("FAKE-0009", "", "no_balance_row"), keys)
        added = [r for r in rows if r["RowSource"] == "no_balance_row"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["LocationLabel"], "(no balance row)")
        self.assertEqual(added[0]["BalQty"], Decimal(0))
        self.assertEqual(counts["zero"], 1)
        self.assertEqual(counts["inactive_dropped"], 1)
        self.assertEqual(counts["no_balance_row"], 1)
        self.assertEqual(counts["negative"], 1)
        self.assertEqual(per_location["LOC-B"]["negative"], 1)
        self.assertEqual(per_location["LOC-B"]["sum_qty"], "-2")
        self.assertEqual(per_location["LOC-A"], {"rows": 2, "non_zero": 1, "negative": 0, "sum_qty": "5"})

    def test_inactive_non_zero_kept(self):
        rows, counts, _, _ = self.build(balances=[bal("FAKE-0007", "LOC-A", 4, active="F")], no_rows=[])
        self.assertEqual(self.keys(rows), [("FAKE-0007", "LOC-A", "balance")])
        self.assertEqual(counts["inactive_dropped"], 0)

    def test_inactive_zero_kept_when_setting_off(self):
        rows, counts, _, _ = self.build(rows={"drop_zero_rows_for_inactive_items": False})
        self.assertIn(("FAKE-0003", "LOC-A", "balance"), self.keys(rows))
        self.assertEqual(counts["inactive_dropped"], 0)

    def test_item_without_rows_added_once(self):
        rows, counts, _, _ = self.build(no_rows=[no_row_item("FAKE-0009"), no_row_item("FAKE-0009"), no_row_item("FAKE-0001")])
        added = [r["ItemCode"] for r in rows if r["RowSource"] == "no_balance_row"]
        self.assertEqual(added, ["FAKE-0009"])
        self.assertEqual(counts["no_balance_row"], 1)

    def test_keep_rows_mode(self):
        rows, counts, _, _ = self.build(rows={"zero_row_mode": "keep_rows"})
        self.assertNotIn("no_balance_row", [r["RowSource"] for r in rows])
        self.assertIn(("FAKE-0002", "LOC-A", "balance"), self.keys(rows))
        self.assertEqual(counts["no_balance_row"], 0)

    def test_drop_zero_rows_mode(self):
        rows, counts, _, _ = self.build(rows={"zero_row_mode": "drop_zero_rows"})
        self.assertTrue(all(r["BalQty"] != 0 for r in rows))
        self.assertEqual(counts["zero_dropped"], 1)
        self.assertEqual(counts["inactive_dropped"], 1)
        self.assertEqual(counts["zero"], 0)

    def test_no_row_query_only_in_add_mode(self):
        source = FakeSource()
        run(settings_for(self.tmp, rows={"zero_row_mode": "keep_rows"}), source)
        self.assertEqual(source.no_row_reads, 0)
        run(settings_for(self.tmp), source)
        self.assertEqual(source.no_row_reads, 1)

    def test_locations_label_and_unmapped(self):
        balances = default_balances() + [
            bal("FAKE-0006", "LOC-C", 1, loc_desc="Fake outlet C"),
            bal("FAKE-0006", "LOC-D", 1, loc_desc=None),
        ]
        rows, _, per_location, unmapped = self.build(balances=balances)
        labels = {r["Location"]: r["LocationLabel"] for r in rows if r["RowSource"] == "balance"}
        self.assertEqual(labels["LOC-A"], "LOC-A - Example outlet")
        self.assertEqual(labels["LOC-GIT"], "LOC-GIT - Example in transit")
        self.assertEqual(labels["LOC-B"], "LOC-B - Fake outlet B")
        self.assertEqual(labels["LOC-C"], "LOC-C - Fake outlet C")
        self.assertEqual(labels["LOC-D"], "LOC-D")
        self.assertEqual(unmapped, ["LOC-B", "LOC-C", "LOC-D"])
        self.assertEqual(set(per_location), {"LOC-A", "LOC-B", "LOC-C", "LOC-D", "LOC-GIT"})

    def test_no_location_code_dropped_from_manifest(self):
        balances = [bal("FAKE-0003", "LOC-Z", 0, active="F")]
        rows, _, per_location, _ = self.build(balances=balances, no_rows=[])
        self.assertEqual(rows, [])
        self.assertIn("LOC-Z", per_location)
        self.assertEqual(per_location["LOC-Z"]["rows"], 0)

    def test_batch_and_null_counts(self):
        balances = [bal("FAKE-0008", "LOC-A", 3, batch=2, null_rows=1, rows=3), bal("FAKE-0010", "LOC-A", None, null_rows=1)]
        rows, counts, _, _ = self.build(balances=balances, no_rows=[])
        self.assertEqual(counts["non_empty_batch"], 2)
        self.assertEqual(counts["null_balqty_rows"], 2)
        self.assertEqual(counts["source_balance_rows"], 4)
        self.assertEqual(counts["balance_groups_read"], 2)

    def test_tidy_qty(self):
        self.assertEqual(str(hourly.tidy_qty(Decimal("5.00000000"))), "5")
        self.assertEqual(str(hourly.tidy_qty(Decimal("2.50000000"))), "2.5")
        self.assertEqual(str(hourly.tidy_qty(Decimal("-3.00000000"))), "-3")
        self.assertEqual(str(hourly.tidy_qty(Decimal("100.00000000"))), "100")
        self.assertEqual(str(hourly.tidy_qty(Decimal("-0.00000000"))), "0")


class CsvOutputTests(TempDirTestCase):
    def test_csv_format_and_safety(self):
        balances = default_balances() + [bal("FAKE-0011", "LOC-A", "-7.00000000", desc="=SUM(A1:A9)")]
        manifest = run(settings_for(self.tmp), FakeSource(balances=balances))
        self.assertEqual(manifest["status"], "success")
        data = (self.run_dir(manifest) / hourly.CSV_NAME).read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "UTF-8 without BOM")
        self.assertIn(b"\r\n", data)
        rows = read_csv(self.run_dir(manifest) / hourly.CSV_NAME)
        self.assertEqual(list(rows[0].keys()), hourly.DEFAULT_COLUMNS)
        row = next(r for r in rows if r["ItemCode"] == "FAKE-0011")
        self.assertEqual(row["ItemDescription"], "'=SUM(A1:A9)")
        self.assertEqual(row["BalQty"], "-7")
        self.assertEqual(row["as_of_local"], "2026-10-23T14:00:06+08:00")
        self.assertEqual(manifest["row_count"], len(rows))
        self.assertEqual(manifest["csv"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(manifest["csv"]["byte_size"], len(data))

    def test_columns_setting_controls_order(self):
        columns = ["Location", "ItemCode", "BalQty", "UOM"]
        manifest = run(settings_for(self.tmp, output={"columns": columns}))
        rows = read_csv(self.run_dir(manifest) / hourly.CSV_NAME)
        self.assertEqual(list(rows[0].keys()), columns)

    def test_manifest_contents(self):
        manifest = run(settings_for(self.tmp))
        on_disk = json.loads((self.run_dir(manifest) / hourly.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["job"], "ac2_hourly_stock_balance")
        self.assertEqual(on_disk["status"], "success")
        self.assertIsNone(on_disk["failure_class"])
        self.assertEqual(on_disk["source"]["surface"], "dbo.ItemBatchBalQty")
        self.assertEqual(on_disk["source"]["database"], FAKE_DB)
        self.assertEqual(on_disk["source"]["query_sha256"], hashlib.sha256(hourly.BALANCE_QUERY.encode("utf-8")).hexdigest())
        self.assertEqual(len(on_disk["source"]["settings_sha256"]), 64)
        self.assertEqual(on_disk["started_at"], FIXED_NOW.isoformat())
        self.assertTrue(on_disk["finished_at"].endswith("+08:00"))
        self.assertEqual(on_disk["unmapped_location_codes"], ["LOC-B"])
        self.assertEqual(on_disk["delivery"]["status"], "not_configured")
        for key in ("zero", "negative", "no_balance_row", "non_empty_batch", "inactive_dropped"):
            self.assertIn(key, on_disk["counts"])
        folder = Path(on_disk["hourly_folder"])
        self.assertEqual(folder.parts[0], "hourly")
        self.assertEqual(folder.parts[1], "2026-10-23")
        self.assertRegex(folder.parts[2], r"^ac2_stockbal_20261023T140005\+0800_[0-9a-f]{8}$")
        self.assertTrue(folder.parts[2].endswith(on_disk["run_id"][:8]))


class LargeBalanceTests(TempDirTestCase):
    def assert_config_error(self, threshold):
        with self.assertRaises(hourly.ConfigError) as ctx:
            settings_for(self.tmp, output={"large_balance_threshold": threshold})
        self.assertIn("output.large_balance_threshold", str(ctx.exception))

    def on_disk(self, manifest, name=hourly.MANIFEST_NAME, root=None):
        return json.loads((self.run_dir(manifest, root) / name).read_text(encoding="utf-8"))

    def test_setting_default_and_validation(self):
        self.assertEqual(settings_for(self.tmp)["output"]["large_balance_threshold"], 100000)
        for good in (1, 250, hourly.LARGE_BALANCE_THRESHOLD_MAX):
            self.assertEqual(settings_for(self.tmp, output={"large_balance_threshold": good})["output"]["large_balance_threshold"], good)
        for bad in (0, -1, None, True, 1.5, 100000.0, "100000", hourly.LARGE_BALANCE_THRESHOLD_MAX + 1):
            self.assert_config_error(bad)

    def test_threshold_boundary_is_strictly_above(self):
        balances = [
            bal("FAKE-0101", "LOC-A", "100000.00000000"),
            bal("FAKE-0102", "LOC-A", "-100000.00000000"),
            bal("FAKE-0103", "LOC-A", "100000.00010000"),
            bal("FAKE-0104", "LOC-B", "-100001.00000000"),
            bal("FAKE-0105", "LOC-A", "99999.00000000"),
        ]
        manifest = run(settings_for(self.tmp), FakeSource(balances=balances, no_row_items=[]))
        large = manifest["large_balances"]
        self.assertEqual(large["count"], 2)
        self.assertEqual(
            large["rows"],
            [
                {"ItemCode": "FAKE-0104", "Location": "LOC-B", "BalQty": "-100001"},
                {"ItemCode": "FAKE-0103", "Location": "LOC-A", "BalQty": "100000.0001"},
            ],
        )

    def test_positive_and_negative_pair_flagged_and_run_still_succeeds(self):
        balances = default_balances() + [
            bal("FAKE-0201", "LOC-A", "123456789000.00000000"),
            bal("FAKE-0201", "LOC-B", "-123456789000.00000000"),
        ]
        manifest = run(settings_for(self.tmp), FakeSource(balances=balances))
        self.assertEqual(manifest["status"], "success")
        self.assertIsNone(manifest["failure_class"])
        on_disk = self.on_disk(manifest)
        self.assertEqual(on_disk["large_balances"], {
            "threshold": 100000,
            "count": 2,
            "rows": [
                {"ItemCode": "FAKE-0201", "Location": "LOC-A", "BalQty": "123456789000"},
                {"ItemCode": "FAKE-0201", "Location": "LOC-B", "BalQty": "-123456789000"},
            ],
            "rows_truncated": False,
        })
        warnings = [w for w in on_disk["warnings"] if w.startswith("Large balance:")]
        self.assertEqual(len(warnings), 1)
        self.assertIn("2 exported row(s)", warnings[0])
        self.assertIn("above 100000", warnings[0])
        self.assertNotIn("FAKE-0201", warnings[0], "the warning line names no item")
        # The flagged rows are still exported unchanged and the latest pair still advances.
        rows = read_csv(self.run_dir(manifest) / hourly.CSV_NAME)
        flagged = sorted(r["BalQty"] for r in rows if r["ItemCode"] == "FAKE-0201")
        self.assertEqual(flagged, ["-123456789000", "123456789000"])
        latest = json.loads((self.archive / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(latest["run_id"], manifest["run_id"])
        self.assertEqual(latest["large_balances"]["count"], 2)
        status = json.loads((self.archive / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual((status["status"], status["large_balance_count"]), ("success", 2))

    def test_list_capped_at_20_largest_first(self):
        balances = [bal("FAKE-%04d" % n, "LOC-A", (200000 + n) * (1 if n % 2 else -1)) for n in range(1, 26)]
        manifest = run(settings_for(self.tmp), FakeSource(balances=balances, no_row_items=[]))
        large = manifest["large_balances"]
        self.assertEqual(large["count"], 25)
        self.assertEqual(len(large["rows"]), hourly.LARGE_BALANCE_LIST_CAP)
        self.assertEqual(len(large["rows"]), 20)
        self.assertTrue(large["rows_truncated"])
        sizes = [abs(Decimal(r["BalQty"])) for r in large["rows"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(large["rows"][0], {"ItemCode": "FAKE-0025", "Location": "LOC-A", "BalQty": "200025"})
        self.assertEqual(large["rows"][1]["BalQty"], "-200024")
        self.assertIn("25 exported row(s)", " ".join(manifest["warnings"]))

    def test_exactly_20_is_not_truncated(self):
        balances = [bal("FAKE-%04d" % n, "LOC-A", 500000) for n in range(1, 21)]
        large = run(settings_for(self.tmp), FakeSource(balances=balances, no_row_items=[]))["large_balances"]
        self.assertEqual((large["count"], len(large["rows"]), large["rows_truncated"]), (20, 20, False))

    def test_no_large_balance_gives_empty_block_and_no_warning(self):
        manifest = run(settings_for(self.tmp))
        self.assertEqual(manifest["large_balances"], {"threshold": 100000, "count": 0, "rows": [], "rows_truncated": False})
        self.assertFalse([w for w in manifest["warnings"] if w.startswith("Large balance:")])
        self.assertEqual(manifest["_last_run_status"]["large_balance_count"], 0)

    def test_custom_threshold(self):
        manifest = run(settings_for(self.tmp, output={"large_balance_threshold": 3}))
        large = manifest["large_balances"]
        self.assertEqual(large["threshold"], 3)
        self.assertEqual(
            [(r["ItemCode"], r["Location"], r["BalQty"]) for r in large["rows"]],
            [("FAKE-0001", "LOC-A", "5"), ("FAKE-0004", "LOC-GIT", "3.5")],
        )

    def test_failed_run_has_no_count(self):
        manifest = run(settings_for(self.tmp), FakeSource(multi_uom=1))
        self.assertEqual(manifest["status"], "failed")
        self.assertIsNone(manifest["large_balances"])
        status = json.loads((self.archive / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertIsNone(status["large_balance_count"])

    def test_failure_after_rows_are_built_clears_the_block(self):
        settings = settings_for(self.tmp)
        balances = default_balances() + [bal("FAKE-0401", "LOC-A", 300000)]
        fixed = "0123456789abcdef0123456789abcdef"
        with mock.patch.object(hourly.uuid, "uuid4", return_value=mock.Mock(hex=fixed)):
            first = run(settings, FakeSource(balances=balances))
            second = run(settings, FakeSource(balances=balances))
        self.assertEqual(first["large_balances"]["count"], 1)
        self.assertEqual((second["status"], second["failure_class"]), ("failed", "write_failed"))
        self.assertIsNone(second["large_balances"])
        self.assertIsNone(second["_last_run_status"]["large_balance_count"])
        self.assertFalse([w for w in second["warnings"] if w.startswith("Large balance:")])

    def test_failed_run_on_disk_drops_the_warning_line(self):
        # G2 N10: a run that fails after the rows are built must not keep the warning line.
        balances = default_balances() + [bal("FAKE-0403", "LOC-A", 300000)]
        with mock.patch.object(hourly, "file_sha256", return_value="0" * 64):
            manifest = run(settings_for(self.tmp), FakeSource(balances=balances))
        self.assertEqual((manifest["status"], manifest["failure_class"]), ("failed", "write_failed"))
        on_disk = self.on_disk(manifest)
        self.assertIsNone(on_disk["large_balances"])
        self.assertFalse([w for w in on_disk["warnings"] if w.startswith("Large balance:")])

    def test_latest_publish_failure_reports_no_count(self):
        balances = default_balances() + [bal("FAKE-0402", "LOC-A", 300000)]
        with mock.patch.object(hourly, "publish_latest", side_effect=OSError("held open")):
            manifest = run(settings_for(self.tmp), FakeSource(balances=balances))
        status = json.loads((self.archive / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual((status["status"], status["failure_class"]), ("failed", "latest_publish_failed"))
        self.assertIsNone(status["large_balance_count"])
        self.assertEqual(manifest["large_balances"]["count"], 1, "the hourly manifest itself is a success")

    def test_cli_and_check_latest_report_the_count_without_row_values(self):
        raw = raw_settings(self.tmp, output={"delivery_root": str(self.delivery)})
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        balances = default_balances() + [bal("FAKE-0301", "LOC-A", "-987654321")]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = hourly.main(["--config", str(path)], source=FakeSource(balances=balances), environ=ENV, clock=clock_at())
        self.assertEqual(code, 0, "a large balance is a warning, never a failure")
        printed = json.loads(out.getvalue())
        self.assertEqual((printed["status"], printed["large_balance_count"]), ("success", 1))
        with contextlib.redirect_stdout(io.StringIO()) as out2:
            code = hourly.main(["--config", str(path), "--check-latest"], clock=clock_at(FIXED_NOW + timedelta(minutes=5)))
        self.assertEqual(code, 0)
        checked = json.loads(out2.getvalue())
        self.assertEqual((checked["status"], checked["large_balance_count"]), ("fresh", 1))
        for text in (out.getvalue(), out2.getvalue()):
            self.assertNotIn("FAKE-0301", text)
            self.assertNotIn("987654321", text)
        status = json.loads((self.delivery / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["large_balance_count"], 1)

    def test_check_latest_on_a_manifest_written_before_the_check(self):
        settings = settings_for(self.tmp)
        run(settings)
        path = self.archive / hourly.LATEST_MANIFEST_NAME
        latest = json.loads(path.read_text(encoding="utf-8"))
        del latest["large_balances"]
        path.write_text(json.dumps(latest), encoding="utf-8")
        code, result = hourly.check_latest(settings, clock=clock_at(FIXED_NOW))
        self.assertEqual((code, result["status"], result["large_balance_count"]), (0, "fresh", None))


class NoOverwriteTests(TempDirTestCase):
    def test_two_runs_same_hour_make_two_folders(self):
        settings = settings_for(self.tmp)
        first = run(settings)
        first_files = tree_bytes(self.run_dir(first))
        second = run(settings)
        self.assertNotEqual(first["hourly_folder"], second["hourly_folder"])
        self.assertEqual(tree_bytes(self.run_dir(first)), first_files)
        self.assertEqual(len(list((self.archive / "hourly" / "2026-10-23").iterdir())), 2)

    def test_exclusive_create_never_overwrites(self):
        path = Path(self.tmp) / "x.csv"
        hourly.write_exclusive(path, b"first")
        with self.assertRaises(FileExistsError):
            hourly.write_exclusive(path, b"second")
        self.assertEqual(path.read_bytes(), b"first")

    def test_run_folder_collision_is_a_write_failure_not_an_overwrite(self):
        settings = settings_for(self.tmp)
        fixed = "0123456789abcdef0123456789abcdef"
        with mock.patch.object(hourly.uuid, "uuid4", return_value=mock.Mock(hex=fixed)):
            first = run(settings)
            before = tree_bytes(self.run_dir(first))
            second = run(settings)
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["failure_class"], "write_failed")
        self.assertEqual(tree_bytes(self.run_dir(first)), before)


class LatestTests(TempDirTestCase):
    def test_latest_advances_after_verified_success(self):
        manifest = run(settings_for(self.tmp))
        hourly_csv = (self.run_dir(manifest) / hourly.CSV_NAME).read_bytes()
        self.assertEqual((self.archive / hourly.LATEST_CSV_NAME).read_bytes(), hourly_csv)
        latest = json.loads((self.archive / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(latest["run_id"], manifest["run_id"])
        self.assertEqual(latest["as_of_local"], manifest["as_of_local"])
        status = json.loads((self.archive / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "success")
        self.assertEqual(status["run_id"], manifest["run_id"])
        self.assertEqual([p.name for p in self.archive.glob("*.tmp")], [])
        self.assertFalse((self.archive / hourly.LOCK_NAME).exists(), "lock released")

    def test_failed_run_leaves_latest_and_updates_status(self):
        settings = settings_for(self.tmp)
        run(settings)
        latest_before = {
            name: (self.archive / name).read_bytes() for name in (hourly.LATEST_CSV_NAME, hourly.LATEST_MANIFEST_NAME)
        }
        failed = run(settings, FakeSource(multi_uom=1))
        for name, data in latest_before.items():
            self.assertEqual((self.archive / name).read_bytes(), data)
        status = json.loads((self.archive / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["failure_class"], "uom_guard")
        self.assertEqual(status["run_id"], failed["run_id"])

    def test_publish_refuses_when_hourly_csv_changed(self):
        manifest = run(settings_for(self.tmp))
        (self.run_dir(manifest) / hourly.CSV_NAME).write_bytes(b"tampered")
        with self.assertRaises(hourly.RunFailure):
            hourly.publish_latest(self.archive, self.run_dir(manifest), manifest, manifest["run_id"])


class DeliveryTests(TempDirTestCase):
    def settings(self):
        return settings_for(self.tmp, output={"delivery_root": str(self.delivery)})

    def status(self):
        return json.loads((self.delivery / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))

    def tamper(self, name_fragment, root=None):
        """Corrupt the copies of one file name, in the Drive sync folder only unless root is given."""
        real = hourly.copy_bytes_exclusive
        root = Path(root or self.delivery)

        def bad_copy(path, data):
            hit = name_fragment in Path(path).name and Path(path).parent == root
            real(path, data + b"x" if hit else data)

        return mock.patch.object(hourly, "copy_bytes_exclusive", side_effect=bad_copy)

    def changed_source(self):
        """A second run whose balances (and so CSV hash) differ from the first."""
        return FakeSource(balances=default_balances() + [bal("FAKE-0005", "LOC-A", "7.00000000")])

    def assert_drive_pair_is(self, run_id):
        """Drive's latest CSV and manifest both belong to run_id and their hashes agree."""
        latest = json.loads((self.delivery / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        csv_data = (self.delivery / hourly.LATEST_CSV_NAME).read_bytes()
        self.assertEqual(latest["run_id"], run_id)
        self.assertEqual(hashlib.sha256(csv_data).hexdigest(), latest["csv"]["sha256"], "CSV and manifest agree")
        self.assertEqual([p.name for p in self.delivery.rglob("*.tmp")], [], "no temp file is left")

    def test_delivery_is_latest_only_no_hourly_folders(self):
        manifest = run(self.settings())
        self.assertEqual(manifest["delivery"]["mode"], "latest_only")
        self.assertEqual(
            manifest["delivery"]["files"],
            [hourly.LATEST_CSV_NAME, hourly.LATEST_MANIFEST_NAME, hourly.FEED_NAME, hourly.ITEM_INDEX_NAME],
        )
        self.assertFalse((self.delivery / "hourly").exists(), "no hourly folders in Drive")
        self.assertEqual(
            sorted(p.name for p in self.delivery.iterdir()),
            sorted([hourly.DAILY_DIR, hourly.FEED_NAME, hourly.ITEM_INDEX_NAME, hourly.LAST_RUN_STATUS_NAME,
                    hourly.LATEST_CSV_NAME, hourly.LATEST_MANIFEST_NAME]),
        )
        self.assertEqual(
            (self.delivery / hourly.LATEST_CSV_NAME).read_bytes(),
            (self.run_dir(manifest) / hourly.CSV_NAME).read_bytes(),
        )
        latest = json.loads((self.delivery / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(latest["run_id"], manifest["run_id"])
        self.assertEqual((self.delivery / hourly.FEED_NAME).read_bytes(), (self.archive / hourly.FEED_NAME).read_bytes())
        self.assertEqual(self.status()["delivery_status"], "handed_to_drive_sync_folder")
        self.assertEqual([p.name for p in self.delivery.rglob("*.tmp")], [])
        self.assertNotIn(hourly.CLOSE_STATE_NAME, [p.name for p in self.delivery.rglob("*")], "state stays local")
        manifest_text = (self.run_dir(manifest) / hourly.MANIFEST_NAME).read_text(encoding="utf-8")
        self.assertNotIn("uploaded", manifest_text.lower())
        self.assertNotIn(str(self.tmp), manifest_text, "delivery paths are relative")

    def test_second_run_replaces_the_pair_and_adds_nothing(self):
        settings = self.settings()
        run(settings)
        before = sorted(str(p.relative_to(self.delivery)) for p in self.delivery.rglob("*"))
        second = run(settings, clock=clock_at(FIXED_NOW + timedelta(hours=1)))
        after = sorted(str(p.relative_to(self.delivery)) for p in self.delivery.rglob("*"))
        self.assertEqual(before, after, "an hourly run adds no files to Drive")
        latest = json.loads((self.delivery / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(latest["run_id"], second["run_id"])

    def test_tampered_csv_copy_is_verify_failed_and_latest_not_advanced(self):
        settings = self.settings()
        good = run(settings)
        before = {n: (self.delivery / n).read_bytes() for n in (hourly.LATEST_CSV_NAME, hourly.LATEST_MANIFEST_NAME)}
        with self.tamper(hourly.LATEST_CSV_NAME):
            manifest = run(settings)
        self.assertEqual(manifest["status"], "success", "archive data is still good")
        self.assertEqual(self.status()["delivery_status"], "verify_failed")
        for name, data in before.items():
            self.assertEqual((self.delivery / name).read_bytes(), data, name)
        self.assertEqual(json.loads(before[hourly.LATEST_MANIFEST_NAME])["run_id"], good["run_id"])
        self.assertEqual([p.name for p in self.delivery.rglob("*.tmp")], [])
        # The archive latest pair still advances: it does not depend on the Drive copy.
        archive_latest = json.loads((self.archive / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(archive_latest["run_id"], manifest["run_id"])

    def test_tampered_manifest_copy_keeps_the_previous_consistent_pair(self):
        # G2 r6 H4: the CSV used to be replaced before the manifest copy was checked.
        settings = self.settings()
        good = run(settings)
        before = tree_bytes(self.delivery)
        with self.tamper(hourly.LATEST_MANIFEST_NAME):
            second = run(settings, self.changed_source())
        self.assertNotEqual(second["csv"]["sha256"], good["csv"]["sha256"], "run 2 has a different CSV")
        self.assertEqual(self.status()["delivery_status"], "verify_failed")
        self.assert_drive_pair_is(good["run_id"])
        for name in (hourly.LATEST_CSV_NAME, hourly.LATEST_MANIFEST_NAME, hourly.FEED_NAME):
            self.assertEqual((self.delivery / name).read_bytes(), before[name], name)
        self.assertEqual(hourly.check_latest(self.settings(), clock=clock_at())[0], hourly.EXIT_OK)

    def test_tampered_feed_copy_keeps_the_previous_latest_set(self):
        settings = self.settings()
        good = run(settings)
        before = tree_bytes(self.delivery)
        with self.tamper(hourly.FEED_NAME):
            second = run(settings, self.changed_source())
        self.assertEqual(self.status()["delivery_status"], "verify_failed")
        self.assert_drive_pair_is(good["run_id"])
        self.assertEqual((self.delivery / hourly.FEED_NAME).read_bytes(), before[hourly.FEED_NAME])
        # N18: the feed did not reach Drive, so it is not reported as published.
        self.assertEqual(self.status()["feed_status"], "failed")
        self.assertEqual(second["feed"]["failure_class"], "feed_publish_failed")
        # The archive still advanced: it does not depend on the Drive copy.
        archive_latest = json.loads((self.archive / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(archive_latest["run_id"], second["run_id"])

    def test_tampered_archive_feed_write_keeps_both_previous_feeds(self):
        # N18: the archive feed goes through a verified temp file; a bad write keeps the old feed
        # there, and Drive is not sent a feed the archive lacks. The latest pair still advances.
        settings = self.settings()
        run(settings)
        before = {root: (root / hourly.FEED_NAME).read_bytes() for root in (self.archive, self.delivery)}
        with self.tamper(hourly.FEED_NAME, root=self.archive):
            second = run(settings, self.changed_source())
        for root, data in before.items():
            self.assertEqual((root / hourly.FEED_NAME).read_bytes(), data, root.name)
        self.assertEqual(self.status()["feed_status"], "failed")
        self.assertEqual(self.status()["delivery_status"], "handed_to_drive_sync_folder")
        self.assert_drive_pair_is(second["run_id"])
        latest = json.loads((self.delivery / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(latest["feed"]["status"], "failed", "the Drive manifest does not claim a new feed")
        self.assertEqual([p.name for p in self.archive.glob("*.tmp")], [])

    def test_tampered_archive_manifest_write_keeps_the_previous_archive_pair(self):
        settings = settings_for(self.tmp)
        good = run(settings)
        with self.tamper(hourly.LATEST_MANIFEST_NAME, root=self.archive):
            second = run(settings, self.changed_source())
        self.assertEqual(second["status"], "success", "the hourly archive itself is good")
        status = json.loads((self.archive / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual((status["status"], status["failure_class"]), ("failed", "latest_publish_failed"))
        latest = json.loads((self.archive / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(latest["run_id"], good["run_id"])
        data = (self.archive / hourly.LATEST_CSV_NAME).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), latest["csv"]["sha256"])

    def test_publish_set_moves_nothing_until_every_copy_verifies(self):
        root = Path(self.tmp) / "set"
        root.mkdir()
        (root / "a").write_bytes(b"old a")
        (root / "b").write_bytes(b"old b")
        real = hourly.copy_bytes_exclusive

        def bad_b(path, data):
            real(path, data + b"x" if Path(path).name.startswith(".b.") else data)

        with mock.patch.object(hourly, "copy_bytes_exclusive", side_effect=bad_b):
            with self.assertRaises(hourly.VerifyError):
                hourly.publish_verified_set([(root / "a", b"new a"), (root / "b", b"new b")], "t1")
        self.assertEqual(((root / "a").read_bytes(), (root / "b").read_bytes()), (b"old a", b"old b"))
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["a", "b"])
        with mock.patch.object(hourly, "copy_bytes_exclusive", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                hourly.publish_verified_set([(root / "a", b"new a"), (root / "b", b"new b")], "t2")
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["a", "b"])
        hashes = hourly.publish_verified_set([(root / "a", b"new a"), (root / "b", b"new b")], "t3")
        self.assertEqual(((root / "a").read_bytes(), (root / "b").read_bytes()), (b"new a", b"new b"))
        self.assertEqual(hashes, [hashlib.sha256(b"new a").hexdigest(), hashlib.sha256(b"new b").hexdigest()])

    def test_publish_set_drops_the_rest_when_a_move_fails(self):
        # The documented limit: a target a sync client still holds after the retries stops the set.
        root = Path(self.tmp) / "set"
        root.mkdir()
        real = os.replace

        def held_b(src, dst):
            if Path(dst).name == "b":
                raise PermissionError("held")
            real(src, dst)

        with mock.patch.object(hourly.os, "replace", side_effect=held_b), \
                mock.patch.object(hourly.time, "sleep"):
            with self.assertRaises(PermissionError):
                hourly.publish_verified_set([(root / "a", b"a"), (root / "b", b"b"), (root / "c", b"c")], "t1")
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["a"], "no temp file is left")

    def test_first_run_with_bad_copy_leaves_no_latest(self):
        with self.tamper(hourly.LATEST_CSV_NAME):
            run(self.settings())
        self.assertFalse((self.delivery / hourly.LATEST_CSV_NAME).exists())
        self.assertFalse((self.delivery / hourly.LATEST_MANIFEST_NAME).exists())
        self.assertEqual(self.status()["delivery_status"], "verify_failed")

    def test_cli_exit_code_when_delivery_fails(self):
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw_settings(self.tmp, output={"delivery_root": str(self.delivery)})), encoding="utf-8")
        with self.tamper(hourly.LATEST_CSV_NAME), contextlib.redirect_stdout(io.StringIO()) as out:
            code = hourly.main(["--config", str(path)], source=FakeSource(), environ=ENV, clock=clock_at())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["delivery_status"], "verify_failed")

    def test_null_delivery_root_works(self):
        manifest = run(settings_for(self.tmp))
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["delivery"]["status"], "not_configured")
        self.assertFalse(self.delivery.exists())

    def test_failed_run_status_reaches_delivery_root(self):
        manifest = run(self.settings(), FakeSource(multi_uom=1))
        self.assertEqual(manifest["delivery"]["status"], "skipped_failed_run")
        status = self.status()
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["daily_close_status"], "skipped_failed_run")
        self.assertFalse((self.delivery / hourly.LATEST_CSV_NAME).exists())
        self.assertFalse((self.delivery / hourly.FEED_NAME).exists())
        self.assertFalse((self.delivery / hourly.DAILY_DIR).exists())
        self.assertFalse(self.run_dir(manifest, self.delivery).exists())


class StaleCheckTests(TempDirTestCase):
    def check(self, settings, now):
        return hourly.check_latest(settings, clock=clock_at(now))

    def test_exit_codes(self):
        settings = settings_for(self.tmp)
        code, result = self.check(settings, FIXED_NOW)
        self.assertEqual((code, result["status"]), (3, "missing"))
        run(settings)
        code, result = self.check(settings, FIXED_NOW + timedelta(minutes=30))
        self.assertEqual((code, result["status"]), (0, "fresh"))
        # Age counts from as_of_local (the SQL read time, 14:00:06).
        code, result = self.check(settings, SQL_NOW + timedelta(minutes=70, seconds=59))
        self.assertEqual((code, result["status"]), (0, "fresh"))
        code, result = self.check(settings, SQL_NOW + timedelta(minutes=71))
        self.assertEqual((code, result["status"], result["age_minutes"]), (2, "stale", 71))
        (self.archive / hourly.LATEST_CSV_NAME).write_bytes(b"tampered")
        code, result = self.check(settings, FIXED_NOW)
        self.assertEqual((code, result["status"]), (3, "invalid"))

    def test_prefers_delivery_root_and_cli(self):
        raw = raw_settings(self.tmp, output={"delivery_root": str(self.delivery)})
        run(hourly.validate_settings(raw))
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        (self.archive / hourly.LATEST_MANIFEST_NAME).unlink()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = hourly.main(["--config", str(path), "--check-latest"], clock=clock_at(FIXED_NOW + timedelta(minutes=5)))
        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertEqual(json.loads(printed)["status"], "fresh")
        self.assertNotIn(str(self.tmp), printed)
        (self.delivery / hourly.LATEST_MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = hourly.main(["--config", str(path), "--check-latest"], clock=clock_at(FIXED_NOW))
        self.assertEqual(code, 3)


REAL_BOOT_TIME = hourly._boot_time


class LockTests(TempDirTestCase):
    OTHER_RUN = "deadbeefcafe0001"
    OTHER_PID = 4242
    # Mocked boot time: long before every lock below unless a test says otherwise.
    BOOT = FIXED_NOW - timedelta(days=30)

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(hourly, "_boot_time", return_value=self.BOOT)
        self.boot_time = patcher.start()
        self.addCleanup(patcher.stop)

    def write_lock(self, started, content=None):
        self.archive.mkdir(parents=True, exist_ok=True)
        lock = self.archive / hourly.LOCK_NAME
        if content is None:
            content = json.dumps({"run_id": self.OTHER_RUN, "pid": self.OTHER_PID, "started_at": started.isoformat()})
        lock.write_text(content, encoding="utf-8")
        return lock

    def test_concurrent_run_writes_already_running_manifest(self):
        lock = self.write_lock(FIXED_NOW - timedelta(minutes=2))
        before = lock.read_bytes()
        source = FakeSource()
        with mock.patch.object(hourly, "_pid_running", side_effect=AssertionError("young lock: PID not consulted")):
            manifest = run(settings_for(self.tmp), source)
        self.assertEqual(manifest["failure_class"], "already_running")
        self.assertEqual(source.sessions, 0, "no SQL read while another run holds the lock")
        on_disk = json.loads((self.run_dir(manifest) / hourly.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["failure_class"], "already_running")
        self.assertEqual(lock.read_bytes(), before, "the other run's lock is never removed")

    def test_young_lock_of_dead_process_still_gives_already_running(self):
        lock = self.write_lock(FIXED_NOW - timedelta(minutes=hourly.LOCK_STALE_AFTER_MINUTES))
        with mock.patch.object(hourly, "_pid_running", return_value=False):
            manifest = run(settings_for(self.tmp))
        self.assertEqual(manifest["failure_class"], "already_running")
        self.assertTrue(lock.exists())

    def test_killed_run_lock_is_cleared_and_run_succeeds_with_warning(self):
        started = FIXED_NOW - timedelta(hours=2)
        lock = self.write_lock(started)
        with mock.patch.object(hourly, "_pid_running", return_value=False) as pid_running:
            manifest = run(settings_for(self.tmp))
        pid_running.assert_called_once_with(self.OTHER_PID, started)
        self.assertEqual((manifest["status"], manifest["failure_class"]), ("success", None))
        warning = "Cleared a lock left by run deadbeef, whose process is no longer running (lock age 120 minutes)"
        self.assertIn(warning, manifest["warnings"])
        on_disk = json.loads((self.run_dir(manifest) / hourly.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertIn(warning, on_disk["warnings"])
        latest = json.loads((self.archive / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertIn(warning, latest["warnings"])
        self.assertFalse(lock.exists(), "this run's own lock is released at the end")
        # The next hour runs normally, with no warning.
        later = run(settings_for(self.tmp), clock=clock_at(FIXED_NOW + timedelta(hours=1)))
        self.assertEqual((later["status"], later["warnings"]), ("success", []))

    def test_old_lock_of_live_process_is_respected(self):
        lock = self.write_lock(FIXED_NOW - timedelta(hours=2))
        before = lock.read_bytes()
        source = FakeSource()
        with mock.patch.object(hourly, "_pid_running", return_value=True):
            manifest = run(settings_for(self.tmp), source)
        self.assertEqual(manifest["failure_class"], "already_running")
        self.assertEqual(source.sessions, 0)
        self.assertEqual(lock.read_bytes(), before)

    def test_untrustworthy_lock_is_stale_lock_and_kept(self):
        old = (FIXED_NOW - timedelta(hours=2)).isoformat()
        contents = (
            "{not json",
            json.dumps({"run_id": self.OTHER_RUN, "started_at": old}),
            json.dumps({"run_id": self.OTHER_RUN, "pid": True, "started_at": old}),
            json.dumps({"run_id": self.OTHER_RUN, "pid": self.OTHER_PID, "started_at": "2026-10-23T12:00:05"}),
        )
        for content in contents:
            with self.subTest(content=content):
                lock = self.write_lock(None, content)
                with mock.patch.object(hourly, "_pid_running", side_effect=AssertionError("not consulted")):
                    manifest = run(settings_for(self.tmp))
                self.assertEqual(manifest["failure_class"], "stale_lock")
                self.assertEqual(lock.read_text(encoding="utf-8"), content)

    def test_pid_running_on_real_processes(self):
        now = datetime.now(SGT)
        self.assertTrue(hourly._pid_running(os.getpid(), now))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=60)
        self.assertFalse(hourly._pid_running(child.pid, now - timedelta(hours=2)))

    @unittest.skipUnless(os.name == "nt", "process creation time check is Windows-only")
    def test_reused_pid_is_not_the_lock_owner_on_windows(self):
        # This test process was created long after a lock written 2 hours ago.
        self.assertFalse(hourly._pid_running(os.getpid(), datetime.now(SGT) - timedelta(hours=2)))

    def test_lock_written_before_last_boot_is_cleared_even_if_pid_looks_running(self):
        # After a reboot an uninspectable system process can reuse the killed run's PID.
        started = FIXED_NOW - timedelta(hours=2)
        lock = self.write_lock(started)
        self.boot_time.return_value = started + timedelta(minutes=30)
        with mock.patch.object(hourly, "_pid_running", side_effect=AssertionError("pre-boot lock: PID not consulted")):
            manifest = run(settings_for(self.tmp))
        self.assertEqual((manifest["status"], manifest["failure_class"]), ("success", None))
        warning = "Cleared a lock left by run deadbeef, written before the last restart (lock age 120 minutes)"
        self.assertIn(warning, manifest["warnings"])
        on_disk = json.loads((self.run_dir(manifest) / hourly.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertIn(warning, on_disk["warnings"])
        self.assertFalse(lock.exists())

    def test_post_boot_live_lock_or_unknown_boot_keeps_already_running(self):
        started = FIXED_NOW - timedelta(hours=2)
        tolerance = timedelta(seconds=hourly.LOCK_BOOT_TOLERANCE_SECONDS)
        for boot in (started - timedelta(hours=1), started, started + tolerance - timedelta(seconds=1), None):
            with self.subTest(boot=boot):
                lock = self.write_lock(started)
                before = lock.read_bytes()
                self.boot_time.return_value = boot
                source = FakeSource()
                with mock.patch.object(hourly, "_pid_running", return_value=True) as pid_running:
                    manifest = run(settings_for(self.tmp), source)
                pid_running.assert_called_once_with(self.OTHER_PID, started)
                self.assertEqual(manifest["failure_class"], "already_running")
                self.assertEqual(source.sessions, 0)
                self.assertEqual(lock.read_bytes(), before)

    def test_young_lock_before_boot_waits_for_the_age_gate(self):
        lock = self.write_lock(FIXED_NOW - timedelta(minutes=10))
        self.boot_time.return_value = FIXED_NOW - timedelta(minutes=5)
        with mock.patch.object(hourly, "_pid_running", side_effect=AssertionError("young lock: PID not consulted")):
            manifest = run(settings_for(self.tmp))
        self.assertEqual(manifest["failure_class"], "already_running")
        self.assertTrue(lock.exists())

    def test_boot_time_is_real_on_windows_and_unknown_elsewhere(self):
        boot = REAL_BOOT_TIME()
        if os.name != "nt":
            self.assertIsNone(boot)
            return
        now = datetime.now(SGT)
        self.assertIsNotNone(boot.tzinfo)
        self.assertLess(boot, now)
        self.assertGreater(boot, datetime(2000, 1, 1, tzinfo=SGT))


class PrivacyTests(TempDirTestCase):
    def sql_login_settings(self):
        return settings_for(
            self.tmp,
            identity={"auth_mode": "sql_login", "sql_login": FAKE_SQL_LOGIN, "credential_target": FAKE_CRED_TARGET},
            schedule={"logon_type": "Password"},
            output={"delivery_root": str(self.delivery)},
        )

    def test_connection_string_modes(self):
        integrated = hourly.build_connection_string(settings_for(self.tmp))
        self.assertIn("Trusted_Connection=yes", integrated)
        self.assertIn("DATABASE=%s" % FAKE_DB, integrated)
        self.assertNotIn("PWD", integrated)
        sql = hourly.build_connection_string(self.sql_login_settings(), password="pa}ss;word")
        self.assertIn("UID={%s}" % FAKE_SQL_LOGIN, sql)
        self.assertIn("PWD={pa}}ss;word}", sql)
        with self.assertRaises(hourly.CredentialUnavailableError):
            hourly.build_connection_string(self.sql_login_settings())

    def test_manifest_and_output_hold_no_identity_or_host(self):
        settings = self.sql_login_settings()
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw_settings(
            self.tmp,
            identity={"auth_mode": "sql_login", "sql_login": FAKE_SQL_LOGIN, "credential_target": FAKE_CRED_TARGET},
            schedule={"logon_type": "Password"},
            output={"delivery_root": str(self.delivery)},
        )), encoding="utf-8")
        env = dict(ENV, AUTOCOUNT_EXPECTED_SQL_LOGIN=FAKE_SQL_LOGIN)
        source = FakeSource(identity=observed_identity(login=FAKE_SQL_LOGIN))
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = hourly.main(["--config", str(path)], source=source, environ=env, clock=clock_at())
        self.assertEqual(code, 0)
        # A failing run too.
        with contextlib.redirect_stdout(io.StringIO()) as out2:
            hourly.main(["--config", str(path)], source=FakeSource(identity=observed_identity(login="other")), environ=env, clock=clock_at())
        texts = [out.getvalue(), err.getvalue(), out2.getvalue()]
        for root in (self.archive, self.delivery):
            texts.extend(p.read_text(encoding="utf-8") for p in root.rglob("*.json"))
        forbidden = [FAKE_SQL_LOGIN, FAKE_USER, FAKE_CRED_TARGET, FAKE_SERVER, "FAKE-SQL-HOST", "Trusted_Connection",
                     "PWD=", "UID=", "DRIVER=", "other"]
        for text in texts:
            for value in forbidden:
                self.assertNotIn(value, text)
        self.assertEqual(settings["identity"]["sql_login"], FAKE_SQL_LOGIN)

    def test_csv_values_never_printed(self):
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw_settings(self.tmp)), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            hourly.main(["--config", str(path)], source=FakeSource(), environ=ENV, clock=clock_at())
        for value in ("FAKE-0001", "Fake item", "LOC-A"):
            self.assertNotIn(value, out.getvalue())


class QueryContractTests(unittest.TestCase):
    QUERIES = (
        hourly.IDENTITY_QUERY,
        hourly.CHANGE_COUNT_QUERY,
        hourly.SQL_TIME_QUERY,
        hourly.BALANCE_QUERY,
        hourly.MULTI_UOM_QUERY,
        hourly.ITEMS_WITHOUT_ROWS_QUERY,
        hourly.sync_query("LastFinishSyncTime"),
        hourly.sync_query("LastTransFinishSyncTime"),
        # Round 6 (G2 r6 H5): the ledger, item master and location reads.
        hourly.LEDGER_QUERY,
        hourly.ITEM_MASTER_QUERY,
        hourly.LOCATION_QUERY,
        # Dashboard v2: barcode, reorder level and minimum quantity per item and UOM.
        hourly.ITEM_UOM_QUERY,
    )

    def test_no_cost_column(self):
        for query in self.QUERIES:
            self.assertNotIn("cost", query.lower())

    def test_read_only_statements(self):
        forbidden = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|EXEC|EXECUTE|TRUNCATE|GRANT)\b", re.I)
        for query in self.QUERIES:
            self.assertIsNone(forbidden.search(query), query)
            self.assertTrue(query.lstrip().upper().startswith("SELECT"))
            self.assertNotIn("NOLOCK", query.upper())
            self.assertNotIn("SNAPSHOT", query.upper())
            self.assertNotIn("CHANGELOG", query.upper().replace("CHANGECOUNT", ""))

    def test_balance_query_shape(self):
        q = hourly.BALANCE_QUERY
        self.assertIn("FROM dbo.ItemBatchBalQty AS b", q)
        self.assertIn("JOIN dbo.Item AS i", q)
        self.assertIn("JOIN dbo.Location AS l", q)
        self.assertIn("GROUP BY b.ItemCode, b.UOM, b.Location", q)
        self.assertNotIn("CSGN", q.upper())

    def test_sync_query_selects_location_and_time_only(self):
        q = hourly.sync_query("LastFinishSyncTime")
        self.assertEqual(q, "SELECT Location, [LastFinishSyncTime] AS LastSyncTime FROM dbo.SyncRegistration")

    def test_session_sets_isolation_and_lock_timeout(self):
        executed = []

        class Cursor:
            def execute(self, sql, *params):
                executed.append(sql)

        class Connection:
            timeout = 0

            def cursor(self):
                return Cursor()

        settings = hourly.validate_settings(raw_settings(tempfile.gettempdir()))
        connection = Connection()
        hourly.SqlServerSession(connection, settings, RuntimeError)
        self.assertEqual(connection.timeout, 60)
        self.assertEqual(executed, ["SET TRANSACTION ISOLATION LEVEL READ COMMITTED; SET LOCK_TIMEOUT 5000;"])

    def test_lock_timeout_error_classified(self):
        self.assertIsInstance(hourly._classify_sql_error(RuntimeError("HYT00", "[1222] Lock request time out period exceeded.")), hourly.SourceLockTimeout)
        self.assertIsInstance(hourly._classify_sql_error(RuntimeError("42000", "other")), hourly.SourceReadError)


class LiveCheckSqlTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "sql" / "autocount2" / "stock_balance_hourly_live_check.readonly.template.sql").read_text(encoding="utf-8")
        code = re.sub(r"/\*.*?\*/", "", self.text, flags=re.S)
        self.code = "\n".join(line.split("--", 1)[0] for line in code.splitlines())

    def test_database_guard_comes_first(self):
        statements = [s.strip() for s in self.code.split(";") if s.strip()]
        self.assertEqual(statements[0], "SET NOCOUNT ON")
        guard = statements[1]
        self.assertIn("IF DB_NAME() <> N'$(ExpectedDb)'", guard)
        self.assertIn("RAISERROR", guard)
        self.assertLess(self.code.index("SET NOEXEC ON"), self.code.index("SET LOCK_TIMEOUT 5000"))
        self.assertLess(self.code.index("SET LOCK_TIMEOUT 5000"), self.code.index("SELECT"))

    def test_read_only_and_no_cost(self):
        self.assertIsNone(re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|EXEC|TRUNCATE|GRANT)\b", self.code, re.I))
        self.assertNotIn("cost", self.code.lower())
        self.assertNotIn("changelog", self.code.lower())
        self.assertNotIn("NOLOCK", self.code.upper())
        self.assertNotIn("DocInfo", self.code)

    def test_item_index_column_check(self):
        for table, column in (("Item", "ItemGroup"), ("Item", "ItemBrand"), ("ItemUOM", "BarCode"),
                              ("ItemUOM", "ReOLevel"), ("ItemUOM", "MinQty")):
            self.assertIn("COL_LENGTH(N'dbo.%s', N'%s')" % (table, column), self.code)


class InstallerTests(unittest.TestCase):
    PATH = ROOT / "scripts" / "install_autocount_stock_balance_hourly_task.ps1"

    def setUp(self):
        self.text = self.PATH.read_text(encoding="utf-8")

    def test_static_contract(self):
        self.assertIn("-MultipleInstances IgnoreNew", self.text)
        self.assertNotIn("-Force", self.text)
        self.assertIn("-ExecutionTimeLimit (New-TimeSpan -Minutes 15)", self.text)
        self.assertIn("-RunLevel Limited", self.text)
        self.assertIn("-RepetitionInterval (New-TimeSpan -Hours 1)", self.text)
        self.assertIn("Get-ScheduledTask -TaskName $TaskName", self.text)
        self.assertNotIn("GetCurrent()", self.text, "no default Windows user")
        self.assertRegex(self.text, r"\[Parameter\(Mandatory = \$true\)\]\s*\[string\]\$UserId")
        self.assertIn("separate owner OK", self.text)

    def test_execution_limit_matches_the_lock_rules(self):
        limit = re.search(r"-ExecutionTimeLimit \(New-TimeSpan -Minutes (\d+)\)", self.text)
        self.assertEqual(int(limit.group(1)), hourly.TASK_EXECUTION_LIMIT_MINUTES)
        self.assertGreater(hourly.LOCK_STALE_AFTER_MINUTES, hourly.TASK_EXECUTION_LIMIT_MINUTES)

    def test_repetition_never_stops_a_run(self):
        self.assertIn("$trigger.Repetition.StopAtDurationEnd = $false", self.text)
        self.assertNotIn("StopAtDurationEnd = $true", self.text)

    def test_single_hourly_trigger_without_daily_start(self):
        # R1 (28 Oct 2026): a daily 00:00 start plus a one-day hourly repeat started 00:00 twice.
        code = re.sub(r"<#.*?#>", "", self.text, flags=re.S)
        code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
        self.assertEqual(code.count("New-ScheduledTaskTrigger"), 1, "exactly one trigger")
        self.assertIn(
            "$trigger = New-ScheduledTaskTrigger -Once -At $startAt -RepetitionInterval (New-TimeSpan -Hours 1)\n",
            code,
        )
        self.assertNotIn("-Daily", code)
        self.assertNotIn("-RepetitionDuration", code)
        self.assertNotIn("MaxValue", code)
        self.assertEqual(code.count("-Trigger $trigger "), 2, "both registration paths use the one trigger")
        self.assertLess(code.index('$repeatEvery -cne "PT1H"'), code.index("if ($DryRun) {"))

    def test_run_minute_comes_from_the_validated_settings(self):
        self.assertNotIn("[int]$RunMinute = 0", self.text, "no separate RunMinute parameter")
        self.assertIn("$RunMinute = $validated.run_minute", self.text)
        self.assertIn("$RunMinute -lt 0 -or $RunMinute -gt 59", self.text)
        self.assertIn("AddMinutes($RunMinute)", self.text)

    def test_python_exe_is_a_mandatory_full_path(self):
        self.assertRegex(self.text, r"\[Parameter\(Mandatory = \$true\)\]\s*\[string\]\$PythonExe\s*,")
        self.assertNotIn('$PythonExe = "python.exe"', self.text)
        self.assertIn("[System.IO.Path]::IsPathRooted($PythonExe)", self.text)
        self.assertIn("Test-Path -LiteralPath $PythonExe -PathType Leaf", self.text)

    def test_logon_type_comes_from_the_validated_settings(self):
        self.assertNotIn("$LogonType =", self.text, "no free LogonType parameter")
        self.assertIn("--dry-run-config", self.text)
        self.assertIn("$validated.scheduled_logon_type", self.text)
        self.assertIn('($authMode -ceq "windows_integrated") -and ($logonType -ceq "S4U")', self.text)
        self.assertIn('($authMode -ceq "sql_login") -and ($logonType -ceq "Password")', self.text)
        self.assertIn("-LogonType S4U", self.text)
        self.assertEqual(sorted(hourly.SUPPORTED_LOGON_PAIRS.items()), [("sql_login", "Password"), ("windows_integrated", "S4U")])

    def test_password_logon_prompts_and_never_echoes(self):
        self.assertIn("Get-Credential -UserName $UserId", self.text)
        self.assertIn("-User $UserId", self.text)
        self.assertIn("-Password $credential.GetNetworkCredential().Password", self.text)
        for forbidden in ("ConvertFrom-SecureString", "Out-File", "Set-Content", "Add-Content", "Export-Clixml",
                          "Start-Transcript", "Write-Output", "-Verbose"):
            self.assertNotIn(forbidden, self.text)
        for line in self.text.splitlines():
            if "Write-Host" in line:
                self.assertNotIn("$credential", line)
                self.assertNotIn("Password", line.replace("LogonType Password", ""))
        self.assertEqual(self.text.count("GetNetworkCredential()"), 1)

    def test_v7_supervised_run_is_named(self):
        self.assertIn("Start-ScheduledTask", self.text)
        self.assertIn("last_run_status.json", self.text)
        self.assertIn("Disable-ScheduledTask", self.text)

    def test_powershell_parse_only(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("Neither pwsh nor powershell is available for the parse-only check")
        command = (
            "$errs = $null; "
            "[System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$null, [ref]$errs) | Out-Null; "
            "if (@($errs).Count -gt 0) { $errs | ForEach-Object { Write-Output $_.Message }; exit 1 } else { exit 0 }"
        ) % str(self.PATH).replace("'", "''")
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repo_root_is_worked_out_in_the_body(self):
        # $PSScriptRoot is empty in param() defaults under Windows PowerShell 5.1 (VM step V7).
        # The whole param(...) block is checked, so the old default fails this test.
        block = re.search(r"^param\((.*?)^\)", self.text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(block)
        self.assertNotIn("$PSScriptRoot", block.group(1))
        self.assertRegex(block.group(1), r'\[string\]\$RepoRoot = ""\s*,')
        body = self.text[block.end():]
        self.assertIn("if ([string]::IsNullOrWhiteSpace($RepoRoot)) {", body)
        self.assertIn('$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path', body)

    def test_task_path_folder_and_root_warning(self):
        self.assertRegex(self.text, r'\[string\]\$TaskPath = "\\XB\\"\s*,')
        self.assertIn("$TaskPath -cnotmatch '\\A(", self.text)
        self.assertIn(")+\\\\\\z'", self.text)
        self.assertIn("Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue", self.text)
        self.assertIn('Get-ScheduledTask -TaskName $TaskName -TaskPath "\\" -ErrorAction SilentlyContinue', self.text)
        self.assertEqual(self.text.count("Register-ScheduledTask `"), 2)
        self.assertEqual(self.text.count("-TaskPath $TaskPath `"), 2, "both registrations use the folder")

    def test_dry_run_stops_before_prompt_and_register(self):
        dry = self.text.index("if ($DryRun) {")
        self.assertLess(self.text.index("--dry-run-config"), dry)
        self.assertLess(dry, self.text.index("Get-Credential -UserName"))
        self.assertLess(dry, self.text.index("Register-ScheduledTask `"))
        self.assertIn("Nothing was registered.", self.text[dry:self.text.index("Register-ScheduledTask `")])


class InstallerPowerShell51Tests(TempDirTestCase):
    """Runs the installer under Windows PowerShell 5.1 with -File and -DryRun.

    -DryRun stops before the password prompt and Register-ScheduledTask, and the task name is a
    test-only name, so nothing is ever registered. A stub pyodbc module on PYTHONPATH stands in
    for the driver (the installer only imports it). Fake user name and fake settings only.
    """

    PATH = ROOT / "scripts" / "install_autocount_stock_balance_hourly_task.ps1"
    TASK_NAME = "XB AC2 installer test (never registered)"

    def setUp(self):
        super().setUp()
        if os.name != "nt":
            self.skipTest("Windows PowerShell 5.1 runs only on Windows")
        self.shell = shutil.which("powershell")
        if self.shell is None:
            self.skipTest("Windows PowerShell is not installed")
        stub = Path(self.tmp) / "stub"
        stub.mkdir()
        (stub / "pyodbc.py").write_text("# test stub: the installer only imports pyodbc\n", encoding="utf-8")
        self.env = dict(os.environ, PYTHONPATH=str(stub))
        self.config = self.write_config(raw_settings(self.tmp))

    def write_config(self, raw, name="settings.local.json"):
        path = Path(self.tmp) / name
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def run_installer(self, *extra, config=None):
        args = [self.shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(self.PATH), "-UserId", "EXAMPLE-PC\\example-user",
                "-ConfigPath", str(config or self.config), "-PythonExe", sys.executable,
                "-TaskName", self.TASK_NAME, "-DryRun", *extra]
        done = subprocess.run(args, capture_output=True, text=True, timeout=120, env=self.env, cwd=self.tmp)
        return done.returncode, done.stdout + done.stderr

    def test_runs_under_windows_powershell_5_1(self):
        done = subprocess.run([self.shell, "-NoProfile", "-NonInteractive", "-Command",
                               "$PSVersionTable.PSVersion.Major"],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.stdout.strip(), "5", done.stdout + done.stderr)

    def test_dry_run_without_repo_root_finds_this_clone(self):
        code, out = self.run_installer()
        self.assertEqual(code, 0, out)
        self.assertIn("Dry run: would register \\XB\\" + self.TASK_NAME, out)
        self.assertIn("Repo root: " + str(ROOT), out)
        self.assertIn("LogonType S4U (windows_integrated)", out)
        # The trigger object is really built on this host: one trigger, hourly, no end.
        self.assertIn("Trigger: one trigger, starts 00:00, repeats every PT1H, no end", out)
        self.assertIn("Nothing was registered.", out)

    def test_dry_run_with_empty_repo_root_finds_this_clone(self):
        code, out = self.run_installer("-RepoRoot", "")
        self.assertEqual(code, 0, out)
        self.assertIn("Repo root: " + str(ROOT), out)

    def test_dry_run_password_pair_never_prompts(self):
        # Windows PowerShell 5.1 does not make Get-Credential fail under -NonInteractive: it waits
        # on the prompt. A regression past the dry run therefore shows as a timeout here, not as a
        # quick failure; exit 0 within the timeout proves the dry run stops first (G2 N14).
        config = self.write_config(raw_settings(
            self.tmp,
            identity={"auth_mode": "sql_login", "sql_login": FAKE_SQL_LOGIN, "credential_target": FAKE_CRED_TARGET},
            schedule={"logon_type": "Password"},
        ), name="sql.local.json")
        code, out = self.run_installer(config=config)
        self.assertEqual(code, 0, out)
        self.assertIn("LogonType Password (sql_login)", out)
        self.assertIn("Nothing was registered.", out)

    def test_dry_run_accepts_other_task_paths(self):
        for good in ("\\XB\\Stock export\\", "\\Other\\", "\\X-B_1\\"):
            with self.subTest(task_path=good):
                code, out = self.run_installer("-TaskPath", good)
                self.assertEqual(code, 0, out)
                self.assertIn("would register " + good + self.TASK_NAME, out)

    def test_bad_task_path_is_refused(self):
        bad_paths = ("", "\\", "XB", "\\XB", "XB\\", "\\XB\\\\", "\\X;B\\", "\\..\\", "\\XB\\..\\",
                     "\\XB/\\", "\\ XB\\", "\\XB \\", "\\XB\\\n", "\\XB\\\t", "\\K\\")
        for bad in bad_paths:
            with self.subTest(task_path=bad):
                code, out = self.run_installer("-TaskPath", bad)
                self.assertNotEqual(code, 0, out)
                self.assertIn("TaskPath must look like", out)
                self.assertNotIn("Dry run:", out)


# ---------------------------------------------------------------------------------------------
# Round 6: ledger check, daily close, backfill, restate window, feed, retention (#245)
# ---------------------------------------------------------------------------------------------


def at(year, month, day, hour=0, minute=0, second=5):
    return datetime(year, month, day, hour, minute, second, tzinfo=SGT)


def ledger_source(ledger, items, **kwargs):
    """A fake source whose stored balances are the ledger totals, so the balance check passes."""
    totals = {}
    for r in ledger:
        key = (r["ItemCode"], r["UOM"], r["Location"])
        totals[key] = totals.get(key, Decimal(0)) + r["Qty"]
    by_item = {i["ItemCode"]: i for i in items}
    balances = [
        bal(item, loc, qty, uom=uom, base=by_item[item]["BaseUOM"], active=by_item[item]["IsActive"],
            stock=by_item[item]["StockControl"], desc=by_item[item]["ItemDescription"])
        for (item, uom, loc), qty in sorted(totals.items())
    ]
    kwargs.setdefault("no_row_items", [])
    return FakeSource(balances=balances, ledger=ledger, item_master=items, **kwargs)


# Fake August: a timed POS-like sale before go-live, opening stock on 31 Jul, a fee code that is
# not stock-controlled, and ordinary movements.
AUG_ITEMS = [
    master("FAKE-A"),
    master("FAKE-B"),
    master("FAKE-LOW", desc="Fake low item"),
    master("FAKE-FEE", stock="F", desc="Fake marketplace fee"),
]


def aug_ledger():
    return [
        led("FAKE-A", "LOC-A", -1, day=datetime(2026, 7, 3, 18, 34, 0)),
        led("FAKE-A", "LOC-A", 10, day=date(2026, 7, 31)),
        led("FAKE-B", "LOC-B", 5, day=date(2026, 7, 31)),
        led("FAKE-LOW", "LOC-B", 2, day=date(2026, 7, 31)),
        led("FAKE-FEE", "LOC-W", -3, day=date(2026, 8, 1)),
        led("FAKE-A", "LOC-A", -2, day=date(2026, 8, 2)),
    ]


class Round6TestCase(TempDirTestCase):
    def settings(self, **overrides):
        base = {"daily_close": {"backfill_start": "2026-08-01"}, "location_labels": {"LOC-A": "LOC-A - Example outlet"}}
        base.update(overrides)
        return settings_for(self.tmp, **base)

    def run_at(self, settings, when, ledger=None, items=None, **kwargs):
        source = ledger_source(aug_ledger() if ledger is None else ledger, AUG_ITEMS if items is None else items,
                               sql_now=when + timedelta(seconds=1), **kwargs)
        return run(settings, source, clock=clock_at(when))

    def daily(self, day, root=None, suffix=".csv"):
        return Path(root or self.archive) / "daily" / day[:7] / ("stock_close_%s%s" % (day, suffix))

    def daily_rows(self, day, root=None):
        return read_csv(self.daily(day, root))

    def daily_meta(self, day, root=None):
        return json.loads(self.daily(day, root, ".manifest.json").read_text(encoding="utf-8"))

    def feed(self, root=None):
        return json.loads(gzip.decompress((Path(root or self.archive) / hourly.FEED_NAME).read_bytes()))

    def qty(self, day, item, loc):
        found = [r["CloseQty"] for r in self.daily_rows(day) if r["ItemCode"] == item and r["Location"] == loc]
        return found[0] if found else None


class LedgerCloseTests(Round6TestCase):
    def test_sum_from_first_row_not_from_the_backfill_date(self):
        manifest = self.run_at(self.settings(), at(2026, 8, 4))
        self.assertEqual(manifest["status"], "success")
        close = manifest["daily_close"]
        self.assertEqual((close["status"], close["mode"], close["days_written"]), ("published", "initial", 3))
        self.assertEqual(close["published_close_day"], "2026-08-03")
        # 1 Aug = the 3 Jul sale (-1) + 31 Jul opening (+10), both before the backfill start.
        self.assertEqual(self.qty("2026-08-01", "FAKE-A", "LOC-A"), "9")
        self.assertEqual(self.qty("2026-08-02", "FAKE-A", "LOC-A"), "7")
        self.assertEqual(self.qty("2026-08-03", "FAKE-B", "LOC-B"), "5")
        self.assertFalse(self.daily("2026-07-31").exists(), "nothing before backfill_start")
        self.assertFalse(self.daily("2026-08-04").exists(), "today is not closed yet")
        meta = self.daily_meta("2026-08-01")
        self.assertEqual(meta["close_rule"].split(" dated before ")[1][:16], "2026-08-02 00:00")
        self.assertEqual((meta["revision"], meta["write_reason"], meta["final_from"]), (1, "rebuild", "2026-08-09"))
        self.assertEqual(meta["csv"]["sha256"], hashlib.sha256(self.daily("2026-08-01").read_bytes()).hexdigest())

    def test_as_of_boundary_with_timed_rows(self):
        ledger = aug_ledger() + [
            led("FAKE-B", "LOC-B", -1, day=datetime(2026, 8, 1, 23, 59, 59)),
            led("FAKE-B", "LOC-B", -2, day=datetime(2026, 8, 2, 0, 0, 0)),
        ]
        self.run_at(self.settings(), at(2026, 8, 3), ledger=ledger)
        self.assertEqual(self.qty("2026-08-01", "FAKE-B", "LOC-B"), "4", "23:59:59 belongs to 1 Aug")
        self.assertEqual(self.qty("2026-08-02", "FAKE-B", "LOC-B"), "2", "00:00:00 belongs to 2 Aug")

    def test_iter_closes_uses_next_midnight_and_skips_zero(self):
        key = ("FAKE-A", "PCS", "LOC-A")
        moves = {date(2026, 7, 3): {key: Decimal(-1)}, date(2026, 8, 1): {key: Decimal(1)}, date(2026, 8, 2): {key: Decimal(4)}}
        closes = {day: dict(c) for day, c in hourly.iter_closes(moves, date(2026, 8, 2), first_day=date(2026, 8, 1))}
        self.assertEqual(closes, {date(2026, 8, 1): {}, date(2026, 8, 2): {key: Decimal(4)}})

    def test_non_stock_items_excluded_and_counted(self):
        manifest = self.run_at(self.settings(), at(2026, 8, 4))
        self.assertEqual(manifest["ledger_check"]["result"], "pass", "the check still covers every item")
        self.assertEqual(manifest["ledger"]["non_stock_items_excluded"], 1)
        self.assertEqual(manifest["ledger"]["non_stock_rows_excluded"], 1)
        self.assertEqual(manifest["counts"]["non_stock_excluded"], 1)
        latest = read_csv(self.archive / hourly.LATEST_CSV_NAME)
        self.assertNotIn("FAKE-FEE", {r["ItemCode"] for r in latest})
        for day in ("2026-08-01", "2026-08-03"):
            self.assertNotIn("FAKE-FEE", {r["ItemCode"] for r in self.daily_rows(day)})
        feed = self.feed()
        self.assertNotIn("FAKE-FEE", json.dumps(feed))
        self.assertNotIn("LOC-W", feed["latest"])
        self.assertEqual(feed["anomalies"]["non_stock_items_excluded"], 1)

    def test_exclusion_can_be_switched_off(self):
        manifest = self.run_at(self.settings(rows={"exclude_non_stock_items": False}), at(2026, 8, 4))
        self.assertEqual(self.qty("2026-08-01", "FAKE-FEE", "LOC-W"), "-3")
        self.assertEqual(manifest["counts"]["non_stock_excluded"], 0)

    def test_item_missing_from_master_is_kept_not_hidden(self):
        ledger = aug_ledger() + [led("FAKE-GONE", "LOC-A", 1, day=date(2026, 8, 1))]
        source = ledger_source(ledger, AUG_ITEMS + [master("FAKE-GONE")], sql_now=at(2026, 8, 3, second=6))
        source.item_master = list(AUG_ITEMS)
        manifest = run(self.settings(), source, clock=clock_at(at(2026, 8, 3)))
        # Its UOM cannot be checked against a base UOM, so the guard fails closed.
        self.assertEqual(manifest["failure_class"], "uom_guard")
        self.assertEqual(manifest["ledger"]["items_missing_from_master"], 1)

    def test_unmapped_location_is_kept_labelled_and_counted(self):
        ledger = aug_ledger() + [
            led("FAKE-B", "LOC-Z", 1, day=date(2026, 8, 1)),
            led("FAKE-B", "LOC-Z", -1, day=date(2026, 8, 2)),
        ]
        source = ledger_source(ledger, AUG_ITEMS, sql_now=at(2026, 8, 3, second=6),
                               locations=[{"Location": "LOC-Z", "LocationDescription": "Fake unlisted"}])
        manifest = run(self.settings(), source, clock=clock_at(at(2026, 8, 3)))
        row = [r for r in self.daily_rows("2026-08-01") if r["Location"] == "LOC-Z"][0]
        self.assertEqual(row["LocationLabel"], "LOC-Z - Fake unlisted")
        self.assertIn("LOC-Z", self.daily_meta("2026-08-01")["unmapped_location_codes"])
        self.assertIn("LOC-Z", manifest["daily_close"]["unmapped_location_codes"])
        feed = self.feed()
        self.assertIn({"code": "LOC-Z", "label": "LOC-Z - Fake unlisted", "mapped": False}, feed["locations"])
        self.assertEqual(feed["history"]["per_location"]["LOC-Z"]["units"], [1, 0])
        self.assertGreaterEqual(feed["anomalies"]["unmapped_location_count"], 1)

    def test_future_dated_rows_are_counted_and_not_closed(self):
        ledger = aug_ledger() + [led("FAKE-B", "LOC-B", 7, day=date(2026, 8, 20))]
        manifest = self.run_at(self.settings(), at(2026, 8, 4), ledger=ledger)
        self.assertEqual(manifest["ledger"]["future_dated_rows"], 1)
        self.assertEqual(self.qty("2026-08-03", "FAKE-B", "LOC-B"), "5")
        self.assertEqual(self.feed()["anomalies"]["future_dated_ledger_rows"], 1)

    def test_ledger_with_non_base_uom_fails_the_uom_guard(self):
        ledger = aug_ledger() + [led("FAKE-B", "LOC-B", 0, day=date(2026, 8, 1), uom="CTN")]
        manifest = self.run_at(self.settings(), at(2026, 8, 4), ledger=ledger)
        self.assertEqual(manifest["failure_class"], "uom_guard")
        self.assertEqual(manifest["uom_guard"]["ledger_non_base_uom_groups"], 1)
        self.assertFalse((self.archive / "daily").exists())

    def test_ledger_query_contract(self):
        q = hourly.LEDGER_QUERY
        self.assertIn("FROM dbo.StockDTL AS d", q)
        self.assertIn("CAST(d.DocDate AS date) AS DocDay", q)
        self.assertIn("GROUP BY d.ItemCode, d.UOM, d.Location, CAST(d.DocDate AS date)", q)
        self.assertNotIn("WHERE", q.upper(), "never filtered by date: every close sums from the first row")
        self.assertNotIn("DocInfo", q)
        self.assertNotIn("DocNo", q)
        q = hourly.ITEM_UOM_QUERY
        self.assertEqual(q, "SELECT u.ItemCode, u.UOM, u.BarCode, u.ReOLevel, u.MinQty FROM dbo.ItemUOM AS u")
        self.assertNotIn("Price", q)
        self.assertIn("i.ItemGroup", hourly.ITEM_MASTER_QUERY)
        self.assertIn("i.ItemBrand", hourly.ITEM_MASTER_QUERY)


class LedgerCheckTests(Round6TestCase):
    def assert_fail_closed(self, manifest):
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_class"], "ledger_mismatch")
        self.assertNotIn("FAKE-", manifest["message"])
        for name in (hourly.LATEST_CSV_NAME, hourly.LATEST_MANIFEST_NAME, hourly.FEED_NAME):
            self.assertFalse((self.archive / name).exists(), name)
        self.assertFalse((self.archive / "daily").exists())
        self.assertFalse((self.run_dir(manifest) / hourly.CSV_NAME).exists())
        status = json.loads((self.archive / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual((status["status"], status["failure_class"]), ("failed", "ledger_mismatch"))

    def test_mismatch_fails_closed(self):
        source = ledger_source(aug_ledger(), AUG_ITEMS, sql_now=at(2026, 8, 4, second=6))
        source.balances[0]["BalQty"] += 1
        manifest = run(self.settings(), source, clock=clock_at(at(2026, 8, 4)))
        self.assert_fail_closed(manifest)
        self.assertEqual(manifest["ledger_check"]["mismatched"], 1)

    def test_balance_only_non_zero_fails(self):
        source = ledger_source(aug_ledger(), AUG_ITEMS, sql_now=at(2026, 8, 4, second=6))
        source.balances.append(bal("FAKE-B", "LOC-C", "3"))
        self.assert_fail_closed(run(self.settings(), source, clock=clock_at(at(2026, 8, 4))))

    def test_ledger_only_non_zero_fails(self):
        source = ledger_source(aug_ledger(), AUG_ITEMS, sql_now=at(2026, 8, 4, second=6))
        source.ledger.append(led("FAKE-B", "LOC-C", 3, day=date(2026, 8, 1)))
        self.assert_fail_closed(run(self.settings(), source, clock=clock_at(at(2026, 8, 4))))

    def test_zero_only_keys_on_either_side_pass(self):
        source = ledger_source(aug_ledger(), AUG_ITEMS, sql_now=at(2026, 8, 4, second=6))
        source.balances.append(bal("FAKE-B", "LOC-C", "0"))
        source.ledger += [led("FAKE-B", "LOC-D", 2, day=date(2026, 8, 1)), led("FAKE-B", "LOC-D", -2, day=date(2026, 8, 2))]
        manifest = run(self.settings(), source, clock=clock_at(at(2026, 8, 4)))
        self.assertEqual(manifest["status"], "success")
        check = manifest["ledger_check"]
        self.assertEqual((check["balance_only"], check["ledger_only"], check["result"]), (1, 1, "pass"))
        self.assertEqual(self.qty("2026-08-01", "FAKE-B", "LOC-D"), "2", "a key that nets 0 today still had stock")

    def test_ledger_read_is_inside_every_changecount_window(self):
        source = ledger_source(aug_ledger(), AUG_ITEMS, sql_now=at(2026, 8, 4, second=6), changecounts=[(1, 2), (2, 2)])
        manifest = run(self.settings(), source, clock=clock_at(at(2026, 8, 4)))
        self.assertEqual((manifest["status"], manifest["attempts"]), ("success", 2))
        self.assertEqual(source.ledger_reads, 2, "the ledger is read again with each balance read")


class RestateWindowTests(Round6TestCase):
    def test_hourly_runs_after_the_close_write_no_daily_files(self):
        settings = self.settings()
        self.run_at(settings, at(2026, 8, 10))
        before = tree_bytes(self.archive / "daily")
        later = self.run_at(settings, at(2026, 8, 10, 5))
        self.assertEqual(later["daily_close"]["status"], "not_due")
        self.assertEqual(tree_bytes(self.archive / "daily"), before)

    def test_double_start_at_midnight_closes_once(self):
        settings = self.settings()
        first = self.run_at(settings, at(2026, 8, 10, second=0))
        before = tree_bytes(self.archive / "daily")
        second = self.run_at(settings, at(2026, 8, 10, second=1))
        self.assertEqual(first["daily_close"]["status"], "published")
        self.assertEqual(second["daily_close"]["status"], "not_due")
        self.assertEqual(tree_bytes(self.archive / "daily"), before)

    def test_missed_midnights_are_caught_up(self):
        settings = self.settings()
        self.run_at(settings, at(2026, 8, 10))
        manifest = self.run_at(settings, at(2026, 8, 13, 3))
        close = manifest["daily_close"]
        self.assertEqual((close["mode"], close["new_days"], close["revised_day_count"]), ("nightly", 3, 0))
        for day in ("2026-08-10", "2026-08-11", "2026-08-12"):
            self.assertEqual(self.daily_meta(day)["write_reason"], "new")

    def test_restate_inside_window_and_flag_outside(self):
        settings = self.settings(output={"delivery_root": str(self.delivery)})
        self.run_at(settings, at(2026, 8, 10))
        old_aug2 = self.daily("2026-08-02").read_bytes()
        old_aug3_drive = self.daily("2026-08-03", self.delivery).read_bytes()
        # Keyed late: one document backdated into the window, one older than the window.
        ledger = aug_ledger() + [
            led("FAKE-B", "LOC-B", -1, day=date(2026, 8, 5)),
            led("FAKE-A", "LOC-A", 4, day=date(2026, 8, 2)),
        ]
        manifest = self.run_at(settings, at(2026, 8, 11), ledger=ledger)
        close = manifest["daily_close"]
        self.assertEqual(close["status"], "published")
        self.assertEqual(close["provisional_from"], "2026-08-04")
        self.assertEqual(close["new_days"], 1)
        self.assertEqual([d["day"] for d in close["revised_days"]], ["2026-08-04", "2026-08-05", "2026-08-06",
                                                                     "2026-08-07", "2026-08-08", "2026-08-09"])
        self.assertEqual(close["revised_days"][1], {"day": "2026-08-05", "rows_changed": 2, "net_qty_change": "3"})
        self.assertEqual(close["late_change_day_count"], 2)
        self.assertEqual(close["late_changes_beyond_window"][0], {"day": "2026-08-02", "rows_changed": 1, "net_qty_change": "4"})
        self.assertTrue(any(w.startswith("Late change: 2 day(s)") for w in manifest["warnings"]))
        # Older days are left exactly as published, locally and in Drive.
        self.assertEqual(self.daily("2026-08-02").read_bytes(), old_aug2)
        self.assertEqual(self.daily("2026-08-03", self.delivery).read_bytes(), old_aug3_drive)
        # Days inside the window are replaced with the rebuilt truth, in both places.
        self.assertEqual(self.qty("2026-08-05", "FAKE-B", "LOC-B"), "4")
        self.assertEqual(self.qty("2026-08-05", "FAKE-A", "LOC-A"), "11")
        self.assertEqual(self.daily_meta("2026-08-05")["revision"], 2)
        self.assertEqual(self.daily_meta("2026-08-05")["write_reason"], "revised")
        self.assertEqual(self.daily("2026-08-05").read_bytes(), self.daily("2026-08-05", self.delivery).read_bytes())
        # The feed always shows the rebuilt truth, including the flagged older days.
        feed = self.feed(self.delivery)
        aug2 = feed["history"]["days"].index("2026-08-02")
        self.assertEqual(feed["history"]["per_location"]["LOC-A"]["units"][aug2], 11)
        self.assertEqual(feed["anomalies"]["late_change_day_count"], 2)
        status = json.loads((self.delivery / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual((status["revised_day_count"], status["late_change_day_count"]), (6, 2))
        # The flag is raised once: the next night compares against what was read this time.
        nxt = self.run_at(settings, at(2026, 8, 12), ledger=ledger)
        self.assertEqual((nxt["daily_close"]["late_change_day_count"], nxt["daily_close"]["revised_day_count"]), (0, 0))

    def test_backfill_command_rewrites_every_day(self):
        settings = self.settings()
        self.run_at(settings, at(2026, 8, 10))
        ledger = aug_ledger() + [led("FAKE-A", "LOC-A", 4, day=date(2026, 8, 2))]
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw_settings(self.tmp, daily_close={"backfill_start": "2026-08-01"})), encoding="utf-8")
        source = ledger_source(ledger, AUG_ITEMS, sql_now=at(2026, 8, 10, 9, second=6))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = hourly.main(["--config", str(path), "--backfill"], source=source, environ=ENV,
                               clock=clock_at(at(2026, 8, 10, 9)))
        self.assertEqual(code, 0, out.getvalue())
        printed = json.loads(out.getvalue())
        self.assertEqual((printed["daily_close_status"], printed["published_close_day"]), ("published", "2026-08-09"))
        self.assertEqual(self.qty("2026-08-02", "FAKE-A", "LOC-A"), "11")
        self.assertEqual(self.daily_meta("2026-08-02")["revision"], 2)
        self.assertEqual(self.daily_meta("2026-08-02")["write_reason"], "rebuild")

    def test_missing_state_rebuilds_with_a_warning(self):
        settings = self.settings()
        self.run_at(settings, at(2026, 8, 10))
        (self.archive / "daily" / hourly.CLOSE_STATE_NAME).unlink()
        manifest = self.run_at(settings, at(2026, 8, 10, 1))
        self.assertEqual((manifest["daily_close"]["mode"], manifest["daily_close"]["state"]), ("initial", "missing"))
        self.assertIn("Close state was missing: every day from backfill_start is written again", manifest["warnings"])
        self.assertTrue((self.archive / "daily" / hourly.CLOSE_STATE_NAME).exists())

    def test_unreadable_state_rebuilds_with_a_warning(self):
        settings = self.settings()
        self.run_at(settings, at(2026, 8, 10))
        (self.archive / "daily" / hourly.CLOSE_STATE_NAME).write_bytes(b"not gzip")
        manifest = self.run_at(settings, at(2026, 8, 10, 1))
        self.assertEqual(manifest["daily_close"]["state"], "unreadable")
        self.assertEqual(manifest["daily_close"]["status"], "published")

    def test_close_write_failure_keeps_snapshot_and_retries_next_run(self):
        settings = self.settings()
        real = hourly.copy_bytes_exclusive

        def failing(path, data):
            if "stock_close_2026-08-02" in Path(path).name:
                raise OSError("disk full")
            return real(path, data)

        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw_settings(self.tmp, daily_close={"backfill_start": "2026-08-01"})), encoding="utf-8")
        source = ledger_source(aug_ledger(), AUG_ITEMS, sql_now=at(2026, 8, 4, second=6))
        with mock.patch.object(hourly, "copy_bytes_exclusive", side_effect=failing), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = hourly.main(["--config", str(path)], source=source, environ=ENV, clock=clock_at(at(2026, 8, 4)))
        self.assertEqual(code, 1)
        printed = json.loads(out.getvalue())
        self.assertEqual((printed["status"], printed["daily_close_status"]), ("success", "failed"))
        status = json.loads((self.archive / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["daily_close_failure_class"], "daily_close_write_failed")
        self.assertTrue((self.archive / hourly.LATEST_CSV_NAME).exists(), "the hourly snapshot is still published")
        self.assertFalse((self.archive / "daily" / hourly.CLOSE_STATE_NAME).exists())
        retry = self.run_at(settings, at(2026, 8, 4, 1))
        self.assertEqual(retry["daily_close"]["status"], "published")
        self.assertTrue(self.daily("2026-08-03").exists())

    def assert_day_pair_agrees(self, day, root, revision):
        meta = self.daily_meta(day, root)
        self.assertEqual(meta["revision"], revision, (day, root.name))
        self.assertEqual(hashlib.sha256(self.daily(day, root).read_bytes()).hexdigest(), meta["csv"]["sha256"])

    def test_bad_day_manifest_copy_keeps_the_previous_consistent_day_pair(self):
        # G2 r6 H4 for the daily files: a revised day's CSV is not replaced unless its manifest
        # copy verifies too.
        settings = self.settings(output={"delivery_root": str(self.delivery)})
        self.run_at(settings, at(2026, 8, 10))
        before = tree_bytes(self.delivery / "daily")
        ledger = aug_ledger() + [led("FAKE-B", "LOC-B", -1, day=date(2026, 8, 5))]
        real = hourly.copy_bytes_exclusive

        def bad_copy(path, data):
            hit = Path(path).name.startswith(".stock_close_2026-08-05.manifest.json") and self.delivery in Path(path).parents
            real(path, data + b"x" if hit else data)

        with mock.patch.object(hourly, "copy_bytes_exclusive", side_effect=bad_copy):
            failed = self.run_at(settings, at(2026, 8, 11), ledger=ledger)
        self.assertEqual(failed["daily_close"]["status"], "failed")
        self.assertEqual(failed["status"], "success", "the hourly snapshot is still published")
        self.assert_day_pair_agrees("2026-08-05", self.delivery, 1)
        for name in ("2026-08/stock_close_2026-08-05.csv", "2026-08/stock_close_2026-08-05.manifest.json"):
            key = str(Path(name))
            self.assertEqual((self.delivery / "daily" / key).read_bytes(), before[key], name)
        self.assertEqual([p.name for p in self.delivery.rglob("*.tmp")], [])
        # The close state was not saved, so the next run rewrites the day in both places.
        retry = self.run_at(settings, at(2026, 8, 11, 1), ledger=ledger)
        self.assertEqual(retry["daily_close"]["status"], "published")
        self.assert_day_pair_agrees("2026-08-05", self.delivery, 2)
        self.assertEqual(self.daily("2026-08-05").read_bytes(), self.daily("2026-08-05", self.delivery).read_bytes())

    def test_restate_days_setting_moves_the_window(self):
        settings = self.settings(daily_close={"backfill_start": "2026-08-01", "restate_days": 2})
        self.run_at(settings, at(2026, 8, 10))
        ledger = aug_ledger() + [led("FAKE-B", "LOC-B", -1, day=date(2026, 8, 7))]
        close = self.run_at(settings, at(2026, 8, 11), ledger=ledger)["daily_close"]
        self.assertEqual([d["day"] for d in close["revised_days"]], ["2026-08-09"])
        self.assertEqual([d["day"] for d in close["late_changes_beyond_window"]], ["2026-08-07", "2026-08-08"])

    def test_changed_closes_is_pure_and_cumulative(self):
        key = ("FAKE-A", "PCS", "LOC-A")
        old = {date(2026, 8, 1): {key: Decimal(5)}}
        new = {date(2026, 8, 1): {key: Decimal(5)}, date(2026, 8, 2): {key: Decimal(-1)}}
        days = hourly.day_range(date(2026, 8, 1), date(2026, 8, 3))
        changed = hourly.changed_closes(old, new, days)
        self.assertEqual(sorted(changed), [date(2026, 8, 2), date(2026, 8, 3)])
        self.assertEqual(changed[date(2026, 8, 3)], {"rows_changed": 1, "net_qty_change": Decimal(-1)})
        self.assertEqual(hourly.changed_closes(new, new, days), {})


class FeedTests(Round6TestCase):
    TOP_KEYS = {
        "schema", "schema_version", "job", "run_id", "as_of_local", "timezone", "cadence_minutes", "next_run_local",
        "stale_after_minutes", "backfill_start", "last_close_day", "published_close_day", "provisional_from",
        "quantity_rule", "locations", "latest", "history", "movers", "negatives", "low_stock", "anomalies", "pos_sync",
    }

    def test_schema_and_shape(self):
        manifest = self.run_at(self.settings(), at(2026, 8, 4, 14))
        data = (self.archive / hourly.FEED_NAME).read_bytes()
        self.assertEqual(data[:2], b"\x1f\x8b", "gzip")
        self.assertEqual(manifest["feed"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(manifest["feed"]["status"], "published")
        feed = self.feed()
        self.assertEqual(set(feed), self.TOP_KEYS)
        self.assertEqual((feed["schema"], feed["schema_version"]), ("xb.ac2.stock_dashboard_feed", 1))
        self.assertEqual(feed["run_id"], manifest["run_id"])
        self.assertEqual((feed["last_close_day"], feed["published_close_day"]), ("2026-08-03", "2026-08-03"))
        self.assertEqual(feed["next_run_local"], "2026-08-04T15:00:00+08:00")
        self.assertEqual(feed["history"]["days"], ["2026-08-01", "2026-08-02", "2026-08-03"])
        for loc, series in feed["history"]["per_location"].items():
            for name in ("units", "skus_in_stock", "negative"):
                self.assertEqual(len(series[name]), 3, (loc, name))
        self.assertEqual(feed["history"]["per_location"]["LOC-A"]["units"], [9, 7, 7])
        self.assertEqual(feed["latest"]["LOC-A"], {"units": 7, "skus_in_stock": 1, "zero": 0, "negative": 0, "low_stock": 0})
        self.assertEqual(set(feed["movers"]), {"1d", "7d", "30d"})
        text = json.dumps(feed).lower()
        for word in ("cost", "price", "amount", "customer", "member", "phone", "email", "debtor", "docno"):
            self.assertNotIn(word, text)

    def test_negatives_low_stock_and_caps(self):
        ledger = aug_ledger() + [
            led("FAKE-B", "LOC-B", -6, day=date(2026, 8, 2)),
            led("FAKE-A", "LOC-B", 1, day=date(2026, 8, 3)),
        ]
        # Only a positive ReOLevel counts as a reorder level in the feed.
        uoms = [item_uom("FAKE-LOW", reorder=3), item_uom("FAKE-A", reorder=0, min_qty=4)]
        settings = self.settings(feed={"low_stock_threshold": 2, "list_cap": 1})
        self.run_at(settings, at(2026, 8, 4, 9), ledger=ledger, item_uoms=uoms)
        feed = self.feed()
        self.assertEqual(feed["negatives"]["count"], 1)
        self.assertEqual(
            feed["negatives"]["rows"][0],
            {"item": "FAKE-B", "description": "Fake item", "location": "LOC-B", "uom": "PCS", "qty": -1,
             "negative_since": "2026-08-02"},
        )
        low = feed["low_stock"]
        self.assertEqual((low["count"], low["truncated"], len(low["rows"])), (2, True, 1))
        self.assertEqual(low["rows"][0]["qty"], 1)
        self.assertEqual(low["items_with_reorder_level"], 1)
        self.assertEqual(feed["latest"]["LOC-B"]["low_stock"], 2)

    def test_movers(self):
        self.run_at(self.settings(), at(2026, 8, 4))
        movers = self.feed()["movers"]
        self.assertEqual(movers["1d"]["from"], "2026-08-02")
        self.assertEqual(movers["1d"]["changed_keys"], 0)
        week = movers["7d"]
        self.assertEqual(week["from"], "2026-07-27")
        self.assertEqual([(m["item"], m["change"], m["close"]) for m in week["up"]],
                         [("FAKE-A", 8, 7), ("FAKE-B", 5, 5), ("FAKE-LOW", 2, 2)])
        # FAKE-A was -1 on 27 Jul (the 3 Jul sale before go-live), so its 7-day change is +8.

    def test_feed_written_every_hour_beside_the_latest_pair(self):
        settings = self.settings(output={"delivery_root": str(self.delivery)})
        self.run_at(settings, at(2026, 8, 4))
        second = self.run_at(settings, at(2026, 8, 4, 1))
        self.assertEqual(self.feed(self.delivery)["run_id"], second["run_id"])
        self.assertEqual((self.delivery / hourly.FEED_NAME).read_bytes(), (self.archive / hourly.FEED_NAME).read_bytes())

    def test_feed_failure_is_reported_and_snapshot_kept(self):
        with mock.patch.object(hourly, "build_feed", side_effect=RuntimeError("bug")):
            manifest = self.run_at(self.settings(), at(2026, 8, 4))
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["feed"], {"status": "failed", "failure_class": "feed_publish_failed"})
        status = json.loads((self.archive / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["feed_status"], "failed")
        self.assertFalse((self.archive / hourly.FEED_NAME).exists())

    def test_size_stays_compact_for_a_realistic_book(self):
        items = [master("FAKE-%05d" % n) for n in range(3000)]
        ledger = []
        for n in range(3000):
            ledger.append(led("FAKE-%05d" % n, "LOC-%d" % (n % 6), 5 + n % 7, day=date(2026, 7, 31)))
            for d in range(1, 30, 3):
                ledger.append(led("FAKE-%05d" % n, "LOC-%d" % (n % 6), -1 if n % 2 else 1, day=date(2026, 8, d)))
        settings = self.settings()
        manifest = self.run_at(settings, at(2026, 9, 1), ledger=ledger, items=items)
        self.assertEqual(manifest["status"], "success")
        self.assertLess(manifest["feed"]["byte_size"], 1000000)
        self.assertEqual(manifest["daily_close"]["days_written"], 31)


class ItemIndexTests(Round6TestCase):
    """Dashboard v2 (#245): the item index beside the feed."""

    TOP_KEYS = {
        "schema", "schema_version", "job", "run_id", "as_of_local", "timezone", "quantity_rule", "last_close_day",
        "history_from", "history_days", "low_stock_default_threshold", "large_balance_threshold", "locations",
        "item_fields", "items", "stock_fields", "stock", "history_fields", "history", "counts",
    }

    def index(self, root=None):
        return json.loads(gzip.decompress((Path(root or self.archive) / hourly.ITEM_INDEX_NAME).read_bytes()))

    def items_by_code(self, index):
        fields = index["item_fields"]
        return {row[0]: dict(zip(fields, row)) for row in index["items"]}

    def stock_by_key(self, index):
        codes = [row[0] for row in index["items"]]
        return {(codes[i], index["locations"][l]): (qty, last) for i, l, qty, last in index["stock"]}

    def history_by_key(self, index):
        codes = [row[0] for row in index["items"]]
        return {(codes[i], index["locations"][l]): changes for i, l, changes in index["history"]}

    def status(self, root=None):
        return json.loads((Path(root or self.archive) / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))

    def test_schema_shape_and_content(self):
        items = AUG_ITEMS + [
            master("FAKE-OLD", active="F"),
            master("FAKE-INACT", active="F", desc="", group="", brand=""),
        ]
        ledger = aug_ledger() + [led("FAKE-INACT", "LOC-A", 1, day=date(2026, 8, 3))]
        uoms = [
            item_uom("FAKE-A", barcode="0000000000017", reorder=3, min_qty=1),
            item_uom("FAKE-LOW", barcode="", min_qty=2),
            # Not the base UOM: never used for the barcode or the levels.
            item_uom("FAKE-B", uom="CTN", barcode="0000000000099", reorder=9),
        ]
        manifest = self.run_at(self.settings(), at(2026, 8, 4, 14), ledger=ledger, items=items, item_uoms=uoms)
        self.assertEqual(manifest["status"], "success")
        data = (self.archive / hourly.ITEM_INDEX_NAME).read_bytes()
        self.assertEqual(data[:2], b"\x1f\x8b", "gzip")
        self.assertEqual(manifest["item_index"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(manifest["item_index"]["byte_size"], len(data))
        self.assertEqual(manifest["item_index"]["status"], "published")
        self.assertEqual(self.status()["item_index_status"], "published")

        index = self.index()
        self.assertEqual(set(index), self.TOP_KEYS)
        self.assertEqual((index["schema"], index["schema_version"]), ("xb.ac2.stock_item_index", 1))
        self.assertEqual(index["run_id"], manifest["run_id"])
        self.assertEqual((index["last_close_day"], index["history_from"], index["history_days"]), ("2026-08-03", "2026-08-01", 3))
        self.assertEqual((index["low_stock_default_threshold"], index["large_balance_threshold"]), (2, 100000))
        self.assertEqual(index["locations"], ["LOC-A", "LOC-B"], "the non-stock fee code's location is not listed")

        by_code = self.items_by_code(index)
        self.assertEqual(sorted(by_code), ["FAKE-A", "FAKE-B", "FAKE-INACT", "FAKE-LOW"])
        self.assertEqual(
            by_code["FAKE-A"],
            {"code": "FAKE-A", "description": "Fake item", "barcode": "0000000000017", "group": "FAKE-GRP",
             "brand": "FAKE-BRD", "uom": "PCS", "active": True, "reorder_level": 3, "min_qty": 1},
        )
        self.assertEqual((by_code["FAKE-B"]["barcode"], by_code["FAKE-B"]["reorder_level"]), ("", None))
        self.assertEqual((by_code["FAKE-LOW"]["reorder_level"], by_code["FAKE-LOW"]["min_qty"]), (None, 2))
        self.assertFalse(by_code["FAKE-INACT"]["active"], "inactive but holding stock: listed")
        self.assertEqual(index["counts"], {
            "items": 4,
            "inactive_without_stock_or_movement_excluded": 1,
            "non_stock_items_excluded": 1,
            "items_without_description": 1,
            "items_without_barcode": 3,
            "items_with_reorder_level": 1,
            "items_with_min_qty": 2,
            "stock_keys": 4,
            "history_keys": 4,
        })
        self.assertEqual(manifest["item_index"]["counts"], index["counts"])

        self.assertEqual(self.stock_by_key(index), {
            ("FAKE-A", "LOC-A"): (7, "2026-08-02"),
            ("FAKE-B", "LOC-B"): (5, "2026-07-31"),
            ("FAKE-INACT", "LOC-A"): (1, "2026-08-03"),
            ("FAKE-LOW", "LOC-B"): (2, "2026-07-31"),
        })
        # Closes sum from the first ledger row (the 3 Jul sale), as the daily files do.
        self.assertEqual(self.history_by_key(index), {
            ("FAKE-A", "LOC-A"): [[0, 9], [1, 7]],
            ("FAKE-B", "LOC-B"): [[0, 5]],
            ("FAKE-INACT", "LOC-A"): [[2, 1]],
            ("FAKE-LOW", "LOC-B"): [[0, 2]],
        })
        self.assertEqual(self.qty("2026-08-01", "FAKE-A", "LOC-A"), "9")
        text = json.dumps(index).lower()
        for word in ("cost", "price", "amount", "customer", "member", "phone", "email", "debtor", "docno", "fake-fee"):
            self.assertNotIn(word, text)

    def test_last_movement_counts_net_zero_days_and_skips_future_rows(self):
        ledger = aug_ledger() + [
            # A transfer in and out on the same day nets to zero: still a movement.
            led("FAKE-B", "LOC-B", 0, day=date(2026, 8, 3), rows=2),
            led("FAKE-B", "LOC-B", 7, day=date(2026, 8, 20)),
            # Sold out: the zero balance row stays, with its last movement.
            led("FAKE-LOW", "LOC-B", -2, day=date(2026, 8, 3)),
        ]
        manifest = self.run_at(self.settings(), at(2026, 8, 4), ledger=ledger)
        self.assertEqual(manifest["status"], "success")
        index = self.index()
        stock = self.stock_by_key(index)
        self.assertEqual(stock[("FAKE-B", "LOC-B")], (12, "2026-08-03"), "the 20 Aug row is future-dated")
        self.assertEqual(stock[("FAKE-LOW", "LOC-B")], (0, "2026-08-03"))
        history = self.history_by_key(index)
        self.assertEqual(history[("FAKE-B", "LOC-B")], [[0, 5]], "a net-zero day does not change the close")
        self.assertEqual(history[("FAKE-LOW", "LOC-B")], [[0, 2], [2, 0]])

    def test_zero_rows_that_never_moved_are_left_out(self):
        items = AUG_ITEMS + [master("FAKE-Z")]
        source = ledger_source(aug_ledger(), items, sql_now=at(2026, 8, 4) + timedelta(seconds=1))
        source.balances.append(bal("FAKE-Z", "LOC-A", 0))
        manifest = run(self.settings(), source, clock=clock_at(at(2026, 8, 4)))
        self.assertEqual(manifest["status"], "success")
        index = self.index()
        self.assertIn("FAKE-Z", self.items_by_code(index), "active item: still searchable")
        self.assertNotIn(("FAKE-Z", "LOC-A"), self.stock_by_key(index))

    def test_history_window_is_ninety_days(self):
        ledger = aug_ledger() + [
            led("FAKE-A", "LOC-A", 1, day=date(2026, 9, 1)),
            led("FAKE-A", "LOC-A", -3, day=date(2026, 9, 3)),
        ]
        self.run_at(self.settings(), at(2026, 12, 1), ledger=ledger)
        index = self.index()
        self.assertEqual((index["history_from"], index["last_close_day"], index["history_days"]),
                         ("2026-09-02", "2026-11-30", 90))
        self.assertEqual(self.history_by_key(index)[("FAKE-A", "LOC-A")], [[0, 8], [1, 5]])

    def test_index_travels_in_the_drive_latest_set(self):
        settings = self.settings(output={"delivery_root": str(self.delivery)})
        manifest = self.run_at(settings, at(2026, 8, 4))
        self.assertIn(hourly.ITEM_INDEX_NAME, manifest["delivery"]["files"])
        self.assertEqual((self.delivery / hourly.ITEM_INDEX_NAME).read_bytes(),
                         (self.archive / hourly.ITEM_INDEX_NAME).read_bytes())
        self.assertEqual(self.status(self.delivery)["item_index_status"], "published")
        latest = json.loads((self.delivery / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(latest["item_index"]["sha256"], manifest["item_index"]["sha256"])

    def tamper(self, root):
        real = hourly.copy_bytes_exclusive

        def bad_copy(path, data):
            hit = hourly.ITEM_INDEX_NAME in Path(path).name and Path(path).parent == Path(root)
            real(path, data + b"x" if hit else data)

        return mock.patch.object(hourly, "copy_bytes_exclusive", side_effect=bad_copy)

    def test_bad_drive_copy_keeps_the_previous_latest_set(self):
        settings = self.settings(output={"delivery_root": str(self.delivery)})
        good = self.run_at(settings, at(2026, 8, 4))
        before = tree_bytes(self.delivery)
        with self.tamper(self.delivery):
            second = self.run_at(settings, at(2026, 8, 4, 1))
        for name in (hourly.LATEST_CSV_NAME, hourly.LATEST_MANIFEST_NAME, hourly.FEED_NAME, hourly.ITEM_INDEX_NAME):
            self.assertEqual((self.delivery / name).read_bytes(), before[name], name)
        status = self.status(self.delivery)
        self.assertEqual(status["delivery_status"], "verify_failed")
        self.assertEqual((status["feed_status"], status["item_index_status"]), ("failed", "failed"))
        self.assertEqual(second["item_index"]["failure_class"], "item_index_publish_failed")
        self.assertEqual(self.index(self.delivery)["run_id"], good["run_id"])
        self.assertEqual([p.name for p in self.delivery.rglob("*.tmp")], [])

    def test_bad_archive_write_keeps_both_previous_copies(self):
        settings = self.settings(output={"delivery_root": str(self.delivery)})
        self.run_at(settings, at(2026, 8, 4))
        before = {root: (root / hourly.ITEM_INDEX_NAME).read_bytes() for root in (self.archive, self.delivery)}
        with self.tamper(self.archive):
            second = self.run_at(settings, at(2026, 8, 4, 1))
        for root, data in before.items():
            self.assertEqual((root / hourly.ITEM_INDEX_NAME).read_bytes(), data, root.name)
        self.assertEqual(second["item_index"]["status"], "failed")
        status = self.status(self.delivery)
        self.assertEqual((status["delivery_status"], status["feed_status"]), ("handed_to_drive_sync_folder", "published"))
        self.assertEqual(status["item_index_status"], "failed")
        latest = json.loads((self.delivery / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(latest["run_id"], second["run_id"], "the latest pair still advances")
        self.assertEqual(latest["item_index"]["status"], "failed", "the Drive manifest does not claim a new index")

    def test_build_failure_is_reported_and_exits_one(self):
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw_settings(self.tmp)), encoding="utf-8")
        with mock.patch.object(hourly, "build_item_index", side_effect=RuntimeError("bug")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = hourly.main(["--config", str(path)], source=FakeSource(), environ=ENV, clock=clock_at())
        self.assertEqual(code, hourly.EXIT_FAILED)
        printed = json.loads(out.getvalue())
        self.assertEqual((printed["status"], printed["feed_status"], printed["item_index_status"]),
                         ("success", "published", "failed"))
        self.assertFalse((self.archive / hourly.ITEM_INDEX_NAME).exists())
        self.assertTrue((self.archive / hourly.LATEST_CSV_NAME).exists(), "the latest pair is still published")

    def test_failed_run_writes_no_index(self):
        manifest = run(self.settings(), FakeSource(identity=observed_identity(login="sa")))
        self.assertEqual(manifest["status"], "failed")
        self.assertIsNone(manifest["item_index"])
        self.assertIsNone(self.status()["item_index_status"])
        self.assertFalse((self.archive / hourly.ITEM_INDEX_NAME).exists())

    def test_size_stays_compact_for_a_realistic_book(self):
        items = [master("FAKE-%05d" % n, desc="Fake outdoor item number %05d size M colour blue" % n)
                 for n in range(6000)]
        uoms = [item_uom("FAKE-%05d" % n, barcode="%013d" % n, reorder=n % 4) for n in range(6000)]
        ledger = []
        for n in range(6000):
            ledger.append(led("FAKE-%05d" % n, "LOC-%d" % (n % 6), 5 + n % 7, day=date(2026, 7, 31)))
            for d in range(1, 30, 3):
                ledger.append(led("FAKE-%05d" % n, "LOC-%d" % (n % 6), -1 if n % 2 else 1, day=date(2026, 8, d)))
        manifest = self.run_at(self.settings(), at(2026, 9, 1), ledger=ledger, items=items, item_uoms=uoms)
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["item_index"]["counts"]["items"], 6000)
        self.assertLess(manifest["item_index"]["byte_size"], 1000000)


class RetentionTests(Round6TestCase):
    OLD = "ac2_stockbal_20261020T100000+0800_aaaaaaaa"
    NEW = "ac2_stockbal_20261023T120000+0800_bbbbbbbb"

    def make(self, relative, data=b"x"):
        path = self.archive / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def build_tree(self):
        self.make("hourly/2026-10-20/%s/stock_balance.csv" % self.OLD)
        self.make("hourly/2026-10-20/%s/manifest.json" % self.OLD)
        # An expired run folder holding a foreign file: its own manifest goes, the rest stays.
        own_beside_foreign = self.make("hourly/2026-10-20/ac2_stockbal_20261020T110000+0800_cccccccc/manifest.json")
        foreign = self.make("hourly/2026-10-20/ac2_stockbal_20261020T110000+0800_cccccccc/notes.txt")
        self.make("hourly/2026-10-23/%s/stock_balance.csv" % self.NEW)
        odd_folder = self.make("hourly/misc/stock_balance.csv")
        odd_run = self.make("hourly/2026-10-20/backup/stock_balance.csv")
        old_daily = self.make("daily/2026-07/stock_close_2026-07-20.csv")
        old_meta = self.make("daily/2026-07/stock_close_2026-07-20.manifest.json")
        keep_daily = self.make("daily/2026-07/stock_close_2026-07-25.csv")
        foreign_daily = self.make("daily/2026-07/other.csv")
        state = self.make("daily/" + hourly.CLOSE_STATE_NAME)
        return {
            "removed": [old_daily, old_meta, own_beside_foreign],
            "kept": [foreign, odd_folder, odd_run, keep_daily, foreign_daily, state],
        }

    def test_off_by_default(self):
        tree = self.build_tree()
        before = tree_bytes(self.archive)
        result = hourly.prune_archive(self.archive, self.settings(), at(2026, 10, 23, 14), date(2026, 10, 23), None)
        self.assertFalse(result["prune_enabled"])
        self.assertEqual(tree_bytes(self.archive), before)
        self.assertTrue(all(p.exists() for p in tree["removed"]))

    def test_prunes_only_own_expired_files(self):
        tree = self.build_tree()
        outside = Path(self.tmp) / "outside" / "stock_close_2026-01-01.csv"
        outside.parent.mkdir()
        outside.write_bytes(b"keep")
        settings = self.settings(retention={"prune_enabled": True})
        result = hourly.prune_archive(self.archive, settings, at(2026, 10, 23, 14), date(2026, 10, 23), None)
        self.assertFalse((self.archive / "hourly" / "2026-10-20" / self.OLD).exists())
        self.assertTrue((self.archive / "hourly" / "2026-10-23" / self.NEW / "stock_balance.csv").exists())
        for path in tree["removed"]:
            self.assertFalse(path.exists(), path)
        for path in tree["kept"]:
            self.assertTrue(path.exists(), path)
        self.assertTrue(outside.exists())
        self.assertEqual((result["hourly_folders_removed"], result["daily_files_removed"]), (1, 2))
        self.assertGreaterEqual(result["kept_unexpected"], 4)
        self.assertEqual(result["errors"], 0)

    def test_never_follows_a_junction_out_of_the_archive(self):
        if os.name != "nt":
            self.skipTest("junctions are Windows only")
        target = Path(self.tmp) / "elsewhere"
        target.mkdir()
        (target / "stock_balance.csv").write_bytes(b"not ours")
        (target / "manifest.json").write_bytes(b"not ours")
        link = self.archive / "hourly" / "2026-10-01" / "ac2_stockbal_20261001T000000+0800_dddddddd"
        link.parent.mkdir(parents=True)
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if done.returncode != 0:
            self.skipTest("could not create a junction here")
        # Remove the junction itself (never its target) before the temp folder is deleted.
        self.addCleanup(os.rmdir, link)
        month_target = Path(self.tmp) / "elsewhere_daily"
        month_target.mkdir()
        (month_target / "stock_close_2026-01-01.csv").write_bytes(b"not ours")
        month_link = self.archive / "daily" / "2026-01"
        month_link.parent.mkdir(parents=True)
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(month_link), str(month_target)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.addCleanup(os.rmdir, month_link)
        settings = self.settings(retention={"prune_enabled": True})
        result = hourly.prune_archive(self.archive, settings, at(2026, 10, 23, 14), date(2026, 10, 23), None)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["manifest.json", "stock_balance.csv"])
        self.assertTrue((month_target / "stock_close_2026-01-01.csv").exists())
        self.assertGreaterEqual(result["kept_unexpected"], 2)

    def test_run_prunes_when_enabled_and_keeps_its_own_folder(self):
        self.build_tree()
        settings = self.settings(retention={"prune_enabled": True, "hourly_keep_hours": 2})
        manifest = self.run_at(settings, at(2026, 10, 23, 14))
        self.assertTrue((self.run_dir(manifest) / hourly.CSV_NAME).exists())
        self.assertEqual(manifest["retention"]["hourly_folders_removed"], 2)
        on_disk = json.loads((self.run_dir(manifest) / hourly.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["retention"], manifest["retention"])
        self.assertTrue((self.archive / "daily" / hourly.CLOSE_STATE_NAME).exists())

    def test_drive_folder_is_never_pruned(self):
        settings = self.settings(output={"delivery_root": str(self.delivery)},
                                 retention={"prune_enabled": True, "daily_keep_days": 8})
        self.run_at(settings, at(2026, 8, 4))
        drive_daily = self.daily("2026-08-01", self.delivery).read_bytes()
        self.run_at(settings, at(2026, 8, 20))
        self.assertFalse(self.daily("2026-08-01").exists(), "local copy pruned after 8 days")
        self.assertEqual(self.daily("2026-08-01", self.delivery).read_bytes(), drive_daily)


class Round6ConfigTests(TempDirTestCase):
    def assert_config_error(self, fragment, **overrides):
        with self.assertRaises(hourly.ConfigError) as caught:
            settings_for(self.tmp, **overrides)
        self.assertIn(fragment, str(caught.exception))

    def test_defaults(self):
        raw = raw_settings(self.tmp)
        del raw["daily_close"]
        settings = hourly.validate_settings(raw)
        self.assertEqual(settings["daily_close"], {"backfill_start": "2026-08-01", "restate_days": 7})
        self.assertEqual(settings["retention"], {"prune_enabled": False, "hourly_keep_hours": 48, "daily_keep_days": 90})
        self.assertEqual(settings["feed"], {"low_stock_threshold": 2, "list_cap": 200, "movers_top": 20})
        self.assertTrue(settings["rows"]["exclude_non_stock_items"])
        self.assertEqual(settings["schedule"]["frequency"], "hourly")

    def test_bad_values(self):
        for bad in ("2026-W31-1", "01/08/2026", "2026-8-1", "2026-02-30", 20260801, None):
            self.assert_config_error("daily_close.backfill_start", daily_close={"backfill_start": bad})
        self.assert_config_error("daily_close.restate_days", daily_close={"restate_days": 0})
        self.assert_config_error("retention.daily_keep_days", retention={"daily_keep_days": 7})
        self.assert_config_error("retention.prune_enabled", retention={"prune_enabled": "yes"})
        self.assert_config_error("retention.hourly_keep_hours", retention={"hourly_keep_hours": 1})
        self.assert_config_error("feed.list_cap", feed={"list_cap": 0})
        self.assert_config_error("rows.exclude_non_stock_items", rows={"exclude_non_stock_items": 1})
        self.assert_config_error("daily_close.hourly", daily_close={"hourly": True})
        self.assert_config_error("schedule.frequency", schedule={"frequency": "daily"})


class RepoHygieneTests(unittest.TestCase):
    def test_local_settings_file_is_gitignored(self):
        self.assertIn("config/*.local.json", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_daily_extractor_imported_not_modified_contract(self):
        # The hourly job reuses the daily extractor's helpers; their names must keep existing.
        from scripts import autocount_stock_extract as extract

        for name in ("SqlExecutionContextError", "resolve_expected_sql_context", "sanitize_error", "load_timezone"):
            self.assertTrue(hasattr(extract, name), name)



# ---------------------------------------------------------------------------------------------
# Error alerts (Telegram through an n8n webhook). Fake URL, fake secret, fake transport only.
# ---------------------------------------------------------------------------------------------

FAKE_ALERT_URL = "https://alerts.example.com/webhook/fake-alert-path"
FAKE_SECRET_ENV = "XB_FAKE_ALERT_SECRET"
FAKE_SECRET = "fake-shared-secret-0001"
ALERT_ENV = dict(ENV, **{FAKE_SECRET_ENV: FAKE_SECRET})
CANARY_ITEM = "CANARY-ITEM-0201"
CANARY_LOCATION = "CANARY-LOC"


def alert_block(**overrides):
    value = {"webhook_enabled": True, "webhook_url": FAKE_ALERT_URL, "secret_env_var": FAKE_SECRET_ENV}
    value.update(overrides)
    return value


class FakeTransport:
    """Records every POST. codes: HTTP answers in order (default 200); error: raised instead."""

    def __init__(self, codes=None, error=None):
        self.calls = []
        self.codes = list(codes or [])
        self.error = error

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "raw": body, "body": json.loads(body), "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.codes.pop(0) if self.codes else 200


def clean_status(**overrides):
    value = {
        "job": hourly.JOB,
        "run_id": "0123456789abcdef0123456789abcdef",
        "status": "success",
        "failure_class": None,
        "daily_close_status": "not_due",
        "delivery_status": "handed_to_drive_sync_folder",
        "feed_status": "published",
        "item_index_status": "published",
        "large_balance_count": 0,
        "late_change_day_count": None,
    }
    value.update(overrides)
    return value


def failed_status(failure_class="ledger_mismatch", **overrides):
    value = clean_status(
        status="failed", failure_class=failure_class, daily_close_status="skipped_failed_run",
        delivery_status="skipped_failed_run", feed_status=None, item_index_status=None, large_balance_count=None,
    )
    value.update(overrides)
    return value


class AlertConfigTests(TempDirTestCase):
    def assert_config_error(self, alert, fragment, **extra):
        with self.assertRaises(hourly.ConfigError) as caught:
            settings_for(self.tmp, alert=alert, **extra)
        self.assertIn(fragment, str(caught.exception))
        # A rejected value never appears in the message.
        for value in alert.values():
            if isinstance(value, str) and len(value) > 3:
                self.assertNotIn(value, str(caught.exception))

    def test_off_by_default_with_bounded_defaults(self):
        self.assertEqual(
            settings_for(self.tmp)["alert"],
            {
                "webhook_enabled": False,
                "webhook_url": None,
                "auth_header_name": "X-XB-Alert-Token",
                "secret_env_var": None,
                "secret_credential_target": None,
                "timeout_seconds": 10,
                "repeat_hours": 24,
            },
        )

    def test_example_settings_ship_alerts_off_without_a_secret(self):
        example = json.loads((ROOT / "config" / "autocount_stock_balance_hourly.example.json").read_text(encoding="utf-8"))
        alert = example["alert"]
        self.assertIs(alert["webhook_enabled"], False)
        self.assertTrue(alert["webhook_url"].startswith("https://n8n.example.com/webhook/"))
        self.assertIsNone(alert["secret_credential_target"])
        self.assertTrue(hourly.ALERT_ENV_VAR_RE.match(alert["secret_env_var"]))
        self.assertEqual(set(alert), set(hourly.DEFAULT_SETTINGS["alert"]))

    def test_enabled_needs_url_and_one_secret_source(self):
        self.assertTrue(settings_for(self.tmp, alert=alert_block())["alert"]["webhook_enabled"])
        self.assert_config_error(alert_block(webhook_url=None), "alert.webhook_url is required")
        self.assert_config_error(alert_block(secret_env_var=None), "secret_env_var or secret_credential_target is required")

    def test_rejected_values(self):
        cases = [
            ({"webhook_enabled": "yes"}, "alert.webhook_enabled"),
            ({"webhook_url": "http://alerts.example.com/webhook/x"}, "alert.webhook_url"),
            ({"webhook_url": "https://user:pw@alerts.example.com/webhook/x"}, "alert.webhook_url"),
            ({"webhook_url": "https://alerts.example.com/webhook/x?token=abc"}, "alert.webhook_url"),
            ({"webhook_url": "https://alerts.example.com/webhook/x#frag"}, "alert.webhook_url"),
            ({"webhook_url": "https://alerts.example.com:99999/webhook/x"}, "alert.webhook_url"),
            ({"webhook_url": "https:///webhook/x"}, "alert.webhook_url"),
            ({"webhook_url": "https://alerts.example.com/web hook"}, "alert.webhook_url"),
            ({"webhook_url": 42}, "alert.webhook_url"),
            ({"secret_env_var": "1BAD"}, "alert.secret_env_var"),
            ({"secret_env_var": "BAD-NAME"}, "alert.secret_env_var"),
            ({"secret_env_var": ""}, "alert.secret_env_var"),
            ({"auth_header_name": "Content-Type"}, "alert.auth_header_name"),
            ({"auth_header_name": "X Bad"}, "alert.auth_header_name"),
            ({"auth_header_name": None}, "alert.auth_header_name"),
            ({"timeout_seconds": 0}, "alert.timeout_seconds"),
            ({"timeout_seconds": 31}, "alert.timeout_seconds"),
            ({"timeout_seconds": True}, "alert.timeout_seconds"),
            ({"repeat_hours": 0}, "alert.repeat_hours"),
            ({"repeat_hours": 169}, "alert.repeat_hours"),
            ({"secret": "x"}, "unknown setting(s): alert.secret"),
        ]
        for override, fragment in cases:
            with self.subTest(override=override):
                self.assert_config_error(alert_block(**override), fragment)

    def test_credential_target_only_with_a_password_logon(self):
        # Pair (a) is S4U, which cannot read Credential Manager secrets: the env var is the way.
        self.assert_config_error(
            alert_block(secret_env_var=None, secret_credential_target="FAKE-ALERT-TARGET"),
            "needs schedule.logon_type Password",
        )
        pair_b = {
            "identity": {"auth_mode": "sql_login", "sql_login": FAKE_SQL_LOGIN, "credential_target": FAKE_CRED_TARGET},
            "schedule": {"logon_type": "Password"},
        }
        settings = settings_for(self.tmp, alert=alert_block(secret_env_var=None, secret_credential_target="FAKE-ALERT-TARGET"), **pair_b)
        self.assertEqual(settings["alert"]["secret_credential_target"], "FAKE-ALERT-TARGET")
        self.assert_config_error(
            alert_block(secret_credential_target="FAKE-ALERT-TARGET"), "set only one of", **pair_b
        )


class AlertRuleTests(unittest.TestCase):
    def test_conditions_table(self):
        cases = [
            ("clean hour", clean_status(), []),
            ("clean, no delivery configured", clean_status(delivery_status="not_configured"), []),
            ("close published", clean_status(daily_close_status="published", late_change_day_count=0), []),
            ("ledger mismatch", failed_status(), ["run_failed:ledger_mismatch"]),
            ("source error", failed_status("source_error"), ["run_failed:source_error"]),
            ("latest publish failed", failed_status("latest_publish_failed"), ["run_failed:latest_publish_failed"]),
            ("unknown failure class", failed_status("made_up"), ["run_failed:unknown"]),
            ("daily close failed", clean_status(daily_close_status="failed"), ["daily_close_failed"]),
            ("delivery verify failed", clean_status(delivery_status="verify_failed"), ["delivery_failed"]),
            ("delivery copy failed", clean_status(delivery_status="copy_failed", feed_status="failed", item_index_status="failed"),
             ["delivery_failed", "feed_failed", "item_index_failed"]),
            ("feed failed", clean_status(feed_status="failed"), ["feed_failed"]),
            ("item index failed", clean_status(item_index_status="failed"), ["item_index_failed"]),
            ("large balance", clean_status(large_balance_count=2), ["large_balance"]),
            ("large balance not an int", clean_status(large_balance_count=True), []),
            ("late change", clean_status(late_change_day_count=3), ["late_change"]),
            ("all success-side conditions",
             clean_status(daily_close_status="failed", delivery_status="copy_failed", feed_status="failed",
                          item_index_status="failed", large_balance_count=1, late_change_day_count=1),
             ["daily_close_failed", "delivery_failed", "feed_failed", "item_index_failed", "large_balance", "late_change"]),
            # A failed run raises run_failed only, whatever else the dict says.
            ("failed run with flags", failed_status(large_balance_count=5, daily_close_status="failed"),
             ["run_failed:ledger_mismatch"]),
        ]
        for name, status, expected in cases:
            with self.subTest(name):
                self.assertEqual(hourly.alert_conditions(status), expected)

    def test_dedup_rule_table(self):
        now = FIXED_NOW
        ago = lambda hours: (now - timedelta(hours=hours)).isoformat()  # noqa: E731
        cases = [
            ("nothing active", {}, [], (None, {})),
            ("cleared conditions are dropped", {"large_balance": ago(1)}, [], (None, {})),
            ("new condition", {}, ["large_balance"], ("new", {})),
            ("same condition within the day", {"large_balance": ago(1)}, ["large_balance"], (None, {"large_balance": ago(1)})),
            ("just under the repeat", {"large_balance": ago(23.99)}, ["large_balance"], (None, {"large_balance": ago(23.99)})),
            ("daily repeat", {"large_balance": ago(24)}, ["large_balance"], ("repeat", {"large_balance": ago(24)})),
            ("another condition joins", {"large_balance": ago(1)}, ["large_balance", "late_change"],
             ("new", {"large_balance": ago(1)})),
            ("different failure class is new", {"run_failed:lock_timeout": ago(1)}, ["run_failed:source_error"], ("new", {})),
            ("damaged time", {"large_balance": "not a time"}, ["large_balance"], ("new", {"large_balance": "not a time"})),
            ("naive time", {"large_balance": "2026-10-23T13:00:00"}, ["large_balance"],
             ("new", {"large_balance": "2026-10-23T13:00:00"})),
        ]
        for name, alerted, active, expected in cases:
            with self.subTest(name):
                self.assertEqual(hourly.plan_alert(alerted, active, now, 24), expected)

    def test_payload_allowlist_holds_against_a_poisoned_status(self):
        poisoned = clean_status(
            large_balance_count=3,
            failure_class=None,
            message="Large balance " + CANARY_ITEM,
            hourly_folder="hourly/" + CANARY_LOCATION,
            large_balances={"rows": [{"ItemCode": CANARY_ITEM, "Location": CANARY_LOCATION, "BalQty": "123456789"}]},
            delivery_status="handed_to_drive_sync_folder " + CANARY_LOCATION,
            daily_close_status=CANARY_ITEM,
            feed_status="built",
            run_id="../" + CANARY_ITEM,
            late_change_day_count="7",
        )
        payload = hourly.build_alert_payload(poisoned, hourly.alert_conditions(poisoned) + ["x:" + CANARY_ITEM], "new", FIXED_NOW)
        self.assertEqual(tuple(payload), hourly.ALERT_PAYLOAD_KEYS)
        text = json.dumps(payload)
        for canary in (CANARY_ITEM, CANARY_LOCATION, "123456789", "hourly/", "Large balance"):
            self.assertNotIn(canary, text)
        self.assertEqual(
            payload,
            {
                "schema": "xb.ac2.stock_alert.v1",
                "event": "ac2_stock_alert",
                "run_id": "unknown",
                "run_hour_local": "2026-10-23T14:00:00+08:00",
                "alert_kind": "new",
                "status": "success",
                "failure_class": None,
                "conditions": ["large_balance"],
                "daily_close_status": None,
                "delivery_status": None,
                "feed_status": None,
                "item_index_status": "published",
                "large_balance_count": 3,
                "late_change_day_count": 0,
            },
        )

    def test_payload_failed_run(self):
        status = failed_status("made_up_class")
        payload = hourly.build_alert_payload(status, hourly.alert_conditions(status), "repeat", FIXED_NOW)
        self.assertEqual(
            (payload["status"], payload["failure_class"], payload["conditions"], payload["alert_kind"], payload["run_id"]),
            ("failed", "unknown", ["run_failed"], "repeat", "0123456789abcdef0123456789abcdef"),
        )

    def test_secret_read(self):
        alert = settings_for(tempfile.gettempdir(), alert=alert_block())["alert"]
        self.assertEqual(hourly.read_alert_secret(alert, ALERT_ENV), FAKE_SECRET)
        for bad in (None, "", "two\nlines", "x" * 4097, "tab\there"):
            env = dict(ENV) if bad is None else dict(ENV, **{FAKE_SECRET_ENV: bad})
            self.assertIsNone(hourly.read_alert_secret(alert, env), repr(bad)[:20])
        target = dict(alert, secret_env_var=None, secret_credential_target="FAKE-ALERT-TARGET")
        self.assertEqual(hourly.read_alert_secret(target, {}, lambda name: FAKE_SECRET), FAKE_SECRET)

        def unavailable(name):
            raise hourly.CredentialUnavailableError("no credential")

        self.assertIsNone(hourly.read_alert_secret(target, {}, unavailable))


class AlertRunTests(TempDirTestCase):
    def settings(self, **alert):
        return settings_for(self.tmp, alert=alert_block(**alert))

    def run_alert(self, settings, source=None, hours=0, transport=None, environ=None):
        when = FIXED_NOW + timedelta(hours=hours)
        return hourly.run_hourly(
            settings,
            source=source or FakeSource(),
            environ=ALERT_ENV if environ is None else environ,
            clock=clock_at(when),
            sleep=lambda seconds: None,
            alert_transport=transport,
        )

    def status_file(self):
        return json.loads((self.archive / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))

    def state(self):
        path = self.archive / hourly.ALERT_STATE_NAME
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def mismatch_source(self):
        return FakeSource(ledger=[])

    def test_disabled_sends_nothing_and_says_so(self):
        transport = FakeTransport()
        manifest = self.run_alert(settings_for(self.tmp), self.mismatch_source(), transport=transport)
        self.assertEqual(manifest["failure_class"], "ledger_mismatch")
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.status_file()["alert_status"], "disabled")
        self.assertIsNone(self.state())

    def test_clean_hour_sends_nothing(self):
        transport = FakeTransport()
        self.run_alert(self.settings(), transport=transport)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.status_file()["alert_status"], "no_alert")

    def test_failed_run_posts_one_sanitized_event(self):
        transport = FakeTransport()
        manifest = self.run_alert(self.settings(), self.mismatch_source(), transport=transport)
        (call,) = transport.calls
        self.assertEqual(call["url"], FAKE_ALERT_URL)
        self.assertEqual(call["timeout"], 10)
        self.assertEqual(
            call["headers"],
            {"Content-Type": "application/json", "User-Agent": hourly.ALERT_USER_AGENT, "X-XB-Alert-Token": FAKE_SECRET},
        )
        body = call["body"]
        self.assertEqual(tuple(body), hourly.ALERT_PAYLOAD_KEYS)
        self.assertEqual(
            (body["status"], body["failure_class"], body["conditions"], body["alert_kind"], body["run_id"]),
            ("failed", "ledger_mismatch", ["run_failed"], "new", manifest["run_id"]),
        )
        self.assertEqual(body["run_hour_local"], "2026-10-23T14:00:00+08:00")
        raw = call["raw"].decode("ascii")
        for leak in ("FAKE-0001", "LOC-A", FAKE_SECRET, FAKE_DB, FAKE_SERVER.split("\\")[0], str(self.tmp), "hourly/"):
            self.assertNotIn(leak, raw)
        self.assertEqual(self.status_file()["alert_status"], "sent")
        self.assertEqual(self.state()["alerted"], {"run_failed:ledger_mismatch": FIXED_NOW.isoformat()})
        # The shared secret never lands on disk.
        for path in self.archive.rglob("*"):
            if path.is_file():
                self.assertNotIn(FAKE_SECRET.encode(), path.read_bytes(), path.name)

    def test_large_balance_event_carries_the_count_only(self):
        balances = default_balances() + [bal(CANARY_ITEM, "LOC-A", "123456789")]
        transport = FakeTransport()
        manifest = self.run_alert(self.settings(), FakeSource(balances=balances), transport=transport)
        self.assertEqual(manifest["status"], "success")
        (call,) = transport.calls
        self.assertEqual((call["body"]["conditions"], call["body"]["large_balance_count"]), (["large_balance"], 1))
        self.assertNotIn(CANARY_ITEM, call["raw"].decode("ascii"))
        self.assertNotIn("123456789", call["raw"].decode("ascii"))

    def test_same_problem_alerts_once_a_day_then_again_after_recovery(self):
        settings = self.settings()
        transport = FakeTransport()
        expected_kinds = []
        for hours in range(0, 26):
            self.run_alert(settings, self.mismatch_source(), hours=hours, transport=transport)
            if hours in (0, 24):
                expected_kinds.append("new" if hours == 0 else "repeat")
            self.assertEqual(self.status_file()["alert_status"], "sent" if hours in (0, 24) else "suppressed", hours)
        self.assertEqual([c["body"]["alert_kind"] for c in transport.calls], expected_kinds)
        # Recovery sends nothing (errors only) and clears the state ...
        self.run_alert(settings, hours=26, transport=transport)
        self.assertEqual(self.status_file()["alert_status"], "no_alert")
        self.assertEqual(self.state()["alerted"], {})
        self.assertEqual(len(transport.calls), 2)
        # ... so the next occurrence is new again.
        self.run_alert(settings, self.mismatch_source(), hours=27, transport=transport)
        self.assertEqual([c["body"]["alert_kind"] for c in transport.calls], ["new", "repeat", "new"])

    def test_failed_send_is_retried_next_hour(self):
        settings = self.settings()
        transport = FakeTransport(codes=[502, 200])
        self.run_alert(settings, self.mismatch_source(), transport=transport)
        self.assertEqual(self.status_file()["alert_status"], "send_failed")
        self.assertIsNone(self.state())
        self.run_alert(settings, self.mismatch_source(), hours=1, transport=transport)
        self.assertEqual(self.status_file()["alert_status"], "sent")
        self.assertEqual([c["body"]["alert_kind"] for c in transport.calls], ["new", "new"])

    def test_missing_secret_sends_nothing(self):
        transport = FakeTransport()
        self.run_alert(self.settings(), self.mismatch_source(), transport=transport, environ=ENV)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.status_file()["alert_status"], "secret_unavailable")

    def test_unreadable_state_alerts_again(self):
        settings = self.settings()
        transport = FakeTransport()
        self.run_alert(settings, self.mismatch_source(), transport=transport)
        (self.archive / hourly.ALERT_STATE_NAME).write_text("{not json", encoding="utf-8")
        self.run_alert(settings, self.mismatch_source(), hours=1, transport=transport)
        self.assertEqual(len(transport.calls), 2)

    def test_state_write_failure_after_a_send_still_reports_sent(self):
        transport = FakeTransport()
        with mock.patch.object(hourly, "save_alert_state", side_effect=OSError("disk full")):
            self.run_alert(self.settings(), self.mismatch_source(), transport=transport)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.status_file()["alert_status"], "sent")

    def test_alert_state_stays_local_and_out_of_drive(self):
        settings = settings_for(self.tmp, alert=alert_block(), output={"delivery_root": str(self.delivery)})
        self.run_alert(settings, self.mismatch_source(), transport=FakeTransport())
        self.assertTrue((self.archive / hourly.ALERT_STATE_NAME).exists())
        self.assertFalse((self.delivery / hourly.ALERT_STATE_NAME).exists())
        self.assertEqual(json.loads((self.delivery / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))["alert_status"], "sent")

    def test_config_failure_status_records_no_alert_attempt(self):
        raw = raw_settings(self.tmp, alert=alert_block(webhook_url="http://insecure.example.com/x"))
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        transport = FakeTransport()
        with contextlib.redirect_stdout(io.StringIO()):
            code = hourly.main(["--config", str(path)], source=FakeSource(), environ=ALERT_ENV, clock=clock_at(),
                               alert_transport=transport)
        self.assertEqual(code, hourly.EXIT_FAILED)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.status_file()["alert_status"], "skipped_config_invalid")


class AlertNeverChangesTheRunTests(TempDirTestCase):
    """An alert failure must never change the exit code, the run status or any output file."""

    def main_run(self, alert, source_factory, transport, environ=ALERT_ENV):
        root = Path(self.tmp) / ("case_%d" % len(os.listdir(self.tmp)))
        raw = raw_settings(root, alert=alert, output={"delivery_root": str(root / "drive_sync")})
        path = Path(self.tmp) / ("settings_%s.local.json" % root.name)
        path.write_text(json.dumps(raw), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = hourly.main(["--config", str(path)], source=source_factory(), environ=environ, clock=clock_at(),
                               alert_transport=transport)
        printed = json.loads(out.getvalue())
        archive = root / "archive"
        files = sorted(
            str(p.relative_to(archive)).replace("\\", "/").split("/ac2_stockbal_")[0]
            for p in archive.rglob("*") if p.is_file() and p.name != hourly.ALERT_STATE_NAME
        )
        return code, printed, files

    def test_table(self):
        scenarios = {
            "clean hour": lambda: FakeSource(),
            "ledger mismatch": lambda: FakeSource(ledger=[]),
            "large balance": lambda: FakeSource(balances=default_balances() + [bal("FAKE-0201", "LOC-A", "123456789")]),
        }
        # Factories: every case gets a fresh transport.
        behaviours = {
            "answer 200": (lambda: FakeTransport(), ALERT_ENV),
            "answer 500": (lambda: FakeTransport(codes=[500]), ALERT_ENV),
            "answer 302": (lambda: FakeTransport(codes=[302]), ALERT_ENV),
            "timeout": (lambda: FakeTransport(error=TimeoutError("timed out")), ALERT_ENV),
            "network error": (lambda: FakeTransport(error=OSError("unreachable")), ALERT_ENV),
            "bug in transport": (lambda: FakeTransport(error=RuntimeError("bug")), ALERT_ENV),
            "secret missing": (lambda: FakeTransport(), ENV),
        }
        ignored = ("alert_status", "run_id", "message")
        for scenario, factory in scenarios.items():
            base_code, base_printed, base_files = self.main_run({"webhook_enabled": False}, factory, None)
            for name, (make_transport, environ) in behaviours.items():
                with self.subTest(scenario=scenario, behaviour=name):
                    code, printed, files = self.main_run(alert_block(), factory, make_transport(), environ)
                    self.assertEqual(code, base_code)
                    self.assertEqual(
                        {k: v for k, v in printed.items() if k not in ignored},
                        {k: v for k, v in base_printed.items() if k not in ignored},
                    )
                    self.assertEqual(files, base_files)
                    if scenario == "clean hour":
                        self.assertEqual(printed["alert_status"], "no_alert")
                    elif name == "answer 200":
                        self.assertEqual(printed["alert_status"], "sent")
                    elif name == "secret missing":
                        self.assertEqual(printed["alert_status"], "secret_unavailable")
                    elif name == "bug in transport":
                        self.assertEqual(printed["alert_status"], "error")
                    else:
                        self.assertEqual(printed["alert_status"], "send_failed")


class AlertTransportTests(unittest.TestCase):
    """The real POST helper against a loopback HTTP server (no proxy, no redirect)."""

    def serve(self, code, location=None):
        import http.server
        import threading

        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server hook
                length = int(self.headers.get("Content-Length", "0"))
                headers = {key.lower(): value for key, value in self.headers.items()}
                seen.append({"path": self.path, "headers": headers, "body": self.rfile.read(length)})
                self.send_response(code)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self):  # noqa: N802 - a followed redirect would arrive here
                seen.append({"path": self.path, "headers": {}, "body": b"GET"})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d/webhook/fake" % server.server_address[1], seen

    def test_post_returns_the_status_and_sends_the_headers(self):
        url, seen = self.serve(200)
        code = hourly.post_alert(url, {"Content-Type": "application/json", "X-XB-Alert-Token": FAKE_SECRET}, b"{}", 5)
        self.assertEqual(code, 200)
        self.assertEqual(seen[0]["headers"]["x-xb-alert-token"], FAKE_SECRET)
        self.assertEqual(seen[0]["body"], b"{}")

    def test_error_answer_is_returned_not_raised(self):
        url, _ = self.serve(502)
        self.assertEqual(hourly.post_alert(url, {}, b"{}", 5), 502)

    def test_redirect_is_not_followed(self):
        target, followed = self.serve(200)
        # urllib's default handler would follow a 302 to a POST (as a GET, with the headers).
        for code in (302, 307):
            url, seen = self.serve(code, location=target)
            self.assertEqual(hourly.post_alert(url, {"X-XB-Alert-Token": FAKE_SECRET}, b"{}", 5), code)
            self.assertEqual((len(seen), followed), (1, []))

    def test_proxy_settings_are_ignored(self):
        url, seen = self.serve(200)
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                                          "NO_PROXY": ""}):
            self.assertEqual(hourly.post_alert(url, {}, b"{}", 5), 200)
        self.assertEqual(len(seen), 1)


class CloseStateWarningTests(Round6TestCase):
    """VM D2 (28 Oct): a first --backfill with no close state wrote everything and gave no
    "Close state was missing" warning. That is by design: the warning is for a lost state on a
    normal run; daily_close.state still records "missing"."""

    def test_first_backfill_and_first_run_do_not_warn(self):
        for force in (True, False):
            with self.subTest(force=force):
                shutil.rmtree(self.archive, ignore_errors=True)
                when = at(2026, 8, 10)
                manifest = hourly.run_hourly(
                    self.settings(), source=ledger_source(aug_ledger(), AUG_ITEMS, sql_now=when + timedelta(seconds=1)),
                    environ=ENV, clock=clock_at(when), sleep=lambda seconds: None, force_close=force,
                )
                self.assertEqual(manifest["daily_close"]["status"], "published")
                self.assertEqual(
                    (manifest["daily_close"]["mode"], manifest["daily_close"]["state"]),
                    ("backfill" if force else "initial", "missing"),
                )
                self.assertFalse([w for w in manifest["warnings"] if w.startswith("Close state was")])

    def test_backfill_over_a_lost_state_does_not_warn(self):
        settings = self.settings()
        self.run_at(settings, at(2026, 8, 10))
        (self.archive / "daily" / hourly.CLOSE_STATE_NAME).unlink()
        when = at(2026, 8, 10, 1)
        manifest = hourly.run_hourly(
            settings, source=ledger_source(aug_ledger(), AUG_ITEMS, sql_now=when + timedelta(seconds=1)),
            environ=ENV, clock=clock_at(when), sleep=lambda seconds: None, force_close=True,
        )
        self.assertEqual((manifest["daily_close"]["mode"], manifest["daily_close"]["state"]), ("backfill", "missing"))
        self.assertFalse([w for w in manifest["warnings"] if w.startswith("Close state was")])


if __name__ == "__main__":
    unittest.main()
