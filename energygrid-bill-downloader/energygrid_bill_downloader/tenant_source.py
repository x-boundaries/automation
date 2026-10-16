"""Tenant Bill source for the native bridge (#226 V3 G1, binding the #227 Tenant stream).

The daily check is plain HTTP and never opens a browser:

1. Sign in once with the stored credential. This proves the account still works and binds the
   private list claim to that account (same user id and username, an allowed role).
2. One LIST call returns the tenant's released bills. Each row gives a stable row id, the printed
   Invoice No. and the billing cycle `YYYY-MM`.

The portal has no server PDF: it renders the bill in the browser. So a headless browser runs ONLY in
`acquire`, for a bill the Core decided to admit. It signs in, opens that row's preview, and saves the
preview's own download. The render is bound to the row twice before the bytes are accepted:

- the preview must load exactly the requested row id (the portal's own bill request is observed), and
- the PDF text must carry the row's Invoice No. (read through the PDF's ToUnicode maps).

Anything else fails closed with one support reference from the closed `EG_TENANT_*` set.

Identity and naming: the Invoice No. is the source identity (`<Invoice No>.pdf`), the invoice date is
the first day of the billing cycle, so the display name reads `MMM YYYY - EnergyGrid - Tenant Bill -
<Invoice No>.pdf` for the billing month.

All endpoints, the list claim and the credential path are private configuration outside Git. No URL,
claim value, Invoice No., response body or credential ever appears in an exception, log or repr. The
credential is read in memory from the existing DPAPI-protected file and is never placed in an
environment variable, a command line or a file.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .errors import ConfigError, SourceContractError
from .http_source import StrictPostTransport, _reject_constant, _unique_object
from .invoice import DATE_PROFILE_ISO_V1, Candidate, InventorySnapshot, Stream

TENANT_CONFIG_SCHEMA = "energygrid.tenant.v1"
TENANT_NAMESPACE = "ENERGYGRID-EMS-TENANT-V1"
TENANT_EVIDENCE = "EG_TENANT_HTTP_COMPLETE_LIST_V1"
TENANT_ADAPTER_ID = "EMS_TENANT_HTTP_RENDER_V1"

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 32 * 1024 * 1024
RELEASED = "released"
RESULT_KEY = "item_list_result"
# The printed Invoice No. becomes a file name, so only a plain shape is accepted.
INVOICE_NO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z", re.ASCII)
CYCLE_RE = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])\Z", re.ASCII)
ROW_ID_RE = re.compile(r"[0-9]{1,18}\Z", re.ASCII)
DOWNLOAD_NAME = "bill.pdf"

STAGE_LOGIN = "login"
STAGE_LIST = "list"
STAGE_RENDER = "fetch"

# The closed support-reference vocabulary of this module.
REF_CREDENTIAL_UNAVAILABLE = "EG_TENANT_CREDENTIAL_UNAVAILABLE"
REF_LOGIN_REJECTED = "EG_TENANT_LOGIN_REJECTED"
REF_LOGIN_SCHEMA = "EG_TENANT_LOGIN_SCHEMA_DRIFT"
REF_CLAIM_MISMATCH = "EG_TENANT_CLAIM_ACCOUNT_MISMATCH"
REF_JSON_INVALID = "EG_TENANT_JSON_INVALID"
REF_RESPONSE_OVERSIZE = "EG_TENANT_RESPONSE_OVERSIZE"
REF_LIST_NOT_SUCCESS = "EG_TENANT_LIST_NOT_SUCCESS"
REF_LIST_SCHEMA = "EG_TENANT_LIST_SCHEMA_DRIFT"
REF_LIST_ROW_SCHEMA = "EG_TENANT_LIST_ROW_SCHEMA_DRIFT"
REF_LIST_TENANT_MISMATCH = "EG_TENANT_LIST_TENANT_MISMATCH"
REF_LIST_INCOMPLETE = "EG_TENANT_LIST_INCOMPLETE"
REF_LIST_CEILING = "EG_TENANT_LIST_CEILING"
REF_LIST_EMPTY = "EG_TENANT_LIST_EMPTY"
REF_HANDLE_INVALID = "EG_FETCH_HANDLE_INVALID"
REF_RENDER_FAILED = "EG_TENANT_RENDER_FAILED"
REF_RENDER_ROW_NOT_VISIBLE = "EG_TENANT_RENDER_ROW_NOT_VISIBLE"
REF_RENDER_ROW_MISMATCH = "EG_TENANT_RENDER_ROW_MISMATCH"
REF_RENDER_DOWNLOAD_UNEXPECTED = "EG_TENANT_RENDER_DOWNLOAD_UNEXPECTED"
REF_PDF_INVOICE_MISMATCH = "EG_TENANT_PDF_INVOICE_MISMATCH"

TENANT_SUPPORT_REFS = frozenset(
    {
        REF_CREDENTIAL_UNAVAILABLE, REF_LOGIN_REJECTED, REF_LOGIN_SCHEMA, REF_CLAIM_MISMATCH, REF_JSON_INVALID,
        REF_RESPONSE_OVERSIZE,
        REF_LIST_NOT_SUCCESS, REF_LIST_SCHEMA, REF_LIST_ROW_SCHEMA, REF_LIST_TENANT_MISMATCH, REF_LIST_INCOMPLETE,
        REF_LIST_CEILING, REF_LIST_EMPTY, REF_HANDLE_INVALID, REF_RENDER_FAILED, REF_RENDER_ROW_NOT_VISIBLE,
        REF_RENDER_ROW_MISMATCH, REF_RENDER_DOWNLOAD_UNEXPECTED, REF_PDF_INVOICE_MISMATCH,
    }
)

Credentials = Callable[[], tuple[str, str]]


# ---------------------------------------------------------------------------
# Private configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantSettings:
    login_url: str = field(repr=False)
    list_url: str = field(repr=False)
    portal_type: str = field(repr=False)
    # The portal's own list request body for this account: {"svcClaimDto": {...}, "request": {...}}.
    list_body: dict = field(repr=False)
    # The fixed service headers the portal's own client sends with the list call.
    service_headers: dict = field(repr=False)
    # The path of the portal's own bill request that a preview makes; it names the row being rendered.
    render_request_path: str = field(repr=False)
    credential_path: Path = field(repr=False)
    # The reviewed browser runtime config (portal URL, browser cache, timeout) used for the render only.
    browser_config_path: Path = field(repr=False)


def require_https(url: str, label: str) -> str:
    """Credential-bearing Tenant endpoints use HTTPS only (H-6). Plain HTTP is refused, never downgraded."""
    from urllib.parse import urlparse

    if type(url) is not str or urlparse(url).scheme != "https":
        raise ConfigError(f"{label} must use https")
    return url


def load_tenant_settings(raw: Any, *, private_path: Callable[[Any, str], Path]) -> TenantSettings:
    """Load the closed private Tenant config. `private_path` enforces the outside-Git rule."""
    from .config import _validate_endpoint

    keys = {"schema", "login_url", "list_url", "portal_type", "list_body", "service_headers", "render_request_path",
            "credential_path", "browser_config_path"}
    if type(raw) is not dict or set(raw) != keys or raw["schema"] != TENANT_CONFIG_SCHEMA:
        raise ConfigError("tenant config has an invalid shape")
    portal_type = raw["portal_type"]
    if type(portal_type) is not str or not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", portal_type):
        raise ConfigError("tenant portal_type is invalid")
    body = raw["list_body"]
    if type(body) is not dict or set(body) != {"svcClaimDto", "request"}:
        raise ConfigError("tenant list_body must hold exactly svcClaimDto and request")
    claim, request = body["svcClaimDto"], body["request"]
    if type(claim) is not dict or type(claim.get("user_id")) is not int or type(claim.get("username")) is not str:
        raise ConfigError("tenant list_body claim is invalid")
    if claim.get("role_id") is None or type(request) is not dict:
        raise ConfigError("tenant list_body claim is invalid")
    filters = request.get("filter_value_info")
    if type(filters) is not dict or type(filters.get("tenant_id")) is not str or not filters["tenant_id"]:
        raise ConfigError("tenant list_body must filter on one tenant_id")
    if request.get("item_kind") != "bill":
        raise ConfigError("tenant list_body must list bills")
    headers = raw["service_headers"]
    if type(headers) is not dict or len(headers) > 4 or not all(
        type(name) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,40}", name)
        and name.lower() not in {"content-type", "accept", "user-agent", "host", "content-length"}
        and type(value) is str and value.isprintable() and len(value) <= 256
        for name, value in headers.items()
    ):
        raise ConfigError("tenant service_headers is invalid")
    render_path = raw["render_request_path"]
    if type(render_path) is not str or not re.fullmatch(r"(?:/[A-Za-z0-9_.-]{1,64}){1,8}", render_path):
        raise ConfigError("tenant render_request_path is invalid")
    return TenantSettings(
        login_url=require_https(_validate_endpoint(raw["login_url"], "tenant.login_url"), "tenant.login_url"),
        list_url=require_https(_validate_endpoint(raw["list_url"], "tenant.list_url"), "tenant.list_url"),
        portal_type=portal_type,
        list_body=copy.deepcopy(body),
        service_headers=dict(headers),
        render_request_path=render_path,
        credential_path=private_path(raw["credential_path"], "tenant.credential_path"),
        browser_config_path=private_path(raw["browser_config_path"], "tenant.browser_config_path"),
    )


# ---------------------------------------------------------------------------
# Credential (DPAPI clixml, read in memory)
# ---------------------------------------------------------------------------

# Reads the PSCredential written by the existing runtime installer. The path arrives through the
# child's environment, so nothing private is on the command line; the pair returns on stdout only.
_CLIXML_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "$c=Import-Clixml -LiteralPath $env:EG_TENANT_CREDENTIAL_PATH;"
    "[Console]::Out.Write((ConvertTo-Json -Compress @{u=$c.UserName;p=$c.GetNetworkCredential().Password}))"
)


def windows_system_directory() -> Path:
    """The Windows system directory from the OS itself (GetSystemDirectoryW), never from PATH or an
    environment variable such as SystemRoot that a caller could change."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    length = ctypes.WinDLL("kernel32", use_last_error=True).GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("system directory unavailable")
    return Path(buffer.value)


