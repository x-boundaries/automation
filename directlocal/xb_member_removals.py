"""#268 slice E: automatic member removal in AutoCount (expire -> GUEST,
refund revoke), with an undo list and a tool undo.

Scope: owner, 6 Nov 2026 ("MEMBER until the current ExpiryDate") and
handover rule 9 ("after 2 years back to Registered"); the G0 slice E design
(``xb268`` mailbox, 20 Nov 2026) as accepted by the Controller on 22 Nov
2026 (all ten Controller answers); owner, 24 Nov 2026: build now. Runs on the
AutoCount VM from its own clone, state root, config and task. Nothing runs on
merge, and every write switch defaults to off.

Two kinds of removal, one move only (``MEMBER`` -> ``GUEST``)
-----------------------------------------------------------
``expire``  A ``MEMBER`` who is active and whose readable ``ExpiryDate`` (on or
            after 2020-01-01) is a date strictly before today in Singapore
            (valid through the expiry day, as the tags tool), with today taken
            from the AutoCount server and checked against the VM clock.
``revoke``  A ``MEMBER`` whom this programme's member core made ``MEMBER``
            (``GUEST`` -> ``MEMBER``, one DONE ``upgrade`` write) with one
            order, where the member core flagged that order
            ``refund_on_basis`` and the refund is a positive amount fact
            (``REFUNDED``, or the measured amount below the threshold), no
            other qualifying receipt exists, the link it used is still active,
            and nobody touched the member since (same ``LastModified`` and
            ``ExpiryDate`` as the upgrade left).
Anything less is a count or an alert (for the digest, "forward to Claude"),
never a write.

Writes (``--apply`` and the kind's own switch, both): one ``SaveMember`` per
member through profile ``benefit_x1`` of ``ac2_member_removals_bridge.ps1``:
only ``MemberType`` is assigned (``GUEST``); ``ExpiryDate`` is never written
and must read back unchanged. INTENT before the child; DONE / SKIPPED /
NOT_WRITTEN / UNCERTAIN / HELD after; UNCERTAIN is settled by read-back only
(``resolve``), never re-sent. Per-run caps, a per-day cap counted from the
ledger, a surge stop, the ``STOP`` file and the type move lock (shared by
every AutoCount member writer) bound every run.

Undo: ``undo-list`` (masked; ``--show`` prints full MemberNos on the VM screen
only) and ``restore`` (profile ``benefit_x1r``, ``GUEST`` -> ``MEMBER`` for a
member this tool removed and nobody touched since; own switch).

Never: Shopify (no client, no token, no query text in this module), any
``ExpiryDate`` write, any type but ``MEMBER`` as a source or ``GUEST`` as a
target (restore: the reverse, bound to one DONE removal), the member core
ledger or the #155 ledger opened writable, a customer message.

Privacy: the ledger (private state root on the VM) keeps MemberNos and order
GIDs. Output carries codes, counts, keyed refs (``r-`` removal, ``m-``
member, ``o-`` order) and MemberNos masked to the last 3 characters only. The
one exception is ``undo-list --show``: plain text for the VM screen, never a
JSON line, a run row, the digest or the email.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.append(str(_HERE))

import xb_directlocal as dl  # noqa: E402
import xb_member_benefits as mb  # noqa: E402
import xb_member_sync as sync  # noqa: E402
import xb_member_type_move as tm  # noqa: E402

CONFIG_SCHEMA = "xb.directlocal.member_removals.config.v1"
LEDGER_FILE = "member_removals.sqlite3"
LOCK_FILE = "member_removals.lock"
STOP_FILE = "STOP"
TOOL = "xb268_member_removals"
SCHEMA_VERSION = 1
PROFILE = "benefit_x1"
RESTORE_PROFILE = "benefit_x1r"
BRIDGE_SCRIPT = dl.REPO_ROOT / "scripts" / "ac2_member_removals_bridge.ps1"

MEMBER = "MEMBER"
GUEST = "GUEST"
FROM_TYPE = MEMBER  # the only source type of a removal
TO_TYPE = GUEST  # the only target type of a removal
KINDS = ("expire", "revoke")
# Singapore, from the member sync (UTC+8, no DST).
SGT = sync.SGT
# A date before this is a placeholder (2000-01-01, 1753-01-01): read as
# unreadable, never as expired. The tags tool's EXPIRY_FLOOR (pinned by a test).
EXPIRY_FLOOR = date(2020, 1, 1)
# The VM clock (Singapore time) against the AutoCount server's local time:
# more than this apart, nothing is written. The tags tool's tolerance.
CLOCK_TOLERANCE = timedelta(minutes=10)
SCAN_PAGE_SIZE = tm.SCAN_PAGE_SIZE  # the child's pinned TOP 500
SCAN_MAX_PAGES = 40  # 20,000 due rows: far above the surge stop
# Rule versions: any change to the expire or revoke facts raises one; they are
# part of the rule fingerprint kept at init.
EXPIRE_RULE_VERSION = 1
REVOKE_RULE_VERSION = 1
# The member core ledger this tool reads (read only). Pinned as literals and
# proven equal to the member core's own constants by a test, so a member core
# v3 stops this tool (member_core_ledger_version_unknown) instead of being
# misread.
MEMBER_CORE_LEDGER_FILE = "member_benefits.sqlite3"
MEMBER_CORE_TOOL = "xb268_member_benefits"
MEMBER_CORE_LEDGER_VERSIONS = (2,)
REFUND_ON_BASIS = "refund_on_basis"
PAID_STATUSES = frozenset({"PAID", "PARTIALLY_REFUNDED", "REFUNDED"})

RELEASE_FINDINGS = ("as_intended", "not_as_intended")
_REMOVAL_REF_RE = re.compile(r"^r-[0-9a-f]{20}\Z")
_RUN_ID_RE = re.compile(r"^run-[0-9a-f]{32}\Z")
_LAST3_RE = re.compile(r"^[A-Za-z0-9._/-]{1,3}\Z")
_STORED_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}$")
_SERVER_NOW_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$")
_DAY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COLUMN_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

REMOVAL_SKIP_CODES = frozenset({"member_not_found", "type_not_in_scope_now", "type_changed_since_read", "changed_since_read", "not_due_now"})
RESTORE_SKIP_CODES = frozenset({"member_not_found", "changed_since_removal"})
REMOVAL_CHILD_CODES = frozenset({
    "removal_request_invalid", "removal_type_not_allowed", "removal_not_due", "removal_scan_failed", "removal_member_read_failed",
    "pos_sync_state_unreadable",
    # type move helpers reused by the child
    "member_no_invalid",
})
CHILD_ERROR_CODES = dl.BRIDGE_CHILD_ERROR_CODES | REMOVAL_CHILD_CODES
REFUSALS_BEFORE_CLAIM = frozenset({"member_type_unknown", "production_book_not_authorised", "removal_type_not_allowed",
                                   "removal_request_invalid", "removal_not_due"})

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

_CONFIG_REQUIRED = {"schema", "state_root", "autocount_book", "member_core_config"}
_CONFIG_DEFAULTS: dict[str, Any] = {
    # Observe is the default: no AutoCount save is reachable while these are false.
    "expire_write_enabled": False,
    "revoke_write_enabled": False,
    "restore_enabled": False,
    "max_expires_per_run": 3,
    "max_revokes_per_run": 3,
    "max_removals_per_day": 20,
    "max_member_reads_per_run": 30,
    "surge_stop_count": 100,
    "surge_ack_until": None,
    "write_deadline_seconds": 120,
    # The #268 type move's state root: every write takes its lock, so the
    # three AutoCount member writers never run together. Required on the live
    # book once any write switch is on.
    "type_move_state_root": None,
    "ac2_credential_target": None,
    "ac2_login_mode": None,
    "ac2_refused_user_ids": [],
}
_BOUNDS = {
    "max_expires_per_run": (1, 50),
    "max_revokes_per_run": (1, 20),
    "max_removals_per_day": (1, 500),
    "max_member_reads_per_run": (1, 100),
    "surge_stop_count": (10, 1000),
    "write_deadline_seconds": (30, 600),
}
_SWITCHES = ("expire_write_enabled", "revoke_write_enabled", "restore_enabled")
WRITE_SWITCH = {"expire": "expire_write_enabled", "revoke": "revoke_write_enabled"}
CAP = {"expire": "max_expires_per_run", "revoke": "max_revokes_per_run"}


@dataclass(frozen=True)
class RemovalsConfig:
    state_root: str
    autocount_book: str
    member_core_config: str
    core: mb.BenefitsConfig  # the member core's own config, parsed by the member core (read only)
    expire_write_enabled: bool
    revoke_write_enabled: bool
    restore_enabled: bool
    max_expires_per_run: int
    max_revokes_per_run: int
    max_removals_per_day: int
    max_member_reads_per_run: int
    surge_stop_count: int
    surge_ack_until: date | None
    write_deadline_seconds: int
    type_move_state_root: str | None
    ac2_credential_target: str | None
    ac2_login_mode: str | None
    ac2_refused_user_ids: tuple[str, ...]

    @property
    def qualify_threshold_cents(self) -> int:
        """Always the member core's threshold: there is no copy to drift."""
        return self.core.qualify_threshold_cents

    def rule_fingerprint(self) -> str:
        """``sha256:`` + hex of the canonical JSON of every setting that
        changes which member is due: the rule versions, the floor and the
        member core's threshold. Kept in the ledger at ``init``. Not in it:
        switches, caps, the surge settings, roots."""

        value = {"expire_rule_version": EXPIRE_RULE_VERSION, "revoke_rule_version": REVOKE_RULE_VERSION,
                 "expiry_floor": EXPIRY_FLOOR.isoformat(), "qualify_threshold_cents": self.qualify_threshold_cents,
                 "clock_tolerance_seconds": int(CLOCK_TOLERANCE.total_seconds())}
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    def summary(self) -> dict[str, Any]:
        """PII-free view for ``check-config``."""

        return {
            "autocount_book": self.autocount_book, "expire_write_enabled": self.expire_write_enabled,
            "revoke_write_enabled": self.revoke_write_enabled, "restore_enabled": self.restore_enabled,
            "max_expires_per_run": self.max_expires_per_run, "max_revokes_per_run": self.max_revokes_per_run,
            "max_removals_per_day": self.max_removals_per_day, "max_member_reads_per_run": self.max_member_reads_per_run,
            "surge_stop_count": self.surge_stop_count,
            "surge_ack_until": self.surge_ack_until.isoformat() if self.surge_ack_until else None,
            "expiry_floor": EXPIRY_FLOOR.isoformat(), "qualify_threshold_cents": self.qualify_threshold_cents,
            "from_type": FROM_TYPE, "to_type": TO_TYPE, "type_move_lock": self.type_move_state_root is not None,
            "ac2_credential": "credential_manager" if self.ac2_credential_target else "owner_console_environment",
            "ac2_login_mode": self.ac2_login_mode, "rule_fingerprint": self.rule_fingerprint(),
        }


def _bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise dl.DirectLocalError(f"removals_config_{key}_invalid")
    return value


