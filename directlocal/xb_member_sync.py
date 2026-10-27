"""#155 phase 1: two-way member CREATE sync between Shopify and AutoCount 2.

Scope (Owner decisions, #155 build step 2)
------------------------------------------
* Shopify -> AC2: a Shopify customer who registers as a member (tag
  ``member-mg``, registration instant at or after ``shopify_cutover_utc``)
  becomes an AutoCount member. MemberNo is the phone number in digits
  (Singapore ``65`` + 8 digits; a foreign number keeps its country code; a
  Singapore 8-digit number starting 3/6/8/9 gets ``65``). Without a usable
  phone the member gets AutoCount's own ``GetNextMemberNo`` number. MemberType ``REGISTERED``, ExpiryDate NULL, DOB ``2000-MM-01``
  from the configured birthday-month metafield (NULL when absent), IsActive
  ``T``. The Shopify expiry metafield is ignored.
* AC2 -> Shopify: a member created in AutoCount (for example at the till)
  reaches HQ ``dbo.ChangeLog`` as an insert (``TableName = 'Member'``,
  ``ChangeType = 'I'``, XML in ``Content``). Rows are read by an ``ID``
  watermark kept in the sync ledger, re-reading a small overlap below it
  (gaps tolerated; a replay is a no-op; an unparseable row or a numbering
  reset stops with an alert). Before creating, the Shopify customer is looked
  up by phone, and only customers whose own phone matches count: found ->
  link, never a duplicate.
  ``customerCreate`` is sent only when ``shopify_create_enabled`` is true.
  ChangeLog reads are off until ``changelog_xml_column`` is set (Owner Q4).
* No review queue. Same phone + the same name links, where the same name
  means every word of the shorter full name is a word of the longer one
  (Owner Q1, ``full_name_matches``); same phone + another name creates
  ``phone + name letters`` (fits nvarchar(20), a digit 2-9 when taken); no
  phone uses ``GetNextMemberNo``; email never links. In AC2 -> Shopify, a
  Shopify customer holding the phone under another name is a different
  person: a separate customer is created without the phone. Every link, skip
  and fallback is counted in the run summary (counts and keyed refs only).
* Loop prevention both ways through the ledger mapping (customer GID <->
  MemberNo, both unique): a member this sync created in AC2 never comes back
  to Shopify as new, and a customer it created in Shopify never goes back to
  AC2.

Safety
------
* ``AED_XBOUNDARIES`` is refused here and in the PowerShell bridge; only
  ``AED_TESTINGAPI`` is authorised in this source.
* Every write switch defaults OFF; at most ``max_writes_per_run`` (<= 5)
  writes per run across both directions; unknown config keys are refused.
* A write is recorded (INTENT) before it starts. A crash, timeout or unclear
  result is never retried blindly: the next run only looks the record up and
  links it when found.
* The ledger (``member_sync.sqlite3`` in a private state root) keeps GIDs,
  MemberNos, watermarks, codes and salted digests. Output carries counts and
  keyed references (``c-``/``m-``), never a name, phone, email or MemberNo.
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
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

_HERE = Path(__file__).resolve().parent
# Appended, never prepended (same rule as the Core): nothing here may shadow a
# standard-library module.
if str(_HERE) not in sys.path:
    sys.path.append(str(_HERE))

import xb_directlocal as dl  # noqa: E402
import xb_shopify_direct as direct  # noqa: E402

CONFIG_SCHEMA = "xb.directlocal.member_sync.config.v1"
SUMMARY_SCHEMA = "xb.directlocal.member_sync.run.v1"
LEDGER_FILE = "member_sync.sqlite3"
# v2 (2026-10-22, G2 B2 and fact 7): writes.intent_digest and changelog_seen,
# added to a v1 ledger in one transaction (additive; nothing else changes).
SCHEMA_VERSION = 2

AUTHORISED_BOOKS = frozenset({"AED_TESTINGAPI"})
FORBIDDEN_BOOKS = frozenset({"AED_XBOUNDARIES"})
MAX_WRITES_PER_RUN_LIMIT = 5
MEMBER_NO_WIDTH = 20  # AutoCount Member.MemberNo nvarchar(20)
SGT = timezone(timedelta(hours=8))
REGISTRATION_TAG = "member-mg"
SHOPIFY_PAGE_SIZE = 50
SHOPIFY_WATERMARK_MARGIN_SECONDS = 900  # search-index lag overlap, as in the Core
REGISTRATION_WAIT_MAX_CHECKS = 5  # Forms writes start_date ~3 s after the tag
LOOKUP_MAX_ROWS = 50
PENDING_LOOKUP_BATCH = 20
# Round 6: after a sync create, re-check for a later staff-made member of
# the same person for this long (bounded lookups per run).
DUPLICATE_WATCH_DAYS = 7
DUPLICATE_WATCH_PER_RUN = 10
LOCAL_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
MEMBER_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
# Round 7: AC2 Member.CreatedTime as the bridge sends it
# (CONVERT(varchar(23), CreatedTime, 126); style 126 drops a zero fraction).
AC2_CREATED_TIME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,3})?$")
# Round 7c: watermarks of the one-off initial link. Shopify -> AC2 waits
# until an applied pass has read every pre-cutover customer.
INITIAL_LINK_DONE_MARK = "initial_link_completed_at"
INITIAL_LINK_CURSORS = {True: "initial_link_updated_at", False: "initial_link_dry_run_updated_at"}
INITIAL_LINK_BATCH = PENDING_LOOKUP_BATCH // 2  # two MemberNo forms per customer
# Watermarks-table key of the ChangeLog overlap the last run used (G2 H3).
OVERLAP_MARK = "ac2_changelog_overlap"
IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _:-]{0,39}$")
METAFIELD_PART_RE = re.compile(r"^[a-z0-9_]{1,64}$")
METAFIELD_TYPES = frozenset({"number_integer", "single_line_text_field"})
CREDENTIAL_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,127}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_SG_LOCAL_RE = re.compile(r"^[3689][0-9]{7}$")
_PHONE_SEPARATORS_RE = re.compile(r"[\s().-]")
_MONTHS = {name: index for index, names in enumerate(
    (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
     ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december")),
    start=1) for name in names}

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

_CONFIG_REQUIRED = {"schema", "state_root", "autocount_book", "shopify", "shopify_cutover_utc"}
_CONFIG_DEFAULTS: dict[str, Any] = {
    "ac2_create_enabled": False,
    "shopify_create_enabled": False,
    "max_writes_per_run": MAX_WRITES_PER_RUN_LIMIT,
    # Owner: placeholder until Shopify's developer confirms it.
    "birthday_month_metafield": {"namespace": "custom", "key": "birthday_month", "type": "number_integer"},
    # Owner (2026-10-21): tags on a customer created from AC2.
    "shopify_create_tags": ["ac2-member"],
    # The AC2 MemberNo on a customer created from AC2 (maps it back).
    "ac2_member_no_metafield": {"namespace": "custom", "key": "ac2_member_no", "type": "single_line_text_field"},
    # Name of the generic Windows Credential Manager entry holding the
    # AutoCount account the sync logs in with (the Owner's own account,
    # 2026-10-21); null keeps the attended owner-console credential path.
    # Every AutoCount API session runs as SQL login sa whatever the account,
    # so the limits are this source's guards, not the account's rights.
    "ac2_credential_target": None,
    # VM fact r5 (2026-10-21): dbo.ChangeLog.Content nvarchar(max) holds the
    # insert XML. Owner Q4 (2026-10-21): null (off) by default, so AC2 ->
    # Shopify reads no ChangeLog and makes no links; the operator sets
    # "Content" at UAT, after confirming the name rule. Shopify creates still
    # need shopify_create_enabled.
    "changelog_xml_column": None,
    "changelog_batch": 200,
    # IDs below the watermark re-read on every run, so an insert that
    # committed after a higher ID was read is still seen (fact 7).
    "changelog_overlap": 50,
    "max_shopify_customers_per_run": 20,
    # Round 8 (G2 M1): pending sign-ups re-checked per run, outside the
    # budget above, so they never block new sign-ups.
    "max_pending_rechecks_per_run": 100,
    "shopify_max_pages": 10,
    "http_timeout_seconds": 30,
    # Round 6 (Owner, 22 Oct 2026): staff create members by hand from the
    # sign-up sheet. A sign-up still unmatched at this time (HH:MM,
    # Asia/Singapore) on the day after sign-up is created by the sync, with
    # this AutoCount member type (the test book's type; production differs).
    "deferred_create_local_time": "06:00",
    "ac2_create_member_type": "REGISTERED",
    # Round 7 (Owner, 22 Oct 2026): staff key the member in AC2 on the spot.
    # The AC2 member whose MemberNo is the sign-up's phone MemberNo links when
    # its CreatedTime and the sign-up time (round 7c: registration_at, the
    # membership.start_date metafield's createdAt) are at most this many
    # minutes apart (either way). Round 10 (Owner UAT r4, 24 Oct 2026):
    # CreatedTime is when staff OPEN the New Member form, not when they save
    # it, so the window runs from form open; default 60 (was 10).
    "memberno_link_window_minutes": 60,
    # Round 7c (Owner): the one-off initial link (CLI ``initial-link``) is a
    # dry run unless ``--apply`` is given AND this is on.
    "initial_link_enabled": False,
}
_SHOPIFY_KEYS_REQUIRED = {"store_gid", "shop_domain", "api_version", "auth"}
_SHOPIFY_KEYS_OPTIONAL = {"token_cache"}
_BOUNDS = {
    "max_writes_per_run": (0, MAX_WRITES_PER_RUN_LIMIT),
    "changelog_batch": (1, 500),
    "changelog_overlap": (0, 200),
    "max_shopify_customers_per_run": (1, 100),
    "max_pending_rechecks_per_run": (1, 500),
    "shopify_max_pages": (1, 40),
    "http_timeout_seconds": (5, 60),
    "memberno_link_window_minutes": (1, 60),
}


@dataclass(frozen=True)
class ShopifyConnection:
    """Duck-typed for ``xb_shopify_poller.token_provider`` (names of
    environment variables only, never a secret value)."""

    store_gid: str
    shop_domain: str
    api_version: str
    auth_method: str
    auth_env: Mapping[str, str]
    token_cache: str
    http_timeout_seconds: int


@dataclass(frozen=True)
class SyncConfig:
    state_root: str
    autocount_book: str
    shopify: ShopifyConnection
    shopify_cutover_utc: datetime
    ac2_create_enabled: bool
    shopify_create_enabled: bool
    max_writes_per_run: int
    birthday_month_metafield: Mapping[str, str]
    ac2_member_no_metafield: Mapping[str, str]
    shopify_create_tags: tuple[str, ...]
    ac2_credential_target: str | None
    changelog_xml_column: str | None
    deferred_create_local_time: str
    ac2_create_member_type: str
    changelog_batch: int
    changelog_overlap: int
    max_shopify_customers_per_run: int
    max_pending_rechecks_per_run: int
    shopify_max_pages: int
    http_timeout_seconds: int
    memberno_link_window_minutes: int
    initial_link_enabled: bool

    def summary(self) -> dict[str, Any]:
        """PII-free view for ``check-config``."""

        return {
            "autocount_book": self.autocount_book, "ac2_create_enabled": self.ac2_create_enabled,
            "shopify_create_enabled": self.shopify_create_enabled, "max_writes_per_run": self.max_writes_per_run,
            "shopify_cutover_utc": dl.ts(self.shopify_cutover_utc), "birthday_month_metafield": dict(self.birthday_month_metafield),
            "shopify_create_tags": list(self.shopify_create_tags), "ac2_credential": "credential_manager" if self.ac2_credential_target else "owner_console_environment",
            "changelog_configured": self.changelog_xml_column is not None,
            "deferred_create_local_time": self.deferred_create_local_time, "ac2_create_member_type": self.ac2_create_member_type,
            "memberno_link_window_minutes": self.memberno_link_window_minutes, "initial_link_enabled": self.initial_link_enabled,
        }


def check_book(book: Any) -> str:
    """The production book is refused by name; anything not authorised is
    refused too."""

    if book in FORBIDDEN_BOOKS:
        raise dl.DirectLocalError("production_book_refused")
    if book not in AUTHORISED_BOOKS:
        raise dl.DirectLocalError("autocount_book_not_authorised")
    return book


def _bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise dl.DirectLocalError(f"sync_config_{key}_invalid")
    return value


def parse_config(value: Any) -> SyncConfig:
    if not isinstance(value, dict):
        raise dl.DirectLocalError("sync_config_invalid")
    keys = set(value)
    if not _CONFIG_REQUIRED <= keys or not keys <= _CONFIG_REQUIRED | set(_CONFIG_DEFAULTS):
        raise dl.DirectLocalError("sync_config_keys_invalid")
    if value["schema"] != CONFIG_SCHEMA:
        raise dl.DirectLocalError("sync_config_schema_invalid")
    merged = {**_CONFIG_DEFAULTS, **value}
    book = check_book(merged["autocount_book"])
    if not isinstance(merged["state_root"], str) or not merged["state_root"]:
        raise dl.DirectLocalError("sync_config_state_root_invalid")
    cutover = dl.parse_created_at(merged["shopify_cutover_utc"]) if isinstance(merged["shopify_cutover_utc"], str) else None
    if cutover is None:
        raise dl.DirectLocalError("sync_config_cutover_invalid")
    numbers = {}
    for key, (low, high) in _BOUNDS.items():
        number = merged[key]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise dl.DirectLocalError(f"sync_config_{key}_invalid")
        numbers[key] = number
    shopify = _parse_shopify(merged["shopify"], numbers["http_timeout_seconds"])
    birthday = merged["birthday_month_metafield"]
    if not isinstance(birthday, dict) or set(birthday) != {"namespace", "key", "type"} or not all(
        isinstance(birthday[k], str) and METAFIELD_PART_RE.fullmatch(birthday[k]) for k in ("namespace", "key")
    ) or birthday["type"] not in METAFIELD_TYPES:
        raise dl.DirectLocalError("sync_config_birthday_month_metafield_invalid")
    member_no_field = merged["ac2_member_no_metafield"]
    if not isinstance(member_no_field, dict) or set(member_no_field) != {"namespace", "key", "type"} or not all(
        isinstance(member_no_field[k], str) and METAFIELD_PART_RE.fullmatch(member_no_field[k]) for k in ("namespace", "key")
    ) or member_no_field["type"] != "single_line_text_field" or (member_no_field["namespace"], member_no_field["key"]) == (birthday["namespace"], birthday["key"]):
        raise dl.DirectLocalError("sync_config_ac2_member_no_metafield_invalid")
    tags = merged["shopify_create_tags"]
    if not isinstance(tags, list) or len(tags) > 10 or any(not isinstance(t, str) or not TAG_RE.fullmatch(t) for t in tags) or REGISTRATION_TAG in tags:
        # The registration tag would send the customer straight back to AC2.
        raise dl.DirectLocalError("sync_config_shopify_create_tags_invalid")
    target = merged["ac2_credential_target"]
    if target is not None and (not isinstance(target, str) or not CREDENTIAL_TARGET_RE.fullmatch(target)):
        raise dl.DirectLocalError("sync_config_ac2_credential_target_invalid")
    if numbers["changelog_overlap"] >= numbers["changelog_batch"]:
        raise dl.DirectLocalError("sync_config_changelog_overlap_invalid")
    column = merged["changelog_xml_column"]
    if column is not None and (not isinstance(column, str) or not IDENTIFIER_RE.fullmatch(column)):
        raise dl.DirectLocalError("sync_config_changelog_xml_column_invalid")
    local_time = merged["deferred_create_local_time"]
    if not isinstance(local_time, str) or not LOCAL_TIME_RE.fullmatch(local_time):
        raise dl.DirectLocalError("sync_config_deferred_create_local_time_invalid")
    member_type = merged["ac2_create_member_type"]
    if not isinstance(member_type, str) or not MEMBER_TYPE_RE.fullmatch(member_type):
        raise dl.DirectLocalError("sync_config_ac2_create_member_type_invalid")
    return SyncConfig(
        state_root=merged["state_root"], autocount_book=book, shopify=shopify, shopify_cutover_utc=cutover,
        ac2_create_enabled=_bool(merged["ac2_create_enabled"], "ac2_create_enabled"),
        shopify_create_enabled=_bool(merged["shopify_create_enabled"], "shopify_create_enabled"),
        initial_link_enabled=_bool(merged["initial_link_enabled"], "initial_link_enabled"),
        birthday_month_metafield=dict(birthday), ac2_member_no_metafield=dict(member_no_field), shopify_create_tags=tuple(tags), ac2_credential_target=target,
        changelog_xml_column=column, deferred_create_local_time=local_time, ac2_create_member_type=member_type, **numbers,
    )


def _parse_shopify(value: Any, timeout: int) -> ShopifyConnection:
    if not isinstance(value, dict) or not _SHOPIFY_KEYS_REQUIRED <= set(value) or not set(value) <= _SHOPIFY_KEYS_REQUIRED | _SHOPIFY_KEYS_OPTIONAL:
        raise dl.DirectLocalError("sync_config_shopify_invalid")
    if dl.QUALIFIED_CONNECTOR_STORES.get(value["store_gid"]) != value["shop_domain"] and not (
        isinstance(value["store_gid"], str) and dl.SHOP_GID_RE.fullmatch(value["store_gid"])
        and isinstance(value["shop_domain"], str) and dl.SYNTHETIC_DOMAIN_RE.fullmatch(value["shop_domain"])
    ):
        raise dl.DirectLocalError("sync_config_store_not_qualified")
    if not isinstance(value["api_version"], str) or not dl.API_VERSION_RE.fullmatch(value["api_version"]):
        raise dl.DirectLocalError("shopify_api_version_invalid")
    auth = value["auth"]
    method = auth.get("method") if isinstance(auth, dict) else None
    names = {"client_credentials": {"client_id_env", "client_secret_env"}, "admin_api_access_token": {"token_env"}}.get(method)
    if names is None or set(auth) != names | {"method"}:
        raise dl.DirectLocalError("sync_config_shopify_auth_invalid")
    env = {key: auth[key] for key in names}
    if any(not isinstance(name, str) or not re.fullmatch(r"^XB_SHOPIFY_[A-Z0-9_]{1,48}$", name) for name in env.values()) or len(set(env.values())) != len(env):
        raise dl.DirectLocalError("sync_config_shopify_auth_invalid")
    token_cache = value.get("token_cache", "none")
    if token_cache not in ("none", "dpapi_current_user") or (token_cache != "none" and method != "client_credentials"):
        raise dl.DirectLocalError("sync_config_shopify_token_cache_invalid")
    return ShopifyConnection(value["store_gid"], value["shop_domain"], value["api_version"], method, env, token_cache, timeout)


def load_config(path: str | Path) -> SyncConfig:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise dl.DirectLocalError("sync_config_unreadable") from None
    if len(raw) > 65_536:
        raise dl.DirectLocalError("sync_config_invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise dl.DirectLocalError("sync_config_invalid") from None
    return parse_config(value)


# --------------------------------------------------------------------------
# Matching rules (pure)
# --------------------------------------------------------------------------

def phone_member_no(raw: Any) -> tuple[str | None, str]:
    """``(digits, status)``: the MemberNo for a phone, with ``status`` one of
    ``ok``, ``absent`` or ``invalid``.

    * ``+65`` and 8 digits starting 3/6/8/9 -> ``65`` + 8 digits.
    * Any other ``+`` number of 8-15 digits -> the full number with its
      country code.
    * Without ``+``: 8 digits starting 3/6/8/9 -> ``65`` prepended; ``65`` +
      such 8 digits -> unchanged; 9-15 digits -> taken as a full international
      number; anything else is invalid (never guessed).
    """

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, "absent"
    if not isinstance(raw, str) or len(raw) > 40:
        return None, "invalid"
    text = _PHONE_SEPARATORS_RE.sub("", raw.strip())
    plus = text.startswith("+")
    digits = text[1:] if plus else text
    if not digits.isdigit() or not digits.isascii():
        return None, "invalid"
    if plus:
        if digits.startswith("65"):
            return (digits, "ok") if len(digits) == 10 and _SG_LOCAL_RE.fullmatch(digits[2:]) else (None, "invalid")
        return (digits, "ok") if 8 <= len(digits) <= 15 and digits[0] != "0" else (None, "invalid")
    if len(digits) == 8:
        return ("65" + digits, "ok") if _SG_LOCAL_RE.fullmatch(digits) else (None, "invalid")
    if len(digits) == 10 and digits.startswith("65") and _SG_LOCAL_RE.fullmatch(digits[2:]):
        return digits, "ok"
    if 9 <= len(digits) <= 15 and digits[0] != "0" and not digits.startswith("65"):
        return digits, "ok"
    return None, "invalid"


def e164(digits: str) -> str:
    return "+" + digits


def sg_local(digits: str | None) -> str | None:
    """The Singapore 8 digits of a ``65xxxxxxxx`` MemberNo, else None."""

    return digits[2:] if digits and len(digits) == 10 and digits.startswith("65") and _SG_LOCAL_RE.fullmatch(digits[2:]) else None


def member_no_key(value: str) -> str:
    """AutoCount's MemberNo uniqueness ignores case and trailing spaces; one
    production MemberNo also has a leading space. Duplicate checks therefore
    trim both ends and compare case-insensitively."""

    return value.strip().casefold()


def _name_tokens(text: Any) -> tuple[str, ...]:
    """The words of a name, in order: NFKC, case-folded, and every character
    that is not a Unicode letter, mark or decimal digit (punctuation, hyphens,
    apostrophes, dots, commas, any whitespace) read as a space. Any script: a
    name written without spaces (common in CJK) is one word."""

    if not isinstance(text, str):
        return ()
    folded = unicodedata.normalize("NFKC", text).casefold()
    return tuple("".join(ch if unicodedata.category(ch)[0] in "LM" or unicodedata.category(ch) == "Nd" else " " for ch in folded).split())


def name_words(text: Any) -> frozenset[str]:
    """The name as a set of words (order and repeats do not matter)."""

    return frozenset(_name_tokens(text))


def shopify_full_name(first_name: Any, last_name: Any) -> str:
    """Shopify's full name: ``firstName`` + " " + ``lastName`` (either may be
    empty; AC2 -> Shopify creates put the whole name in ``firstName``)."""

    return " ".join(part for part in (first_name, last_name) if isinstance(part, str))


MIN_NAME_WORDS = 2


def full_name_matches(name_a: Any, name_b: Any) -> bool:
    """Owner Q1 (2026-10-21, addendum r3a): on the same phone, the names
    match when the shorter name has at least two words and every one of them
    is a word of the longer one ("Ann Lee" and "MARY ANN LEE"). Exact words
    only: no prefixes, no fuzzy matching ("Anne" is not "Ann"). A name of 0
    or 1 words (blank, only punctuation, "Lee", or a name written without
    spaces) never matches, even an identical one."""

    shorter, longer = sorted((name_words(name_a), name_words(name_b)), key=len)
    return len(shorter) >= MIN_NAME_WORDS and shorter <= longer


def name_letters(*parts: Any) -> str:
    """A-Z only, upper case, accents folded (``Zoe`` from ``Zoë``)."""

    text = " ".join(p for p in parts if isinstance(p, str))
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z]", "", folded.upper())


def fallback_member_no(phone_digits: str, first_name: Any, last_name: Any) -> str | None:
    """Shared-phone MemberNo (Owner, 2026-10-21): ``<phone digits><FIRST+LAST
    letters>`` truncated to 20 characters. None when no Latin letter is left."""

    letters = name_letters(first_name, last_name)
    if not letters:
        return None
    return (phone_digits + letters)[:MEMBER_NO_WIDTH]


def suffix_candidates(base: str) -> list[str]:
    """``base``, then ``base`` with a digit 2-9 appended, letters trimmed so
    the MemberNo stays within 20 characters."""

    return [base] + [base[:MEMBER_NO_WIDTH - 1] + str(digit) for digit in range(2, 10)]


def deferred_create_cutoff(signed_up: datetime, local_time: str) -> datetime:
    """Round 6: the next morning. ``local_time`` (HH:MM) in Asia/Singapore
    (UTC+8 all year, no daylight saving, so a fixed offset is exact) on the
    day after the sign-up's Singapore date; never the same morning."""

    day = signed_up.astimezone(SGT).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, int(local_time[:2]), int(local_time[3:]), tzinfo=SGT)