def powershell_executable(system_directory: Callable[[], Path] = windows_system_directory) -> Path:
    """Absolute path of Windows PowerShell 5.1 under the system directory (H-5). Fails closed."""
    candidate = system_directory() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not candidate.is_absolute() or not candidate.is_file():
        raise OSError("powershell.exe not found in the system directory")
    return candidate


def dpapi_credentials(path: Path, runner: Callable[..., Any] = subprocess.run,
                      executable: Callable[[], Path] = powershell_executable) -> Credentials:
    """Return a loader that reads the credential once per process, in memory only."""
    cache: list[tuple[str, str]] = []

    def load() -> tuple[str, str]:
        if cache:
            return cache[0]
        import os

        env = dict(os.environ)
        env["EG_TENANT_CREDENTIAL_PATH"] = str(path)
        try:
            completed = runner(
                [str(executable()), "-NoProfile", "-NonInteractive", "-Command", _CLIXML_SCRIPT],
                capture_output=True, env=env, timeout=60, check=False,
            )
            if completed.returncode != 0:
                raise ValueError("credential reader failed")
            pair = json.loads(completed.stdout.decode("utf-8"))
            username, password = pair["u"], pair["p"]
            if type(username) is not str or type(password) is not str or not username or not password:
                raise ValueError("credential is incomplete")
        except Exception:
            # Never chain the cause: it could carry reader output.
            raise SourceContractError(REF_CREDENTIAL_UNAVAILABLE, stage=STAGE_LOGIN) from None
        cache.append((username, password))
        return cache[0]

    return load


