"""#268 member core: one online order of S$300 or more makes an AutoCount
``MEMBER`` with a 2-year ``ExpiryDate``, plus Shopify -> AC2 claim links.

Scope: owner, 10 Nov 2026 ("only single transaction >= $300 qualify MEMBER",
"membership has expiry date of 2 years", "need auto linking from shopify to
AC2"), as cut by the Controller's G1 brief from the G0 full-journey design
(sections 2.1-2.3, 3 and 10). Runs on the AutoCount VM from its own clone,
state root and config. Nothing runs on merge.

What one run does (``observe`` or ``apply``)
--------------------------------------------
1. Reads paid Shopify orders updated since its watermark (read only, its own
   Shopify app; the token's scopes must be exactly ``read_customers`` and
   ``read_orders``). Bounded pages, backoff on 429, and the watermark moves
   only after a clean read of every page; the overlap is replay-safe.
2. Records each order as one ``receipts`` row (forever; replay updates the
   same row): the before-discount line total incl. GST (gift-card purchase
   lines count, shipping does not), minus refunded and removed units, minus
   the amount paid by gift card, never below zero (config).
3. Decides per receipt: the customer carries the #155 sign-up marker (tag
   ``member-mg`` and the ``membership.start_date`` metafield time) and the
   order's Singapore date is on or after the sign-up date (or the same
   Singapore day, config). It finds the AC2 member through the #155 ledger's
   links (read only, never copied) or the tool's own claim links.
4. Reads that AC2 member and decides: ``GUEST`` -> ``MEMBER`` with
   ``ExpiryDate`` = order date + 24 months (``would_upgrade``), or a later
   ``ExpiryDate`` for a ``MEMBER`` (``would_extend``). ``Default``, held and
   every other type are counted and never written.
5. ``apply`` only (switch ``benefits_write_enabled``, per-run cap): one
   ``SaveMember`` per decision through profile ``benefit_p1`` of the #268
   write path (``ac2_member_benefits_bridge.ps1``): INTENT before the child,
   DONE / SKIPPED / NOT_WRITTEN / UNCERTAIN / HELD after; UNCERTAIN is
   resolved by read-back only (``resolve``), never re-sent.

Never: any Shopify write (no write scope, no mutation in this module), a
discount, a tag, the POS, a refund revoke, an expiry job, any ``MEMBER`` ->
``GUEST`` move, an earlier ``ExpiryDate``, a change to the #155 sync.

Privacy: the ledger (private state root on the VM) keeps order and customer
GIDs, MemberNos and the payment gateway names of each order. Output carries
codes, counts, keyed refs and MemberNos masked to the last 3 characters only;
a gateway name is shown only when it is a plain code word, else as "other".
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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.append(str(_HERE))

import xb_directlocal as dl  # noqa: E402
import xb_member_sync as sync  # noqa: E402
import xb_member_type_move as tm  # noqa: E402
import xb_shopify_direct as direct  # noqa: E402

CONFIG_SCHEMA = "xb.directlocal.member_benefits.config.v1"
LEDGER_FILE = "member_benefits.sqlite3"
LOCK_FILE = "member_benefits.lock"
STOP_FILE = "STOP"
TOOL = "xb268_member_benefits"
SCHEMA_VERSION = 2  # slice B1: ledger v2 (claim unlinks, write releases, rule fingerprint); no migration
PROFILE = "benefit_p1"
BRIDGE_SCRIPT = dl.REPO_ROOT / "scripts" / "ac2_member_benefits_bridge.ps1"

GUEST = "GUEST"
MEMBER = "MEMBER"
SCOPE_TYPES = (MEMBER, GUEST)  # the only types ever read for a write
TARGET_TYPE = MEMBER  # the only type ever written
FROM_TYPE_BY_KIND = {"upgrade": GUEST, "extend": MEMBER}
SGT = sync.SGT
REGISTRATION_TAG = sync.REGISTRATION_TAG  # the #155 sign-up marker
REQUIRED_SCOPES = frozenset({"read_customers", "read_orders"})
CURRENCY = "SGD"
EXPIRY_MONTHS = 24
# Page sizes of ORDERS_QUERY. Shopify refuses a query whose requested cost is
# over 1,000 points before it runs (shopify.dev, rate limits). On the strict
# reading (every selected object counted once for each row of every connection
# and list above it) the query costs
#     2 + 5 x (11 + 6 x 20 + 3 x 3 + 3 x 10) = 852 points
# (slice B1: + 1 per order for the transactionsCount object),
# under the 900 this tool allows itself. The sum is written out line by line
# in the runbook (section 9) and pinned by OrdersQueryCostTests: work it out
# again before changing a size or the query text.
SHOPIFY_ORDER_PAGE = 5
SHOPIFY_LINE_PAGE = 20  # an order with more line items is amount_unverifiable
SHOPIFY_SHIPPING_PAGE = 3
# Order.transactions is a plain list cut to this size (no pageInfo), so a
# list that comes back full cannot be shown to be complete. Slice B1 (M1):
# Order.transactionsCount (a Count: count Int!, precision CountPrecision!,
# shopify.dev 2026-10) must be EXACT and equal to the list's length.
SHOPIFY_PAYMENT_PAGE = 10
COUNT_EXACT = "EXACT"  # CountPrecision: AT_LEAST or EXACT (shopify.dev 2026-10)
# OrderTransactionKind, all eight values (shopify.dev 2026-10). Slice B1 (M3):
# any other kind on a gift card gateway makes the payments unreadable.
PAYMENT_KINDS = frozenset({"AUTHORIZATION", "CAPTURE", "CHANGE", "EMV_AUTHORIZATION", "REFUND", "SALE", "SUGGESTED_REFUND", "VOID"})
# The gift-card-paid amount: transactions that took money (a sale, or the
# capture of an authorisation) and succeeded. A refund back to a gift card is
# never added back.
GIFT_CARD_PAYMENT_KINDS = frozenset({"SALE", "CAPTURE"})
PAYMENT_SUCCESS = "SUCCESS"
PAYMENT_FAILED_STATUSES = frozenset({"FAILURE", "ERROR"})  # took no money: ignored
MAX_GIFT_CARD_GATEWAYS = 5
PAYMENTS_UNREADABLE = "payments_unreadable"
WATERMARK = "orders_updated_at"
WATERMARK_MARGIN = timedelta(minutes=15)  # search-index lag overlap, as #155
RESUME_MARK = "orders_resume"  # G2 B3: {since, cursor} of a read that ended at the page cap
SHOPIFY_ORDER_WINDOW = timedelta(days=60)  # read_orders sees orders created in the last 60 days
MAX_UPGRADES_PER_RUN_LIMIT = 20
OBSERVE_REREAD = timedelta(hours=24)
INDEX_MAX_LIMIT = 1000
TYPE_MOVE_LOCK_FILE = tm.LOCK_FILE

PAID_STATUSES = frozenset({"PAID", "PARTIALLY_REFUNDED", "REFUNDED"})
# Receipts whose decision is settled: replay only records refunds on them.
DECIDED_STATES = frozenset({"applied", "no_change", "not_in_scope", "held"})
RECEIPT_STATES = (
    "ignored_unpaid", "ignored_test", "void", "before_start", "no_customer", "amount_unverifiable", "below_threshold",
    "not_signed_up", "pending_signup", "waiting_member", "ready", "applied", "no_change", "not_in_scope", "held",
)
# Re-checked on every run until they settle or their wait ends.
PENDING_STATES = ("pending_signup", "waiting_member", "ready")
# G2 B2: claim outcomes that settle a receipt as ``held`` (terminal, one
# alert) instead of waiting for ever. Such a receipt is re-opened (back to
# ``ready``) only when a #155 link or a claim link for its customer appears.
CLAIM_HOLD_CODES = frozenset({"claim_name_mismatch", "claim_phone_not_unique", "claim_link_conflict", "claim_memberno_conflict",
                              "claim_member_not_in_scope", "claim_no_phone", "claim_pair_unlinked"})
# G2 B1: a refund by money that no refunded or removed unit explains.
REFUND_UNATTRIBUTED = "refund_unattributed"
# G2 B5: a claim-held receipt whose order stopped qualifying while it was
# held (unit refund, cancellation, any other reason). Never re-opened.
ORDER_NO_LONGER_QUALIFIES = "order_no_longer_qualifies"
# Slice B1: the only codes a held claim receipt may settle to (pinned in the
# receipts_decided_code trigger). ``order_past_read_window`` is used from
# slice B2 on; it is listed now so B2 needs no schema change.
ORDER_PAST_READ_WINDOW = "order_past_read_window"
# Slice B1 (G0 1.5): the terminal code of a ready receipt whose HELD write was
# released with the finding not_as_intended (staff upgrade by hand if due).
HELD_WRITE_RELEASED = "held_write_released"
RELEASE_FINDINGS = ("as_intended", "not_as_intended")
UNLINK_REASONS = ("wrong_person", "customers_merged", "member_renumbered", "other")  # as the claim_unlinks CHECK
_ORDER_REF_RE = re.compile(r"^o-[0-9a-f]{20}\Z")
_CUSTOMER_REF_RE = re.compile(r"^c-[0-9a-f]{20}\Z")
_LAST3_RE = re.compile(r"^[A-Za-z0-9._/-]{1,3}\Z")  # the last 3 characters of a MemberNo (dl.MEMBER_NO_RE)
CLAIM_SETTLE_CODES = frozenset({ORDER_NO_LONGER_QUALIFIES, REFUND_UNATTRIBUTED, ORDER_PAST_READ_WINDOW})
# Slice B2 (B2-1, B2-2): alerts, each once per order (review subjects
# ``moved:<order>`` and ``settled:<order>``; no schema change).
ORDER_CUSTOMER_CHANGED = "order_customer_changed_after_upgrade"
CLAIM_HOLD_SETTLED_UNCLEAR = "claim_hold_settled_unclear"
RECEIPT_PAST_READ_WINDOW = "receipt_past_read_window"  # B2-4: review subject aged:<order>
# The replayed states that settle a claim hold silently: positive facts only
# (a cancellation; an amount measured below the threshold). A move or a
# removed customer is the other positive fact (checked apart).
CLAIM_SETTLE_POSITIVE_STATES = frozenset({"void", "below_threshold"})
# Slice B1: the version of the amount rule (goods_amount, gift_card_paid,
# qualifying_amount, qualifies, money_refund_unattributed and the order
# parsing of amounts). Any change to one of them raises it; it is part of the
# rule fingerprint kept in the ledger (BenefitsConfig.rule_fingerprint).
AMOUNT_RULE_VERSION = 2  # 2: slice B1 (M1 transactionsCount, M3 unknown kind, M4 gateway trimmed)

_LOCAL_STORED_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}$")
_NEW_EXPIRY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T00:00:00$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COLUMN_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_ORDER_GID_RE = re.compile(r"^gid://shopify/Order/[1-9][0-9]{0,19}$")
_CUSTOMER_GID_RE = re.compile(r"^gid://shopify/Customer/[1-9][0-9]{0,19}$")
_LINE_GID_RE = re.compile(r"^gid://shopify/LineItem/[1-9][0-9]{0,19}$")
_AMOUNT_RE = re.compile(r"^-?[0-9]{1,12}(?:\.[0-9]{1,6})?$")
_GATEWAY_RE = re.compile(r"^[a-z0-9_]{1,64}$")  # a gateway name as a plain code word

SAVE_SKIP_CODES = frozenset({"member_not_found", "type_not_in_scope_now", "type_changed_since_read", "changed_since_read", "expiry_not_later_now"})
BENEFIT_CHILD_CODES = frozenset({
    "benefit_request_invalid", "benefit_type_not_allowed", "benefit_expiry_not_later", "benefit_scan_failed", "benefit_member_read_failed",
    # type move helpers reused by the child
    "member_no_invalid",
})
CHILD_ERROR_CODES = dl.BRIDGE_CHILD_ERROR_CODES | BENEFIT_CHILD_CODES
REFUSALS_BEFORE_CLAIM = frozenset({"member_type_unknown", "production_book_not_authorised", "benefit_type_not_allowed",
                                   "benefit_request_invalid", "benefit_expiry_not_later"})

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

_CONFIG_REQUIRED = {"schema", "state_root", "autocount_book", "shopify", "orders_start_utc", "member_sync_state_root"}
_CONFIG_DEFAULTS: dict[str, Any] = {
    # Observe is the default: no AC2 save is reachable while this is false.
    "benefits_write_enabled": False,
    "max_upgrades_per_run": 3,
    "max_member_reads_per_run": 20,
    "max_customer_rechecks_per_run": 50,
    "write_deadline_seconds": 120,
    # The rule (owner 10 Nov 2026 and the Controller brief). The owner may
    # still adjust these before go-live; see the runbook.
    "qualify_threshold_cents": 30_000,
    "amount_before_discounts": True,
    "amount_include_tax": True,  # only true: line prices are read incl. GST (Order.taxesIncluded)
    "amount_exclude_shipping": True,
    # Owner, 10 Nov 2026: buying a gift card counts towards the S$300; the
    # part of an order paid with a gift card does not.
    "amount_exclude_gift_cards": False,
    "gift_card_gateways": ["gift_card"],  # OrderTransaction.gateway names, lower case
    "amount_count_sale_items": True,  # only true: an order line records no sale price
    "same_day_signup_counts": True,
    "waiting_member_alert_hours": 48,
    # G2 B2: a waiting_member receipt (no AC2 member yet) is re-checked at
    # most once per this many hours, unless a link for its customer appears.
    "waiting_member_recheck_hours": 6,
    "signup_wait_hours": 48,
    "shopify_max_pages": 20,  # x 5 orders a page: 100 orders a run, as before the page size was lowered
    "http_timeout_seconds": 30,
    # The #268 type move's state root: apply takes its lock, so the two tools
    # never write AutoCount members at the same time. Required for apply on
    # the live book.
    "type_move_state_root": None,
    "ac2_credential_target": None,
    "ac2_login_mode": None,
    "ac2_refused_user_ids": [],
}
_BOUNDS = {
    "max_upgrades_per_run": (1, MAX_UPGRADES_PER_RUN_LIMIT),
    "max_member_reads_per_run": (1, 100),
    "max_customer_rechecks_per_run": (1, 500),
    "write_deadline_seconds": (30, 600),
    "qualify_threshold_cents": (100, 10_000_000),
    "waiting_member_alert_hours": (1, 720),
    "waiting_member_recheck_hours": (1, 72),
    "signup_wait_hours": (1, 720),
    "shopify_max_pages": (1, 40),
    "http_timeout_seconds": (5, 60),
}
_ONLY_TRUE = ("amount_include_tax", "amount_count_sale_items")


@dataclass(frozen=True)
class BenefitsConfig:
    state_root: str
    autocount_book: str
    shopify: sync.ShopifyConnection
    orders_start_utc: datetime
    member_sync_state_root: str
    benefits_write_enabled: bool
    max_upgrades_per_run: int
    max_member_reads_per_run: int
    max_customer_rechecks_per_run: int
    write_deadline_seconds: int
    qualify_threshold_cents: int
    amount_before_discounts: bool
    amount_include_tax: bool
    amount_exclude_shipping: bool
    amount_exclude_gift_cards: bool
    gift_card_gateways: tuple[str, ...]
    amount_count_sale_items: bool
    same_day_signup_counts: bool
    waiting_member_alert_hours: int
    waiting_member_recheck_hours: int
    signup_wait_hours: int
    shopify_max_pages: int
    http_timeout_seconds: int
    type_move_state_root: str | None
    ac2_credential_target: str | None
    ac2_login_mode: str | None
    ac2_refused_user_ids: tuple[str, ...]

    def rule_fingerprint(self) -> str:
        """Slice B1: ``sha256:`` + hex of the canonical JSON of every setting
        that changes a measured amount or the threshold, plus
        ``AMOUNT_RULE_VERSION``. Kept in the ledger at ``init``. Not in it, on
        purpose: the sign-up settings and ``orders_start_utc`` (a replay
        decides undecided receipts again under them), caps, timeouts, roots."""

        value = {
            "amount_rule_version": AMOUNT_RULE_VERSION, "qualify_threshold_cents": self.qualify_threshold_cents,
            "amount_before_discounts": self.amount_before_discounts, "amount_include_tax": self.amount_include_tax,
            "amount_exclude_shipping": self.amount_exclude_shipping, "amount_exclude_gift_cards": self.amount_exclude_gift_cards,
            "gift_card_gateways": sorted(self.gift_card_gateways), "amount_count_sale_items": self.amount_count_sale_items,
        }
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    def summary(self) -> dict[str, Any]:
        """PII-free view for ``check-config``."""

        return {
            "autocount_book": self.autocount_book, "api_version": self.shopify.api_version, "required_scopes": sorted(REQUIRED_SCOPES),
            "orders_start_utc": dl.ts(self.orders_start_utc), "benefits_write_enabled": self.benefits_write_enabled,
            "max_upgrades_per_run": self.max_upgrades_per_run, "max_member_reads_per_run": self.max_member_reads_per_run,
            "qualify_threshold_cents": self.qualify_threshold_cents, "amount_before_discounts": self.amount_before_discounts,
            "amount_include_tax": self.amount_include_tax, "amount_exclude_shipping": self.amount_exclude_shipping,
            "amount_exclude_gift_cards": self.amount_exclude_gift_cards, "gift_card_gateways": list(self.gift_card_gateways),
            "amount_count_sale_items": self.amount_count_sale_items,
            "same_day_signup_counts": self.same_day_signup_counts, "expiry_months": EXPIRY_MONTHS, "target_type": TARGET_TYPE,
            "type_move_lock": self.type_move_state_root is not None,
            "ac2_credential": "credential_manager" if self.ac2_credential_target else "owner_console_environment",
            "ac2_login_mode": self.ac2_login_mode,
            "rule_fingerprint": self.rule_fingerprint(),  # slice B1: as kept in the ledger at init
        }


def _bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise dl.DirectLocalError(f"benefits_config_{key}_invalid")
    return value


def parse_config(value: Any) -> BenefitsConfig:
    if not isinstance(value, dict):
        raise dl.DirectLocalError("benefits_config_invalid")
    if value.get("schema") in (sync.CONFIG_SCHEMA, tm.CONFIG_SCHEMA):
        raise dl.DirectLocalError("benefits_config_is_other_tool")
    keys = set(value)
    if not _CONFIG_REQUIRED <= keys or not keys <= _CONFIG_REQUIRED | set(_CONFIG_DEFAULTS):
        raise dl.DirectLocalError("benefits_config_keys_invalid")
    if value["schema"] != CONFIG_SCHEMA:
        raise dl.DirectLocalError("benefits_config_schema_invalid")
    merged = {**_CONFIG_DEFAULTS, **value}
    book = sync.check_book(merged["autocount_book"])
    live = book == sync.LIVE_BOOK
    for key in ("state_root", "member_sync_state_root"):
        if not isinstance(merged[key], str) or not merged[key]:
            raise dl.DirectLocalError(f"benefits_config_{key}_invalid")
    start = dl.parse_created_at(merged["orders_start_utc"]) if isinstance(merged["orders_start_utc"], str) else None
    if start is None:
        raise dl.DirectLocalError("benefits_config_orders_start_utc_invalid")
    numbers = {}
    for key, (low, high) in _BOUNDS.items():
        number = merged[key]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise dl.DirectLocalError(f"benefits_config_{key}_invalid")
        numbers[key] = number
    try:
        shopify = sync._parse_shopify(merged["shopify"], numbers["http_timeout_seconds"])
    except dl.DirectLocalError as exc:
        raise dl.DirectLocalError(exc.code.replace("sync_config_", "benefits_config_", 1)) from None
    flags = {key: _bool(merged[key], key) for key in (
        "benefits_write_enabled", "amount_before_discounts", "amount_include_tax", "amount_exclude_shipping",
        "amount_exclude_gift_cards", "amount_count_sale_items", "same_day_signup_counts")}
    for key in _ONLY_TRUE:
        if not flags[key]:
            # Not computable from an order today; see the runbook.
            raise dl.DirectLocalError(f"benefits_config_{key}_unsupported")
    gateways = merged["gift_card_gateways"]
    if (not isinstance(gateways, list) or not 1 <= len(gateways) <= MAX_GIFT_CARD_GATEWAYS
            or any(not isinstance(name, str) or not _GATEWAY_RE.fullmatch(name) for name in gateways)):
        raise dl.DirectLocalError("benefits_config_gift_card_gateways_invalid")
    move_root = merged["type_move_state_root"]
    if move_root is not None and (not isinstance(move_root, str) or not move_root):
        raise dl.DirectLocalError("benefits_config_type_move_state_root_invalid")
    if live and flags["benefits_write_enabled"] and move_root is None:
        raise dl.DirectLocalError("benefits_config_type_move_state_root_required")
    target = merged["ac2_credential_target"]
    if target is not None and (not isinstance(target, str) or not sync.CREDENTIAL_TARGET_RE.fullmatch(target)):
        raise dl.DirectLocalError("benefits_config_ac2_credential_target_invalid")
    if live and target != sync.LIVE_CREDENTIAL_TARGET:
        raise dl.DirectLocalError("benefits_config_ac2_credential_target_live_required")
    if not live and target is not None and target.strip().casefold() == sync.LIVE_CREDENTIAL_TARGET.casefold():
        raise dl.DirectLocalError("benefits_config_ac2_credential_target_live_only")
    login_mode = merged["ac2_login_mode"]
    if login_mode is not None and login_mode not in sync.AC2_LOGIN_MODES:
        raise dl.DirectLocalError("benefits_config_ac2_login_mode_invalid")
    if live and login_mode is None:
        raise dl.DirectLocalError("benefits_config_ac2_login_mode_live_required")
    refused = merged["ac2_refused_user_ids"]
    if not isinstance(refused, list) or len(refused) > 10 or any(not isinstance(u, str) or not sync.AC2_USER_ID_RE.fullmatch(u) for u in refused):
        raise dl.DirectLocalError("benefits_config_ac2_refused_user_ids_invalid")
    if login_mode == "owner_shared" and refused:
        raise dl.DirectLocalError("benefits_config_ac2_refused_user_ids_owner_shared_refused")
    if live and login_mode == "dedicated" and not refused:
        raise dl.DirectLocalError("benefits_config_ac2_refused_user_ids_live_required")
    return BenefitsConfig(
        state_root=merged["state_root"], autocount_book=book, shopify=shopify, orders_start_utc=start,
        member_sync_state_root=merged["member_sync_state_root"], type_move_state_root=move_root,
        ac2_credential_target=target, ac2_login_mode=login_mode, ac2_refused_user_ids=tuple(refused),
        gift_card_gateways=tuple(gateways), **flags, **numbers,
    )


def load_config(path: str | Path) -> BenefitsConfig:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise dl.DirectLocalError("benefits_config_unreadable") from None
    if len(raw) > 65_536:
        raise dl.DirectLocalError("benefits_config_invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise dl.DirectLocalError("benefits_config_invalid") from None
    return parse_config(value)


def state_root(config: BenefitsConfig) -> Path:
    """The tool's own private root: never the #155 sync's, the type move's or
    the DirectLocal Core's."""

    root = dl.validate_state_root(config.state_root)
    for other in (sync.LEDGER_FILE, tm.LEDGER_FILE, "ledger.sqlite3"):
        if (root / other).exists():
            raise dl.DirectLocalError("benefits_state_root_shared")
    if Path(config.member_sync_state_root).resolve() == root.resolve():
        raise dl.DirectLocalError("benefits_state_root_shared")
    dl.verify_private_state(root)
    (root / "claims").mkdir(exist_ok=True)
    return root


