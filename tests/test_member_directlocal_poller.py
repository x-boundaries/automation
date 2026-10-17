"""#155 E8: Claude-free Shopify poller, direct-HTTP capture provenance and
the disabled Task Scheduler template.

Offline only. A fake Shopify Admin API (keyset cursors, search-index lag,
scripted HTTP/GraphQL failures) is injected as the transport; sockets and
child processes are patched to fail, so no live Shopify call and no
AutoCount process can happen. All identities are synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "directlocal"))

import xb_directlocal as dl  # noqa: E402
import xb_shopify_direct as direct  # noqa: E402
import xb_shopify_poller as poller  # noqa: E402

STORE = "gid://shopify/Shop/900000001"
DOMAIN = "xb-poller-test.synthetic.invalid"
API_VERSION = "2026-07"
T0 = datetime(2026, 10, 17, 0, 0, 0, tzinfo=timezone.utc)
STATIC_TOKEN = "shpat_synthetic_token_value_0001"
CLIENT_ID = "synthetic-client-id-000001"
CLIENT_SECRET = "synthetic-client-secret-000001"
ENV = {"XB_SHOPIFY_POLLER_TOKEN": STATIC_TOKEN, "XB_SHOPIFY_POLLER_CLIENT_ID": CLIENT_ID, "XB_SHOPIFY_POLLER_CLIENT_SECRET": CLIENT_SECRET}
TEMPLATE = ROOT / "config" / "directlocal_shopify_poller.task.template.xml"
EXAMPLE = ROOT / "config" / "directlocal_shopify_poller.example.json"


class SimulatedCrash(BaseException):
    """The poller process dying at a precise point."""


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def gid(n: int) -> str:
    return f"gid://shopify/Customer/{n}"


@dataclass
class FakeCustomer:
    num: int
    created: datetime
    updated: datetime
    tags: list[str]
    registered: datetime | None = None  # membership.start_date metafield createdAt
    metafield_num: int | None = None
    start: str | None = "2026-10-17"
    expiry: str | None = "2028-10-16"
    first: str = "Synthetic"
    last: str = "Poller Member"
    email: str | None = "poller.member@example.test"
    phone: str | None = "+65 8000 0101"
    indexed: bool = True  # visible to the search index (recent query)
    offset: str = "Z"  # rendering offset of the metafield createdAt

    def node(self) -> dict:
        return {"id": gid(self.num), "createdAt": iso(self.created), "tags": list(self.tags), "registration": self._registration()}

    def _registration(self) -> dict | None:
        if self.registered is None:
            return None
        if self.offset == "Z":
            when = iso(self.registered)
        else:
            hours = int(self.offset[:3])
            when = (self.registered + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S") + self.offset
        return {"id": f"gid://shopify/Metafield/{self.metafield_num or self.num + 500000}", "createdAt": when}

    def profile(self) -> dict:
        registration = self._registration()
        return {
            "id": gid(self.num), "createdAt": iso(self.created), "tags": list(self.tags), "firstName": self.first, "lastName": self.last,
            "defaultEmailAddress": None if self.email is None else {"emailAddress": self.email},
            "defaultPhoneNumber": None if self.phone is None else {"phoneNumber": self.phone},
            "startDate": None if registration is None or self.start is None else {**registration, "type": "date", "value": self.start},
            "expiryDate": None if registration is None or self.expiry is None else {"type": "date", "value": self.expiry},
        }


def _cursor(key) -> str:
    return base64.b64encode(json.dumps(key).encode()).decode()


def _uncursor(text: str):
    return tuple(json.loads(base64.b64decode(text)))


def http(status: int, value=None, headers=None, body: bytes | None = None) -> direct.HttpResponse:
    return direct.HttpResponse(status, {k.lower(): v for k, v in (headers or {}).items()}, body if body is not None else json.dumps(value).encode())


class FakeShopify:
    """Admin API double: client-credentials grant, session check, the Core's
    pinned queries with keyset cursors, and scripted failures."""

    def __init__(self, clock):
        self.clock = clock
        self.customers: dict[int, FakeCustomer] = {}
        self.scopes = ["read_customers"]
        self.valid_tokens = {STATIC_TOKEN}
        self.issued = 0
        self.token_scope = "read_customers"
        self.failures: list[tuple[str, object]] = []  # (kind, response or exception)
        self.calls: list[str] = []
        self.on_request = None
        self.available = 1000.0
        self.cursor_override: str | None = None
        self.issued_cursors: list[str] = []
        self.shop = {"id": STORE, "myshopifyDomain": DOMAIN}

    # helpers
    def add(self, num: int, **kwargs) -> FakeCustomer:
        customer = FakeCustomer(num=num, **kwargs)
        self.customers[num] = customer
        return customer

    def register(self, num: int, at: datetime, *, created: datetime | None = None, metafield: bool = True, **kwargs) -> FakeCustomer:
        """A Shopify Forms registration at ``at`` (new or existing customer)."""

        customer = self.customers.get(num)
        if customer is None:
            customer = self.add(num, created=created or at, updated=at, tags=[], **kwargs)
        customer.tags = sorted(set(customer.tags) | {"member-mg", "shopify-forms-1178308"})
        customer.updated = at
        if metafield:
            customer.registered = at + timedelta(seconds=3)
            customer.updated = customer.registered
        for key, value in kwargs.items():
            setattr(customer, key, value)
        return customer

    def fail_next(self, kind: str, response) -> None:
        self.failures.append((kind, response))

    def _extensions(self) -> dict:
        return {"cost": {"requestedQueryCost": 52, "actualQueryCost": 12, "throttleStatus": {"maximumAvailable": 1000.0, "currentlyAvailable": self.available, "restoreRate": 50.0}}}

    def _ok(self, data: dict) -> direct.HttpResponse:
        return http(200, {"data": data, "extensions": self._extensions()})

    def __call__(self, url, body, headers, timeout):
        assert url.startswith(f"https://{DOMAIN}/"), "fake reached for a foreign host"
        path = url[len(f"https://{DOMAIN}"):]
        if path == "/admin/oauth/access_token":
            self.calls.append("token")
            form = dict(urllib.parse.parse_qsl(body.decode()))
            if form != {"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}:
                return http(401, {"error": "invalid_client"})
            self.issued += 1
            token = f"shpca_synthetic_issued_{self.issued:04d}"
            self.valid_tokens.add(token)
            return http(200, {"access_token": token, "scope": self.token_scope, "expires_in": 86399})
        assert path == f"/admin/api/{API_VERSION}/graphql.json"
        payload = json.loads(body)
        query, variables = payload["query"], payload["variables"]
        kind = "session" if query == direct.SESSION_QUERY else dl.PINNED_QUERIES[dl._sha256(query)]
        self.calls.append(kind)
        if self.on_request is not None:
            self.on_request(kind, variables)
        for index, (when, response) in enumerate(self.failures):
            if when in (kind, "any"):
                del self.failures[index]
                if isinstance(response, BaseException):
                    raise response
                return response
        if headers.get("X-Shopify-Access-Token") not in self.valid_tokens:
            return http(401, {"errors": "[API] Invalid API key or access token"})
        if kind == "session":
            return self._ok({"shop": self.shop, "currentAppInstallation": {"accessScopes": [{"handle": s} for s in self.scopes]}})
        if kind == "profile":
            num = int(variables["id"].rsplit("/", 1)[1])
            customer = self.customers.get(num)
            return self._ok({"shop": self.shop, "customer": None if customer is None else customer.profile()})
        if kind == "baseline":
            items = sorted(self.customers.values(), key=lambda c: c.num)
            keyfn = lambda c: (c.num,)  # noqa: E731
        else:
            mark = datetime.strptime(variables["search"], "updated_at:>='%Y-%m-%dT%H:%M:%SZ'").replace(tzinfo=timezone.utc)
            items = sorted((c for c in self.customers.values() if c.indexed and c.updated >= mark), key=lambda c: (iso(c.updated), c.num))
            keyfn = lambda c: (iso(c.updated), c.num)  # noqa: E731
        if variables.get("after") and variables["after"] != self.cursor_override:
            key = _uncursor(variables["after"])
            items = [c for c in items if keyfn(c) > key]
        page = items[: variables["first"]]
        has_next = len(items) > variables["first"]
        end = _cursor(list(keyfn(page[-1]))) if page else None
        if end is not None:
            self.issued_cursors.append(end)
        if self.cursor_override is not None and has_next:
            end = self.cursor_override
        return self._ok({"shop": self.shop, "customers": {"pageInfo": {"hasNextPage": has_next, "endCursor": end}, "nodes": [c.node() for c in page]}})


class Clock:
    def __init__(self, now: datetime):
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)

    def monotonic(self) -> float:
        return self.now.timestamp()


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def _no_process(*args, **kwargs):
    raise AssertionError("child process attempted in an offline test")


class OfflineCase(unittest.TestCase):
    """Every test runs with sockets and child processes disabled."""

    def setUp(self):
        for target in ("socket.socket.connect", "socket.create_connection", "subprocess.Popen", "urllib.request.urlopen"):
            patcher = mock.patch(target, side_effect=_no_process if "Popen" in target else _no_network)
            patcher.start()
            self.addCleanup(patcher.stop)


class PollerCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.temp = Path(tempfile.mkdtemp(prefix="xb-poller-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.clock = Clock(T0)
        ledger = dl.Ledger(self.temp, clock=self.clock)
        dl.Processor(ledger).init_ledger(mode="synthetic", store_gid=STORE, shop_domain=DOMAIN)
        ledger.close()
        self.shopify = FakeShopify(self.clock)
        # 3 historical members (one registered via Forms long ago), 1 non-member.
        self.shopify.add(1001, created=T0 - timedelta(days=400), updated=T0 - timedelta(days=30), tags=["member-mg"], registered=T0 - timedelta(days=400))
        self.shopify.add(1002, created=T0 - timedelta(days=90), updated=T0 - timedelta(days=2), tags=["member-mg"], registered=T0 - timedelta(days=89))
        self.shopify.add(1003, created=T0 - timedelta(days=20), updated=T0 - timedelta(days=1), tags=["Member-MG "], registered=T0 - timedelta(days=20))
        self.shopify.add(1004, created=T0 - timedelta(days=10), updated=T0 - timedelta(days=1), tags=["vip"])
        self.config_values = {
            "schema": poller.CONFIG_SCHEMA, "state_root": str(self.temp), "store_gid": STORE, "shop_domain": DOMAIN, "api_version": API_VERSION,
            "auth": {"method": "admin_api_access_token", "token_env": "XB_SHOPIFY_POLLER_TOKEN"},
            "autocount_write_mode": "none", "profile_reads_enabled": True,
        }
        self.protector = None

    def config(self, **overrides) -> poller.PollerConfig:
        return poller.parse_config({**self.config_values, **overrides})

    def poll(self, *, advance: timedelta = timedelta(0), baseline: bool = False, **overrides) -> tuple[int, dict]:
        self.clock.now += advance
        return poller.run_once(self.config(**overrides), environ=ENV, transport=self.shopify, sleep=self.clock.sleep, clock=self.clock, monotonic=self.clock.monotonic, baseline=baseline, token_protector=self.protector)

    def baseline(self) -> dict:
        code, report = self.poll(baseline=True)
        self.assertEqual((code, report["status"]), (0, "ok"), report)
        return report

    def db(self, sql: str, *params):
        ledger = dl.Ledger(self.temp, clock=self.clock)
        try:
            return [dict(row) for row in ledger.conn.execute(sql, params).fetchall()]
        finally:
            ledger.close()

    def customer(self, num: int) -> dict | None:
        rows = self.db("SELECT * FROM customers WHERE customer_gid = ?", gid(num))
        return rows[0] if rows else None

    def runs(self) -> list[dict]:
        return self.db("SELECT kind, state, search, started_at FROM runs ORDER BY started_at, rowid")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

class ConfigTests(PollerCase):
    def test_example_config_parses_and_names_no_secret(self):
        value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        config = poller.parse_config(value)
        self.assertEqual(config.autocount_write_mode, "none")
        self.assertFalse(config.profile_reads_enabled)
        self.assertEqual(config.full_scan_interval_hours, 24)
        self.assertEqual((config.store_gid, config.shop_domain), next(iter(dl.QUALIFIED_CONNECTOR_STORES.items())))
        text = EXAMPLE.read_text(encoding="utf-8")
        for marker in ("shpat_", "shpca_", "secret\": \"", "token\": \""):
            self.assertNotIn(marker, text)

    def test_strict_keys_bounds_and_write_mode(self):
        cases = {
            "poller_config_keys_invalid": {"token": STATIC_TOKEN},
            "autocount_write_mode_not_available": {"autocount_write_mode": "autocount"},
            "poller_config_auth_invalid": {"auth": {"method": "admin_api_access_token", "token_env": "PATH"}},
            "poller_config_recent_max_pages_invalid": {"recent_max_pages": 0},
            "poller_config_max_run_seconds_invalid": {"max_run_seconds": True},
            "shopify_api_version_invalid": {"api_version": "unstable"},
            "poller_config_profile_scope_invalid": {"profile_discovered_since": "yesterday"},
            "poller_config_token_cache_invalid": {"token_cache": "dpapi_current_user"},  # static legacy token is never cached
        }
        for code, override in cases.items():
            with self.subTest(code=code), self.assertRaises(dl.DirectLocalError) as raised:
                self.config(**override)
            self.assertEqual(raised.exception.code, code)
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.config(auth={"method": "admin_api_access_token", "token_env": "XB_SHOPIFY_A", "token": STATIC_TOKEN})
        self.assertEqual(raised.exception.code, "poller_config_auth_invalid")

    def test_check_config_reports_presence_never_values(self):
        result = poller.check_config(self.config(), {"XB_SHOPIFY_POLLER_TOKEN": STATIC_TOKEN})
        self.assertEqual(result["env_present"], {"XB_SHOPIFY_POLLER_TOKEN": True})
        self.assertNotIn(STATIC_TOKEN, json.dumps(result))

    def test_cli_requires_explicit_no_write_and_baseline_confirmation(self):
        path = self.temp / "poller.json"
        path.write_text(json.dumps(self.config_values), encoding="utf-8")
        for argv, code in (
            (["--config", str(path), "run-once"], "autocount_write_mode_must_be_explicit"),
            (["--config", str(path), "baseline", "--no-autocount-write"], "baseline_confirmation_required"),
        ):
            with self.subTest(argv=argv), mock.patch("sys.stdout") as out:
                self.assertEqual(poller.main(argv), 2)
                self.assertEqual(json.loads(out.write.call_args[0][0])["code"], code)


# --------------------------------------------------------------------------
# Direct client
# --------------------------------------------------------------------------

class ClientTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock(T0)
        self.shopify = FakeShopify(self.clock)

    def client(self, provider=None, **kwargs) -> direct.ShopifyDirectClient:
        return direct.ShopifyDirectClient(
            shop_domain=DOMAIN, api_version=API_VERSION, token_provider=provider or direct.StaticTokenProvider(STATIC_TOKEN),
            transport=self.shopify, sleep=self.clock.sleep, monotonic=self.clock.monotonic, **kwargs,
        )

    def code(self, call) -> str:
        with self.assertRaises(direct.ShopifyDirectError) as raised:
            call()
        return raised.exception.code

    def page(self, client):
        return client.execute(dl.BASELINE_QUERY, {"first": dl.PAGE_SIZE, "after": None})

    def test_graphql_errors_fail_closed_even_on_http_200(self):
        cases = {
            "shopify_graphql_error": http(200, {"errors": [{"message": "x"}]}),
            "shopify_access_denied": http(200, {"errors": [{"message": "x", "extensions": {"code": "ACCESS_DENIED"}}], "data": {"customers": None}}),
            "shopify_response_shape_invalid": http(200, {"data": {"shop": {}}, "unexpected": 1}),
            "shopify_response_invalid": http(200, body=b"<html>maintenance</html>"),
            "shopify_response_too_large": http(200, body=b" " * (direct.MAX_RESPONSE_BYTES + 1)),
        }
        for expected, response in cases.items():
            with self.subTest(expected=expected):
                self.shopify.fail_next("baseline", response)
                self.assertEqual(self.code(lambda: self.page(self.client())), expected)
        # Partial data next to errors is never accepted.
        self.shopify.fail_next("baseline", http(200, {"data": {"shop": self.shopify.shop, "customers": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}, "errors": [{"message": "partial"}]}))
        self.assertEqual(self.code(lambda: self.page(self.client())), "shopify_graphql_error")

    def test_http_failures_are_classified_and_bounded(self):
        for status, expected in ((403, "shopify_access_denied"), (404, "shopify_not_found"), (402, "shopify_shop_unavailable"), (418, "shopify_http_error")):
            with self.subTest(status=status):
                self.shopify.fail_next("baseline", http(status, {}))
                self.assertEqual(self.code(lambda: self.page(self.client())), expected)
        client = self.client()
        for _ in range(direct.RETRY_MAX_ATTEMPTS_PER_REQUEST):
            self.shopify.fail_next("baseline", http(502, {}))
        self.assertEqual(self.code(lambda: self.page(client)), "shopify_retry_budget_exhausted")
        self.assertEqual(client.stats.retries, direct.RETRY_MAX_ATTEMPTS_PER_REQUEST - 1)
        self.shopify.fail_next("baseline", http(503, {}, {"Retry-After": "120"}))
        self.assertEqual(self.code(lambda: self.page(self.client())), "shopify_retry_after_exceeds_bound")

    def test_throttling_waits_from_cost_metadata_then_succeeds(self):
        throttled = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                     "extensions": {"cost": {"requestedQueryCost": 252, "throttleStatus": {"maximumAvailable": 1000.0, "currentlyAvailable": 2.0, "restoreRate": 50.0}}}}
        self.shopify.fail_next("baseline", http(200, throttled))
        self.shopify.fail_next("baseline", http(429, {}, {"Retry-After": "2.0"}))
        client = self.client()
        data = self.page(client)
        self.assertIn("customers", data)
        self.assertNotIn("extensions", data)
        self.assertEqual(self.clock.slept, [5, 2])
        self.assertEqual(client.stats.retries, 2)
        impossible = {"errors": [{"extensions": {"code": "THROTTLED"}}], "extensions": {"cost": {"requestedQueryCost": 2000, "throttleStatus": {"maximumAvailable": 1000.0, "currentlyAvailable": 1000.0, "restoreRate": 50.0}}}}
        self.shopify.fail_next("baseline", http(200, impossible))
        self.assertEqual(self.code(lambda: self.page(self.client())), "shopify_query_cost_exceeds_bucket")

    def test_low_bucket_paces_the_next_request(self):
        client = self.client()
        self.shopify.available = 12.0
        self.page(client)
        self.page(client)
        self.assertEqual(self.clock.slept, [1])  # (52 - 12) / 50 rounded up
        self.assertEqual(client.stats.min_available, 12.0)

    def test_static_token_rejection_is_not_retried(self):
        self.shopify.valid_tokens.clear()
        client = self.client()
        self.assertEqual(self.code(lambda: self.page(client)), "shopify_auth_rejected")
        self.assertEqual(self.shopify.calls, ["baseline"])
        for bad in ("", "has space in token value", None, "short"):
            with self.subTest(bad=bad):
                self.assertEqual(self.code(lambda: direct.StaticTokenProvider(bad)), "shopify_token_missing")

    def test_client_credentials_cache_expiry_and_one_refresh_after_401(self):
        provider = direct.ClientCredentialsTokenProvider(DOMAIN, CLIENT_ID, CLIENT_SECRET, transport=self.shopify, clock=self.clock)
        client = self.client(provider)
        self.page(client)
        self.page(client)
        self.assertEqual(self.shopify.issued, 1)  # cached in memory
        self.shopify.valid_tokens.clear()  # revoked/expired early on the server
        self.page(client)
        self.assertEqual((self.shopify.issued, client.stats.auth_refreshes), (2, 1))
        self.clock.now += timedelta(seconds=86399 - direct.TOKEN_REFRESH_MARGIN_SECONDS)
        self.page(client)
        self.assertEqual(self.shopify.issued, 3)  # refreshed before expiry
        # A second 401 within one request fails closed.
        self.shopify.fail_next("baseline", http(401, {}))
        self.shopify.fail_next("baseline", http(401, {}))
        self.assertEqual(self.code(lambda: self.page(client)), "shopify_auth_rejected")

    def test_client_credentials_rejections_and_scope_minimality(self):
        wrong = direct.ClientCredentialsTokenProvider(DOMAIN, CLIENT_ID, "wrong-secret-value-000000", transport=self.shopify, clock=self.clock)
        self.assertEqual(self.code(wrong.token), "shopify_client_credentials_rejected")
        for scope in ("read_customers,write_customers", "read_orders", "read_customers,read_orders"):
            with self.subTest(scope=scope):
                self.shopify.token_scope = scope
                provider = direct.ClientCredentialsTokenProvider(DOMAIN, CLIENT_ID, CLIENT_SECRET, transport=self.shopify, clock=self.clock)
                self.assertEqual(self.code(provider.token), "shopify_token_scope_not_minimal")

    def test_session_check_binds_store_and_exact_scopes(self):
        self.client().verify_session(store_gid=STORE)
        self.shopify.scopes = ["read_customers", "write_customers"]
        self.assertEqual(self.code(lambda: self.client().verify_session(store_gid=STORE)), "shopify_token_scope_not_minimal")
        self.shopify.scopes = ["read_customers"]
        self.assertEqual(self.code(lambda: self.client().verify_session(store_gid="gid://shopify/Shop/900000002")), "shopify_store_identity_mismatch")

    def test_only_pinned_queries_are_sent(self):
        mutation = "mutation { customerDelete(input: {id: \"gid://shopify/Customer/1\"}) { deletedCustomerId } }"
        self.assertEqual(self.code(lambda: self.client().execute(mutation, {})), "shopify_query_not_pinned")
        self.assertEqual(self.shopify.calls, [])
        source = (ROOT / "directlocal" / "xb_shopify_direct.py").read_text(encoding="utf-8")
        self.assertNotIn("mutation", source)

    def test_real_transport_refuses_non_shopify_hosts_before_any_socket(self):
        transport = direct.UrllibTransport()
        for url in (f"https://{DOMAIN}/admin/api/{API_VERSION}/graphql.json", "http://xboundariessg.myshopify.com/x", "https://xboundariessg.myshopify.com:8443/x", "https://evil.example/x"):
            with self.subTest(url=url), self.assertRaises(direct.ShopifyDirectError) as raised:
                transport(url, b"{}", {}, 5)
            self.assertEqual(raised.exception.code, "shopify_host_not_allowed")
        with self.assertRaises(direct.ShopifyDirectError) as raised:
            direct.store_url("evil.example", "/x")
        self.assertEqual(raised.exception.code, "shopify_host_not_allowed")

    def test_real_transport_disables_proxies_and_redirects(self):
        # An environment proxy is ignored: build_opener skips its default
        # ProxyHandler when one is given, and the empty one routes nothing.
        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.synthetic.invalid:8080", "https_proxy": "http://proxy.synthetic.invalid:8080"}):
            handlers = direct.UrllibTransport()._opener.handlers
        proxies = [h for h in handlers if isinstance(h, direct.urllib.request.ProxyHandler)]
        self.assertTrue(all(not h.proxies for h in proxies))
        redirects = [h for h in handlers if isinstance(h, direct.urllib.request.HTTPRedirectHandler)]
        self.assertEqual([type(h) for h in redirects], [direct._NoRedirect])
        with self.assertRaises(direct.ShopifyDirectError) as raised:
            redirects[0].redirect_request(None, None, 302, "Found", {}, "https://evil.example/")
        self.assertEqual(raised.exception.code, "shopify_redirect_refused")

    def test_errors_never_render_secrets(self):
        error = direct.ShopifyDirectError("shopify_auth_rejected")
        self.assertEqual(repr(error), "ShopifyDirectError(code='shopify_auth_rejected', retryable=False)")
        self.assertEqual(repr(direct.StaticTokenProvider(STATIC_TOKEN)), "StaticTokenProvider()")


# --------------------------------------------------------------------------
# Poller end to end (synthetic ledger, fake Shopify)
# --------------------------------------------------------------------------

class PollerFlowTests(PollerCase):
    def test_baseline_then_new_and_existing_customer_registrations(self):
        report = self.baseline()
        self.assertEqual((report["scan"], report["discovered"]), ("baseline", 0))
        self.shopify.register(2001, T0 + timedelta(minutes=1))  # new customer
        self.shopify.register(1004, T0 + timedelta(minutes=1, seconds=10))  # existing customer, registers now
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["status"], report["scan"]), (0, "ok", "recent"), report)
        self.assertEqual((report["discovered"], report["profiles_read"], report["decisions"]), (2, 2, {"ADMITTED": 2}))
        for num in (2001, 1004):
            row = self.customer(num)
            self.assertEqual(row["state"], "ADMITTED")
            self.assertEqual(row["registration_id"], f"gid://shopify/Metafield/{num + 500000}")
        self.assertEqual(self.customer(1001), None)  # baseline member never discovered
        runs = self.runs()
        self.assertEqual([(r["kind"], r["state"]) for r in runs], [("BASELINE", "SEALED"), ("DISCOVERY", "SEALED")])
        self.assertEqual(runs[1]["search"], "updated_at:>='2026-10-17T00:00:00Z'")  # cutover

    def test_replay_never_repeats_admission_or_profile_reads(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1))
        for index in range(4):
            code, report = self.poll(advance=timedelta(minutes=2))
            self.assertEqual(code, 0, report)
        self.assertEqual(self.shopify.calls.count("profile"), 1)
        self.assertEqual(self.db("SELECT COUNT(*) AS n FROM customers")[0]["n"], 1)
        row = self.customer(2001)
        self.assertEqual((row["state"], row["check_count"]), ("ADMITTED", 1))
        self.assertEqual(report["customers"], {"ADMITTED": 1})
        # Each recent run overlaps the last sealed one by the Core's 15-minute
        # margin, never before the cutover.
        self.poll(advance=timedelta(minutes=30))
        self.poll(advance=timedelta(minutes=2))
        searches = [r["search"] for r in self.runs() if r["search"]]
        self.assertEqual(searches[-2:], ["updated_at:>='2026-10-17T00:00:00Z'", "updated_at:>='2026-10-17T00:23:00Z'"])

    def test_delayed_metafield_waits_then_admits_on_a_later_run(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1), metafield=False)
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual(report["decisions"], {"WAITING": 1})
        # A run within profile_retry_seconds does not spend the wait budget.
        self.poll(advance=timedelta(seconds=30))
        self.assertEqual(self.customer(2001)["check_count"], 1)
        self.shopify.register(2001, T0 + timedelta(minutes=1))  # Forms writes start_date
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual(report["decisions"], {"ADMITTED": 1})
        self.assertEqual(self.customer(2001)["check_count"], 2)

    def test_tag_without_registration_metafield_ends_in_review(self):
        self.baseline()
        self.shopify.customers[1004].tags.append("member-mg")  # staff tag, no Forms registration
        self.shopify.customers[1004].updated = T0 + timedelta(minutes=1)
        for _ in range(dl.PROFILE_WAIT_MAX_CHECKS):
            self.poll(advance=timedelta(minutes=2))
        row = self.customer(1004)
        self.assertEqual((row["state"], row["decision_code"], row["check_count"]), ("MANUAL_REVIEW", "preexisting_customer_not_new_signup", 5))
        self.poll(advance=timedelta(minutes=2))
        self.assertEqual(self.shopify.calls.count("profile"), 5)

    def test_changing_registration_metafield_goes_to_review(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1), expiry=None)  # dates incomplete: WAIT, event bound
        self.poll(advance=timedelta(minutes=2))
        self.assertEqual(self.customer(2001)["state"], "WAITING")
        self.shopify.customers[2001].metafield_num = 777777
        self.shopify.customers[2001].expiry = "2028-10-16"
        self.poll(advance=timedelta(minutes=2))
        row = self.customer(2001)
        self.assertEqual((row["state"], row["decision_code"]), ("MANUAL_REVIEW", "registration_event_changed"))

    def test_timezone_offset_registration_instant(self):
        self.baseline()
        # 08:00:03+08:00 is 00:00:03Z on the cutover day: after C, so new.
        self.shopify.register(1004, T0, offset="+08:00")
        self.poll(advance=timedelta(minutes=2))
        row = self.customer(1004)
        self.assertEqual((row["state"], row["registration_at"]), ("ADMITTED", "2026-10-17T00:00:03Z"))

    def test_index_lag_is_caught_by_the_daily_full_scan(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1), indexed=False)
        for _ in range(3):
            code, report = self.poll(advance=timedelta(minutes=2))
            self.assertEqual((report["scan"], report["discovered"]), ("recent", 0))
        code, report = self.poll(advance=timedelta(hours=24))
        self.assertEqual((report["scan"], report["discovered"], report["decisions"]), ("full", 1, {"ADMITTED": 1}))
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual(report["scan"], "recent")

    def test_backfill_after_an_outage_and_pagination(self):
        self.baseline()
        self.poll(advance=timedelta(minutes=2))
        for n in range(120):  # registrations while the poller was down for 3 hours
            self.shopify.register(3000 + n, T0 + timedelta(minutes=10 + n))
        code, report = self.poll(advance=timedelta(hours=3), max_profile_reads_per_run=50)
        self.assertEqual((code, report["scan"], report["pages"], report["discovered"]), (0, "recent", 3, 120), report)
        self.assertEqual(report["profiles_read"], 50)
        for _ in range(2):
            self.poll(advance=timedelta(minutes=2), max_profile_reads_per_run=50)
        self.assertEqual(self.db("SELECT COUNT(*) AS n FROM customers WHERE state = 'ADMITTED'")[0]["n"], 120)

    def test_recent_overflow_escalates_to_full_scan(self):
        self.baseline()
        for n in range(60):
            self.shopify.register(3000 + n, T0 + timedelta(minutes=1))
        code, report = self.poll(advance=timedelta(minutes=2), recent_max_pages=1, profile_reads_enabled=False)
        self.assertEqual((code, report["scan"], report["discovered"]), (0, "recent_escalated_full", 60), report)
        self.assertEqual([r["state"] for r in self.runs()], ["SEALED", "ABANDONED", "SEALED"])
        code, report = self.poll(advance=timedelta(minutes=2), recent_max_pages=1, full_scan_max_pages=1, profile_reads_enabled=False)
        self.assertEqual((code, report["code"]), (2, "full_scan_page_limit_exceeded"))

    def test_profile_reads_disabled_is_discovery_only(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1))
        code, report = self.poll(advance=timedelta(minutes=2), profile_reads_enabled=False)
        self.assertEqual((report["discovered"], report["profiles_read"]), (1, 0))
        self.assertNotIn("profile", self.shopify.calls)
        self.assertEqual(self.customer(2001)["state"], "DISCOVERED")

    def test_profile_scope_leaves_earlier_discoveries_untouched(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1))
        self.poll(advance=timedelta(minutes=2), profile_reads_enabled=False)
        since = iso(self.clock.now + timedelta(minutes=1))
        self.shopify.register(2002, T0 + timedelta(minutes=5))
        code, report = self.poll(advance=timedelta(minutes=4), profile_discovered_since=since)
        self.assertEqual((report["profiles_read"], report["profiles_out_of_scope"]), (1, 1))
        self.assertEqual((self.customer(2001)["state"], self.customer(2002)["state"]), ("DISCOVERED", "ADMITTED"))

    def test_client_credentials_token_expiry_mid_run(self):
        self.config_values["auth"] = {"method": "client_credentials", "client_id_env": "XB_SHOPIFY_POLLER_CLIENT_ID", "client_secret_env": "XB_SHOPIFY_POLLER_CLIENT_SECRET"}
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1))
        self.shopify.on_request = lambda kind, variables: self.shopify.valid_tokens.clear() if kind == "recent" and self.shopify.issued == 2 else None
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["shopify"]["auth_refreshes"], report["decisions"]), (0, 1, {"ADMITTED": 1}), report)


class PollerFailureTests(PollerCase):
    def test_static_token_expiry_fails_closed_backs_off_and_recovers(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1))
        self.shopify.fail_next("recent", http(401, {}))
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual(code, 2)
        self.assertEqual((report["status"], report["code"], report["consecutive_failures"]), ("failed", "shopify_auth_rejected", 1))
        self.assertEqual(self.runs()[-1]["state"], "ABANDONED")
        self.assertIsNone(self.customer(2001))
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["status"], report["code"]), (0, "skipped_backoff", "shopify_auth_rejected"))
        code, report = self.poll(advance=timedelta(hours=1))
        self.assertEqual((code, report["status"], report["decisions"]), (0, "ok", {"ADMITTED": 1}))
        # The watermark never moved past the failed run: the search starts at C.
        self.assertEqual([r["search"] for r in self.runs() if r["state"] == "SEALED" and r["search"]], ["updated_at:>='2026-10-17T00:00:00Z'"])
        state = json.loads((self.temp / poller.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual((state["consecutive_failures"], state["not_before"]), (0, None))

    def test_graphql_error_on_a_page_abandons_the_run(self):
        self.baseline()
        for n in range(60):
            self.shopify.register(3000 + n, T0 + timedelta(minutes=1))
        calls = []
        def second_page_fails(kind, variables):
            if kind == "recent":
                calls.append(variables.get("after"))
                if len(calls) == 2:
                    self.shopify.fail_next("recent", http(200, {"errors": [{"message": "Internal error"}], "data": None}))
        self.shopify.on_request = second_page_fails
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["code"], report["pages"]), (2, "shopify_graphql_error", 0))
        self.assertEqual(self.runs()[-1]["state"], "ABANDONED")
        self.assertEqual(self.db("SELECT COUNT(*) AS n FROM customers")[0]["n"], 0)
        self.shopify.on_request = None
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["discovered"]), (0, 60))

    def test_throttling_inside_a_run_is_absorbed(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1))
        self.shopify.fail_next("recent", http(200, {"errors": [{"extensions": {"code": "THROTTLED"}}], "extensions": {"cost": {"requestedQueryCost": 52, "throttleStatus": {"maximumAvailable": 1000.0, "currentlyAvailable": 0.0, "restoreRate": 50.0}}}}))
        self.shopify.fail_next("profile", http(503, {}, {"Retry-After": "1"}))
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["shopify"]["retries"], report["decisions"]), (0, 2, {"ADMITTED": 1}), report)

    def test_duplicate_gid_across_pages_is_never_sealed(self):
        self.baseline()
        for n in range(60):
            self.shopify.register(3000 + n, T0 + timedelta(minutes=1))
        def churn(kind, variables):  # a page-1 customer is updated mid-pagination
            if kind == "recent" and variables.get("after"):
                self.shopify.customers[3000].updated = self.clock.now
        self.shopify.on_request = churn
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["code"]), (2, "run_not_sealed:baseline_duplicate_gid"))
        self.assertEqual(self.runs()[-1]["state"], "ABANDONED")
        self.shopify.on_request = None
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["discovered"]), (0, 60))

    def test_corrupted_cursor_and_incomplete_page(self):
        self.baseline()
        for n in range(120):
            self.shopify.register(3000 + n, T0 + timedelta(minutes=1))
        self.shopify.cursor_override = "same-cursor"
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["code"]), (2, "shopify_cursor_invalid"))
        self.shopify.cursor_override = None
        self.shopify.fail_next("recent", http(200, {"data": {"shop": self.shopify.shop, "customers": None}}))
        code, report = self.poll(advance=timedelta(hours=1))
        self.assertEqual((code, report["code"]), (2, "shopify_response_shape_invalid"))
        self.assertEqual(self.db("SELECT COUNT(*) AS n FROM customers")[0]["n"], 0)

    def test_permission_and_scope_failures_need_the_operator(self):
        self.baseline()
        for response, code in ((http(403, {}), "shopify_access_denied"), (None, "shopify_token_scope_not_minimal")):
            with self.subTest(code=code):
                if response is None:
                    self.shopify.scopes = ["read_customers", "write_customers"]
                else:
                    self.shopify.fail_next("session", response)
                exit_code, report = self.poll(advance=timedelta(hours=2))
                self.assertEqual((exit_code, report["code"]), (2, code))
                state = json.loads((self.temp / poller.STATE_FILE).read_text(encoding="utf-8"))
                self.assertEqual(dl.parse_ts(state["not_before"]) - self.clock.now, timedelta(seconds=poller.BACKOFF_OPERATOR_SECONDS))
        self.assertNotIn("recent", self.shopify.calls)  # no customer read before the session check passes

    def test_transient_backoff_is_bounded(self):
        self.assertEqual([poller.backoff_seconds(n, "shopify_transport_failed") for n in (1, 2, 3, 6, 50)], [60, 120, 240, 1800, 1800])
        self.assertEqual(poller.backoff_seconds(1, "shopify_auth_rejected"), 3600)

    def test_corrupted_backoff_state_resets_visibly(self):
        self.baseline()
        (self.temp / poller.STATE_FILE).write_text("{not json", encoding="utf-8")
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["status"], report["state_note"]), (0, "ok", "poller_state_reset"))


class PollerConcurrencyAndRecoveryTests(PollerCase):
    def test_second_invocation_skips_while_the_first_holds_the_ledger(self):
        self.baseline()
        holder = dl.Ledger(self.temp, clock=self.clock)
        try:
            code, report = self.poll(advance=timedelta(minutes=2))
        finally:
            holder.close()
        self.assertEqual((code, report["status"]), (0, "skipped_instance_running"))
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.shopify.calls.count("recent"), 0)

    def test_crash_mid_pagination_then_restart(self):
        self.baseline()
        for n in range(60):
            self.shopify.register(3000 + n, T0 + timedelta(minutes=1))
        pages = []
        def crash_on_second(kind, variables):
            if kind == "recent":
                pages.append(1)
                if len(pages) == 2:
                    raise SimulatedCrash()
        self.shopify.on_request = crash_on_second
        with self.assertRaises(SimulatedCrash):
            self.poll(advance=timedelta(minutes=2))
        self.assertEqual(self.runs()[-1]["state"], "CAPTURING")
        self.assertEqual(len(list((self.temp / "inbox").glob("xbdh-*.json"))), 1)
        self.shopify.on_request = None
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["stale_run_abandoned"], report["discovered"]), (0, True, 60), report)
        self.assertEqual([r["state"] for r in self.runs()], ["SEALED", "ABANDONED", "SEALED"])
        self.assertEqual(self.db("SELECT COUNT(*) AS n FROM customers")[0]["n"], 60)

    def test_crash_after_profile_capture_before_processing(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1))
        with mock.patch.object(poller.NoWriteProcessor, "process", side_effect=SimulatedCrash()):
            with self.assertRaises(SimulatedCrash):
                self.poll(advance=timedelta(minutes=2))
        row = self.customer(2001)
        self.assertEqual((row["state"], row["check_count"]), ("DISCOVERED", 0))  # discovery sealed, no decision
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["decisions"]), (0, {"ADMITTED": 1}))
        row = self.customer(2001)
        self.assertEqual((row["state"], row["check_count"]), ("ADMITTED", 1))
        self.assertEqual(self.shopify.calls.count("profile"), 2)  # the unconsumed capture was never counted
        self.poll(advance=timedelta(minutes=20))
        self.assertEqual(list((self.temp / "inbox").glob("*.json")), [])  # stale capture purged

    def test_crash_inside_the_seal_transaction_rolls_back(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1))
        with mock.patch.object(dl.Ledger, "event", _event_crashing_on("customer_discovered")):
            with self.assertRaises(SimulatedCrash):
                self.poll(advance=timedelta(minutes=2))
        self.assertIsNone(self.customer(2001))
        self.assertEqual(self.runs()[-1]["state"], "CAPTURING")
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["discovered"], report["decisions"]), (0, 1, {"ADMITTED": 1}))

    def test_interrupted_capture_write_leaves_only_an_ignored_temporary(self):
        self.baseline()
        partial = self.temp / "inbox" / ".xbdh-0123456789abcdef0123456789abcdef.tmp"
        partial.write_bytes(b'{"schema_version": "xb.directlocal.capture.direct_ht')
        self.shopify.register(2001, T0 + timedelta(minutes=1))
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["decisions"]), (0, {"ADMITTED": 1}))
        self.assertTrue(partial.exists())  # younger than the purge threshold, never parsed
        with mock.patch.object(dl, "utc_now", return_value=dl.utc_now() + timedelta(hours=2)):
            self.poll(advance=timedelta(minutes=2))
        self.assertFalse(partial.exists())

    def test_attended_runs_and_captures_are_never_touched(self):
        self.baseline()
        ledger = dl.Ledger(self.temp, clock=self.clock)
        dl.Processor(ledger).begin_run("DISCOVERY", recent=True)  # attended (no origin)
        ledger.close()
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["code"]), (2, "open_run_not_poller_owned"))
        self.assertEqual(self.runs()[-1]["state"], "CAPTURING")
        ledger = dl.Ledger(self.temp, clock=self.clock)
        run_id = ledger.conn.execute("SELECT run_id FROM runs WHERE state = 'CAPTURING'").fetchone()[0]
        dl.Processor(ledger).abandon_run(run_id, confirm_suffix=run_id[-8:])
        ledger.close()
        hook = {"schema_version": dl.CAPTURE_SCHEMA, "provenance": dl.CAPTURE_PROVENANCE, "captured_at": iso(self.clock.now), "session_id": "s",
                "tool_use_id": "toolu_attended_1", "tool_name": dl.CONNECTOR_TOOL, "tool_input": {"query": dl.IDENTITY_QUERY, "variables": {}}, "tool_response": {"data": {"shop": self.shopify.shop}}}
        (self.temp / "inbox" / "toolu_attended_1.json").write_text(json.dumps(hook), encoding="utf-8")
        code, report = self.poll(advance=timedelta(hours=2))
        self.assertEqual((code, report["code"]), (2, "attended_capture_in_inbox"))
        self.assertTrue((self.temp / "inbox" / "toolu_attended_1.json").exists())

    def test_store_binding_and_unsealed_baseline_refuse(self):
        code, report = self.poll()
        self.assertEqual((code, report["code"]), (2, "baseline_not_sealed"))
        self.assertEqual(self.shopify.calls, [])
        code, report = self.poll(store_gid="gid://shopify/Shop/900000002")
        self.assertEqual((code, report["code"]), (2, "poller_store_binding_mismatch"))
        self.baseline()
        code, report = self.poll(baseline=True)
        self.assertEqual((code, report["code"]), (2, "baseline_already_exists"))


def _event_crashing_on(event_type: str):
    original = dl.Ledger.event

    def event(self, conn, kind, **kwargs):
        if kind == event_type:
            raise SimulatedCrash()
        return original(self, conn, kind, **kwargs)

    return event


class NoWriteAndPrivacyTests(PollerCase):
    def test_no_write_processor_cannot_reach_autocount(self):
        ledger = dl.Ledger(self.temp, clock=self.clock)
        try:
            processor = poller.NoWriteProcessor(ledger)
            bridge = dl.BridgeClient(["powershell.exe"], autocount_book="AED_TESTINGAPI")
            for call in (
                lambda: processor.process(self.temp / "inbox" / "x.json", bridge=bridge, enable_write=True),
                lambda: processor.process(self.temp / "inbox" / "x.json", enable_write=True),
                lambda: processor.book_check(bridge),
                lambda: processor.pos_sync_status(bridge),
                lambda: processor.reconcile(self.temp / "inbox" / "x.json", bridge=bridge),
                lambda: processor.approve_write(gid(1), confirm_suffix="0001"),
                lambda: processor.activate_policy(confirm_book="AED_TESTINGAPI", max_creates=1, ttl_hours=1),
                lambda: processor._write(gid(1), {}, bridge),
            ):
                with self.assertRaises(dl.DirectLocalError) as raised:
                    call()
                self.assertEqual(raised.exception.code, "autocount_writer_unreachable_no_write_mode")
        finally:
            ledger.close()

    def test_poller_source_has_no_autocount_write_route(self):
        for name in ("xb_shopify_poller.py", "xb_shopify_direct.py"):
            source = (ROOT / "directlocal" / name).read_text(encoding="utf-8")
            for forbidden in ("enable_write=True", "BridgeClient(", "_bridge_for", "write_once", "default_bridge_command", "subprocess"):
                self.assertNotIn(forbidden, source, (name, forbidden))

    def test_admitted_customers_wait_for_a_future_write_stage(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1))
        for _ in range(3):
            self.poll(advance=timedelta(minutes=2))
        self.assertEqual(self.db("SELECT COUNT(*) AS n FROM fences")[0]["n"], 0)
        self.assertEqual(self.db("SELECT COUNT(*) AS n FROM approvals")[0]["n"], 0)
        self.assertEqual(self.db("SELECT COUNT(*) AS n FROM allocation_attempts")[0]["n"], 0)

    def test_output_log_and_ledger_carry_no_pii_tokens_or_cursors(self):
        self.config_values["auth"] = {"method": "client_credentials", "client_id_env": "XB_SHOPIFY_POLLER_CLIENT_ID", "client_secret_env": "XB_SHOPIFY_POLLER_CLIENT_SECRET"}
        self.baseline()
        for n in range(60):
            self.shopify.register(3000 + n, T0 + timedelta(minutes=1), first=f"Alpha{n}", email=f"alpha{n}@example.test", phone="+65 8123 4567")
        reports = [self.poll(advance=timedelta(minutes=2), max_profile_reads_per_run=50)[1] for _ in range(2)]
        self.shopify.fail_next("recent", http(401, {}))
        self.shopify.fail_next("recent", http(401, {}))
        reports.append(self.poll(advance=timedelta(minutes=2))[1])
        text = json.dumps(reports) + (self.temp / poller.LOG_FILE).read_text(encoding="utf-8") + (self.temp / poller.STATE_FILE).read_text(encoding="utf-8")
        for secret in ("gid://", "Customer/30", "Alpha", "example.test", "81234567", "8123 4567", CLIENT_SECRET, CLIENT_ID, "shpca_", STATIC_TOKEN, "Metafield", *self.shopify.issued_cursors):
            self.assertNotIn(secret, text, secret)
        self.assertTrue(self.shopify.issued_cursors)
        self.assertEqual(reports[-1]["code"], "shopify_auth_rejected")
        ledger = dl.Ledger(self.temp, clock=self.clock)
        try:
            ledger.conn.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            ledger.close()
        blob = b"".join(path.read_bytes() for path in self.temp.glob("ledger.sqlite3*"))
        for value in (b"Alpha", b"example.test", b"81234567", CLIENT_SECRET.encode(), b"shpca_"):
            self.assertNotIn(value, blob)
        # Profile captures are deleted after use; only consumed page captures may remain until purge.
        for path in (self.temp / "inbox").glob("*.json"):
            self.assertNotIn(b"Alpha", path.read_bytes())


class FakeProtector:
    """Stands in for DPAPI off Windows: reversible, entropy-bound, and never
    leaves the plaintext in the blob."""

    name = "fake_protector"

    def __init__(self):
        self.fail_protect = False

    def protect(self, data: bytes, entropy: bytes) -> bytes:
        if self.fail_protect:
            raise direct.ShopifyDirectError("token_cache_unsaved")
        return b"FAKE1" + hashlib.sha256(entropy).digest() + bytes(byte ^ 0x5A for byte in data)

    def unprotect(self, blob: bytes, entropy: bytes) -> bytes:
        if blob[:5] != b"FAKE1" or blob[5:37] != hashlib.sha256(entropy).digest():
            raise direct.ShopifyDirectError("token_cache_unreadable")
        return bytes(byte ^ 0x5A for byte in blob[37:])


class TokenReuseTests(PollerCase):
    def setUp(self):
        super().setUp()
        self.config_values["auth"] = {"method": "client_credentials", "client_id_env": "XB_SHOPIFY_POLLER_CLIENT_ID", "client_secret_env": "XB_SHOPIFY_POLLER_CLIENT_SECRET"}
        self.config_values["token_cache"] = "dpapi_current_user"
        self.protector = FakeProtector()
        self.cache_file = self.temp / poller.TOKEN_CACHE_FILE

    def test_one_exchange_serves_every_two_minute_run_until_expiry(self):
        self.baseline()
        self.assertEqual(self.shopify.issued, 1)
        for _ in range(30):  # one hour of scheduled runs
            code, report = self.poll(advance=timedelta(minutes=2))
            self.assertEqual((code, report["shopify"]["token_exchanges"], report["shopify"]["token_cache_hits"]), (0, 0, 1), report)
        self.assertEqual(self.shopify.issued, 1)
        raw = self.cache_file.read_bytes()
        for secret in (b"shpca_", CLIENT_SECRET.encode(), CLIENT_ID.encode()):
            self.assertNotIn(secret, raw)
        code, report = self.poll(advance=timedelta(hours=23))  # inside the 5-minute refresh margin
        self.assertEqual((report["shopify"]["token_exchanges"], report["shopify"]["token_cache_note"]), (1, "token_cache_expired"))
        self.assertEqual(self.shopify.issued, 2)
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((report["shopify"]["token_exchanges"], self.shopify.issued), (0, 2))

    def test_rejected_cached_token_is_dropped_and_exchanged_once(self):
        self.baseline()
        self.shopify.valid_tokens.clear()  # revoked on Shopify's side
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["shopify"]["auth_refreshes"], report["shopify"]["token_exchanges"]), (0, 1, 1), report)
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((report["shopify"]["token_exchanges"], self.shopify.issued), (0, 2))

    def test_corrupt_or_foreign_cache_is_never_trusted(self):
        self.baseline()
        self.cache_file.write_bytes(b"not a protected blob")
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["shopify"]["token_exchanges"], report["shopify"]["token_cache_note"]), (0, 1, "token_cache_unreadable"))
        self.assertTrue(self.cache_file.read_bytes().startswith(b"FAKE1"))
        cache = direct.TokenCache(self.cache_file, self.protector, shop_domain=DOMAIN, client_id="another-client-id-000001")
        self.assertIsNone(cache.load(self.clock.now))
        self.assertEqual(cache.note, "token_cache_unreadable")
        self.assertFalse(self.cache_file.exists())  # discarded, not reused
        other = direct.TokenCache(self.temp / "x.dpapi", self.protector, shop_domain=DOMAIN, client_id=CLIENT_ID)
        other.save("shpca_synthetic_issued_9999", self.clock.now + timedelta(hours=1), frozenset({"read_customers", "write_customers"}))
        self.assertIsNone(other.load(self.clock.now))  # a cached scope set is re-checked

    def test_unsaved_cache_is_visible_and_never_plaintext(self):
        self.protector.fail_protect = True
        self.baseline()
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["shopify"]["token_exchanges"], report["shopify"]["token_cache_note"]), (0, 1, "token_cache_unsaved"))
        self.assertFalse(self.cache_file.exists())
        self.assertEqual([p.name for p in self.temp.glob(".*.tmp")], [])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_real_dpapi_round_trip_is_bound_to_store_and_client(self):
        protector = direct.DpapiProtector()
        cache = direct.TokenCache(self.cache_file, protector, shop_domain=DOMAIN, client_id=CLIENT_ID)
        expires = self.clock.now + timedelta(hours=20)
        cache.save("shpca_synthetic_issued_0001", expires, frozenset({"read_customers"}))
        self.assertIsNone(cache.note)
        raw = self.cache_file.read_bytes()
        self.assertNotIn(b"shpca_", raw)
        self.assertNotIn(DOMAIN.encode(), raw)
        self.assertEqual(cache.load(self.clock.now), ("shpca_synthetic_issued_0001", expires.replace(microsecond=0)))
        foreign = direct.TokenCache(self.cache_file, protector, shop_domain=DOMAIN, client_id="another-client-id-000001")
        self.assertIsNone(foreign.load(self.clock.now))

    @unittest.skipIf(os.name == "nt", "non-Windows hosts have no DPAPI")
    def test_dpapi_cache_refuses_off_windows(self):
        self.protector = None
        self.baseline_without_cache()
        code, report = self.poll(advance=timedelta(minutes=2))
        self.assertEqual((code, report["code"]), (2, "token_cache_unavailable"))

    def baseline_without_cache(self):
        mode = self.config_values.pop("token_cache")
        try:
            self.baseline()
        finally:
            self.config_values["token_cache"] = mode


# --------------------------------------------------------------------------
# Core compatibility (direct provenance next to the attended hook path)
# --------------------------------------------------------------------------

class CoreCompatibilityTests(PollerCase):
    def direct_record(self, **overrides) -> dict:
        variables = {"first": dl.PAGE_SIZE, "after": None}
        data = {"shop": self.shopify.shop, "customers": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}
        value = {
            "schema_version": dl.DIRECT_CAPTURE_SCHEMA, "provenance": dl.DIRECT_CAPTURE_PROVENANCE, "captured_at": iso(self.clock.now),
            "request_id": "xbdh-" + "a" * 32, "endpoint": {"host": DOMAIN, "api_version": API_VERSION},
            "request": {"query": dl.BASELINE_QUERY, "variables": variables}, "response": {"data": data},
        }
        value.update(overrides)
        return value

    def parse(self, value: dict) -> dl.Capture:
        return dl.parse_capture(json.dumps(value).encode(), mode="synthetic", store_gid=STORE, shop_domain=DOMAIN)

    def test_direct_record_is_strict(self):
        capture = self.parse(self.direct_record())
        self.assertEqual((capture.kind, capture.provenance), ("baseline", dl.DIRECT_CAPTURE_PROVENANCE))
        good = self.direct_record()
        cases = {
            "capture_record_shape_invalid": {"extra": 1},
            "capture_tool_use_id_invalid": {"request_id": "toolu_spoofed"},
            "capture_endpoint_mismatch": {"endpoint": {"host": "other.synthetic.invalid", "api_version": API_VERSION}},
            "capture_endpoint_invalid": {"endpoint": {"host": DOMAIN, "api_version": "unstable"}},
            "query_not_pinned": {"request": {"query": dl.BASELINE_QUERY + " ", "variables": good["request"]["variables"]}},
            "pagination_variables_invalid": {"request": {"query": dl.BASELINE_QUERY, "variables": {"first": 250, "after": None}}},
            "direct_response_shape_invalid": {"response": {"data": good["response"]["data"], "errors": []}},
            "capture_store_identity_mismatch": {"response": {"data": {**good["response"]["data"], "shop": {"id": "gid://shopify/Shop/900000002", "myshopifyDomain": DOMAIN}}}},
        }
        for code, override in cases.items():
            with self.subTest(code=code), self.assertRaises(dl.CaptureRejected) as raised:
                self.parse({**good, **override})
            self.assertEqual(raised.exception.code, code)

    def test_hook_records_cannot_use_the_direct_namespace_or_shape(self):
        hook = {"schema_version": dl.CAPTURE_SCHEMA, "provenance": dl.CAPTURE_PROVENANCE, "captured_at": iso(self.clock.now), "session_id": "s",
                "tool_use_id": "xbdh-" + "b" * 32, "tool_name": dl.CONNECTOR_TOOL, "tool_input": {"query": dl.IDENTITY_QUERY, "variables": {}}, "tool_response": {"data": {"shop": self.shopify.shop}}}
        with self.assertRaises(dl.CaptureRejected) as raised:
            self.parse(hook)
        self.assertEqual(raised.exception.code, "capture_tool_use_id_invalid")
        hook["tool_use_id"] = "toolu_ok_1"
        self.assertEqual(self.parse(hook).provenance, dl.CAPTURE_PROVENANCE)
        # A direct-schema record claiming the hook provenance is a malformed hook record.
        with self.assertRaises(dl.CaptureRejected) as raised:
            self.parse(self.direct_record(provenance=dl.CAPTURE_PROVENANCE))
        self.assertEqual(raised.exception.code, "capture_record_shape_invalid")

    def test_run_origin_binds_page_provenance(self):
        ledger = dl.Ledger(self.temp, clock=self.clock)
        try:
            processor = dl.Processor(ledger)
            with self.assertRaises(dl.DirectLocalError) as raised:
                processor.begin_run("BASELINE", origin="claude_connector")
            self.assertEqual(raised.exception.code, "run_origin_invalid")
            processor.begin_run("BASELINE")  # attended
            self.clock.now += timedelta(seconds=5)
            (self.temp / "inbox" / "direct.json").write_text(json.dumps(self.direct_record(captured_at=iso(self.clock.now))), encoding="utf-8")
            with self.assertRaises(dl.DirectLocalError) as raised:
                processor.ingest_run()
            self.assertEqual(raised.exception.code, "run_not_sealed:run_origin_provenance_mismatch")
            run_id = ledger.conn.execute("SELECT run_id FROM runs WHERE state = 'CAPTURING'").fetchone()[0]
            processor.abandon_run(run_id, confirm_suffix=run_id[-8:])
            self.clock.now += timedelta(seconds=5)
            begun = processor.begin_run("BASELINE", origin=dl.DIRECT_RUN_ORIGIN)
            self.assertEqual(processor.run_origin(ledger.conn, begun["run_id"]), dl.DIRECT_RUN_ORIGIN)
            self.clock.now += timedelta(seconds=5)
            hook_page = {"schema_version": dl.CAPTURE_SCHEMA, "provenance": dl.CAPTURE_PROVENANCE, "captured_at": iso(self.clock.now), "session_id": "s",
                         "tool_use_id": "toolu_page_1", "tool_name": dl.CONNECTOR_TOOL,
                         "tool_input": {"query": dl.BASELINE_QUERY, "variables": {"first": dl.PAGE_SIZE, "after": None}, "first": dl.PAGE_SIZE},
                         "tool_response": {"data": self.direct_record()["response"]["data"]}}
            (self.temp / "inbox" / "toolu_page_1.json").write_text(json.dumps(hook_page), encoding="utf-8")
            with self.assertRaises(dl.DirectLocalError) as raised:
                processor.ingest_run()
            self.assertEqual(raised.exception.code, "run_not_sealed:run_origin_provenance_mismatch")
        finally:
            ledger.close()

    def test_poller_runs_are_hash_chained_and_schema_is_unchanged(self):
        self.baseline()
        self.shopify.register(2001, T0 + timedelta(minutes=1))
        self.poll(advance=timedelta(minutes=2))
        ledger = dl.Ledger(self.temp, clock=self.clock)
        try:
            self.assertEqual(ledger.conn.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertGreater(ledger.verify_events(), 0)
            sealed = [row["code"] for row in ledger.conn.execute("SELECT code FROM events WHERE type LIKE 'run_sealed:%'")]
            self.assertEqual(sealed, [dl.DIRECT_RUN_ORIGIN, dl.DIRECT_RUN_ORIGIN])
            ids = [row[0] for row in ledger.conn.execute("SELECT tool_use_id FROM captures")]
            self.assertTrue(ids and all(dl.DIRECT_REQUEST_ID_RE.fullmatch(value) for value in ids))
        finally:
            ledger.close()


# --------------------------------------------------------------------------
# Task Scheduler template
# --------------------------------------------------------------------------

class SchedulerTemplateTests(unittest.TestCase):
    NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    def setUp(self):
        self.raw = TEMPLATE.read_bytes()
        self.root = ET.fromstring(self.raw)

    def text(self, path: str) -> str:
        return self.root.find(path, self.NS).text

    def test_template_is_disabled_single_instance_two_minute_run_once(self):
        self.raw.decode("ascii")
        self.assertEqual(self.text("t:Settings/t:Enabled"), "false")
        self.assertEqual(self.text("t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(self.text("t:Settings/t:ExecutionTimeLimit"), "PT10M")
        self.assertEqual(self.text("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval"), "PT2M")
        self.assertEqual(self.text("t:Principals/t:Principal/t:RunLevel"), "LeastPrivilege")
        self.assertEqual(self.text("t:Principals/t:Principal/t:LogonType"), "Password")  # S4U cannot use DPAPI
        arguments = self.text("t:Actions/t:Exec/t:Arguments")
        self.assertTrue(arguments.startswith("-I -S -B "), arguments)
        self.assertIn("xb_shopify_poller.py", arguments)
        self.assertTrue(arguments.endswith(" run-once --no-autocount-write"), arguments)
        for forbidden in ("baseline", "xb_directlocal.py", "enable-autocount-write", "process"):
            self.assertNotIn(forbidden, arguments)
        self.assertEqual(len(self.root.findall("t:Actions/t:Exec", self.NS)), 1)
        # Placeholders only: no account, path or secret is baked in.
        for placeholder in ("__XB_ACCOUNT_SID__", "__PYTHON_EXE__", "__RUNTIME_ROOT__", "__POLLER_CONFIG_PATH__"):
            self.assertIn(placeholder.encode(), self.raw)

    def test_execution_limit_exceeds_the_poller_run_budget(self):
        config = poller.parse_config(json.loads(EXAMPLE.read_text(encoding="utf-8")))
        self.assertLess(config.max_run_seconds, 600)


if __name__ == "__main__":
    unittest.main()
