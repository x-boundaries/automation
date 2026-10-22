"""#155 DirectLocal core: attended Shopify connector capture -> local SQLite
ledger -> existing AutoCount MemberCommand adapter (through a child bridge).

No gateway, webhook receiver, PostgreSQL, n8n or network listener is used.

Data path
---------
* A Claude Code PostToolUse hook (``scripts/directlocal_connector_hook.py``)
  writes the hook-delivered ``tool_input``/``tool_response`` JSON of the pinned
  connector queries into ``<state_root>/inbox``. That JSON is what Claude Code
  hands the hook; it is NOT proof of the original Shopify wire bytes.
* ``parse_capture`` verifies tool, pinned query, pagination variables, store
  identity (``shop { id myshopifyDomain }`` in every response) and schema.
  The response shape is bound to the ledger mode: ``synthetic`` accepts only
  ``{"data": {...}}`` objects; ``connector`` accepts only the JSON text that
  decodes to exactly ``{"data": {...}}`` (qualified 2026-10-14). Errors,
  extensions, plain-text failures (disconnected or expired authentication)
  and any other wrapper fail closed.
* A second, separately versioned provenance (#155 E8, 2026-10-17) carries the
  same pinned queries read by the Claude-free poller
  (``directlocal/xb_shopify_poller.py``) straight from the Shopify Admin
  GraphQL API: ``shopify_admin_graphql_direct_http_v1`` with its own record
  schema and ``xbdh-`` request ids. It is never written as, or accepted as, a
  hook record. A run is sealed only when every page has one provenance, and
  direct pages only in a run the poller started (``origin``).
* The reused ``capture_member_mg_baseline`` / ``evaluate_profile`` take every
  cutover and admission decision.

Write safety
------------
A customer has at most one fence, ever. The fence is committed FENCED and then
DISPATCHED (with a deadline) before the child writer starts; no code path moves
a fence back to FENCED/DISPATCHED, so a crash, replay, timeout or retry can
never start a second ``SaveMember`` for it. The child additionally creates an
exclusive ``<fence_id>.claim`` file before ``SaveMember`` and refuses if it
already exists. An uncertain outcome is only ever reconciled read-only with the
same bound MemberNo.

Ledger modes and environment binding
------------------------------------
The mode, AutoCount environment and book are fixed when the ledger is
initialised and can never change (SQLite triggers).

* ``synthetic`` (environment ``synthetic``, no book): inert. The processor
  refuses every bridge whose ``autocount_target`` is not the test-only
  ``SYNTHETIC_BRIDGE_TARGET``, and the PowerShell bridge refuses to run
  without a positively marked fake session factory, which its ``-File``
  entrypoint cannot receive.
* ``connector`` (real Shopify connector data): the store must be the
  qualified store identity, the state root must be private (no inherited
  ACEs; only the current user, SYSTEM and Administrators), and the
  environment must be ``development`` (book ``AED_TESTINGAPI``). Only a real
  bridge bound to that same book is accepted; the bridge then pins the book
  for its process and proves it from the authenticated session. The
  ``production`` environment is recognised but refused by this source (gate
  E9 needs a separate source change).

The ledger stores GIDs, MemberNos, counts, codes and salted digests only.
Public output carries keyed, non-enumerable references (``c-``/``m-``
prefixes, HMAC-SHA256 under a per-ledger random key), never a MemberNo.
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
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
# The reused admission functions live in the frozen gateway package; they are
# imported read-only (stdlib-only modules) and never run as a service.
# Appended, never prepended (G2 A2, 2026-10-16): a file in the gateway source
# tree must not shadow a standard-library module. The Owner console also runs
# this file with ``python -I -S -B``, so neither the script's own directory nor
# site-packages is on the path.
_GATEWAY_SRC = str(REPO_ROOT / "member_gateway" / "src")
if _GATEWAY_SRC not in sys.path:
    sys.path.append(_GATEWAY_SRC)

from xb_member_gateway.shopify_admission import (  # noqa: E402
    AdmissionDecision,
    BaselineCaptureError,
    ShopifyReadError,
    baseline_digest,
    capture_member_mg_baseline,
    evaluate_profile,
    parse_created_at,
)
from xb_member_gateway.shopify_webhook import GID_RE  # noqa: E402

CAPTURE_SCHEMA = "xb.directlocal.capture.v1"
CAPTURE_PROVENANCE = "claude_code_posttooluse_hook_delivered_json"
CONNECTOR_TOOL = "mcp__27c48ff3-54ed-4728-aa90-a7906cba3158__graphql_query"
# Direct HTTP capture (#155 E8): the same pinned queries, read by the local
# poller over outbound HTTPS. Distinct schema, provenance and id namespace; the
# record holds only the validated ``data`` object (``errors`` or ``extensions``
# never reach a capture).
DIRECT_CAPTURE_SCHEMA = "xb.directlocal.capture.direct_http.v1"
DIRECT_CAPTURE_PROVENANCE = "shopify_admin_graphql_direct_http_v1"
DIRECT_REQUEST_ID_PREFIX = "xbdh-"
DIRECT_REQUEST_ID_RE = re.compile(r"^xbdh-[0-9a-f]{32}$")
DIRECT_RUN_ORIGIN = "direct_http_poller_v1"
RUN_ORIGINS = frozenset({DIRECT_RUN_ORIGIN})
API_VERSION_RE = re.compile(r"^20[0-9]{2}-(?:01|04|07|10)$")
PAGE_SIZE = 50  # the connector tool's maximum ``first``
MAX_MEMBER_NO_CANDIDATES = 3
PROFILE_WAIT_MAX_CHECKS = 5
CAPTURE_MAX_AGE_SECONDS = 900
APPROVAL_MAX_TTL_SECONDS = 1800
POLICY_APPROVAL_TTL_SECONDS = 300
POLICY_MAX_TTL_HOURS = 168
OUTLET_RE = re.compile(r"^[A-Z0-9]{2,10}$")
WRITER_DEADLINE_SECONDS = 120
WRITER_GRACE_SECONDS = 15
HOLD_EXIT_CODE = 75
# Test-only bridge target. A synthetic ledger accepts no other bridge.
SYNTHETIC_BRIDGE_TARGET = "synthetic_fake_member_command"
REAL_BRIDGE_TARGET = "real_autocount"
# AutoCount 2.2 Member column widths, read from the nominated test book's
# catalog on 2026-10-14: Name nvarchar(100), MobilePhone nvarchar(25),
# EmailAddress nvarchar(200), MemberNo nvarchar(20). nvarchar(n) holds n
# UTF-16 code units, so a character outside the BMP counts twice. Wider values
# are never sent to SaveMember (they would fail or truncate).
AUTOCOUNT_FIELD_WIDTHS = {"name": 100, "mobile_phone": 25, "email_address": 200}
# C0 controls, DEL and unpaired surrogates are never sent to AutoCount.
_AUTOCOUNT_TEXT_FORBIDDEN_RE = re.compile("[\x00-\x1f\x7f\ud800-\udfff]")
# Qualified 2026-10-14 from one attended identity read through the PostToolUse
# hook and re-observed 2026-10-15: tool_response is a JSON *text* whose decoded
# value is exactly {"data": {...}}. Hook-delivered JSON, not Shopify wire bytes.
CONNECTOR_RESPONSE_SHAPE = "connector_graphql_json_text_v1"
# Response shape per ledger mode; see module docstring.
QUALIFIED_RESPONSE_SHAPES = {"synthetic": "graphql_data_object_v1", "connector": CONNECTOR_RESPONSE_SHAPE}
SHOP_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.myshopify\.com$")
# The only store a connector ledger may bind (non-PII identity observed
# through the connector on 2026-10-14 and 2026-10-15).
QUALIFIED_CONNECTOR_STORES = {"gid://shopify/Shop/48759472277": "xboundariessg.myshopify.com"}
# AutoCount environment -> account book. Bound once at init-ledger.
ENVIRONMENT_BOOKS = {"synthetic": None, "development": "AED_TESTINGAPI", "production": "AED_XBOUNDARIES"}
# Environments this source may start a real bridge for. Production (E9)
# requires a later, separately approved source change.
AUTHORISED_REAL_ENVIRONMENTS = frozenset({"development"})
MEMBER_REF_HEX = 20  # 80-bit keyed reference

BASELINE_QUERY = """query XbDirectLocalCustomerPage($first: Int!, $after: String) {
  shop { id myshopifyDomain }
  customers(first: $first, after: $after, sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    nodes { id createdAt tags registration: metafield(namespace: "membership", key: "start_date") { id createdAt } }
  }
}"""

PROFILE_QUERY = """query XbDirectLocalCustomerProfile($id: ID!) {
  shop { id myshopifyDomain }
  customer(id: $id) {
    id
    createdAt
    tags
    firstName
    lastName
    defaultEmailAddress { emailAddress }
    defaultPhoneNumber { phoneNumber }
    startDate: metafield(namespace: "membership", key: "start_date") { id createdAt type value }
    expiryDate: metafield(namespace: "membership", key: "expiry_date") { type value }
  }
}"""


def _sha256(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return "sha256:" + hashlib.sha256(data).hexdigest()


# Store identity only: no customer object is selected.
IDENTITY_QUERY = """query XbDirectLocalShopIdentity {
  shop { id myshopifyDomain }
}"""

# Incremental discovery (2026-10-15): the store has about 4,300 customers, so
# an exhaustive scan is ~90 connector calls. A recent run reads only customers
# updated since a watermark; a registration always updates the customer, and
# the periodic exhaustive discovery still catches any search-index lag.
RECENT_QUERY = """query XbDirectLocalRecentCustomers($first: Int!, $after: String, $search: String!) {
  shop { id myshopifyDomain }
  customers(first: $first, after: $after, sortKey: UPDATED_AT, query: $search) {
    pageInfo { hasNextPage endCursor }
    nodes { id createdAt tags registration: metafield(namespace: "membership", key: "start_date") { id createdAt } }
  }
}"""
RECENT_SEARCH_RE = re.compile(r"^updated_at:>='[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z'$")
RECENT_WATERMARK_MARGIN_SECONDS = 900

PINNED_QUERIES = {_sha256(BASELINE_QUERY): "baseline", _sha256(RECENT_QUERY): "recent", _sha256(PROFILE_QUERY): "profile", _sha256(IDENTITY_QUERY): "identity"}
PAGE_KINDS = frozenset({"baseline", "recent"})

SHOP_GID_RE = re.compile(r"^gid://shopify/Shop/[1-9][0-9]{0,19}$")
SYNTHETIC_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.synthetic\.invalid$")
TOOL_USE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MEMBER_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,19}$")
CODE_RE = re.compile(r"^[a-z0-9_:.-]{1,80}$")
_PHONE_DIGITS_RE = re.compile(r"^[0-9]{6,15}$")
_CUSTOMER_FIELDS = frozenset({"id", "createdAt", "tags", "firstName", "lastName", "defaultEmailAddress", "defaultPhoneNumber", "startDate", "expiryDate"})


class DirectLocalError(RuntimeError):
    """Bounded refusal; only the code is ever rendered."""

    def __init__(self, code: str):
        self.code = code if CODE_RE.fullmatch(code or "") else "directlocal_error"
        super().__init__(self.code)


class CaptureRejected(DirectLocalError):
    pass


class DirectLocalHold(DirectLocalError):
    """A writer may still be live; nothing else may run until it is resolved."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    parsed = parse_created_at(value)
    if parsed is None:
        raise DirectLocalError("timestamp_invalid")
    return parsed


# --------------------------------------------------------------------------
# Singapore phone duplicate keys
# --------------------------------------------------------------------------

def duplicate_phone_keys(phone: str | None) -> tuple[str, ...]:
    """Symmetric duplicate-lookup keys for a ``canonical_phone`` value.

    ``65xxxxxxxx`` also matches ``xxxxxxxx`` and vice versa. The stored
    AutoCount MobilePhone value is never changed by this; it is lookup only.
    """

    if phone is None:
        return ()
    if not isinstance(phone, str) or not _PHONE_DIGITS_RE.fullmatch(phone):
        raise DirectLocalError("phone_key_input_invalid")
    if len(phone) == 10 and phone.startswith("65"):
        return (phone, phone[2:])
    if len(phone) == 8:
        return (phone, "65" + phone)
    return (phone,)


def utf16_code_units(value: str) -> int:
    """Length as SQL Server ``nvarchar`` counts it (UTF-16 code units)."""

    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def autocount_text_issue(payload: Mapping[str, Any]) -> str | None:
    """Review code if any text field cannot be stored exactly, else ``None``."""

    for field, width in AUTOCOUNT_FIELD_WIDTHS.items():
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or _AUTOCOUNT_TEXT_FORBIDDEN_RE.search(value):
            return "autocount_text_invalid"
        if utf16_code_units(value) > width:
            return "autocount_field_width_exceeded"
    return None


METAFIELD_GID_RE = re.compile(r"^gid://shopify/Metafield/[1-9][0-9]{0,19}$")


def registration_event(record: Mapping[str, Any], field: str) -> tuple[str | None, datetime | None]:
    """(metafield GID, instant) of the membership registration.

    Qualified 2026-10-15 (read-only, #155:6195382795): Shopify Forms (form
    1178308) creates or matches the customer; ``membership.start_date`` is
    written about three seconds later, also for an EXISTING customer whose
    ``createdAt`` long predates the registration. So the registration instant
    is that metafield's ``createdAt``, not ``Customer.createdAt``. A customer
    without the metafield yet (registration still in flight) falls back to
    ``Customer.createdAt``; an unparseable metafield yields ``(None, None)``.
    The fallback only routes discovery: an old customer outside the baseline
    that is still missing the metafield waits (``Processor._decide``).
    """

    node = record.get(field)
    if node is None:
        return None, parse_created_at(record.get("createdAt"))
    if not isinstance(node, Mapping) or not isinstance(node.get("id"), str) or not METAFIELD_GID_RE.fullmatch(node["id"]):
        return None, None
    instant = parse_created_at(node.get("createdAt"))
    return (node["id"], instant) if instant is not None else (None, None)


def _registration_view(record: Mapping[str, Any], field: str) -> dict[str, Any]:
    """The record with ``createdAt`` replaced by the registration instant, so
    the reused cutover rules (``createdAt >= C`` is new) apply to the
    registration event. An unparseable registration stays unparseable."""

    event_id, instant = registration_event(record, field)
    view = dict(record)
    if instant is None:
        view["createdAt"] = None
    elif event_id is not None:
        view["createdAt"] = record[field]["createdAt"]  # the validated original text
    return view


# --------------------------------------------------------------------------
# Connector capture parsing
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Capture:
    kind: str  # baseline | profile
    capture_sha256: str
    tool_use_id: str
    captured_at: datetime
    variables: Mapping[str, Any]
    data: Mapping[str, Any]
    provenance: str = CAPTURE_PROVENANCE

    def __repr__(self) -> str:  # never render data (profile captures carry PII)
        return f"Capture(kind={self.kind!r}, tool_use_id={self.tool_use_id!r})"


