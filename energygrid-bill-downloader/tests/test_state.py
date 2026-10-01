from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from energygrid_bill_downloader.errors import StateError
from energygrid_bill_downloader.publication import FileInfo
from energygrid_bill_downloader.state import StateStore


# Independent fixture schema: list tests never initialize or inspect state
# through the writable production StateStore.
FIXTURE_SCHEMA = """
CREATE TABLE bills (
 filename_key TEXT PRIMARY KEY, portal_filename TEXT NOT NULL,
 first_seen_at_utc TEXT NOT NULL, last_seen_at_utc TEXT NOT NULL,
 archived_at_utc TEXT, byte_size INTEGER, sha256 TEXT, status TEXT NOT NULL,
 last_error_class TEXT, last_error_at_utc TEXT,
 attempt_count INTEGER NOT NULL DEFAULT 0, completion_source TEXT
);
"""


def create_state_fixture(path: Path, *, version: int = 1, schema: str = FIXTURE_SCHEMA) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(schema)
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()
    finally:
        connection.close()


def state_snapshot(directory: Path, *, metadata_only: frozenset[str] = frozenset()) -> dict:
    if not directory.exists():
        return {}
    return {
        path.relative_to(directory).as_posix(): (
            path.is_dir(), path.stat().st_mtime_ns,
            None if path.is_dir() else path.stat().st_size
            if path.relative_to(directory).as_posix() in metadata_only else path.read_bytes(),
        )
        for path in [directory, *sorted(directory.rglob("*"))]
    }


class ReadOnlyStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # URI metacharacters which are valid even on Windows.
        self.path = self.root / "state # % unicode-\u00e9" / "bills # %.sqlite3"

    def check_open(self, *, accepted: bool) -> None:
        before = state_snapshot(self.root)
        real_connect = sqlite3.connect
        connections = []

        def connect(database, **kwargs):
            self.assertEqual(self.path.resolve().as_uri() + "?mode=ro", database)
            self.assertTrue(kwargs["uri"])
            self.assertNotIn("immutable", database)
            connection = real_connect(database, **kwargs)
            connections.append(connection)
            return connection

        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")) as mkdir, \
                mock.patch.object(StateStore, "_initialize", side_effect=AssertionError("initialize")) as initialize, \
                mock.patch("energygrid_bill_downloader.state.sqlite3.connect", side_effect=connect):
            if accepted:
                with StateStore(self.path, read_only=True) as state:
                    self.assertEqual([], list(state.records()))
                    self.assertIsNone(state.get("missing"))
                    self.assertEqual((1,), state.connection.execute("PRAGMA query_only").fetchone())
                    self.assertEqual((2,), state.connection.execute("PRAGMA temp_store").fetchone())
                    self.assertEqual((1,), state.connection.execute("PRAGMA user_version").fetchone())
            else:
                with self.assertRaises(StateError):
                    with StateStore(self.path, read_only=True):
                        self.fail("incompatible state admitted")
            mkdir.assert_not_called()
            initialize.assert_not_called()
        self.assertEqual(before, state_snapshot(self.root))
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_current_schema_and_encoded_uri(self) -> None:
        create_state_fixture(self.path)
        self.check_open(accepted=True)

    def test_missing_parent_and_database_create_nothing(self) -> None:
        self.check_open(accepted=False)
        self.path.parent.mkdir()
        self.check_open(accepted=False)

    def test_zero_old_newer_missing_view_virtual_and_wrong_column_schemas(self) -> None:
        cases = [
            ("version-zero", 0, FIXTURE_SCHEMA),
            ("newer", 2, FIXTURE_SCHEMA),
            ("missing", 1, ""),
            ("view", 1, "CREATE VIEW bills AS SELECT 1 AS filename_key;"),
            ("virtual", 1, "CREATE VIRTUAL TABLE bills USING fts5(filename_key);"),
            ("missing-columns", 1, "CREATE TABLE bills (filename_key TEXT PRIMARY KEY);"),
            ("wrong-type", 1, FIXTURE_SCHEMA.replace("byte_size INTEGER", "byte_size BLOB")),
            ("missing-primary-key", 1, FIXTURE_SCHEMA.replace("TEXT PRIMARY KEY", "TEXT")),
        ]
        for label, version, schema in cases:
            with self.subTest(label=label):
                self.path = self.root / label / "state.sqlite3"
                create_state_fixture(self.path, version=version, schema=schema)
                self.check_open(accepted=False)

    def test_corrupt_wal_and_every_sidecar_fail_before_sqlite_open(self) -> None:
        for label in ("corrupt", "wal-format", "-wal", "-shm", "-journal", "directory"):
            with self.subTest(label=label):
                self.path = self.root / label / "state.sqlite3"
                create_state_fixture(self.path)
                if label == "corrupt":
                    self.path.write_bytes(b"not a database")
                elif label == "wal-format":
                    value = bytearray(self.path.read_bytes())
                    value[18:20] = b"\x02\x02"
                    self.path.write_bytes(value)
                elif label == "directory":
                    self.path = self.path.parent
                else:
                    self.path.with_name(self.path.name + label).write_bytes(b"canary")
                with mock.patch("energygrid_bill_downloader.state.sqlite3.connect") as connect:
                    self.check_open(accepted=False)
                    connect.assert_not_called()

    def test_sidecar_inspection_error_and_unreadable_db_fail_closed(self) -> None:
        create_state_fixture(self.path)
        real_stat = Path.lstat

        def stat(path, *args, **kwargs):
            if path.name.endswith("-shm"):
                raise PermissionError("synthetic private detail")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", stat):
            self.check_open(accepted=False)
        with mock.patch.object(Path, "open", side_effect=PermissionError("private")):
            with self.assertRaises(StateError):
                with StateStore(self.path, read_only=True):
                    self.fail("unreadable state admitted")

    def test_mutators_reject_before_any_transaction(self) -> None:
        create_state_fixture(self.path)
        before = state_snapshot(self.root)
        with StateStore(self.path, read_only=True) as state:
            statements = []
            state.connection.set_trace_callback(statements.append)
            for mutate in (
                lambda: state.mark_seen("key", "file.pdf"),
                lambda: state.record_archived("key", "file.pdf", FileInfo(3, "a" * 64)),
                lambda: state.record_failure("key", "file.pdf", "FAILED"),
                lambda: state._transaction("DELETE FROM bills"),
                state._initialize,
            ):
                with self.assertRaises(StateError):
                    mutate()
            self.assertEqual([], statements)
            with self.assertRaises(sqlite3.OperationalError):
                state.connection.execute("DELETE FROM bills")
        self.assertEqual(before, state_snapshot(self.root))


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state" / "bills.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_records_survive_reopen_and_keep_minimum_fields(self) -> None:
        info = FileInfo(byte_size=123, sha256="a" * 64)
        with StateStore(self.db_path) as state:
            state.mark_seen("invoice-key", "2026-05-01_account_ref.pdf", now="2026-08-17T00:00:00+00:00")
            state.record_archived(
                "invoice-key",
                "2026-05-01_account_ref.pdf",
                info,
                completion_source="downloaded",
                now="2026-08-17T00:01:00+00:00",
            )
            record = state.get("invoice-key")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, "ARCHIVED")
            self.assertEqual(record.byte_size, 123)
            self.assertEqual(record.sha256, "a" * 64)
            self.assertEqual(record.completion_source, "downloaded")
        with StateStore(self.db_path) as state:
            record = state.get("invoice-key")
            self.assertIsNotNone(record)
            self.assertEqual(len(list(state.records())), 1)

    def test_failure_and_conflict_statuses_are_truthful(self) -> None:
        with StateStore(self.db_path) as state:
            state.mark_seen("failed", "failed.pdf")
            state.record_failure("failed", "failed.pdf", "DOWNLOAD_FAILED")
            self.assertEqual(state.get("failed").status, "FAILED")  # type: ignore[union-attr]
            state.record_failure("failed", "failed.pdf", "ARCHIVE_CONFLICT", status="CONFLICT")
            record = state.get("failed")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, "CONFLICT")
            self.assertEqual(record.last_error_class, "ARCHIVE_CONFLICT")
            self.assertEqual(record.attempt_count, 2)

    def test_newer_schema_is_not_reset(self) -> None:
        import sqlite3

        self.db_path.parent.mkdir(parents=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("PRAGMA user_version=99")
        connection.close()
        with self.assertRaises(StateError):
            with StateStore(self.db_path):
                pass

    def test_writable_version_zero_initializes_and_migrates(self) -> None:
        for schema in ("", FIXTURE_SCHEMA):
            with self.subTest(existing_schema=bool(schema)):
                path = self.db_path.with_name("existing.sqlite3" if schema else "empty.sqlite3")
                create_state_fixture(path, version=0, schema=schema)
                with StateStore(path) as state:
                    self.assertEqual((1,), state.connection.execute("PRAGMA user_version").fetchone())
                    state.mark_seen("key", "file.pdf")
                    self.assertEqual("SEEN", state.get("key").status)


if __name__ == "__main__":
    unittest.main()
