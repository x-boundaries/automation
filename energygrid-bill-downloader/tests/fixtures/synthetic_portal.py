"""A tiny local HTTP portal used by browser-backed tests.

The fixture intentionally resembles only the observed interaction contract. It
has no external network path and accepts any non-empty synthetic credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse


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
    # The private accessible row text; defaults to a distinct synthetic label.
    row_text: str | None = None


class SyntheticPortalServer:
    """Threaded local server implementing the narrow portal test contract.

    After login the fixture serves the live single surface observed by the
    G3-074 census (DL-XB-199): an authenticated landing carrying the exact
    `EMS` witness, a `Billing Manager` button, a `tab "EB Bill"` beside a
    selected `tab "Tenant Bill"`, a visible account witness, an exact
    `button "Search"` and, after one Search, one role table whose invoice rows
    each carry exactly one Download button. The table deliberately carries no
    test id, no filename attribute and no href: the only invoice name is the
    download's own suggested filename.

    `variant` is a comma-separated set of behaviour switches; every switch is
    documented where it is read.
    """

    def __init__(
        self,
        bills: list[SyntheticBill] | None = None,
        *,
        login_success: bool = True,
        variant: str = "normal",
        account_text: str = "SYNTHETIC-INTENDED-ACCOUNT",
        search_delay_ms: int = 0,
        results_delay_ms: int = 0,
        pagination_sentinel: tuple[str, str] | None = None,
        aria_rowcount: str | None = None,
    ) -> None:
        self.bills = list(bills or [])
        self.login_success = login_success
        self.variant = variant
        self.variants = frozenset(item.strip() for item in variant.split(",") if item.strip())
        self.account_text = account_text
        self.search_delay_ms = search_delay_ms
        self.results_delay_ms = results_delay_ms
        self.pagination_sentinel = pagination_sentinel
        self.aria_rowcount = aria_rowcount
        self.download_counts: dict[str, int] = {}
        # Every served download request, in order, by row index.
        self.download_order: list[int] = []
        # Every Download button click, including inert ones that request nothing.
        self.download_clicks = 0
        self.search_count = 0
        self.activation_count = 0
        # Every real EMS / Billing Manager actuation this fixture observed. The
        # production path must never produce either.
        self.ems_actuation_count = 0
        self.billing_manager_actuation_count = 0
        self.eb_bill_tab_clicks = 0
        # Inspection-only pagination sentinels must never be clicked.
        self.pagination_clicks = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def row_text(self, index: int) -> str:
        """The private accessible row text of one bill: never its filename."""

        bill = self.bills[index]
        return bill.row_text if bill.row_text is not None else f"Synthetic invoice {index + 1}"

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

            def _ems_control(self) -> str:
                """The exact `EMS` control: the authentication witness.

                Authentication only ever reads its count and visibility. The
                production path never clicks it; an actuation is recorded by
                the shell script below so an accidental click is observable.
                """

                return '<button type="button" data-testid="ems-entry">EMS</button>'

            def _authenticated_shell(self) -> str:
                """The authenticated chrome: the exact EMS and Billing Manager buttons.

                Both record every actuation with a synchronous request and then
                go nowhere, so an accidental production click on either is
                counted instead of silently vanishing. The EMS button is also
                the authentication witness the landing is proven by.
                """

                control = self._ems_control()
                script = """
                <script>
                (function () {
                    function record(path) {
                        const request = new XMLHttpRequest();
                        request.open('GET', path, false);
                        request.send();
                    }
                    for (const control of document.querySelectorAll('[data-testid="ems-entry"]')) {
                        control.addEventListener('click', () => record('/synthetic-ems'));
                    }
                    const manager = document.getElementById('billing-manager');
                    if (manager) manager.addEventListener('click', () => record('/synthetic-billing-manager'));
                })();
                </script>
                """
                return (
                    control
                    + '<button type="button" id="billing-manager">Billing Manager</button>'
                    + script
                )

            def _landing_page(self) -> bytes:
                """The live single surface: tabs, account witness, Search, results.

                Switches read here:

                - ``eb_bill_preselected``: EB Bill is already the selected tab.
                - ``eb_bill_link_only``: EB Bill is a link, not a tab.
                - ``tab_inert``: clicking EB Bill selects nothing.
                - ``account_absent`` / ``account_duplicate``: zero or two
                  account witnesses outside the results.
                - ``account_in_rows``: every invoice row also carries a cell
                  with the exact account text.
                - ``two_tables``, ``header_missing``, ``ambiguous_download``,
                  ``missing_download``: results-shape drift.
                - ``reorder_after_first_download`` /
                  ``text_drift_after_first_download``: the rows change after
                  the first Download click.
                - ``tab_lost_on_download``: a Download click deselects EB Bill.
                - ``popup_on_download``: a Download click also opens a page.
                """

                variants = fixture.variants
                preselected = "eb_bill_preselected" in variants
                if "eb_bill_link_only" in variants:
                    eb_bill = '<a href="#eb-bill" id="tab-eb">EB Bill</a>'
                else:
                    eb_bill = (
                        '<div role="tab" id="tab-eb" tabindex="0" aria-selected="'
                        + ("true" if preselected else "false")
                        + '">EB Bill</div>'
                    )
                tenant = (
                    '<div role="tab" id="tab-tenant" tabindex="0" aria-selected="'
                    + ("false" if preselected else "true")
                    + '">Tenant Bill</div>'
                )
                witness = '<div class="account-witness"><span>' + escape(fixture.account_text) + "</span></div>"
                if "account_absent" in variants:
                    account = ""
                elif "account_duplicate" in variants:
                    account = witness * 2
                else:
                    account = witness
                rows = [
                    {
                        "text": fixture.row_text(index),
                        "index": index,
                        "inert": bill.mode == "inert",
                    }
                    for index, bill in enumerate(fixture.bills)
                ]
                config = {
                    "rows": rows,
                    "account": fixture.account_text,
                    "variants": sorted(variants),
                    "searchDelay": fixture.search_delay_ms,
                    "resultsDelay": fixture.results_delay_ms,
                    "pagination": list(fixture.pagination_sentinel) if fixture.pagination_sentinel else None,
                    "ariaRowcount": fixture.aria_rowcount,
                }
                payload = json.dumps(config, ensure_ascii=True).replace("<", "\\u003c")
                script = """
                <script>
                (function () {
                    const config = %(payload)s;
                    const has = (name) => config.variants.includes(name);
                    const ebTab = document.getElementById('tab-eb');
                    const tenantTab = document.getElementById('tab-tenant');
                    const ebPanel = document.getElementById('eb-panel');
                    const tenantPanel = document.getElementById('tenant-panel');
                    const searchSlot = document.getElementById('search-slot');
                    const results = document.getElementById('results');
                    let downloadClicks = 0;
                    function record(path) {
                        const request = new XMLHttpRequest();
                        request.open('GET', path, false);
                        request.send();
                    }
                    function renderSearch() {
                        if (searchSlot.childElementCount) return;
                        const search = document.createElement('button');
                        search.type = 'button';
                        search.textContent = 'Search';
                        search.addEventListener('click', () => {
                            record('/synthetic-search');
                            setTimeout(renderResults, config.resultsDelay);
                        });
                        searchSlot.appendChild(search);
                    }
                    function select(which) {
                        const eb = which === 'eb';
                        if (ebTab.getAttribute('role') === 'tab') ebTab.setAttribute('aria-selected', eb ? 'true' : 'false');
                        tenantTab.setAttribute('aria-selected', eb ? 'false' : 'true');
                        ebPanel.hidden = !eb;
                        tenantPanel.hidden = eb;
                        if (eb) setTimeout(renderSearch, config.searchDelay);
                    }
                    function cell(content) {
                        const node = document.createElement('span');
                        node.setAttribute('role', 'cell');
                        if (typeof content === 'string') node.textContent = content;
                        else if (content) node.appendChild(content);
                        return node;
                    }
                    function downloadButton(item) {
                        const button = document.createElement('button');
                        button.type = 'button';
                        button.textContent = 'Download';
                        button.addEventListener('click', () => {
                            downloadClicks += 1;
                            record('/synthetic-download-click');
                            if (has('popup_on_download')) window.open('/synthetic-popup');
                            if (has('tab_lost_on_download')) select('tenant');
                            if (!item.inert) window.location.href = '/download/' + item.index;
                            if (downloadClicks === 1 && has('reorder_after_first_download')) {
                                setTimeout(() => {
                                    const table = results.querySelector('[role="table"]');
                                    const rows = table.querySelectorAll('[role="row"]');
                                    if (rows.length > 2) table.appendChild(rows[1]);
                                }, 0);
                            }
                            if (downloadClicks === 1 && has('text_drift_after_first_download')) {
                                setTimeout(() => {
                                    const first = results.querySelector('[role="row"]:nth-child(2) [role="cell"]');
                                    if (first) first.textContent = first.textContent + ' (viewed)';
                                }, 0);
                            }
                        });
                        return button;
                    }
                    function table() {
                        const node = document.createElement('div');
                        node.setAttribute('role', 'table');
                        node.setAttribute('aria-label', 'Invoices');
                        if (config.ariaRowcount !== null) node.setAttribute('aria-rowcount', config.ariaRowcount);
                        if (!has('header_missing')) {
                            const header = document.createElement('div');
                            header.setAttribute('role', 'row');
                            for (const label of ['Invoice', 'Action']) {
                                const heading = document.createElement('span');
                                heading.setAttribute('role', 'columnheader');
                                heading.textContent = label;
                                header.appendChild(heading);
                            }
                            node.appendChild(header);
                        }
                        config.rows.forEach((item, position) => {
                            const row = document.createElement('div');
                            row.setAttribute('role', 'row');
                            row.appendChild(cell(item.text));
                            if (has('account_in_rows')) row.appendChild(cell(config.account));
                            const actions = document.createElement('span');
                            actions.setAttribute('role', 'cell');
                            const buttons = has('missing_download') && position === 0 ? 0
                                : has('ambiguous_download') && position === 0 ? 2 : 1;
                            for (let count = 0; count < buttons; count++) actions.appendChild(downloadButton(item));
                            row.appendChild(actions);
                            node.appendChild(row);
                        });
                        return node;
                    }
                    function renderResults() {
                        results.replaceChildren(table());
                        if (has('two_tables')) results.appendChild(table());
                        if (config.pagination) {
                            const [role, name] = config.pagination;
                            const control = document.createElement(role === 'link' ? 'a' : 'button');
                            if (role === 'link') control.href = '#more';
                            else control.type = 'button';
                            control.textContent = name;
                            control.addEventListener('click', () => record('/synthetic-pagination'));
                            results.appendChild(control);
                        }
                    }
                    ebTab.addEventListener('click', () => {
                        record('/synthetic-eb-tab');
                        if (!has('tab_inert')) select('eb');
                    });
                    tenantTab.addEventListener('click', () => select('tenant'));
                    if (has('eb_bill_preselected')) select('eb');
                })();
                </script>
                """ % {"payload": payload}
                content = (
                    self._authenticated_shell()
                    + '<div role="tablist" aria-label="Bills">'
                    + eb_bill
                    + tenant
                    + "</div>"
                    + '<section id="tenant-panel"' + (" hidden" if preselected else "") + ">"
                    + "<p>Tenant bills</p></section>"
                    + '<section id="eb-panel"' + ("" if preselected else " hidden") + ">"
                    + account
                    + '<div id="search-slot"></div>'
                    + '<div id="results"></div>'
                    + "</section>"
                    + script
                )
                return self._page("Energy@Grid", content)
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
                    self._send(self._landing_page())
                    return
                counters = {
                    "/synthetic-ems": "ems_actuation_count",
                    "/synthetic-billing-manager": "billing_manager_actuation_count",
                    "/synthetic-eb-tab": "eb_bill_tab_clicks",
                    "/synthetic-search": "search_count",
                    "/synthetic-download-click": "download_clicks",
                    "/synthetic-pagination": "pagination_clicks",
                }
                if parsed.path in counters:
                    name = counters[parsed.path]
                    setattr(fixture, name, getattr(fixture, name) + 1)
                    self._send(b"ok", content_type="text/plain")
                    return
                if parsed.path == "/synthetic-popup":
                    self._send(self._page("Popup", "<p>popup</p>"))
                    return
                if parsed.path.startswith("/download/"):
                    try:
                        index = int(parsed.path.rsplit("/", 1)[1])
                        bill = fixture.bills[index]
                    except (ValueError, IndexError):
                        self._send(b"not found", status=HTTPStatus.NOT_FOUND, content_type="text/plain")
                        return
                    fixture.download_counts[bill.filename] = fixture.download_counts.get(bill.filename, 0) + 1
                    fixture.download_order.append(index)
                    download_name = bill.suggested_filename or bill.filename
                    disposition = {"Content-Disposition": f'attachment; filename="{download_name}"'}
                    if bill.mode == "html":
                        self._send(b"<html>not a bill</html>", headers=disposition)
                    elif bill.mode == "zero":
                        self._send(b"", content_type="application/pdf", headers=disposition)
                    elif bill.mode == "truncated":
                        self._send(b"%PDF-1.7\ntruncated", content_type="application/pdf", headers=disposition)
                    else:
                        self._send(bill.payload, content_type="application/pdf", headers=disposition)
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