def check_tool_input(tool_input: Any) -> str:
    """Return the pinned query kind for an allowed connector call, else raise.

    Shared by the PreToolUse guard and capture parsing so both enforce the
    exact same allowlist.
    """

    if not isinstance(tool_input, Mapping) or not set(tool_input) <= {"query", "variables", "first", "after"}:
        raise CaptureRejected("tool_input_shape_invalid")
    query = tool_input.get("query")
    kind = PINNED_QUERIES.get(_sha256(query)) if isinstance(query, str) else None
    if kind is None:
        raise CaptureRejected("query_not_pinned")
    if kind == "identity":
        if not set(tool_input) <= {"query", "variables"} or tool_input.get("variables", {}) != {}:
            raise CaptureRejected("identity_variables_invalid")
        return kind
    variables = tool_input.get("variables")
    if not isinstance(variables, Mapping):
        raise CaptureRejected("variables_invalid")
    if kind in PAGE_KINDS:
        allowed = {"first", "after"} | ({"search"} if kind == "recent" else set())
        if not set(variables) <= allowed or variables.get("first") != PAGE_SIZE or isinstance(variables.get("first"), bool):
            raise CaptureRejected("pagination_variables_invalid")
        if kind == "recent" and (not isinstance(variables.get("search"), str) or not RECENT_SEARCH_RE.fullmatch(variables["search"])):
            raise CaptureRejected("recent_search_invalid")
        if tool_input.get("first") != PAGE_SIZE or isinstance(tool_input.get("first"), bool):
            raise CaptureRejected("pagination_variables_invalid")
        after = variables.get("after")
        if after is not None and (not isinstance(after, str) or not after or len(after) > 1024):
            raise CaptureRejected("pagination_variables_invalid")
        if tool_input.get("after") != after:
            raise CaptureRejected("pagination_variables_invalid")
    else:
        if set(variables) != {"id"} or not isinstance(variables["id"], str) or not GID_RE.fullmatch(variables["id"]):
            raise CaptureRejected("profile_variables_invalid")
        if "first" in tool_input or "after" in tool_input:
            raise CaptureRejected("profile_variables_invalid")
    return kind


def _response_data(tool_response: Any, mode: str) -> Mapping[str, Any]:
    shape = QUALIFIED_RESPONSE_SHAPES.get(mode)
    if shape is None:
        raise CaptureRejected("capture_response_shape_unqualified")
    if shape == CONNECTOR_RESPONSE_SHAPE:
        return connector_response_data(tool_response)
    # graphql_data_object_v1: exactly {"data": {...}}; any GraphQL error rejects.
    if not isinstance(tool_response, Mapping) or set(tool_response) != {"data"} or not isinstance(tool_response["data"], Mapping):
        raise CaptureRejected("capture_response_shape_invalid")
    return tool_response["data"]


def connector_response_data(tool_response: Any) -> Mapping[str, Any]:
    """Decode the qualified connector shape. Errors, extensions or any other
    wrapper fail closed (they were not part of the qualified shape)."""

    if not isinstance(tool_response, str) or len(tool_response) > 4_194_304:
        raise CaptureRejected("connector_response_shape_invalid")
    try:
        value = json.loads(tool_response)
    except (json.JSONDecodeError, RecursionError):
        raise CaptureRejected("connector_response_shape_invalid") from None
    if not isinstance(value, dict) or set(value) != {"data"} or not isinstance(value["data"], dict):
        raise CaptureRejected("connector_response_shape_invalid")
    return value["data"]


def inspect_identity_capture(raw: bytes) -> dict[str, str]:
    """Validate one hook-captured store-identity read and return only the
    store identity (no customer object can be present)."""

    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise CaptureRejected("capture_json_invalid") from None
    expected = {"schema_version", "provenance", "captured_at", "session_id", "tool_use_id", "tool_name", "tool_input", "tool_response"}
    if not isinstance(record, dict) or set(record) != expected or record["schema_version"] != CAPTURE_SCHEMA or record["provenance"] != CAPTURE_PROVENANCE:
        raise CaptureRejected("capture_record_shape_invalid")
    if record["tool_name"] != CONNECTOR_TOOL or not isinstance(record["tool_use_id"], str) or not TOOL_USE_ID_RE.fullmatch(record["tool_use_id"]):
        raise CaptureRejected("capture_record_shape_invalid")
    if parse_created_at(record["captured_at"]) is None:
        raise CaptureRejected("capture_timestamp_invalid")
    if check_tool_input(record["tool_input"]) != "identity":
        raise CaptureRejected("capture_not_identity")
    data = connector_response_data(record["tool_response"])
    shop = data.get("shop")
    if set(data) != {"shop"} or not isinstance(shop, dict) or set(shop) != {"id", "myshopifyDomain"}:
        raise CaptureRejected("identity_shape_invalid")
    if not isinstance(shop["id"], str) or not SHOP_GID_RE.fullmatch(shop["id"]) or not isinstance(shop["myshopifyDomain"], str) or not SHOP_DOMAIN_RE.fullmatch(shop["myshopifyDomain"]):
        raise CaptureRejected("identity_values_invalid")
    return {
        "response_shape": CONNECTOR_RESPONSE_SHAPE, "provenance": CAPTURE_PROVENANCE, "capture_sha256": _sha256(raw),
        "tool_use_id": record["tool_use_id"], "store_gid": shop["id"], "shop_domain": shop["myshopifyDomain"],
    }


def direct_tool_input(query: Any, variables: Any) -> dict[str, Any]:
    """The connector-equivalent tool input of one direct request, so the
    direct path is checked by exactly the same ``check_tool_input`` allowlist
    (pinned query text, page size, cursor and search shape)."""

    tool_input: dict[str, Any] = {"query": query, "variables": variables}
    kind = PINNED_QUERIES.get(_sha256(query)) if isinstance(query, str) else None
    if kind in PAGE_KINDS and isinstance(variables, Mapping):
        tool_input["first"] = variables.get("first")
        if variables.get("after") is not None:
            tool_input["after"] = variables["after"]
    return tool_input


def _parse_direct_record(record: dict, shop_domain: str) -> tuple[str, str, datetime, Mapping[str, Any], Mapping[str, Any]]:
    """(kind, request id, captured_at, variables, data) of one direct-HTTP
    capture written by the poller. Exact keys; the endpoint must be this
    ledger's store; the response must be exactly ``{"data": {...}}``."""

    expected = {"schema_version", "provenance", "captured_at", "request_id", "endpoint", "request", "response"}
    if set(record) != expected or record["schema_version"] != DIRECT_CAPTURE_SCHEMA:
        raise CaptureRejected("capture_record_shape_invalid")
    request_id = record["request_id"]
    if not isinstance(request_id, str) or not DIRECT_REQUEST_ID_RE.fullmatch(request_id):
        raise CaptureRejected("capture_tool_use_id_invalid")
    captured_at = parse_created_at(record["captured_at"])
    if captured_at is None:
        raise CaptureRejected("capture_timestamp_invalid")
    endpoint = record["endpoint"]
    if not isinstance(endpoint, dict) or set(endpoint) != {"host", "api_version"}:
        raise CaptureRejected("capture_endpoint_invalid")
    if endpoint["host"] != shop_domain:
        raise CaptureRejected("capture_endpoint_mismatch")
    if not isinstance(endpoint["api_version"], str) or not API_VERSION_RE.fullmatch(endpoint["api_version"]):
        raise CaptureRejected("capture_endpoint_invalid")
    request = record["request"]
    if not isinstance(request, dict) or set(request) != {"query", "variables"} or not isinstance(request["variables"], dict):
        raise CaptureRejected("tool_input_shape_invalid")
    kind = check_tool_input(direct_tool_input(request["query"], request["variables"]))
    response = record["response"]
    if not isinstance(response, dict) or set(response) != {"data"} or not isinstance(response["data"], dict):
        raise CaptureRejected("direct_response_shape_invalid")
    return kind, request_id, captured_at, request["variables"], response["data"]


def parse_capture(raw: bytes, *, mode: str, store_gid: str, shop_domain: str) -> Capture:
    """Strictly validate one capture record (attended hook or direct HTTP).
    Anything unexpected rejects."""

    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise CaptureRejected("capture_json_invalid") from None
    if isinstance(record, dict) and record.get("provenance") == DIRECT_CAPTURE_PROVENANCE:
        if mode not in QUALIFIED_RESPONSE_SHAPES:
            raise CaptureRejected("capture_response_shape_unqualified")
        kind, tool_use_id, captured_at, variables, data = _parse_direct_record(record, shop_domain)
        provenance = DIRECT_CAPTURE_PROVENANCE
    else:
        expected = {"schema_version", "provenance", "captured_at", "session_id", "tool_use_id", "tool_name", "tool_input", "tool_response"}
        if not isinstance(record, dict) or set(record) != expected:
            raise CaptureRejected("capture_record_shape_invalid")
        if record["schema_version"] != CAPTURE_SCHEMA or record["provenance"] != CAPTURE_PROVENANCE:
            raise CaptureRejected("capture_record_shape_invalid")
        if record["tool_name"] != CONNECTOR_TOOL:
            raise CaptureRejected("capture_tool_not_allowed")
        tool_use_id = record["tool_use_id"]
        # The xbdh- namespace belongs to direct captures only.
        if not isinstance(tool_use_id, str) or not TOOL_USE_ID_RE.fullmatch(tool_use_id) or tool_use_id.startswith(DIRECT_REQUEST_ID_PREFIX):
            raise CaptureRejected("capture_tool_use_id_invalid")
        captured_at = parse_created_at(record["captured_at"])
        if captured_at is None:
            raise CaptureRejected("capture_timestamp_invalid")
        kind = check_tool_input(record["tool_input"])
        data = _response_data(record["tool_response"], mode)
        variables = record["tool_input"].get("variables", {})
        provenance = CAPTURE_PROVENANCE
    if data.get("shop") != {"id": store_gid, "myshopifyDomain": shop_domain}:
        raise CaptureRejected("capture_store_identity_mismatch")
    if kind == "identity":
        if set(data) != {"shop"}:
            raise CaptureRejected("identity_shape_invalid")
    elif kind in PAGE_KINDS:
        page = data.get("customers")
        if set(data) != {"shop", "customers"} or not isinstance(page, Mapping) or set(page) != {"pageInfo", "nodes"}:
            raise CaptureRejected("baseline_page_shape_invalid")
        info, nodes = page["pageInfo"], page["nodes"]
        if not isinstance(info, Mapping) or set(info) != {"hasNextPage", "endCursor"} or not isinstance(nodes, list):
            raise CaptureRejected("baseline_page_shape_invalid")
        if len(nodes) > PAGE_SIZE or any(not isinstance(node, Mapping) or set(node) != {"id", "createdAt", "tags", "registration"} for node in nodes):
            raise CaptureRejected("baseline_page_shape_invalid")
    else:
        customer = data.get("customer")
        if set(data) != {"shop", "customer"}:
            raise CaptureRejected("profile_shape_invalid")
        if customer is not None:
            if not isinstance(customer, Mapping) or set(customer) != _CUSTOMER_FIELDS:
                raise CaptureRejected("profile_shape_invalid")
            if customer["id"] != variables["id"]:
                raise CaptureRejected("profile_identity_mismatch")
    return Capture(kind, _sha256(raw), tool_use_id, captured_at, dict(variables), data, provenance)


# --------------------------------------------------------------------------
# State root and single-owner lock
# --------------------------------------------------------------------------

def _state_root_location(path: str | Path, env: Mapping[str, str]) -> Path:
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        raise DirectLocalError("state_root_unc_refused")
    root = Path(text)
    if not root.is_absolute():
        raise DirectLocalError("state_root_not_absolute")
    resolved = root.resolve()
    local_app_data = env.get("LOCALAPPDATA")
    candidates = [REPO_ROOT.resolve()] + ([Path(local_app_data).resolve()] if local_app_data else [])
    for forbidden in candidates:
        if resolved == forbidden or forbidden in resolved.parents:
            raise DirectLocalError("state_root_location_refused")
    return resolved


def validate_state_root(path: str | Path, *, environ: Mapping[str, str] | None = None) -> Path:
    """Refuse UNC, MSIX-redirected ``%LOCALAPPDATA%``, repo-internal and
    missing roots. The root itself is never created here."""

    resolved = _state_root_location(path, os.environ if environ is None else environ)
    if not resolved.is_dir():
        raise DirectLocalError("state_root_missing")
    return resolved


# Only these principals may own or hold any access to connector-ledger state.
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_SDDL_ALIASES = {"SY": _SYSTEM_SID, "BA": _ADMINISTRATORS_SID}
_SDDL_RE = re.compile(r"^O:(?P<owner>[A-Z]{2}|S-1-[0-9-]+)D:(?P<flags>[A-Z]*)(?P<aces>(?:\([^()]*\))*)$")
_SID_RE = re.compile(r"^S-1-[0-9]+(?:-[0-9]+){0,15}$")


# Machine-independent SDDL aliases (for the offline resolver only). Machine-
# or domain-relative aliases such as LA are unresolvable offline.
_WELL_KNOWN_ALIASES = {**_SDDL_ALIASES, "CO": "S-1-3-0", "OW": "S-1-3-4", "AU": "S-1-5-11", "BU": "S-1-5-32-545", "WD": "S-1-1-0"}


def _static_sid(token: str) -> str | None:
    """Offline resolver: full SIDs pass through; fixed aliases map."""

    return token if _SID_RE.fullmatch(token or "") else _WELL_KNOWN_ALIASES.get(token)


def _win_resolve_sid(token: str) -> str | None:
    """Canonical SID for an SDDL SID token, resolved by Windows itself.

    SDDL prints some principals as two-letter aliases. Hosted Windows CI
    (2026-10-16) runs as the renamed built-in Administrator (RID 500), which
    SDDL prints as ``LA``; an alias-blind check misread the root's own grant
    as foreign. ``None`` when Windows cannot resolve the token.
    """

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSidToSidW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
    advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(token, ctypes.byref(sid)):
        return None
    try:
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(sid)


def _win_current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise DirectLocalError("state_root_acl_unverifiable")
    try:
        buffer = ctypes.create_string_buffer(512)
        size = wintypes.DWORD()
        if not advapi32.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(size)):  # TokenUser
            raise DirectLocalError("state_root_acl_unverifiable")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise DirectLocalError("state_root_acl_unverifiable")
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def _win_sddl(path: Path) -> str:
    """Owner + DACL of ``path`` as SDDL (read-only)."""

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = (wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD) + (ctypes.POINTER(ctypes.c_void_p),) * 5
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG))
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    info = 0x1 | 0x4  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    if advapi32.GetNamedSecurityInfoW(str(path), 1, info, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)) != 0:  # SE_FILE_OBJECT
        raise DirectLocalError("state_root_acl_unverifiable")
    try:
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, info, ctypes.byref(text), None):
            raise DirectLocalError("state_root_acl_unverifiable")
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(descriptor)


def sddl_custody_issue(sddl: str, *, user_sid: str, require_protected: bool, resolve: Callable[[str], str | None] = _static_sid) -> str | None:
    """Pure SDDL check. ``None`` when only the current user, SYSTEM and
    Administrators own or are granted anything (deny entries are harmless).
    Every owner and ACE principal is compared as a canonical SID; a token
    that cannot be resolved is unverifiable, never assumed safe."""

    match = _SDDL_RE.fullmatch(sddl or "")
    if match is None:
        return "state_root_acl_unverifiable"
    allowed = {user_sid, _SYSTEM_SID, _ADMINISTRATORS_SID}
    owner = resolve(match["owner"])
    if owner is None:
        return "state_root_acl_unverifiable"
    if owner not in allowed:
        return "state_root_owner_unsafe"
    if require_protected and "P" not in match["flags"]:
        return "state_root_acl_inherited"
    aces = re.findall(r"\(([^()]*)\)", match["aces"])
    if not aces:
        return "state_root_acl_unverifiable"  # an empty or null DACL is never private
    for ace in aces:
        parts = ace.split(";")
        if len(parts) != 6:
            return "state_root_acl_unsafe"
        if parts[0] == "D":
            continue
        if parts[0] != "A":
            return "state_root_acl_unsafe"
        principal = resolve(parts[5])
        if principal is None:
            return "state_root_acl_unverifiable"
        if principal not in allowed:
            return "state_root_acl_unsafe"
    return None


