"""#155 phase 1: two-way member CREATE sync between Shopify and AutoCount 2.

Scope (Owner decisions, #155 build step 2)
------------------------------------------
* Shopify -> AC2: a Shopify customer who registers as a member (tag
  ``member-mg``, registration instant at or after ``shopify_cutover_utc``)
  becomes an AutoCount member. MemberNo is the phone number in digits
  (Singapore ``65`` + 8 digits; a foreign number keeps its country code; a
  Singapore 8-digit number starting 3/6/8/9 gets ``65``). Without a usable
  phone the member gets AutoCount's own ``GetNextMemberNo`` number (test book only;
  go-live R8: never on the live book). MemberType ``REGISTERED`` (live: ``GUEST``), ExpiryDate NULL, DOB ``2000-MM-01``
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
  phone uses ``GetNextMemberNo``; email never links Shopify -> AC2. In AC2 ->
  Shopify (BF2a), a Shopify customer holding the phone under another name is
  a different person: the member is held for a person (never a second
  customer). Every link, skip and fallback is counted in the run summary
  (counts and keyed refs only).
* #155 slice BF1 (owner, 11 and 12 Nov 2026), one-off and attended:
  ``backfill-registrations`` brings older ``member-mg`` sign-ups (before the
  cutover) into AC2 (link, or a create through the same write-once child;
  doubtful cases held for a person), dry run by default, ``--apply`` only with
  ``backfill_registrations_enabled``; ``coverage`` is a read-only count of
  who is linked and who is not. Neither changes ``run``.
* #155 slice BF2a (owner, 11 and 12 Nov 2026; G0 BF2 design), one-off and
  attended: ``backfill-members`` links every in-scope AutoCount member to a
  Shopify customer (by phone, then by email; the two-word name rule) or
  creates one, only with a usable email and only when no Shopify customer
  holds that phone or email; doubtful cases are held for a person. Dry run
  by default; ``--apply`` only with ``backfill_members_enabled`` AND
  ``shopify_create_enabled``. A member whose AutoCount Note holds
  ``XB-NO-SHOPIFY`` is never created (read only as a 0/1 flag). The live
  AC2 -> Shopify path (S6) uses the same create rules: the "separate
  customer without the phone" path is gone (held), phone-only members are
  not created, and email-only members are linked or created by email.
* Loop prevention both ways through the ledger mapping (customer GID <->
  MemberNo, both unique): a member this sync created in AC2 never comes back
  to Shopify as new, and a customer it created in Shopify never goes back to
  AC2.

Safety
------
* Go-live (G0 go-live design R1-R8, Owner SCOPE-CONFIRMED 29 Oct 2026): the
  test book ``AED_TESTINGAPI`` and the production book ``AED_XBOUNDARIES``
  are the only books, compared exactly. The production book is admitted for
  this sync only: in the PowerShell bridge only ``sync-lookup``,
  ``sync-changelog`` and ``write-once`` with profile ``sync_p1``; here no
  create without a usable phone (no ``GetNextMemberNo``), and the AutoCount
  login only through the sync's own Credential Manager entry, in the login
  mode the live config states (``ac2_login_mode``: ``owner_shared`` or
  ``dedicated`` with its refused user IDs). The Core, owner console and E7
  scripts stay test-book only.
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
# v3 (go-live R2): a fresh ledger may hold the production book. SQLite cannot
# change a table CHECK in place, so v2 -> v3 is a version bump only: an older
# ledger is always a test-book ledger and keeps its old CHECK.
SCHEMA_VERSION = 3

TEST_BOOK = "AED_TESTINGAPI"
LIVE_BOOK = "AED_XBOUNDARIES"
AUTHORISED_BOOKS = frozenset({TEST_BOOK, LIVE_BOOK})
# Go-live V9 (Owner): on the live book the sync logs in only with the
# AutoCount user read from this Credential Manager target.
LIVE_CREDENTIAL_TARGET = "XB-AC2-MemberSync"
# G2 #281 LATER-3: the test book has its own target, so the live sync user is
# never used against the test book (and the S6 probe never needs it there).
TEST_CREDENTIAL_TARGET = "XB-AC2-MemberSync-Test"
AC2_USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Owner decision 30 Oct 2026 ("Use my login"): which AutoCount user that
# target holds. ``owner_shared``: the Owner's own user, shared with the
# Owner's all-day session on the VM (no refused user IDs). ``dedicated``: a
# separate sync user, with the Owner's ID in ``ac2_refused_user_ids``. The
# live config must state one; there is no default.
AC2_LOGIN_MODES = ("owner_shared", "dedicated")
MAX_WRITES_PER_RUN_LIMIT = 5
MEMBER_NO_WIDTH = 20  # AutoCount Member.MemberNo nvarchar(20)
SGT = timezone(timedelta(hours=8))
REGISTRATION_TAG = "member-mg"
SHOPIFY_PAGE_SIZE = 50
SHOPIFY_WATERMARK_MARGIN_SECONDS = 900  # search-index lag overlap, as in the Core
REGISTRATION_WAIT_MAX_CHECKS = 5  # Forms writes start_date ~3 s after the tag
# #155 P1 (G0 r2): with immediate_create_enabled, runs are also started by the
# sign-up probe, so the two bounded holds (the sign-up time hold and the AC2
# held_for_sign_up / stuck hold) are wall-clock: today's 5 checks at the
# 15-minute schedule, i.e. the first hold plus 4 intervals. With the switch
# off they stay run counts, exactly as before.
SCHEDULE_MINUTES = 15
REGISTRATION_WAIT = timedelta(minutes=(REGISTRATION_WAIT_MAX_CHECKS - 1) * SCHEDULE_MINUTES)
HOLD_SINCE_PREFIX = "hold_since:"  # watermarks rows: first hold time per held item
CAP_ALERT_MARK = "create_rate_capped_alerted_at"  # once per hour (owner D5)
CAP_ALERT_INTERVAL = timedelta(hours=1)
# Owner D1 (4 Nov 2026): all hours. A window is either this word or
# "HH:MM-HH:MM" (Asia/Singapore, start included, end excluded; a start after
# the end runs over midnight; start == end is refused, use all_day).
ALL_DAY = "all_day"
CREATE_WINDOW_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]-(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
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
# #155 BF2a (13 Nov fix, live field membership.birthday_month): a
# single_line_text_field birthday month is written as one of these names
# (the store's choice list); a number_integer field keeps the number.
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
# #155 BF2a: a usable email (design 2.3.1): trimmed, at most 254 characters,
# no whitespace, exactly one "@", a non-empty local part, a dotted domain.
USABLE_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX_LENGTH = 254
# #155 BF2a (design 2.4): create tags that could start a membership, a
# marketing list or the Forms flow are refused in config (casefold).
_REFUSED_CREATE_TAG_PREFIXES = ("member", "shopify-forms")
_REFUSED_CREATE_TAGS = frozenset({"xb member", "newsletter", "prospect"})

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
    # Go-live R8 (Owner Q2 = A): a sign-up with no usable phone is created
    # with AutoCount's GetNextMemberNo number only when this is true. null
    # means the book's default: true on the test book (phase 1 as merged),
    # false on the live book, where true is refused.
    "ac2_create_without_phone": None,
    # Go-live V9 (Owner): AutoCount user IDs the sync must never log in as
    # (the Owner's own account at least). Required on the live book in
    # ``dedicated`` mode, refused in ``owner_shared`` mode. Kept only in the
    # private config on the VM, never in this repository.
    "ac2_refused_user_ids": [],
    # Owner 30 Oct 2026: ``owner_shared`` or ``dedicated`` (AC2_LOGIN_MODES).
    # null is accepted on the test book only; the live book must state it.
    "ac2_login_mode": None,
    # #155 P1 (G0 r2, owner decisions 4 Nov 2026). Off: today's 06:00 rule,
    # unchanged. On (with ac2_create_enabled): an unmatched sign-up inside the
    # window is created at once, within the caps below; outside the window or
    # over a cap it waits for the 06:00 rule as before.
    "immediate_create_enabled": False,
    "immediate_create_window_local": ALL_DAY,
    # Owner D5: both directions together, counted from the ledger's writes
    # rows (rolling 60 minutes and 24 hours), so a restart never resets them.
    # They gate only the immediate path.
    "max_creates_per_hour": 6,
    "max_creates_per_day": 144,
    # #155 P2: the sign-up probe (``probe``) starts \XB\XB-MemberSync only
    # when this is true; otherwise it records "would wake" (observe).
    "probe_wake_enabled": False,
    "probe_max_wakes_per_day": 60,
    # #155 slice BF1 (owner, 11 and 12 Nov 2026): the one-off
    # ``backfill-registrations`` writes (links, and GUEST creates through the
    # write-once child) only with ``--apply`` AND this on. Off by default.
    # Never add this key, or the next one, to the 15-minute task's live
    # config while that task runs an older commit: an older commit refuses
    # unknown keys (runbook: the backfill uses its own copy of the config).
    "backfill_registrations_enabled": False,
    # #155 BF1: the member core's (#268) state root. When set, the backfill
    # and ``coverage`` read its claim links read-only (a member is linked if
    # either ledger says so); null: not read, and the output says so.
    "member_core_state_root": None,
    # #155 slice BF2a (owner 11 and 12 Nov 2026): the one-off
    # ``backfill-members`` creates Shopify customers only with ``--apply``
    # AND this AND ``shopify_create_enabled``. Off by default; like the BF1
    # keys, never added to the live task's config while it runs an older
    # commit (the backfill uses its own copy).
    "backfill_members_enabled": False,
    # #155 BF2a (design 2.2): the AutoCount member types that are linked to,
    # or created in, Shopify (backfill and S6). Any other type, or a member
    # with no type, is out of scope.
    "shopify_create_member_types": ["MEMBER", "GUEST"],
    # #155 BF2a (design 2.7): members that need a Shopify read (at most two
    # reads each) per backfill-members call.
    "backfill_members_lookups_per_call": 50,
    # #155 BF2b (design 2.7): the backfill's own caps, used only by
    # ``backfill-members`` (S6 keeps max_writes_per_run). null: the BF2a caps
    # (max_writes_per_run a call, RUNS_PER_HOUR x max_writes_per_run in any 60
    # minutes). Set in the backfill's own config copy for a sitting, together
    # with the daily summary's ``backfill_members_window``: a backfill create
    # outside a declared window, or over its caps, stops the next summary.
    "backfill_members_max_per_call": None,
    "backfill_members_max_per_hour": None,
}
# #155 BF2b: the bounds of the backfill's own caps (design 2.7).
BACKFILL_MEMBERS_MAX_PER_CALL_LIMIT = 25
BACKFILL_MEMBERS_MAX_PER_HOUR_LIMIT = 60
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
    "max_creates_per_hour": (1, 30),
    "max_creates_per_day": (1, 720),
    "probe_max_wakes_per_day": (1, 240),
    "backfill_members_lookups_per_call": (1, 200),
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
    ac2_create_without_phone: bool = False
    ac2_refused_user_ids: tuple[str, ...] = ()
    ac2_login_mode: str | None = None
    immediate_create_enabled: bool = False
    immediate_create_window_local: str = ALL_DAY
    max_creates_per_hour: int = 6
    max_creates_per_day: int = 144
    probe_wake_enabled: bool = False
    probe_max_wakes_per_day: int = 60
    backfill_registrations_enabled: bool = False
    member_core_state_root: str | None = None
    backfill_members_enabled: bool = False
    shopify_create_member_types: tuple[str, ...] = ("MEMBER", "GUEST")
    backfill_members_lookups_per_call: int = 50
    backfill_members_max_per_call: int | None = None
    backfill_members_max_per_hour: int | None = None

    @property
    def backfill_members_call_cap(self) -> int:
        """#155 BF2b: creates in one ``backfill-members`` call (BF2a: max_writes_per_run)."""

        return self.max_writes_per_run if self.backfill_members_max_per_call is None else self.backfill_members_max_per_call

    @property
    def backfill_members_hour_cap(self) -> int:
        """#155 BF2b: writes (all kinds) in any 60 minutes that a backfill call
        may reach (BF2a: RUNS_PER_HOUR x max_writes_per_run)."""

        return RUNS_PER_HOUR * self.max_writes_per_run if self.backfill_members_max_per_hour is None else self.backfill_members_max_per_hour

    @property
    def immediate_path_active(self) -> bool:
        """The immediate path needs both switches: ``ac2_create_enabled``
        false stops every create (the S5 rollback) whatever this one says."""

        return self.immediate_create_enabled and self.ac2_create_enabled

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
            "ac2_create_without_phone": self.ac2_create_without_phone, "ac2_refused_user_ids_count": len(self.ac2_refused_user_ids),
            "ac2_login_mode": self.ac2_login_mode,
            "immediate_create_enabled": self.immediate_create_enabled, "immediate_create_window_local": self.immediate_create_window_local,
            "max_creates_per_hour": self.max_creates_per_hour, "max_creates_per_day": self.max_creates_per_day,
            "probe_wake_enabled": self.probe_wake_enabled, "probe_max_wakes_per_day": self.probe_max_wakes_per_day,
            # #155 BF1: printed only when set, so with both keys absent or
            # off check-config prints exactly what it printed before.
            **({"backfill_registrations_enabled": True} if self.backfill_registrations_enabled else {}),
            **({"member_core_state_root_set": True} if self.member_core_state_root is not None else {}),
            # #155 BF2a: likewise printed only when not the default.
            **({"backfill_members_enabled": True} if self.backfill_members_enabled else {}),
            **({"shopify_create_member_types": list(self.shopify_create_member_types)}
               if self.shopify_create_member_types != tuple(_CONFIG_DEFAULTS["shopify_create_member_types"]) else {}),
            **({"backfill_members_lookups_per_call": self.backfill_members_lookups_per_call}
               if self.backfill_members_lookups_per_call != _CONFIG_DEFAULTS["backfill_members_lookups_per_call"] else {}),
            # #155 BF2b: likewise printed only when set.
            **({"backfill_members_max_per_call": self.backfill_members_max_per_call} if self.backfill_members_max_per_call is not None else {}),
            **({"backfill_members_max_per_hour": self.backfill_members_max_per_hour} if self.backfill_members_max_per_hour is not None else {}),
        }