def parse_config(value: Any, *, core_loader: Callable[[str], mb.BenefitsConfig] = mb.load_config) -> RemovalsConfig:
    if not isinstance(value, dict):
        raise dl.DirectLocalError("removals_config_invalid")
    if value.get("schema") in (sync.CONFIG_SCHEMA, tm.CONFIG_SCHEMA, mb.CONFIG_SCHEMA):
        raise dl.DirectLocalError("removals_config_is_other_tool")
    if "shopify" in value:
        raise dl.DirectLocalError("removals_config_shopify_refused")  # this tool never talks to Shopify
    keys = set(value)
    if not _CONFIG_REQUIRED <= keys or not keys <= _CONFIG_REQUIRED | set(_CONFIG_DEFAULTS):
        raise dl.DirectLocalError("removals_config_keys_invalid")
    if value["schema"] != CONFIG_SCHEMA:
        raise dl.DirectLocalError("removals_config_schema_invalid")
    merged = {**_CONFIG_DEFAULTS, **value}
    book = sync.check_book(merged["autocount_book"])
    live = book == sync.LIVE_BOOK
    for key in ("state_root", "member_core_config"):
        if not isinstance(merged[key], str) or not merged[key]:
            raise dl.DirectLocalError(f"removals_config_{key}_invalid")
    numbers = {}
    for key, (low, high) in _BOUNDS.items():
        number = merged[key]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise dl.DirectLocalError(f"removals_config_{key}_invalid")
        numbers[key] = number
    flags = {key: _bool(merged[key], key) for key in _SWITCHES}
    ack = merged["surge_ack_until"]
    if ack is not None:
        if not isinstance(ack, str) or not _DAY_RE.fullmatch(ack):
            raise dl.DirectLocalError("removals_config_surge_ack_until_invalid")
        try:
            ack = date.fromisoformat(ack)
        except ValueError:
            raise dl.DirectLocalError("removals_config_surge_ack_until_invalid") from None
    move_root = merged["type_move_state_root"]
    if move_root is not None and (not isinstance(move_root, str) or not move_root):
        raise dl.DirectLocalError("removals_config_type_move_state_root_invalid")
    if live and any(flags.values()) and move_root is None:
        raise dl.DirectLocalError("removals_config_type_move_state_root_required")
    target = merged["ac2_credential_target"]
    if target is not None and (not isinstance(target, str) or not sync.CREDENTIAL_TARGET_RE.fullmatch(target)):
        raise dl.DirectLocalError("removals_config_ac2_credential_target_invalid")
    if live and target != sync.LIVE_CREDENTIAL_TARGET:
        raise dl.DirectLocalError("removals_config_ac2_credential_target_live_required")
    if not live and target is not None and target.strip().casefold() == sync.LIVE_CREDENTIAL_TARGET.casefold():
        raise dl.DirectLocalError("removals_config_ac2_credential_target_live_only")
    login_mode = merged["ac2_login_mode"]
    if login_mode is not None and login_mode not in sync.AC2_LOGIN_MODES:
        raise dl.DirectLocalError("removals_config_ac2_login_mode_invalid")
    if live and login_mode is None:
        raise dl.DirectLocalError("removals_config_ac2_login_mode_live_required")
    refused = merged["ac2_refused_user_ids"]
    if not isinstance(refused, list) or len(refused) > 10 or any(not isinstance(u, str) or not sync.AC2_USER_ID_RE.fullmatch(u) for u in refused):
        raise dl.DirectLocalError("removals_config_ac2_refused_user_ids_invalid")
    if login_mode == "owner_shared" and refused:
        raise dl.DirectLocalError("removals_config_ac2_refused_user_ids_owner_shared_refused")
    if live and login_mode == "dedicated" and not refused:
        raise dl.DirectLocalError("removals_config_ac2_refused_user_ids_live_required")
    try:
        core = core_loader(merged["member_core_config"])
    except dl.DirectLocalError as exc:
        raise dl.DirectLocalError("removals_member_core_config_refused") from exc
    if core.autocount_book != book:
        raise dl.DirectLocalError("removals_member_core_book_mismatch")
    return RemovalsConfig(
        state_root=merged["state_root"], autocount_book=book, member_core_config=merged["member_core_config"], core=core,
        surge_ack_until=ack, type_move_state_root=move_root, ac2_credential_target=target, ac2_login_mode=login_mode,
        ac2_refused_user_ids=tuple(refused), **flags, **numbers,
    )


