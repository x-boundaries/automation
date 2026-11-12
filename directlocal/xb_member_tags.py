"""#268 slice C1: the Shopify customer tag ``member-qualified``, kept right
from AutoCount (the member 15% discount points at a segment of that tag).

Scope: the G0 slice C design (Controller brief, 12 Nov 2026), cut C1. Runs on
the AutoCount VM from its own clone, state root, config and Shopify app (one
scope, ``write_customers``). Nothing runs on merge; the default is ``observe``.

What one run does (``observe`` or ``apply``)
--------------------------------------------
1. Checks the Shopify session: store, domain and the app's scopes (exactly
   ``{write_customers}`` or exactly ``{read_customers, write_customers}``).
2. Reads two link ledgers read-only (SQLite ``mode=ro``, ``query_only``,
   never copied or written): the #155 sign-up sync's ``links`` and the #268
   member core's active claim links, plus the member core's voided pairs.
3. Reads AutoCount read-only through the type move's guarded reads (scan of
   ``MEMBER``, ``GUEST`` and ``Default``: MemberNo, IsActive, ExpiryDate),
   plus a capped single read of a linked member found in no scan. The read
   client refuses every write operation.
4. Reads every linked customer by ID in Shopify (tags, and whether
   ``membership.start_date`` exists), and searches for customers that carry
   the tag, each then read by ID.
5. Decides per customer (design 1.6): the tag goes on only for one clean link
   to an active ``MEMBER`` whose expiry is today or later (Singapore date); it
   comes off only on a positive fact (inactive, expired, not a ``MEMBER``, or
   the customer's only link was voided by the member core). Everything else is
   left alone with an alert.
6. ``apply`` only (switch ``tags_write_enabled``, per-run caps, volume guard,
   ``STOP`` file): ``tagsAdd`` / ``tagsRemove`` with exactly
   ``["member-qualified"]`` on a customer this run read by ID. An INTENT row is
   committed before each mutation; an unproven answer stops the run and is
   settled by the next run's read, never by sending again.

Never: any other Shopify mutation (no customer create, update or delete, no
metafield), any AutoCount write (no save child is ever started), the birthday
tag (slice D), the removal of a tag that has no basis (slice C2).

Privacy: the ledger (private state root on the VM) keeps customer GIDs,
MemberNos, this tool's tag operations, codes and counts. Output carries codes,
counts, keyed refs (``c-`` customer, ``m-`` member) and MemberNos masked to the
last 3 characters only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
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
from typing import Any, Callable, Iterable, Iterator, Mapping

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.append(str(_HERE))

import xb_directlocal as dl  # noqa: E402
import xb_member_sync as sync  # noqa: E402
import xb_member_type_move as tm  # noqa: E402
import xb_shopify_direct as direct  # noqa: E402

CONFIG_SCHEMA = "xb.directlocal.member_tags.config.v1"
LEDGER_FILE = "member_tags.sqlite3"
LOCK_FILE = "member_tags.lock"
STOP_FILE = "STOP"
TOOL = "xb268_member_tags"
SCHEMA_VERSION = 1
TOKEN_CACHE_FILE = "tags_token_cache.bin"

# Every tag this tool may ever send, and the ones it sends in this slice.
# member-bday-month is slice D: in the set (the ledger CHECK), never sent now.
TAG = "member-qualified"
TAG_SET = ("member-qualified", "member-bday-month")
ACTIVE_TAGS = ("member-qualified",)

# The two input ledgers, pinned as literals (this module does not import the
# member core). tests/test_member_tags.py proves they equal the tools' own
# constants, so a version bump there fails here instead of being misread.
SYNC_LEDGER_FILE = "member_sync.sqlite3"
SYNC_LEDGER_VERSIONS = (3,)
MEMBER_CORE_LEDGER_FILE = "member_benefits.sqlite3"
MEMBER_CORE_TOOL = "xb268_member_benefits"
MEMBER_CORE_LEDGER_VERSIONS = (2,)
MEMBER_CORE_CONFIG_SCHEMA = "xb.directlocal.member_benefits.config.v1"
TYPE_MOVE_LEDGER_FILE = tm.LEDGER_FILE
CORE_LEDGER_FILE = "ledger.sqlite3"

# AutoCount: the type move's guarded reads, read-only.
MEMBER = tm.MEMBER
SCAN_TYPES = tm.SCAN_TYPES  # Default, MEMBER, GUEST
SCAN_PAGE_SIZE = tm.SCAN_PAGE_SIZE
SCAN_MAX_PAGES = tm.SCAN_MAX_PAGES
READ_OPERATIONS = ("type-move-scan", "type-move-member")
BRIDGE_SCRIPT = tm.BRIDGE_SCRIPT
SGT = sync.SGT
# An ExpiryDate before this day is a placeholder (R2 3.1 stage 2: 2000-01-01),
# read as unreadable, never as expired.
EXPIRY_FLOOR = date(2020, 1, 1)
REGISTRATION_TAG = sync.REGISTRATION_TAG  # "member-mg": the MG member dates Flow's tag

# Shopify: the scope sets this app's token may hold (shopify.dev does not say
# which list a write_customers-only token reports; both are pinned).
SCOPE_SETS = {
    frozenset({"write_customers"}): "write_only",
    frozenset({"read_customers", "write_customers"}): "read_write",
}

_CUSTOMER_GID_RE = re.compile(r"^gid://shopify/Customer/([1-9][0-9]{0,19})\Z")
_LAST3_RE = re.compile(r"^[A-Za-z0-9._/-]{1,3}\Z")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

_CONFIG_REQUIRED = {"schema", "state_root", "autocount_book", "shopify", "member_sync_state_root", "member_core_state_root"}
_CONFIG_DEFAULTS: dict[str, Any] = {
    # Observe is the default: no mutation is reachable while this is false.
    "tags_write_enabled": False,
    "max_tag_adds_per_run": 10,
    "max_tag_removes_per_run": 5,
    "max_member_reads_per_run": 20,
    "shopify_max_carrier_pages": 60,
    "http_timeout_seconds": 30,
    "ac2_credential_target": None,
    "ac2_login_mode": None,
    "ac2_refused_user_ids": [],
}
_BOUNDS = {
    "max_tag_adds_per_run": (1, 250),
    "max_tag_removes_per_run": (1, 50),
    "max_member_reads_per_run": (1, 100),
    "shopify_max_carrier_pages": (1, 100),
    "http_timeout_seconds": (5, 60),
}
OTHER_TOOL_SCHEMAS = (sync.CONFIG_SCHEMA, tm.CONFIG_SCHEMA, MEMBER_CORE_CONFIG_SCHEMA)


@dataclass(frozen=True)
class TagsConfig:
    state_root: str
    autocount_book: str
    shopify: sync.ShopifyConnection
    member_sync_state_root: str
    member_core_state_root: str
    tags_write_enabled: bool
    max_tag_adds_per_run: int
    max_tag_removes_per_run: int
    max_member_reads_per_run: int
    shopify_max_carrier_pages: int
    http_timeout_seconds: int
    ac2_credential_target: str | None
    ac2_login_mode: str | None
    ac2_refused_user_ids: tuple[str, ...]

    def summary(self) -> dict[str, Any]:
        """PII-free view for ``check-config``."""

        return {
            "status": "ok", "autocount_book": self.autocount_book, "api_version": self.shopify.api_version,
            "scope_sets": sorted(SCOPE_SETS.values()), "tag": TAG, "tags_write_enabled": self.tags_write_enabled,
            "max_tag_adds_per_run": self.max_tag_adds_per_run, "max_tag_removes_per_run": self.max_tag_removes_per_run,
            "max_member_reads_per_run": self.max_member_reads_per_run, "shopify_max_carrier_pages": self.shopify_max_carrier_pages,
            "ac2_credential": "credential_manager" if self.ac2_credential_target else "owner_console_environment",
            "ac2_login_mode": self.ac2_login_mode,
        }


def parse_config(value: Any) -> TagsConfig:
    if not isinstance(value, dict):
        raise dl.DirectLocalError("tags_config_invalid")
    if value.get("schema") in OTHER_TOOL_SCHEMAS:
        raise dl.DirectLocalError("tags_config_is_other_tool")
    keys = set(value)
    if not _CONFIG_REQUIRED <= keys or not keys <= _CONFIG_REQUIRED | set(_CONFIG_DEFAULTS):
        raise dl.DirectLocalError("tags_config_keys_invalid")
    if value["schema"] != CONFIG_SCHEMA:
        raise dl.DirectLocalError("tags_config_schema_invalid")
    merged = {**_CONFIG_DEFAULTS, **value}
    book = sync.check_book(merged["autocount_book"])
    live = book == sync.LIVE_BOOK
    for key in ("state_root", "member_sync_state_root", "member_core_state_root"):
        if not isinstance(merged[key], str) or not merged[key]:
            raise dl.DirectLocalError(f"tags_config_{key}_invalid")
    numbers = {}
    for key, (low, high) in _BOUNDS.items():
        number = merged[key]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise dl.DirectLocalError(f"tags_config_{key}_invalid")
        numbers[key] = number
    try:
        shopify = sync._parse_shopify(merged["shopify"], numbers["http_timeout_seconds"])
    except dl.DirectLocalError as exc:
        raise dl.DirectLocalError(exc.code.replace("sync_config_", "tags_config_", 1)) from None
    if not isinstance(merged["tags_write_enabled"], bool):
        raise dl.DirectLocalError("tags_config_tags_write_enabled_invalid")
    target = merged["ac2_credential_target"]
    if target is not None and (not isinstance(target, str) or not sync.CREDENTIAL_TARGET_RE.fullmatch(target)):
        raise dl.DirectLocalError("tags_config_ac2_credential_target_invalid")
    if live and target != sync.LIVE_CREDENTIAL_TARGET:
        raise dl.DirectLocalError("tags_config_ac2_credential_target_live_required")
    if not live and target is not None and target.strip().casefold() == sync.LIVE_CREDENTIAL_TARGET.casefold():
        raise dl.DirectLocalError("tags_config_ac2_credential_target_live_only")
    login_mode = merged["ac2_login_mode"]
    if login_mode is not None and login_mode not in sync.AC2_LOGIN_MODES:
        raise dl.DirectLocalError("tags_config_ac2_login_mode_invalid")
    if live and login_mode is None:
        raise dl.DirectLocalError("tags_config_ac2_login_mode_live_required")
    refused = merged["ac2_refused_user_ids"]
    if not isinstance(refused, list) or len(refused) > 10 or any(not isinstance(u, str) or not sync.AC2_USER_ID_RE.fullmatch(u) for u in refused):
        raise dl.DirectLocalError("tags_config_ac2_refused_user_ids_invalid")
    if login_mode == "owner_shared" and refused:
        raise dl.DirectLocalError("tags_config_ac2_refused_user_ids_owner_shared_refused")
    if live and login_mode == "dedicated" and not refused:
        raise dl.DirectLocalError("tags_config_ac2_refused_user_ids_live_required")
    return TagsConfig(
        state_root=merged["state_root"], autocount_book=book, shopify=shopify, member_sync_state_root=merged["member_sync_state_root"],
        member_core_state_root=merged["member_core_state_root"], tags_write_enabled=merged["tags_write_enabled"],
        ac2_credential_target=target, ac2_login_mode=login_mode, ac2_refused_user_ids=tuple(refused), **numbers,
    )


def load_config(path: str | Path) -> TagsConfig:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise dl.DirectLocalError("tags_config_unreadable") from None
    if len(raw) > 65_536:
        raise dl.DirectLocalError("tags_config_invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise dl.DirectLocalError("tags_config_invalid") from None
    return parse_config(value)


def state_root(config: TagsConfig) -> Path:
    """The tool's own private root: never another tool's, and never one of
    its two input roots (and those two differ from each other)."""

    root = dl.validate_state_root(config.state_root)
    for other in (SYNC_LEDGER_FILE, MEMBER_CORE_LEDGER_FILE, TYPE_MOVE_LEDGER_FILE, CORE_LEDGER_FILE):
        if (root / other).exists():
            raise dl.DirectLocalError("tags_state_root_shared")
    roots = [root.resolve(), Path(config.member_sync_state_root).resolve(), Path(config.member_core_state_root).resolve()]
    if len(set(roots)) != len(roots):
        raise dl.DirectLocalError("tags_state_root_shared")
    dl.verify_private_state(root)
    return root


def mask(member_no: str | None) -> str:
    return sync.mask_last3(member_no)


def customer_number(gid: str) -> int:
    """The numeric part of a customer GID (the deterministic order)."""

    match = _CUSTOMER_GID_RE.fullmatch(gid) if isinstance(gid, str) else None
    if match is None:
        raise dl.DirectLocalError("tags_customer_gid_invalid")
    return int(match.group(1))


# --------------------------------------------------------------------------
# Inputs: the two link ledgers (read only, design 1.1)
# --------------------------------------------------------------------------

LINK_CONFLICT_CODES = ("link_conflict_customer", "link_conflict_member", "link_voided_pair_active_in_sync")
SYNC_LINKS_SQL = "SELECT customer_gid, member_no, member_no_key FROM links"
ACTIVE_CLAIMS_SQL = "SELECT customer_gid, member_no, member_no_key FROM active_claim_links"
# G2 M2 (#155 recheck 1): the one text in this module that reads the member
# core's raw claim_links. A voided pair is history, never a basis.
VOIDED_PAIRS_SQL = "SELECT c.customer_gid, c.member_no_key FROM claim_links c JOIN claim_unlinks u ON u.link_id = c.link_id"


def open_read_only(path: Path) -> sqlite3.Connection:
    """Another tool's ledger: SQLite ``mode=ro`` plus ``query_only``, never
    copied, never written; the caller closes it after the read."""

    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _read_input(path: Path, prefix: str, versions: tuple[int, ...], config: TagsConfig, tool: str | None,
                read: Callable[[sqlite3.Connection], Any]) -> Any:
    """The four checks before any read (file, open, version, binding), then
    ``read``; each failure is one refusal code with ``prefix``."""

    if not path.is_file():
        raise dl.DirectLocalError(f"{prefix}_missing")
    try:
        conn = open_read_only(path)
    except sqlite3.Error:
        raise dl.DirectLocalError(f"{prefix}_unreadable") from None
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] not in versions:
            raise dl.DirectLocalError(f"{prefix}_version_unknown")
        columns = "tool, autocount_book, store_gid, shop_domain" if tool is not None else "autocount_book, store_gid, shop_domain"
        meta = conn.execute(f"SELECT {columns} FROM meta WHERE id = 1").fetchone()
        expected = (config.autocount_book, config.shopify.store_gid, config.shopify.shop_domain)
        if meta is None or tuple(meta) != ((tool,) if tool is not None else ()) + expected:
            raise dl.DirectLocalError(f"{prefix}_binding_mismatch")
        return read(conn)
    except sqlite3.Error:
        raise dl.DirectLocalError(f"{prefix}_unreadable") from None
    finally:
        conn.close()


def read_sync_links(config: TagsConfig) -> list[tuple[str, str]]:
    """The #155 sync ledger's links ``(customer_gid, member_no)``."""

    return _read_input(Path(config.member_sync_state_root) / SYNC_LEDGER_FILE, "sync_ledger", SYNC_LEDGER_VERSIONS, config, None,
                       lambda conn: [(row["customer_gid"], row["member_no"]) for row in conn.execute(SYNC_LINKS_SQL)])