def check_book(book: Any) -> str:
    """Exactly one of the two book names (ordinal string equality: a
    lookalike, another case or any other value is refused)."""

    if not isinstance(book, str) or book not in AUTHORISED_BOOKS:
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
    if not isinstance(tags, list) or len(tags) > 10 or any(not isinstance(t, str) or not TAG_RE.fullmatch(t) or create_tag_refused(t) for t in tags):
        # The registration tag would send the customer straight back to AC2;
        # BF2a: no membership, marketing or Forms tag at all (design 2.4).
        raise dl.DirectLocalError("sync_config_shopify_create_tags_invalid")
    member_types = merged["shopify_create_member_types"]
    if (not isinstance(member_types, list) or not 1 <= len(member_types) <= 10 or len(set(member_types)) != len(member_types)
            or any(not isinstance(t, str) or not MEMBER_TYPE_RE.fullmatch(t) for t in member_types)):
        raise dl.DirectLocalError("sync_config_shopify_create_member_types_invalid")
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
    live = book == LIVE_BOOK
    without_phone = merged["ac2_create_without_phone"]
    if without_phone is None:
        without_phone = not live
    elif _bool(without_phone, "ac2_create_without_phone") and live:
        # Go-live R8 (Owner Q2 = A): never GetNextMemberNo on the live book.
        raise dl.DirectLocalError("sync_config_ac2_create_without_phone_live_refused")
    refused_users = merged["ac2_refused_user_ids"]
    if not isinstance(refused_users, list) or len(refused_users) > 10 or any(not isinstance(u, str) or not AC2_USER_ID_RE.fullmatch(u) for u in refused_users):
        raise dl.DirectLocalError("sync_config_ac2_refused_user_ids_invalid")
    if live and target != LIVE_CREDENTIAL_TARGET:
        # Go-live V9: never the attended owner-console credential path.
        raise dl.DirectLocalError("sync_config_ac2_credential_target_live_required")
    if not live and target is not None and target.strip().casefold() == LIVE_CREDENTIAL_TARGET.casefold():
        # G2 #281 LATER-3: Credential Manager targets ignore case, so any
        # spelling of the live target is refused for the test book. Migration:
        # store the test credential under TEST_CREDENTIAL_TARGET (runbook).
        raise dl.DirectLocalError("sync_config_ac2_credential_target_live_only")
    login_mode = merged["ac2_login_mode"]
    if login_mode is not None and (not isinstance(login_mode, str) or login_mode not in AC2_LOGIN_MODES):
        raise dl.DirectLocalError("sync_config_ac2_login_mode_invalid")
    if live and login_mode is None:
        # Owner 30 Oct 2026: the live config states which AutoCount user the
        # sync logs in with; neither choice is a silent default.
        raise dl.DirectLocalError("sync_config_ac2_login_mode_live_required")
    if login_mode == "owner_shared" and refused_users:
        # The Owner's own user is the login in this mode, so a refused list
        # (meant to hold the Owner's ID) contradicts it, on either book.
        raise dl.DirectLocalError("sync_config_ac2_refused_user_ids_owner_shared_refused")
    if live and login_mode == "dedicated" and not refused_users:
        raise dl.DirectLocalError("sync_config_ac2_refused_user_ids_live_required")
    window = merged["immediate_create_window_local"]
    if not isinstance(window, str) or not (window == ALL_DAY or (CREATE_WINDOW_RE.fullmatch(window) and window[:5] != window[6:])):
        raise dl.DirectLocalError("sync_config_immediate_create_window_local_invalid")
    if numbers["max_creates_per_hour"] > numbers["max_creates_per_day"]:
        raise dl.DirectLocalError("sync_config_max_creates_per_day_invalid")
    core_root = merged["member_core_state_root"]
    if core_root is not None and (not isinstance(core_root, str) or not core_root or core_root == merged["state_root"]):
        # #155 BF1: another tool's root, never this sync's own.
        raise dl.DirectLocalError("sync_config_member_core_state_root_invalid")
    caps = {}
    for key, high in (("backfill_members_max_per_call", BACKFILL_MEMBERS_MAX_PER_CALL_LIMIT),
                      ("backfill_members_max_per_hour", BACKFILL_MEMBERS_MAX_PER_HOUR_LIMIT)):
        # #155 BF2b (design 2.7): null (the BF2a caps) or 1..high.
        number = merged[key]
        if number is not None and (isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= high):
            raise dl.DirectLocalError(f"sync_config_{key}_invalid")
        caps[key] = number
    if None not in caps.values() and caps["backfill_members_max_per_call"] > caps["backfill_members_max_per_hour"]:
        raise dl.DirectLocalError("sync_config_backfill_members_max_per_call_invalid")
    return SyncConfig(
        state_root=merged["state_root"], autocount_book=book, shopify=shopify, shopify_cutover_utc=cutover,
        ac2_create_enabled=_bool(merged["ac2_create_enabled"], "ac2_create_enabled"),
        shopify_create_enabled=_bool(merged["shopify_create_enabled"], "shopify_create_enabled"),
        initial_link_enabled=_bool(merged["initial_link_enabled"], "initial_link_enabled"),
        birthday_month_metafield=dict(birthday), ac2_member_no_metafield=dict(member_no_field), shopify_create_tags=tuple(tags), ac2_credential_target=target,
        changelog_xml_column=column, deferred_create_local_time=local_time, ac2_create_member_type=member_type,
        ac2_create_without_phone=without_phone, ac2_refused_user_ids=tuple(refused_users), ac2_login_mode=login_mode,
        immediate_create_enabled=_bool(merged["immediate_create_enabled"], "immediate_create_enabled"), immediate_create_window_local=window,
        probe_wake_enabled=_bool(merged["probe_wake_enabled"], "probe_wake_enabled"),
        backfill_registrations_enabled=_bool(merged["backfill_registrations_enabled"], "backfill_registrations_enabled"),
        member_core_state_root=core_root,
        backfill_members_enabled=_bool(merged["backfill_members_enabled"], "backfill_members_enabled"),
        shopify_create_member_types=tuple(member_types), **numbers, **caps,
    )


def create_tag_refused(tag: str) -> bool:
    """#155 BF2a (design 2.4): a create tag that is the registration tag, any
    ``member*`` tag (``member``, ``member-mg``, ``member-legacy``,
    ``member-qualified``, ``member-bday-month``), ``xb member``,
    ``newsletter``, ``prospect`` or any ``shopify-forms*`` tag, compared
    trimmed and case-insensitively, is refused."""

    folded = tag.strip().casefold()
    return folded.startswith(_REFUSED_CREATE_TAG_PREFIXES) or folded in _REFUSED_CREATE_TAGS


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


def in_create_window(moment: datetime, window: str) -> bool:
    """#155 P1: whether ``moment`` (any timezone) falls in the immediate-create
    window, read in Asia/Singapore. ``all_day`` is always inside; otherwise
    the start minute is inside and the end minute is not, and a start after
    the end runs over midnight ("22:00-06:00")."""

    if window == ALL_DAY:
        return True
    local = moment.astimezone(SGT)
    second = local.hour * 3600 + local.minute * 60 + local.second
    start = int(window[:2]) * 3600 + int(window[3:5]) * 60
    end = int(window[6:8]) * 3600 + int(window[9:11]) * 60
    return start <= second < end if start < end else (second >= start or second < end)


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
    # #155 BF2a (design 2.1): the scope columns of sync-lookup and
    # sync-member-scan, and the decline marker as the bridge computes it
    # (True/False; None when the row did not say: never created).
    member_type: str | None = None
    is_active: str | None = None
    expiry_date: str | None = None
    no_shopify: bool | None = None

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
# #155 slice BF1: the one-off backfill of older sign-ups (pure rules)
# --------------------------------------------------------------------------

# Owner 11 and 12 Nov 2026 (G0 two-way backfill plan, sections 2.1 to 2.4 and
# Controller decisions 1 to 3): a ``member-mg`` sign-up from before the
# cutover with no AutoCount member becomes a member of the configured create
# type (live: GUEST); a customer with a legacy tag is never created; a
# shared family phone, both forms of one number, no phone, a malformed phone
# and no name are held for a person (a link in this ledger can never be
# removed, so nothing doubtful is linked or created).
LEGACY_TAG = "member-legacy"
# Fix round 1 (Controller ruling 3, fail closed): the tags the store treats as
# legacy (as the superseded member gateway read them), compared trimmed and
# case-insensitively. A fixed list, not a setting: a link is permanent, so a
# customer carrying any of them is never created as a new member.
LEGACY_TAGS = frozenset({"member-legacy", "xb member"})
BACKFILL_RUN_KIND = "backfill_registrations"
BACKFILL_RUN_PREFIX = "backfill-"
# Resumable: the numeric customer ID of the last customer a call finished,
# one cursor per mode (a dry run never moves the apply cursor).
BACKFILL_CURSORS = {True: "backfill_registrations_cursor", False: "backfill_registrations_dry_run_cursor"}
BACKFILL_DONE_MARK = "backfill_registrations_completed_at"
# The same file name as the type move and the member core use for their stop
# file, here in the sync's own state root; checked before every customer.
STOP_FILE = "STOP"
# The daily summary allows max_writes_per_run writes for each of its 15-minute
# runs in a clock hour; the backfill keeps every rolling 60 minutes within it.
RUNS_PER_HOUR = 60 // SCHEDULE_MINUTES
_CUSTOMER_NUMBER_RE = re.compile(r"^gid://shopify/Customer/([1-9][0-9]{0,19})$")
# Codes of a customer held for a person (the weekly list). ``coverage`` reads
# the latest of these per customer from the applied backfill runs.
BACKFILL_PERSON_CODES = frozenset({
    "backfill_signup_time_missing", "backfill_no_name", "backfill_no_phone", "backfill_phone_invalid",
    "backfill_memberno_conflict", "backfill_shared_phone", "backfill_phone_on_multiple_records",
    "backfill_legacy_needs_person", "backfill_member_linked_elsewhere", "backfill_text_unstorable",
    "backfill_member_no_phone_differs", "backfill_claim_pair_unlinked",
})


def has_legacy_tag(tags: Iterable[Any]) -> bool:
    """True when any tag is one of ``LEGACY_TAGS`` (trimmed, any case)."""

    return any(isinstance(tag, str) and tag.strip().casefold() in LEGACY_TAGS for tag in tags)


def customer_number(gid: Any) -> int | None:
    """The numeric part of a customer GID (Shopify numbers customers in the
    order they were made), or None for any other shape."""

    match = _CUSTOMER_NUMBER_RE.fullmatch(gid) if isinstance(gid, str) else None
    return int(match.group(1)) if match else None


def backfill_precheck(customer: ShopifyCustomer, cutover: datetime) -> str | None:
    """The reasons a tagged, unlinked customer needs no AutoCount lookup:
    signed up on or after the cutover (the live sync's own customers), or
    held for a person before any lookup (no sign-up time, no name, no phone,
    a malformed phone). None: the customer is looked up. The sign-up time is
    the live rule's: the ``membership.start_date`` metafield's creation
    time, else Customer.createdAt."""

    instant = customer.registration_at or customer.created_at
    if instant is None:
        return "backfill_signup_time_missing"
    if instant >= cutover:
        return "backfill_signed_up_after_cutover"
    if not name_words(shopify_full_name(customer.first_name, customer.last_name)):
        return "backfill_no_name"
    status = phone_member_no(customer.phone)[1]
    if status == "absent":
        return "backfill_no_phone"
    if status == "invalid":
        return "backfill_phone_invalid"
    return None


def decide_backfill(customer: ShopifyCustomer, phone_digits: str, candidates: Iterable[Ac2Member]) -> Decision:
    """One older sign-up against the AC2 rows found by its phone. Actions:
    LINK, CREATE or HOLD (a person decides; never linked, never created).

    * One member whose MemberNo is exactly the phone (``member_no_forms``):
      linked, with no time window and no name rule (these are old; the first
      link-up's rule), unless that member's own MobilePhone is a usable
      number other than the customer's phone (held: fix round 1, Controller
      ruling 4; the live sync and ``initial-link`` keep their rule). Both
      forms of the number: held.
    * Otherwise exactly one member holding the phone (MobilePhone, or the 8
      digits inside a longer MemberNo) under the same name (the 2-word rule):
      linked. More than one: held.
    * The phone held under another name (a shared family phone): held, not
      created with the fallback number (Controller decision 2).
    * No member holds the phone: created with MemberNo = the phone (the live
      rule), unless the customer carries a legacy tag (``LEGACY_TAGS``,
      held).
    """

    rows = list(candidates)
    hits = [r for r in rows if _phone_hit(r, phone_digits)]
    forms = member_no_forms(phone_digits)
    exact = [r for r in hits if member_no_key(r.member_no) in forms]
    if len(exact) > 1:
        return Decision("HOLD", "backfill_memberno_conflict", exact[0].member_no)
    if exact:
        own_phone, status = phone_member_no(exact[0].mobile_phone)
        if status == "ok" and own_phone != phone_digits:
            return Decision("HOLD", "backfill_member_no_phone_differs", exact[0].member_no)
        return Decision("LINK", "backfill_member_no", exact[0].member_no)
    full_name = shopify_full_name(customer.first_name, customer.last_name)
    same = [r for r in hits if full_name_matches(full_name, r.name)]
    if len(same) == 1:
        return Decision("LINK", "backfill_phone_and_name", same[0].member_no)
    if len(same) > 1:
        return Decision("HOLD", "backfill_phone_on_multiple_records")
    if hits:
        return Decision("HOLD", "backfill_shared_phone", None, name_fallback_notes(full_name, [r.name for r in hits]))
    if has_legacy_tag(customer.tags):
        return Decision("HOLD", "backfill_legacy_needs_person")
    return Decision("CREATE", "backfill_new_member_phone", phone_digits)


def sync_p1_payload(customer: ShopifyCustomer, name: str, digits: str | None, member_type: str, month: int | None, today: Callable[[], str]) -> dict[str, Any]:
    """The ``sync_p1`` create payload (shared by the live create and the
    backfill). RegisterDate is the sign-up date: the ``membership.start_date``
    value when it is a date, else the sign-up time (or Customer.createdAt) in
    Singapore, else ``today()`` (called only then)."""

    registered = customer.registration_value if isinstance(customer.registration_value, str) and DATE_RE.fullmatch(customer.registration_value) else None
    if registered is None:
        instant = customer.registration_at or customer.created_at
        registered = instant.astimezone(SGT).strftime("%Y-%m-%d") if instant else today()
    return {
        "profile": "sync_p1", "member_type": member_type, "name": name, "mobile_phone": digits, "email_address": customer.email or None,
        "register_date": registered, "dob": dob_from_month(month),
    }


# --------------------------------------------------------------------------
# #155 slice BF2a: AutoCount members -> Shopify (pure rules)
# --------------------------------------------------------------------------

# Owner 11 and 12 Nov 2026 and the Controller's BF2a brief: link by phone,
# then by email (the two-word name rule, never a merge); create only with a
# usable email and only when no Shopify customer holds that phone or email;
# the decline marker in AutoCount's Note stops a create; anything doubtful is
# held for a person. A link in this ledger is permanent.
BF2_RUN_KIND = "backfill_members"
BF2_RUN_PREFIX = "bf2-"  # not "backfill-": coverage reads that prefix as BF1
BF2_CURSORS = {True: "backfill_members_cursor", False: "backfill_members_dry_run_cursor"}
BF2_DONE_MARK = "backfill_members_completed_at"
BF2_STOP_FILE = "STOP-backfill-members"  # BF1's STOP stops it too
BF2_BACKFILL_TAG = "ac2-backfill"  # fixed, added in backfill mode only
BF2_SCAN_PAGE = 1000  # sync-member-scan TOP
BF2_SCAN_MAX_PAGES = 20  # a longer list is a read failure, never a partial pass
# An expiry before this date is a placeholder (as the 12 Nov VM count reads it).
EXPIRY_PLACEHOLDER_BEFORE = "2001-01-01"
# design 2.8: an uncertain create not found by email after this long is
# settled as not made; its member is eligible again from the next call or run.
UNCERTAIN_SETTLE_AFTER = timedelta(minutes=60)
# Controller Q1 (13 Nov 2026) = YES: a member with the decline marker is
# never created, but MAY be linked to a Shopify customer they made
# themselves (ledger only, no Shopify write). One rule; a test pins it.
DECLINED_MEMBER_LINK_ALLOWED = True
# Codes counted without a ref (not a person's case). Every other bf2_ hold
# carries a keyed ref and the masked MemberNo (the person list).
BF2_COUNT_ONLY = frozenset({
    "bf2_already_linked", "bf2_claim_linked", "bf2_out_of_scope_type", "bf2_out_of_scope_inactive",
    "bf2_out_of_scope_expired", "bf2_phone_only_not_created",
})
# Calls stopped for an operator (exit 3).
BF2_OPERATOR_STOPS = ("bf2_create_uncertain", "bf2_create_rejected", "bf2_read_incomplete")
# A Shopify create can carry the AutoCount Name only within these bounds.
SHOPIFY_NAME_MAX = 255
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
NOT_READ = object()  # decide_ac2_to_shopify: the email read has not happened
# #155 BF2b (Controller ruling on BF2a Q4): an S6 row SKIPPED as out of scope
# is read again once this long has passed, at most this many a run (and only
# while the run's lookup batch has room), so a till member whose type moves
# to an in-scope type later is not skipped for good.
OUT_OF_SCOPE_RECHECK_AFTER = timedelta(hours=24)
OUT_OF_SCOPE_REQUEUE_PER_RUN = 10
# #155 BF2b (Controller answer N2): unparsable ChangeLog Member rows skipped
# (counted, alerted) in one run; one more stops the read as before.
CHANGELOG_UNPARSED_SKIP_MAX = 5


