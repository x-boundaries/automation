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
WORKER_SESSION_HEADER = "X-XB-Worker-Session"
WORKER_SESSION_RE = re.compile(r"^ws-[0-9a-f]{32}$")
WORKER_HOST_BINDING_RE = re.compile(r"^host-[A-Za-z0-9._:-]{1,120}$")


def worker_session(value: str) -> str:
    """Validate the public-safe per-process lease/session identifier."""

    if not isinstance(value, str) or not WORKER_SESSION_RE.fullmatch(value):
        raise AuthenticationError("worker_session_invalid")
    return value


def worker_host_binding(value: str) -> str:
    """Validate the opaque host identity bound to a writer execution."""

    if not isinstance(value, str) or not WORKER_HOST_BINDING_RE.fullmatch(value):
        raise AuthenticationError("worker_host_binding_invalid")
    return value


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
        try:
            config.validate()
        except ValueError:
            return cls({})
        if config.worker_token_sha256 is None or config.recovery_token_sha256 is None:
            return cls({})
        worker_token = os.environ.get(config.worker_token_env, "")
        recovery_token = os.environ.get(config.recovery_token_env, "")
        if not worker_token or not recovery_token:
            return cls({})
        worker_digest = hashlib.sha256(worker_token.encode("utf-8")).hexdigest()
        recovery_digest = hashlib.sha256(recovery_token.encode("utf-8")).hexdigest()
        if hmac.compare_digest(worker_digest, recovery_digest):
            return cls({})
        if not hmac.compare_digest(config.worker_token_sha256.lower(), worker_digest):
            return cls({})
        if not hmac.compare_digest(config.recovery_token_sha256.lower(), recovery_digest):
            return cls({})
        return cls(
            {
                worker_digest: Principal(
                    subject="configured-worker",
                    scopes=frozenset(
                        {
                            "worker.claim",
                            "worker.heartbeat",
                            "worker.allocation",
                            "worker.write_intent",
                            "worker.dispatch",
                            "worker.writer_register",
                            "worker.writer_termination",
                            "worker.writer_quarantine",
                            "worker.result",
                            "worker.reconcile",
                            "job.read",
                        }
                    ),
                ),
                recovery_digest: Principal(
                    subject="configured-recovery",
                    scopes=frozenset({"worker.writer_termination_recovery"}),
                ),
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
