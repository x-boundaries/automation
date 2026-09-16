"""Deterministic production composition; no repair or initialization lives here."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .api import GatewayApp, GatewayService, serve
from .auth import AuthenticationError, BearerTokenAuthenticator
from .config import ConfigError, GatewayConfig, load_config
from .repository import PostgresRepository, RepositoryError


# Dark bring-up composes a gateway whose AutoCount adapter is deliberately not
# ready. Only startup composition is exempt: the reason stays in
# GatewayConfig.readiness_reasons(), so readiness stays false, dispatch stays
# ineligible, and AutoCount execution stays unavailable.
_STARTUP_EXEMPT_READINESS_REASONS = frozenset({"autocount_adapter_not_ready"})


class BootstrapError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GatewayComposition:
    config: GatewayConfig
    repository: Any
    app: GatewayApp
    bind_address: str
    bind_port: int


def _required(environment: Mapping[str, str], name: str, code: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value or any(ord(character) < 32 for character in value):
        raise BootstrapError(code)
    return value


def _private_bind_address(value: str) -> str:
    """Accept only a private IP literal; the supplied value is never echoed.

    Deployment, not the application, places the gateway on its fixed private
    backend address. No name resolution happens here, so a hostname is a
    bounded rejection rather than a silent lookup.
    """

    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise BootstrapError("bind_address_invalid") from exc
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if address.is_unspecified:
        # 0.0.0.0 and :: are classified private, so the wildcard needs its own
        # fence. There is never a fallback to an all-interface bind.
        raise BootstrapError("bind_address_unspecified")
    if address.is_multicast or address.is_reserved or not address.is_private:
        raise BootstrapError("bind_address_not_private")
    return value


def compose_gateway(
    config_path: str,
    *,
    environment: Mapping[str, str],
    repository_factory: Callable[..., Any] = PostgresRepository,
) -> GatewayComposition:
    """Compose only after every fail-closed admission check succeeds."""

    try:
        config = load_config(config_path)
        if config.production_activation_enabled or not config.kill_switch_enabled:
            raise BootstrapError("unsafe_startup_defaults")
        reasons = tuple(
            reason
            for reason in config.readiness_reasons()
            if reason not in _STARTUP_EXEMPT_READINESS_REASONS
        )
        if reasons:
            raise BootstrapError(reasons[0])
        runtime_names = (
            config.postgres_dsn_env, config.bind_address_env, config.bind_port_env,
            config.reference_hmac_key_env, *config.credential_env_names,
        )
        if len({name.casefold() for name in runtime_names}) != len(runtime_names):
            raise BootstrapError("runtime_environment_bindings_must_differ")
        dsn = _required(environment, config.postgres_dsn_env, "postgres_dsn_binding_missing")
        address = _private_bind_address(
            _required(environment, config.bind_address_env, "bind_address_binding_missing")
        )
        port_text = _required(environment, config.bind_port_env, "bind_port_binding_missing")
        reference_key = _required(environment, config.reference_hmac_key_env, "reference_hmac_binding_missing")
        bearer_values = [_required(environment, name, "bearer_binding_missing") for name in config.credential_env_names]
        if reference_key in bearer_values:
            raise BootstrapError("reference_hmac_must_not_authenticate")
        try:
            port = int(port_text, 10)
        except ValueError as exc:
            raise BootstrapError("bind_port_invalid") from exc
        if not 1 <= port <= 65535:
            raise BootstrapError("bind_port_invalid")
        authenticator = BearerTokenAuthenticator.from_environment(config, environment, strict=True)
        repository = repository_factory(dsn=dsn, reference_key=reference_key.encode("utf-8"))
        repository.verify_bootstrap_readiness(config)
        service = GatewayService(config, repository, adapter_ready=config.autocount_adapter_ready)
        app = GatewayApp(service, authenticator)
        return GatewayComposition(config, repository, app, address, port)
    except BootstrapError:
        raise
    except (ConfigError, AuthenticationError, RepositoryError) as exc:
        code = getattr(exc, "code", str(exc))
        if not isinstance(code, str) or not code or len(code) > 80:
            code = "bootstrap_rejected"
        raise BootstrapError(code) from exc
    except Exception as exc:
        # Driver/connectivity failures can include connection details. Keep the
        # production entry point bounded and preserve the cause only in-process.
        raise BootstrapError("bootstrap_rejected") from exc


def run(
    config_path: str,
    *,
    environment: Mapping[str, str],
    repository_factory: Callable[..., Any] = PostgresRepository,
    serve_gateway: Callable[[GatewayApp, str, int], None] = serve,
) -> None:
    composition = compose_gateway(
        config_path, environment=environment, repository_factory=repository_factory
    )
    serve_gateway(composition.app, composition.bind_address, composition.bind_port)
