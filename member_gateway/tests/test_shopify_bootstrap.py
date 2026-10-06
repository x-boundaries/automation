"""Shopify M1 config, gateway composition and dedicated receiver composition."""

import json
import tempfile
import unittest
from pathlib import Path

from xb_member_gateway.bootstrap import BootstrapError, compose_gateway
from xb_member_gateway.config import ConfigError, ShopifyM1Config, load_shopify_config
from xb_member_gateway.repository import InMemoryRepository
from xb_member_gateway.shopify_receiver import capture_baseline, compose_receiver

try:
    from .test_bootstrap import TOKENS, FakeRepository, config_value, runtime
    from ._shopify_support import BASELINE_GID, NEW_GID, SHOP, runtime_key
except ImportError:  # discovered as a top-level module
    from test_bootstrap import TOKENS, FakeRepository, config_value, runtime  # type: ignore
    from _shopify_support import BASELINE_GID, NEW_GID, SHOP, runtime_key  # type: ignore

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config/member_shopify_m1.production.example.json"


def shopify_value(**changes):
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    value.update({"shopify_enabled": True, "shop_domain": SHOP})
    value.update(changes)
    return value


class MemoryRepositoryFactory:
    def __init__(self):
        self.repository = None

    def __call__(self, *, dsn, reference_key):
        self.repository = InMemoryRepository(reference_key=reference_key)
        self.repository.verify_bootstrap_readiness = lambda config: None
        return self.repository