def birthday_month(value: Any) -> int | None:
    """Month 1-12 from the metafield value: a number, a month name, or a date
    (``YYYY-MM-DD``, ``YYYY-MM``, ``--MM-DD``). Anything else is None."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 12 else None
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text.isdigit() and text.isascii():
        number = int(text)
        return number if 1 <= number <= 12 else None
    if text in _MONTHS:
        return _MONTHS[text]
    match = re.fullmatch(r"(?:[0-9]{4}|-)-([0-9]{2})(?:-[0-9]{2})?", text)
    if match and 1 <= int(match.group(1)) <= 12:
        return int(match.group(1))
    return None


def dob_from_month(month: int | None) -> str | None:
    return None if month is None else f"2000-{month:02d}-01"


@dataclass(frozen=True)
class Ac2Member:
    member_no: str
    name: str | None = None
    mobile_phone: str | None = None
    email_address: str | None = None
    dob: str | None = None
    created_at: datetime | None = None  # round 7: CreatedTime (form open), in UTC

    def __repr__(self) -> str:  # never render member data
        return "Ac2Member()"


def ac2_created_time(value: Any) -> datetime | None:
    """Round 7: AC2 ``Member.CreatedTime`` as UTC. AC2 stores Singapore local
    time with no offset (VM fact G0, 22 Oct 2026), so the text is read as
    Asia/Singapore (UTC+8 all year). Round 10 (Owner UAT r4): it is the time
    the New Member form was opened, not saved. None when absent or not in the
    bridge's exact shape; a missing time never links."""

    if not isinstance(value, str) or not AC2_CREATED_TIME_RE.fullmatch(value):
        return None
    try:
        local = datetime.fromisoformat(value)
    except ValueError:
        return None
    return local.replace(tzinfo=SGT).astimezone(timezone.utc)


@dataclass(frozen=True)
class ShopifyCustomer:
    gid: str
    first_name: str | None
    last_name: str | None
    phone: str | None
    email: str | None
    created_at: datetime | None
    updated_at: datetime | None
    tags: tuple[str, ...]
    registration_at: datetime | None
    registration_value: str | None
    birthday_value: Any

    def __repr__(self) -> str:  # never render customer data
        return "ShopifyCustomer()"


