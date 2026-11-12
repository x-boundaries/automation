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


def mask(member_no: str | None) -> str:
    return sync.mask_last3(member_no)


def customer_number(gid: str) -> int:
    """The numeric part of a customer GID (the deterministic order)."""

    match = _CUSTOMER_GID_RE.fullmatch(gid) if isinstance(gid, str) else None
    if match is None:
        raise dl.DirectLocalError("tags_customer_gid_invalid")
    return int(match.group(1))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


COMMANDS = ("check-config",)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#268 slice C1: the Shopify tag member-qualified, kept right from AutoCount")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        _emit(config.summary())
        return 0
    except dl.DirectLocalError as exc:
        _emit({"status": "refused", "code": exc.code})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