def usable_email(value: Any) -> str | None:
    """The trimmed email when usable (``USABLE_EMAIL_RE``, at most 254
    characters), else None. Compared elsewhere only after casefold.
    Fix round 1 (G2 B1): printable ASCII only, the bridge's own set
    (``[!-~]``), so no email Python calls usable is refused there;
    any other character (accented, full-width, zero-width, BOM, control)
    counts as no usable email, everywhere.
    BF2b (G2 LATER 7): only spaces are trimmed, as the bridge's SQL
    ``LTRIM/RTRIM`` trims; a tab or line break around the email is kept, so
    the email is unusable here too (fail closed: Python and AutoCount see the
    same set of emails)."""

    if not isinstance(value, str):
        return None
    text = value.strip(" ")
    if not (text.isascii() and text.isprintable()):
        return None
    return text if len(text) <= EMAIL_MAX_LENGTH and USABLE_EMAIL_RE.fullmatch(text) else None


def email_key(value: Any) -> str | None:
    """Trim + casefold of a usable email (the only way two emails compare)."""

    text = usable_email(value)
    return None if text is None else text.casefold()


def member_scope_code(member: Ac2Member, member_types: Iterable[str], today_sgt: str) -> str | None:
    """None when the member is in scope (design 2.2), else the count code.
    Fail closed: a missing or unknown type, a flag other than ``T`` and an
    unreadable expiry are out of scope. Expiry: blank, before 2001-01-01 (a
    placeholder) or today (Singapore date) or later is in scope."""

    kind = member.member_type.strip() if isinstance(member.member_type, str) else None
    if kind is None or kind not in tuple(member_types):
        return "bf2_out_of_scope_type"
    if not isinstance(member.is_active, str) or member.is_active.strip() != "T":
        return "bf2_out_of_scope_inactive"
    expiry = member.expiry_date
    if expiry is None or (isinstance(expiry, str) and not expiry.strip()):
        return None
    if not isinstance(expiry, str) or not DATE_RE.fullmatch(expiry.strip()):
        return "bf2_out_of_scope_expired"
    expiry = expiry.strip()
    if expiry < EXPIRY_PLACEHOLDER_BEFORE or expiry >= today_sgt:
        return None
    return "bf2_out_of_scope_expired"


def shopify_name_issue(name: Any) -> bool:
    """True when the AutoCount Name cannot become the customer's first name:
    blank (no word), longer than Shopify's 255, or holding a control
    character."""

    return (not isinstance(name, str) or not name_words(name) or len(name.strip()) > SHOPIFY_NAME_MAX
            or bool(_CONTROL_RE.search(name)))


def birthday_metafield_value(dob: Any, field_type: str) -> str | None:
    """The birthday month metafield value from AutoCount's DOB
    (``YYYY-MM-DD``): the English month NAME for ``single_line_text_field``
    (the store's choice list), the number for ``number_integer``; None when
    there is no usable month."""

    if not isinstance(dob, str) or not DATE_RE.fullmatch(dob[:10]):
        return None
    month = int(dob[5:7])
    if not 1 <= month <= 12:
        return None
    return MONTH_NAMES[month - 1] if field_type == "single_line_text_field" else str(month)


def build_customer_input(*, name: str, email: str, phone_digits: str | None, member_no: str, dob: Any, tags: Iterable[str],
                         member_no_field: Mapping[str, str], birthday_field: Mapping[str, str]) -> dict[str, Any]:
    """The one ``CustomerInput`` the sync sends (design 2.4): ``firstName``
    (the whole AutoCount Name), ``email`` (required), ``phone`` only when
    the member has a usable phone, ``tags`` and ``metafields`` (MemberNo,
    and the birthday month when the DOB has one). Nothing else: no consent,
    note, locale, address or tax key can be sent."""

    if usable_email(email) is None:
        raise dl.DirectLocalError("shopify_customer_input_invalid")
    customer_input: dict[str, Any] = {"firstName": name, "email": usable_email(email), "tags": list(dict.fromkeys(tags))}
    if phone_digits is not None:
        customer_input["phone"] = e164(phone_digits)
    metafields = [{"namespace": member_no_field["namespace"], "key": member_no_field["key"], "type": member_no_field["type"], "value": member_no.strip()}]
    value = birthday_metafield_value(dob, birthday_field["type"])
    if value is not None:
        metafields.append({"namespace": birthday_field["namespace"], "key": birthday_field["key"], "type": birthday_field["type"], "value": value})
    customer_input["metafields"] = metafields
    return customer_input


@dataclass(frozen=True)
class Bf2Context:
    """What ``decide_ac2_to_shopify`` needs beyond the member and the reads:
    ``customer_linked(gid)`` (sync ledger or active member core claim, or
    already used in this call), the member core's voided pairs, and the
    members settled as "not created" earlier in this call."""

    customer_linked: Callable[[str], bool]
    voided: frozenset[tuple[str, str]] = frozenset()
    settled_keys: frozenset[str] = frozenset()

    def __repr__(self) -> str:
        return "Bf2Context()"


@dataclass(frozen=True)
class Bf2Decision:
    """One member's BF2a decision: ``gid`` is the customer a LINK is made
    to (None for every other action)."""

    action: str  # LINK | CREATE | HOLD | COUNT | READ_EMAIL
    code: str
    gid: str | None = None
    notes: tuple[str, ...] = ()


# Classes decided before the contact checks: no "unusable email" note.
BF2_PRE_SCOPE_CODES = frozenset({"bf2_already_linked", "bf2_claim_linked", "bf2_member_held_by_open_write",
                                 "bf2_out_of_scope_type", "bf2_out_of_scope_inactive", "bf2_out_of_scope_expired"})
_SG_WINDOW_RE = re.compile(r"(?=([3689][0-9]{7}))")


class Bf2Index:
    """The scanned member list, ordered by MemberNo key (trim + casefold,
    the cursor's order), with the AutoCount-side sharing checks: members per
    MemberNo key, per email (trim + casefold), and per phone the sync's own
    rule (``_phone_hit``) would see."""

    def __init__(self, members: Iterable[Ac2Member]):
        self.ordered = sorted(members, key=lambda m: (member_no_key(m.member_no), m.member_no))
        self.by_key: dict[str, list[Ac2Member]] = {}
        self.by_email: dict[str, set[str]] = {}
        self._by_phone: dict[str, set[int]] = {}
        for position, member in enumerate(self.ordered):
            key = member_no_key(member.member_no)
            self.by_key.setdefault(key, []).append(member)
            mail = email_key(member.email_address)
            if mail is not None:
                self.by_email.setdefault(mail, set()).add(key)
            for digits in self._phone_candidates(member):
                self._by_phone.setdefault(digits, set()).add(position)

    def __repr__(self) -> str:
        return "Bf2Index()"

    @staticmethod
    def _phone_candidates(member: Ac2Member) -> set[str]:
        """Every phone ``_phone_hit`` could match for this member: its
        MobilePhone, its MemberNo as a phone, and every Singapore 8 digits
        inside its MemberNo (each checked again with ``_phone_hit``)."""

        found = {phone_member_no(member.mobile_phone)[0]}
        key = member_no_key(member.member_no)
        if key.isdigit() and key.isascii():
            found.add(key)
            if _SG_LOCAL_RE.fullmatch(key):
                found.add("65" + key)
        found |= {"65" + window for window in _SG_WINDOW_RE.findall(member.member_no)}
        found.discard(None)
        return found

    def phone_shared(self, member: Ac2Member, digits: str) -> bool:
        """Another member (another MemberNo key) holds this phone."""

        own = member_no_key(member.member_no)
        return any(member_no_key(self.ordered[p].member_no) != own and _phone_hit(self.ordered[p], digits) for p in self._by_phone.get(digits, ()))


def _bf2_customer_rules(member: Ac2Member, found: ShopifyCustomer, ctx: Bf2Context) -> Bf2Decision | None:
    """B2/B3: the found customer belongs to someone else, or staff unlinked
    exactly this pair in the member core."""

    if ctx.customer_linked(found.gid):
        return Bf2Decision("HOLD", "bf2_customer_linked_elsewhere")
    if (found.gid, member_no_key(member.member_no)) in ctx.voided:
        return Bf2Decision("HOLD", "bf2_claim_pair_unlinked")
    return None


def _bf2_link(member: Ac2Member, found: ShopifyCustomer, code: str, notes: tuple[str, ...] = ()) -> Bf2Decision:
    if member.no_shopify is not False:
        if not DECLINED_MEMBER_LINK_ALLOWED:
            return Bf2Decision("HOLD", "bf2_declined_not_linked")
        notes = notes + ("bf2_declined_linked",)
    return Bf2Decision("LINK", code, found.gid, notes)


def decide_ac2_to_shopify(member: Ac2Member, phone_customers: list[ShopifyCustomer], email_customer: Any, ctx: Bf2Context) -> Bf2Decision:
    """Design 2.3.2 for one member that passed the 2.3.1 checks. Actions:
    LINK, CREATE, HOLD (a person decides), COUNT (counted only), or
    READ_EMAIL (call again with ``email_customer`` read; ``NOT_READ`` until
    then). ``phone_customers``: the customers whose own phone is the
    member's (empty without a usable phone). ``email_customer``: the one
    customer holding the member's email, or None."""

    digits = _ac2_phone(member)
    email = usable_email(member.email_address)
    if len(phone_customers) > 1:
        return Bf2Decision("HOLD", "bf2_phone_on_multiple_customers")  # B1
    if len(phone_customers) == 1:
        found = phone_customers[0]
        held = _bf2_customer_rules(member, found, ctx)  # B2, B3
        if held is not None:
            return held
        if full_name_matches(shopify_full_name(found.first_name, found.last_name), member.name):  # B4
            notes = ("bf2_link_email_differs",) if email and email_key(found.email) and email_key(found.email) != email.casefold() else ()
            return _bf2_link(member, found, "bf2_phone_and_name", notes)
        return Bf2Decision("HOLD", "bf2_phone_other_name")  # B5: the email is not tried
    if email is not None and email_customer is NOT_READ:
        return Bf2Decision("READ_EMAIL", "bf2_read_email")
    if email is not None and email_customer is not None:  # C1
        found = email_customer
        held = _bf2_customer_rules(member, found, ctx)
        if held is not None:
            return held
        theirs = phone_member_no(found.phone)[0]
        if digits is not None and theirs is not None and theirs != digits:  # C2
            return Bf2Decision("HOLD", "bf2_email_customer_other_phone")
        if full_name_matches(shopify_full_name(found.first_name, found.last_name), member.name):  # C3
            return _bf2_link(member, found, "bf2_email_and_name")
        return Bf2Decision("HOLD", "bf2_email_other_name")  # C4
    if email is None:
        return Bf2Decision("COUNT", "bf2_phone_only_not_created")  # D1 (owner, 12 Nov)
    if member.no_shopify is not False:
        return Bf2Decision("HOLD", "bf2_declined_not_created")  # D2 (unknown flag: never created)
    if member_no_key(member.member_no) in ctx.settled_keys:
        return Bf2Decision("HOLD", "bf2_member_linked_elsewhere")  # D3: settled in this call
    return Bf2Decision("CREATE", "bf2_created")  # D4


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
  CHECK (autocount_book IN ('AED_TESTINGAPI', 'AED_XBOUNDARIES')));
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
            elif version in (1, 2):
                # v1 -> v2 (additive), then v2 -> v3 (version bump only: an
                # existing ledger keeps its test-book-only meta CHECK).
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    if version == 1:
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

    def customer_identity_digest(self, name: Any, email: Any, phone: Any) -> str:
        """#155 BF2a (design 2.8): salted digest of the normalised name, the
        casefold email and the phone digits of a Shopify customer the sync
        creates. Its own prefix, so it never equals an AC2 identity digest."""

        body = json.dumps(["customer_identity", normalised_name(name), email_key(email), phone_member_no(phone)[0]], ensure_ascii=True)
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