@dataclass(frozen=True)
class Decision:
    action: str  # LINK | CREATE | SKIP
    code: str
    member_no: str | None = None
    notes: tuple[str, ...] = ()  # counted with the code (UAT visibility only)


def name_fallback_notes(name: Any, others: Iterable[Any]) -> tuple[str, ...]:
    """Counters for a same-phone pair that did not link, so UAT can see the
    fallbacks a short name caused (round 5): ``name_fallback_one_word`` when
    either side has fewer than 2 words (it can never link, r3a), and
    ``name_fallback_initial`` when either side has a one-character word (an
    initial never matches a full word). Counts only; no decision changes."""

    word_sets = [name_words(value) for value in (name, *others)]
    notes = []
    if any(len(words) < MIN_NAME_WORDS for words in word_sets):
        notes.append("name_fallback_one_word")
    if any(len(word) == 1 for words in word_sets for word in words):
        notes.append("name_fallback_initial")
    return tuple(notes)


def _phone_hit(member: Ac2Member, digits: str) -> bool:
    """Same person's phone in AC2: MemberNo equals the phone (or its SG 8
    digits), MobilePhone normalises to the phone, or the SG 8 digits are
    embedded in the MemberNo."""

    local = sg_local(digits)
    key = member_no_key(member.member_no)
    if key in ({digits} | ({local} if local else set())):
        return True
    if phone_member_no(member.mobile_phone)[0] == digits:
        return True
    return bool(local and local in member.member_no)


def member_no_forms(phone_digits: str | None) -> set[str]:
    """Round 7c: the MemberNo keys that are this phone exactly: the phone
    MemberNo, plus the 8 digits without ``65`` for a Singapore number."""

    if phone_digits is None:
        return set()
    local = sg_local(phone_digits)
    return {phone_digits} | ({local} if local else set())


def decide_shopify_to_ac2(customer: ShopifyCustomer, phone_digits: str | None, candidates: Iterable[Ac2Member], window: timedelta = timedelta(minutes=60),
                          signed_up: datetime | None = None) -> Decision:
    """The no-review-queue rules for one Shopify registration. ``candidates``
    are AC2 rows found by phone. Email is never consulted.

    Round 7 (Owner): the record whose MemberNo is exactly the phone (round
    7c: the phone MemberNo or its 8 Singapore digits) decides by time alone:
    linked when its CreatedTime and ``signed_up`` (round 7c: the sign-up
    time, not Customer.createdAt) are at most ``window`` apart (either way),
    else neither linked nor created; two such records are a conflict. Name
    is not used there. The 2-word name rule is left only for records that
    hold the phone otherwise (MobilePhone, the 8 digits embedded in a longer
    MemberNo), where it chooses between a link and the shared-phone fallback
    MemberNo.

    A CREATE without ``member_no`` takes AutoCount's own next number; a
    shared-phone CREATE carries the base MemberNo, whose free 2-9 variant the
    engine picks."""

    rows = list(candidates)
    if phone_digits is None:
        return Decision("CREATE", "no_phone_generator_member_no")
    hits = [r for r in rows if _phone_hit(r, phone_digits)]
    forms = member_no_forms(phone_digits)
    exact = [r for r in hits if member_no_key(r.member_no) in forms]
    if len(exact) > 1:
        # Never guessed, never merged. Round 9: the MemberNo rides along (the
        # alert masks it; both forms end in the same 3 digits).
        return Decision("SKIP", "memberno_conflict", exact[0].member_no)
    if exact:
        staff = exact[0].created_at
        if staff is None or signed_up is None:
            return Decision("SKIP", "created_time_missing", exact[0].member_no)
        if abs(staff - signed_up) <= window:
            return Decision("LINK", "memberno_and_created_time_match", exact[0].member_no)
        return Decision("SKIP", "memberno_exists_outside_window", exact[0].member_no)
    full_name = shopify_full_name(customer.first_name, customer.last_name)
    same = [r for r in hits if full_name_matches(full_name, r.name)]
    if len(same) == 1:
        return Decision("LINK", "phone_and_name_match", same[0].member_no)
    if len(same) > 1:
        # Shared-phone records: never guessed, never merged.
        return Decision("SKIP", "phone_on_multiple_records")
    if not hits:
        return Decision("CREATE", "new_member_phone", phone_digits)
    notes = name_fallback_notes(full_name, [r.name for r in hits])
    member_no = fallback_member_no(phone_digits, customer.first_name, customer.last_name)
    if member_no is None:
        # Owner round 2, Q1: no Latin letters -> phone + first free digit 2-9.
        return Decision("CREATE", "shared_phone_digit_fallback", phone_digits, notes)
    return Decision("CREATE", "shared_phone_name_fallback", member_no, notes)


# --------------------------------------------------------------------------
# ChangeLog insert XML
# --------------------------------------------------------------------------

_XML_FIELDS = {"memberno": "member_no", "name": "name", "mobilephone": "mobile_phone", "emailaddress": "email_address", "dob": "dob"}


def parse_changelog_member(xml_text: Any) -> Ac2Member | None:
    """The member of one ChangeLog insert row (the full row as XML). Columns
    may be child elements or attributes; names compare case-insensitively.
    None when the XML is unusable or has no MemberNo."""

    if not isinstance(xml_text, str) or not xml_text.strip() or len(xml_text) > 65_536 or "<!DOCTYPE" in xml_text.upper():
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    values: dict[str, str] = {}

    def collect(element: ET.Element) -> None:
        for name, value in element.attrib.items():
            key = _XML_FIELDS.get(name.rsplit("}", 1)[-1].lower())
            if key and key not in values:
                values[key] = value
        for child in element:
            key = _XML_FIELDS.get(child.tag.rsplit("}", 1)[-1].lower())
            if key and key not in values and child.text is not None:
                values[key] = child.text
            collect(child)

    collect(root)
    member_no = values.get("member_no")
    if not member_no or not member_no.strip():
        return None
    dob = values.get("dob")
    return Ac2Member(member_no, values.get("name"), values.get("mobile_phone"), values.get("email_address"), dob[:10] if dob else None)


# --------------------------------------------------------------------------
# Sync ledger
# --------------------------------------------------------------------------

LEDGER_SCHEMA = """
CREATE TABLE meta(
  id INTEGER PRIMARY KEY CHECK (id = 1), ledger_id TEXT NOT NULL, autocount_book TEXT NOT NULL,
  store_gid TEXT NOT NULL, shop_domain TEXT NOT NULL, ref_key TEXT NOT NULL CHECK (length(ref_key) = 64), created_at TEXT NOT NULL,
  CHECK (autocount_book <> 'AED_XBOUNDARIES'));
CREATE TABLE links(
  customer_gid TEXT NOT NULL UNIQUE, member_no TEXT NOT NULL, member_no_key TEXT NOT NULL UNIQUE,
  origin TEXT NOT NULL CHECK (origin IN ('created_in_ac2', 'created_in_shopify', 'matched')),
  code TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE writes(
  write_id TEXT PRIMARY KEY, direction TEXT NOT NULL CHECK (direction IN ('shopify_to_ac2', 'ac2_to_shopify')),
  customer_gid TEXT, member_no TEXT NOT NULL, member_no_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('INTENT', 'DONE', 'NOT_WRITTEN', 'REJECTED', 'UNCERTAIN', 'RESOLVED')),
  code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, intent_digest TEXT);
CREATE TABLE changelog_seen(id INTEGER PRIMARY KEY, seen_at TEXT NOT NULL);
CREATE TABLE watermarks(name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE member_snapshots(
  member_no_key TEXT PRIMARY KEY, changelog_id INTEGER NOT NULL, digest TEXT NOT NULL, first_seen_at TEXT NOT NULL);
CREATE TABLE ac2_pending(
  member_no_key TEXT PRIMARY KEY, member_no TEXT NOT NULL, changelog_id INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('PENDING', 'DONE', 'SKIPPED')), code TEXT, checks INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE shopify_pending(
  customer_gid TEXT PRIMARY KEY, state TEXT NOT NULL CHECK (state IN ('PENDING', 'DONE', 'SKIPPED')),
  code TEXT, checks INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, summary TEXT);
CREATE TRIGGER meta_immutable BEFORE UPDATE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER meta_no_delete BEFORE DELETE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER links_immutable BEFORE UPDATE ON links BEGIN SELECT RAISE(ABORT, 'link_immutable'); END;
CREATE TRIGGER links_no_delete BEFORE DELETE ON links BEGIN SELECT RAISE(ABORT, 'link_immutable'); END;
CREATE TRIGGER writes_no_delete BEFORE DELETE ON writes BEGIN SELECT RAISE(ABORT, 'write_immutable'); END;
CREATE TRIGGER writes_binding_immutable BEFORE UPDATE OF write_id, direction, member_no, member_no_key, created_at, intent_digest ON writes
  BEGIN SELECT RAISE(ABORT, 'write_immutable'); END;
CREATE TRIGGER writes_transition BEFORE UPDATE OF state ON writes
  WHEN NOT ((OLD.state = 'INTENT' AND NEW.state IN ('DONE', 'NOT_WRITTEN', 'REJECTED', 'UNCERTAIN'))
         OR (OLD.state = 'UNCERTAIN' AND NEW.state = 'RESOLVED'))
  BEGIN SELECT RAISE(ABORT, 'write_transition_invalid'); END;
CREATE TRIGGER snapshots_no_delete BEFORE DELETE ON member_snapshots BEGIN SELECT RAISE(ABORT, 'snapshot_immutable'); END;
"""
# v1 -> v2, additive only: the nullable intent digest (older rows keep NULL
# and are never linked on resolution), the seen-ID table, and the digest in
# the binding trigger.
_LEDGER_V2_MIGRATION = (
    "ALTER TABLE writes ADD COLUMN intent_digest TEXT",
    "CREATE TABLE changelog_seen(id INTEGER PRIMARY KEY, seen_at TEXT NOT NULL)",
    "DROP TRIGGER writes_binding_immutable",
    "CREATE TRIGGER writes_binding_immutable BEFORE UPDATE OF write_id, direction, member_no, member_no_key, created_at, intent_digest ON writes "
    "BEGIN SELECT RAISE(ABORT, 'write_immutable'); END",
)


def normalised_name(value: Any) -> str:
    """The name's words in order, from the same normalisation as the name rule
    (for the identity digest only). Exact: "Ann Lee" is not "Mary Ann Lee"."""

    return " ".join(_name_tokens(value))