def load_config(path: str | Path, **kwargs: Any) -> RemovalsConfig:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise dl.DirectLocalError("removals_config_unreadable") from None
    if len(raw) > 65_536:
        raise dl.DirectLocalError("removals_config_invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise dl.DirectLocalError("removals_config_invalid") from None
    return parse_config(value, **kwargs)


def state_root(config: RemovalsConfig) -> Path:
    """The tool's own private root: never the member core's, the #155 sync's,
    the type move's or the DirectLocal Core's."""

    root = dl.validate_state_root(config.state_root)
    for other in (sync.LEDGER_FILE, tm.LEDGER_FILE, mb.LEDGER_FILE, "ledger.sqlite3"):
        if (root / other).exists():
            raise dl.DirectLocalError("removals_state_root_shared")
    for other_root in (config.core.state_root, config.core.member_sync_state_root, config.type_move_state_root):
        if other_root and Path(other_root).resolve() == root.resolve():
            raise dl.DirectLocalError("removals_state_root_shared")
    dl.verify_private_state(root)
    (root / "claims").mkdir(exist_ok=True)
    return root


# --------------------------------------------------------------------------
# Pure rules
# --------------------------------------------------------------------------

def mask(member_no: str | None) -> str:
    return sync.mask_last3(member_no)


def stored_day(value: Any) -> date | None:
    """The date of an AutoCount date/time text (scan or member read form), or
    None when it is NULL or not that shape."""

    parsed = tm.parse_local(value)
    return None if parsed is None else parsed.date()


def expiry_due_day(expiry_local: Any) -> tuple[date | None, str | None]:
    """``(day, None)`` for a readable ExpiryDate on or after the floor, or
    ``(None, "expiry_unreadable")`` for NULL, a placeholder or junk."""

    day = stored_day(expiry_local)
    if day is None or day < EXPIRY_FLOOR:
        return None, "expiry_unreadable"
    return day, None


def is_due(expiry_day: date, run_day: date) -> bool:
    """Valid through the expiry day; due from the next Singapore day."""

    return expiry_day < run_day


def server_day(server_now_local: Any, now: datetime) -> date:
    """The run day: the AutoCount server's local date, after its clock agrees
    with the VM clock in Singapore time within ``CLOCK_TOLERANCE``."""

    if not isinstance(server_now_local, str) or not _SERVER_NOW_RE.fullmatch(server_now_local):
        raise dl.BridgeError("bridge_output_invalid")
    server = tm.parse_local(server_now_local)
    if server is None:
        raise dl.BridgeError("bridge_output_invalid")
    if now.tzinfo is None:
        raise dl.DirectLocalError("clock_must_be_timezone_aware")
    local = now.astimezone(SGT).replace(tzinfo=None)
    if abs(local - server) > CLOCK_TOLERANCE:
        raise _ClockMismatch()
    return server.date()


def same_stored(a: Any, b: Any) -> bool:
    """Two AutoCount date/time texts name the same instant (NULL equals NULL
    only)."""

    if a is None or b is None:
        return a is None and b is None
    pa, pb = tm.parse_local(a), tm.parse_local(b)
    return pa is not None and pa == pb


def check_save_payload(payload: Mapping[str, Any]) -> None:
    """Python's copy of the child's write-boundary rules, run before the
    INTENT row: MEMBER -> GUEST only, both expected values present, expire
    only when the expected expiry is before the run day and on or after the
    floor, revoke only when the expected expiry is the upgrade's basis."""

    keys = {"kind", "member_no", "from_type", "to_type", "expected_last_modified_local", "expected_expiry_local", "run_day_local",
            "basis_expiry_local", "salt"}
    if set(payload) != keys or payload["kind"] not in KINDS:
        raise dl.DirectLocalError("removal_request_invalid")
    if payload["from_type"] != FROM_TYPE or payload["to_type"] != TO_TYPE:
        raise dl.DirectLocalError("removal_type_not_allowed")
    if not isinstance(payload["member_no"], str) or not dl.MEMBER_NO_RE.fullmatch(payload["member_no"]):
        raise dl.DirectLocalError("removal_request_invalid")
    for key in ("expected_last_modified_local", "expected_expiry_local"):
        if not isinstance(payload[key], str) or not _STORED_RE.fullmatch(payload[key]):
            raise dl.DirectLocalError("removal_request_invalid")
    expiry = stored_day(payload["expected_expiry_local"])
    if payload["kind"] == "expire":
        if payload["basis_expiry_local"] is not None or not isinstance(payload["run_day_local"], str) or not _DAY_RE.fullmatch(payload["run_day_local"]):
            raise dl.DirectLocalError("removal_request_invalid")
        if expiry is None or expiry < EXPIRY_FLOOR or not is_due(expiry, date.fromisoformat(payload["run_day_local"])):
            raise dl.DirectLocalError("removal_not_due")
    else:
        if payload["run_day_local"] is not None or not isinstance(payload["basis_expiry_local"], str) or not _STORED_RE.fullmatch(payload["basis_expiry_local"]):
            raise dl.DirectLocalError("removal_request_invalid")
        if payload["basis_expiry_local"] != payload["expected_expiry_local"]:
            raise dl.DirectLocalError("removal_request_invalid")


def check_restore_payload(payload: Mapping[str, Any]) -> None:
    keys = {"kind", "member_no", "from_type", "to_type", "expected_last_modified_local", "expected_expiry_local", "removal_id", "salt"}
    if set(payload) != keys or payload["kind"] != "restore":
        raise dl.DirectLocalError("removal_request_invalid")
    if payload["from_type"] != GUEST or payload["to_type"] != MEMBER:
        raise dl.DirectLocalError("removal_type_not_allowed")
    if not isinstance(payload["member_no"], str) or not dl.MEMBER_NO_RE.fullmatch(payload["member_no"]):
        raise dl.DirectLocalError("removal_request_invalid")
    for key in ("expected_last_modified_local", "expected_expiry_local"):
        if not isinstance(payload[key], str) or not _STORED_RE.fullmatch(payload[key]):
            raise dl.DirectLocalError("removal_request_invalid")
    if not isinstance(payload["removal_id"], str) or not re.fullmatch(r"rm-[0-9a-f]{32}", payload["removal_id"]):
        raise dl.DirectLocalError("removal_request_invalid")


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

LEDGER_SCHEMA = """
CREATE TABLE meta(
  id INTEGER PRIMARY KEY CHECK (id = 1), tool TEXT NOT NULL CHECK (tool = 'xb268_member_removals'), ledger_id TEXT NOT NULL,
  autocount_book TEXT NOT NULL CHECK (autocount_book IN ('AED_TESTINGAPI', 'AED_XBOUNDARIES')),
  ref_key TEXT NOT NULL CHECK (length(ref_key) = 64), created_at TEXT NOT NULL,
  rule_fingerprint TEXT NOT NULL CHECK (rule_fingerprint GLOB 'sha256:*' AND length(rule_fingerprint) = 71));
CREATE TABLE removals(
  removal_id TEXT PRIMARY KEY CHECK (removal_id GLOB 'rm-*'), run_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('expire', 'revoke')),
  member_no TEXT NOT NULL, from_type TEXT NOT NULL CHECK (from_type = 'MEMBER'), to_type TEXT NOT NULL CHECK (to_type = 'GUEST'),
  expected_last_modified_local TEXT NOT NULL, expected_expiry_local TEXT NOT NULL,
  day_local TEXT NOT NULL CHECK (day_local GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  order_gid TEXT, core_write_id TEXT, salt TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('INTENT', 'DONE', 'SKIPPED', 'NOT_WRITTEN', 'UNCERTAIN', 'HELD')),
  code TEXT, post_last_modified_local TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  deadline_at TEXT NOT NULL, child_pid INTEGER, child_created TEXT,
  CHECK (kind <> 'expire' OR (substr(expected_expiry_local, 1, 10) < day_local AND substr(expected_expiry_local, 1, 10) >= '2020-01-01'
                              AND order_gid IS NULL AND core_write_id IS NULL)),
  CHECK (kind <> 'revoke' OR (order_gid IS NOT NULL AND core_write_id IS NOT NULL)),
  CHECK (state <> 'DONE' OR post_last_modified_local IS NOT NULL));
CREATE TABLE restores(
  restore_id TEXT PRIMARY KEY CHECK (restore_id GLOB 'rs-*'), removal_id TEXT NOT NULL REFERENCES removals(removal_id), run_id TEXT NOT NULL,
  member_no TEXT NOT NULL, from_type TEXT NOT NULL CHECK (from_type = 'GUEST'), to_type TEXT NOT NULL CHECK (to_type = 'MEMBER'),
  expected_last_modified_local TEXT NOT NULL, expected_expiry_local TEXT NOT NULL, salt TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('INTENT', 'DONE', 'SKIPPED', 'NOT_WRITTEN', 'UNCERTAIN', 'HELD')),
  code TEXT, post_last_modified_local TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  deadline_at TEXT NOT NULL, child_pid INTEGER, child_created TEXT,
  CHECK (state <> 'DONE' OR post_last_modified_local IS NOT NULL));
CREATE TABLE write_releases(
  write_id TEXT PRIMARY KEY, finding TEXT NOT NULL CHECK (finding IN ('as_intended', 'not_as_intended')),
  member_read_digest TEXT NOT NULL CHECK (member_read_digest GLOB 'sha256:*' AND length(member_read_digest) = 71),
  run_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE reviews(subject TEXT PRIMARY KEY, code TEXT NOT NULL, first_seen_at TEXT NOT NULL);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, command TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, summary TEXT);
CREATE VIEW undo_list AS
  SELECT r.rowid AS seq, r.* FROM removals r WHERE r.state = 'DONE'
   AND NOT EXISTS (SELECT 1 FROM restores s WHERE s.removal_id = r.removal_id AND s.state = 'DONE');
CREATE TRIGGER meta_immutable BEFORE UPDATE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER meta_no_delete BEFORE DELETE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER removals_no_delete BEFORE DELETE ON removals BEGIN SELECT RAISE(ABORT, 'removal_immutable'); END;
CREATE TRIGGER removals_binding_immutable BEFORE UPDATE OF removal_id, run_id, kind, member_no, from_type, to_type,
  expected_last_modified_local, expected_expiry_local, day_local, order_gid, core_write_id, salt, created_at, deadline_at ON removals
  BEGIN SELECT RAISE(ABORT, 'removal_immutable'); END;
CREATE TRIGGER removals_transition BEFORE UPDATE OF state ON removals
  WHEN NOT ((OLD.state = 'INTENT' AND NEW.state IN ('DONE', 'SKIPPED', 'NOT_WRITTEN', 'UNCERTAIN', 'HELD'))
         OR (OLD.state = 'UNCERTAIN' AND NEW.state IN ('DONE', 'NOT_WRITTEN', 'HELD')))
  BEGIN SELECT RAISE(ABORT, 'write_transition_invalid'); END;
CREATE TRIGGER restores_need_done_removal BEFORE INSERT ON restores
  WHEN NOT EXISTS (SELECT 1 FROM removals r WHERE r.removal_id = NEW.removal_id AND r.state = 'DONE' AND r.member_no = NEW.member_no
                   AND r.post_last_modified_local = NEW.expected_last_modified_local AND r.expected_expiry_local = NEW.expected_expiry_local)
  BEGIN SELECT RAISE(ABORT, 'restore_removal_not_done'); END;
CREATE TRIGGER restores_one_active BEFORE INSERT ON restores
  WHEN EXISTS (SELECT 1 FROM restores s WHERE s.removal_id = NEW.removal_id AND s.state IN ('INTENT', 'DONE', 'UNCERTAIN', 'HELD'))
  BEGIN SELECT RAISE(ABORT, 'restore_already_recorded'); END;
CREATE TRIGGER restores_no_delete BEFORE DELETE ON restores BEGIN SELECT RAISE(ABORT, 'restore_immutable'); END;
CREATE TRIGGER restores_binding_immutable BEFORE UPDATE OF restore_id, removal_id, run_id, member_no, from_type, to_type,
  expected_last_modified_local, expected_expiry_local, salt, created_at, deadline_at ON restores
  BEGIN SELECT RAISE(ABORT, 'restore_immutable'); END;
CREATE TRIGGER restores_transition BEFORE UPDATE OF state ON restores
  WHEN NOT ((OLD.state = 'INTENT' AND NEW.state IN ('DONE', 'SKIPPED', 'NOT_WRITTEN', 'UNCERTAIN', 'HELD'))
         OR (OLD.state = 'UNCERTAIN' AND NEW.state IN ('DONE', 'NOT_WRITTEN', 'HELD')))
  BEGIN SELECT RAISE(ABORT, 'write_transition_invalid'); END;
CREATE TRIGGER write_releases_held_only BEFORE INSERT ON write_releases
  WHEN NOT EXISTS (SELECT 1 FROM removals r WHERE r.removal_id = NEW.write_id AND r.state = 'HELD')
   AND NOT EXISTS (SELECT 1 FROM restores s WHERE s.restore_id = NEW.write_id AND s.state = 'HELD')
  BEGIN SELECT RAISE(ABORT, 'release_not_held'); END;
CREATE TRIGGER write_releases_immutable BEFORE UPDATE ON write_releases BEGIN SELECT RAISE(ABORT, 'write_release_immutable'); END;
CREATE TRIGGER write_releases_no_delete BEFORE DELETE ON write_releases BEGIN SELECT RAISE(ABORT, 'write_release_immutable'); END;
CREATE TRIGGER reviews_immutable BEFORE UPDATE ON reviews BEGIN SELECT RAISE(ABORT, 'review_immutable'); END;
CREATE TRIGGER reviews_no_delete BEFORE DELETE ON reviews BEGIN SELECT RAISE(ABORT, 'review_immutable'); END;
"""

_OPEN_STATES = "('INTENT', 'UNCERTAIN')"
_LANDED_OR_OPEN = "('INTENT', 'DONE', 'UNCERTAIN', 'HELD')"


class RemovalsLedger:
    """Single-owner SQLite ledger in the tool's own private root."""

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = dl.utc_now):
        self.root = Path(root)
        self.clock = clock
        self._lock = dl._OwnerLock(self.root / LOCK_FILE)
        self.conn = None
        try:
            self.conn = sqlite3.connect(self.root / LEDGER_FILE, isolation_level=None, timeout=0)
            self.conn.row_factory = sqlite3.Row
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not (version == 0 and not tables) and (version != SCHEMA_VERSION or "removals" not in tables):
                raise dl.DirectLocalError("removals_ledger_not_recognised")
            # recursive_triggers: an INSERT OR REPLACE deletes the old row
            # first; with this on, the no-delete triggers fire and abort it.
            for pragma in ("journal_mode=WAL", "synchronous=FULL", "foreign_keys=ON", "trusted_schema=OFF", "recursive_triggers=ON"):
                self.conn.execute(f"PRAGMA {pragma}")
            if version == 0 and not tables:
                with self.tx() as conn:
                    for statement in dl._split_sql(LEDGER_SCHEMA):
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            if self.conn is not None:
                self.conn.close()
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

    def init_meta(self, config: RemovalsConfig) -> dict[str, Any]:
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
            if row is not None:
                self._check(row, config)
                return {"status": "existing"}
            conn.execute("INSERT INTO meta(id, tool, ledger_id, autocount_book, ref_key, created_at, rule_fingerprint) VALUES (1, ?, ?, ?, ?, ?, ?)",
                         (TOOL, f"mr-{uuid.uuid4().hex}", config.autocount_book, secrets.token_hex(32), dl.ts(self.now()), config.rule_fingerprint()))
        return {"status": "initialised"}

    @staticmethod
    def _check(row: sqlite3.Row, config: RemovalsConfig) -> None:
        if (row["tool"], row["autocount_book"]) != (TOOL, sync.check_book(config.autocount_book)):
            raise dl.DirectLocalError("removals_ledger_binding_mismatch")

    def check_binding(self, config: RemovalsConfig) -> None:
        row = self.conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        if row is None:
            raise dl.DirectLocalError("removals_ledger_not_initialised")
        self._check(row, config)

    def rule_settings_match(self, config: RemovalsConfig) -> bool:
        row = self.conn.execute("SELECT rule_fingerprint FROM meta WHERE id = 1").fetchone()
        return row is not None and hmac.compare_digest(row[0], config.rule_fingerprint())

    def ref(self, kind: str, value: str) -> str:
        return mb.keyed_ref(self.conn.execute("SELECT ref_key FROM meta WHERE id = 1").fetchone()[0], kind, value)

    def start_run(self, command: str) -> str:
        run_id = f"run-{uuid.uuid4().hex}"
        with self.tx() as conn:
            conn.execute("INSERT INTO runs(run_id, command, started_at) VALUES (?, ?, ?)", (run_id, command, dl.ts(self.now())))
        return run_id

    def finish_run(self, run_id: str, summary: Mapping[str, Any]) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE run_id = ?", (dl.ts(self.now()), json.dumps(summary, sort_keys=True), run_id))

    def last_finished_summary(self, commands: tuple[str, ...]) -> dict[str, Any] | None:
        marks = ",".join("?" for _ in commands)
        row = self.conn.execute(f"SELECT summary FROM runs WHERE command IN ({marks}) AND finished_at IS NOT NULL ORDER BY started_at DESC, rowid DESC LIMIT 1",
                                commands).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0] or "{}")
        except (json.JSONDecodeError, RecursionError):
            return None
        return value if isinstance(value, dict) else None

    def review_once(self, subject: str, code: str) -> bool:
        """True the first time a subject needs review (alert once)."""

        with self.tx() as conn:
            if conn.execute("SELECT 1 FROM reviews WHERE subject = ?", (subject,)).fetchone() is not None:
                return False
            conn.execute("INSERT INTO reviews(subject, code, first_seen_at) VALUES (?, ?, ?)", (subject, code, dl.ts(self.now())))
        return True

    # -- write rows -----------------------------------------------------------
    def open_rows(self) -> list[tuple[str, sqlite3.Row]]:
        rows = [("removal", r) for r in self.conn.execute(f"SELECT * FROM removals WHERE state IN {_OPEN_STATES} ORDER BY rowid")]
        rows += [("restore", r) for r in self.conn.execute(f"SELECT * FROM restores WHERE state IN {_OPEN_STATES} ORDER BY rowid")]
        return rows

    def unreleased_held(self) -> list[tuple[str, sqlite3.Row]]:
        rows = [("removal", r) for r in self.conn.execute("SELECT * FROM removals w WHERE w.state = 'HELD' AND NOT EXISTS "
                                                          "(SELECT 1 FROM write_releases x WHERE x.write_id = w.removal_id) ORDER BY rowid")]
        rows += [("restore", r) for r in self.conn.execute("SELECT * FROM restores w WHERE w.state = 'HELD' AND NOT EXISTS "
                                                           "(SELECT 1 FROM write_releases x WHERE x.write_id = w.restore_id) ORDER BY rowid")]
        return rows

    def removal_landed_or_open(self, member_no: str) -> sqlite3.Row | None:
        """The latest removal of this member that landed or may have landed."""

        return self.conn.execute(f"SELECT * FROM removals WHERE member_no = ? AND state IN {_LANDED_OR_OPEN} ORDER BY rowid DESC LIMIT 1",
                                 (member_no,)).fetchone()

    def revoke_recorded(self, order_gid: str) -> bool:
        return self.conn.execute(f"SELECT 1 FROM removals WHERE kind = 'revoke' AND order_gid = ? AND state IN {_LANDED_OR_OPEN} LIMIT 1",
                                 (order_gid,)).fetchone() is not None

    def put_back_after_expire(self, member_no: str, expiry_local: Any) -> bool:
        """This tool expired the member once and the member is ``MEMBER``
        again with the same ExpiryDate: someone put it back (``restore`` or a
        hand undo). Never expired again until the ExpiryDate changes."""

        for row in self.conn.execute("SELECT expected_expiry_local FROM removals WHERE member_no = ? AND kind = 'expire' AND state = 'DONE'",
                                     (member_no,)):
            if same_stored(row[0], expiry_local):
                return True
        return False

    def removals_today(self, day: str) -> int:
        """Removals of this Singapore day that landed or may have landed: the
        per-day cap counts from the ledger, never from memory."""

        return self.conn.execute(f"SELECT COUNT(*) FROM removals WHERE day_local = ? AND state IN {_LANDED_OR_OPEN}", (day,)).fetchone()[0]

    def add_removal(self, *, run_id: str, payload: Mapping[str, Any], day_local: str, order_gid: str | None, core_write_id: str | None,
                    deadline: datetime) -> sqlite3.Row:
        removal_id = f"rm-{uuid.uuid4().hex}"
        now = dl.ts(self.now())
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO removals(removal_id, run_id, kind, member_no, from_type, to_type, expected_last_modified_local, expected_expiry_local, "
                "day_local, order_gid, core_write_id, salt, state, created_at, updated_at, deadline_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTENT', ?, ?, ?)",
                (removal_id, run_id, payload["kind"], payload["member_no"], payload["from_type"], payload["to_type"], payload["expected_last_modified_local"],
                 payload["expected_expiry_local"], day_local, order_gid, core_write_id, payload["salt"], now, now, dl.ts(deadline)))
        return self.conn.execute("SELECT * FROM removals WHERE removal_id = ?", (removal_id,)).fetchone()

    def add_restore(self, *, run_id: str, removal: sqlite3.Row, salt: str, deadline: datetime) -> sqlite3.Row:
        restore_id = f"rs-{uuid.uuid4().hex}"
        now = dl.ts(self.now())
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO restores(restore_id, removal_id, run_id, member_no, from_type, to_type, expected_last_modified_local, expected_expiry_local, "
                "salt, state, created_at, updated_at, deadline_at) VALUES (?, ?, ?, ?, 'GUEST', 'MEMBER', ?, ?, ?, 'INTENT', ?, ?, ?)",
                (restore_id, removal["removal_id"], run_id, removal["member_no"], removal["post_last_modified_local"], removal["expected_expiry_local"],
                 salt, now, now, dl.ts(deadline)))
        return self.conn.execute("SELECT * FROM restores WHERE restore_id = ?", (restore_id,)).fetchone()

    def set_child(self, table: str, row_id: str, pid: int, created: str | None) -> None:
        key = "removal_id" if table == "removals" else "restore_id"
        with self.tx() as conn:
            conn.execute(f"UPDATE {table} SET child_pid = ?, child_created = ? WHERE {key} = ?", (pid, created, row_id))

    def set_outcome(self, table: str, row_id: str, state: str, code: str | None, post: str | None = None) -> None:
        key = "removal_id" if table == "removals" else "restore_id"
        with self.tx() as conn:
            conn.execute(f"UPDATE {table} SET state = ?, code = ?, post_last_modified_local = ?, updated_at = ? WHERE {key} = ?",
                         (state, code, post, dl.ts(self.now()), row_id))


