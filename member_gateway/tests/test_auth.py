import hashlib
import os
import unittest
from unittest.mock import patch

from xb_member_gateway.auth import (
    AuthenticationError,
    BearerTokenAuthenticator,
)
from xb_member_gateway.config import ConfigError, GatewayConfig


TOKENS = {
    "source": "synthetic-source-secret", "operator": "synthetic-operator-secret",
    "control": "synthetic-control-secret", "worker": "synthetic-worker-secret",
    "recovery": "synthetic-recovery-secret", "mailer": "synthetic-mailer-secret",
}
ENVS = {name: f"TEST_XB_MEMBER_GATEWAY_{name.upper()}_TOKEN" for name in TOKENS}
WORKER_TOKEN = TOKENS["worker"]
RECOVERY_TOKEN = TOKENS["recovery"]
WORKER_ENV = ENVS["worker"]
RECOVERY_ENV = ENVS["recovery"]


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def valid_config(**changes):
    value = {
        "member_no_max_length": 20,
        **{f"{name}_token_sha256": digest(token) for name, token in TOKENS.items()},
        **{f"{name}_token_env": ENVS[name] for name in TOKENS},
        "production_activation_enabled": True,
        "kill_switch_enabled": False,
    }
    value.update(changes)
    return GatewayConfig.from_mapping(value)


class BearerAuthenticatorTests(unittest.TestCase):
    def test_environment_binds_distinct_principals_to_least_privilege_scopes(self):
        config = valid_config()
        with patch.dict(
            os.environ,
            {ENVS[name]: token for name, token in TOKENS.items()},
            clear=False,
        ):
            authenticator = BearerTokenAuthenticator.from_environment(config)
            worker = authenticator.authenticate({"Authorization": f"Bearer {WORKER_TOKEN}"})
            recovery = authenticator.authenticate({"Authorization": f"Bearer {RECOVERY_TOKEN}"})
            source = authenticator.authenticate({"Authorization": f"Bearer {TOKENS['source']}"})
            operator = authenticator.authenticate({"Authorization": f"Bearer {TOKENS['operator']}"})
            control = authenticator.authenticate({"Authorization": f"Bearer {TOKENS['control']}"})
            mailer = authenticator.authenticate({"Authorization": f"Bearer {TOKENS['mailer']}"})

        self.assertEqual(worker.subject, "configured-worker")
        self.assertIn("worker.claim", worker.scopes)
        self.assertIn("worker.dispatch", worker.scopes)
        self.assertIn("worker.result", worker.scopes)
        self.assertNotIn("worker.writer_termination_recovery", worker.scopes)
        self.assertEqual(
            recovery.scopes,
            frozenset({"worker.writer_termination_recovery"}),
        )
        self.assertNotIn("job.read", recovery.scopes)
        self.assertEqual(source.scopes, frozenset({"source.ingest"}))
        self.assertEqual(operator.scopes, frozenset({"operator.status.read", "operator.reconciliation.read"}))
        self.assertEqual(control.scopes, frozenset({"control.kill_switch", "control.activate"}))
        self.assertEqual(mailer.subject, "configured-mailer")
        self.assertEqual(
            mailer.scopes,
            frozenset({"welcome_email.claim", "welcome_email.send_intent", "welcome_email.result"}),
        )
        welcome = {"welcome_email.claim", "welcome_email.send_intent", "welcome_email.result"}
        for other in (worker, recovery, source, operator, control):
            self.assertTrue(welcome.isdisjoint(other.scopes), other.subject)
        for forbidden in (
            "worker.claim", "worker.allocation", "worker.write_intent", "worker.dispatch",
            "worker.result", "worker.reconcile", "job.read", "source.ingest",
            "control.kill_switch", "control.activate", "operator.status.read",
            "operator.reconciliation.read", "worker.writer_termination_recovery",
        ):
            self.assertFalse(mailer.allows(forbidden), forbidden)

    def test_sixth_mailer_principal_is_required_and_distinct(self):
        config = valid_config()
        runtime = {ENVS[name]: token for name, token in TOKENS.items() if name != "mailer"}
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

    def test_missing_recovery_runtime_credential_fails_closed(self):
        config = valid_config()
        runtime = {ENVS[name]: token for name, token in TOKENS.items() if name != "recovery"}
        with patch.dict(os.environ, runtime, clear=True):
            authenticator = BearerTokenAuthenticator.from_environment(config)
            with self.assertRaises(AuthenticationError):
                authenticator.authenticate({"Authorization": f"Bearer {WORKER_TOKEN}"})

    def test_missing_recovery_digest_makes_readiness_fail_closed(self):
        config = valid_config(recovery_token_sha256=None)
        self.assertFalse(config.gateway_ready)
        self.assertIn("recovery_credential_digest_required", config.readiness_reasons())

    def test_same_worker_and_recovery_digest_is_rejected(self):
        with self.assertRaisesRegex(
            ConfigError, "credential_digests_must_differ"
        ):
            valid_config(recovery_token_sha256=digest(WORKER_TOKEN))

    def test_aliased_worker_and_recovery_sources_are_rejected(self):
        with self.assertRaisesRegex(
            ConfigError, "credential_environment_bindings_must_differ"
        ):
            valid_config(recovery_token_env=WORKER_ENV)


if __name__ == "__main__":
    unittest.main()