class SyncLedger:
    """Single-owner SQLite ledger beside (not inside) the Core ledger."""

    def __init__(self, state_root: Path, *, clock: Callable[[], datetime] = dl.utc_now, check_custody: bool = True):
        self.root = Path(state_root)
        self.clock = clock
        if check_custody:
            dl.verify_private_state(self.root)
        self._lock = dl._OwnerLock(self.root / "member_sync.lock")
        try:
            self.conn = sqlite3.connect(self.root / LEDGER_FILE, isolation_level=None, timeout=0)
            self.conn.row_factory = sqlite3.Row
            for pragma in ("journal_mode=WAL", "synchronous=FULL", "foreign_keys=ON", "trusted_schema=OFF"):
                self.conn.execute(f"PRAGMA {pragma}")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in dl._split_sql(LEDGER_SCHEMA):
                        self.conn.execute(statement)
                    self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            elif version == 1:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _LEDGER_V2_MIGRATION:
                        self.conn.execute(statement)
                    self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            elif version != SCHEMA_VERSION:
                raise dl.DirectLocalError("sync_ledger_schema_unsupported")
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

    def init_meta(self, *, autocount_book: str, store_gid: str, shop_domain: str) -> dict[str, Any]:
        check_book(autocount_book)
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
            if row is not None:
                if (row["autocount_book"], row["store_gid"], row["shop_domain"]) != (autocount_book, store_gid, shop_domain):
                    raise dl.DirectLocalError("sync_ledger_binding_mismatch")
                return {"status": "existing"}
            conn.execute(
                "INSERT INTO meta(id, ledger_id, autocount_book, store_gid, shop_domain, ref_key, created_at) VALUES (1, ?, ?, ?, ?, ?, ?)",
                (f"sync-{uuid.uuid4().hex}", autocount_book, store_gid, shop_domain, secrets.token_hex(32), dl.ts(self.now())),
            )
        return {"status": "initialised"}

    def meta(self) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        if row is None:
            raise dl.DirectLocalError("sync_ledger_not_initialised")
        return row

    def ref(self, kind: str, value: str) -> str:
        digest = hmac.new(bytes.fromhex(self.meta()["ref_key"]), f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{kind}-{digest[:dl.MEMBER_REF_HEX]}"

    def digest(self, member: Ac2Member) -> str:
        body = json.dumps([member.name, member.mobile_phone, member.email_address, member.dob], ensure_ascii=True)
        return "sha256:" + hmac.new(bytes.fromhex(self.meta()["ref_key"]), body.encode("ascii"), hashlib.sha256).hexdigest()

    def identity_digest(self, name: Any, mobile_phone: Any) -> str:
        """Salted digest (the ledger's ref key) of the normalised name and
        phone of the person a write is for. Only the digest is stored."""

        body = json.dumps(["identity", normalised_name(name), phone_member_no(mobile_phone)[0]], ensure_ascii=True)
        return "sha256:" + hmac.new(bytes.fromhex(self.meta()["ref_key"]), body.encode("ascii"), hashlib.sha256).hexdigest()

    # -- mapping ------------------------------------------------------------
    def link_for_gid(self, gid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM links WHERE customer_gid = ?", (gid,)).fetchone()

    def link_for_member(self, member_no: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM links WHERE member_no_key = ?", (member_no_key(member_no),)).fetchone()

    def open_write_for_member(self, member_no: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM writes WHERE member_no_key = ? AND state IN ('INTENT', 'UNCERTAIN', 'DONE') ORDER BY created_at DESC LIMIT 1",
            (member_no_key(member_no),),
        ).fetchone()

    def open_write_for_customer(self, gid: str) -> sqlite3.Row | None:
        """A Shopify -> AC2 write for this customer that may still land."""

        return self.conn.execute(
            "SELECT * FROM writes WHERE customer_gid = ? AND direction = 'shopify_to_ac2' AND state IN ('INTENT', 'UNCERTAIN') LIMIT 1",
            (gid,),
        ).fetchone()

    def add_link(self, conn: sqlite3.Connection, gid: str, member_no: str, origin: str, code: str) -> None:
        conn.execute(
            "INSERT INTO links(customer_gid, member_no, member_no_key, origin, code, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (gid, member_no, member_no_key(member_no), origin, code, dl.ts(self.now())),
        )

    def watermark(self, name: str) -> str | None:
        row = self.conn.execute("SELECT value FROM watermarks WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else None

    def set_watermark(self, conn: sqlite3.Connection, name: str, value: str) -> None:
        conn.execute(
            "INSERT INTO watermarks(name, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (name, value, dl.ts(self.now())),
        )


# --------------------------------------------------------------------------
# Ports (real clients below; fakes in tests)
# --------------------------------------------------------------------------

class WriteRefused(dl.DirectLocalError):
    """A write switch is off or the run's write budget is spent."""


@dataclass
class RunSummary:
    counts: dict[str, int] = field(default_factory=dict)
    refs: list[dict[str, str]] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    writes: int = 0

    def alert(self, code: str, ref: str | None = None, member_no: str | None = None) -> None:
        """Needs an operator; the run summary status says so."""

        self.count(code, ref, member_no=member_no)
        if code not in self.alerts:
            self.alerts.append(code)

    def count(self, code: str, ref: str | None = None, action: str | None = None, member_no: str | None = None) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1
        if ref is not None:
            entry = {"ref": ref, "action": action or "skip", "code": code}
            if member_no is not None:
                # Round 7: so staff can find the record; last 3 characters only.
                entry["member_no_masked"] = mask_last3(member_no)
            self.refs.append(entry)


class SyncEngine:
    """One bounded run of the phase-1 create sync.

    ``shopify`` provides ``registrations(search, max_pages)``, ``customer(gid)``,
    ``find_by_phone(e164)`` and ``create_customer(input)``. ``ac2`` provides
    ``lookup(phone_digits, member_nos)``, ``changelog_inserts(after_id, limit,
    column)`` and ``create_member(member_no, payload, claim_dir)``.
    """

    def __init__(self, ledger: SyncLedger, config: SyncConfig, *, shopify: Any, ac2: Any):
        self.ledger = ledger
        self.config = config
        self.shopify = shopify
        self.ac2 = ac2
        meta = ledger.meta()
        if (meta["autocount_book"], meta["store_gid"], meta["shop_domain"]) != (check_book(config.autocount_book), config.shopify.store_gid, config.shopify.shop_domain):
            raise dl.DirectLocalError("sync_ledger_binding_mismatch")
        self.summary = RunSummary()
        self._pending_sign_ups: tuple[dict[str, list[ShopifyCustomer]], bool] | None = None  # round 8: per run

    # -- write budget ----------------------------------------------------------
    def _spend_write(self, switch: bool, disabled_code: str) -> None:
        if not switch:
            raise WriteRefused(disabled_code)
        if self.summary.writes >= self.config.max_writes_per_run:
            raise WriteRefused("max_writes_per_run_reached")
        self.summary.writes += 1

    def _intent(self, direction: str, member_no: str, gid: str | None, identity_digest: str | None = None) -> str:
        write_id = f"write-{uuid.uuid4().hex}"
        now = dl.ts(self.ledger.now())
        with self.ledger.tx() as conn:
            conn.execute(
                "INSERT INTO writes(write_id, direction, customer_gid, member_no, member_no_key, state, created_at, updated_at, intent_digest) "
                "VALUES (?, ?, ?, ?, ?, 'INTENT', ?, ?, ?)",
                (write_id, direction, gid, member_no, member_no_key(member_no), now, now, identity_digest),
            )
        return write_id

    def _close_write(self, conn: sqlite3.Connection, write_id: str, state: str, code: str | None, gid: str | None = None) -> None:
        conn.execute(
            "UPDATE writes SET state = ?, code = ?, customer_gid = COALESCE(?, customer_gid), updated_at = ? WHERE write_id = ?",
            (state, code, gid, dl.ts(self.ledger.now()), write_id),
        )

    # -- run -------------------------------------------------------------------
    def run(self, *, direction: str = "both") -> dict[str, Any]:
        if direction not in ("both", "shopify_to_ac2", "ac2_to_shopify"):
            raise dl.DirectLocalError("sync_direction_invalid")
        run_id = f"run-{uuid.uuid4().hex}"
        started = self.ledger.now()
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO runs(run_id, started_at) VALUES (?, ?)", (run_id, dl.ts(started)))
        self._recover_crashed_intents()
        if direction in ("both", "shopify_to_ac2"):
            self._resolve_uncertain("shopify_to_ac2")
            self.shopify_to_ac2(started)
        if direction in ("both", "ac2_to_shopify"):
            self._resolve_uncertain("ac2_to_shopify")
            self.ac2_to_shopify()
        result = {
            "schema": SUMMARY_SCHEMA, "status": "completed_with_alerts" if self.summary.alerts else "completed", "direction": direction,
            "alerts": list(self.summary.alerts),
            "autocount_book": self.config.autocount_book, "writes": self.summary.writes,
            "ac2_create_enabled": self.config.ac2_create_enabled, "shopify_create_enabled": self.config.shopify_create_enabled,
            "counts": dict(sorted(self.summary.counts.items())), "refs": self.summary.refs,
        }
        with self.ledger.tx() as conn:
            conn.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE run_id = ?", (dl.ts(self.ledger.now()), json.dumps(result["counts"], sort_keys=True), run_id))
        return result

    def initial_link(self, *, apply: bool) -> dict[str, Any]:
        """Round 7c (Owner): link the Shopify customers made before the
        cutover (Customer.createdAt < shopify_cutover_utc, any tag) to the AC2
        member whose MemberNo is exactly their phone (``member_no_forms``),
        with no time or name rule. A dry run (the default) writes no link;
        ``apply`` needs ``initial_link_enabled``. Only ledger links are
        written: no AC2 or Shopify write, no field change. A conflict (both
        number forms in AC2, or the member already linked elsewhere) alerts
        and is never merged. Resumable: a cursor on updatedAt per mode; an
        applied pass that reaches the end opens Shopify -> AC2."""

        if apply and not self.config.initial_link_enabled:
            raise dl.DirectLocalError("initial_link_disabled")
        cursor = INITIAL_LINK_CURSORS[apply]
        floor = self.ledger.watermark(cursor)
        cutover = dl.ts(self.config.shopify_cutover_utc)
        search = f"created_at:<'{cutover}'" + (f" AND updated_at:>='{floor}'" if floor else "")
        customers, complete = self.shopify.registrations(search, self.config.shopify_max_pages, self.config.birthday_month_metafield)
        historical = []
        for customer in customers:
            if customer.created_at is None or customer.created_at >= self.config.shopify_cutover_utc:
                continue  # defensive: the search already limits this
            if self.ledger.link_for_gid(customer.gid) is not None:
                self.summary.count("initial_link_already_linked")
                continue
            digits = phone_member_no(customer.phone)[0]
            if digits is None:
                self.summary.count("initial_link_no_phone")
                continue
            historical.append((customer, member_no_forms(digits)))
        claimed: set[str] = set()
        for start in range(0, len(historical), INITIAL_LINK_BATCH):
            batch = historical[start:start + INITIAL_LINK_BATCH]
            members = self.ac2.lookup(None, sorted({form for _, forms in batch for form in forms}))
            for customer, forms in batch:
                self._initial_link_one(customer, [m for m in members if member_no_key(m.member_no) in forms], claimed, apply)
        if customers and not complete:
            last = dl.ts(max(c.updated_at for c in customers if c.updated_at is not None))
            if floor is not None and last <= floor:
                # More customers share one updatedAt than one run reads.
                self.summary.alert("initial_link_cursor_stuck")
            else:
                with self.ledger.tx() as conn:
                    self.ledger.set_watermark(conn, cursor, last)
        if complete:
            with self.ledger.tx() as conn:
                # A finished pass: the next one (dry run or a re-check) starts
                # from the beginning again; an applied one opens Shopify -> AC2.
                conn.execute("DELETE FROM watermarks WHERE name = ?", (cursor,))
                if apply:
                    self.ledger.set_watermark(conn, INITIAL_LINK_DONE_MARK, dl.ts(self.ledger.now()))
        return {
            "schema": SUMMARY_SCHEMA, "status": "completed_with_alerts" if self.summary.alerts else "completed", "direction": "initial_link",
            "mode": "apply" if apply else "dry_run", "complete": complete, "alerts": list(self.summary.alerts),
            "autocount_book": self.config.autocount_book, "writes": self.summary.writes,
            "counts": dict(sorted(self.summary.counts.items())), "refs": self.summary.refs,
        }

    def _initial_link_one(self, customer: ShopifyCustomer, found: list[Ac2Member], claimed: set[str], apply: bool) -> None:
        ref = self.ledger.ref("c", customer.gid)
        if not found:
            self.summary.count("initial_link_no_member")
            return
        member_no = found[0].member_no
        key = member_no_key(member_no)
        if len(found) > 1 or key in claimed or self.ledger.link_for_member(member_no) is not None:
            self.summary.alert("initial_link_member_no_conflict", ref, member_no=member_no)
            return
        if self.ledger.open_write_for_member(member_no) is not None or self.ledger.open_write_for_customer(customer.gid) is not None:
            self.summary.count("initial_link_held_by_open_write", ref, member_no=member_no)
            return
        claimed.add(key)
        if not apply:
            self.summary.count("initial_link_would_link", ref, "would_link", member_no=member_no)
            return
        with self.ledger.tx() as conn:
            self.ledger.add_link(conn, customer.gid, member_no, "matched", "initial_link_member_no")
        self.summary.count("initial_link_linked", ref, "link", member_no=member_no)

    def _recover_crashed_intents(self) -> None:
        """An INTENT left by a crashed run may or may not have been written:
        it becomes UNCERTAIN and is only ever looked up, never re-sent. Its
        customer is held (G2 H2): the write-once child of a killed parent may
        still save, so the customer is never registered again."""

        held = []
        with self.ledger.tx() as conn:
            for row in conn.execute("SELECT write_id, direction, customer_gid FROM writes WHERE state = 'INTENT'").fetchall():
                self._close_write(conn, row["write_id"], "UNCERTAIN", "crashed_intent")
                self.summary.count("crashed_intent_uncertain")
                if row["direction"] == "shopify_to_ac2" and row["customer_gid"]:
                    self._set_shopify_pending(conn, row["customer_gid"], "SKIPPED", "customer_held_by_open_write")
                    held.append(row["customer_gid"])
        for gid in held:
            self.summary.alert("customer_held_by_open_write", self.ledger.ref("c", gid))

    def _resolve_uncertain(self, direction: str) -> None:
        rows = self.ledger.conn.execute("SELECT * FROM writes WHERE state = 'UNCERTAIN' AND direction = ?", (direction,)).fetchall()
        for row in rows:
            if self.ledger.link_for_member(row["member_no"]) is not None:
                with self.ledger.tx() as conn:
                    self._close_write(conn, row["write_id"], "RESOLVED", "already_linked")
                continue
            if direction == "shopify_to_ac2":
                found = [m for m in self.ac2.lookup(None, [row["member_no"]]) if member_no_key(m.member_no) == row["member_no_key"]]
                gid = row["customer_gid"]
                ref = self.ledger.ref("m", row["member_no_key"])
                if len(found) == 1 and row["intent_digest"] is None:
                    # G2 B2: a row without the intended identity is never linked.
                    self.summary.count("uncertain_write_identity_unverifiable", ref)
                elif len(found) == 1 and not hmac.compare_digest(self.ledger.identity_digest(found[0].name, found[0].mobile_phone), row["intent_digest"]):
                    # Someone else holds the number: never linked; stays UNCERTAIN.
                    self.summary.count("uncertain_write_identity_mismatch", ref)
                elif len(found) == 1 and gid and self.ledger.link_for_gid(gid) is None:
                    with self.ledger.tx() as conn:
                        self.ledger.add_link(conn, gid, found[0].member_no, "created_in_ac2", "uncertain_write_found")
                        self._close_write(conn, row["write_id"], "RESOLVED", "uncertain_write_found")
                    self.summary.count("uncertain_write_resolved_linked", self.ledger.ref("m", row["member_no_key"]), "link")
                else:
                    self.summary.count("uncertain_write_unresolved", self.ledger.ref("m", row["member_no_key"]))
            else:
                members = [m for m in self.ac2.lookup(None, [row["member_no"]]) if member_no_key(m.member_no) == row["member_no_key"]]
                digits = _ac2_phone(members[0]) if len(members) == 1 else None
                customers = self.shopify.find_by_phone(e164(digits)) if digits else []
                if (len(members) == 1 and len(customers) == 1 and self.ledger.link_for_gid(customers[0].gid) is None
                        and full_name_matches(shopify_full_name(customers[0].first_name, customers[0].last_name), members[0].name)):
                    with self.ledger.tx() as conn:
                        self.ledger.add_link(conn, customers[0].gid, members[0].member_no, "created_in_shopify", "uncertain_write_found")
                        self._close_write(conn, row["write_id"], "RESOLVED", "uncertain_write_found", customers[0].gid)
                        conn.execute("UPDATE ac2_pending SET state = 'DONE', code = 'uncertain_write_found', updated_at = ? WHERE member_no_key = ?", (dl.ts(self.ledger.now()), row["member_no_key"]))
                    self.summary.count("uncertain_write_resolved_linked", self.ledger.ref("m", row["member_no_key"]), "link")
                else:
                    self.summary.count("uncertain_write_unresolved", self.ledger.ref("m", row["member_no_key"]))

    # -- Shopify -> AC2 ----------------------------------------------------------
    def shopify_to_ac2(self, started: datetime) -> None:
        if self.ledger.watermark(INITIAL_LINK_DONE_MARK) is None:
            # Round 7c (Owner): existing customers are linked first. Until an
            # applied initial link has completed, no sign-up is read and no
            # link or create is made (the start-up housekeeping above still
            # runs; round 8, G2 L4).
            self.summary.alert("initial_link_not_complete")
            return
        # Round 8 (G2 M1): pending sign-ups are re-checked under their own
        # bound, oldest first (each re-check moves it to the back), so the
        # budget below is only for new sign-ups and they are always read.
        limit = self.config.max_pending_rechecks_per_run
        pending = self.ledger.conn.execute("SELECT customer_gid FROM shopify_pending WHERE state = 'PENDING' ORDER BY updated_at LIMIT ?", (limit + 1,)).fetchall()
        if len(pending) > limit:
            self.summary.alert("shopify_pending_backlog")
            pending = pending[:limit]
        budget = self.config.max_shopify_customers_per_run
        for row in pending:
            customer = self.shopify.customer(row["customer_gid"], self.config.birthday_month_metafield)
            if customer is None:
                self._shopify_pending(row["customer_gid"], "SKIPPED", "customer_not_found")
                self.summary.count("shopify_customer_not_found", self.ledger.ref("c", row["customer_gid"]))
                continue
            self._register(customer)
        since = self.ledger.watermark("shopify_updated_at") or dl.ts(self.config.shopify_cutover_utc)
        search = f"tag:'{REGISTRATION_TAG}' AND updated_at:>='{since}'"
        customers, complete = self.shopify.registrations(search, self.config.shopify_max_pages, self.config.birthday_month_metafield)
        for customer in customers:
            if budget <= 0:
                complete = False
                self.summary.count("shopify_customer_budget_reached")
                break
            if self.ledger.link_for_gid(customer.gid) is not None:
                self.summary.count("shopify_already_linked")
                continue
            pending_row = self.ledger.conn.execute("SELECT state FROM shopify_pending WHERE customer_gid = ?", (customer.gid,)).fetchone()
            if pending_row is not None:
                continue  # handled through the pending list
            budget -= 1
            self._register(customer)
        if complete:
            mark = started - timedelta(seconds=SHOPIFY_WATERMARK_MARGIN_SECONDS)
            with self.ledger.tx() as conn:
                self.ledger.set_watermark(conn, "shopify_updated_at", dl.ts(max(mark, self.config.shopify_cutover_utc)))
        self._watch_sync_created_members()

    def _watch_sync_created_members(self) -> None:
        """Round 6: a member staff make by hand AFTER the sync created one for
        the same person (same phone, name rule) is a possible duplicate:
        alert and count only, never merged, linked or written. Read-only, for
        members this sync created in the last DUPLICATE_WATCH_DAYS days. A
        member created without a phone cannot be checked."""

        since = dl.ts(self.ledger.now() - timedelta(days=DUPLICATE_WATCH_DAYS))
        rows = self.ledger.conn.execute(
            "SELECT member_no, member_no_key FROM links WHERE origin = 'created_in_ac2' AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (since, DUPLICATE_WATCH_PER_RUN),
        ).fetchall()
        for row in rows:
            ours = [m for m in self.ac2.lookup(None, [row["member_no"]]) if member_no_key(m.member_no) == row["member_no_key"]]
            digits = phone_member_no(ours[0].mobile_phone)[0] if len(ours) == 1 else None
            if digits is None:
                continue
            others = [
                m for m in self.ac2.lookup(digits, [])
                if member_no_key(m.member_no) != row["member_no_key"] and _phone_hit(m, digits)
                and full_name_matches(ours[0].name, m.name) and self.ledger.link_for_member(m.member_no) is None
            ]
            if others:
                self.summary.alert("possible_duplicate_member", self.ledger.ref("m", row["member_no_key"]))

    def _shopify_pending(self, gid: str, state: str, code: str, *, count_check: bool = False) -> None:
        with self.ledger.tx() as conn:
            self._set_shopify_pending(conn, gid, state, code, count_check=count_check)

    def _set_shopify_pending(self, conn: sqlite3.Connection, gid: str, state: str, code: str, *, count_check: bool = False) -> None:
        conn.execute(
            "INSERT INTO shopify_pending(customer_gid, state, code, checks, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(customer_gid) DO UPDATE SET state = excluded.state, code = excluded.code, checks = shopify_pending.checks + ?, updated_at = excluded.updated_at",
            (gid, state, code, 1 if count_check else 0, dl.ts(self.ledger.now()), 1 if count_check else 0),
        )

    def _register(self, customer: ShopifyCustomer) -> None:
        gid = customer.gid
        ref = self.ledger.ref("c", gid)
        if self.ledger.link_for_gid(gid) is not None:
            self._shopify_pending(gid, "DONE", "already_linked")
            self.summary.count("shopify_already_linked")
            return
        if REGISTRATION_TAG not in customer.tags:
            self._shopify_pending(gid, "SKIPPED", "registration_tag_absent")
            self.summary.count("shopify_registration_tag_absent", ref)
            return
        instant = customer.registration_at
        if instant is None:
            row = self.ledger.conn.execute("SELECT checks FROM shopify_pending WHERE customer_gid = ?", (gid,)).fetchone()
            if row is None or row["checks"] + 1 < REGISTRATION_WAIT_MAX_CHECKS:
                self._shopify_pending(gid, "PENDING", "registration_event_pending", count_check=True)
                self.summary.count("shopify_registration_event_pending", ref)
                return
            instant = customer.created_at  # bounded wait spent: Customer.createdAt decides
        if instant is None or instant < self.config.shopify_cutover_utc:
            self._shopify_pending(gid, "SKIPPED", "registered_before_cutover")
            self.summary.count("shopify_registered_before_cutover", ref)
            return
        digits, phone_status = phone_member_no(customer.phone)
        if phone_status == "invalid":
            self.summary.count("shopify_phone_invalid_treated_as_absent", ref)
        candidates = self.ac2.lookup(digits, []) if digits is not None else []
        decision = decide_shopify_to_ac2(customer, digits, candidates, timedelta(minutes=self.config.memberno_link_window_minutes), signed_up=instant)
        if decision.action == "SKIP":
            self._shopify_pending(gid, "SKIPPED", decision.code)
            if decision.code in ("memberno_exists_outside_window", "created_time_missing", "memberno_conflict"):
                # Round 7: the phone MemberNo is in AC2 but cannot be linked
                # by time; never created again. An operator decides.
                self.summary.alert(decision.code, ref, member_no=decision.member_no)
            else:
                self.summary.count(decision.code, ref)
            return
        if decision.action == "CREATE" and self.ledger.now() < deferred_create_cutoff(instant, self.config.deferred_create_local_time):
            # Round 6 (Owner): staff create members by hand from the sign-up
            # sheet. Until the next morning's cutoff the sync only looks for
            # their member (each run re-reads this pending customer) and
            # writes nothing.
            self._shopify_pending(gid, "PENDING", "waiting_for_staff")
            self.summary.count("waiting_for_staff", ref)
            return
        member_no = decision.member_no
        if member_no is None:
            self._create_in_ac2(customer, digits, decision, ref)  # GetNextMemberNo at write time
            return
        if decision.code in ("shared_phone_name_fallback", "shared_phone_digit_fallback"):
            member_no = self._free_suffix(member_no, include_base=decision.code == "shared_phone_name_fallback")
            if member_no is None:
                self._shopify_pending(gid, "SKIPPED", "shared_phone_member_no_exhausted")
                self.summary.count("shared_phone_member_no_exhausted", ref)
                return
            if member_no != decision.member_no and decision.code == "shared_phone_name_fallback":
                self.summary.count("shared_phone_suffix_digit")
            decision = replace(decision, member_no=member_no)
        member_ref = self.ledger.ref("m", member_no_key(member_no))
        linked = self.ledger.link_for_member(member_no)
        if linked is not None:
            # The AC2 member is already mapped to another Shopify customer
            # (round 8, G2 H1: an alert, never silent).
            self._shopify_pending(gid, "SKIPPED", "member_linked_to_other_customer")
            self.summary.alert("member_linked_to_other_customer", ref, member_no=member_no)
            return
        if decision.action == "LINK":
            with self.ledger.tx() as conn:
                self.ledger.add_link(conn, gid, member_no, "matched", decision.code)
            self._shopify_pending(gid, "DONE", decision.code)
            self.summary.count(decision.code, member_ref, "link")
            return
        if self.ledger.open_write_for_member(member_no) is not None:
            self._shopify_pending(gid, "SKIPPED", "member_no_held_by_open_write")
            self.summary.count("member_no_held_by_open_write", ref)
            return
        self._create_in_ac2(customer, digits, decision, member_ref)

    def _member_no_taken(self, numbers: list[str]) -> set[str]:
        """Keys of the numbers held in AC2 (trimmed, case-insensitive) or by
        this ledger (a link or an open write)."""

        taken = {member_no_key(m.member_no) for m in self.ac2.lookup(None, numbers)}
        for number in numbers:
            if self.ledger.link_for_member(number) is not None or self.ledger.open_write_for_member(number) is not None:
                taken.add(member_no_key(number))
        return taken

    def _free_suffix(self, base: str, *, include_base: bool = True) -> str | None:
        candidates = suffix_candidates(base) if include_base else suffix_candidates(base)[1:]
        taken = self._member_no_taken(candidates)
        return next((c for c in candidates if member_no_key(c) not in taken), None)

    def _allocate_generator(self) -> tuple[str | None, str]:
        """AutoCount's own number (``GetNextMemberNo``, a preview that saving
        does not advance, observed 16 Oct 2026). A taken preview continues in
        the generator's format (``member_no_successor``), at most three
        candidates, each checked against AC2 and this ledger."""

        try:
            candidate = self.ac2.next_member_no()
        except dl.DirectLocalError:
            return None, "member_no_generator_failed"
        # Numbers this ledger already used: the preview does not advance after
        # a save, so continue above them instead of spending candidates.
        tried: set[str] = {row[0] for row in self.ledger.conn.execute("SELECT member_no FROM links UNION SELECT member_no FROM writes")}
        for _ in range(dl.MAX_MEMBER_NO_CANDIDATES):
            if not isinstance(candidate, str) or not dl.MEMBER_NO_RE.fullmatch(candidate):
                return None, "member_no_generator_invalid"
            if member_no_key(candidate) not in self._member_no_taken([candidate]):
                return candidate, "no_phone_generator_member_no"
            tried.add(candidate)
            candidate = dl.member_no_successor(candidate, tried)
            if candidate is None:
                return None, "member_no_generator_not_advancing"
        return None, "member_no_candidates_exhausted"

    def _create_in_ac2(self, customer: ShopifyCustomer, digits: str | None, decision: Decision, member_ref: str) -> None:
        gid = customer.gid
        member_no = decision.member_no
        if self.ledger.open_write_for_customer(gid) is not None:
            # G2 H2: an earlier write for this customer may still land (a
            # killed run's child); another create would be a second member.
            # Never re-sent blindly, like any unclear result.
            self._shopify_pending(gid, "SKIPPED", "customer_held_by_open_write")
            self.summary.alert("customer_held_by_open_write", member_ref)
            return
        name = " ".join(part.strip() for part in (customer.first_name, customer.last_name) if isinstance(part, str) and part.strip())
        if not name:
            self._shopify_pending(gid, "SKIPPED", "name_missing")
            self.summary.count("shopify_name_missing", member_ref)
            return
        month = birthday_month(customer.birthday_value)
        if customer.birthday_value not in (None, "") and month is None:
            self.summary.count("birthday_month_unparsed", member_ref)
        registered = customer.registration_value if isinstance(customer.registration_value, str) and DATE_RE.fullmatch(customer.registration_value) else None
        if registered is None:
            instant = customer.registration_at or customer.created_at
            registered = instant.astimezone(SGT).strftime("%Y-%m-%d") if instant else self.ledger.now().astimezone(SGT).strftime("%Y-%m-%d")
        payload = {
            "profile": "sync_p1", "member_type": self.config.ac2_create_member_type, "name": name, "mobile_phone": digits, "email_address": customer.email or None,
            "register_date": registered, "dob": dob_from_month(month),
        }
        issue = dl.autocount_text_issue(payload)
        if issue or (member_no is not None and not dl.MEMBER_NO_RE.fullmatch(member_no)):
            self._shopify_pending(gid, "SKIPPED", issue or "member_no_invalid")
            self.summary.count(issue or "member_no_invalid", member_ref)
            return
        try:
            self._spend_write(self.config.ac2_create_enabled, "ac2_create_disabled")
        except WriteRefused as refused:
            self._shopify_pending(gid, "PENDING", refused.code)
            self.summary.count(refused.code, member_ref, "would_create")
            if refused.code == "ac2_create_disabled":
                # Dry run (round 5): count the MemberNo rule this create would
                # use, as a real create counts it; nothing else changes.
                self._count_decision(decision)
                # Round 8 (G2 M1): past the cutoff with creates off it stays
                # pending for good: an operator must know.
                self.summary.alert("sign_up_past_cutoff_create_disabled", self.ledger.ref("c", gid), member_no=member_no)
            return
        if member_no is None:
            # Allocated only when the write will really start.
            member_no, code = self._allocate_generator()
            if member_no is None:
                self.summary.writes -= 1  # nothing was written
                self._shopify_pending(gid, "PENDING", code)
                self.summary.count(code, member_ref)
                return
            member_ref = self.ledger.ref("m", member_no_key(member_no))
            if self.ledger.link_for_member(member_no) is not None or self.ledger.open_write_for_member(member_no) is not None:
                self.summary.writes -= 1
                self._shopify_pending(gid, "PENDING", "member_no_held_by_open_write")
                self.summary.count("member_no_held_by_open_write", member_ref)
                return
        write_id = self._intent("shopify_to_ac2", member_no, gid, self.ledger.identity_digest(name, digits))
        state, code = self.ac2.create_member(member_no, payload, write_id=write_id)
        with self.ledger.tx() as conn:
            if state == "CREATED_VERIFIED":
                self._close_write(conn, write_id, "DONE", "created")
                self.ledger.add_link(conn, gid, member_no, "created_in_ac2", decision.code)
            elif state == "NOT_WRITTEN_PROVEN":
                self._close_write(conn, write_id, "NOT_WRITTEN", code)
            else:
                self._close_write(conn, write_id, "UNCERTAIN", code or state.lower())
        if state == "CREATED_VERIFIED":
            self._shopify_pending(gid, "DONE", decision.code)
            self.summary.count("ac2_member_created", member_ref, "create")
            # Round 6: every sync create comes after the staff wait; the
            # operator checks staff did not make the member too.
            self.summary.alert("created_after_wait")
            self._count_decision(decision)
        elif state == "NOT_WRITTEN_PROVEN":
            self._shopify_pending(gid, "PENDING", "ac2_not_written")
            self.summary.count("ac2_create_not_written", member_ref)
        else:
            self._shopify_pending(gid, "SKIPPED", "ac2_write_uncertain")
            self.summary.count("ac2_create_uncertain", member_ref)

    def _count_decision(self, decision: Decision) -> None:
        self.summary.count(decision.code)
        for note in decision.notes:
            self.summary.count(note)

    # -- AC2 -> Shopify ----------------------------------------------------------
    def ac2_to_shopify(self) -> None:
        column = self.config.changelog_xml_column
        if column is None:
            self.summary.count("changelog_column_unconfigured")
            return
        overlap = self.config.changelog_overlap
        watermark = self.ledger.watermark("ac2_changelog_id")
        if watermark is None:
            self._initialise_changelog(column, overlap)
            return
        current = int(watermark)
        self._baseline_overlap(column, current, overlap)
        after = max(current - overlap, 0) if overlap else current
        rows, max_id = self.ac2.changelog_inserts(after, self.config.changelog_batch + overlap, column)
        if max_id < current:
            # ChangeLog numbering went backwards (reseed or restore): refuse,
            # never reset the watermark automatically.
            self.summary.alert("changelog_id_reset_detected")
            return
        for row_id, xml_text in sorted(rows, key=lambda item: item[0]):
            if self.ledger.conn.execute("SELECT 1 FROM changelog_seen WHERE id = ?", (row_id,)).fetchone() is not None:
                if row_id > current:
                    self.summary.count("changelog_row_replayed")  # a replay is a no-op
                continue  # an overlap re-read of a processed row: nothing to do
            member = parse_changelog_member(xml_text)
            if member is None:
                # Stop here: the watermark stays at the last good ID, so this
                # row and everything after it are read again next run.
                self.summary.alert("changelog_row_unparsed")
                break
            with self.ledger.tx() as conn:
                key = member_no_key(member.member_no)
                conn.execute(
                    "INSERT OR IGNORE INTO member_snapshots(member_no_key, changelog_id, digest, first_seen_at) VALUES (?, ?, ?, ?)",
                    (key, row_id, self.ledger.digest(member), dl.ts(self.ledger.now())),
                )
                if self.ledger.link_for_member(member.member_no) is not None or self.ledger.open_write_for_member(member.member_no) is not None:
                    self.summary.count("ac2_insert_linked_or_own_write")  # loop prevention
                else:
                    inserted = conn.execute(
                        "INSERT OR IGNORE INTO ac2_pending(member_no_key, member_no, changelog_id, state, updated_at) VALUES (?, ?, ?, 'PENDING', ?)",
                        (key, member.member_no, row_id, dl.ts(self.ledger.now())),
                    ).rowcount
                    self.summary.count("ac2_insert_queued" if inserted else "ac2_insert_already_seen")
                if row_id <= current:
                    self.summary.count("changelog_late_row_processed")
                conn.execute("INSERT INTO changelog_seen(id, seen_at) VALUES (?, ?)", (row_id, dl.ts(self.ledger.now())))
                if row_id > current:
                    self.ledger.set_watermark(conn, "ac2_changelog_id", str(row_id))
                    current = row_id
        with self.ledger.tx() as conn:
            conn.execute("DELETE FROM changelog_seen WHERE id <= ?", (current - overlap,))  # never re-read again
        self._process_ac2_pending()

    def _initialise_changelog(self, column: str, overlap: int) -> None:
        """First run: start from the current highest ID (history is the
        phase-2 backfill). Rows already visible inside the overlap window
        are history too, so they are marked seen; an insert that commits
        later below the watermark is then still processed."""

        _, max_id = self.ac2.changelog_inserts(None, 1, column)
        visible: list = []
        if overlap and max_id > 0:
            visible, _ = self.ac2.changelog_inserts(max(max_id - overlap, 0), overlap, column)
        with self.ledger.tx() as conn:
            for row_id, _xml in visible:
                if row_id <= max_id:
                    conn.execute("INSERT OR IGNORE INTO changelog_seen(id, seen_at) VALUES (?, ?)", (row_id, dl.ts(self.ledger.now())))
            self.ledger.set_watermark(conn, "ac2_changelog_id", str(int(max_id)))
            self.ledger.set_watermark(conn, OVERLAP_MARK, str(overlap))
        self.summary.count("changelog_watermark_initialised")

    def _baseline_overlap(self, column: str, current: int, overlap: int) -> None:
        """G2 H3 (Option A): the seen IDs cover the overlap window only when
        the last run used at least this overlap. A ledger without the stored
        overlap (upgraded from v1, or written before this rule) or a larger
        overlap than stored would re-read rows that were never recorded or
        were already pruned, and queue history as new. Those rows are marked
        seen without processing, as on the first run (the same accepted risk:
        an insert committing late inside the window on that one run is
        treated as history). A numbering reset is left to the main read."""

        stored = self.ledger.watermark(OVERLAP_MARK)
        if stored is not None and int(stored) >= overlap:
            if int(stored) != overlap:
                with self.ledger.tx() as conn:  # smaller now: pruning follows it
                    self.ledger.set_watermark(conn, OVERLAP_MARK, str(overlap))
            return
        rows: list = []
        if overlap and current > 0:
            rows, max_id = self.ac2.changelog_inserts(max(current - overlap, 0), overlap, column)
            if max_id < current:
                return
        marked = 0
        with self.ledger.tx() as conn:
            for row_id, _xml in rows:
                if row_id <= current:
                    marked += conn.execute("INSERT OR IGNORE INTO changelog_seen(id, seen_at) VALUES (?, ?)", (row_id, dl.ts(self.ledger.now()))).rowcount
            self.ledger.set_watermark(conn, OVERLAP_MARK, str(overlap))
        if overlap:
            self.summary.alert("changelog_overlap_baselined")
            for _ in range(marked):
                self.summary.count("changelog_overlap_baselined_rows")

    def _ac2_done(self, key: str, state: str, code: str, *, count_check: bool = False) -> None:
        with self.ledger.tx() as conn:
            conn.execute(
                "UPDATE ac2_pending SET state = ?, code = ?, checks = checks + ?, updated_at = ? WHERE member_no_key = ?",
                (state, code, 1 if count_check else 0, dl.ts(self.ledger.now()), key),
            )

    def _requeue_stuck_holds(self) -> None:
        """Round 10 (G2 L8): ``ac2_member_hold_stuck`` only means the pending
        sign-ups could not all be read. Once they can (no more PENDING than
        ``max_pending_rechecks_per_run``, the ``sure`` rule), every stuck
        member is PENDING again with ``checks`` 0 and is decided like any
        other: linked, or created in Shopify for a genuine walk-in. A later
        backlog can hold it again for the same bounded wait (one more stuck
        alert)."""

        limit = self.config.max_pending_rechecks_per_run
        backlog = self.ledger.conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM shopify_pending WHERE state = 'PENDING' LIMIT ?)", (limit + 1,)).fetchone()[0]
        if backlog > limit:
            return
        keys = [row["member_no_key"] for row in self.ledger.conn.execute("SELECT member_no_key FROM ac2_pending WHERE state = 'SKIPPED' AND code = 'ac2_member_hold_stuck'")]
        for key in keys:
            with self.ledger.tx() as conn:
                conn.execute(
                    "UPDATE ac2_pending SET state = 'PENDING', code = 'hold_requeued', checks = 0, updated_at = ? WHERE member_no_key = ?",
                    (dl.ts(self.ledger.now()), key),
                )
            self.summary.count("ac2_member_hold_requeued", self.ledger.ref("m", key))

    def _process_ac2_pending(self) -> None:
        self._requeue_stuck_holds()
        pending = self.ledger.conn.execute("SELECT * FROM ac2_pending WHERE state = 'PENDING' ORDER BY changelog_id LIMIT ?", (PENDING_LOOKUP_BATCH,)).fetchall()
        if not pending:
            return
        found = {member_no_key(m.member_no): m for m in self.ac2.lookup(None, [row["member_no"] for row in pending])}
        for row in pending:
            key = row["member_no_key"]
            ref = self.ledger.ref("m", key)
            if self.ledger.link_for_member(row["member_no"]) is not None or self.ledger.open_write_for_member(row["member_no"]) is not None:
                self._ac2_done(key, "DONE", "already_linked")
                self.summary.count("ac2_already_linked")
                continue
            member = found.get(key)
            if member is None:
                self._ac2_done(key, "SKIPPED", "ac2_member_not_found")  # deleted or renumbered since the insert
                self.summary.count("ac2_member_not_found", ref)
                continue
            digits = _ac2_phone(member)
            if digits is None:
                self._ac2_done(key, "SKIPPED", "ac2_member_without_phone")
                self.summary.count("ac2_member_without_phone", ref)
                continue
            forms = member_no_forms(digits)
            phone_form = member_no_key(member.member_no) in forms
            if phone_form and len({member_no_key(r.member_no) for r in self.ac2.lookup(digits, []) if _phone_hit(r, digits) and member_no_key(r.member_no) in forms}) > 1:
                # Round 9 (G2 M2): both forms of the phone are MemberNos: the
                # Shopify -> AC2 conflict rule. Never linked, never created.
                self._ac2_done(key, "SKIPPED", "memberno_conflict")
                self.summary.alert("memberno_conflict", ref, member_no=member.member_no)
                continue
            customers = self.shopify.find_by_phone(e164(digits))
            if phone_form and not customers:
                # Round 8 (G2 H1): a sign-up the phone search does not show
                # yet is still a sign-up: look in the pending list too.
                customers, sure = self._pending_sign_ups_with_phone(digits)
                if not customers and not sure:
                    if row["checks"] + 1 < REGISTRATION_WAIT_MAX_CHECKS:
                        self._ac2_done(key, "PENDING", "held_for_sign_up", count_check=True)
                        self.summary.alert("ac2_member_held_for_sign_up", ref, member_no=member.member_no)
                    else:
                        # Round 9 (G2 L6): the same bounded wait as the sign-up
                        # time hold; never created while stuck. Round 10 (G2
                        # L8): re-queued once the backlog clears.
                        self._ac2_done(key, "SKIPPED", "ac2_member_hold_stuck", count_check=True)
                        self.summary.alert("ac2_member_hold_stuck", ref, member_no=member.member_no)
                    continue
            if len(customers) > 1:
                self._ac2_done(key, "SKIPPED", "shopify_phone_on_multiple_customers")
                self.summary.count("shopify_phone_on_multiple_customers", ref)
                continue
            if len(customers) == 1 and phone_form:
                self._ac2_member_by_time(row, member, customers[0], key, ref)
                continue
            if len(customers) == 1:
                customer = customers[0]
                if not full_name_matches(shopify_full_name(customer.first_name, customer.last_name), member.name):
                    # Owner (2026-10-21): another name (Q1 word rule) is
                    # another person. Shopify phones are unique, so the
                    # separate customer is created without the phone; never linked.
                    self.summary.count("shopify_phone_owned_by_other_name")
                    for note in name_fallback_notes(member.name, [shopify_full_name(customer.first_name, customer.last_name)]):
                        self.summary.count(note)
                    self._create_in_shopify(member, None, key, ref)
                elif (link := self.ledger.link_for_gid(customer.gid)) is not None:
                    if link["origin"] == "created_in_ac2":
                        # Round 6: staff made a member for a person the sync
                        # already created: possible duplicate, never merged.
                        self._ac2_done(key, "SKIPPED", "possible_duplicate_member")
                        self.summary.alert("possible_duplicate_member", ref)
                    else:
                        self._ac2_done(key, "SKIPPED", "shopify_customer_linked_to_other_member")
                        self.summary.alert("shopify_customer_linked_to_other_member", ref, member_no=member.member_no)
                else:
                    with self.ledger.tx() as conn:
                        self.ledger.add_link(conn, customer.gid, member.member_no, "matched", "shopify_phone_and_name_match")
                    self._ac2_done(key, "DONE", "linked_existing_shopify_customer")
                    self.summary.count("linked_existing_shopify_customer", ref, "link")
                continue
            self._create_in_shopify(member, digits, key, ref)

    def _ac2_member_by_time(self, row: sqlite3.Row, member: Ac2Member, customer: ShopifyCustomer, key: str, ref: str) -> None:
        """Round 8 (G2 H1): an AC2 member whose MemberNo is exactly the phone
        of a Shopify customer is never created in Shopify and never judged by
        name (staff typing is unreliable). The Shopify -> AC2 rule decides:
        linked when CreatedTime and the customer's sign-up time are at most
        the window apart; otherwise held with an alert. A sign-up whose
        start_date metafield is not readable yet is held (alert) for the same
        bounded wait as Shopify -> AC2, after which Customer.createdAt stands
        in, as it does there."""

        if (link := self.ledger.link_for_gid(customer.gid)) is not None:
            code = "possible_duplicate_member" if link["origin"] == "created_in_ac2" else "shopify_customer_linked_to_other_member"
            self._ac2_done(key, "SKIPPED", code)
            self.summary.alert(code, ref, member_no=member.member_no)
            return
        # The phone search returns no sign-up fields: read the customer.
        full = self.shopify.customer(customer.gid, self.config.birthday_month_metafield) or customer
        signed_up = full.registration_at
        if signed_up is None and REGISTRATION_TAG in full.tags and row["checks"] + 1 < REGISTRATION_WAIT_MAX_CHECKS:
            self._ac2_done(key, "PENDING", "held_for_sign_up", count_check=True)
            self.summary.alert("ac2_member_held_for_sign_up", ref, member_no=member.member_no)
            return
        signed_up = signed_up or full.created_at
        if member.created_at is None or signed_up is None:
            self._ac2_done(key, "SKIPPED", "created_time_missing")
            self.summary.alert("created_time_missing", ref, member_no=member.member_no)
            return
        if abs(member.created_at - signed_up) > timedelta(minutes=self.config.memberno_link_window_minutes):
            self._ac2_done(key, "SKIPPED", "memberno_exists_outside_window")
            self.summary.alert("memberno_exists_outside_window", ref, member_no=member.member_no)
            return
        with self.ledger.tx() as conn:
            self.ledger.add_link(conn, customer.gid, member.member_no, "matched", "memberno_and_created_time_match")
        self._ac2_done(key, "DONE", "memberno_and_created_time_match")
        self.summary.count("memberno_and_created_time_match", ref, "link")

    def _pending_sign_ups_with_phone(self, digits: str) -> tuple[list[ShopifyCustomer], bool]:
        """Round 8 (G2 H1): pending Shopify sign-ups (not linked yet) with this
        phone, read once per run (bounded by max_pending_rechecks_per_run).
        ``sure`` is False when more sign-ups are pending than were read."""

        if self._pending_sign_ups is None:
            limit = self.config.max_pending_rechecks_per_run
            rows = self.ledger.conn.execute("SELECT customer_gid FROM shopify_pending WHERE state = 'PENDING' ORDER BY updated_at LIMIT ?", (limit + 1,)).fetchall()
            found: dict[str, list[ShopifyCustomer]] = {}
            for row in rows[:limit]:
                item = self.shopify.customer(row["customer_gid"], self.config.birthday_month_metafield)
                number = phone_member_no(item.phone)[0] if item is not None else None
                if number is not None:
                    found.setdefault(number, []).append(item)
            self._pending_sign_ups = (found, len(rows) <= limit)
        found, sure = self._pending_sign_ups
        return list(found.get(digits, [])), sure

    def _create_in_shopify(self, member: Ac2Member, digits: str | None, key: str, ref: str) -> None:
        """``digits`` None: a separate customer without the phone (it belongs
        to another Shopify customer). Shopify requires a name, phone or email
        (customerCreate, API 2026-10); the AC2 Name is always present here."""

        name = (member.name or "").strip()
        if not name:
            self._ac2_done(key, "SKIPPED", "ac2_member_without_name")
            self.summary.count("ac2_member_without_name", ref)
            return
        try:
            self._spend_write(self.config.shopify_create_enabled, "shopify_create_disabled")
        except WriteRefused as refused:
            self.summary.count(refused.code, ref, "would_create")  # stays PENDING
            return
        customer_input: dict[str, Any] = {"firstName": name, "tags": list(self.config.shopify_create_tags)}
        if digits is not None:
            customer_input["phone"] = e164(digits)
        email = (member.email_address or "").strip()
        if email:
            customer_input["email"] = email
        number_field = self.config.ac2_member_no_metafield
        metafields = [{"namespace": number_field["namespace"], "key": number_field["key"], "type": number_field["type"], "value": member.member_no.strip()}]
        month = int(member.dob[5:7]) if isinstance(member.dob, str) and DATE_RE.fullmatch(member.dob[:10]) else None
        if month:
            meta = self.config.birthday_month_metafield
            metafields.append({"namespace": meta["namespace"], "key": meta["key"], "type": meta["type"], "value": str(month)})
        customer_input["metafields"] = metafields
        write_id = self._intent("ac2_to_shopify", member.member_no, None)
        try:
            gid = self.shopify.create_customer(customer_input)
        except ShopifyCreateRejected:
            with self.ledger.tx() as conn:
                self._close_write(conn, write_id, "REJECTED", "shopify_user_errors")
            self._ac2_done(key, "SKIPPED", "shopify_create_rejected")
            self.summary.count("shopify_create_rejected", ref)
            return
        except dl.DirectLocalError:
            with self.ledger.tx() as conn:
                self._close_write(conn, write_id, "UNCERTAIN", "shopify_create_outcome_unknown")
            self._ac2_done(key, "SKIPPED", "shopify_create_uncertain")
            self.summary.count("shopify_create_uncertain", ref)
            return
        with self.ledger.tx() as conn:
            self._close_write(conn, write_id, "DONE", "created", gid)
            self.ledger.add_link(conn, gid, member.member_no, "created_in_shopify", "created_from_ac2")
        self._ac2_done(key, "DONE", "shopify_customer_created" if digits is not None else "shopify_separate_customer_created")
        self.summary.count("shopify_customer_created" if digits is not None else "shopify_separate_customer_created_without_phone", ref, "create")


_SG_MEMBER_NO_RE = re.compile(r"65[3689][0-9]{7}|[3689][0-9]{7}")


def _ac2_phone(member: Ac2Member) -> str | None:
    """The member's phone: MobilePhone (``phone_member_no``, unchanged), else
    a MemberNo that is a Singapore number (Owner Q3, 2026-10-21): ``65`` + 8
    digits starting 3/6/8/9 as is, or those 8 digits alone with ``65``. Any
    other MemberNo (a generator number, a foreign number, a phone + suffix
    digit, a leading 0, 9 digits) gives no phone."""

    digits, _ = phone_member_no(member.mobile_phone)
    if digits is None:
        number = member.member_no.strip()
        if _SG_MEMBER_NO_RE.fullmatch(number):
            digits = number if len(number) == 10 else "65" + number
    return digits


class ShopifyCreateRejected(dl.DirectLocalError):
    """customerCreate answered with userErrors (nothing was created)."""


# --------------------------------------------------------------------------
# Real Shopify client
# --------------------------------------------------------------------------

_CUSTOMER_FIELDS = """id createdAt updatedAt tags firstName lastName
      defaultEmailAddress { emailAddress }
      defaultPhoneNumber { phoneNumber }
      registration: metafield(namespace: "membership", key: "start_date") { createdAt value }
      birthday: metafield(namespace: $birthdayNamespace, key: $birthdayKey) { value }"""
REGISTRATIONS_QUERY = """query XbMemberSyncRegistrations($first: Int!, $after: String, $search: String!, $birthdayNamespace: String!, $birthdayKey: String!) {
  customers(first: $first, after: $after, sortKey: UPDATED_AT, query: $search) {
    pageInfo { hasNextPage endCursor }
    nodes { """ + _CUSTOMER_FIELDS + """ }
  }
}"""
CUSTOMER_QUERY = """query XbMemberSyncCustomer($id: ID!, $birthdayNamespace: String!, $birthdayKey: String!) {
  customer(id: $id) { """ + _CUSTOMER_FIELDS + """ }
}"""
PHONE_LOOKUP_QUERY = """query XbMemberSyncPhoneLookup($search: String!) {
  customers(first: 5, query: $search) { nodes { id firstName lastName defaultPhoneNumber { phoneNumber } } }
}"""
CUSTOMER_CREATE_MUTATION = """mutation XbMemberSyncCustomerCreate($input: CustomerInput!) {
  customerCreate(input: $input) { customer { id } userErrors { field message } }
}"""
SYNC_READ_QUERIES = frozenset({direct.SESSION_QUERY, REGISTRATIONS_QUERY, CUSTOMER_QUERY, PHONE_LOOKUP_QUERY})
CUSTOMER_INPUT_KEYS = frozenset({"firstName", "phone", "email", "tags", "metafields"})


def _customer_from_node(node: Any) -> ShopifyCustomer:
    if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not dl.GID_RE.fullmatch(node["id"]):
        raise direct.ShopifyDirectError("shopify_response_shape_invalid")
    registration = node.get("registration") if isinstance(node.get("registration"), dict) else None
    phone = node.get("defaultPhoneNumber") if isinstance(node.get("defaultPhoneNumber"), dict) else {}
    email = node.get("defaultEmailAddress") if isinstance(node.get("defaultEmailAddress"), dict) else {}
    birthday = node.get("birthday") if isinstance(node.get("birthday"), dict) else {}
    tags = node.get("tags") if isinstance(node.get("tags"), list) else []
    return ShopifyCustomer(
        gid=node["id"], first_name=node.get("firstName"), last_name=node.get("lastName"),
        phone=phone.get("phoneNumber"), email=email.get("emailAddress"),
        created_at=dl.parse_created_at(node.get("createdAt")), updated_at=dl.parse_created_at(node.get("updatedAt")),
        tags=tuple(t for t in tags if isinstance(t, str)),
        registration_at=dl.parse_created_at(registration.get("createdAt")) if registration else None,
        registration_value=registration.get("value") if registration else None, birthday_value=birthday.get("value"),
    )


class SyncShopifyClient(direct.ShopifyDirectClient):
    """Admin GraphQL client for the sync: its own pinned reads plus exactly
    one write, ``customerCreate``, refused unless ``create_enabled``. The
    write is never retried after a transport or server failure (its outcome
    is unknown); the next run looks the customer up by phone instead."""

    def __init__(self, *, create_enabled: bool, **kwargs: Any):
        super().__init__(**kwargs)
        self.create_enabled = create_enabled

    def __repr__(self) -> str:
        return "SyncShopifyClient()"

    def _query_allowed(self, query: str) -> bool:
        return query in SYNC_READ_QUERIES or (query == CUSTOMER_CREATE_MUTATION and self.create_enabled)

    def _retry_allowed(self, query: str, error: direct.ShopifyDirectError) -> bool:
        # A throttled GraphQL answer means the mutation was not executed.
        return query != CUSTOMER_CREATE_MUTATION or error.code == "shopify_graphql_throttled"

    def verify_session(self, *, store_gid: str) -> None:
        data = self.execute(direct.SESSION_QUERY, {})
        if set(data) != {"shop", "currentAppInstallation"} or data.get("shop") != {"id": store_gid, "myshopifyDomain": self.shop_domain}:
            raise direct.ShopifyDirectError("shopify_store_identity_mismatch")
        installation = data["currentAppInstallation"]
        scopes = installation.get("accessScopes") if isinstance(installation, dict) else None
        if not isinstance(scopes, list) or any(not isinstance(item, dict) or not isinstance(item.get("handle"), str) for item in scopes):
            raise direct.ShopifyDirectError("shopify_response_shape_invalid")
        handles = frozenset(item["handle"] for item in scopes)
        expected = {"read_customers", "write_customers"} if self.create_enabled else {"read_customers"}
        if handles != expected:
            raise direct.ShopifyDirectError("shopify_token_scope_not_expected")

    def registrations(self, search: str, max_pages: int, birthday: Mapping[str, str]) -> tuple[list[ShopifyCustomer], bool]:
        customers: list[ShopifyCustomer] = []
        after = None
        for _ in range(max_pages):
            data = self.execute(REGISTRATIONS_QUERY, {"first": SHOPIFY_PAGE_SIZE, "after": after, "search": search, "birthdayNamespace": birthday["namespace"], "birthdayKey": birthday["key"]})
            page = data.get("customers")
            if not isinstance(page, dict) or not isinstance(page.get("nodes"), list) or not isinstance(page.get("pageInfo"), dict):
                raise direct.ShopifyDirectError("shopify_response_shape_invalid")
            customers.extend(_customer_from_node(node) for node in page["nodes"])
            if not page["pageInfo"].get("hasNextPage"):
                return customers, True
            after = page["pageInfo"].get("endCursor")
            if not isinstance(after, str) or not after:
                raise direct.ShopifyDirectError("shopify_response_shape_invalid")
        return customers, False

    def customer(self, gid: str, birthday: Mapping[str, str]) -> ShopifyCustomer | None:
        data = self.execute(CUSTOMER_QUERY, {"id": gid, "birthdayNamespace": birthday["namespace"], "birthdayKey": birthday["key"]})
        node = data.get("customer")
        return None if node is None else _customer_from_node(node)

    def find_by_phone(self, phone_e164: str) -> list[ShopifyCustomer]:
        """Customers whose own phone is this phone. The search is only a
        candidate list (its matching may be loose): every node's phone goes
        through the same ``phone_member_no`` normalisation as ours, and a node
        with no phone or another phone is dropped before any decision (G2 B1)."""

        digits, status = phone_member_no(phone_e164)
        if status != "ok":
            raise dl.DirectLocalError("phone_lookup_input_invalid")
        # Open item for live UAT: Shopify's customer search syntax for phone.
        data = self.execute(PHONE_LOOKUP_QUERY, {"search": f"phone:{phone_e164}"})
        page = data.get("customers")
        if not isinstance(page, dict) or not isinstance(page.get("nodes"), list):
            raise direct.ShopifyDirectError("shopify_response_shape_invalid")
        customers = [_customer_from_node({**node, "tags": []}) for node in page["nodes"]]
        return [c for c in customers if phone_member_no(c.phone)[0] == digits]

    def create_customer(self, customer_input: Mapping[str, Any]) -> str:
        if not self.create_enabled:
            raise WriteRefused("shopify_create_disabled")
        if not set(customer_input) <= CUSTOMER_INPUT_KEYS:
            raise dl.DirectLocalError("shopify_customer_input_invalid")
        data = self.execute(CUSTOMER_CREATE_MUTATION, {"input": dict(customer_input)})
        result = data.get("customerCreate")
        if not isinstance(result, dict):
            raise direct.ShopifyDirectError("shopify_response_shape_invalid")
        if result.get("userErrors"):
            raise ShopifyCreateRejected("shopify_create_rejected")
        customer = result.get("customer")
        if not isinstance(customer, dict) or not isinstance(customer.get("id"), str) or not dl.GID_RE.fullmatch(customer["id"]):
            raise direct.ShopifyDirectError("shopify_response_shape_invalid")
        return customer["id"]


# --------------------------------------------------------------------------
# Real AutoCount bridge client and credential loader
# --------------------------------------------------------------------------

AC2_PASSWORD_ENV = "XB_AC2_SYNC_PASSWORD"


@dataclass(frozen=True)
class Ac2Credential:
    user_id: str
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return f"Ac2Credential(user_id={mask_last3(self.user_id)!r})"

    def child_environment(self, base: Mapping[str, str]) -> dict[str, str]:
        """The bridge child's environment: the adapter reads the user ID and,
        through ``XB_AC2_PASSWORD_ENV_VAR``, the password. The parent's own
        environment never holds either."""

        env = {k: v for k, v in base.items() if k not in ("AC2_PROBE_USER_ID", "AC2_PROBE_PASSWORD", "XB_AC2_USER_ID", "XB_AC2_PASSWORD_ENV_VAR", AC2_PASSWORD_ENV)}
        env.update({"XB_AC2_USER_ID": self.user_id, "XB_AC2_PASSWORD_ENV_VAR": AC2_PASSWORD_ENV, AC2_PASSWORD_ENV: self.password})
        return env


def mask_last3(value: str | None) -> str:
    """Only the last three characters are ever shown."""

    if not value:
        return ""
    return "*" * max(len(value) - 3, 3) + value[-3:]


def _win_cred_read(target: str) -> tuple[str, str] | None:
    """(user name, secret) of a generic Windows Credential Manager entry of
    the current Windows user, or None when absent. Read in memory only."""

    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredReadW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL)))
    advapi32.CredFree.argtypes = (ctypes.c_void_p,)
    pointer = ctypes.POINTER(CREDENTIAL)()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(pointer)):  # CRED_TYPE_GENERIC
        return None
    try:
        credential = pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return credential.UserName or "", blob.decode("utf-16-le")
    finally:
        advapi32.CredFree(pointer)


