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
    suggested_filename: str | None = None


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
        account_options: list[str] | None = None,
        default_account: str | None = None,
        account_bills: dict[str, list[SyntheticBill]] | None = None,
        displayed_account: str | None = None,
        client_side_pagination: bool = False,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self.bills = list(bills or [])
        self.page_size = page_size
        self.login_success = login_success
        self.variant = variant
        self.next_loop = next_loop
        self.account_options = list(account_options or ["SYNTHETIC-INTENDED-ACCOUNT"])
        self.default_account = default_account or self.account_options[0]
        self.account_bills = {
            name: list(values)
            for name, values in (account_bills or {self.default_account: self.bills}).items()
        }
        self.displayed_account = displayed_account
        self.client_side_pagination = client_side_pagination
        for mapped_bills in self.account_bills.values():
            for bill in mapped_bills:
                if bill not in self.bills:
                    self.bills.append(bill)
        self.download_counts: dict[str, int] = {}
        self.search_count = 0
        self.activation_count = 0
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
                """Model the public Flutter semantics gate, not an open Login link.

                The activation control lives inside the semantics placeholder, as
                on the live surface, so a successful activation removes both and
                exposes a single semantic Login button.
                """

                variant = fixture.variant
                attributes = ' type="button" data-testid="semantics-activation"'
                if variant == "hidden_activation":
                    # Still in the accessibility tree, but with a genuinely empty
                    # bounding box, so the visibility gate is what must fail --
                    # visibility:hidden would instead drop it from the tree.
                    attributes += (
                        ' style="width:0;height:0;padding:0;border:0;overflow:hidden"'
                    )
                if variant == "disabled_activation":
                    attributes += " disabled"
                activation = f"<button{attributes}>Enable accessibility</button>"
                if variant == "missing_activation":
                    activation_markup = ""
                elif variant == "ambiguous_activation":
                    activation_markup = activation * 2
                else:
                    activation_markup = activation
                login_variant = variant if variant in {
                    "missing_login",
                    "ambiguous_login",
                    "hidden_login",
                    "disabled_login",
                    "wrong_role_login",
                } else "normal"
                alert = f'<div role="alert">{escape(error)}</div>' if error else ""
                script = """
                <script>
                const placeholder = document.querySelector('flt-semantics-placeholder');
                const host = document.querySelector('flt-semantics-host');
                const keepPlaceholder = %(keep_placeholder)s;
                const loginVariant = '%(login_variant)s';
                function recordActivation() {
                    // Synchronous so the server count is recorded before the
                    // dispatched handler returns; no race with the assertion.
                    const request = new XMLHttpRequest();
                    request.open('GET', '/synthetic-activate', false);
                    request.send();
                }
                function loginControl() {
                    if (loginVariant === 'wrong_role_login') {
                        const anchor = document.createElement('a');
                        anchor.setAttribute('role', 'link');
                        anchor.href = '/login';
                        anchor.textContent = 'Login';
                        return anchor;
                    }
                    const button = document.createElement('button');
                    button.type = 'button';
                    button.textContent = 'Login';
                    if (loginVariant === 'hidden_login') {
                        // Empty bounding box, still in the accessibility tree.
                        button.style.cssText =
                            'width:0;height:0;padding:0;border:0;overflow:hidden';
                    }
                    if (loginVariant === 'disabled_login') button.disabled = true;
                    button.addEventListener('click', () => window.location.href = '/login');
                    return button;
                }
                function activate() {
                    recordActivation();
                    if (!keepPlaceholder && placeholder) placeholder.remove();
                    if (loginVariant === 'missing_login') return;
                    host.appendChild(loginControl());
                    if (loginVariant === 'ambiguous_login') host.appendChild(loginControl());
                }
                for (const control of document.querySelectorAll('[data-testid="semantics-activation"]')) {
                    control.addEventListener('click', activate);
                }
                </script>
                """ % {
                    "keep_placeholder": "true" if variant == "placeholder_persists" else "false",
                    "login_variant": login_variant,
                }
                return self._page(
                    "Energy@Grid",
                    "<flt-semantics-placeholder>"
                    + activation_markup
                    + "</flt-semantics-placeholder>"
                    + "<flt-semantics-host></flt-semantics-host>"
                    + alert
                    + script,
                )

            def _login_form(self) -> bytes:
                """Model the observed credential-entry distinction.

                This fixture models the observed typed-vs-assigned distinction,
                not the portal's internals. What was observed live is that
                assignment-based entry left the canonical Login control stably
                absent, while user-like typed entry on the same path produced
                exactly one visible, enabled, actionable Login control. Any
                account of why -- an editing host that ignores a value it did
                not observe being edited, or an incomplete form as the sole
                reason the live `EG_LOGIN_SUBMIT_NOT_APPEAR` terminal was
                reported -- is hypothesis, not measured portal behaviour.

                Keying off `keydown` reproduces that distinction
                deterministically, because a value assignment fires `input` but
                no key events while real typing fires both. Withholding the
                submit control until both fields have been typed into is a
                deliberate conservative regression-model choice: it fails on a
                regression to assignment for either credential. It is not
                asserted as the live portal's exact internal implementation or
                as a live invariant.
                """

                password_label = "Password"
                if fixture.variant == "login_label_drift":
                    password_label = "Passcode"
                script = """
                <script>
                const typed = {username: false, password: false};
                function renderSubmit() {
                    if (!typed.username || !typed.password) return;
                    if (document.getElementById('login-submit')) return;
                    const button = document.createElement('button');
                    button.id = 'login-submit';
                    button.type = 'submit';
                    button.textContent = 'Login';
                    document.getElementById('login-form').appendChild(button);
                }
                for (const name of ['username', 'password']) {
                    const input = document.getElementById(name);
                    input.addEventListener('keydown', () => {
                        typed[name] = true;
                        renderSubmit();
                    });
                }
                </script>
                """
                return self._page(
                    "Login",
                    '<form id="login-form" method=post action=/login>'
                    '<label for="username">Username</label>'
                    '<input id="username" name="username" type="text">'
                    f'<label for="password">{password_label}</label>'
                    '<input id="password" name="password" type="password">'
                    "</form>" + script,
                )

            def _authenticated_shell(self) -> str:
                """The authenticated-landing witness, and only it.

                The exact `EMS` control is what proves authentication on the
                observed surface. It is deliberately independent of Billing
                Manager, so a landing with no Billing Manager at all still
                authenticates and fails later, in navigation.
                """

                if fixture.variant == "missing_ems":
                    return ""
                if fixture.variant == "ambiguous_ems":
                    return (
                        '<button type="button">EMS</button>'
                        '<button type="button">EMS</button>'
                    )
                if fixture.variant == "hidden_ems":
                    # Still in the accessibility tree, with an empty box.
                    return (
                        '<button type="button" '
                        'style="width:0;height:0;padding:0;border:0;overflow:hidden">'
                        "EMS</button>"
                    )
                return '<button type="button">EMS</button>'

            def _app_page(self) -> bytes:
                if fixture.variant == "missing_billing_manager":
                    return self._page(
                        "Application",
                        self._authenticated_shell() + "<main>Unexpected application</main>",
                    )
                return self._page(
                    "Application",
                    self._authenticated_shell()
                    + '<a href="/billing" role="link">Billing Manager</a>',
                )

            def _billing_page(self) -> bytes:
                if fixture.variant == "missing_eb_bill":
                    return self._page(
                        "Billing",
                        self._authenticated_shell() + "<main>Unexpected billing page</main>",
                    )
                return self._page(
                    "Billing",
                    self._authenticated_shell()
                    + '<a href="/eb-bill" role="link">EB Bill</a>',
                )

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

            def _account_invoice_page(self, page: int, searched_account: str | None = None) -> bytes:
                import json

                searched = searched_account is not None
                selected_account = searched_account or fixture.default_account
                selected_bills = fixture.account_bills.get(selected_account, [])
                selected_written = False
                options: list[str] = []
                for index, account in enumerate(fixture.account_options):
                    selected = ""
                    if account == selected_account and not selected_written:
                        selected = " selected"
                        selected_written = True
                    options.append(
                        f'<option value="synthetic-account-{index}"{selected}>{escape(account)}</option>'
                    )

                def rows_markup(values: list[SyntheticBill], start: int) -> str:
                    rows: list[str] = []
                    for offset, bill in enumerate(values, start=start):
                        index = fixture.bills.index(bill)
                        button_count = 2 if fixture.variant == "ambiguous_download" else 1
                        buttons = "".join(
                            f'<button type="button" title="Download" onclick="location.href=\'/download/{index}\'">Download</button>'
                            for _ in range(button_count)
                        )
                        rows.append(
                            '<div data-testid="invoice-row" data-filename="'
                            + escape(bill.filename, quote=True)
                            + '" data-download="/download/'
                            + str(index)
                            + '">' + escape(bill.filename) + buttons + "</div>"
                        )
                    return "".join(rows)

                start = 0
                if searched:
                    start = (page - 1) * fixture.page_size
                    selected_page = selected_bills[start : start + fixture.page_size]
                    rows = rows_markup(selected_page, start)
                else:
                    selected_page = []
                    rows = ""
                if fixture.variant == "missing_invoice_list":
                    listing = "<main>No list marker</main>"
                elif rows:
                    listing = '<div data-testid="invoice-list" data-page="' + str(page) + '">' + rows + "</div>"
                else:
                    listing = '<div data-testid="invoice-list" data-page="' + str(page) + '"><div data-testid="invoice-list-empty">'
                    listing += "Search required" if not searched else "No invoices"
                    listing += "</div></div>"
                has_next = searched and (start + fixture.page_size < len(selected_bills))
                if fixture.next_loop:
                    has_next = True
                next_page = page + 1 if has_next else page
                query = urlencode({"page": next_page, "account": selected_account, "searched": "1"})
                next_button = (
                    '<button type="button" title="Next page" data-next="'
                    + str(next_page)
                    + '" onclick="location.href=\'/eb-bill?'
                    + query
                    + '\'"'
                    + ("" if has_next else " disabled")
                    + ">Next page</button>"
                )
                account_markup = "" if fixture.variant == "missing_account_selector" else (
                    '<label for="account-identity">Tenant/account</label>'
                    '<select id="account-identity" aria-label="Tenant/account">'
                    + "".join(options)
                    + "</select>"
                )
                displayed = selected_account if not searched else (fixture.displayed_account or selected_account)
                if searched and fixture.variant == "account_mismatch" and fixture.displayed_account is None:
                    displayed = "SYNTHETIC-DISPLAYED-MISMATCH"
                # The results route carries its own EB Bill nav entry, exactly as
                # the billing route does. Route proof compares the current
                # address with this control's own target, so an already-valid
                # saved results address needs no navigation click at all.
                eb_bill_markup = (
                    "" if fixture.variant == "missing_eb_bill"
                    else '<a href="/eb-bill" role="link">EB Bill</a>'
                )
                search_button = "" if fixture.variant == "missing_search" else (
                    '<button id="search-button" type="button">Search</button>'
                )
                page_payload = {
                    account: [
                        {"filename": bill.filename, "path": "/download/" + str(fixture.bills.index(bill))}
                        for bill in fixture.account_bills.get(account, [])
                    ]
                    for account in fixture.account_options
                }
                payload = json.dumps(page_payload, ensure_ascii=True).replace("<", "\\u003c")
                mismatch = fixture.variant == "account_mismatch" or fixture.displayed_account is not None
                script = f"""
                <script>
                const accountBills = {payload};
                const account = document.getElementById('account-identity');
                const selectedMarker = document.querySelector('[data-testid="selected-account"]');
                const state = document.querySelector('[data-testid="invoice-results-state"]');
                const list = document.querySelector('[data-testid="invoice-list"]');
                const next = document.querySelector('[title="Next page"]');
                const search = document.getElementById('search-button');
                const pageSize = {fixture.page_size};
                const clientSide = {'true' if fixture.client_side_pagination else 'false'};
                const nextLoop = {'true' if fixture.next_loop else 'false'};
                const ambiguousDownload = {'true' if fixture.variant == 'ambiguous_download' else 'false'};
                const mismatch = {'true' if mismatch else 'false'};
                function selectedText() {{
                    return account ? account.options[account.selectedIndex].textContent : '';
                }}
                function render(page, values) {{
                    if (!list || !next) return;
                    list.dataset.page = String(page);
                    list.replaceChildren();
                    const start = (page - 1) * pageSize;
                    const current = values.slice(start, start + pageSize);
                    for (const item of current) {{
                        const row = document.createElement('div');
                        row.dataset.testid = 'invoice-row';
                        row.dataset.filename = item.filename;
                        row.textContent = item.filename;
                        const buttonCount = ambiguousDownload ? 2 : 1;
                        for (let buttonIndex = 0; buttonIndex < buttonCount; buttonIndex++) {{
                            const button = document.createElement('button');
                            button.type = 'button';
                            button.title = 'Download';
                            button.textContent = 'Download';
                            button.addEventListener('click', () => window.location.href = item.path);
                            row.appendChild(button);
                        }}
                        list.appendChild(row);
                    }}
                    if (!current.length) {{
                        const empty = document.createElement('div');
                        empty.dataset.testid = 'invoice-list-empty';
                        empty.textContent = 'No invoices';
                        list.appendChild(empty);
                    }}
                    const hasNext = nextLoop || page * pageSize < values.length;
                    next.disabled = !hasNext;
                    next.dataset.next = String(page + 1);
                    next.onclick = () => {{
                        if (next.disabled) return;
                        if (clientSide) render(page + 1, values);
                        else window.location.href = '/eb-bill?' + new URLSearchParams({{page: page + 1, account: selectedText(), searched: '1'}}).toString();
                    }};
                }}
                if (account) account.addEventListener('change', () => selectedMarker.textContent = selectedText());
                if (search) search.addEventListener('click', () => {{
                    const selected = selectedText();
                    fetch('/synthetic-search?account=' + encodeURIComponent(selected)).then(() => {{
                        selectedMarker.textContent = mismatch ? 'SYNTHETIC-DISPLAYED-MISMATCH' : selected;
                        state.dataset.state = 'post-search';
                        state.textContent = 'Search complete';
                        render(1, accountBills[selected] || []);
                    }});
                }});
                </script>
                """
                content = (
                    eb_bill_markup
                    + account_markup
                    + '<div data-testid="selected-account">'
                    + escape(displayed)
                    + "</div>"
                    + search_button
                    + '<div data-testid="invoice-results-state" data-state="'
                    + ("post-search" if searched else "pre-search")
                    + '">'
                    + ("Search complete" if searched else "Search required")
                    + "</div>"
                    + listing
                    + next_button
                    + script
                )
                return self._page("EB Bill", content)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._send(self._login_page())
                    return
                if parsed.path == "/login":
                    self._send(self._login_form())
                    return
                if parsed.path == "/synthetic-activate":
                    fixture.activation_count += 1
                    self._send(b"ok", content_type="text/plain")
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
                if parsed.path == "/synthetic-search":
                    fixture.search_count += 1
                    self._send(b"ok", content_type="text/plain")
                    return

                if parsed.path == "/eb-bill":
                    raw_page = parse_qs(parsed.query).get("page", ["1"])[0]
                    try:
                        page = max(1, int(raw_page))
                    except ValueError:
                        page = 1
                    query = parse_qs(parsed.query)
                    searched_account = query.get("account", [None])[0] if query.get("searched") == ["1"] else None
                    self._send(self._account_invoice_page(page, searched_account=searched_account))
                    return
                if parsed.path.startswith("/download/"):
                    try:
                        index = int(parsed.path.rsplit("/", 1)[1])
                        bill = fixture.bills[index]
                    except (ValueError, IndexError):
                        self._send(b"not found", status=HTTPStatus.NOT_FOUND, content_type="text/plain")
                        return
                    fixture.download_counts[bill.filename] = fixture.download_counts.get(bill.filename, 0) + 1
                    download_name = bill.suggested_filename or bill.filename
                    if bill.mode == "error":
                        self._send(b"temporary download failure", status=HTTPStatus.INTERNAL_SERVER_ERROR, content_type="text/plain")
                    elif bill.mode == "html":
                        self._send(
                            b"<html>not a bill</html>",
                            headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
                        )
                    elif bill.mode == "zero":
                        self._send(
                            b"",
                            content_type="application/pdf",
                            headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
                        )
                    elif bill.mode == "truncated":
                        self._send(
                            b"%PDF-1.7\ntruncated",
                            content_type="application/pdf",
                            headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
                        )
                    else:
                        self._send(
                            bill.payload,
                            content_type="application/pdf",
                            headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
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
        "account_identity": "SYNTHETIC-INTENDED-ACCOUNT",
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
