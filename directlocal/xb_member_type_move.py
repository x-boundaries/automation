"""#268: one-off AutoCount member type move, ``Default`` -> ``MEMBER`` or ``GUEST``.

Design: G0 #268 (comment 6659862935, option b: SDK ``GetMember`` -> set
``MemberType`` -> ``SaveMember``), with the owner amendments of 6 Nov 2026.
Attended, one-off, run on the AutoCount VM from its own clone, state root and
config. No scheduled task. Nothing runs on merge.

The pinned rule (owner, 6 Nov 2026)
-----------------------------------
* ``from_type`` is exactly ``Default``. Each member's target is fixed at
  ``snapshot`` from AutoCount's own fields: ``MEMBER`` when the member is
  active (``IsActive`` ``T``) AND ``ExpiryDate`` is later than the snapshot
  time (AutoCount server local time); ``GUEST`` otherwise (inactive,
  ``ExpiryDate`` NULL, or at or before the snapshot time). No third target
  exists (Python constants, ledger CHECKs and the PowerShell child all pin
  it). ``Default`` the type is never edited or deleted.
* Just before ``SaveMember`` the child re-reads the member and saves only if
  the type is still ``Default``, ``LastModified`` equals the snapshot and the
  active flag and ``ExpiryDate`` still give the same target. Otherwise it
  skips with ``not_default_now``, ``changed_since_snapshot`` or
  ``target_changed`` (proven not written: no claim file).
* Only ``MemberType`` is assigned. The read-back compares every other column
  by salted digest (``LastModified``/``LastModifiedUserID`` excepted).
* ``rollback`` (own switch, own owner OK) puts back to ``Default`` only
  members with a ledger ``DONE`` move that are still on the type the tool set
  and whose ``LastModified`` equals the tool's post-save value.

Safety
------
* Every write switch defaults OFF (``type_move_enabled``,
  ``rollback_enabled``); refused before any login. At most
  ``max_moves_per_run`` saves per run (default 10, hard max 500) and
  ``max_move_runs_per_day`` move runs per rolling 24 hours (default 2).
* A ``STOP`` file in the state root is checked before every member.
* Every save is recorded (``INTENT``) before its child starts, and has one
  child, one ``SaveMember`` and is never retried in the same run. Anything
  but a clean ``DONE`` or a pre-check skip stops the run.
* ``UNCERTAIN`` is resolved by read-back only (``resolve``), never by a
  second save.
* The tool refuses the #155 sync's config, state root and ledger.
* The ledger (``member_type_move.sqlite3``, private state root) keeps
  MemberNos, codes and timestamps. Output carries counts and codes; MemberNos
  appear only masked to the last 3 characters, and only with
  ``--show-masked`` (VM console).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.append(str(_HERE))

import xb_directlocal as dl  # noqa: E402
import xb_member_sync as sync  # noqa: E402

CONFIG_SCHEMA = "xb.directlocal.member_type_move.config.v1"
LEDGER_FILE = "member_type_move.sqlite3"
LOCK_FILE = "member_type_move.lock"
STOP_FILE = "STOP"
TOOL = "xb268_member_type_move"
SCHEMA_VERSION = 1

FROM_TYPE = "Default"
MEMBER = "MEMBER"
GUEST = "GUEST"
TARGET_TYPES = (MEMBER, GUEST)
SCAN_TYPES = (FROM_TYPE, MEMBER, GUEST)
PROFILE = "type_move_p1"
BRIDGE_SCRIPT = dl.REPO_ROOT / "scripts" / "ac2_member_type_move_bridge.ps1"

MAX_MOVES_PER_RUN_LIMIT = 500
SCAN_PAGE_SIZE = 500  # the child's pinned TOP 500
SCAN_MAX_PAGES = 200
MOVE_SKIP_CODES = frozenset({"not_default_now", "changed_since_snapshot", "target_changed", "member_not_found"})
ROLLBACK_SKIP_CODES = frozenset({"not_moved_type_now", "changed_since_move", "member_not_found"})
# Child codes of the type move's own scripts; the adapter's and the book
# guard's come from the #155 allowlist (dl.BRIDGE_CHILD_ERROR_CODES).
TYPE_MOVE_CHILD_CODES = frozenset({
    "type_move_request_invalid", "type_move_type_not_allowed", "type_move_scan_failed",
    "type_move_counts_failed", "type_move_member_read_failed",
})
CHILD_ERROR_CODES = dl.BRIDGE_CHILD_ERROR_CODES | TYPE_MOVE_CHILD_CODES
# Child refusals before the claim that the run reports by name.
REFUSALS_BEFORE_CLAIM = frozenset({"member_type_unknown", "production_book_not_authorised", "type_move_type_not_allowed", "type_move_request_invalid"})

_LOCAL_RE = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,3}))?$")
_SERVER_NOW_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$")
_LAST_MODIFIED_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}$")
_COLUMN_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

_CONFIG_REQUIRED = {"schema", "state_root", "autocount_book"}
_CONFIG_DEFAULTS: dict[str, Any] = {
    "type_move_enabled": False,
    "rollback_enabled": False,
    # G0 section 7: pilot 10, then 250 per batch, at most 2 batches per evening.
    "max_moves_per_run": 10,
    "max_move_runs_per_day": 2,
    "write_deadline_seconds": 120,
    # As the #155 sync: the AutoCount login comes from a Credential Manager
    # entry (live: the sync's own entry, in the mode the config states).
    "ac2_credential_target": None,
    "ac2_login_mode": None,
    "ac2_refused_user_ids": [],
}
_BOUNDS = {
    "max_moves_per_run": (1, MAX_MOVES_PER_RUN_LIMIT),
    "max_move_runs_per_day": (1, 10),
    "write_deadline_seconds": (30, 600),
}


@dataclass(frozen=True)
class MoveConfig:
    state_root: str
    autocount_book: str
    type_move_enabled: bool
    rollback_enabled: bool
    max_moves_per_run: int
    max_move_runs_per_day: int
    write_deadline_seconds: int
    ac2_credential_target: str | None
    ac2_login_mode: str | None
    ac2_refused_user_ids: tuple[str, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "autocount_book": self.autocount_book, "from_type": FROM_TYPE, "target_types": list(TARGET_TYPES),
            "type_move_enabled": self.type_move_enabled, "rollback_enabled": self.rollback_enabled,
            "max_moves_per_run": self.max_moves_per_run, "max_move_runs_per_day": self.max_move_runs_per_day,
            "write_deadline_seconds": self.write_deadline_seconds,
            "ac2_credential": "credential_manager" if self.ac2_credential_target else "owner_console_environment",
            "ac2_login_mode": self.ac2_login_mode, "ac2_refused_user_ids_count": len(self.ac2_refused_user_ids),
        }


def _bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise dl.DirectLocalError(f"type_move_config_{key}_invalid")
    return value


def parse_config(value: Any) -> MoveConfig:
    if not isinstance(value, dict):
        raise dl.DirectLocalError("type_move_config_invalid")
    if value.get("schema") == sync.CONFIG_SCHEMA:
        raise dl.DirectLocalError("type_move_config_is_member_sync")
    keys = set(value)
    if not _CONFIG_REQUIRED <= keys or not keys <= _CONFIG_REQUIRED | set(_CONFIG_DEFAULTS):
        raise dl.DirectLocalError("type_move_config_keys_invalid")
    if value["schema"] != CONFIG_SCHEMA:
        raise dl.DirectLocalError("type_move_config_schema_invalid")
    merged = {**_CONFIG_DEFAULTS, **value}
    book = sync.check_book(merged["autocount_book"])
    if not isinstance(merged["state_root"], str) or not merged["state_root"]:
        raise dl.DirectLocalError("type_move_config_state_root_invalid")
    numbers = {}
    for key, (low, high) in _BOUNDS.items():
        number = merged[key]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise dl.DirectLocalError(f"type_move_config_{key}_invalid")
        numbers[key] = number
    live = book == sync.LIVE_BOOK
    target = merged["ac2_credential_target"]
    if target is not None and (not isinstance(target, str) or not sync.CREDENTIAL_TARGET_RE.fullmatch(target)):
        raise dl.DirectLocalError("type_move_config_ac2_credential_target_invalid")
    if live and target != sync.LIVE_CREDENTIAL_TARGET:
        raise dl.DirectLocalError("type_move_config_ac2_credential_target_live_required")
    if not live and target is not None and target.strip().casefold() == sync.LIVE_CREDENTIAL_TARGET.casefold():
        raise dl.DirectLocalError("type_move_config_ac2_credential_target_live_only")
    login_mode = merged["ac2_login_mode"]
    if login_mode is not None and login_mode not in sync.AC2_LOGIN_MODES:
        raise dl.DirectLocalError("type_move_config_ac2_login_mode_invalid")
    if live and login_mode is None:
        raise dl.DirectLocalError("type_move_config_ac2_login_mode_live_required")
    refused = merged["ac2_refused_user_ids"]
    if not isinstance(refused, list) or len(refused) > 10 or any(not isinstance(u, str) or not sync.AC2_USER_ID_RE.fullmatch(u) for u in refused):
        raise dl.DirectLocalError("type_move_config_ac2_refused_user_ids_invalid")
    if login_mode == "owner_shared" and refused:
        raise dl.DirectLocalError("type_move_config_ac2_refused_user_ids_owner_shared_refused")
    if live and login_mode == "dedicated" and not refused:
        raise dl.DirectLocalError("type_move_config_ac2_refused_user_ids_live_required")
    return MoveConfig(
        state_root=merged["state_root"], autocount_book=book,
        type_move_enabled=_bool(merged["type_move_enabled"], "type_move_enabled"),
        rollback_enabled=_bool(merged["rollback_enabled"], "rollback_enabled"),
        ac2_credential_target=target, ac2_login_mode=login_mode, ac2_refused_user_ids=tuple(refused), **numbers,
    )


def load_config(path: str | Path) -> MoveConfig:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise dl.DirectLocalError("type_move_config_unreadable") from None
    if len(raw) > 65_536:
        raise dl.DirectLocalError("type_move_config_invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise dl.DirectLocalError("type_move_config_invalid") from None
    return parse_config(value)


def state_root(config: MoveConfig) -> Path:
    """The tool's own private root: never one holding the #155 sync ledger or
    the DirectLocal Core ledger."""

    root = dl.validate_state_root(config.state_root)
    if (root / sync.LEDGER_FILE).exists() or (root / "ledger.sqlite3").exists():
        raise dl.DirectLocalError("type_move_state_root_shared")
    dl.verify_private_state(root)
    (root / "claims").mkdir(exist_ok=True)
    return root


# --------------------------------------------------------------------------
# The pinned rule (pure)
# --------------------------------------------------------------------------

def parse_local(value: Any) -> datetime | None:
    """AutoCount server local time from ``CONVERT(varchar(23), x, 126)``
    (fraction optional); None when absent or not that shape."""

    match = _LOCAL_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    try:
        base = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return base.replace(microsecond=int((match.group(2) or "0").ljust(3, "0")) * 1000)


def canonical_last_modified(value: Any) -> str | None:
    """The child's form of LastModified: ``yyyy-MM-ddTHH:mm:ss.fff``."""

    parsed = parse_local(value)
    return None if parsed is None else parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}"