def load_ac2_credential(target: str, *, cred_read: Callable[[str], tuple[str, str] | None] | None = None) -> Ac2Credential:
    """Load the credential of the AutoCount account the sync logs in with.
    Never prints, logs or returns the secret in an error; failures are fixed
    codes. ``ac2_credential_sa_refused`` refuses an AutoCount user ID of
    ``sa`` only; it does not change the SQL login (always ``sa``)."""

    if not isinstance(target, str) or not CREDENTIAL_TARGET_RE.fullmatch(target):
        raise dl.DirectLocalError("ac2_credential_target_invalid")
    try:
        value = (cred_read or _win_cred_read)(target)
    except Exception:  # noqa: BLE001 - the OS error text is never surfaced
        raise dl.DirectLocalError("ac2_credential_unreadable") from None
    if value is None:
        raise dl.DirectLocalError("ac2_credential_missing")
    user_id, password = value
    if not isinstance(user_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user_id):
        raise dl.DirectLocalError("ac2_credential_user_invalid")
    if user_id.casefold() == "sa":
        raise dl.DirectLocalError("ac2_credential_sa_refused")
    if not isinstance(password, str) or not password or len(password) > 256:
        raise dl.DirectLocalError("ac2_credential_secret_invalid")
    return Ac2Credential(user_id, password)


