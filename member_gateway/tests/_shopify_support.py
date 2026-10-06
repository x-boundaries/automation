"""Synthetic Shopify M1 fixtures. No real customer, shop, token or key.

Phones use the reserved fictional NANP 555-01xx range, emails use the
reserved ``example.test`` domain, and every secret/key is generated at test
runtime and never written anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

from xb_member_gateway.api import GatewayApp, GatewayService, ShopifyRuntime
from xb_member_gateway.auth import Principal, StaticAuthenticator
from xb_member_gateway.config import GatewayConfig, ShopifyM1Config
from xb_member_gateway.protected_payload import ProtectedPayloadCipher
from xb_member_gateway.shopify_admission import ShopifyAdmissionProcessor
from xb_member_gateway.shopify_webhook import WEBHOOK_ROUTE, ShopifyWebhookReceiver

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
CUTOVER = NOW - timedelta(hours=1)
SHOP = "synthetic-xb-test.myshopify.com"
API_VERSION = "2026-10"
BASELINE_GID = "gid://shopify/Customer/9000000001"
NEW_GID = "gid://shopify/Customer/9000000101"
SECOND_GID = "gid://shopify/Customer/9000000102"
SYNTHETIC_NAME = ("Synthetic", "Shopify Alpha")
SYNTHETIC_PHONE = "+1 555-010-0101"
SYNTHETIC_PHONE_DIGITS = "15550100101"
SYNTHETIC_EMAIL = "Synthetic.Alpha@Example.Test"
SYNTHETIC_EMAIL_NORMALIZED = "synthetic.alpha@example.test"

WORKER_SCOPES = frozenset({
    "worker.claim", "worker.heartbeat", "worker.allocation", "worker.write_intent", "worker.dispatch",
    "worker.writer_register", "worker.writer_termination", "worker.writer_quarantine", "worker.result",
    "worker.reconcile", "job.read",
})


def runtime_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def runtime_secret() -> bytes:
    return os.urandom(32)


def shopify_config(**changes) -> ShopifyM1Config:
    value = {"shopify_enabled": True, "shop_domain": SHOP, "api_version": API_VERSION, "profile_wait_max_checks": 3, "profile_wait_seconds": 60}
    value.update(changes)
    return ShopifyM1Config.from_mapping(value)


def gateway_config(**changes) -> GatewayConfig:
    value = {
        "member_no_max_length": 20, "worker_token_sha256": "0" * 64, "recovery_token_sha256": "1" * 64,
        "production_activation_enabled": True, "kill_switch_enabled": False,
    }
    value.update(changes)
    return GatewayConfig.from_mapping(value)


def customer(gid=NEW_GID, *, tags=("member-mg",), created_at=None, first=SYNTHETIC_NAME[0], last=SYNTHETIC_NAME[1], phone=SYNTHETIC_PHONE, email=SYNTHETIC_EMAIL, start="2026-10-06", expiry="2028-10-05", start_type="date", expiry_type="date"):
    return {
        "id": gid,
        "createdAt": (created_at or NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "tags": list(tags),
        "firstName": first,
        "lastName": last,
        "defaultEmailAddress": None if email is None else {"emailAddress": email},
        "defaultPhoneNumber": None if phone is None else {"phoneNumber": phone},
        "startDate": None if start is None else {"type": start_type, "value": start},
        "expiryDate": None if expiry is None else {"type": expiry_type, "value": expiry},
    }


class FakeReader:
    """Read-only fake of the Admin client; records every GID read."""

    def __init__(self, customers=None, *, error=None):
        self.customers = dict(customers or {})
        self.error = error
        self.reads = []

    def read_customer(self, gid):
        self.reads.append(gid)
        if self.error is not None:
            raise self.error
        return self.customers.get(gid)


def webhook_body(topic, gid):
    numeric = int(gid.rsplit("/", 1)[1])
    if topic == "customer.tags_added":
        body = {"customerId": gid, "tags": ["member-mg"]}
    else:
        body = {
            "id": numeric, "admin_graphql_api_id": gid, "first_name": SYNTHETIC_NAME[0], "last_name": SYNTHETIC_NAME[1],
            "email": SYNTHETIC_EMAIL, "phone": SYNTHETIC_PHONE, "tags": "member-mg",
        }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def signed_headers(secret, raw, *, topic="customers/create", webhook_id="wh-00000000-0001", shop=SHOP, version=API_VERSION, hmac_value=None):
    digest = base64.b64encode(hmac.new(secret, raw, hashlib.sha256).digest()).decode("ascii")
    return {
        "X-Shopify-Hmac-Sha256": digest if hmac_value is None else hmac_value,
        "X-Shopify-Topic": topic,
        "X-Shopify-Shop-Domain": shop,
        "X-Shopify-API-Version": version,
        "X-Shopify-Webhook-Id": webhook_id,
        "X-Shopify-Event-Id": "evt-0001",
        "X-Shopify-Triggered-At": "2026-10-06T11:59:00.000Z",
        "Content-Type": "application/json",
    }


class ShopifyHarness:
    """Wires receiver -> admission -> gateway API over one repository."""

    def __init__(self, repository, *, config=None, gw_config=None, reader=None, baseline=(BASELINE_GID,), enable=True, clock=NOW):
        self.repository = repository
        self.config = config or shopify_config()
        self.secret = runtime_secret()
        self.cipher = ProtectedPayloadCipher.from_binding(self.config.protected_payload_key_id, runtime_key())
        self.receiver = ShopifyWebhookReceiver(self.config, repository, self.secret, clock=clock)
        self.reader = reader or FakeReader()
        self.processor = ShopifyAdmissionProcessor(self.config, repository, self.reader, self.cipher, clock=lambda: self.clock)
        self.clock = clock
        repository.set_control("kill_switch_enabled", False)
        repository.set_control("production_activation_enabled", True)
        self.service = GatewayService(gw_config or gateway_config(), repository, adapter_ready=True, clock=clock, shopify=ShopifyRuntime(self.config, self.cipher))
        self.app = GatewayApp(self.service, StaticAuthenticator({
            "synthetic-worker-token": Principal("synthetic-worker", WORKER_SCOPES),
            "synthetic-operator-token": Principal("synthetic-operator", frozenset({"operator.status.read", "operator.reconciliation.read"})),
            "synthetic-control-token": Principal("synthetic-control", frozenset({"control.kill_switch", "control.activate"})),
            "synthetic-recovery-token": Principal("synthetic-recovery", frozenset({"worker.writer_termination_recovery"})),
        }))
        self.headers = {"Authorization": "Bearer synthetic-worker-token", "X-XB-Worker-Session": "ws-" + "a" * 32}
        self.baseline = None
        if baseline is not None:
            self.baseline = repository.store_shopify_baseline(list(baseline), shop_domain=SHOP, api_version=API_VERSION, capture_started_at=CUTOVER, now=CUTOVER + timedelta(minutes=1))
            if enable:
                repository.enable_shopify_admission(self.baseline.baseline_id, "synthetic-approval-155-g3")

    def set_clock(self, value):
        self.clock = value
        self.service.clock = value
        self.receiver.clock = value

    def deliver(self, gid=NEW_GID, *, topic="customers/create", webhook_id="wh-00000000-0001", **header_changes):
        raw = webhook_body(topic, gid)
        return self.receiver.handle("POST", WEBHOOK_ROUTE, signed_headers(self.secret, raw, topic=topic, webhook_id=webhook_id, **header_changes), raw)

    def call(self, method, path, body=None, headers=None):
        if headers is None:
            headers = {"Authorization": "Bearer synthetic-control-token"} if path.startswith("/v1/control") else (
                {"Authorization": "Bearer synthetic-operator-token"} if path.startswith("/v1/operator") else self.headers
            )
        return self.app.handle(method, path, headers=headers, body={} if body is None else body)

    def admit(self, gid=NEW_GID, profile=None, webhook_id="wh-00000000-0001"):
        self.reader.customers[gid] = profile or customer(gid)
        response = self.deliver(gid, webhook_id=webhook_id)
        assert response.status == 200, response.body
        counts = self.processor.run_once()
        return self.repository.get_shopify_admission(gid), counts

    def claim_through_allocation(self, *, member_no="M000101", precheck=None):
        claimed = self.call("POST", "/v1/worker/claim", {})
        assert claimed.status == 200 and claimed.body["claimed"], claimed.body
        job = claimed.body["job"]
        assert self.call("POST", f"/v1/jobs/{job['job_id']}/precheck", {}).status == 200
        body = precheck or {"outcome": "NO_CANDIDATE", "phone_member_no_hit": False, "mobile_phone_hit": False, "email_hit": False}
        checked = self.call("POST", f"/v1/jobs/{job['job_id']}/shopify/legacy-precheck", body)
        assert checked.status == 200, checked.body
        if checked.body["state"] == "MANUAL_REVIEW":
            return job, None
        candidate = self.call("POST", f"/v1/jobs/{job['job_id']}/allocation/candidate", {})
        assert candidate.status == 200, candidate.body
        probe = self.call("POST", f"/v1/jobs/{job['job_id']}/allocation/probe", {"candidate": member_no, "status": "FREE", "probe_reference": "probe-" + "1" * 32})
        assert probe.status == 200 and probe.body["bound"], probe.body
        return job, probe.body["member_no"]

    def dispatch(self, job, member_no):
        job_id = job["job_id"]
        assert self.call("POST", f"/v1/jobs/{job_id}/allocation/recheck", {"status": "FREE", "probe_reference": "probe-" + "2" * 32}).status == 200
        intent = self.call("POST", f"/v1/jobs/{job_id}/write-intent", {"operation": "member.create", "member_no": member_no, "payload_hash": job["payload_hash"]})
        assert intent.status == 200, intent.body
        fence = self.call("POST", f"/v1/jobs/{job_id}/dispatch-fence", {"operation": "member.create", "member_no": member_no, "host_binding": "host-shopify-test"})
        assert fence.status == 200, fence.body
        writer = {"fence_id": fence.body["dispatch_fence_id"], "attempt": job["attempt"], "execution_id": fence.body["execution_id"], "host_binding": "host-shopify-test", "pid": 4321, "process_start_time": "2026-10-06T12:00:01Z"}
        assert self.call("POST", f"/v1/jobs/{job_id}/writer/register", writer).status == 200
        return fence.body, writer

    def confirm(self, job, writer):
        proof = dict(writer, evidence_type="process_exit", evidence_reference="evidence-shopify", exit_code=0)
        response = self.call("POST", f"/v1/jobs/{job['job_id']}/writer/termination", proof)
        assert response.status == 200, response.body

    def result(self, job, fence, member_no, status, *, found, match, error_code=None):
        return self.call("POST", f"/v1/jobs/{job['job_id']}/result", {
            "schema_version": "xb.member.gateway.result.v1", "job_id": job["job_id"], "operation": "member.create",
            "dispatch_fence_id": fence["dispatch_fence_id"], "status": status, "member_no": member_no,
            "save_invocation_count": 1, "readback_found": found, "readback_match": match, "error_code": error_code,
        })