class FakeClient:
    def __init__(self, config, token):
        self.token_present = bool(token)
        self.pages = [{"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [{"id": BASELINE_GID, "tags": ["member-mg"]}]},
                      {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": NEW_GID, "tags": []}]}]

    def baseline_page(self, after):
        return self.pages.pop(0)

    def read_customer(self, gid):
        return None


class ShopifyConfigTests(unittest.TestCase):
    def test_example_is_closed_disabled_and_secret_free(self):
        raw = EXAMPLE.read_text(encoding="utf-8")
        config = load_shopify_config(EXAMPLE)
        self.assertFalse(config.shopify_enabled)
        self.assertIsNone(config.shop_domain)
        self.assertEqual(config.webhook_topics, ("customers/create", "customers/update", "customer.tags_added"))
        self.assertIn("shopify_disabled", config.readiness_reasons())
        for forbidden in ("shpss_", "shpat_", "myshopify.com\"", "-----BEGIN"):
            self.assertNotIn(forbidden, raw)

    def test_closed_validation(self):
        for changes, code in (
            ({"unknown": 1}, "shopify_unknown_config_fields"),
            ({"webhook_topics": ["orders/create"]}, "shopify_webhook_topics_invalid"),
            ({"shop_domain": "evil.example.com"}, "shopify_shop_domain_invalid"),
            ({"api_version": "latest"}, "shopify_api_version_invalid"),
            ({"max_member_no_candidates": 50}, "shopify_max_member_no_candidates_invalid"),
            ({"admin_token_env": "XB_SHOPIFY_WEBHOOK_SECRET"}, "shopify_environment_bindings_must_differ"),
        ):
            with self.assertRaisesRegex(ConfigError, code):
                ShopifyM1Config.from_mapping(shopify_value(**changes), require_complete=True)
        value = shopify_value()
        value.pop("api_version")
        with self.assertRaisesRegex(ConfigError, "shopify_required_config_fields_missing"):
            ShopifyM1Config.from_mapping(value, require_complete=True)


class CompositionTests(unittest.TestCase):
    def write(self, name, value):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def test_gateway_without_shopify_config_is_unchanged(self):
        composition = compose_gateway(self.write("gw.json", config_value()), environment=runtime(), repository_factory=FakeRepository)
        self.assertIsNone(composition.app.service.shopify)

    def test_gateway_binds_protected_key_only_from_runtime_and_fails_closed(self):
        gw = self.write("gw.json", config_value())
        shop = self.write("shop.json", shopify_value())
        env = dict(runtime(), XB_MEMBER_GATEWAY_PROTECTED_PAYLOAD_KEY=runtime_key())
        composition = compose_gateway(gw, environment=env, repository_factory=FakeRepository, shopify_config_path=shop)
        self.assertTrue(composition.app.service.shopify_enabled)
        self.assertNotIn(env["XB_MEMBER_GATEWAY_PROTECTED_PAYLOAD_KEY"], repr(composition.app.service.shopify))
        for broken, code in (
            ({k: v for k, v in env.items() if k != "XB_MEMBER_GATEWAY_PROTECTED_PAYLOAD_KEY"}, "protected_payload_key_binding_missing"),
            (dict(env, XB_MEMBER_GATEWAY_PROTECTED_PAYLOAD_KEY="short"), "protected_payload_key_invalid"),
            (dict(env, XB_MEMBER_GATEWAY_PROTECTED_PAYLOAD_KEY=TOKENS["worker"]), "protected_payload_key_must_be_distinct"),
        ):
            with self.assertRaises(BootstrapError) as raised:
                compose_gateway(gw, environment=broken, repository_factory=FakeRepository, shopify_config_path=shop)
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn(env["XB_MEMBER_GATEWAY_PROTECTED_PAYLOAD_KEY"], raised.exception.code)

    def receiver_env(self):
        return dict(
            runtime(), XB_MEMBER_GATEWAY_PROTECTED_PAYLOAD_KEY=runtime_key(),
            XB_SHOPIFY_WEBHOOK_SECRET="synthetic-webhook-secret-0001", XB_SHOPIFY_ADMIN_TOKEN="synthetic-admin-token-0001",
            XB_SHOPIFY_RECEIVER_BIND_ADDRESS="10.0.0.5", XB_SHOPIFY_RECEIVER_BIND_PORT="8444",
        )

    def test_receiver_composes_privately_and_capture_seals_baseline(self):
        factory = MemoryRepositoryFactory()
        composition = compose_receiver(self.write("gw.json", config_value()), self.write("shop.json", shopify_value()), environment=self.receiver_env(), repository_factory=factory, client_factory=FakeClient)
        self.assertEqual((composition.bind_address, composition.bind_port), ("10.0.0.5", 8444))
        sealed = capture_baseline(composition)
        self.assertEqual((sealed["member_count"], sealed["state"]), (1, "SEALED"))
        self.assertTrue(factory.repository.verify_shopify_baseline(sealed["baseline_id"]))
        self.assertEqual(set(sealed), {"baseline_id", "member_count", "member_digest", "state"})

    def test_receiver_rejects_public_bind_shared_secrets_and_disabled_config(self):
        gw = self.write("gw.json", config_value())
        shop = self.write("shop.json", shopify_value())
        for changes, code in (
            ({"XB_SHOPIFY_RECEIVER_BIND_ADDRESS": "8.8.8.8"}, "bind_address_not_private"),
            ({"XB_SHOPIFY_ADMIN_TOKEN": "synthetic-webhook-secret-0001"}, "shopify_secret_bindings_invalid"),
            ({"XB_SHOPIFY_WEBHOOK_SECRET": ""}, "shopify_webhook_secret_binding_missing"),
        ):
            with self.assertRaises(BootstrapError) as raised:
                compose_receiver(gw, shop, environment=dict(self.receiver_env(), **changes), repository_factory=MemoryRepositoryFactory(), client_factory=FakeClient)
            self.assertEqual(raised.exception.code, code)
        with self.assertRaises(BootstrapError) as raised:
            compose_receiver(gw, self.write("off.json", shopify_value(shopify_enabled=False)), environment=self.receiver_env(), repository_factory=MemoryRepositoryFactory(), client_factory=FakeClient)
        self.assertEqual(raised.exception.code, "shopify_disabled")


if __name__ == "__main__":
    unittest.main()


def _conforms(test, schema, value, path="$"):
    """Small closed-schema checker: types, const/enum, pattern, required, closed keys."""
    import re as _re

    if "oneOf" in schema:
        errors = 0
        for option in schema["oneOf"]:
            try:
                _conforms(test, option, value, path)
            except AssertionError:
                errors += 1
        test.assertEqual(errors, len(schema["oneOf"]) - 1, path)
        return
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        mapping = {"object": dict, "string": str, "integer": int, "boolean": bool, "null": type(None)}
        test.assertTrue(any(isinstance(value, mapping[item]) and not (item == "integer" and isinstance(value, bool)) for item in allowed), path)
    if "const" in schema:
        test.assertEqual(value, schema["const"], path)
    if "enum" in schema:
        test.assertIn(value, schema["enum"], path)
    if isinstance(value, str) and "pattern" in schema:
        test.assertRegex(value, _re.compile(schema["pattern"]), path)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            test.assertIn(key, value, f"{path}.{key}")
        properties = schema.get("properties")
        if properties is not None:
            test.assertTrue(set(value) <= set(properties), f"{path} extra {set(value) - set(properties)}")
            for key, item in value.items():
                _conforms(test, properties[key], item, f"{path}.{key}")
        elif isinstance(schema.get("additionalProperties"), dict):
            for key, item in value.items():
                _conforms(test, schema["additionalProperties"], item, f"{path}.{key}")


class ShopifySchemaContractTests(unittest.TestCase):
    def schema(self, name):
        return json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))

    def test_real_outputs_conform_to_closed_contracts(self):
        try:
            from ._shopify_support import ShopifyHarness
        except ImportError:
            from _shopify_support import ShopifyHarness  # type: ignore
        harness = ShopifyHarness(InMemoryRepository())
        harness.admit()
        claimed = harness.call("POST", "/v1/worker/claim", {}).body["job"]
        _conforms(self, self.schema("member_gateway_job.v3"), claimed)
        _conforms(self, self.schema("member_gateway_job.v3"), harness.call("GET", f"/v1/jobs/{claimed['job_id']}/status").body)
        _conforms(self, self.schema("member_gateway_shopify_operator_status.v1"), harness.call("GET", "/v1/operator/shopify-status").body)
        _conforms(self, self.schema("member_gateway_shopify_reconcile_claim.v1"), harness.call("POST", "/v1/worker/reconcile/claim", {}).body)
        _conforms(self, self.schema("member_gateway_shopify_legacy_precheck.v1"), {"outcome": "NO_CANDIDATE", "phone_member_no_hit": False, "mobile_phone_hit": False, "email_hit": False})
        with self.assertRaises(AssertionError):
            _conforms(self, self.schema("member_gateway_job.v3"), dict(claimed, member_payload={}))
