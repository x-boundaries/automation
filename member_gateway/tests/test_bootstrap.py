import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from xb_member_gateway.bootstrap import BootstrapError, compose_gateway, run
from xb_member_gateway.repository import RepositoryError


TOKENS = {name: f"synthetic-{name}-bearer" for name in ("source", "operator", "control", "worker", "recovery")}


def config_value():
    return {
        "schema_version": "xb.member.gateway.config.v2",
        "environment": "production", "expected_environment": "production",
        "production_activation_enabled": False, "kill_switch_enabled": True,
        "autocount_adapter_ready": True,
        "allowed_form_aliases": ["member_registration"],
        "allowed_mapping_versions": ["member-intake.v1"],
        "source_form_id": "synthetic-form",
        "source_question_ids": {name: f"synthetic-{name}" for name in ("name", "phone", "email", "birthday_month", "marketing_consent", "pdpa_acknowledged")},
        "source_cutover_watermark": "2026-09-15T00:00:00Z",
        "initial_source_window_max": 1, "member_no_max_length": 20,
        "lease_seconds": 600, "heartbeat_seconds": 120, "execution_deadline_seconds": 300,
        "max_attempts": 3, "worker_concurrency": 1, "claim_size": 1,
        **{f"{name}_token_sha256": hashlib.sha256(token.encode()).hexdigest() for name, token in TOKENS.items()},
        "postgres_dsn_env": "TEST_DATABASE_URL", "bind_address_env": "TEST_BIND_ADDRESS",
        "bind_port_env": "TEST_BIND_PORT", "reference_hmac_key_env": "TEST_REFERENCE_KEY",
        **{f"{name}_token_env": f"TEST_{name.upper()}_TOKEN" for name in TOKENS},
    }


def runtime():
    return {
        "TEST_DATABASE_URL": "postgresql://synthetic.invalid/db", "TEST_BIND_ADDRESS": "127.0.0.1",
        "TEST_BIND_PORT": "8443", "TEST_REFERENCE_KEY": "synthetic-reference-only-key",
        **{f"TEST_{name.upper()}_TOKEN": token for name, token in TOKENS.items()},
    }


class FakeRepository:
    def __init__(self, *, dsn, reference_key):
        self.dsn_present = bool(dsn)
        self.reference_key_present = bool(reference_key)
        self.verified = False

    def verify_bootstrap_readiness(self, config):
        self.verified = True

    def get_control(self):
        return {"production_activation_enabled": False, "kill_switch_enabled": True}


class BootstrapTests(unittest.TestCase):
    def write_config(self, value=None):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "gateway.json"
        path.write_text(json.dumps(value or config_value()), encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return str(path)

    def test_valid_synthetic_composition_verifies_before_listen(self):
        calls = []
        def repository_factory(**kwargs):
            calls.append("repository")
            return FakeRepository(**kwargs)
        composition = compose_gateway(self.write_config(), environment=runtime(), repository_factory=repository_factory)
        self.assertTrue(composition.repository.verified)
        self.assertEqual((composition.bind_address, composition.bind_port), ("127.0.0.1", 8443))
        run(self.write_config(), environment=runtime(), repository_factory=repository_factory, serve_gateway=lambda app, host, port: calls.append("listen"))
        self.assertEqual(calls[-1], "listen")

    def test_every_required_runtime_binding_fails_before_repository_construction(self):
        for name in runtime():
            with self.subTest(name=name):
                values = runtime()
                secret = values.pop(name)
                called = []
                with self.assertRaises(BootstrapError) as caught:
                    compose_gateway(self.write_config(), environment=values, repository_factory=lambda **kwargs: called.append(kwargs))
                self.assertFalse(called)
                self.assertNotIn(secret, str(caught.exception))

    def test_readiness_mismatch_fails_before_listen_without_secret_output(self):
        class Rejected(FakeRepository):
            def verify_bootstrap_readiness(self, config):
                raise RepositoryError("required_migrations_missing")
        with self.assertRaisesRegex(BootstrapError, "required_migrations_missing"):
            compose_gateway(self.write_config(), environment=runtime(), repository_factory=Rejected)

    def test_missing_watermark_and_aliased_principals_fail_closed(self):
        value = config_value()
        value["source_cutover_watermark"] = None
        with self.assertRaisesRegex(BootstrapError, "source_cutover_watermark_required"):
            compose_gateway(self.write_config(value), environment=runtime(), repository_factory=FakeRepository)
        values = runtime()
        values["TEST_OPERATOR_TOKEN"] = values["TEST_SOURCE_TOKEN"]
        with self.assertRaisesRegex(BootstrapError, "authentication_binding_aliased"):
            compose_gateway(self.write_config(), environment=values, repository_factory=FakeRepository)

    def test_missing_required_config_field_fails_closed(self):
        for field in ("postgres_dsn_env", "source_token_env", "source_token_sha256", "source_cutover_watermark", "source_question_ids", "initial_source_window_max"):
            with self.subTest(field=field):
                value = config_value()
                value.pop(field)
                with self.assertRaisesRegex(BootstrapError, "required_config_fields_missing"):
                    compose_gateway(self.write_config(value), environment=runtime(), repository_factory=FakeRepository)

    def test_reference_hmac_cannot_alias_bearer(self):
        values = runtime()
        values["TEST_REFERENCE_KEY"] = values["TEST_SOURCE_TOKEN"]
        with self.assertRaisesRegex(BootstrapError, "reference_hmac_must_not_authenticate"):
            compose_gateway(self.write_config(), environment=values, repository_factory=FakeRepository)

    def test_unexpected_driver_failure_is_bounded(self):
        marker = "postgresql://must-not-escape.invalid/private"
        class Broken(FakeRepository):
            def verify_bootstrap_readiness(self, config):
                raise RuntimeError(marker)
        with self.assertRaises(BootstrapError) as caught:
            compose_gateway(self.write_config(), environment=runtime(), repository_factory=Broken)
        self.assertEqual(str(caught.exception), "bootstrap_rejected")
        self.assertNotIn(marker, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
