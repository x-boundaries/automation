"""#268 slices C1 and D: the Shopify customer tags ``member-qualified`` and
``member-bday-month``, kept right from AutoCount (the member 15% discount
points at a segment of the first; the birthday 20% at a segment of both).

Scope: the G0 slice C design (Controller brief, 12 Nov 2026), cut C1, and the
G0 slice D design (13 Nov 2026, section 2, with the C2 items). Runs on the
AutoCount VM from its own clone, state root, config and Shopify app (one
scope, ``write_customers``). Nothing runs on merge; the default is ``observe``
and both write switches (``tags_write_enabled``, ``bday_tag_enabled``) are off.

Slice D in one line: ``member-bday-month`` is on only when the customer
qualifies for ``member-qualified`` (the same verdict) and the Shopify field
``membership.birthday_month`` is exactly this Singapore month's English name;
it comes off when the field names another month, or on C1's positive facts;
everything unreadable or conflicting is left alone with an alert. One counts
read per run checks the scans against the book (no removal on a mismatch) and
the VM clock against the AutoCount server (no mutation on a mismatch).

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
   ``STOP`` file; for the birthday tag also ``bday_tag_enabled``, its own caps,
   pilot list and volume guard): ``tagsAdd`` / ``tagsRemove`` with exactly one
   tag, ``["member-qualified"]`` or ``["member-bday-month"]``, on a customer
   this run read by ID. An INTENT row is committed before each mutation; an
   unproven answer stops the run and is settled by the next run's read, never
   by sending again.

Never: any other Shopify mutation (no customer create, update or delete, no
metafield), any AutoCount write (no save child is ever started), the removal
of a tag that has no basis unless ``remove_unbased_tags_enabled`` is on.

Privacy: the ledger (private state root on the VM) keeps customer GIDs,
MemberNos, this tool's tag operations, codes and counts. Output carries codes,
counts, keyed refs (``c-`` customer, ``m-`` member) and MemberNos masked to the
last 3 characters only.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import hmac
import http.client
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

# Every tag this tool may ever send (the ledger CHECK), and the ones it sends.
# member-bday-month is sent only with bday_tag_enabled (slice D).
TAG ="member-qualified"
TAG_SET = ("member-qualified", "member-bday-month")
# Slice D: both tags are active. A mutation still carries exactly one of them.
ACTIVE_TAGS = ("member-qualified", "member-bday-month")
BDAY_TAG = "member-bday-month"

# Slice D: the Shopify field membership.birthday_month is a choice list of
# these exact English names (written by the MG Member Sign-up form). A literal
# tuple on purpose: names from the calendar module or a date format follow
# the machine's locale.
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")
BDAY_FIELD_TYPE = "single_line_text_field"
BDAY_MISSING, BDAY_MONTH, BDAY_UNREADABLE = "missing", "month", "unreadable"

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
# Slice D (Controller answer Q3): type-move-counts is the third read, already in
# the bridge and admitted by the book guard's type_move_p1 profile (no
# PowerShell or book guard change). It feeds the type-count and clock checks.
READ_OPERATIONS = ("type-move-scan", "type-move-member", "type-move-counts")
# The VM clock (Singapore time) against the AutoCount server's local time from
# the same counts read (design 2.2): more than this apart, no mutation is sent.
CLOCK_TOLERANCE = timedelta(minutes=10)
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
    # Slice D: the birthday tag has its own switch, caps and pilot allow-list.
    # With the switch off its decisions are counted (would_bday_*), never sent.
    "bday_tag_enabled": False,
    "max_bday_adds_per_run": 10,
    "max_bday_removes_per_run": 10,
    "bday_only_customers": [],
    # C2 (slice D design 2.6): take off a tag that has no basis (row 14, D13),
    # for both tags, under each tag's cap and volume guard. Off until U6/U7.
    "remove_unbased_tags_enabled": False,
}
_BOUNDS = {
    "max_tag_adds_per_run": (1, 250),
    "max_tag_removes_per_run": (1, 50),
    "max_member_reads_per_run": (1, 100),
    "shopify_max_carrier_pages": (1, 100),
    "http_timeout_seconds": (5, 60),
    "max_bday_adds_per_run": (1, 250),
    "max_bday_removes_per_run": (1, 250),
}
BDAY_PILOT_MAX = 5
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
    bday_tag_enabled: bool
    max_bday_adds_per_run: int
    max_bday_removes_per_run: int
    bday_only_customers: tuple[str, ...]
    remove_unbased_tags_enabled: bool

    def __repr__(self) -> str:  # the pilot list holds customer GIDs
        return "TagsConfig()"

    def summary(self) -> dict[str, Any]:
        """PII-free view for ``check-config``."""

        return {
            "status": "ok", "autocount_book": self.autocount_book, "api_version": self.shopify.api_version,
            "scope_sets": sorted(SCOPE_SETS.values()), "tag": TAG, "tags_write_enabled": self.tags_write_enabled,
            "max_tag_adds_per_run": self.max_tag_adds_per_run, "max_tag_removes_per_run": self.max_tag_removes_per_run,
            "max_member_reads_per_run": self.max_member_reads_per_run, "shopify_max_carrier_pages": self.shopify_max_carrier_pages,
            "ac2_credential": "credential_manager" if self.ac2_credential_target else "owner_console_environment",
            "ac2_login_mode": self.ac2_login_mode,
            # Slice D: the birthday settings and the C2 switch (the pilot list as a count: never a GID).
            "bday_tag": BDAY_TAG, "bday_tag_enabled": self.bday_tag_enabled, "max_bday_adds_per_run": self.max_bday_adds_per_run,
            "max_bday_removes_per_run": self.max_bday_removes_per_run, "bday_only_customers": len(self.bday_only_customers),
            "remove_unbased_tags_enabled": self.remove_unbased_tags_enabled,
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
    for key in ("bday_tag_enabled", "remove_unbased_tags_enabled"):
        if not isinstance(merged[key], bool):
            raise dl.DirectLocalError(f"tags_config_{key}_invalid")
    pilot = merged["bday_only_customers"]
    if (not isinstance(pilot, list) or len(pilot) > BDAY_PILOT_MAX or len(set(map(str, pilot))) != len(pilot)
            or any(not isinstance(gid, str) or not _CUSTOMER_GID_RE.fullmatch(gid) for gid in pilot)):
        raise dl.DirectLocalError("tags_config_bday_only_customers_invalid")
    return TagsConfig(
        state_root=merged["state_root"], autocount_book=book, shopify=shopify, member_sync_state_root=merged["member_sync_state_root"],
        member_core_state_root=merged["member_core_state_root"], tags_write_enabled=merged["tags_write_enabled"],
        ac2_credential_target=target, ac2_login_mode=login_mode, ac2_refused_user_ids=tuple(refused),
        bday_tag_enabled=merged["bday_tag_enabled"], bday_only_customers=tuple(pilot),
        remove_unbased_tags_enabled=merged["remove_unbased_tags_enabled"], **numbers,
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
        # One read transaction (G2 L1): both SELECTs see one snapshot, so an
        # unlink that commits between them cannot put a pair in both lists.
        conn.execute("BEGIN")
        try:
            active = [(row["customer_gid"], row["member_no"]) for row in conn.execute(ACTIVE_CLAIMS_SQL)]
            voided = [(row["customer_gid"], row["member_no_key"]) for row in conn.execute(VOIDED_PAIRS_SQL)]
        finally:
            conn.execute("ROLLBACK")
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
# AutoCount: read only, through the type move's guarded reads (design 1.2)
# --------------------------------------------------------------------------

class TagsReadBridgeClient(tm.TypeMoveBridgeClient):
    """ac2_member_type_move_bridge.ps1 (profile type_move_p1), three reads
    only: ``type-move-scan``, ``type-move-member`` and ``type-move-counts``
    (the type move client's own ``counts``). Every child start goes through
    ``_start``, which admits exactly those three read operations; the save,
    the fenced write runner and every other operation are refused before any
    process starts. This tool never writes AutoCount."""

    def __repr__(self) -> str:
        return "TagsReadBridgeClient()"

    def _start(self, arguments: list[str]):
        if len(arguments) != 3 or arguments[0] != "-Operation" or arguments[2] != "-EnableProductionAdapter":
            raise dl.DirectLocalError("tags_autocount_write_refused")
        if arguments[1] not in READ_OPERATIONS:
            raise dl.DirectLocalError("tags_operation_not_offered")
        return super()._start(arguments)

    def save(self, *args: Any, **kwargs: Any) -> Any:
        raise dl.DirectLocalError("tags_autocount_write_refused")

    def _write(self, *args: Any, **kwargs: Any) -> Any:
        raise dl.DirectLocalError("tags_autocount_write_refused")

    def write_once(self, *args: Any, **kwargs: Any) -> Any:
        raise dl.DirectLocalError("tags_autocount_write_refused")


def scan_type(ac2: Any, member_type: str) -> list[dict[str, Any]]:
    """Every member of one type, in the book's own MemberNo order (the type
    move's paging rule): a repeated MemberNo or a full page whose last
    MemberNo does not advance stops; so does a page cap."""

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    after = ""
    for _ in range(SCAN_MAX_PAGES):
        page = ac2.scan(member_type, after)
        for row in page:
            if row["member_no"] in seen:
                raise dl.DirectLocalError("tags_scan_order_invalid")
            seen.add(row["member_no"])
        rows.extend(page)
        if len(page) < SCAN_PAGE_SIZE:
            return rows
        if page[-1]["member_no"] == after:
            raise dl.DirectLocalError("tags_scan_order_invalid")
        after = page[-1]["member_no"]
    raise dl.DirectLocalError("tags_scan_too_many_pages")


def scan_members(ac2: Any, sizes: dict[str, int] | None = None) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """MemberNo key -> every ``(type, row)`` found across the three scans;
    ``sizes`` (when given) receives each scan's row count."""

    found: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for member_type in SCAN_TYPES:
        rows = scan_type(ac2, member_type)
        if sizes is not None:
            sizes[member_type] = len(rows)
        for row in rows:
            found.setdefault(sync.member_no_key(row["member_no"]), []).append((member_type, row))
    return found


def type_counts_match(counts: Mapping[str, Any], sizes: Mapping[str, int]) -> bool:
    """Design 2.7: for each of ``Default``, ``MEMBER`` and ``GUEST`` the
    scan's row count equals that type's count from ``type-move-counts``
    (rows grouped by type and active flag; the type compared exactly, as the
    scan's own parameter). A malformed count row is a read failure."""

    totals = {member_type: 0 for member_type in SCAN_TYPES}
    for row in counts.get("counts") or []:
        if (not isinstance(row, dict) or not (row.get("member_type") is None or isinstance(row.get("member_type"), str))
                or isinstance(row.get("members"), bool) or not isinstance(row.get("members"), int) or row["members"] < 0):
            raise dl.BridgeError("bridge_output_invalid")
        if row["member_type"] in totals:
            totals[row["member_type"]] += row["members"]
    return all(totals[member_type] == sizes.get(member_type) for member_type in SCAN_TYPES)


def clock_matches(server_now_local: Any, now: datetime) -> bool:
    """Design 2.2: the VM clock in Singapore time is within
    ``CLOCK_TOLERANCE`` of the AutoCount server's local time."""

    server = tm.parse_local(server_now_local)
    if server is None:
        raise dl.BridgeError("bridge_output_invalid")
    local = now.astimezone(SGT).replace(tzinfo=None)
    return abs(local - server) <= CLOCK_TOLERANCE


def sg_today(moment: datetime) -> date:
    """Today in Singapore (UTC+8, no DST) for a timezone-aware instant."""

    if moment.tzinfo is None:
        raise dl.DirectLocalError("clock_must_be_timezone_aware")
    return moment.astimezone(SGT).date()


def month_name(day: date) -> str:
    """The English month name of a Singapore date, from the literal tuple."""

    return MONTH_NAMES[day.month - 1]


def expiry_day(expiry_local: Any) -> date | None:
    """The date part of an ExpiryDate, or None when it is null, unparseable or
    a placeholder before 2020-01-01 (never read as expired)."""

    parsed = tm.parse_local(expiry_local)
    if parsed is None or parsed.date() < EXPIRY_FLOOR:
        return None
    return parsed.date()


ON, OFF, LEAVE = "on", "off", "leave"


def member_verdict(entries: list[tuple[str, Mapping[str, Any]]], today: date) -> tuple[str, str] | None:
    """``(on|off|leave, code)`` for a linked member from the scans (design
    1.6 rows 4, 6-10, 12), or None when it was found in no scan (the single
    read decides, rows 11-11c). ``off`` only on a positive fact."""

    if not entries:
        return None
    if len(entries) > 1:
        return LEAVE, "member_reads_conflict"
    member_type, row = entries[0]
    if member_type != MEMBER:
        return OFF, "member_not_member"  # GUEST or Default
    active = row.get("is_active")
    if active == "F":
        return OFF, "member_inactive"
    if active != "T":
        return LEAVE, "member_active_unreadable"
    day = expiry_day(row.get("expiry_local"))
    if day is None:
        return LEAVE, "expiry_unreadable"
    if day < today:
        return OFF, "member_expired"
    return ON, "member_active"  # valid through the expiry day


def single_read_verdict(result: Mapping[str, Any]) -> tuple[str, str]:
    """A linked member found in no scan, by its one ``type-move-member`` read."""

    if not result.get("found"):
        return LEAVE, "member_not_found"
    if result.get("member_type") == MEMBER:
        return LEAVE, "member_scan_missed"
    return OFF, "member_not_member"


# --------------------------------------------------------------------------
# Shopify (design 1.3): pinned texts, Admin GraphQL 2026-10
# --------------------------------------------------------------------------

CUSTOMERS_QUERY = """query XbMemberTagsCustomers($ids: [ID!]!) {
  nodes(ids: $ids) { ... on Customer { id tags
    registration: metafield(namespace: "membership", key: "start_date") { id }
    birthday: metafield(namespace: "membership", key: "birthday_month") { value type } } }
}"""
CARRIERS_QUERY = """query XbMemberTagsCarriers($first: Int!, $after: String, $search: String!) {
  customers(first: $first, after: $after, query: $search) { nodes { id tags } pageInfo { hasNextPage endCursor } }
}"""
TAGS_ADD_MUTATION = """mutation XbMemberTagsAdd($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) { node { ... on Customer { id tags } } userErrors { field message } }
}"""
TAGS_REMOVE_MUTATION = """mutation XbMemberTagsRemove($id: ID!, $tags: [String!]!) {
  tagsRemove(id: $id, tags: $tags) { node { ... on Customer { id tags } } userErrors { field message } }
}"""
READ_QUERIES = frozenset({direct.SESSION_QUERY, CUSTOMERS_QUERY, CARRIERS_QUERY})
MUTATIONS = {"add": (TAGS_ADD_MUTATION, "tagsAdd"), "remove": (TAGS_REMOVE_MUTATION, "tagsRemove")}
MUTATION_TEXTS = frozenset(text for text, _field in MUTATIONS.values())
QUERY_NAMES = {CUSTOMERS_QUERY: "XbMemberTagsCustomers", CARRIERS_QUERY: "XbMemberTagsCarriers"}
# One search per active tag, each built from the pinned tag only (design 2.5).
CARRIER_SEARCHES = {tag: f"tag:'{tag}'" for tag in ACTIVE_TAGS}
# Page sizes. Shopify refuses a query whose requested cost is over 1,000
# points before it runs (shopify.dev rate limits). On the strict reading used
# by the member core (an object costs 1, a scalar 0, every selected object is
# counted once per row of the list or connection above it, a list of IDs has
# one row per ID):
#     customers by ID: 100 x (Customer 1 + 2 metafields 1 each)    = 300
#     carriers: connection 1 + pageInfo 1 + 100 x (Customer 1)     = 102
# Both under the 500 the design allows before the nodes page is lowered to 50
# (slice D design 2.9: the birthday metafield took the by-ID query from 200).
# Pinned by CostTests; the runbook (section 8) writes the sums out.
NODES_PAGE = 100
CARRIER_PAGE = 100
CUSTOMERS_QUERY_COST = 300
CARRIERS_QUERY_COST = 102
MUTATION_COST = 10
COST_CEILING = 500

# Write answers (design 1.4).
DONE, REJECTED, NOT_WRITTEN, UNCERTAIN = "DONE", "REJECTED", "NOT_WRITTEN", "UNCERTAIN"
THROTTLED = "shopify_graphql_throttled"
BUDGET_CODES = frozenset({"shopify_retry_budget_exhausted", "shopify_query_cost_exceeds_bucket"})


@dataclass(frozen=True)
class CustomerState:
    """One customer as read by ID: its tags, whether the
    ``membership.start_date`` metafield exists (its value is never read), and
    the ``membership.birthday_month`` reading (slice D design 2.1):
    ``bday_state`` is ``missing``, ``month`` (``bday_month`` is then exactly
    one of ``MONTH_NAMES``) or ``unreadable``. Never stored or printed."""

    gid: str
    tags: tuple[str, ...]
    has_registration: bool
    bday_state: str = BDAY_MISSING
    bday_month: str | None = None

    def __repr__(self) -> str:  # never render customer data
        return "CustomerState()"


def _shape(ok: bool) -> None:
    if not ok:
        raise direct.ShopifyDirectError("shopify_response_shape_invalid")


def _tags(value: Any) -> tuple[str, ...]:
    _shape(isinstance(value, list) and all(isinstance(t, str) for t in value))
    return tuple(value)


def customer_state(node: Any, gid: str) -> CustomerState | None:
    """A ``nodes`` entry for ``gid``: None (customer_not_found) for null or a
    non-Customer entry (an empty object); a Customer must carry exactly this ID."""

    if node is None or node == {}:
        return None
    _shape(isinstance(node, dict) and node.get("id") == gid)
    registration = node.get("registration")
    _shape(registration is None or (isinstance(registration, dict) and isinstance(registration.get("id"), str)))
    bday_state, bday_month = birthday_reading(node.get("birthday"))
    return CustomerState(gid, _tags(node.get("tags")), registration is not None, bday_state, bday_month)


def birthday_reading(birthday: Any) -> tuple[str, str | None]:
    """``membership.birthday_month`` (design 2.1): null is ``missing``; a
    single line text whose value is exactly one of ``MONTH_NAMES`` (no strip,
    no casefold) is ``month``; any other type (a list included) or value is
    ``unreadable`` and is never guessed. A wrong shape stops the read."""

    if birthday is None:
        return BDAY_MISSING, None
    _shape(isinstance(birthday, dict) and isinstance(birthday.get("value"), str) and isinstance(birthday.get("type"), str))
    if birthday["type"] == BDAY_FIELD_TYPE and birthday["value"] in MONTH_NAMES:
        return BDAY_MONTH, birthday["value"]
    return BDAY_UNREADABLE, None


def check_tag_variables(gid: Any, tags: Any) -> None:
    """The one place a mutation's variables are checked: a customer GID and
    exactly ``[TAG]`` with the tag in ``ACTIVE_TAGS``."""

    if not isinstance(tags, list) or len(tags) != 1 or not isinstance(tags[0], str) or tags[0] not in ACTIVE_TAGS:
        raise dl.DirectLocalError("tags_tag_not_allowed")
    if not isinstance(gid, str) or not _CUSTOMER_GID_RE.fullmatch(gid):
        raise dl.DirectLocalError("tags_customer_gid_invalid")


def whole_points(value: Any) -> int | None:
    """A query cost as whole points (rounded up); None for anything that is
    not a plain number from 0 to 1,000,000."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1_000_000:
        return None
    return math.ceil(value)


class TagsShopifyClient(direct.ShopifyDirectClient):
    """Admin GraphQL client of the tagging app: three pinned reads and, only
    when ``mutations_enabled`` (``apply`` with ``tags_write_enabled``), the two
    pinned tag mutations. A mutation is retried only on a throttled GraphQL
    answer (not executed), never on a transport failure, timeout or 5xx."""

    def __init__(self, *, mutations_enabled: bool, **kwargs: Any):
        super().__init__(**kwargs)
        self.mutations_enabled = bool(mutations_enabled)
        self.query_costs: dict[str, int | None] = {name: None for name in QUERY_NAMES.values()}
        self._mutation_errors: list[str] = []

    def __repr__(self) -> str:
        return "TagsShopifyClient()"

    def _query_allowed(self, query: str) -> bool:
        return query in READ_QUERIES or (query in MUTATION_TEXTS and self.mutations_enabled)

    def _retry_allowed(self, query: str, error: direct.ShopifyDirectError) -> bool:
        if query in MUTATION_TEXTS:
            self._mutation_errors.append(error.code)
            return error.code == THROTTLED
        return True

    def _run(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        data = self.execute(query, variables)
        cost = whole_points(self._throttle.requested_cost) if self._throttle is not None else None
        name = QUERY_NAMES[query]
        if cost is not None and (self.query_costs[name] is None or cost > self.query_costs[name]):
            self.query_costs[name] = cost
        return data

    def verify_session(self, *, store_gid: str) -> str:
        """Store and domain equal the config; the scope handles are exactly one
        of the two pinned sets. Returns ``write_only`` or ``read_write``."""

        data = self.execute(direct.SESSION_QUERY, {})
        if set(data) != {"shop", "currentAppInstallation"} or data.get("shop") != {"id": store_gid, "myshopifyDomain": self.shop_domain}:
            raise direct.ShopifyDirectError("shopify_store_identity_mismatch")
        installation = data["currentAppInstallation"]
        scopes = installation.get("accessScopes") if isinstance(installation, dict) else None
        if not isinstance(scopes, list) or any(not isinstance(item, dict) or set(item) != {"handle"} or not isinstance(item["handle"], str) for item in scopes):
            raise direct.ShopifyDirectError("shopify_response_shape_invalid")
        handles = [item["handle"] for item in scopes]
        if len(set(handles)) != len(handles) or frozenset(handles) not in SCOPE_SETS:
            raise direct.ShopifyDirectError("shopify_token_scope_not_expected")
        return SCOPE_SETS[frozenset(handles)]

    def customers(self, gids: Iterable[str]) -> dict[str, CustomerState | None]:
        """Each customer by ID (the authoritative tag state), pages of ``NODES_PAGE``."""

        wanted = sorted(set(gids), key=customer_number)
        found: dict[str, CustomerState | None] = {}
        for start in range(0, len(wanted), NODES_PAGE):
            page = wanted[start:start + NODES_PAGE]
            nodes = self._run(CUSTOMERS_QUERY, {"ids": page}).get("nodes")
            _shape(isinstance(nodes, list) and len(nodes) == len(page))
            for gid, node in zip(page, nodes):
                found[gid] = customer_state(node, gid)
        return found

    def carriers(self, tag: str, max_pages: int) -> tuple[set[str], bool]:
        """Customers the search index lists as carrying ``tag`` (one of the
        active tags; anything else is refused before any HTTP call): a
        candidate list only (each is then read by ID). ``complete`` is false
        at the cap."""

        if not isinstance(tag, str) or tag not in CARRIER_SEARCHES:
            raise dl.DirectLocalError("tags_tag_not_allowed")
        search = CARRIER_SEARCHES[tag]
        gids: set[str] = set()
        after = None
        for _ in range(max_pages):
            page = self._run(CARRIERS_QUERY, {"first": CARRIER_PAGE, "after": after, "search": search}).get("customers")
            _shape(isinstance(page, dict) and isinstance(page.get("nodes"), list) and isinstance(page.get("pageInfo"), dict))
            for node in page["nodes"]:
                _shape(isinstance(node, dict) and isinstance(node.get("id"), str) and bool(_CUSTOMER_GID_RE.fullmatch(node["id"])))
                _tags(node.get("tags"))
                gids.add(node["id"])
            if page["pageInfo"].get("hasNextPage") is False:
                return gids, True
            after = page["pageInfo"].get("endCursor")
            _shape(isinstance(after, str) and bool(after))
        return gids, False

    def set_tag(self, op: str, gid: str, tags: Any) -> tuple[str, str | None]:
        """One tag mutation; ``(state, code)`` as design 1.4. Refusals (a tag
        or ID not allowed, mutations off) raise before any HTTP call."""

        check_tag_variables(gid, tags)
        if op not in MUTATIONS:
            raise dl.DirectLocalError("tags_operation_not_offered")
        query, payload_field = MUTATIONS[op]
        if not self._query_allowed(query):
            raise direct.ShopifyDirectError("shopify_query_not_pinned")
        sent_before = self.stats.requests
        self._mutation_errors = []
        try:
            data = self.execute(query, {"id": gid, "tags": [tags[0]]})
        except direct.ShopifyDirectError as exc:
            if self.stats.requests == sent_before:
                return NOT_WRITTEN, exc.code  # never sent
            if exc.code in BUDGET_CODES and self._mutation_errors and all(code == THROTTLED for code in self._mutation_errors):
                return NOT_WRITTEN, "shopify_retry_budget_exhausted"  # every answer said: not executed
            return UNCERTAIN, exc.code
        payload = data.get(payload_field)
        if set(data) != {payload_field} or not isinstance(payload, dict) or not isinstance(payload.get("userErrors"), list):
            return UNCERTAIN, "tag_response_unproven"
        if payload["userErrors"]:
            return REJECTED, "shopify_user_error"  # no message is stored or printed
        node = payload.get("node")
        if not isinstance(node, dict) or node.get("id") != gid or not isinstance(node.get("tags"), list):
            return UNCERTAIN, "tag_response_unproven"
        present = tags[0] in node["tags"]
        if present != (op == "add"):
            return UNCERTAIN, "tag_response_unproven"
        return DONE, None


class TagsTokenCache(direct.TokenCache):
    """The #155 DPAPI token cache, checking this app's two scope sets."""

    def load(self, now: datetime) -> tuple[str, datetime] | None:
        try:
            if not self.path.exists():
                return None
            raw = self.path.read_bytes()
        except OSError:
            self.note = "token_cache_unreadable"
            return None
        try:
            if len(raw) > direct.TOKEN_CACHE_MAX_BYTES:
                raise direct.ShopifyDirectError("token_cache_unreadable")
            value = json.loads(self.protector.unprotect(raw, self._entropy).decode("utf-8"))
            if not isinstance(value, dict) or set(value) != {"schema", "shop_domain", "expires_at", "scope", "access_token"}:
                raise direct.ShopifyDirectError("token_cache_unreadable")
            if value["schema"] != direct.TOKEN_CACHE_SCHEMA or value["shop_domain"] != self.shop_domain:
                raise direct.ShopifyDirectError("token_cache_unreadable")
            expires_at = dl.parse_created_at(value["expires_at"])
            token = direct._secret(value["access_token"], "token_cache_unreadable")
            if direct._scopes(value["scope"]) not in SCOPE_SETS or expires_at is None:
                raise direct.ShopifyDirectError("token_cache_unreadable")
        except (direct.ShopifyDirectError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            self.note = "token_cache_unreadable"
            self.clear()
            return None
        if now >= expires_at - timedelta(seconds=direct.TOKEN_REFRESH_MARGIN_SECONDS):
            self.note = "token_cache_expired"
            self.clear()
            return None
        return token, expires_at


class TagsTokenProvider(direct.ClientCredentialsTokenProvider):
    """The #155 client-credentials grant, for this app's two scope sets."""

    def __init__(self, shop_domain: str, client_id: Any, client_secret: Any, **kwargs: Any):
        protector = kwargs.pop("cache_protector", None)
        path = kwargs.pop("cache_path", None)
        super().__init__(shop_domain, client_id, client_secret, **kwargs)
        self.cache = None if protector is None else TagsTokenCache(path, protector, shop_domain=shop_domain, client_id=self._client_id)

    def __repr__(self) -> str:
        return "TagsTokenProvider()"

    def token(self) -> str:
        now = self._clock()
        if self._token is not None and self._expires_at is not None and now < self._expires_at - timedelta(seconds=direct.TOKEN_REFRESH_MARGIN_SECONDS):
            return self._token
        self._token = self._expires_at = None
        cached = self.cache.load(now) if self.cache is not None else None
        if cached is not None:
            self._token, self._expires_at = cached
            self.cache_hits += 1
            return self._token
        import urllib.parse

        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._client_secret}).encode("ascii")
        self.requests += 1
        response = self._transport(self._url, body, {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, self._timeout)
        if response.status in (400, 401, 403):
            raise direct.ShopifyDirectError("shopify_client_credentials_rejected")
        if response.status != 200:
            error = direct.http_error(response)
            raise direct.ShopifyDirectError("shopify_token_request_failed", retryable=error.retryable, retry_after_seconds=error.retry_after_seconds)
        try:
            value = json.loads(response.body.decode("utf-8")) if len(response.body) <= 65_536 else None
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            value = None
        if not isinstance(value, dict):
            raise direct.ShopifyDirectError("shopify_token_response_invalid")
        token = direct._secret(value.get("access_token"), "shopify_token_response_invalid")
        expires_in = value.get("expires_in")
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) or not direct.TOKEN_MIN_LIFETIME_SECONDS <= expires_in <= direct.TOKEN_MAX_LIFETIME_SECONDS:
            raise direct.ShopifyDirectError("shopify_token_response_invalid")
        scopes = direct._scopes(value.get("scope"))
        if scopes not in SCOPE_SETS:
            raise direct.ShopifyDirectError("shopify_token_scope_not_expected")
        self._token, self._expires_at = token, now + timedelta(seconds=expires_in)
        if self.cache is not None:
            self.cache.save(token, self._expires_at, scopes)
        return token