# ---------------------------------------------------------------------------
# HTTP sign-in and list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantRow:
    """One released LIST row. Every value is private and excluded from repr."""

    ordinal: int
    row_id: str = field(repr=False)
    invoice_no: str = field(repr=False)
    cycle: str = field(repr=False)


class TenantHttpSource(StrictPostTransport):
    def __init__(
        self,
        settings: TenantSettings,
        credentials: Credentials,
        timeout_seconds: float,
        max_attempts: int,
        sleep: Callable[[float], None] = time.sleep,
        retry_delay_seconds: float | None = None,
    ) -> None:
        super().__init__(timeout_seconds, max_attempts, sleep, retry_delay_seconds)
        self._settings = settings
        self._credentials = credentials
        self.login_calls = 0
        self.list_calls = 0

    def __repr__(self) -> str:
        return "TenantHttpSource()"

    def inventory(self, safety_ceiling: int) -> list[TenantRow]:
        account = self._login()
        body = copy.deepcopy(self._settings.list_body)
        claim = body["svcClaimDto"]
        if claim["user_id"] != account["user_id"] or claim["username"] != account["username"]:
            raise SourceContractError(REF_CLAIM_MISMATCH)
        if str(claim["role_id"]) not in account["role_ids"]:
            raise SourceContractError(REF_CLAIM_MISMATCH)
        request = body["request"]
        # One page large enough for the whole inventory; the returned count proves completeness.
        request.update(current_page="1", max_rows_per_page=str(safety_ceiling + 1), get_count_only="false")
        document = self._post_json(self._settings.list_url, body, STAGE_LIST, lambda: self._count("list"),
                                   self._settings.service_headers)
        return self._parse_list(document, request["filter_value_info"]["tenant_id"], safety_ceiling)

    def _login(self) -> dict[str, Any]:
        username, password = self._credentials()
        payload = {"username": username, "password": password, "email": "", "authProvider": "local",
                   "portal_type": self._settings.portal_type}
        document = self._post_json(self._settings.login_url, payload, STAGE_LOGIN, lambda: self._count("login"))
        if type(document) is not dict or document.get("error") is not None:
            raise SourceContractError(REF_LOGIN_REJECTED, stage=STAGE_LOGIN)
        info = document.get("user_info")
        if type(document.get("token")) is not str or not document["token"] or type(info) is not dict:
            raise SourceContractError(REF_LOGIN_REJECTED, stage=STAGE_LOGIN)
        roles = info.get("role_list")
        if type(info.get("id")) is not int or type(info.get("username")) is not str or type(roles) is not list:
            raise SourceContractError(REF_LOGIN_SCHEMA, stage=STAGE_LOGIN)
        if info.get("enabled") is not True:
            raise SourceContractError(REF_LOGIN_REJECTED, stage=STAGE_LOGIN)
        role_ids = set()
        for role in roles:
            if type(role) is not dict or role.get("id") is None:
                raise SourceContractError(REF_LOGIN_SCHEMA, stage=STAGE_LOGIN)
            role_ids.add(str(role["id"]))
        return {"user_id": info["id"], "username": info["username"], "role_ids": role_ids}

    def _parse_list(self, document: Any, tenant_id: str, safety_ceiling: int) -> list[TenantRow]:
        if type(document) is not dict or document.get("success") is not True:
            raise SourceContractError(REF_LIST_NOT_SUCCESS)
        data = document.get("data")
        if type(data) is not dict or data.get("result_key") != RESULT_KEY or type(data.get("result")) is not dict:
            raise SourceContractError(REF_LIST_SCHEMA)
        result = data["result"].get(RESULT_KEY)
        if type(result) is not dict or type(result.get("item_list")) is not list:
            raise SourceContractError(REF_LIST_SCHEMA)
        items = result["item_list"]
        count = result.get("count")
        if type(count) is str and count.isascii() and count.isdecimal():
            count = int(count)
        if type(count) is not int:
            raise SourceContractError(REF_LIST_SCHEMA)
        if count > safety_ceiling or len(items) > safety_ceiling:
            raise SourceContractError(REF_LIST_CEILING)
        if count != len(items):
            # A partial page is never treated as the whole inventory.
            raise SourceContractError(REF_LIST_INCOMPLETE)
        if not items:
            raise SourceContractError(REF_LIST_EMPTY)
        rows: list[TenantRow] = []
        for item in items:
            if type(item) is not dict or not all(type(item.get(key)) is str for key in
                                                 ("id", "audit_label", "cycle_str", "lc_status", "tenant_id")):
                raise SourceContractError(REF_LIST_ROW_SCHEMA)
            if item["tenant_id"] != tenant_id:
                # Exact equality: a neighbouring tenant's row is never accepted.
                raise SourceContractError(REF_LIST_TENANT_MISMATCH)
            if item["lc_status"] != RELEASED:
                # Not yet released: the portal does not offer it as a bill, so it is not a candidate yet.
                continue
            if (not ROW_ID_RE.fullmatch(item["id"]) or not INVOICE_NO_RE.fullmatch(item["audit_label"])
                    or not CYCLE_RE.fullmatch(item["cycle_str"])):
                raise SourceContractError(REF_LIST_ROW_SCHEMA)
            rows.append(TenantRow(ordinal=len(rows), row_id=item["id"], invoice_no=item["audit_label"],
                                  cycle=item["cycle_str"]))
        if not rows:
            raise SourceContractError(REF_LIST_EMPTY)
        return rows

    def _post_json(self, url: str, payload: dict, stage: str, count: Callable[[], None],
                   extra_headers: dict[str, str] | None = None) -> Any:
        body, content_type = self._post(url, payload, accept="application/json", max_bytes=MAX_JSON_BYTES,
                                        stage=stage, oversize_ref=REF_RESPONSE_OVERSIZE, count=count,
                                        extra_headers=extra_headers)
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise SourceContractError(REF_JSON_INVALID, stage=stage)
        try:
            return json.loads(body.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, ValueError):
            raise SourceContractError(REF_JSON_INVALID, stage=stage) from None

    def _count(self, operation: str) -> None:
        if operation == "login":
            self.login_calls += 1
        else:
            self.list_calls += 1


