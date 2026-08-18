from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import ConfigError


MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 300
MIN_ATTEMPTS = 1
MAX_ATTEMPTS = 3
MIN_INVENTORY_CEILING = 1
MAX_INVENTORY_CEILING = 100_000


def find_checkout_root(start: Path | None = None) -> Path | None:
    """Find the nearest Git checkout marker without invoking Git."""

    current = (start or Path(__file__)).resolve(strict=False)
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        if marker.is_dir() or marker.is_file():
            return candidate
    return None


def resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def is_within(path: Path, root: Path) -> bool:
    try:
        resolved(path).relative_to(resolved(root))
        return True
    except ValueError:
        return False


def require_external(path: Path, checkout_root: Path | None, label: str) -> Path:
    if not path.is_absolute():
        raise ConfigError(f"{label} must be an absolute path")
    value = resolved(path)
    if checkout_root is not None and is_within(value, checkout_root):
        raise ConfigError(f"{label} must resolve outside the Git checkout")
    return value


def require_archive_location(path: Path, checkout_root: Path | None) -> Path:
    if not path.is_absolute():
        raise ConfigError("archive_root must be an absolute path")
    value = resolved(path)
    if checkout_root is not None and is_within(value, checkout_root):
        private_archive_root = resolved(checkout_root / "_MandarinGallery")
        if not is_within(value, private_archive_root):
            raise ConfigError(
                "archive_root must resolve outside the Git checkout or beneath the checkout-private _MandarinGallery root"
            )
    return value


def _validate_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError("portal_url must be a non-empty string")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("portal_url must be an absolute HTTP(S) URL")
    return value


def _validate_account_identity(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("account_identity must be a non-empty string")
    return value


@dataclass(frozen=True)
class RuntimeConfig:
    portal_url: str
    archive_root: Path
    state_path: Path
    temp_root: Path
    log_root: Path
    account_identity: str = ""
    timeout_seconds: int = 30
    max_attempts: int = 2
    inventory_safety_ceiling: int = 1000
    browser_cache_path: Path | None = None
    checkout_root: Path | None = None

    def with_overrides(self, overrides: dict[str, Any]) -> "RuntimeConfig":
        values: dict[str, Any] = {}
        for key in ("archive_root", "state_path", "temp_root", "log_root"):
            if overrides.get(key) is not None:
                values[key] = Path(overrides[key])
        for key in ("timeout_seconds", "max_attempts"):
            if overrides.get(key) is not None:
                values[key] = int(overrides[key])
        if not values:
            return self
        return load_runtime_config(
            {
                "portal_url": self.portal_url,
                "account_identity": self.account_identity,
                "archive_root": str(values.get("archive_root", self.archive_root)),
                "state_path": str(values.get("state_path", self.state_path)),
                "temp_root": str(values.get("temp_root", self.temp_root)),
                "log_root": str(values.get("log_root", self.log_root)),
                "timeout_seconds": values.get("timeout_seconds", self.timeout_seconds),
                "max_attempts": values.get("max_attempts", self.max_attempts),
                "inventory_safety_ceiling": self.inventory_safety_ceiling,
                "browser_cache_path": str(self.browser_cache_path) if self.browser_cache_path else None,
            },
            checkout_root=self.checkout_root,
        )

    def preflight(self, require_archive: bool = True) -> None:
        if require_archive and (not self.archive_root.exists() or not self.archive_root.is_dir()):
            raise ConfigError("archive_root must already exist as a directory")
        if self.state_path.exists() and self.state_path.is_dir():
            raise ConfigError("state_path must be a file path")
        for directory in (self.state_path.parent, self.temp_root, self.log_root):
            directory.mkdir(parents=True, exist_ok=True)
        if self.browser_cache_path is not None:
            self.browser_cache_path.parent.mkdir(parents=True, exist_ok=True)


def load_config_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError("config file was not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("config file is unreadable or invalid JSON") from exc


def load_runtime_config(raw: dict[str, Any], checkout_root: Path | None = None) -> RuntimeConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    account_identity = _validate_account_identity(raw.get("account_identity"))
    checkout = resolved(checkout_root) if checkout_root is not None else find_checkout_root()
    required = ("archive_root", "state_path", "temp_root", "log_root")
    for key in required:
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ConfigError(f"{key} must be a non-empty absolute path")
    archive_root = require_archive_location(Path(raw["archive_root"]), checkout)
    state_path = require_external(Path(raw["state_path"]), checkout, "state_path")
    temp_root = require_external(Path(raw["temp_root"]), checkout, "temp_root")
    log_root = require_external(Path(raw["log_root"]), checkout, "log_root")

    private_roots = (archive_root, state_path.parent, temp_root, log_root)
    for index, first in enumerate(private_roots):
        for second in private_roots[index + 1 :]:
            if is_within(first, second) or is_within(second, first):
                raise ConfigError("archive and private runtime paths must be separate")

    browser_raw = raw.get("browser_cache_path")
    if browser_raw is None:
        browser_raw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    browser_cache_path: Path | None = None
    if browser_raw and browser_raw != "0":
        browser_cache_path = require_external(Path(browser_raw), checkout, "browser_cache_path")
    if browser_cache_path is not None:
        if any(is_within(browser_cache_path, root) or is_within(root, browser_cache_path) for root in private_roots):
            raise ConfigError("browser cache path must be separate from archive and runtime paths")

    timeout = _bounded_int(raw.get("timeout_seconds", 30), MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, "timeout_seconds")
    attempts = _bounded_int(raw.get("max_attempts", 2), MIN_ATTEMPTS, MAX_ATTEMPTS, "max_attempts")
    ceiling = _bounded_int(
        raw.get("inventory_safety_ceiling", 1000),
        MIN_INVENTORY_CEILING,
        MAX_INVENTORY_CEILING,
        "inventory_safety_ceiling",
    )
    return RuntimeConfig(
        portal_url=_validate_url(raw.get("portal_url")),
        account_identity=account_identity,
        archive_root=archive_root,
        state_path=state_path,
        temp_root=temp_root,
        log_root=log_root,
        timeout_seconds=timeout,
        max_attempts=attempts,
        inventory_safety_ceiling=ceiling,
        browser_cache_path=browser_cache_path,
        checkout_root=checkout,
    )


def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be an integer")
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be an integer") from exc
    if not minimum <= converted <= maximum:
        raise ConfigError(f"{label} is outside its allowed bounds")
    return converted