class SyncBridgeClient(dl.BridgeClient):
    """The DirectLocal bridge with the sync's two read-only operations and
    the ``sync_p1`` create through the unchanged write-once path."""

    def __init__(self, command: list[str], *, autocount_book: str, claims: Path, env: Mapping[str, str] | None = None, deadline_seconds: int = dl.WRITER_DEADLINE_SECONDS, **kwargs: Any):
        super().__init__(command, autocount_book=check_book(autocount_book), **kwargs)
        self.claims = Path(claims)
        self.deadline_seconds = deadline_seconds
        self._env = None if env is None else dict(env)

    def __repr__(self) -> str:
        return "SyncBridgeClient()"

    def _start(self, arguments: list[str]):
        book = ["-TargetBook", self.autocount_book] if self.autocount_book else []
        return self._popen(
            self.command + arguments + book, stdin=dl.subprocess.PIPE, stdout=dl.subprocess.PIPE, stderr=dl.subprocess.PIPE,
            creationflags=getattr(dl.subprocess, "CREATE_NO_WINDOW", 0), env=self._env,
        )

    def lookup(self, phone_digits: str | None, member_nos: list[str]) -> list[Ac2Member]:
        request = {"phone_digits": phone_digits, "sg_local": sg_local(phone_digits), "member_nos": [m for m in member_nos if m][:PENDING_LOOKUP_BATCH]}
        result = self._read("sync-lookup", request)
        rows = result.get("members")
        if not isinstance(rows, list) or len(rows) > LOOKUP_MAX_ROWS:
            raise dl.BridgeError("bridge_output_invalid")
        members = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("member_no"), str):
                raise dl.BridgeError("bridge_output_invalid")
            members.append(Ac2Member(row["member_no"], row.get("name"), row.get("mobile_phone"), row.get("email_address"), row.get("dob"), ac2_created_time(row.get("created_time"))))
        return members

    def changelog_inserts(self, after_id: int | None, limit: int, column: str) -> tuple[list[tuple[int, str]], int]:
        result = self._read("sync-changelog", {"after_id": after_id if after_id is not None else -1, "limit": limit, "xml_column": column})
        rows, max_id = result.get("rows"), result.get("max_id")
        if not isinstance(rows, list) or isinstance(max_id, bool) or not isinstance(max_id, int):
            raise dl.BridgeError("bridge_output_invalid")
        parsed = []
        for row in rows:
            if not isinstance(row, dict) or isinstance(row.get("id"), bool) or not isinstance(row.get("id"), int):
                raise dl.BridgeError("bridge_output_invalid")
            parsed.append((row["id"], row.get("xml")))
        return parsed, max_id

    def create_member(self, member_no: str, payload: Mapping[str, Any], *, write_id: str) -> tuple[str, str | None]:
        fence_id = "fence-" + write_id.removeprefix("write-")
        salt = secrets.token_hex(16)
        text = dl.canonical_payload(payload)
        digest = dl.payload_digest(salt, text)
        deadline = (dl.utc_now() + timedelta(seconds=self.deadline_seconds)).replace(microsecond=0)
        run = self.write_once(
            fence_id=fence_id, claim_directory=self.claims, deadline=deadline,
            stdin_value={"fence_id": fence_id, "member_no": member_no, "salt": salt, "payload_canonical": text, "payload_digest": digest},
            wait_seconds=self.deadline_seconds + dl.WRITER_GRACE_SECONDS, on_started=lambda pid, created: None,
        )
        return dl.classify_write(run, claim_exists=(self.claims / f"{fence_id}.claim").exists())


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def _state_root(config: SyncConfig) -> Path:
    root = dl.validate_state_root(config.state_root)
    (root / "claims").mkdir(exist_ok=True)
    return root


