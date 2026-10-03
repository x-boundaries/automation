from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
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
RUNTIME_V2_SCHEMA = "energygrid.runtime.v2"
DUAL_SOURCE = "dual_stream"
BOUND_ADMISSION = "BOUND"
UNBOUND_ADMISSION = "UNBOUND"

# DL-XB-199 G3-101. `browser` is the legacy Playwright source; `direct_http` is
# the MVP daily source. The choice is explicit private configuration, never a
# fallback: one run uses exactly one source.
SOURCE_BROWSER = "browser"
SOURCE_DIRECT_HTTP = "direct_http"
SOURCES = (SOURCE_BROWSER, SOURCE_DIRECT_HTTP)
MAX_TENANT_ID_LENGTH = 128
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ENV_NAME_RE = r"\A[A-Z][A-Z0-9_]{0,63}\Z"
HEADER_NAME_RE = r"\A[A-Za-z0-9-]{1,64}\Z"


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
class DirectHttpSettings:
    """Private interface identity. Every field is excluded from `repr`.

    The endpoints and the tenant identifier live only in the private host
    configuration; nothing here is ever logged, printed or alerted.
    """

    list_url: str = field(repr=False)
    fetch_url: str = field(repr=False)
    tenant_id: str = field(repr=False)

    def to_raw(self) -> dict[str, str]:
        return {"list_url": self.list_url, "fetch_url": self.fetch_url, "tenant_id": self.tenant_id}


@dataclass(frozen=True)
class AlertSettings:
    """Loopback-only alert ingress. The URL path and any token stay private."""

    url: str = field(repr=False)
    auth_header_name: str | None = field(default=None, repr=False)
    auth_token_env: str | None = field(default=None, repr=False)

    def to_raw(self) -> dict[str, Any]:
        raw: dict[str, Any] = {"url": self.url}
        if self.auth_header_name is not None:
            raw["auth_header_name"] = self.auth_header_name
            raw["auth_token_env"] = self.auth_token_env
        return raw


@dataclass(frozen=True)
class DualStreamEntry:
    admission: str
    source_namespace: str | None = field(default=None, repr=False)
    adapter_id: str | None = None
    date_profile: str | None = None
    evidence_ref: str | None = None
    settings: DirectHttpSettings | None = field(default=None, repr=False)


@dataclass(frozen=True)
class DriveSettings:
    mode: str
    root: Path | None = field(default=None, repr=False)
    binding_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class DeliverySettings:
    url: str = field(repr=False)
    auth_header_name: str = field(repr=False)
    auth_token_env: str = field(repr=False)
    max_pdf_bytes: int
    timeout_seconds: int