def read_member_core(config: TagsConfig) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The member core v2 ledger: active claims ``(customer_gid, member_no)``
    and voided pairs ``(customer_gid, member_no_key)``."""

    def read(conn: sqlite3.Connection) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        active = [(row["customer_gid"], row["member_no"]) for row in conn.execute(ACTIVE_CLAIMS_SQL)]
        voided = [(row["customer_gid"], row["member_no_key"]) for row in conn.execute(VOIDED_PAIRS_SQL)]
        return active, voided

    return _read_input(Path(config.member_core_state_root) / MEMBER_CORE_LEDGER_FILE, "member_core_ledger", MEMBER_CORE_LEDGER_VERSIONS,
                       config, MEMBER_CORE_TOOL, read)


@dataclass
class LinkSet:
    """``basis``: customer -> (MemberNo, ``sync155`` or ``claim``) for one
    clean link; ``conflicts``: customer -> conflict code (left alone);
    ``voided``: customers that hold a pair the member core voided."""

    basis: dict[str, tuple[str, str]]
    conflicts: dict[str, str]
    voided: set[str]


def build_links(sync_links: Iterable[tuple[str, str]], claims: Iterable[tuple[str, str]], voided: Iterable[tuple[str, str]]) -> LinkSet:
    """``basis = sync links + active claims`` (keys normalised with
    ``sync.member_no_key``); a pair in both is one clean link. A voided pair
    is never a basis."""

    voided_pairs = {(gid, sync.member_no_key(key)) for gid, key in voided}
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for source, rows in (("sync155", sync_links), ("claim", claims)):
        for gid, member_no in rows:
            entry = pairs.setdefault((gid, sync.member_no_key(member_no)), {"member_no": member_no, "sources": set()})
            entry["sources"].add(source)
    gid_keys: dict[str, set[str]] = {}
    key_gids: dict[str, set[str]] = {}
    for gid, key in pairs:
        gid_keys.setdefault(gid, set()).add(key)
        key_gids.setdefault(key, set()).add(gid)
    conflicts: dict[str, str] = {}
    for (gid, key), entry in pairs.items():
        if "sync155" in entry["sources"] and (gid, key) in voided_pairs:
            conflicts[gid] = "link_voided_pair_active_in_sync"  # staff said wrong; the sync cannot void
    for gid, keys in gid_keys.items():
        if len(keys) > 1:
            conflicts.setdefault(gid, "link_conflict_customer")
    for key, gids in key_gids.items():
        if len(gids) > 1:
            for gid in gids:
                conflicts.setdefault(gid, "link_conflict_member")
    basis = {gid: (entry["member_no"], "sync155" if "sync155" in entry["sources"] else "claim")
             for (gid, _key), entry in pairs.items() if gid not in conflicts}
    return LinkSet(basis, conflicts, {gid for gid, _key in voided_pairs})


# --------------------------------------------------------------------------
# Own ledger (append-only, as the member core's)
# --------------------------------------------------------------------------

WRITE_STATES = ("INTENT", "DONE", "REJECTED", "NOT_WRITTEN", "UNCERTAIN")
OPEN_STATES = ("INTENT", "UNCERTAIN")
LEDGER_SCHEMA = """
CREATE TABLE meta(
  id INTEGER PRIMARY KEY CHECK (id = 1), tool TEXT NOT NULL CHECK (tool = 'xb268_member_tags'), ledger_id TEXT NOT NULL,
  autocount_book TEXT NOT NULL CHECK (autocount_book IN ('AED_TESTINGAPI', 'AED_XBOUNDARIES')),
  store_gid TEXT NOT NULL, shop_domain TEXT NOT NULL, ref_key TEXT NOT NULL CHECK (length(ref_key) = 64), created_at TEXT NOT NULL,
  tag_set TEXT NOT NULL CHECK (tag_set = 'member-qualified,member-bday-month'));