_PRINCIPAL_CLASSES = {
    _SYSTEM_SID: "system", _ADMINISTRATORS_SID: "administrators", "S-1-3-4": "owner_rights", "S-1-3-0": "creator_owner",
    "S-1-5-11": "authenticated_users", "S-1-5-32-545": "users", "S-1-1-0": "everyone",
}


def sddl_custody_classes(sddl: str, *, user_sid: str, resolve: Callable[[str], str | None] = _static_sid) -> dict[str, Any]:
    """Sanitised view of one descriptor for diagnostics: DACL flags, owner
    class and per-ACE type, inheritance flags and principal class. Never a
    SID, account name or path. An SDDL alias token (for example ``LA``) is a
    public constant and is reported so the alias in use is visible."""

    def principal(token: str) -> str:
        sid = resolve(token)
        if sid is None:
            return "unresolvable"
        if sid == user_sid:
            name = "current_user"
        elif sid in _PRINCIPAL_CLASSES:
            name = _PRINCIPAL_CLASSES[sid]
        elif sid.startswith("S-1-5-21-"):
            name = "other_account"
        else:
            name = "other_well_known"
        return f"{name}(alias:{token})" if not token.startswith("S-1-") else name

    match = _SDDL_RE.fullmatch(sddl or "")
    if match is None:
        return {"parsed": False}
    aces = []
    for ace in re.findall(r"\(([^()]*)\)", match["aces"]):
        parts = ace.split(";")
        aces.append({"type": parts[0], "flags": parts[1], "principal": principal(parts[5])} if len(parts) == 6 else {"type": "unparsed"})
    return {"parsed": True, "dacl_flags": match["flags"], "owner": principal(match["owner"]), "aces": aces}


def describe_private_state(root: Path) -> list[dict[str, Any]]:
    """Diagnostic counterpart of ``verify_private_state`` (Windows only):
    sanitised classes for the root and its custody children, with the
    verdict each would get. Read-only; no SID, name or path is returned."""

    if os.name != "nt":
        return []
    user_sid = _win_current_user_sid()
    rows = []
    for name in (".", "inbox", "claims", "evidence", "ledger.sqlite3"):
        path = root if name == "." else root / name
        if not path.exists():
            continue
        sddl = _win_sddl(path)
        rows.append({
            "role": "root" if name == "." else name,
            "verdict": sddl_custody_issue(sddl, user_sid=user_sid, require_protected=(name == "."), resolve=_win_resolve_sid) or "private",
            **sddl_custody_classes(sddl, user_sid=user_sid, resolve=_win_resolve_sid),
        })
    return rows


def verify_private_state(root: Path) -> None:
    """Refuse a connector-ledger state root that anyone but the current user,
    SYSTEM and Administrators could read. The root must not inherit; the
    ledger files and subdirectories are checked as they effectively are."""

    targets = [(root, True)] + [(root / name, False) for name in ("inbox", "claims", "evidence", "ledger.sqlite3") if (root / name).exists()]
    if os.name == "nt":
        user_sid = _win_current_user_sid()
        for path, protected in targets:
            issue = sddl_custody_issue(_win_sddl(path), user_sid=user_sid, require_protected=protected, resolve=_win_resolve_sid)
            if issue:
                raise DirectLocalError(issue)
        return
    # POSIX: a 0700 root owned by this user already bars everyone else from
    # everything below it; children need only the same owner.
    for path, protected in targets:
        info = path.stat()
        if info.st_uid != os.getuid() or (protected and info.st_mode & 0o077):
            raise DirectLocalError("state_root_acl_unsafe")