def check_binding(meta: Mapping[str, Any], config: SyncConfig) -> None:
    """A ledger serves only the book, store and domain it was made for (I2):
    a live config never opens a test ledger, and the reverse."""

    if (meta["autocount_book"], meta["store_gid"], meta["shop_domain"]) != (check_book(config.autocount_book), config.shopify.store_gid, config.shopify.shop_domain):
        raise dl.DirectLocalError("sync_ledger_binding_mismatch")


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
    # #155 P1: seconds from sign-up to the HQ create, recorded only while
    # immediate_create_enabled is true (the digest's latency figures).
    latencies: list[int] = field(default_factory=list)

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
        check_binding(ledger.meta(), config)
        self.summary = RunSummary()
        self._pending_sign_ups: tuple[dict[str, list[ShopifyCustomer]], bool] | None = None  # round 8: per run
        # #155 BF2a (design 2.8): members whose uncertain create was settled
        # as "not made" in this run or call: eligible again only next time.
        self._settled_keys: set[str] = set()

    def _today_sgt(self) -> str:
        return self.ledger.now().astimezone(SGT).strftime("%Y-%m-%d")

    # -- write budget ----------------------------------------------------------
    def _spend_write(self, switch: bool, disabled_code: str, limit: int | None = None) -> None:
        """``limit``: the call's own write budget (#155 BF2b: the backfill's
        ``backfill_members_call_cap``); None: ``max_writes_per_run``."""

        if not switch:
            raise WriteRefused(disabled_code)
        if self.summary.writes >= (self.config.max_writes_per_run if limit is None else limit):
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

    # -- #155 P1: immediate create, caps and wall-clock holds ---------------------
    def _immediate_create_allowed(self) -> bool:
        """An unmatched sign-up may be created before the 06:00 cutoff only
        with both switches on and inside the window (owner D1: all hours by
        default). The caps are checked just before the write."""

        return self.config.immediate_path_active and in_create_window(self.ledger.now(), self.config.immediate_create_window_local)

    def _create_rate_capped(self) -> bool:
        """Owner D5: writes in both directions together, from the ledger's
        ``writes`` rows (any state: an attempt counts), so a restart or a new
        process never resets the count."""

        now = self.ledger.now()

        def since(span: timedelta) -> int:
            return int(self.ledger.conn.execute("SELECT COUNT(*) FROM writes WHERE created_at >= ?", (dl.ts(now - span),)).fetchone()[0])

        return since(timedelta(hours=1)) >= self.config.max_creates_per_hour or since(timedelta(hours=24)) >= self.config.max_creates_per_day

    def _cap_hold(self, gid: str, ref: str) -> None:
        """Over a cap: held (the 06:00 rule still creates it at the cutoff),
        one alert per hour at most (the time is kept in the ledger)."""

        self._shopify_pending(gid, "PENDING", "create_rate_capped")
        last = self.ledger.watermark(CAP_ALERT_MARK)
        now = self.ledger.now()
        if last is None or now - dl.parse_ts(last) >= CAP_ALERT_INTERVAL:
            with self.ledger.tx() as conn:
                self.ledger.set_watermark(conn, CAP_ALERT_MARK, dl.ts(now))
            self.summary.alert("create_rate_capped", ref)
        else:
            self.summary.count("create_rate_capped", ref)

    def _wait_continues(self, kind: str, key: str, checks: int) -> bool:
        """The bounded hold for a sign-up time that is not readable yet. Off
        (``immediate_create_enabled`` false): today's rule, at most
        REGISTRATION_WAIT_MAX_CHECKS - 1 held runs. On: held while less than
        REGISTRATION_WAIT has passed since the first hold (kept in the
        watermarks table, so a restart or extra woken runs change nothing)."""

        if not self.config.immediate_create_enabled:
            return checks + 1 < REGISTRATION_WAIT_MAX_CHECKS
        name = f"{HOLD_SINCE_PREFIX}{kind}:{key}"
        first = self.ledger.watermark(name)
        now = self.ledger.now()
        if first is None:
            with self.ledger.tx() as conn:
                self.ledger.set_watermark(conn, name, dl.ts(now))
            return True
        if now - dl.parse_ts(first) < REGISTRATION_WAIT:
            return True
        with self.ledger.tx() as conn:
            conn.execute("DELETE FROM watermarks WHERE name = ?", (name,))
        return False

    def _prune_hold_clocks(self) -> None:
        """Drop the first-hold times of items no longer held, so a later hold
        of the same item starts a new bounded wait."""

        held = {f"{HOLD_SINCE_PREFIX}shopify:{row[0]}" for row in self.ledger.conn.execute(
            "SELECT customer_gid FROM shopify_pending WHERE state = 'PENDING' AND code = 'registration_event_pending'")}
        held |= {f"{HOLD_SINCE_PREFIX}ac2:{row[0]}" for row in self.ledger.conn.execute(
            "SELECT member_no_key FROM ac2_pending WHERE state = 'PENDING' AND code = 'held_for_sign_up'")}
        stale = [row[0] for row in self.ledger.conn.execute("SELECT name FROM watermarks WHERE substr(name, 1, ?) = ?", (len(HOLD_SINCE_PREFIX), HOLD_SINCE_PREFIX)) if row[0] not in held]
        if stale:
            with self.ledger.tx() as conn:
                conn.executemany("DELETE FROM watermarks WHERE name = ?", [(name,) for name in stale])

    # -- run -------------------------------------------------------------------
    def run(self, *, direction: str = "both") -> dict[str, Any]:
        if direction not in ("both", "shopify_to_ac2", "ac2_to_shopify"):
            raise dl.DirectLocalError("sync_direction_invalid")
        run_id = f"run-{uuid.uuid4().hex}"
        started = self.ledger.now()
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO runs(run_id, started_at) VALUES (?, ?)", (run_id, dl.ts(started)))
        self._recover_crashed_intents()
        if self.config.immediate_create_enabled:
            self._prune_hold_clocks()
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
        # Go-live (digest): counts, alert codes and keyed refs with masked
        # MemberNos only, as the run prints them. Older rows hold counts only.
        stored = {"counts": result["counts"], "alerts": result["alerts"], "refs": result["refs"]}
        if self.summary.latencies:
            # #155 P1: only ever set with immediate_create_enabled (seconds only).
            result["create_latency_seconds"] = stored["create_latency_seconds"] = list(self.summary.latencies)
        with self.ledger.tx() as conn:
            conn.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE run_id = ?", (dl.ts(self.ledger.now()), json.dumps(stored, sort_keys=True), run_id))
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

    # -- #155 slice BF1: the one-off backfill of older sign-ups -------------------
    def backfill_registrations(self, *, apply: bool, max_creates: int, claims: "ClaimLinks | None" = None) -> dict[str, Any]:
        """Older ``member-mg`` sign-ups (before the cutover) with no link:
        linked to their AutoCount member, created as the configured member
        type (live: GUEST) through the unchanged write-once child, or held
        for a person (``decide_backfill``). A dry run (the default) writes
        nothing but its own cursor. ``apply`` needs
        ``backfill_registrations_enabled``; it creates at most
        ``max_creates`` (1 to ``max_writes_per_run``) members a call, never
        a write that would put a rolling 60 minutes over the daily summary's
        hourly allowance, and stops before the next customer when the stop
        file exists or after a create that is not verified. At most
        ``max_shopify_customers_per_run`` customers are looked up in AC2 a
        call. Resumable: customers are taken in customer-number order after
        the mode's cursor. No Shopify write, no new AutoCount operation, no
        alert (counts only); an applied call is stored as its own run kind.
        ``claims``: the member core's claim links (read-only), or None."""

        if apply and not self.config.backfill_registrations_enabled:
            raise dl.DirectLocalError("backfill_registrations_disabled")
        if isinstance(max_creates, bool) or not isinstance(max_creates, int) or not 1 <= max_creates <= self.config.max_writes_per_run:
            raise dl.DirectLocalError("backfill_max_invalid")
        run_id = f"{BACKFILL_RUN_PREFIX}{uuid.uuid4().hex}"
        if apply:
            with self.ledger.tx() as conn:
                conn.execute("INSERT INTO runs(run_id, started_at) VALUES (?, ?)", (run_id, dl.ts(self.ledger.now())))
        cursor_name = BACKFILL_CURSORS[apply]
        floor = self.ledger.watermark(cursor_name)
        floor_number = int(floor) if floor is not None else 0
        customers, complete = self.shopify.registrations(f"tag:'{REGISTRATION_TAG}'", self.config.shopify_max_pages, self.config.birthday_month_metafield)
        stopped = None
        finished = False
        last = floor_number
        population: dict[str, int] = {}
        if not complete:
            # Customers come back in updatedAt order, so a partial read is not
            # a prefix in customer-number order: nothing is decided.
            stopped = "backfill_read_incomplete"
        else:
            ordered = self._backfill_order(customers)
            population = self._backfill_population(ordered, claims)
            lookups = creates = 0
            claimed: set[str] = set()
            finished = True
            for number, customer in ordered:
                if number <= floor_number:
                    continue
                if self._backfill_stop_requested():
                    stopped, finished = "stop_file_present", False
                    break
                code = self._backfill_skip(customer, claims)
                if code is not None:
                    self._backfill_count(code, customer)
                    last = number
                    continue
                if lookups >= self.config.max_shopify_customers_per_run:
                    stopped, finished = "backfill_lookup_budget_reached", False
                    break
                lookups += 1
                digits = phone_member_no(customer.phone)[0]
                decision = decide_backfill(customer, digits, self.ac2.lookup(digits, []))
                outcome = self._backfill_act(customer, digits, decision, claimed, claims, apply=apply, creates=creates, max_creates=max_creates)
                if outcome == "created":
                    creates += 1
                elif outcome != "done":
                    stopped, finished = outcome, False
                    break
                last = number
        with self.ledger.tx() as conn:
            if finished:
                # A finished pass: the next call in this mode starts again.
                conn.execute("DELETE FROM watermarks WHERE name = ?", (cursor_name,))
                if apply:
                    self.ledger.set_watermark(conn, BACKFILL_DONE_MARK, dl.ts(self.ledger.now()))
            elif last > floor_number:
                self.ledger.set_watermark(conn, cursor_name, str(last))
        result = {
            "schema": SUMMARY_SCHEMA, "status": "completed", "direction": BACKFILL_RUN_KIND, "mode": "apply" if apply else "dry_run",
            "complete": finished, "stopped": stopped, "alerts": [], "autocount_book": self.config.autocount_book,
            "member_type": self.config.ac2_create_member_type, "max_creates": max_creates, "writes": self.summary.writes,
            "member_core_ledger": "read" if claims is not None else "not_configured",
            "population": dict(sorted(population.items())), "counts": dict(sorted(self.summary.counts.items())), "refs": self.summary.refs,
        }
        if apply:
            # Its own run kind (a later summary can show "backfill: N"); no
            # alert is ever stored, so the daily summary's alert list is
            # unchanged. Counts, codes and keyed refs with masked MemberNos.
            stored = {"kind": BACKFILL_RUN_KIND, "counts": result["counts"], "alerts": [], "refs": result["refs"], "stopped": stopped}
            with self.ledger.tx() as conn:
                conn.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE run_id = ?", (dl.ts(self.ledger.now()), json.dumps(stored, sort_keys=True), run_id))
        return result

    def _backfill_stop_requested(self) -> bool:
        return (self.ledger.root / STOP_FILE).exists()

    @staticmethod
    def _backfill_order(customers: Iterable[ShopifyCustomer]) -> list[tuple[int, ShopifyCustomer]]:
        """Tagged customers once each, in customer-number order. A GID of any
        other shape is never decided (it cannot hold a cursor place)."""

        unique: dict[int, ShopifyCustomer] = {}
        for item in customers:
            number = customer_number(item.gid)
            if number is not None and REGISTRATION_TAG in item.tags:
                unique[number] = item
        return sorted(unique.items())

    def _backfill_skip(self, customer: ShopifyCustomer, claims: "ClaimLinks | None") -> str | None:
        """Decided without AutoCount: already linked (either ledger), an open
        write that may still land, or ``backfill_precheck``."""

        if self.ledger.link_for_gid(customer.gid) is not None:
            return "backfill_already_linked"
        if claims is not None and customer.gid in claims.by_gid:
            return "backfill_claim_linked"
        if self.ledger.open_write_for_customer(customer.gid) is not None:
            # Never re-sent: the write may still land (as the live G2 H2 rule).
            return "backfill_held_by_open_write"
        return backfill_precheck(customer, self.config.shopify_cutover_utc)

    def _backfill_population(self, ordered: list[tuple[int, ShopifyCustomer]], claims: "ClaimLinks | None") -> dict[str, int]:
        """Every tagged customer read, by the class it has before any lookup
        (the dry run's first figures: how many need a lookup at all)."""

        population: dict[str, int] = {"tagged": len(ordered)}
        for _, customer in ordered:
            code = self._backfill_skip(customer, claims) or "backfill_needs_lookup"
            population[code] = population.get(code, 0) + 1
        return population

    def _backfill_count(self, code: str, customer: ShopifyCustomer, action: str | None = None, member_no: str | None = None) -> None:
        if code in ("backfill_already_linked", "backfill_claim_linked", "backfill_signed_up_after_cutover"):
            self.summary.count(code)
        else:
            self.summary.count(code, self.ledger.ref("c", customer.gid), action or "hold", member_no=member_no)

    def _backfill_act(self, customer: ShopifyCustomer, digits: str, decision: Decision, claimed: set[str], claims: "ClaimLinks | None", *,
                      apply: bool, creates: int, max_creates: int) -> str:
        """``done``, ``created``, or the code that stops the call (the cursor
        then stays before this customer: after an uncertain write the next
        call finds the open write and holds it, never re-sends)."""

        gid = customer.gid
        if decision.action == "HOLD":
            self._backfill_count(decision.code, customer, member_no=decision.member_no)
            for note in decision.notes:
                self.summary.count(note)
            return "done"
        member_no = decision.member_no
        key = member_no_key(member_no)
        if claims is not None and (gid, key) in claims.voided:
            # Fix round 2 (Controller ruling 2): staff unlinked exactly this
            # pair in the member core; a link here is permanent, so a person
            # decides (the member core refuses the pair too).
            self._backfill_count("backfill_claim_pair_unlinked", customer, member_no=member_no)
            return "done"
        if key in claimed or self.ledger.link_for_member(member_no) is not None or (claims is not None and key in claims.by_key):
            # The member already belongs to another customer: never a second link.
            self._backfill_count("backfill_member_linked_elsewhere", customer, member_no=member_no)
            return "done"
        if self.ledger.open_write_for_member(member_no) is not None:
            self._backfill_count("backfill_member_held_by_open_write", customer, member_no=member_no)
            return "done"
        if decision.action == "LINK":
            claimed.add(key)
            if not apply:
                self._backfill_count("backfill_would_link", customer, "would_link", member_no)
                self.summary.count(decision.code)
                return "done"
            with self.ledger.tx() as conn:
                self.ledger.add_link(conn, gid, member_no, "matched", decision.code)
            self._backfill_count("backfill_linked", customer, "link", member_no)
            self.summary.count(decision.code)
            return "done"
        name = " ".join(part.strip() for part in (customer.first_name, customer.last_name) if isinstance(part, str) and part.strip())
        month = birthday_month(customer.birthday_value)
        payload = sync_p1_payload(customer, name, digits, self.config.ac2_create_member_type, month, self._today_sgt)
        issue = dl.autocount_text_issue(payload)
        if issue or not dl.MEMBER_NO_RE.fullmatch(member_no):
            self._backfill_count("backfill_text_unstorable", customer, member_no=member_no)
            self.summary.count(issue or "member_no_invalid")
            return "done"
        if customer.birthday_value not in (None, "") and month is None:
            self.summary.count("birthday_month_unparsed")
        if not apply:
            claimed.add(key)
            self._backfill_count("backfill_would_create", customer, "would_create", member_no)
            return "done"
        if not self.config.ac2_create_enabled:
            return "ac2_create_disabled"  # the live switch stops every create, the backfill's too
        if creates >= max_creates or self.summary.writes >= self.config.max_writes_per_run:
            return "backfill_max_reached"
        hour = int(self.ledger.conn.execute("SELECT COUNT(*) FROM writes WHERE created_at >= ?", (dl.ts(self.ledger.now() - timedelta(hours=1)),)).fetchone()[0])
        if hour >= RUNS_PER_HOUR * self.config.max_writes_per_run:
            return "backfill_hour_cap_reached"
        claimed.add(key)
        self._spend_write(True, "ac2_create_disabled")
        write_id = self._intent("shopify_to_ac2", member_no, gid, self.ledger.identity_digest(name, digits))
        state, code = self.ac2.create_member(member_no, payload, write_id=write_id)
        with self.ledger.tx() as conn:
            if state == "CREATED_VERIFIED":
                self._close_write(conn, write_id, "DONE", "created")
                self.ledger.add_link(conn, gid, member_no, "created_in_ac2", decision.code)
            elif state == "NOT_WRITTEN_PROVEN":
                self._close_write(conn, write_id, "NOT_WRITTEN", code)
            else:
                # Looked up by the live run's existing resolution, never re-sent.
                self._close_write(conn, write_id, "UNCERTAIN", code or state.lower())
        if state == "CREATED_VERIFIED":
            self._backfill_count("backfill_ac2_member_created", customer, "create", member_no)
            self.summary.count(decision.code)
            return "created"
        if state == "NOT_WRITTEN_PROVEN":
            self._backfill_count("backfill_ac2_create_not_written", customer, member_no=member_no)
            if code and re.fullmatch(r"[a-z0-9_]{1,64}", code):
                self.summary.count(code)
            return "backfill_create_not_written"
        self._backfill_count("backfill_ac2_create_uncertain", customer, member_no=member_no)
        return "backfill_create_uncertain"

    # -- #155 slice BF2a: AutoCount members -> Shopify (one-off) ------------------
    def backfill_members(self, *, apply: bool, max_creates: int, claims: "ClaimLinks | None" = None) -> dict[str, Any]:
        """Every AutoCount member (``sync-member-scan``) with no link: linked
        to its Shopify customer (phone, then email, the name rule), created
        in Shopify (only with a usable email, no customer holding the phone
        or email, no decline marker), or held for a person (design 2.3). A
        dry run (the default) writes nothing but its own cursor. ``apply``
        needs ``backfill_members_enabled`` AND ``shopify_create_enabled``;
        it settles earlier uncertain creates first, creates at most
        ``max_creates`` (1 to ``backfill_members_call_cap``: BF2a
        ``max_writes_per_run``, BF2b ``backfill_members_max_per_call``)
        customers a call, never over the rolling-hour cap
        (``backfill_members_hour_cap``), holds a member whose create Shopify
        refused for the same details, stops before the next member when a stop
        file exists, and stops for an operator after a create that is
        rejected or uncertain, or a read that fails. At most
        ``backfill_members_lookups_per_call`` members are read in Shopify a
        call. Resumable by a MemberNo cursor per mode. No alert (counts
        only); an applied call is stored as run kind ``backfill_members``."""

        if apply and not self.config.backfill_members_enabled:
            raise dl.DirectLocalError("backfill_members_disabled")
        if apply and not self.config.shopify_create_enabled:
            raise dl.DirectLocalError("shopify_create_disabled")
        if isinstance(max_creates, bool) or not isinstance(max_creates, int) or not 1 <= max_creates <= self.config.backfill_members_call_cap:
            raise dl.DirectLocalError("backfill_members_max_invalid")
        run_id = f"{BF2_RUN_PREFIX}{uuid.uuid4().hex}"
        stopped = None
        if apply:
            with self.ledger.tx() as conn:
                conn.execute("INSERT INTO runs(run_id, started_at) VALUES (?, ?)", (run_id, dl.ts(self.ledger.now())))
            try:
                self._resolve_uncertain("ac2_to_shopify")  # design 2.8: settle before any decision
            except dl.DirectLocalError:
                stopped = "bf2_read_incomplete"
        cursor_name = BF2_CURSORS[apply]
        floor = self.ledger.watermark(cursor_name)
        finished = False
        last: str | None = None
        population: dict[str, int] = {}
        members = None
        if stopped is None:
            try:
                members = self._bf2_scan()
            except dl.DirectLocalError:
                stopped = "bf2_read_incomplete"  # nothing decided on a partial list
        if members is not None:
            index = Bf2Index(members)
            if floor is None:
                population = self._bf2_population(index, claims)
            lookups = creates = 0
            claimed_gids: set[str] = set()
            finished = True
            for member in index.ordered:
                key = member_no_key(member.member_no)
                if floor is not None and key <= floor:
                    continue
                if self._bf2_stop_requested():
                    stopped, finished = "stop_file_present", False
                    break
                code = self._bf2_precheck(member, index, claims)
                if code not in BF2_PRE_SCOPE_CODES and isinstance(member.email_address, str) and member.email_address.strip() and usable_email(member.email_address) is None:
                    self.summary.count("bf2_email_unusable")  # counted as no email (design 2.3.1)
                if code is not None:
                    self._bf2_count(code, member)
                    if code == "bf2_create_rejected_held":
                        self.summary.count(self._rejected_create_class(member))  # the class only (runbook)
                    last = key
                    continue
                if lookups >= self.config.backfill_members_lookups_per_call:
                    stopped, finished = "backfill_lookup_budget_reached", False
                    break
                lookups += 1
                outcome = self._bf2_one(member, claims, claimed_gids, apply=apply, creates=creates, max_creates=max_creates)
                if outcome == "created":
                    creates += 1
                elif outcome != "done":
                    stopped, finished = outcome, False
                    break
                last = key
        with self.ledger.tx() as conn:
            if finished:
                # A finished pass: the next call in this mode starts again.
                conn.execute("DELETE FROM watermarks WHERE name = ?", (cursor_name,))
                if apply:
                    self.ledger.set_watermark(conn, BF2_DONE_MARK, dl.ts(self.ledger.now()))
            elif last is not None and (floor is None or last > floor):
                self.ledger.set_watermark(conn, cursor_name, last)
        result = {
            "schema": SUMMARY_SCHEMA, "status": "completed", "direction": BF2_RUN_KIND, "mode": "apply" if apply else "dry_run",
            "complete": finished, "stopped": stopped, "alerts": [], "autocount_book": self.config.autocount_book,
            "max_creates": max_creates, "writes": self.summary.writes,
            "member_core_ledger": "read" if claims is not None else "not_configured",
            "population": dict(sorted(population.items())), "counts": dict(sorted(self.summary.counts.items())), "refs": self.summary.refs,
        }
        if apply:
            # Its own run kind; no alert is ever stored. Counts, codes and
            # keyed refs with masked MemberNos only.
            stored = {"kind": BF2_RUN_KIND, "counts": result["counts"], "alerts": [], "refs": result["refs"], "stopped": stopped}
            with self.ledger.tx() as conn:
                conn.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE run_id = ?", (dl.ts(self.ledger.now()), json.dumps(stored, sort_keys=True), run_id))
        return result

    def _bf2_stop_requested(self) -> bool:
        return (self.ledger.root / STOP_FILE).exists() or (self.ledger.root / BF2_STOP_FILE).exists()

    def _bf2_scan(self) -> list[Ac2Member]:
        """The whole member list, page by page (keyset on MemberNo). A list
        longer than the page bound, or a page that does not advance, is a
        read failure: nothing is decided on part of the list."""

        members: list[Ac2Member] = []
        after = ""
        for _ in range(BF2_SCAN_MAX_PAGES):
            page = self.ac2.member_scan(after)
            members.extend(page)
            if len(page) < BF2_SCAN_PAGE:
                return members
            if page[-1].member_no == after:
                break
            after = page[-1].member_no
        raise dl.DirectLocalError("bf2_scan_incomplete")

    def _bf2_precheck(self, member: Ac2Member, index: "Bf2Index", claims: "ClaimLinks | None") -> str | None:
        """Design 2.3.1, before any Shopify read. None: the member is read."""

        key = member_no_key(member.member_no)
        if self.ledger.link_for_member(member.member_no) is not None:
            return "bf2_already_linked"  # A1
        if claims is not None and key in claims.by_key:
            return "bf2_claim_linked"  # A2
        if self.ledger.open_write_for_member(member.member_no) is not None:
            return "bf2_member_held_by_open_write"  # A3: never re-sent
        scope = member_scope_code(member, self.config.shopify_create_member_types, self._today_sgt())
        if scope is not None:
            return scope  # A4
        digits = _ac2_phone(member)
        email = email_key(member.email_address)
        if digits is None and email is None:
            return "bf2_no_contact"  # A5
        if email is not None and len(index.by_email.get(email, ())) > 1:
            return "bf2_email_shared_in_ac2"  # A6
        if len(index.by_key.get(key, ())) > 1 or (digits is not None and len({k for k in member_no_forms(digits) if k in index.by_key}) > 1):
            return "bf2_memberno_conflict"  # A8 (checked before A7, which it is a case of)
        if digits is not None and index.phone_shared(member, digits):
            return "bf2_phone_shared_in_ac2"  # A7
        if shopify_name_issue(member.name):
            return "bf2_no_name"  # A9
        if self._rejected_create_class(member) is not None:
            # BF2b (G2 LATER 3): Shopify refused a create for exactly these
            # details; held until staff change the email, phone or name.
            return "bf2_create_rejected_held"
        return None

    def _rejected_create_class(self, member: Ac2Member) -> str | None:
        """#155 BF2b: the classified code of a REJECTED create sent for this
        member with its current name, email and phone (the INTENT's identity
        digest), or None. Any change to those details releases the hold."""

        email = usable_email(member.email_address)
        if email is None:
            return None
        digest = self.ledger.customer_identity_digest((member.name or "").strip(), email, _ac2_phone(member))
        row = self.ledger.conn.execute(
            "SELECT code FROM writes WHERE member_no_key = ? AND direction = 'ac2_to_shopify' AND state = 'REJECTED' AND intent_digest = ? "
            "ORDER BY created_at DESC LIMIT 1", (member_no_key(member.member_no), digest)).fetchone()
        return None if row is None else (row["code"] or "shopify_user_errors_other")

    def _bf2_population(self, index: "Bf2Index", claims: "ClaimLinks | None") -> dict[str, int]:
        """Every member scanned, by its class before any read (the first
        call of a pass); the classes add up to ``scanned``."""

        population: dict[str, int] = {"scanned": len(index.ordered)}
        for member in index.ordered:
            code = self._bf2_precheck(member, index, claims) or "bf2_needs_lookup"
            population[code] = population.get(code, 0) + 1
        return population

    def _bf2_count(self, code: str, member: Ac2Member, action: str | None = None) -> None:
        if code in BF2_COUNT_ONLY:
            self.summary.count(code)
        else:
            self.summary.count(code, self.ledger.ref("m", member_no_key(member.member_no)), action or "hold", member_no=member.member_no)

    def _bf2_one(self, member: Ac2Member, claims: "ClaimLinks | None", claimed_gids: set[str], *, apply: bool, creates: int, max_creates: int) -> str:
        """``done``, ``created``, or the code that stops the call (the cursor
        then stays before this member)."""

        key = member_no_key(member.member_no)
        digits = _ac2_phone(member)
        email = usable_email(member.email_address)

        def linked(gid: str) -> bool:
            return gid in claimed_gids or self.ledger.link_for_gid(gid) is not None or (claims is not None and gid in claims.by_gid)

        ctx = Bf2Context(customer_linked=linked, voided=claims.voided if claims is not None else frozenset(), settled_keys=frozenset(self._settled_keys))
        try:
            phone_customers = self.shopify.find_by_phone(e164(digits)) if digits is not None else []
            decision = decide_ac2_to_shopify(member, phone_customers, NOT_READ, ctx)
            if decision.action == "READ_EMAIL":
                decision = decide_ac2_to_shopify(member, phone_customers, self.shopify.find_by_email(email), ctx)
        except dl.DirectLocalError:
            return "bf2_read_incomplete"
        if decision.action == "LINK" and self.ledger.open_write_for_customer(decision.gid) is not None:
            # Fix round 1 (G2 LATER 2): as initial-link and BF1, never linked
            # while a write for that customer may still land.
            decision = Bf2Decision("HOLD", "bf2_customer_held_by_open_write")
        if decision.action in ("COUNT", "HOLD"):
            self._bf2_count(decision.code, member)
            return "done"
        ref = self.ledger.ref("m", key)
        if decision.action == "LINK":
            claimed_gids.add(decision.gid)
            if apply:
                with self.ledger.tx() as conn:
                    self.ledger.add_link(conn, decision.gid, member.member_no, "matched", decision.code)
            self._bf2_count("bf2_linked" if apply else "bf2_would_link", member, "link" if apply else "would_link")
            for code in (decision.code, *decision.notes):
                self.summary.count(code)
            return "done"
        if not apply:
            self._bf2_count("bf2_would_create", member, "would_create")
            return "done"
        if not self.config.shopify_create_enabled:
            return "shopify_create_disabled"  # refused before any login too
        if creates >= max_creates or self.summary.writes >= self.config.backfill_members_call_cap:
            return "backfill_max_reached"
        hour = int(self.ledger.conn.execute("SELECT COUNT(*) FROM writes WHERE created_at >= ?", (dl.ts(self.ledger.now() - timedelta(hours=1)),)).fetchone()[0])
        if hour >= self.config.backfill_members_hour_cap:  # BF2b: the backfill's own hour cap (design 2.7)
            return "backfill_hour_cap_reached"
        outcome = self._create_in_shopify(member, digits, email, key, ref, backfill=True)
        if outcome == "created":
            self._bf2_count("bf2_created", member, "create")
            return "created"
        if outcome == "rejected":
            self._bf2_count("bf2_create_rejected", member)
            return "bf2_create_rejected"
        if outcome == "uncertain":
            self._bf2_count("bf2_create_uncertain", member)
            return "bf2_create_uncertain"
        if outcome == "refused":
            return "backfill_max_reached"
        self._bf2_count("bf2_no_name", member)  # "invalid": nothing sent (the prechecks make this unreachable)
        return "done"

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
                self._resolve_uncertain_shopify_create(row)

    def _resolve_uncertain_shopify_create(self, row: sqlite3.Row) -> None:
        """#155 BF2a (design 2.8): an AC2 -> Shopify create whose outcome is
        unknown is settled by reading the member's current email
        (``customerByIdentifier``), never re-sent. Found with the INTENT's
        identity digest and not linked: linked. Found with another identity:
        stays UNCERTAIN. Not found: stays UNCERTAIN under 60 minutes, then
        settled as not made; the member is eligible again only from the next
        call or run. A row without a digest is never linked automatically."""

        ref = self.ledger.ref("m", row["member_no_key"])
        members = [m for m in self.ac2.lookup(None, [row["member_no"]]) if member_no_key(m.member_no) == row["member_no_key"]]
        email = usable_email(members[0].email_address) if len(members) == 1 else None
        if row["intent_digest"] is None:
            self.summary.count("uncertain_write_identity_unverifiable", ref)
            return
        if email is None:
            self.summary.count("uncertain_write_unresolved", ref)
            return
        found = self.shopify.find_by_email(email)
        if found is not None:
            if not hmac.compare_digest(self.ledger.customer_identity_digest(shopify_full_name(found.first_name, found.last_name), found.email, found.phone),
                                       row["intent_digest"]):
                self.summary.count("uncertain_write_identity_mismatch", ref)  # someone else holds the email
            elif self.ledger.link_for_gid(found.gid) is not None:
                self.summary.count("uncertain_write_unresolved", ref)
            else:
                with self.ledger.tx() as conn:
                    self.ledger.add_link(conn, found.gid, members[0].member_no, "created_in_shopify", "uncertain_write_found")
                    self._close_write(conn, row["write_id"], "RESOLVED", "uncertain_write_found", found.gid)
                    conn.execute("UPDATE ac2_pending SET state = 'DONE', code = 'uncertain_write_found', updated_at = ? WHERE member_no_key = ?",
                                 (dl.ts(self.ledger.now()), row["member_no_key"]))
                self.summary.count("uncertain_write_resolved_linked", ref, "link")
            return
        if not hmac.compare_digest(self.ledger.customer_identity_digest((members[0].name or "").strip(), email, _ac2_phone(members[0])),
                                   row["intent_digest"]):
            # BF2b (G2 LATER 6): the member's name, email or phone changed
            # since the INTENT, so "not found by the current email" proves
            # nothing (the create may have landed under the old email): never
            # settled automatically; it stays UNCERTAIN for a person.
            self.summary.count("uncertain_write_identity_changed", ref)
            return
        if self.ledger.now() - dl.parse_ts(row["created_at"]) < UNCERTAIN_SETTLE_AFTER:
            self.summary.count("uncertain_write_unresolved", ref)
            return
        with self.ledger.tx() as conn:
            self._close_write(conn, row["write_id"], "RESOLVED", "uncertain_write_settled_not_found")
            # S6: decided again from the next run (this run skips it).
            conn.execute("UPDATE ac2_pending SET state = 'PENDING', code = 'uncertain_write_settled', checks = 0, updated_at = ? WHERE member_no_key = ?",
                         (dl.ts(self.ledger.now()), row["member_no_key"]))
        self._settled_keys.add(row["member_no_key"])
        self.summary.count("uncertain_write_settled_not_found", ref)

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
            if self._wait_continues("shopify", gid, 0 if row is None else row["checks"]):
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
        immediate = False
        if decision.action == "CREATE" and self.ledger.now() < deferred_create_cutoff(instant, self.config.deferred_create_local_time):
            # #155 P1 (owner D1, D3): with the immediate path on and inside
            # its window the create goes ahead now (the caps are checked just
            # before the write); staff keying still links or wins, see
            # _create_in_ac2. Otherwise round 6 as before:
            immediate = self._immediate_create_allowed()
            if not immediate:
                # Round 6 (Owner): staff create members by hand from the sign-up
                # sheet. Until the next morning's cutoff the sync only looks for
                # their member (each run re-reads this pending customer) and
                # writes nothing.
                self._shopify_pending(gid, "PENDING", "waiting_for_staff")
                self.summary.count("waiting_for_staff", ref)
                return
        if decision.action == "CREATE" and decision.member_no is None and not self.config.ac2_create_without_phone:
            # Go-live R8 (Owner Q2 = A): past the cutoff and no usable phone.
            # Never GetNextMemberNo here: staff key this member by hand.
            # SKIPPED rows are never read again, so the alert is raised once.
            self._shopify_pending(gid, "SKIPPED", "no_phone_sign_up_needs_staff")
            self.summary.alert("no_phone_sign_up_needs_staff", ref)
            return
        member_no = decision.member_no
        if member_no is None:
            self._create_in_ac2(customer, digits, decision, ref, immediate=immediate, signed_up=instant)  # GetNextMemberNo at write time
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
        self._create_in_ac2(customer, digits, decision, member_ref, immediate=immediate, signed_up=instant)

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

        if self.config.autocount_book == LIVE_BOOK:
            # Go-live R8: never on the live book (the bridge refuses
            # next-member-no there too); config and _register stop it first.
            return None, "member_no_generator_not_authorised"
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

    def _create_in_ac2(self, customer: ShopifyCustomer, digits: str | None, decision: Decision, member_ref: str, *,
                       immediate: bool = False, signed_up: datetime | None = None) -> None:
        """``immediate``: before the 06:00 cutoff, through the #155 P1 path
        (capped; counted ``created_immediately``). Otherwise the 06:00 rule
        (``created_after_wait``). Every other rule is the same for both."""

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
        payload = sync_p1_payload(customer, name, digits, self.config.ac2_create_member_type, month, self._today_sgt)
        issue = dl.autocount_text_issue(payload)
        if issue or (member_no is not None and not dl.MEMBER_NO_RE.fullmatch(member_no)):
            self._shopify_pending(gid, "SKIPPED", issue or "member_no_invalid")
            self.summary.count(issue or "member_no_invalid", member_ref)
            return
        if immediate and self._create_rate_capped():
            self._cap_hold(gid, member_ref)
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
            if immediate:
                # #155 P1: a count, not an alert (one per sign-up would flood
                # the digest); the 06:00 path below keeps its alert.
                self.summary.count("created_immediately")
            else:
                # Round 6: every sync create comes after the staff wait; the
                # operator checks staff did not make the member too.
                self.summary.alert("created_after_wait")
            if self.config.immediate_create_enabled and signed_up is not None:
                self.summary.latencies.append(max(int((self.ledger.now() - signed_up).total_seconds()), 0))
            self._count_decision(decision)
        elif state == "NOT_WRITTEN_PROVEN":
            self._shopify_pending(gid, "PENDING", "ac2_not_written")
            self.summary.count("ac2_create_not_written", member_ref)
            if code == "member_type_unknown":
                # Go-live R7: the configured type is not in the book; every
                # create is refused before the claim until an operator fixes it.
                self.summary.alert("member_type_unknown", member_ref)
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
        unparsed = 0
        for row_id, xml_text in sorted(rows, key=lambda item: item[0]):
            if self.ledger.conn.execute("SELECT 1 FROM changelog_seen WHERE id = ?", (row_id,)).fetchone() is not None:
                if row_id > current:
                    self.summary.count("changelog_row_replayed")  # a replay is a no-op
                continue  # an overlap re-read of a processed row: nothing to do
            member = parse_changelog_member(xml_text)
            if member is None:
                # Every run that meets one alerts (a person looks).
                self.summary.alert("changelog_row_unparsed")
                if unparsed >= CHANGELOG_UNPARSED_SKIP_MAX:
                    # Too many in one run: stop here as before; the watermark
                    # stays at the last good ID, so this row and everything
                    # after it are read again next run.
                    break
                # BF2b (Controller answer N2): counted and skipped (marked
                # seen, the watermark moves past it), so one bad row no longer
                # blocks every later insert. The backfill re-sweep finds the
                # member.
                unparsed += 1
                with self.ledger.tx() as conn:
                    conn.execute("INSERT INTO changelog_seen(id, seen_at) VALUES (?, ?)", (row_id, dl.ts(self.ledger.now())))
                    if row_id > current:
                        self.ledger.set_watermark(conn, "ac2_changelog_id", str(row_id))
                        current = row_id
                self.summary.count("changelog_row_unparsed_skipped")
                continue
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

    def _requeue_out_of_scope(self) -> None:
        """#155 BF2b (Controller ruling on BF2a Q4): a row SKIPPED as
        ``ac2_member_out_of_scope`` (for example a ``Default`` till member)
        is PENDING again once OUT_OF_SCOPE_RECHECK_AFTER has passed since it
        was decided, oldest first, at most OUT_OF_SCOPE_REQUEUE_PER_RUN a run
        and only while this run's lookup batch has room. It is then decided
        like any other row: still out of scope, SKIPPED again (read again a
        day later); in scope (the type moved, active, expiry ok), linked or
        created. No lookup of its own: it rides in the run's one lookup."""

        queued = self.ledger.conn.execute("SELECT COUNT(*) FROM ac2_pending WHERE state = 'PENDING'").fetchone()[0]
        room = min(OUT_OF_SCOPE_REQUEUE_PER_RUN, PENDING_LOOKUP_BATCH - queued)
        if room <= 0:
            return
        due = dl.ts(self.ledger.now() - OUT_OF_SCOPE_RECHECK_AFTER)
        keys = [row["member_no_key"] for row in self.ledger.conn.execute(
            "SELECT member_no_key FROM ac2_pending WHERE state = 'SKIPPED' AND code = 'ac2_member_out_of_scope' AND updated_at <= ? "
            "ORDER BY updated_at, member_no_key LIMIT ?", (due, room))]
        for key in keys:
            with self.ledger.tx() as conn:
                conn.execute("UPDATE ac2_pending SET state = 'PENDING', code = 'out_of_scope_recheck', checks = 0, updated_at = ? WHERE member_no_key = ?",
                             (dl.ts(self.ledger.now()), key))
            self.summary.count("ac2_member_out_of_scope_rechecked", self.ledger.ref("m", key))

    def _process_ac2_pending(self) -> None:
        self._requeue_stuck_holds()
        self._requeue_out_of_scope()
        # Go-live L10 (G2 r10): a row waiting only for shopify_create_enabled
        # (code would_create) must not hold a batch slot while creates are
        # off, so every other PENDING row is read first, oldest first; the
        # would_create rows follow oldest first. G2 #281 LATER-1: once creates
        # are on, the whole queue is read oldest first (plain changelog_id),
        # so a new walk-in never jumps the backlog.
        if self.config.shopify_create_enabled:
            order_sql = "SELECT * FROM ac2_pending WHERE state = 'PENDING' ORDER BY changelog_id LIMIT ?"
        else:
            order_sql = "SELECT * FROM ac2_pending WHERE state = 'PENDING' ORDER BY (code IS 'would_create'), changelog_id LIMIT ?"
        pending = self.ledger.conn.execute(order_sql, (PENDING_LOOKUP_BATCH,)).fetchall()
        if not pending:
            return
        if len(pending) == PENDING_LOOKUP_BATCH and all(row["code"] == "would_create" for row in pending):
            # Nothing decidable is left in a full batch: one alert per run.
            self.summary.alert("ac2_pending_would_create_backlog")
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
            if key in self._settled_keys:
                continue  # BF2a (design 2.8): settled in this run, decided again next run
            if member_scope_code(member, self.config.shopify_create_member_types, self._today_sgt()) is not None:
                # BF2a (design 2.2, 2.3.3): another type, inactive or expired.
                self._ac2_done(key, "SKIPPED", "ac2_member_out_of_scope")
                self.summary.count("ac2_member_out_of_scope", ref)
                continue
            digits = _ac2_phone(member)
            email = usable_email(member.email_address)
            if digits is None and email is None:
                self._ac2_done(key, "SKIPPED", "ac2_member_without_phone")
                self.summary.count("ac2_member_without_phone", ref)
                continue
            if digits is None:
                self._s6_by_email(member, None, email, key, ref)  # BF2a: email-only member
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
                    if self._wait_continues("ac2", key, row["checks"]):
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
                    # another person; never linked. BF2a (design 2.3.3,
                    # Controller decision 2): held for a person, never a
                    # second customer without the phone.
                    self._ac2_done(key, "SKIPPED", "ac2_phone_on_other_customer")
                    self.summary.count("ac2_phone_on_other_customer", ref)
                    for note in name_fallback_notes(member.name, [shopify_full_name(customer.first_name, customer.last_name)]):
                        self.summary.count(note)
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
            self._s6_by_email(member, digits, email, key, ref)

    def _s6_by_email(self, member: Ac2Member, digits: str | None, email: str | None, key: str, ref: str) -> None:
        """#155 BF2a (design 2.3.3): no Shopify customer holds the member's
        phone (or the member has none). Without a usable email: not created.
        With one: held when another AutoCount member has it; linked when one
        customer holds it under the same name (and no other phone); held when
        that customer is someone else; never created with the decline
        marker; otherwise created. New codes are counts, never alerts.
        BF2b (G2 LATER 5): with ``shopify_create_enabled`` false, a member
        that could only end ``would_create`` skips the AutoCount email check
        (one AutoCount sign-in a row a run) unless the email read finds a
        customer; the row is decided in full once creates are on. BF2b (G2
        LATER 8): before a create, a member whose phone another AutoCount
        member holds is SKIPPED (``ac2_phone_shared_in_ac2``, a count), as
        the backfill's A7."""

        if email is None:
            self._ac2_done(key, "SKIPPED", "ac2_member_phone_only_not_created")
            self.summary.count("ac2_member_phone_only_not_created", ref)
            return
        deferred = not self.config.shopify_create_enabled and member.no_shopify is False
        if not deferred and self._s6_email_shared(member, email, key, ref):
            return
        found = self.shopify.find_by_email(email)
        if found is not None:
            if deferred and self._s6_email_shared(member, email, key, ref):
                return
            theirs = phone_member_no(found.phone)[0]
            if (link := self.ledger.link_for_gid(found.gid)) is not None:
                # The same existing alerts as the phone path.
                code = "possible_duplicate_member" if link["origin"] == "created_in_ac2" else "shopify_customer_linked_to_other_member"
                self._ac2_done(key, "SKIPPED", code)
                self.summary.alert(code, ref, member_no=member.member_no)
            elif (digits is not None and theirs is not None and theirs != digits) or not full_name_matches(
                    shopify_full_name(found.first_name, found.last_name), member.name):
                self._ac2_done(key, "SKIPPED", "ac2_email_on_other_customer")
                self.summary.count("ac2_email_on_other_customer", ref)
            elif self.ledger.open_write_for_customer(found.gid) is not None:
                # Fix round 1 (G2 LATER 2): a write for that customer may
                # still land; never linked meanwhile (a count, not an alert).
                self._ac2_done(key, "SKIPPED", "ac2_customer_held_by_open_write")
                self.summary.count("ac2_customer_held_by_open_write", ref)
            else:
                with self.ledger.tx() as conn:
                    self.ledger.add_link(conn, found.gid, member.member_no, "matched", "linked_existing_shopify_customer_by_email")
                self._ac2_done(key, "DONE", "linked_existing_shopify_customer_by_email")
                self.summary.count("linked_existing_shopify_customer_by_email", ref, "link")
            return
        if member.no_shopify is not False:
            # The decline marker (or a row that did not say): never created.
            self._ac2_done(key, "SKIPPED", "ac2_member_declined_not_created")
            self.summary.count("ac2_member_declined_not_created", ref)
            return
        if digits is not None and self.config.shopify_create_enabled and any(
                member_no_key(other.member_no) != key and _phone_hit(other, digits) for other in self.ac2.lookup(digits, [])):
            self._ac2_done(key, "SKIPPED", "ac2_phone_shared_in_ac2")
            self.summary.count("ac2_phone_shared_in_ac2", ref)
            return
        self._create_in_shopify(member, digits, email, key, ref)

    def _s6_email_shared(self, member: Ac2Member, email: str, key: str, ref: str) -> bool:
        """BF2a: another AutoCount member holds this email (an email-only
        ``sync-lookup``): the row is SKIPPED (``ac2_email_shared_in_ac2``)."""

        mine = email.casefold()
        if any(email_key(m.email_address) == mine and member_no_key(m.member_no) != key for m in self.ac2.lookup(None, [], email=email)):
            self._ac2_done(key, "SKIPPED", "ac2_email_shared_in_ac2")
            self.summary.count("ac2_email_shared_in_ac2", ref)
            return True
        return False

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
        if signed_up is None and REGISTRATION_TAG in full.tags and self._wait_continues("ac2", key, row["checks"]):
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

    def _create_in_shopify(self, member: Ac2Member, digits: str | None, email: str | None, key: str, ref: str, *, backfill: bool = False) -> str:
        """The sync's only Shopify write (S6 and ``backfill-members``; design
        2.4). ``email`` is required (a usable email; Shopify keeps it unique,
        which makes a retry after a wrong settle a refusal, never a second
        customer); ``digits`` None: an email-only member, created without a
        phone. The ac2_pending row is updated on the S6 path only. Returns
        ``created``, ``rejected``, ``uncertain``, ``refused`` (switch off or
        write budget spent; nothing sent) or ``invalid`` (nothing sent)."""

        name = (member.name or "").strip()
        if not name:
            if not backfill:
                self._ac2_done(key, "SKIPPED", "ac2_member_without_name")
            self.summary.count("ac2_member_without_name", ref)
            return "invalid"
        if usable_email(email) is None:
            if not backfill:
                self._ac2_done(key, "SKIPPED", "shopify_customer_input_invalid")
            self.summary.count("shopify_customer_input_invalid", ref)
            return "invalid"
        try:
            self._spend_write(self.config.shopify_create_enabled, "shopify_create_disabled",
                              self.config.backfill_members_call_cap if backfill else None)
        except WriteRefused as refused:
            self.summary.count(refused.code, ref, "would_create")  # stays PENDING
            if refused.code == "shopify_create_disabled" and not backfill:
                # Go-live L10: read after every decidable row (see above).
                self._ac2_done(key, "PENDING", "would_create")
            return "refused"
        tags = list(self.config.shopify_create_tags) + ([BF2_BACKFILL_TAG] if backfill else [])
        customer_input = build_customer_input(
            name=name, email=email, phone_digits=digits, member_no=member.member_no, dob=member.dob, tags=tags,
            member_no_field=self.config.ac2_member_no_metafield, birthday_field=self.config.birthday_month_metafield,
        )
        # INTENT before the send, with the identity a settle read must find.
        write_id = self._intent("ac2_to_shopify", member.member_no, None, self.ledger.customer_identity_digest(name, email, digits))
        try:
            gid = self.shopify.create_customer(customer_input)
        except ShopifyCreateRejected as rejected:
            with self.ledger.tx() as conn:
                # Only a classified code from the field path; never the message.
                self._close_write(conn, write_id, "REJECTED", rejected.field_class)
            if not backfill:
                self._ac2_done(key, "SKIPPED", "shopify_create_rejected")
            self.summary.count("shopify_create_rejected", ref)
            return "rejected"
        except dl.DirectLocalError:
            with self.ledger.tx() as conn:
                self._close_write(conn, write_id, "UNCERTAIN", "shopify_create_outcome_unknown")
            if not backfill:
                self._ac2_done(key, "SKIPPED", "shopify_create_uncertain")
            self.summary.count("shopify_create_uncertain", ref)
            return "uncertain"
        with self.ledger.tx() as conn:
            self._close_write(conn, write_id, "DONE", "created", gid)
            self.ledger.add_link(conn, gid, member.member_no, "created_in_shopify", "bf2_created" if backfill else "created_from_ac2")
        if not backfill:
            self._ac2_done(key, "DONE", "shopify_customer_created")
        self.summary.count("shopify_customer_created", ref, "create")
        return "created"


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
    """customerCreate answered with userErrors (nothing was created).
    ``field_class`` is the classified field path of the first error
    (``shopify_user_errors_email``, ``_phone``, ``_metafield``, ``_other``);
    the message text is never kept."""

    def __init__(self, code: str = "shopify_create_rejected", field_class: str = "shopify_user_errors_other"):
        super().__init__(code)
        self.field_class = field_class