# --------------------------------------------------------------------------
# Pure rules
# --------------------------------------------------------------------------

def money_cents(value: Any) -> int | None:
    """A Shopify ``Decimal`` amount in cents, or None when it is not a plain
    decimal with at most two non-zero places (never rounded)."""

    if not isinstance(value, str) or not _AMOUNT_RE.fullmatch(value):
        return None
    try:
        cents = Decimal(value) * 100
    except InvalidOperation:
        return None
    if cents != cents.to_integral_value():
        return None
    return int(cents)


def add_months(day: date, months: int) -> date:
    """``day`` plus whole months; a day the target month lacks becomes its
    last day (29 Feb + 24 months = 28 Feb)."""

    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    for candidate in (day.day, 30, 29, 28):
        try:
            return date(year, month, min(day.day, candidate))
        except ValueError:
            continue
    raise ValueError("add_months")


def sg_date(moment: datetime) -> date:
    return moment.astimezone(SGT).date()


def new_expiry_local(order_day: date) -> str:
    """The ``ExpiryDate`` the tool writes: the order's Singapore date + 24
    months, at 00:00 (a date; AutoCount stores Singapore local time)."""

    return add_months(order_day, EXPIRY_MONTHS).strftime("%Y-%m-%dT00:00:00")


def stored_expiry(value: Any) -> tuple[datetime | None, bool]:
    """``(expiry, readable)`` of the child's ExpiryDate text
    (``yyyy-MM-ddTHH:mm:ss.fff``); ``(None, True)`` for NULL."""

    if value is None:
        return None, True
    if not isinstance(value, str) or not _LOCAL_STORED_RE.fullmatch(value):
        return None, False
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f"), True
    except ValueError:
        return None, False


def is_later(new_local: str, expected_local: str | None) -> bool:
    """Strictly later than the current ExpiryDate (NULL: anything is later)."""

    if expected_local is None:
        return True
    current, readable = stored_expiry(expected_local)
    if not readable or current is None:
        return False
    return datetime.strptime(new_local, "%Y-%m-%dT%H:%M:%S") > current


@dataclass(frozen=True)
class OrderLine:
    quantity: int
    current_quantity: int
    is_gift_card: bool
    unit_cents: int
    discount_cents: int

    def __repr__(self) -> str:  # slice B2 (B2-6): never render order data
        return "OrderLine()"


@dataclass(frozen=True)
class Payment:
    """One ``OrderTransaction``. ``gateway`` is lower case ("" when Shopify
    gives none); ``cents`` is None when the amount is not SGD, negative or
    malformed."""

    gateway: str
    kind: str
    status: str
    cents: int | None

    def __repr__(self) -> str:  # slice B2 (B2-6): never render the gateway or an amount
        return "Payment()"


@dataclass(frozen=True)
class Customer:
    gid: str
    first_name: str | None
    last_name: str | None
    phone: str | None
    tags: tuple[str, ...]
    created_at: datetime | None
    registration_at: datetime | None

    def __repr__(self) -> str:  # never render customer data
        return "Customer()"


@dataclass(frozen=True)
class Order:
    gid: str
    created_at: datetime
    updated_at: datetime
    cancelled: bool
    test: bool
    financial_status: str | None
    taxes_included: bool
    currency: str | None
    customer: Customer | None
    lines: tuple[OrderLine, ...]
    lines_complete: bool
    shipping_cents: tuple[int, ...]
    shipping_complete: bool
    refunded_cents: int | None
    payments: tuple[Payment, ...]
    payments_complete: bool

    def __repr__(self) -> str:
        return "Order()"


def goods_amount(order: Order, config: BenefitsConfig) -> tuple[int, int] | None:
    """``(gross, kept)`` cents of the goods, or None when the order cannot be
    measured (taxes not included in its prices, a currency other than SGD,
    or more lines than one page). Gift-card purchase lines count (config).
    ``kept`` takes off refunded and removed units
    (``LineItem.currentQuantity``), so a refund seen before the upgrade is
    netted off; a money-only refund with no line is not."""

    if order.currency != CURRENCY or not order.lines_complete or (not config.amount_exclude_shipping and not order.shipping_complete):
        return None
    if config.amount_include_tax and not order.taxes_included:
        return None
    gross = net = 0
    for line in order.lines:
        if config.amount_exclude_gift_cards and line.is_gift_card:
            continue
        line_gross = line.unit_cents * line.quantity
        if not config.amount_before_discounts:
            line_gross -= line.discount_cents
        gross += line_gross
        net += line_gross * line.current_quantity // line.quantity if line.quantity else 0
    if not config.amount_exclude_shipping:
        shipping = sum(order.shipping_cents)
        gross += shipping
        net += shipping
    return gross, net


def gift_card_paid(order: Order, config: BenefitsConfig) -> int | None:
    """Cents paid by gift card: the sum of the successful ``SALE`` and
    ``CAPTURE`` transactions whose gateway is one of ``gift_card_gateways``.
    None when the payments cannot be read: the list is not shown complete
    (``payments_complete``), an amount is not SGD or is malformed, a gift
    card payment has a kind Shopify does not document, or a gift card payment
    has not settled (``PENDING``, ``AWAITING_RESPONSE``, ``UNKNOWN``). A failed one
    (``FAILURE``, ``ERROR``) took no money and is ignored, as is every other
    kind: a refund back to a gift card is never added back (the safe side)."""

    if not order.payments_complete:
        return None
    paid = 0
    for payment in order.payments:
        if payment.cents is None:
            return None
        if payment.gateway not in config.gift_card_gateways:
            continue
        if payment.kind not in PAYMENT_KINDS:
            return None  # slice B1 (M3): a kind Shopify does not document; never guessed
        if payment.kind not in GIFT_CARD_PAYMENT_KINDS:
            continue
        if payment.status == PAYMENT_SUCCESS:
            paid += payment.cents
        elif payment.status not in PAYMENT_FAILED_STATUSES:
            return None
    return paid


def qualifying_amount(order: Order, config: BenefitsConfig) -> tuple[int, int] | None:
    """``(gross, net)`` cents by the rule, or None when the order cannot be
    measured. ``net`` is the qualifying amount: the goods kept
    (``goods_amount``) minus everything paid by gift card
    (``gift_card_paid``), never below zero. A payment cannot be tied to a
    line, so the gift card amount comes off in full: this can only
    under-count."""

    goods, paid = goods_amount(order, config), gift_card_paid(order, config)
    if goods is None or paid is None:
        return None
    gross, kept = goods
    return gross, max(kept - paid, 0)


def qualifies(net_cents: int, config: BenefitsConfig) -> bool:
    """One order of S$300.00 or more (threshold in config)."""

    return net_cents >= config.qualify_threshold_cents


def refunded_unit_cents(order: Order) -> int:
    """The before-discount value of every refunded or removed unit
    (``quantity - currentQuantity``), gift-card lines included."""

    return sum(line.unit_cents * (line.quantity - line.current_quantity) for line in order.lines)


def money_refund_unattributed(order: Order) -> bool:
    """G2 B1: a refund by money that no refunded or removed unit explains:
    the order is fully refunded (``REFUNDED``), or ``totalRefundedSet`` is
    more than the value of its refunded or removed units. Such money can be
    an amount-only refund, a shipping refund or an adjustment, so it is never
    netted: before the decision the receipt is held, after it one alert."""

    if order.financial_status == "REFUNDED":
        return True
    return order.refunded_cents is not None and order.refunded_cents > refunded_unit_cents(order)


def signup_status(customer: Customer | None, order: Order, config: BenefitsConfig) -> str:
    """``signed_up``, ``pending`` (marker without its time yet, or sign-up
    not seen yet but the wait is still open) or ``not_signed_up``.

    The #155 marker: tag ``member-mg`` and the time of the
    ``membership.start_date`` metafield. The order counts when its Singapore
    date is on or after the sign-up's Singapore date; an order placed before
    the sign-up counts only when both fall on the same Singapore day
    (``same_day_signup_counts``)."""

    if customer is None or REGISTRATION_TAG not in customer.tags or customer.registration_at is None:
        return "not_signed_up"
    order_day, signup_day = sg_date(order.created_at), sg_date(customer.registration_at)
    if customer.registration_at <= order.created_at:
        return "signed_up"
    if order_day == signup_day and config.same_day_signup_counts:
        return "signed_up"
    return "not_signed_up"


@dataclass(frozen=True)
class MemberRead:
    found: bool
    member_no: str | None = None
    member_type: str | None = None
    name: str | None = None
    is_active: str | None = None
    expiry_local: str | None = None
    last_modified_local: str | None = None
    fields_digest: str | None = None

    def __repr__(self) -> str:
        return "MemberRead()"


@dataclass(frozen=True)
class Verdict:
    action: str  # upgrade | extend | no_change | not_in_scope | hold
    code: str
    new_expiry_local: str | None = None


def decide(member: MemberRead, order_day: date) -> Verdict:
    """The upgrade-only evaluator. AC2 decides the type: only ``GUEST`` and
    ``MEMBER`` are ever written; the expiry only ever moves later."""

    if not member.found:
        return Verdict("hold", "member_not_found")
    if member.member_type not in SCOPE_TYPES:
        return Verdict("not_in_scope", "member_not_in_scope")
    if member.is_active != "T":
        return Verdict("hold", "member_inactive")
    if not isinstance(member.last_modified_local, str) or not _LOCAL_STORED_RE.fullmatch(member.last_modified_local):
        return Verdict("hold", "last_modified_unreadable")
    current, readable = stored_expiry(member.expiry_local)
    if not readable:
        return Verdict("hold", "expiry_unreadable")
    new = new_expiry_local(order_day)
    later = is_later(new, member.expiry_local)
    if member.member_type == GUEST:
        if not later:
            return Verdict("hold", "guest_expiry_already_later")
        return Verdict("upgrade", "would_upgrade", new)
    if later:
        return Verdict("extend", "would_extend", new)
    return Verdict("no_change", "expiry_already_later")


