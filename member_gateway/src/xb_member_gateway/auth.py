"""Least-privilege caller authentication for the gateway API."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol

from .config import GatewayConfig


class AuthenticationError(PermissionError):
    """Raised without including credentials or authorization header values."""

    def __init__(self, code: str = "authentication_required"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    scopes: frozenset[str]

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        ...


def require_scope(principal: Principal, scope: str) -> None:
    if not principal.allows(scope):
        raise AuthenticationError("scope_denied")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


_SUBJECT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class BearerTokenAuthenticator:
    """Compare a bearer token to a precomputed SHA-256 digest.

    Token values are accepted only at the process boundary and are never
    returned, logged, or included in an error.
    """

    def __init__(self, digest_to_principal: Mapping[str, Principal]):
        self._entries = tuple(
            (digest.lower(), principal)
            for digest, principal in digest_to_principal.items()
            if _DIGEST_RE.fullmatch(str(digest).lower())
        )

    @classmethod
    def from_environment(cls, config: GatewayConfig) -> "BearerTokenAuthenticator":
        token = os.environ.get(config.worker_token_env, "")
        if not token:
            return cls({})
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        configured_digest = config.worker_token_sha256 or digest
        if not hmac.compare_digest(configured_digest.lower(), digest):
            return cls({})
        return cls(
            {
                digest: Principal(
                    subject="configured-worker",
                    scopes=frozenset(
                        {
                            "worker.claim",
                            "worker.heartbeat",
                            "worker.allocation",
                            "worker.write_intent",
                            "worker.dispatch",
                            "worker.result",
                            "worker.reconcile",
                            "job.read",
                        }
                    ),
                )
            }
        )

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        value = _header(headers, "Authorization")
        if not value or not value.startswith("Bearer "):
            raise AuthenticationError()
        token = value[7:]
        if not token or any(character.isspace() for character in token):
            raise AuthenticationError()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for expected, principal in self._entries:
            if hmac.compare_digest(expected, digest):
                return principal
        raise AuthenticationError()


class StaticAuthenticator:
    """Explicit fake authenticator used only by offline tests."""

    def __init__(self, entries: Mapping[str, Principal]):
        self._entries = dict(entries)

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        value = _header(headers, "Authorization")
        if not value or not value.startswith("Bearer "):
            raise AuthenticationError()
        token = value[7:]
        principal = self._entries.get(token)
        if principal is None:
            raise AuthenticationError()
        return principal


class DenyAllAuthenticator:
    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        raise AuthenticationError()


def principal(subject: str, scopes: Iterable[str]) -> Principal:
    if not _SUBJECT_RE.fullmatch(subject):
        raise ValueError("subject_invalid")
    return Principal(subject=subject, scopes=frozenset(scopes))