def build_real_engine(config: SyncConfig, ledger: SyncLedger, *, environ: Mapping[str, str]) -> SyncEngine:
    import xb_shopify_poller as poller

    transport = direct.UrllibTransport()
    shopify = SyncShopifyClient(
        create_enabled=config.shopify_create_enabled, shop_domain=config.shopify.shop_domain, api_version=config.shopify.api_version,
        token_provider=poller.token_provider(config.shopify, environ, transport, dl.utc_now, root=ledger.root), transport=transport,
        timeout=config.http_timeout_seconds,
    )
    shopify.verify_session(store_gid=config.shopify.store_gid)
    env = None
    if config.ac2_credential_target is not None:
        env = load_ac2_credential(config.ac2_credential_target).child_environment(environ)
    ac2 = SyncBridgeClient(dl.default_bridge_command(), autocount_book=config.autocount_book, claims=ledger.root / "claims", env=env)
    return SyncEngine(ledger, config, shopify=shopify, ac2=ac2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#155 phase 1 two-way member create sync")
    parser.add_argument("command", choices=("check-config", "init", "run", "status", "check-credential", "initial-link"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true", help="initial-link only: write the links (needs initial_link_enabled); default is a dry run")
    parser.add_argument("--direction", default="both", choices=("both", "shopify_to_ac2", "ac2_to_shopify"))
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check-config":
            _emit({"status": "ok", **config.summary()})
            return 0
        if args.command == "check-credential":
            if config.ac2_credential_target is None:
                raise dl.DirectLocalError("ac2_credential_target_unconfigured")
            credential = load_ac2_credential(config.ac2_credential_target)
            _emit({"status": "ok", "user_id": mask_last3(credential.user_id), "secret_present": True})
            return 0
        if args.command == "initial-link" and args.apply and not config.initial_link_enabled:
            raise dl.DirectLocalError("initial_link_disabled")  # before any login or read
        root = _state_root(config)
        ledger = SyncLedger(root)
        try:
            if args.command == "init":
                _emit(ledger.init_meta(autocount_book=config.autocount_book, store_gid=config.shopify.store_gid, shop_domain=config.shopify.shop_domain))
            elif args.command == "status":
                ledger.meta()
                counts = {name: ledger.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("links", "writes", "ac2_pending", "shopify_pending")}
                _emit({"status": "ok", "counts": counts, "uncertain_writes": ledger.conn.execute("SELECT COUNT(*) FROM writes WHERE state = 'UNCERTAIN'").fetchone()[0]})
            elif args.command == "initial-link":
                result = build_real_engine(config, ledger, environ=os.environ).initial_link(apply=args.apply)
                _emit(result)
                if result["alerts"]:
                    return 3
            else:
                result = build_real_engine(config, ledger, environ=os.environ).run(direction=args.direction)
                _emit(result)
                if result["alerts"]:
                    return 3  # completed, but an operator must look (summary.alerts)
        finally:
            ledger.close()
        return 0
    except dl.DirectLocalError as exc:
        _emit({"status": "refused", "code": exc.code})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