def classify_user_errors(errors: Any) -> str:
    """#155 BF2a (design 2.8.6): the first userError's ``field`` path read
    only for the words ``email``, ``phone`` or ``metafields``."""

    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    path = first.get("field")
    words = {str(part).casefold() for part in path} if isinstance(path, list) else set()
    for word, code in (("email", "shopify_user_errors_email"), ("phone", "shopify_user_errors_phone"), ("metafields", "shopify_user_errors_metafield")):
        if word in words:
            return code
    return "shopify_user_errors_other"


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
# #155 BF2a (design 2.1): a direct lookup by email (read_customers). The
# answer is kept only when its own email equals ours after trim + casefold.
EMAIL_LOOKUP_QUERY = """query XbMemberSyncEmailLookup($email: String!) {
  customerByIdentifier(identifier: {emailAddress: $email}) { id firstName lastName defaultEmailAddress { emailAddress } defaultPhoneNumber { phoneNumber } }
}"""
CUSTOMER_CREATE_MUTATION = """mutation XbMemberSyncCustomerCreate($input: CustomerInput!) {
  customerCreate(input: $input) { customer { id } userErrors { field message } }
}"""
SYNC_READ_QUERIES = frozenset({direct.SESSION_QUERY, REGISTRATIONS_QUERY, CUSTOMER_QUERY, PHONE_LOOKUP_QUERY, EMAIL_LOOKUP_QUERY})
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

    def find_by_email(self, email: str) -> ShopifyCustomer | None:
        """#155 BF2a: the one customer holding this email, or None. A null
        answer is "none"; any GraphQL error is a read failure (raised, so
        the caller stops; shopify.dev documents no not-found error form).
        An answer whose own email differs after trim + casefold is dropped."""

        wanted = email_key(email)
        if wanted is None:
            raise dl.DirectLocalError("email_lookup_input_invalid")
        data = self.execute(EMAIL_LOOKUP_QUERY, {"email": usable_email(email)})
        if "customerByIdentifier" not in data:
            raise direct.ShopifyDirectError("shopify_response_shape_invalid")
        node = data["customerByIdentifier"]
        if node is None:
            return None
        found = _customer_from_node({**node, "tags": []})
        return found if email_key(found.email) == wanted else None

    def create_customer(self, customer_input: Mapping[str, Any]) -> str:
        if not self.create_enabled:
            raise WriteRefused("shopify_create_disabled")
        if not set(customer_input) <= CUSTOMER_INPUT_KEYS or usable_email(customer_input.get("email")) is None:
            # BF2a: every create carries a usable email (design 2.4).
            raise dl.DirectLocalError("shopify_customer_input_invalid")
        data = self.execute(CUSTOMER_CREATE_MUTATION, {"input": dict(customer_input)})
        result = data.get("customerCreate")
        if not isinstance(result, dict):
            raise direct.ShopifyDirectError("shopify_response_shape_invalid")
        if result.get("userErrors"):
            raise ShopifyCreateRejected("shopify_create_rejected", classify_user_errors(result.get("userErrors")))
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


