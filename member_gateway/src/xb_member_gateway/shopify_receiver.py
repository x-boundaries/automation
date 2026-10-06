"""Dedicated Shopify webhook receiver process (provider-neutral).

``python -m xb_member_gateway.shopify_receiver serve --config ... --shopify-config ...``
binds a private backend address only; the public edge (hostname/TLS/tunnel)
is a separately approved deployment binding that forwards exactly one route.

``... capture-baseline ...`` performs the exhaustive ``member-mg`` cutover
capture while admission is disabled and prints only the PII-free seal
(baseline id, member count, digest).

Secrets (webhook secret, Admin token, protected-payload key, DSN, reference
key) are read from the runtime environment only and never echoed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .bootstrap import BootstrapError, _private_bind_address, _required, shopify_runtime
from .config import ConfigError, ShopifyM1Config, load_config, load_shopify_config
from .repository import PostgresRepository, RepositoryError
from .shopify_admission import (
    BaselineCaptureError,
    ShopifyAdminClient,
    ShopifyAdmissionProcessor,
    ShopifyReadError,
    capture_member_mg_baseline,
    run_admission_loop,
)
from .shopify_webhook import ShopifyWebhookReceiver, serve_receiver


@dataclass(frozen=True)
class ReceiverComposition:
    shopify_config: ShopifyM1Config
    repository: Any
    receiver: ShopifyWebhookReceiver
    processor: ShopifyAdmissionProcessor
    client: Any
    bind_address: str
    bind_port: int


def compose_receiver(
    config_path: str,
    shopify_config_path: str,
    *,
    environment: Mapping[str, str],
    repository_factory: Callable[..., Any] = PostgresRepository,
    client_factory: Callable[[ShopifyM1Config, str], Any] = ShopifyAdminClient,
) -> ReceiverComposition:
    try:
        config = load_config(config_path)
        shopify_config = load_shopify_config(shopify_config_path)
        reasons = shopify_config.readiness_reasons()
        if reasons:
            raise BootstrapError(reasons[0])
        names = (
            config.postgres_dsn_env, config.reference_hmac_key_env, *config.credential_env_names,
            config.bind_address_env, config.bind_port_env,
            shopify_config.webhook_secret_env, shopify_config.admin_token_env, shopify_config.protected_payload_key_env,
            shopify_config.receiver_bind_address_env, shopify_config.receiver_bind_port_env,
        )
        if len({name.casefold() for name in names}) != len(names):
            raise BootstrapError("runtime_environment_bindings_must_differ")
        dsn = _required(environment, config.postgres_dsn_env, "postgres_dsn_binding_missing")
        reference_key = _required(environment, config.reference_hmac_key_env, "reference_hmac_binding_missing")
        secret = _required(environment, shopify_config.webhook_secret_env, "shopify_webhook_secret_binding_missing")
        token = _required(environment, shopify_config.admin_token_env, "shopify_admin_token_binding_missing")
        address = _private_bind_address(_required(environment, shopify_config.receiver_bind_address_env, "bind_address_binding_missing"))
        port_text = _required(environment, shopify_config.receiver_bind_port_env, "bind_port_binding_missing")
        try:
            port = int(port_text, 10)
        except ValueError as exc:
            raise BootstrapError("bind_port_invalid") from exc
        if not 1 <= port <= 65535:
            raise BootstrapError("bind_port_invalid")
        if len({secret, token, reference_key, dsn}) != 4 or len(secret) < 16:
            raise BootstrapError("shopify_secret_bindings_invalid")
        runtime = shopify_runtime(shopify_config, environment, forbidden_values=(secret, token, reference_key, dsn))
        repository = repository_factory(dsn=dsn, reference_key=reference_key.encode("utf-8"))
        repository.verify_bootstrap_readiness(config)
        client = client_factory(shopify_config, token)
        receiver = ShopifyWebhookReceiver(shopify_config, repository, secret.encode("utf-8"))
        processor = ShopifyAdmissionProcessor(shopify_config, repository, client, runtime.cipher)
        return ReceiverComposition(shopify_config, repository, receiver, processor, client, address, port)
    except BootstrapError:
        raise
    except (ConfigError, RepositoryError, ShopifyReadError) as exc:
        code = getattr(exc, "code", str(exc))
        raise BootstrapError(code if isinstance(code, str) and 0 < len(code) <= 80 else "bootstrap_rejected") from None
    except Exception:
        raise BootstrapError("bootstrap_rejected") from None


def capture_baseline(composition: ReceiverComposition, *, now: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Admission must be disabled; nothing is persisted unless fully sealed."""

    clock = now or (lambda: datetime.now(timezone.utc))
    if composition.repository.shopify_admission_gate() is not None:
        raise BootstrapError("shopify_admission_must_be_disabled_for_capture")
    started = clock()
    try:
        gids = capture_member_mg_baseline(composition.client.baseline_page)
    except BaselineCaptureError as exc:
        raise BootstrapError(exc.code.split(":", 1)[0]) from None
    baseline = composition.repository.store_shopify_baseline(
        gids, shop_domain=composition.shopify_config.shop_domain, api_version=composition.shopify_config.api_version,
        capture_started_at=started, now=clock(),
    )
    if not composition.repository.verify_shopify_baseline(baseline.baseline_id):
        raise BootstrapError("shopify_baseline_readback_failed")
    return {"baseline_id": baseline.baseline_id, "member_count": baseline.member_count, "member_digest": baseline.member_digest, "state": baseline.state}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xb_member_gateway.shopify_receiver")
    parser.add_argument("command", choices=("serve", "capture-baseline"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--shopify-config", required=True)
    args = parser.parse_args(argv)
    try:
        composition = compose_receiver(args.config, args.shopify_config, environment=os.environ)
        if args.command == "capture-baseline":
            sys.stdout.write(json.dumps(capture_baseline(composition), sort_keys=True) + "\n")
            return 0
        stop = threading.Event()
        loop = threading.Thread(
            target=run_admission_loop,
            args=(composition.processor, stop, composition.shopify_config.admission_poll_seconds),
            name="shopify-admission", daemon=True,
        )
        loop.start()
        try:
            serve_receiver(composition.receiver, composition.bind_address, composition.bind_port)
        finally:
            stop.set()
    except BootstrapError as exc:
        sys.stderr.write(f"shopify_receiver_failed:{exc.code}\n")
        return 2
    except RepositoryError as exc:  # repository codes are bounded identifiers
        code = str(exc)
        sys.stderr.write(f"shopify_receiver_failed:{code if re.fullmatch(r'[a-z0-9_.:-]{1,80}', code) else 'repository_rejected'}\n")
        return 2
    except Exception:  # never echo driver/transport detail
        sys.stderr.write("shopify_receiver_failed:runtime_rejected\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
