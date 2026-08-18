from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .errors import StateError
from .publication import FileInfo


SCHEMA_VERSION = 1
ALLOWED_STATUSES = {"SEEN", "ARCHIVED", "PRESENT_RECONCILED", "FAILED", "CONFLICT"}
REQUIRED_COLUMNS = {
    "filename_key",
    "portal_filename",
    "first_seen_at_utc",
    "last_seen_at_utc",
    "archived_at_utc",
    "byte_size",
    "sha256",
    "status",
    "last_error_class",
    "last_error_at_utc",
    "attempt_count",
    "completion_source",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class BillRecord:
    filename_key: str
    portal_filename: str
    first_seen_at_utc: str
    last_seen_at_utc: str
    archived_at_utc: str | None
    byte_size: int | None
    sha256: str | None
    status: str
    last_error_class: str | None
    last_error_at_utc: str | None
    attempt_count: int
    completion_source: str | None


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> "StateStore":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            self.connection.execute("PRAGMA foreign_keys=ON")
            self._initialize()
            return self
        except (OSError, sqlite3.Error, StateError) as exc:
            self.close()
            raise StateError("state database could not be opened") from exc

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def get(self, filename_key: str) -> BillRecord | None:
        row = self._execute(
            "SELECT filename_key, portal_filename, first_seen_at_utc, last_seen_at_utc, "
            "archived_at_utc, byte_size, sha256, status, last_error_class, last_error_at_utc, "
            "attempt_count, completion_source FROM bills WHERE filename_key = ?",
            (filename_key,),
        ).fetchone()
        return _record_from_row(row) if row else None

    def mark_seen(self, filename_key: str, portal_filename: str, now: str | None = None) -> None:
        timestamp = now or utc_now()
        self._transaction(
            "INSERT INTO bills (filename_key, portal_filename, first_seen_at_utc, last_seen_at_utc, status, attempt_count) "
            "VALUES (?, ?, ?, ?, 'SEEN', 0) "
            "ON CONFLICT(filename_key) DO UPDATE SET portal_filename=excluded.portal_filename, last_seen_at_utc=excluded.last_seen_at_utc",
            (filename_key, portal_filename, timestamp, timestamp),
        )

    def record_archived(
        self,
        filename_key: str,
        portal_filename: str,
        info: FileInfo,
        completion_source: str = "downloaded",
        now: str | None = None,
    ) -> None:
        if completion_source not in {"downloaded", "preexisting"}:
            raise StateError("invalid completion source")
        timestamp = now or utc_now()
        self._transaction(
            "INSERT INTO bills (filename_key, portal_filename, first_seen_at_utc, last_seen_at_utc, archived_at_utc, "
            "byte_size, sha256, status, attempt_count, completion_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(filename_key) DO UPDATE SET portal_filename=excluded.portal_filename, "
            "last_seen_at_utc=excluded.last_seen_at_utc, archived_at_utc=excluded.archived_at_utc, "
            "byte_size=excluded.byte_size, sha256=excluded.sha256, status=excluded.status, "
            "last_error_class=NULL, last_error_at_utc=NULL, completion_source=excluded.completion_source",
            (
                filename_key,
                portal_filename,
                timestamp,
                timestamp,
                timestamp,
                info.byte_size,
                info.sha256,
                "ARCHIVED" if completion_source == "downloaded" else "PRESENT_RECONCILED",
                0,
                completion_source,
            ),
        )

    def record_failure(
        self,
        filename_key: str,
        portal_filename: str,
        error_class: str,
        status: str = "FAILED",
        now: str | None = None,
    ) -> None:
        if status not in ALLOWED_STATUSES:
            raise StateError("invalid state status")
        timestamp = now or utc_now()
        self._transaction(
            "INSERT INTO bills (filename_key, portal_filename, first_seen_at_utc, last_seen_at_utc, status, "
            "last_error_class, last_error_at_utc, attempt_count) VALUES (?, ?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(filename_key) DO UPDATE SET portal_filename=excluded.portal_filename, "
            "last_seen_at_utc=excluded.last_seen_at_utc, status=excluded.status, "
            "last_error_class=excluded.last_error_class, last_error_at_utc=excluded.last_error_at_utc, "
            "attempt_count=bills.attempt_count + 1",
            (filename_key, portal_filename, timestamp, timestamp, status, error_class, timestamp),
        )

    def records(self) -> Iterator[BillRecord]:
        rows = self._execute(
            "SELECT filename_key, portal_filename, first_seen_at_utc, last_seen_at_utc, archived_at_utc, "
            "byte_size, sha256, status, last_error_class, last_error_at_utc, attempt_count, completion_source "
            "FROM bills ORDER BY filename_key"
        ).fetchall()
        for row in rows:
            yield _record_from_row(row)

    def _initialize(self) -> None:
        connection = self._require_connection()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise StateError("state database schema is newer than this program")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS bills ("
                "filename_key TEXT PRIMARY KEY,"
                "portal_filename TEXT NOT NULL,"
                "first_seen_at_utc TEXT NOT NULL,"
                "last_seen_at_utc TEXT NOT NULL,"
                "archived_at_utc TEXT,"
                "byte_size INTEGER,"
                "sha256 TEXT,"
                "status TEXT NOT NULL,"
                "last_error_class TEXT,"
                "last_error_at_utc TEXT,"
                "attempt_count INTEGER NOT NULL DEFAULT 0,"
                "completion_source TEXT"
                ")"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(bills)").fetchall()}
            if not REQUIRED_COLUMNS.issubset(columns):
                raise StateError("state database schema is incompatible")
            if version == 0:
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def _transaction(self, statement: str, parameters: tuple = ()) -> None:
        connection = self._require_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(statement, parameters)
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise StateError("state update failed") from exc

    def _execute(self, statement: str, parameters: tuple = ()) -> sqlite3.Cursor:
        return self._require_connection().execute(statement, parameters)

    def _require_connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise StateError("state database is not open")
        return self.connection


def _record_from_row(row: tuple) -> BillRecord:
    return BillRecord(*row)
