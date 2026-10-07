"""Authoritative Shopify read, cutover baseline and M1 admission decisions.

Shopify is authoritative; webhook bodies are never trusted. Every decision is
taken from a fresh read-only Admin GraphQL read (``read_customers`` only).

Cutover (#155 targeted G2 re-entry): ``C`` is the sealed baseline's
``capture_started_at``, read from the gateway PostgreSQL clock (UTC, whole
second) before page 1. Shopify ``Customer.createdAt`` is the only temporal
classifier: historical iff ``createdAt < C``; post-cutover iff
``createdAt >= C``. Webhook arrival time and page position never classify.

Admission law (M1 = new canonical ``member-mg`` create only):

* no ``member-mg`` tag                         -> NOT_ELIGIBLE (re-evaluable)
* GID in the sealed historical baseline       -> EXCLUDED_BASELINE
  (MANUAL_REVIEW ``baseline_created_at_conflict`` if a re-read reports
  ``createdAt >= C``; never ADMIT)
* ``member-legacy`` / ``XB Member`` present    -> MANUAL_REVIEW
* customer created before the cutover          -> MANUAL_REVIEW (an update on
  a pre-existing GID never invents a new signup)
* Name/start/expiry absent                     -> bounded wait, then MANUAL_REVIEW
* malformed date/phone/email, expiry < start   -> MANUAL_REVIEW
* otherwise                                    -> ADMIT exactly once

Only GIDs, counts, digests and PII-free codes are ever persisted by this
module; the create payload leaves it only as an AEAD envelope.

Baseline transient-read recovery (#155 Web-directed G3 continuation): the
capture retries only retryable reads (GraphQL ``THROTTLED``, HTTP
429/500/502/503/504, transport failure) of the exact same page/cursor within
deterministic per-page, global and wait budgets; ``C`` is never recomputed and
exhaustion fails closed before anything is persisted.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from .canonical import CanonicalizationError, canonical_phone, normalize_email, normalize_spaces
from .config import ShopifyM1Config
from .protected_payload import ProtectedPayloadCipher, envelope_digest, validate_create_payload
from .shopify_webhook import GID_RE

MEMBER_TAG = "member-mg"
REVIEW_TAGS = ("member-legacy", "XB Member")
BASELINE_PAGE_SIZE = 250
BASELINE_MAX_PAGES = 4000
# Bounded same-cursor recovery of retryable baseline page reads.
BASELINE_RETRY_MAX_ATTEMPTS_PER_PAGE = 6
BASELINE_RETRY_MAX_TOTAL_RETRIES = 60
BASELINE_RETRY_MAX_TOTAL_WAIT_SECONDS = 900
BASELINE_RETRY_MAX_SINGLE_WAIT_SECONDS = 60
BASELINE_RETRY_MIN_WAIT_SECONDS = 1
BASELINE_RETRYABLE_READ_CODES = frozenset({"shopify_graphql_throttled", "shopify_http_error", "shopify_transport_failed"})
_RETRY_AFTER_STATUSES = frozenset({429, 503})
_RETRY_AFTER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# RFC 3339 date-time with an explicit ``Z`` or numeric offset; no naive,
# date-only or basic-format value is ever trusted as a creation instant.
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")

CUSTOMER_QUERY = """query XbShopifyMemberRead($id: ID!) {
  customer(id: $id) {
    id
    createdAt
    tags
    firstName
    lastName
    defaultEmailAddress { emailAddress }
    defaultPhoneNumber { phoneNumber }
    startDate: metafield(namespace: "membership", key: "start_date") { type value }
    expiryDate: metafield(namespace: "membership", key: "expiry_date") { type value }
  }
}"""

BASELINE_QUERY = """query XbShopifyBaselinePage($first: Int!, $after: String) {
  customers(first: $first, after: $after, sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    nodes { id createdAt tags }
  }
}"""


@dataclass(frozen=True, slots=True)
class ShopifyThrottleStatus:
    """Numeric GraphQL cost budget only (``extensions.cost``); each field is
    ``None`` when absent, non-numeric, non-finite or negative."""

    requested_cost: float | None = None
    currently_available: float | None = None
    restore_rate: float | None = None
    maximum_available: float | None = None


class ShopifyReadError(RuntimeError):
    """Bounded read failure. Only the code, the retryable flag, a normalised
    Retry-After (whole seconds) and numeric throttle budget may be carried;
    no token, header, body, cursor, GID or provider message."""

    def __init__(self, code: str, *, retryable: bool = False, retry_after_seconds: int | None = None, throttle: ShopifyThrottleStatus | None = None):
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.throttle = throttle
        super().__init__(code)

    def __repr__(self) -> str:
        return f"ShopifyReadError(code={self.code!r}, retryable={self.retryable!r})"


def _budget_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer beyond float range is unusable metadata
        return None
    return number if math.isfinite(number) and number >= 0 else None


def parse_throttle_status(response: Mapping[str, Any]) -> ShopifyThrottleStatus | None:
    """Validated top-level ``extensions.cost`` throttle budget, if present."""

    extensions = response.get("extensions")
    cost = extensions.get("cost") if isinstance(extensions, Mapping) else None
    if not isinstance(cost, Mapping):
        return None
    status = cost.get("throttleStatus")
    status = status if isinstance(status, Mapping) else {}
    return ShopifyThrottleStatus(
        requested_cost=_budget_number(cost.get("requestedQueryCost")),
        currently_available=_budget_number(status.get("currentlyAvailable")),
        restore_rate=_budget_number(status.get("restoreRate")),
        maximum_available=_budget_number(status.get("maximumAvailable")),
    )


def parse_retry_after_seconds(value: Any) -> int | None:
    """Shopify ``Retry-After`` is numeric seconds (e.g. ``2.0``): a finite
    non-negative integer/decimal is rounded UP to whole seconds (minimum 1).
    Anything else (date, negative, NaN/inf, garbage) is unusable -> ``None``."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _RETRY_AFTER_RE.fullmatch(text):
        return None
    try:
        seconds = int(Decimal(text).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError):
        return None
    return max(seconds, BASELINE_RETRY_MIN_WAIT_SECONDS)


