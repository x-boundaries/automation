import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from xb_member_gateway.bootstrap import BootstrapError, compose_gateway, run
from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.eligibility import EligibilityContext, evaluate_eligibility
from xb_member_gateway.models import JobRecord
from xb_member_gateway.repository import RepositoryError


TOKENS = {name: f"synthetic-{name}-bearer" for name in ("source", "operator", "control", "worker", "recovery", "mailer")}


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
        "source_production_cutover_exact": "2026-09-15T00:00:00.000Z",
        "source_admission_mode": "first_member",
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

    def test_missing_cutover_mode_or_sixth_principal_fails_closed(self):
        value = config_value()
        value["source_production_cutover_exact"] = None
        with self.assertRaisesRegex(BootstrapError, "source_production_cutover_exact_required"):
            compose_gateway(self.write_config(value), environment=runtime(), repository_factory=FakeRepository)
        value = config_value()
        value["mailer_token_sha256"] = None
        with self.assertRaisesRegex(BootstrapError, "mailer_credential_digest_required"):
            compose_gateway(self.write_config(value), environment=runtime(), repository_factory=FakeRepository)
        value = config_value()
        value["source_admission_mode"] = "unbounded"
        with self.assertRaisesRegex(BootstrapError, "source_admission_mode_invalid"):
            compose_gateway(self.write_config(value), environment=runtime(), repository_factory=FakeRepository)
        values = runtime()
        values["TEST_MAILER_TOKEN"] = values["TEST_WORKER_TOKEN"]
        with self.assertRaisesRegex(BootstrapError, "authentication_binding_aliased"):
            compose_gateway(self.write_config(), environment=values, repository_factory=FakeRepository)

    def test_missing_required_config_field_fails_closed(self):
        for field in ("postgres_dsn_env", "source_token_env", "source_token_sha256", "source_cutover_watermark", "source_production_cutover_exact", "source_admission_mode", "mailer_token_sha256", "mailer_token_env", "source_question_ids", "initial_source_window_max"):
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


PRIVATE_BACKEND_ADDRESS = "10.24.0.12"

ACCEPTED_BIND_ADDRESSES = (
    PRIVATE_BACKEND_ADDRESS,
    "172.20.5.9",
    "192.168.40.7",
    "127.0.0.1",
    "fd00:155::12",
)

REJECTED_BIND_ADDRESSES = (
    ("0.0.0.0", "bind_address_unspecified"),
    ("::", "bind_address_unspecified"),
    ("::ffff:0.0.0.0", "bind_address_unspecified"),
    ("8.8.8.8", "bind_address_not_private"),
    ("2001:4860:4860::8888", "bind_address_not_private"),
    ("224.0.0.1", "bind_address_not_private"),
    ("240.0.0.1", "bind_address_not_private"),
    ("not-an-ip", "bind_address_invalid"),
    ("gateway.backend.invalid", "bind_address_invalid"),
    ("10.24.0.12:8443", "bind_address_invalid"),
    ("*", "bind_address_invalid"),
)


def dark_config_value():
    """Locked dark posture: activation false, kill switch on, adapter not ready."""

    value = config_value()
    value["autocount_adapter_ready"] = False
    return value


def dark_runtime(bind_address=PRIVATE_BACKEND_ADDRESS):
    values = runtime()
    values["TEST_BIND_ADDRESS"] = bind_address
    return values


def dark_job_record():
    return JobRecord(
        job_id="job-dark-0001",
        request_id="req-dark-0001",
        source_response_ref="source-ref-dark-0001",
        response_id="response-dark-0001",
        payload_hash="0" * 64,
        operation="member.create",
        member_payload={
            "name": "Synthetic Dark Fixture",
            "phone": "600000000",
            "email": "synthetic-dark@example.invalid",
            "birthday_month": "January",
            "marketing_consent": "No",
            "pdpa_acknowledged": True,
        },
        created_at="2026-09-17T00:00:00Z",
    )


