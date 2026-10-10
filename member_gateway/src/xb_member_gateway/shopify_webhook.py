"""Dedicated, provider-neutral Shopify HTTPS webhook receiver boundary.

Order of operations is the security contract:

1. one bounded application route, exact raw bytes captured before anything else;
2. ``X-Shopify-Hmac-Sha256`` verified in constant time over those exact bytes
   with the app client secret; an absent/invalid HMAC returns 401 having
   touched no repository and parsed nothing;
3. only then are the delivery headers allowlisted (topic, shop, API version)
   and the body parsed solely to extract the Shopify Customer GID;
4. only bounded delivery metadata and the GID are persisted, deduplicated on
   ``X-Shopify-Webhook-Id``; the raw body and every profile value are dropped.

The authoritative profile is re-read asynchronously by the admission
processor; webhook payload contents are never trusted for eligibility. The
public edge (hostname, TLS, tunnel) is deployment binding, not product code.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from .config import ShopifyM1Config
from .models import ShopifyWebhookReceipt

WEBHOOK_ROUTE = "/v1/shopify/webhooks/customers"
MAX_BODY_BYTES = 262_144
GID_RE = re.compile(r"^gid://shopify/Customer/[1-9][0-9]{0,19}$")
_WEBHOOK_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,100}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,100}$")
_TRIGGERED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")


class ShopifyWebhookError(RuntimeError):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    found = [str(value) for key, value in headers.items() if str(key).lower() == wanted]
    if len(found) > 1:
        raise ShopifyWebhookError(400, "shopify_header_duplicated")
    return found[0] if found else None


def verify_shopify_hmac(raw_body: bytes, supplied: str | None, secret: bytes) -> bool:
    """Constant-time base64(HMAC-SHA256(secret, raw_body)) comparison."""

    if not isinstance(raw_body, (bytes, bytearray)) or not secret or not isinstance(supplied, str):
        return False
    try:
        supplied_digest = base64.b64decode(supplied.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    expected = hmac.new(secret, bytes(raw_body), hashlib.sha256).digest()
    return len(supplied_digest) == len(expected) and hmac.compare_digest(expected, supplied_digest)


def _extract_gid(topic: str, body: Any) -> str:
    if not isinstance(body, Mapping):
        raise ShopifyWebhookError(400, "shopify_payload_invalid")
    if topic == "customer.tags_added":
        gid = body.get("customerId")
    else:
        gid = body.get("admin_graphql_api_id")
        numeric = body.get("id")
        if isinstance(gid, str) and numeric is not None:
            if isinstance(numeric, bool) or not isinstance(numeric, int) or gid != f"gid://shopify/Customer/{numeric}":
                raise ShopifyWebhookError(400, "shopify_customer_identity_inconsistent")
    if not isinstance(gid, str) or not GID_RE.fullmatch(gid):
        raise ShopifyWebhookError(400, "shopify_customer_gid_invalid")
    return gid


def parse_verified_delivery(config: ShopifyM1Config, headers: Mapping[str, str], raw_body: bytes) -> ShopifyWebhookReceipt:
    """Called only after HMAC verification succeeded."""

    topic = _header(headers, "X-Shopify-Topic")
    if topic not in config.webhook_topics:
        raise ShopifyWebhookError(422, "shopify_topic_not_allowlisted")
    if config.shop_domain is None or _header(headers, "X-Shopify-Shop-Domain") != config.shop_domain:
        raise ShopifyWebhookError(422, "shopify_shop_not_allowlisted")
    if _header(headers, "X-Shopify-API-Version") != config.api_version:
        raise ShopifyWebhookError(422, "shopify_api_version_not_allowlisted")
    webhook_id = _header(headers, "X-Shopify-Webhook-Id")
    if webhook_id is None or not _WEBHOOK_ID_RE.fullmatch(webhook_id):
        raise ShopifyWebhookError(400, "shopify_webhook_id_invalid")
    event_id = _header(headers, "X-Shopify-Event-Id")
    if event_id is not None and not _EVENT_ID_RE.fullmatch(event_id):
        raise ShopifyWebhookError(400, "shopify_event_id_invalid")
    triggered_at = _header(headers, "X-Shopify-Triggered-At")
    if triggered_at is not None and not _TRIGGERED_AT_RE.fullmatch(triggered_at):
        raise ShopifyWebhookError(400, "shopify_triggered_at_invalid")
    try:
        body = json.loads(bytes(raw_body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShopifyWebhookError(400, "shopify_payload_invalid") from exc
    gid = _extract_gid(topic, body)
    del body  # profile values are never retained past GID extraction
    return ShopifyWebhookReceipt(webhook_id, topic, config.shop_domain, config.api_version, event_id, triggered_at, gid)


@dataclass(frozen=True, slots=True)
class ReceiverResponse:
    status: int
    body: dict[str, Any]


class ShopifyWebhookReceiver:
    """Route + HMAC + dedup boundary. ``repository`` needs ``record_shopify_webhook``."""

    def __init__(self, config: ShopifyM1Config, repository: Any, secret: bytes, *, clock=None):
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 16:
            raise ValueError("shopify_webhook_secret_invalid")
        self.config = config
        self.repository = repository
        self._secret = bytes(secret)
        self.clock = clock

    @staticmethod
    def _error(status: int, code: str) -> ReceiverResponse:
        return ReceiverResponse(status, {"error_code": code, "trace_id": f"trace-{uuid.uuid4().hex}"})

    def handle(self, method: str, path: str, headers: Mapping[str, str], raw_body: bytes) -> ReceiverResponse:
        if method == "GET" and path == "/livez":
            return ReceiverResponse(200, {"status": "ok", "service": "xb-shopify-webhook-receiver"})
        if method != "POST" or path != WEBHOOK_ROUTE:
            return self._error(404, "route_not_found")
        if not self.config.shopify_enabled:
            return self._error(503, "shopify_receiver_disabled")
        if not isinstance(raw_body, (bytes, bytearray)) or len(raw_body) > MAX_BODY_BYTES:
            return self._error(413, "request_body_too_large")
        try:
            supplied = _header(headers, "X-Shopify-Hmac-Sha256")
        except ShopifyWebhookError as exc:
            return self._error(401, "shopify_hmac_invalid")
        if not verify_shopify_hmac(raw_body, supplied, self._secret):
            # Nothing parsed, nothing persisted, nothing echoed.
            return self._error(401, "shopify_hmac_invalid")
        try:
            receipt = parse_verified_delivery(self.config, headers, raw_body)
            replayed = self.repository.record_shopify_webhook(receipt, now=self.clock)
        except ShopifyWebhookError as exc:
            return self._error(exc.status, exc.code)
        except Exception as exc:  # repository conflicts/availability are bounded
            code = getattr(exc, "code", None) or str(exc)
            if code == "shopify_webhook_identity_conflict":
                return self._error(409, code)
            return self._error(503, "shopify_receipt_unavailable")
        return ReceiverResponse(200, {"status": "accepted", "replayed": bool(replayed)})


class _ReceiverHandler(BaseHTTPRequestHandler):
    receiver: ShopifyWebhookReceiver

    def _serve(self) -> None:
        if self.headers.get("Transfer-Encoding") is not None:
            response = ShopifyWebhookReceiver._error(411, "content_length_required")
        else:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                response = ShopifyWebhookReceiver._error(413, "request_body_too_large")
            else:
                raw = self.rfile.read(length) if length else b""
                response = self.receiver.handle(self.command, self.path, dict(self.headers), raw)
        encoded = json.dumps(response.body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve_receiver(receiver: ShopifyWebhookReceiver, host: str, port: int) -> None:
    handler = type("ShopifyReceiverHandler", (_ReceiverHandler,), {"receiver": receiver})
    with ThreadingHTTPServer((host, port), handler) as server:
        server.serve_forever()