def load_ac2_credential(target: str, *, refused_user_ids: Iterable[str] = (), cred_read: Callable[[str], tuple[str, str] | None] | None = None) -> Ac2Credential:
    """Load the credential of the AutoCount account the sync logs in with.
    Never prints, logs or returns the secret in an error; failures are fixed
    codes. ``ac2_credential_sa_refused`` refuses an AutoCount user ID of
    ``sa`` only; it does not change the SQL login (always ``sa``). Go-live
    V9: ``ac2_credential_user_refused`` refuses any of ``refused_user_ids``
    (the Owner's own account in ``dedicated`` mode; none in
    ``owner_shared`` mode), case-insensitively, before any login."""

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
    if user_id.casefold() in {refused.casefold() for refused in refused_user_ids}:
        raise dl.DirectLocalError("ac2_credential_user_refused")
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

    def lookup(self, phone_digits: str | None, member_nos: list[str], *, email: str | None = None) -> list[Ac2Member]:
        """``email`` (BF2a, S6's AutoCount-side shared-email check): the
        members holding exactly this email, alone (no phone or number)."""

        if email is not None:
            if usable_email(email) is None or phone_digits is not None or member_nos:
                raise dl.DirectLocalError("sync_lookup_request_invalid")
            request: dict[str, Any] = {"phone_digits": None, "sg_local": None, "member_nos": [], "email_address": usable_email(email)}
        else:
            request = {"phone_digits": phone_digits, "sg_local": sg_local(phone_digits), "member_nos": [m for m in member_nos if m][:PENDING_LOOKUP_BATCH]}
        result = self._read("sync-lookup", request)
        rows = result.get("members")
        if not isinstance(rows, list) or len(rows) > LOOKUP_MAX_ROWS:
            raise dl.BridgeError("bridge_output_invalid")
        return [_member_from_row(row) for row in rows]

    def member_scan(self, after_member_no: str) -> list[Ac2Member]:
        """#155 BF2a: one page (at most ``BF2_SCAN_PAGE``) of every member
        after a MemberNo (keyset; "" for the first page), in AutoCount's
        MemberNo order, with the scope columns and the decline flag."""

        if not isinstance(after_member_no, str) or len(after_member_no) > 40 or _CONTROL_RE.search(after_member_no):
            raise dl.DirectLocalError("sync_member_scan_request_invalid")
        result = self._read("sync-member-scan", {"after_member_no": after_member_no})
        rows = result.get("members")
        if not isinstance(rows, list) or len(rows) > BF2_SCAN_PAGE:
            raise dl.BridgeError("bridge_output_invalid")
        return [_member_from_row(row) for row in rows]

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
        state, code = dl.classify_write(run, claim_exists=(self.claims / f"{fence_id}.claim").exists())
        if (state, code) == ("NOT_WRITTEN_PROVEN", "writer_failed_before_claim"):
            # Go-live R7: name the bridge's own refusal when it is one the
            # engine acts on (classify_write proves only "before the claim").
            refusal = _bridge_refusal(run.stdout)
            if refusal in BRIDGE_REFUSALS_BEFORE_CLAIM:
                code = refusal
        return state, code


