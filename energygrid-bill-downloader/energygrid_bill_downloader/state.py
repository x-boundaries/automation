from __future__ import annotations

import sqlite3
import stat
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .errors import StateError
from .publication import FileInfo, filename_key


class StreamStateConflictError(StateError):
    """A durable fact conflict owned by one stream rather than shared state."""

    def __init__(self, stream: str, support_ref: str) -> None:
        super().__init__("stream state conflicts with the selected invoice")
        self.stream = stream
        self.support_ref = support_ref


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
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> "StateStore":
        try:
            if self.read_only:
                self._open_read_only()
                return self
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            self.connection.execute("PRAGMA foreign_keys=ON")
            self._initialize()
            return self
        except (OSError, sqlite3.Error, StateError) as exc:
            self.close()
            raise StateError("state database could not be opened") from exc

    def _open_read_only(self) -> None:
        if not stat.S_ISREG(self.path.lstat().st_mode):
            raise StateError("state database must be an existing regular file")
        # lstat distinguishes absence from an inspection error and also sees
        # dangling sidecar links. Neither WAL nor journal recovery is allowed.
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                self.path.with_name(self.path.name + suffix).lstat()
            except FileNotFoundError:
                continue
            raise StateError("state database has an operational sidecar")
        with self.path.open("rb") as stream:
            header = stream.read(100)
        if (
            len(header) != 100 or header[:16] != b"SQLite format 3\x00"
            or header[18:20] != b"\x01\x01"
        ):
            raise StateError("state database format is incompatible")
        # as_uri percent-encodes reserved characters in the *path*, before the
        # mode query is appended. immutable=1 would bypass SQLite's locking.
        uri = self.path.resolve().as_uri() + "?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
        connection = self.connection
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise StateError("state database schema is incompatible")
        tables = [row for row in connection.execute("PRAGMA table_list") if row[0:2] == ("main", "bills")]
        if len(tables) != 1 or tables[0][2] != "table":
            raise StateError("state database schema is incompatible")
        columns = {row[1]: row for row in connection.execute("PRAGMA main.table_xinfo(bills)")}
        if not REQUIRED_COLUMNS.issubset(columns):
            raise StateError("state database schema is incompatible")
        for name in REQUIRED_COLUMNS:
            column = columns[name]
            expected_type = "INTEGER" if name in {"byte_size", "attempt_count"} else "TEXT"
            if column[2].upper() != expected_type or column[6] != 0:
                raise StateError("state database schema is incompatible")
        if columns["filename_key"][5] != 1:
            raise StateError("state database schema is incompatible")

    def _require_writable(self) -> None:
        if self.read_only:
            raise StateError("state database is read-only")

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
        self._require_writable()
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
        self._require_writable()
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


# The legacy `bills` schema stays at v1 and remains available to bounded
# diagnostics. Daily dual-stream runs use this additive v2 store exclusively.
V2_SCHEMA_VERSION = 2
V2_BUSY_TIMEOUT_MS = 5000
V2_STREAMS = ("EB_BILL", "TENANT_BILL")
V2_TABLES = {
    "bills",
    "energygrid_invoice_v2",
    "energygrid_stream_v2",
    "energygrid_delivery_v1",
    "energygrid_file_operation_v2",
}
V2_COLUMN_TYPES = {
    "bills": {name: ("INTEGER" if name in {"byte_size", "attempt_count"} else "TEXT") for name in REQUIRED_COLUMNS},
    "energygrid_invoice_v2": {
        name: ("INTEGER" if name in {"day_ordinal", "byte_size", "drive_size"} else "TEXT")
        for name in (
            "invoice_id", "legacy_filename_key", "classification", "stream", "source_namespace",
            "source_invoice_key", "source_filename", "raw_date", "date_profile", "bill_date",
            "day_ordinal", "evidence_ref", "canonical_filename", "archive_relpath", "legacy_path",
            "archive_state", "byte_size", "sha256", "archived_at_utc", "migration_state",
            "drive_state", "drive_binding_id", "drive_relpath", "drive_size", "drive_sha256",
            "drive_staged_at_utc", "created_at_utc", "created_run_id", "last_seen_run_id",
        )
    },
    "energygrid_stream_v2": {
        name: ("INTEGER" if name == "watermark_day" else "TEXT")
        for name in (
            "stream", "source_namespace", "adapter_id", "date_profile", "evidence_ref", "admission",
            "watermark_day", "watermark_invoice_id", "watermark_run_id", "watermark_at_utc",
        )
    },
    "energygrid_delivery_v1": {
        name: ("INTEGER" if name == "byte_size" else "TEXT")
        for name in (
            "delivery_id", "invoice_id", "schema", "stream", "bill_date", "attachment_name", "sha256",
            "byte_size", "state", "intent_run_id", "intent_at_utc", "dispatch_run_id",
            "dispatch_started_at_utc", "outcome_run_id", "outcome_at_utc", "accepted_at_utc",
            "support_ref", "evidence",
        )
    },
    "energygrid_file_operation_v2": {
        name: ("INTEGER" if name == "expected_size" else "TEXT")
        for name in (
            "operation_id", "invoice_id", "kind", "state", "private_path_ref", "target_relpath",
            "binding_id", "expected_size", "expected_sha256", "source_role", "run_id",
            "prepared_at_utc", "committed_at_utc", "evidence_ref", "support_ref",
        )
    },
}