def removal_payload(row: sqlite3.Row) -> dict[str, Any]:
    """The child's payload, built from the stored row so the ledger and the
    child always agree."""

    expire = row["kind"] == "expire"
    return {"kind": row["kind"], "member_no": row["member_no"], "from_type": row["from_type"], "to_type": row["to_type"],
            "expected_last_modified_local": row["expected_last_modified_local"], "expected_expiry_local": row["expected_expiry_local"],
            "run_day_local": row["day_local"] if expire else None, "basis_expiry_local": None if expire else row["expected_expiry_local"],
            "salt": row["salt"]}


def restore_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {"kind": "restore", "member_no": row["member_no"], "from_type": row["from_type"], "to_type": row["to_type"],
            "expected_last_modified_local": row["expected_last_modified_local"], "expected_expiry_local": row["expected_expiry_local"],
            "removal_id": row["removal_id"], "salt": row["salt"]}


# --------------------------------------------------------------------------
# Member core and #155 ledgers (read only)
# --------------------------------------------------------------------------

def open_read_only(path: Path) -> sqlite3.Connection:
    """SQLite ``mode=ro`` and ``query_only``: no lock taken, nothing written."""

    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


@dataclass
class CoreFacts:
    """What one run needs from the member core ledger (one snapshot)."""

    open_members: set[str] = field(default_factory=set)  # INTENT, UNCERTAIN, or HELD not released
    pending_members: set[str] = field(default_factory=set)  # receipts ready or waiting_member
    flagged: list[dict[str, Any]] = field(default_factory=list)  # applied receipts alerted refund_on_basis
    receipts_by_member: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # applied / no_change
    upgrade_writes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # order_gid -> writes (kind upgrade)
    released_as_intended: set[str] = field(default_factory=set)  # order_gids with a HELD write released as_intended
    active_claims: set[tuple[str, str]] = field(default_factory=set)  # (customer_gid, member_no_key)


def read_member_core(config: RemovalsConfig) -> CoreFacts:
    """The member core v2 ledger, read only, after the four checks (file,
    open, version, binding). Never copied; never written."""

    path = Path(config.core.state_root) / MEMBER_CORE_LEDGER_FILE
    if not path.is_file():
        raise dl.DirectLocalError("member_core_ledger_missing")
    try:
        conn = open_read_only(path)
    except sqlite3.Error:
        raise dl.DirectLocalError("member_core_ledger_unreadable") from None
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] not in MEMBER_CORE_LEDGER_VERSIONS:
            raise dl.DirectLocalError("member_core_ledger_version_unknown")
        meta = conn.execute("SELECT tool, autocount_book, store_gid, shop_domain FROM meta WHERE id = 1").fetchone()
        if meta is None or tuple(meta) != (MEMBER_CORE_TOOL, config.autocount_book, config.core.shopify.store_gid, config.core.shopify.shop_domain):
            raise dl.DirectLocalError("member_core_ledger_binding_mismatch")
        facts = CoreFacts()
        conn.execute("BEGIN")  # one read snapshot for every SELECT below
        try:
            for row in conn.execute("SELECT member_no FROM writes w WHERE w.state IN ('INTENT', 'UNCERTAIN') OR (w.state = 'HELD' AND NOT EXISTS "
                                    "(SELECT 1 FROM write_releases r WHERE r.write_id = w.write_id))"):
                facts.open_members.add(row["member_no"])
            for row in conn.execute("SELECT member_no FROM receipts WHERE state IN ('ready', 'waiting_member') AND member_no IS NOT NULL"):
                facts.pending_members.add(row["member_no"])
            for row in conn.execute("SELECT order_gid, customer_gid, order_created_at, financial_status, net_cents, state, code, member_no, link_source, "
                                    "basis_expiry_local, alerted FROM receipts WHERE state IN ('applied', 'no_change') AND member_no IS NOT NULL "
                                    "ORDER BY order_created_at, order_gid"):
                value = dict(row)
                facts.receipts_by_member.setdefault(value["member_no"], []).append(value)
                if value["state"] == "applied" and value["alerted"] == REFUND_ON_BASIS:
                    facts.flagged.append(value)
            for row in conn.execute("SELECT write_id, order_gid, kind, member_no, from_type, to_type, new_expiry_local, state, "
                                    "post_last_modified_local FROM writes WHERE kind = 'upgrade' ORDER BY rowid"):
                facts.upgrade_writes.setdefault(row["order_gid"], []).append(dict(row))
            for row in conn.execute("SELECT w.order_gid FROM writes w JOIN write_releases r ON r.write_id = w.write_id "
                                    "WHERE w.state = 'HELD' AND r.finding = 'as_intended'"):
                facts.released_as_intended.add(row["order_gid"])
            for row in conn.execute("SELECT customer_gid, member_no_key FROM active_claim_links"):
                facts.active_claims.add((row["customer_gid"], row["member_no_key"]))
        finally:
            conn.execute("ROLLBACK")
        return facts
    except sqlite3.Error:
        raise dl.DirectLocalError("member_core_ledger_unreadable") from None
    finally:
        conn.close()


def load_sync_links(config: RemovalsConfig) -> mb.SyncLinks:
    """The #155 links through the member core's own read-only loader."""

    return mb.SyncLinks.load(config.core.member_sync_state_root, config.core)


# --------------------------------------------------------------------------
# AutoCount bridge client
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SaveOutcome:
    state: str  # DONE, SKIPPED, NOT_WRITTEN, UNCERTAIN or HELD
    code: str | None
    post_last_modified_local: str | None = None
    changed_fields: tuple[str, ...] = ()


def failure_code(stdout: bytes, returncode: int | None = None) -> str:
    line, _ = dl._last_output_line(stdout)
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
    code = failure_code(stdout).removeprefix("bridge_read_failed:") if isinstance(stdout, bytes) else ""
    return code if code in CHILD_ERROR_CODES else None


def classify_save(run: dl.WriteRun, *, claim_exists: bool, skip_codes: frozenset[str] = REMOVAL_SKIP_CODES) -> SaveOutcome:
    """Outcome of one removal-save or removal-restore child. Absence of the
    claim file is the only proof that SaveMember was never reached."""

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
    except (json.JSONDecodeError, IndexError, RecursionError, AttributeError):
        outcome = None
    if not isinstance(outcome, dict):
        return SaveOutcome("UNCERTAIN", "writer_outcome_invalid")
    if outcome.get("status") == "skipped":
        if set(outcome) == {"status", "code"} and outcome["code"] in skip_codes and not claim_exists:
            return SaveOutcome("SKIPPED", outcome["code"])
        return SaveOutcome("UNCERTAIN", "writer_outcome_invalid")
    changed = outcome.get("changed_fields")
    changed = [changed] if isinstance(changed, str) else changed
    post = outcome.get("post_last_modified_local")
    valid = (
        outcome.get("status") == "saved" and outcome.get("save_invocation_count") == 1 and claim_exists
        and all(isinstance(outcome.get(k), bool) for k in ("readback_found", "readback_type_match", "readback_expiry_match", "fields_unchanged", "outbound_queue_changed"))
        and isinstance(changed, list) and all(isinstance(c, str) and _COLUMN_RE.fullmatch(c) for c in changed)
        and (post is None or (isinstance(post, str) and _STORED_RE.fullmatch(post)))
    )
    if not valid:
        return SaveOutcome("UNCERTAIN", "writer_outcome_invalid")
    fields = tuple(sorted(changed))[:20]
    if not outcome["readback_found"]:
        return SaveOutcome("UNCERTAIN", "readback_absent")
    if not outcome["readback_type_match"]:
        return SaveOutcome("HELD", "readback_type_mismatch", post, fields)
    if not outcome["readback_expiry_match"]:
        return SaveOutcome("HELD", "readback_expiry_changed", post, fields)
    if not outcome["fields_unchanged"]:
        return SaveOutcome("HELD", "readback_fields_changed", post, fields)
    if outcome["outbound_queue_changed"]:
        return SaveOutcome("HELD", "outbound_queue_changed", post, fields)
    if post is None:
        return SaveOutcome("HELD", "readback_last_modified_missing", None, fields)
    return SaveOutcome("DONE", None, post, fields)