def http_read_error(status: int, retry_after: Any = None) -> ShopifyReadError:
    """Classify an HTTP failure; Retry-After is kept only for 429/503."""

    retryable = status in {429, 500, 502, 503, 504}
    seconds = parse_retry_after_seconds(retry_after) if status in _RETRY_AFTER_STATUSES else None
    return ShopifyReadError("shopify_http_error", retryable=retryable, retry_after_seconds=seconds)


class BaselineCaptureError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("clock_must_be_timezone_aware")
    return current.astimezone(timezone.utc)


class ShopifyAdminClient:
    """Read-only Admin GraphQL transport. Token is held in memory only."""

    def __init__(self, config: ShopifyM1Config, token: str, *, transport: Callable[[str, bytes, Mapping[str, str], int], bytes] | None = None, timeout: int = 10):
        if config.shop_domain is None:
            raise ShopifyReadError("shopify_shop_domain_required")
        if not isinstance(token, str) or not token or any(ch.isspace() for ch in token):
            raise ShopifyReadError("shopify_admin_token_missing")
        self._url = f"https://{config.shop_domain}/admin/api/{config.api_version}/graphql.json"
        self._token = token
        self._transport = transport or self._urllib_transport
        self._timeout = timeout

    def __repr__(self) -> str:
        return "ShopifyAdminClient()"

    @staticmethod
    def _urllib_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: int) -> bytes:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
                return response.read(4_194_304)
        except urllib.error.HTTPError as exc:
            headers = exc.headers
            raise http_read_error(exc.code, headers.get("Retry-After") if headers is not None else None) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ShopifyReadError("shopify_transport_failed", retryable=True) from None

    def query(self, document: str, variables: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps({"query": document, "variables": dict(variables)}, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json", "X-Shopify-Access-Token": self._token}
        raw = self._transport(self._url, body, headers, self._timeout)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            raise ShopifyReadError("shopify_response_invalid") from None
        if not isinstance(value, Mapping):
            raise ShopifyReadError("shopify_response_invalid")
        errors = value.get("errors")
        if errors:
            throttled = isinstance(errors, list) and any(
                isinstance(item, Mapping) and isinstance(item.get("extensions"), Mapping) and item["extensions"].get("code") == "THROTTLED"
                for item in errors
            )
            if throttled:
                raise ShopifyReadError("shopify_graphql_throttled", retryable=True, throttle=parse_throttle_status(value))
            raise ShopifyReadError("shopify_graphql_error")
        data = value.get("data")
        if not isinstance(data, Mapping):
            raise ShopifyReadError("shopify_response_invalid")
        return data

    def read_customer(self, gid: str) -> Mapping[str, Any] | None:
        if not GID_RE.fullmatch(gid):
            raise ShopifyReadError("shopify_customer_gid_invalid")
        data = self.query(CUSTOMER_QUERY, {"id": gid})
        customer = data.get("customer")
        if customer is None:
            return None
        if not isinstance(customer, Mapping) or customer.get("id") != gid:
            raise ShopifyReadError("shopify_customer_identity_mismatch")
        return customer

    def baseline_page(self, after: str | None) -> Mapping[str, Any]:
        data = self.query(BASELINE_QUERY, {"first": BASELINE_PAGE_SIZE, "after": after})
        page = data.get("customers")
        if not isinstance(page, Mapping):
            raise ShopifyReadError("shopify_response_invalid")
        return page


def baseline_digest(gids: list[str] | tuple[str, ...]) -> str:
    """Deterministic sorted-GID digest; codepoint order equals C-collation for ASCII GIDs."""

    ordered = sorted(gids)
    return "sha256:" + hashlib.sha256("\n".join(ordered).encode("ascii")).hexdigest()


def _has_tag(tags: Any, wanted: str) -> bool:
    return isinstance(tags, list) and any(isinstance(tag, str) and tag.strip().casefold() == wanted.casefold() for tag in tags)


def parse_created_at(value: Any) -> datetime | None:
    """Strict RFC 3339 instant in UTC, or ``None`` when absent/malformed/naive."""

    if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def baseline_retry_wait_seconds(error: ShopifyReadError, retry_number: int) -> int:
    """Deterministic wait (whole seconds, no jitter) before same-cursor retry
    ``retry_number`` (1-based, per page) of a retryable read.

    Raises ``BaselineCaptureError`` for an impossible query cost or a
    Retry-After above the single-wait bound; never rounds a provider delay down.
    """

    throttle = error.throttle
    if throttle is not None and throttle.requested_cost is not None and throttle.maximum_available is not None and throttle.requested_cost > throttle.maximum_available:
        raise BaselineCaptureError("baseline_query_cost_exceeds_bucket")
    if error.retry_after_seconds is not None:
        if error.retry_after_seconds > BASELINE_RETRY_MAX_SINGLE_WAIT_SECONDS:
            raise BaselineCaptureError("baseline_read_retry_after_exceeds_bound")
        return max(error.retry_after_seconds, BASELINE_RETRY_MIN_WAIT_SECONDS)
    if (
        error.code == "shopify_graphql_throttled" and throttle is not None
        and None not in (throttle.requested_cost, throttle.currently_available, throttle.restore_rate, throttle.maximum_available)
        and throttle.restore_rate > 0
    ):
        deficit = throttle.requested_cost - throttle.currently_available
        wait = math.ceil(deficit / throttle.restore_rate) if deficit > 0 else BASELINE_RETRY_MIN_WAIT_SECONDS
        return min(max(wait, BASELINE_RETRY_MIN_WAIT_SECONDS), BASELINE_RETRY_MAX_SINGLE_WAIT_SECONDS)
    return min(2 ** (retry_number - 1), BASELINE_RETRY_MAX_SINGLE_WAIT_SECONDS)


def capture_member_mg_baseline(page_reader: Callable[[str | None], Mapping[str, Any]], *, cutover_at: datetime, max_pages: int = BASELINE_MAX_PAGES, sleep: Callable[[int], None] | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Exhaustive cursor scan of every customer until ``hasNextPage`` is false.

    The scan reads all customers (not a search query, whose index may lag) and
    partitions exact ``member-mg`` GIDs by Shopify ``createdAt`` against the
    fixed cutover ``C``: ``(historical, transition)`` where historical is
    ``createdAt < C`` and transition is ``createdAt >= C`` (equality is
    post-cutover). Any malformed page, missing/repeated cursor, duplicate GID,
    missing/malformed/offset-less ``createdAt``, non-retryable read error or
    page-limit overrun fails closed and yields nothing, so a partial capture
    can never be sealed. ``createdAt`` is used only here and is never returned
    or stored.

    A retryable read (``BASELINE_RETRYABLE_READ_CODES``) is retried with the
    exact same ``after`` within ``BASELINE_RETRY_*`` budgets; every budget is
    checked before sleeping and exhaustion fails closed with
    ``baseline_read_retry_exhausted:<client code>``. A page contributes data
    only after it is accepted whole; ``C`` is never recomputed here.
    """

    if not isinstance(cutover_at, datetime) or cutover_at.tzinfo is None:
        raise BaselineCaptureError("baseline_cutover_invalid")
    cutover = cutover_at.astimezone(timezone.utc)
    pause = sleep or time.sleep
    after: str | None = None
    seen_cursors: set[str] = set()
    seen_ids: set[str] = set()
    historical: list[str] = []
    transition: list[str] = []
    total_retries = 0
    total_wait = 0
    for _ in range(max_pages):
        page_attempts = 0
        while True:
            page_attempts += 1
            try:
                page = page_reader(after)
                break
            except ShopifyReadError as exc:
                if not (exc.retryable and exc.code in BASELINE_RETRYABLE_READ_CODES):
                    raise BaselineCaptureError(f"baseline_read_failed:{exc.code}") from None
                wait = baseline_retry_wait_seconds(exc, page_attempts)
                if (
                    page_attempts >= BASELINE_RETRY_MAX_ATTEMPTS_PER_PAGE
                    or total_retries >= BASELINE_RETRY_MAX_TOTAL_RETRIES
                    or total_wait + wait > BASELINE_RETRY_MAX_TOTAL_WAIT_SECONDS
                ):
                    raise BaselineCaptureError(f"baseline_read_retry_exhausted:{exc.code}") from None
            pause(wait)
            total_retries += 1
            total_wait += wait
        if not isinstance(page, Mapping):
            raise BaselineCaptureError("baseline_page_invalid")
        info, nodes = page.get("pageInfo"), page.get("nodes")
        if not isinstance(info, Mapping) or not isinstance(nodes, list):
            raise BaselineCaptureError("baseline_page_invalid")
        has_next = info.get("hasNextPage")
        if not isinstance(has_next, bool):
            raise BaselineCaptureError("baseline_page_info_invalid")
        page_ids: set[str] = set()
        page_historical: list[str] = []
        page_transition: list[str] = []
        for node in nodes:
            if not isinstance(node, Mapping):
                raise BaselineCaptureError("baseline_node_invalid")
            gid, tags = node.get("id"), node.get("tags")
            if not isinstance(gid, str) or not GID_RE.fullmatch(gid) or not isinstance(tags, list):
                raise BaselineCaptureError("baseline_node_invalid")
            if gid in seen_ids or gid in page_ids:
                raise BaselineCaptureError("baseline_duplicate_gid")
            page_ids.add(gid)
            created_at = parse_created_at(node.get("createdAt"))
            if created_at is None:
                raise BaselineCaptureError("baseline_created_at_invalid")
            if _has_tag(tags, MEMBER_TAG):
                (page_historical if created_at < cutover else page_transition).append(gid)
        cursor = info.get("endCursor")
        if has_next:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
                raise BaselineCaptureError("baseline_cursor_missing")
            if cursor in seen_cursors or cursor == after:
                raise BaselineCaptureError("baseline_cursor_repeated")
        # The page is structurally accepted whole; only now does it contribute.
        seen_ids.update(page_ids)
        historical.extend(page_historical)
        transition.extend(page_transition)
        if not has_next:
            return tuple(sorted(historical)), tuple(sorted(transition))
        seen_cursors.add(cursor)
        after = cursor
    raise BaselineCaptureError("baseline_page_limit_exceeded")


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    outcome: str  # ADMIT | WAIT | NOT_ELIGIBLE | EXCLUDED_BASELINE | MANUAL_REVIEW
    code: str | None = None
    create_payload: dict[str, Any] | None = None

    def __repr__(self) -> str:  # never render the payload
        return f"AdmissionDecision(outcome={self.outcome!r}, code={self.code!r})"


def _metafield_date(field: Any) -> tuple[str | None, str | None]:
    """Return (value, error_code). ``None`` value with no error means absent."""

    if field is None:
        return None, None
    if not isinstance(field, Mapping) or field.get("type") != "date":
        return None, "membership_date_malformed"
    value = field.get("value")
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None, "membership_date_malformed"
    try:
        date.fromisoformat(value)
    except ValueError:
        return None, "membership_date_malformed"
    return value, None


def evaluate_profile(customer: Mapping[str, Any] | None, *, in_baseline: bool, cutover_at: datetime, profile_complete_required: bool) -> AdmissionDecision:
    """Pure decision over one authoritative read. ``profile_complete_required``
    is true once the bounded wait budget is exhausted."""

    if customer is None:
        return AdmissionDecision("NOT_ELIGIBLE", "customer_not_found")
    tags = customer.get("tags")
    if not _has_tag(tags, MEMBER_TAG):
        return AdmissionDecision("NOT_ELIGIBLE", "not_member_mg")
    created_at = parse_created_at(customer.get("createdAt"))
    if in_baseline:
        # Sealed membership is immutable; createdAt is read-only in Shopify.
        # A contradiction is surfaced for review and never admitted.
        if created_at is not None and created_at >= cutover_at:
            return AdmissionDecision("MANUAL_REVIEW", "baseline_created_at_conflict")
        return AdmissionDecision("EXCLUDED_BASELINE")
    if any(_has_tag(tags, tag) for tag in REVIEW_TAGS):
        return AdmissionDecision("MANUAL_REVIEW", "legacy_tag_present")
    if created_at is None:
        return AdmissionDecision("MANUAL_REVIEW", "created_at_malformed")
    if created_at < cutover_at:
        return AdmissionDecision("MANUAL_REVIEW", "preexisting_customer_not_new_signup")

    start, start_error = _metafield_date(customer.get("startDate"))
    expiry, expiry_error = _metafield_date(customer.get("expiryDate"))
    if start_error or expiry_error:
        return AdmissionDecision("MANUAL_REVIEW", "membership_date_malformed")
    parts = []
    for key in ("firstName", "lastName"):
        item = customer.get(key)
        if item is not None and not isinstance(item, str):
            return AdmissionDecision("MANUAL_REVIEW", "name_missing")
        if isinstance(item, str) and normalize_spaces(item):
            parts.append(normalize_spaces(item))
    name = " ".join(parts)
    missing = "name_missing" if not name else "membership_dates_missing" if (start is None or expiry is None) else None
    if missing:
        return AdmissionDecision("MANUAL_REVIEW", missing) if profile_complete_required else AdmissionDecision("WAIT", missing)
    if expiry < start:
        return AdmissionDecision("MANUAL_REVIEW", "membership_expiry_before_start")
    if len(name) > 200:
        return AdmissionDecision("MANUAL_REVIEW", "name_missing")

    phone_node, email_node = customer.get("defaultPhoneNumber"), customer.get("defaultEmailAddress")
    mobile_phone = email_address = None
    if phone_node is not None:
        raw = phone_node.get("phoneNumber") if isinstance(phone_node, Mapping) else None
        try:
            mobile_phone = canonical_phone(raw)
        except CanonicalizationError:
            return AdmissionDecision("MANUAL_REVIEW", "phone_malformed")
    if email_node is not None:
        raw = email_node.get("emailAddress") if isinstance(email_node, Mapping) else None
        try:
            email_address = normalize_email(raw)
        except CanonicalizationError:
            return AdmissionDecision("MANUAL_REVIEW", "email_malformed")
    payload = validate_create_payload({
        "name": name, "mobile_phone": mobile_phone, "email_address": email_address,
        "register_date": start, "expiry_date": expiry,
    })
    return AdmissionDecision("ADMIT", None, payload)


class ShopifyAdmissionProcessor:
    """Asynchronous authoritative re-read of pending GIDs. Fail closed whenever
    admission is disabled or the bound baseline does not verify."""

    def __init__(self, config: ShopifyM1Config, repository: Any, reader: Any, cipher: ProtectedPayloadCipher, *, clock: Callable[[], datetime] | None = None):
        self.config = config
        self.repository = repository
        self.reader = reader
        self.cipher = cipher
        self.clock = clock

    def _now(self) -> datetime:
        return _utc(self.clock() if self.clock else None)

    def run_once(self, *, limit: int = 20) -> dict[str, int]:
        counts = {"processed": 0, "admitted": 0, "waiting": 0, "review": 0, "excluded": 0, "not_eligible": 0, "deferred": 0}
        control = self.repository.shopify_admission_gate()
        if control is None:
            return counts
        baseline, cutover_at = control
        for admission in self.repository.due_shopify_admissions(now=self._now(), limit=limit):
            counts["processed"] += 1
            gid = admission.customer_gid
            try:
                customer = self.reader.read_customer(gid)
            except ShopifyReadError as exc:
                if exc.retryable:
                    self.repository.defer_shopify_admission(gid, expected_state_version=admission.state_version, next_check_at=self._now() + timedelta(seconds=self.config.profile_wait_seconds), now=self._now())
                    counts["deferred"] += 1
                    continue
                customer, decision = None, AdmissionDecision("MANUAL_REVIEW", "profile_read_failed")
            else:
                decision = evaluate_profile(
                    customer,
                    in_baseline=self.repository.shopify_baseline_contains(baseline, gid),
                    cutover_at=cutover_at,
                    profile_complete_required=admission.check_count + 1 >= self.config.profile_wait_max_checks,
                )
            del customer
            if decision.outcome == "ADMIT":
                job_id = f"job-{uuid.uuid4().hex}"
                envelope = self.cipher.encrypt(job_id, decision.create_payload or {})
                self.repository.admit_shopify_member(
                    gid, expected_state_version=admission.state_version, baseline_id=baseline,
                    job_id=job_id, envelope=envelope, payload_digest=envelope_digest(envelope),
                    key_id=self.cipher.key_id, now=self._now(),
                )
                counts["admitted"] += 1
            elif decision.outcome == "WAIT":
                self.repository.wait_shopify_admission(gid, expected_state_version=admission.state_version, code=decision.code or "membership_dates_missing", next_check_at=self._now() + timedelta(seconds=self.config.profile_wait_seconds), now=self._now())
                counts["waiting"] += 1
            else:
                self.repository.resolve_shopify_admission(gid, expected_state_version=admission.state_version, outcome=decision.outcome, code=decision.code, baseline_id=baseline, now=self._now())
                counts[{"MANUAL_REVIEW": "review", "EXCLUDED_BASELINE": "excluded", "NOT_ELIGIBLE": "not_eligible"}[decision.outcome]] += 1
        return counts


def _report_cycle_failure(code: str) -> None:
    import sys

    sys.stderr.write(f"shopify_admission_cycle_failed:{code}\n")


def run_admission_loop(processor: ShopifyAdmissionProcessor, stop: threading.Event, interval_seconds: int, *, on_failure: Callable[[str], None] = _report_cycle_failure) -> None:
    """Background loop for the dedicated receiver process. A failed cycle is
    reported as a bounded code (never a value) and retried next interval;
    pending GIDs stay PENDING, so nothing is lost or silently admitted."""

    while not stop.is_set():
        try:
            processor.run_once()
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            on_failure(code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_.:-]{1,80}", code) else type(exc).__name__)
        stop.wait(interval_seconds)
