"""Owner notifications for the native bridge (#226 V3 G1, Web #226:6168033136 and :6168238127).

Email is optional owner feedback, never Core success. Core success for a stream is
"archived and verified in Drive" (native_bridge). This module sends at most one email
per event to one private recipient:

- SUCCESS: an invoice's Drive upload is VERIFIED (body carries the Drive link).
- FAILURE: an actionable incident, deduplicated: a Drive intent in HOLD or UNKNOWN (once
  per intent and state), or a failed acquisition (once per incident; a stream that
  recovers closes it, and a later failure opens a new one).
- FAILURE (owner notice, P-7): an operator's audited `hold-release NO_DISPATCH`, once per
  release (derived from the v5 audit row, so it survives a crash after the release).

Gmail's own failures never create notifications (no recursion), never block Drive work
or the other stream, and are never resent. A notification is PENDING until the host
drafts it; the Core parses the RAW draft itself, verifies it and SEALs it before the host
sends that exact draft. A crash or lost answer after the seal becomes UNKNOWN; only a
late SENT for the bound draft resolves it.

The store is a separate bridge-owned SQLite file next to the v4 state, so the reviewed
v4 schema is unchanged. Every command runs inside the v4 lease session of the bridge.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .native_bridge import (
    EXIT_HOLD, EXIT_OK, PDF_MIME, STREAM_LABELS, NativeConfig, NativeRefusal, _PROVIDER_ID_RE, _outbox,
    _provider_outcome, _read_json, _result, _write_atomic, _write_json_once, ceiling_period, ms_from_iso,
)

KIND_SUCCESS, KIND_FAILURE = "SUCCESS", "FAILURE"
HOLD_RELEASED_STATUS, HOLD_RELEASED_REF = "HOLD_RELEASED_NO_DISPATCH", "EG_V5_HOLD_RELEASED"
STATES = ("PENDING", "SEALED", "SENT", "UNKNOWN", "HOLD")
HANDLED_ACQUISITION = frozenset({"ADMITTED", "ALREADY_ADMITTED", "PREDECESSOR_DELIVERED", "EMPTY"})
_NOTIFICATION_ID_RE = re.compile(r"egn-[0-9a-f]{32}", re.ASCII)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification (
  notification_id TEXT PRIMARY KEY CHECK (notification_id GLOB 'egn-*'),
  dedup_key TEXT NOT NULL UNIQUE,
  seq INTEGER NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('SUCCESS','FAILURE')),
  stream TEXT NOT NULL CHECK (stream IN ('EB_BILL','TENANT_BILL')),
  status_text TEXT NOT NULL,
  support_ref TEXT,
  display_filename TEXT,
  drive_file_id TEXT,
  invoice_key TEXT,
  sha256 TEXT,
  byte_size INTEGER,
  state TEXT NOT NULL CHECK (state IN ('PENDING','SEALED','SENT','UNKNOWN','HOLD')),
  draft_id TEXT,
  message_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (state = 'PENDING' OR draft_id IS NOT NULL),
  CHECK (state != 'SENT' OR message_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS acquisition_watch (
  stream TEXT PRIMARY KEY CHECK (stream IN ('EB_BILL','TENANT_BILL')),
  open_key TEXT,
  epoch INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS gmail_seal (
  notification_id TEXT PRIMARY KEY REFERENCES notification(notification_id),
  sealed_at TEXT NOT NULL CHECK (length(sealed_at) = 20)
);
CREATE TRIGGER IF NOT EXISTS gmail_seal_no_update BEFORE UPDATE ON gmail_seal
BEGIN SELECT RAISE(ABORT,'EG_NATIVE_SEAL_LEDGER_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS gmail_seal_no_delete BEFORE DELETE ON gmail_seal
BEGIN SELECT RAISE(ABORT,'EG_NATIVE_SEAL_LEDGER_IMMUTABLE'); END;
"""