def _member_from_row(row: Any) -> Ac2Member:
    """One bridge member row. The decline flag is True only for 1, False
    only for 0; anything else (absent, another value) is None, which never
    creates."""

    if not isinstance(row, dict) or not isinstance(row.get("member_no"), str):
        raise dl.BridgeError("bridge_output_invalid")
    flag = row.get("no_shopify")
    no_shopify = None if isinstance(flag, bool) else {1: True, 0: False}.get(flag) if isinstance(flag, int) else None
    return Ac2Member(
        row["member_no"], row.get("name"), row.get("mobile_phone"), row.get("email_address"), row.get("dob"), ac2_created_time(row.get("created_time")),
        member_type=row.get("member_type") if isinstance(row.get("member_type"), str) else None,
        is_active=row.get("is_active") if isinstance(row.get("is_active"), str) else None,
        expiry_date=row.get("expiry_date") if isinstance(row.get("expiry_date"), str) else None,
        no_shopify=no_shopify,
    )


# Child refusals before the claim that the engine reports by name.
BRIDGE_REFUSALS_BEFORE_CLAIM = frozenset({"member_type_unknown", "member_no_taken", "production_book_not_authorised"})


def _bridge_refusal(stdout: Any) -> str | None:
    """The ``error`` code of the child's last output line, or None."""

    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if not isinstance(stdout, str):
        return None
    lines = [line for line in stdout.replace("\r", "\n").split("\n") if line.strip()]
    try:
        value = json.loads(lines[-1]) if lines else None
    except (json.JSONDecodeError, RecursionError):
        return None
    code = value.get("error") if isinstance(value, dict) else None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code) else None


# --------------------------------------------------------------------------
# #155 slice BF1: the member core's claim links and the coverage check
# --------------------------------------------------------------------------

# The member core's (#268) ledger file and tool name, as xb_member_benefits
# defines them (not imported: that module imports this one; a test pins both).
MEMBER_CORE_LEDGER_FILE = "member_benefits.sqlite3"
MEMBER_CORE_TOOL = "xb268_member_benefits"
COVERAGE_SCHEMA = "xb.directlocal.member_sync.coverage.v1"
COVERAGE_MAX_REFS = 200
# Fix round 2 (Controller ruling 2): the one member core ledger version this
# module reads (xb_member_benefits.SCHEMA_VERSION; a test pins both). Any
# other version is refused: a later one must be a deliberate change here.
MEMBER_CORE_LEDGER_VERSION = 2


@dataclass(frozen=True)
class ClaimLinks:
    """The member core's active claim links (customer GID -> MemberNo key,
    and back), plus the voided pairs ``(customer GID, MemberNo key)`` that
    staff unlinked there: history, never an active link, never relinked."""

    by_gid: Mapping[str, str]
    by_key: Mapping[str, str]
    voided: frozenset[tuple[str, str]] = frozenset()

    def __repr__(self) -> str:  # never render customer or member data
        return "ClaimLinks()"


def _open_read_only(path: Path, missing: str, unreadable: str) -> sqlite3.Connection:
    if not path.is_file():
        raise dl.DirectLocalError(missing)
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        raise dl.DirectLocalError(unreadable) from None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def read_claim_links(root: str | Path, config: SyncConfig) -> ClaimLinks:
    """Read-only (no lock, no migration, no write), as the member core
    reads this sync's links: its version must be ``MEMBER_CORE_LEDGER_VERSION``
    and its meta the member core's, bound to the same book, store and domain.
    Active links come from ``active_claim_links``; voided pairs separately."""

    conn = _open_read_only(Path(root) / MEMBER_CORE_LEDGER_FILE, "member_core_ledger_missing", "member_core_ledger_unreadable")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != MEMBER_CORE_LEDGER_VERSION:
            raise dl.DirectLocalError("member_core_ledger_version_unknown")
        meta = conn.execute("SELECT tool, autocount_book, store_gid, shop_domain FROM meta WHERE id = 1").fetchone()
        if meta is None or meta["tool"] != MEMBER_CORE_TOOL or (meta["autocount_book"], meta["store_gid"], meta["shop_domain"]) != (
                config.autocount_book, config.shopify.store_gid, config.shopify.shop_domain):
            raise dl.DirectLocalError("member_core_ledger_binding_mismatch")
        by_gid: dict[str, str] = {}
        by_key: dict[str, str] = {}
        for row in conn.execute("SELECT customer_gid, member_no_key FROM active_claim_links"):
            by_gid[row["customer_gid"]] = row["member_no_key"]
            by_key[row["member_no_key"]] = row["customer_gid"]
        voided = frozenset((row["customer_gid"], row["member_no_key"]) for row in conn.execute(
            "SELECT c.customer_gid, c.member_no_key FROM claim_links c JOIN claim_unlinks u ON u.link_id = c.link_id"))
        return ClaimLinks(by_gid, by_key, voided)
    except sqlite3.Error:
        raise dl.DirectLocalError("member_core_ledger_unreadable") from None
    finally:
        conn.close()


def load_claim_links(config: SyncConfig) -> ClaimLinks | None:
    """The claim links when ``member_core_state_root`` is set, else None."""

    if config.member_core_state_root is None:
        return None
    root = dl.validate_state_root(config.member_core_state_root)
    dl.verify_private_state(root)
    return read_claim_links(root, config)


def _keyed_ref(ref_key: str, kind: str, value: str) -> str:
    """``SyncLedger.ref`` from a read-only connection (same key, same shape)."""

    digest = hmac.new(bytes.fromhex(ref_key), f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{kind}-{digest[:dl.MEMBER_REF_HEX]}"


def coverage_report(root: Path, config: SyncConfig, *, shopify: Any, claims: ClaimLinks | None) -> dict[str, Any]:
    """#155 BF1 / G0 section 4: the read-only coverage check. Reads the
    customers tagged ``member-mg`` or ``member-legacy`` (Shopify reads
    only), this sync's ledger read-only (no lock, no migration, no write)
    and, when given, the member core's claim links. Writes nothing anywhere
    and opens no AutoCount session. Every tagged customer falls in exactly
    one class, and every linked AutoCount member in exactly one class, so
    each side adds up to its total. Counts, codes and keyed refs (with a
    masked MemberNo) only."""

    customers: dict[str, ShopifyCustomer] = {}
    for tag in (REGISTRATION_TAG, LEGACY_TAG):
        found, complete = shopify.registrations(f"tag:'{tag}'", config.shopify_max_pages, config.birthday_month_metafield)
        if not complete:
            raise dl.DirectLocalError("coverage_read_incomplete")
        for item in found:
            if REGISTRATION_TAG in item.tags or has_legacy_tag(item.tags):  # the search is only a candidate list
                customers[item.gid] = item
    conn = _open_read_only(Path(root) / LEDGER_FILE, "sync_ledger_missing", "sync_ledger_unreadable")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] not in (2, SCHEMA_VERSION):
            raise dl.DirectLocalError("sync_ledger_schema_unsupported")
        meta = conn.execute("SELECT autocount_book, store_gid, shop_domain, ref_key FROM meta WHERE id = 1").fetchone()
        if meta is None:
            raise dl.DirectLocalError("sync_ledger_not_initialised")
        check_binding(meta, config)
        ref_key = meta["ref_key"]
        links = {row["customer_gid"]: row["member_no_key"] for row in conn.execute("SELECT customer_gid, member_no_key FROM links")}
        open_writes = {row[0] for row in conn.execute(
            "SELECT customer_gid FROM writes WHERE direction = 'shopify_to_ac2' AND state IN ('INTENT', 'UNCERTAIN') AND customer_gid IS NOT NULL")}
        # The latest person code per customer ref from the applied backfill
        # runs (a later link or create clears it; a dry run stores nothing).
        held: dict[str, str] = {}
        for (summary,) in conn.execute("SELECT summary FROM runs WHERE substr(run_id, 1, ?) = ? AND finished_at IS NOT NULL ORDER BY started_at",
                                       (len(BACKFILL_RUN_PREFIX), BACKFILL_RUN_PREFIX)):
            try:
                stored = json.loads(summary or "{}")
            except (json.JSONDecodeError, RecursionError):
                continue
            if not isinstance(stored, dict) or stored.get("kind") != BACKFILL_RUN_KIND or not isinstance(stored.get("refs"), list):
                continue
            for entry in stored["refs"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("ref"), str):
                    continue
                if entry.get("code") in BACKFILL_PERSON_CODES:
                    held[entry["ref"]] = entry["code"]
                else:
                    held.pop(entry["ref"], None)
        pending = {f"{state}/{code}": number for state, code, number in conn.execute(
            "SELECT state, COALESCE(code, ''), COUNT(*) FROM shopify_pending GROUP BY state, code")}
    except sqlite3.Error:
        raise dl.DirectLocalError("sync_ledger_unreadable") from None
    finally:
        conn.close()
    claim_gids = dict(claims.by_gid) if claims is not None else {}
    claim_keys = dict(claims.by_key) if claims is not None else {}
    unlinked_gids = {pair[0] for pair in claims.voided} if claims is not None else set()
    classes: dict[str, int] = {}
    refs: list[dict[str, str]] = []
    listed = 0  # customers with a ref-worthy class (refs holds the first COVERAGE_MAX_REFS)
    for gid_value, item in sorted(customers.items()):
        ref = _keyed_ref(ref_key, "c", gid_value)
        if gid_value in links:
            code = "linked"
        elif gid_value in claim_gids:
            code = "linked_claim_only"
        elif gid_value in open_writes:
            code = "open_write"
        elif gid_value in unlinked_gids:
            code = "claim_unlinked_not_linked"  # fix round 2: staff unlinked it in the member core; a person decides
        elif REGISTRATION_TAG not in item.tags:
            code = "legacy_not_linked"  # never created by a tool: a person decides
        else:
            pre = backfill_precheck(item, config.shopify_cutover_utc)
            if pre == "backfill_signed_up_after_cutover":
                code = "after_cutover_not_linked"  # the live sync's own (pending or held there)
            elif pre is not None:
                code = pre
            else:
                code = held.get(ref, "gap")
        classes[code] = classes.get(code, 0) + 1
        if code not in ("linked", "linked_claim_only", "gap", "after_cutover_not_linked"):
            listed += 1
            if len(refs) < COVERAGE_MAX_REFS:
                refs.append({"ref": ref, "code": code})
    member_classes: dict[str, int] = {}
    sync_by_key = {key: gid_value for gid_value, key in links.items()}
    for key in set(sync_by_key) | set(claim_keys):
        if key in sync_by_key and key in claim_keys:
            code = "linked_both" if sync_by_key[key] == claim_keys[key] else "linked_conflict"
        elif key in sync_by_key:
            code = "linked_sync_only"
        else:
            code = "linked_claim_only"
        member_classes[code] = member_classes.get(code, 0) + 1
    customer_conflicts = sum(1 for gid_value, key in links.items() if gid_value in claim_gids and claim_gids[gid_value] != key)
    person = sum(n for code, n in classes.items() if code not in ("linked", "linked_claim_only", "gap", "after_cutover_not_linked", "open_write"))
    return {
        "schema": COVERAGE_SCHEMA, "status": "ok", "autocount_book": config.autocount_book,
        "member_core_ledger": "read" if claims is not None else "not_configured",
        "shopify": {"tagged_total": len(customers), "classes": dict(sorted(classes.items())), "gap": classes.get("gap", 0),
                    "after_cutover_not_linked": classes.get("after_cutover_not_linked", 0), "needs_person": person},
        "ac2": {"linked_members_total": len(set(sync_by_key) | set(claim_keys)), "classes": dict(sorted(member_classes.items())),
                "customers_linked_to_two_members": customer_conflicts,
                # No AutoCount session: the book's member totals per type are in
                # the daily summary email (its read-only counts).
                "members_in_book": "not_read"},
        "shopify_pending": dict(sorted(pending.items())),
        "complete": classes.get("gap", 0) == 0 and classes.get("after_cutover_not_linked", 0) == 0 and classes.get("open_write", 0) == 0
                    and classes.get("claim_unlinked_not_linked", 0) == 0 and member_classes.get("linked_conflict", 0) == 0 and customer_conflicts == 0,
        "refs": refs,
        # Fix round 1 (G2 LATER 1): true when more customers have a listed
        # class than refs holds (the person list is then not the whole list).
        "refs_truncated": listed > len(refs),
    }


