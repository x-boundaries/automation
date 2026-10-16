"""One-off, operator-run backfill of older EnergyGrid bills into the flat Drive folder (#226 V3 G1).

The scheduled Core stays latest-only: it admits only the newest bill of each stream and never
backfills. This module is a separate manual command for the owner's request to fill in the older
bills once. It never touches the v4 admission state, and it never runs from the scheduler.

- One portal inventory per bound stream. The newest invoice date of each stream is skipped: it
  belongs to the scheduled Core. A later backfill run skips only the then-newest date, so a bill
  that was newest at an earlier run is picked up then.
- Each older bill is fetched once, validated as a PDF, and written with exclusive create into the
  Drive for desktop synced folder (LOCAL_SYNC only) under the v4 display name, plus the user-facing
  named archive copy. Nothing is ever overwritten.
- A bill whose display name is already in the Drive folder listing (host evidence) or in the
  backfill ledger is skipped and not fetched. A different local file at the same name is a HOLD.
- `max_files` caps the writes per run; the rest are DEFERRED to a later run.
- `record` verifies each written file against a later Drive listing (exact title, parent, size,
  single match) and the Core hash of its local mirror.

The ledger is a separate bridge-owned SQLite file next to the v4 state. Every command runs inside
the v4 lease session, so it serialises with the scheduled bridge.
"""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterator

from .errors import AppError, OfflineV4Error
from .native_bridge import (
    DRIVE_LOCAL_SYNC, EXIT_HOLD, EXIT_OK, STREAM_LABELS, NativeConfig, NativeRefusal, _PROVIDER_ID_RE, _file_state,
    _result, clean_stale_stages, publish_staged, sync_folder_problem,
)
from .naming import display_filename, display_key
from .publication import publish_no_replace, validate_pdf