def check_save_payload(payload: Mapping[str, Any]) -> None:
    """Python's copy of the child's write-boundary rules, run before the
    INTENT row: to MEMBER only, from GUEST (upgrade) or MEMBER (extend) only,
    ExpiryDate strictly later."""

    keys = {"kind", "member_no", "from_type", "to_type", "expected_last_modified_local", "expected_expiry_local", "new_expiry_local", "salt"}
    if set(payload) != keys or payload["kind"] not in FROM_TYPE_BY_KIND:
        raise dl.DirectLocalError("benefit_payload_invalid")
    if payload["to_type"] != TARGET_TYPE or payload["from_type"] != FROM_TYPE_BY_KIND[payload["kind"]]:
        raise dl.DirectLocalError("benefit_type_not_allowed")
    if not isinstance(payload["new_expiry_local"], str) or not _NEW_EXPIRY_RE.fullmatch(payload["new_expiry_local"]):
        raise dl.DirectLocalError("benefit_payload_invalid")
    if not is_later(payload["new_expiry_local"], payload["expected_expiry_local"]):
        raise dl.DirectLocalError("benefit_expiry_not_later")
    if not isinstance(payload["member_no"], str) or not dl.MEMBER_NO_RE.fullmatch(payload["member_no"]):
        raise dl.DirectLocalError("benefit_payload_invalid")


def mask(member_no: str | None) -> str:
    return sync.mask_last3(member_no)


def phone_forms(phone: Any) -> tuple[str | None, set[str]]:
    """The #155 phone rule: the phone's MemberNo digits and the MemberNo keys
    that are exactly this phone."""

    digits, status = sync.phone_member_no(phone)
    if status != "ok":
        return None, set()
    return digits, sync.member_no_forms(digits)


def member_no_phone(member_no: str) -> str | None:
    """The phone digits a MemberNo is, under the #155 phone rule (the
    MemberNo is the phone, or its 8 Singapore digits); else None."""

    digits, forms = phone_forms(member_no)
    return digits if digits is not None and sync.member_no_key(member_no) in forms else None


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

LEDGER_SCHEMA = """
CREATE TABLE meta(
  id INTEGER PRIMARY KEY CHECK (id = 1), tool TEXT NOT NULL CHECK (tool = 'xb268_member_benefits'), ledger_id TEXT NOT NULL,
  autocount_book TEXT NOT NULL CHECK (autocount_book IN ('AED_TESTINGAPI', 'AED_XBOUNDARIES')),
  store_gid TEXT NOT NULL, shop_domain TEXT NOT NULL, ref_key TEXT NOT NULL CHECK (length(ref_key) = 64), created_at TEXT NOT NULL,
  rule_fingerprint TEXT NOT NULL CHECK (rule_fingerprint GLOB 'sha256:*' AND length(rule_fingerprint) = 71));
CREATE TABLE receipts(
  order_gid TEXT PRIMARY KEY, customer_gid TEXT, order_created_at TEXT NOT NULL, order_date_sgt TEXT NOT NULL,
  order_updated_at TEXT NOT NULL, financial_status TEXT, gross_cents INTEGER, net_cents INTEGER, refunded_cents INTEGER,
  gift_card_cents INTEGER, payment_gateways TEXT,
  state TEXT NOT NULL CHECK (state IN ('ignored_unpaid', 'ignored_test', 'void', 'before_start', 'no_customer', 'amount_unverifiable',
    'below_threshold', 'not_signed_up', 'pending_signup', 'waiting_member', 'ready', 'applied', 'no_change', 'not_in_scope', 'held')),
  code TEXT, member_no TEXT, link_source TEXT CHECK (link_source IS NULL OR link_source IN ('sync155', 'claim')),
  basis_expiry_local TEXT, observed TEXT, observed_at TEXT, waiting_since TEXT, checked_at TEXT, alerted TEXT,
  first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK (state <> 'applied' OR (member_no IS NOT NULL AND basis_expiry_local IS NOT NULL)));
CREATE TABLE claim_links(
  link_id INTEGER PRIMARY KEY, customer_gid TEXT NOT NULL, member_no TEXT NOT NULL, member_no_key TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('order', 'index')), code TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE claim_unlinks(
  link_id INTEGER PRIMARY KEY REFERENCES claim_links(link_id),
  reason TEXT NOT NULL CHECK (reason IN ('wrong_person', 'customers_merged', 'member_renumbered', 'other')),
  run_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE VIEW active_claim_links AS
  SELECT * FROM claim_links c WHERE NOT EXISTS (SELECT 1 FROM claim_unlinks u WHERE u.link_id = c.link_id);
CREATE TABLE reviews(subject TEXT PRIMARY KEY, code TEXT NOT NULL, first_seen_at TEXT NOT NULL);
CREATE TABLE writes(
  write_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, order_gid TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('upgrade', 'extend')),
  member_no TEXT NOT NULL, from_type TEXT NOT NULL, to_type TEXT NOT NULL CHECK (to_type = 'MEMBER'),
  expected_last_modified_local TEXT NOT NULL, expected_expiry_local TEXT, new_expiry_local TEXT NOT NULL, salt TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('INTENT', 'DONE', 'SKIPPED', 'NOT_WRITTEN', 'UNCERTAIN', 'HELD')),
  code TEXT, post_last_modified_local TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  deadline_at TEXT NOT NULL, child_pid INTEGER, child_created TEXT,
  CHECK ((kind = 'upgrade' AND from_type = 'GUEST') OR (kind = 'extend' AND from_type = 'MEMBER')),
  CHECK (new_expiry_local GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T00:00:00'),
  CHECK (expected_expiry_local IS NULL OR substr(new_expiry_local, 1, 10) > substr(expected_expiry_local, 1, 10)),
  CHECK (state <> 'DONE' OR post_last_modified_local IS NOT NULL));
CREATE TABLE write_releases(
  write_id TEXT PRIMARY KEY REFERENCES writes(write_id),
  finding TEXT NOT NULL CHECK (finding IN ('as_intended', 'not_as_intended')),
  member_read_digest TEXT NOT NULL CHECK (member_read_digest GLOB 'sha256:*' AND length(member_read_digest) = 71),
  run_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE watermarks(name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, command TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, summary TEXT);
CREATE TRIGGER meta_immutable BEFORE UPDATE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER meta_no_delete BEFORE DELETE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER receipts_no_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT, 'receipt_immutable'); END;
CREATE TRIGGER receipts_binding_immutable BEFORE UPDATE OF order_gid, order_created_at, first_seen_at ON receipts
  BEGIN SELECT RAISE(ABORT, 'receipt_immutable'); END;
CREATE TRIGGER receipts_decided_stay BEFORE UPDATE OF state ON receipts
  WHEN OLD.state IN ('applied', 'no_change', 'not_in_scope', 'held') AND NEW.state <> OLD.state
   AND NOT (OLD.state = 'held' AND NEW.state = 'ready' AND COALESCE(OLD.code, '') IN ('claim_name_mismatch', 'claim_phone_not_unique',
            'claim_link_conflict', 'claim_memberno_conflict', 'claim_member_not_in_scope', 'claim_no_phone', 'claim_pair_unlinked'))
  BEGIN SELECT RAISE(ABORT, 'receipt_decided'); END;
CREATE TRIGGER receipts_decided_code BEFORE UPDATE OF code ON receipts
  WHEN OLD.state IN ('applied', 'no_change', 'not_in_scope', 'held') AND NEW.state = OLD.state AND NEW.code IS NOT OLD.code
   AND NOT (OLD.state = 'held'
            AND COALESCE(OLD.code, '') IN ('claim_name_mismatch', 'claim_phone_not_unique',
                'claim_link_conflict', 'claim_memberno_conflict', 'claim_member_not_in_scope', 'claim_no_phone', 'claim_pair_unlinked')
            AND COALESCE(NEW.code, '') IN ('order_no_longer_qualifies', 'refund_unattributed', 'order_past_read_window'))
  BEGIN SELECT RAISE(ABORT, 'receipt_decided'); END;
CREATE TRIGGER claim_links_one_active BEFORE INSERT ON claim_links
  WHEN EXISTS (SELECT 1 FROM active_claim_links a WHERE a.customer_gid = NEW.customer_gid OR a.member_no_key = NEW.member_no_key)
  BEGIN SELECT RAISE(ABORT, 'claim_link_conflict'); END;
CREATE TRIGGER claim_links_pair_unlinked BEFORE INSERT ON claim_links
  WHEN EXISTS (SELECT 1 FROM claim_links c JOIN claim_unlinks u ON u.link_id = c.link_id
               WHERE c.customer_gid = NEW.customer_gid AND c.member_no_key = NEW.member_no_key)
  BEGIN SELECT RAISE(ABORT, 'claim_pair_unlinked'); END;
CREATE TRIGGER claim_links_immutable BEFORE UPDATE ON claim_links BEGIN SELECT RAISE(ABORT, 'claim_link_immutable'); END;
CREATE TRIGGER claim_links_no_delete BEFORE DELETE ON claim_links BEGIN SELECT RAISE(ABORT, 'claim_link_immutable'); END;
CREATE TRIGGER claim_unlinks_immutable BEFORE UPDATE ON claim_unlinks BEGIN SELECT RAISE(ABORT, 'claim_unlink_immutable'); END;
CREATE TRIGGER claim_unlinks_no_delete BEFORE DELETE ON claim_unlinks BEGIN SELECT RAISE(ABORT, 'claim_unlink_immutable'); END;
CREATE TRIGGER write_releases_held_only BEFORE INSERT ON write_releases
  WHEN NOT EXISTS (SELECT 1 FROM writes w WHERE w.write_id = NEW.write_id AND w.state = 'HELD')
  BEGIN SELECT RAISE(ABORT, 'release_not_held'); END;
CREATE TRIGGER write_releases_immutable BEFORE UPDATE ON write_releases BEGIN SELECT RAISE(ABORT, 'write_release_immutable'); END;
CREATE TRIGGER write_releases_no_delete BEFORE DELETE ON write_releases BEGIN SELECT RAISE(ABORT, 'write_release_immutable'); END;
CREATE TRIGGER writes_no_delete BEFORE DELETE ON writes BEGIN SELECT RAISE(ABORT, 'write_immutable'); END;
CREATE TRIGGER writes_binding_immutable BEFORE UPDATE OF write_id, run_id, order_gid, kind, member_no, from_type, to_type,
  expected_last_modified_local, expected_expiry_local, new_expiry_local, salt, created_at, deadline_at ON writes
  BEGIN SELECT RAISE(ABORT, 'write_immutable'); END;
CREATE TRIGGER writes_transition BEFORE UPDATE OF state ON writes
  WHEN NOT ((OLD.state = 'INTENT' AND NEW.state IN ('DONE', 'SKIPPED', 'NOT_WRITTEN', 'UNCERTAIN', 'HELD'))
         OR (OLD.state = 'UNCERTAIN' AND NEW.state IN ('DONE', 'NOT_WRITTEN', 'HELD')))
  BEGIN SELECT RAISE(ABORT, 'write_transition_invalid'); END;
"""