# ---------------------------------------------------------------------------
# PDF text check
# ---------------------------------------------------------------------------

_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_BFCHAR_RE = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
_BFRANGE_RE = re.compile(rb"beginbfrange(.*?)endbfrange", re.S)
_PAIR_RE = re.compile(rb"<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{2,64})>")
_RANGE_RE = re.compile(rb"<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{2,8})>")
_HEX_RE = re.compile(rb"<([0-9A-Fa-f]+)>")
# A text-showing operator: a TJ array, or a single hex string shown with Tj.
_SHOW_RE = re.compile(rb"\[([^\]]*)\]\s*TJ|(<[0-9A-Fa-f]+>)\s*Tj")
MAX_STREAMS = 2000
MAX_INFLATED_BYTES = 64 * 1024 * 1024


def pdf_text_runs(pdf: bytes) -> list[str]:
    """Decode the hex text runs of every content stream through every ToUnicode map.

    The portal renders two-byte Identity-H fonts with ToUnicode maps. Each content stream is
    decoded once per map, so text set in any one font is found. Unknown codes become U+FFFD, so a
    wrong map can never produce a false match.
    """
    streams: list[bytes] = []
    total = 0
    for match in _STREAM_RE.finditer(pdf):
        if len(streams) >= MAX_STREAMS:
            break
        try:
            inflater = zlib.decompressobj()
            data = inflater.decompress(match.group(1), MAX_INFLATED_BYTES - total)
        except zlib.error:
            continue
        total += len(data)
        streams.append(data)
        if total >= MAX_INFLATED_BYTES:
            break
    maps: list[dict[int, str]] = []
    for data in streams:
        if b"begincmap" not in data:
            continue
        mapping: dict[int, str] = {}
        for block in _BFCHAR_RE.findall(data):
            for source, target in _PAIR_RE.findall(block):
                try:
                    mapping[int(source, 16)] = bytes.fromhex(target.decode("ascii")).decode("utf-16-be")
                except ValueError:
                    continue
        for block in _BFRANGE_RE.findall(data):
            for low, high, target in _RANGE_RE.findall(block):
                start, end, base = int(low, 16), int(high, 16), int(target, 16)
                if end - start > 0xFFFF or base + end - start > 0x10FFFF:
                    continue
                for offset in range(end - start + 1):
                    mapping[start + offset] = chr(base + offset)
        maps.append(mapping)
    runs: list[str] = []
    for data in streams:
        if b"TJ" not in data and b"Tj" not in data:
            continue
        # One entry per text-showing operator, in document order. The hex strings inside one TJ array
        # (split only by kerning numbers) form one shown string; separate operators never run together.
        shows = [_HEX_RE.findall(match.group(1) or match.group(2)) for match in _SHOW_RE.finditer(data)]
        for mapping in maps:
            runs.append("\n".join(
                "".join(
                    "".join(mapping.get(int(text[index:index + 4], 16), "\ufffd") for index in range(0, len(text) - 3, 4))
                    for text in show
                )
                for show in shows
            ))
    return runs


