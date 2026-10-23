"""Offline tests for the hourly AC2 stock balance export (#245).

In-memory fakes and temporary folders only. No SQL Server, AutoCount, Drive, n8n or Task
Scheduler is touched. Every identity, host, item code and location code here is synthetic.
No fixture files: data is built in memory.
"""

import contextlib
import copy
import csv
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
from datetime import datetime, timedelta
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
        return SQL_NOW

    def read_balances(self):
        if self.lock_timeout:
            raise hourly.SourceLockTimeout("Lock timeout while reading")
        return copy.deepcopy(self.source.balances)

    def read_multi_uom_item_count(self):
        return self.source.multi_uom

    def read_items_without_rows(self):
        self.source.no_row_reads += 1
        return copy.deepcopy(self.source.no_row_items)

    def read_sync(self):
        if self.source.sync_error:
            raise hourly.SyncReadError("SyncRegistration read failed")
        return copy.deepcopy(self.source.sync_rows)


class FakeSource:
    def __init__(self, balances=None, no_row_items=None, sync_rows=None, identity=None,
                 changecounts=None, multi_uom=0, lock_timeouts=None, sync_error=False):
        self.balances = default_balances() if balances is None else balances
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
        for code in raw["location_labels"]:
            self.assertTrue(code.startswith("LOC-"), "example labels must use fake codes")
        self.assertIn("EXAMPLE", raw["output"]["delivery_root"])

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

    def test_delivery_copies_verified(self):
        manifest = run(self.settings())
        self.assertEqual(manifest["delivery"]["status"], "handed_to_drive_sync_folder")
        self.assertEqual(manifest["delivery"]["files"], [manifest["hourly_folder"] + "/" + hourly.CSV_NAME])
        for name in (hourly.CSV_NAME, hourly.MANIFEST_NAME):
            self.assertEqual(
                (self.run_dir(manifest, self.delivery) / name).read_bytes(),
                (self.run_dir(manifest) / name).read_bytes(),
            )
        self.assertEqual(
            (self.delivery / hourly.LATEST_CSV_NAME).read_bytes(),
            (self.run_dir(manifest) / hourly.CSV_NAME).read_bytes(),
        )
        status = json.loads((self.delivery / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["delivery_status"], "handed_to_drive_sync_folder")
        manifest_text = (self.run_dir(manifest) / hourly.MANIFEST_NAME).read_text(encoding="utf-8")
        self.assertNotIn("uploaded", manifest_text.lower())
        self.assertNotIn(str(self.tmp), manifest_text, "delivery paths are relative")

    def test_tampered_copy_is_verify_failed_and_latest_not_advanced(self):
        settings = self.settings()
        good = run(settings)
        latest_before = (self.delivery / hourly.LATEST_CSV_NAME).read_bytes()
        real_copy = hourly.copy_exclusive

        def bad_copy(src, dst):
            real_copy(src, dst)
            if Path(dst).name == hourly.CSV_NAME:
                with open(dst, "ab") as handle:
                    handle.write(b"x")

        with mock.patch.object(hourly, "copy_exclusive", side_effect=bad_copy):
            manifest = run(settings)
        self.assertEqual(manifest["status"], "success", "archive data is still good")
        self.assertEqual(manifest["delivery"]["status"], "verify_failed")
        self.assertEqual((self.delivery / hourly.LATEST_CSV_NAME).read_bytes(), latest_before)
        latest = json.loads((self.delivery / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(latest["run_id"], good["run_id"])
        status = json.loads((self.delivery / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["delivery_status"], "verify_failed")
        # The archive latest pair still advances: the archive copy verified.
        archive_latest = json.loads((self.archive / hourly.LATEST_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(archive_latest["run_id"], manifest["run_id"])

    def test_tampered_manifest_copy_blocks_delivery_latest(self):
        settings = self.settings()
        real_copy = hourly.copy_exclusive

        def bad_copy(src, dst):
            real_copy(src, dst)
            if Path(dst).name == hourly.MANIFEST_NAME:
                with open(dst, "ab") as handle:
                    handle.write(b" ")

        with mock.patch.object(hourly, "copy_exclusive", side_effect=bad_copy):
            run(settings)
        self.assertFalse((self.delivery / hourly.LATEST_CSV_NAME).exists())
        status = json.loads((self.delivery / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["delivery_status"], "verify_failed")

    def test_cli_exit_code_when_delivery_fails(self):
        path = Path(self.tmp) / "settings.local.json"
        path.write_text(json.dumps(raw_settings(self.tmp, output={"delivery_root": str(self.delivery)})), encoding="utf-8")

        def bad_copy(src, dst):
            hourly.write_exclusive(dst, b"not the same")

        with mock.patch.object(hourly, "copy_exclusive", side_effect=bad_copy), contextlib.redirect_stdout(io.StringIO()) as out:
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
        status = json.loads((self.delivery / hourly.LAST_RUN_STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "failed")
        self.assertFalse((self.delivery / hourly.LATEST_CSV_NAME).exists())
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


class LockTests(TempDirTestCase):
    OTHER_RUN = "deadbeefcafe0001"
    OTHER_PID = 4242

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


class RepoHygieneTests(unittest.TestCase):
    def test_local_settings_file_is_gitignored(self):
        self.assertIn("config/*.local.json", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_daily_extractor_imported_not_modified_contract(self):
        # The hourly job reuses the daily extractor's helpers; their names must keep existing.
        from scripts import autocount_stock_extract as extract

        for name in ("SqlExecutionContextError", "resolve_expected_sql_context", "sanitize_error", "load_timezone"):
            self.assertTrue(hasattr(extract, name), name)


if __name__ == "__main__":
    unittest.main()