def token_provider(connection: sync.ShopifyConnection, environ: Mapping[str, str], transport: Any, clock: Callable[[], datetime], *,
                   root: Path, protector: Any = None) -> Any:
    """As the #155 sync: app values from the named environment variables of
    the task account, the access token reused through a DPAPI cache (its own
    file in this tool's state root)."""

    if connection.auth_method == "admin_api_access_token":
        return direct.StaticTokenProvider(environ.get(connection.auth_env["token_env"]))
    cache_protector = None
    if connection.token_cache == "dpapi_current_user":
        cache_protector = protector if protector is not None else direct.DpapiProtector()
    return TagsTokenProvider(
        connection.shop_domain, environ.get(connection.auth_env["client_id_env"]), environ.get(connection.auth_env["client_secret_env"]),
        transport=transport, clock=clock, timeout=connection.http_timeout_seconds, cache_protector=cache_protector,
        cache_path=root / TOKEN_CACHE_FILE,
    )


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

    def done_adds_since(self, customer_gid: str, since: datetime, tag: str) -> int:
        """DONE adds of ``tag`` for this customer settled at or after ``since``
        (the flapping guard, per tag)."""

        return self.conn.execute("SELECT COUNT(*) FROM tag_writes WHERE customer_gid = ? AND tag = ? AND op = 'add' AND state = 'DONE' "
                                 "AND updated_at >= ?", (customer_gid, tag, dl.ts(since))).fetchone()[0]

    def last_done_add_at(self, customer_gid: str, tag: str) -> datetime | None:
        """When this tool's last DONE add of ``tag`` for this customer was
        recorded (the month-change exemption), or None if it never added it."""

        value = self.conn.execute("SELECT MAX(updated_at) FROM tag_writes WHERE customer_gid = ? AND tag = ? AND op = 'add' "
                                  "AND state = 'DONE'", (customer_gid, tag)).fetchone()[0]
        return None if value is None else dl.parse_ts(value)

    def status(self, config: TagsConfig) -> dict[str, Any]:
        self.check_binding(config)
        conn = self.conn
        # Keys as in C1 (op:state) for member-qualified; bday:op:state for the birthday tag (slice D).
        writes = {("bday:" if row[0] == BDAY_TAG else "") + f"{row[1]}:{row[2]}": row[3]
                  for row in conn.execute("SELECT tag, op, state, COUNT(*) FROM tag_writes GROUP BY tag, op, state")}
        return {"status": "ok", "command": "status", "tag_writes": writes, "open_writes": len(self.open_writes()),
                "reviews": conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
                "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                "stop_file_present": (self.root / STOP_FILE).exists(), "tag_set": ",".join(TAG_SET)}


# --------------------------------------------------------------------------
# Engine (design 1.4 to 1.7, 1.10)
# --------------------------------------------------------------------------

MAX_REFS = 200
FLAP_WINDOW = timedelta(hours=24)
FLAP_DONE_ADDS = 2


@dataclass
class RunSummary:
    command: str
    counts: dict[str, int] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)
    refs: list[dict[str, str]] = field(default_factory=list)
    stopped: str | None = None

    def count(self, code: str, ref: str | None = None, member_no: str | None = None) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1
        if ref is not None:
            self.add_ref(code, ref, member_no)

    def add_ref(self, code: str, ref: str, member_no: str | None = None) -> None:
        if len(self.refs) < MAX_REFS:
            entry = {"ref": ref, "code": code}
            if member_no:
                entry["member_no_masked"] = mask(member_no)
            self.refs.append(entry)

    def alert(self, code: str, ref: str | None = None, member_no: str | None = None) -> None:
        """A run-level alert: counted, listed, every run it happens."""

        self.count(code, ref, member_no)
        if code not in self.alerts:
            self.alerts.append(code)

    def flag(self, code: str) -> None:
        if code not in self.alerts:
            self.alerts.append(code)


