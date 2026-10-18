"""Authenticated same-PC n8n client for the Claude-free daily run (#226 V3 G1).

Two fixed webhooks on 127.0.0.1 only (docs/native_daily_claude_free.md sections 3 and 4):

    POST /webhook/xb-energygrid-drive-verify-v1   read-only Google Drive listing of the fixed folder
    POST /webhook/xb-energygrid-owner-email-v1    PROBE (no send) or one SEND

The host, the paths and the scheme are constants. The client uses an empty proxy map (so no
HTTP(S)_PROXY variable can route a request elsewhere), follows no redirect, bounds the answer size,
times out, and sends exactly one request per call: it never retries. The Header Auth value is read from
the private token file into memory for each call and never logged, returned or written.

A transport result is data for the Core to judge (`Answer`); this module decides nothing about
verification or delivery.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
VERIFY_PATH = "/webhook/xb-energygrid-drive-verify-v1"
EMAIL_PATH = "/webhook/xb-energygrid-owner-email-v1"
HEALTH_PATH = "/healthz"
MAX_ANSWER_BYTES = 4 * 1024 * 1024
VERIFY_TIMEOUT_SECONDS = 60
EMAIL_TIMEOUT_SECONDS = 60
HEALTH_TIMEOUT_SECONDS = 5
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}", re.ASCII)

# Transport outcomes. SENT_UNCERTAIN: the request may have reached n8n (timeout, reset, lost or
# unreadable answer). NOT_SENT: no request left this process (bad token, connection refused before send).
OK, HTTP_STATUS, SENT_UNCERTAIN, NOT_SENT = "OK", "HTTP_STATUS", "SENT_UNCERTAIN", "NOT_SENT"


@dataclass(frozen=True)
class Answer:
    outcome: str
    status: int | None = None
    body: Any = None
    support_ref: str | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None


def _opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) overrides the environment proxies; no redirect is ever followed.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class LoopbackClient:
    """One authenticated loopback POST per call. `opener` is injectable for offline tests."""

    def __init__(self, port: int, token_file: Path, header_name: str, opener: Any = None) -> None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port")
        self._base = f"http://{HOST}:{port}"
        self._token_file = token_file
        self._header = header_name
        self._opener = opener or _opener()

    def _token(self) -> str | None:
        try:
            if not self._token_file.is_file() or self._token_file.is_symlink():
                return None
            token = self._token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        return token if _TOKEN_RE.fullmatch(token) else None

    def health(self) -> bool:
        """GET /healthz (no credential). True only for HTTP 200."""
        request = urllib.request.Request(self._base + HEALTH_PATH, method="GET")
        try:
            with self._opener.open(request, timeout=HEALTH_TIMEOUT_SECONDS) as response:
                return response.status == 200
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            return False

    def post(self, path: str, document: dict, timeout: int) -> Answer:
        if path not in (VERIFY_PATH, EMAIL_PATH):
            raise ValueError("path")
        token = self._token()
        if token is None:
            return Answer(NOT_SENT, support_ref="EG_N8N_TOKEN_UNAVAILABLE")
        data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(self._base + path, data=data, method="POST",
                                         headers={"Content-Type": "application/json", self._header: token})
        try:
            with self._opener.open(request, timeout=timeout) as response:
                payload = response.read(MAX_ANSWER_BYTES + 1)
                if len(payload) <= MAX_ANSWER_BYTES and getattr(response, "length", None):
                    # Fewer bytes arrived than the answer announced: a truncated answer is not an answer.
                    return Answer(SENT_UNCERTAIN, support_ref="EG_N8N_TRANSPORT_UNCERTAIN")
                return _answer(response.status, payload)
        except urllib.error.HTTPError as exc:
            # A non-2xx answer (including a refused redirect) is still an answer from n8n.
            try:
                payload = exc.read(MAX_ANSWER_BYTES + 1)
            except (OSError, http.client.HTTPException):
                payload = b""
            finally:
                exc.close()
            return _answer(exc.code, payload)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ConnectionRefusedError):
                return Answer(NOT_SENT, support_ref="EG_N8N_UNREACHABLE")
            return Answer(SENT_UNCERTAIN, support_ref="EG_N8N_TRANSPORT_UNCERTAIN")
        except ConnectionRefusedError:
            return Answer(NOT_SENT, support_ref="EG_N8N_UNREACHABLE")
        except (http.client.HTTPException, OSError, ValueError):
            # http.client.HTTPException (BadStatusLine, IncompleteRead, ...) is not an OSError (G2 H1). The
            # request had already gone out, so it is uncertain and never resent.
            return Answer(SENT_UNCERTAIN, support_ref="EG_N8N_TRANSPORT_UNCERTAIN")


def _answer(status: int, payload: bytes) -> Answer:
    if len(payload) > MAX_ANSWER_BYTES:
        return Answer(HTTP_STATUS, status=status, support_ref="EG_N8N_ANSWER_TOO_LARGE")
    try:
        body = json.loads(payload.decode("utf-8")) if payload else None
    except (UnicodeDecodeError, ValueError):
        body = None
    return Answer(OK if status == 200 else HTTP_STATUS, status=status, body=body)
