"""Privacy-safe hashes and keyed references."""

from __future__ import annotations

import hashlib
import hmac
import unicodedata


class CryptoError(ValueError):
    """Raised when a safe reference cannot be produced."""


def sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def payload_hash(canonical_json: str | bytes) -> str:
    return f"sha256:{sha256_hex(canonical_json)}"


def hmac_reference(value: str, key: bytes | str, *, version: str = "hmac-v1") -> str:
    """Return a public-safe versioned reference; never return the source value."""

    if not isinstance(value, str) or not value:
        raise CryptoError("reference_value_required")
    if isinstance(key, str):
        key = key.encode("utf-8")
    if not isinstance(key, bytes) or not key:
        raise CryptoError("reference_key_required")
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    digest = hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{version}:{digest}"


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left), str(right))