MAX_BACKFILL_FILES = 50

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill (
  display_key TEXT PRIMARY KEY,
  stream TEXT NOT NULL CHECK (stream IN ('EB_BILL','TENANT_BILL')),
  display_filename TEXT NOT NULL,
  invoice_date TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('WRITTEN','VERIFIED','HOLD')),
  support_ref TEXT,
  drive_file_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (state != 'VERIFIED' OR drive_file_id IS NOT NULL)
);
"""


def ledger_path(cfg: NativeConfig) -> Path:
    return cfg.state_path.with_name(cfg.state_path.stem + ".backfill.sqlite")


def drive_query(folder_id: str) -> str:
    """The exact Drive listing the host runs before and after a backfill (all EnergyGrid files).

    Drive search matches whole words and ignores punctuation, so `'EnergyGrid - '` finds nothing;
    the word `EnergyGrid` is the reliable term. Extra matches are harmless (exact-title lookups)."""
    return f"parentId = '{folder_id}' and title contains 'EnergyGrid'"


class BackfillLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> "BackfillLedger":
        conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(_SCHEMA)
        self.connection = conn
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    @contextlib.contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection
        assert conn is not None
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def rows(self, where: str = "1=1", params: tuple = ()) -> list[dict]:
        assert self.connection is not None
        cursor = self.connection.execute(f"SELECT * FROM backfill WHERE {where} ORDER BY invoice_date, display_key",
                                         params)
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def get(self, key: str) -> dict | None:
        found = self.rows("display_key=?", (key,))
        return found[0] if found else None

    def summary(self) -> dict[str, int]:
        counts = {"WRITTEN": 0, "VERIFIED": 0, "HOLD": 0}
        for row in self.rows():
            counts[row["state"]] += 1
        return counts


def _listing(cfg: NativeConfig, streams: list[str], evidence: dict) -> dict[str, list[dict]]:
    """Validate one complete host Drive listing and index it by display key."""
    folders = {cfg.drive.folder_ids[stream] for stream in streams}
    if len(folders) != 1:
        raise NativeRefusal("EG_BACKFILL_FOLDER_UNSUPPORTED")
    folder = folders.pop()
    files = evidence.get("files")
    if (type(evidence) is not dict or evidence.get("folder_id") != folder or evidence.get("query") != drive_query(folder)
            or evidence.get("complete") is not True or type(files) is not list):
        raise NativeRefusal("EG_BACKFILL_DRIVE_EVIDENCE_INVALID")
    index: dict[str, list[dict]] = {}
    for file in files:
        if (type(file) is not dict or type(file.get("id")) is not str or not _PROVIDER_ID_RE.fullmatch(file["id"])
                or type(file.get("title")) is not str or not file["title"]):
            raise NativeRefusal("EG_BACKFILL_DRIVE_EVIDENCE_INVALID")
        index.setdefault(display_key(file["title"]), []).append(file)
    return index


def _check_config(cfg: NativeConfig) -> None:
    if cfg.drive.mode != DRIVE_LOCAL_SYNC or cfg.drive.sync_folders is None:
        raise NativeRefusal("EG_BACKFILL_LOCAL_SYNC_REQUIRED")


def run(cfg: NativeConfig, ledger: BackfillLedger, adapters: dict[str, Any], evidence: dict, *, max_files: int,
        now_iso: str) -> dict:
    """Write the older bills of each bound stream into the synced folder, at most `max_files`."""
    _check_config(cfg)
    if type(max_files) is not int or not 1 <= max_files <= MAX_BACKFILL_FILES:
        raise NativeRefusal("EG_BACKFILL_MAX_INVALID")
    streams = [stream for stream in STREAM_LABELS if stream in adapters]
    existing = _listing(cfg, streams, evidence)
    written = 0
    items: list[dict] = []
    for stream in streams:
        snapshot = adapters[stream].inventory(cfg.inventory_ceiling)
        candidates = sorted(snapshot.candidates, key=lambda item: (item.invoice_date, item.source_invoice_key))
        if not candidates:
            continue
        newest = candidates[-1].invoice_date
        for candidate in candidates:
            item = {"stream": stream, "invoice_date": candidate.invoice_date.isoformat()}
            items.append(item)
            if candidate.invoice_date == newest:
                item["result"] = "SKIPPED_LATEST"
                continue
            try:
                name = display_filename(candidate.source_filename, candidate.invoice_date, stream=stream)
            except OfflineV4Error as exc:
                item.update(result="HOLD", support_ref=exc.support_ref)
                continue
            item["display_filename"] = name
            key = display_key(name)
            row = ledger.get(key)
            if row is not None:
                item["result"] = "LEDGER_" + row["state"]
                continue
            if key in existing:
                item["result"] = "SKIPPED_IN_DRIVE"
                continue
            if written >= max_files:
                item["result"] = "DEFERRED"
                continue
            item.update(_write_one(cfg, ledger, adapters[stream], candidate, stream, name, key, now_iso))
            if item["result"] == "WRITTEN":
                written += 1
    return _result("backfill-run", "BACKFILL_WRITTEN", EXIT_OK, written=written, items=items,
                   ledger=ledger.summary(), drive_query=drive_query(cfg.drive.folder_ids[streams[0]]) if streams else None)


def _write_one(cfg: NativeConfig, ledger: BackfillLedger, adapter: Any, candidate: Any, stream: str, name: str,
               key: str, now_iso: str) -> dict:
    temp = cfg.temp_root / f"backfill-{uuid.uuid4().hex}.pdf"
    try:
        adapter.acquire(candidate, temp)
        info = validate_pdf(temp)
        payload = temp.read_bytes()
        identity = {"sha256": info.sha256, "byte_size": info.byte_size}
        target = cfg.drive.sync_folders[stream] / name
        problem = sync_folder_problem(target.parent)
        if problem is not None:
            # P-2: the synced folder is not available now. Nothing is written or recorded; a later run retries.
            return {"result": "RETRY_LATER", "support_ref": problem}
        if _file_state(target, identity) == "MISSING":
            clean_stale_stages(target.parent)
            with contextlib.suppress(AppError, OSError):
                # H-4: staged and atomic; the final name is never a partial file. Re-judged just below.
                publish_staged(target.parent, name, payload)
        if _file_state(target, identity) != "PRESENT":
            state, support_ref = "HOLD", "EG_BACKFILL_SYNC_CONFLICT"
        else:
            state, support_ref = "WRITTEN", None
        named = cfg.named_archive_root / STREAM_LABELS[stream] / name
        if _file_state(named, identity) == "MISSING":
            publish_no_replace(temp, named)
        named_state = _file_state(named, identity)
    except (AppError, OSError) as exc:
        return {"result": "HOLD", "support_ref": getattr(exc, "support_ref", None) or "EG_BACKFILL_FETCH_FAILED"}
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()
    with ledger.write() as conn:
        conn.execute(
            "INSERT INTO backfill (display_key, stream, display_filename, invoice_date, sha256, byte_size, state, "
            "support_ref, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, stream, name, candidate.invoice_date.isoformat(), info.sha256, info.byte_size, state, support_ref,
             now_iso, now_iso),
        )
    return {"result": state, "support_ref": support_ref, "named_archive": named_state}


def record(cfg: NativeConfig, ledger: BackfillLedger, evidence: dict, *, now_iso: str) -> dict:
    """Verify every WRITTEN file against a fresh Drive listing and its local mirror."""
    _check_config(cfg)
    rows = ledger.rows("state='WRITTEN'")
    streams = sorted({row["stream"] for row in rows}) or [next(iter(STREAM_LABELS))]
    existing = _listing(cfg, streams, evidence)
    items = []
    for row in rows:
        matches = [file for file in existing.get(row["display_key"], []) if file["title"] == row["display_filename"]]
        item = {"display_filename": row["display_filename"]}
        items.append(item)
        if not matches:
            item["result"] = "AWAITING_SYNC"
            continue
        mirror = _file_state(cfg.drive.sync_folders[row["stream"]] / row["display_filename"], row)
        file = matches[0]
        size = file.get("fileSize")
        if len(matches) != 1:
            state, support_ref, file_id = "HOLD", "EG_BACKFILL_DRIVE_DUPLICATE", None
        elif (file.get("parentId") != cfg.drive.folder_ids[row["stream"]] or type(size) not in (str, int)
              or str(size) != str(row["byte_size"]) or mirror != "PRESENT"):
            state, support_ref, file_id = "HOLD", "EG_BACKFILL_DRIVE_MISMATCH", None
        else:
            state, support_ref, file_id = "VERIFIED", None, file["id"]
        with ledger.write() as conn:
            conn.execute("UPDATE backfill SET state=?, support_ref=?, drive_file_id=?, updated_at=? "
                         "WHERE display_key=? AND state='WRITTEN'",
                         (state, support_ref, file_id, now_iso, row["display_key"]))
        item.update(result=state, support_ref=support_ref)
    summary = ledger.summary()
    outcome = "BACKFILL_VERIFIED" if summary["WRITTEN"] == 0 and summary["HOLD"] == 0 else "BACKFILL_INCOMPLETE"
    return _result("backfill-record", outcome, EXIT_OK if outcome == "BACKFILL_VERIFIED" else EXIT_HOLD,
                   items=items, ledger=summary)
