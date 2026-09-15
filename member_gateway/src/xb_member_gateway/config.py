"""Closed, fail-closed configuration for the member gateway."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    """Configuration cannot safely support the production gateway."""


_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_QUESTION_FIELDS = (
    "name", "phone", "email", "birthday_month", "marketing_consent", "pdpa_acknowledged",
)


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field}_must_be_boolean")
    return value


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{field}_must_be_positive_integer")
    return value


def _env(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ENV_RE.fullmatch(value):
        raise ConfigError(f"{field}_invalid")
    return value


def _digest(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ConfigError(f"{field}_invalid")
    return value.lower()


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    schema_version: str = "xb.member.gateway.config.v2"
    environment: str = "production"
    expected_environment: str = "production"
    production_activation_enabled: bool = False
    kill_switch_enabled: bool = True
    autocount_adapter_ready: bool = True
    allowed_form_aliases: tuple[str, ...] = ("member_registration",)
    allowed_mapping_versions: tuple[str, ...] = ("member-intake.v1",)
    source_form_id: str | None = "synthetic-form"
    source_question_ids: tuple[tuple[str, str | None], ...] = tuple(
        (field, f"synthetic-{field}") for field in _QUESTION_FIELDS
    )
    source_cutover_watermark: str | None = "1970-01-01T00:00:00Z"
    initial_source_window_max: int = 1
    member_no_max_length: int | None = 20
    lease_seconds: int = 600
    heartbeat_seconds: int = 120
    execution_deadline_seconds: int = 300
    max_attempts: int = 3
    worker_concurrency: int = 1
    claim_size: int = 1
    source_token_sha256: str | None = None
    operator_token_sha256: str | None = None
    control_token_sha256: str | None = None
    worker_token_sha256: str | None = None
    recovery_token_sha256: str | None = None
    postgres_dsn_env: str = "XB_MEMBER_GATEWAY_DATABASE_URL"
    bind_address_env: str = "XB_MEMBER_GATEWAY_BIND_ADDRESS"
    bind_port_env: str = "XB_MEMBER_GATEWAY_BIND_PORT"
    reference_hmac_key_env: str = "XB_MEMBER_GATEWAY_REFERENCE_HMAC_KEY"
    source_token_env: str = "XB_MEMBER_GATEWAY_SOURCE_TOKEN"
    operator_token_env: str = "XB_MEMBER_GATEWAY_OPERATOR_TOKEN"
    control_token_env: str = "XB_MEMBER_GATEWAY_CONTROL_TOKEN"
    worker_token_env: str = "XB_MEMBER_GATEWAY_WORKER_TOKEN"
    recovery_token_env: str = "XB_MEMBER_GATEWAY_RECOVERY_TOKEN"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, require_complete: bool = False) -> "GatewayConfig":
        if not isinstance(value, Mapping):
            raise ConfigError("config_must_be_object")
        defaults = cls()
        if set(value) - set(cls.__dataclass_fields__):
            raise ConfigError("unknown_config_fields")
        if require_complete and set(value) != set(cls.__dataclass_fields__):
            raise ConfigError("required_config_fields_missing")
        aliases = value.get("allowed_form_aliases", defaults.allowed_form_aliases)
        mappings = value.get("allowed_mapping_versions", defaults.allowed_mapping_versions)
        if not isinstance(aliases, (list, tuple)) or not aliases or any(not isinstance(x, str) or not _ID_RE.fullmatch(x) for x in aliases):
            raise ConfigError("allowed_form_aliases_invalid")
        if not isinstance(mappings, (list, tuple)) or not mappings or any(not isinstance(x, str) or not _ID_RE.fullmatch(x) for x in mappings):
            raise ConfigError("allowed_mapping_versions_invalid")
        questions = value.get("source_question_ids", dict(defaults.source_question_ids))
        if not isinstance(questions, Mapping) or set(questions) != set(_QUESTION_FIELDS):
            raise ConfigError("source_question_ids_invalid")
        pairs: list[tuple[str, str | None]] = []
        for field in _QUESTION_FIELDS:
            question_id = questions[field]
            if question_id is not None and (not isinstance(question_id, str) or not _ID_RE.fullmatch(question_id)):
                raise ConfigError("source_question_ids_invalid")
            pairs.append((field, question_id))
        source_form_id = value.get("source_form_id", defaults.source_form_id)
        if source_form_id is not None and (not isinstance(source_form_id, str) or not _ID_RE.fullmatch(source_form_id)):
            raise ConfigError("source_form_id_invalid")
        max_length = value.get("member_no_max_length", defaults.member_no_max_length)
        if max_length is not None and (isinstance(max_length, bool) or not isinstance(max_length, int)):
            raise ConfigError("member_no_max_length_invalid")
        result = cls(
            schema_version=value.get("schema_version", defaults.schema_version),
            environment=value.get("environment", defaults.environment),
            expected_environment=value.get("expected_environment", defaults.expected_environment),
            production_activation_enabled=_bool(value.get("production_activation_enabled", defaults.production_activation_enabled), "production_activation_enabled"),
            kill_switch_enabled=_bool(value.get("kill_switch_enabled", defaults.kill_switch_enabled), "kill_switch_enabled"),
            autocount_adapter_ready=_bool(value.get("autocount_adapter_ready", defaults.autocount_adapter_ready), "autocount_adapter_ready"),
            allowed_form_aliases=tuple(aliases), allowed_mapping_versions=tuple(mappings),
            source_form_id=source_form_id, source_question_ids=tuple(pairs),
            source_cutover_watermark=value.get("source_cutover_watermark", defaults.source_cutover_watermark),
            initial_source_window_max=_positive(value.get("initial_source_window_max", defaults.initial_source_window_max), "initial_source_window_max"),
            member_no_max_length=max_length,
            lease_seconds=_positive(value.get("lease_seconds", defaults.lease_seconds), "lease_seconds"),
            heartbeat_seconds=_positive(value.get("heartbeat_seconds", defaults.heartbeat_seconds), "heartbeat_seconds"),
            execution_deadline_seconds=_positive(value.get("execution_deadline_seconds", defaults.execution_deadline_seconds), "execution_deadline_seconds"),
            max_attempts=_positive(value.get("max_attempts", defaults.max_attempts), "max_attempts"),
            worker_concurrency=_positive(value.get("worker_concurrency", defaults.worker_concurrency), "worker_concurrency"),
            claim_size=_positive(value.get("claim_size", defaults.claim_size), "claim_size"),
            source_token_sha256=_digest(value.get("source_token_sha256", defaults.source_token_sha256), "source_token_sha256"),
            operator_token_sha256=_digest(value.get("operator_token_sha256", defaults.operator_token_sha256), "operator_token_sha256"),
            control_token_sha256=_digest(value.get("control_token_sha256", defaults.control_token_sha256), "control_token_sha256"),
            worker_token_sha256=_digest(value.get("worker_token_sha256", defaults.worker_token_sha256), "worker_token_sha256"),
            recovery_token_sha256=_digest(value.get("recovery_token_sha256", defaults.recovery_token_sha256), "recovery_token_sha256"),
            postgres_dsn_env=_env(value.get("postgres_dsn_env", defaults.postgres_dsn_env), "postgres_dsn_env"),
            bind_address_env=_env(value.get("bind_address_env", defaults.bind_address_env), "bind_address_env"),
            bind_port_env=_env(value.get("bind_port_env", defaults.bind_port_env), "bind_port_env"),
            reference_hmac_key_env=_env(value.get("reference_hmac_key_env", defaults.reference_hmac_key_env), "reference_hmac_key_env"),
            source_token_env=_env(value.get("source_token_env", defaults.source_token_env), "source_token_env"),
            operator_token_env=_env(value.get("operator_token_env", defaults.operator_token_env), "operator_token_env"),
            control_token_env=_env(value.get("control_token_env", defaults.control_token_env), "control_token_env"),
            worker_token_env=_env(value.get("worker_token_env", defaults.worker_token_env), "worker_token_env"),
            recovery_token_env=_env(value.get("recovery_token_env", defaults.recovery_token_env), "recovery_token_env"),
        )
        result.validate()
        return result

    @property
    def credential_digests(self) -> tuple[str | None, ...]:
        return (self.source_token_sha256, self.operator_token_sha256, self.control_token_sha256, self.worker_token_sha256, self.recovery_token_sha256)

    @property
    def credential_env_names(self) -> tuple[str, ...]:
        return (self.source_token_env, self.operator_token_env, self.control_token_env, self.worker_token_env, self.recovery_token_env)

    @property
    def question_id_mapping(self) -> dict[str, str | None]:
        return dict(self.source_question_ids)

    def validate(self) -> None:
        if self.schema_version != "xb.member.gateway.config.v2":
            raise ConfigError("config_schema_version_invalid")
        if not isinstance(self.environment, str) or not self.environment.strip() or not isinstance(self.expected_environment, str) or not self.expected_environment.strip():
            raise ConfigError("environment_invalid")
        if self.lease_seconds <= self.heartbeat_seconds:
            raise ConfigError("heartbeat_must_be_shorter_than_lease")
        if self.heartbeat_seconds >= self.execution_deadline_seconds:
            raise ConfigError("heartbeat_must_be_shorter_than_execution_deadline")
        if self.execution_deadline_seconds >= self.lease_seconds:
            raise ConfigError("execution_deadline_exceeds_lease")
        if self.worker_concurrency != 1 or self.claim_size != 1:
            raise ConfigError("worker_concurrency_must_be_one")
        if self.initial_source_window_max != 1:
            raise ConfigError("initial_source_window_max_must_be_one")
        if self.member_no_max_length is not None and self.member_no_max_length != 20:
            raise ConfigError("member_no_max_length_must_be_twenty")
        if len({name.casefold() for name in self.credential_env_names}) != 5:
            raise ConfigError("credential_environment_bindings_must_differ")
        present = [digest.casefold() for digest in self.credential_digests if digest is not None]
        if len(set(present)) != len(present):
            raise ConfigError("credential_digests_must_differ")
        from .canonical import parse_rfc3339
        if self.source_cutover_watermark is not None:
            try:
                parse_rfc3339(self.source_cutover_watermark, field="source_cutover_watermark")
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        question_ids = [item for _, item in self.source_question_ids if item is not None]
        if len(set(question_ids)) != len(question_ids):
            raise ConfigError("source_question_ids_must_differ")

    @property
    def environment_matches(self) -> bool:
        return self.environment == self.expected_environment

    @property
    def member_no_constraint_valid(self) -> bool:
        return self.member_no_max_length == 20

    @property
    def kill_switch_clear(self) -> bool:
        return not self.kill_switch_enabled

    @property
    def worker_gateway_ready(self) -> bool:
        return (
            self.environment_matches and self.member_no_constraint_valid
            and self.worker_token_sha256 is not None and self.recovery_token_sha256 is not None
        )

    def readiness_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        try:
            self.validate()
        except ConfigError as exc:
            reasons.append(str(exc))
        if not self.environment_matches:
            reasons.append("environment_mismatch")
        if not self.member_no_constraint_valid:
            reasons.append("member_no_max_length_required")
        if not self.autocount_adapter_ready:
            reasons.append("autocount_adapter_not_ready")
        if self.source_form_id is None:
            reasons.append("source_form_id_required")
        if any(item is None for _, item in self.source_question_ids):
            reasons.append("source_question_ids_required")
        if self.source_cutover_watermark is None:
            reasons.append("source_cutover_watermark_required")
        for name, digest in zip(("source", "operator", "control", "worker", "recovery"), self.credential_digests):
            if digest is None:
                reasons.append(f"{name}_credential_digest_required")
        return tuple(dict.fromkeys(reasons))

    @property
    def gateway_ready(self) -> bool:
        return not self.readiness_reasons()


def load_config(path: str | Path) -> GatewayConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("config_unreadable") from exc
    return GatewayConfig.from_mapping(raw, require_complete=True)
