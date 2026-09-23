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

SOURCE_SCOPES = frozenset({"source.ingest"})
OPERATOR_SCOPES = frozenset({"operator.status.read", "operator.reconciliation.read"})
CONTROL_SCOPES = frozenset({"control.kill_switch", "control.activate"})
WORKER_SCOPES = frozenset(
    {
        "worker.claim", "worker.heartbeat", "worker.allocation", "worker.write_intent",
        "worker.dispatch", "worker.writer_register", "worker.writer_termination",
        "worker.writer_quarantine", "worker.result", "worker.reconcile", "job.read",
    }
)
RECOVERY_SCOPES = frozenset({"worker.writer_termination_recovery"})
# The n8n mailer may only claim, record send intent, and post a result for a
# welcome-email outbox row. It can never reach member, source or control paths.
MAILER_SCOPES = frozenset({"welcome_email.claim", "welcome_email.send_intent", "welcome_email.result"})
PRINCIPAL_COUNT = 6


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
    def from_environment(
        cls,
        config: GatewayConfig,
        environment: Mapping[str, str] | None = None,
        *,
        strict: bool = False,
    ) -> "BearerTokenAuthenticator":
        def rejected(code: str) -> "BearerTokenAuthenticator":
            if strict:
                raise AuthenticationError(code)
            return cls({})

        try:
            config.validate()
        except ValueError:
            return rejected("authentication_configuration_invalid")
        if any(digest is None for digest in config.credential_digests):
            return rejected("authentication_digest_missing")
        source = environment if environment is not None else os.environ
        tokens = [source.get(name, "") for name in config.credential_env_names]
        if any(not token or any(character.isspace() for character in token) for token in tokens):
            return rejected("authentication_binding_missing")
        if len(set(tokens)) != PRINCIPAL_COUNT:
            return rejected("authentication_binding_aliased")
        digests = [hashlib.sha256(token.encode("utf-8")).hexdigest() for token in tokens]
        if len(set(digests)) != PRINCIPAL_COUNT:
            return rejected("authentication_binding_aliased")
        if any(not hmac.compare_digest(expected or "", actual) for expected, actual in zip(config.credential_digests, digests)):
            return rejected("authentication_binding_mismatch")
        principals = (
            Principal("configured-source", SOURCE_SCOPES),
            Principal("configured-operator", OPERATOR_SCOPES),
            Principal("configured-control", CONTROL_SCOPES),
            Principal("configured-worker", WORKER_SCOPES),
            Principal("configured-recovery", RECOVERY_SCOPES),
            Principal("configured-mailer", MAILER_SCOPES),
        )
        return cls(dict(zip(digests, principals)))

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