# Characters that may appear inside an Invoice No. (INVOICE_NO_RE). The match must not continue into
# one of them on either side, so "...-00083" never matches inside "...-000831" (H-2).
_TOKEN_CHAR = "A-Za-z0-9_-"


def pdf_shows_invoice(pdf: bytes, invoice_no: str) -> bool:
    if type(invoice_no) is not str or not INVOICE_NO_RE.fullmatch(invoice_no):
        return False
    pattern = re.compile(f"(?<![{_TOKEN_CHAR}]){re.escape(invoice_no)}(?![{_TOKEN_CHAR}])")
    return any(pattern.search(run) for run in pdf_text_runs(pdf))


# ---------------------------------------------------------------------------
# Render (headless browser, acquire only)
# ---------------------------------------------------------------------------


class TenantRenderer(Protocol):
    def render(self, row: TenantRow, destination: Path) -> None: ...


class TenantBillAdapter:
    """The v4 source adapter: HTTP inventory, render only inside `acquire`."""

    def __init__(self, source: TenantHttpSource, renderer: TenantRenderer) -> None:
        self._source = source
        self._renderer = renderer
        self._snapshot_candidates: tuple[Candidate, ...] = ()

    def __repr__(self) -> str:
        return "TenantBillAdapter()"

    def inventory(self, safety_ceiling: int) -> InventorySnapshot:
        self._snapshot_candidates = ()
        rows = self._source.inventory(safety_ceiling)
        candidates = tuple(
            Candidate.create(
                stream=Stream.TENANT_BILL,
                source_namespace=TENANT_NAMESPACE,
                source_filename=f"{row.invoice_no}.pdf",
                # The billing month is the bill's month (owner decision); its first day is the invoice date.
                raw_date=f"{row.cycle}-01",
                date_profile=DATE_PROFILE_ISO_V1,
                evidence_ref=TENANT_EVIDENCE,
                fetch_handle=row,
            )
            for row in rows
        )
        self._snapshot_candidates = candidates
        return InventorySnapshot(
            stream=Stream.TENANT_BILL,
            source_namespace=TENANT_NAMESPACE,
            date_profile=DATE_PROFILE_ISO_V1,
            completeness_witness=TENANT_EVIDENCE,
            candidates=candidates,
        )

    def acquire(self, candidate: Candidate, destination: Path) -> str:
        row = candidate.fetch_handle
        if (
            not any(candidate is issued for issued in self._snapshot_candidates)
            or not isinstance(row, TenantRow)
            or candidate.stream is not Stream.TENANT_BILL
            or candidate.source_namespace != TENANT_NAMESPACE
        ):
            raise SourceContractError(REF_HANDLE_INVALID, stage=STAGE_RENDER)
        self._renderer.render(row, destination)
        try:
            data = destination.read_bytes()
        except OSError:
            raise SourceContractError(REF_RENDER_FAILED, stage=STAGE_RENDER) from None
        if not data.startswith(b"%PDF-") or len(data) > MAX_PDF_BYTES:
            raise SourceContractError(REF_RENDER_DOWNLOAD_UNEXPECTED, stage=STAGE_RENDER)
        if not pdf_shows_invoice(data, row.invoice_no):
            # The rendered bill must be the listed one. The file stays in the run's temp folder only.
            raise SourceContractError(REF_PDF_INVOICE_MISMATCH, stage=STAGE_RENDER)
        return candidate.source_filename