@dataclass(frozen=True)
class MemberRead:
    found: bool
    member_type: str | None = None
    is_active: str | None = None
    expiry_local: str | None = None
    last_modified_local: str | None = None
    fields_digest: str | None = None
    server_now_local: str | None = None

    def __repr__(self) -> str:
        return "MemberRead()"


class RemovalsBridgeClient(tm.TypeMoveBridgeClient):
    """ac2_member_removals_bridge.ps1: two reads and two fenced saves. The
    process start and the fenced write runner are the type move's."""

    def __repr__(self) -> str:
        return "RemovalsBridgeClient()"

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

    def counts(self) -> dict[str, Any]:  # not offered by this entry point
        raise dl.DirectLocalError("removal_operation_not_offered")

    def scan(self, after_member_no: str, run_day: date) -> tuple[list[dict[str, Any]], str]:
        """One page of MEMBERs whose ExpiryDate is before ``run_day``, NULL or
        a placeholder, and the server's local time."""

        result = self._read("removal-scan", {"after_member_no": after_member_no, "run_day_local": run_day.isoformat()})
        rows = result.get("members")
        rows = [rows] if isinstance(rows, dict) else ([] if rows is None else rows)
        if not isinstance(rows, list) or len(rows) > SCAN_PAGE_SIZE:
            raise dl.BridgeError("bridge_output_invalid")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("member_no"), str) or not row["member_no"]:
                raise dl.BridgeError("bridge_output_invalid")
        server = result.get("server_now_local")
        if not isinstance(server, str) or not _SERVER_NOW_RE.fullmatch(server):
            raise dl.BridgeError("bridge_output_invalid")
        return rows, server

    def member(self, member_no: str, salt: str) -> MemberRead:
        result = self._read("removal-member", {"member_no": member_no, "salt": salt})
        server = result.get("server_now_local")
        if not isinstance(result.get("found"), bool) or not isinstance(server, str) or not _SERVER_NOW_RE.fullmatch(server):
            raise dl.BridgeError("bridge_output_invalid")
        if not result["found"]:
            return MemberRead(False, server_now_local=server)
        if not (isinstance(result.get("member_type"), str) and isinstance(result.get("fields_digest"), str) and _DIGEST_RE.fullmatch(result["fields_digest"])):
            raise dl.BridgeError("bridge_output_invalid")
        text = {k: result.get(k) if isinstance(result.get(k), str) else None for k in ("is_active", "expiry_local", "last_modified_local")}
        return MemberRead(True, member_type=result["member_type"], fields_digest=result["fields_digest"], server_now_local=server, **text)

    def _fenced(self, operation: str, payload: Mapping[str, Any], *, row_id: str, deadline: datetime,
                on_started: Callable[[int, str | None], None], skip_codes: frozenset[str]) -> SaveOutcome:
        fence_id = "fence-" + row_id[3:]
        salt = secrets.token_hex(16)
        text = dl.canonical_payload(payload)
        stdin = {"fence_id": fence_id, "payload_canonical": text, "payload_digest": dl.payload_digest(salt, text), "digest_salt": salt}
        arguments = ["-Operation", operation, "-EnableProductionAdapter", "-FenceId", fence_id,
                     "-ClaimDirectory", str(self.claims), "-WriteDeadlineUtc", deadline.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]
        wait = (deadline - dl.utc_now()).total_seconds() + dl.WRITER_GRACE_SECONDS
        run = self._write(arguments, stdin, wait_seconds=wait, on_started=on_started)
        return classify_save(run, claim_exists=(self.claims / f"{fence_id}.claim").exists(), skip_codes=skip_codes)

    def save(self, payload: Mapping[str, Any], *, move_id: str, deadline: datetime,
             on_started: Callable[[int, str | None], None] = lambda pid, created: None) -> SaveOutcome:
        check_save_payload(payload)
        return self._fenced("removal-save", payload, row_id=move_id, deadline=deadline, on_started=on_started, skip_codes=REMOVAL_SKIP_CODES)

    def restore(self, payload: Mapping[str, Any], *, move_id: str, deadline: datetime,
                on_started: Callable[[int, str | None], None] = lambda pid, created: None) -> SaveOutcome:
        check_restore_payload(payload)
        return self._fenced("removal-restore", payload, row_id=move_id, deadline=deadline, on_started=on_started, skip_codes=RESTORE_SKIP_CODES)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

@dataclass
class RunSummary:
    command: str
    counts: dict[str, int] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)
    refs: list[dict[str, str]] = field(default_factory=list)
    saves: int = 0
    stopped: str | None = None
    run_day: str | None = None

    def count(self, code: str, ref: str | None = None, member_no: str | None = None, number: int = 1) -> None:
        self.counts[code] = self.counts.get(code, 0) + number
        if ref is not None and len(self.refs) < 200:
            entry = {"ref": ref, "code": code}
            if member_no:
                entry["member_no_masked"] = mask(member_no)
            self.refs.append(entry)

    def alert(self, code: str, ref: str | None = None, member_no: str | None = None) -> None:
        self.count(code, ref, member_no)
        if code not in self.alerts:
            self.alerts.append(code)

    def result(self, run_id: str) -> dict[str, Any]:
        return {"status": "ok", "command": self.command, "run_id": run_id, "saves": self.saves, "counts": dict(sorted(self.counts.items())),
                "alerts": list(self.alerts), "refs": list(self.refs), "stopped": self.stopped, "run_day": self.run_day}


class _Budget(Exception):
    """The run's AutoCount read budget is spent."""


class _ClockMismatch(dl.DirectLocalError):
    def __init__(self) -> None:
        super().__init__("clock_mismatch")


class _Stop(Exception):
    """An unclear write: the run stops here."""


@contextmanager
def type_move_lock(config: RemovalsConfig) -> Iterator[None]:
    """Every write holds the type move's own ledger lock, as the member core's
    ``apply`` does, so the three AutoCount member writers never run together.
    Busy raises ``type_move_running``."""

    if config.type_move_state_root is None:
        yield
        return
    root = Path(config.type_move_state_root)
    if not (root / tm.LEDGER_FILE).is_file():
        raise dl.DirectLocalError("type_move_state_root_invalid")
    try:
        lock = dl._OwnerLock(root / tm.LOCK_FILE)
    except dl.DirectLocalError:
        raise dl.DirectLocalError("type_move_running") from None
    try:
        yield
    finally:
        lock.release()