@dataclass(frozen=True)
class PlannedWrite:
    gid: str
    op: str  # add or remove
    reason: str
    member_no: str | None
    basis: str | None  # sync155, claim or voided
    tag: str = TAG  # exactly one of ACTIVE_TAGS (slice D: one tag per write)


class TagsEngine:
    """One bounded run. ``shopify`` provides ``verify_session``, ``customers``,
    ``carriers`` and ``set_tag``; ``ac2`` provides ``scan`` and ``member``;
    ``inputs`` returns ``(sync_links, active_claims, voided_pairs)``."""

    def __init__(self, config: TagsConfig, ledger: TagsLedger, *, shopify: Any, ac2: Any,
                 inputs: Callable[[], tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]] | None = None):
        self.config = config
        self.ledger = ledger
        self.shopify = shopify
        self.ac2 = ac2
        self._inputs = inputs or (lambda: (read_sync_links(config), *read_member_core(config)))
        self.reads = 0
        self.month_now: str | None = None
        self.today: date | None = None
        self.unbased_ok = {TAG: False, BDAY_TAG: False}
        self.clock_ok = False  # set by each run from the counts read; nothing is sent before
        self.counts_ok = False

    def now(self) -> datetime:
        return self.ledger.now()

    def _stop_requested(self) -> bool:
        return (self.ledger.root / STOP_FILE).exists()

    def _alert_once(self, summary: RunSummary, code: str, ref: str, member_no: str | None = None) -> None:
        """Counted every run; raised as an alert (with its ref) only the first
        time for this subject (design 1.6: alert once per subject)."""

        summary.count(code)
        if self.ledger.review_once(f"{code}:{ref}", code):
            summary.flag(code)
            summary.add_ref(code, ref, member_no)

    # -- the run ----------------------------------------------------------
    def run(self, *, apply: bool) -> dict[str, Any]:
        command = "apply" if apply else "observe"
        if apply and not self.config.tags_write_enabled:
            raise dl.DirectLocalError("tags_write_disabled")
        self.ledger.check_binding(self.config)
        # Local inputs first (each refusal records nothing and needs no HTTP call).
        sync_links, claims, voided = self._inputs()
        links = build_links(sync_links, claims, voided)
        scope_set = self.shopify.verify_session(store_gid=self.config.shopify.store_gid)
        open_rows = self.ledger.open_writes()
        settle_reads: dict[str, CustomerState | None] | None = {}
        if open_rows:
            try:
                settle_reads = self.shopify.customers({row["customer_gid"] for row in open_rows})
            except dl.DirectLocalError:
                if apply:
                    raise dl.DirectLocalError("tags_uncertain_unresolved") from None
                settle_reads = None  # observe goes on; those customers are left alone (row 16)
        run_id = self.ledger.start_run(command)
        summary = RunSummary(command)
        self.month_now, self.today, self.clock_ok, self.counts_ok = None, None, False, False
        self.unbased_ok = {TAG: False, BDAY_TAG: False}
        states: dict[str, CustomerState | None] = {}
        carriers_total: int | None = None
        bday_carriers_total: int | None = None
        try:
            unresolved = self._settle(open_rows, settle_reads, summary)
            sizes: dict[str, int] = {}
            facts = scan_members(self.ac2, sizes)
            # Design 2.7: one counts read after the scans (a failure stops the
            # run here, before any write, as a scan failure does).
            type_counts = self.ac2.counts()
            now = self.now()
            today = sg_today(now)
            # Once per run (design 2.2): a run that starts at 23:59 on the
            # last day uses that month for every customer.
            self.month_now = month_name(today)
            self.today = today
            self.clock_ok = clock_matches(type_counts.get("server_now_local"), now)
            self.counts_ok = type_counts_match(type_counts, sizes)
            if not self.counts_ok:
                summary.alert("scan_count_mismatch")  # no removal of either tag this run
            wanted = set(links.basis) | set(links.conflicts) | links.voided | {row["customer_gid"] for row in open_rows}
            states = self.shopify.customers(wanted)
            carriers, complete = self.shopify.carriers(TAG, self.config.shopify_max_carrier_pages)
            if complete:
                carriers_total = len(carriers)
            else:
                summary.alert("tag_search_incomplete")
            # Slice D: the second search, for the birthday tag (design 2.5).
            bday_carriers, bday_complete = self.shopify.carriers(BDAY_TAG, self.config.shopify_max_carrier_pages)
            if bday_complete:
                bday_carriers_total = len(bday_carriers)
            else:
                summary.alert("bday_tag_search_incomplete")
            # C2 (design 2.6): a tag with no basis comes off only with the
            # switch on, never on an empty link set (a replaced ledger) and
            # never on that tag's incomplete search.
            links_empty = not sync_links and not claims
            if self.config.remove_unbased_tags_enabled and links_empty:
                summary.alert("tags_basis_empty")
            unbased = self.config.remove_unbased_tags_enabled and not links_empty
            self.unbased_ok = {TAG: unbased and complete, BDAY_TAG: unbased and bday_complete}
            extra = (carriers | bday_carriers) - set(states)
            if extra:
                states.update(self.shopify.customers(extra))
            plans = []
            for gid in sorted(set(states), key=customer_number):
                plans.extend(self._decide(gid, links, states.get(gid), facts, today, unresolved, summary))
            self._write(plans, states, summary, run_id=run_id, apply=apply)
        except dl.DirectLocalError as exc:
            summary.stopped = exc.code
            summary.alert(exc.code)
        linked = [states[gid] for gid in set(links.basis) | set(links.conflicts) if states.get(gid) is not None]
        tagged_now = sum(1 for state in linked if TAG in state.tags)
        # Slice D (design 2.8): the birthday tag on linked customers, and those
        # still carrying it whose valid month is not this month (0 after a run
        # that was not capped or withheld). The month value is never printed;
        # bday_month_now names today's month only.
        bday_tagged = [state for state in linked if BDAY_TAG in state.tags]
        other_month = None if self.month_now is None else sum(
            1 for state in bday_tagged if state.bday_state == BDAY_MONTH and state.bday_month != self.month_now)
        costs = dict(getattr(self.shopify, "query_costs", {}) or {})
        result = {"status": "ok", "command": command, "run_id": run_id, "counts": dict(sorted(summary.counts.items())),
                  "alerts": list(summary.alerts), "refs": list(summary.refs), "stopped": summary.stopped, "scope_set": scope_set,
                  "query_costs": {name: costs.get(name) for name in QUERY_NAMES.values()}, "tagged_now": tagged_now,
                  "carriers_total": carriers_total, "bday_tagged_now": len(bday_tagged), "bday_month_now": self.month_now,
                  "bday_carriers_total": bday_carriers_total, "bday_tagged_other_month": other_month}
        self.ledger.finish_run(run_id, result)
        return result

    def _settle(self, open_rows: list[sqlite3.Row], reads: dict[str, CustomerState | None] | None, summary: RunSummary) -> set[str]:
        """Design 1.4: every INTENT or UNCERTAIN row is settled by this run's
        by-ID read before planning. Returns the customers left unresolved."""

        if reads is None:
            return {row["customer_gid"] for row in open_rows}
        for row in open_rows:
            state = reads.get(row["customer_gid"])
            if state is None:
                self.ledger.set_outcome(row["write_id"], NOT_WRITTEN, "customer_not_found")
                summary.count("write_settled_not_found")
            elif (row["tag"] in state.tags) == (row["op"] == "add"):
                self.ledger.set_outcome(row["write_id"], DONE, "resolved_by_read")
                summary.count("write_settled_done")
            else:
                self.ledger.set_outcome(row["write_id"], NOT_WRITTEN, "resolved_by_read")
                summary.count("write_settled_not_written")
        return set()

    def _single_read(self, member_no: str) -> tuple[str, str]:
        if self.reads >= self.config.max_member_reads_per_run:
            return LEAVE, "member_reads_capped"
        self.reads += 1
        return single_read_verdict(self.ac2.member(member_no, secrets.token_hex(16)))

    def _decide(self, gid: str, links: LinkSet, state: CustomerState | None, facts: Mapping[str, list[tuple[str, dict[str, Any]]]],
                today: date, unresolved: set[str], summary: RunSummary) -> list[PlannedWrite]:
        """Design 1.6 and slice D 2.3, one customer: 0, 1 or 2 writes. The
        ``member-qualified`` plan is computed exactly as in C1 (D-P4); the
        birthday plan follows from its verdict Q."""

        ref = self.ledger.ref("c", gid)
        if gid in unresolved:  # row 16 (D1: both tags left alone)
            self._alert_once(summary, "write_unresolved", ref)
            return []
        if gid in links.conflicts:  # row 1 (D1)
            self._alert_once(summary, links.conflicts[gid], ref)
            return []
        if state is None:  # row 2 (D1)
            self._alert_once(summary, "customer_not_found", ref)
            return []
        plan, verdict = self._decide_qualified(gid, links, state, facts, today, summary, ref)
        bday = self._decide_bday(gid, links, state, verdict, summary, ref)
        return [p for p in (plan, bday) if p is not None]

    def _decide_qualified(self, gid: str, links: LinkSet, state: CustomerState, facts: Mapping[str, list[tuple[str, dict[str, Any]]]],
                          today: date, summary: RunSummary, ref: str) -> tuple[PlannedWrite | None, tuple[str, str] | None]:
        """Design 1.6 rows 3 to 15 for ``member-qualified``, unchanged. Also
        returns Q, the verdict the birthday tag follows: ``(on|off|leave,
        code)`` for a customer with a basis (``leave`` for a row 3 case
        variant, so no extra AutoCount read is made), None for one without."""

        if any(tag != TAG and tag.strip().casefold() == TAG for tag in state.tags):  # row 3
            self._alert_once(summary, "tag_case_variant", ref)
            return None, (LEAVE, "tag_case_variant")
        tagged = TAG in state.tags
        if gid in links.basis:
            member_no, basis = links.basis[gid]
            verdict = member_verdict(facts.get(sync.member_no_key(member_no), []), today)
            if verdict is None:  # rows 11 to 11c
                verdict = self._single_read(member_no)
                if verdict[1] == "member_reads_capped":
                    summary.alert("member_reads_capped")
                    return None, verdict
            desired, code = verdict
            if desired == LEAVE:  # rows 9, 10, 11a, 11b, 12
                self._alert_once(summary, code, ref, member_no)
                return None, verdict
            if desired == ON:  # row 4
                if tagged:
                    summary.count("kept_tagged")
                    return None, verdict
                if any(tag.strip().casefold() == REGISTRATION_TAG for tag in state.tags) and not state.has_registration:  # row 5
                    self._alert_once(summary, "flow_dates_guard", ref, member_no)
                    return None, verdict
                if self.ledger.done_adds_since(gid, self.now() - FLAP_WINDOW, TAG) >= FLAP_DONE_ADDS:
                    self._alert_once(summary, "tag_keeps_disappearing", ref, member_no)
                    return None, verdict
                return PlannedWrite(gid, "add", code, member_no, basis), verdict
            if not tagged:  # rows 6, 7, 8, 11 with no tag: row 15
                summary.count("no_change_untagged")
                return None, verdict
            return PlannedWrite(gid, "remove", code, member_no, basis), verdict
        if not tagged:
            summary.count("no_change_untagged")
            return None, None
        if gid in links.voided:  # row 13
            return PlannedWrite(gid, "remove", "link_voided", None, "voided"), None
        if self.unbased_ok[TAG]:  # row 14 with the C2 switch (design 2.6)
            return PlannedWrite(gid, "remove", "tag_without_basis", None, None), None
        self._alert_once(summary, "tag_without_basis", ref)  # row 14 (removal only with the C2 switch)
        return None, None

    def _decide_bday(self, gid: str, links: LinkSet, state: CustomerState, verdict: tuple[str, str] | None, summary: RunSummary,
                     ref: str) -> PlannedWrite | None:
        """Slice D design 2.3, rows D2 to D13 (D1 is handled by the caller,
        D14 and D15 at the send). On only when Q is ``on`` and the month field
        is exactly this Singapore month; off only on a positive fact (another
        valid month whatever Q is, Q ``off``, a voided pair, or this tool's
        own add made in an earlier month); everything else is left alone."""

        if any(tag != BDAY_TAG and tag.strip().casefold() == BDAY_TAG for tag in state.tags):  # D2
            self._alert_once(summary, "bday_tag_case_variant", ref)
            return None
        tagged = BDAY_TAG in state.tags
        # Fix round 1 (G2 section 9): a valid month that is not this month is
        # a positive fact on its own, so the birthday tag comes off whatever
        # Q is (D4, D9, D13); only D1 and D2 leave it alone.
        other_month = tagged and state.bday_state == BDAY_MONTH and state.bday_month != self.month_now
        if gid in links.basis:
            member_no, basis = links.basis[gid]
            desired, code = verdict
            if desired == OFF:  # D8: the same positive fact
                if not tagged:
                    summary.count("bday_no_change_untagged")
                    return None
                return PlannedWrite(gid, "remove", code, member_no, basis, BDAY_TAG)
            if other_month:  # D4 (and D9 with another valid month)
                return PlannedWrite(gid, "remove", "bday_month_other", member_no, basis, BDAY_TAG)
            if desired == LEAVE:  # D9: the C1 alert is already raised
                summary.count("bday_left_with_member")
                return None
            if state.bday_state == BDAY_UNREADABLE:  # D7
                if tagged:
                    return self._bday_no_month_tagged(gid, "bday_month_unreadable", summary, ref, member_no, basis)
                self._alert_once(summary, "bday_month_unreadable", ref, member_no)
                return None
            if state.bday_state != BDAY_MONTH:  # no month on record
                if tagged:  # D6: a cleared field is not taken as a fact
                    return self._bday_no_month_tagged(gid, "bday_month_missing", summary, ref, member_no, basis)
                summary.count("bday_no_month")  # D5: no alert
                return None
            if state.bday_month != self.month_now:  # D4, not tagged
                summary.count("bday_no_change_untagged")
                return None
            if tagged:  # D3
                summary.count("bday_kept")
                return None
            if any(tag.strip().casefold() == REGISTRATION_TAG for tag in state.tags) and not state.has_registration:  # D10
                self._alert_once(summary, "flow_dates_guard", ref, member_no)
                return None
            if self.ledger.done_adds_since(gid, self.now() - FLAP_WINDOW, BDAY_TAG) >= FLAP_DONE_ADDS:  # D11
                self._alert_once(summary, "bday_tag_keeps_disappearing", ref, member_no)
                return None
            return PlannedWrite(gid, "add", "bday_month_current", member_no, basis, BDAY_TAG)
        if not tagged:
            return None
        if gid in links.voided:  # D12
            return PlannedWrite(gid, "remove", "link_voided", None, "voided", BDAY_TAG)
        if other_month:  # D13 with another valid month (fix round 1)
            return PlannedWrite(gid, "remove", "bday_month_other", None, None, BDAY_TAG)
        if self.unbased_ok[BDAY_TAG]:  # D13 with the C2 switch (design 2.6)
            return PlannedWrite(gid, "remove", "tag_without_basis", None, None, BDAY_TAG)
        self._alert_once(summary, "bday_tag_without_basis", ref)  # D13 (removal only with the C2 switch)
        return None

    def _bday_no_month_tagged(self, gid: str, code: str, summary: RunSummary, ref: str, member_no: str,
                              basis: str) -> PlannedWrite | None:
        """Fix round 1 (G2 section 9), D6 and D7 with the birthday tag on: off
        when this tool's last DONE birthday add for the customer was in an
        earlier Singapore month (its month has passed); otherwise left alone
        and alerted every run, not once, so the digest shows it daily."""

        if self._month_change(gid):
            return PlannedWrite(gid, "remove", "bday_added_earlier_month", member_no, basis, BDAY_TAG)
        summary.alert(code, ref, member_no)
        return None

    def _month_change(self, gid: str) -> bool:
        """Design 2.4: this tool's last DONE birthday add for the customer was
        recorded in an earlier Singapore month. It makes a ``bday_month_other``
        removal a month-change removal (exempt from the volume guard), and
        takes the tag off in D6 and D7 (fix round 1; not exempt)."""

        added = self.ledger.last_done_add_at(gid, BDAY_TAG)
        if added is None or self.today is None:
            return False
        added_day = added.astimezone(SGT).date()
        return (added_day.year, added_day.month) < (self.today.year, self.today.month)

    def _bday_volume_guard(self, removes: list[PlannedWrite], states: Mapping[str, CustomerState | None],
                           summary: RunSummary) -> list[PlannedWrite]:
        """Design 2.4: month-change removals are exempt; every other birthday
        removal is withheld as a whole when there are more than
        ``max(5, birthday-tagged // 10)`` of them."""

        exempt = [p for p in removes if p.reason == "bday_month_other" and self._month_change(p.gid)]
        other = [p for p in removes if p not in exempt]
        if exempt:
            summary.counts["bday_month_change_removals"] = len(exempt)
        bday_tagged = sum(1 for state in states.values() if state is not None and BDAY_TAG in state.tags)
        if len(other) > max(5, bday_tagged // 10):
            summary.alert("bday_untag_volume_unexpected")
            summary.counts["bday_untag_withheld"] = len(other)
            other = []
        return sorted(exempt + other, key=lambda p: customer_number(p.gid))

    def _bday_sendable(self, adds: list[PlannedWrite], removes: list[PlannedWrite],
                       summary: RunSummary) -> tuple[list[PlannedWrite], list[PlannedWrite]]:
        """The birthday writes ``apply`` may send (design 2.3 D14, D15 and
        2.4): none while ``bday_tag_enabled`` is false; only the pilot's
        customers while ``bday_only_customers`` is not empty; then the
        birthday caps."""

        if not self.config.bday_tag_enabled:  # D14
            if adds or removes:
                summary.counts["bday_writes_disabled"] = len(adds) + len(removes)
            return [], []
        if self.config.bday_only_customers:  # D15
            pilot = set(self.config.bday_only_customers)
            outside = [p for p in adds + removes if p.gid not in pilot]
            if outside:
                summary.counts["bday_not_in_pilot"] = len(outside)
            adds = [p for p in adds if p.gid in pilot]
            removes = [p for p in removes if p.gid in pilot]
        if len(adds) > self.config.max_bday_adds_per_run:
            summary.counts["bday_tag_adds_capped"] = len(adds) - self.config.max_bday_adds_per_run
            adds = adds[:self.config.max_bday_adds_per_run]
        if len(removes) > self.config.max_bday_removes_per_run:
            summary.alert("bday_tag_removes_capped")
            summary.counts["bday_tag_removes_capped"] = len(removes) - self.config.max_bday_removes_per_run
            removes = removes[:self.config.max_bday_removes_per_run]
        return adds, removes

    def _write(self, plans: list[PlannedWrite], states: dict[str, CustomerState | None], summary: RunSummary, *, run_id: str,
               apply: bool) -> None:
        bday_plans = [p for p in plans if p.tag == BDAY_TAG]
        plans = [p for p in plans if p.tag == TAG]
        adds = [p for p in plans if p.op == "add"]
        removes = [p for p in plans if p.op == "remove"]
        for plan in plans:
            word = "would_tag" if plan.op == "add" else "would_untag"
            summary.count(word, self.ledger.ref("c", plan.gid), plan.member_no)
            if plan.op == "remove":
                summary.count(f"untag_reason:{plan.reason}")
        for plan in bday_plans:
            summary.count("would_bday_tag" if plan.op == "add" else "would_bday_untag", self.ledger.ref("c", plan.gid), plan.member_no)
            if plan.op == "remove":
                summary.count(f"bday_untag_reason:{plan.reason}")
        # Volume guard (design 1.7): a systematic misread cannot trickle removals out.
        tagged = sum(1 for state in states.values() if state is not None and TAG in state.tags)
        if len(removes) > max(5, tagged // 10):
            summary.alert("untag_volume_unexpected")
            summary.counts["untag_withheld"] = len(removes)
            removes = []
        bday_adds = [p for p in bday_plans if p.op == "add"]
        bday_removes = self._bday_volume_guard([p for p in bday_plans if p.op == "remove"], states, summary)
        # Type-count check (design 2.7): a scan that does not add up to the
        # book's own counts takes neither tag off (adds go on).
        if not self.counts_ok and (removes or bday_removes):
            summary.counts["untag_withheld_scan_count"] = len(removes) + len(bday_removes)
            removes, bday_removes = [], []
        # Clock guard (design 2.2): no mutation at all; reads and the record still run.
        if not self.clock_ok:
            summary.stopped = "clock_mismatch"
            summary.alert("clock_mismatch")
            return
        if not apply:
            return
        if len(adds) > self.config.max_tag_adds_per_run:
            summary.counts["tag_adds_capped"] = len(adds) - self.config.max_tag_adds_per_run
            adds = adds[:self.config.max_tag_adds_per_run]
        if len(removes) > self.config.max_tag_removes_per_run:
            summary.alert("tag_removes_capped")
            summary.counts["tag_removes_capped"] = len(removes) - self.config.max_tag_removes_per_run
            removes = removes[:self.config.max_tag_removes_per_run]
        bday_adds, bday_removes = self._bday_sendable(bday_adds, bday_removes, summary)
        # Design 2.3: every member-qualified write first, then the birthday
        # writes, each by customer GID ascending.
        for plan in adds + removes + bday_adds + bday_removes:
            if self._stop_requested():
                summary.stopped = "stop_file_present"
                summary.alert("stop_file_present")
                return
            if plan.gid not in states or states[plan.gid] is None:
                raise dl.DirectLocalError("tags_customer_not_read")
            check_tag_variables(plan.gid, [plan.tag])  # before the INTENT row
            write_id = self.ledger.add_intent(run_id=run_id, customer_gid=plan.gid, tag=plan.tag, op=plan.op, reason=plan.reason,
                                              member_no=plan.member_no, basis=plan.basis)
            try:
                state, code = self.shopify.set_tag(plan.op, plan.gid, [plan.tag])
            except (http.client.HTTPException, OSError):
                # G2 L6: a transport error the shared transport does not map
                # (IncompleteRead, a raw socket error): it may have landed.
                state, code = UNCERTAIN, "shopify_transport_unmapped"
            self.ledger.set_outcome(write_id, state, code)
            ref = self.ledger.ref("c", plan.gid)
            if state == DONE:
                words = ("tagged", "untagged") if plan.tag == TAG else ("bday_tagged", "bday_untagged")
                summary.count(words[0] if plan.op == "add" else words[1], ref, plan.member_no)
                old = states[plan.gid]  # only this write's tag changes
                tags = old.tags + (plan.tag,) if plan.op == "add" else tuple(t for t in old.tags if t != plan.tag)
                states[plan.gid] = dataclasses.replace(old, tags=tags)
            elif state == REJECTED:
                summary.alert(code, ref, plan.member_no)
            elif state == NOT_WRITTEN:
                summary.alert("tag_not_written", ref, plan.member_no)
                if code in BUDGET_CODES:
                    summary.stopped = code
                    summary.alert(code)
                    return
            else:
                summary.alert("write_uncertain", ref, plan.member_no)
                summary.stopped = "write_uncertain"
                return


# --------------------------------------------------------------------------
# Digest (read only)
# --------------------------------------------------------------------------

_DIGEST_CODE_RE = re.compile(r"^[a-z0-9_:]{1,64}$")
_DIGEST_REF_RE = re.compile(r"^[cm]-[0-9a-f]{20}$")
_DIGEST_MASK_RE = re.compile(r"^\*{3,}[^*]{1,3}$")
EMAIL_MAX_ALERTS = 20


def ledger_digest(root: Path, *, hours: int, now: datetime, config: TagsConfig) -> dict[str, Any]:
    """Read-only: runs, counts and alerts per code over the last ``hours``,
    alert refs (keyed refs and masked MemberNos only), open writes, and the
    latest run's shadow counts, ``tagged_now`` and ``carriers_total``."""

    path = Path(root) / LEDGER_FILE
    if not path.is_file():
        raise dl.DirectLocalError("tags_ledger_missing")
    try:
        conn = open_read_only(path)
    except sqlite3.Error:
        raise dl.DirectLocalError("tags_ledger_unreadable") from None
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise dl.DirectLocalError("tags_ledger_not_recognised")
        meta = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        if meta is None:
            raise dl.DirectLocalError("tags_ledger_not_initialised")
        TagsLedger._check(meta, config)
        since = dl.ts(now - timedelta(hours=hours))
        counts: dict[str, int] = {}
        alerts: dict[str, int] = {}
        refs: list[dict[str, str]] = []
        completed = unfinished = 0
        latest: dict[str, Any] = {}
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
            latest = stored
            for code, number in (stored.get("counts") or {}).items():
                if isinstance(code, str) and _DIGEST_CODE_RE.fullmatch(code) and type(number) is int:
                    counts[code] = counts.get(code, 0) + number
            run_alerts = [c for c in stored.get("alerts") or [] if isinstance(c, str) and _DIGEST_CODE_RE.fullmatch(c)]
            for code in run_alerts:
                alerts[code] = alerts.get(code, 0) + 1
            for entry in stored.get("refs") or []:
                if not isinstance(entry, dict) or entry.get("code") not in run_alerts or len(refs) >= MAX_REFS:
                    continue
                if not isinstance(entry.get("ref"), str) or not _DIGEST_REF_RE.fullmatch(entry["ref"]):
                    continue
                item = {"ref": entry["ref"], "code": entry["code"]}
                masked = entry.get("member_no_masked")
                if isinstance(masked, str) and _DIGEST_MASK_RE.fullmatch(masked):
                    item["member_no_masked"] = masked
                refs.append(item)
        last_counts = latest.get("counts") if isinstance(latest.get("counts"), dict) else {}

        def number(value: Any) -> int | None:
            return value if type(value) is int else None

        shadow = {word: number(last_counts.get(word)) or 0 for word in ("would_tag", "would_untag", "would_bday_tag", "would_bday_untag")}
        open_writes = {"uncertain_writes": conn.execute("SELECT COUNT(*) FROM tag_writes WHERE state = 'UNCERTAIN'").fetchone()[0],
                       "intent_writes": conn.execute("SELECT COUNT(*) FROM tag_writes WHERE state = 'INTENT'").fetchone()[0]}
        # Slice D (design 2.8): the month changed and the tags have not. True
        # when the latest completed run's month is not the digest's own
        # Singapore month (a run with no month counts as not this month);
        # false when no run completed in the window (runs_completed says so).
        stale_month = completed > 0 and latest.get("bday_month_now") != month_name(sg_today(now))
        # The fixed, flat block for the #288 daily email: whole numbers, codes
        # and booleans only (0 for a figure when no run completed).
        email = {"runs_completed": completed, "runs_unfinished": unfinished,
                 "tagged_now": number(latest.get("tagged_now")) or 0, "bday_tagged_now": number(latest.get("bday_tagged_now")) or 0,
                 **shadow, "alerts": sorted(alerts)[:EMAIL_MAX_ALERTS], "stale_month": stale_month,
                 "open_writes": open_writes["uncertain_writes"] + open_writes["intent_writes"]}
        return {
            "status": "ok", "command": "digest", "hours": hours, "runs": {"completed": completed, "unfinished": unfinished},
            "counts": dict(sorted(counts.items())), "alerts": dict(sorted(alerts.items())), "alert_refs": refs,
            "open": open_writes, "shadow": shadow,
            "tagged_now": number(latest.get("tagged_now")), "carriers_total": number(latest.get("carriers_total")),
            "bday_tagged_now": number(latest.get("bday_tagged_now")), "bday_carriers_total": number(latest.get("bday_carriers_total")),
            "stale_month": stale_month, "email": email,
        }
    except sqlite3.Error:
        raise dl.DirectLocalError("tags_ledger_unreadable") from None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def build_real_engine(config: TagsConfig, ledger: TagsLedger, *, environ: Mapping[str, str], apply: bool) -> TagsEngine:
    transport = direct.UrllibTransport()
    shopify = TagsShopifyClient(
        mutations_enabled=apply and config.tags_write_enabled, shop_domain=config.shopify.shop_domain,
        api_version=config.shopify.api_version, token_provider=token_provider(config.shopify, environ, transport, dl.utc_now, root=ledger.root),
        transport=transport, timeout=config.http_timeout_seconds,
    )
    env = None
    if config.ac2_credential_target is not None:
        env = sync.load_ac2_credential(config.ac2_credential_target, refused_user_ids=config.ac2_refused_user_ids).child_environment(environ)
    elif config.autocount_book == sync.LIVE_BOOK:
        raise dl.DirectLocalError("ac2_credential_target_unconfigured")  # parse_config refuses this first
    # The claims folder is required by the type move client and never used:
    # this client refuses every write before a child starts.
    ac2 = TagsReadBridgeClient(dl.default_bridge_command(BRIDGE_SCRIPT), autocount_book=config.autocount_book,
                               claims=ledger.root / "claims-unused", env=env)
    return TagsEngine(config, ledger, shopify=shopify, ac2=ac2)


COMMANDS = ("check-config", "init", "status", "observe", "apply", "digest")


def main(argv: list[str] | None = None, *, engine_factory: Callable[[TagsConfig, TagsLedger, bool], TagsEngine] | None = None,
         clock: Callable[[], datetime] = dl.utc_now) -> int:
    parser = argparse.ArgumentParser(description="#268: the Shopify tags member-qualified and member-bday-month, kept right from AutoCount")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--config", required=True)
    parser.add_argument("--hours", type=int, default=24, help="digest only: the last N hours of runs (1-720)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check-config":
            _emit(config.summary())
            return 0
        # The write switch is refused before any login, read or ledger write.
        if args.command == "apply" and not config.tags_write_enabled:
            raise dl.DirectLocalError("tags_write_disabled")
        root = state_root(config)
        if args.command == "digest":
            if not 1 <= args.hours <= 720:
                raise dl.DirectLocalError("digest_hours_invalid")
            _emit(ledger_digest(root, hours=args.hours, now=clock(), config=config))
            return 0
        ledger = TagsLedger(root, clock=clock)
        try:
            if args.command == "init":
                _emit(ledger.init_meta(config))
                return 0
            if args.command == "status":
                _emit(ledger.status(config))
                return 0
            ledger.check_binding(config)
            apply = args.command == "apply"
            factory = engine_factory or (lambda cfg, led, write: build_real_engine(cfg, led, environ=os.environ, apply=write))
            result = factory(config, ledger, apply).run(apply=apply)
            _emit(result)
            return 3 if result["alerts"] or result["stopped"] else 0
        finally:
            ledger.close()
    except dl.DirectLocalError as exc:
        _emit({"status": "refused", "code": exc.code})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
