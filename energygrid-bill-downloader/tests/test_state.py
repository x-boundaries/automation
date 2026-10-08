from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import uuid

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

    def test_apply_migration_rejects_corrupt_database_without_changing_bytes(self) -> None:
        from energygrid_bill_downloader.state import migrate_state_database

        self.path.parent.mkdir(parents=True)
        original = b"corrupt migration database canary"
        self.path.write_bytes(original)
        with self.assertRaises(StateError):
            migrate_state_database(self.path, apply=True)
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual([self.path], list(self.path.parent.iterdir()))

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


class V2StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "private-state" / "bills.sqlite3"

    def test_daily_v2_open_refuses_missing_and_v1_without_mutation(self) -> None:
        from energygrid_bill_downloader.state import StateV2Store

        with self.assertRaises(StateError):
            with StateV2Store(self.path, read_only=True):
                self.fail("missing v2 state was accepted")
        self.assertFalse(self.path.exists())
        create_state_fixture(self.path)
        before = state_snapshot(self.root)
        with self.assertRaises(StateError):
            with StateV2Store(self.path, read_only=True):
                self.fail("legacy v1 state was accepted as v2")
        self.assertEqual(before, state_snapshot(self.root))

    def test_migration_default_returns_plan_without_changing_legacy_state(self) -> None:
        from energygrid_bill_downloader.state import migrate_state_database

        create_state_fixture(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("invoice-1.pdf", "invoice-1.pdf", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", None, None, None, "SEEN", None, None, 0, None),
                )
        before = state_snapshot(self.root)
        result = migrate_state_database(self.path)
        self.assertEqual({"status": "PLAN_READY", "legacy_rows": 1}, result)
        self.assertEqual(before, state_snapshot(self.root))
        self.assertFalse(list(self.path.parent.glob("*.bak")))

    def test_apply_preserves_v1_rows_and_makes_imported_invoice_unclassified(self) -> None:
        from energygrid_bill_downloader.state import StateV2Store, migrate_state_database
        from fixtures.synthetic_dual_stream import test_stream_entries
        from energygrid_bill_downloader.invoice import Stream

        create_state_fixture(self.path)
        sha = "a" * 64
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("legacy.pdf", "legacy.pdf", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", None, 123, sha, "SEEN", None, None, 1, None),
                )
        result = migrate_state_database(self.path, apply=True, streams=test_stream_entries(bound=(Stream.EB_BILL,)))
        self.assertEqual({"status": "MIGRATED", "legacy_rows": 1}, result)
        backups = list(self.path.parent.glob("bills.sqlite3.v1-*.bak"))
        self.assertEqual(1, len(backups))
        with StateV2Store(self.path, read_only=True) as state:
            row = state.connection.execute(
                "SELECT classification,legacy_filename_key,legacy_path,archive_state,migration_state,drive_state,byte_size,sha256 FROM energygrid_invoice_v2"
            ).fetchone()
            self.assertEqual(("UNCLASSIFIED", "legacy.pdf", "legacy.pdf", "UNVERIFIED", "UNCLASSIFIED", "NOT_STAGED", 123, sha), row)
            self.assertEqual("UNBOUND", state.stream("TENANT_BILL")["admission"])
            self.assertIsNone(state.delivery_for_invoice(state.connection.execute("SELECT invoice_id FROM energygrid_invoice_v2").fetchone()[0]))
        with closing(sqlite3.connect(backups[0])) as backup:
            with backup:
                self.assertEqual((1,), backup.execute("PRAGMA user_version").fetchone())
                self.assertEqual(("legacy.pdf", 123, sha), backup.execute("SELECT filename_key,byte_size,sha256 FROM bills").fetchone())

    def test_write_once_watermark_cannot_regress_or_be_cleared(self) -> None:
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_dual_stream import candidate, create_v2_database
        from energygrid_bill_downloader.invoice import Stream

        create_v2_database(self.path)
        with StateV2Store(self.path) as state:
            invoice_id = state.accept_latest(candidate(Stream.EB_BILL), "00000000-0000-0000-0000-000000000001", "2026-10-02T00:00:00+00:00")
            with self.assertRaises(StateError):
                with state.transaction() as connection:
                    connection.execute("UPDATE energygrid_stream_v2 SET watermark_day=NULL WHERE stream='EB_BILL'")
            with self.assertRaises(StateError):
                with state.transaction() as connection:
                    connection.execute("UPDATE energygrid_stream_v2 SET watermark_day=1 WHERE stream='EB_BILL'")
            self.assertEqual(invoice_id, state.stream("EB_BILL")["watermark_invoice_id"])

    def test_classified_migration_retains_imported_invoice_identity(self) -> None:
        import uuid
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.state import StateV2Store, _stable_invoice_id, migrate_state_database
        from fixtures.synthetic_dual_stream import candidate, test_stream_entries

        create_state_fixture(self.path)
        filename = "eb-imported-2026-10-01.pdf"
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (filename, filename, "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00", None, None, None, "SEEN", None, None, 0, None),
                )
        streams = test_stream_entries(bound=(Stream.EB_BILL,))
        mapping = [{
            "legacy_filename_key": filename,
            "stream": Stream.EB_BILL.value,
            "source_namespace": streams[Stream.EB_BILL.value].source_namespace,
            "source_filename": filename,
            "raw_date": "2026-10-01",
            "date_profile": streams[Stream.EB_BILL.value].date_profile,
            "evidence_ref": streams[Stream.EB_BILL.value].evidence_ref,
        }]
        result = migrate_state_database(self.path, apply=True, streams=streams, mapping_entries=mapping)
        self.assertEqual("MIGRATED", result["status"])
        item = candidate(Stream.EB_BILL, name=filename, invoice_date="2026-10-01")
        with StateV2Store(self.path) as state:
            retained = state.resolve_latest_candidate(item)
            self.assertIsNotNone(retained)
            assert retained is not None
            retained_id = retained["invoice_id"]
            self.assertNotEqual(_stable_invoice_id(item.source_namespace, item.stream.value, item.source_invoice_key), retained_id)
            invoice = state.invoice(retained_id)
            info = FileInfo(123, "a" * 64)
            archive = state.start_file_operation(
                operation_id=str(uuid.uuid4()), invoice_id=retained_id, kind="ARCHIVE_PUBLISH",
                private_path_ref="C:/synthetic/archive-source", target_relpath=invoice["archive_relpath"],
                binding_id=None, info=info, source_role="SOURCE_ACQUISITION",
                run_id="00000000-0000-0000-0000-000000000001", timestamp="2026-10-02T00:00:01+00:00",
            )
            state.complete_file_operation(archive["operation_id"], "2026-10-02T00:00:02+00:00", evidence_ref="EG_ARCHIVE_TEST")
            state.update_invoice_file_state(
                retained_id, archive_state="COMMITTED", byte_size=info.byte_size,
                sha256=info.sha256, archived_at_utc="2026-10-02T00:00:02+00:00",
            )
            drive = state.start_file_operation(
                operation_id=str(uuid.uuid4()), invoice_id=retained_id, kind="DRIVE_STAGE",
                private_path_ref="C:/synthetic/drive-stage", target_relpath=invoice["archive_relpath"],
                binding_id="SYNTHETIC_DRIVE_BINDING", info=info, source_role="ARCHIVE_COMMITTED",
                run_id="00000000-0000-0000-0000-000000000001", timestamp="2026-10-02T00:00:03+00:00",
            )
            state.complete_file_operation(drive["operation_id"], "2026-10-02T00:00:04+00:00", evidence_ref="EG_DRIVE_TEST")
            state.update_invoice_file_state(
                retained_id, drive_state="DRIVE_STAGED", drive_binding_id="SYNTHETIC_DRIVE_BINDING",
                drive_relpath=invoice["archive_relpath"], drive_size=info.byte_size,
                drive_sha256=info.sha256, drive_staged_at_utc="2026-10-02T00:00:04+00:00",
            )
            metadata = {
                "schema": "energygrid.invoice_delivery.v1", "stream": item.stream.value,
                "bill_date": item.invoice_date.isoformat(), "attachment_name": item.canonical_filename,
                "pdf_sha256": info.sha256, "pdf_byte_size": info.byte_size,
            }
            delivery_id = "egmail-v1-" + "2" * 32
            delivery, created = state.prepare_delivery(
                invoice_id=retained_id, metadata=metadata, run_id="00000000-0000-0000-0000-000000000001",
                timestamp="2026-10-02T00:00:05+00:00", delivery_id=delivery_id,
            )
            self.assertTrue(created)
            self.assertTrue(state.claim_delivery_dispatch(delivery_id, "00000000-0000-0000-0000-000000000001", "2026-10-02T00:00:06+00:00"))
            self.assertTrue(state.record_delivery_outcome(
                delivery_id, "00000000-0000-0000-0000-000000000001", "2026-10-02T00:00:07+00:00",
                state="DELIVERED", evidence="VALIDATED_N8N_RESULT", support_ref="EG_TEST_ACCEPTED",
                accepted_at_utc="2026-10-02T00:00:07+00:00",
            ))
            invoice_before = state.invoice(retained_id)
            delivery_before = state.delivery(delivery_id)
            operations_before = state.file_operation_history(retained_id, "ARCHIVE_PUBLISH", None, invoice["archive_relpath"])
            operations_before += state.file_operation_history(retained_id, "DRIVE_STAGE", "SYNTHETIC_DRIVE_BINDING", invoice["archive_relpath"])
            provenance_before = state.legacy_archive_provenance(retained_id)
            accepted_id = state.accept_latest(item, "00000000-0000-0000-0000-000000000002", "2026-10-03T00:00:00+00:00")
            self.assertEqual(retained_id, accepted_id)
            self.assertEqual(retained_id, state.invoice_by_identity(item.source_namespace, item.stream.value, item.source_invoice_key)["invoice_id"])
            self.assertEqual(invoice_before, state.invoice(retained_id))
            self.assertEqual(delivery_before, state.delivery(delivery_id))
            self.assertEqual(operations_before, (
                state.file_operation_history(retained_id, "ARCHIVE_PUBLISH", None, invoice["archive_relpath"])
                + state.file_operation_history(retained_id, "DRIVE_STAGE", "SYNTHETIC_DRIVE_BINDING", invoice["archive_relpath"])
            ))
            self.assertEqual(provenance_before, state.legacy_archive_provenance(retained_id))
        with StateV2Store(self.path) as state:
            self.assertEqual("COMPLETE_V2", state.validation_status)
            self.assertEqual(retained_id, state.stream("EB_BILL")["watermark_invoice_id"])
        before_bytes = self.path.read_bytes()
        with StateV2Store(self.path, read_only=True) as state:
            before_business = (
                tuple(state.connection.execute("SELECT invoice_id,legacy_filename_key,classification,stream,source_invoice_key,archive_state,drive_state FROM energygrid_invoice_v2")),
                tuple(state.connection.execute("SELECT delivery_id,invoice_id,state,dispatch_started_at_utc,accepted_at_utc FROM energygrid_delivery_v1")),
                tuple(state.connection.execute("SELECT operation_id,kind,state,committed_at_utc FROM energygrid_file_operation_v2 ORDER BY operation_id")),
                tuple(state.connection.execute("SELECT stream,watermark_day,watermark_invoice_id FROM energygrid_stream_v2 ORDER BY stream")),
            )
        repeated = migrate_state_database(self.path, apply=True, streams=streams, mapping_entries=mapping)
        self.assertEqual({"status": "ALREADY_V2", "legacy_rows": 0}, repeated)
        self.assertEqual(before_bytes, self.path.read_bytes())
        with StateV2Store(self.path, read_only=True) as state:
            after_business = (
                tuple(state.connection.execute("SELECT invoice_id,legacy_filename_key,classification,stream,source_invoice_key,archive_state,drive_state FROM energygrid_invoice_v2")),
                tuple(state.connection.execute("SELECT delivery_id,invoice_id,state,dispatch_started_at_utc,accepted_at_utc FROM energygrid_delivery_v1")),
                tuple(state.connection.execute("SELECT operation_id,kind,state,committed_at_utc FROM energygrid_file_operation_v2 ORDER BY operation_id")),
                tuple(state.connection.execute("SELECT stream,watermark_day,watermark_invoice_id FROM energygrid_stream_v2 ORDER BY stream")),
            )
        self.assertEqual(before_business, after_business)

    def test_four_row_v1_mapping_replay_preserves_rows_and_retained_ids(self) -> None:
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.state import StateV2Store, migrate_state_database
        from fixtures.synthetic_dual_stream import test_stream_entries

        create_state_fixture(self.path)
        source_rows = (
            ("eb-older.pdf", Stream.EB_BILL, "2026-09-01"),
            ("eb-latest.pdf", Stream.EB_BILL, "2026-10-01"),
            ("tenant-older.pdf", Stream.TENANT_BILL, "2026-09-02"),
            ("tenant-latest.pdf", Stream.TENANT_BILL, "2026-10-02"),
        )
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                for name, _stream, date in source_rows:
                    connection.execute(
                        "INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (name, name, f"{date}T00:00:00+00:00", f"{date}T00:00:01+00:00", None, None, None, "SEEN", None, None, 0, None),
                    )
                original_bills = tuple(connection.execute("SELECT * FROM bills ORDER BY filename_key"))
        streams = test_stream_entries()
        mapping = [{
            "legacy_filename_key": name,
            "stream": stream.value,
            "source_namespace": streams[stream.value].source_namespace,
            "source_filename": name,
            "raw_date": date,
            "date_profile": streams[stream.value].date_profile,
            "evidence_ref": streams[stream.value].evidence_ref,
        } for name, stream, date in source_rows]
        self.assertEqual({"status": "MIGRATED", "legacy_rows": 4}, migrate_state_database(
            self.path, apply=True, streams=streams, mapping_entries=mapping,
        ))
        with StateV2Store(self.path, read_only=True) as state:
            rows = tuple(state.connection.execute(
                "SELECT invoice_id,legacy_filename_key,classification,stream,source_invoice_key FROM energygrid_invoice_v2 ORDER BY legacy_filename_key",
            ))
            self.assertEqual(4, len(rows))
            self.assertTrue(all(row[2] == "CLASSIFIED" for row in rows))
            rows_by_key = {row[1]: row[0] for row in rows}
            self.assertEqual({"eb-latest.pdf": rows_by_key["eb-latest.pdf"], "tenant-latest.pdf": rows_by_key["tenant-latest.pdf"]}, {
                "eb-latest.pdf": state.stream("EB_BILL")["watermark_invoice_id"],
                "tenant-latest.pdf": state.stream("TENANT_BILL")["watermark_invoice_id"],
            })
            before_ids = tuple(row[0] for row in rows)
            self.assertEqual(original_bills, tuple(state.connection.execute("SELECT * FROM bills ORDER BY filename_key")))
        before_bytes = self.path.read_bytes()
        self.assertEqual({"status": "RESUMABLE_V2", "legacy_rows": 0}, migrate_state_database(
            self.path, apply=True, streams=streams, mapping_entries=mapping,
        ))
        self.assertEqual(before_bytes, self.path.read_bytes())
        with StateV2Store(self.path, read_only=True) as state:
            after_ids = tuple(row[0] for row in state.connection.execute(
                "SELECT invoice_id FROM energygrid_invoice_v2 ORDER BY legacy_filename_key",
            ))
            self.assertEqual(before_ids, after_ids)
            self.assertEqual(4, state.connection.execute("SELECT COUNT(*) FROM energygrid_invoice_v2").fetchone()[0])

    def test_failed_mapping_apply_rolls_back_then_valid_mapping_resumes(self) -> None:
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.state import StateV2Store, migrate_state_database
        from fixtures.synthetic_dual_stream import test_stream_entries

        create_state_fixture(self.path)
        names = ("eb-resume.pdf", "tenant-resume.pdf")
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                for name in names:
                    connection.execute(
                        "INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (name, name, "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:01+00:00", None, None, None, "SEEN", None, None, 0, None),
                    )
        streams = test_stream_entries()
        mappings = [{
            "legacy_filename_key": name, "stream": stream.value,
            "source_namespace": streams[stream.value].source_namespace, "source_filename": name,
            "raw_date": "2026-10-01", "date_profile": streams[stream.value].date_profile,
            "evidence_ref": streams[stream.value].evidence_ref,
        } for name, stream in zip(names, (Stream.EB_BILL, Stream.TENANT_BILL), strict=True)]
        bad_mapping = [*mappings, {**mappings[0], "legacy_filename_key": "missing.pdf", "source_filename": "missing.pdf"}]
        original = self.path.read_bytes()
        with self.assertRaises(StateError):
            migrate_state_database(self.path, apply=True, streams=streams, mapping_entries=bad_mapping)
        self.assertEqual(original, self.path.read_bytes())
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual((1,), connection.execute("PRAGMA user_version").fetchone())
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM bills").fetchone()[0])
            self.assertEqual(0, len(connection.execute("SELECT name FROM sqlite_schema WHERE name='energygrid_invoice_v2'").fetchall()))
        result = migrate_state_database(self.path, apply=True, streams=streams, mapping_entries=mappings)
        self.assertEqual({"status": "MIGRATED", "legacy_rows": 2}, result)
        with StateV2Store(self.path, read_only=True) as state:
            self.assertEqual(2, state.connection.execute("SELECT COUNT(*) FROM energygrid_invoice_v2 WHERE classification='CLASSIFIED'").fetchone()[0])
            self.assertEqual(2, state.connection.execute("SELECT COUNT(DISTINCT invoice_id) FROM energygrid_invoice_v2").fetchone()[0])

    def test_version_two_incomplete_noop_and_changed_contracts_fail_without_repair(self) -> None:
        from energygrid_bill_downloader.state import StateV2Store, V2_SCHEMA_SQL, migrate_state_database
        from fixtures.synthetic_dual_stream import create_v2_database

        def rewrite_schema(path: Path, name: str, old: str, new: str) -> None:
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA writable_schema=ON")
                sql = connection.execute("SELECT sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()[0]
                self.assertIn(old, sql, (name, old))
                connection.execute("UPDATE sqlite_schema SET sql=? WHERE name=?", (sql.replace(old, new, 1), name))
                connection.execute("PRAGMA schema_version=83")
                connection.execute("PRAGMA writable_schema=OFF")
                connection.commit()

        defects = ("missing_table", "missing_column", "missing_index", "missing_trigger", "noop_guard", "changed_check", "changed_foreign_key")
        for defect in defects:
            with self.subTest(defect=defect):
                path = self.root / f"invalid-{defect}.sqlite3"
                create_v2_database(path)
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("PRAGMA foreign_keys=ON")
                    if defect == "missing_table":
                        connection.execute("DROP TABLE energygrid_delivery_v1")
                    elif defect == "missing_index":
                        connection.execute("DROP INDEX energygrid_invoice_identity_v2")
                    elif defect == "missing_trigger":
                        connection.execute("DROP TRIGGER energygrid_invoice_archive_commit_guard_v2")
                    elif defect == "noop_guard":
                        connection.execute("DROP TRIGGER energygrid_invoice_no_delete_v2")
                        connection.execute("CREATE TRIGGER energygrid_invoice_no_delete_v2 BEFORE DELETE ON energygrid_invoice_v2 BEGIN SELECT 1; END")
                if defect == "missing_column":
                    rewrite_schema(
                        path, "energygrid_invoice_v2",
                        "source_namespace TEXT, source_invoice_key TEXT, source_filename TEXT, raw_date TEXT,",
                        "source_namespace TEXT, source_invoice_key TEXT, raw_date TEXT,",
                    )
                elif defect == "changed_check":
                    rewrite_schema(
                        path, "energygrid_invoice_v2",
                        "CHECK(classification IN ('UNCLASSIFIED','CLASSIFIED'))",
                        "CHECK(classification IN ('UNCLASSIFIED','CLASSIFIED','EXTRA'))",
                    )
                elif defect == "changed_foreign_key":
                    rewrite_schema(
                        path, "energygrid_invoice_v2",
                        "legacy_filename_key TEXT UNIQUE REFERENCES bills(filename_key)",
                        "legacy_filename_key TEXT UNIQUE REFERENCES bills(filename_key) ON DELETE CASCADE",
                    )
                before = path.read_bytes()
                with self.assertRaises(StateError):
                    with StateV2Store(path):
                        self.fail(f"invalid v2 contract was accepted: {defect}")
                with self.assertRaises(StateError):
                    migrate_state_database(path, apply=True)
                self.assertEqual(before, path.read_bytes())

    def test_conflicting_mapping_and_foreign_key_failure_are_not_admitted(self) -> None:
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.state import StateV2Store, V2_SCHEMA_SQL, migrate_state_database
        from fixtures.synthetic_dual_stream import create_v2_database, test_stream_entries

        create_state_fixture(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("mapping.pdf", "mapping.pdf", "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:01+00:00", None, None, None, "SEEN", None, None, 0, None),
                )
        streams = test_stream_entries()
        mapping = {
            "legacy_filename_key": "mapping.pdf", "stream": Stream.EB_BILL.value,
            "source_namespace": streams[Stream.EB_BILL.value].source_namespace, "source_filename": "mapping.pdf",
            "raw_date": "2026-10-01", "date_profile": streams[Stream.EB_BILL.value].date_profile,
            "evidence_ref": streams[Stream.EB_BILL.value].evidence_ref,
        }
        before = self.path.read_bytes()
        with self.assertRaises(StateError):
            migrate_state_database(self.path, apply=True, streams=streams, mapping_entries=[mapping, dict(mapping)])
        self.assertEqual(before, self.path.read_bytes())

        v2 = self.root / "foreign-key.sqlite3"
        create_v2_database(v2)
        delivery_guard = next(statement for statement in V2_SCHEMA_SQL if statement.startswith("CREATE TRIGGER energygrid_delivery_invoice_guard_v1"))
        with closing(sqlite3.connect(v2, isolation_level=None)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP TRIGGER energygrid_delivery_invoice_guard_v1")
            connection.execute(
                "INSERT INTO energygrid_delivery_v1 (delivery_id,invoice_id,schema,stream,bill_date,attachment_name,sha256,byte_size,state,intent_run_id,intent_at_utc) VALUES (?,?,?,?,?,?,?,?,'PENDING_SEND',?,?)",
                ("egmail-v1-" + "9" * 32, "missing-invoice", "energygrid.invoice_delivery.v1", "EB_BILL", "2026-10-01", "2026-10-01.pdf", "a" * 64, 123, "00000000-0000-0000-0000-000000000001", "2026-10-01T00:00:00+00:00"),
            )
            connection.execute(delivery_guard)
        with self.assertRaises(StateError):
            with StateV2Store(v2):
                self.fail("v2 foreign-key violation was admitted")
        with self.assertRaises(StateError):
            migrate_state_database(v2, apply=True)

    def test_integrity_check_failure_fails_closed_without_changing_database_bytes(self) -> None:
        from energygrid_bill_downloader.state import StateV2Store, migrate_state_database
        from fixtures.synthetic_dual_stream import create_v2_database

        create_v2_database(self.path)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "INSERT INTO energygrid_invoice_v2 "
                "(invoice_id,classification,archive_state,migration_state,drive_state,created_at_utc,created_run_id) "
                "VALUES ('integrity-canary','INVALID','UNVERIFIED','UNCLASSIFIED','NOT_STAGED',?,?)",
                ("2026-10-04T00:00:00+00:00", "00000000-0000-0000-0000-000000000001"),
            )
            connection.execute("PRAGMA ignore_check_constraints=OFF")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertIn("CHECK constraint failed", integrity)

        before = self.path.read_bytes()
        with self.assertRaises(StateError):
            migrate_state_database(self.path, apply=True)
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(StateError):
            with StateV2Store(self.path, read_only=True):
                self.fail("v2 state with a failed integrity check was admitted")
        self.assertEqual(before, self.path.read_bytes())

    def test_shared_v2_validator_distinguishes_complete_resumable_and_invalid_schemas(self) -> None:
        from energygrid_bill_downloader.state import (
            StateV2Store,
            _V2_PREDECESSOR_ARCHIVE_GUARD_SQL,
            _inspect_v2_database,
            migrate_state_database,
        )
        from energygrid_bill_downloader.invoice import Stream
        from fixtures.synthetic_dual_stream import test_stream_entries

        def status(path: Path) -> str:
            with closing(sqlite3.connect(path, isolation_level=None)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=FULL")
                return _inspect_v2_database(connection)

        complete = self.root / "complete.sqlite3"
        from fixtures.synthetic_dual_stream import create_v2_database
        create_v2_database(complete)
        self.assertEqual("COMPLETE_V2", status(complete))

        resumable = self.root / "resumable.sqlite3"
        create_state_fixture(resumable)
        with closing(sqlite3.connect(resumable)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("unclassified.pdf", "unclassified.pdf", "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00", None, None, None, "SEEN", None, None, 0, None),
                )
        migrate_state_database(resumable, apply=True, streams=test_stream_entries(bound=(Stream.EB_BILL,)))
        self.assertEqual("RESUMABLE_V2", status(resumable))

        predecessor = self.root / "predecessor.sqlite3"
        create_v2_database(predecessor)
        with closing(sqlite3.connect(predecessor)) as connection:
            with connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("DROP TRIGGER energygrid_invoice_archive_commit_guard_v2")
                connection.execute(_V2_PREDECESSOR_ARCHIVE_GUARD_SQL)
        self.assertEqual("RECOGNIZED_PREDECESSOR", status(predecessor))
        self.assertEqual("UPGRADE_REQUIRED", migrate_state_database(predecessor)["status"])
        self.assertEqual("UPGRADED_V2", migrate_state_database(predecessor, apply=True)["status"])
        with StateV2Store(predecessor) as state:
            self.assertEqual("COMPLETE_V2", state.validation_status)

        incomplete = self.root / "incomplete.sqlite3"
        create_v2_database(incomplete)
        with closing(sqlite3.connect(incomplete)) as connection:
            with connection:
                connection.execute("DROP INDEX energygrid_invoice_identity_v2")
        self.assertEqual("INCOMPLETE_V2", status(incomplete))
        with self.assertRaises(StateError):
            with StateV2Store(incomplete):
                self.fail("incomplete v2 was accepted")

        incompatible = self.root / "incompatible.sqlite3"
        create_v2_database(incompatible)
        with closing(sqlite3.connect(incompatible)) as connection:
            with connection:
                connection.execute("CREATE TABLE unexpected_extra (id INTEGER)")
        self.assertEqual("INCOMPATIBLE_V2", status(incompatible))
        with self.assertRaises(StateError):
            with StateV2Store(incompatible):
                self.fail("incompatible v2 was accepted")

        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a SQLite database")
        with closing(sqlite3.connect(corrupt)) as connection:
            self.assertEqual("CORRUPT_OR_UNREADABLE_V2", _inspect_v2_database(connection))

    def test_semantic_validator_rejects_private_delivery_support_reference(self) -> None:
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_dual_stream import candidate, create_v2_database
        import uuid

        create_v2_database(self.path)
        item = candidate()
        info = FileInfo(123, "a" * 64)
        run_id = "00000000-0000-0000-0000-000000000001"
        with StateV2Store(self.path) as state:
            invoice_id = state.accept_latest(item, run_id, "2026-10-02T00:00:00+00:00")
            invoice = state.invoice(invoice_id)
            archive = state.start_file_operation(
                operation_id=str(uuid.uuid4()), invoice_id=invoice_id, kind="ARCHIVE_PUBLISH",
                private_path_ref="C:/synthetic/archive.tmp", target_relpath=invoice["archive_relpath"],
                binding_id=None, info=info, source_role="SOURCE_ACQUISITION", run_id=run_id,
                timestamp="2026-10-02T00:00:01+00:00",
            )
            state.complete_file_operation(archive["operation_id"], "2026-10-02T00:00:02+00:00", evidence_ref="EG_ARCHIVE_TEST")
            state.update_invoice_file_state(invoice_id, archive_state="COMMITTED", byte_size=info.byte_size, sha256=info.sha256, archived_at_utc="2026-10-02T00:00:02+00:00")
            drive = state.start_file_operation(
                operation_id=str(uuid.uuid4()), invoice_id=invoice_id, kind="DRIVE_STAGE",
                private_path_ref="C:/synthetic/drive.tmp", target_relpath=invoice["archive_relpath"],
                binding_id="SYNTHETIC_DRIVE_BINDING", info=info, source_role="ARCHIVE_COMMITTED",
                run_id=run_id, timestamp="2026-10-02T00:00:03+00:00",
            )
            state.complete_file_operation(drive["operation_id"], "2026-10-02T00:00:04+00:00", evidence_ref="EG_DRIVE_TEST")
            state.update_invoice_file_state(
                invoice_id, drive_state="DRIVE_STAGED", drive_binding_id="SYNTHETIC_DRIVE_BINDING",
                drive_relpath=invoice["archive_relpath"], drive_size=info.byte_size,
                drive_sha256=info.sha256, drive_staged_at_utc="2026-10-02T00:00:04+00:00",
            )
            with state.transaction() as connection:
                connection.execute(
                    "INSERT INTO energygrid_delivery_v1 (delivery_id,invoice_id,schema,stream,bill_date,attachment_name,sha256,byte_size,state,intent_run_id,intent_at_utc,dispatch_run_id,dispatch_started_at_utc,outcome_run_id,outcome_at_utc,accepted_at_utc,support_ref,evidence) VALUES (?,?,?,?,?,?,?,?,'DELIVERED',?,?,?,?,?,?,?,'PRIVATE-ACCOUNT-8831','VALIDATED_N8N_RESULT')",
                    ("egmail-v1-" + "1" * 32, invoice_id, "energygrid.invoice_delivery.v1", "EB_BILL", invoice["bill_date"], invoice["canonical_filename"], info.sha256, info.byte_size, run_id, "2026-10-02T00:00:05+00:00", run_id, "2026-10-02T00:00:06+00:00", run_id, "2026-10-02T00:00:07+00:00", "2026-10-02T00:00:07+00:00"),
                )
        with self.assertRaises(StateError):
            with StateV2Store(self.path):
                self.fail("private delivery support reference was accepted")


class StateV3MigrationTests(unittest.TestCase):
    """#226 G3 schema v3: v1->v3 and v2->v3 plan/apply, frozen historical
    DRIVE_STAGED facts, and the Drive receipt triggers."""

    RUN = "00000000-0000-4000-8000-0000000000d1"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def v1_database(self, name: str = "eb-latest.pdf", invoice_date: str = "2026-10-01") -> tuple[Path, bytes]:
        from fixtures.synthetic_delivery import synthetic_pdf

        path = self.root / "v1" / "state.sqlite3"
        path.parent.mkdir()
        payload = synthetic_pdf(name.encode())
        digest = hashlib.sha256(payload).hexdigest()
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "CREATE TABLE bills (filename_key TEXT PRIMARY KEY,portal_filename TEXT NOT NULL,first_seen_at_utc TEXT NOT NULL,last_seen_at_utc TEXT NOT NULL,archived_at_utc TEXT,byte_size INTEGER,sha256 TEXT,status TEXT NOT NULL,last_error_class TEXT,last_error_at_utc TEXT,attempt_count INTEGER NOT NULL DEFAULT 0,completion_source TEXT)"
            )
            connection.execute("PRAGMA user_version=1")
            connection.execute("INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                               (name, name, "t", "t", "t", len(payload), digest, "ARCHIVED", None, None, 1, "downloaded"))
            connection.commit()
        return path, payload

    def mapping(self, name: str = "eb-latest.pdf", invoice_date: str = "2026-10-01") -> list[dict]:
        from fixtures.synthetic_dual_stream import test_stream_entries

        entry = test_stream_entries()["EB_BILL"]
        return [{"legacy_filename_key": name, "stream": "EB_BILL", "source_namespace": entry.source_namespace,
                 "source_filename": name, "raw_date": invoice_date, "date_profile": entry.date_profile,
                 "evidence_ref": entry.evidence_ref}]

    def test_v1_to_v3_plan_is_read_only_and_apply_backs_up_classifies_and_validates(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store, migrate_state_database_v3
        from fixtures.synthetic_dual_stream import test_stream_entries

        path, _payload = self.v1_database()
        before = path.read_bytes()
        plan = migrate_state_database_v3(path, streams=test_stream_entries(), mapping_entries=self.mapping())
        self.assertEqual({"status": "PLAN_READY_V3", "legacy_rows": 1}, plan)
        self.assertEqual(before, path.read_bytes())
        result = migrate_state_database_v3(path, apply=True, streams=test_stream_entries(), mapping_entries=self.mapping(),
                                           migration_run_id=self.RUN)
        self.assertEqual({"status": "MIGRATED_V3", "legacy_rows": 1}, result)
        backups = list(path.parent.glob("state.sqlite3.v1-*.bak"))
        self.assertEqual(1, len(backups))
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(1, backup.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(["eb-latest.pdf"], [row[0] for row in backup.execute("SELECT filename_key FROM bills")])
        with StateV3Store(path, read_only=True) as state:
            self.assertEqual("RESUMABLE_V3", state.validation_status)
            self.assertEqual(3, state._conn().execute("PRAGMA user_version").fetchone()[0])
            invoice = state.invoice(state.stream("EB_BILL")["watermark_invoice_id"])
            self.assertEqual(("CLASSIFIED", "NOT_STAGED"), (invoice["classification"], invoice["drive_state"]))
            self.assertEqual([], state.drive_operations_for_invoice(invoice["invoice_id"]))
        self.assertEqual({"status": "RESUMABLE_V3", "legacy_rows": 0}, migrate_state_database_v3(path, apply=True))

    def test_failed_v3_inspection_rolls_back_every_change(self) -> None:
        from energygrid_bill_downloader import state as state_module
        from fixtures.synthetic_dual_stream import test_stream_entries

        path, _payload = self.v1_database()
        before = path.read_bytes()
        with mock.patch.object(state_module, "_inspect_v3_database", return_value="INCOMPATIBLE_V3"):
            with self.assertRaises(StateError):
                state_module.migrate_state_database_v3(path, apply=True, streams=test_stream_entries(),
                                                       mapping_entries=self.mapping(), migration_run_id=self.RUN)
        self.assertEqual(before, path.read_bytes())

    def staged_v2_database(self, *, drive_stage_open: bool = False) -> tuple[Path, str]:
        """A v2 database whose latest invoice is historically DRIVE_STAGED and DELIVERED."""
        from fixtures.synthetic_delivery import synthetic_pdf
        from fixtures.synthetic_dual_stream import candidate, create_v2_database
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.publication import FileInfo
        from energygrid_bill_downloader.state import StateV2Store

        path = self.root / "v2" / "state.sqlite3"
        path.parent.mkdir()
        create_v2_database(path)
        payload = synthetic_pdf(b"historical staged")
        info = FileInfo(len(payload), hashlib.sha256(payload).hexdigest())
        with StateV2Store(path) as state:
            invoice_id = state.accept_latest(candidate(Stream.EB_BILL, name="eb-latest.pdf"), self.RUN, "t")
            invoice = state.invoice(invoice_id)
            op = state.start_file_operation(operation_id=str(uuid.uuid4()), invoice_id=invoice_id, kind="ARCHIVE_PUBLISH",
                                            private_path_ref="x", target_relpath=invoice["archive_relpath"], binding_id=None,
                                            info=info, source_role="SOURCE_ACQUISITION", run_id=self.RUN, timestamp="t")
            state.complete_file_operation(op["operation_id"], "t", evidence_ref="EG_ARCHIVE_HASH_VERIFIED")
            state.update_invoice_file_state(invoice_id, archive_state="COMMITTED", byte_size=info.byte_size, sha256=info.sha256, archived_at_utc="t")
            stage = state.start_file_operation(operation_id=str(uuid.uuid4()), invoice_id=invoice_id, kind="DRIVE_STAGE",
                                               private_path_ref="y", target_relpath=invoice["archive_relpath"], binding_id="LEGACY_BINDING",
                                               info=info, source_role="ARCHIVE_COMMITTED", run_id=self.RUN, timestamp="t")
            if drive_stage_open:
                return path, invoice_id
            state.complete_file_operation(stage["operation_id"], "t", evidence_ref="EG_DRIVE_STAGE_HASH_VERIFIED")
            state.update_invoice_file_state(invoice_id, drive_state="DRIVE_STAGED", drive_binding_id="LEGACY_BINDING",
                                            drive_relpath=invoice["archive_relpath"], drive_size=info.byte_size,
                                            drive_sha256=info.sha256, drive_staged_at_utc="t")
            metadata = {"schema": "energygrid.invoice_delivery.v1", "stream": "EB_BILL", "bill_date": invoice["bill_date"],
                        "attachment_name": invoice["canonical_filename"], "pdf_byte_size": info.byte_size, "pdf_sha256": info.sha256}
            delivery_id = "egmail-v1-" + "1" * 32
            state.prepare_delivery(invoice_id=invoice_id, metadata=metadata, run_id=self.RUN, timestamp="t", delivery_id=delivery_id)
            state.claim_delivery_dispatch(delivery_id, self.RUN, "t")
            state.record_delivery_outcome(delivery_id, self.RUN, "t", state="DELIVERED", evidence="VALIDATED_N8N_RESULT",
                                          support_ref="EG_MAIL_ACCEPTED", accepted_at_utc="t")
        return path, invoice_id

    def test_v2_to_v3_preserves_and_freezes_historical_drive_staged_without_fabricating_a_receipt(self) -> None:
        from energygrid_bill_downloader.state import StateV2Store, StateV3Store, migrate_state_database_v3

        path, invoice_id = self.staged_v2_database()
        before = path.read_bytes()
        self.assertEqual({"status": "PLAN_UPGRADE_V3", "legacy_rows": 0}, migrate_state_database_v3(path))
        self.assertEqual(before, path.read_bytes())
        self.assertEqual({"status": "UPGRADED_V3", "legacy_rows": 0}, migrate_state_database_v3(path, apply=True))
        self.assertEqual(1, len(list(path.parent.glob("state.sqlite3.v2-*.bak"))))
        with self.assertRaises(StateError):
            with StateV2Store(path):
                pass
        with StateV3Store(path) as state:
            invoice = state.invoice(invoice_id)
            self.assertEqual(("DRIVE_STAGED", "LEGACY_BINDING"), (invoice["drive_state"], invoice["drive_binding_id"]))
            self.assertEqual([], state.drive_operations_for_invoice(invoice_id), "no fabricated v3 receipt")
            self.assertEqual("DELIVERED", state.delivery_for_invoice(invoice_id)["state"], "delivered stays delivered")
            connection = state._conn()
            for statement in (
                "UPDATE energygrid_invoice_v2 SET drive_state='NOT_STAGED' WHERE invoice_id=?",
                "UPDATE energygrid_invoice_v2 SET drive_binding_id='OTHER' WHERE invoice_id=?",
            ):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(statement, (invoice_id,))
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "INSERT INTO energygrid_file_operation_v2 (operation_id,invoice_id,kind,state,private_path_ref,target_relpath,binding_id,expected_size,expected_sha256,source_role,run_id,prepared_at_utc) "
                    "VALUES ('x',?,'DRIVE_STAGE','PREPARED','p','EB Bill/2026-10-01.pdf','B',1,?,'ARCHIVE_COMMITTED',?,'t')",
                    (invoice_id, "0" * 64, self.RUN),
                )

    def test_open_drive_stage_operation_holds_v3_migration_without_change(self) -> None:
        from energygrid_bill_downloader.state import StreamStateConflictError, migrate_state_database_v3

        path, _ = self.staged_v2_database(drive_stage_open=True)
        before = path.read_bytes()
        self.assertEqual({"status": "HOLD", "support_ref": "EG_V3_MIGRATION_DRIVE_STAGE_OPEN", "legacy_rows": 0},
                         migrate_state_database_v3(path))
        with self.assertRaises(StreamStateConflictError):
            migrate_state_database_v3(path, apply=True)
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(list(path.parent.glob("*.bak")))

    def test_fresh_v3_and_daily_open_never_create_or_migrate(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store, migrate_state_database_v3

        missing = self.root / "fresh" / "state.sqlite3"
        with self.assertRaises(StateError):
            with StateV3Store(missing):
                pass
        self.assertFalse(missing.exists())
        self.assertEqual({"status": "PLAN_CREATE_V3", "legacy_rows": 0}, migrate_state_database_v3(missing))
        self.assertEqual({"status": "CREATED_V3", "legacy_rows": 0}, migrate_state_database_v3(missing, apply=True))
        self.assertEqual({"status": "ALREADY_V3", "legacy_rows": 0}, migrate_state_database_v3(missing, apply=True))
        v1, _ = self.v1_database()
        with self.assertRaises(StateError):
            with StateV3Store(v1):
                pass

    def test_drift_from_the_reference_v3_schema_is_not_opened(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store, migrate_state_database_v3

        path = self.root / "drift" / "state.sqlite3"
        migrate_state_database_v3(path, apply=True)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("DROP TRIGGER energygrid_delivery_invoice_guard_v3")
            connection.commit()
        with self.assertRaises(StateError):
            with StateV3Store(path):
                pass


class StateV3TriggerTests(unittest.TestCase):
    RUN = "00000000-0000-4000-8000-0000000000e1"

    def setUp(self) -> None:
        from fixtures.synthetic_dual_stream import bind_synthetic_drive, create_v3_database

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        create_v3_database(self.path)
        bind_synthetic_drive(self.path)

    def committed_latest(self, state, name="eb-latest.pdf", invoice_date="2026-10-01") -> dict:
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.publication import FileInfo
        from fixtures.synthetic_dual_stream import candidate

        info = FileInfo(1234, hashlib.sha256(name.encode()).hexdigest())
        invoice_id = state.accept_latest(candidate(Stream.EB_BILL, name=name, invoice_date=invoice_date), self.RUN, "t")
        invoice = state.invoice(invoice_id)
        op = state.start_file_operation(operation_id=str(uuid.uuid4()), invoice_id=invoice_id, kind="ARCHIVE_PUBLISH",
                                        private_path_ref="x", target_relpath=invoice["archive_relpath"], binding_id=None,
                                        info=info, source_role="SOURCE_ACQUISITION", run_id=self.RUN, timestamp="t")
        state.complete_file_operation(op["operation_id"], "t", evidence_ref="EG_ARCHIVE_HASH_VERIFIED")
        state.update_invoice_file_state(invoice_id, archive_state="COMMITTED", byte_size=info.byte_size, sha256=info.sha256, archived_at_utc="t")
        return state.invoice(invoice_id)

    def intent(self, state, invoice) -> dict:
        row, created = state.create_drive_intent(invoice=invoice, binding=state.active_binding("EB_BILL"), local_md5="a" * 32,
                                                 operation_id=str(uuid.uuid4()), run_id=self.RUN, timestamp="t")
        self.assertTrue(created)
        return row

    def test_intent_only_for_the_current_latest_and_the_active_binding(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store

        with StateV3Store(self.path) as state:
            older = self.committed_latest(state)
            self.committed_latest(state, name="eb-newer.pdf", invoice_date="2026-10-05")
            with self.assertRaises(StateError):
                self.intent(state, older)
            binding = dict(state.active_binding("EB_BILL"), folder_id="synthOtherFolder00001")
            with self.assertRaises(StateError):
                state.create_drive_intent(invoice=state.invoice(state.stream("EB_BILL")["watermark_invoice_id"]), binding=binding,
                                          local_md5="a" * 32, operation_id=str(uuid.uuid4()), run_id=self.RUN, timestamp="t")

    def test_identity_app_properties_and_terminal_states_are_frozen_and_rows_retained(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store

        with StateV3Store(self.path) as state:
            row = self.intent(state, self.committed_latest(state))
            connection = state._conn()
            for statement, parameters in (
                ("UPDATE energygrid_drive_operation_v3 SET local_sha256=? WHERE operation_id=?", ("0" * 64, row["operation_id"])),
                ("UPDATE energygrid_drive_operation_v3 SET app_properties_json=? WHERE operation_id=?", ("{}", row["operation_id"])),
                ("UPDATE energygrid_drive_operation_v3 SET upload_attempt_count=1, reserved_remote_file_id='synthBypass000000001', "
                 "reserved_run_id='r', reserved_at_utc='t' WHERE operation_id=?", (row["operation_id"],)),
                ("UPDATE energygrid_drive_operation_v3 SET state='DRIVE_VERIFIED' WHERE operation_id=?", (row["operation_id"],)),
            ):
                with self.subTest(statement=statement[:60]), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(statement, parameters)
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM energygrid_drive_operation_v3")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM energygrid_drive_binding_v3")
            state.finish_drive_operation(row["operation_id"], run_id=self.RUN, timestamp="t", new_state="DRIVE_CONFLICT",
                                         support_ref="EG_DRIVE_NAME_OCCUPIED")
            with self.assertRaises(StateError):
                state.finish_drive_operation(row["operation_id"], run_id=self.RUN, timestamp="t", new_state="HOLD",
                                             support_ref="EG_DRIVE_HOLD")

    def test_verified_requires_dispatch_reserved_id_and_matching_receipt(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store

        receipt = {"remote_file_id": "synthReservedFile00001", "remote_parent_id": "synthEbBillFolder00001",
                   "remote_name_observed": "2026-10-01.pdf", "remote_mime_type": "application/pdf", "remote_size": 1234,
                   "remote_sha256": None, "remote_md5": "a" * 32, "accepted_app_properties_json": None,
                   "verification_method": "MD5_SIZE", "receipt_sha256": "b" * 64}
        with StateV3Store(self.path) as state:
            row = self.intent(state, self.committed_latest(state))
            receipt["remote_sha256"] = None
            receipt["accepted_app_properties_json"] = row["app_properties_json"]
            # No reservation and no dispatch: VERIFIED is impossible.
            with self.assertRaises(StateError):
                state.finish_drive_operation(row["operation_id"], run_id=self.RUN, timestamp="t", new_state="DRIVE_VERIFIED", receipt=receipt)
            state.reserve_drive_file_id(row["operation_id"], "synthReservedFile00001", self.RUN, "t")
            state.begin_drive_dispatch(row["operation_id"], self.RUN, "t")
            # Open dispatch marker: still impossible.
            with self.assertRaises(StateError):
                state.finish_drive_operation(row["operation_id"], run_id=self.RUN, timestamp="t", new_state="DRIVE_VERIFIED", receipt=receipt)
            for key, value in (("remote_file_id", "synthDifferentFile001"), ("remote_size", 1233), ("remote_md5", "c" * 32),
                               ("remote_parent_id", "synthOtherFolder00001")):
                with self.subTest(key=key), self.assertRaises(StateError):
                    state.finish_drive_operation(row["operation_id"], run_id=self.RUN, timestamp="t", new_state="DRIVE_VERIFIED",
                                                 receipt={**receipt, key: value}, dispatch_outcome="VALID_RESULT")
            verified = state.finish_drive_operation(row["operation_id"], run_id=self.RUN, timestamp="t", new_state="DRIVE_VERIFIED",
                                                    receipt=receipt, dispatch_outcome="VALID_RESULT")
            self.assertEqual(verified["reserved_remote_file_id"], verified["remote_file_id"])
            with self.assertRaises(sqlite3.DatabaseError):
                state._conn().execute("UPDATE energygrid_drive_operation_v3 SET verified_at_utc='x' WHERE operation_id=?", (row["operation_id"],))
            # The binding cannot be retired while... VERIFIED is final, retirement is allowed now.
            state.retire_binding(state.active_binding("EB_BILL")["binding_id"], "t")

    def test_binding_cannot_retire_with_open_operation_and_run_rows_are_insert_only(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store

        with StateV3Store(self.path) as state:
            self.intent(state, self.committed_latest(state))
            with self.assertRaises(StateError):
                state.retire_binding(state.active_binding("EB_BILL")["binding_id"], "t")
            state.insert_run_record(run_id=self.RUN, started_at_utc="t", acquire_state="COMPLETED", acquire_exit_code=0,
                                    acquire_support_ref=None, streams={"EB_BILL": ("READY", None)}, timestamp="t")
            with self.assertRaises(StateError):
                state.insert_run_record(run_id=self.RUN, started_at_utc="t", acquire_state="COMPLETED", acquire_exit_code=0,
                                        acquire_support_ref=None, streams={}, timestamp="t")
            for statement in ("UPDATE energygrid_run_v3 SET acquire_state='FAILED'", "DELETE FROM energygrid_run_v3"):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                    state._conn().execute(statement)


class StateV3RunStreamStopTests(StateV3MigrationTests):
    """#226 G2 fairness reclosure R12: the append-only per-run stream stop table
    is part of the unreleased v3 contract (user_version stays 3)."""

    STOP_OBJECTS = (
        ("table", "energygrid_run_stream_stop_v3"),
        ("trigger", "energygrid_run_stream_stop_no_update_v3"),
        ("trigger", "energygrid_run_stream_stop_no_delete_v3"),
    )

    def assert_stop_objects(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
            present = set(connection.execute("SELECT type,name FROM sqlite_master"))
            self.assertEqual(3, connection.execute("PRAGMA user_version").fetchone()[0])
        for item in self.STOP_OBJECTS:
            self.assertIn(item, present)

    def fresh(self, name: str = "fresh") -> Path:
        from fixtures.synthetic_dual_stream import create_v3_database

        path = self.root / name / "state.sqlite3"
        path.parent.mkdir()
        create_v3_database(path)
        return path

    def record_run(self, state, run_id: str = StateV3MigrationTests.RUN) -> None:
        state.insert_run_record(run_id=run_id, started_at_utc="t", acquire_state="COMPLETED", acquire_exit_code=0,
                                acquire_support_ref=None, streams={"EB_BILL": ("EMPTY", None), "TENANT_BILL": ("EMPTY", None)},
                                timestamp="t")

    def test_create_and_every_migration_path_carry_the_stop_table(self) -> None:
        from energygrid_bill_downloader.state import V3_TABLES, migrate_state_database_v3
        from fixtures.synthetic_dual_stream import test_stream_entries

        self.assertIn("energygrid_run_stream_stop_v3", V3_TABLES)
        self.assert_stop_objects(self.fresh())
        v1, _payload = self.v1_database()
        migrate_state_database_v3(v1, apply=True, streams=test_stream_entries(), mapping_entries=self.mapping(),
                                  migration_run_id=self.RUN)
        self.assert_stop_objects(v1)
        v2, _invoice = self.staged_v2_database()
        migrate_state_database_v3(v2, apply=True)
        self.assert_stop_objects(v2)

    def test_pre_fix_v3_shape_without_the_table_is_incomplete_and_refused_without_repair(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store, _inspect_v3_database, migrate_state_database_v3

        path = self.fresh()
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("DROP TABLE energygrid_run_stream_stop_v3")  # also drops its two triggers
            connection.commit()
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            self.assertEqual("INCOMPLETE_V3", _inspect_v3_database(connection))
        before = path.read_bytes()
        for read_only in (True, False):
            with self.subTest(read_only=read_only), self.assertRaises(StateError):
                with StateV3Store(path, read_only=read_only):
                    pass
        with self.assertRaises(StateError):
            migrate_state_database_v3(path, apply=True)
        self.assertEqual(before, path.read_bytes(), "no in-place repair of a pre-fix v3 database")

    def test_insert_reads_back_and_applies_only_to_its_own_run(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store

        other = "00000000-0000-4000-8000-0000000000d2"
        with StateV3Store(self.fresh()) as state:
            self.record_run(state)
            self.record_run(state, other)
            self.assertEqual(frozenset(), state.run_stream_stops(self.RUN))
            state.insert_run_stream_stop(run_id=self.RUN, stream="EB_BILL", command="drive-reconcile",
                                         outcome="DRIVE_UPLOAD_UNCERTAIN", exit_code=20,
                                         support_ref="EG_DRIVE_RECONCILE_UNAVAILABLE", timestamp="t")
            self.assertEqual(frozenset({"EB_BILL"}), state.run_stream_stops(self.RUN))
            self.assertEqual(frozenset(), state.run_stream_stops(other), "a later run never sees an earlier stop")
            state.insert_run_stream_stop(run_id=self.RUN, stream="TENANT_BILL", command="drive-upload",
                                         outcome="RESERVATION_UNAVAILABLE", exit_code=10,
                                         support_ref=None, timestamp="t")
            self.assertEqual(frozenset({"EB_BILL", "TENANT_BILL"}), state.run_stream_stops(self.RUN))

    def test_duplicate_update_delete_foreign_key_and_check_refusals(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store

        valid = {"run_id": self.RUN, "stream": "EB_BILL", "command": "deliver", "outcome": "HOLD", "exit_code": 20,
                 "support_ref": "EG_DELIVERY_PREPARATION_FAILED", "timestamp": "t"}
        with StateV3Store(self.fresh()) as state:
            with self.assertRaises(StateError, msg="foreign key: unknown run"):
                state.insert_run_stream_stop(**valid)
            self.record_run(state)
            state.insert_run_stream_stop(**valid)
            with self.assertRaises(StateError, msg="one stop row per run and stream"):
                state.insert_run_stream_stop(**valid)
            for change in ({"command": "drive-intent", "outcome": "DRIVE_CONFLICT"}, {"exit_code": 10},
                           {"outcome": "DELIVERED", "exit_code": 0}, {"command": "acquire", "outcome": "ACQUIRE_FAILED"},
                           {"stream": "OTHER"}, {"run_id": "not-a-run"}, {"support_ref": "private value"},
                           {"exit_code": True}):
                with self.subTest(change=change), self.assertRaises(StateError):
                    state.insert_run_stream_stop(**{**valid, "stream": "TENANT_BILL", **change})
            connection = state._conn()
            for statement in ("UPDATE energygrid_run_stream_stop_v3 SET outcome='REQUEST_REJECTED'",
                              "DELETE FROM energygrid_run_stream_stop_v3"):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(statement)
            insert = ("INSERT INTO energygrid_run_stream_stop_v3 (run_id,stream,command,outcome,exit_code,support_ref,stopped_at_utc) "
                      "VALUES (?,?,?,?,?,?,'t')")
            for row in (
                (self.RUN, "OTHER", "deliver", "HOLD", 20, None),
                (self.RUN, "TENANT_BILL", "acquire", "HOLD", 20, None),
                (self.RUN, "TENANT_BILL", "deliver", "DELIVERED", 20, None),
                (self.RUN, "TENANT_BILL", "deliver", "HOLD", 64, None),
                (self.RUN, "TENANT_BILL", "deliver", "HOLD", 20.5, None),
                (self.RUN, "TENANT_BILL", "deliver", "HOLD", 20, "EG_lower"),
                (self.RUN, "TENANT_BILL", "deliver", "HOLD", 20, "XX_REF"),
            ):
                with self.subTest(row=row), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(insert, row)
            # Every refusal above was a CHECK/type refusal, not the primary key.
            self.assertEqual(frozenset({"EB_BILL"}), state.run_stream_stops(self.RUN))

    def test_check_valid_but_unclassified_tuple_is_incompatible(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store, _inspect_v3_database

        path = self.fresh()
        with StateV3Store(path) as state:
            self.record_run(state)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            # Passes every CHECK, but drive-intent never stops a stream with DRIVE_CONFLICT.
            connection.execute("INSERT INTO energygrid_run_stream_stop_v3 VALUES (?,'EB_BILL','drive-intent','DRIVE_CONFLICT',20,NULL,'t')",
                               (self.RUN,))
            connection.commit()
            connection.execute("PRAGMA synchronous=FULL")
            self.assertEqual("INCOMPATIBLE_V3", _inspect_v3_database(connection))
        with self.assertRaises(StateError):
            with StateV3Store(path, read_only=True):
                pass

    def test_stop_rows_never_mark_the_database_resumable(self) -> None:
        from energygrid_bill_downloader.state import StateV3Store

        path = self.fresh()
        with StateV3Store(path) as state:
            self.assertEqual("COMPLETE_V3", state.validation_status)
            self.record_run(state)
            state.insert_run_stream_stop(run_id=self.RUN, stream="EB_BILL", command="drive-upload", outcome="HOLD",
                                         exit_code=20, support_ref="EG_DRIVE_AUTH_UNAVAILABLE", timestamp="t")
        with StateV3Store(path, read_only=True) as state:
            self.assertEqual("COMPLETE_V3", state.validation_status)


if __name__ == "__main__":
    unittest.main()