class RemovalsEngine:
    """One bounded run. ``ac2`` provides ``scan``, ``member``, ``save`` and
    ``restore``; ``core`` loads the member core facts (read only);
    ``sync_links`` loads the #155 links (read only)."""

    def __init__(self, config: RemovalsConfig, ledger: RemovalsLedger, *, ac2: Any,
                 core: Callable[[], CoreFacts] | None = None, sync_links: Callable[[], mb.SyncLinks] | None = None,
                 writer_probe: Callable[[int, str | None], str] = dl.probe_writer):
        self.config = config
        self.ledger = ledger
        self.ac2 = ac2
        self._core = core or (lambda: read_member_core(config))
        self._sync_links = sync_links or (lambda: load_sync_links(config))
        self.writer_probe = writer_probe
        self.reads = 0
        self.run_day: date | None = None

    # -- helpers --------------------------------------------------------------
    def now(self) -> datetime:
        return self.ledger.now()

    def _stop_requested(self) -> bool:
        return (self.ledger.root / STOP_FILE).exists()

    def _clock(self, server_now_local: Any) -> date:
        day = server_day(server_now_local, self.now())
        if self.run_day is not None and day != self.run_day:
            raise _ClockMismatch()  # the server day moved inside one run: stop, the next run starts clean
        self.run_day = day
        return day

    def _read_member(self, member_no: str) -> MemberRead:
        if not isinstance(member_no, str) or not dl.MEMBER_NO_RE.fullmatch(member_no):
            raise dl.DirectLocalError("member_no_unsupported")  # callers check first; never sent to the bridge
        if self.reads >= self.config.max_member_reads_per_run:
            raise _Budget()
        self.reads += 1
        read = self.ac2.member(member_no, secrets.token_hex(16))
        self._clock(read.server_now_local)
        return read

    def _review(self, subject: str, code: str, summary: RunSummary, ref: str | None, member_no: str | None = None) -> None:
        if self.ledger.review_once(subject, code):
            summary.alert(code, ref, member_no)
        else:
            summary.count(code)

    def _write_allowed(self, kind: str, apply: bool) -> bool:
        return apply and bool(getattr(self.config, WRITE_SWITCH[kind]))

    def _preflight(self, *, writes: bool) -> None:
        self.ledger.check_binding(self.config)
        if not self.ledger.rule_settings_match(self.config):
            raise dl.DirectLocalError("removals_rule_settings_changed")
        if writes:
            if self.ledger.open_rows():
                raise dl.DirectLocalError("removal_uncertain_unresolved")
            if self.ledger.unreleased_held():
                raise dl.DirectLocalError("removal_held_rows_present")

    # -- runs -------------------------------------------------------------------
    def run(self, kinds: tuple[str, ...], *, apply: bool, command: str) -> dict[str, Any]:
        """``kinds`` in order (``scheduled``: revoke, then expire). A kind
        writes only when ``apply`` and its own switch are both on."""

        writes = any(self._write_allowed(kind, apply) for kind in kinds)
        if apply and not writes:
            raise dl.DirectLocalError("removals_write_disabled")
        self._preflight(writes=writes)
        core = self._core()
        links = self._sync_links() if "revoke" in kinds else None
        summary = RunSummary(command)
        run_id = self.ledger.start_run(command)
        try:
            for kind in kinds:
                if kind == "revoke":
                    self._revoke(core, links, summary, run_id=run_id, write=self._write_allowed("revoke", apply))
                else:
                    self._expire(core, summary, run_id=run_id, write=self._write_allowed("expire", apply))
        except _Budget:
            summary.alert("member_reads_capped")
        except _Stop:
            pass
        except dl.DirectLocalError as exc:
            summary.stopped = exc.code
            summary.alert(exc.code)
            if isinstance(exc, dl.BridgeError) or exc.code.startswith("bridge_"):
                summary.run_day = self.run_day.isoformat() if self.run_day else None
                self.ledger.finish_run(run_id, summary.result(run_id))
                raise
        summary.run_day = self.run_day.isoformat() if self.run_day else None
        self.ledger.finish_run(run_id, summary.result(run_id))
        return summary.result(run_id)

    def busy(self, command: str) -> dict[str, Any]:
        """The type move lock is held (the member core's apply or a type
        move command): nothing is read or written; the next hourly run tries
        again. Only the second busy run in a row alerts."""

        self.ledger.check_binding(self.config)
        previous = self.ledger.last_finished_summary(("expire", "revoke", "scheduled"))
        summary = RunSummary(command)
        summary.stopped = "writer_busy"
        if previous is not None and previous.get("stopped") == "writer_busy":
            summary.alert("writer_busy_again")
        else:
            summary.count("writer_busy")
        run_id = self.ledger.start_run(command)
        self.ledger.finish_run(run_id, summary.result(run_id))
        return summary.result(run_id)

    def _member_blocked(self, member_no: str, core: CoreFacts, summary: RunSummary, ref: str) -> bool:
        if member_no in core.open_members:
            summary.count("core_write_open", ref, member_no)
            return True
        if member_no in core.pending_members:
            summary.count("renewal_pending", ref, member_no)
            return True
        return False

    def _due_cap(self, kind: str, summary: RunSummary, saves_this_kind: int) -> bool:
        """True when this kind may not write again this run (per-run cap or
        the per-day cap, counted from the ledger)."""

        if saves_this_kind >= getattr(self.config, CAP[kind]):
            summary.count(f"{kind}_cap_reached")
            return True
        if self.ledger.removals_today(self.run_day.isoformat()) >= self.config.max_removals_per_day:
            summary.count("day_cap_reached")
            return True
        return False

    # -- expire ---------------------------------------------------------------
    def _scan(self, run_day: date) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        after = ""
        for _ in range(SCAN_MAX_PAGES):
            page, server = self.ac2.scan(after, run_day)
            self._clock(server)
            for row in page:
                if row["member_no"] in seen:
                    raise dl.DirectLocalError("removal_scan_order_invalid")
                seen.add(row["member_no"])
            rows.extend(page)
            if len(page) < SCAN_PAGE_SIZE:
                return rows
            after = page[-1]["member_no"]
        raise dl.DirectLocalError("removal_scan_too_large")

    def _expire(self, core: CoreFacts, summary: RunSummary, *, run_id: str, write: bool) -> None:
        # The run day comes from the server, checked against the VM clock: a
        # first-page probe with the VM's day, then the pages for the server's.
        probe_day = self.run_day or self.now().astimezone(SGT).date()
        rows = self._scan(probe_day)
        if self.run_day != probe_day:
            rows = self._scan(self.run_day)
        run_day = self.run_day
        due: list[dict[str, Any]] = []
        for row in rows:
            member_no = row["member_no"]
            ref = self.ledger.ref("m", member_no)
            if not dl.MEMBER_NO_RE.fullmatch(member_no):
                self._review(f"member_no:{member_no}", "member_no_unsupported", summary, ref)
                continue
            day, problem = expiry_due_day(row.get("expiry_local"))
            if problem:
                self._review(f"expiry:{member_no}", "expiry_unreadable", summary, ref, member_no)
                continue
            if not is_due(day, run_day):
                summary.count("not_due")  # the scan's filter is never trusted
                continue
            if row.get("is_active") == "F":
                summary.count("expired_inactive")
                continue
            if row.get("is_active") != "T":
                self._review(f"active:{member_no}", "active_flag_unexpected", summary, ref, member_no)
                continue
            due.append(row)
        summary.count("due_expires", number=len(due))
        surge = len(due) > self.config.surge_stop_count and not (self.config.surge_ack_until and self.config.surge_ack_until >= run_day)
        if surge:
            self._review(f"surge:{run_day.isoformat()}", "removal_surge", summary, None)
            write = False  # observe only until the owner approves a batch plan
        saves = 0
        for row in due:
            member_no = row["member_no"]
            ref = self.ledger.ref("m", member_no)
            if self._member_blocked(member_no, core, summary, ref):
                continue
            recorded = self.ledger.removal_landed_or_open(member_no)
            if recorded is not None and recorded["state"] != "DONE":
                summary.count("removal_open", ref, member_no)
                continue
            if self.ledger.put_back_after_expire(member_no, row.get("expiry_local")):
                summary.count("put_back_kept", ref, member_no)
                continue
            if not write:
                summary.count("would_expire", ref, member_no)
                continue
            if self._due_cap("expire", summary, saves):
                summary.count("would_expire", number=1)
                continue
            if self._stop_requested():
                summary.stopped = "stop_file_present"
                summary.count("would_expire", ref, member_no)
                raise _Stop()
            read = self._read_member(member_no)
            expiry, problem = expiry_due_day(read.expiry_local) if read.found else (None, "member_not_found")
            if (not read.found or read.member_type != MEMBER or read.is_active != "T" or problem or not is_due(expiry, run_day)
                    or not same_stored(read.expiry_local, row.get("expiry_local")) or not same_stored(read.last_modified_local, row.get("last_modified_local"))
                    or not isinstance(read.last_modified_local, str) or not _STORED_RE.fullmatch(read.last_modified_local)):
                summary.count("changed_since_read", ref, member_no)  # retried next run
                continue
            payload = {"kind": "expire", "member_no": member_no, "from_type": FROM_TYPE, "to_type": TO_TYPE,
                       "expected_last_modified_local": read.last_modified_local, "expected_expiry_local": read.expiry_local,
                       "run_day_local": run_day.isoformat(), "basis_expiry_local": None, "salt": secrets.token_hex(16)}
            if self._remove(payload, summary, run_id=run_id, ref=ref, order_gid=None, core_write_id=None):
                saves += 1

    # -- revoke ---------------------------------------------------------------
    def _link_active(self, receipt: Mapping[str, Any], core: CoreFacts, links: mb.SyncLinks) -> bool:
        member_no, customer = receipt["member_no"], receipt["customer_gid"]
        if receipt["link_source"] == "claim":
            return (customer, sync.member_no_key(member_no)) in core.active_claims
        if receipt["link_source"] == "sync155":
            linked = links.by_gid.get(customer)
            return linked is not None and sync.member_no_key(linked) == sync.member_no_key(member_no)
        return False

    def _qualifies_now(self, receipt: Mapping[str, Any]) -> bool:
        return (receipt["financial_status"] in PAID_STATUSES and receipt["financial_status"] != "REFUNDED"
                and isinstance(receipt["net_cents"], int) and receipt["net_cents"] >= self.config.qualify_threshold_cents)

    def _revoke(self, core: CoreFacts, links: mb.SyncLinks, summary: RunSummary, *, run_id: str, write: bool) -> None:
        saves = 0
        for receipt in core.flagged:
            order_gid, member_no = receipt["order_gid"], receipt["member_no"]
            ref = self.ledger.ref("o", order_gid)
            if self.ledger.revoke_recorded(order_gid):
                summary.count("revoke_recorded")  # never a second send
                continue
            if receipt["code"] != "upgrade":
                self._review(f"extend:{order_gid}", "refund_on_extend", summary, ref, member_no)
                continue
            done = [w for w in core.upgrade_writes.get(order_gid, []) if w["state"] == "DONE"]
            if not done:
                code = "revoke_needs_person"  # a write released as_intended, or no DONE upgrade write
                self._review(f"person:{order_gid}", code, summary, ref, member_no)
                continue
            upgrade = done[-1]
            basis = receipt["basis_expiry_local"]
            if (upgrade["from_type"] != GUEST or upgrade["to_type"] != MEMBER or upgrade["member_no"] != member_no
                    or not isinstance(basis, str) or upgrade["new_expiry_local"] != basis):
                self._review(f"person:{order_gid}", "revoke_needs_person", summary, ref, member_no)
                continue
            if not (receipt["financial_status"] == "REFUNDED"
                    or (isinstance(receipt["net_cents"], int) and receipt["net_cents"] < self.config.qualify_threshold_cents)):
                self._review(f"person:{order_gid}", "revoke_needs_person", summary, ref, member_no)  # money-only refund or a non-REFUNDED cancel
                continue
            others = [r for r in core.receipts_by_member.get(member_no, []) if r["order_gid"] != order_gid
                      and r["alerted"] != REFUND_ON_BASIS and self._qualifies_now(r)]
            if others:
                summary.count("revoke_other_basis", ref, member_no)
                continue
            if not self._link_active(receipt, core, links):
                self._review(f"link:{order_gid}", "revoke_link_voided", summary, ref, member_no)
                continue
            if not dl.MEMBER_NO_RE.fullmatch(member_no):
                self._review(f"member_no:{member_no}", "member_no_unsupported", summary, ref)
                continue
            if self._member_blocked(member_no, core, summary, ref):
                continue
            recorded = self.ledger.removal_landed_or_open(member_no)
            if recorded is not None and recorded["state"] != "DONE":
                summary.count("removal_open", ref, member_no)
                continue
            if write and self._stop_requested():
                summary.stopped = "stop_file_present"
                summary.count("would_revoke", ref, member_no)
                raise _Stop()
            read = self._read_member(member_no)
            expected_expiry = basis + ".000"
            if not read.found or read.member_type != MEMBER or not same_stored(read.expiry_local, expected_expiry) \
                    or upgrade["post_last_modified_local"] is None or not same_stored(read.last_modified_local, upgrade["post_last_modified_local"]) \
                    or not isinstance(read.last_modified_local, str) or not _STORED_RE.fullmatch(read.last_modified_local) \
                    or not isinstance(read.expiry_local, str) or not _STORED_RE.fullmatch(read.expiry_local):
                self._review(f"changed:{order_gid}", "revoke_member_changed", summary, ref, member_no)
                continue
            if read.is_active != "T":
                summary.count("revoke_member_inactive", ref, member_no)
                continue
            if not write:
                summary.count("would_revoke", ref, member_no)
                continue
            if self._due_cap("revoke", summary, saves):
                summary.count("would_revoke")
                continue
            payload = {"kind": "revoke", "member_no": member_no, "from_type": FROM_TYPE, "to_type": TO_TYPE,
                       "expected_last_modified_local": read.last_modified_local, "expected_expiry_local": read.expiry_local,
                       "run_day_local": None, "basis_expiry_local": read.expiry_local, "salt": secrets.token_hex(16)}
            if self._remove(payload, summary, run_id=run_id, ref=ref, order_gid=order_gid, core_write_id=upgrade["write_id"]):
                saves += 1

    # -- the one write path ---------------------------------------------------
    def _remove(self, payload: dict[str, Any], summary: RunSummary, *, run_id: str, ref: str, order_gid: str | None,
                core_write_id: str | None) -> bool:
        """INTENT, the child, the outcome. True when DONE; an unclear write
        stops the run."""

        check_save_payload(payload)
        deadline = (self.now() + timedelta(seconds=self.config.write_deadline_seconds)).replace(microsecond=0)
        row = self.ledger.add_removal(run_id=run_id, payload=payload, day_local=self.run_day.isoformat(), order_gid=order_gid,
                                      core_write_id=core_write_id, deadline=deadline)
        removal_id = row["removal_id"]
        try:
            outcome = self.ac2.save(removal_payload(row), move_id=removal_id, deadline=dl.parse_ts(row["deadline_at"]),
                                    on_started=lambda pid, created: self.ledger.set_child("removals", removal_id, pid, created))
        except Exception:  # noqa: BLE001 - never a blind retry: the child may have saved
            outcome = SaveOutcome("UNCERTAIN", "save_call_failed")
        self.ledger.set_outcome("removals", removal_id, outcome.state, outcome.code, outcome.post_last_modified_local if outcome.state == "DONE" else None)
        member_no = payload["member_no"]
        removal_ref = self.ledger.ref("r", removal_id)
        if outcome.state == "DONE":
            summary.saves += 1
            summary.count("expired" if payload["kind"] == "expire" else "revoked", removal_ref, member_no)
            return True
        if outcome.state in ("SKIPPED", "NOT_WRITTEN"):
            summary.count(outcome.code or "not_written", ref, member_no)  # re-read next run (staff win)
            return False
        summary.stopped = f"{outcome.state.lower()}:{outcome.code}"
        summary.alert(f"write_{outcome.state.lower()}", removal_ref, member_no)
        if outcome.changed_fields:
            summary.counts["changed_fields"] = len(outcome.changed_fields)
        raise _Stop()

    # -- resolve (read-back only) --------------------------------------------
    def _writers_settled(self, rows: list[sqlite3.Row]) -> None:
        now = self.now()
        for row in rows:
            if now < dl.parse_ts(row["deadline_at"]) + timedelta(seconds=dl.WRITER_GRACE_SECONDS):
                raise dl.DirectLocalHold("writer_may_be_live")
            if row["child_pid"] is not None and self.writer_probe(row["child_pid"], row["child_created"]) != "exited":
                raise dl.DirectLocalHold("writer_may_be_live")

    def resolve(self) -> dict[str, Any]:
        self.ledger.check_binding(self.config)
        open_rows = self.ledger.open_rows()
        self._writers_settled([row for _, row in open_rows])
        result: dict[str, Any] = {"status": "ok", "command": "resolve", "writes": 0, "resolved": {}, "alerts": []}
        for table_kind, row in open_rows:
            table = "removals" if table_kind == "removal" else "restores"
            row_id = row["removal_id"] if table_kind == "removal" else row["restore_id"]
            if row["state"] == "INTENT":
                self.ledger.set_outcome(table, row_id, "UNCERTAIN", "parent_interrupted")
            claim = self.ledger.root / "claims" / ("fence-" + row_id[3:] + ".claim")
            if not claim.exists():
                state, code, post = "NOT_WRITTEN", "resolved_no_claim", None
            else:
                state, code, post = self._resolve_one(row, claim)
            self.ledger.set_outcome(table, row_id, state, code, post)
            key = f"{table_kind}:{state}:{code}"
            result["resolved"][key] = result["resolved"].get(key, 0) + 1
            if state == "HELD" and code not in result["alerts"]:
                result["alerts"].append(code)
        return result

    def _resolve_one(self, row: sqlite3.Row, claim: Path) -> tuple[str, str, str | None]:
        try:
            before = json.loads(claim.read_text(encoding="ascii")).get("before_digest")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            before = None
        now = self.ac2.member(row["member_no"], row["salt"])
        if not now.found:
            return "HELD", "resolved_member_missing", None
        if now.member_type == row["to_type"] and now.expiry_local == row["expected_expiry_local"] and isinstance(before, str) and now.fields_digest == before:
            if isinstance(now.last_modified_local, str) and _STORED_RE.fullmatch(now.last_modified_local):
                return "DONE", "resolved_saved", now.last_modified_local
            return "HELD", "resolved_last_modified_missing", None
        if (now.member_type == row["from_type"] and now.last_modified_local == row["expected_last_modified_local"]
                and now.expiry_local == row["expected_expiry_local"]):
            return "NOT_WRITTEN", "resolved_not_written", None
        return "HELD", "resolved_unclear", None

    # -- release-held ---------------------------------------------------------
    def _find_by_ref(self, removal_ref: Any, member_last3: Any) -> sqlite3.Row:
        if (not isinstance(removal_ref, str) or not _REMOVAL_REF_RE.fullmatch(removal_ref) or not isinstance(member_last3, str)
                or not _LAST3_RE.fullmatch(member_last3)):
            raise dl.DirectLocalError("removal_arguments_invalid")
        for row in self.ledger.conn.execute("SELECT * FROM removals ORDER BY rowid"):
            if hmac.compare_digest(self.ledger.ref("r", row["removal_id"]), removal_ref):
                if row["member_no"][-3:] != member_last3:
                    raise dl.DirectLocalError("removal_member_mismatch")
                return row
        raise dl.DirectLocalError("removal_not_found")

    def release_held(self, *, removal_ref: Any, member_last3: Any, finding: Any, apply: bool) -> dict[str, Any]:
        """Clear the latch of one HELD removal (or of the HELD restore of that
        removal) after the owner checked the member in AutoCount. One read
        proves the finding; nothing is sent to AutoCount; the release is a
        new row. A dry run unless ``apply``."""

        if finding not in RELEASE_FINDINGS:
            raise dl.DirectLocalError("removal_arguments_invalid")
        self.ledger.check_binding(self.config)
        if self.ledger.open_rows():
            raise dl.DirectLocalError("removal_uncertain_unresolved")
        removal = self._find_by_ref(removal_ref, member_last3)
        held = [(kind, row) for kind, row in self.ledger.unreleased_held()
                if (row["removal_id"] == removal["removal_id"])]
        if not held:
            raise dl.DirectLocalError("release_no_held_write")
        kind, write = held[0]
        self._writers_settled([write])
        read = self.ac2.member(write["member_no"], secrets.token_hex(16))
        intended = read.found and read.member_type == write["to_type"] and read.expiry_local == write["expected_expiry_local"]
        if intended != (finding == "as_intended"):
            raise dl.DirectLocalError("release_finding_not_proven")
        proof = read.fields_digest if read.found else "sha256:" + hashlib.sha256(b"removal_member_not_found").hexdigest()
        if not isinstance(proof, str) or not _DIGEST_RE.fullmatch(proof):
            raise dl.BridgeError("bridge_output_invalid")
        run_id = f"run-{uuid.uuid4().hex}" if apply else None
        result = {"status": "ok", "command": "release_held" if apply else "release_held_dry_run", "run_id": run_id, "finding": finding,
                  "write_kind": kind, "held_writes_left": len(self.ledger.unreleased_held()) - 1, "counts": {f"{kind}_released": 1},
                  "refs": [{"ref": removal_ref, "code": "write_released", "member_no_masked": mask(write["member_no"])}], "alerts": []}
        if not apply:
            return result
        now = dl.ts(self.now())
        write_id = write["removal_id"] if kind == "removal" else write["restore_id"]
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO write_releases(write_id, finding, member_read_digest, run_id, created_at) VALUES (?, ?, ?, ?, ?)",
                         (write_id, finding, proof, run_id, now))
            conn.execute("INSERT INTO runs(run_id, command, started_at, finished_at, summary) VALUES (?, 'release_held', ?, ?, ?)",
                         (run_id, now, now, json.dumps(result, sort_keys=True)))
        return result

    # -- restore (tool undo) --------------------------------------------------
    def restore(self, *, removal_ref: Any = None, member_last3: Any = None, run: Any = None, max_rows: int | None = None,
                apply: bool) -> dict[str, Any]:
        """Undo: one removal by its ``r-`` ref, or the last N DONE removals of
        one run (latest first), through profile ``benefit_x1r``. A dry run
        unless ``apply`` (needs ``restore_enabled``)."""

        if apply and not self.config.restore_enabled:
            raise dl.DirectLocalError("restore_disabled")
        if (removal_ref is None) == (run is None):
            raise dl.DirectLocalError("restore_arguments_invalid")
        self._preflight(writes=apply)
        if removal_ref is not None:
            targets = [self._find_by_ref(removal_ref, member_last3)]
        else:
            if not isinstance(run, str) or not _RUN_ID_RE.fullmatch(run) or not isinstance(max_rows, int) or not 1 <= max_rows <= 50:
                raise dl.DirectLocalError("restore_arguments_invalid")
            targets = self.ledger.conn.execute("SELECT * FROM undo_list WHERE run_id = ? ORDER BY seq DESC LIMIT ?", (run, max_rows)).fetchall()
            if not targets:
                raise dl.DirectLocalError("restore_nothing_to_undo")
        summary = RunSummary("restore" if apply else "restore_dry_run")
        run_id = self.ledger.start_run(summary.command)
        try:
            for removal in targets:
                self._restore_one(removal, summary, run_id=run_id, apply=apply)
        except _Budget:
            summary.alert("member_reads_capped")
        except _Stop:
            pass
        except dl.DirectLocalError as exc:
            summary.stopped = exc.code
            summary.alert(exc.code)
            self.ledger.finish_run(run_id, summary.result(run_id))
            raise
        self.ledger.finish_run(run_id, summary.result(run_id))
        return summary.result(run_id)

    def _restore_one(self, removal: sqlite3.Row, summary: RunSummary, *, run_id: str, apply: bool) -> None:
        member_no = removal["member_no"]
        removal_ref = self.ledger.ref("r", removal["removal_id"])
        if removal["state"] != "DONE":
            summary.count("restore_not_done", removal_ref, member_no)
            return
        if self.ledger.conn.execute(f"SELECT 1 FROM restores WHERE removal_id = ? AND state IN {_LANDED_OR_OPEN}", (removal["removal_id"],)).fetchone():
            summary.count("restore_already_recorded", removal_ref, member_no)
            return
        if self._stop_requested():
            summary.stopped = "stop_file_present"
            raise _Stop()
        read = self._read_member(member_no)
        if (not read.found or read.member_type != GUEST or read.last_modified_local != removal["post_last_modified_local"]
                or read.expiry_local != removal["expected_expiry_local"]):
            summary.count("changed_since_removal", removal_ref, member_no)  # a person decides
            return
        if not apply:
            summary.count("would_restore", removal_ref, member_no)
            return
        deadline = (self.now() + timedelta(seconds=self.config.write_deadline_seconds)).replace(microsecond=0)
        row = self.ledger.add_restore(run_id=run_id, removal=removal, salt=secrets.token_hex(16), deadline=deadline)
        restore_id = row["restore_id"]
        try:
            outcome = self.ac2.restore(restore_payload(row), move_id=restore_id, deadline=dl.parse_ts(row["deadline_at"]),
                                       on_started=lambda pid, created: self.ledger.set_child("restores", restore_id, pid, created))
        except Exception:  # noqa: BLE001 - never a blind retry
            outcome = SaveOutcome("UNCERTAIN", "save_call_failed")
        self.ledger.set_outcome("restores", restore_id, outcome.state, outcome.code, outcome.post_last_modified_local if outcome.state == "DONE" else None)
        if outcome.state == "DONE":
            summary.saves += 1
            summary.count("restored", removal_ref, member_no)
            return
        if outcome.state in ("SKIPPED", "NOT_WRITTEN"):
            summary.count(outcome.code or "not_written", removal_ref, member_no)
            return
        summary.stopped = f"{outcome.state.lower()}:{outcome.code}"
        summary.alert(f"restore_{outcome.state.lower()}", removal_ref, member_no)
        raise _Stop()

    # -- status -----------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        self.ledger.check_binding(self.config)
        conn = self.ledger.conn
        removals = {f"{row[0]}:{row[1]}": row[2] for row in conn.execute("SELECT kind, state, COUNT(*) FROM removals GROUP BY kind, state")}
        restores = {row[0]: row[1] for row in conn.execute("SELECT state, COUNT(*) FROM restores GROUP BY state")}
        return {"status": "ok", "command": "status", "removals": removals, "restores": restores,
                "open_writes": len(self.ledger.open_rows()), "held_writes": len(self.ledger.unreleased_held()),
                "undo_list": conn.execute("SELECT COUNT(*) FROM undo_list").fetchone()[0],
                "reviews": conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
                "rule_settings_match": self.ledger.rule_settings_match(self.config), "stop_file_present": self._stop_requested(),
                "expire_write_enabled": self.config.expire_write_enabled, "revoke_write_enabled": self.config.revoke_write_enabled,
                "restore_enabled": self.config.restore_enabled}