def build_read_only_shopify(config: SyncConfig, root: Path, *, environ: Mapping[str, str]) -> SyncShopifyClient:
    """The sync's Shopify client with every write refused: the session is
    checked against the scopes the config expects, then its one mutation is
    switched off for the rest of the process."""

    import xb_shopify_poller as poller

    transport = direct.UrllibTransport()
    shopify = SyncShopifyClient(
        create_enabled=config.shopify_create_enabled, shop_domain=config.shopify.shop_domain, api_version=config.shopify.api_version,
        token_provider=poller.token_provider(config.shopify, environ, transport, dl.utc_now, root=root), transport=transport,
        timeout=config.http_timeout_seconds,
    )
    shopify.verify_session(store_gid=config.shopify.store_gid)
    shopify.create_enabled = False
    return shopify


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
        env = load_ac2_credential(config.ac2_credential_target, refused_user_ids=config.ac2_refused_user_ids).child_environment(environ)
    elif config.autocount_book == LIVE_BOOK:
        raise dl.DirectLocalError("ac2_credential_target_unconfigured")  # parse_config refuses this first
    ac2 = SyncBridgeClient(dl.default_bridge_command(), autocount_book=config.autocount_book, claims=ledger.root / "claims", env=env)
    return SyncEngine(ledger, config, shopify=shopify, ac2=ac2)


DIGEST_SCHEMA = "xb.directlocal.member_sync.digest.v1"
DIGEST_MAX_HOURS = 720
DIGEST_MAX_REFS = 200
_DIGEST_REF_RE = re.compile(r"^[cm]-[0-9a-f]{1,64}$")
_DIGEST_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
DIGEST_MAX_LATENCY_SECONDS = 30 * 24 * 3600


def _nearest_rank(ordered: list[int], percent: int) -> int | None:
    """Nearest-rank percentile of an ascending list (None when empty)."""

    if not ordered:
        return None
    return ordered[max(-(-len(ordered) * percent // 100), 1) - 1]


BF2_DIGEST_CODES = ("shopify_customer_created", "shopify_create_uncertain", "shopify_create_rejected")


def _digest_backfill_members(conn: sqlite3.Connection, runs: list, *, since: str, now: datetime, per_hour: Mapping[str, int]) -> dict[str, Any] | None:
    """#155 BF2b (design 2.12): the ``backfill-members`` calls (run ids
    ``bf2-``) of the period, apart from the live runs, for the daily
    summary: calls, their create outcomes (from each call's stored counts),
    and their writes. A write row does not name its run, so a write belongs
    to the call whose time span holds its ``created_at`` (from the call's
    start to its end, or to the next run's start, or now, for a call that
    did not finish): runs never overlap (they share the ledger lock). None
    when no call ran in the period."""

    starts = [dl.parse_created_at(row["started_at"]) for row in runs]
    spans = []
    outcomes = {code: 0 for code in BF2_DIGEST_CODES}
    for index, row in enumerate(runs):
        if not str(row["run_id"]).startswith(BF2_RUN_PREFIX) or starts[index] is None:
            continue
        finished = dl.parse_created_at(row["finished_at"]) if row["finished_at"] else None
        later = [s for s in starts[index + 1:] if s is not None]
        spans.append((starts[index], finished or (later[0] if later else now)))
        try:
            stored = json.loads(row["summary"] or "{}")
        except (json.JSONDecodeError, RecursionError):
            stored = {}
        counts = stored.get("counts") if isinstance(stored, dict) and isinstance(stored.get("counts"), dict) else {}
        for code in BF2_DIGEST_CODES:
            number = counts.get(code)
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                outcomes[code] += number
    if not spans:
        return None
    per_call = [0] * len(spans)
    backfill_hour: dict[str, int] = {}
    for (created,) in conn.execute("SELECT created_at FROM writes WHERE created_at >= ?", (since,)):
        moment = dl.parse_created_at(created)
        if moment is None:
            continue
        for position, (start, end) in enumerate(spans):
            if start <= moment <= end:
                per_call[position] += 1
                hour = moment.astimezone(SGT).strftime("%Y-%m-%dT%H")
                backfill_hour[hour] = backfill_hour.get(hour, 0) + 1
                break
    return {
        "calls": len(spans), "creates": outcomes["shopify_customer_created"], "uncertain": outcomes["shopify_create_uncertain"],
        "rejected": outcomes["shopify_create_rejected"], "writes": sum(per_call), "writes_per_call_max": max(per_call),
        "writes_per_hour_max": max(backfill_hour.values(), default=0),
        "live_writes_per_hour_max": max((n - backfill_hour.get(hour, 0) for hour, n in per_hour.items()), default=0),
    }


def ledger_digest(root: Path, *, hours: int, now: datetime, config: SyncConfig) -> dict[str, Any]:
    """Go-live S7: the read-only daily digest. Opens the ledger read-only
    (no lock, no migration, no write), reads the ``runs`` of the last
    ``hours`` and the open-work totals. Output: run counts and durations,
    counts per code, alerts per code with keyed refs and masked MemberNos
    only (what each run already printed), never a name, phone, email,
    customer GID or full MemberNo."""

    path = Path(root) / LEDGER_FILE
    if not path.is_file():
        raise dl.DirectLocalError("sync_ledger_missing")
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        raise dl.DirectLocalError("sync_ledger_unreadable") from None
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA user_version").fetchone()[0] not in (2, SCHEMA_VERSION):
            raise dl.DirectLocalError("sync_ledger_schema_unsupported")
        meta = conn.execute("SELECT autocount_book, store_gid, shop_domain FROM meta WHERE id = 1").fetchone()
        if meta is None:
            raise dl.DirectLocalError("sync_ledger_not_initialised")
        check_binding(meta, config)
        since = dl.ts(now - timedelta(hours=hours))
        runs = conn.execute("SELECT run_id, started_at, finished_at, summary FROM runs WHERE started_at >= ? ORDER BY started_at", (since,)).fetchall()
        counts: dict[str, int] = {}
        alerts: dict[str, int] = {}
        refs: list[dict[str, str]] = []
        durations: list[int] = []
        latencies: list[int] = []
        unfinished = 0
        for row in runs:
            started, finished = dl.parse_created_at(row["started_at"]), dl.parse_created_at(row["finished_at"]) if row["finished_at"] else None
            if finished is None or started is None:
                unfinished += 1  # refused or stopped part-way (exit 2 or killed)
                continue
            durations.append(int((finished - started).total_seconds()))
            try:
                stored = json.loads(row["summary"] or "{}")
            except (json.JSONDecodeError, RecursionError):
                stored = {}
            run_counts = stored.get("counts", stored) if isinstance(stored, dict) else {}
            for code, number in (run_counts.items() if isinstance(run_counts, dict) else ()):
                if isinstance(code, str) and _DIGEST_CODE_RE.fullmatch(code) and isinstance(number, int) and not isinstance(number, bool):
                    counts[code] = counts.get(code, 0) + number
            stored_latencies = stored.get("create_latency_seconds") if isinstance(stored, dict) else None
            for seconds in (stored_latencies if isinstance(stored_latencies, list) else []):
                if isinstance(seconds, int) and not isinstance(seconds, bool) and 0 <= seconds <= DIGEST_MAX_LATENCY_SECONDS:
                    latencies.append(seconds)
            run_alerts = [code for code in stored.get("alerts", []) if isinstance(code, str) and _DIGEST_CODE_RE.fullmatch(code)] if isinstance(stored, dict) else []
            for code in run_alerts:
                alerts[code] = alerts.get(code, 0) + 1
            for entry in (stored.get("refs", []) if isinstance(stored, dict) else []):
                # Only the refs of alert codes, re-checked field by field.
                if not isinstance(entry, dict) or entry.get("code") not in run_alerts or len(refs) >= DIGEST_MAX_REFS:
                    continue
                ref = entry.get("ref")
                if not isinstance(ref, str) or not _DIGEST_REF_RE.fullmatch(ref):
                    continue
                item = {"ref": ref, "code": entry["code"]}
                masked = entry.get("member_no_masked")
                if isinstance(masked, str) and re.fullmatch(r"\*{3,}[^*]{1,3}", masked):
                    item["member_no_masked"] = masked
                refs.append(item)
        durations.sort()
        latencies.sort()
        # #155 P1: writes per Singapore clock hour, both directions, any state
        # (what the caps count), from the ledger's own rows.
        per_hour: dict[str, int] = {}
        for (created,) in conn.execute("SELECT created_at FROM writes WHERE created_at >= ?", (since,)):
            moment = dl.parse_created_at(created)
            if moment is not None:
                hour = moment.astimezone(SGT).strftime("%Y-%m-%dT%H")
                per_hour[hour] = per_hour.get(hour, 0) + 1

        def total(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])

        import xb_member_sync_probe as probe  # the probe module reads only its status file here

        backfill = _digest_backfill_members(conn, runs, since=since, now=now, per_hour=per_hour)
        return {
            "schema": DIGEST_SCHEMA, "status": "ok", "autocount_book": meta["autocount_book"], "hours": hours,
            "runs": {"completed": len(durations), "unfinished": unfinished,
                     "duration_seconds_max": durations[-1] if durations else None,
                     "duration_seconds_median": durations[len(durations) // 2] if durations else None},
            "counts": dict(sorted(counts.items())), "alerts": dict(sorted(alerts.items())), "alert_refs": refs,
            "open": {
                "shopify_pending": total("SELECT COUNT(*) FROM shopify_pending WHERE state = 'PENDING'"),
                "ac2_pending": total("SELECT COUNT(*) FROM ac2_pending WHERE state = 'PENDING'"),
                "ac2_pending_would_create": total("SELECT COUNT(*) FROM ac2_pending WHERE state = 'PENDING' AND code = 'would_create'"),
                "uncertain_writes": total("SELECT COUNT(*) FROM writes WHERE state = 'UNCERTAIN'"),
                "links": total("SELECT COUNT(*) FROM links"),
            },
            # #155 P1 / P2 (G0 r2 monitoring): latency is recorded only while
            # immediate_create_enabled is on; the probe figures come from its
            # status file (counts per Singapore day), null when absent.
            "create_latency_seconds": {"count": len(latencies), "p50": _nearest_rank(latencies, 50), "p95": _nearest_rank(latencies, 95),
                                       "max": latencies[-1] if latencies else None},
            "creates": {"ac2_member_created": counts.get("ac2_member_created", 0), "created_immediately": counts.get("created_immediately", 0),
                        "created_after_wait": counts.get("created_after_wait", 0), "writes_per_hour_max": max(per_hour.values(), default=0),
                        "writes_per_hour": dict(sorted(per_hour.items()))},
            "caps": {"create_rate_capped": counts.get("create_rate_capped", 0), "create_rate_capped_alerts": alerts.get("create_rate_capped", 0)},
            "probe": probe.probe_digest(Path(root), since=now - timedelta(hours=hours)),
            # #155 BF2b: only when a backfill-members call ran in the period,
            # so a digest without one is exactly as before.
            **({"backfill_members": backfill} if backfill is not None else {}),
        }
    except sqlite3.Error:
        raise dl.DirectLocalError("sync_ledger_unreadable") from None
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#155 phase 1 two-way member create sync")
    parser.add_argument("command", choices=("check-config", "init", "run", "status", "check-credential", "initial-link", "digest", "probe",
                                            "backfill-registrations", "coverage", "backfill-members"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true", help="initial-link (needs initial_link_enabled), backfill-registrations (needs "
                        "backfill_registrations_enabled) or backfill-members (needs backfill_members_enabled and "
                        "shopify_create_enabled): write; default is a dry run")
    parser.add_argument("--direction", default="both", choices=("both", "shopify_to_ac2", "ac2_to_shopify"))
    parser.add_argument("--hours", type=int, default=24, help="digest only: the last N hours of runs (1-720)")
    parser.add_argument("--max", type=int, default=1, dest="max_creates",
                        help="backfill-registrations / backfill-members only: members or customers created in this call (1 to max_writes_per_run)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check-config":
            _emit({"status": "ok", **config.summary()})
            return 0
        if args.command == "check-credential":
            if config.ac2_credential_target is None:
                raise dl.DirectLocalError("ac2_credential_target_unconfigured")
            credential = load_ac2_credential(config.ac2_credential_target, refused_user_ids=config.ac2_refused_user_ids)
            _emit({"status": "ok", "user_id": mask_last3(credential.user_id), "secret_present": True})
            return 0
        if args.command == "digest":
            # Read-only: no lock, no migration, no login (go-live S7).
            if not 1 <= args.hours <= DIGEST_MAX_HOURS:
                raise dl.DirectLocalError("digest_hours_invalid")
            root = dl.validate_state_root(config.state_root)
            dl.verify_private_state(root)
            _emit(ledger_digest(root, hours=args.hours, now=dl.utc_now(), config=config))
            return 0
        if args.command == "probe":
            # #155 P2: Shopify read only; the ledger read-only with no lock
            # (as digest); never AutoCount. It may start \XB\XB-MemberSync.
            import xb_member_sync_probe as probe

            root = dl.validate_state_root(config.state_root)
            dl.verify_private_state(root)
            result = probe.run_probe(config, root, environ=os.environ)
            _emit(result)
            return 3 if result["decision"] == "wake_failed" else 0  # 3: an operator must look
        if args.command == "initial-link" and args.apply and not config.initial_link_enabled:
            raise dl.DirectLocalError("initial_link_disabled")  # before any login or read
        if args.command == "coverage":
            # #155 BF1: read-only everywhere, as digest (no lock, no migration,
            # no AutoCount session); Shopify reads only.
            root = dl.validate_state_root(config.state_root)
            dl.verify_private_state(root)
            claims = load_claim_links(config)
            _emit(coverage_report(root, config, shopify=build_read_only_shopify(config, root, environ=os.environ), claims=claims))
            return 0
        if args.command == "backfill-registrations":
            # #155 BF1: both refusals before any login, read or lock.
            if args.apply and not config.backfill_registrations_enabled:
                raise dl.DirectLocalError("backfill_registrations_disabled")
            if not 1 <= args.max_creates <= config.max_writes_per_run:
                raise dl.DirectLocalError("backfill_max_invalid")
        if args.command == "backfill-members":
            # #155 BF2a: every refusal before any login, read or lock.
            if args.apply and not config.backfill_members_enabled:
                raise dl.DirectLocalError("backfill_members_disabled")
            if args.apply and not config.shopify_create_enabled:
                raise dl.DirectLocalError("shopify_create_disabled")
            if not 1 <= args.max_creates <= config.backfill_members_call_cap:
                raise dl.DirectLocalError("backfill_members_max_invalid")
        root = _state_root(config)
        ledger = SyncLedger(root)
        try:
            if args.command == "init":
                _emit(ledger.init_meta(autocount_book=config.autocount_book, store_gid=config.shopify.store_gid, shop_domain=config.shopify.shop_domain))
            elif args.command == "status":
                check_binding(ledger.meta(), config)  # go-live L4
                counts = {name: ledger.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("links", "writes", "ac2_pending", "shopify_pending")}
                _emit({"status": "ok", "counts": counts, "uncertain_writes": ledger.conn.execute("SELECT COUNT(*) FROM writes WHERE state = 'UNCERTAIN'").fetchone()[0]})
            elif args.command == "initial-link":
                result = build_real_engine(config, ledger, environ=os.environ).initial_link(apply=args.apply)
                _emit(result)
                if result["alerts"]:
                    return 3
            elif args.command == "backfill-registrations":
                claims = load_claim_links(config)
                engine = build_real_engine(config, ledger, environ=os.environ)
                engine.shopify.create_enabled = False  # the backfill never writes to Shopify
                result = engine.backfill_registrations(apply=args.apply, max_creates=args.max_creates, claims=claims)
                _emit(result)
                if result["stopped"] in ("backfill_create_not_written", "backfill_create_uncertain", "backfill_read_incomplete"):
                    return 3  # an operator must look before the next call
            elif args.command == "backfill-members":
                claims = load_claim_links(config)
                engine = build_real_engine(config, ledger, environ=os.environ)
                if not args.apply:
                    engine.shopify.create_enabled = False  # a dry run never writes to Shopify
                result = engine.backfill_members(apply=args.apply, max_creates=args.max_creates, claims=claims)
                _emit(result)
                if result["stopped"] in BF2_OPERATOR_STOPS:
                    return 3  # an operator must look before the next call
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
