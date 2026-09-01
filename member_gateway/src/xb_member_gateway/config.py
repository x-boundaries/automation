"""Fail-closed configuration for the member gateway."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    """Configuration cannot safely support an unattended write."""


_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field}_must_be_boolean")
    return value


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{field}_must_be_positive_integer")
    return value


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    schema_version: str = "xb.member.gateway.config.v1"
    environment: str = "production"
    expected_environment: str = "production"
    production_activation_enabled: bool = False
    kill_switch_enabled: bool = True
    allowed_form_aliases: tuple[str, ...] = ("member_registration",)
    allowed_mapping_versions: tuple[str, ...] = ("member-intake.v1",)
    member_no_max_length: int | None = None
    lease_seconds: int = 600
    heartbeat_seconds: int = 120
    execution_deadline_seconds: int = 300
    max_attempts: int = 3
    worker_concurrency: int = 1
    claim_size: int = 1
    worker_token_sha256: str | None = None
    source_hmac_key_env: str = "XB_MEMBER_GATEWAY_HMAC_KEY"
    worker_token_env: str = "XB_MEMBER_GATEWAY_WORKER_TOKEN"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GatewayConfig":
        if not isinstance(value, Mapping):
            raise ConfigError("config_must_be_object")
        defaults = cls()
        allowed = {
            "schema_version", "environment", "expected_environment", "production_activation_enabled",
            "kill_switch_enabled", "allowed_form_aliases", "allowed_mapping_versions",
            "member_no_max_length", "lease_seconds", "heartbeat_seconds", "execution_deadline_seconds",
            "max_attempts", "worker_concurrency", "claim_size", "worker_token_sha256",
            "source_hmac_key_env", "worker_token_env",
        }
        if set(value) - allowed:
            raise ConfigError("unknown_config_fields")
        aliases = value.get("allowed_form_aliases", defaults.allowed_form_aliases)
        mappings = value.get("allowed_mapping_versions", defaults.allowed_mapping_versions)
        if not isinstance(aliases, (list, tuple)) or not aliases or any(not isinstance(x, str) or not x.strip() for x in aliases):
            raise ConfigError("allowed_form_aliases_invalid")
        if not isinstance(mappings, (list, tuple)) or not mappings or any(not isinstance(x, str) or not x.strip() for x in mappings):
            raise ConfigError("allowed_mapping_versions_invalid")
        max_length = value.get("member_no_max_length", defaults.member_no_max_length)
        if max_length is not None and (isinstance(max_length, bool) or not isinstance(max_length, int)):
            raise ConfigError("member_no_max_length_invalid")
        token_hash = value.get("worker_token_sha256", defaults.worker_token_sha256)
        if token_hash is not None and (not isinstance(token_hash, str) or not _HASH_RE.fullmatch(token_hash)):
            raise ConfigError("worker_token_sha256_invalid")
        source_env = value.get("source_hmac_key_env", defaults.source_hmac_key_env)
        worker_env = value.get("worker_token_env", defaults.worker_token_env)
        if not isinstance(source_env, str) or not _ENV_RE.fullmatch(source_env):
            raise ConfigError("source_hmac_key_env_invalid")
        if not isinstance(worker_env, str) or not _ENV_RE.fullmatch(worker_env):
            raise ConfigError("worker_token_env_invalid")
        result = cls(
            schema_version=value.get("schema_version", defaults.schema_version),
            environment=value.get("environment", defaults.environment),
            expected_environment=value.get("expected_environment", defaults.expected_environment),
            production_activation_enabled=_bool(value.get("production_activation_enabled", defaults.production_activation_enabled), "production_activation_enabled"),
            kill_switch_enabled=_bool(value.get("kill_switch_enabled", defaults.kill_switch_enabled), "kill_switch_enabled"),
            allowed_form_aliases=tuple(aliases), allowed_mapping_versions=tuple(mappings),
            member_no_max_length=max_length,
            lease_seconds=_positive(value.get("lease_seconds", defaults.lease_seconds), "lease_seconds"),
            heartbeat_seconds=_positive(value.get("heartbeat_seconds", defaults.heartbeat_seconds), "heartbeat_seconds"),
            execution_deadline_seconds=_positive(value.get("execution_deadline_seconds", defaults.execution_deadline_seconds), "execution_deadline_seconds"),
            max_attempts=_positive(value.get("max_attempts", defaults.max_attempts), "max_attempts"),
            worker_concurrency=_positive(value.get("worker_concurrency", defaults.worker_concurrency), "worker_concurrency"),
            claim_size=_positive(value.get("claim_size", defaults.claim_size), "claim_size"),
            worker_token_sha256=token_hash, source_hmac_key_env=source_env, worker_token_env=worker_env,
        )
        result.validate()
        return result

    def validate(self) -> None:
        if self.schema_version != "xb.member.gateway.config.v1":
            raise ConfigError("config_schema_version_invalid")
        if not isinstance(self.environment, str) or not self.environment.strip() or not isinstance(self.expected_environment, str) or not self.expected_environment.strip():
            raise ConfigError("environment_invalid")
        if self.lease_seconds <= self.heartbeat_seconds:
            raise ConfigError("heartbeat_must_be_shorter_than_lease")
        if self.heartbeat_seconds >= self.execution_deadline_seconds:
            raise ConfigError("heartbeat_must_be_shorter_than_execution_deadline")
        if self.execution_deadline_seconds >= self.lease_seconds:
            raise ConfigError("execution_deadline_exceeds_lease")
        if self.worker_concurrency != 1:
            raise ConfigError("worker_concurrency_must_be_one")
        if self.claim_size != 1:
            raise ConfigError("claim_size_must_be_one")
        if self.member_no_max_length is not None and not 10 <= self.member_no_max_length <= 20:
            raise ConfigError("member_no_max_length_incompatible")

    @property
    def environment_matches(self) -> bool:
        return self.environment == self.expected_environment

    @property
    def kill_switch_clear(self) -> bool:
        return not self.kill_switch_enabled

    @property
    def member_no_constraint_valid(self) -> bool:
        return isinstance(self.member_no_max_length, int) and not isinstance(self.member_no_max_length, bool) and 10 <= self.member_no_max_length <= 20

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
        if self.worker_token_sha256 is None:
            reasons.append("worker_credential_digest_required")
        return tuple(dict.fromkeys(reasons))

    @property
    def gateway_ready(self) -> bool:
        return not self.readiness_reasons()


def load_config(path: str | Path) -> GatewayConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("config_unreadable") from exc
    return GatewayConfig.from_mapping(raw)