# --------------------------------------------------------------------------
# Read-only views: digest and undo list
# --------------------------------------------------------------------------

DIGEST_SCHEMA = "xb.directlocal.member_removals.digest.v1"
_DIGEST_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_DIGEST_REF_RE = re.compile(r"^[rom]-[0-9a-f]{20}$")
_MASK_RE = re.compile(r"^\*{3,}[^*]{1,3}$")
UNDO_LIST_MAX = 500


def _open_own_read_only(root: Path, config: RemovalsConfig) -> tuple[sqlite3.Connection, sqlite3.Row]:
    path = Path(root) / LEDGER_FILE
    if not path.is_file():
        raise dl.DirectLocalError("removals_ledger_missing")
    try:
        conn = open_read_only(path)
    except sqlite3.Error:
        raise dl.DirectLocalError("removals_ledger_unreadable") from None
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise dl.DirectLocalError("removals_ledger_not_recognised")
        meta = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        if meta is None:
            raise dl.DirectLocalError("removals_ledger_not_initialised")
        RemovalsLedger._check(meta, config)
        return conn, meta
    except sqlite3.Error:
        conn.close()
        raise dl.DirectLocalError("removals_ledger_unreadable") from None
    except BaseException:
        conn.close()
        raise


def ledger_digest(root: Path, *, hours: int, now: datetime, config: RemovalsConfig) -> dict[str, Any]:
    """Read-only daily digest: runs, counts and alerts per code, alert refs
    (keyed refs and masked MemberNos only), the writes of the last N hours
    and the open totals."""

    conn, meta = _open_own_read_only(root, config)
    try:
        since = dl.ts(now - timedelta(hours=hours))
        counts: dict[str, int] = {}
        alerts: dict[str, int] = {}
        refs: list[dict[str, str]] = []
        completed = unfinished = 0
        for row in conn.execute("SELECT finished_at, summary FROM runs WHERE started_at >= ? ORDER BY started_at", (since,)):
            if row["finished_at"] is None:
                unfinished += 1
                continue
            completed += 1
            try:
                stored = json.loads(row["summary"] or "{}")
            except (json.JSONDecodeError, RecursionError):
                stored = {}
            stored = stored if isinstance(stored, dict) else {}
            for code, number in (stored.get("counts") or {}).items():
                if isinstance(code, str) and _DIGEST_CODE_RE.fullmatch(code) and isinstance(number, int) and not isinstance(number, bool):
                    counts[code] = counts.get(code, 0) + number
            run_alerts = [c for c in stored.get("alerts") or [] if isinstance(c, str) and _DIGEST_CODE_RE.fullmatch(c)]
            for code in run_alerts:
                alerts[code] = alerts.get(code, 0) + 1
            for entry in stored.get("refs") or []:
                if not isinstance(entry, dict) or entry.get("code") not in run_alerts or len(refs) >= 200:
                    continue
                if not isinstance(entry.get("ref"), str) or not _DIGEST_REF_RE.fullmatch(entry["ref"]):
                    continue
                item = {"ref": entry["ref"], "code": entry["code"]}
                masked = entry.get("member_no_masked")
                if isinstance(masked, str) and _MASK_RE.fullmatch(masked):
                    item["member_no_masked"] = masked
                refs.append(item)
        window = {f"{row[0]}": row[1] for row in conn.execute("SELECT kind, COUNT(*) FROM removals WHERE state = 'DONE' AND updated_at >= ? GROUP BY kind", (since,))}
        restored = conn.execute("SELECT COUNT(*) FROM restores WHERE state = 'DONE' AND updated_at >= ?", (since,)).fetchone()[0]
        return {
            "schema": DIGEST_SCHEMA, "status": "ok", "autocount_book": meta["autocount_book"], "hours": hours,
            "rule_settings_match": hmac.compare_digest(meta["rule_fingerprint"], config.rule_fingerprint()),
            "runs": {"completed": completed, "unfinished": unfinished}, "counts": dict(sorted(counts.items())),
            "alerts": dict(sorted(alerts.items())), "alert_refs": refs,
            "window": {"expired": window.get("expire", 0), "revoked": window.get("revoke", 0), "restored": restored},
            "open": {
                "uncertain_writes": conn.execute(f"SELECT (SELECT COUNT(*) FROM removals WHERE state IN {_OPEN_STATES}) + "
                                                 f"(SELECT COUNT(*) FROM restores WHERE state IN {_OPEN_STATES})").fetchone()[0],
                "held_writes": conn.execute("SELECT (SELECT COUNT(*) FROM removals w WHERE w.state = 'HELD' AND NOT EXISTS "
                                            "(SELECT 1 FROM write_releases x WHERE x.write_id = w.removal_id)) + "
                                            "(SELECT COUNT(*) FROM restores w WHERE w.state = 'HELD' AND NOT EXISTS "
                                            "(SELECT 1 FROM write_releases x WHERE x.write_id = w.restore_id))").fetchone()[0],
                "undo_list": conn.execute("SELECT COUNT(*) FROM undo_list").fetchone()[0],
            },
        }
    except sqlite3.Error:
        raise dl.DirectLocalError("removals_ledger_unreadable") from None
    finally:
        conn.close()