# Fixed portal surface names for the render.
TENANT_TAB_NAME = "Tenant Bill"
RENDER_VIEWPORT = {"width": 1280, "height": 1500}


class PlaywrightTenantRenderer:
    """Sign in through the reviewed browser login, open one row's preview, save its download."""

    def __init__(self, browser_config, credentials: Credentials, render_request_path: str) -> None:
        self._config = browser_config
        self._credentials = credentials
        self._request_path = render_request_path

    def __repr__(self) -> str:
        return "PlaywrightTenantRenderer()"

    def render(self, row: TenantRow, destination: Path) -> None:
        from .portal import PlaywrightPortal

        credentials = self._credentials

        class _Session(PlaywrightPortal):
            # The pair stays in this process; it is never put in the environment the browser inherits.
            @staticmethod
            def _require_runtime_credentials() -> tuple[str, str]:
                return credentials()

        deadline_s = max(30.0, float(self._config.timeout_seconds) * 3)
        try:
            with _Session(self._config) as portal:
                page = portal.page
                page.set_viewport_size(RENDER_VIEWPORT)
                requested: list[str] = []
                page.on("request", lambda request: _record_bill_request(request, self._request_path, requested))
                portal.login()
                tab = page.get_by_role("tab", name=TENANT_TAB_NAME, exact=True)
                _wait(page, lambda: tab.count() == 1, deadline_s)
                tab.click()
                groups = page.locator("flt-semantics[role=group]").filter(has_text=row.invoice_no)
                if not _wait(page, lambda: groups.count() > 0, deadline_s):
                    raise SourceContractError(REF_RENDER_ROW_NOT_VISIBLE, stage=STAGE_RENDER)
                # The innermost matching group is the table row; its first button is the preview icon.
                icon = groups.last.locator("flt-semantics[role=button]").first
                if (icon.inner_text() or "").strip():
                    raise SourceContractError(REF_RENDER_ROW_NOT_VISIBLE, stage=STAGE_RENDER)
                icon.click()
                if not _wait(page, lambda: len(requested) > 0, deadline_s):
                    raise SourceContractError(REF_RENDER_ROW_MISMATCH, stage=STAGE_RENDER)
                if requested != [row.row_id]:
                    raise SourceContractError(REF_RENDER_ROW_MISMATCH, stage=STAGE_RENDER)
                action = _download_action(page, deadline_s)
                with page.expect_download(timeout=int(deadline_s * 1000)) as info:
                    page.mouse.click(action[0], action[1])
                download = info.value
                if download.suggested_filename != DOWNLOAD_NAME or requested != [row.row_id]:
                    raise SourceContractError(REF_RENDER_DOWNLOAD_UNEXPECTED, stage=STAGE_RENDER)
                saved = destination.with_name(destination.name + ".download")
                download.save_as(str(saved))
                data = saved.read_bytes()
                saved.unlink()
        except SourceContractError:
            raise
        except Exception:
            raise SourceContractError(REF_RENDER_FAILED, stage=STAGE_RENDER) from None
        with destination.open("xb") as handle:
            handle.write(data)