CREATE TABLE tag_writes(
  write_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, customer_gid TEXT NOT NULL CHECK (customer_gid GLOB 'gid://shopify/Customer/[1-9]*'),
  tag TEXT NOT NULL CHECK (tag IN ('member-qualified', 'member-bday-month')), op TEXT NOT NULL CHECK (op IN ('add', 'remove')),
  reason TEXT NOT NULL, member_no TEXT, basis TEXT CHECK (basis IS NULL OR basis IN ('sync155', 'claim', 'voided')),
  state TEXT NOT NULL CHECK (state IN ('INTENT', 'DONE', 'REJECTED', 'NOT_WRITTEN', 'UNCERTAIN')),
  code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, command TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, summary TEXT);
CREATE TABLE reviews(subject TEXT PRIMARY KEY, code TEXT NOT NULL, first_seen_at TEXT NOT NULL);
CREATE TRIGGER meta_immutable BEFORE UPDATE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER meta_no_delete BEFORE DELETE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER tag_writes_no_delete BEFORE DELETE ON tag_writes BEGIN SELECT RAISE(ABORT, 'tag_write_immutable'); END;
CREATE TRIGGER tag_writes_binding_immutable BEFORE UPDATE OF write_id, run_id, customer_gid, tag, op, reason, member_no, basis, created_at
  ON tag_writes BEGIN SELECT RAISE(ABORT, 'tag_write_immutable'); END;