def undo_listing(root: Path, *, config: RemovalsConfig, since: date | None = None, kind: str | None = None, full: bool = False) -> dict[str, Any]:
    """The undo list (DONE removals with no DONE restore), opened as the
    digest is. ``full`` keeps the full MemberNo and the ExpiryDate it kept:
    for ``show_undo`` (the VM screen) only, never ``_emit``."""

    if kind is not None and kind not in KINDS:
        raise dl.DirectLocalError("undo_list_kind_invalid")
    conn, meta = _open_own_read_only(root, config)
    try:
        query = "SELECT removal_id, kind, member_no, day_local, expected_expiry_local FROM undo_list"
        clauses, params = [], []
        if since is not None:
            clauses.append("day_local >= ?")
            params.append(since.isoformat())
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        rows = conn.execute(query + " ORDER BY day_local DESC, seq DESC", params).fetchall()
        by_day: dict[str, dict[str, int]] = {}
        entries = []
        for row in rows:
            by_day.setdefault(row["day_local"], {"expire": 0, "revoke": 0})[row["kind"]] += 1
            if len(entries) < UNDO_LIST_MAX:
                entry = {"ref": mb.keyed_ref(meta["ref_key"], "r", row["removal_id"]), "kind": row["kind"], "day": row["day_local"]}
                if full:
                    entry.update(member_no=row["member_no"], expiry_local=row["expected_expiry_local"])
                else:
                    entry["member_no_masked"] = mask(row["member_no"])
                entries.append(entry)
        return {"status": "ok", "command": "undo_list", "total": len(rows), "by_day": dict(sorted(by_day.items())), "removals": entries,
                "truncated": len(rows) > UNDO_LIST_MAX}
    except sqlite3.Error:
        raise dl.DirectLocalError("removals_ledger_unreadable") from None
    finally:
        conn.close()


def show_undo(listing: Mapping[str, Any]) -> None:
    """Plain text for the VM screen: the list staff need for a hand undo."""

    write = sys.stdout.write
    write(f"Removals that can be undone: {listing['total']}\n")
    write("VM screen only: full MemberNos. Do not copy them into email, chat, issues or logs.\n")
    write("Hand undo: set the member back to MEMBER in AutoCount. Nothing else changed (ExpiryDate was never written).\n")
    for number, entry in enumerate(listing["removals"], start=1):
        write(f"\n{number}. {entry['kind']} on {entry['day']} (Singapore day), ref {entry['ref']}\n")
        write(f"   MemberNo: {json.dumps(entry['member_no'])}   ExpiryDate kept: {entry['expiry_local']}\n")
    if listing["truncated"]:
        write(f"\n... {listing['total'] - len(listing['removals'])} more not shown (at most {UNDO_LIST_MAX}).\n")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def build_real_engine(config: RemovalsConfig, ledger: RemovalsLedger, *, environ: Mapping[str, str]) -> RemovalsEngine:
    env = None
    if config.ac2_credential_target is not None:
        env = sync.load_ac2_credential(config.ac2_credential_target, refused_user_ids=config.ac2_refused_user_ids).child_environment(environ)
    elif config.autocount_book == sync.LIVE_BOOK:
        raise dl.DirectLocalError("ac2_credential_target_unconfigured")  # parse_config refuses this first
    ac2 = RemovalsBridgeClient(dl.default_bridge_command(BRIDGE_SCRIPT), autocount_book=config.autocount_book, claims=ledger.root / "claims",
                               env=env, deadline_seconds=config.write_deadline_seconds)
    return RemovalsEngine(config, ledger, ac2=ac2)


COMMANDS = ("check-config", "init", "status", "expire", "revoke", "scheduled", "resolve", "release-held", "undo-list", "restore", "digest")
RUN_KINDS = {"expire": ("expire",), "revoke": ("revoke",), "scheduled": ("revoke", "expire")}


def main(argv: list[str] | None = None, *, engine_factory: Callable[[RemovalsConfig, RemovalsLedger], RemovalsEngine] | None = None,
         clock: Callable[[], datetime] = dl.utc_now, core_loader: Callable[[str], mb.BenefitsConfig] = mb.load_config) -> int:
    parser = argparse.ArgumentParser(description="#268 slice E: member removal in AutoCount (expire -> GUEST, refund revoke), undo list")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true", help="expire, revoke, scheduled: write (needs the kind's switch); "
                                                             "restore, release-held: record (default: dry run)")
    parser.add_argument("--removal-ref", help="release-held, restore: the r- keyed ref of one removal")
    parser.add_argument("--member-last3", help="release-held, restore: the last 3 characters of the MemberNo")
    parser.add_argument("--finding", help="release-held only: as_intended or not_as_intended (what the owner saw in AutoCount)")
    parser.add_argument("--run", help="restore only: the run_id whose DONE removals are undone (with --max)")
    parser.add_argument("--max", type=int, help="restore --run only: removals undone, latest first (1-50)")
    parser.add_argument("--since", help="undo-list only: Singapore day YYYY-MM-DD")
    parser.add_argument("--kind", help="undo-list only: expire or revoke")
    parser.add_argument("--show", action="store_true", help="undo-list only: plain text with full MemberNos, VM screen only")
    parser.add_argument("--hours", type=int, default=24, help="digest only: the last N hours of runs (1-720)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, core_loader=core_loader)
        if args.command == "check-config":
            _emit({"status": "ok", **config.summary()})
            return 0
        kinds = RUN_KINDS.get(args.command)
        # Every write switch is refused before any login, read or ledger write.
        if kinds is not None and args.apply and not any(getattr(config, WRITE_SWITCH[k]) for k in kinds):
            raise dl.DirectLocalError("removals_write_disabled")
        if args.command == "restore" and args.apply and not config.restore_enabled:
            raise dl.DirectLocalError("restore_disabled")
        root = state_root(config)
        if args.command == "digest":
            if not 1 <= args.hours <= 720:
                raise dl.DirectLocalError("digest_hours_invalid")
            _emit(ledger_digest(root, hours=args.hours, now=clock(), config=config))
            return 0
        if args.command == "undo-list":
            since = None
            if args.since is not None:
                if not _DAY_RE.fullmatch(args.since):
                    raise dl.DirectLocalError("undo_list_since_invalid")
                try:
                    since = date.fromisoformat(args.since)
                except ValueError:
                    raise dl.DirectLocalError("undo_list_since_invalid") from None
            listing = undo_listing(root, config=config, since=since, kind=args.kind, full=args.show)
            if args.show:
                show_undo(listing)  # VM screen only: never _emit
            else:
                _emit(listing)
            return 0
        ledger = RemovalsLedger(root, clock=clock)
        try:
            if args.command == "init":
                _emit(ledger.init_meta(config))
                return 0
            ledger.check_binding(config)
            if args.command == "status":
                _emit(RemovalsEngine(config, ledger, ac2=None).status())
                return 0
            factory = engine_factory or (lambda cfg, led: build_real_engine(cfg, led, environ=os.environ))
            engine = factory(config, ledger)
            if kinds is not None:
                if args.apply:
                    try:
                        with type_move_lock(config):
                            result = engine.run(kinds, apply=True, command=args.command)
                    except dl.DirectLocalError as exc:
                        if exc.code != "type_move_running":
                            raise
                        result = engine.busy(args.command)
                else:
                    result = engine.run(kinds, apply=False, command=args.command)
            elif args.command == "resolve":
                result = engine.resolve()
            elif args.command == "release-held":
                result = engine.release_held(removal_ref=args.removal_ref, member_last3=args.member_last3, finding=args.finding, apply=args.apply)
            else:
                if args.apply:
                    with type_move_lock(config):
                        result = engine.restore(removal_ref=args.removal_ref, member_last3=args.member_last3, run=args.run, max_rows=args.max, apply=True)
                else:
                    result = engine.restore(removal_ref=args.removal_ref, member_last3=args.member_last3, run=args.run, max_rows=args.max, apply=False)
            _emit(result)
            return 3 if result.get("alerts") else 0
        finally:
            ledger.close()
    except dl.DirectLocalError as exc:
        _emit({"status": "refused", "code": exc.code})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
