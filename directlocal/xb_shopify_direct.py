"""#155 E8: bounded read-only Shopify Admin GraphQL client for the DirectLocal
poller, plus the direct-HTTP capture writer.

Scope
-----
* Outbound HTTPS POST only, to exactly ``https://<store>.myshopify.com``: the
  store's GraphQL endpoint and, for the client-credentials grant, its token
  endpoint. No listener, no redirect is followed, no environment proxy is
  used, TLS verification uses the default system trust store.
* Only the Core's pinned queries (``xb_directlocal.PINNED_QUERIES``) and one
  session check (store identity and the token's access scopes) are sent. No
  GraphQL write operation exists in this module.
* A response is accepted only as ``{"data": {...}}`` with optional
  ``extensions``. ``errors`` (also on HTTP 200, also next to partial data),
  any other top-level key, non-JSON, oversized or non-UTF-8 bodies fail
  closed. ``extensions.cost`` is read only as numeric throttle metadata and
  never reaches a capture.
* Errors carry a fixed code, a retryable flag and numbers only: never a
  token, header, body, cursor, GID or provider message.

Authentication (no secret is ever written in plaintext)
-------------------------------------------------------
* ``client_credentials``: an app created in the Shopify Dev Dashboard; the
  app and the store must belong to the same Shopify organisation.
  ``POST /admin/oauth/access_token`` with ``grant_type=client_credentials``
  returns a token that expires after about 24 hours (shopify.dev, client
  credentials grant, read 2026-10-17). It is refreshed 5 minutes before
  expiry and once after a 401. Across runs the token is reused through
  ``TokenCache``: the access token only (never the client secret), encrypted
  with Windows DPAPI for the current user and bound by DPAPI entropy to the
  store and client ID, in the private state root. Without the cache every run
  exchanges once (bounded manual runs only).
* ``admin_api_access_token``: the static Admin API token of an already
  existing legacy custom app created in the Shopify admin (new ones cannot be
  created since 1 January 2026). It cannot be refreshed; a 401 fails closed.
* Either way the granted scopes must be exactly ``read_customers``; a write
  scope (or any other scope) refuses before any customer is read.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import xb_directlocal as dl
from xb_member_gateway.shopify_admission import ShopifyThrottleStatus, parse_retry_after_seconds, parse_throttle_status

MAX_RESPONSE_BYTES = 4_194_304
REQUIRED_SCOPES = frozenset({"read_customers"})
TOKEN_REFRESH_MARGIN_SECONDS = 300
TOKEN_MIN_LIFETIME_SECONDS = 600
TOKEN_MAX_LIFETIME_SECONDS = 172_800
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{16,512}$")  # printable ASCII, no whitespace
_SCOPE_RE = re.compile(r"^[a-z_]{1,64}$")
# Bounded retries per client (one poller invocation).
RETRY_MAX_ATTEMPTS_PER_REQUEST = 4
RETRY_MAX_TOTAL = 12
RETRY_MAX_TOTAL_WAIT_SECONDS = 90
RETRY_MAX_SINGLE_WAIT_SECONDS = 30
PACING_MAX_WAIT_SECONDS = 10

# Store identity and the token's own access scopes; no customer object.
SESSION_QUERY = """query XbDirectLocalSessionCheck {
  shop { id myshopifyDomain }
  currentAppInstallation { accessScopes { handle } }
}"""

# Codes after which only an operator can help (credential, app or scope
# setup); the poller backs off longer.
OPERATOR_ACTION_CODES = frozenset({
    "shopify_auth_rejected", "shopify_access_denied", "shopify_token_scope_not_minimal",
    "shopify_client_credentials_rejected", "shopify_token_missing", "shopify_store_identity_mismatch",
    "shopify_shop_unavailable", "shopify_not_found",
})


class ShopifyDirectError(dl.DirectLocalError):
    """Bounded read failure: code, retryable flag and numbers only."""

    def __init__(self, code: str, *, retryable: bool = False, retry_after_seconds: int | None = None, throttle: ShopifyThrottleStatus | None = None):
        super().__init__(code)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.throttle = throttle

    def __repr__(self) -> str:
        return f"ShopifyDirectError(code={self.code!r}, retryable={self.retryable!r})"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # lower-case names
    body: bytes

    def __repr__(self) -> str:  # never render the body
        return f"HttpResponse(status={self.status!r})"


class Transport(Protocol):
    def __call__(self, url: str, body: bytes, headers: Mapping[str, str], timeout: int) -> HttpResponse: ...


def store_url(shop_domain: str, path: str) -> str:
    """Fixed HTTPS URL on the store host. Only ``*.myshopify.com`` (real) and
    ``*.synthetic.invalid`` (offline tests, never resolvable) are accepted."""

    if not isinstance(shop_domain, str) or not (dl.SHOP_DOMAIN_RE.fullmatch(shop_domain) or dl.SYNTHETIC_DOMAIN_RE.fullmatch(shop_domain)):
        raise ShopifyDirectError("shopify_host_not_allowed")
    return f"https://{shop_domain}{path}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        raise ShopifyDirectError("shopify_redirect_refused")


class UrllibTransport:
    """The only network path. Refuses any URL that is not HTTPS to a
    ``*.myshopify.com`` host before a socket is opened."""

    def __init__(self) -> None:
        context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), _NoRedirect(),
        )

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str], timeout: int) -> HttpResponse:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.port is not None or parts.username or not dl.SHOP_DOMAIN_RE.fullmatch(parts.hostname or ""):
            raise ShopifyDirectError("shopify_host_not_allowed")
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return HttpResponse(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as exc:
            headers_in = {k.lower(): v for k, v in (exc.headers.items() if exc.headers is not None else [])}
            try:
                payload = exc.read(MAX_RESPONSE_BYTES + 1)
            except OSError:
                payload = b""
            return HttpResponse(exc.code, headers_in, payload)
        except ShopifyDirectError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError):
            raise ShopifyDirectError("shopify_transport_failed", retryable=True) from None


def http_error(response: HttpResponse) -> ShopifyDirectError:
    """Fixed code for a non-200 GraphQL response."""

    status = response.status
    if status == 401:
        return ShopifyDirectError("shopify_auth_rejected")
    if status == 403:
        return ShopifyDirectError("shopify_access_denied")
    if status == 404:
        return ShopifyDirectError("shopify_not_found")
    if status in (402, 423):
        return ShopifyDirectError("shopify_shop_unavailable")
    retry_after = parse_retry_after_seconds(response.headers.get("retry-after"))
    if status == 429:
        return ShopifyDirectError("shopify_http_throttled", retryable=True, retry_after_seconds=retry_after)
    if status in (500, 502, 503, 504):
        return ShopifyDirectError("shopify_http_server_error", retryable=True, retry_after_seconds=retry_after if status == 503 else None)
    return ShopifyDirectError("shopify_http_error")


def decode_graphql(response: HttpResponse) -> tuple[dict[str, Any], ShopifyThrottleStatus | None]:
    """``(data, throttle)`` of one HTTP 200 GraphQL response, else raise."""

    if len(response.body) > MAX_RESPONSE_BYTES:
        raise ShopifyDirectError("shopify_response_too_large")
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ShopifyDirectError("shopify_response_invalid") from None
    if not isinstance(value, dict):
        raise ShopifyDirectError("shopify_response_invalid")
    throttle = parse_throttle_status(value)
    if "errors" in value:
        errors = value["errors"]
        codes = set()
        if isinstance(errors, list):
            for item in errors:
                extensions = item.get("extensions") if isinstance(item, dict) else None
                if isinstance(extensions, dict) and isinstance(extensions.get("code"), str):
                    codes.add(extensions["code"])
        if "THROTTLED" in codes:
            raise ShopifyDirectError("shopify_graphql_throttled", retryable=True, throttle=throttle)
        if "ACCESS_DENIED" in codes:
            raise ShopifyDirectError("shopify_access_denied")
        raise ShopifyDirectError("shopify_graphql_error")  # including partial data
    if not set(value) <= {"data", "extensions"} or not isinstance(value.get("data"), dict):
        raise ShopifyDirectError("shopify_response_shape_invalid")
    return value["data"], throttle


# --------------------------------------------------------------------------
# Token providers
# --------------------------------------------------------------------------

def _scopes(text: Any) -> frozenset[str]:
    if not isinstance(text, str):
        raise ShopifyDirectError("shopify_token_response_invalid")
    items = [item.strip() for item in text.split(",") if item.strip()]
    if not items or any(not _SCOPE_RE.fullmatch(item) for item in items):
        raise ShopifyDirectError("shopify_token_response_invalid")
    return frozenset(items)


def require_minimal_scopes(scopes: frozenset[str]) -> None:
    if scopes != REQUIRED_SCOPES:
        raise ShopifyDirectError("shopify_token_scope_not_minimal")


def _secret(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise ShopifyDirectError(code)
    return value


class StaticTokenProvider:
    """Admin API access token of an existing custom app (cannot refresh)."""

    method = "admin_api_access_token"

    def __init__(self, token: Any):
        self._token = _secret(token, "shopify_token_missing")

    def __repr__(self) -> str:
        return "StaticTokenProvider()"

    def token(self) -> str:
        return self._token

    def invalidate(self) -> bool:
        return False


TOKEN_CACHE_SCHEMA = "xb.directlocal.shopify_token_cache.v1"
TOKEN_CACHE_MAX_BYTES = 16_384


class DpapiProtector:
    """Windows DPAPI, current-user scope, no UI. Only this Windows user on
    this machine can decrypt; the entropy binds a blob to one store and
    client ID. ``protect``/``unprotect`` raise ``ShopifyDirectError``."""

    name = "dpapi_current_user"

    def __init__(self) -> None:
        if os.name != "nt":
            raise ShopifyDirectError("token_cache_unavailable")

    @staticmethod
    def _call(function_name: str, data: bytes, entropy: bytes) -> bytes:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = getattr(crypt32, function_name)
        function.argtypes = (ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob))
        function.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        source = ctypes.create_string_buffer(data, len(data))
        salt = ctypes.create_string_buffer(entropy, len(entropy))
        blob_in = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_char)))
        blob_entropy = Blob(len(entropy), ctypes.cast(salt, ctypes.POINTER(ctypes.c_char)))
        blob_out = Blob()
        if not function(ctypes.byref(blob_in), None, ctypes.byref(blob_entropy), None, None, 0x1, ctypes.byref(blob_out)):  # CRYPTPROTECT_UI_FORBIDDEN
            raise ShopifyDirectError("token_cache_unreadable" if function_name == "CryptUnprotectData" else "token_cache_unsaved")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)

    def protect(self, data: bytes, entropy: bytes) -> bytes:
        return self._call("CryptProtectData", data, entropy)

    def unprotect(self, blob: bytes, entropy: bytes) -> bytes:
        return self._call("CryptUnprotectData", blob, entropy)


class TokenCache:
    """Cross-run reuse of one client-credentials access token.

    The file holds only a protected blob of ``{schema, shop_domain,
    expires_at, scope, access_token}``. Anything unreadable, foreign or
    expiring is discarded (with a visible ``note``) and a fresh token is
    exchanged; it is never trusted. The client secret is never cached."""

    def __init__(self, path: Path, protector: Any, *, shop_domain: str, client_id: str):
        self.path = Path(path)
        self.protector = protector
        self.shop_domain = shop_domain
        # Entropy: a blob made for another store or app never decrypts here.
        self._entropy = hashlib.sha256("\n".join((TOKEN_CACHE_SCHEMA, shop_domain, client_id)).encode("utf-8")).digest()
        self.note: str | None = None

    def __repr__(self) -> str:
        return "TokenCache()"

    def load(self, now: datetime) -> tuple[str, datetime] | None:
        try:
            if not self.path.exists():
                return None
            raw = self.path.read_bytes()
        except OSError:
            self.note = "token_cache_unreadable"
            return None
        try:
            if len(raw) > TOKEN_CACHE_MAX_BYTES:
                raise ShopifyDirectError("token_cache_unreadable")
            value = json.loads(self.protector.unprotect(raw, self._entropy).decode("utf-8"))
            if not isinstance(value, dict) or set(value) != {"schema", "shop_domain", "expires_at", "scope", "access_token"}:
                raise ShopifyDirectError("token_cache_unreadable")
            if value["schema"] != TOKEN_CACHE_SCHEMA or value["shop_domain"] != self.shop_domain:
                raise ShopifyDirectError("token_cache_unreadable")
            expires_at = dl.parse_created_at(value["expires_at"])
            token = _secret(value["access_token"], "token_cache_unreadable")
            require_minimal_scopes(_scopes(value["scope"]))
            if expires_at is None:
                raise ShopifyDirectError("token_cache_unreadable")
        except (ShopifyDirectError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            self.note = "token_cache_unreadable"
            self.clear()
            return None
        if now >= expires_at - timedelta(seconds=TOKEN_REFRESH_MARGIN_SECONDS):
            self.note = "token_cache_expired"
            self.clear()
            return None
        return token, expires_at

    def save(self, token: str, expires_at: datetime, scopes: frozenset[str]) -> None:
        value = {
            "schema": TOKEN_CACHE_SCHEMA, "shop_domain": self.shop_domain, "scope": ",".join(sorted(scopes)), "access_token": token,
            "expires_at": expires_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            blob = self.protector.protect(json.dumps(value, sort_keys=True).encode("utf-8"), self._entropy)
            with open(temporary, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except (ShopifyDirectError, OSError):
            # Visible and narrow: this run keeps its in-memory token and the
            # next run exchanges again. Never a plaintext fallback.
            self.note = "token_cache_unsaved"
            try:
                temporary.unlink()
            except OSError:
                pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            self.note = "token_cache_unclearable"


class ClientCredentialsTokenProvider:
    """Client-credentials grant; in memory, plus the optional ``TokenCache``."""

    method = "client_credentials"

    def __init__(self, shop_domain: str, client_id: Any, client_secret: Any, *, transport: Transport, clock: Callable[[], datetime], timeout: int = 30, cache_protector: Any = None, cache_path: Path | None = None):
        self._url = store_url(shop_domain, "/admin/oauth/access_token")
        self._client_id = _secret(client_id, "shopify_token_missing")
        self._client_secret = _secret(client_secret, "shopify_token_missing")
        self._transport = transport
        self._clock = clock
        self._timeout = timeout
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self.cache = None if cache_protector is None else TokenCache(cache_path, cache_protector, shop_domain=shop_domain, client_id=self._client_id)
        self.requests = 0
        self.cache_hits = 0

    def __repr__(self) -> str:
        return "ClientCredentialsTokenProvider()"

    def token(self) -> str:
        now = self._clock()
        if self._token is not None and self._expires_at is not None and now < self._expires_at - timedelta(seconds=TOKEN_REFRESH_MARGIN_SECONDS):
            return self._token
        self._token = self._expires_at = None
        cached = self.cache.load(now) if self.cache is not None else None
        if cached is not None:
            self._token, self._expires_at = cached
            self.cache_hits += 1
            return self._token
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._client_secret}).encode("ascii")
        self.requests += 1
        response = self._transport(self._url, body, {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, self._timeout)
        if response.status in (400, 401, 403):
            raise ShopifyDirectError("shopify_client_credentials_rejected")
        if response.status != 200:
            error = http_error(response)
            raise ShopifyDirectError("shopify_token_request_failed", retryable=error.retryable, retry_after_seconds=error.retry_after_seconds)
        if len(response.body) > 65_536:
            raise ShopifyDirectError("shopify_token_response_invalid")
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise ShopifyDirectError("shopify_token_response_invalid") from None
        if not isinstance(value, dict):
            raise ShopifyDirectError("shopify_token_response_invalid")
        token = _secret(value.get("access_token"), "shopify_token_response_invalid")
        expires_in = value.get("expires_in")
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) or not TOKEN_MIN_LIFETIME_SECONDS <= expires_in <= TOKEN_MAX_LIFETIME_SECONDS:
            raise ShopifyDirectError("shopify_token_response_invalid")
        scopes = _scopes(value.get("scope"))
        require_minimal_scopes(scopes)
        self._token, self._expires_at = token, now + timedelta(seconds=expires_in)
        if self.cache is not None:
            self.cache.save(token, self._expires_at, scopes)
        return token

    def invalidate(self) -> bool:
        """After a 401: forget the token in memory and in the cache."""

        self._token = self._expires_at = None
        if self.cache is not None:
            self.cache.clear()
        return True


# --------------------------------------------------------------------------
# GraphQL client
# --------------------------------------------------------------------------

@dataclass
class ClientStats:
    requests: int = 0
    retries: int = 0
    wait_seconds: int = 0
    min_available: float | None = None
    auth_refreshes: int = 0


class ShopifyDirectClient:
    """Read-only Admin GraphQL client for one poller invocation."""

    def __init__(self, *, shop_domain: str, api_version: str, token_provider: Any, transport: Transport, sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic, deadline: float | None = None, timeout: int = 30):
        if not isinstance(api_version, str) or not dl.API_VERSION_RE.fullmatch(api_version):
            raise ShopifyDirectError("shopify_api_version_invalid")
        self.shop_domain = shop_domain
        self.api_version = api_version
        self._url = store_url(shop_domain, f"/admin/api/{api_version}/graphql.json")
        self._tokens = token_provider
        self._transport = transport
        self._sleep = sleep
        self._monotonic = monotonic
        self._deadline = deadline
        self._timeout = timeout
        self._throttle: ShopifyThrottleStatus | None = None
        self.stats = ClientStats()

    def __repr__(self) -> str:
        return "ShopifyDirectClient()"

    def _wait(self, seconds: float) -> None:
        seconds = max(0, math.ceil(seconds))
        if seconds == 0:
            return
        if self.stats.wait_seconds + seconds > RETRY_MAX_TOTAL_WAIT_SECONDS:
            raise ShopifyDirectError("shopify_retry_budget_exhausted")
        if self._deadline is not None and self._monotonic() + seconds > self._deadline:
            raise ShopifyDirectError("poller_time_budget_exceeded")
        self.stats.wait_seconds += seconds
        self._sleep(seconds)

    def _pace(self) -> None:
        """Wait for the leaky bucket to hold the last query's cost again."""

        t = self._throttle
        if t is None or None in (t.requested_cost, t.currently_available, t.restore_rate) or not t.restore_rate:
            return
        deficit = t.requested_cost - t.currently_available
        if deficit > 0:
            self._wait(min(deficit / t.restore_rate, PACING_MAX_WAIT_SECONDS))

    @staticmethod
    def _retry_wait(error: ShopifyDirectError, attempt: int) -> int:
        throttle = error.throttle
        if throttle is not None and throttle.requested_cost is not None and throttle.maximum_available is not None and throttle.requested_cost > throttle.maximum_available:
            raise ShopifyDirectError("shopify_query_cost_exceeds_bucket")
        if error.retry_after_seconds is not None:
            if error.retry_after_seconds > RETRY_MAX_SINGLE_WAIT_SECONDS:
                raise ShopifyDirectError("shopify_retry_after_exceeds_bound")
            return max(error.retry_after_seconds, 1)
        if throttle is not None and None not in (throttle.requested_cost, throttle.currently_available, throttle.restore_rate) and throttle.restore_rate > 0:
            deficit = throttle.requested_cost - throttle.currently_available
            return min(max(math.ceil(deficit / throttle.restore_rate), 1), RETRY_MAX_SINGLE_WAIT_SECONDS)
        return min(2 ** (attempt - 1), RETRY_MAX_SINGLE_WAIT_SECONDS)

    def _query_allowed(self, query: str) -> bool:
        """The poller's allowlist: the Core's pinned queries and the session
        check. A subclass with its own pinned set overrides this."""

        return query == SESSION_QUERY or dl.PINNED_QUERIES.get(dl._sha256(query)) is not None

    def _retry_allowed(self, query: str, error: ShopifyDirectError) -> bool:
        """Every pinned read may be retried. A subclass sending a write
        overrides this so an uncertain write is never repeated."""

        return True

    def execute(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        """``data`` of one pinned query (or the session check), with bounded
        retries for throttling, 5xx and transport failures, and one token
        refresh after a 401 when the provider can refresh."""

        if not self._query_allowed(query):
            raise ShopifyDirectError("shopify_query_not_pinned")
        body = json.dumps({"query": query, "variables": dict(variables)}, separators=(",", ":")).encode("utf-8")
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            if self._deadline is not None and self._monotonic() > self._deadline:
                raise ShopifyDirectError("poller_time_budget_exceeded")
            self._pace()
            try:
                headers = {"Content-Type": "application/json", "Accept": "application/json", "X-Shopify-Access-Token": self._tokens.token()}
                self.stats.requests += 1
                response = self._transport(self._url, body, headers, self._timeout)
                if response.status != 200:
                    raise http_error(response)
                data, throttle = decode_graphql(response)
            except ShopifyDirectError as exc:
                if exc.code == "shopify_auth_rejected" and not refreshed and self._tokens.invalidate():
                    refreshed = True
                    self.stats.auth_refreshes += 1
                    continue
                if exc.throttle is not None:
                    self._throttle = exc.throttle
                if not exc.retryable or not self._retry_allowed(query, exc):
                    raise
                if attempt >= RETRY_MAX_ATTEMPTS_PER_REQUEST or self.stats.retries >= RETRY_MAX_TOTAL:
                    raise ShopifyDirectError("shopify_retry_budget_exhausted") from None
                wait = self._retry_wait(exc, attempt)
                self.stats.retries += 1
                self._throttle = None  # the retry wait already covers the bucket
                self._wait(wait)
                continue
            self._throttle = throttle
            if throttle is not None and throttle.currently_available is not None:
                current = self.stats.min_available
                self.stats.min_available = throttle.currently_available if current is None else min(current, throttle.currently_available)
            return data

    def verify_session(self, *, store_gid: str) -> None:
        """Store identity equals the ledger's store; scopes are exactly
        ``read_customers``. Runs before any customer read."""

        data = self.execute(SESSION_QUERY, {})
        if set(data) != {"shop", "currentAppInstallation"} or data.get("shop") != {"id": store_gid, "myshopifyDomain": self.shop_domain}:
            raise ShopifyDirectError("shopify_store_identity_mismatch")
        installation = data["currentAppInstallation"]
        scopes = installation.get("accessScopes") if isinstance(installation, dict) else None
        if not isinstance(scopes, list) or any(not isinstance(item, dict) or set(item) != {"handle"} or not isinstance(item["handle"], str) for item in scopes):
            raise ShopifyDirectError("shopify_response_shape_invalid")
        handles = [item["handle"] for item in scopes]
        if len(set(handles)) != len(handles):
            raise ShopifyDirectError("shopify_response_shape_invalid")
        require_minimal_scopes(frozenset(handles))


# --------------------------------------------------------------------------
# Direct capture writer
# --------------------------------------------------------------------------

def write_capture(inbox: Path, *, query: str, variables: Mapping[str, Any], data: Mapping[str, Any], shop_domain: str, api_version: str, captured_at: datetime) -> Path:
    """Write one direct-HTTP capture into the Core inbox, exclusively and
    durably (fsync, then a no-overwrite link). A crash leaves at most a
    ``.xbdh-*.tmp`` file, which ``purge-inbox`` removes and no reader opens."""

    request_id = dl.DIRECT_REQUEST_ID_PREFIX + uuid.uuid4().hex
    record = {
        "schema_version": dl.DIRECT_CAPTURE_SCHEMA, "provenance": dl.DIRECT_CAPTURE_PROVENANCE,
        "captured_at": captured_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "request_id": request_id, "endpoint": {"host": shop_domain, "api_version": api_version},
        "request": {"query": query, "variables": dict(variables)}, "response": {"data": dict(data)},
    }
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    final = inbox / f"{request_id}.json"
    temporary = inbox / f".{request_id}.tmp"
    with open(temporary, "xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, final)  # fails if the final record already exists
    finally:
        temporary.unlink()
    return final