def _win_set_private_dacl(path: Path) -> None:
    """Replace the whole DACL of ``path`` with exactly: current user, SYSTEM
    and Administrators, full control, inheritable, protected (no inherited
    ACEs). One SetNamedSecurityInfoW call, so no pre-existing ACE can
    survive; hosted CI (2026-10-16) showed icacls leaving an explicit OWNER
    RIGHTS ACE on a new directory under Python 3.12's temp tree."""

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG))
    advapi32.GetSecurityDescriptorDacl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL))
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = (wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    sddl = f"D:PAI(A;OICI;FA;;;{_SYSTEM_SID})(A;OICI;FA;;;{_ADMINISTRATORS_SID})(A;OICI;FA;;;{_win_current_user_sid()})"
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise DirectLocalError("state_root_harden_failed")
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present or not dacl:
            raise DirectLocalError("state_root_harden_failed")
        info = 0x4 | 0x80000000  # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
        if advapi32.SetNamedSecurityInfoW(str(path), 1, info, None, None, dacl, None) != 0:  # SE_FILE_OBJECT
            raise DirectLocalError("state_root_harden_failed")
    finally:
        kernel32.LocalFree(descriptor)


def prepare_state_root(path: str | Path, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Create a new private state root (gate E0). The directory must not
    exist; inheritance is removed and only the current user, SYSTEM and
    Administrators are granted access. Nothing existing is modified."""

    resolved = _state_root_location(path, os.environ if environ is None else environ)
    if resolved.exists():
        raise DirectLocalError("state_root_already_exists")
    if not resolved.parent.is_dir():
        raise DirectLocalError("state_root_parent_missing")
    os.mkdir(resolved)
    if os.name == "nt":
        _win_set_private_dacl(resolved)
    else:
        os.chmod(resolved, 0o700)
    for name in ("inbox", "claims", "evidence"):
        (resolved / name).mkdir()
    verify_private_state(resolved)
    return {"status": "prepared", "private": True}


class _OwnerLock:
    """OS-level exclusive lock, released by the OS if the process dies."""

    def __init__(self, path: Path):
        self._handle = open(path, "a+b")  # noqa: SIM115 - held for the owner's lifetime
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.close()
            raise DirectLocalError("ledger_locked_by_another_owner") from None

    def release(self) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._handle.close()


# --------------------------------------------------------------------------
# SQLite ledger
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE ledger_meta(
  id INTEGER PRIMARY KEY CHECK (id = 1), ledger_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('synthetic', 'connector')),
  environment TEXT NOT NULL CHECK (environment IN ('synthetic', 'development', 'production')),
  autocount_book TEXT, ref_key TEXT NOT NULL CHECK (length(ref_key) = 64),
  store_gid TEXT NOT NULL, shop_domain TEXT NOT NULL, created_at TEXT NOT NULL,
  CHECK ((mode = 'synthetic') = (environment = 'synthetic')),
  CHECK ((environment = 'synthetic') = (autocount_book IS NULL)));
CREATE TABLE writer_owner(
  id INTEGER PRIMARY KEY CHECK (id = 1), epoch INTEGER NOT NULL, pid INTEGER NOT NULL, acquired_at TEXT NOT NULL);
CREATE TABLE runs(
  run_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('BASELINE', 'DISCOVERY')),
  cutover_at TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('CAPTURING', 'SEALED', 'ABANDONED')),
  started_at TEXT NOT NULL, sealed_at TEXT, page_count INTEGER, historical_count INTEGER,
  transition_count INTEGER, historical_digest TEXT, search TEXT,
  CHECK (search IS NULL OR kind = 'DISCOVERY'));
CREATE UNIQUE INDEX runs_single_baseline ON runs(kind) WHERE kind = 'BASELINE' AND state <> 'ABANDONED';
CREATE UNIQUE INDEX runs_single_open ON runs(state) WHERE state = 'CAPTURING';
CREATE TABLE run_pages(
  run_id TEXT NOT NULL REFERENCES runs(run_id), page_index INTEGER NOT NULL,
  capture_sha256 TEXT NOT NULL UNIQUE, has_next INTEGER NOT NULL, node_count INTEGER NOT NULL,
  PRIMARY KEY (run_id, page_index));
CREATE TABLE baseline_members(customer_gid TEXT PRIMARY KEY);
CREATE TABLE captures(
  capture_sha256 TEXT PRIMARY KEY, tool_use_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, consumed_at TEXT NOT NULL);
CREATE TABLE customers(
  store_gid TEXT NOT NULL, customer_gid TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('DISCOVERED', 'NOT_ELIGIBLE', 'WAITING', 'ADMITTED', 'EXCLUDED_BASELINE', 'MANUAL_REVIEW', 'FENCED')),
  check_count INTEGER NOT NULL DEFAULT 0, decision_code TEXT, member_no TEXT UNIQUE, updated_at TEXT NOT NULL,
  registration_id TEXT, registration_at TEXT, discovered_at TEXT,
  PRIMARY KEY (store_gid, customer_gid));
CREATE TABLE admission_policies(
  policy_id TEXT PRIMARY KEY, environment TEXT NOT NULL, autocount_book TEXT NOT NULL, store_gid TEXT NOT NULL,
  max_creates INTEGER NOT NULL CHECK (max_creates BETWEEN 1 AND 500), created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  revoked_at TEXT);
CREATE TABLE pos_observations(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, fence_id TEXT NOT NULL REFERENCES fences(fence_id), outlet TEXT NOT NULL,
  observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE allocation_attempts(
  customer_gid TEXT NOT NULL, attempt_no INTEGER NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),
  candidate TEXT, status TEXT, created_at TEXT NOT NULL, generator_preview TEXT,
  derivation TEXT CHECK (derivation IN ('generator', 'successor')), PRIMARY KEY (customer_gid, attempt_no));
CREATE TABLE approvals(
  approval_id TEXT PRIMARY KEY, customer_gid TEXT NOT NULL, created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, consumed_at TEXT, policy_id TEXT REFERENCES admission_policies(policy_id));
CREATE TABLE fences(
  fence_id TEXT PRIMARY KEY, customer_gid TEXT NOT NULL UNIQUE, member_no TEXT NOT NULL UNIQUE,
  approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(approval_id),
  payload_digest TEXT NOT NULL, salt TEXT NOT NULL, owner_epoch INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('FENCED', 'DISPATCHED', 'NOT_WRITTEN_PROVEN', 'CREATED_VERIFIED',
    'CREATED_READBACK_MISMATCH', 'CREATED_OUTBOUND_QUEUE_CHANGED', 'WRITE_OUTCOME_UNCERTAIN', 'WRITER_TERMINATION_UNCONFIRMED',
    'RECONCILED_VERIFIED', 'MANUAL_REVIEW')),
  code TEXT, deadline_at TEXT, child_pid INTEGER, child_created TEXT, exit_code INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, type TEXT NOT NULL,
  customer_gid TEXT, fence_id TEXT, code TEXT, prev_hash TEXT NOT NULL, hash TEXT NOT NULL UNIQUE);

CREATE TRIGGER ledger_meta_immutable BEFORE UPDATE ON ledger_meta BEGIN SELECT RAISE(ABORT, 'ledger_meta_immutable'); END;
CREATE TRIGGER ledger_meta_no_delete BEFORE DELETE ON ledger_meta BEGIN SELECT RAISE(ABORT, 'ledger_meta_immutable'); END;
CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'run_immutable'); END;
CREATE TRIGGER runs_cutover_immutable BEFORE UPDATE OF cutover_at, kind, started_at, search ON runs
  BEGIN SELECT RAISE(ABORT, 'run_cutover_immutable'); END;
CREATE TRIGGER runs_sealed_immutable BEFORE UPDATE ON runs WHEN OLD.state = 'SEALED'
  BEGIN SELECT RAISE(ABORT, 'run_sealed_immutable'); END;
CREATE TRIGGER runs_abandoned_immutable BEFORE UPDATE ON runs WHEN OLD.state = 'ABANDONED'
  BEGIN SELECT RAISE(ABORT, 'run_abandoned_immutable'); END;
CREATE TRIGGER run_pages_immutable BEFORE UPDATE ON run_pages BEGIN SELECT RAISE(ABORT, 'run_pages_immutable'); END;
CREATE TRIGGER run_pages_no_delete BEFORE DELETE ON run_pages BEGIN SELECT RAISE(ABORT, 'run_pages_immutable'); END;
CREATE TRIGGER baseline_members_immutable BEFORE UPDATE ON baseline_members BEGIN SELECT RAISE(ABORT, 'baseline_immutable'); END;
CREATE TRIGGER baseline_members_no_delete BEFORE DELETE ON baseline_members BEGIN SELECT RAISE(ABORT, 'baseline_immutable'); END;
CREATE TRIGGER baseline_members_sealed_only BEFORE INSERT ON baseline_members
  WHEN EXISTS (SELECT 1 FROM runs WHERE kind = 'BASELINE' AND state = 'SEALED')
  BEGIN SELECT RAISE(ABORT, 'baseline_immutable'); END;
CREATE TRIGGER captures_immutable BEFORE UPDATE ON captures BEGIN SELECT RAISE(ABORT, 'capture_ledger_immutable'); END;
CREATE TRIGGER captures_no_delete BEFORE DELETE ON captures BEGIN SELECT RAISE(ABORT, 'capture_ledger_immutable'); END;
CREATE TRIGGER customers_no_delete BEFORE DELETE ON customers BEGIN SELECT RAISE(ABORT, 'customer_immutable'); END;
CREATE TRIGGER customers_terminal BEFORE UPDATE OF state ON customers
  WHEN OLD.state IN ('EXCLUDED_BASELINE', 'MANUAL_REVIEW', 'FENCED') AND NEW.state IS NOT OLD.state
  BEGIN SELECT RAISE(ABORT, 'customer_state_terminal'); END;
CREATE TRIGGER customers_registration_bound BEFORE UPDATE OF registration_id, registration_at, discovered_at ON customers
  WHEN (OLD.registration_id IS NOT NULL AND NEW.registration_id IS NOT OLD.registration_id)
    OR (OLD.registration_at IS NOT NULL AND NEW.registration_at IS NOT OLD.registration_at)
    OR (OLD.discovered_at IS NOT NULL AND NEW.discovered_at IS NOT OLD.discovered_at)
  BEGIN SELECT RAISE(ABORT, 'registration_immutable'); END;
CREATE TRIGGER policies_no_delete BEFORE DELETE ON admission_policies BEGIN SELECT RAISE(ABORT, 'policy_immutable'); END;
CREATE TRIGGER policies_revoke_only BEFORE UPDATE ON admission_policies
  WHEN OLD.revoked_at IS NOT NULL OR NEW.policy_id IS NOT OLD.policy_id OR NEW.environment IS NOT OLD.environment
    OR NEW.autocount_book IS NOT OLD.autocount_book OR NEW.store_gid IS NOT OLD.store_gid OR NEW.max_creates IS NOT OLD.max_creates
    OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
  BEGIN SELECT RAISE(ABORT, 'policy_immutable'); END;
CREATE TRIGGER pos_observations_append_only BEFORE UPDATE ON pos_observations BEGIN SELECT RAISE(ABORT, 'pos_observation_immutable'); END;
CREATE TRIGGER pos_observations_no_delete BEFORE DELETE ON pos_observations BEGIN SELECT RAISE(ABORT, 'pos_observation_immutable'); END;
CREATE TRIGGER customers_member_no_bound BEFORE UPDATE OF member_no ON customers
  WHEN OLD.member_no IS NOT NULL AND NEW.member_no IS NOT OLD.member_no
  BEGIN SELECT RAISE(ABORT, 'member_no_immutable'); END;
CREATE TRIGGER attempts_no_delete BEFORE DELETE ON allocation_attempts BEGIN SELECT RAISE(ABORT, 'attempt_immutable'); END;
CREATE TRIGGER attempts_recorded_once BEFORE UPDATE ON allocation_attempts WHEN OLD.status IS NOT NULL
  BEGIN SELECT RAISE(ABORT, 'attempt_immutable'); END;
CREATE TRIGGER approvals_no_delete BEFORE DELETE ON approvals BEGIN SELECT RAISE(ABORT, 'approval_immutable'); END;
CREATE TRIGGER approvals_consumed_once BEFORE UPDATE ON approvals
  WHEN OLD.consumed_at IS NOT NULL OR NEW.approval_id IS NOT OLD.approval_id OR NEW.customer_gid IS NOT OLD.customer_gid
    OR NEW.expires_at IS NOT OLD.expires_at OR NEW.policy_id IS NOT OLD.policy_id
  BEGIN SELECT RAISE(ABORT, 'approval_immutable'); END;
CREATE TRIGGER fences_no_delete BEFORE DELETE ON fences BEGIN SELECT RAISE(ABORT, 'fence_immutable'); END;
CREATE TRIGGER fences_binding_immutable BEFORE UPDATE OF fence_id, customer_gid, member_no, approval_id, payload_digest, salt, owner_epoch, created_at ON fences
  BEGIN SELECT RAISE(ABORT, 'fence_immutable'); END;
CREATE TRIGGER fences_transition BEFORE UPDATE OF state ON fences
  WHEN NOT (
       (OLD.state = 'FENCED' AND NEW.state IN ('DISPATCHED', 'MANUAL_REVIEW'))
    OR (OLD.state = 'DISPATCHED' AND NEW.state IN ('NOT_WRITTEN_PROVEN', 'CREATED_VERIFIED', 'CREATED_READBACK_MISMATCH',
                                                   'CREATED_OUTBOUND_QUEUE_CHANGED', 'WRITE_OUTCOME_UNCERTAIN', 'WRITER_TERMINATION_UNCONFIRMED'))
    OR (OLD.state = 'WRITER_TERMINATION_UNCONFIRMED' AND NEW.state = 'WRITE_OUTCOME_UNCERTAIN')
    OR (OLD.state = 'WRITE_OUTCOME_UNCERTAIN' AND NEW.state IN ('RECONCILED_VERIFIED', 'MANUAL_REVIEW')))
  BEGIN SELECT RAISE(ABORT, 'fence_transition_invalid'); END;
CREATE TRIGGER fences_dispatch_deadline BEFORE UPDATE OF state ON fences
  WHEN NEW.state = 'DISPATCHED' AND NEW.deadline_at IS NULL
  BEGIN SELECT RAISE(ABORT, 'fence_deadline_required'); END;
CREATE TRIGGER events_append_only BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events_append_only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events_append_only'); END;
"""

# v4 (2026-10-16, G2 A3): each allocation attempt records the generator's raw
# preview and whether the candidate is that preview or a derived successor.
# A v3 ledger is refused until the attended ``migrate-ledger`` command adds
# those two columns; existing rows keep NULL (provenance not recorded before
# v4) and nothing else changes. v3 (2026-10-15): mode/environment/book
# binding, ref key, outbound-queue terminal state. v2 (synthetic-only)
# ledgers are refused, never migrated.
SCHEMA_VERSION = 4
_MIGRATABLE_SCHEMA_VERSION = 3
_SCHEMA_V4_MIGRATION = (
    "ALTER TABLE allocation_attempts ADD COLUMN generator_preview TEXT",
    "ALTER TABLE allocation_attempts ADD COLUMN derivation TEXT CHECK (derivation IN ('generator', 'successor'))",
)
GENESIS_HASH = "sha256:" + "0" * 64
CUSTOMER_TERMINAL = frozenset({"EXCLUDED_BASELINE", "MANUAL_REVIEW", "FENCED"})


def _event_hash(prev_hash: str, at: str, event_type: str, customer_gid: str | None, fence_id: str | None, code: str | None) -> str:
    body = json.dumps([prev_hash, at, event_type, customer_gid, fence_id, code], separators=(",", ":"))
    return _sha256(body)


class Ledger:
    """Single-owner SQLite ledger. Every mutation runs in ``tx()``."""

    def __init__(self, state_root: Path, *, clock: Callable[[], datetime] = utc_now, allow_migration: bool = False):
        self.root = Path(state_root)
        self.clock = clock
        self._lock = _OwnerLock(self.root / "ledger.lock")
        self.conn: sqlite3.Connection | None = None
        try:
            self.conn = sqlite3.connect(self.root / "ledger.sqlite3", isolation_level=None, timeout=0)
            self.conn.row_factory = sqlite3.Row
            if self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise DirectLocalError("ledger_wal_unavailable")
            for pragma in ("synchronous=FULL", "foreign_keys=ON", "trusted_schema=OFF"):
                self.conn.execute(f"PRAGMA {pragma}")
            if self.conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise DirectLocalError("ledger_integrity_check_failed")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_sql(SCHEMA):
                        self.conn.execute(statement)
                    self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
                version = SCHEMA_VERSION
            elif version == _MIGRATABLE_SCHEMA_VERSION:
                # Only ``migrate-ledger`` opens it, and then only to migrate.
                if not allow_migration:
                    raise DirectLocalError("ledger_schema_migration_required")
            elif version != SCHEMA_VERSION:
                raise DirectLocalError("ledger_schema_version_unsupported")
            self.schema_version = version
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute("SELECT epoch FROM writer_owner WHERE id = 1").fetchone()
            self.epoch = (row["epoch"] if row else 0) + 1
            self.conn.execute(
                "INSERT INTO writer_owner(id, epoch, pid, acquired_at) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET epoch = excluded.epoch, pid = excluded.pid, acquired_at = excluded.acquired_at",
                (self.epoch, os.getpid(), ts(self.now())),
            )
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn is not None:
                self.conn.close()
            self._lock.release()
            raise

    def migrate_schema(self) -> dict[str, Any]:
        """Attended, additive v3 -> v4 migration (``migrate-ledger``): two
        nullable allocation-provenance columns, one transaction, one event.
        No existing row, binding, trigger or event is changed."""

        if self.schema_version == SCHEMA_VERSION:
            return {"status": "current", "schema_version": SCHEMA_VERSION}
        with self.tx() as conn:
            for statement in _SCHEMA_V4_MIGRATION:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.event(conn, f"ledger_schema_migrated:v{SCHEMA_VERSION}")
        self.schema_version = SCHEMA_VERSION
        return {"status": "migrated", "schema_version": SCHEMA_VERSION}

    def now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise DirectLocalError("clock_must_be_timezone_aware")
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
            row = self.conn.execute("SELECT epoch FROM writer_owner WHERE id = 1").fetchone()
            if row is None or row["epoch"] != self.epoch:
                raise DirectLocalError("ledger_owner_epoch_lost")
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def event(self, conn: sqlite3.Connection, event_type: str, *, customer_gid: str | None = None, fence_id: str | None = None, code: str | None = None) -> None:
        last = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS_HASH
        at = ts(self.now())
        conn.execute(
            "INSERT INTO events(at, type, customer_gid, fence_id, code, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (at, event_type, customer_gid, fence_id, code, prev, _event_hash(prev, at, event_type, customer_gid, fence_id, code)),
        )

    def verify_events(self) -> int:
        prev = GENESIS_HASH
        count = 0
        for row in self.conn.execute("SELECT * FROM events ORDER BY seq"):
            if row["prev_hash"] != prev or row["hash"] != _event_hash(prev, row["at"], row["type"], row["customer_gid"], row["fence_id"], row["code"]):
                raise DirectLocalError("event_chain_broken")
            prev = row["hash"]
            count += 1
        return count

    def meta(self) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM ledger_meta WHERE id = 1").fetchone()
        if row is None:
            raise DirectLocalError("ledger_not_initialised")
        return row


def _split_sql(script: str) -> list[str]:
    """Split the static schema on statement ends, keeping trigger bodies whole."""

    statements, buffer = [], []
    for line in script.strip().splitlines():
        buffer.append(line)
        text = "\n".join(buffer).strip()
        if text.endswith(";") and (not text.upper().startswith("CREATE TRIGGER") or text.upper().endswith("END;")):
            statements.append(text)
            buffer = []
    if "\n".join(buffer).strip():
        raise DirectLocalError("schema_parse_failed")
    return statements


# --------------------------------------------------------------------------
# AutoCount child bridge client
# --------------------------------------------------------------------------

def default_bridge_command(bridge_script: Path | None = None) -> list[str]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    script = bridge_script or REPO_ROOT / "scripts" / "ac2_directlocal_member_bridge.ps1"
    return [
        str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
        "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
    ]


@dataclass(frozen=True, slots=True)
class WriteRun:
    started: bool
    exit_code: int | None = None
    stdout: str = ""
    timed_out: bool = False
    exited: bool = True


class BridgeError(DirectLocalError):
    pass


# Fixed codes the bridge child (and the adapter/guard it loads) may throw.
# Only these are surfaced, as "bridge_read_failed:<code>"; anything else in a
# failing child's output (vendor text, unknown codes, malformed lines) becomes
# the generic "bridge_read_failed". stderr is never read.
BRIDGE_CHILD_ERROR_CODES = frozenset({
    "adapter_managed_defaults_are_not_caller_inputs", "autocount_adapter_disabled",
    "autocount_assembly_load_failed", "autocount_assembly_missing", "autocount_assembly_path_invalid",
    "autocount_assembly_path_missing", "autocount_authentication_failed",
    "autocount_authentication_surface_missing", "autocount_build_not_qualified",
    "autocount_connection_config_missing", "autocount_core_types_missing",
    "autocount_login_rejected", "autocount_member_command_binding_invalid",
    "autocount_member_field_missing", "autocount_member_field_read_only", "autocount_member_row_unavailable",
    "autocount_new_member_failed", "autocount_password_missing", "autocount_real_bridge_disabled",
    "autocount_session_binding_invalid", "autocount_session_factory_not_found",
    "autocount_session_initialization_failed", "autocount_sql_book_mismatch", "autocount_sql_connection_failed",
    "book_check_requires_real_session", "book_host_wrong", "book_identity_ambiguous", "book_identity_missing",
    "book_identity_unverifiable", "book_identity_wrong", "bridge_failed", "bridge_input_missing",
    "claim_create_failed", "claim_directory_invalid", "empty_string_not_null_equivalent",
    "fence_already_claimed", "fence_binding_mismatch", "fence_id_invalid", "host_not_authorised",
    "legacy_lookup_columns_missing", "legacy_lookup_input_invalid", "legacy_lookup_table_unavailable",
    "member_date_invalid", "member_expiry_before_register", "member_field_not_allowed", "member_field_required",
    "member_no_generator_failed", "member_no_invalid", "member_no_probe_failed", "member_no_required", "member_no_taken",
    "outbound_queue_not_empty", "payload_digest_mismatch", "phone_keys_invalid", "pos_sync_state_unreadable",
    "pos_sync_status_requires_real_session", "production_book_not_authorised", "production_book_refused",
    "real_autocount_object_refused", "resolve_guard_unavailable", "save_member_invocation_count_invalid", "session_factory_env_refused",
    "sync_changelog_failed", "sync_changelog_request_invalid", "sync_lookup_failed",
    "synthetic_session_binding_invalid", "synthetic_session_marker_missing", "synthetic_session_with_target_book",
    "target_book_not_authorised", "user_script_present", "vendor_error_telemetry_enabled",
    "vendor_query_not_allowlisted", "vendor_query_not_ascii", "vendor_query_not_read_only", "writer_deadline_expired", "writer_deadline_invalid",
})


def _last_output_line(stdout: bytes) -> tuple[bytes | None, int]:
    # Only the last non-empty line is decoded, so vendor bytes printed earlier
    # cannot hide the envelope. Returns (line, number of non-empty lines).
    lines = [line for line in stdout.replace(b"\r", b"\n").split(b"\n") if line.strip()]
    return (lines[-1] if lines else None), len(lines)


def bridge_failure_code(stdout: bytes, returncode: int | None = None) -> str:
    """Map a failing child's stdout to a fixed code. Exactly one envelope shape
    is trusted: a last line {"error": <allowlisted code>} with no other key.

    Anything else is generic, qualified only by values this function computes
    (never child text): a fixed shape class, the exit code as unsigned hex and
    the non-empty line count (capped), e.g. ``bridge_read_failed:output_empty:xe0434352:l0``.
    """
    line, count = _last_output_line(stdout)
    shape = "output_empty"
    if line is not None:
        try:
            envelope = json.loads(line.decode("utf-8"))
        except UnicodeDecodeError:
            shape = "output_not_utf8"
        except (json.JSONDecodeError, RecursionError):  # deep nesting: generic, never a crash (G2 O3)
            shape = "output_not_json"
        else:
            if not isinstance(envelope, dict):
                shape = "output_not_json"
            elif "error" not in envelope:
                shape = "result_on_failed_exit"
            elif set(envelope) != {"error"} or not isinstance(envelope["error"], str):
                shape = "envelope_invalid"
            elif envelope["error"] in BRIDGE_CHILD_ERROR_CODES:
                return f"bridge_read_failed:{envelope['error']}"
            else:
                shape = "code_unlisted"
    exit_part = "xnone" if returncode is None else f"x{returncode & 0xFFFFFFFF:08x}"
    return f"bridge_read_failed:{shape}:{exit_part}:l{min(count, 9)}"


def _member_no_parts(value: str | None) -> tuple[str, int, int] | None:
    match = re.fullmatch(r"(.*?)([0-9]+)", value or "")
    return (match.group(1), len(match.group(2)), int(match.group(2))) if match else None


def member_no_successor(seed: str, seen: set) -> str | None:
    """The next number in the generator's own format (same prefix, same digit
    width) above ``seed`` and every same-format value in ``seen``. None when
    the format has no trailing digits or the width would grow."""
    parts = _member_no_parts(seed)
    if parts is None:
        return None
    prefix, width, top = parts
    for value in seen:
        other = _member_no_parts(value)
        if other is not None and other[:2] == (prefix, width):
            top = max(top, other[2])
    digits = str(top + 1).zfill(width)
    candidate = prefix + digits
    if len(digits) != width or not MEMBER_NO_RE.fullmatch(candidate):
        return None
    return candidate


def payload_digest(salt: str, payload_canonical: str) -> str:
    return _sha256(f"{salt}:{payload_canonical}")


def canonical_payload(payload: Mapping[str, Any]) -> str:
    # ASCII-only so the child's console stdin encoding cannot alter it.
    return json.dumps(dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class BridgeClient:
    """One short-lived PowerShell child per operation. Protected values travel
    only on stdin; arguments carry no customer data; stderr is discarded."""

    def __init__(self, command: list[str], *, autocount_target: str = REAL_BRIDGE_TARGET, autocount_book: str | None = None, read_timeout_seconds: int = 120, popen: Callable[..., Any] = subprocess.Popen):
        self.command = list(command)
        self.autocount_target = autocount_target
        self.autocount_book = autocount_book
        self.read_timeout_seconds = read_timeout_seconds
        self._popen = popen

    def _start(self, arguments: list[str]):
        # The bound book is the bridge's only route to a real session; it is
        # re-proved inside the child from the authenticated vendor session.
        book = ["-TargetBook", self.autocount_book] if self.autocount_book else []
        return self._popen(
            self.command + arguments + book, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    @staticmethod
    def _stdin(value: Mapping[str, Any]) -> bytes:
        return (json.dumps(dict(value), ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")

    def _read(self, operation: str, value: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            process = self._start(["-Operation", operation, "-EnableProductionAdapter"])
        except OSError:
            raise BridgeError("bridge_start_failed") from None
        try:
            stdout, _ = process.communicate(self._stdin(value), timeout=self.read_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise BridgeError("bridge_read_timeout") from None
        if process.returncode != 0:
            raise BridgeError(bridge_failure_code(stdout, process.returncode))
        line, _ = _last_output_line(stdout)
        try:
            result = json.loads(line.decode("utf-8")) if line is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise BridgeError("bridge_output_invalid") from None
        if not isinstance(result, dict):
            raise BridgeError("bridge_output_invalid")
        return result

    def precheck(self, phone_keys: tuple[str, ...], email_address: str | None) -> Mapping[str, Any]:
        return self._read("legacy-precheck", {"phone_keys": list(phone_keys), "email_address": email_address})

    def next_member_no(self) -> str:
        value = self._read("next-member-no", {}).get("member_no")
        if not isinstance(value, str):
            raise BridgeError("bridge_output_invalid")
        return value

    def probe(self, member_no: str) -> str:
        status = self._read("probe", {"member_no": member_no}).get("status")
        if status not in ("FREE", "OCCUPIED"):
            raise BridgeError("bridge_output_invalid")
        return status

    def reconcile(self, member_no: str, payload_canonical_text: str) -> Mapping[str, Any]:
        return self._read("reconcile", {"member_no": member_no, "payload_canonical": payload_canonical_text})

    def book_check(self) -> Mapping[str, Any]:
        return self._read("book-check", {})

    def pos_sync_status(self) -> Mapping[str, Any]:
        return self._read("pos-sync-status", {})

    def write_once(self, *, fence_id: str, claim_directory: Path, deadline: datetime, stdin_value: Mapping[str, Any], wait_seconds: float, on_started: Callable[[int, str | None], None]) -> WriteRun:
        arguments = [
            "-Operation", "write-once", "-EnableProductionAdapter", "-FenceId", fence_id,
            "-ClaimDirectory", str(claim_directory), "-WriteDeadlineUtc", deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ]
        try:
            process = self._start(arguments)
        except OSError:
            return WriteRun(started=False)
        on_started(int(process.pid), child_creation_time(process))
        try:
            stdout, _ = process.communicate(self._stdin(stdin_value), timeout=max(wait_seconds, 1))
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                return WriteRun(started=True, timed_out=True, exited=False)
            return WriteRun(started=True, exit_code=process.returncode, timed_out=True, exited=True)
        return WriteRun(started=True, exit_code=process.returncode, stdout=stdout.decode("utf-8", "replace")[-4096:])


def classify_write(run: WriteRun, *, claim_exists: bool) -> tuple[str, str | None]:
    """Fence terminal state for one write-once child. Absence of the claim
    file is the only proof that ``SaveMember`` was never reached."""

    if not run.started:
        return ("NOT_WRITTEN_PROVEN", "writer_start_failed") if not claim_exists else ("WRITE_OUTCOME_UNCERTAIN", "claim_without_writer_start")
    if run.timed_out:
        if not run.exited:
            return "WRITER_TERMINATION_UNCONFIRMED", "writer_timeout_exit_unconfirmed"
        return ("WRITE_OUTCOME_UNCERTAIN", "writer_timeout") if claim_exists else ("NOT_WRITTEN_PROVEN", "writer_timeout_before_claim")
    if run.exit_code == 0:
        try:
            outcome = json.loads(run.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError, RecursionError):
            outcome = None
        valid = (
            isinstance(outcome, dict) and outcome.get("save_invocation_count") == 1
            and all(isinstance(outcome.get(key), bool) for key in ("readback_found", "readback_match", "outbound_queue_changed"))
        )
        if not valid:
            return "WRITE_OUTCOME_UNCERTAIN", "writer_outcome_invalid"
        if not outcome["readback_found"]:
            return "WRITE_OUTCOME_UNCERTAIN", "readback_absent"
        if not outcome["readback_match"]:
            return "CREATED_READBACK_MISMATCH", "readback_mismatch_manual_review"
        if outcome["outbound_queue_changed"]:
            # A mail/e-invoice queue row appeared during the write: hold for review.
            return "CREATED_OUTBOUND_QUEUE_CHANGED", "outbound_queue_changed_manual_review"
        return "CREATED_VERIFIED", None
    if run.exit_code == 3:
        return "WRITE_OUTCOME_UNCERTAIN", "fence_already_claimed"
    return ("WRITE_OUTCOME_UNCERTAIN", "writer_failed_after_claim") if claim_exists else ("NOT_WRITTEN_PROVEN", "writer_failed_before_claim")


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel32


def _creation_time(kernel32, handle) -> str | None:
    import ctypes
    from ctypes import wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel32.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
        return None
    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def child_creation_time(process: Any) -> str | None:
    """Creation instant of a started child, so a reused PID is never mistaken
    for it. ``None`` (unknown) keeps recovery on HOLD."""

    handle = getattr(process, "_handle", None)
    if os.name != "nt" or handle is None:
        return None
    return _creation_time(_kernel32(), int(handle))


def probe_writer(pid: int, created: str | None) -> str:
    """``running``, ``exited`` or ``unknown``. Only ``exited`` is proof: the
    PID no longer exists, or it now belongs to a process with a different
    creation instant (PID reuse). Any failure or missing identity is
    ``unknown`` and recovery stays on HOLD."""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return "exited" if ctypes.get_last_error() == 87 else "unknown"  # 87: no such process
        try:
            current = _creation_time(kernel32, handle)
            if current is None or created is None:
                return "unknown"
            if current != created:
                return "exited"
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return "unknown"
            return "running" if code.value == 259 else "exited"  # 259: STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return "exited"
    except OSError:
        return "unknown"
    return "unknown"  # present, but without a creation identity it cannot be ours or not


# --------------------------------------------------------------------------
# Processor
# --------------------------------------------------------------------------

class Processor:
    def __init__(self, ledger: Ledger, *, writer_probe: Callable[[int, str | None], str] = probe_writer, writer_deadline_seconds: int = WRITER_DEADLINE_SECONDS, writer_grace_seconds: int = WRITER_GRACE_SECONDS):
        self.ledger = ledger
        self.writer_probe = writer_probe
        self.writer_deadline_seconds = writer_deadline_seconds
        self.writer_grace_seconds = writer_grace_seconds
        self.inbox = ledger.root / "inbox"
        self.claims = ledger.root / "claims"

    # -- setup ------------------------------------------------------------
    def init_ledger(self, *, mode: str, store_gid: str, shop_domain: str, environment: str | None = None) -> dict[str, Any]:
        if mode not in QUALIFIED_RESPONSE_SHAPES:
            raise DirectLocalError("capture_response_shape_unqualified")
        if mode == "synthetic":
            environment = environment or "synthetic"
            if environment != "synthetic":
                raise DirectLocalError("environment_mode_mismatch")
            if not SHOP_GID_RE.fullmatch(store_gid) or not SYNTHETIC_DOMAIN_RE.fullmatch(shop_domain):
                raise DirectLocalError("synthetic_store_identity_invalid")
        else:
            if environment not in ENVIRONMENT_BOOKS or environment == "synthetic":
                raise DirectLocalError("environment_mode_mismatch")
            if environment not in AUTHORISED_REAL_ENVIRONMENTS:
                raise DirectLocalError(f"{environment}_environment_not_authorised")
            if QUALIFIED_CONNECTOR_STORES.get(store_gid) != shop_domain:
                raise DirectLocalError("connector_store_identity_not_qualified")
            verify_private_state(self.ledger.root)
        with self.ledger.tx() as conn:
            if conn.execute("SELECT 1 FROM ledger_meta").fetchone():
                raise DirectLocalError("ledger_already_initialised")
            conn.execute(
                "INSERT INTO ledger_meta(id, ledger_id, mode, environment, autocount_book, ref_key, store_gid, shop_domain, created_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"ledger-{uuid.uuid4().hex}", mode, environment, ENVIRONMENT_BOOKS[environment], secrets.token_hex(32), store_gid, shop_domain, ts(self.ledger.now())),
            )
            self.ledger.event(conn, f"ledger_initialised:{mode}:{environment}")
        self.inbox.mkdir(exist_ok=True)
        self.claims.mkdir(exist_ok=True)
        return {"status": "initialised", "mode": mode, "environment": environment, "autocount_book": ENVIRONMENT_BOOKS[environment]}

    def check_custody(self) -> None:
        """A connector ledger holds real customer identifiers: its state root
        must stay private for every command, not only at init."""

        if self.ledger.meta()["mode"] == "connector":
            verify_private_state(self.ledger.root)

    def _require_bridge(self, bridge: Any) -> None:
        """Bind the bridge to the ledger. A synthetic ledger is inert: only the
        test-only fake target is accepted, so no real AutoCount process can be
        started from it. A connector ledger accepts only a real bridge bound to
        its own authorised book, and only while its state root is private."""

        meta = self.ledger.meta()
        target = getattr(bridge, "autocount_target", None)
        if meta["mode"] == "synthetic":
            if target != SYNTHETIC_BRIDGE_TARGET:
                raise DirectLocalError("real_autocount_refused_synthetic_ledger")
            return
        if meta["environment"] not in AUTHORISED_REAL_ENVIRONMENTS:
            raise DirectLocalError(f"{meta['environment']}_environment_not_authorised")
        if target != REAL_BRIDGE_TARGET or getattr(bridge, "autocount_book", None) != meta["autocount_book"]:
            raise DirectLocalError("bridge_book_binding_mismatch")
        self.check_custody()

    def _ref(self, kind: str, value: str) -> str:
        """Keyed, non-enumerable public reference (``c-`` customer, ``m-``
        member). Without the ledger's random key it cannot be reversed by
        enumerating GIDs or MemberNos."""

        key = bytes.fromhex(self.ledger.meta()["ref_key"])
        return f"{kind}-" + hmac.new(key, f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()[:MEMBER_REF_HEX]

    def _cutover(self, conn: sqlite3.Connection) -> datetime:
        row = conn.execute("SELECT cutover_at FROM runs WHERE kind = 'BASELINE' AND state = 'SEALED'").fetchone()
        if row is None:
            raise DirectLocalError("baseline_not_sealed")
        return parse_ts(row["cutover_at"])

    # -- runs (baseline / discovery) ----------------------------------------
    def begin_run(self, kind: str, *, recent: bool = False, origin: str | None = None) -> dict[str, Any]:
        """``origin`` names a non-attended run starter (only the direct-HTTP
        poller). It is recorded in the same transaction as the run, in the
        hash-chained ``run_started`` event, and binds the run's provenance."""

        self.ledger.meta()
        if recent and kind != "DISCOVERY":
            raise DirectLocalError("run_kind_invalid")
        if origin is not None and origin not in RUN_ORIGINS:
            raise DirectLocalError("run_origin_invalid")
        with self.ledger.tx() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE state = 'CAPTURING'").fetchone():
                raise DirectLocalError("run_already_open")
            baseline = conn.execute("SELECT state, cutover_at FROM runs WHERE kind = 'BASELINE' AND state <> 'ABANDONED'").fetchone()
            now = self.ledger.now().replace(microsecond=0)
            if kind == "BASELINE":
                if baseline is not None:
                    raise DirectLocalError("baseline_already_exists")
                previous = conn.execute("SELECT MAX(cutover_at) AS c FROM runs WHERE kind = 'BASELINE' AND state = 'ABANDONED'").fetchone()["c"]
                if previous is not None and now <= parse_ts(previous):
                    raise DirectLocalError("cutover_not_advanced")
                cutover = ts(now)  # C: fixed before any page is requested
            elif kind == "DISCOVERY":
                if baseline is None or baseline["state"] != "SEALED":
                    raise DirectLocalError("baseline_not_sealed")
                cutover = baseline["cutover_at"]
            else:
                raise DirectLocalError("run_kind_invalid")
            search = None
            if recent:
                # Watermark: the last sealed discovery start minus a margin for
                # search-index lag, never before the cutover.
                last = conn.execute("SELECT MAX(started_at) AS s FROM runs WHERE kind = 'DISCOVERY' AND state = 'SEALED'").fetchone()["s"]
                mark = parse_ts(cutover) if last is None else max(parse_ts(cutover), parse_ts(last) - timedelta(seconds=RECENT_WATERMARK_MARGIN_SECONDS))
                search = f"updated_at:>='{ts(mark)}'"
            run_id = f"run-{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO runs(run_id, kind, cutover_at, state, started_at, search) VALUES (?, ?, ?, 'CAPTURING', ?, ?)",
                (run_id, kind, cutover, ts(now), search),
            )
            self.ledger.event(conn, f"run_started:{kind.lower()}{':recent' if recent else ''}", code=None if origin is None else f"{origin}:{run_id}")
        query = RECENT_QUERY if recent else BASELINE_QUERY
        return {"run_id": run_id, "kind": kind, "cutover_at": cutover, "search": search, "page_size": PAGE_SIZE, "query_sha256": _sha256(query)}

    def run_origin(self, conn: sqlite3.Connection, run_id: str) -> str | None:
        """The recorded non-attended origin of a run, or ``None`` (attended)."""

        for origin in sorted(RUN_ORIGINS):
            if conn.execute("SELECT 1 FROM events WHERE type LIKE 'run_started:%' AND code = ?", (f"{origin}:{run_id}",)).fetchone():
                return origin
        return None

    @staticmethod
    def _page_of(run: sqlite3.Row, capture: Capture) -> bool:
        """Whether a page capture belongs to ``run`` (same query form and search)."""

        if run["search"] is None:
            return capture.kind == "baseline"
        return capture.kind == "recent" and capture.variables.get("search") == run["search"]

    def abandon_run(self, run_id: str, *, confirm_suffix: str) -> dict[str, Any]:
        """Abandon an incomplete (CAPTURING) run; a sealed run never changes.
        Its unconsumed page captures are recorded as consumed so a fresh run
        (with a new, later cutover for a baseline) starts from a clean inbox."""

        if not re.fullmatch(r"[0-9a-f]{8}", confirm_suffix or "") or not run_id.endswith(confirm_suffix):
            raise DirectLocalError("abandon_confirmation_mismatch")
        meta = self.ledger.meta()
        with self.ledger.tx() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None or run["state"] != "CAPTURING":
                raise DirectLocalError("run_not_abandonable")
            now = ts(self.ledger.now())
            retired = 0
            for path in sorted(self.inbox.glob("*.json")):
                try:
                    capture = parse_capture(path.read_bytes(), mode=meta["mode"], store_gid=meta["store_gid"], shop_domain=meta["shop_domain"])
                except CaptureRejected:
                    continue  # never consumed; it still blocks any later ingest
                if capture.kind not in PAGE_KINDS or conn.execute("SELECT 1 FROM captures WHERE capture_sha256 = ? OR tool_use_id = ?", (capture.capture_sha256, capture.tool_use_id)).fetchone():
                    continue
                conn.execute("INSERT INTO captures(capture_sha256, tool_use_id, kind, consumed_at) VALUES (?, ?, 'abandoned', ?)", (capture.capture_sha256, capture.tool_use_id, now))
                retired += 1
            conn.execute("UPDATE runs SET state = 'ABANDONED', sealed_at = ? WHERE run_id = ?", (now, run_id))
            self.ledger.event(conn, f"run_abandoned:{run['kind'].lower()}")
        return {"run_id": run_id, "state": "ABANDONED", "captures_retired": retired}

    def _inbox_captures(self, conn: sqlite3.Connection, meta: sqlite3.Row) -> list[tuple[Path, Capture]]:
        found = []
        for path in sorted(self.inbox.glob("*.json")):
            capture = parse_capture(path.read_bytes(), mode=meta["mode"], store_gid=meta["store_gid"], shop_domain=meta["shop_domain"])
            found.append((path, capture))
        return found

    def ingest_run(self) -> dict[str, Any]:
        meta = self.ledger.meta()
        with self.ledger.tx() as conn:
            run = conn.execute("SELECT * FROM runs WHERE state = 'CAPTURING'").fetchone()
            if run is None:
                raise DirectLocalError("no_open_run")
            started = parse_ts(run["started_at"])
            pages: dict[str | None, Capture] = {}
            duplicates: list[Capture] = []  # identical re-captures, consumed with the run
            for _, capture in self._inbox_captures(conn, meta):
                if not self._page_of(run, capture):
                    continue
                if conn.execute("SELECT 1 FROM captures WHERE capture_sha256 = ? OR tool_use_id = ?", (capture.capture_sha256, capture.tool_use_id)).fetchone():
                    continue  # consumed by an earlier run: replay suppressed
                if capture.captured_at < started:
                    raise DirectLocalError("page_capture_predates_run")
                after = capture.variables.get("after")
                existing = pages.get(after)
                if existing is not None:
                    if existing.data != capture.data:
                        raise DirectLocalError("page_capture_conflict")
                    duplicates.append(capture)
                    continue
                pages[after] = capture
            used: list[Capture] = []

            def page_reader(after: str | None) -> Mapping[str, Any]:
                capture = pages.get(after)
                if capture is None:
                    raise ShopifyReadError("capture_page_missing")
                used.append(capture)
                page = capture.data["customers"]
                return {"pageInfo": page["pageInfo"], "nodes": [_registration_view(node, "registration") for node in page["nodes"]]}

            try:
                historical, transition = capture_member_mg_baseline(page_reader, cutover_at=parse_ts(run["cutover_at"]))
            except BaselineCaptureError as exc:
                raise DirectLocalError(f"run_not_sealed:{exc.code}") from None
            if len(used) != len(pages):
                raise DirectLocalError("run_not_sealed:unchained_page_capture")
            # One provenance per run, bound to who started it: direct pages
            # only in a poller-started run, hook pages only in an attended one.
            provenances = {capture.provenance for capture in used}
            if len(provenances) > 1:
                raise DirectLocalError("run_not_sealed:mixed_capture_provenance")
            origin = self.run_origin(conn, run["run_id"])
            expected = DIRECT_CAPTURE_PROVENANCE if origin == DIRECT_RUN_ORIGIN else CAPTURE_PROVENANCE
            if provenances != {expected}:
                raise DirectLocalError("run_not_sealed:run_origin_provenance_mismatch")
            now = ts(self.ledger.now())
            for capture in duplicates:
                conn.execute("INSERT INTO captures(capture_sha256, tool_use_id, kind, consumed_at) VALUES (?, ?, ?, ?)", (capture.capture_sha256, capture.tool_use_id, capture.kind, now))
            for index, capture in enumerate(used):
                page = capture.data["customers"]
                conn.execute(
                    "INSERT INTO run_pages(run_id, page_index, capture_sha256, has_next, node_count) VALUES (?, ?, ?, ?, ?)",
                    (run["run_id"], index, capture.capture_sha256, int(page["pageInfo"]["hasNextPage"]), len(page["nodes"])),
                )
                conn.execute("INSERT INTO captures(capture_sha256, tool_use_id, kind, consumed_at) VALUES (?, ?, ?, ?)", (capture.capture_sha256, capture.tool_use_id, capture.kind, now))
            discovered = 0
            if run["kind"] == "BASELINE":
                for gid in historical:
                    conn.execute("INSERT INTO baseline_members(customer_gid) VALUES (?)", (gid,))
                candidates = transition
            else:
                baseline = {row["customer_gid"] for row in conn.execute("SELECT customer_gid FROM baseline_members")}
                # Historical-by-createdAt GIDs missing from the sealed baseline
                # are surfaced too; evaluate_profile sends them to review.
                candidates = tuple(transition) + tuple(gid for gid in historical if gid not in baseline)
            for gid in candidates:
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO customers(store_gid, customer_gid, state, updated_at, discovered_at) VALUES (?, ?, 'DISCOVERED', ?, ?)",
                    (meta["store_gid"], gid, now, now),
                ).rowcount
                if inserted:
                    discovered += 1
                    self.ledger.event(conn, "customer_discovered", customer_gid=gid)
            conn.execute(
                "UPDATE runs SET state = 'SEALED', sealed_at = ?, page_count = ?, historical_count = ?, transition_count = ?, historical_digest = ? WHERE run_id = ?",
                (now, len(used), len(historical), len(transition), baseline_digest(list(historical)), run["run_id"]),
            )
            self.ledger.event(conn, f"run_sealed:{run['kind'].lower()}", code=origin)
        return {"run_id": run["run_id"], "kind": run["kind"], "pages": len(used), "historical": len(historical), "transition": len(transition), "discovered": discovered}

    # -- recovery -----------------------------------------------------------
    def recover(self) -> dict[str, int]:
        counts = {"orphaned": 0, "uncertain": 0, "not_written": 0}
        now = self.ledger.now()
        with self.ledger.tx() as conn:
            for fence in conn.execute("SELECT * FROM fences WHERE state IN ('FENCED', 'DISPATCHED', 'WRITER_TERMINATION_UNCONFIRMED')").fetchall():
                fence_id = fence["fence_id"]
                if fence["state"] == "FENCED":
                    # The child is only ever started after DISPATCHED commits.
                    self._fence_state(conn, fence, "MANUAL_REVIEW", "fence_orphaned_pre_dispatch")
                    counts["orphaned"] += 1
                    continue
                if fence["state"] == "DISPATCHED" and now < parse_ts(fence["deadline_at"]) + timedelta(seconds=self.writer_grace_seconds):
                    raise DirectLocalHold("writer_may_be_live")
                # Exit must be proven against the recorded child identity
                # (PID + creation instant). Unknown identity or an
                # indeterminate probe stays on HOLD; only an attended
                # release-writer-hold can move it on (to uncertain, never to
                # not-written).
                if fence["child_pid"] is None or fence["child_created"] is None:
                    raise DirectLocalHold("writer_identity_unrecorded")
                probe = self.writer_probe(fence["child_pid"], fence["child_created"])
                if probe != "exited":
                    raise DirectLocalHold("writer_may_be_live" if probe == "running" else "writer_exit_unproven")
                claim = (self.claims / f"{fence_id}.claim").exists()
                if fence["state"] == "DISPATCHED" and not claim:
                    self._fence_state(conn, fence, "NOT_WRITTEN_PROVEN", "dispatch_owner_lost_before_claim")
                    counts["not_written"] += 1
                else:
                    code = "dispatch_owner_lost" if fence["state"] == "DISPATCHED" else "writer_termination_resolved"
                    self._fence_state(conn, fence, "WRITE_OUTCOME_UNCERTAIN", code)
                    counts["uncertain"] += 1
        return counts

    def release_writer_hold(self, fence_id: str, *, confirm_suffix: str) -> dict[str, Any]:
        """Attended exit from a HOLD whose writer exit cannot be proven. The
        operator attests no bridge process remains; the fence becomes
        WRITE_OUTCOME_UNCERTAIN (reconcile read-only), never not-written."""

        if not re.fullmatch(r"[0-9a-f]{8}", confirm_suffix or "") or not fence_id.endswith(confirm_suffix):
            raise DirectLocalError("release_confirmation_mismatch")
        with self.ledger.tx() as conn:
            fence = conn.execute("SELECT * FROM fences WHERE fence_id = ?", (fence_id,)).fetchone()
            if fence is None or fence["state"] not in ("DISPATCHED", "WRITER_TERMINATION_UNCONFIRMED"):
                raise DirectLocalError("no_writer_hold")
            if self.ledger.now() < parse_ts(fence["deadline_at"]) + timedelta(seconds=self.writer_grace_seconds):
                raise DirectLocalHold("writer_may_be_live")
            self._fence_state(conn, fence, "WRITE_OUTCOME_UNCERTAIN", "operator_attested_writer_absent")
        return {"fence_state": "WRITE_OUTCOME_UNCERTAIN", "code": "operator_attested_writer_absent"}

    def _fence_state(self, conn: sqlite3.Connection, fence: sqlite3.Row, state: str, code: str | None, **extra: Any) -> None:
        columns = ", ".join(f"{name} = ?" for name in extra)
        conn.execute(
            f"UPDATE fences SET state = ?, code = ?, updated_at = ?{', ' + columns if columns else ''} WHERE fence_id = ?",
            (state, code, ts(self.ledger.now()), *extra.values(), fence["fence_id"]),
        )
        self.ledger.event(conn, f"fence:{state.lower()}", customer_gid=fence["customer_gid"], fence_id=fence["fence_id"], code=code)

    # -- profile processing ---------------------------------------------------
    def _consume_profile(self, conn: sqlite3.Connection, capture_path: Path, meta: sqlite3.Row) -> Capture:
        path = Path(capture_path).resolve()
        if path.parent != self.inbox.resolve():
            raise DirectLocalError("capture_outside_inbox")
        capture = parse_capture(path.read_bytes(), mode=meta["mode"], store_gid=meta["store_gid"], shop_domain=meta["shop_domain"])
        if capture.kind != "profile":
            raise DirectLocalError("capture_not_profile")
        if conn.execute("SELECT 1 FROM captures WHERE capture_sha256 = ? OR tool_use_id = ?", (capture.capture_sha256, capture.tool_use_id)).fetchone():
            raise DirectLocalError("capture_replayed")
        age = (self.ledger.now() - capture.captured_at).total_seconds()
        if age < -60 or age > CAPTURE_MAX_AGE_SECONDS:
            raise DirectLocalError("capture_stale")
        conn.execute(
            "INSERT INTO captures(capture_sha256, tool_use_id, kind, consumed_at) VALUES (?, ?, 'profile', ?)",
            (capture.capture_sha256, capture.tool_use_id, ts(self.ledger.now())),
        )
        return capture

    def _decide(self, conn: sqlite3.Connection, capture: Capture, customer: sqlite3.Row, *, complete_required: bool | None = None):
        gid = capture.variables["id"]
        in_baseline = conn.execute("SELECT 1 FROM baseline_members WHERE customer_gid = ?", (gid,)).fetchone() is not None
        required = customer["check_count"] + 1 >= PROFILE_WAIT_MAX_CHECKS if complete_required is None else complete_required
        record = capture.data["customer"]
        view = None if record is None else _registration_view(record, "startDate")
        decision = evaluate_profile(view, in_baseline=in_baseline, cutover_at=self._cutover(conn), profile_complete_required=required)
        # G2 A1 (2026-10-16): Forms tags member-mg about three seconds before
        # it writes start_date, also for an EXISTING customer. Until that
        # metafield exists the registration instant is unknown, so the
        # Customer.createdAt fallback must not end an old customer's
        # registration as pre-existing: it waits within the same bounded
        # budget, and only an exhausted budget (for example a staff-applied
        # tag with no registration) goes to review.
        if decision.outcome == "MANUAL_REVIEW" and decision.code == "preexisting_customer_not_new_signup" and record.get("startDate") is None and not required:
            return AdmissionDecision("WAIT", "registration_event_pending")
        return decision

    def _bind_registration(self, conn: sqlite3.Connection, capture: Capture, customer: sqlite3.Row) -> str | None:
        """Bind the registration event (metafield GID + instant) on first
        sight; a later read naming a different event goes to review."""

        record = capture.data["customer"]
        if record is None:
            return None
        event_id, instant = registration_event(record, "startDate")
        if event_id is None or instant is None:
            return None  # in flight or malformed: evaluate_profile decides (wait / review)
        at = instant.isoformat().replace("+00:00", "Z")
        if customer["registration_id"] is None:
            conn.execute("UPDATE customers SET registration_id = ?, registration_at = ? WHERE customer_gid = ?", (event_id, at, customer["customer_gid"]))
            return None
        if (customer["registration_id"], customer["registration_at"]) != (event_id, at):
            return "registration_event_changed"
        return None

    @staticmethod
    def _customer(conn: sqlite3.Connection, gid: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM customers WHERE customer_gid = ?", (gid,)).fetchone()
        if row is None:
            raise DirectLocalError("customer_not_discovered")
        return row

    def _set_customer(self, conn: sqlite3.Connection, gid: str, state: str, code: str | None, *, count_check: bool = False) -> None:
        conn.execute(
            "UPDATE customers SET state = ?, decision_code = ?, check_count = check_count + ?, updated_at = ? WHERE customer_gid = ?",
            (state, code, 1 if count_check else 0, ts(self.ledger.now()), gid),
        )
        self.ledger.event(conn, f"customer:{state.lower()}", customer_gid=gid, code=code)

    def _review(self, gid: str, code: str) -> dict[str, Any]:
        with self.ledger.tx() as conn:
            self._set_customer(conn, gid, "MANUAL_REVIEW", code)
        return {"customer_state": "MANUAL_REVIEW", "code": code}

    def _discard(self, capture_path: Path) -> bool:
        try:
            Path(capture_path).unlink()
            return True
        except OSError:
            return False

    def process(self, capture_path: Path, *, bridge: BridgeClient | None = None, enable_write: bool = False) -> dict[str, Any]:
        if enable_write:
            self._require_bridge(bridge)  # before any state change
        self.recover()
        meta = self.ledger.meta()
        outcome_map = {"ADMIT": "ADMITTED", "WAIT": "WAITING", "NOT_ELIGIBLE": "NOT_ELIGIBLE", "EXCLUDED_BASELINE": "EXCLUDED_BASELINE", "MANUAL_REVIEW": "MANUAL_REVIEW"}
        with self.ledger.tx() as conn:
            capture = self._consume_profile(conn, capture_path, meta)
            gid = capture.variables["id"]
            customer = self._customer(conn, gid)
            if customer["state"] in CUSTOMER_TERMINAL:
                raise DirectLocalError("customer_state_terminal")
            decision = self._decide(conn, capture, customer)
            state, code = outcome_map[decision.outcome], decision.code
            changed = self._bind_registration(conn, capture, customer)
            if changed and state in ("ADMITTED", "WAITING"):
                state, code = "MANUAL_REVIEW", changed
            if state == "ADMITTED":
                # A value AutoCount cannot store exactly is never admitted.
                issue = autocount_text_issue(decision.create_payload or {})
                if issue:
                    state, code = "MANUAL_REVIEW", issue
            self._set_customer(conn, gid, state, code, count_check=True)
        result: dict[str, Any] = {"customer_ref": self._ref("c", gid), "customer_state": state, "code": code, "capture_discarded": self._discard(capture_path)}
        if state != "ADMITTED" or not enable_write:
            return result
        result.update(self._write(gid, decision.create_payload or {}, bridge))
        return result

    def approve_write(self, gid: str, *, confirm_suffix: str, ttl_seconds: int = 900) -> dict[str, Any]:
        # The operator retypes the GID's trailing digits; a mistyped GID never approves.
        if not GID_RE.fullmatch(gid) or not re.fullmatch(r"[0-9]{4,20}", confirm_suffix or "") or not gid.endswith(confirm_suffix):
            raise DirectLocalError("approval_confirmation_mismatch")
        if not 60 <= ttl_seconds <= APPROVAL_MAX_TTL_SECONDS:
            raise DirectLocalError("approval_ttl_invalid")
        self.recover()
        with self.ledger.tx() as conn:
            if self._customer(conn, gid)["state"] != "ADMITTED":
                raise DirectLocalError("approval_requires_admitted_customer")
            approval_id = f"approval-{uuid.uuid4().hex}"
            now = self.ledger.now()
            conn.execute(
                "INSERT INTO approvals(approval_id, customer_gid, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (approval_id, gid, ts(now), ts(now + timedelta(seconds=ttl_seconds))),
            )
            self.ledger.event(conn, "write_approved", customer_gid=gid)
        return {"approval_id": approval_id, "expires_in_seconds": ttl_seconds}

    def _live_approval(self, conn: sqlite3.Connection, gid: str) -> str | None:
        row = conn.execute(
            "SELECT approval_id FROM approvals WHERE customer_gid = ? AND consumed_at IS NULL AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
            (gid, ts(self.ledger.now())),
        ).fetchone()
        return row["approval_id"] if row else None

    # -- standing admission policy ---------------------------------------------
    def _live_policy(self, conn: sqlite3.Connection) -> sqlite3.Row | None:
        meta = self.ledger.meta()
        return conn.execute(
            "SELECT * FROM admission_policies WHERE revoked_at IS NULL AND expires_at > ? AND environment = ? AND autocount_book = ? AND store_gid = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (ts(self.ledger.now()), meta["environment"], meta["autocount_book"], meta["store_gid"]),
        ).fetchone()

    def _policy_approval(self, conn: sqlite3.Connection, gid: str) -> bool:
        """Mint one single-use approval for an ADMITTED customer under the live
        policy, within its create bound. Never for a customer that already
        had one: a customer has at most one fence, ever."""

        policy = self._live_policy(conn)
        if policy is None or self.ledger.meta()["environment"] not in AUTHORISED_REAL_ENVIRONMENTS:
            return False
        if self._customer(conn, gid)["state"] != "ADMITTED":
            return False
        minted = conn.execute("SELECT COUNT(*) FROM approvals WHERE policy_id = ?", (policy["policy_id"],)).fetchone()[0]
        if minted >= policy["max_creates"] or conn.execute("SELECT 1 FROM approvals WHERE customer_gid = ? AND policy_id IS NOT NULL", (gid,)).fetchone():
            return False
        now = self.ledger.now()
        conn.execute(
            "INSERT INTO approvals(approval_id, customer_gid, created_at, expires_at, policy_id) VALUES (?, ?, ?, ?, ?)",
            (f"approval-{uuid.uuid4().hex}", gid, ts(now), ts(now + timedelta(seconds=POLICY_APPROVAL_TTL_SECONDS)), policy["policy_id"]),
        )
        self.ledger.event(conn, "write_auto_approved", customer_gid=gid, code=policy["policy_id"])
        return True

    def activate_policy(self, *, confirm_book: str, max_creates: int, ttl_hours: int) -> dict[str, Any]:
        """Standing admission (#155:6195468717): after this one explicit,
        bounded activation, each eligible registration receives a single-use
        approval automatically. Bound to this ledger's environment, book and
        store; capped; expiring; revocable. Production is refused by source."""

        meta = self.ledger.meta()
        if meta["mode"] != "connector" or meta["environment"] not in AUTHORISED_REAL_ENVIRONMENTS:
            raise DirectLocalError(f"{meta['environment']}_environment_not_authorised")
        if confirm_book != meta["autocount_book"]:
            raise DirectLocalError("policy_book_confirmation_mismatch")
        if not 1 <= max_creates <= 500 or not 1 <= ttl_hours <= POLICY_MAX_TTL_HOURS:
            raise DirectLocalError("policy_bounds_invalid")
        self.check_custody()
        with self.ledger.tx() as conn:
            if self._live_policy(conn) is not None:
                raise DirectLocalError("policy_already_active")
            policy_id = f"policy-{uuid.uuid4().hex}"
            now = self.ledger.now()
            conn.execute(
                "INSERT INTO admission_policies(policy_id, environment, autocount_book, store_gid, max_creates, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (policy_id, meta["environment"], meta["autocount_book"], meta["store_gid"], max_creates, ts(now), ts(now + timedelta(hours=ttl_hours))),
            )
            self.ledger.event(conn, "policy_activated", code=policy_id)
        return {"policy_id": policy_id, "max_creates": max_creates, "expires_in_hours": ttl_hours, "autocount_book": meta["autocount_book"]}

    def revoke_policy(self) -> dict[str, Any]:
        with self.ledger.tx() as conn:
            policy = self._live_policy(conn)
            if policy is None:
                raise DirectLocalError("no_active_policy")
            conn.execute("UPDATE admission_policies SET revoked_at = ? WHERE policy_id = ?", (ts(self.ledger.now()), policy["policy_id"]))
            self.ledger.event(conn, "policy_revoked", code=policy["policy_id"])
        return {"policy_id": policy["policy_id"], "state": "REVOKED"}

    # -- POS visibility evidence ------------------------------------------------
    def _fence_by_member_ref(self, conn: sqlite3.Connection, member_ref: str) -> sqlite3.Row:
        for fence in conn.execute("SELECT * FROM fences"):
            if hmac.compare_digest(self._ref("m", fence["member_no"]), member_ref or ""):
                return fence
        raise DirectLocalError("member_ref_unknown")

    def record_pos_visible(self, *, member_ref: str, outlet: str, observed_at: str) -> dict[str, Any]:
        """Operator-attested observation that the member is selectable at a
        POS terminal of ``outlet`` (UAT evidence; never a write trigger)."""

        if not OUTLET_RE.fullmatch(outlet or ""):
            raise DirectLocalError("outlet_invalid")
        observed = parse_ts(observed_at)
        with self.ledger.tx() as conn:
            fence = self._fence_by_member_ref(conn, member_ref)
            if fence["state"] not in ("CREATED_VERIFIED", "RECONCILED_VERIFIED"):
                raise DirectLocalError("member_not_verified_created")
            if observed < parse_ts(fence["created_at"]) or observed > self.ledger.now() + timedelta(seconds=60):
                raise DirectLocalError("pos_observation_time_invalid")
            conn.execute("INSERT INTO pos_observations(fence_id, outlet, observed_at, recorded_at) VALUES (?, ?, ?, ?)", (fence["fence_id"], outlet, ts(observed), ts(self.ledger.now())))
            self.ledger.event(conn, "pos_visible_recorded", customer_gid=fence["customer_gid"], fence_id=fence["fence_id"], code=outlet)
        return {"member_ref": member_ref, "outlet": outlet, "recorded": True}

    def pos_sync_status(self, bridge: BridgeClient) -> dict[str, Any]:
        """Read-only HQ view of POS outlet sync (no member data): each outlet's
        last finished sync and liveness, plus the Member change counter."""

        if self.ledger.meta()["mode"] != "connector":
            raise DirectLocalError("pos_sync_status_requires_connector_ledger")
        self._require_bridge(bridge)
        return dict(bridge.pos_sync_status())

    def _allocate(self, gid: str, bridge: BridgeClient) -> tuple[str | None, str | None]:
        with self.ledger.tx() as conn:
            bound = self._customer(conn, gid)["member_no"]
        if bound is not None:
            return bound, None
        while True:
            with self.ledger.tx() as conn:
                attempts = conn.execute("SELECT candidate FROM allocation_attempts WHERE customer_gid = ? ORDER BY attempt_no", (gid,)).fetchall()
                if len(attempts) >= MAX_MEMBER_NO_CANDIDATES:
                    return None, "member_no_candidates_exhausted"
                attempt_no = len(attempts) + 1
                # Committed before the generator call: a crash still spends it.
                conn.execute("INSERT INTO allocation_attempts(customer_gid, attempt_no, created_at) VALUES (?, ?, ?)", (gid, attempt_no, ts(self.ledger.now())))
                self.ledger.event(conn, "member_no_attempt", customer_gid=gid)
            previous = {row["candidate"] for row in attempts}
            # G2 A3: the generator's raw preview and whether the candidate is
            # that preview or a successor derived from it, recorded with the
            # status in the attempt's single update. No extra vendor call.
            preview: str | None = None
            derivation: str | None = None

            def record(candidate: str | None, status: str) -> None:
                with self.ledger.tx() as conn:
                    conn.execute(
                        "UPDATE allocation_attempts SET candidate = ?, status = ?, generator_preview = ?, derivation = ? WHERE customer_gid = ? AND attempt_no = ?",
                        (candidate, status, preview, derivation, gid, attempt_no),
                    )

            try:
                candidate = bridge.next_member_no()
            except BridgeError:
                record(None, "GENERATOR_FAILED")
                return None, "member_no_generator_failed"
            if not MEMBER_NO_RE.fullmatch(candidate):
                record(None, "GENERATOR_INVALID")  # an invalid value is never stored
                return None, "member_no_generator_invalid"
            preview, derivation = candidate, "generator"
            if candidate in previous:
                # GetNextMemberNo previews a counter that a save under an
                # explicit number does not advance (AED_TESTINGAPI, 16 Oct
                # 2026: it kept returning the E7 member's number). Continue the
                # official format above every number already tried here or
                # bound in this ledger; the candidate is still ledger-checked
                # and probed, within MAX_MEMBER_NO_CANDIDATES.
                with self.ledger.tx() as conn:
                    bound_nos = {row["member_no"] for row in conn.execute("SELECT member_no FROM customers WHERE member_no IS NOT NULL")}
                successor = member_no_successor(candidate, previous | bound_nos)
                if successor is None:
                    record(candidate, "REPEATED")
                    return None, "member_no_generator_not_advancing"
                candidate, derivation = successor, "successor"
            with self.ledger.tx() as conn:
                held = conn.execute("SELECT 1 FROM customers WHERE member_no = ?", (candidate,)).fetchone()
            if held:
                record(candidate, "LEDGER_HELD")
                continue
            try:
                status = bridge.probe(candidate)
            except BridgeError:
                record(candidate, "PROBE_FAILED")
                return None, "member_no_probe_failed"
            if status == "OCCUPIED":
                record(candidate, "OCCUPIED")
                continue
            with self.ledger.tx() as conn:
                conn.execute(
                    "UPDATE allocation_attempts SET candidate = ?, status = 'FREE_BOUND', generator_preview = ?, derivation = ? WHERE customer_gid = ? AND attempt_no = ?",
                    (candidate, preview, derivation, gid, attempt_no),
                )
                conn.execute("UPDATE customers SET member_no = ?, updated_at = ? WHERE customer_gid = ?", (candidate, ts(self.ledger.now()), gid))
                self.ledger.event(conn, "member_no_bound", customer_gid=gid)
            return candidate, None

    def _write(self, gid: str, payload: Mapping[str, Any], bridge: BridgeClient) -> dict[str, Any]:
        self._require_bridge(bridge)
        issue = autocount_text_issue(payload)  # defence in depth; admission already checked
        if issue:
            return self._review(gid, issue)
        with self.ledger.tx() as conn:
            if self._live_approval(conn, gid) is None and not self._policy_approval(conn, gid):
                return {"write": "not_started", "code": "write_approval_missing"}
        try:
            precheck = bridge.precheck(duplicate_phone_keys(payload.get("mobile_phone")), payload.get("email_address"))
        except BridgeError:
            return self._review(gid, "legacy_lookup_failed")
        flags = ("phone_member_no_hit", "mobile_phone_hit", "email_hit")
        if precheck.get("outcome") != "NO_CANDIDATE" or any(precheck.get(flag) is not False for flag in flags):
            code = {"CANDIDATE_FOUND": "legacy_candidate_found", "LOOKUP_FAILED": "legacy_lookup_failed"}.get(precheck.get("outcome"), "legacy_precheck_ambiguous")
            return self._review(gid, code)
        member_no, code = self._allocate(gid, bridge)
        if member_no is None:
            return self._review(gid, code or "member_no_allocation_failed")
        try:
            recheck = bridge.probe(member_no)
        except BridgeError:
            recheck = "PROBE_FAILED"
        if recheck != "FREE":
            return self._review(gid, "bound_member_no_recheck_failed")

        salt = secrets.token_hex(16)
        payload_text = canonical_payload(payload)
        digest = payload_digest(salt, payload_text)
        fence_id = f"fence-{uuid.uuid4().hex}"
        with self.ledger.tx() as conn:
            customer = self._customer(conn, gid)
            approval_id = self._live_approval(conn, gid)
            if customer["state"] != "ADMITTED" or customer["member_no"] != member_no or approval_id is None:
                raise DirectLocalError("fence_preconditions_changed")
            now = ts(self.ledger.now())
            conn.execute("UPDATE approvals SET consumed_at = ? WHERE approval_id = ?", (now, approval_id))
            conn.execute(
                "INSERT INTO fences(fence_id, customer_gid, member_no, approval_id, payload_digest, salt, owner_epoch, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'FENCED', ?, ?)",
                (fence_id, gid, member_no, approval_id, digest, salt, self.ledger.epoch, now, now),
            )
            self._set_customer(conn, gid, "FENCED", None)
            self.ledger.event(conn, "fence:fenced", customer_gid=gid, fence_id=fence_id)
        deadline = (self.ledger.now() + timedelta(seconds=self.writer_deadline_seconds)).replace(microsecond=0)
        with self.ledger.tx() as conn:
            fence = conn.execute("SELECT * FROM fences WHERE fence_id = ?", (fence_id,)).fetchone()
            self._fence_state(conn, fence, "DISPATCHED", None, deadline_at=ts(deadline))

        def on_started(pid: int, created: str | None) -> None:
            with self.ledger.tx() as conn:
                conn.execute("UPDATE fences SET child_pid = ?, child_created = ?, updated_at = ? WHERE fence_id = ? AND state = 'DISPATCHED'", (pid, created, ts(self.ledger.now()), fence_id))

        run = bridge.write_once(
            fence_id=fence_id, claim_directory=self.claims, deadline=deadline,
            stdin_value={"fence_id": fence_id, "member_no": member_no, "salt": salt, "payload_canonical": payload_text, "payload_digest": digest},
            wait_seconds=(deadline - self.ledger.now()).total_seconds() + self.writer_grace_seconds, on_started=on_started,
        )
        state, code = classify_write(run, claim_exists=(self.claims / f"{fence_id}.claim").exists())
        with self.ledger.tx() as conn:
            fence = conn.execute("SELECT * FROM fences WHERE fence_id = ?", (fence_id,)).fetchone()
            self._fence_state(conn, fence, state, code, exit_code=run.exit_code)
        return {"fence_state": state, "code": code, "member_no_bound": True, "member_ref": self._ref("m", member_no)}

    def reconcile(self, capture_path: Path, *, bridge: BridgeClient) -> dict[str, Any]:
        """Read-only check of one uncertain write with the same MemberNo."""

        self._require_bridge(bridge)
        self.recover()
        meta = self.ledger.meta()
        with self.ledger.tx() as conn:
            capture = self._consume_profile(conn, capture_path, meta)
            gid = capture.variables["id"]
            customer = self._customer(conn, gid)
            fence = conn.execute("SELECT * FROM fences WHERE customer_gid = ?", (gid,)).fetchone()
            if fence is None or fence["state"] != "WRITE_OUTCOME_UNCERTAIN":
                raise DirectLocalError("no_uncertain_write")
            decision = self._decide(conn, capture, customer, complete_required=True)
        self._discard(capture_path)
        if decision.outcome != "ADMIT" or payload_digest(fence["salt"], canonical_payload(decision.create_payload or {})) != fence["payload_digest"]:
            with self.ledger.tx() as conn:
                self._fence_state(conn, fence, "MANUAL_REVIEW", "reconcile_source_changed")
            return {"fence_state": "MANUAL_REVIEW", "code": "reconcile_source_changed"}
        try:
            check = bridge.reconcile(fence["member_no"], canonical_payload(decision.create_payload or {}))
        except BridgeError:
            return {"fence_state": "WRITE_OUTCOME_UNCERTAIN", "code": "reconciliation_lookup_uncertain"}
        found, match = check.get("found"), check.get("match")
        if not isinstance(found, bool) or not isinstance(match, bool):
            return {"fence_state": "WRITE_OUTCOME_UNCERTAIN", "code": "reconciliation_lookup_uncertain"}
        state, code = ("RECONCILED_VERIFIED", None) if found and match else ("MANUAL_REVIEW", "readback_mismatch_manual_review" if found else "confirmed_absent_manual_followup")
        with self.ledger.tx() as conn:
            self._fence_state(conn, fence, state, code)
        return {"fence_state": state, "code": code, "member_ref": self._ref("m", fence["member_no"])}

    def _inbox_summary(self) -> dict[str, Any]:
        files = sorted(self.inbox.glob("*.json"))
        now = utc_now().timestamp()
        oldest = max((now - path.stat().st_mtime for path in files), default=0)
        return {"files": len(files), "oldest_seconds": int(oldest)}

    def status(self) -> dict[str, Any]:
        conn = self.ledger.conn
        meta = self.ledger.meta()
        return {
            "mode": meta["mode"], "environment": meta["environment"], "autocount_book": meta["autocount_book"],
            "integrity": conn.execute("PRAGMA quick_check").fetchone()[0],
            "events_verified": self.ledger.verify_events(),
            "customers": {row[0]: row[1] for row in conn.execute("SELECT state, COUNT(*) FROM customers GROUP BY state")},
            "fences": {row[0]: row[1] for row in conn.execute("SELECT state, COUNT(*) FROM fences GROUP BY state")},
            "runs": [dict(row) for row in conn.execute("SELECT run_id, kind, state, cutover_at, page_count, historical_count, transition_count FROM runs ORDER BY started_at")],
            "inbox": self._inbox_summary(),
        }

    # -- attended operator helpers ---------------------------------------------
    def next_request(self) -> dict[str, Any]:
        """The exact connector call the attended session should make next.
        Read-only: the PreToolUse guard still enforces the pinned queries."""

        meta = self.ledger.meta()
        conn = self.ledger.conn
        run = conn.execute("SELECT * FROM runs WHERE state = 'CAPTURING'").fetchone()
        if run is not None:
            started = parse_ts(run["started_at"])
            pages: dict[str | None, Capture] = {}
            for _, capture in self._inbox_captures(conn, meta):
                if not self._page_of(run, capture) or capture.captured_at < started:
                    continue
                if conn.execute("SELECT 1 FROM captures WHERE capture_sha256 = ? OR tool_use_id = ?", (capture.capture_sha256, capture.tool_use_id)).fetchone():
                    continue
                pages.setdefault(capture.variables.get("after"), capture)
            after: str | None = None
            for _ in range(len(pages) + 1):
                capture = pages.get(after)
                if capture is None:
                    variables: dict[str, Any] = {"first": PAGE_SIZE, "after": after}
                    if run["search"] is not None:
                        variables["search"] = run["search"]
                    tool_input: dict[str, Any] = {"query": RECENT_QUERY if run["search"] else BASELINE_QUERY, "variables": variables, "first": PAGE_SIZE}
                    if after is not None:
                        tool_input["after"] = after
                    return {"status": "request", "purpose": f"{run['kind'].lower()}_page", "tool_name": CONNECTOR_TOOL, "tool_input": tool_input}
                info = capture.data["customers"]["pageInfo"]
                if not info["hasNextPage"]:
                    return {"status": "run_pages_complete", "next": "ingest-run"}
                after = info["endCursor"]
            raise DirectLocalError("run_page_chain_invalid")
        row = conn.execute(
            "SELECT customer_gid, state FROM customers WHERE state IN ('DISCOVERED', 'WAITING', 'ADMITTED') ORDER BY updated_at, customer_gid LIMIT 1"
        ).fetchone()
        if row is None:
            return {"status": "idle"}
        return {
            "status": "request", "purpose": "profile", "customer_ref": self._ref("c", row["customer_gid"]), "customer_state": row["state"],
            "tool_name": CONNECTOR_TOOL, "tool_input": {"query": PROFILE_QUERY, "variables": {"id": row["customer_gid"]}},
        }

    def purge_inbox(self) -> dict[str, int]:
        """Bounded retention for transient captures (profile captures carry
        PII). Removes captures already consumed, rejected captures (for
        example disconnected or expired-authentication responses), profile
        captures past the freshness window, baseline pages no open run can
        use, and stale hook temporaries. Unconsumed pages of the open run are
        kept. Only counts are reported."""

        meta = self.ledger.meta()
        conn = self.ledger.conn
        run = conn.execute("SELECT started_at FROM runs WHERE state = 'CAPTURING'").fetchone()
        started = parse_ts(run["started_at"]) if run else None
        now = self.ledger.now()
        counts = {"consumed": 0, "rejected": 0, "expired_profile": 0, "orphan_page": 0, "stale_temporary": 0, "kept": 0, "delete_failed": 0}

        def remove(path: Path, reason: str) -> None:
            if self._discard(path):
                counts[reason] += 1
            else:
                counts["delete_failed"] += 1

        for path in sorted(self.inbox.glob(".*.tmp")):
            if utc_now().timestamp() - path.stat().st_mtime > 3600:
                remove(path, "stale_temporary")
        for path in sorted(self.inbox.glob("*.json")):
            try:
                capture = parse_capture(path.read_bytes(), mode=meta["mode"], store_gid=meta["store_gid"], shop_domain=meta["shop_domain"])
            except (CaptureRejected, OSError):
                remove(path, "rejected")
                continue
            if conn.execute("SELECT 1 FROM captures WHERE capture_sha256 = ? OR tool_use_id = ?", (capture.capture_sha256, capture.tool_use_id)).fetchone():
                remove(path, "consumed")
            elif capture.kind == "profile" and (now - capture.captured_at).total_seconds() > CAPTURE_MAX_AGE_SECONDS:
                remove(path, "expired_profile")
            elif capture.kind != "profile" and (started is None or capture.captured_at < started):
                remove(path, "orphan_page")
            else:
                counts["kept"] += 1
        with self.ledger.tx() as tx:
            self.ledger.event(tx, "inbox_purged", code=f"removed:{sum(v for k, v in counts.items() if k not in ('kept', 'delete_failed'))}")
        return counts

    def pending(self) -> dict[str, Any]:
        """Customers awaiting a profile read or an approval. GIDs are local
        identifiers for the attended operator; never paste them publicly."""

        rows = self.ledger.conn.execute(
            "SELECT customer_gid, state, decision_code, check_count FROM customers WHERE state IN ('DISCOVERED', 'WAITING', 'ADMITTED') ORDER BY updated_at, customer_gid"
        ).fetchall()
        return {"customers": [{"customer_ref": self._ref("c", r["customer_gid"]), "gid": r["customer_gid"], "state": r["state"], "code": r["decision_code"], "checks": r["check_count"]} for r in rows]}

    def uat_report(self) -> dict[str, Any]:
        """PII-free evidence for UAT: keyed references, states and codes only."""

        conn = self.ledger.conn
        def since(start: str | None, end: str | None) -> int | None:
            return None if start is None or end is None else int((parse_ts(end) - parse_ts(start)).total_seconds())

        fences = []
        for r in conn.execute(
            "SELECT f.fence_id, f.customer_gid, f.member_no, f.state, f.code, f.created_at, f.updated_at, c.registration_at, c.discovered_at "
            "FROM fences f JOIN customers c ON c.customer_gid = f.customer_gid ORDER BY f.created_at"
        ):
            pos = conn.execute("SELECT outlet, MIN(observed_at) AS first FROM pos_observations WHERE fence_id = ? GROUP BY outlet ORDER BY outlet", (r["fence_id"],)).fetchall()
            fences.append({
                "customer_ref": self._ref("c", r["customer_gid"]), "member_ref": self._ref("m", r["member_no"]), "state": r["state"], "code": r["code"],
                "latency_seconds": {
                    "registration_to_discovered": since(r["registration_at"], r["discovered_at"]),
                    "registration_to_fenced": since(r["registration_at"], r["created_at"]),
                    "registration_to_outcome": since(r["registration_at"], r["updated_at"]),
                    "registration_to_pos_visible": {row["outlet"]: since(r["registration_at"], row["first"]) for row in pos},
                },
            })
        reviews = [
            {"customer_ref": self._ref("c", r["customer_gid"]), "code": r["decision_code"]}
            for r in conn.execute("SELECT customer_gid, decision_code FROM customers WHERE state = 'MANUAL_REVIEW' ORDER BY updated_at")
        ]
        # Counts only: which candidates were the generator's own preview and
        # which were derived successors. A NULL derivation is reported, never
        # rewritten (G2 O12): "no_candidate" for GENERATOR_FAILED/INVALID,
        # which never had a candidate in any schema; "unrecorded_pre_v4" for
        # any other status, which only a row recorded before schema v4 has.
        derivations = {row[0]: row[1] for row in conn.execute(
            "SELECT CASE WHEN derivation IS NOT NULL THEN derivation "
            "WHEN status IN ('GENERATOR_FAILED', 'GENERATOR_INVALID') THEN 'no_candidate' ELSE 'unrecorded_pre_v4' END AS bucket, COUNT(*) "
            "FROM allocation_attempts WHERE status IS NOT NULL GROUP BY bucket ORDER BY bucket")}
        return {
            **self.status(), "fence_detail": fences, "manual_review": reviews,
            "allocation_attempts": conn.execute("SELECT COUNT(*) FROM allocation_attempts").fetchone()[0], "allocation_derivations": derivations,
        }

    def book_check(self, bridge: BridgeClient) -> dict[str, Any]:
        """Read-only AutoCount qualification (gate E5): the bridge logs in to
        the bound book, proves its identity and the outbound queues. No member
        is read, numbered or written."""

        if self.ledger.meta()["mode"] != "connector":
            raise DirectLocalError("book_check_requires_connector_ledger")
        self._require_bridge(bridge)
        result = bridge.book_check()
        if result.get("book") != self.ledger.meta()["autocount_book"] or result.get("identity_verified") is not True or result.get("outbound_quiet") is not True:
            raise DirectLocalError("book_check_failed")
        return {"book": result["book"], "identity_verified": True, "outbound_quiet": True}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(text: str) -> None:
    # One bounded JSON line per command: codes, counts, states and keyed
    # references. Only ``pending`` and ``next-request`` name Shopify GIDs
    # (local identifiers the attended operator needs); none carries PII.
    sys.stdout.write(text + "\n")


def _bridge_for(processor: Processor, bridge_script: str | None) -> BridgeClient:
    command = default_bridge_command(Path(bridge_script) if bridge_script else None)
    meta = processor.ledger.meta()
    if meta["mode"] == "connector":
        return BridgeClient(command, autocount_target=REAL_BRIDGE_TARGET, autocount_book=meta["autocount_book"])
    # Always the real target: a synthetic ledger refuses it before any spawn.
    return BridgeClient(command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python directlocal/xb_directlocal.py", description="DirectLocal #155 member-sync core")
    parser.add_argument("--state-root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare-state-root")
    init = commands.add_parser("init-ledger")
    init.add_argument("--mode", required=True)
    init.add_argument("--environment")
    init.add_argument("--store-gid", required=True)
    init.add_argument("--shop-domain", required=True)
    inspect = commands.add_parser("inspect-identity")
    inspect.add_argument("--capture", required=True)
    commands.add_parser("begin-discovery").add_argument("--recent", action="store_true")
    for name in ("begin-baseline", "ingest-run", "recover", "status", "next-request", "purge-inbox", "pending", "uat-report", "revoke-policy", "migrate-ledger"):
        commands.add_parser(name)
    for name in ("book-check", "pos-sync-status"):
        commands.add_parser(name).add_argument("--bridge-script")
    policy = commands.add_parser("activate-policy")
    policy.add_argument("--confirm-book", required=True)
    policy.add_argument("--max-creates", type=int, required=True)
    policy.add_argument("--ttl-hours", type=int, required=True)
    pos = commands.add_parser("record-pos-visible")
    pos.add_argument("--member-ref", required=True)
    pos.add_argument("--outlet", required=True)
    pos.add_argument("--observed-at", required=True)
    abandon = commands.add_parser("abandon-run")
    abandon.add_argument("--run-id", required=True)
    abandon.add_argument("--confirm-suffix", required=True)
    release = commands.add_parser("release-writer-hold")
    release.add_argument("--fence-id", required=True)
    release.add_argument("--confirm-suffix", required=True)
    approve = commands.add_parser("approve-write")
    approve.add_argument("--gid", required=True)
    approve.add_argument("--confirm-suffix", required=True)
    approve.add_argument("--ttl-seconds", type=int, default=900)
    for name in ("process", "reconcile"):
        sub = commands.add_parser(name)
        sub.add_argument("--capture", required=True)
        sub.add_argument("--bridge-script")
        if name == "process":
            sub.add_argument("--enable-autocount-write", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "inspect-identity":  # read-only file check; no ledger is opened
        try:
            _emit(json.dumps(inspect_identity_capture(Path(args.capture).read_bytes()), sort_keys=True))
            return 0
        except (DirectLocalError, OSError) as exc:
            _emit(json.dumps({"status": "refused", "code": getattr(exc, "code", "capture_unreadable")}))
            return 2
    if args.command == "prepare-state-root":  # creates a new private root; opens no ledger
        try:
            _emit(json.dumps(prepare_state_root(args.state_root), sort_keys=True))
            return 0
        except (DirectLocalError, OSError) as exc:
            _emit(json.dumps({"status": "refused", "code": getattr(exc, "code", "state_root_prepare_failed")}))
            return 2
    try:
        ledger = Ledger(validate_state_root(args.state_root), allow_migration=args.command == "migrate-ledger")
    except DirectLocalError as exc:
        _emit(json.dumps({"status": "refused", "code": exc.code}))
        return 2
    try:
        processor = Processor(ledger)
        if args.command == "init-ledger":
            result = processor.init_ledger(mode=args.mode, store_gid=args.store_gid, shop_domain=args.shop_domain, environment=args.environment)
        else:
            processor.check_custody()  # every command on a connector ledger
            simple = {
                "begin-baseline": lambda: processor.begin_run("BASELINE"),
                "begin-discovery": lambda: processor.begin_run("DISCOVERY", recent=args.recent),
                "ingest-run": processor.ingest_run,
                "recover": processor.recover,
                "status": processor.status,
                "next-request": processor.next_request,
                "purge-inbox": processor.purge_inbox,
                "pending": processor.pending,
                "uat-report": processor.uat_report,
                "revoke-policy": processor.revoke_policy,
                "migrate-ledger": ledger.migrate_schema,
            }
            if args.command in simple:
                result = simple[args.command]()
            elif args.command == "abandon-run":
                result = processor.abandon_run(args.run_id, confirm_suffix=args.confirm_suffix)
            elif args.command == "release-writer-hold":
                result = processor.release_writer_hold(args.fence_id, confirm_suffix=args.confirm_suffix)
            elif args.command == "approve-write":
                result = processor.approve_write(args.gid, confirm_suffix=args.confirm_suffix, ttl_seconds=args.ttl_seconds)
            elif args.command == "book-check":
                result = processor.book_check(_bridge_for(processor, args.bridge_script))
            elif args.command == "pos-sync-status":
                result = processor.pos_sync_status(_bridge_for(processor, args.bridge_script))
            elif args.command == "activate-policy":
                result = processor.activate_policy(confirm_book=args.confirm_book, max_creates=args.max_creates, ttl_hours=args.ttl_hours)
            elif args.command == "record-pos-visible":
                result = processor.record_pos_visible(member_ref=args.member_ref, outlet=args.outlet, observed_at=args.observed_at)
            elif args.command == "process":
                result = processor.process(Path(args.capture), bridge=_bridge_for(processor, args.bridge_script), enable_write=args.enable_autocount_write)
            else:
                result = processor.reconcile(Path(args.capture), bridge=_bridge_for(processor, args.bridge_script))
        _emit(json.dumps(result, sort_keys=True, default=str))
        return 0
    except DirectLocalHold as exc:
        _emit(json.dumps({"status": "hold", "code": exc.code}))
        return HOLD_EXIT_CODE
    except (DirectLocalError, sqlite3.IntegrityError) as exc:
        code = exc.code if isinstance(exc, DirectLocalError) else "ledger_constraint_refused"
        _emit(json.dumps({"status": "refused", "code": code}))
        return 2
    finally:
        ledger.close()


if __name__ == "__main__":
    sys.exit(main())