V2_SCHEMA_SQL = (
    "CREATE TABLE energygrid_invoice_v2 ("
    "invoice_id TEXT PRIMARY KEY,"
    "legacy_filename_key TEXT UNIQUE REFERENCES bills(filename_key),"
    "classification TEXT NOT NULL CHECK(classification IN ('UNCLASSIFIED','CLASSIFIED')),"
    "stream TEXT REFERENCES energygrid_stream_v2(stream),"
    "source_namespace TEXT, source_invoice_key TEXT, source_filename TEXT, raw_date TEXT,"
    "date_profile TEXT, bill_date TEXT, day_ordinal INTEGER, evidence_ref TEXT,"
    "canonical_filename TEXT, archive_relpath TEXT, legacy_path TEXT,"
    "archive_state TEXT NOT NULL CHECK(archive_state IN ('UNVERIFIED','ABSENT','PREPARED','COMMITTED','CONFLICT')),"
    "byte_size INTEGER, sha256 TEXT, archived_at_utc TEXT,"
    "migration_state TEXT NOT NULL CHECK(migration_state IN ('UNCLASSIFIED','CLASSIFIED','MOVE_PLANNED','COMPLETE','HOLD','NOT_REQUIRED')),"
    "drive_state TEXT NOT NULL CHECK(drive_state IN ('NOT_STAGED','PREPARED','DRIVE_STAGED','CONFLICT')),"
    "drive_binding_id TEXT, drive_relpath TEXT, drive_size INTEGER, drive_sha256 TEXT, drive_staged_at_utc TEXT,"
    "created_at_utc TEXT NOT NULL, created_run_id TEXT NOT NULL, last_seen_run_id TEXT,"
    "CHECK((classification='UNCLASSIFIED' AND stream IS NULL AND source_namespace IS NULL AND source_invoice_key IS NULL AND source_filename IS NULL AND raw_date IS NULL AND date_profile IS NULL AND bill_date IS NULL AND day_ordinal IS NULL AND evidence_ref IS NULL AND canonical_filename IS NULL AND archive_relpath IS NULL) OR (classification='CLASSIFIED' AND stream IS NOT NULL AND source_namespace IS NOT NULL AND source_invoice_key IS NOT NULL AND source_filename IS NOT NULL AND raw_date IS NOT NULL AND date_profile='INVOICE_DATE_ISO_V1' AND bill_date IS NOT NULL AND day_ordinal BETWEEN 1 AND 3652059 AND evidence_ref IS NOT NULL AND canonical_filename=bill_date||'.pdf' AND archive_relpath IS NOT NULL)),"
    "CHECK(classification!='CLASSIFIED' OR archive_relpath=(CASE stream WHEN 'EB_BILL' THEN 'EB Bill/' ELSE 'Tenant Bill/' END)||canonical_filename),"
    "CHECK(archive_state!='COMMITTED' OR (classification='CLASSIFIED' AND archive_relpath IS NOT NULL AND byte_size>0 AND length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*' AND archived_at_utc IS NOT NULL)),"
    "CHECK(drive_state!='DRIVE_STAGED' OR (classification='CLASSIFIED' AND drive_binding_id IS NOT NULL AND drive_relpath=archive_relpath AND drive_size=byte_size AND drive_sha256=sha256 AND drive_staged_at_utc IS NOT NULL)),"
    "CHECK(byte_size IS NULL OR (typeof(byte_size)='integer' AND byte_size>0)),"
    "CHECK(drive_size IS NULL OR (typeof(drive_size)='integer' AND drive_size>0))"
    ") STRICT",
    "CREATE TABLE energygrid_stream_v2 ("
    "stream TEXT PRIMARY KEY CHECK(stream IN ('EB_BILL','TENANT_BILL')),"
    "source_namespace TEXT, adapter_id TEXT, date_profile TEXT, evidence_ref TEXT,"
    "admission TEXT NOT NULL CHECK(admission IN ('UNBOUND','BOUND','HOLD')),"
    "watermark_day INTEGER, watermark_invoice_id TEXT REFERENCES energygrid_invoice_v2(invoice_id),"
    "watermark_run_id TEXT, watermark_at_utc TEXT,"
    "CHECK((admission='UNBOUND' AND source_namespace IS NULL AND adapter_id IS NULL AND date_profile IS NULL AND evidence_ref IS NULL) OR (admission='BOUND' AND source_namespace IS NOT NULL AND adapter_id='DIRECT_HTTP_V1' AND date_profile='INVOICE_DATE_ISO_V1' AND evidence_ref IS NOT NULL) OR admission='HOLD'),"
    "CHECK((watermark_day IS NULL AND watermark_invoice_id IS NULL AND watermark_run_id IS NULL AND watermark_at_utc IS NULL) OR (watermark_day BETWEEN 1 AND 3652059 AND watermark_invoice_id IS NOT NULL AND watermark_run_id IS NOT NULL AND watermark_at_utc IS NOT NULL))"
    ") STRICT",
    "CREATE TABLE energygrid_delivery_v1 ("
    "delivery_id TEXT PRIMARY KEY CHECK(length(delivery_id)=42 AND substr(delivery_id,1,10)='egmail-v1-' AND substr(delivery_id,11) NOT GLOB '*[^0-9a-f]*'),"
    "invoice_id TEXT NOT NULL UNIQUE REFERENCES energygrid_invoice_v2(invoice_id),"
    "schema TEXT NOT NULL CHECK(schema='energygrid.invoice_delivery.v1'),"
    "stream TEXT NOT NULL CHECK(stream IN ('EB_BILL','TENANT_BILL')), bill_date TEXT NOT NULL,"
    "attachment_name TEXT NOT NULL CHECK(attachment_name=bill_date||'.pdf'), sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'), byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size>0),"
    "state TEXT NOT NULL CHECK(state IN ('PENDING_SEND','DELIVERY_OUTCOME_UNCERTAIN','REQUEST_REJECTED','DELIVERED')),"
    "intent_run_id TEXT NOT NULL, intent_at_utc TEXT NOT NULL, dispatch_run_id TEXT, dispatch_started_at_utc TEXT,"
    "outcome_run_id TEXT, outcome_at_utc TEXT, accepted_at_utc TEXT, support_ref TEXT, evidence TEXT,"
    "CHECK((dispatch_started_at_utc IS NULL AND dispatch_run_id IS NULL) OR (dispatch_started_at_utc IS NOT NULL AND dispatch_run_id IS NOT NULL)),"
    "CHECK((state='PENDING_SEND' AND outcome_run_id IS NULL AND outcome_at_utc IS NULL AND accepted_at_utc IS NULL AND support_ref IS NULL AND evidence IS NULL) OR state!='PENDING_SEND'),"
    "CHECK(state!='DELIVERED' OR (dispatch_started_at_utc IS NOT NULL AND dispatch_run_id IS NOT NULL AND accepted_at_utc IS NOT NULL AND outcome_run_id IS NOT NULL AND outcome_at_utc IS NOT NULL AND support_ref IS NOT NULL AND evidence IS NOT NULL)),"
    "CHECK(state NOT IN ('DELIVERY_OUTCOME_UNCERTAIN','REQUEST_REJECTED') OR (dispatch_started_at_utc IS NOT NULL AND dispatch_run_id IS NOT NULL AND accepted_at_utc IS NULL AND outcome_run_id IS NOT NULL AND outcome_at_utc IS NOT NULL AND support_ref IS NOT NULL AND evidence IS NOT NULL))"
    ") STRICT",
    "CREATE TABLE energygrid_file_operation_v2 ("
    "operation_id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES energygrid_invoice_v2(invoice_id),"
    "kind TEXT NOT NULL CHECK(kind IN ('ARCHIVE_PUBLISH','LEGACY_MOVE','DRIVE_STAGE')),"
    "state TEXT NOT NULL CHECK(state IN ('PREPARED','COMMITTED','HOLD')),"
    "private_path_ref TEXT NOT NULL, target_relpath TEXT NOT NULL, binding_id TEXT,"
    "expected_size INTEGER NOT NULL CHECK(typeof(expected_size)='integer' AND expected_size>0),"
    "expected_sha256 TEXT NOT NULL CHECK(length(expected_sha256)=64 AND expected_sha256 NOT GLOB '*[^0-9a-f]*'), source_role TEXT NOT NULL,"
    "run_id TEXT NOT NULL, prepared_at_utc TEXT NOT NULL, committed_at_utc TEXT, evidence_ref TEXT, support_ref TEXT,"
    "CHECK((state='PREPARED' AND committed_at_utc IS NULL AND evidence_ref IS NULL AND support_ref IS NULL) OR (state='COMMITTED' AND committed_at_utc IS NOT NULL AND evidence_ref IS NOT NULL AND support_ref IS NULL) OR (state='HOLD' AND committed_at_utc IS NULL AND evidence_ref IS NULL AND support_ref IS NOT NULL))"
    ") STRICT",
    "CREATE UNIQUE INDEX energygrid_invoice_identity_v2 ON energygrid_invoice_v2(source_namespace,stream,source_invoice_key) WHERE classification='CLASSIFIED'",
    "CREATE UNIQUE INDEX energygrid_invoice_archive_relpath_v2 ON energygrid_invoice_v2(archive_relpath) WHERE classification='CLASSIFIED'",
    "CREATE UNIQUE INDEX energygrid_invoice_drive_path_v2 ON energygrid_invoice_v2(drive_binding_id,drive_relpath) WHERE drive_state='DRIVE_STAGED'",
    "CREATE UNIQUE INDEX energygrid_file_operation_prepared_v2 ON energygrid_file_operation_v2(invoice_id,kind,ifnull(binding_id,''),target_relpath) WHERE state IN ('PREPARED','HOLD')",
    "CREATE TRIGGER energygrid_stream_watermark_guard_v2 BEFORE UPDATE OF watermark_day,watermark_invoice_id,watermark_run_id,watermark_at_utc ON energygrid_stream_v2 BEGIN "
    "SELECT CASE WHEN OLD.watermark_day IS NOT NULL AND (NEW.watermark_day IS NULL OR NEW.watermark_day<OLD.watermark_day) THEN RAISE(ABORT,'watermark regression') END; "
    "SELECT CASE WHEN OLD.watermark_day=NEW.watermark_day AND OLD.watermark_invoice_id IS NOT NEW.watermark_invoice_id THEN RAISE(ABORT,'watermark identity conflict') END; "
    "SELECT CASE WHEN NEW.watermark_day IS NOT NULL AND NOT EXISTS (SELECT 1 FROM energygrid_invoice_v2 i WHERE i.invoice_id=NEW.watermark_invoice_id AND i.classification='CLASSIFIED' AND i.stream=NEW.stream AND i.source_namespace=NEW.source_namespace AND i.day_ordinal=NEW.watermark_day) THEN RAISE(ABORT,'watermark invoice mismatch') END; END",
    "CREATE TRIGGER energygrid_invoice_binding_insert_guard_v2 BEFORE INSERT ON energygrid_invoice_v2 WHEN NEW.classification='CLASSIFIED' AND NOT EXISTS (SELECT 1 FROM energygrid_stream_v2 s WHERE s.stream=NEW.stream AND s.admission='BOUND' AND s.source_namespace=NEW.source_namespace AND s.date_profile=NEW.date_profile AND s.evidence_ref=NEW.evidence_ref) BEGIN SELECT RAISE(ABORT,'invoice stream binding mismatch'); END",
    "CREATE TRIGGER energygrid_invoice_binding_update_guard_v2 BEFORE UPDATE OF classification,stream,source_namespace,date_profile,evidence_ref ON energygrid_invoice_v2 WHEN OLD.classification='UNCLASSIFIED' AND NEW.classification='CLASSIFIED' AND NOT EXISTS (SELECT 1 FROM energygrid_stream_v2 s WHERE s.stream=NEW.stream AND s.admission='BOUND' AND s.source_namespace=NEW.source_namespace AND s.date_profile=NEW.date_profile AND s.evidence_ref=NEW.evidence_ref) BEGIN SELECT RAISE(ABORT,'invoice stream binding mismatch'); END",
    "CREATE TRIGGER energygrid_invoice_no_delete_v2 BEFORE DELETE ON energygrid_invoice_v2 BEGIN SELECT RAISE(ABORT,'invoice rows are retained'); END",
    "CREATE TRIGGER energygrid_invoice_archive_commit_guard_v2 BEFORE UPDATE OF archive_state ON energygrid_invoice_v2 WHEN NEW.archive_state='COMMITTED' AND OLD.archive_state!='COMMITTED' AND NOT EXISTS (SELECT 1 FROM energygrid_file_operation_v2 o WHERE o.invoice_id=NEW.invoice_id AND o.kind IN ('ARCHIVE_PUBLISH','LEGACY_MOVE') AND o.state='COMMITTED' AND o.target_relpath=NEW.archive_relpath AND o.expected_size=NEW.byte_size AND o.expected_sha256=NEW.sha256) BEGIN SELECT RAISE(ABORT,'archive commit lacks matching journal'); END",
    "CREATE TRIGGER energygrid_invoice_drive_commit_guard_v2 BEFORE UPDATE OF drive_state ON energygrid_invoice_v2 WHEN NEW.drive_state='DRIVE_STAGED' AND OLD.drive_state!='DRIVE_STAGED' AND NOT EXISTS (SELECT 1 FROM energygrid_file_operation_v2 o WHERE o.invoice_id=NEW.invoice_id AND o.kind='DRIVE_STAGE' AND o.state='COMMITTED' AND o.binding_id=NEW.drive_binding_id AND o.target_relpath=NEW.drive_relpath AND o.expected_size=NEW.drive_size AND o.expected_sha256=NEW.drive_sha256) BEGIN SELECT RAISE(ABORT,'Drive stage lacks matching journal'); END",
    "CREATE TRIGGER energygrid_invoice_identity_freeze_v2 BEFORE UPDATE ON energygrid_invoice_v2 WHEN OLD.classification='CLASSIFIED' AND (NEW.classification IS NOT OLD.classification OR NEW.stream IS NOT OLD.stream OR NEW.source_namespace IS NOT OLD.source_namespace OR NEW.source_invoice_key IS NOT OLD.source_invoice_key OR NEW.source_filename IS NOT OLD.source_filename OR NEW.raw_date IS NOT OLD.raw_date OR NEW.date_profile IS NOT OLD.date_profile OR NEW.bill_date IS NOT OLD.bill_date OR NEW.day_ordinal IS NOT OLD.day_ordinal OR NEW.evidence_ref IS NOT OLD.evidence_ref OR NEW.canonical_filename IS NOT OLD.canonical_filename OR NEW.archive_relpath IS NOT OLD.archive_relpath) BEGIN SELECT RAISE(ABORT,'classified invoice identity is frozen'); END",
    "CREATE TRIGGER energygrid_invoice_archive_freeze_v2 BEFORE UPDATE OF archive_state,byte_size,sha256,archived_at_utc ON energygrid_invoice_v2 WHEN OLD.archive_state='COMMITTED' AND (NEW.archive_state IS NOT OLD.archive_state OR NEW.byte_size IS NOT OLD.byte_size OR NEW.sha256 IS NOT OLD.sha256 OR NEW.archived_at_utc IS NOT OLD.archived_at_utc) BEGIN SELECT RAISE(ABORT,'committed archive facts are frozen'); END",
    "CREATE TRIGGER energygrid_invoice_drive_freeze_v2 BEFORE UPDATE OF drive_state,drive_binding_id,drive_relpath,drive_size,drive_sha256,drive_staged_at_utc ON energygrid_invoice_v2 WHEN OLD.drive_state='DRIVE_STAGED' AND (NEW.drive_state IS NOT OLD.drive_state OR NEW.drive_binding_id IS NOT OLD.drive_binding_id OR NEW.drive_relpath IS NOT OLD.drive_relpath OR NEW.drive_size IS NOT OLD.drive_size OR NEW.drive_sha256 IS NOT OLD.drive_sha256 OR NEW.drive_staged_at_utc IS NOT OLD.drive_staged_at_utc) BEGIN SELECT RAISE(ABORT,'staged Drive facts are frozen'); END",
    "CREATE TRIGGER energygrid_stream_binding_freeze_v2 BEFORE UPDATE OF source_namespace,adapter_id,date_profile,evidence_ref,admission ON energygrid_stream_v2 WHEN OLD.admission='BOUND' AND (NEW.source_namespace IS NOT OLD.source_namespace OR NEW.adapter_id IS NOT OLD.adapter_id OR NEW.date_profile IS NOT OLD.date_profile OR NEW.evidence_ref IS NOT OLD.evidence_ref OR NEW.admission IS NOT OLD.admission) AND NOT (NEW.admission='HOLD' AND NEW.source_namespace IS OLD.source_namespace AND NEW.adapter_id IS OLD.adapter_id AND NEW.date_profile IS OLD.date_profile AND NEW.evidence_ref IS OLD.evidence_ref) BEGIN SELECT RAISE(ABORT,'stream binding requires migration'); END",
    "CREATE TRIGGER energygrid_stream_no_delete_v2 BEFORE DELETE ON energygrid_stream_v2 BEGIN SELECT RAISE(ABORT,'stream rows are retained'); END",
    "CREATE TRIGGER energygrid_delivery_freeze_v1 BEFORE UPDATE ON energygrid_delivery_v1 WHEN NEW.delivery_id!=OLD.delivery_id OR NEW.invoice_id!=OLD.invoice_id OR NEW.schema!=OLD.schema OR NEW.stream!=OLD.stream OR NEW.bill_date!=OLD.bill_date OR NEW.attachment_name!=OLD.attachment_name OR NEW.sha256!=OLD.sha256 OR NEW.byte_size!=OLD.byte_size OR OLD.state!='PENDING_SEND' BEGIN SELECT RAISE(ABORT,'delivery facts are frozen'); END",
    "CREATE TRIGGER energygrid_delivery_marker_once_v1 BEFORE UPDATE OF dispatch_started_at_utc,dispatch_run_id ON energygrid_delivery_v1 WHEN OLD.dispatch_started_at_utc IS NOT NULL OR (NEW.dispatch_started_at_utc IS NULL)!=(NEW.dispatch_run_id IS NULL) BEGIN SELECT RAISE(ABORT,'dispatch marker is write-once'); END",
    "CREATE TRIGGER energygrid_delivery_invoice_guard_v1 BEFORE INSERT ON energygrid_delivery_v1 WHEN NOT EXISTS (SELECT 1 FROM energygrid_invoice_v2 i WHERE i.invoice_id=NEW.invoice_id AND i.classification='CLASSIFIED' AND i.stream=NEW.stream AND i.bill_date=NEW.bill_date AND i.canonical_filename=NEW.attachment_name AND i.archive_state='COMMITTED' AND i.drive_state='DRIVE_STAGED' AND i.byte_size=NEW.byte_size AND i.sha256=NEW.sha256) BEGIN SELECT RAISE(ABORT,'delivery invoice facts mismatch'); END",
    "CREATE TRIGGER energygrid_delivery_no_delete_v1 BEFORE DELETE ON energygrid_delivery_v1 BEGIN SELECT RAISE(ABORT,'delivery rows are retained'); END",
    "CREATE TRIGGER energygrid_file_operation_plan_freeze_v2 BEFORE UPDATE ON energygrid_file_operation_v2 WHEN NEW.operation_id!=OLD.operation_id OR NEW.invoice_id!=OLD.invoice_id OR NEW.kind!=OLD.kind OR NEW.private_path_ref!=OLD.private_path_ref OR NEW.target_relpath!=OLD.target_relpath OR NEW.binding_id IS NOT OLD.binding_id OR NEW.expected_size!=OLD.expected_size OR NEW.expected_sha256!=OLD.expected_sha256 OR NEW.source_role!=OLD.source_role OR NEW.run_id!=OLD.run_id OR NEW.prepared_at_utc!=OLD.prepared_at_utc BEGIN SELECT RAISE(ABORT,'file operation plan is frozen'); END",
    "CREATE TRIGGER energygrid_file_operation_state_guard_v2 BEFORE UPDATE ON energygrid_file_operation_v2 WHEN OLD.state IN ('COMMITTED','HOLD') OR (OLD.state='PREPARED' AND NEW.state NOT IN ('PREPARED','COMMITTED','HOLD')) BEGIN SELECT RAISE(ABORT,'file operation state is terminal'); END",
    "CREATE TRIGGER energygrid_file_operation_no_delete_v2 BEFORE DELETE ON energygrid_file_operation_v2 BEGIN SELECT RAISE(ABORT,'file operation rows are retained'); END",
    "CREATE TRIGGER energygrid_bills_no_update_v2 BEFORE UPDATE ON bills BEGIN SELECT RAISE(ABORT,'legacy bills are immutable after v2 migration'); END",
    "CREATE TRIGGER energygrid_bills_no_delete_v2 BEFORE DELETE ON bills BEGIN SELECT RAISE(ABORT,'legacy bills are immutable after v2 migration'); END",
)