def target_for(is_active: Any, expiry_local: Any, snapshot_at: datetime) -> tuple[str | None, str | None]:
    """``(target, None)`` by the pinned rule, or ``(None, hold_code)`` when
    the row cannot be classified (never moved by the tool)."""

    if is_active == "T":
        active = True
    elif is_active == "F":
        active = False
    else:
        return None, "active_flag_unexpected"
    if not active or expiry_local is None:
        return GUEST, None
    expiry = parse_local(expiry_local)
    if expiry is None:
        return None, "expiry_unparseable"
    return (MEMBER if expiry > snapshot_at else GUEST), None


def classify_row(row: Mapping[str, Any], snapshot_at: datetime) -> dict[str, Any]:
    member_no = row["member_no"]
    last_modified = canonical_last_modified(row.get("last_modified_local"))
    target, hold = target_for(row.get("is_active"), row.get("expiry_local"), snapshot_at)
    if not dl.MEMBER_NO_RE.fullmatch(member_no):
        target, hold = None, "member_no_unsupported"
    elif row.get("last_modified_local") is None:
        target, hold = None, "last_modified_missing"
    elif last_modified is None:
        target, hold = None, "last_modified_unparseable"
    return {
        "member_no": member_no, "is_active": row.get("is_active") if row.get("is_active") in ("T", "F") else None,
        "expiry_local": row.get("expiry_local") if isinstance(row.get("expiry_local"), str) else None,
        "last_modified_local": last_modified, "target": target, "hold_code": hold,
    }


def mask(member_no: str) -> str:
    return sync.mask_last3(member_no)


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

