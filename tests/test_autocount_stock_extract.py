import csv
import contextlib
import io
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_stock_extract as extract


class FakeSource:
    def __init__(self, rows_by_dataset):
        self.rows_by_dataset = rows_by_dataset
        self.calls = []

    def fetch_dataset(self, dataset_name, dataset_config, context):
        self.calls.append((dataset_name, dataset_config, context))
        return list(self.rows_by_dataset.get(dataset_name, []))


class FakeNotifier:
    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)


# Synthetic identities only. No real AutoCount server, database, login or Windows account name
# appears in this repository.
SYNTHETIC_LOGIN = "SYNTHHOST\\svc_synthetic_readonly"
SYNTHETIC_USER = "svc_synthetic_readonly"
SYNTHETIC_DATABASE = "SYNTHETIC_STOCK_DB"
SYNTHETIC_OTHER_LOGIN = "SYNTHHOST\\svc_synthetic_admin"
SYNTHETIC_OTHER_USER = "dbo"
SYNTHETIC_OTHER_DATABASE = "SYNTHETIC_OTHER_DB"
SYNTHETIC_QUERY = "SELECT ItemCode FROM synthetic_item"
SYNTHETIC_SECOND_QUERY = "SELECT ItemCode, BalQty FROM synthetic_balance"


class FakeSqlCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self.connection.executed.append(sql)
        if sql == extract.SQL_EXECUTION_CONTEXT_QUERY:
            self.description = [("actual_login",), ("actual_user",), ("actual_database",)]
            self._rows = [tuple(self.connection.context_row)]
            return self
        self.connection.business_queries.append(sql)
        self.description = [("ItemCode",)]
        self._rows = [("SYNTH-ITEM-1",)]
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeSqlConnection:
    def __init__(self, context_row):
        self.context_row = context_row
        self.executed = []
        self.business_queries = []

    def cursor(self):
        return FakeSqlCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        # Never swallow: a fail-closed identity error must propagate out of the with block.
        return False


class FakePyodbcModule:
    def __init__(self, context_row):
        self.context_row = context_row
        self.connections = []

    def connect(self, connection_string, autocommit=False):
        connection = FakeSqlConnection(self.context_row)
        self.connections.append(connection)
        return connection


@contextlib.contextmanager
def fake_pyodbc(module):
    """Serve the extractor's in-function ``import pyodbc`` from a fake, never a live driver."""
    previous = sys.modules.get("pyodbc")
    sys.modules["pyodbc"] = module
    try:
        yield module
    finally:
        if previous is None:
            sys.modules.pop("pyodbc", None)
        else:
            sys.modules["pyodbc"] = previous


@contextlib.contextmanager
def expected_sql_context_env(login=None, user=None, database=None):
    """Set or clear the expected-context environment for one test. Synthetic values only."""
    names = [
        extract.EXPECTED_SQL_CONTEXT_ENV["login"],
        extract.EXPECTED_SQL_CONTEXT_ENV["user"],
        extract.EXPECTED_SQL_CONTEXT_ENV["database"],
    ]
    values = [login, user, database]
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name, value in zip(names, values):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def sqlserver_config(archive_root, datasets=None):
    config = base_config(archive_root)
    config["source_connection"] = {
        "kind": "sqlserver",
        # Synthetic placeholder; the fake driver never parses it.
        "connection_string": "synthetic-test-connection",
    }
    config["datasets"] = datasets or {"stock_master": {"mode": "full", "query": SYNTHETIC_QUERY}}
    return config


def base_config(archive_root):
    return {
        "source": "autocount_ac2",
        "job": "daily_stock_extract",
        "timezone": "Asia/Singapore",
        "overlap_days": 3,
        "archive_root": str(archive_root),
        "datasets": {
            "stock_master": {"mode": "full", "query": "SELECT * FROM item"},
            "stock_balance": {"mode": "snapshot", "query": "SELECT * FROM balance"},
            "stock_movement": {"mode": "window", "query": "SELECT * FROM movement"},
        },
    }


