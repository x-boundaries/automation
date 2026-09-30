"""Direct-HTTP bill source (DL-XB-199 G3-101).

The daily MVP source. It implements the same two-call seam as the legacy
browser portal -- `inventory(ceiling)` and `download(row, destination)` -- over
two POST JSON operations bound to one privately configured tenant identifier:

* LIST returns the complete bill inventory for that tenant only.
* FETCH returns one listed bill's bytes, requested by filename + tenant.

Nothing here enumerates tenants, follows a redirect, uses an ambient proxy,
opens a browser or retries anything but a bounded transient transport failure.
Every contract violation fails closed with one closed public-safe support
reference. No endpoint, tenant identifier, response body, filename or header
value is ever placed in an exception message, a log field or a `repr`.

Request body field names below are the protocol vocabulary; the endpoint URLs
and the tenant identifier are private configuration (`config.DirectHttpSettings`).
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import DirectHttpSettings
from .errors import SourceContractError, SourceTransportError


# Frozen LIST response contract.
LIST_TOP_LEVEL_KEYS = frozenset({"success", "files"})
LIST_ROW_KEYS = frozenset({"date", "filename", "tenant_id"})
# The request body field names. The FETCH body carries exactly these two.
REQUEST_TENANT_FIELD = "tenant_id"
REQUEST_FILENAME_FIELD = "filename"

MAX_LIST_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 32 * 1024 * 1024

# Only these HTTP statuses are transient. Every other non-200 is terminal.
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_DELAY_SECONDS = 2.0
USER_AGENT = "xb-energygrid-bill-downloader/1"

STAGE_LIST = "list"
STAGE_FETCH = "fetch"

# The closed support-reference vocabulary this module can raise.
REF_TRANSPORT_EXHAUSTED = "EG_HTTP_TRANSPORT_EXHAUSTED"
REF_REDIRECT_REFUSED = "EG_HTTP_REDIRECT_REFUSED"
REF_TLS_REFUSED = "EG_HTTP_TLS_REFUSED"
REF_STATUS_UNEXPECTED = "EG_HTTP_STATUS_UNEXPECTED"
REF_FRAMING_INCOMPLETE = "EG_HTTP_FRAMING_INCOMPLETE"
REF_LIST_CONTENT_TYPE = "EG_HTTP_LIST_CONTENT_TYPE"
REF_LIST_OVERSIZE = "EG_HTTP_LIST_OVERSIZE"
REF_LIST_MALFORMED_JSON = "EG_HTTP_LIST_MALFORMED_JSON"
REF_LIST_REJECTED = "EG_HTTP_LIST_REJECTED"
REF_LIST_NOT_SUCCESS = "EG_HTTP_LIST_NOT_SUCCESS"
REF_LIST_SCHEMA_DRIFT = "EG_HTTP_LIST_SCHEMA_DRIFT"
REF_LIST_ROW_SCHEMA_DRIFT = "EG_HTTP_LIST_ROW_SCHEMA_DRIFT"
REF_LIST_TENANT_MISMATCH = "EG_HTTP_LIST_TENANT_MISMATCH"
REF_LIST_EMPTY = "EG_HTTP_LIST_EMPTY"
REF_LIST_CEILING = "EG_HTTP_LIST_CEILING"
REF_FETCH_OVERSIZE = "EG_HTTP_FETCH_OVERSIZE"
REF_FETCH_EMPTY = "EG_HTTP_FETCH_EMPTY"

HTTP_SOURCE_SUPPORT_REFS = frozenset(
    {
        REF_TRANSPORT_EXHAUSTED,
        REF_REDIRECT_REFUSED,
        REF_TLS_REFUSED,
        REF_STATUS_UNEXPECTED,
        REF_FRAMING_INCOMPLETE,
        REF_LIST_CONTENT_TYPE,
        REF_LIST_OVERSIZE,
        REF_LIST_MALFORMED_JSON,
        REF_LIST_REJECTED,
        REF_LIST_NOT_SUCCESS,
        REF_LIST_SCHEMA_DRIFT,
        REF_LIST_ROW_SCHEMA_DRIFT,
        REF_LIST_TENANT_MISMATCH,
        REF_LIST_EMPTY,
        REF_LIST_CEILING,
        REF_FETCH_OVERSIZE,
        REF_FETCH_EMPTY,
    }
)


@dataclass(frozen=True)
class ListedBill:
    """One LIST row. The filename and date are private and excluded from repr."""

    ordinal: int
    filename: str = field(repr=False)
    date: str = field(repr=False)


class _Transient(Exception):
    """Internal marker: one attempt failed in a retryable way."""


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        # Returning None makes urllib raise HTTPError with the 3xx status,
        # which the caller maps to a terminal redirect refusal.
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    # An empty ProxyHandler disables ambient proxy variables, so the request
    # goes exactly where the private configuration says.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirect())


class DirectHttpSource:
    def __init__(
        self,
        settings: DirectHttpSettings,
        timeout_seconds: float,
        max_attempts: int,
        sleep: Callable[[float], None] = time.sleep,
        retry_delay_seconds: float | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._settings = settings
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._sleep = sleep
        # Read at construction so tests may patch the module constant.
        self._retry_delay = RETRY_DELAY_SECONDS if retry_delay_seconds is None else retry_delay_seconds
        self._opener = _build_opener()
        # Bookkeeping for evidence and tests: operation attempt counts only.
        self.list_calls = 0
        self.fetch_calls = 0

    def __repr__(self) -> str:
        return "DirectHttpSource()"

    # ------------------------------------------------------------------ LIST

    def inventory(self, safety_ceiling: int) -> list[ListedBill]:
        body, content_type = self._post(
            self._settings.list_url,
            {REQUEST_TENANT_FIELD: self._settings.tenant_id},
            accept="application/json",
            max_bytes=MAX_LIST_BYTES,
            stage=STAGE_LIST,
            oversize_ref=REF_LIST_OVERSIZE,
            count=lambda: self._count("list"),
        )
        if _media_type(content_type) != "application/json":
            raise SourceContractError(REF_LIST_CONTENT_TYPE)
        try:
            document = json.loads(
                body.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object
            )
        except (UnicodeDecodeError, ValueError):
            raise SourceContractError(REF_LIST_MALFORMED_JSON) from None
        return self._parse_inventory(document, safety_ceiling)

    def _parse_inventory(self, document: Any, safety_ceiling: int) -> list[ListedBill]:
        if not isinstance(document, dict):
            raise SourceContractError(REF_LIST_SCHEMA_DRIFT)
        keys = set(document)
        if keys == {"message"}:
            # The observed shape for an unknown/invalid tenant: a bare message.
            raise SourceContractError(REF_LIST_REJECTED)
        if keys != LIST_TOP_LEVEL_KEYS:
            raise SourceContractError(REF_LIST_SCHEMA_DRIFT)
        if document["success"] is not True:
            if document["success"] is False:
                raise SourceContractError(REF_LIST_NOT_SUCCESS)
            raise SourceContractError(REF_LIST_SCHEMA_DRIFT)
        files = document["files"]
        if not isinstance(files, list):
            raise SourceContractError(REF_LIST_SCHEMA_DRIFT)
        if not files:
            raise SourceContractError(REF_LIST_EMPTY)
        if len(files) > safety_ceiling:
            raise SourceContractError(REF_LIST_CEILING)
        rows: list[ListedBill] = []
        for ordinal, item in enumerate(files):
            if not isinstance(item, dict) or set(item) != LIST_ROW_KEYS:
                raise SourceContractError(REF_LIST_ROW_SCHEMA_DRIFT)
            if not all(type(item[key]) is str for key in LIST_ROW_KEYS):
                raise SourceContractError(REF_LIST_ROW_SCHEMA_DRIFT)
            # Exact equality: no normalisation, trimming or case folding, so a
            # neighbouring tenant can never be accepted by a loose comparison.
            if item["tenant_id"] != self._settings.tenant_id:
                raise SourceContractError(REF_LIST_TENANT_MISMATCH)
            rows.append(ListedBill(ordinal=ordinal, filename=item["filename"], date=item["date"]))
        return rows

    # ----------------------------------------------------------------- FETCH

    def download(self, row: ListedBill, destination: Path) -> str:
        """Fetch one listed bill into `destination` (an owned temp path).

        Bytes are written only after the complete, correctly framed body has
        been received. PDF validation belongs to the caller, before anything
        leaves the temp directory. Returns the listed filename.
        """

        if not isinstance(row, ListedBill):
            raise TypeError("row was not issued by this source")
        body, _content_type = self._post(
            self._settings.fetch_url,
            {REQUEST_FILENAME_FIELD: row.filename, REQUEST_TENANT_FIELD: self._settings.tenant_id},
            accept="*/*",
            max_bytes=MAX_PDF_BYTES,
            stage=STAGE_FETCH,
            oversize_ref=REF_FETCH_OVERSIZE,
            count=lambda: self._count("fetch"),
        )
        if not body:
            raise SourceContractError(REF_FETCH_EMPTY, stage=STAGE_FETCH)
        with destination.open("xb") as handle:
            handle.write(body)
        return row.filename

    # ------------------------------------------------------------- transport

    def _count(self, operation: str) -> None:
        if operation == "list":
            self.list_calls += 1
        else:
            self.fetch_calls += 1

    def _post(
        self,
        url: str,
        payload: dict[str, str],
        *,
        accept: str,
        max_bytes: int,
        stage: str,
        oversize_ref: str,
        count: Callable[[], None],
    ) -> tuple[bytes, str]:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        for attempt in range(1, self._max_attempts + 1):
            count()
            try:
                return self._attempt(url, data, accept, max_bytes, stage, oversize_ref)
            except _Transient:
                if attempt >= self._max_attempts:
                    raise SourceTransportError(REF_TRANSPORT_EXHAUSTED, stage=stage) from None
                self._sleep(self._retry_delay * attempt)
        raise SourceTransportError(REF_TRANSPORT_EXHAUSTED, stage=stage)  # pragma: no cover

    def _attempt(
        self, url: str, data: bytes, accept: str, max_bytes: int, stage: str, oversize_ref: str
    ) -> tuple[bytes, str]:
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": accept, "User-Agent": USER_AGENT},
        )
        try:
            response = self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in RETRYABLE_HTTP_STATUSES:
                raise _Transient() from None
            if 300 <= code < 400:
                raise SourceContractError(REF_REDIRECT_REFUSED, stage=stage) from None
            raise SourceContractError(REF_STATUS_UNEXPECTED, stage=stage) from None
        except ssl.SSLError:
            # A certificate or TLS failure is host/identity drift, never transient.
            raise SourceContractError(REF_TLS_REFUSED, stage=stage) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (ssl.SSLError, ssl.CertificateError)):
                raise SourceContractError(REF_TLS_REFUSED, stage=stage) from None
            # Connection refused/reset, DNS failure and timeout before headers.
            raise _Transient() from None
        except (OSError, http.client.RemoteDisconnected):
            raise _Transient() from None
        except http.client.HTTPException:
            raise SourceContractError(REF_FRAMING_INCOMPLETE, stage=stage) from None
        with response:
            if response.status != 200:
                raise SourceContractError(REF_STATUS_UNEXPECTED, stage=stage)
            content_type = response.headers.get("Content-Type", "")
            transfer_encodings = response.headers.get_all("Transfer-Encoding", [])
            content_lengths = response.headers.get_all("Content-Length", [])
            chunked = response.chunked
            expected: int | None = None
            if transfer_encodings:
                if (
                    content_lengths
                    or len(transfer_encodings) != 1
                    or transfer_encodings[0].strip().lower() != "chunked"
                    or not chunked
                    or response.length is not None
                ):
                    raise SourceContractError(REF_FRAMING_INCOMPLETE, stage=stage) from None
            else:
                if chunked or len(content_lengths) != 1:
                    raise SourceContractError(REF_FRAMING_INCOMPLETE, stage=stage)
                length_value = content_lengths[0].strip()
                if not length_value.isascii() or not length_value.isdecimal():
                    raise SourceContractError(REF_FRAMING_INCOMPLETE, stage=stage) from None
                expected = int(length_value)
                if expected > max_bytes:
                    raise SourceContractError(oversize_ref, stage=stage)
                if response.length != expected:
                    raise SourceContractError(REF_FRAMING_INCOMPLETE, stage=stage)
            try:
                body = response.read(max_bytes + 1)
            except http.client.IncompleteRead:
                raise SourceContractError(REF_FRAMING_INCOMPLETE, stage=stage) from None
            except (socket.timeout, TimeoutError, ConnectionError):
                raise _Transient() from None
            except (OSError, http.client.HTTPException):
                raise SourceContractError(REF_FRAMING_INCOMPLETE, stage=stage) from None
            if len(body) > max_bytes:
                raise SourceContractError(oversize_ref, stage=stage)
            if expected is not None and len(body) != expected:
                raise SourceContractError(REF_FRAMING_INCOMPLETE, stage=stage)
            return body, content_type


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # A repeated key makes the document ambiguous; json.loads would silently
    # keep the last value, so refuse it instead.
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_name: str) -> Any:
    # NaN / Infinity are not JSON; refuse them rather than accept a lax parse.
    raise ValueError("non-standard JSON constant")