class StateV2Store:
    """Existing v2 state only. Opening a daily run never creates or migrates it."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        self.connection: sqlite3.Connection | None = None
        self.validation_status: str | None = None

    def __enter__(self) -> "StateV2Store":
        try:
            if not stat.S_ISREG(self.path.lstat().st_mode):
                raise StateError("v2 state database must be an existing regular file")
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    self.path.with_name(self.path.name + suffix).lstat()
                except FileNotFoundError:
                    continue
                raise StateError("v2 state database has an operational sidecar")
            uri = self.path.resolve().as_uri() + ("?mode=ro" if self.read_only else "?mode=rw")
            self.connection = sqlite3.connect(uri, uri=True, timeout=V2_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
            conn = self.connection
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={V2_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA synchronous=FULL")
            if self.read_only:
                conn.execute("PRAGMA query_only=ON")
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if journal.lower() != "delete" or conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise StateError("v2 SQLite durability settings are unavailable")
            if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise StateError("v2 SQLite foreign keys are unavailable")
            self.validation_status = _inspect_v2_database(conn)
            if self.validation_status not in {"COMPLETE_V2", "RESUMABLE_V2"}:
                raise StateError("v2 state database is not a complete supported schema")
            return self
        except (OSError, sqlite3.Error, StateError) as exc:
            self.close()
            raise StateError("v2 state database could not be opened") from exc

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _conn(self) -> sqlite3.Connection:
        if self.connection is None:
            raise StateError("v2 state database is not open")
        return self.connection

    def transaction(self):
        if self.read_only:
            raise StateError("v2 state database is read-only")
        return _V2Transaction(self._conn())

    def stream(self, stream: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM energygrid_stream_v2 WHERE stream=?", (stream,)).fetchone()
        if row is None:
            return None
        names = [item[1] for item in self._conn().execute("PRAGMA table_xinfo(energygrid_stream_v2)")]
        return dict(zip(names, row, strict=True))

    def invoice_by_identity(self, namespace: str, stream: str, source_key: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM energygrid_invoice_v2 WHERE source_namespace=? AND stream=? AND source_invoice_key=? AND classification='CLASSIFIED'",
            (namespace, stream, source_key),
        ).fetchone()
        return _v2_invoice_dict(self._conn(), row) if row else None

    def invoice(self, invoice_id: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM energygrid_invoice_v2 WHERE invoice_id=?", (invoice_id,)).fetchone()
        return _v2_invoice_dict(self._conn(), row) if row else None

    def resolve_latest_candidate(self, candidate) -> dict | None:
        """Resolve a candidate to retained identity after checking frozen facts."""
        from .invoice import Candidate

        if not isinstance(candidate, Candidate):
            raise StateError("latest invoice binding is invalid")
        return _resolve_candidate_row(self._conn(), candidate)

    def file_operation(self, invoice_id: str, kind: str, binding_id: str | None, target_relpath: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM energygrid_file_operation_v2 WHERE invoice_id=? AND kind=? AND ifnull(binding_id,'')=ifnull(?,'') AND target_relpath=? AND state IN ('PREPARED','HOLD') ORDER BY prepared_at_utc DESC LIMIT 1",
            (invoice_id, kind, binding_id, target_relpath),
        ).fetchone()
        return _named_row(self._conn(), "energygrid_file_operation_v2", row) if row else None

    def active_file_operations(self, invoice_id: str, kinds: set[str] | None = None) -> list[dict]:
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            rows = self._conn().execute(
                f"SELECT * FROM energygrid_file_operation_v2 WHERE invoice_id=? AND kind IN ({placeholders}) AND state IN ('PREPARED','HOLD') ORDER BY prepared_at_utc,operation_id",
                (invoice_id, *sorted(kinds)),
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM energygrid_file_operation_v2 WHERE invoice_id=? AND state IN ('PREPARED','HOLD') ORDER BY prepared_at_utc,operation_id",
                (invoice_id,),
            ).fetchall()
        return [_named_row(self._conn(), "energygrid_file_operation_v2", row) for row in rows]

    def active_file_operation_conflicts(
        self, invoice_id: str, kinds: set[str], target_relpath: str
    ) -> list[dict]:
        if not kinds:
            return []
        placeholders = ",".join("?" for _ in kinds)
        rows = self._conn().execute(
            f"SELECT * FROM energygrid_file_operation_v2 WHERE kind IN ({placeholders}) "
            "AND state IN ('PREPARED','HOLD') AND (invoice_id=? OR target_relpath=?) ORDER BY prepared_at_utc,operation_id",
            (*sorted(kinds), invoice_id, target_relpath),
        ).fetchall()
        return [_named_row(self._conn(), "energygrid_file_operation_v2", row) for row in rows]

    def file_operation_history(self, invoice_id: str, kind: str, binding_id: str | None, target_relpath: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM energygrid_file_operation_v2 WHERE invoice_id=? AND kind=? AND ifnull(binding_id,'')=ifnull(?,'') AND target_relpath=? AND state='COMMITTED' ORDER BY committed_at_utc,operation_id",
            (invoice_id, kind, binding_id, target_relpath),
        ).fetchall()
        return [_named_row(self._conn(), "energygrid_file_operation_v2", row) for row in rows]

    def legacy_archive_provenance(self, invoice_id: str) -> dict | None:
        row = self._conn().execute(
            "SELECT b.filename_key,b.portal_filename,b.status,b.archived_at_utc,b.byte_size,b.sha256,b.completion_source "
            "FROM energygrid_invoice_v2 i JOIN bills b ON b.filename_key=i.legacy_filename_key WHERE i.invoice_id=?",
            (invoice_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(zip(
            ("legacy_filename_key", "legacy_path", "status", "archived_at_utc", "byte_size", "sha256", "completion_source"),
            row,
            strict=True,
        ))

    def has_open_file_operation(self, invoice_id: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM energygrid_file_operation_v2 WHERE invoice_id=? AND state IN ('PREPARED','HOLD') LIMIT 1",
            (invoice_id,),
        ).fetchone()
        return row is not None

    def verify_stream_binding(self, stream: str, entry) -> bool:
        row = self.stream(stream)
        if row is None:
            return False
        if entry.admission == "UNBOUND":
            return row["admission"] == "UNBOUND" and all(row[key] is None for key in ("source_namespace", "adapter_id", "date_profile", "evidence_ref"))
        return (
            row["admission"] == "BOUND"
            and row["source_namespace"] == entry.source_namespace
            and row["adapter_id"] == entry.adapter_id
            and row["date_profile"] == entry.date_profile
            and row["evidence_ref"] == entry.evidence_ref
        )

    def accept_latest(self, candidate, run_id: str, timestamp: str) -> str:
        """Durably classify the selected current latest and advance its watermark."""
        from .invoice import Candidate, Stream

        if not isinstance(candidate, Candidate) or not _valid_run_id(run_id):
            raise StateError("latest invoice binding is invalid")
        stream_name = candidate.stream.value
        with self.transaction() as connection:
            stream_row = connection.execute(
                "SELECT admission,source_namespace,date_profile,watermark_day,watermark_invoice_id FROM energygrid_stream_v2 WHERE stream=?",
                (stream_name,),
            ).fetchone()
            if stream_row is None or stream_row[0] != "BOUND" or stream_row[1] != candidate.source_namespace or stream_row[2] != candidate.date_profile:
                raise StreamStateConflictError(stream_name, "EG_STREAM_BINDING_MISMATCH")
            retained = _resolve_candidate_row(connection, candidate)
            invoice_id = retained["invoice_id"] if retained is not None else _stable_invoice_id(
                candidate.source_namespace, stream_name, candidate.source_invoice_key
            )
            previous_day, previous_invoice = stream_row[3], stream_row[4]
            if previous_day is not None and candidate.day_ordinal < previous_day:
                raise StreamStateConflictError(stream_name, "EG_SOURCE_REGRESSION")
            if previous_day == candidate.day_ordinal and previous_invoice is not None and previous_invoice != invoice_id:
                raise StreamStateConflictError(stream_name, "EG_LATEST_IDENTITY_CONFLICT")
            if retained is None:
                stream_label = "EB Bill" if candidate.stream is Stream.EB_BILL else "Tenant Bill"
                relpath = f"{stream_label}/{candidate.canonical_filename}"
                connection.execute(
                    "INSERT INTO energygrid_invoice_v2 (invoice_id,classification,stream,source_namespace,source_invoice_key,source_filename,raw_date,date_profile,bill_date,day_ordinal,evidence_ref,canonical_filename,archive_relpath,archive_state,migration_state,drive_state,created_at_utc,created_run_id,last_seen_run_id) VALUES (?,'CLASSIFIED',?,?,?,?,?,?,?,?,?,?,?,'ABSENT','CLASSIFIED','NOT_STAGED',?,?,?)",
                    (invoice_id, stream_name, candidate.source_namespace, candidate.source_invoice_key, candidate.source_filename, candidate.raw_date, candidate.date_profile, candidate.invoice_date.isoformat(), candidate.day_ordinal, candidate.evidence_ref, candidate.canonical_filename, relpath, timestamp, run_id, run_id),
                )
            elif previous_day != candidate.day_ordinal:
                connection.execute(
                    "UPDATE energygrid_invoice_v2 SET last_seen_run_id=? WHERE invoice_id=?",
                    (run_id, invoice_id),
                )
            if previous_day is None or candidate.day_ordinal > previous_day:
                connection.execute(
                    "UPDATE energygrid_stream_v2 SET watermark_day=?,watermark_invoice_id=?,watermark_run_id=?,watermark_at_utc=? WHERE stream=?",
                    (candidate.day_ordinal, invoice_id, run_id, timestamp, stream_name),
                )
        return invoice_id

    def update_invoice_file_state(
        self,
        invoice_id: str,
        *,
        archive_state: str | None = None,
        byte_size: int | None = None,
        sha256: str | None = None,
        archived_at_utc: str | None = None,
        migration_state: str | None = None,
        drive_state: str | None = None,
        drive_binding_id: str | None = None,
        drive_relpath: str | None = None,
        drive_size: int | None = None,
        drive_sha256: str | None = None,
        drive_staged_at_utc: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            fields = []
            values: list[Any] = []
            for name, value in (
                ("archive_state", archive_state), ("byte_size", byte_size), ("sha256", sha256),
                ("archived_at_utc", archived_at_utc), ("migration_state", migration_state),
                ("drive_state", drive_state),
                ("drive_binding_id", drive_binding_id), ("drive_relpath", drive_relpath),
                ("drive_size", drive_size), ("drive_sha256", drive_sha256),
                ("drive_staged_at_utc", drive_staged_at_utc),
            ):
                if value is not None:
                    fields.append(f"{name}=?")
                    values.append(value)
            if not fields:
                return
            values.append(invoice_id)
            cursor = connection.execute(f"UPDATE energygrid_invoice_v2 SET {','.join(fields)} WHERE invoice_id=?", values)
            if cursor.rowcount != 1:
                raise StateError("invoice record is unavailable")

    def delivery(self, delivery_id: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM energygrid_delivery_v1 WHERE delivery_id=?", (delivery_id,)).fetchone()
        return _named_row(self._conn(), "energygrid_delivery_v1", row) if row else None

    def delivery_for_invoice(self, invoice_id: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM energygrid_delivery_v1 WHERE invoice_id=?", (invoice_id,)).fetchone()
        return _named_row(self._conn(), "energygrid_delivery_v1", row) if row else None

    def prepare_delivery(self, *, invoice_id: str, metadata: dict, run_id: str, timestamp: str, delivery_id: str) -> tuple[dict, bool]:
        """Create one frozen send intent per invoice, or return its existing row."""
        if not _valid_run_id(run_id):
            raise StateError("delivery run identity is invalid")
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM energygrid_delivery_v1 WHERE invoice_id=?", (invoice_id,)).fetchone()
            if row is not None:
                existing = _named_row(connection, "energygrid_delivery_v1", row)
                expected = {
                    "schema": metadata["schema"], "stream": metadata["stream"],
                    "bill_date": metadata["bill_date"], "attachment_name": metadata["attachment_name"],
                    "sha256": metadata["pdf_sha256"], "byte_size": metadata["pdf_byte_size"],
                }
                if any(existing[key] != value for key, value in expected.items()):
                    owner = connection.execute(
                        "SELECT stream FROM energygrid_invoice_v2 WHERE invoice_id=? AND classification='CLASSIFIED'",
                        (invoice_id,),
                    ).fetchone()
                    if owner is None:
                        raise StateError("delivery invoice is unavailable")
                    raise StreamStateConflictError(owner[0], "EG_DELIVERY_INTENT_FACTS_CONFLICT")
                return existing, False
            connection.execute(
                "INSERT INTO energygrid_delivery_v1 (delivery_id,invoice_id,schema,stream,bill_date,attachment_name,sha256,byte_size,state,intent_run_id,intent_at_utc) VALUES (?,?,?,?,?,?,?,?,'PENDING_SEND',?,?)",
                (delivery_id, invoice_id, metadata["schema"], metadata["stream"], metadata["bill_date"], metadata["attachment_name"], metadata["pdf_sha256"], metadata["pdf_byte_size"], run_id, timestamp),
            )
            row = connection.execute("SELECT * FROM energygrid_delivery_v1 WHERE delivery_id=?", (delivery_id,)).fetchone()
            return _named_row(connection, "energygrid_delivery_v1", row), True

    def claim_delivery_dispatch(self, delivery_id: str, run_id: str, timestamp: str) -> bool:
        """Return true only after the write-once marker commits successfully."""
        if not _valid_run_id(run_id):
            raise StateError("delivery run identity is invalid")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE energygrid_delivery_v1 SET dispatch_started_at_utc=?,dispatch_run_id=? WHERE delivery_id=? AND state='PENDING_SEND' AND dispatch_started_at_utc IS NULL",
                (timestamp, run_id, delivery_id),
            )
            return cursor.rowcount == 1

    def record_delivery_outcome(
        self,
        delivery_id: str,
        run_id: str,
        timestamp: str,
        *,
        state: str,
        evidence: str,
        support_ref: str,
        accepted_at_utc: str | None = None,
    ) -> bool:
        if state not in {"DELIVERED", "DELIVERY_OUTCOME_UNCERTAIN", "REQUEST_REJECTED"} or not _valid_run_id(run_id):
            raise StateError("delivery outcome is invalid")
        if not re.fullmatch(r"EG_[A-Z0-9_]{1,60}", support_ref, re.ASCII):
            raise StateError("delivery support reference is invalid")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE energygrid_delivery_v1 SET state=?,outcome_run_id=?,outcome_at_utc=?,accepted_at_utc=?,support_ref=?,evidence=? WHERE delivery_id=? AND state='PENDING_SEND' AND dispatch_started_at_utc IS NOT NULL",
                (state, run_id, timestamp, accepted_at_utc, support_ref, evidence, delivery_id),
            )
            return cursor.rowcount == 1

    def start_file_operation(
        self,
        *,
        operation_id: str,
        invoice_id: str,
        kind: str,
        private_path_ref: str,
        target_relpath: str,
        binding_id: str | None,
        info: FileInfo,
        source_role: str,
        run_id: str,
        timestamp: str,
    ) -> dict:
        if kind not in {"ARCHIVE_PUBLISH", "LEGACY_MOVE", "DRIVE_STAGE"} or not _valid_run_id(run_id):
            raise StateError("file operation is invalid")
        with self.transaction() as connection:
            archive_kinds = {"ARCHIVE_PUBLISH", "LEGACY_MOVE"}
            related_kinds = archive_kinds if kind in archive_kinds else {kind}
            placeholders = ",".join("?" for _ in related_kinds)
            active_rows = connection.execute(
                f"SELECT * FROM energygrid_file_operation_v2 WHERE invoice_id=? AND kind IN ({placeholders}) AND state IN ('PREPARED','HOLD') ORDER BY prepared_at_utc,operation_id",
                (invoice_id, *sorted(related_kinds)),
            ).fetchall()
            target_rows = connection.execute(
                f"SELECT * FROM energygrid_file_operation_v2 WHERE kind IN ({placeholders}) AND state IN ('PREPARED','HOLD') AND target_relpath=? ORDER BY prepared_at_utc,operation_id",
                (*sorted(related_kinds), target_relpath),
            ).fetchall()
            expected_identity = (kind, binding_id, target_relpath)
            if any(
                (row[2], row[6], row[5]) != expected_identity
                for row in active_rows
            ) or len(active_rows) > 1 or any(row[1] != invoice_id for row in target_rows):
                owner = connection.execute(
                    "SELECT stream FROM energygrid_invoice_v2 WHERE invoice_id=? AND classification='CLASSIFIED'",
                    (invoice_id,),
                ).fetchone()
                if owner is None:
                    raise StateError("file operation invoice is unavailable")
                raise StreamStateConflictError(owner[0], "EG_FILE_OPERATION_AUTHORITY_CONFLICT")
            if active_rows:
                existing = _named_row(connection, "energygrid_file_operation_v2", active_rows[0])
                if existing["expected_size"] != info.byte_size or existing["expected_sha256"] != info.sha256:
                    owner = connection.execute(
                        "SELECT stream FROM energygrid_invoice_v2 WHERE invoice_id=?",
                        (invoice_id,),
                    ).fetchone()
                    if owner is None:
                        raise StateError("file operation invoice is unavailable")
                    raise StreamStateConflictError(owner[0], "EG_FILE_OPERATION_FACTS_CONFLICT")
                return existing
            connection.execute(
                "INSERT INTO energygrid_file_operation_v2 (operation_id,invoice_id,kind,state,private_path_ref,target_relpath,binding_id,expected_size,expected_sha256,source_role,run_id,prepared_at_utc) VALUES (?,?,?,'PREPARED',?,?,?,?,?,?,?,?)",
                (operation_id, invoice_id, kind, private_path_ref, target_relpath, binding_id, info.byte_size, info.sha256, source_role, run_id, timestamp),
            )
            row = connection.execute("SELECT * FROM energygrid_file_operation_v2 WHERE operation_id=?", (operation_id,)).fetchone()
            return _named_row(connection, "energygrid_file_operation_v2", row)

    def complete_file_operation(self, operation_id: str, timestamp: str, *, evidence_ref: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE energygrid_file_operation_v2 SET state='COMMITTED',committed_at_utc=?,evidence_ref=? WHERE operation_id=? AND state='PREPARED'",
                (timestamp, evidence_ref, operation_id),
            )
            if cursor.rowcount != 1:
                raise StateError("file operation cannot be committed")

    def hold_file_operation(self, operation_id: str, support_ref: str) -> None:
        if not re.fullmatch(r"EG_[A-Z0-9_]{1,60}", support_ref, re.ASCII):
            raise StateError("file operation support reference is invalid")
        with self.transaction() as connection:
            connection.execute(
                "UPDATE energygrid_file_operation_v2 SET state='HOLD',support_ref=? WHERE operation_id=? AND state='PREPARED'",
                (support_ref, operation_id),
            )

    def recover_uncertain_deliveries(self, run_id: str, timestamp: str) -> int:
        if not _valid_run_id(run_id):
            raise StateError("delivery recovery run identity is invalid")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE energygrid_delivery_v1 SET state='DELIVERY_OUTCOME_UNCERTAIN',outcome_run_id=?,outcome_at_utc=?,support_ref='EG_MAIL_RECOVERY_UNCERTAIN',evidence='RECOVERED_DISPATCH_MARKER' WHERE state='PENDING_SEND' AND dispatch_started_at_utc IS NOT NULL",
                (run_id, timestamp),
            )
            return cursor.rowcount


class _V2Transaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            try:
                self.connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                raise StateError("v2 state transaction failed") from exc
            return
        try:
            self.connection.execute("COMMIT")
        except sqlite3.Error as error:
            try:
                self.connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise StateError("v2 state transaction failed") from error


def _named_row(connection: sqlite3.Connection, table: str, row: tuple) -> dict:
    names = [item[1] for item in connection.execute(f"PRAGMA table_xinfo({table})")]
    return dict(zip(names, row, strict=True))


def _v2_invoice_dict(connection: sqlite3.Connection, row: tuple) -> dict:
    return _named_row(connection, "energygrid_invoice_v2", row)


def _resolve_candidate_row(connection: sqlite3.Connection, candidate) -> dict | None:
    stream = candidate.stream.value
    row = connection.execute(
        "SELECT * FROM energygrid_invoice_v2 WHERE source_namespace=? AND stream=? AND source_invoice_key=? AND classification='CLASSIFIED'",
        (candidate.source_namespace, stream, candidate.source_invoice_key),
    ).fetchone()
    possible_legacy_matches = connection.execute(
        "SELECT legacy_filename_key FROM energygrid_invoice_v2 "
        "WHERE classification='UNCLASSIFIED' AND legacy_filename_key IS NOT NULL "
        "UNION SELECT b.filename_key FROM bills b LEFT JOIN energygrid_invoice_v2 i "
        "ON i.legacy_filename_key=b.filename_key WHERE i.invoice_id IS NULL"
    ).fetchall()
    if any(
        type(legacy_key) is not str or filename_key(legacy_key) == candidate.source_invoice_key
        for (legacy_key,) in possible_legacy_matches
    ):
        raise StreamStateConflictError(stream, "EG_LEGACY_IDENTITY_UNRESOLVED")
    if row is not None:
        invoice = _v2_invoice_dict(connection, row)
        folder = "EB Bill" if stream == "EB_BILL" else "Tenant Bill"
        expected = {
            "source_namespace": candidate.source_namespace,
            "stream": stream,
            "source_invoice_key": candidate.source_invoice_key,
            "source_filename": candidate.source_filename,
            "raw_date": candidate.raw_date,
            "date_profile": candidate.date_profile,
            "bill_date": candidate.invoice_date.isoformat(),
            "day_ordinal": candidate.day_ordinal,
            "evidence_ref": candidate.evidence_ref,
            "canonical_filename": candidate.canonical_filename,
            "archive_relpath": f"{folder}/{candidate.canonical_filename}",
        }
        if any(invoice.get(name) != value for name, value in expected.items()):
            raise StreamStateConflictError(stream, "EG_LATEST_IDENTITY_FACTS_CHANGED")
        if invoice.get("migration_state") == "HOLD":
            raise StreamStateConflictError(stream, "EG_LEGACY_MIGRATION_HOLD")
        return invoice

    return None


def _stable_invoice_id(namespace: str, stream: str, source_key: str) -> str:
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"energygrid-v2:{namespace}:{stream}:{source_key}"))


def _valid_run_id(value: str) -> bool:
    import re

    return type(value) is str and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.ASCII) is not None


_LEGACY_BILLS_SCHEMA_SQL = (
    "CREATE TABLE bills (filename_key TEXT PRIMARY KEY,portal_filename TEXT NOT NULL,"
    "first_seen_at_utc TEXT NOT NULL,last_seen_at_utc TEXT NOT NULL,archived_at_utc TEXT,"
    "byte_size INTEGER,sha256 TEXT,status TEXT NOT NULL,last_error_class TEXT,last_error_at_utc TEXT,"
    "attempt_count INTEGER NOT NULL DEFAULT 0,completion_source TEXT)"
)
_V2_PREDECESSOR_ARCHIVE_GUARD_SQL = (
    "CREATE TRIGGER energygrid_invoice_archive_commit_guard_v2 BEFORE UPDATE OF archive_state ON energygrid_invoice_v2 "
    "WHEN NEW.archive_state='COMMITTED' AND OLD.archive_state!='COMMITTED' AND NOT EXISTS "
    "(SELECT 1 FROM energygrid_file_operation_v2 o WHERE o.invoice_id=NEW.invoice_id AND o.kind='ARCHIVE_PUBLISH' "
    "AND o.state='COMMITTED' AND o.target_relpath=NEW.archive_relpath AND o.expected_size=NEW.byte_size "
    "AND o.expected_sha256=NEW.sha256) BEGIN SELECT RAISE(ABORT,'archive commit lacks matching journal'); END"
)


def _backup_state_database(connection: sqlite3.Connection, path: Path, label: str) -> Path:
    import os
    import uuid

    backup_path = Path(str(path) + f".{label}-{uuid.uuid4().hex}.bak")
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    backup = sqlite3.connect(backup_path)
    try:
        connection.backup(backup)
        backup.commit()
    finally:
        backup.close()
    with backup_path.open("rb+") as handle:
        os.fsync(handle.fileno())
    return backup_path


def _schema_manifest(connection: sqlite3.Connection) -> dict[tuple[str, str], str | None]:
    return {
        (kind, name): _normalize_schema_sql(sql) if sql is not None else None
        for kind, name, sql in connection.execute(
            "SELECT type,name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    }


def _normalize_schema_sql(sql: str) -> str:
    pieces: list[str] = []
    quote: str | None = None
    pending_space = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            pieces.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    pieces.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"', "`"}:
            if pending_space and pieces and pieces[-1] not in {"(", ".", ","}:
                pieces.append(" ")
            pending_space = False
            quote = char
            pieces.append(char)
        elif char.isspace():
            pending_space = True
        else:
            if pending_space and pieces and pieces[-1] not in {"(", ".", ","} and char not in {")", ",", "."
            }:
                pieces.append(" ")
            pending_space = False
            pieces.append(char)
        index += 1
    return "".join(pieces).strip()


def _reference_v2_manifest(*, predecessor: bool = False) -> dict[tuple[str, str], str | None]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(_LEGACY_BILLS_SCHEMA_SQL)
        for statement in V2_SCHEMA_SQL:
            if predecessor and statement.startswith("CREATE TRIGGER energygrid_invoice_archive_commit_guard_v2"):
                statement = _V2_PREDECESSOR_ARCHIVE_GUARD_SQL
            connection.execute(statement)
        return _schema_manifest(connection)
    finally:
        connection.close()


def _inspect_v2_database(connection: sqlite3.Connection) -> str:
    """Return a closed schema/data status without repairing the supplied database."""
    try:
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != V2_SCHEMA_VERSION:
            return "INCOMPATIBLE_V2"
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
            return "INCOMPATIBLE_V2"
        if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
            return "INCOMPATIBLE_V2"
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            return "INCOMPATIBLE_V2"

        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            return "CORRUPT_OR_UNREADABLE_V2"

        actual = _schema_manifest(connection)
        current = _reference_v2_manifest()
        predecessor = _reference_v2_manifest(predecessor=True)
        if actual == predecessor:
            generation = "RECOGNIZED_PREDECESSOR"
        elif actual == current:
            generation = "CURRENT"
        else:
            expected_keys = set(current)
            actual_keys = set(actual)
            if expected_keys - actual_keys:
                return "INCOMPLETE_V2"
            if actual_keys - expected_keys:
                return "INCOMPATIBLE_V2"
            return "INCOMPATIBLE_V2"

        if list(connection.execute("PRAGMA foreign_key_check")):
            return "INCOMPATIBLE_V2"
        if not _v2_semantic_data_is_valid(connection):
            return "INCOMPATIBLE_V2"
        if generation == "RECOGNIZED_PREDECESSOR":
            return generation
        return "RESUMABLE_V2" if _v2_has_resumable_work(connection) else "COMPLETE_V2"
    except sqlite3.DatabaseError:
        return "CORRUPT_OR_UNREADABLE_V2"
    except sqlite3.Error:
        return "CORRUPT_OR_UNREADABLE_V2"


def _v2_semantic_data_is_valid(connection: sqlite3.Connection) -> bool:
    from .invoice import DATE_PROFILE_ISO_V1, parse_invoice_date
    from .publication import filename_key

    streams = {
        row[0]: _named_row(connection, "energygrid_stream_v2", row)
        for row in connection.execute("SELECT * FROM energygrid_stream_v2")
    }
    if set(streams) != set(V2_STREAMS):
        return False
    invoices = {
        row[0]: _named_row(connection, "energygrid_invoice_v2", row)
        for row in connection.execute("SELECT * FROM energygrid_invoice_v2")
    }
    legacy_rows = {
        row[0]: row
        for row in connection.execute(
            "SELECT filename_key,portal_filename,archived_at_utc,byte_size,sha256,status,completion_source FROM bills"
        )
    }
    if connection.execute(
        "SELECT 1 FROM bills b LEFT JOIN energygrid_invoice_v2 i ON i.legacy_filename_key=b.filename_key "
        "WHERE i.invoice_id IS NULL LIMIT 1"
    ).fetchone():
        return False
    operations = [
        _named_row(connection, "energygrid_file_operation_v2", row)
        for row in connection.execute("SELECT * FROM energygrid_file_operation_v2")
    ]
    deliveries = {
        row[1]: _named_row(connection, "energygrid_delivery_v1", row)
        for row in connection.execute("SELECT * FROM energygrid_delivery_v1")
    }

    for invoice in invoices.values():
        legacy_key = invoice["legacy_filename_key"]
        if legacy_key is not None:
            legacy = legacy_rows.get(legacy_key)
            if legacy is None or invoice["legacy_path"] != legacy[1]:
                return False
            if legacy[5] in {"ARCHIVED", "PRESENT_RECONCILED"}:
                if (
                    legacy[2] is None or type(legacy[3]) is not int or legacy[3] <= 0
                    or type(legacy[4]) is not str or re.fullmatch(r"[0-9a-f]{64}", legacy[4], re.ASCII) is None
                    or legacy[6] not in {"downloaded", "preexisting"}
                    or invoice["byte_size"] != legacy[3] or invoice["sha256"] != legacy[4]
                ):
                    return False
            elif invoice["archive_state"] == "UNVERIFIED" and (
                invoice["byte_size"] != legacy[3] or invoice["sha256"] != legacy[4]
            ):
                return False
        elif invoice["legacy_path"] is not None:
            return False

        if invoice["classification"] == "UNCLASSIFIED":
            if any(invoice[key] is not None for key in (
                "stream", "source_namespace", "source_invoice_key", "source_filename", "raw_date",
                "date_profile", "bill_date", "day_ordinal", "evidence_ref", "canonical_filename", "archive_relpath",
            )):
                return False
            continue

        try:
            stream = invoice["stream"]
            if stream not in streams:
                return False
            binding = streams[stream]
            if (
                binding["admission"] not in {"BOUND", "HOLD"}
                or binding["source_namespace"] != invoice["source_namespace"]
                or binding["date_profile"] != invoice["date_profile"]
                or binding["evidence_ref"] != invoice["evidence_ref"]
                or binding["adapter_id"] != "DIRECT_HTTP_V1"
            ):
                return False
            if invoice["date_profile"] != DATE_PROFILE_ISO_V1:
                return False
            parsed = parse_invoice_date(invoice["raw_date"], invoice["date_profile"])
            if (
                parsed.isoformat() != invoice["bill_date"]
                or parsed.toordinal() != invoice["day_ordinal"]
                or filename_key(invoice["source_filename"]) != invoice["source_invoice_key"]
                or invoice["canonical_filename"] != f"{parsed.isoformat()}.pdf"
            ):
                return False
            if invoice["legacy_filename_key"] is not None and (
                invoice["legacy_filename_key"] != invoice["source_invoice_key"]
            ):
                return False
            folder = "EB Bill" if stream == "EB_BILL" else "Tenant Bill"
            if invoice["archive_relpath"] != f"{folder}/{invoice['canonical_filename']}":
                return False
        except (TypeError, ValueError, StateError):
            return False

    for stream_name, stream in streams.items():
        if stream["watermark_day"] is None:
            if stream["watermark_invoice_id"] is not None:
                return False
            continue
        invoice = invoices.get(stream["watermark_invoice_id"])
        if (
            invoice is None or invoice["classification"] != "CLASSIFIED"
            or invoice["stream"] != stream_name or invoice["source_namespace"] != stream["source_namespace"]
            or invoice["day_ordinal"] != stream["watermark_day"]
        ):
            return False

    for operation in operations:
        invoice = invoices.get(operation["invoice_id"])
        if invoice is None or operation["target_relpath"] != invoice["archive_relpath"]:
            return False
        if operation["kind"] == "ARCHIVE_PUBLISH":
            if operation["binding_id"] is not None or operation["source_role"] != "SOURCE_ACQUISITION":
                return False
        elif operation["kind"] == "LEGACY_MOVE":
            if (
                operation["binding_id"] is not None or operation["source_role"] != "LEGACY_ARCHIVE"
                or invoice["legacy_filename_key"] is None
            ):
                return False
        elif operation["kind"] == "DRIVE_STAGE":
            if operation["binding_id"] is None or operation["source_role"] != "ARCHIVE_COMMITTED":
                return False
        else:
            return False
        if invoice["archive_state"] == "COMMITTED" and operation["kind"] in {"ARCHIVE_PUBLISH", "LEGACY_MOVE"}:
            if operation["state"] == "COMMITTED" and (
                operation["expected_size"] != invoice["byte_size"]
                or operation["expected_sha256"] != invoice["sha256"]
            ):
                return False

    for invoice in invoices.values():
        if invoice["classification"] != "CLASSIFIED":
            continue
        archive_commits = [
            op for op in operations
            if op["invoice_id"] == invoice["invoice_id"]
            and op["kind"] in {"ARCHIVE_PUBLISH", "LEGACY_MOVE"}
            and op["state"] == "COMMITTED"
            and op["target_relpath"] == invoice["archive_relpath"]
            and op["expected_size"] == invoice["byte_size"]
            and op["expected_sha256"] == invoice["sha256"]
        ]
        if invoice["archive_state"] == "COMMITTED" and not archive_commits:
            return False
        if invoice["drive_state"] == "DRIVE_STAGED":
            drive_commits = [
                op for op in operations
                if op["invoice_id"] == invoice["invoice_id"] and op["kind"] == "DRIVE_STAGE"
                and op["state"] == "COMMITTED" and op["binding_id"] == invoice["drive_binding_id"]
                and op["target_relpath"] == invoice["drive_relpath"]
                and op["expected_size"] == invoice["drive_size"]
                and op["expected_sha256"] == invoice["drive_sha256"]
            ]
            if not drive_commits:
                return False
        delivery = deliveries.get(invoice["invoice_id"])
        if delivery is not None:
            if (
                delivery["stream"] != invoice["stream"] or delivery["bill_date"] != invoice["bill_date"]
                or delivery["attachment_name"] != invoice["canonical_filename"]
                or delivery["byte_size"] != invoice["byte_size"] or delivery["sha256"] != invoice["sha256"]
            ):
                return False
            if delivery["state"] != "PENDING_SEND" and (
                type(delivery["support_ref"]) is not str
                or re.fullmatch(r"EG_[A-Z0-9_]{1,60}", delivery["support_ref"], re.ASCII) is None
                or delivery["evidence"] not in {"VALIDATED_N8N_RESULT", "NO_VALID_N8N_RESULT", "RECOVERED_DISPATCH_MARKER"}
            ):
                return False
    return True


def _v2_has_resumable_work(connection: sqlite3.Connection) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM energygrid_invoice_v2 WHERE classification='UNCLASSIFIED' "
            "OR archive_state IN ('PREPARED','CONFLICT','UNVERIFIED') "
            "OR drive_state IN ('PREPARED','CONFLICT') OR migration_state IN ('UNCLASSIFIED','MOVE_PLANNED','HOLD') LIMIT 1"
        ).fetchone()
        or connection.execute(
            "SELECT 1 FROM energygrid_stream_v2 WHERE admission='HOLD' LIMIT 1"
        ).fetchone()
        or connection.execute(
            "SELECT 1 FROM energygrid_file_operation_v2 WHERE state IN ('PREPARED','HOLD') LIMIT 1"
        ).fetchone()
        or connection.execute(
            "SELECT 1 FROM energygrid_file_operation_v2 AS operation "
            "JOIN energygrid_invoice_v2 AS invoice USING(invoice_id) "
            "WHERE operation.state='COMMITTED' AND ("
            "(operation.kind IN ('ARCHIVE_PUBLISH','LEGACY_MOVE') AND invoice.archive_state!='COMMITTED') "
            "OR (operation.kind='DRIVE_STAGE' AND invoice.drive_state!='DRIVE_STAGED')) LIMIT 1"
        ).fetchone()
        or connection.execute(
            "SELECT 1 FROM energygrid_stream_v2 AS stream "
            "JOIN energygrid_invoice_v2 AS invoice ON invoice.invoice_id=stream.watermark_invoice_id "
            "LEFT JOIN energygrid_delivery_v1 AS delivery ON delivery.invoice_id=invoice.invoice_id "
            "WHERE stream.watermark_invoice_id IS NOT NULL AND ("
            "invoice.archive_state!='COMMITTED' OR invoice.drive_state!='DRIVE_STAGED' "
            "OR delivery.delivery_id IS NULL OR delivery.state!='DELIVERED') LIMIT 1"
        ).fetchone()
        or connection.execute(
            "SELECT 1 FROM energygrid_delivery_v1 WHERE state!='DELIVERED' LIMIT 1"
        ).fetchone()
    )


def migrate_state_database(
    path: Path,
    *,
    apply: bool = False,
    streams: dict | None = None,
    mapping_entries: list[dict] | None = None,
    migration_run_id: str | None = None,
) -> dict[str, int | str]:
    """Bounded v1-to-v2 migration; a default call only returns a read-only plan."""
    if not path.is_absolute():
        raise StateError("migration database path must be absolute")
    exists = path.exists()
    if exists and not stat.S_ISREG(path.lstat().st_mode):
        raise StateError("migration database must be a regular file")
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        raise StateError("migration database has an operational sidecar")
    count = 0
    mapping_entries = mapping_entries or []
    _validate_migration_mapping(mapping_entries, streams or {})
    migration_run_id = migration_run_id or "00000000-0000-0000-0000-000000000000"
    if not _valid_run_id(migration_run_id):
        raise StateError("migration run identity is invalid")
    version = 0
    if exists:
        uri = path.resolve().as_uri() + ("?mode=rw" if apply else "?mode=ro")
        try:
            connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        except sqlite3.Error:
            raise StateError("migration database could not be opened") from None
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={V2_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA synchronous=FULL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 1:
                names = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                if names != {"bills"}:
                    raise StateError("migration source schema is unsupported")
                columns = {row[1]: row[2].upper() for row in connection.execute("PRAGMA table_xinfo(bills)")}
                expected_types = {name: ("INTEGER" if name in {"byte_size", "attempt_count"} else "TEXT") for name in REQUIRED_COLUMNS}
                if columns != expected_types:
                    raise StateError("migration source schema is unsupported")
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StateError("migration source integrity check failed")
                rows = connection.execute(
                    "SELECT filename_key,portal_filename,first_seen_at_utc,last_seen_at_utc,archived_at_utc,byte_size,sha256,status,last_error_class,last_error_at_utc,attempt_count,completion_source FROM bills ORDER BY filename_key"
                ).fetchall()
                count = len(rows)
            elif version == V2_SCHEMA_VERSION:
                status = _inspect_v2_database(connection)
                if status == "COMPLETE_V2":
                    return {"status": "ALREADY_V2", "legacy_rows": 0}
                if status == "RESUMABLE_V2":
                    return {"status": "RESUMABLE_V2", "legacy_rows": 0}
                if status != "RECOGNIZED_PREDECESSOR":
                    raise StateError("existing v2 database is incomplete or incompatible")
                if not apply:
                    return {"status": "UPGRADE_REQUIRED", "legacy_rows": 0}
                _backup_state_database(connection, path, "v2-predecessor")
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("DROP TRIGGER energygrid_invoice_archive_commit_guard_v2")
                    current_guard = next(
                        statement for statement in V2_SCHEMA_SQL
                        if statement.startswith("CREATE TRIGGER energygrid_invoice_archive_commit_guard_v2")
                    )
                    connection.execute(current_guard)
                    status = _inspect_v2_database(connection)
                    if status not in {"COMPLETE_V2", "RESUMABLE_V2"}:
                        raise StateError("bounded v2 predecessor upgrade did not validate")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
                return {"status": "UPGRADED_V2", "legacy_rows": 0}
            else:
                raise StateError("migration source schema is unsupported")
            if not apply:
                return {"status": "PLAN_READY", "legacy_rows": count}
            # Preserve a durable exact-source backup before any schema write.
            _backup_state_database(connection, path, "v1")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            try:
                _create_v2_schema(connection, streams)
                for row in rows:
                    invoice_id = _stable_invoice_id("legacy", "LEGACY", row[0])
                    connection.execute(
                        "INSERT INTO energygrid_invoice_v2 (invoice_id,legacy_filename_key,classification,legacy_path,archive_state,byte_size,sha256,migration_state,drive_state,created_at_utc,created_run_id) VALUES (?,?,'UNCLASSIFIED',?,'UNVERIFIED',?,?,'UNCLASSIFIED','NOT_STAGED',?,'00000000-0000-0000-0000-000000000000')",
                        (invoice_id, row[0], row[1], row[5], row[6], row[2]),
                    )
                _apply_migration_mapping(connection, mapping_entries, streams or {}, migration_run_id)
                connection.execute(f"PRAGMA user_version={V2_SCHEMA_VERSION}")
                if _inspect_v2_database(connection) not in {"COMPLETE_V2", "RESUMABLE_V2"}:
                    raise StateError("migration schema or ownership validation failed")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            return {"status": "MIGRATED", "legacy_rows": count}
        except sqlite3.Error:
            raise StateError("migration database could not be inspected or updated") from None
        finally:
            connection.close()
    if not apply:
        return {"status": "PLAN_CREATE_V2", "legacy_rows": 0}
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={V2_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(_LEGACY_BILLS_SCHEMA_SQL)
            _create_v2_schema(connection, streams)
            if mapping_entries:
                raise StateError("fresh v2 state cannot classify legacy mappings")
            connection.execute(f"PRAGMA user_version={V2_SCHEMA_VERSION}")
            if _inspect_v2_database(connection) != "COMPLETE_V2":
                raise StateError("fresh v2 schema validation failed")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return {"status": "CREATED_V2", "legacy_rows": 0}
    finally:
        connection.close()


def _create_v2_schema(connection: sqlite3.Connection, streams: dict | None) -> None:
    for statement in V2_SCHEMA_SQL:
        connection.execute(statement)
    configured = streams or {}
    for name in V2_STREAMS:
        entry = configured.get(name)
        if entry is None:
            values = (name, None, None, None, None, "UNBOUND")
        else:
            values = (
                name,
                getattr(entry, "source_namespace", None),
                getattr(entry, "adapter_id", None),
                getattr(entry, "date_profile", None),
                getattr(entry, "evidence_ref", None),
                getattr(entry, "admission", "UNBOUND"),
            )
        connection.execute(
            "INSERT INTO energygrid_stream_v2 (stream,source_namespace,adapter_id,date_profile,evidence_ref,admission) VALUES (?,?,?,?,?,?)",
            values,
        )


def _validate_migration_mapping(entries: list[dict], streams: dict) -> None:
    from .invoice import Candidate, Stream

    if type(entries) is not list:
        raise StateError("migration mapping is invalid")
    seen_legacy: set[str] = set()
    for item in entries:
        required = {"legacy_filename_key", "stream", "source_namespace", "source_filename", "raw_date", "date_profile", "evidence_ref"}
        if type(item) is not dict or set(item) != required:
            raise StateError("migration mapping is invalid")
        legacy_key = item["legacy_filename_key"]
        if type(legacy_key) is not str or not legacy_key or legacy_key in seen_legacy:
            raise StateError("migration mapping is invalid")
        seen_legacy.add(legacy_key)
        name = item["stream"]
        if name not in {stream.value for stream in Stream}:
            raise StateError("migration mapping is invalid")
        entry = streams.get(name)
        if (
            entry is None or getattr(entry, "admission", None) != "BOUND"
            or item["source_namespace"] != entry.source_namespace
            or item["date_profile"] != entry.date_profile
            or item["evidence_ref"] != entry.evidence_ref
        ):
            raise StateError("migration mapping lacks matching admitted source evidence")
        candidate = Candidate.create(
            stream=Stream(name), source_namespace=item["source_namespace"],
            source_filename=item["source_filename"], raw_date=item["raw_date"],
            date_profile=item["date_profile"], evidence_ref=item["evidence_ref"],
            fetch_handle=None,
        )
        from .publication import filename_key
        if candidate.source_invoice_key != filename_key(legacy_key):
            raise StateError("migration mapping identity does not match legacy key")


def _apply_migration_mapping(connection: sqlite3.Connection, entries: list[dict], streams: dict, run_id: str) -> None:
    from .invoice import Candidate, Stream

    for item in entries:
        row = connection.execute(
            "SELECT invoice_id,legacy_path FROM energygrid_invoice_v2 WHERE legacy_filename_key=?",
            (item["legacy_filename_key"],),
        ).fetchone()
        if row is None:
            raise StateError("migration mapping does not match a legacy invoice")
        candidate = Candidate.create(
            stream=Stream(item["stream"]), source_namespace=item["source_namespace"],
            source_filename=item["source_filename"], raw_date=item["raw_date"],
            date_profile=item["date_profile"], evidence_ref=item["evidence_ref"],
            fetch_handle=None,
        )
        folder = "EB Bill" if candidate.stream is Stream.EB_BILL else "Tenant Bill"
        relpath = f"{folder}/{candidate.canonical_filename}"
        connection.execute(
            "UPDATE energygrid_invoice_v2 SET classification='CLASSIFIED',stream=?,source_namespace=?,source_invoice_key=?,source_filename=?,raw_date=?,date_profile=?,bill_date=?,day_ordinal=?,evidence_ref=?,canonical_filename=?,archive_relpath=?,migration_state='CLASSIFIED' WHERE invoice_id=? AND classification='UNCLASSIFIED'",
            (candidate.stream.value, candidate.source_namespace, candidate.source_invoice_key, candidate.source_filename, candidate.raw_date, candidate.date_profile, candidate.invoice_date.isoformat(), candidate.day_ordinal, candidate.evidence_ref, candidate.canonical_filename, relpath, row[0]),
        )
    for name, entry in streams.items():
        if getattr(entry, "admission", None) != "BOUND":
            continue
        maximum = connection.execute(
            "SELECT MAX(day_ordinal) FROM energygrid_invoice_v2 WHERE classification='CLASSIFIED' AND stream=? AND source_namespace=?",
            (name, entry.source_namespace),
        ).fetchone()[0]
        if maximum is None:
            continue
        latest = connection.execute(
            "SELECT invoice_id FROM energygrid_invoice_v2 WHERE classification='CLASSIFIED' AND stream=? AND source_namespace=? AND day_ordinal=? ORDER BY invoice_id",
            (name, entry.source_namespace, maximum),
        ).fetchall()
        if len(latest) != 1:
            connection.execute("UPDATE energygrid_stream_v2 SET admission='HOLD' WHERE stream=?", (name,))
            continue
        connection.execute(
            "UPDATE energygrid_stream_v2 SET watermark_day=?,watermark_invoice_id=?,watermark_run_id=?,watermark_at_utc=? WHERE stream=?",
            (maximum, latest[0][0], run_id, utc_now(), name),
        )