LEDGER_SCHEMA = """
CREATE TABLE meta(
  id INTEGER PRIMARY KEY CHECK (id = 1), tool TEXT NOT NULL CHECK (tool = 'xb268_member_type_move'),
  ledger_id TEXT NOT NULL, autocount_book TEXT NOT NULL CHECK (autocount_book IN ('AED_TESTINGAPI', 'AED_XBOUNDARIES')),
  from_type TEXT NOT NULL CHECK (from_type = 'Default'), created_at TEXT NOT NULL);
CREATE TABLE snapshot(
  id INTEGER PRIMARY KEY CHECK (id = 1), taken_at TEXT NOT NULL, snapshot_at_local TEXT NOT NULL,
  members INTEGER NOT NULL, type_counts TEXT NOT NULL);
CREATE TABLE snapshot_rows(
  member_no TEXT PRIMARY KEY, is_active TEXT CHECK (is_active IN ('T', 'F')), expiry_local TEXT, last_modified_local TEXT,
  target TEXT CHECK (target IN ('MEMBER', 'GUEST')), hold_code TEXT,
  CHECK ((target IS NOT NULL AND hold_code IS NULL AND last_modified_local IS NOT NULL) OR (target IS NULL AND hold_code IS NOT NULL)));
CREATE TABLE moves(
  move_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('move', 'rollback')),
  member_no TEXT NOT NULL, from_type TEXT NOT NULL, to_type TEXT NOT NULL,
  expected_last_modified_local TEXT NOT NULL, salt TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('INTENT', 'DONE', 'SKIPPED', 'NOT_WRITTEN', 'UNCERTAIN', 'HELD')),
  code TEXT, post_last_modified_local TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK ((kind = 'move' AND from_type = 'Default' AND to_type IN ('MEMBER', 'GUEST'))
      OR (kind = 'rollback' AND from_type IN ('MEMBER', 'GUEST') AND to_type = 'Default')),
  CHECK (state <> 'DONE' OR post_last_modified_local IS NOT NULL));
CREATE TABLE runs(run_id TEXT PRIMARY KEY, command TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, summary TEXT);
CREATE TRIGGER meta_immutable BEFORE UPDATE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER meta_no_delete BEFORE DELETE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER snapshot_immutable BEFORE UPDATE ON snapshot BEGIN SELECT RAISE(ABORT, 'snapshot_immutable'); END;
CREATE TRIGGER snapshot_no_delete BEFORE DELETE ON snapshot BEGIN SELECT RAISE(ABORT, 'snapshot_immutable'); END;
CREATE TRIGGER snapshot_rows_immutable BEFORE UPDATE ON snapshot_rows BEGIN SELECT RAISE(ABORT, 'snapshot_immutable'); END;
CREATE TRIGGER snapshot_rows_no_delete BEFORE DELETE ON snapshot_rows BEGIN SELECT RAISE(ABORT, 'snapshot_immutable'); END;
CREATE TRIGGER moves_no_delete BEFORE DELETE ON moves BEGIN SELECT RAISE(ABORT, 'move_immutable'); END;
CREATE TRIGGER moves_binding_immutable BEFORE UPDATE OF move_id, run_id, kind, member_no, from_type, to_type, expected_last_modified_local, salt, created_at ON moves
  BEGIN SELECT RAISE(ABORT, 'move_immutable'); END;
CREATE TRIGGER moves_transition BEFORE UPDATE OF state ON moves
  WHEN NOT ((OLD.state = 'INTENT' AND NEW.state IN ('DONE', 'SKIPPED', 'NOT_WRITTEN', 'UNCERTAIN', 'HELD'))
         OR (OLD.state = 'UNCERTAIN' AND NEW.state IN ('DONE', 'NOT_WRITTEN', 'HELD')))
  BEGIN SELECT RAISE(ABORT, 'move_transition_invalid'); END;
"""