def _record_bill_request(request: Any, path: str, requested: list[str]) -> None:
    try:
        if request.method != "POST" or not request.url.split("?", 1)[0].endswith(path):
            return
        body = json.loads(request.post_data or "{}")
        index = body.get("request", {}).get("billing_rec_index")
        requested.append(index if type(index) is str else "")
    except Exception:
        requested.append("")


def _wait(page: Any, condition: Callable[[], bool], deadline_s: float) -> bool:
    end = time.monotonic() + deadline_s
    while True:
        try:
            if condition():
                return True
        except Exception:
            pass
        if time.monotonic() >= end:
            return False
        page.wait_for_timeout(500)


def _download_action(page: Any, deadline_s: float) -> tuple[int, int]:
    """The preview's action bar is the lowest pair of icon buttons; download is the right one."""
    found: list[tuple[int, int]] = []

    def bar() -> bool:
        boxes = page.evaluate(
            """() => Array.from(document.querySelectorAll('flt-semantics[role=button]'))
                 .filter(e => !(e.innerText || '').trim() && !(e.getAttribute('aria-label') || '').trim())
                 .map(e => { const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })
                 .filter(b => b[2] > 0 && b[3] > 0)"""
        )
        if not boxes:
            return False
        lowest = max(box[1] for box in boxes)
        pair = sorted(box for box in boxes if abs(box[1] - lowest) < 1)
        # The preview is a bottom sheet: its bar sits near the bottom, never at the table's pager.
        if len(pair) != 2 or lowest < RENDER_VIEWPORT["height"] * 0.8 or lowest + pair[1][3] > RENDER_VIEWPORT["height"]:
            return False
        right = pair[1]
        found[:] = [(int(right[0] + right[2] / 2), int(right[1] + right[3] / 2))]
        return True

    if not _wait(page, bar, deadline_s):
        raise SourceContractError(REF_RENDER_DOWNLOAD_UNEXPECTED, stage=STAGE_RENDER)
    return found[0]