class AutoCountStockExtractTests(unittest.TestCase):
    def test_load_config_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "autocount_stock_extract.local.json"
            path.write_text("\ufeff" + json.dumps(base_config(Path(tmpdir))), encoding="utf-8")

            loaded = extract.load_config(path)

            self.assertEqual(loaded["source"], "autocount_ac2")
            self.assertEqual(loaded["timezone"], "Asia/Singapore")

    def test_build_run_context_uses_overlap_window(self):
        context = extract.build_run_context(
            business_date="2026-06-04",
            overlap_days=3,
            timezone_name="Asia/Singapore",
            now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
        )

        self.assertEqual(context["business_date"], "2026-06-04")
        self.assertEqual(context["storage_batch_id"], "ac2_stock_2026-06-04")
        self.assertEqual(context["movement_from"], "2026-06-01T00:00:00+08:00")
        self.assertEqual(context["movement_to"], "2026-06-05T00:00:00+08:00")
        self.assertEqual(context["snapshot_as_at"], "2026-06-04T23:59:59+08:00")

    def test_successful_run_archives_csvs_and_manifest_without_raw_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rows = {
                "stock_master": [{"ItemCode": "SKU-001", "IsActive": "T"}],
                "stock_balance": [{"ItemCode": "SKU-001", "Location": "HQ", "BalQty": 5}],
                "stock_movement": [{"DocNo": "ADJ-001", "ItemCode": "SKU-001", "Qty": 1}],
            }
            notifier = FakeNotifier()

            manifest = extract.run_extraction(
                base_config(Path(tmpdir)),
                source=FakeSource(rows),
                notifier=notifier,
                business_date="2026-06-04",
                now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
            )

            batch_dir = Path(tmpdir) / "ac2_stock_2026-06-04"
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["row_counts"]["stock_master"], 1)
            self.assertEqual(manifest["row_counts"]["stock_balance"], 1)
            self.assertEqual(manifest["row_counts"]["stock_movement"], 1)
            self.assertTrue((batch_dir / "stock_master.csv").exists())
            self.assertTrue((batch_dir / "stock_balance.csv").exists())
            self.assertTrue((batch_dir / "stock_movement.csv").exists())

            saved_manifest = json.loads((batch_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_manifest["storage_batch_id"], "ac2_stock_2026-06-04")
            self.assertRegex(saved_manifest["run_id"], r"^[0-9a-f-]{36}$")
            self.assertEqual(manifest["run_id"], saved_manifest["run_id"])
            self.assertNotIn("rows", saved_manifest)
            self.assertNotIn("data", saved_manifest)
            self.assertIn("files", saved_manifest["storage"])
            for dataset_name in ("stock_master", "stock_balance", "stock_movement"):
                dataset_file = batch_dir / f"{dataset_name}.csv"
                file_record = saved_manifest["storage"]["files"][dataset_name]

                self.assertEqual(file_record["path"], str(dataset_file))
                self.assertEqual(file_record["byte_size"], dataset_file.stat().st_size)
                self.assertEqual(file_record["sha256"], hashlib.sha256(dataset_file.read_bytes()).hexdigest())
            self.assertEqual(notifier.payloads, [saved_manifest])

    def test_rerun_same_business_date_replaces_dataset_file_and_uses_new_run_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = base_config(Path(tmpdir))
            first_source = FakeSource({"stock_master": [{"ItemCode": "SKU-001"}]})
            second_source = FakeSource({"stock_master": [{"ItemCode": "SKU-002"}]})

            first_manifest = extract.run_extraction(
                config,
                source=first_source,
                notifier=None,
                business_date="2026-06-04",
                now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
            )
            second_manifest = extract.run_extraction(
                config,
                source=second_source,
                notifier=None,
                business_date="2026-06-04",
                now=datetime.fromisoformat("2026-06-05T02:10:00+08:00"),
            )

            csv_path = Path(tmpdir) / "ac2_stock_2026-06-04" / "stock_master.csv"
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(rows, [{"ItemCode": "SKU-002"}])
            self.assertEqual(first_manifest["storage_batch_id"], second_manifest["storage_batch_id"])
            self.assertNotEqual(first_manifest["run_id"], second_manifest["run_id"])

    def test_failed_run_writes_failure_manifest_and_notifies_without_secret_config(self):
        class FailingSource(FakeSource):
            def fetch_dataset(self, dataset_name, dataset_config, context):
                if dataset_name == "stock_movement":
                    raise RuntimeError("database unavailable")
                return super().fetch_dataset(dataset_name, dataset_config, context)

        with tempfile.TemporaryDirectory() as tmpdir:
            config = base_config(Path(tmpdir))
            config["notification"] = {"webhook_url": "https://example.invalid/secret-path"}
            notifier = FakeNotifier()

            manifest = extract.run_extraction(
                config,
                source=FailingSource({"stock_master": [{"ItemCode": "SKU-001"}]}),
                notifier=notifier,
                business_date="2026-06-04",
                now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
            )

            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["exception_count"], 1)
            self.assertIn("stock_movement", manifest["exceptions"][0]["dataset"])
            self.assertNotIn("webhook_url", json.dumps(manifest))
            self.assertEqual(notifier.payloads, [manifest])

    def test_notification_failure_does_not_fail_successful_archive(self):
        class FailingNotifier:
            def send(self, payload):
                raise RuntimeError("webhook receiver unavailable")

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = extract.run_extraction(
                base_config(Path(tmpdir)),
                source=FakeSource({"stock_master": [{"ItemCode": "SKU-001"}]}),
                notifier=FailingNotifier(),
                business_date="2026-06-04",
                now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
            )

            saved_manifest = json.loads(
                (Path(tmpdir) / "ac2_stock_2026-06-04" / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(saved_manifest["status"], "success")
            self.assertIn("notification_error", manifest)

    def test_sql_context_parameters_are_converted_to_datetime_objects(self):
        context = extract.build_run_context(
            business_date="2026-06-04",
            overlap_days=3,
            timezone_name="Asia/Singapore",
            now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
        )

        params = extract.resolve_query_parameters(["movement_from", "movement_to", "snapshot_as_at"], context)

        self.assertEqual(params[0], datetime.fromisoformat("2026-06-01T00:00:00"))
        self.assertEqual(params[1], datetime.fromisoformat("2026-06-05T00:00:00"))
        self.assertEqual(params[2], datetime.fromisoformat("2026-06-04T23:59:59"))

    def test_missing_timezone_data_error_is_operator_friendly(self):
        original_zoneinfo = extract.ZoneInfo

        def missing_timezone_data(timezone_name):
            raise ZoneInfoNotFoundError(f"No time zone found with key {timezone_name}")

        extract.ZoneInfo = missing_timezone_data
        try:
            with self.assertRaisesRegex(RuntimeError, "Windows Python.*tzdata.*python -m pip install tzdata"):
                extract.build_run_context(
                    business_date="2026-06-04",
                    timezone_name="Asia/Singapore",
                    now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
                )
        finally:
            extract.ZoneInfo = original_zoneinfo

    def test_smoke_example_config_contract_is_secret_free(self):
        config_path = ROOT / "config" / "autocount_stock_extract.ac2_smoke.example.json"

        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(config["job"], "phase1_stock_extract_smoke")
        self.assertEqual(config["source"], "autocount_ac2")
        self.assertEqual(config["timezone"], "Asia/Singapore")
        self.assertEqual(config["overlap_days"], 3)
        self.assertEqual(config["archive_root"], r"C:\XB\autocount_outputs\extract\stock")
        self.assertEqual(
            config["source_connection"],
            {
                "kind": "sqlserver",
                "connection_string_env": "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
            },
        )
        self.assertNotIn("connection_string", set(config["source_connection"]) - {"connection_string_env"})
        self.assertNotRegex(json.dumps(config), r"(?i)(Driver=|Server=|Password=|PWD=|Trusted_Connection=)")
        self.assertEqual(set(config["datasets"]), {"stock_master", "stock_balance", "stock_movement"})

    def test_smoke_example_config_dry_run_context_still_works(self):
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = extract.main(
                [
                    "--config",
                    str(ROOT / "config" / "autocount_stock_extract.ac2_smoke.example.json"),
                    "--business-date",
                    "2026-06-04",
                    "--dry-run",
                ]
            )

        context = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(context["business_date"], "2026-06-04")
        self.assertEqual(context["movement_from"], "2026-06-01T00:00:00+08:00")
        self.assertEqual(context["movement_to"], "2026-06-05T00:00:00+08:00")

    # ---- #149: fail-closed SQL execution context guard ---- #

    def assertNoPrivateIdentity(self, text):
        """No observed or expected identity, nor any connection detail, may reach the operator."""
        for private in (
            SYNTHETIC_LOGIN,
            SYNTHETIC_USER,
            SYNTHETIC_DATABASE,
            SYNTHETIC_OTHER_LOGIN,
            SYNTHETIC_OTHER_DATABASE,
            "synthetic-test-connection",
        ):
            self.assertNotIn(private, text, private)
        self.assertNotRegex(text, r"(?i)(Driver=|Server=|Password=|PWD=|Trusted_Connection=)")

    def test_expected_context_env_names_are_the_canonical_contract(self):
        self.assertEqual(
            extract.EXPECTED_SQL_CONTEXT_ENV,
            {
                "login": "AUTOCOUNT_EXPECTED_SQL_LOGIN",
                "user": "AUTOCOUNT_EXPECTED_SQL_USER",
                "database": "AUTOCOUNT_EXPECTED_SQL_DATABASE",
            },
        )

    def test_resolve_expected_context_rejects_missing_and_blank_values(self):
        with expected_sql_context_env():
            with self.assertRaises(extract.SqlExecutionContextError) as caught:
                extract.resolve_expected_sql_context()
        message = str(caught.exception)
        self.assertIn("not configured", message)
        # The variable NAMES are a committed contract and may be named; values never are.
        for env_name in extract.EXPECTED_SQL_CONTEXT_ENV.values():
            self.assertIn(env_name, message)

        with expected_sql_context_env(SYNTHETIC_LOGIN, "   ", SYNTHETIC_DATABASE):
            with self.assertRaises(extract.SqlExecutionContextError) as blank:
                extract.resolve_expected_sql_context()
        self.assertIn("AUTOCOUNT_EXPECTED_SQL_USER", str(blank.exception))
        self.assertNoPrivateIdentity(str(blank.exception))

    def test_sqlserver_mode_without_expected_context_fails_before_any_connection(self):
        module = FakePyodbcModule((SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE))

        with tempfile.TemporaryDirectory() as tmpdir:
            with expected_sql_context_env(), fake_pyodbc(module):
                with self.assertRaises(extract.SqlExecutionContextError) as caught:
                    extract.run_extraction(
                        sqlserver_config(Path(tmpdir)),
                        business_date="2026-06-04",
                        now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
                    )

            self.assertEqual(module.connections, [])
            self.assertFalse((Path(tmpdir) / "ac2_stock_2026-06-04" / "run_manifest.json").exists())
        self.assertIn("not configured", str(caught.exception))
        self.assertNoPrivateIdentity(str(caught.exception))

    def test_sqlserver_mode_login_mismatch_fails_before_business_query(self):
        module = FakePyodbcModule((SYNTHETIC_OTHER_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE))

        with tempfile.TemporaryDirectory() as tmpdir:
            with expected_sql_context_env(SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE), fake_pyodbc(module):
                with self.assertRaises(extract.SqlExecutionContextError) as caught:
                    extract.run_extraction(
                        sqlserver_config(Path(tmpdir)),
                        business_date="2026-06-04",
                        now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
                    )

            self.assertFalse((Path(tmpdir) / "ac2_stock_2026-06-04" / "run_manifest.json").exists())
        self.assertEqual(len(module.connections), 1)
        self.assertEqual(module.connections[0].business_queries, [])
        self.assertEqual(module.connections[0].executed, [extract.SQL_EXECUTION_CONTEXT_QUERY])
        self.assertEqual(str(caught.exception), "SQL execution context mismatch: login")
        self.assertNoPrivateIdentity(str(caught.exception))

    def test_sqlserver_mode_database_user_mismatch_fails_before_business_query(self):
        module = FakePyodbcModule((SYNTHETIC_LOGIN, SYNTHETIC_OTHER_USER, SYNTHETIC_DATABASE))

        with tempfile.TemporaryDirectory() as tmpdir:
            with expected_sql_context_env(SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE), fake_pyodbc(module):
                with self.assertRaises(extract.SqlExecutionContextError) as caught:
                    extract.run_extraction(
                        sqlserver_config(Path(tmpdir)),
                        business_date="2026-06-04",
                        now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
                    )

        self.assertEqual(module.connections[0].business_queries, [])
        self.assertEqual(str(caught.exception), "SQL execution context mismatch: user")
        self.assertNoPrivateIdentity(str(caught.exception))

    def test_sqlserver_mode_database_mismatch_fails_before_business_query(self):
        module = FakePyodbcModule((SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_OTHER_DATABASE))

        with tempfile.TemporaryDirectory() as tmpdir:
            with expected_sql_context_env(SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE), fake_pyodbc(module):
                with self.assertRaises(extract.SqlExecutionContextError) as caught:
                    extract.run_extraction(
                        sqlserver_config(Path(tmpdir)),
                        business_date="2026-06-04",
                        now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
                    )

        self.assertEqual(module.connections[0].business_queries, [])
        self.assertEqual(str(caught.exception), "SQL execution context mismatch: database")
        self.assertNoPrivateIdentity(str(caught.exception))

    def test_sqlserver_mode_exact_context_match_permits_configured_query(self):
        module = FakePyodbcModule((SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE))

        with tempfile.TemporaryDirectory() as tmpdir:
            with expected_sql_context_env(SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE), fake_pyodbc(module):
                manifest = extract.run_extraction(
                    sqlserver_config(Path(tmpdir)),
                    business_date="2026-06-04",
                    now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
                )

            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["row_counts"]["stock_master"], 1)
            self.assertTrue((Path(tmpdir) / "ac2_stock_2026-06-04" / "stock_master.csv").exists())
        self.assertEqual(module.connections[0].business_queries, [SYNTHETIC_QUERY])

    def test_context_assertion_runs_first_on_every_separately_opened_connection(self):
        module = FakePyodbcModule((SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE))
        datasets = {
            "stock_master": {"mode": "full", "query": SYNTHETIC_QUERY},
            "stock_balance": {"mode": "snapshot", "query": SYNTHETIC_SECOND_QUERY},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with expected_sql_context_env(SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE), fake_pyodbc(module):
                manifest = extract.run_extraction(
                    sqlserver_config(Path(tmpdir), datasets=datasets),
                    business_date="2026-06-04",
                    now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
                )

        self.assertEqual(manifest["status"], "success")
        # Each dataset opens its own connection; a context proven on one never authorises
        # a business query on another.
        self.assertEqual(len(module.connections), 2)
        self.assertEqual(
            [connection.executed for connection in module.connections],
            [
                [extract.SQL_EXECUTION_CONTEXT_QUERY, SYNTHETIC_QUERY],
                [extract.SQL_EXECUTION_CONTEXT_QUERY, SYNTHETIC_SECOND_QUERY],
            ],
        )

    def test_unreadable_context_row_fails_closed(self):
        class EmptyContextCursor(FakeSqlCursor):
            def fetchone(self):
                return None

        class EmptyContextConnection(FakeSqlConnection):
            def cursor(self):
                return EmptyContextCursor(self)

        class EmptyContextModule(FakePyodbcModule):
            def connect(self, connection_string, autocommit=False):
                connection = EmptyContextConnection(self.context_row)
                self.connections.append(connection)
                return connection

        module = EmptyContextModule((SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE))

        with tempfile.TemporaryDirectory() as tmpdir:
            with expected_sql_context_env(SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE), fake_pyodbc(module):
                with self.assertRaises(extract.SqlExecutionContextError) as caught:
                    extract.run_extraction(
                        sqlserver_config(Path(tmpdir)),
                        business_date="2026-06-04",
                        now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
                    )

        self.assertEqual(module.connections[0].business_queries, [])
        self.assertIn("could not be verified", str(caught.exception))
        self.assertNoPrivateIdentity(str(caught.exception))

    def test_sample_source_is_unaffected_by_expected_context_requirement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = base_config(Path(tmpdir))
            config["source_connection"] = {"kind": "sample"}
            config["datasets"] = {"stock_master": {"mode": "full", "query": SYNTHETIC_QUERY}}
            config["sample_rows"] = {"stock_master": [{"ItemCode": "SYNTH-ITEM-1"}]}

            with expected_sql_context_env():
                manifest = extract.run_extraction(
                    config,
                    business_date="2026-06-04",
                    now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
                )

            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["row_counts"]["stock_master"], 1)

    def test_dry_run_needs_no_expected_context_values_and_never_connects(self):
        module = FakePyodbcModule((SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE))
        stdout = io.StringIO()

        with expected_sql_context_env(), fake_pyodbc(module):
            with contextlib.redirect_stdout(stdout):
                exit_code = extract.main(
                    [
                        "--config",
                        str(ROOT / "config" / "autocount_stock_extract.ac2_smoke.example.json"),
                        "--business-date",
                        "2026-06-04",
                        "--dry-run",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(module.connections, [])
        self.assertEqual(json.loads(stdout.getvalue())["business_date"], "2026-06-04")

    def test_cli_reports_generic_failure_when_expected_context_is_missing(self):
        module = FakePyodbcModule((SYNTHETIC_LOGIN, SYNTHETIC_USER, SYNTHETIC_DATABASE))
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "autocount_stock_extract.local.json"
            config_path.write_text(json.dumps(sqlserver_config(Path(tmpdir))), encoding="utf-8")

            with expected_sql_context_env(), fake_pyodbc(module):
                with contextlib.redirect_stdout(stdout):
                    exit_code = extract.main(
                        ["--config", str(config_path), "--business-date", "2026-06-04"]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("not configured", payload["error"])
        self.assertEqual(module.connections, [])
        self.assertNoPrivateIdentity(stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