class MoveLedger:
    """Single-owner SQLite ledger in the tool's own private root."""

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = dl.utc_now):
        self.root = Path(root)
        self.clock = clock
        self._lock = dl._OwnerLock(self.root / LOCK_FILE)
        try:
            self.conn = sqlite3.connect(self.root / LEDGER_FILE, isolation_level=None, timeout=0)
            self.conn.row_factory = sqlite3.Row
            for pragma in ("journal_mode=WAL", "synchronous=FULL", "foreign_keys=ON", "trusted_schema=OFF"):
                self.conn.execute(f"PRAGMA {pragma}")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if version == 0 and not tables:
                with self.tx() as conn:
                    for statement in dl._split_sql(LEDGER_SCHEMA):
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION or "moves" not in tables:
                # A #155 sync ledger (or anything else) under this name.
                raise dl.DirectLocalError("type_move_ledger_not_recognised")
        except BaseException:
            self._lock.release()
            raise

    def now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise dl.DirectLocalError("clock_must_be_timezone_aware")
        return value.astimezone(timezone.utc)

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self._lock.release()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def init_meta(self, autocount_book: str) -> dict[str, Any]:
        sync.check_book(autocount_book)
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
            if row is not None:
                if row["autocount_book"] != autocount_book or row["tool"] != TOOL:
                    raise dl.DirectLocalError("type_move_ledger_binding_mismatch")
                return {"status": "existing"}
            conn.execute("INSERT INTO meta(id, tool, ledger_id, autocount_book, from_type, created_at) VALUES (1, ?, ?, ?, ?, ?)",
                         (TOOL, f"tm-{uuid.uuid4().hex}", autocount_book, FROM_TYPE, dl.ts(self.now())))
        return {"status": "initialised"}

    def check_binding(self, config: MoveConfig) -> None:
        row = self.conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        if row is None:
            raise dl.DirectLocalError("type_move_ledger_not_initialised")
        if row["autocount_book"] != sync.check_book(config.autocount_book) or row["tool"] != TOOL:
            raise dl.DirectLocalError("type_move_ledger_binding_mismatch")

    def snapshot(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM snapshot WHERE id = 1").fetchone()

    def start_run(self, command: str) -> str:
        run_id = f"run-{uuid.uuid4().hex}"
        with self.tx() as conn:
            conn.execute("INSERT INTO runs(run_id, command, started_at) VALUES (?, ?, ?)", (run_id, command, dl.ts(self.now())))
        return run_id

    def finish_run(self, run_id: str, summary: Mapping[str, Any]) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE run_id = ?", (dl.ts(self.now()), json.dumps(summary, sort_keys=True), run_id))

    def runs_since(self, command: str, since: datetime) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM runs WHERE command = ? AND started_at >= ?", (command, dl.ts(since))).fetchone()[0]

    def add_intent(self, *, run_id: str, kind: str, member_no: str, from_type: str, to_type: str, expected: str) -> sqlite3.Row:
        move_id = f"move-{uuid.uuid4().hex}"
        now = dl.ts(self.now())
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO moves(move_id, run_id, kind, member_no, from_type, to_type, expected_last_modified_local, salt, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'INTENT', ?, ?)",
                (move_id, run_id, kind, member_no, from_type, to_type, expected, secrets.token_hex(16), now, now),
            )
        return self.conn.execute("SELECT * FROM moves WHERE move_id = ?", (move_id,)).fetchone()

    def set_outcome(self, move_id: str, state: str, code: str | None, post_last_modified: str | None = None) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE moves SET state = ?, code = ?, post_last_modified_local = ?, updated_at = ? WHERE move_id = ?",
                         (state, code, post_last_modified, dl.ts(self.now()), move_id))

    def open_moves(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM moves WHERE state IN ('INTENT', 'UNCERTAIN') ORDER BY rowid").fetchall()

    def move_candidates(self) -> Iterator[sqlite3.Row]:
        """Snapshot members with a target and no move attempt that settled
        or may have landed (a NOT_WRITTEN one is eligible again)."""

        yield from self.conn.execute(
            "SELECT * FROM snapshot_rows s WHERE s.target IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM moves m WHERE m.member_no = s.member_no AND m.kind = 'move' AND m.state <> 'NOT_WRITTEN') ORDER BY s.member_no"
        ).fetchall()

    def rollback_candidates(self) -> list[sqlite3.Row]:
        """DONE moves with no rollback attempt that settled or may have
        landed, latest first (insertion order: timestamps are per second)."""

        return self.conn.execute(
            "SELECT * FROM moves m WHERE m.kind = 'move' AND m.state = 'DONE' AND NOT EXISTS ("
            "SELECT 1 FROM moves r WHERE r.member_no = m.member_no AND r.kind = 'rollback' AND r.state <> 'NOT_WRITTEN') "
            "ORDER BY m.rowid DESC"
        ).fetchall()


# --------------------------------------------------------------------------
# Bridge client
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SaveOutcome:
    state: str  # DONE, SKIPPED, NOT_WRITTEN, UNCERTAIN or HELD
    code: str | None
    post_last_modified_local: str | None = None
    changed_fields: tuple[str, ...] = ()


def failure_code(stdout: bytes, returncode: int | None = None) -> str:
    """dl.bridge_failure_code with the type move's codes added."""

    line, count = dl._last_output_line(stdout)
    if line is not None:
        try:
            envelope = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            envelope = None
        if isinstance(envelope, dict) and set(envelope) == {"error"} and envelope["error"] in CHILD_ERROR_CODES:
            return f"bridge_read_failed:{envelope['error']}"
    return dl.bridge_failure_code(stdout, returncode)


def child_refusal(stdout: Any) -> str | None:
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8", "replace")
    code = failure_code(stdout) if isinstance(stdout, bytes) else ""
    code = code.removeprefix("bridge_read_failed:")
    return code if code in CHILD_ERROR_CODES else None


def classify_save(run: dl.WriteRun, *, claim_exists: bool, kind: str) -> SaveOutcome:
    """Outcome of one type-move-save child. Absence of the claim file is the
    only proof that SaveMember was never reached."""

    if not run.started:
        return SaveOutcome("UNCERTAIN", "claim_without_writer_start") if claim_exists else SaveOutcome("NOT_WRITTEN", "writer_start_failed")
    if run.timed_out:
        if not run.exited:
            return SaveOutcome("UNCERTAIN", "writer_timeout_exit_unconfirmed")
        return SaveOutcome("UNCERTAIN", "writer_timeout") if claim_exists else SaveOutcome("NOT_WRITTEN", "writer_timeout_before_claim")
    if run.exit_code == 3:
        return SaveOutcome("UNCERTAIN", "fence_already_claimed")
    if run.exit_code != 0:
        if claim_exists:
            return SaveOutcome("UNCERTAIN", "writer_failed_after_claim")
        refusal = child_refusal(run.stdout)
        return SaveOutcome("NOT_WRITTEN", refusal if refusal in REFUSALS_BEFORE_CLAIM else "writer_failed_before_claim")
    try:
        outcome = json.loads(run.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError, RecursionError):
        outcome = None
    if not isinstance(outcome, dict):
        return SaveOutcome("UNCERTAIN", "writer_outcome_invalid")
    if outcome.get("status") == "skipped":
        skips = MOVE_SKIP_CODES if kind == "move" else ROLLBACK_SKIP_CODES
        if set(outcome) == {"status", "code"} and outcome["code"] in skips and not claim_exists:
            return SaveOutcome("SKIPPED", outcome["code"])
        return SaveOutcome("UNCERTAIN", "writer_outcome_invalid")
    changed = outcome.get("changed_fields")
    changed = [changed] if isinstance(changed, str) else changed  # one-item arrays may arrive flattened
    post = outcome.get("post_last_modified_local")
    valid = (
        outcome.get("status") == "saved" and outcome.get("save_invocation_count") == 1 and claim_exists
        and all(isinstance(outcome.get(k), bool) for k in ("readback_found", "readback_type_match", "fields_unchanged", "outbound_queue_changed"))
        and isinstance(changed, list) and all(isinstance(c, str) and _COLUMN_RE.fullmatch(c) for c in changed)
        and (post is None or (isinstance(post, str) and _LAST_MODIFIED_RE.fullmatch(post)))
    )
    if not valid:
        return SaveOutcome("UNCERTAIN", "writer_outcome_invalid")
    fields = tuple(sorted(changed))[:20]
    if not outcome["readback_found"]:
        return SaveOutcome("UNCERTAIN", "readback_absent")
    if not outcome["readback_type_match"]:
        return SaveOutcome("HELD", "readback_type_mismatch", post, fields)
    if not outcome["fields_unchanged"]:
        return SaveOutcome("HELD", "readback_fields_changed", post, fields)
    if outcome["outbound_queue_changed"]:
        return SaveOutcome("HELD", "outbound_queue_changed", post, fields)
    if post is None:
        return SaveOutcome("HELD", "readback_last_modified_missing", None, fields)
    return SaveOutcome("DONE", None, post, fields)


class TypeMoveBridgeClient(dl.BridgeClient):
    """ac2_member_type_move_bridge.ps1: three reads and the fenced save."""

    def __init__(self, command: list[str], *, autocount_book: str | None, claims: Path, env: Mapping[str, str] | None = None,
                 deadline_seconds: int = dl.WRITER_DEADLINE_SECONDS, **kwargs: Any):
        super().__init__(command, autocount_book=None if autocount_book is None else sync.check_book(autocount_book), **kwargs)
        self.claims = Path(claims)
        self.deadline_seconds = deadline_seconds
        self._env = None if env is None else dict(env)

    def __repr__(self) -> str:
        return "TypeMoveBridgeClient()"

    def _start(self, arguments: list[str]):
        book = ["-TargetBook", self.autocount_book] if self.autocount_book else []
        return self._popen(
            self.command + arguments + book, stdin=dl.subprocess.PIPE, stdout=dl.subprocess.PIPE, stderr=dl.subprocess.PIPE,
            creationflags=getattr(dl.subprocess, "CREATE_NO_WINDOW", 0), env=self._env,
        )

    def _read(self, operation: str, value: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            process = self._start(["-Operation", operation, "-EnableProductionAdapter"])
        except OSError:
            raise dl.BridgeError("bridge_start_failed") from None
        try:
            stdout, _ = process.communicate(self._stdin(value), timeout=self.read_timeout_seconds)
        except dl.subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise dl.BridgeError("bridge_read_timeout") from None
        if process.returncode != 0:
            raise dl.BridgeError(failure_code(stdout, process.returncode))
        line, _ = dl._last_output_line(stdout)
        try:
            result = json.loads(line.decode("utf-8")) if line is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise dl.BridgeError("bridge_output_invalid") from None
        if not isinstance(result, dict):
            raise dl.BridgeError("bridge_output_invalid")
        return result

    def scan(self, member_type: str, after_member_no: str) -> list[dict[str, Any]]:
        if member_type not in SCAN_TYPES:
            raise dl.DirectLocalError("type_move_type_not_allowed")
        rows = self._read("type-move-scan", {"member_type": member_type, "after_member_no": after_member_no}).get("members")
        rows = [rows] if isinstance(rows, dict) else rows
        if not isinstance(rows, list) or len(rows) > SCAN_PAGE_SIZE:
            raise dl.BridgeError("bridge_output_invalid")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("member_no"), str) or not row["member_no"]:
                raise dl.BridgeError("bridge_output_invalid")
        return rows

    def counts(self) -> dict[str, Any]:
        result = dict(self._read("type-move-counts", {}))
        counts = result.get("counts")
        result["counts"] = [counts] if isinstance(counts, dict) else (counts or [])
        outlets = result.get("outlets")
        result["outlets"] = [outlets] if isinstance(outlets, dict) else (outlets or [])
        types = result.get("types_exist")
        if (not isinstance(result["counts"], list) or not isinstance(types, dict) or set(types) != set(SCAN_TYPES)
                or not all(isinstance(v, bool) for v in types.values()) or not isinstance(result.get("server_now_local"), str)
                or not _SERVER_NOW_RE.fullmatch(result["server_now_local"])):
            raise dl.BridgeError("bridge_output_invalid")
        return result

    def member(self, member_no: str, salt: str) -> dict[str, Any]:
        result = dict(self._read("type-move-member", {"member_no": member_no, "salt": salt}))
        if not isinstance(result.get("found"), bool) or (result["found"] and not (
                isinstance(result.get("member_type"), str) and isinstance(result.get("fields_digest"), str) and _DIGEST_RE.fullmatch(result["fields_digest"]))):
            raise dl.BridgeError("bridge_output_invalid")
        return result

    def save(self, payload: Mapping[str, Any], *, move_id: str) -> SaveOutcome:
        fence_id = "fence-" + move_id.removeprefix("move-")
        salt = secrets.token_hex(16)
        text = dl.canonical_payload(payload)
        stdin = {"fence_id": fence_id, "payload_canonical": text, "payload_digest": dl.payload_digest(salt, text), "digest_salt": salt}
        deadline = (dl.utc_now() + timedelta(seconds=self.deadline_seconds)).replace(microsecond=0)
        arguments = ["-Operation", "type-move-save", "-EnableProductionAdapter", "-FenceId", fence_id,
                     "-ClaimDirectory", str(self.claims), "-WriteDeadlineUtc", deadline.strftime("%Y-%m-%dT%H:%M:%SZ")]
        run = self._write(arguments, stdin, wait_seconds=self.deadline_seconds + dl.WRITER_GRACE_SECONDS)
        return classify_save(run, claim_exists=(self.claims / f"{fence_id}.claim").exists(), kind=str(payload.get("kind")))

    def _write(self, arguments: list[str], stdin: Mapping[str, Any], *, wait_seconds: float) -> dl.WriteRun:
        try:
            process = self._start(arguments)
        except OSError:
            return dl.WriteRun(started=False)
        try:
            stdout, _ = process.communicate(self._stdin(stdin), timeout=max(wait_seconds, 1))
        except dl.subprocess.TimeoutExpired:
            process.kill()
            try:
                process.communicate(timeout=10)
            except dl.subprocess.TimeoutExpired:
                return dl.WriteRun(started=True, timed_out=True, exited=False)
            return dl.WriteRun(started=True, exit_code=process.returncode, timed_out=True, exited=True)
        return dl.WriteRun(started=True, exit_code=process.returncode, stdout=stdout.decode("utf-8", "replace")[-4096:])


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

def _zero_split() -> dict[str, dict[str, int]]:
    return {MEMBER: {"active": 0, "inactive": 0}, GUEST: {"active": 0, "inactive": 0}}


def _bump(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


class TypeMoveEngine:
    def __init__(self, config: MoveConfig, bridge: Any, *, ledger: MoveLedger | None = None, root: Path | None = None):
        self.config = config
        self.bridge = bridge
        self.ledger = ledger
        self.root = Path(root) if root is not None else (ledger.root if ledger is not None else None)

    # -- reads ----------------------------------------------------------------
    def scan_all(self, member_type: str) -> list[dict[str, Any]]:
        # Pages follow the book's own MemberNo order (its collation, not
        # Python's); a repeated MemberNo or a cursor that does not move stops.
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        after = ""
        for _ in range(SCAN_MAX_PAGES):
            page = self.bridge.scan(member_type, after)
            for row in page:
                if row["member_no"] in seen:
                    raise dl.DirectLocalError("type_move_scan_order_invalid")
                seen.add(row["member_no"])
            rows.extend(page)
            if len(page) < SCAN_PAGE_SIZE:
                return rows
            after = page[-1]["member_no"]
        raise dl.DirectLocalError("type_move_scan_too_many_pages")

    def counts(self) -> dict[str, Any]:
        return self.bridge.counts()

    @staticmethod
    def type_counts(counts: Mapping[str, Any]) -> list[dict[str, Any]]:
        out = []
        for row in counts["counts"]:
            if not isinstance(row, dict):
                raise dl.BridgeError("bridge_output_invalid")
            out.append({"member_type": row.get("member_type"), "is_active": row.get("is_active"), "members": row.get("members")})
        return out

    def _require_types(self, counts: Mapping[str, Any]) -> None:
        if not all(counts["types_exist"][t] for t in SCAN_TYPES):
            raise dl.DirectLocalError("member_type_unknown")

    def _stop_requested(self) -> bool:
        return self.root is not None and (self.root / STOP_FILE).exists()

    # -- plan (read-only) -----------------------------------------------------
    def plan(self, *, show_masked: bool = False) -> dict[str, Any]:
        counts = self.counts()
        snapshot_at = parse_local(counts["server_now_local"])
        rows = [classify_row(row, snapshot_at) for row in self.scan_all(FROM_TYPE)]
        result = self._target_summary(rows)
        result.update({"status": "ok", "command": "plan", "writes": 0, "server_now_local": counts["server_now_local"],
                       "types_exist": counts["types_exist"], "type_counts": self.type_counts(counts)})
        if show_masked:
            result["masked"] = self._masked_by_target(rows)
        return result

    @staticmethod
    def _target_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        split = _zero_split()
        held: dict[str, int] = {}
        for row in rows:
            if row["target"] is None:
                _bump(held, row["hold_code"])
            else:
                split[row["target"]]["active" if row["is_active"] == "T" else "inactive"] += 1
        return {"default_members": len(rows), "by_target": split, "held": held}

    @staticmethod
    def _masked_by_target(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {MEMBER: [], GUEST: [], "held": []}
        for row in rows:
            out[row["target"] or "held"].append(mask(row["member_no"]))
        return out

    # -- snapshot -------------------------------------------------------------
    def snapshot(self, *, show_masked: bool = False) -> dict[str, Any]:
        ledger = self._ledger()
        if ledger.snapshot() is not None:
            raise dl.DirectLocalError("type_move_snapshot_exists")
        counts = self.counts()
        self._require_types(counts)
        snapshot_at = parse_local(counts["server_now_local"])
        rows = [classify_row(row, snapshot_at) for row in self.scan_all(FROM_TYPE)]
        if len({row["member_no"] for row in rows}) != len(rows):
            raise dl.DirectLocalError("type_move_scan_duplicate_member")
        with ledger.tx() as conn:
            conn.execute("INSERT INTO snapshot(id, taken_at, snapshot_at_local, members, type_counts) VALUES (1, ?, ?, ?, ?)",
                         (dl.ts(ledger.now()), counts["server_now_local"], len(rows), json.dumps(self.type_counts(counts), sort_keys=True)))
            conn.executemany(
                "INSERT INTO snapshot_rows(member_no, is_active, expiry_local, last_modified_local, target, hold_code) VALUES (?, ?, ?, ?, ?, ?)",
                [(r["member_no"], r["is_active"], r["expiry_local"], r["last_modified_local"], r["target"], r["hold_code"]) for r in rows],
            )
        result = self._target_summary(rows)
        result.update({"status": "ok", "command": "snapshot", "writes": 0, "snapshot_at_local": counts["server_now_local"]})
        if show_masked:
            result["masked"] = self._masked_by_target(rows)
        return result

    def _ledger(self) -> MoveLedger:
        if self.ledger is None:
            raise dl.DirectLocalError("type_move_ledger_not_initialised")
        self.ledger.check_binding(self.config)
        return self.ledger

    def _refuse_open_moves(self) -> None:
        if self._ledger().open_moves():
            raise dl.DirectLocalError("type_move_uncertain_unresolved")

    # -- move -----------------------------------------------------------------
    def move(self, *, max_moves: int | None = None, show_masked: bool = False) -> dict[str, Any]:
        if not self.config.type_move_enabled:
            raise dl.DirectLocalError("type_move_disabled")
        ledger = self._ledger()
        snapshot = ledger.snapshot()
        if snapshot is None:
            raise dl.DirectLocalError("type_move_snapshot_missing")
        cap = self._cap(max_moves)
        self._refuse_open_moves()
        if ledger.runs_since("move", ledger.now() - timedelta(hours=24)) >= self.config.max_move_runs_per_day:
            raise dl.DirectLocalError("type_move_runs_per_day_reached")
        run_id = ledger.start_run("move")
        summary: dict[str, Any] = {"status": "ok", "command": "move", "run_id": run_id, "cap": cap, "saves": 0, "moved": _zero_split(),
                                   "skipped": {}, "stopped": None, "alerts": [], "changed_fields": []}
        masked: dict[str, list[str]] = {"moved": [], "skipped": [], "stopped": []}
        try:
            before = self.counts()
            self._require_types(before)
            summary["member_change_counter_before"] = before.get("member_change_counter")
            for row in ledger.move_candidates():
                if summary["saves"] >= cap:
                    break
                if self._stop_requested():
                    summary["stopped"] = "stop_file_present"
                    break
                intent = ledger.add_intent(run_id=run_id, kind="move", member_no=row["member_no"], from_type=FROM_TYPE,
                                           to_type=row["target"], expected=row["last_modified_local"])
                payload = {"kind": "move", "member_no": row["member_no"], "from_type": FROM_TYPE, "to_type": row["target"],
                           "expected_last_modified_local": row["last_modified_local"], "snapshot_at_local": snapshot["snapshot_at_local"], "salt": intent["salt"]}
                outcome = self._save(intent, payload)
                if outcome.state == "DONE":
                    summary["saves"] += 1
                    summary["moved"][row["target"]]["active" if row["is_active"] == "T" else "inactive"] += 1
                    masked["moved"].append(mask(row["member_no"]))
                elif outcome.state == "SKIPPED":
                    _bump(summary["skipped"], outcome.code)
                    masked["skipped"].append(mask(row["member_no"]))
                else:
                    summary["stopped"] = f"{outcome.state.lower()}:{outcome.code}"
                    summary["alerts"].append(summary["stopped"])
                    summary["changed_fields"] = list(outcome.changed_fields)
                    masked["stopped"].append(mask(row["member_no"]))
                    break
            self._after_batch(summary)
        except dl.DirectLocalError as exc:
            summary["stopped"] = exc.code
            summary["alerts"].append(exc.code)
        finally:
            ledger.finish_run(run_id, summary)
        if show_masked:
            summary["masked"] = masked
        return summary

    def _cap(self, max_moves: int | None) -> int:
        if max_moves is None:
            return self.config.max_moves_per_run
        if isinstance(max_moves, bool) or not isinstance(max_moves, int) or not 1 <= max_moves <= self.config.max_moves_per_run:
            raise dl.DirectLocalError("type_move_max_invalid")
        return max_moves

    def _save(self, intent: sqlite3.Row, payload: Mapping[str, Any]) -> SaveOutcome:
        try:
            outcome = self.bridge.save(payload, move_id=intent["move_id"])
        except Exception:  # noqa: BLE001 - never a blind retry: the child may have saved
            outcome = SaveOutcome("UNCERTAIN", "save_call_failed")
        self.ledger.set_outcome(intent["move_id"], outcome.state, outcome.code, outcome.post_last_modified_local if outcome.state == "DONE" else None)
        return outcome

    def _after_batch(self, summary: dict[str, Any]) -> None:
        """Per-batch read-back: the batch end time (server local, for the
        outlets' sync check in reconcile) and the change counter."""

        try:
            after = self.counts()
        except dl.DirectLocalError:
            summary["alerts"].append("batch_readback_unavailable")
            return
        summary["batch_end_local"] = after["server_now_local"]
        summary["member_change_counter_after"] = after.get("member_change_counter")

    # -- resolve (read-back only) --------------------------------------------
    def resolve(self) -> dict[str, Any]:
        ledger = self._ledger()
        result: dict[str, Any] = {"status": "ok", "command": "resolve", "writes": 0, "resolved": {}, "alerts": []}
        for row in ledger.open_moves():
            if row["state"] == "INTENT":
                # The parent stopped between the record and the outcome.
                ledger.set_outcome(row["move_id"], "UNCERTAIN", "parent_interrupted")
            fence_id = "fence-" + row["move_id"].removeprefix("move-")
            claim = ledger.root / "claims" / f"{fence_id}.claim"
            if not claim.exists():
                state, code, post = "NOT_WRITTEN", "resolved_no_claim", None
            else:
                state, code, post = self._resolve_one(row, claim)
            ledger.set_outcome(row["move_id"], state, code, post)
            _bump(result["resolved"], f"{state}:{code}")
            if state == "HELD":
                result["alerts"].append(code)
        return result

    def _resolve_one(self, row: sqlite3.Row, claim: Path) -> tuple[str, str, str | None]:
        try:
            before = json.loads(claim.read_text(encoding="ascii")).get("before_digest")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            before = None
        now = self.bridge.member(row["member_no"], row["salt"])
        if not now["found"]:
            return "HELD", "resolved_member_missing", None
        if now["member_type"] == row["to_type"] and isinstance(before, str) and now["fields_digest"] == before:
            post = now.get("last_modified_local")
            if isinstance(post, str) and _LAST_MODIFIED_RE.fullmatch(post):
                return "DONE", "resolved_saved", post
            return "HELD", "resolved_last_modified_missing", None
        if now["member_type"] == row["from_type"] and now.get("last_modified_local") == row["expected_last_modified_local"]:
            return "NOT_WRITTEN", "resolved_not_written", None
        return "HELD", "resolved_unclear", None

    # -- rollback -------------------------------------------------------------
    def rollback(self, *, max_moves: int, show_masked: bool = False) -> dict[str, Any]:
        if not self.config.rollback_enabled:
            raise dl.DirectLocalError("type_move_rollback_disabled")
        ledger = self._ledger()
        cap = self._cap(max_moves)
        self._refuse_open_moves()
        run_id = ledger.start_run("rollback")
        summary: dict[str, Any] = {"status": "ok", "command": "rollback", "run_id": run_id, "cap": cap, "saves": 0,
                                   "rolled_back": {MEMBER: 0, GUEST: 0}, "skipped": {}, "stopped": None, "alerts": [], "changed_fields": []}
        masked: dict[str, list[str]] = {"rolled_back": [], "skipped": [], "stopped": []}
        try:
            before = self.counts()
            self._require_types(before)
            for move in ledger.rollback_candidates():
                if summary["saves"] >= cap:
                    break
                if self._stop_requested():
                    summary["stopped"] = "stop_file_present"
                    break
                intent = ledger.add_intent(run_id=run_id, kind="rollback", member_no=move["member_no"], from_type=move["to_type"],
                                           to_type=FROM_TYPE, expected=move["post_last_modified_local"])
                payload = {"kind": "rollback", "member_no": move["member_no"], "from_type": move["to_type"], "to_type": FROM_TYPE,
                           "expected_last_modified_local": move["post_last_modified_local"], "snapshot_at_local": None, "salt": intent["salt"]}
                outcome = self._save(intent, payload)
                if outcome.state == "DONE":
                    summary["saves"] += 1
                    summary["rolled_back"][move["to_type"]] += 1
                    masked["rolled_back"].append(mask(move["member_no"]))
                elif outcome.state == "SKIPPED":
                    _bump(summary["skipped"], outcome.code)
                    masked["skipped"].append(mask(move["member_no"]))
                else:
                    summary["stopped"] = f"{outcome.state.lower()}:{outcome.code}"
                    summary["alerts"].append(summary["stopped"])
                    summary["changed_fields"] = list(outcome.changed_fields)
                    masked["stopped"].append(mask(move["member_no"]))
                    break
            self._after_batch(summary)
        except dl.DirectLocalError as exc:
            summary["stopped"] = exc.code
            summary["alerts"].append(exc.code)
        finally:
            ledger.finish_run(run_id, summary)
        if show_masked:
            summary["masked"] = masked
        return summary

    # -- reconcile (read-only) -----------------------------------------------
    def reconcile(self, *, show_masked: bool = False) -> dict[str, Any]:
        ledger = self._ledger()
        snapshot = ledger.snapshot()
        if snapshot is None:
            raise dl.DirectLocalError("type_move_snapshot_missing")
        counts = self.counts()
        current: dict[str, dict[str, Any]] = {}
        for member_type in SCAN_TYPES:
            for row in self.scan_all(member_type):
                current[row["member_no"]] = {"type": member_type, "last_modified_local": canonical_last_modified(row.get("last_modified_local"))}
        snap = {row["member_no"]: row for row in ledger.conn.execute("SELECT * FROM snapshot_rows")}
        last_done: dict[str, sqlite3.Row] = {}
        attempted: dict[str, sqlite3.Row] = {}
        for move in ledger.conn.execute("SELECT * FROM moves ORDER BY rowid"):
            if move["kind"] == "move":
                attempted[move["member_no"]] = move
            if move["state"] == "DONE":
                last_done[move["member_no"]] = move
        net = _zero_split()
        lists: dict[str, list[str]] = {k: [] for k in ("not_on_expected_type", "changed_after_tool", "pending", "skipped", "held_at_snapshot", "new_default", "rolled_back")}
        skipped_by_code: dict[str, int] = {}
        for member_no, row in snap.items():
            now = current.get(member_no)
            done = last_done.get(member_no)
            if done is not None:
                expected_type = done["to_type"]
                if done["kind"] == "rollback":
                    lists["rolled_back"].append(member_no)
                else:
                    net[done["to_type"]]["active" if row["is_active"] == "T" else "inactive"] += 1
                if now is None or now["type"] != expected_type:
                    lists["not_on_expected_type"].append(member_no)
                elif now["last_modified_local"] != done["post_last_modified_local"]:
                    lists["changed_after_tool"].append(member_no)
            elif row["target"] is None:
                lists["held_at_snapshot"].append(member_no)
            elif member_no in attempted and attempted[member_no]["state"] == "SKIPPED":
                lists["skipped"].append(member_no)
                _bump(skipped_by_code, attempted[member_no]["code"])
            else:
                lists["pending"].append(member_no)
        for member_no, now in current.items():
            if now["type"] == FROM_TYPE and member_no not in snap:
                lists["new_default"].append(member_no)
        states = {row[0]: row[1] for row in ledger.conn.execute("SELECT state, COUNT(*) FROM moves GROUP BY state")}
        last_batch = self._last_batch_end(ledger)
        outlets = [o for o in counts["outlets"] if isinstance(o, dict)]
        not_synced = sorted({str(o.get("location")) for o in outlets
                             if last_batch is not None and not (isinstance(o.get("last_finish_sync_local"), str) and o["last_finish_sync_local"] > last_batch)})
        alerts = []
        for key, code in (("not_on_expected_type", "moved_member_not_on_expected_type"), ("pending", "snapshot_members_pending")):
            if lists[key]:
                alerts.append(code)
        if states.get("UNCERTAIN", 0) or states.get("INTENT", 0):
            alerts.append("uncertain_moves_unresolved")
        if states.get("HELD", 0):
            alerts.append("held_moves")
        if not_synced:
            alerts.append("outlets_not_synced_since_last_batch")
        result: dict[str, Any] = {
            "status": "ok", "command": "reconcile", "writes": 0, "snapshot_members": len(snap), "snapshot_at_local": snapshot["snapshot_at_local"],
            "moved_net": net, "counts": {key: len(value) for key, value in lists.items()}, "skipped_by_code": skipped_by_code,
            "ledger_states": states, "type_counts_before": json.loads(snapshot["type_counts"]), "type_counts_now": self.type_counts(counts),
            "last_batch_end_local": last_batch, "outlets": len(outlets), "outlets_not_synced": not_synced,
            "complete": not alerts, "alerts": alerts,
        }
        if show_masked:
            result["masked"] = {key: sorted(mask(m) for m in value) for key, value in lists.items()}
        return result

    @staticmethod
    def _last_batch_end(ledger: MoveLedger) -> str | None:
        latest = None
        for row in ledger.conn.execute("SELECT summary FROM runs WHERE command IN ('move', 'rollback') AND summary IS NOT NULL"):
            try:
                value = json.loads(row[0]).get("batch_end_local")
            except (json.JSONDecodeError, AttributeError):
                value = None
            if isinstance(value, str) and _SERVER_NOW_RE.fullmatch(value) and (latest is None or value > latest):
                latest = value
        return latest

    def status(self) -> dict[str, Any]:
        ledger = self._ledger()
        snapshot = ledger.snapshot()
        states = {f"{row[0]}:{row[1]}": row[2] for row in ledger.conn.execute("SELECT kind, state, COUNT(*) FROM moves GROUP BY kind, state")}
        return {"status": "ok", "command": "status", "snapshot": snapshot is not None,
                "snapshot_members": snapshot["members"] if snapshot is not None else 0, "moves": states,
                "stop_file_present": self._stop_requested()}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def build_bridge(config: MoveConfig, root: Path, *, environ: Mapping[str, str]) -> TypeMoveBridgeClient:
    env = None
    if config.ac2_credential_target is not None:
        env = sync.load_ac2_credential(config.ac2_credential_target, refused_user_ids=config.ac2_refused_user_ids).child_environment(environ)
    elif config.autocount_book == sync.LIVE_BOOK:
        raise dl.DirectLocalError("ac2_credential_target_unconfigured")  # parse_config refuses this first
    return TypeMoveBridgeClient(dl.default_bridge_command(BRIDGE_SCRIPT), autocount_book=config.autocount_book, claims=root / "claims",
                                env=env, deadline_seconds=config.write_deadline_seconds)


COMMANDS = ("check-config", "init", "status", "plan", "snapshot", "move", "resolve", "rollback", "reconcile")
WRITE_SWITCHES = {"move": "type_move_enabled", "rollback": "rollback_enabled"}


def main(argv: list[str] | None = None, *, bridge_factory: Callable[[MoveConfig, Path], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#268 one-off member type move: Default -> MEMBER/GUEST")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--config", required=True)
    parser.add_argument("--max", type=int, default=None, help="move/rollback: saves this run (at most max_moves_per_run)")
    parser.add_argument("--show-masked", action="store_true", help="add MemberNos masked to the last 3 characters (VM console only)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check-config":
            _emit({"status": "ok", **config.summary()})
            return 0
        # The write switches are refused before any login or ledger write.
        switch = WRITE_SWITCHES.get(args.command)
        if switch is not None and not getattr(config, switch):
            raise dl.DirectLocalError("type_move_disabled" if args.command == "move" else "type_move_rollback_disabled")
        if args.command == "rollback" and args.max is None:
            raise dl.DirectLocalError("type_move_max_required")
        root = state_root(config)
        factory = bridge_factory or (lambda cfg, r: build_bridge(cfg, r, environ=os.environ))
        if args.command == "plan":
            # Read-only: no ledger is opened or created.
            _emit(TypeMoveEngine(config, factory(config, root), root=root).plan(show_masked=args.show_masked))
            return 0
        ledger = MoveLedger(root)
        try:
            if args.command == "init":
                _emit(ledger.init_meta(config.autocount_book))
                return 0
            if args.command == "status":
                _emit(TypeMoveEngine(config, None, ledger=ledger).status())
                return 0
            ledger.check_binding(config)
            engine = TypeMoveEngine(config, factory(config, root), ledger=ledger)
            if args.command == "snapshot":
                result = engine.snapshot(show_masked=args.show_masked)
            elif args.command == "move":
                result = engine.move(max_moves=args.max, show_masked=args.show_masked)
            elif args.command == "resolve":
                result = engine.resolve()
            elif args.command == "rollback":
                result = engine.rollback(max_moves=args.max, show_masked=args.show_masked)
            else:
                result = engine.reconcile(show_masked=args.show_masked)
            _emit(result)
            return 3 if result.get("alerts") else 0
        finally:
            ledger.close()
    except dl.DirectLocalError as exc:
        _emit({"status": "refused", "code": exc.code})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
