"""A tiny local HTTP portal used by browser-backed tests.

The fixture intentionally resembles only the observed interaction contract. It
has no external network path and accepts any non-empty synthetic credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlencode, urlparse


def synthetic_pdf(marker: bytes = b"synthetic-bill") -> bytes:
    """Return a small structurally plausible PDF payload for tests."""

    return (
        b"%PDF-1.7\n"
        b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
        b"2 0 obj\n<< /Length 15 >>\nstream\n"
        + marker[:15].ljust(15, b" ")
        + b"\nendstream\nendobj\n"
        b"xref\n0 3\n0000000000 65535 f \n"
        b"trailer\n<< /Size 3 >>\nstartxref\n0\n%%EOF\n"
    )


@dataclass(frozen=True)
class SyntheticBill:
    filename: str
    payload: bytes = field(default_factory=synthetic_pdf)
    mode: str = "success"


class SyntheticPortalServer:
    """Threaded local server implementing the narrow portal test contract."""

    def __init__(
        self,
        bills: list[SyntheticBill] | None = None,
        *,
        page_size: int = 2,
        login_success: bool = True,
        variant: str = "normal",
        next_loop: bool = False,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self.bills = list(bills or [])
        self.page_size = page_size
        self.login_success = login_success
        self.variant = variant
        self.next_loop = next_loop
        self.download_counts: dict[str, int] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("server is not started")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "SyntheticPortalServer":
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SyntheticEnergyGrid/1.0"
            sys_version = ""

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _send(
                self,
                body: bytes,
                *,
                status: HTTPStatus = HTTPStatus.OK,
                content_type: str = "text/html; charset=utf-8",
                headers: dict[str, str] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self, path: str, *, cookie: bool = False) -> None:
                headers = {"Location": path}
                if cookie:
                    headers["Set-Cookie"] = "synthetic_session=1; Path=/"
                self._send(b"", status=HTTPStatus.SEE_OTHER, headers=headers)

            def _authed(self) -> bool:
                return "synthetic_session=1" in self.headers.get("Cookie", "")

            def _page(self, title: str, content: str) -> bytes:
                return (
                    "<!doctype html><html><head><title>"
                    + escape(title)
                    + "</title></head><body>"
                    + content
                    + "</body></html>"
                ).encode("utf-8")

            def _login_page(self, error: str = "") -> bytes:
                login_link = "" if fixture.variant == "missing_login" else (
                    '<a href="/login" role="link">Login</a>'
                )
                alert = f'<div role="alert">{escape(error)}</div>' if error else ""
                return self._page(
                    "Energy@Grid",
                    login_link + alert,
                )

            def _login_form(self) -> bytes:
                password_label = "Password"
                if fixture.variant == "login_label_drift":
                    password_label = "Passcode"
                return self._page(
                    "Login",
                    "<form method=post action=/login>"
                    '<label for="username">Username</label>'
                    '<input id="username" name="username" type="text">'
                    f'<label for="password">{password_label}</label>'
                    '<input id="password" name="password" type="password">'
                    '<button type="submit">Login</button>'
                    "</form>",
                )

            def _app_page(self) -> bytes:
                if fixture.variant == "missing_billing_manager":
                    return self._page("Application", "<main>Unexpected application</main>")
                return self._page(
                    "Application",
                    '<a href="/billing" role="link">Billing Manager</a>',
                )

            def _billing_page(self) -> bytes:
                if fixture.variant == "missing_eb_bill":
                    return self._page("Billing", "<main>Unexpected billing page</main>")
                return self._page("Billing", '<a href="/eb-bill" role="link">EB Bill</a>')

            def _invoice_page(self, page: int) -> bytes:
                start = (page - 1) * fixture.page_size
                selected = fixture.bills[start : start + fixture.page_size]
                rows: list[str] = []
                for index, bill in enumerate(selected, start=start):
                    button_count = 2 if fixture.variant == "ambiguous_download" else 1
                    buttons = "".join(
                        '<button type="button" title="Download" onclick="location.href=\'/download/'
                        + str(index)
                        + '\'">Download</button>'
                        for _ in range(button_count)
                    )
                    rows.append(
                        '<div data-testid="invoice-row" data-filename="'
                        + escape(bill.filename, quote=True)
                        + '" data-download="/download/'
                        + str(index)
                        + '">' + escape(bill.filename) + buttons + "</div>"
                    )
                if fixture.variant == "missing_invoice_list":
                    listing = "<main>No list marker</main>"
                elif rows:
                    listing = '<div data-testid="invoice-list" data-page="' + str(page) + '">' + "".join(rows) + "</div>"
                else:
                    listing = '<div data-testid="invoice-list" data-page="' + str(page) + '"><div data-testid="invoice-list-empty">No invoices</div></div>'
                has_next = start + fixture.page_size < len(fixture.bills)
                if fixture.next_loop:
                    has_next = True
                next_button = (
                    '<button type="button" title="Next page" data-next="'
                    + str(page + 1 if has_next else page)
                    + '" onclick="location.href=\'/eb-bill?page='
                    + str(page + 1 if has_next else page)
                    + '\'"'
                    + ("" if has_next else " disabled")
                    + ">Next page</button>"
                )
                return self._page("EB Bill", listing + next_button)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._send(self._login_page())
                    return
                if parsed.path == "/login":
                    self._send(self._login_form())
                    return
                if not self._authed():
                    self._send(self._login_page("Session expired"), status=HTTPStatus.UNAUTHORIZED)
                    return
                if parsed.path == "/app":
                    self._send(self._app_page())
                    return
                if parsed.path == "/billing":
                    self._send(self._billing_page())
                    return
                if parsed.path == "/eb-bill":
                    raw_page = parse_qs(parsed.query).get("page", ["1"])[0]
                    try:
                        page = max(1, int(raw_page))
                    except ValueError:
                        page = 1
                    self._send(self._invoice_page(page))
                    return
                if parsed.path.startswith("/download/"):
                    try:
                        index = int(parsed.path.rsplit("/", 1)[1])
                        bill = fixture.bills[index]
                    except (ValueError, IndexError):
                        self._send(b"not found", status=HTTPStatus.NOT_FOUND, content_type="text/plain")
                        return
                    fixture.download_counts[bill.filename] = fixture.download_counts.get(bill.filename, 0) + 1
                    if bill.mode == "error":
                        self._send(b"temporary download failure", status=HTTPStatus.INTERNAL_SERVER_ERROR, content_type="text/plain")
                    elif bill.mode == "html":
                        self._send(
                            b"<html>not a bill</html>",
                            headers={"Content-Disposition": f'attachment; filename="{bill.filename}"'},
                        )
                    elif bill.mode == "zero":
                        self._send(
                            b"",
                            content_type="application/pdf",
                            headers={"Content-Disposition": f'attachment; filename="{bill.filename}"'},
                        )
                    elif bill.mode == "truncated":
                        self._send(
                            b"%PDF-1.7\ntruncated",
                            content_type="application/pdf",
                            headers={"Content-Disposition": f'attachment; filename="{bill.filename}"'},
                        )
                    else:
                        self._send(
                            bill.payload,
                            content_type="application/pdf",
                            headers={"Content-Disposition": f'attachment; filename="{bill.filename}"'},
                        )
                    return
                self._send(b"not found", status=HTTPStatus.NOT_FOUND, content_type="text/plain")

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                if urlparse(self.path).path != "/login":
                    self._send(b"not found", status=HTTPStatus.NOT_FOUND, content_type="text/plain")
                    return
                length = int(self.headers.get("Content-Length", "0"))
                values = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
                if fixture.login_success and values.get("username") and values.get("password"):
                    self._redirect("/app", cookie=True)
                else:
                    self._send(self._login_form() + b'<div role="alert">Login failed</div>', status=HTTPStatus.UNAUTHORIZED)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None


def write_config(path: Path, server: SyntheticPortalServer, root: Path, **overrides: object) -> None:
    """Write a synthetic config without credentials or live paths."""

    import json

    values: dict[str, object] = {
        "portal_url": server.base_url,
        "archive_root": str(root / "archive"),
        "state_path": str(root / "state" / "state.sqlite3"),
        "temp_root": str(root / "temp"),
        "log_root": str(root / "logs"),
        "timeout_seconds": 5,
        "max_attempts": 2,
        "inventory_safety_ceiling": 50,
    }
    values.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
