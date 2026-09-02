import hashlib
import os
import unittest
from unittest.mock import patch

from xb_member_gateway.auth import (
    AuthenticationError,
    BearerTokenAuthenticator,
)
from xb_member_gateway.config import ConfigError, GatewayConfig


WORKER_TOKEN = "synthetic-worker-secret"
RECOVERY_TOKEN = "synthetic-recovery-secret"
WORKER_ENV = "TEST_XB_MEMBER_GATEWAY_WORKER_TOKEN"
RECOVERY_ENV = "TEST_XB_MEMBER_GATEWAY_RECOVERY_TOKEN"


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def valid_config(**changes):
    value = {
        "member_no_max_length": 20,
        "worker_token_sha256": digest(WORKER_TOKEN),
        "recovery_token_sha256": digest(RECOVERY_TOKEN),
        "worker_token_env": WORKER_ENV,
        "recovery_token_env": RECOVERY_ENV,
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
            {WORKER_ENV: WORKER_TOKEN, RECOVERY_ENV: RECOVERY_TOKEN},
            clear=False,
        ):
            authenticator = BearerTokenAuthenticator.from_environment(config)
            worker = authenticator.authenticate({"Authorization": f"Bearer {WORKER_TOKEN}"})
            recovery = authenticator.authenticate({"Authorization": f"Bearer {RECOVERY_TOKEN}"})

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

    def test_missing_recovery_runtime_credential_fails_closed(self):
        config = valid_config()
        with patch.dict(os.environ, {WORKER_ENV: WORKER_TOKEN}, clear=True):
            authenticator = BearerTokenAuthenticator.from_environment(config)
            with self.assertRaises(AuthenticationError):
                authenticator.authenticate({"Authorization": f"Bearer {WORKER_TOKEN}"})

    def test_missing_recovery_digest_makes_readiness_fail_closed(self):
        config = valid_config(recovery_token_sha256=None)
        self.assertFalse(config.gateway_ready)
        self.assertIn("recovery_credential_digest_required", config.readiness_reasons())

    def test_same_worker_and_recovery_digest_is_rejected(self):
        with self.assertRaisesRegex(
            ConfigError, "worker_recovery_credential_digests_must_differ"
        ):
            valid_config(recovery_token_sha256=digest(WORKER_TOKEN))

    def test_aliased_worker_and_recovery_sources_are_rejected(self):
        with self.assertRaisesRegex(
            ConfigError, "worker_recovery_credential_sources_must_differ"
        ):
            valid_config(recovery_token_env=WORKER_ENV)


if __name__ == "__main__":
    unittest.main()
