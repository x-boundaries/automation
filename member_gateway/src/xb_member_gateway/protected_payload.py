"""Purpose-bound authenticated encryption for the transient Shopify create payload.

The only protected values the M1 create path may hold are the AutoCount
create fields: Name, optional MobilePhone, optional EmailAddress,
RegisterDate and ExpiryDate. They are stored only as a versioned AES-256-GCM
envelope (PyCA ``cryptography``; no custom primitive) whose associated data
binds the envelope version, key identity and the owning job. The key is a
runtime environment binding only; it never appears in configuration, Git,
logs or errors. Every failure is a bounded code without plaintext detail.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

ENVELOPE_VERSION = "xbpp1"
PAYLOAD_SCHEMA = "xb.shopify.member_create_payload.v1"
PAYLOAD_FIELDS = frozenset({"name", "mobile_phone", "email_address", "register_date", "expiry_date"})
KEY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_ENVELOPE_RE = re.compile(r"^xbpp1\.([a-z0-9][a-z0-9-]{0,31})\.([A-Za-z0-9_-]{16})\.([A-Za-z0-9_-]{24,8192})$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_JOB_ID_RE = re.compile(r"^job-[0-9a-f]{32}$")
NONCE_BYTES = 12
KEY_BYTES = 32


class ProtectedPayloadError(RuntimeError):
    """Bounded failure; never carries plaintext, key or envelope material."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64d(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ProtectedPayloadError("protected_payload_envelope_invalid") from exc


def decode_key(value: str | None) -> bytes:
    """Decode the runtime key binding: base64url of exactly 32 random bytes."""

    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        raise ProtectedPayloadError("protected_payload_key_missing")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ProtectedPayloadError("protected_payload_key_invalid") from exc
    if len(raw) != KEY_BYTES or len(set(raw)) < 8:
        raise ProtectedPayloadError("protected_payload_key_invalid")
    return raw


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_create_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Exact closed shape. Optional contact fields are real ``None``, never ''."""

    if not isinstance(value, Mapping) or set(value) != PAYLOAD_FIELDS:
        raise ProtectedPayloadError("protected_payload_shape_invalid")
    name = value["name"]
    if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 200:
        raise ProtectedPayloadError("protected_payload_shape_invalid")
    for field in ("mobile_phone", "email_address"):
        item = value[field]
        if item is not None and (not isinstance(item, str) or not item or item != item.strip() or len(item) > 254):
            raise ProtectedPayloadError("protected_payload_shape_invalid")
    if not _valid_date(value["register_date"]) or not _valid_date(value["expiry_date"]):
        raise ProtectedPayloadError("protected_payload_shape_invalid")
    if value["expiry_date"] < value["register_date"]:
        raise ProtectedPayloadError("protected_payload_shape_invalid")
    return {field: value[field] for field in sorted(PAYLOAD_FIELDS)}


def _aad(key_id: str, job_id: str) -> bytes:
    return f"{ENVELOPE_VERSION}|{PAYLOAD_SCHEMA}|{key_id}|{job_id}".encode("ascii")


@dataclass(frozen=True)
class ProtectedPayloadCipher:
    """AES-256-GCM envelope ``xbpp1.<key_id>.<nonce>.<ciphertext+tag>``."""

    key_id: str
    _key: bytes

    def __repr__(self) -> str:  # never render key material
        return f"ProtectedPayloadCipher(key_id={self.key_id!r})"

    @classmethod
    def from_binding(cls, key_id: str, key_value: str | None) -> "ProtectedPayloadCipher":
        if not isinstance(key_id, str) or not KEY_ID_RE.fullmatch(key_id):
            raise ProtectedPayloadError("protected_payload_key_id_invalid")
        cipher = cls(key_id, decode_key(key_value))
        cipher._aead()  # fail closed now if the maintained AEAD is unavailable
        return cipher

    def _aead(self) -> Any:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProtectedPayloadError("protected_payload_cipher_unavailable") from exc
        return AESGCM(self._key)

    def encrypt(self, job_id: str, payload: Mapping[str, Any]) -> str:
        if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
            raise ProtectedPayloadError("protected_payload_binding_invalid")
        plaintext = json.dumps(validate_create_payload(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._aead().encrypt(nonce, plaintext, _aad(self.key_id, job_id))
        return f"{ENVELOPE_VERSION}.{self.key_id}.{_b64e(nonce)}.{_b64e(sealed)}"

    def decrypt(self, job_id: str, envelope: str) -> dict[str, Any]:
        if not isinstance(envelope, str):
            raise ProtectedPayloadError("protected_payload_envelope_invalid")
        match = _ENVELOPE_RE.fullmatch(envelope)
        if match is None:
            raise ProtectedPayloadError("protected_payload_envelope_invalid")
        if match.group(1) != self.key_id:
            raise ProtectedPayloadError("protected_payload_key_id_mismatch")
        nonce, sealed = _b64d(match.group(2)), _b64d(match.group(3))
        if len(nonce) != NONCE_BYTES:
            raise ProtectedPayloadError("protected_payload_envelope_invalid")
        try:
            from cryptography.exceptions import InvalidTag
        except ImportError as exc:  # pragma: no cover
            raise ProtectedPayloadError("protected_payload_cipher_unavailable") from exc
        try:
            plaintext = self._aead().decrypt(nonce, sealed, _aad(self.key_id, job_id))
        except (InvalidTag, ValueError) as exc:
            raise ProtectedPayloadError("protected_payload_decrypt_failed") from exc
        try:
            value = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtectedPayloadError("protected_payload_decrypt_failed") from exc
        return validate_create_payload(value)


def envelope_digest(envelope: str) -> str:
    """PII-free binding of the exact envelope (hash of ciphertext, not plaintext)."""

    import hashlib

    return "sha256:" + hashlib.sha256(envelope.encode("ascii")).hexdigest()
