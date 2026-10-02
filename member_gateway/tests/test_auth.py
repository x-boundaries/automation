import hashlib
import os
import unittest
from unittest.mock import patch

from xb_member_gateway.auth import (
    PRINCIPAL_COUNT,
    AuthenticationError,
    BearerTokenAuthenticator,
)
from xb_member_gateway.config import ConfigError, GatewayConfig


TOKENS = {
    "source": "synthetic-source-secret", "operator": "synthetic-operator-secret",
    "control": "synthetic-control-secret", "worker": "synthetic-worker-secret",
    "mailer": "synthetic-mailer-secret",
}
ENVS = {name: f"TEST_XB_MEMBER_GATEWAY_{name.upper()}_TOKEN" for name in TOKENS}


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def valid_config(**changes):
    value = {
        "member_book_mode": "production",
        **{f"{name}_token_sha256": digest(token) for name, token in TOKENS.items()},
        **{f"{name}_token_env": ENVS[name] for name in TOKENS},
        "production_activation_enabled": True,
        "kill_switch_enabled": False,
    }
    value.update(changes)
    return GatewayConfig.from_mapping(value)


class BearerAuthenticatorTests(unittest.TestCase):
    def test_environment_binds_five_distinct_least_privilege_principals(self):
        self.assertEqual(PRINCIPAL_COUNT, 5)
        with patch.dict(os.environ, {ENVS[name]: token for name, token in TOKENS.items()}, clear=False):
            authenticator = BearerTokenAuthenticator.from_environment(valid_config())
            found = {name: authenticator.authenticate({"Authorization": f"Bearer {token}"}) for name, token in TOKENS.items()}
        self.assertEqual(found["worker"].scopes, frozenset({"worker.claim", "worker.result"}))
        self.assertEqual(found["source"].scopes, frozenset({"source.ingest"}))
        self.assertEqual(found["operator"].scopes, frozenset({"operator.status.read", "operator.reconciliation.read"}))
        self.assertEqual(found["control"].scopes, frozenset({"control.kill_switch", "control.activate", "control.resolve"}))
        self.assertEqual(found["mailer"].scopes, frozenset({"welcome_email.claim", "welcome_email.send_intent", "welcome_email.result"}))
        self.assertEqual(len({principal.subject for principal in found.values()}), 5)
        removed = {
            "worker.heartbeat", "worker.allocation", "worker.write_intent", "worker.dispatch", "worker.writer_register",
            "worker.writer_termination", "worker.writer_quarantine", "worker.reconcile", "job.read",
            "worker.writer_termination_recovery",
        }
        for principal in found.values():
            self.assertTrue(removed.isdisjoint(principal.scopes), principal.subject)
        welcome = {"welcome_email.claim", "welcome_email.send_intent", "welcome_email.result"}
        for name in ("worker", "source", "operator", "control"):
            self.assertTrue(welcome.isdisjoint(found[name].scopes), name)

    def test_each_principal_binding_is_required_and_distinct(self):
        config = valid_config()
        for missing in TOKENS:
            runtime = {ENVS[name]: token for name, token in TOKENS.items() if name != missing}
            with self.subTest(missing=missing):
                with patch.dict(os.environ, runtime, clear=True):
                    with self.assertRaisesRegex(AuthenticationError, "authentication_binding_missing"):
                        BearerTokenAuthenticator.from_environment(config, strict=True)
        aliased = {ENVS[name]: token for name, token in TOKENS.items()}
        aliased[ENVS["mailer"]] = TOKENS["source"]
        with self.assertRaisesRegex(AuthenticationError, "authentication_binding_aliased"):
            BearerTokenAuthenticator.from_environment(config, aliased, strict=True)
        self.assertIn("mailer_credential_digest_required", valid_config(mailer_token_sha256=None).readiness_reasons())
        with self.assertRaisesRegex(ConfigError, "credential_environment_bindings_must_differ"):
            valid_config(mailer_token_env=ENVS["source"])
        with self.assertRaisesRegex(ConfigError, "credential_digests_must_differ"):
            valid_config(control_token_sha256=digest(TOKENS["worker"]))

    def test_missing_worker_digest_makes_readiness_fail_closed(self):
        config = valid_config(worker_token_sha256=None)
        self.assertFalse(config.gateway_ready)
        self.assertIn("worker_credential_digest_required", config.readiness_reasons())

    def test_recovery_principal_is_a_configuration_error(self):
        with self.assertRaisesRegex(ConfigError, "recovery_principal_removed"):
            valid_config(recovery_token_sha256=digest("synthetic-recovery-secret"))
        with self.assertRaisesRegex(ConfigError, "recovery_principal_removed"):
            valid_config(recovery_token_env="TEST_XB_MEMBER_GATEWAY_RECOVERY_TOKEN")

    def test_non_strict_mismatch_denies_everything(self):
        runtime = {ENVS[name]: token for name, token in TOKENS.items()}
        runtime[ENVS["worker"]] = "synthetic-other-secret"
        authenticator = BearerTokenAuthenticator.from_environment(valid_config(), runtime)
        with self.assertRaises(AuthenticationError):
            authenticator.authenticate({"Authorization": f"Bearer {TOKENS['source']}"})


if __name__ == "__main__":
    unittest.main()