@dataclass(frozen=True)
class DualRuntimeConfig:
    """Strict v2 configuration; all path and source bindings are private."""

    archive_root: Path = field(repr=False)
    state_path: Path = field(repr=False)
    temp_root: Path = field(repr=False)
    log_root: Path = field(repr=False)
    streams: dict[str, DualStreamEntry] = field(repr=False)
    drive: DriveSettings = field(repr=False)
    delivery: DeliverySettings = field(repr=False)
    inventory_safety_ceiling: int = 1000
    max_attempts: int = 2
    timeout_seconds: int = 30
    alert: AlertSettings | None = field(default=None, repr=False)
    checkout_root: Path | None = field(default=None, repr=False)

    def preflight(self, *, read_only: bool = False) -> None:
        if not self.archive_root.exists() or not self.archive_root.is_dir():
            raise ConfigError("archive_root must already exist as a directory")
        if self.state_path.exists() and self.state_path.is_dir():
            raise ConfigError("state_path must be a file path")
        for directory in (self.state_path.parent, self.temp_root, self.log_root):
            if directory.exists() and not directory.is_dir():
                raise ConfigError("runtime directory path must be a directory")
        if self.drive.mode == "local_stage":
            assert self.drive.root is not None
            if not self.drive.root.exists() or not self.drive.root.is_dir():
                raise ConfigError("drive root must already exist as a directory")
        if read_only:
            return
        # The v2 state file itself is never created by a daily run. These
        # runtime directories contain only bounded diagnostics and owned temps.
        for directory in (self.temp_root, self.log_root):
            directory.mkdir(parents=True, exist_ok=True)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _exact_keys(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ConfigError(f"{label} has an invalid shape")
    return value


def _strict_text(value: Any, label: str, *, max_length: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > max_length:
        raise ConfigError(f"{label} is invalid")
    if not value.isprintable():
        raise ConfigError(f"{label} is invalid")
    return value


def _strict_positive_int(value: Any, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f"{label} is outside its allowed bounds")
    return value


def _reject_reparse_components(path: Path) -> None:
    lexical = Path(os.path.abspath(path))
    parts = lexical.parts
    if not parts:
        return
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ConfigError("configured path component could not be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ConfigError("configured path contains a reparse point")


def _validate_endpoint(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigError(f"{label} must be a non-empty absolute HTTP(S) URL")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError(f"{label} must be a non-empty absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ConfigError(f"{label} must not carry credentials or a fragment")
    return value


def _validate_direct_http(value: Any) -> DirectHttpSettings:
    if not isinstance(value, dict) or set(value) != {"list_url", "fetch_url", "tenant_id"}:
        raise ConfigError("direct_http must be an object with exactly list_url, fetch_url and tenant_id")
    tenant = value["tenant_id"]
    if (
        not isinstance(tenant, str)
        or not tenant
        or tenant != tenant.strip()
        or len(tenant) > MAX_TENANT_ID_LENGTH
        or not tenant.isprintable()
    ):
        raise ConfigError("direct_http.tenant_id must be a non-empty printable string")
    return DirectHttpSettings(
        list_url=_validate_endpoint(value["list_url"], "direct_http.list_url"),
        fetch_url=_validate_endpoint(value["fetch_url"], "direct_http.fetch_url"),
        tenant_id=tenant,
    )


def _validate_alert(value: Any) -> AlertSettings:
    import re

    if not isinstance(value, dict) or not {"url"} <= set(value) <= {"url", "auth_header_name", "auth_token_env"}:
        raise ConfigError("alert must be an object with url and optional auth_header_name/auth_token_env")
    url = _validate_endpoint(value["url"], "alert.url")
    if urlparse(url).hostname not in LOOPBACK_HOSTS:
        raise ConfigError("alert.url must be a loopback address")
    header = value.get("auth_header_name")
    env_name = value.get("auth_token_env")
    if (header is None) != (env_name is None):
        raise ConfigError("alert auth_header_name and auth_token_env must be supplied together")
    if header is not None:
        if not isinstance(header, str) or not re.fullmatch(HEADER_NAME_RE, header):
            raise ConfigError("alert.auth_header_name is invalid")
        if not isinstance(env_name, str) or not re.fullmatch(ENV_NAME_RE, env_name):
            raise ConfigError("alert.auth_token_env is invalid")
    return AlertSettings(url=url, auth_header_name=header, auth_token_env=env_name)


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
    source: str = SOURCE_BROWSER
    direct_http: DirectHttpSettings | None = field(default=None, repr=False)
    alert: AlertSettings | None = field(default=None, repr=False)

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
        raw: dict[str, Any] = {"source": self.source}
        if self.direct_http is not None:
            raw["direct_http"] = self.direct_http.to_raw()
        if self.alert is not None:
            raw["alert"] = self.alert.to_raw()
        return load_runtime_config(
            {
                **raw,
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

    def preflight(self, require_archive: bool = True, *, read_only: bool = False) -> None:
        if require_archive and (not self.archive_root.exists() or not self.archive_root.is_dir()):
            raise ConfigError("archive_root must already exist as a directory")
        if not read_only and self.state_path.exists() and self.state_path.is_dir():
            raise ConfigError("state_path must be a file path")
        if read_only:
            # Metadata only. StateStore owns absent/incompatible DB failures;
            # SafeLogger alone may create the approved log root.
            directories = [self.state_path.parent, self.temp_root, self.log_root]
            if self.browser_cache_path is not None:
                directories.append(self.browser_cache_path.parent)
            for directory in directories:
                if directory.exists() and not directory.is_dir():
                    raise ConfigError("runtime directory path must be a directory")
            return
        for directory in (self.state_path.parent, self.temp_root, self.log_root):
            directory.mkdir(parents=True, exist_ok=True)
        if self.browser_cache_path is not None:
            self.browser_cache_path.parent.mkdir(parents=True, exist_ok=True)


def load_config_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_strict_object)
    except FileNotFoundError as exc:
        raise ConfigError("config file was not found") from exc
    except (OSError, ValueError) as exc:
        raise ConfigError("config file is unreadable or invalid JSON") from exc


def load_dual_stream_config(raw: dict[str, Any], checkout_root: Path | None = None) -> DualRuntimeConfig:
    """Load the closed v2 shape without coercion or permissive extra keys."""

    allowed = {
        "schema", "source", "archive_root", "state_path", "temp_root", "log_root",
        "inventory_safety_ceiling", "timeout_seconds", "max_attempts", "streams",
        "drive", "delivery", "alert",
    }
    if type(raw) is not dict or set(raw) - allowed:
        raise ConfigError("v2 config has an invalid shape")
    if raw.get("schema") != RUNTIME_V2_SCHEMA or raw.get("source") != DUAL_SOURCE:
        raise ConfigError("v2 config schema or source is unsupported")

    roots: dict[str, Path] = {}
    required_paths = ("archive_root", "state_path", "temp_root", "log_root")
    for key in required_paths:
        value = raw.get(key)
        if type(value) is not str or not value:
            raise ConfigError(f"{key} must be a non-empty absolute path")
        path = Path(value)
        _reject_reparse_components(path)
        roots[key] = require_archive_location(path, checkout_root) if key == "archive_root" else require_external(path, checkout_root, key)

    private_roots = [roots[name] for name in ("archive_root", "state_path", "temp_root", "log_root")]

    streams_raw = _exact_keys(raw.get("streams"), {"EB_BILL", "TENANT_BILL"}, "streams")
    streams: dict[str, DualStreamEntry] = {}
    for name in ("EB_BILL", "TENANT_BILL"):
        item = _exact_keys(
            streams_raw[name],
            {"admission", "source_namespace", "adapter_id", "date_profile", "evidence_ref", "settings"},
            f"streams.{name}",
        )
        admission = item["admission"]
        if admission == UNBOUND_ADMISSION:
            if any(item[key] is not None for key in ("source_namespace", "adapter_id", "date_profile", "evidence_ref", "settings")):
                raise ConfigError(f"streams.{name} unbound fields must be null")
            streams[name] = DualStreamEntry(admission=UNBOUND_ADMISSION)
            continue
        if admission != BOUND_ADMISSION:
            raise ConfigError(f"streams.{name}.admission is unsupported")
        namespace = _strict_text(item["source_namespace"], f"streams.{name}.source_namespace")
        if any(ord(ch) < 32 for ch in namespace):
            raise ConfigError(f"streams.{name}.source_namespace is invalid")
        adapter_id = item["adapter_id"]
        date_profile = item["date_profile"]
        evidence_ref = item["evidence_ref"]
        if adapter_id != "DIRECT_HTTP_V1" or date_profile != "INVOICE_DATE_ISO_V1":
            raise ConfigError(f"streams.{name} binding is not an admitted source/date profile")
        if name == "TENANT_BILL":
            # No accepted #227 terminal contract is present in this repository
            # task. Synthetic adapters remain injectable in tests only.
            raise ConfigError("Tenant Bill production admission requires accepted source evidence")
        if type(evidence_ref) is not str or not re.fullmatch(r"EG_[A-Z0-9_]{1,60}\Z", evidence_ref, re.ASCII):
            raise ConfigError(f"streams.{name}.evidence_ref is invalid")
        settings_raw = _exact_keys(item["settings"], {"list_url", "fetch_url", "tenant_id"}, f"streams.{name}.settings")
        settings = _validate_direct_http(settings_raw)
        streams[name] = DualStreamEntry(
            admission=BOUND_ADMISSION,
            source_namespace=namespace,
            adapter_id=adapter_id,
            date_profile=date_profile,
            evidence_ref=evidence_ref,
            settings=settings,
        )

    drive_raw = _exact_keys(raw.get("drive"), {"mode", "root", "binding_id"}, "drive")
    drive_mode = drive_raw["mode"]
    if drive_mode == "unbound":
        if drive_raw["root"] is not None or drive_raw["binding_id"] is not None:
            raise ConfigError("unbound Drive fields must be null")
        drive = DriveSettings(mode="unbound")
    elif drive_mode == "local_stage":
        root_value = _strict_text(drive_raw["root"], "drive.root", max_length=1024)
        binding_id = _strict_text(drive_raw["binding_id"], "drive.binding_id", max_length=128)
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}\Z", binding_id, re.ASCII):
            raise ConfigError("drive.binding_id is invalid")
        _reject_reparse_components(Path(root_value))
        drive_root = require_external(Path(root_value), checkout_root, "drive.root")
        private_roots.append(drive_root)
        drive = DriveSettings(mode="local_stage", root=drive_root, binding_id=binding_id)
    else:
        raise ConfigError("drive.mode is unsupported")

    for index, first in enumerate(private_roots):
        for second in private_roots[index + 1:]:
            if is_within(first, second) or is_within(second, first):
                raise ConfigError("archive, Drive and runtime paths must be separate")

    delivery_raw = _exact_keys(
        raw.get("delivery"),
        {"url", "auth_header_name", "auth_token_env", "max_pdf_bytes", "timeout_seconds"},
        "delivery",
    )
    url = _validate_endpoint(delivery_raw["url"], "delivery.url")
    parsed_url = urlparse(url)
    if parsed_url.query or (parsed_url.scheme == "http" and parsed_url.hostname not in LOOPBACK_HOSTS):
        raise ConfigError("delivery.url must be loopback HTTP or query-free HTTPS")
    header = delivery_raw["auth_header_name"]
    env_name = delivery_raw["auth_token_env"]
    if type(header) is not str or not re.fullmatch(HEADER_NAME_RE, header, re.ASCII):
        raise ConfigError("delivery.auth_header_name is invalid")
    if type(env_name) is not str or not re.fullmatch(ENV_NAME_RE, env_name, re.ASCII):
        raise ConfigError("delivery.auth_token_env is invalid")
    delivery = DeliverySettings(
        url=url,
        auth_header_name=header,
        auth_token_env=env_name,
        max_pdf_bytes=_strict_positive_int(delivery_raw["max_pdf_bytes"], 1, 33_554_432, "delivery.max_pdf_bytes"),
        timeout_seconds=_strict_positive_int(delivery_raw["timeout_seconds"], 1, MAX_TIMEOUT_SECONDS, "delivery.timeout_seconds"),
    )

    alert = _validate_alert(raw["alert"]) if raw.get("alert") is not None else None
    return DualRuntimeConfig(
        archive_root=roots["archive_root"],
        state_path=roots["state_path"],
        temp_root=roots["temp_root"],
        log_root=roots["log_root"],
        streams=streams,
        drive=drive,
        delivery=delivery,
        inventory_safety_ceiling=_strict_positive_int(raw.get("inventory_safety_ceiling", 1000), 1, MAX_INVENTORY_CEILING, "inventory_safety_ceiling"),
        max_attempts=_strict_positive_int(raw.get("max_attempts", 2), MIN_ATTEMPTS, MAX_ATTEMPTS, "max_attempts"),
        timeout_seconds=_strict_positive_int(raw.get("timeout_seconds", 30), MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, "timeout_seconds"),
        alert=alert,
        checkout_root=resolved(checkout_root) if checkout_root is not None else find_checkout_root(),
    )


def load_runtime_config(raw: dict[str, Any], checkout_root: Path | None = None) -> RuntimeConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    source = raw.get("source", SOURCE_BROWSER)
    if source not in SOURCES:
        raise ConfigError("source must be browser or direct_http")
    direct_http: DirectHttpSettings | None = None
    if source == SOURCE_DIRECT_HTTP:
        direct_http = _validate_direct_http(raw.get("direct_http"))
        # The browser-only witness is not used by the direct-HTTP source.
        account_identity = raw.get("account_identity") or ""
        if not isinstance(account_identity, str):
            raise ConfigError("account_identity must be a string")
    else:
        if "direct_http" in raw:
            raise ConfigError("direct_http is configured but source is not direct_http")
        account_identity = _validate_account_identity(raw.get("account_identity"))
    alert = _validate_alert(raw["alert"]) if raw.get("alert") is not None else None
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
    if source == SOURCE_DIRECT_HTTP and raw.get("portal_url") in (None, ""):
        portal_url = ""
    else:
        portal_url = _validate_url(raw.get("portal_url"))
    return RuntimeConfig(
        portal_url=portal_url,
        source=source,
        direct_http=direct_http,
        alert=alert,
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
