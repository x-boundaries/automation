from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from energygrid_bill_downloader.errors import StateError
from energygrid_bill_downloader.publication import FileInfo
from energygrid_bill_downloader.state import StateStore


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


if __name__ == "__main__":
    unittest.main()