class BenefitsLedger:
    """Single-owner SQLite ledger in the tool's own private root."""

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = dl.utc_now):
        self.root = Path(root)
        self.clock = clock
        self._lock = dl._OwnerLock(self.root / LOCK_FILE)
        self.conn = None
        try:
            self.conn = sqlite3.connect(self.root / LEDGER_FILE, isolation_level=None, timeout=0)
            self.conn.row_factory = sqlite3.Row
            # Slice B1: the version is checked before any pragma that could
            # change the file, so a ledger made by older code is left as it is.
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not (version == 0 and not tables) and (version != SCHEMA_VERSION or "receipts" not in tables):
                raise dl.DirectLocalError("benefits_ledger_not_recognised")
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

    def init_meta(self, config: BenefitsConfig) -> dict[str, Any]:
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
            if row is not None:
                self._check(row, config)
                return {"status": "existing"}
            conn.execute(
                "INSERT INTO meta(id, tool, ledger_id, autocount_book, store_gid, shop_domain, ref_key, created_at, rule_fingerprint) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (TOOL, f"mb-{uuid.uuid4().hex}", config.autocount_book, config.shopify.store_gid, config.shopify.shop_domain,
                 secrets.token_hex(32), dl.ts(self.now()), config.rule_fingerprint()),
            )
        return {"status": "initialised"}

    @staticmethod
    def _check(row: sqlite3.Row, config: BenefitsConfig) -> None:
        if (row["tool"], row["autocount_book"], row["store_gid"], row["shop_domain"]) != (
                TOOL, sync.check_book(config.autocount_book), config.shopify.store_gid, config.shopify.shop_domain):
            raise dl.DirectLocalError("benefits_ledger_binding_mismatch")

    def check_binding(self, config: BenefitsConfig) -> None:
        row = self.conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        if row is None:
            raise dl.DirectLocalError("benefits_ledger_not_initialised")
        self._check(row, config)

    def rule_settings_match(self, config: BenefitsConfig) -> bool:
        """Slice B1: the config's rule fingerprint is the one kept at init."""

        row = self.conn.execute("SELECT rule_fingerprint FROM meta WHERE id = 1").fetchone()
        return row is not None and hmac.compare_digest(row[0], config.rule_fingerprint())

    def check_rule_settings(self, config: BenefitsConfig) -> None:
        """Slice B1 (G0 1.4): observe and apply measure amounts, so they
        refuse when an amount setting or the amount rule changed since the
        ledger was made (the receipts already recorded keep their old
        amount). The way on is a new start (runbook)."""

        if not self.rule_settings_match(config):
            raise dl.DirectLocalError("benefits_rule_settings_changed")

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
            conn.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE run_id = ?", (dl.ts(self.now()), json.dumps(summary, sort_keys=True), run_id))

    def watermark(self, name: str) -> str | None:
        row = self.conn.execute("SELECT value FROM watermarks WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else None

    def set_watermark(self, name: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute("INSERT INTO watermarks(name, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value, "
                         "updated_at = excluded.updated_at", (name, value, dl.ts(self.now())))

    def clear_watermark(self, name: str) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM watermarks WHERE name = ?", (name,))

    def receipt(self, order_gid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM receipts WHERE order_gid = ?", (order_gid,)).fetchone()

    def update_receipt(self, order_gid: str, **values: Any) -> None:
        if not values:
            return
        columns = ", ".join(f"{name} = ?" for name in values)
        with self.tx() as conn:
            conn.execute(f"UPDATE receipts SET {columns}, updated_at = ? WHERE order_gid = ?", (*values.values(), dl.ts(self.now()), order_gid))

    # Slice B1: every reader of claim links reads the active ones only (a
    # voided link stays in claim_links as history).
    def claim_for_gid(self, gid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM active_claim_links WHERE customer_gid = ?", (gid,)).fetchone()

    def claim_for_member(self, member_no: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM active_claim_links WHERE member_no_key = ?", (sync.member_no_key(member_no),)).fetchone()

    def pair_unlinked(self, gid: str, member_no: str) -> bool:
        """Slice B1 (G0 1.6): this customer and this member were linked once
        and the link was voided. The one reader of ``claim_links`` outside
        the schema: a voided link is history, never an active link."""

        return self.conn.execute("SELECT 1 FROM claim_links c JOIN claim_unlinks u ON u.link_id = c.link_id "
                                 "WHERE c.customer_gid = ? AND c.member_no_key = ? LIMIT 1",
                                 (gid, sync.member_no_key(member_no))).fetchone() is not None

    def active_claims(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM active_claim_links ORDER BY link_id").fetchall()

    def claim_counts(self) -> tuple[int, int]:
        """``(active, voided)`` claim links."""

        return (self.conn.execute("SELECT COUNT(*) FROM active_claim_links").fetchone()[0],
                self.conn.execute("SELECT COUNT(*) FROM claim_unlinks").fetchone()[0])

    def add_claim(self, gid: str, member_no: str, source: str, code: str) -> None:
        with self.tx() as conn:
            conn.execute("INSERT INTO claim_links(customer_gid, member_no, member_no_key, source, code, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (gid, member_no, sync.member_no_key(member_no), source, code, dl.ts(self.now())))

    def review_once(self, subject: str, code: str) -> bool:
        """True the first time a subject needs review (alert once)."""

        with self.tx() as conn:
            if conn.execute("SELECT 1 FROM reviews WHERE subject = ?", (subject,)).fetchone() is not None:
                return False
            conn.execute("INSERT INTO reviews(subject, code, first_seen_at) VALUES (?, ?, ?)", (subject, code, dl.ts(self.now())))
        return True

    def open_writes(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM writes WHERE state IN ('INTENT', 'UNCERTAIN') ORDER BY rowid").fetchall()

    def held_writes(self) -> int:
        """HELD writes not released yet (slice B1): ``apply`` refuses while any
        exists. A released write stays HELD for ever (write_landed_or_open)."""

        return self.conn.execute("SELECT COUNT(*) FROM writes w WHERE w.state = 'HELD' AND NOT EXISTS "
                                 "(SELECT 1 FROM write_releases r WHERE r.write_id = w.write_id)").fetchone()[0]

    def unreleased_held_writes(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM writes w WHERE w.state = 'HELD' AND NOT EXISTS "
                                 "(SELECT 1 FROM write_releases r WHERE r.write_id = w.write_id) ORDER BY rowid").fetchall()

    def done_write(self, order_gid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM writes WHERE order_gid = ? AND state = 'DONE' ORDER BY rowid DESC LIMIT 1", (order_gid,)).fetchone()

    def write_landed_or_open(self, order_gid: str) -> bool:
        """A write for this receipt that landed or may have landed."""

        return self.conn.execute("SELECT 1 FROM writes WHERE order_gid = ? AND state IN ('INTENT', 'DONE', 'UNCERTAIN', 'HELD') LIMIT 1",
                                 (order_gid,)).fetchone() is not None

    def add_intent(self, *, run_id: str, order_gid: str, payload: Mapping[str, Any], deadline: datetime) -> sqlite3.Row:
        write_id = f"write-{uuid.uuid4().hex}"
        now = dl.ts(self.now())
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO writes(write_id, run_id, order_gid, kind, member_no, from_type, to_type, expected_last_modified_local, expected_expiry_local, "
                "new_expiry_local, salt, state, created_at, updated_at, deadline_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTENT', ?, ?, ?)",
                (write_id, run_id, order_gid, payload["kind"], payload["member_no"], payload["from_type"], payload["to_type"],
                 payload["expected_last_modified_local"], payload["expected_expiry_local"], payload["new_expiry_local"], payload["salt"], now, now, dl.ts(deadline)),
            )
        return self.conn.execute("SELECT * FROM writes WHERE write_id = ?", (write_id,)).fetchone()

    def set_child(self, write_id: str, pid: int, created: str | None) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE writes SET child_pid = ?, child_created = ? WHERE write_id = ?", (pid, created, write_id))

    def set_outcome(self, write_id: str, state: str, code: str | None, post: str | None = None) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE writes SET state = ?, code = ?, post_last_modified_local = ?, updated_at = ? WHERE write_id = ?",
                         (state, code, post, dl.ts(self.now()), write_id))


def payload_of(write: sqlite3.Row) -> dict[str, Any]:
    """The child's payload, built from the stored row so the ledger and the
    child always agree."""

    return {key: write[key] for key in ("kind", "member_no", "from_type", "to_type", "expected_last_modified_local",
                                        "expected_expiry_local", "new_expiry_local", "salt")}


# --------------------------------------------------------------------------
# #155 links (read only)
# --------------------------------------------------------------------------

class SyncLinks:
    """The #155 sync ledger's ``links`` table, opened read-only (SQLite
    ``mode=ro`` and ``query_only``), never copied, never written, and only
    after its book, store and domain match this tool's."""

    def __init__(self, by_gid: Mapping[str, str], by_key: Mapping[str, str]):
        self.by_gid = dict(by_gid)
        self.by_key = dict(by_key)

    @classmethod
    def load(cls, root: str | Path, config: BenefitsConfig) -> "SyncLinks":
        path = Path(root) / sync.LEDGER_FILE
        if not path.is_file():
            raise dl.DirectLocalError("sync_ledger_missing")
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            raise dl.DirectLocalError("sync_ledger_unreadable") from None
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            meta = conn.execute("SELECT autocount_book, store_gid, shop_domain FROM meta WHERE id = 1").fetchone()
            if meta is None or (meta["autocount_book"], meta["store_gid"], meta["shop_domain"]) != (
                    config.autocount_book, config.shopify.store_gid, config.shopify.shop_domain):
                raise dl.DirectLocalError("sync_ledger_binding_mismatch")
            by_gid: dict[str, str] = {}
            by_key: dict[str, str] = {}
            for row in conn.execute("SELECT customer_gid, member_no, member_no_key FROM links"):
                by_gid[row["customer_gid"]] = row["member_no"]
                by_key[row["member_no_key"]] = row["customer_gid"]
            return cls(by_gid, by_key)
        except sqlite3.Error:
            raise dl.DirectLocalError("sync_ledger_unreadable") from None
        finally:
            conn.close()


def link_conflicts(sync_links: SyncLinks, claims: Iterable[sqlite3.Row]) -> tuple[set[str], set[str]]:
    """Customers and member keys that map to two partners across both
    ledgers. A conflict blocks any write for that member."""

    gid_to: dict[str, set[str]] = {}
    key_to: dict[str, set[str]] = {}
    for gid, member_no in sync_links.by_gid.items():
        gid_to.setdefault(gid, set()).add(sync.member_no_key(member_no))
        key_to.setdefault(sync.member_no_key(member_no), set()).add(gid)
    for row in claims:
        gid_to.setdefault(row["customer_gid"], set()).add(row["member_no_key"])
        key_to.setdefault(row["member_no_key"], set()).add(row["customer_gid"])
    bad_gids = {gid for gid, keys in gid_to.items() if len(keys) > 1}
    bad_keys = {key for key, gids in key_to.items() if len(gids) > 1}
    for gid in bad_gids:
        bad_keys |= gid_to[gid]
    for key in bad_keys:
        bad_gids |= key_to[key]
    return bad_gids, bad_keys


# --------------------------------------------------------------------------
# Shopify (read only)
# --------------------------------------------------------------------------

_MONEY = "shopMoney { amount currencyCode }"
_CUSTOMER_FIELDS = """id createdAt tags firstName lastName
      defaultPhoneNumber { phoneNumber }
      registration: metafield(namespace: "membership", key: "start_date") { createdAt }"""
ORDERS_QUERY = """query XbMemberBenefitsOrders($first: Int!, $after: String, $search: String!) {
  orders(first: $first, after: $after, sortKey: UPDATED_AT, query: $search) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id createdAt updatedAt cancelledAt test displayFinancialStatus taxesIncluded currencyCode
      totalRefundedSet { """ + _MONEY + """ }
      customer { """ + _CUSTOMER_FIELDS + """ }
      lineItems(first: """ + str(SHOPIFY_LINE_PAGE) + """) {
        pageInfo { hasNextPage }
        nodes { id quantity currentQuantity isGiftCard originalUnitPriceSet { """ + _MONEY + """ }
          discountAllocations { allocatedAmountSet { """ + _MONEY + """ } } }
      }
      shippingLines(first: """ + str(SHOPIFY_SHIPPING_PAGE) + """) { pageInfo { hasNextPage } nodes { originalPriceSet { """ + _MONEY + """ } } }
      transactions(first: """ + str(SHOPIFY_PAYMENT_PAGE) + """) { gateway kind status amountSet { """ + _MONEY + """ } }
      transactionsCount { count precision }
    }
  }
}"""
CUSTOMER_QUERY = """query XbMemberBenefitsCustomer($id: ID!) {
  customer(id: $id) { """ + _CUSTOMER_FIELDS + """ }
}"""
PHONE_LOOKUP_QUERY = """query XbMemberBenefitsPhoneLookup($search: String!) {
  customers(first: 5, query: $search) { pageInfo { hasNextPage } nodes { id firstName lastName defaultPhoneNumber { phoneNumber } } }
}"""


class PhoneCandidatesTruncated(dl.DirectLocalError):
    """G2 LATER 6: the phone search has more candidates than one page, so
    "exactly one customer" cannot be shown: the claim goes to review."""

    def __init__(self) -> None:
        super().__init__("claim_phone_not_unique")
READ_QUERIES = frozenset({direct.SESSION_QUERY, ORDERS_QUERY, CUSTOMER_QUERY, PHONE_LOOKUP_QUERY})


def _shape(ok: bool) -> None:
    if not ok:
        raise direct.ShopifyDirectError("shopify_response_shape_invalid")


def _money(node: Any) -> tuple[int | None, str | None]:
    _shape(isinstance(node, dict) and isinstance(node.get("shopMoney"), dict))
    money = node["shopMoney"]
    return money_cents(money.get("amount")), money.get("currencyCode") if isinstance(money.get("currencyCode"), str) else None


def _payment_cents(node: Any) -> int | None:
    """A transaction's ``amountSet`` in SGD cents, or None when it is not
    SGD, negative or malformed. Never a failed read: the order is then
    ``payments_unreadable`` and the other orders of the page carry on."""

    money = node.get("shopMoney") if isinstance(node, dict) else None
    if not isinstance(money, dict) or money.get("currencyCode") != CURRENCY:
        return None
    cents = money_cents(money.get("amount"))
    return cents if cents is not None and cents >= 0 else None


def customer_from_node(node: Any) -> Customer:
    _shape(isinstance(node, dict) and isinstance(node.get("id"), str) and bool(_CUSTOMER_GID_RE.fullmatch(node["id"])))
    phone = node.get("defaultPhoneNumber") if isinstance(node.get("defaultPhoneNumber"), dict) else {}
    registration = node.get("registration") if isinstance(node.get("registration"), dict) else None
    tags = node.get("tags") if isinstance(node.get("tags"), list) else []
    return Customer(
        gid=node["id"], first_name=node.get("firstName") if isinstance(node.get("firstName"), str) else None,
        last_name=node.get("lastName") if isinstance(node.get("lastName"), str) else None,
        phone=phone.get("phoneNumber") if isinstance(phone.get("phoneNumber"), str) else None,
        tags=tuple(t for t in tags if isinstance(t, str)), created_at=dl.parse_created_at(node.get("createdAt")),
        registration_at=dl.parse_created_at(registration.get("createdAt")) if registration else None,
    )


def order_from_node(node: Any) -> Order:
    """One order node, every field shape-checked; a malformed node fails the
    whole read (the watermark does not move)."""

    _shape(isinstance(node, dict) and isinstance(node.get("id"), str) and bool(_ORDER_GID_RE.fullmatch(node["id"])))
    created, updated = dl.parse_created_at(node.get("createdAt")), dl.parse_created_at(node.get("updatedAt"))
    _shape(created is not None and updated is not None and isinstance(node.get("test"), bool) and isinstance(node.get("taxesIncluded"), bool))
    status = node.get("displayFinancialStatus")
    _shape(status is None or isinstance(status, str))
    currencies: set[str | None] = {node.get("currencyCode")}
    refunded, refund_currency = _money(node.get("totalRefundedSet"))
    currencies.add(refund_currency)
    items = node.get("lineItems")
    _shape(isinstance(items, dict) and isinstance(items.get("nodes"), list) and isinstance(items.get("pageInfo"), dict))
    lines = []
    for line in items["nodes"]:
        _shape(isinstance(line, dict) and isinstance(line.get("id"), str) and bool(_LINE_GID_RE.fullmatch(line["id"])))
        quantity, current = line.get("quantity"), line.get("currentQuantity")
        _shape(all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (quantity, current)) and current <= quantity
               and isinstance(line.get("isGiftCard"), bool) and isinstance(line.get("discountAllocations"), list))
        unit, currency = _money(line.get("originalUnitPriceSet"))
        currencies.add(currency)
        discount = 0
        for allocation in line["discountAllocations"]:
            _shape(isinstance(allocation, dict))
            cents, currency = _money(allocation.get("allocatedAmountSet"))
            currencies.add(currency)
            _shape(cents is not None and cents >= 0)
            discount += cents
        _shape(unit is not None and unit >= 0)
        lines.append(OrderLine(quantity, current, line["isGiftCard"], unit, discount))
    shipping = node.get("shippingLines")
    _shape(isinstance(shipping, dict) and isinstance(shipping.get("nodes"), list) and isinstance(shipping.get("pageInfo"), dict))
    shipping_cents = []
    for ship in shipping["nodes"]:
        _shape(isinstance(ship, dict))
        cents, currency = _money(ship.get("originalPriceSet"))
        currencies.add(currency)
        _shape(cents is not None and cents >= 0)
        shipping_cents.append(cents)
    transactions = node.get("transactions")
    _shape(isinstance(transactions, list))
    payments = []
    for item in transactions:
        _shape(isinstance(item, dict) and isinstance(item.get("kind"), str) and isinstance(item.get("status"), str)
               and (item.get("gateway") is None or isinstance(item["gateway"], str)))
        payments.append(Payment((item.get("gateway") or "").strip().lower(), item["kind"], item["status"], _payment_cents(item.get("amountSet"))))
    customer = None if node.get("customer") is None else customer_from_node(node["customer"])
    return Order(
        gid=node["id"], created_at=created, updated_at=updated, cancelled=node.get("cancelledAt") is not None, test=node["test"],
        financial_status=status, taxes_included=node["taxesIncluded"], currency=CURRENCY if currencies == {CURRENCY} else None,
        customer=customer, lines=tuple(lines), lines_complete=items["pageInfo"].get("hasNextPage") is False,
        shipping_cents=tuple(shipping_cents), shipping_complete=shipping["pageInfo"].get("hasNextPage") is False, refunded_cents=refunded,
        payments=tuple(payments), payments_complete=payments_complete(node.get("transactionsCount"), len(payments)),
    )


def payments_complete(count_node: Any, listed: int) -> bool:
    """Slice B1 (M1): the transaction list is complete only when
    ``transactionsCount`` is an object whose ``count`` is a whole number equal
    to the number listed and whose ``precision`` is ``EXACT``, and the list
    is shorter than its page. Anything else (null, missing, ``AT_LEAST``, a
    mismatch, a malformed value) is not complete: the order is
    ``payments_unreadable``, never a failed read (one odd order must not stop
    the reader). An empty list with a count of 0, exact, is proven empty."""

    if not isinstance(count_node, dict):
        return False
    count = count_node.get("count")
    return (type(count) is int and count == listed and count_node.get("precision") == COUNT_EXACT
            and listed < SHOPIFY_PAYMENT_PAGE)


class BenefitsShopifyClient(direct.ShopifyDirectClient):
    """Admin GraphQL client of the benefits app: its own pinned reads only.
    No mutation exists in this module, and the token must hold exactly
    ``read_customers`` and ``read_orders`` (no write scope)."""

    def __repr__(self) -> str:
        return "BenefitsShopifyClient()"

    def _query_allowed(self, query: str) -> bool:
        return query in READ_QUERIES

    def verify_session(self, *, store_gid: str) -> None:
        data = self.execute(direct.SESSION_QUERY, {})
        if set(data) != {"shop", "currentAppInstallation"} or data.get("shop") != {"id": store_gid, "myshopifyDomain": self.shop_domain}:
            raise direct.ShopifyDirectError("shopify_store_identity_mismatch")
        installation = data["currentAppInstallation"]
        scopes = installation.get("accessScopes") if isinstance(installation, dict) else None
        if not isinstance(scopes, list) or any(not isinstance(item, dict) or set(item) != {"handle"} or not isinstance(item["handle"], str) for item in scopes):
            raise direct.ShopifyDirectError("shopify_response_shape_invalid")
        handles = [item["handle"] for item in scopes]
        if len(set(handles)) != len(handles) or frozenset(handles) != REQUIRED_SCOPES:
            raise direct.ShopifyDirectError("shopify_token_scope_not_expected")

    last_end_cursor: str | None = None
    last_orders_query_cost: float | None = None

    def orders(self, since: datetime, max_pages: int, after: str | None = None) -> tuple[list[Order], bool]:
        """Orders updated at or after ``since``, ascending by updatedAt,
        starting after ``after`` (a cursor kept from a read that ended at the
        page cap, G2 B3). ``last_end_cursor`` is the cursor to continue from
        when the read ends at the page cap (``complete`` false).
        ``last_orders_query_cost`` is the largest requested cost Shopify
        reported for a page of this read (``extensions.cost
        .requestedQueryCost``, a number only), or None when it reported none."""

        search = f"updated_at:>='{since.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}'"
        orders: list[Order] = []
        self.last_end_cursor = None
        self.last_orders_query_cost = None
        for _ in range(max_pages):
            data = self.execute(ORDERS_QUERY, {"first": SHOPIFY_ORDER_PAGE, "after": after, "search": search})
            cost = self._throttle.requested_cost if self._throttle is not None else None
            if cost is not None and (self.last_orders_query_cost is None or cost > self.last_orders_query_cost):
                self.last_orders_query_cost = cost
            page = data.get("orders")
            _shape(isinstance(page, dict) and isinstance(page.get("nodes"), list) and isinstance(page.get("pageInfo"), dict))
            orders.extend(order_from_node(node) for node in page["nodes"])
            if not page["pageInfo"].get("hasNextPage"):
                return orders, True
            after = page["pageInfo"].get("endCursor")
            _shape(isinstance(after, str) and bool(after))
        self.last_end_cursor = after
        return orders, False

    def customer(self, gid: str) -> Customer | None:
        node = self.execute(CUSTOMER_QUERY, {"id": gid}).get("customer")
        return None if node is None else customer_from_node(node)

    def find_by_phone(self, digits: str) -> list[Customer]:
        """Customers whose own phone is this phone. The search is only a
        candidate list: every node goes through the #155 phone rule, and a
        node with no phone or another phone is dropped (as #155, G2 B1)."""

        data = self.execute(PHONE_LOOKUP_QUERY, {"search": f"phone:{sync.e164(digits)}"})
        page = data.get("customers")
        _shape(isinstance(page, dict) and isinstance(page.get("nodes"), list) and isinstance(page.get("pageInfo"), dict))
        if page["pageInfo"].get("hasNextPage") is not False:
            raise PhoneCandidatesTruncated()
        found = [customer_from_node({**node, "tags": []}) for node in page["nodes"]]
        return [c for c in found if sync.phone_member_no(c.phone)[0] == digits]


class BenefitsTokenCache(direct.TokenCache):
    """The #155 DPAPI token cache, checking this app's two scopes."""

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
            if direct._scopes(value["scope"]) != REQUIRED_SCOPES or expires_at is None:
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


class BenefitsTokenProvider(direct.ClientCredentialsTokenProvider):
    """The #155 client-credentials grant, for this app's exact two scopes."""

    def __init__(self, shop_domain: str, client_id: Any, client_secret: Any, **kwargs: Any):
        protector = kwargs.pop("cache_protector", None)
        path = kwargs.pop("cache_path", None)
        super().__init__(shop_domain, client_id, client_secret, **kwargs)
        self.cache = None if protector is None else BenefitsTokenCache(path, protector, shop_domain=shop_domain, client_id=self._client_id)

    def __repr__(self) -> str:
        return "BenefitsTokenProvider()"

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
        if scopes != REQUIRED_SCOPES:
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
    return BenefitsTokenProvider(
        connection.shop_domain, environ.get(connection.auth_env["client_id_env"]), environ.get(connection.auth_env["client_secret_env"]),
        transport=transport, clock=clock, timeout=connection.http_timeout_seconds, cache_protector=cache_protector,
        cache_path=root / "benefits_token_cache.bin",
    )


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


def classify_save(run: dl.WriteRun, *, claim_exists: bool) -> SaveOutcome:
    """Outcome of one benefit-save child. Absence of the claim file is the
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
    except (json.JSONDecodeError, IndexError, RecursionError, AttributeError):
        outcome = None
    if not isinstance(outcome, dict):
        return SaveOutcome("UNCERTAIN", "writer_outcome_invalid")
    if outcome.get("status") == "skipped":
        if set(outcome) == {"status", "code"} and outcome["code"] in SAVE_SKIP_CODES and not claim_exists:
            return SaveOutcome("SKIPPED", outcome["code"])
        return SaveOutcome("UNCERTAIN", "writer_outcome_invalid")
    changed = outcome.get("changed_fields")
    changed = [changed] if isinstance(changed, str) else changed
    post = outcome.get("post_last_modified_local")
    valid = (
        outcome.get("status") == "saved" and outcome.get("save_invocation_count") == 1 and claim_exists
        and all(isinstance(outcome.get(k), bool) for k in ("readback_found", "readback_type_match", "readback_expiry_match", "fields_unchanged", "outbound_queue_changed"))
        and isinstance(changed, list) and all(isinstance(c, str) and _COLUMN_RE.fullmatch(c) for c in changed)
        and (post is None or (isinstance(post, str) and _LOCAL_STORED_RE.fullmatch(post)))
    )
    if not valid:
        return SaveOutcome("UNCERTAIN", "writer_outcome_invalid")
    fields = tuple(sorted(changed))[:20]
    if not outcome["readback_found"]:
        return SaveOutcome("UNCERTAIN", "readback_absent")
    if not outcome["readback_type_match"]:
        return SaveOutcome("HELD", "readback_type_mismatch", post, fields)
    if not outcome["readback_expiry_match"]:
        return SaveOutcome("HELD", "readback_expiry_mismatch", post, fields)
    if not outcome["fields_unchanged"]:
        return SaveOutcome("HELD", "readback_fields_changed", post, fields)
    if outcome["outbound_queue_changed"]:
        return SaveOutcome("HELD", "outbound_queue_changed", post, fields)
    if post is None:
        return SaveOutcome("HELD", "readback_last_modified_missing", None, fields)
    return SaveOutcome("DONE", None, post, fields)


class BenefitsBridgeClient(tm.TypeMoveBridgeClient):
    """ac2_member_benefits_bridge.ps1: two reads and the fenced save. The
    process start and the fenced write runner are the type move's."""

    def __repr__(self) -> str:
        return "BenefitsBridgeClient()"

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
        raise dl.DirectLocalError("benefit_operation_not_offered")

    def scan(self, member_type: str, after_member_no: str) -> list[dict[str, Any]]:
        if member_type not in SCOPE_TYPES:
            raise dl.DirectLocalError("benefit_type_not_allowed")
        rows = self._read("benefit-scan", {"member_type": member_type, "after_member_no": after_member_no}).get("members")
        rows = [rows] if isinstance(rows, dict) else rows
        if not isinstance(rows, list) or len(rows) > tm.SCAN_PAGE_SIZE:
            raise dl.BridgeError("bridge_output_invalid")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("member_no"), str) or not row["member_no"]:
                raise dl.BridgeError("bridge_output_invalid")
        return rows

    def member(self, member_no: str, salt: str) -> MemberRead:
        result = self._read("benefit-member", {"member_no": member_no, "salt": salt})
        if not isinstance(result.get("found"), bool):
            raise dl.BridgeError("bridge_output_invalid")
        if not result["found"]:
            return MemberRead(False)
        if not (isinstance(result.get("member_type"), str) and isinstance(result.get("fields_digest"), str) and _DIGEST_RE.fullmatch(result["fields_digest"])):
            raise dl.BridgeError("bridge_output_invalid")
        text = {k: result.get(k) if isinstance(result.get(k), str) else None for k in ("member_no", "name", "is_active", "expiry_local", "last_modified_local")}
        return MemberRead(True, member_type=result["member_type"], fields_digest=result["fields_digest"], **text)

    def save(self, payload: Mapping[str, Any], *, move_id: str, deadline: datetime,
             on_started: Callable[[int, str | None], None] = lambda pid, created: None) -> SaveOutcome:
        check_save_payload(payload)
        fence_id = "fence-" + move_id.removeprefix("write-")
        salt = secrets.token_hex(16)
        text = dl.canonical_payload(payload)
        stdin = {"fence_id": fence_id, "payload_canonical": text, "payload_digest": dl.payload_digest(salt, text), "digest_salt": salt}
        arguments = ["-Operation", "benefit-save", "-EnableProductionAdapter", "-FenceId", fence_id,
                     "-ClaimDirectory", str(self.claims), "-WriteDeadlineUtc", deadline.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]
        wait = (deadline - dl.utc_now()).total_seconds() + dl.WRITER_GRACE_SECONDS
        run = self._write(arguments, stdin, wait_seconds=wait, on_started=on_started)
        return classify_save(run, claim_exists=(self.claims / f"{fence_id}.claim").exists())


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
    # Shopify's requested cost of the orders query on this run, in whole
    # points (to compare with the calculated 852); None when none was reported.
    orders_query_cost: int | None = None

    def count(self, code: str, ref: str | None = None, member_no: str | None = None) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1
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
        result = {"status": "ok", "command": self.command, "run_id": run_id, "saves": self.saves, "counts": dict(sorted(self.counts.items())),
                  "alerts": list(self.alerts), "refs": list(self.refs), "stopped": self.stopped}
        if self.command in ("observe", "apply"):  # the commands that read orders
            result["orders_query_cost"] = self.orders_query_cost
        return result


def whole_points(value: Any) -> int | None:
    """A query cost as a whole number of points, rounded up; None for
    anything that is not a plain number from 0 to 1,000,000 (never text)."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1_000_000:  # NaN fails the comparison
        return None
    return math.ceil(value)


class _Budget(Exception):
    """The run's AutoCount read budget is spent."""


class BenefitsEngine:
    """One bounded run. ``shopify`` provides ``verify_session``, ``orders``,
    ``customer`` and ``find_by_phone``; ``ac2`` provides ``member``, ``scan``
    and ``save``; ``sync_links`` loads the #155 links (read only)."""

    def __init__(self, config: BenefitsConfig, ledger: BenefitsLedger, *, shopify: Any, ac2: Any,
                 sync_links: Callable[[], SyncLinks] | None = None,
                 writer_probe: Callable[[int, str | None], str] = dl.probe_writer):
        self.config = config
        self.ledger = ledger
        self.shopify = shopify
        self.ac2 = ac2
        self._sync_links = sync_links or (lambda: SyncLinks.load(config.member_sync_state_root, config))
        self.writer_probe = writer_probe
        self.links: SyncLinks | None = None
        self.conflict_gids: set[str] = set()
        self.conflict_keys: set[str] = set()
        self.linked_gids: set[str] = set()
        self._read_state: tuple[datetime, bool, str | None] | None = None
        self.reads = 0

    # -- helpers --------------------------------------------------------------
    def now(self) -> datetime:
        return self.ledger.now()

    def _stop_requested(self) -> bool:
        return (self.ledger.root / STOP_FILE).exists()

    def _read_member(self, member_no: str) -> MemberRead:
        if self.reads >= self.config.max_member_reads_per_run:
            raise _Budget()
        self.reads += 1
        return self.ac2.member(member_no, secrets.token_hex(16))

    def _load_links(self) -> None:
        self.links = self._sync_links()
        claims = self.ledger.active_claims()
        self.conflict_gids, self.conflict_keys = link_conflicts(self.links, claims)
        self.linked_gids = set(self.links.by_gid) | {row["customer_gid"] for row in claims}

    def _reopen_claim_holds(self, summary: RunSummary) -> None:
        """G2 B2: a receipt held by a claim outcome goes back to ``ready``
        once a #155 link or a claim link for its customer exists."""

        marks = ",".join("?" for _ in CLAIM_HOLD_CODES)
        rows = self.ledger.conn.execute(f"SELECT * FROM receipts WHERE state = 'held' AND code IN ({marks})",
                                        tuple(sorted(CLAIM_HOLD_CODES))).fetchall()
        for row in rows:
            if row["customer_gid"] not in self.linked_gids:
                continue
            ref = self.ledger.ref("o", row["order_gid"])
            if not self._still_qualifies_as_last_read(row):
                # G2 B5: the decision rests on the order as last read; a
                # receipt whose stored facts no longer qualify never re-opens.
                self.ledger.update_receipt(row["order_gid"], code=ORDER_NO_LONGER_QUALIFIES)
                summary.count("claim_hold_settled", ref)
                # Slice B2 (B2-2): silent only for a paid order whose stored
                # amount is measured below the threshold (a unit refund).
                if not (row["financial_status"] in PAID_STATUSES and row["financial_status"] != "REFUNDED"
                        and isinstance(row["net_cents"], int) and not qualifies(row["net_cents"], self.config)):
                    self._settled_unclear(row["order_gid"], summary, ref)
                continue
            self.ledger.update_receipt(row["order_gid"], state="ready", code=None, checked_at=None)
            summary.count("claim_hold_reopened", ref)

    def _age_limit(self, summary: RunSummary) -> None:
        """Slice B2 (B2-4): ``read_orders`` sees orders created in the last
        60 days only, so a receipt whose order is that old (or older) can
        never be read again: a held claim receipt settles to
        ``order_past_read_window`` (never re-opened) and a ``waiting_member``
        one is held with that code (never waits). One alert per order.
        Every other receipt (``applied`` and the other decided ones) is
        never touched."""

        marks = ",".join("?" for _ in CLAIM_HOLD_CODES)
        rows = self.ledger.conn.execute(
            f"SELECT * FROM receipts WHERE (state = 'held' AND code IN ({marks})) OR state = 'waiting_member' ORDER BY order_created_at, order_gid",
            tuple(sorted(CLAIM_HOLD_CODES))).fetchall()
        now = self.now()
        for row in rows:
            created = dl.parse_created_at(row["order_created_at"])
            if created is None or now - created < SHOPIFY_ORDER_WINDOW:
                continue
            if row["state"] == "waiting_member":
                self.ledger.update_receipt(row["order_gid"], state="held", code=ORDER_PAST_READ_WINDOW, waiting_since=None)
            else:
                self.ledger.update_receipt(row["order_gid"], code=ORDER_PAST_READ_WINDOW)
            if self.ledger.review_once("aged:" + row["order_gid"], RECEIPT_PAST_READ_WINDOW):
                summary.alert(RECEIPT_PAST_READ_WINDOW, self.ledger.ref("o", row["order_gid"]), row["member_no"])

    def _settled_unclear(self, order_gid: str, summary: RunSummary, ref: str) -> None:
        """Slice B2 (B2-2): a claim hold settled for a reason that is not a
        positive fact: one alert per order, so staff can upgrade by hand. The
        settle itself is unchanged (it denies only, never grants)."""

        if self.ledger.review_once("settled:" + order_gid, CLAIM_HOLD_SETTLED_UNCLEAR):
            summary.alert(CLAIM_HOLD_SETTLED_UNCLEAR, ref)

    def _still_qualifies_as_last_read(self, row: sqlite3.Row) -> bool:
        """The stored facts of the order as last read: a paid status that is
        not REFUNDED and a net amount at or over the threshold. (``record``
        already settles a claim hold whose replayed order stops qualifying;
        this is the same rule on the stored row.)"""

        return (row["financial_status"] in PAID_STATUSES and row["financial_status"] != "REFUNDED"
                and isinstance(row["net_cents"], int) and qualifies(row["net_cents"], self.config))

    # -- 1. read and record ---------------------------------------------------
    def read_orders(self, summary: RunSummary) -> list[Order]:
        """Every page first; nothing is recorded and the watermark does not
        move unless the whole read is clean (Q22). G2 B3: a read that ends at
        the page cap keeps Shopify's cursor (``orders_resume``), and the next
        run continues after it with the same search, so the tool makes
        progress run after run until it has caught up."""

        resume = self._resume()
        if resume is not None:
            since, after = resume
        else:
            mark = self.ledger.watermark(WATERMARK)
            since = dl.parse_created_at(mark) if mark else self.config.orders_start_utc
            since = max(since - WATERMARK_MARGIN, self.config.orders_start_utc - WATERMARK_MARGIN)
            after = None
        try:
            orders, complete = self.shopify.orders(since, self.config.shopify_max_pages, after=after)
        except dl.DirectLocalError:
            if resume is not None:
                # A cursor Shopify no longer accepts must not stall the tool:
                # the next run starts again from the unmoved watermark.
                self.ledger.clear_watermark(RESUME_MARK)
            raise
        self._read_state = (since, complete, None if complete else getattr(self.shopify, "last_end_cursor", None))
        summary.orders_query_cost = whole_points(getattr(self.shopify, "last_orders_query_cost", None))
        summary.counts["orders_read"] = len(orders)
        if resume is not None:
            summary.count("orders_read_resumed")
        return orders

    def _resume(self) -> tuple[datetime, str] | None:
        value = self.ledger.watermark(RESUME_MARK)
        if value is None:
            return None
        try:
            stored = json.loads(value)
            since, cursor = dl.parse_created_at(stored["since"]), stored["cursor"]
        except (json.JSONDecodeError, KeyError, TypeError):
            since = cursor = None
        if since is None or not isinstance(cursor, str) or not cursor:
            self.ledger.clear_watermark(RESUME_MARK)
            return None
        return since, cursor

    def _advance_watermark(self, orders: list[Order], summary: RunSummary) -> None:
        """After every order of a clean read is recorded: a complete read
        moves the watermark to the newest ``updatedAt`` and ends any resume;
        a read that ended at the page cap keeps its cursor and alerts."""

        since, complete, cursor = self._read_state
        if complete:
            if orders:
                self.ledger.set_watermark(WATERMARK, dl.ts(max(o.updated_at for o in orders)))
            self.ledger.clear_watermark(RESUME_MARK)
            return
        if isinstance(cursor, str) and cursor:
            self.ledger.set_watermark(RESUME_MARK, json.dumps({"since": dl.ts(since), "cursor": cursor}, sort_keys=True))
        summary.alert("orders_read_incomplete")

    def record(self, order: Order, summary: RunSummary) -> None:
        """One receipt per order, forever. A replay updates the same row; a
        decided receipt only records refunds (and alerts on its basis)."""

        ref = self.ledger.ref("o", order.gid)
        amounts = qualifying_amount(order, self.config)
        gross, net = amounts if amounts is not None else (None, None)
        # The gift-card-paid amount behind ``net`` and the gateway names of
        # the order's transactions (lower case, for the digest), kept on the
        # receipt row.
        paid = gift_card_paid(order, self.config)
        gateways = json.dumps(sorted({payment.gateway for payment in order.payments}))
        existing = self.ledger.receipt(order.gid)
        now = dl.ts(self.now())
        if existing is not None and existing["state"] not in DECIDED_STATES:
            # G2 recheck LATER 2: a save that landed before the parent stopped
            # is repaired here, before the order is classified again, so a
            # refund read by the same run still reports refund_on_basis.
            if self._repair_applied(order.gid, summary, ref):
                existing = self.ledger.receipt(order.gid)
        if existing is not None and existing["state"] in DECIDED_STATES:
            self.ledger.update_receipt(order.gid, order_updated_at=dl.ts(order.updated_at), financial_status=order.financial_status,
                                       refunded_cents=order.refunded_cents, net_cents=net if net is not None else existing["net_cents"],
                                       gift_card_cents=paid if paid is not None else existing["gift_card_cents"], payment_gateways=gateways)
            if existing["state"] == "held" and existing["code"] in CLAIM_HOLD_CODES:
                # G2 B5: a claim-held receipt is looked at again on every
                # replay. An order that no longer qualifies settles it for
                # good (a code that never re-opens): silently for a unit
                # refund or a cancellation (nothing was granted), with the
                # refund_unattributed alert, once, for a refund by money.
                state, code = self._classify(order, net)
                # G2 B8: an order that left the receipt's customer settles the hold too.
                moved = (order.customer.gid if order.customer else None) != existing["customer_gid"]
                if not moved and (state, code) == ("held", REFUND_UNATTRIBUTED):
                    self.ledger.update_receipt(order.gid, code=REFUND_UNATTRIBUTED, alerted=REFUND_UNATTRIBUTED)
                    summary.alert(REFUND_UNATTRIBUTED, ref)
                elif moved or state != "ready":
                    self.ledger.update_receipt(order.gid, code=ORDER_NO_LONGER_QUALIFIES)
                    summary.count("claim_hold_settled", ref)
                    # Slice B2 (B2-2): silent for a positive fact (moved or no
                    # customer, a cancellation, an amount measured below the
                    # threshold); any other reason (unpaid, sign-up marker gone,
                    # amount unreadable, a test order) alerts once.
                    if not moved and state not in CLAIM_SETTLE_POSITIVE_STATES:
                        self._settled_unclear(order.gid, summary, ref)
                else:
                    summary.count("receipt_replayed")
                return
            if existing["state"] == "applied" and (order.customer.gid if order.customer else None) != existing["customer_gid"]:
                # Slice B2 (B2-1): the order now shows another customer, or
                # none. The receipt keeps its customer; one alert per order
                # (the owner checks the upgrade went to the right member).
                if self.ledger.review_once("moved:" + order.gid, ORDER_CUSTOMER_CHANGED):
                    summary.alert(ORDER_CUSTOMER_CHANGED, ref, existing["member_no"])
            # G2 B1: a refund by units below the threshold, a cancellation, or
            # any refund by money that no unit explains (incl. REFUNDED).
            dropped = order.cancelled or (net is not None and not qualifies(net, self.config)) or money_refund_unattributed(order)
            if existing["state"] == "applied" and dropped and existing["alerted"] != "refund_on_basis":
                late = self.now() - order.created_at > SHOPIFY_ORDER_WINDOW
                self.ledger.update_receipt(order.gid, alerted="refund_on_basis")
                summary.alert("late_refund_on_basis" if late else "refund_on_basis", ref, existing["member_no"])
            elif order.refunded_cents:
                summary.count("refund_recorded", ref)
            else:
                summary.count("receipt_replayed")
            return
        state, code = self._classify(order, net)
        if existing is not None and state == "ready" and existing["state"] == "waiting_member":
            # The watermark overlap replays an order every run: a replay that
            # still qualifies keeps the receipt waiting (and its back-off, G2 B2).
            state, code = "waiting_member", existing["code"]
        if state == "below_threshold" and net is None:
            summary.count("below_threshold_payments_unread")  # slice B2 (B2-7): the goods alone are below the threshold
        customer = order.customer.gid if order.customer else None
        values = dict(customer_gid=customer, order_updated_at=dl.ts(order.updated_at),
                      financial_status=order.financial_status, gross_cents=gross, net_cents=net, refunded_cents=order.refunded_cents,
                      gift_card_cents=paid, payment_gateways=gateways, state=state, code=code)
        if existing is not None and existing["customer_gid"] != customer:
            # Slice B2 (B2-5): an undecided receipt follows its order to another
            # customer (or none); nothing found for the old customer carries over.
            values.update(waiting_since=None, alerted=None, observed=None, observed_at=None, checked_at=None, member_no=None, link_source=None)
        if existing is None:
            with self.ledger.tx() as conn:
                conn.execute(
                    "INSERT INTO receipts(order_gid, order_created_at, order_date_sgt, first_seen_at, updated_at, " + ", ".join(values) + ") "
                    "VALUES (?, ?, ?, ?, ?, " + ", ".join("?" for _ in values) + ")",
                    (order.gid, dl.ts(order.created_at), sg_date(order.created_at).isoformat(), now, now, *values.values()))
            summary.count(f"receipt_{state}")
        else:
            self.ledger.update_receipt(order.gid, **values)
            summary.count("receipt_replayed")
        if state == "amount_unverifiable":
            summary.alert("order_amount_unverifiable", ref)
        if (state, code) == ("held", REFUND_UNATTRIBUTED):
            # Held is terminal, so this transition (and the alert) happens once.
            self.ledger.update_receipt(order.gid, alerted=REFUND_UNATTRIBUTED)
            summary.alert(REFUND_UNATTRIBUTED, ref)

    def _classify(self, order: Order, net: int | None) -> tuple[str, str | None]:
        if order.test:
            return "ignored_test", None
        if order.cancelled:
            return "void", None
        if order.financial_status not in PAID_STATUSES:
            return "ignored_unpaid", order.financial_status.lower() if order.financial_status else "status_missing"
        if order.created_at < self.config.orders_start_utc:
            return "before_start", None
        if order.customer is None:
            return "no_customer", None
        if net is None:
            if not order.taxes_included:
                return "amount_unverifiable", "taxes_not_included"
            goods = goods_amount(order, self.config)
            if goods is None:
                return "amount_unverifiable", "currency_or_lines"
            if not qualifies(goods[1], self.config):
                # Slice B2 (B2-7): the goods kept are below the threshold and a
                # gift card can only lower the amount, so the unread payments
                # cannot make it qualify (no alert). The amount functions are
                # unchanged: this only orders the checks.
                return "below_threshold", None
            return "amount_unverifiable", PAYMENTS_UNREADABLE
        if not qualifies(net, self.config):
            return "below_threshold", None
        if money_refund_unattributed(order):
            # G2 B1 (Controller: hold, never net by money): staff decide.
            return "held", REFUND_UNATTRIBUTED
        status = signup_status(order.customer, order, self.config)
        if status == "signed_up":
            return "ready", None
        if REGISTRATION_TAG in order.customer.tags and order.customer.registration_at is None:
            return "pending_signup", "signup_time_missing"
        # Re-checked while the sign-up wait is open (Q7: a same-day sign-up
        # may still arrive); settled as not_signed_up after it.
        if self.now() - order.created_at < timedelta(hours=self.config.signup_wait_hours) and status == "not_signed_up" and (
                order.customer.registration_at is None):
            return "pending_signup", "not_signed_up_yet"
        return "not_signed_up", None

    # -- 2. decide and write --------------------------------------------------
    def run(self, *, apply: bool) -> dict[str, Any]:
        command = "apply" if apply else "observe"
        if apply and not self.config.benefits_write_enabled:
            raise dl.DirectLocalError("benefits_write_disabled")
        self.ledger.check_binding(self.config)
        # Slice B1: before the Shopify session check, any read, any ledger
        # write or run row.
        self.ledger.check_rule_settings(self.config)
        if apply:
            if self.ledger.open_writes():
                raise dl.DirectLocalError("benefit_uncertain_unresolved")
            if self.ledger.held_writes():
                raise dl.DirectLocalError("benefit_held_rows_present")
        # Scope and store identity first (Q34), then every order page; any
        # failure refuses the run before anything is recorded (Q22).
        self.shopify.verify_session(store_gid=self.config.shopify.store_gid)
        summary = RunSummary(command)
        orders = self.read_orders(summary)
        self._load_links()
        run_id = self.ledger.start_run(command)
        try:
            for order in orders:
                self.record(order, summary)
            self._advance_watermark(orders, summary)
            self._age_limit(summary)
            if self._read_state[1]:
                self._reopen_claim_holds(summary)
            else:
                # Slice B2 (B2-3): a read that ended at the page cap may not
                # have reached this order's newest facts; re-open next time.
                summary.count("claim_reopen_deferred")
            self._recheck_signups(summary)
            self._decide_all(summary, run_id=run_id, apply=apply)
        except _Budget:
            summary.alert("member_reads_capped")  # G2 B2: an alert, not only a count
        except dl.DirectLocalError as exc:
            summary.stopped = exc.code
            summary.alert(exc.code)
            if isinstance(exc, dl.BridgeError) or exc.code.startswith("bridge_"):
                self.ledger.finish_run(run_id, summary.result(run_id))
                raise
        self.ledger.finish_run(run_id, summary.result(run_id))
        return summary.result(run_id)

    def _pending(self, states: Iterable[str]) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in states)
        return self.ledger.conn.execute(f"SELECT * FROM receipts WHERE state IN ({marks}) ORDER BY order_created_at, order_gid", tuple(states)).fetchall()

    def _recheck_signups(self, summary: RunSummary) -> None:
        """``pending_signup`` receipts: re-read the customer (bounded) until
        the sign-up is seen or the wait ends."""

        for row in self._pending(("pending_signup",))[: self.config.max_customer_rechecks_per_run]:
            customer = self.shopify.customer(row["customer_gid"]) if row["customer_gid"] else None
            created = dl.parse_created_at(row["order_created_at"])
            probe = Order(row["order_gid"], created, created, False, False, "PAID", True, CURRENCY, customer, (), True, (), True, 0, (), True)
            status = signup_status(customer, probe, self.config)
            ref = self.ledger.ref("o", row["order_gid"])
            if status == "signed_up":
                self.ledger.update_receipt(row["order_gid"], state="ready", code=None)
                summary.count("signup_seen", ref)
            elif self.now() - created >= timedelta(hours=self.config.signup_wait_hours):
                self.ledger.update_receipt(row["order_gid"], state="not_signed_up", code=row["code"])
                if row["code"] == "signup_time_missing":
                    summary.alert("signup_time_missing", ref)
                else:
                    summary.count("not_signed_up", ref)
            else:
                summary.count("signup_pending", ref)

    def _decide_all(self, summary: RunSummary, *, run_id: str, apply: bool) -> None:
        # G2 B2: linked ready receipts first, then other ready ones, then
        # waiting_member (each oldest first), so receipts that need a claim
        # cannot spend the read budget before the ones that can be decided.
        def rank(row: sqlite3.Row) -> int:
            if row["state"] == "ready":
                return 0 if row["customer_gid"] in self.linked_gids else 1
            return 2
        for row in sorted(self._pending(("waiting_member", "ready")), key=rank):
            if apply and summary.saves >= self.config.max_upgrades_per_run:
                summary.count("upgrade_cap_reached")
                return
            stop = self._decide_one(row, summary, run_id=run_id, apply=apply)
            if stop:
                return

    def _link(self, row: sqlite3.Row, summary: RunSummary) -> tuple[str | None, str | None, MemberRead | None, str | None]:
        """``(member_no, source, read, hold_code)``: the #155 link, a claim
        link, or a new claim (which needs the AC2 read, returned so it is not
        repeated). ``hold_code`` names a claim outcome that settles the
        receipt (G2 B2); ``None`` with no MemberNo means no AC2 member yet."""

        gid = row["customer_gid"]
        assert self.links is not None
        if gid in self.links.by_gid:
            return self.links.by_gid[gid], "sync155", None, None
        claim = self.ledger.claim_for_gid(gid)
        if claim is not None:
            return claim["member_no"], "claim", None, None
        customer = self.shopify.customer(gid)
        member_no, read, code = self.claim(customer, summary, source="order")
        return member_no, ("claim" if member_no else None), read, code

    def claim(self, customer: Customer | None, summary: RunSummary, *, source: str, member: MemberRead | None = None,
              member_no: str | None = None, same_phone: list[Customer] | None = None) -> tuple[str | None, MemberRead | None, str | None]:
        """The owner's "auto linking": an AC2 MEMBER or GUEST with no #155
        link, exactly one Shopify customer whose phone gives that MemberNo
        under the #155 phone rule, and the #155 name rule matches -> a claim
        link in this tool's ledger. Anything else: no link, and one
        ``claim_needs_review`` alert per subject. Returns ``(member_no, read,
        code)``: ``code`` is the claim outcome (one of ``CLAIM_HOLD_CODES``)
        when there is no link, ``None`` when linked or no member exists yet."""

        assert self.links is not None
        if customer is None:
            return None, None, None
        ref = self.ledger.ref("c", customer.gid)
        digits, forms = phone_forms(customer.phone if member_no is None else member_no)
        if digits is None:
            summary.count("claim_no_phone", ref)
            return None, None, "claim_no_phone"
        if member is None:
            found = []
            for form in sorted(forms):
                read = self._read_member(form)
                if read.found:
                    found.append((form, read))
            if not found:
                return None, None, None
            if len(found) > 1:
                self._review(customer.gid, "claim_memberno_conflict", summary, ref)
                return None, None, "claim_memberno_conflict"
            member_no, member = found[0]
        key = sync.member_no_key(member_no)
        if member.member_type not in SCOPE_TYPES:
            summary.count("claim_member_not_in_scope", ref, member_no)
            return None, member, "claim_member_not_in_scope"
        if self.ledger.pair_unlinked(customer.gid, member_no):
            # Slice B1 (G0 1.6): a voided pair is never linked again by the
            # tool; its own review subject, so an earlier review of the same
            # customer does not swallow the alert.
            self._review("unlinked:" + customer.gid, "claim_pair_unlinked", summary, ref, member_no)
            return None, member, "claim_pair_unlinked"
        if customer.gid in self.links.by_gid or key in self.links.by_key or self.ledger.claim_for_gid(customer.gid) or self.ledger.claim_for_member(member_no):
            self._review(customer.gid, "claim_link_conflict", summary, ref, member_no)
            return None, member, "claim_link_conflict"
        if same_phone is None:
            try:
                same_phone = self.shopify.find_by_phone(digits)
            except PhoneCandidatesTruncated:
                same_phone = None
        if same_phone is None or len(same_phone) != 1 or same_phone[0].gid != customer.gid:
            self._review(customer.gid, "claim_phone_not_unique", summary, ref, member_no)
            return None, member, "claim_phone_not_unique"
        if not sync.full_name_matches(sync.shopify_full_name(customer.first_name, customer.last_name), member.name):
            self._review(customer.gid, "claim_name_mismatch", summary, ref, member_no)
            return None, member, "claim_name_mismatch"
        self.ledger.add_claim(customer.gid, member_no, source, "phone_and_name_match")
        self.linked_gids.add(customer.gid)
        summary.count("claim_linked", ref, member_no)
        return member_no, member, None

    def _review(self, subject: str, code: str, summary: RunSummary, ref: str, member_no: str | None = None) -> None:
        summary.count(code, ref, member_no)
        if self.ledger.review_once(subject, code):
            summary.alert("claim_needs_review", ref, member_no)

    def _repair_applied(self, order_gid: str, summary: RunSummary, ref: str) -> bool:
        """G2 LATER 1: the save landed (the receipt's own ``DONE`` write) but
        the parent stopped before the receipt update: set the receipt
        ``applied`` from that write. Never a second send (I4). True when
        repaired."""

        done = self.ledger.done_write(order_gid)
        if done is None:
            return False
        self.ledger.update_receipt(order_gid, state="applied", code=done["kind"], member_no=done["member_no"],
                                   basis_expiry_local=done["new_expiry_local"])
        summary.count("receipt_repaired_applied", ref, done["member_no"])
        return True

    def _decide_one(self, row: sqlite3.Row, summary: RunSummary, *, run_id: str, apply: bool) -> bool:
        """True stops the run (an unclear write)."""

        gid, order_gid = row["customer_gid"], row["order_gid"]
        ref = self.ledger.ref("o", order_gid)
        if self._repair_applied(order_gid, summary, ref):
            return False
        if self.ledger.write_landed_or_open(order_gid):
            summary.count("write_already_recorded", ref)  # never a second send (I4)
            return False
        if not apply and row["state"] == "ready" and row["observed_at"] and self.now() - dl.parse_created_at(row["observed_at"]) < OBSERVE_REREAD:
            summary.count(row["observed"] or "observed_recently")
            return False
        if (row["state"] == "waiting_member" and gid not in self.linked_gids and row["checked_at"]
                and self.now() - dl.parse_created_at(row["checked_at"]) < timedelta(hours=self.config.waiting_member_recheck_hours)):
            # G2 B2: back off; a #155 link or claim link for the customer
            # makes it due at once.
            summary.count("waiting_member_backoff", ref)
            self._waiting_alert(row, summary, ref)
            return False
        if gid in self.conflict_gids:
            self.ledger.update_receipt(order_gid, state="held", code="link_conflict")
            summary.alert("link_conflict", ref)
            return False
        member_no, source, read, hold_code = self._link(row, summary)
        if member_no is None and hold_code is not None:
            # G2 B2: a claim review, a member out of scope or no phone settles
            # the receipt; it re-opens only when a link appears.
            self.ledger.update_receipt(order_gid, state="held", code=hold_code, waiting_since=None)
            if hold_code == "claim_no_phone":
                summary.alert("qualified_member_no_phone", ref)
            return False
        if member_no is None:
            self._waiting(row, summary, ref)
            return False
        if sync.member_no_key(member_no) in self.conflict_keys:
            self.ledger.update_receipt(order_gid, state="held", code="link_conflict", member_no=member_no, link_source=source)
            summary.alert("link_conflict", ref, member_no)
            return False
        if read is None:
            read = self._read_member(member_no)
        verdict = decide(read, date.fromisoformat(row["order_date_sgt"]))
        common = dict(member_no=member_no, link_source=source, waiting_since=None)
        if verdict.action == "not_in_scope":
            self.ledger.update_receipt(order_gid, state="not_in_scope", code=verdict.code, **common)
            summary.count("member_not_in_scope", ref, member_no)
            return False
        if verdict.action == "hold":
            self.ledger.update_receipt(order_gid, state="held", code=verdict.code, **common)
            summary.alert(verdict.code, ref, member_no)
            return False
        if verdict.action == "no_change":
            self.ledger.update_receipt(order_gid, state="no_change", code=verdict.code, **common)
            summary.count("expiry_already_later", ref, member_no)
            return False
        if not apply:
            self.ledger.update_receipt(order_gid, state="ready", observed=verdict.code, observed_at=dl.ts(self.now()), **common)
            summary.count(verdict.code, ref, member_no)
            return False
        if self._stop_requested():
            summary.stopped = "stop_file_present"
            summary.count(verdict.code, ref, member_no)
            return True
        self.ledger.update_receipt(order_gid, state="ready", **common)
        payload = {"kind": verdict.action, "member_no": member_no, "from_type": FROM_TYPE_BY_KIND[verdict.action], "to_type": TARGET_TYPE,
                   "expected_last_modified_local": read.last_modified_local, "expected_expiry_local": read.expiry_local,
                   "new_expiry_local": verdict.new_expiry_local, "salt": secrets.token_hex(16)}
        check_save_payload(payload)
        deadline = (self.now() + timedelta(seconds=self.config.write_deadline_seconds)).replace(microsecond=0)
        intent = self.ledger.add_intent(run_id=run_id, order_gid=order_gid, payload=payload, deadline=deadline)
        outcome = self._save(intent)
        if outcome.state == "DONE":
            summary.saves += 1
            self.ledger.update_receipt(order_gid, state="applied", code=verdict.action, basis_expiry_local=verdict.new_expiry_local)
            summary.count("upgraded" if verdict.action == "upgrade" else "extended", ref, member_no)
            return False
        if outcome.state in ("SKIPPED", "NOT_WRITTEN"):
            summary.count(outcome.code or "not_written", ref, member_no)  # re-read next run (staff win, Q18)
            return False
        summary.stopped = f"{outcome.state.lower()}:{outcome.code}"
        summary.alert(f"write_{outcome.state.lower()}", ref, member_no)
        if outcome.changed_fields:
            summary.counts["changed_fields"] = len(outcome.changed_fields)
        return True

    def _waiting(self, row: sqlite3.Row, summary: RunSummary, ref: str) -> None:
        """Q6: a qualifying order of a signed-up customer with no AC2 member
        yet waits until #155 creates or links the member; one alert after
        ``waiting_member_alert_hours``."""

        since = row["waiting_since"] or dl.ts(self.now())
        self.ledger.update_receipt(row["order_gid"], state="waiting_member", waiting_since=since, checked_at=dl.ts(self.now()))
        self._waiting_alert(row, summary, ref, since=since)

    def _waiting_alert(self, row: sqlite3.Row, summary: RunSummary, ref: str, *, since: str | None = None) -> None:
        since = since or row["waiting_since"]
        if since and self.now() - dl.parse_created_at(since) >= timedelta(hours=self.config.waiting_member_alert_hours):
            if row["alerted"] != "qualified_member_missing":
                self.ledger.update_receipt(row["order_gid"], alerted="qualified_member_missing")
                summary.alert("qualified_member_missing", ref)
            else:
                summary.count("waiting_member", ref)
        else:
            summary.count("waiting_member", ref)

    def _save(self, intent: sqlite3.Row) -> SaveOutcome:
        write_id = intent["write_id"]
        try:
            outcome = self.ac2.save(payload_of(intent), move_id=write_id, deadline=dl.parse_ts(intent["deadline_at"]),
                                    on_started=lambda pid, created: self.ledger.set_child(write_id, pid, created))
        except Exception:  # noqa: BLE001 - never a blind retry: the child may have saved
            outcome = SaveOutcome("UNCERTAIN", "save_call_failed")
        self.ledger.set_outcome(write_id, outcome.state, outcome.code, outcome.post_last_modified_local if outcome.state == "DONE" else None)
        return outcome

    # -- resolve (read-back only) --------------------------------------------
    def resolve(self) -> dict[str, Any]:
        self.ledger.check_binding(self.config)
        open_rows = self.ledger.open_writes()
        now = self.now()
        for row in open_rows:
            if now < dl.parse_ts(row["deadline_at"]) + timedelta(seconds=dl.WRITER_GRACE_SECONDS):
                raise dl.DirectLocalHold("writer_may_be_live")
            if row["child_pid"] is not None and self.writer_probe(row["child_pid"], row["child_created"]) != "exited":
                raise dl.DirectLocalHold("writer_may_be_live")  # slice B2 (B2-8): "unknown" may be live
        result: dict[str, Any] = {"status": "ok", "command": "resolve", "writes": 0, "resolved": {}, "alerts": []}
        for row in open_rows:
            if row["state"] == "INTENT":
                self.ledger.set_outcome(row["write_id"], "UNCERTAIN", "parent_interrupted")
            claim = self.ledger.root / "claims" / ("fence-" + row["write_id"].removeprefix("write-") + ".claim")
            if not claim.exists():
                state, code, post = "NOT_WRITTEN", "resolved_no_claim", None
            else:
                state, code, post = self._resolve_one(row, claim)
            self.ledger.set_outcome(row["write_id"], state, code, post)
            if state == "DONE":
                receipt = self.ledger.receipt(row["order_gid"])
                if receipt is not None and receipt["state"] in DECIDED_STATES and receipt["state"] != "applied":
                    # G2 recheck LATER 1: a run held (or settled) the receipt
                    # between the save and this resolve. The write is DONE and
                    # the member is saved; the receipt is left as it is,
                    # counted and alerted, and the other rows carry on.
                    result["receipts_left"] = result.get("receipts_left", 0) + 1
                    if "receipt_left_decided" not in result["alerts"]:
                        result["alerts"].append("receipt_left_decided")
                else:
                    self.ledger.update_receipt(row["order_gid"], state="applied", code=row["kind"], basis_expiry_local=row["new_expiry_local"])
            result["resolved"][f"{state}:{code}"] = result["resolved"].get(f"{state}:{code}", 0) + 1
            if state == "HELD":
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
        new_stored = row["new_expiry_local"] + ".000"
        if now.member_type == TARGET_TYPE and now.expiry_local == new_stored and isinstance(before, str) and now.fields_digest == before:
            if isinstance(now.last_modified_local, str) and _LOCAL_STORED_RE.fullmatch(now.last_modified_local):
                return "DONE", "resolved_saved", now.last_modified_local
            return "HELD", "resolved_last_modified_missing", None
        if (now.member_type == row["from_type"] and now.last_modified_local == row["expected_last_modified_local"]
                and now.expiry_local == row["expected_expiry_local"]):
            return "NOT_WRITTEN", "resolved_not_written", None
        return "HELD", "resolved_unclear", None

    # -- release-held (slice B1, G0 1.5) ---------------------------------------
    def _writers_settled(self, rows: Iterable[sqlite3.Row]) -> None:
        """As ``resolve``: a write whose deadline (plus grace) has not passed,
        or whose child still runs, may still be saving."""

        now = self.now()
        for row in rows:
            if now < dl.parse_ts(row["deadline_at"]) + timedelta(seconds=dl.WRITER_GRACE_SECONDS):
                raise dl.DirectLocalHold("writer_may_be_live")
            if row["child_pid"] is not None and self.writer_probe(row["child_pid"], row["child_created"]) != "exited":
                raise dl.DirectLocalHold("writer_may_be_live")  # slice B2 (B2-8): "unknown" may be live

    def release_held(self, *, order_ref: Any, member_last3: Any, finding: Any, apply: bool) -> dict[str, Any]:
        """Clear the latch of one ``HELD`` write after the owner checked that
        member in AutoCount. One read-only ``benefit-member`` read proves the
        finding; nothing is ever sent to AutoCount, the write row stays
        ``HELD`` (never sent again, I4) and the release is a new row. A dry
        run unless ``apply``; every refusal records nothing."""

        if (not isinstance(order_ref, str) or not _ORDER_REF_RE.fullmatch(order_ref) or not isinstance(member_last3, str)
                or not _LAST3_RE.fullmatch(member_last3) or finding not in RELEASE_FINDINGS):
            raise dl.DirectLocalError("release_arguments_invalid")
        self.ledger.check_binding(self.config)
        if self.ledger.open_writes():
            raise dl.DirectLocalError("benefit_uncertain_unresolved")
        held = [row for row in self.ledger.unreleased_held_writes() if self.ledger.ref("o", row["order_gid"]) == order_ref]
        if not held:
            raise dl.DirectLocalError("release_no_held_write")
        write = held[0]
        if write["member_no"][-3:] != member_last3:
            raise dl.DirectLocalError("release_member_mismatch")
        self._writers_settled([write])
        read = self.ac2.member(write["member_no"], secrets.token_hex(16))  # the existing read-only operation, as resolve
        intended = read.found and read.member_type == TARGET_TYPE and read.expiry_local == write["new_expiry_local"] + ".000"
        if intended != (finding == "as_intended"):
            raise dl.DirectLocalError("release_finding_not_proven")
        proof = read.fields_digest if read.found else "sha256:" + hashlib.sha256(b"benefit_member_not_found").hexdigest()
        if not isinstance(proof, str) or not _DIGEST_RE.fullmatch(proof):
            raise dl.BridgeError("bridge_output_invalid")
        receipt = self.ledger.receipt(write["order_gid"])
        if receipt is not None and receipt["state"] == "ready":
            receipt_now = "applied" if finding == "as_intended" else "held"
        else:
            receipt_now = "left_decided"
        count = {"applied": "release_receipt_applied", "held": "release_receipt_held"}.get(receipt_now, "release_receipt_left_decided")
        run_id = f"run-{uuid.uuid4().hex}" if apply else None
        result = {"status": "ok", "command": "release_held" if apply else "release_held_dry_run", "run_id": run_id, "finding": finding,
                  "receipt_now": receipt_now, "held_writes_left": self.ledger.held_writes() - 1, "counts": {count: 1},
                  "refs": [{"ref": order_ref, "code": "write_released", "member_no_masked": mask(write["member_no"])}], "alerts": []}
        if not apply:
            return result
        now = dl.ts(self.now())
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO write_releases(write_id, finding, member_read_digest, run_id, created_at) VALUES (?, ?, ?, ?, ?)",
                         (write["write_id"], finding, proof, run_id, now))
            if receipt_now == "applied":
                conn.execute("UPDATE receipts SET state = 'applied', code = ?, member_no = ?, basis_expiry_local = ?, updated_at = ? "
                             "WHERE order_gid = ?", (write["kind"], write["member_no"], write["new_expiry_local"], now, write["order_gid"]))
            elif receipt_now == "held":
                conn.execute("UPDATE receipts SET state = 'held', code = ?, updated_at = ? WHERE order_gid = ?",
                             (HELD_WRITE_RELEASED, now, write["order_gid"]))
            conn.execute("INSERT INTO runs(run_id, command, started_at, finished_at, summary) VALUES (?, 'release_held', ?, ?, ?)",
                         (run_id, now, now, json.dumps(result, sort_keys=True)))
        return result

    # -- unlink (slice B1, G0 1.6) -----------------------------------------------
    def unlink(self, *, member_last3: Any, reason: Any, accept_applied: bool, apply: bool, customer_ref: Any = None,
               order_ref: Any = None) -> dict[str, Any]:
        """Void one claim link this tool made (by an order or by the index).
        Never touches the #155 sync's links, AutoCount or Shopify. The link row
        stays (history); the void is a new row. Receipts through the link: a
        ``ready`` one loses its member and is linked afresh by the next run;
        a decided one is kept; an ``applied`` one only with
        ``accept_applied`` (one alert: the owner checks that member by hand;
        the tool never downgrades). A dry run unless ``apply``; every refusal
        records nothing.

        The link is named by exactly one selector: ``customer_ref`` (the
        customer's ``c-`` ref) or ``order_ref`` (fix round 1, G2 B13: the
        ``o-`` ref a ``link_conflict``, ``write_held``, ``would_upgrade`` or
        ``upgraded`` entry carries; the link is that receipt's customer's
        active claim link). An ``o-`` ref that names no receipt, a receipt
        with no customer, or a customer with no active claim link (also one
        linked by #155 only) is ``unlink_not_a_claim_link``."""

        selectors = [value for value in (customer_ref, order_ref) if value is not None]
        if (len(selectors) != 1 or not isinstance(selectors[0], str)
                or (customer_ref is not None and not _CUSTOMER_REF_RE.fullmatch(customer_ref)) or not isinstance(member_last3, str)
                or not _LAST3_RE.fullmatch(member_last3) or reason not in UNLINK_REASONS or not isinstance(accept_applied, bool)):
            raise dl.DirectLocalError("unlink_arguments_invalid")
        self.ledger.check_binding(self.config)
        if order_ref is not None:
            # A malformed o- ref names no receipt: the same answer as an unknown one (Controller ruling 2).
            customers = [row["customer_gid"] for row in self.ledger.conn.execute("SELECT order_gid, customer_gid FROM receipts")
                         if self.ledger.ref("o", row["order_gid"]) == order_ref]
            links = [row for row in self.ledger.active_claims() if customers and row["customer_gid"] == customers[0]]
        else:
            links = [row for row in self.ledger.active_claims() if self.ledger.ref("c", row["customer_gid"]) == customer_ref]
        if not links:
            raise dl.DirectLocalError("unlink_not_a_claim_link")  # also a customer linked by #155 only
        link = links[0]
        customer_ref = self.ledger.ref("c", link["customer_gid"])
        if link["member_no"][-3:] != member_last3:
            raise dl.DirectLocalError("unlink_member_mismatch")
        if self.ledger.open_writes():
            raise dl.DirectLocalError("benefit_uncertain_unresolved")
        if any(sync.member_no_key(row["member_no"]) == link["member_no_key"] for row in self.ledger.unreleased_held_writes()):
            raise dl.DirectLocalError("unlink_member_write_held")
        through = [row for row in self.ledger.conn.execute(
            "SELECT * FROM receipts WHERE link_source = 'claim' AND customer_gid = ? AND member_no IS NOT NULL ORDER BY order_gid",
            (link["customer_gid"],)) if sync.member_no_key(row["member_no"]) == link["member_no_key"]]
        if any(row["state"] == "ready" and self.ledger.done_write(row["order_gid"]) is not None for row in through):
            raise dl.DirectLocalError("unlink_receipt_needs_repair")
        if any(row["state"] == "applied" for row in through) and not accept_applied:
            raise dl.DirectLocalError("unlink_has_applied")
        summary = RunSummary("unlink" if apply else "unlink_dry_run")
        summary.count("claim_unlinked", customer_ref, link["member_no"])
        reset = []
        for row in through:
            if row["state"] == "ready":
                reset.append(row["order_gid"])
                summary.count("unlink_receipt_reset")
            elif row["state"] in ("held", "no_change", "not_in_scope"):
                summary.count("unlink_receipt_kept")
            elif row["state"] == "applied":
                summary.count("unlink_receipt_applied")
                summary.alert("unlinked_with_applied", self.ledger.ref("o", row["order_gid"]), row["member_no"])
        run_id = f"run-{uuid.uuid4().hex}" if apply else None
        result = {"status": "ok", "command": summary.command, "run_id": run_id, "reason": reason, "link_source": link["source"],
                  "counts": dict(sorted(summary.counts.items())), "alerts": list(summary.alerts), "refs": list(summary.refs)}
        if not apply:
            return result
        now = dl.ts(self.now())
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO claim_unlinks(link_id, reason, run_id, created_at) VALUES (?, ?, ?, ?)", (link["link_id"], reason, run_id, now))
            for order_gid in reset:
                conn.execute("UPDATE receipts SET member_no = NULL, link_source = NULL, observed = NULL, observed_at = NULL, checked_at = NULL, "
                             "updated_at = ? WHERE order_gid = ? AND state = 'ready'", (now, order_gid))
            conn.execute("INSERT INTO runs(run_id, command, started_at, finished_at, summary) VALUES (?, 'unlink', ?, ?, ?)",
                         (run_id, now, now, json.dumps(result, sort_keys=True)))
        return result

    # -- index (one-off, read only towards Shopify and AC2) -------------------
    def index(self, *, apply: bool, max_members: int) -> dict[str, Any]:
        """Existing AC2 MEMBER and GUEST members with no #155 link and no
        claim: one Shopify phone search each (read only) and the claim rule.
        A dry run unless ``apply``; resumable by a MemberNo cursor."""

        self.ledger.check_binding(self.config)
        if not 1 <= max_members <= INDEX_MAX_LIMIT:
            raise dl.DirectLocalError("benefits_index_max_invalid")
        self.shopify.verify_session(store_gid=self.config.shopify.store_gid)
        self._load_links()
        summary = RunSummary("index_apply" if apply else "index_dry_run")
        run_id = self.ledger.start_run(summary.command)
        examined = 0
        cursor_prefix = "index_cursor:" if apply else "index_dry_cursor:"
        try:
            for member_type in SCOPE_TYPES:
                after = self.ledger.watermark(cursor_prefix + member_type) or ""
                while examined < max_members:
                    page = self.ac2.scan(member_type, after)
                    for member in page:
                        if examined >= max_members:
                            break
                        after = member["member_no"]
                        examined += self._index_one(member, member_type, summary, apply=apply)
                    self.ledger.set_watermark(cursor_prefix + member_type, after)
                    if len(page) < tm.SCAN_PAGE_SIZE:
                        if examined < max_members:
                            summary.count(f"index_{member_type.lower()}_complete")
                        break
                if examined >= max_members:
                    break
        except dl.DirectLocalError as exc:
            summary.stopped = exc.code
            summary.alert(exc.code)
        summary.counts["examined"] = examined
        self.ledger.finish_run(run_id, summary.result(run_id))
        return summary.result(run_id)

    def _index_one(self, row: Mapping[str, Any], member_type: str, summary: RunSummary, *, apply: bool) -> int:
        assert self.links is not None
        member_no = row["member_no"]
        key = sync.member_no_key(member_no)
        if key in self.links.by_key or self.ledger.claim_for_member(member_no) is not None:
            summary.count("index_already_linked")
            return 0
        digits = member_no_phone(member_no)
        if digits is None:
            summary.count("index_member_no_not_phone")
            return 0
        ref = self.ledger.ref("m", key)
        # G2 LATER 2: as the order path, both MemberNo forms of one phone in
        # AC2 (the 10-digit and the 8-digit form) is a review, never a link.
        for other in sorted(sync.member_no_forms(digits) - {key}):
            if self.ac2.member(other, secrets.token_hex(16)).found:
                self._index_review(key, "claim_memberno_conflict", summary, ref, member_no)
                return 1
        try:
            same_phone = self.shopify.find_by_phone(digits)
        except PhoneCandidatesTruncated:
            self._index_review(key, "claim_phone_not_unique", summary, ref, member_no)
            return 1
        if not same_phone:
            summary.count("index_no_shopify_customer")
            return 1
        if len(same_phone) > 1:
            self._index_review(key, "claim_phone_not_unique", summary, ref, member_no)
            return 1
        customer = same_phone[0]
        if self.ledger.pair_unlinked(customer.gid, member_no):
            summary.count("index_pair_unlinked")  # slice B1: a voided pair is never linked again
            return 1
        read = MemberRead(True, member_no=member_no, member_type=member_type, name=row.get("name"))
        if not apply:
            name_ok = sync.full_name_matches(sync.shopify_full_name(customer.first_name, customer.last_name), read.name)
            linked = customer.gid in self.links.by_gid or self.ledger.claim_for_gid(customer.gid) is not None
            summary.count("index_would_claim" if name_ok and not linked else "index_would_review", None)
            return 1
        self.claim(customer, summary, source="index", member=read, member_no=member_no, same_phone=same_phone)
        return 1

    def _index_review(self, key: str, code: str, summary: RunSummary, ref: str, member_no: str) -> None:
        summary.count(code, ref, member_no)
        if self.ledger.review_once("m:" + key, code):
            summary.alert("claim_needs_review", ref, member_no)

    # -- status ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        self.ledger.check_binding(self.config)
        conn = self.ledger.conn
        receipts = {row[0]: row[1] for row in conn.execute("SELECT state, COUNT(*) FROM receipts GROUP BY state")}
        observed = {row[0]: row[1] for row in conn.execute("SELECT observed, COUNT(*) FROM receipts WHERE state = 'ready' AND observed IS NOT NULL GROUP BY observed")}
        writes = {f"{row[0]}:{row[1]}": row[2] for row in conn.execute("SELECT kind, state, COUNT(*) FROM writes GROUP BY kind, state")}
        active, voided = self.ledger.claim_counts()
        return {"status": "ok", "command": "status", "receipts": receipts, "observed": observed, "writes": writes,
                "held_writes": self.ledger.held_writes(),
                "claim_links": active, "claim_links_voided": voided, "rule_settings_match": self.ledger.rule_settings_match(self.config),
                "reviews": conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
                "watermark_set": self.ledger.watermark(WATERMARK) is not None, "stop_file_present": self._stop_requested()}


# --------------------------------------------------------------------------
# Shared write lock with the #268 type move
# --------------------------------------------------------------------------

@contextmanager
def type_move_lock(config: BenefitsConfig) -> Iterator[None]:
    """``apply`` holds the type move's own ledger lock for the whole run, so
    the two tools never write AutoCount members at the same time: while this
    is held the type move refuses (``ledger_locked_by_another_owner``), and
    while a type move command holds it this run refuses (``type_move_running``)."""

    if config.type_move_state_root is None:
        yield
        return
    root = Path(config.type_move_state_root)
    if not (root / tm.LEDGER_FILE).is_file():
        raise dl.DirectLocalError("type_move_state_root_invalid")
    try:
        lock = dl._OwnerLock(root / TYPE_MOVE_LOCK_FILE)
    except dl.DirectLocalError:
        raise dl.DirectLocalError("type_move_running") from None
    try:
        yield
    finally:
        lock.release()


# --------------------------------------------------------------------------
# Digest (read only)
# --------------------------------------------------------------------------

DIGEST_SCHEMA = "xb.directlocal.member_benefits.digest.v1"
_DIGEST_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_DIGEST_REF_RE = re.compile(r"^[ocm]-[0-9a-f]{1,64}$")
_DIGEST_DIGITS_RE = re.compile(r"[0-9]{4}")  # slice B2 (B2-6): a gateway name with a run of 4 digits is shown as "other"


def ledger_digest(root: Path, *, hours: int, now: datetime, config: BenefitsConfig) -> dict[str, Any]:
    """Read-only daily digest: run counts, counts and alerts per code, alert
    refs (keyed refs and masked MemberNos only), open totals, and the gift
    card counts (gateway names as plain code words only)."""

    path = Path(root) / LEDGER_FILE
    if not path.is_file():
        raise dl.DirectLocalError("benefits_ledger_missing")
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        raise dl.DirectLocalError("benefits_ledger_unreadable") from None
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise dl.DirectLocalError("benefits_ledger_not_recognised")  # slice B1: a ledger made by older code
        meta = conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        if meta is None:
            raise dl.DirectLocalError("benefits_ledger_not_initialised")
        BenefitsLedger._check(meta, config)
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
                if isinstance(masked, str) and re.fullmatch(r"\*{3,}[^*]{1,3}", masked):
                    item["member_no_masked"] = masked
                refs.append(item)
        receipts = {row[0]: row[1] for row in conn.execute("SELECT state, COUNT(*) FROM receipts GROUP BY state")}
        observed = {row[0]: row[1] for row in conn.execute("SELECT observed, COUNT(*) FROM receipts WHERE state = 'ready' AND observed IS NOT NULL GROUP BY observed")}
        # Gift cards (all receipts, not only the last N hours): how many
        # receipts had an amount paid by gift card, and on how many receipts
        # each gateway name was seen. A name is shown only as a plain code
        # word; anything else (a manual payment method's free text) is "other".
        gateways: dict[str, int] = {}
        for row in conn.execute("SELECT payment_gateways FROM receipts WHERE payment_gateways IS NOT NULL"):
            try:
                names = json.loads(row[0])
            except (json.JSONDecodeError, RecursionError):
                names = []
            for name in {n if isinstance(n, str) and _DIGEST_CODE_RE.fullmatch(n) and not _DIGEST_DIGITS_RE.search(n) else "other"
                         for n in (names if isinstance(names, list) else [])}:
                gateways[name] = gateways.get(name, 0) + 1
        return {
            "schema": DIGEST_SCHEMA, "status": "ok", "autocount_book": meta["autocount_book"], "hours": hours,
            "rule_settings_match": hmac.compare_digest(meta["rule_fingerprint"], config.rule_fingerprint()),
            "runs": {"completed": completed, "unfinished": unfinished}, "counts": dict(sorted(counts.items())),
            "alerts": dict(sorted(alerts.items())), "alert_refs": refs, "receipts": receipts,
            "shadow": {"would_upgrade": observed.get("would_upgrade", 0), "would_extend": observed.get("would_extend", 0)},
            "gift_cards": {
                "receipts_paid_by_gift_card": conn.execute("SELECT COUNT(*) FROM receipts WHERE gift_card_cents > 0").fetchone()[0],
                "gateways_seen": dict(sorted(gateways.items())),
            },
            "open": {
                "uncertain_writes": conn.execute("SELECT COUNT(*) FROM writes WHERE state IN ('INTENT', 'UNCERTAIN')").fetchone()[0],
                "held_writes": conn.execute("SELECT COUNT(*) FROM writes w WHERE w.state = 'HELD' AND NOT EXISTS "
                                            "(SELECT 1 FROM write_releases r WHERE r.write_id = w.write_id)").fetchone()[0],
                "waiting_member": receipts.get("waiting_member", 0),
                "claim_links": conn.execute("SELECT COUNT(*) FROM active_claim_links").fetchone()[0],
                "claim_links_voided": conn.execute("SELECT COUNT(*) FROM claim_unlinks").fetchone()[0],
            },
        }
    except sqlite3.Error:
        raise dl.DirectLocalError("benefits_ledger_unreadable") from None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def build_real_engine(config: BenefitsConfig, ledger: BenefitsLedger, *, environ: Mapping[str, str]) -> BenefitsEngine:
    transport = direct.UrllibTransport()
    shopify = BenefitsShopifyClient(
        shop_domain=config.shopify.shop_domain, api_version=config.shopify.api_version,
        token_provider=token_provider(config.shopify, environ, transport, dl.utc_now, root=ledger.root),
        transport=transport, timeout=config.http_timeout_seconds,
    )
    env = None
    if config.ac2_credential_target is not None:
        env = sync.load_ac2_credential(config.ac2_credential_target, refused_user_ids=config.ac2_refused_user_ids).child_environment(environ)
    elif config.autocount_book == sync.LIVE_BOOK:
        raise dl.DirectLocalError("ac2_credential_target_unconfigured")  # parse_config refuses this first
    ac2 = BenefitsBridgeClient(dl.default_bridge_command(BRIDGE_SCRIPT), autocount_book=config.autocount_book, claims=ledger.root / "claims",
                               env=env, deadline_seconds=config.write_deadline_seconds)
    return BenefitsEngine(config, ledger, shopify=shopify, ac2=ac2)


COMMANDS = ("check-config", "init", "status", "observe", "apply", "resolve", "index", "digest", "release-held", "unlink")


def main(argv: list[str] | None = None, *, engine_factory: Callable[[BenefitsConfig, BenefitsLedger], BenefitsEngine] | None = None,
         clock: Callable[[], datetime] = dl.utc_now) -> int:
    parser = argparse.ArgumentParser(description="#268 member core: S$300 online order -> AC2 MEMBER + 2-year expiry; claim links")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true", help="index, release-held, unlink: record in this tool's ledger (default: dry run)")
    parser.add_argument("--max", type=int, default=200, help="index only: members examined this run (1-1000)")
    parser.add_argument("--hours", type=int, default=24, help="digest only: the last N hours of runs (1-720)")
    parser.add_argument("--order-ref", help="release-held: the o- keyed ref of the order whose HELD write is released; "
                                            "unlink: the o- keyed ref of a receipt whose customer's claim link is voided")
    parser.add_argument("--member-last3", help="release-held, unlink: the last 3 characters of the MemberNo")
    parser.add_argument("--customer-ref", help="unlink only: the c- keyed ref of the customer whose claim link is voided "
                                               "(exactly one of --customer-ref and --order-ref)")
    parser.add_argument("--reason", help="unlink only: wrong_person, customers_merged, member_renumbered or other")
    parser.add_argument("--accept-applied", action="store_true", help="unlink only: void even when a receipt through the link is applied")
    parser.add_argument("--finding", help="release-held only: as_intended or not_as_intended (what the owner saw in AutoCount)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check-config":
            _emit({"status": "ok", **config.summary()})
            return 0
        # The write switch is refused before any login, read or ledger write.
        if args.command == "apply" and not config.benefits_write_enabled:
            raise dl.DirectLocalError("benefits_write_disabled")
        root = state_root(config)
        if args.command == "digest":
            if not 1 <= args.hours <= 720:
                raise dl.DirectLocalError("digest_hours_invalid")
            _emit(ledger_digest(root, hours=args.hours, now=clock(), config=config))
            return 0
        ledger = BenefitsLedger(root, clock=clock)
        try:
            if args.command == "init":
                _emit(ledger.init_meta(config))
                return 0
            ledger.check_binding(config)
            factory = engine_factory or (lambda cfg, led: build_real_engine(cfg, led, environ=os.environ))
            if args.command == "status":
                _emit(BenefitsEngine(config, ledger, shopify=None, ac2=None).status())
                return 0
            engine = factory(config, ledger)
            if args.command == "observe":
                result = engine.run(apply=False)
            elif args.command == "apply":
                with type_move_lock(config):
                    result = engine.run(apply=True)
            elif args.command == "resolve":
                result = engine.resolve()
            elif args.command == "release-held":
                result = engine.release_held(order_ref=args.order_ref, member_last3=args.member_last3, finding=args.finding, apply=args.apply)
            elif args.command == "unlink":
                result = engine.unlink(customer_ref=args.customer_ref, order_ref=args.order_ref, member_last3=args.member_last3,
                                       reason=args.reason, accept_applied=args.accept_applied, apply=args.apply)
            else:
                result = engine.index(apply=args.apply, max_members=args.max)
            _emit(result)
            return 3 if result.get("alerts") else 0
        finally:
            ledger.close()
    except dl.DirectLocalError as exc:
        _emit({"status": "refused", "code": exc.code})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