def store_path(cfg: NativeConfig) -> Path:
    return cfg.state_path.with_name(cfg.state_path.stem + ".notifications.sqlite")


class NotificationStore:
    """Bridge-owned notification state. Transitions are compare-and-set inside BEGIN IMMEDIATE."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> "NotificationStore":
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
        cursor = self.connection.execute(f"SELECT * FROM notification WHERE {where} ORDER BY seq", params)
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def row(self, notification_id: str) -> dict:
        if type(notification_id) is not str or not _NOTIFICATION_ID_RE.fullmatch(notification_id):
            raise NativeRefusal("EG_NATIVE_NOTIFICATION_ID_INVALID")
        found = self.rows("notification_id=?", (notification_id,))
        if not found:
            raise NativeRefusal("EG_NATIVE_NOTIFICATION_UNKNOWN")
        return found[0]

    def sealed_count(self) -> int:
        assert self.connection is not None
        return int(self.connection.execute("SELECT COUNT(*) FROM notification WHERE draft_id IS NOT NULL").fetchone()[0])

    def sealed_count_since(self, since_iso: str) -> int:
        """Sealed sends that may fall in the period starting at `since_iso` (P-4). A seal recorded in the
        seal ledger counts by its seal time. An older seal with no ledger entry counts unless it is
        provably earlier (last updated before the period), so an earlier send is never forgotten."""
        assert self.connection is not None
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM notification n LEFT JOIN gmail_seal g ON g.notification_id=n.notification_id "
            "WHERE n.draft_id IS NOT NULL AND ((g.sealed_at IS NOT NULL AND g.sealed_at >= ?) "
            "OR (g.sealed_at IS NULL AND n.updated_at >= ?))",
            (since_iso, since_iso),
        ).fetchone()[0])

    def _insert(self, conn: sqlite3.Connection, dedup_key: str, now_iso: str, **values: Any) -> bool:
        if conn.execute("SELECT 1 FROM notification WHERE dedup_key=?", (dedup_key,)).fetchone():
            return False
        seq = int(conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM notification").fetchone()[0])
        notification_id = "egn-" + hashlib.sha256(f"energygrid.notify\n{dedup_key}".encode()).hexdigest()[:32]
        columns = {"notification_id": notification_id, "dedup_key": dedup_key, "seq": seq, "state": "PENDING",
                   "created_at": now_iso, "updated_at": now_iso, **values}
        conn.execute(
            f"INSERT INTO notification ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            tuple(columns.values()),
        )
        return True

    def _move(self, conn: sqlite3.Connection, notification_id: str, expected: tuple[str, ...], target: str,
              now_iso: str, **values: Any) -> None:
        sets = ", ".join(f"{name}=?" for name in ("state", "updated_at", *values))
        placeholders = ",".join("?" for _ in expected)
        changed = conn.execute(
            f"UPDATE notification SET {sets} WHERE notification_id=? AND state IN ({placeholders})",
            (target, now_iso, *values.values(), notification_id, *expected),
        ).rowcount
        if changed != 1:
            raise NativeRefusal("EG_NATIVE_NOTIFICATION_STATE_CHANGED")


# ---------------------------------------------------------------------------
# Event detection (idempotent; called under the bridge lease)
# ---------------------------------------------------------------------------


def recover(store: NotificationStore, now_iso: str) -> int:
    """A sealed notification with no recorded outcome becomes UNKNOWN. It is never resent."""
    with store.write() as conn:
        return conn.execute(
            "UPDATE notification SET state='UNKNOWN', updated_at=? WHERE state='SEALED'", (now_iso,)
        ).rowcount


def sweep(state, store: NotificationStore, now_iso: str, acquisition: dict | None = None) -> int:
    """Create the notifications that current Core state calls for. Returns how many were created."""
    from .native_bridge import verified_drive_file_id

    created = 0
    releases: dict[str, list[dict]] = {}
    for release in state.no_dispatch_releases():
        releases.setdefault(release["stream"], []).append(release)
    with store.write() as conn:
        for stream in STREAM_LABELS:
            for invoice in state.invoices_for_stream(stream):
                drive = state.intents_for_invoice(invoice["invoice_key"])["DRIVE_UPLOAD"]
                if drive["state"] == "VERIFIED":
                    file_id = verified_drive_file_id(state, invoice)
                    if file_id is not None:
                        created += store._insert(
                            conn, f"SUCCESS|{invoice['invoice_key']}", now_iso, kind=KIND_SUCCESS, stream=stream,
                            status_text="PROCESSED", display_filename=invoice["display_filename"],
                            drive_file_id=file_id, invoice_key=invoice["invoice_key"], sha256=invoice["sha256"],
                            byte_size=invoice["byte_size"],
                        )
                elif drive["state"] in ("HOLD", "UNKNOWN"):
                    created += store._insert(
                        conn, f"INTENT|{drive['intent_id']}|{drive['state']}", now_iso, kind=KIND_FAILURE,
                        stream=stream, status_text=f"DRIVE_UPLOAD_{drive['state']}",
                        support_ref=drive["support_ref"] or "EG_V4_INTENT_HELD",
                    )
            for release in releases.get(stream, ()):
                created += store._insert(
                    conn, f"HOLD_RELEASE|{release['intent_id']}|{release['from_version']}", now_iso,
                    kind=KIND_FAILURE, stream=stream, status_text=HOLD_RELEASED_STATUS, support_ref=HOLD_RELEASED_REF,
                    display_filename=release["display_filename"],
                )
            if acquisition is None or stream not in acquisition:
                continue
            status = acquisition[stream]["status"]
            watch = conn.execute("SELECT open_key, epoch FROM acquisition_watch WHERE stream=?", (stream,)).fetchone()
            open_key, epoch = (watch[0], watch[1]) if watch else (None, 0)
            if status in HANDLED_ACQUISITION:
                key = None
            else:
                key = f"{status}|{acquisition[stream].get('support_ref') or ''}"
                if key != open_key:
                    epoch += 1
                    created += store._insert(
                        conn, f"ACQUISITION|{stream}|{epoch}|{key}", now_iso, kind=KIND_FAILURE, stream=stream,
                        status_text=f"ACQUISITION_{status}",
                        support_ref=acquisition[stream].get("support_ref") or "EG_NATIVE_ACQUISITION_FAILED",
                    )
            conn.execute(
                "INSERT INTO acquisition_watch (stream, open_key, epoch) VALUES (?,?,?) "
                "ON CONFLICT(stream) DO UPDATE SET open_key=excluded.open_key, epoch=excluded.epoch",
                (stream, key, epoch),
            )
    return created


# ---------------------------------------------------------------------------
# Message content (fixed, non-sensitive, generated by the Core)
# ---------------------------------------------------------------------------


def drive_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def message_for(cfg: NativeConfig, row: dict) -> tuple[str, str]:
    label = STREAM_LABELS[row["stream"]]
    prefix = cfg.gmail.subject_prefix
    if row["kind"] == KIND_SUCCESS:
        subject = f"{prefix}: {label} processed - {row['display_filename']}"
        body = (f"EnergyGrid processed a new {label}.\n"
                f"File: {row['display_filename']}\n"
                f"Google Drive: {drive_link(row['drive_file_id'])}\n")
    elif row["status_text"] == HOLD_RELEASED_STATUS:
        subject = f"{prefix}: {label} hold released - {row['support_ref']}"
        body = (f"An operator released a held {label} upload that was never sent to Google Drive.\n"
                f"File: {row['display_filename']}\n"
                f"Status: {row['status_text']}\n"
                f"Reference: {row['support_ref']}\n"
                "The next scheduled run uploads it once. Check the EnergyGrid status report.\n")
    else:
        subject = f"{prefix}: {label} needs attention - {row['support_ref']}"
        body = (f"EnergyGrid could not complete the {label} stream.\n"
                f"Status: {row['status_text']}\n"
                f"Reference: {row['support_ref']}\n"
                "Uncertain uploads are never retried automatically. Check the EnergyGrid status report.\n")
    return subject, body


_GOOGLE_REDIRECT_RE = re.compile(r"https://www\.google\.com/url\?q=([^&\s]+)(?:&[^\s]*)?")


def _unwrap_google_redirects(text: str) -> str:
    """Gmail stores links in a draft as `https://www.google.com/url?q=<link>&source=gmail&...`.
    Only that exact wrapper is unwrapped (percent-decoded) back to `<link>`; nothing else changes."""
    def unwrap(match: re.Match) -> str:
        return re.sub(r"%([0-9A-Fa-f]{2})", lambda escape: chr(int(escape.group(1), 16)), match.group(1))
    return _GOOGLE_REDIRECT_RE.sub(unwrap, text)


def _normalised(text: str) -> str:
    return " ".join(_unwrap_google_redirects(text).split())


def attaches_pdf(cfg: NativeConfig, row: dict) -> bool:
    return cfg.gmail.attach_pdf and row["kind"] == KIND_SUCCESS


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def sends_counted(store: NotificationStore, cfg: NativeConfig, now_iso: str) -> int:
    """Sealed Gmail sends that count against the ceiling now (LIFETIME or the current PER_PERIOD)."""
    _key, start = ceiling_period(cfg, ms_from_iso(now_iso))
    return store.sealed_count() if start is None else store.sealed_count_since(start)


def ceiling_incidents(store: NotificationStore, streams: list[str], period_key: str, now_iso: str) -> int:
    """One FAILURE notification per stream per ceiling period when the Drive ceiling blocks its work.
    The Gmail ceiling never creates a notification (sending one could only hit the same ceiling)."""
    created = 0
    with store.write() as conn:
        for stream in streams:
            created += store._insert(
                conn, f"CEILING|DRIVE_UPLOAD|{stream}|{period_key}", now_iso, kind=KIND_FAILURE, stream=stream,
                status_text="DRIVE_CEILING_REACHED", support_ref="EG_NATIVE_EFFECT_CEILING_REACHED",
            )
    return created


def plan(store: NotificationStore, cfg: NativeConfig, archive_payload, now_iso: str) -> dict | None:
    """The oldest PENDING notification as a NOTIFY_DRAFT packet, or None. Writes only the payload file."""
    pending = store.rows("state='PENDING'")
    if not pending:
        return None
    if sends_counted(store, cfg, now_iso) >= cfg.max_gmail_sends:
        return {"outcome": "NOTIFY_CEILING_REACHED", "pending_notifications": len(pending)}
    row = pending[0]
    subject, body = message_for(cfg, row)
    packet: dict[str, Any] = {
        "outcome": "NOTIFY_DRAFT", "notification_id": row["notification_id"], "kind": row["kind"],
        "stream": row["stream"], "gmail_account": cfg.gmail.account, "to": list(cfg.gmail.to), "cc": [], "bcc": [],
        "subject": subject, "body": body, "attach_pdf": attaches_pdf(cfg, row),
    }
    if attaches_pdf(cfg, row):
        payload = archive_payload(row["invoice_key"])
        if payload is None:
            return {"outcome": "NOTIFY_ATTACHMENT_UNAVAILABLE", "notification_id": row["notification_id"]}
        path = _outbox(cfg) / f"{row['notification_id']}.pdf.b64"
        _write_atomic(path, base64.b64encode(payload))
        packet.update(attachment_filename=row["display_filename"], attachment_mime_type=PDF_MIME,
                      payload_base64_path=str(path))
    return packet


def _evidence_path(cfg: NativeConfig, notification_id: str, draft_id: str) -> Path:
    return _outbox(cfg) / f"{notification_id}.draft.{draft_id}.json"


def draft_evidence(store: NotificationStore, cfg: NativeConfig, notification_id: str, draft_id: str,
                   raw_path: Path) -> dict:
    """Parse the native get_draft RAW result in the Core and write write-once draft evidence."""
    from email import policy
    from email.parser import BytesParser
    from email.utils import getaddresses

    store.row(notification_id)
    if type(draft_id) is not str or not _PROVIDER_ID_RE.fullmatch(draft_id):
        raise NativeRefusal("EG_NATIVE_PROVIDER_ID_INVALID")
    try:
        text = re.sub(r"\s+", "", raw_path.read_text(encoding="ascii"))
        if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", text):
            # urlsafe_b64decode would silently drop foreign characters; refuse them instead.
            raise ValueError("not base64url")
        message = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        body_part = message.get_body(preferencelist=("plain", "html"))
        attachments = []
        for part in message.iter_attachments():
            data = part.get_payload(decode=True) or b""
            attachments.append({"filename": part.get_filename(), "mimeType": part.get_content_type(),
                                "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})

        def addresses(header: str) -> list[str]:
            return [address for _name, address in getaddresses([str(value) for value in message.get_all(header, [])])]

        senders = addresses("From")
        evidence = {
            "notification_id": notification_id, "draft_id": draft_id,
            "from": senders[0] if len(senders) == 1 else None, "to": addresses("To"), "cc": addresses("Cc"),
            "bcc": addresses("Bcc"), "subject": str(message.get("Subject", "")),
            "body": body_part.get_content() if body_part is not None else "", "attachments": attachments,
        }
    except (OSError, UnicodeDecodeError, ValueError, binascii.Error, LookupError, TypeError, AttributeError):
        raise NativeRefusal("EG_NATIVE_DRAFT_RAW_UNDECODABLE")
    path = _evidence_path(cfg, notification_id, draft_id)
    try:
        _write_json_once(path, evidence)
    except FileExistsError:
        if _read_json(path) != evidence:
            raise NativeRefusal("EG_NATIVE_DRAFT_EVIDENCE_CONFLICT")
    return _result("notify-draft-evidence", "PARSED", EXIT_OK, notification_id=notification_id, draft_id=draft_id,
                   attachments=len(attachments))


def seal(store: NotificationStore, cfg: NativeConfig, notification_id: str, draft_id: str, now_iso: str) -> dict:
    """Verify the Core-parsed draft, then SEAL and bind exactly that draft. A wrong draft changes nothing."""
    row = store.row(notification_id)
    if row["state"] != "PENDING":
        raise NativeRefusal("EG_NATIVE_NOTIFICATION_NOT_PENDING")
    evidence = _read_json(_evidence_path(cfg, notification_id, draft_id)) if _PROVIDER_ID_RE.fullmatch(
        draft_id if type(draft_id) is str else "") else None
    if evidence is None or (evidence.get("notification_id"), evidence.get("draft_id")) != (notification_id, draft_id):
        raise NativeRefusal("EG_NATIVE_DRAFT_EVIDENCE_REQUIRED")
    subject, body = message_for(cfg, row)
    sender = evidence.get("from")
    if (type(sender) is not str or sender.lower() != cfg.gmail.account.lower()
            or [str(item).lower() for item in evidence.get("to") or []] != [item.lower() for item in cfg.gmail.to]
            or evidence.get("cc") != [] or evidence.get("bcc") != []
            or evidence.get("subject") != subject):
        raise NativeRefusal("EG_NATIVE_DRAFT_ENVELOPE_MISMATCH")
    # Exact equality after whitespace normalisation and Gmail's link-wrapper unwrap (H-1): appended or
    # prepended text in the draft is a mismatch, not a superset that passes.
    if type(evidence.get("body")) is not str or _normalised(body) != _normalised(evidence["body"]):
        raise NativeRefusal("EG_NATIVE_DRAFT_BODY_MISMATCH")
    attachments = evidence.get("attachments") or []
    if attaches_pdf(cfg, row):
        if (len(attachments) != 1 or attachments[0].get("filename") != row["display_filename"]
                or attachments[0].get("mimeType") != PDF_MIME or attachments[0].get("size") != row["byte_size"]):
            raise NativeRefusal("EG_NATIVE_DRAFT_ATTACHMENT_MISMATCH")
        if attachments[0].get("sha256") != row["sha256"]:
            raise NativeRefusal("EG_NATIVE_DRAFT_BYTES_MISMATCH")
    elif attachments:
        raise NativeRefusal("EG_NATIVE_DRAFT_ATTACHMENT_MISMATCH")
    if sends_counted(store, cfg, now_iso) >= cfg.max_gmail_sends:
        raise NativeRefusal("EG_NATIVE_EFFECT_CEILING_REACHED")
    with store.write() as conn:
        store._move(conn, notification_id, ("PENDING",), "SEALED", now_iso, draft_id=draft_id)
        # Durable seal time in the same transaction: the PER_PERIOD count survives restarts (P-4).
        conn.execute("INSERT INTO gmail_seal (notification_id, sealed_at) VALUES (?,?)", (notification_id, now_iso))
    return _result("notify-seal", "SEALED", EXIT_OK, notification_id=notification_id, send_draft_id=draft_id)


def record(store: NotificationStore, notification_id: str, evidence: dict, now_iso: str) -> dict:
    """Record the outcome of sending the bound draft.

    evidence: {"outcome": "SENT" | "DENIED" | "ERROR" | "NO_RESPONSE", "draft_id", "message_id"}
    SENT for the bound draft: SENT (also resolves UNKNOWN). DENIED: HOLD. ERROR/NO_RESPONSE: UNKNOWN.
    Never resent, and a Gmail failure never notifies about itself.
    """
    row = store.row(notification_id)
    outcome = _provider_outcome(evidence, "SENT")
    if row["state"] == "PENDING":
        raise NativeRefusal("EG_NATIVE_NOT_DISPATCHED")
    if outcome == "SENT":
        message_id = evidence.get("message_id")
        if evidence.get("draft_id") != row["draft_id"]:
            raise NativeRefusal("EG_NATIVE_DRAFT_NOT_BOUND")
        if type(message_id) is not str or not _PROVIDER_ID_RE.fullmatch(message_id):
            raise NativeRefusal("EG_NATIVE_GMAIL_EVIDENCE_INVALID")
        if row["state"] == "SENT":
            if row["message_id"] == message_id:
                return _result("notify-record", "REPLAY", EXIT_OK, notification_id=notification_id)
            raise NativeRefusal("EG_NATIVE_NOTIFICATION_CONFLICT")
        if row["state"] not in ("SEALED", "UNKNOWN"):
            raise NativeRefusal("EG_NATIVE_NOTIFICATION_CONFLICT")
        with store.write() as conn:
            store._move(conn, notification_id, ("SEALED", "UNKNOWN"), "SENT", now_iso, message_id=message_id)
        return _result("notify-record", "SENT", EXIT_OK, notification_id=notification_id)
    target = "HOLD" if outcome == "DENIED" else "UNKNOWN"
    if row["state"] == target:
        return _result("notify-record", "REPLAY", EXIT_HOLD, notification_id=notification_id)
    if row["state"] != "SEALED":
        raise NativeRefusal("EG_NATIVE_NOTIFICATION_CONFLICT")
    with store.write() as conn:
        store._move(conn, notification_id, ("SEALED",), target, now_iso)
    return _result("notify-record", target, EXIT_HOLD, notification_id=notification_id)


def summary(store: NotificationStore) -> dict:
    rows = store.rows()
    counts = {name: sum(1 for row in rows if row["state"] == name) for name in STATES}
    return {
        "counts": counts,
        "items": [{"kind": row["kind"], "stream": row["stream"], "state": row["state"],
                   "status": row["status_text"], "support_ref": row["support_ref"],
                   "display_filename": row["display_filename"]} for row in rows],
    }