CREATE TRIGGER tag_writes_settled_stay BEFORE UPDATE ON tag_writes
  WHEN OLD.state IN ('DONE', 'REJECTED', 'NOT_WRITTEN') BEGIN SELECT RAISE(ABORT, 'tag_write_settled'); END;
CREATE TRIGGER tag_writes_transition BEFORE UPDATE OF state ON tag_writes
  WHEN NOT ((OLD.state = 'INTENT' AND NEW.state IN ('DONE', 'REJECTED', 'NOT_WRITTEN', 'UNCERTAIN'))
         OR (OLD.state = 'UNCERTAIN' AND NEW.state IN ('DONE', 'NOT_WRITTEN')))
  BEGIN SELECT RAISE(ABORT, 'tag_write_transition_invalid'); END;
CREATE TRIGGER tag_writes_new_is_intent BEFORE INSERT ON tag_writes
  WHEN NEW.state <> 'INTENT' BEGIN SELECT RAISE(ABORT, 'tag_write_must_start_intent'); END;
CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'run_immutable'); END;
CREATE TRIGGER runs_binding_immutable BEFORE UPDATE OF run_id, command, started_at ON runs BEGIN SELECT RAISE(ABORT, 'run_immutable'); END;
CREATE TRIGGER reviews_immutable BEFORE UPDATE ON reviews BEGIN SELECT RAISE(ABORT, 'review_immutable'); END;
CREATE TRIGGER reviews_no_delete BEFORE DELETE ON reviews BEGIN SELECT RAISE(ABORT, 'review_immutable'); END;
"""


class TagsLedger:
    """Single-owner SQLite ledger in the tool's own private root."""

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = dl.utc_now):
        self.root = Path(root)
        self.clock = clock
        self._lock = dl._OwnerLock(self.root / LOCK_FILE)
        self.conn = None
        try:
            self.conn = sqlite3.connect(self.root / LEDGER_FILE, isolation_level=None, timeout=0)
            self.conn.row_factory = sqlite3.Row
            # The version is checked before any pragma that could change the file.
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not (version == 0 and not tables) and (version != SCHEMA_VERSION or "tag_writes" not in tables):
                raise dl.DirectLocalError("tags_ledger_not_recognised")
            # recursive_triggers: an INSERT OR REPLACE deletes the old row
            # first; with this on, the no-delete triggers fire and abort it.
            for pragma in ("journal_mode=WAL", "synchronous=FULL", "foreign_keys=ON", "trusted_schema=OFF", "recursive_triggers=ON"):
                self.conn.execute(f"PRAGMA {pragma}")
            if version == 0 and not tables:
                with self.tx() as conn:
                    for statement in dl._split_sql(LEDGER_SCHEMA):
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except sqlite3.DatabaseError:
            if self.conn is not None:
                self.conn.close()
            self._lock.release()
            raise dl.DirectLocalError("tags_ledger_not_recognised") from None
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

    def init_meta(self, config: TagsConfig) -> dict[str, Any]:
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
            if row is not None:
                self._check(row, config)
                return {"status": "existing"}
            conn.execute(
                "INSERT INTO meta(id, tool, ledger_id, autocount_book, store_gid, shop_domain, ref_key, created_at, tag_set) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (TOOL, f"mt-{uuid.uuid4().hex}", config.autocount_book, config.shopify.store_gid, config.shopify.shop_domain,
                 secrets.token_hex(32), dl.ts(self.now()), ",".join(TAG_SET)),
            )
        return {"status": "initialised"}

    @staticmethod
    def _check(row: sqlite3.Row, config: TagsConfig) -> None:
        if (row["tool"], row["autocount_book"], row["store_gid"], row["shop_domain"], row["tag_set"]) != (
                TOOL, sync.check_book(config.autocount_book), config.shopify.store_gid, config.shopify.shop_domain, ",".join(TAG_SET)):
            raise dl.DirectLocalError("tags_ledger_binding_mismatch")

    def check_binding(self, config: TagsConfig) -> None:
        row = self.conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        if row is None:
            raise dl.DirectLocalError("tags_ledger_not_initialised")
        self._check(row, config)

    def ref(self, kind: str, value: str) -> str:
        key = self.conn.execute("SELECT ref_key FROM meta WHERE id = 1").fetchone()[0]
        return f"{kind}-" + hmac.new(bytes.fromhex(key), f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()[:dl.MEMBER_REF_HEX]

    def start_run(self, command: str) -> str:
        run_id = f"run-{uuid.uuid4().hex}"
        with self.tx() as conn:
            conn.execute("INSERT INTO runs(run_id, command, started_at) VALUES (?, ?, ?)", (run_id, command, dl.ts(self.now())))
        return run_id

    def finish_run(self, run_id: str, summary: Mapping[str, Any]) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE run_id = ?",
                         (dl.ts(self.now()), json.dumps(summary, sort_keys=True), run_id))

    def review_once(self, subject: str, code: str) -> bool:
        """True the first time a subject needs review (alert once)."""

        with self.tx() as conn:
            if conn.execute("SELECT 1 FROM reviews WHERE subject = ?", (subject,)).fetchone() is not None:
                return False
            conn.execute("INSERT INTO reviews(subject, code, first_seen_at) VALUES (?, ?, ?)", (subject, code, dl.ts(self.now())))
        return True

    def open_writes(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tag_writes WHERE state IN ('INTENT', 'UNCERTAIN') ORDER BY rowid").fetchall()

    def add_intent(self, *, run_id: str, customer_gid: str, tag: str, op: str, reason: str, member_no: str | None,
                   basis: str | None) -> str:
        write_id = f"tagw-{uuid.uuid4().hex}"
        now = dl.ts(self.now())
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO tag_writes(write_id, run_id, customer_gid, tag, op, reason, member_no, basis, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'INTENT', ?, ?)",
                (write_id, run_id, customer_gid, tag, op, reason, member_no, basis, now, now),
            )
        return write_id

    def set_outcome(self, write_id: str, state: str, code: str | None) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE tag_writes SET state = ?, code = ?, updated_at = ? WHERE write_id = ?",
                         (state, code, dl.ts(self.now()), write_id))

    def done_adds_since(self, customer_gid: str, since: datetime) -> int:
        """DONE adds of our tag for this customer settled at or after ``since``
        (the flapping guard)."""

        return self.conn.execute("SELECT COUNT(*) FROM tag_writes WHERE customer_gid = ? AND tag = ? AND op = 'add' AND state = 'DONE' "
                                 "AND updated_at >= ?", (customer_gid, TAG, dl.ts(since))).fetchone()[0]

    def status(self, config: TagsConfig) -> dict[str, Any]:
        self.check_binding(config)
        conn = self.conn
        writes = {f"{row[0]}:{row[1]}": row[2] for row in conn.execute("SELECT op, state, COUNT(*) FROM tag_writes GROUP BY op, state")}
        return {"status": "ok", "command": "status", "tag_writes": writes, "open_writes": len(self.open_writes()),
                "reviews": conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
                "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                "stop_file_present": (self.root / STOP_FILE).exists(), "tag_set": ",".join(TAG_SET)}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


COMMANDS = ("check-config", "init", "status")


def main(argv: list[str] | None = None, *, clock: Callable[[], datetime] = dl.utc_now) -> int:
    parser = argparse.ArgumentParser(description="#268 slice C1: the Shopify tag member-qualified, kept right from AutoCount")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check-config":
            _emit(config.summary())
            return 0
        root = state_root(config)
        ledger = TagsLedger(root, clock=clock)
        try:
            if args.command == "init":
                _emit(ledger.init_meta(config))
                return 0
            _emit(ledger.status(config))
            return 0
        finally:
            ledger.close()
    except dl.DirectLocalError as exc:
        _emit({"status": "refused", "code": exc.code})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