class DarkStartCompositionTests(unittest.TestCase):
    """Adapter-not-ready may compose; it must never look ready or dispatchable."""

    def write_config(self, value=None):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "gateway.json"
        path.write_text(json.dumps(value or dark_config_value()), encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return str(path)

    def test_dark_composition_succeeds_and_still_verifies_repository_readiness(self):
        composition = compose_gateway(
            self.write_config(), environment=dark_runtime(), repository_factory=FakeRepository
        )
        self.assertTrue(composition.repository.verified)
        self.assertFalse(composition.config.autocount_adapter_ready)
        self.assertFalse(composition.config.production_activation_enabled)
        self.assertTrue(composition.config.kill_switch_enabled)
        self.assertEqual(
            (composition.bind_address, composition.bind_port), (PRIVATE_BACKEND_ADDRESS, 8443)
        )

    def test_dark_composition_listens_only_after_repository_verification(self):
        order = []

        def repository_factory(**kwargs):
            order.append("repository")
            return FakeRepository(**kwargs)

        run(
            self.write_config(),
            environment=dark_runtime(),
            repository_factory=repository_factory,
            serve_gateway=lambda app, host, port: order.append(("listen", host, port)),
        )
        self.assertEqual(order, ["repository", ("listen", PRIVATE_BACKEND_ADDRESS, 8443)])

    def test_repository_bootstrap_readiness_failure_still_blocks_dark_composition(self):
        class Rejected(FakeRepository):
            def verify_bootstrap_readiness(self, config):
                raise RepositoryError("required_migrations_missing")

        with self.assertRaisesRegex(BootstrapError, "required_migrations_missing"):
            compose_gateway(
                self.write_config(), environment=dark_runtime(), repository_factory=Rejected
            )

    def test_dark_gateway_reports_not_ready_with_adapter_reason(self):
        composition = compose_gateway(
            self.write_config(), environment=dark_runtime(), repository_factory=FakeRepository
        )
        service = composition.app.service
        self.assertFalse(service.adapter_ready)
        readiness = service.readiness()
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "not_ready")
        self.assertIn("autocount_adapter_not_ready", readiness["reasons"])

    def test_readiness_reasons_still_report_adapter_not_ready(self):
        config = GatewayConfig.from_mapping(dark_config_value(), require_complete=True)
        self.assertIn("autocount_adapter_not_ready", config.readiness_reasons())
        self.assertFalse(config.gateway_ready)

    def test_dispatch_remains_ineligible_while_adapter_is_not_ready(self):
        config = GatewayConfig.from_mapping(dark_config_value(), require_complete=True)
        job = dark_job_record()
        blocked = evaluate_eligibility(
            EligibilityContext(config=config, job=job, autocount_adapter_ready=False)
        )
        self.assertFalse(blocked.eligible)
        self.assertIn("autocount_adapter_ready", blocked.reasons)
        self.assertFalse(blocked.predicates["autocount_adapter_ready"])
        ready_adapter = evaluate_eligibility(
            EligibilityContext(config=config, job=job, autocount_adapter_ready=True)
        )
        self.assertNotIn("autocount_adapter_ready", ready_adapter.reasons)

    def test_adapter_exemption_does_not_suppress_other_readiness_failures(self):
        cases = (
            ("source_cutover_watermark", None, "source_cutover_watermark_required"),
            ("source_form_id", None, "source_form_id_required"),
            ("recovery_token_sha256", None, "recovery_credential_digest_required"),
            ("mailer_token_sha256", None, "mailer_credential_digest_required"),
            ("source_production_cutover_exact", None, "source_production_cutover_exact_required"),
            ("environment", "staging", "environment_mismatch"),
            ("member_no_max_length", 21, "member_no_max_length_must_be_twenty"),
        )
        for field, replacement, expected in cases:
            with self.subTest(field=field):
                value = dark_config_value()
                value[field] = replacement
                with self.assertRaisesRegex(BootstrapError, expected):
                    compose_gateway(
                        self.write_config(value),
                        environment=dark_runtime(),
                        repository_factory=FakeRepository,
                    )

    def test_activation_true_or_kill_false_still_fails_before_composition(self):
        for field, replacement in (
            ("production_activation_enabled", True),
            ("kill_switch_enabled", False),
        ):
            with self.subTest(field=field):
                value = dark_config_value()
                value[field] = replacement
                called = []
                with self.assertRaisesRegex(BootstrapError, "unsafe_startup_defaults"):
                    compose_gateway(
                        self.write_config(value),
                        environment=dark_runtime(),
                        repository_factory=lambda **kwargs: called.append(kwargs),
                    )
                self.assertFalse(called)

    def test_existing_rejection_classes_survive_the_adapter_exemption(self):
        aliased = dark_runtime()
        aliased["TEST_OPERATOR_TOKEN"] = aliased["TEST_SOURCE_TOKEN"]
        with self.assertRaisesRegex(BootstrapError, "authentication_binding_aliased"):
            compose_gateway(
                self.write_config(), environment=aliased, repository_factory=FakeRepository
            )
        shared_key = dark_runtime()
        shared_key["TEST_REFERENCE_KEY"] = shared_key["TEST_SOURCE_TOKEN"]
        with self.assertRaisesRegex(BootstrapError, "reference_hmac_must_not_authenticate"):
            compose_gateway(
                self.write_config(), environment=shared_key, repository_factory=FakeRepository
            )
        incomplete = dark_config_value()
        incomplete.pop("postgres_dsn_env")
        with self.assertRaisesRegex(BootstrapError, "required_config_fields_missing"):
            compose_gateway(
                self.write_config(incomplete),
                environment=dark_runtime(),
                repository_factory=FakeRepository,
            )


class BindAddressAdmissionTests(unittest.TestCase):
    """The backend listener is a fixed private address; there is no wildcard fallback."""

    def write_config(self, value=None):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "gateway.json"
        path.write_text(json.dumps(value or dark_config_value()), encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return str(path)

    def test_private_backend_addresses_compose(self):
        for address in ACCEPTED_BIND_ADDRESSES:
            with self.subTest(address=address):
                composition = compose_gateway(
                    self.write_config(),
                    environment=dark_runtime(address),
                    repository_factory=FakeRepository,
                )
                self.assertEqual(composition.bind_address, address)

    def test_unsafe_bind_addresses_fail_closed_without_echoing_the_value(self):
        for address, expected in REJECTED_BIND_ADDRESSES:
            with self.subTest(address=address):
                called = []
                with self.assertRaises(BootstrapError) as caught:
                    compose_gateway(
                        self.write_config(),
                        environment=dark_runtime(address),
                        repository_factory=lambda **kwargs: called.append(kwargs),
                    )
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn(address, str(caught.exception))
                self.assertFalse(called)

    def test_missing_bind_address_still_uses_the_existing_required_fence(self):
        values = dark_runtime()
        values.pop("TEST_BIND_ADDRESS")
        with self.assertRaisesRegex(BootstrapError, "bind_address_binding_missing"):
            compose_gateway(
                self.write_config(), environment=values, repository_factory=FakeRepository
            )



if __name__ == "__main__":
    unittest.main()
