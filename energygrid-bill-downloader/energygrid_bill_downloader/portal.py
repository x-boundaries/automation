from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RuntimeConfig
from .errors import DependencyError, DownloadError, LayoutChangedError, LoginError


# The fixed marker for the login submit stage. Defined once so the recovery
# below and the step marker in `login()` can never drift apart, which is what
# keeps a proven pre-submit failure mapping to the same support reference.
LOGIN_SUBMIT_STAGE = "login submission did not complete"

# Bounded pre-submit recovery for the Flutter login route
# (DL-XB-141-LOGIN-RECOVERY-001). The canonical Login control can be briefly
# unresolvable while the login route settles, and a blocking action started
# inside that window spends the whole page timeout instead of looking again.
# The ladder increases, no single yield reaches _MAX_SUBMIT_RECOVERY_YIELD_MS,
# and the sum stays inside _SUBMIT_RECOVERY_DEADLINE_SECONDS, so the worst
# case is a bounded recovery rather than a multi-minute hold.
_SUBMIT_RECOVERY_YIELDS_MS = (100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600)
_MAX_SUBMIT_RECOVERY_YIELD_MS = 30000
_SUBMIT_RECOVERY_DEADLINE_SECONDS = 60.0

# Every actionability probe is bounded explicitly. Without a timeout argument a
# trial click inherits `page.set_default_timeout()`, and `RuntimeConfig` allows
# `timeout_seconds` up to MAX_TIMEOUT_SECONDS, so one probe could park for
# minutes -- a monotonic deadline cannot interrupt a call that is already
# blocking. The cap is deliberately small: the recovery is supposed to yield
# and re-resolve, not sit inside a single Playwright wait.
_MAX_SUBMIT_TRIAL_TIMEOUT_MS = 1000


@dataclass(frozen=True)
class BillRef:
    filename: str
    page_url: str


class PlaywrightPortal:
    """The only module that knows the portal DOM contract."""

    def __init__(self, config: RuntimeConfig, headed: bool = False) -> None:
        self.config = config
        self.headed = headed
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self._page_bindings: dict[str, tuple[int, str]] = {}
        self._results_entry_url: str | None = None

    def __enter__(self) -> "PlaywrightPortal":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise DependencyError("Playwright Python is not installed") from exc

        if self.config.browser_cache_path is not None:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(self.config.browser_cache_path)
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(headless=not self.headed)
            self.context = self.browser.new_context(accept_downloads=True)
            self.page = self.context.new_page()
            self.page.set_default_timeout(self.config.timeout_seconds * 1000)
            return self
        except Exception:
            self.close()
            raise DependencyError("Playwright Chromium could not be started")

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        for resource in (self.context, self.browser, self.playwright):
            if resource is None:
                continue
            try:
                resource.close() if resource is not self.playwright else resource.stop()
            except Exception:
                pass
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    def login(self) -> None:
        username = os.environ.get("ENERGYGRID_USERNAME")
        password = os.environ.get("ENERGYGRID_PASSWORD")
        if not username or not password:
            raise LoginError("runtime credentials are unavailable")
        page = self._require_page()
        # Names the step that is about to run, so the generic arm below reports
        # which one failed instead of one message for the whole sequence. It is
        # a fixed internal marker: never a selector, URL, credential, account
        # identity, or anything read back from the page. The sequence itself is
        # unchanged -- the Login entry is resolved and clicked in two statements
        # rather than one so that a generic failure inside the semantics gate
        # stays distinguishable from a failure to click what it returned.
        stage_failure = "portal navigation did not complete"
        try:
            page.goto(self.config.portal_url, wait_until="domcontentloaded")
            stage_failure = "Flutter semantics activation dispatch did not complete"
            login_entry = self._enter_public_semantics(page)
            stage_failure = "post-activation Login control click did not complete"
            login_entry.click()
            stage_failure = "login username entry did not complete"
            page.get_by_label("Username", exact=True).fill(username)
            stage_failure = "login password entry did not complete"
            page.get_by_label("Password", exact=True).fill(password)
            stage_failure = LOGIN_SUBMIT_STAGE
            self._submit_login(page)
            stage_failure = "Billing Manager entry did not appear after login"
            page.get_by_role("link", name="Billing Manager", exact=True).wait_for(state="visible")
        except LayoutChangedError:
            # A proven pre-auth contract failure must not be reclassified as a
            # credential rejection just because the page also renders an alert.
            raise
        except Exception as exc:
            if self._visible(page, page.get_by_role("alert")):
                raise LoginError("portal rejected the login") from exc
            raise LayoutChangedError(stage_failure) from exc

    def _submit_login(self, page: Any) -> None:
        """Click the canonical Login control exactly once, after proving it ready.

        The selector never changes. What changes between attempts is the
        locator object: the failure this recovers from is a stale handle held
        across a long auto-wait while the login route is still settling, so
        every attempt resolves a new one and inspects it without starting a
        wait that could swallow the whole page timeout.

        Exactly one normal click is ever dispatched. A submit that has already
        been sent may have landed even if the call raises, so retrying it
        could duplicate the submission; that failure goes to the caller's
        classification instead.
        """

        deadline = time.monotonic() + _SUBMIT_RECOVERY_DEADLINE_SECONDS
        attempt = 0
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            submit = page.get_by_role("button", name="Login", exact=True)
            if self._submit_control_is_ready(submit):
                try:
                    # Proves Playwright can act on the control without
                    # submitting anything, so a control that is present but
                    # not yet actionable is retried rather than blocked on.
                    # The timeout is explicit: an inherited page default could
                    # outlast the whole recovery budget on its own.
                    submit.click(
                        trial=True,
                        timeout=min(remaining_ms, _MAX_SUBMIT_TRIAL_TIMEOUT_MS),
                    )
                except Exception as exc:
                    # Only a timeout is transient here. Anything else is a real
                    # failure and must reach the caller unaltered.
                    if not self._looks_like_timeout(exc):
                        raise
                else:
                    submit.click()
                    return
            if attempt >= len(_SUBMIT_RECOVERY_YIELDS_MS):
                break
            # Recomputed: the probe above consumed part of the same budget.
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            page.wait_for_timeout(
                min(_SUBMIT_RECOVERY_YIELDS_MS[attempt], _MAX_SUBMIT_RECOVERY_YIELD_MS, remaining_ms)
            )
            attempt += 1
        raise LayoutChangedError(LOGIN_SUBMIT_STAGE)

    @staticmethod
    def _submit_control_is_ready(locator: Any) -> bool:
        """Report readiness immediately, without starting a long auto-wait.

        Ambiguity is drift, not lag: more than one exact match can never
        become correct by waiting, so it fails closed on the spot rather than
        consuming the recovery budget.
        """

        count = locator.count()
        if count > 1:
            raise LayoutChangedError(LOGIN_SUBMIT_STAGE)
        if count == 0:
            return False
        return bool(locator.is_visible() and locator.is_enabled())

    def _enter_public_semantics(self, page: Any) -> Any:
        """Open the public Flutter semantics gate and return the Login entry.

        The public pre-auth page is a Flutter surface whose only initially
        exposed control is the accessibility semantics activation button.
        Application semantics -- and with them the Login entry -- appear only
        after exactly one dispatched click. Every precondition is proven before
        that dispatch, so a changed public contract fails closed without the
        page being interacted with at all.
        """

        activation = page.get_by_role("button", name="Enable accessibility", exact=True)
        self._await_control(activation, "Flutter semantics activation control")
        self._require_single_ready_control(activation, "Flutter semantics activation control")

        # Exactly one activation per login attempt: no retry, no fallback
        # activation mechanism, and no second dispatch on any path below.
        activation.dispatch_event("click")

        try:
            page.locator("flt-semantics-placeholder").wait_for(state="detached")
        except Exception as exc:
            raise LayoutChangedError(
                "Flutter semantics placeholder remained after activation"
            ) from exc

        login_entry = page.get_by_role("button", name="Login", exact=True)
        self._await_control(login_entry, "post-activation Login control")
        self._require_single_ready_control(login_entry, "post-activation Login control")
        return login_entry

    @staticmethod
    def _await_control(locator: Any, description: str) -> None:
        """Wait, within the configured page timeout, for at least one match."""

        try:
            locator.first.wait_for(state="attached")
        except Exception as exc:
            raise LayoutChangedError(f"{description} did not appear") from exc

    @staticmethod
    def _require_single_ready_control(locator: Any, description: str) -> None:
        """Fail closed unless exactly one match is present, visible and enabled."""

        try:
            count = locator.count()
            # Short-circuits so an ambiguous match is never asked for state.
            ready = count == 1 and locator.is_visible() and locator.is_enabled()
        except Exception as exc:
            raise LayoutChangedError(f"{description} could not be resolved") from exc
        if count != 1:
            raise LayoutChangedError(f"{description} is missing or ambiguous")
        if not ready:
            raise LayoutChangedError(f"{description} is hidden or disabled")

    def inventory(self, safety_ceiling: int) -> list[BillRef]:
        page = self._require_page()
        self._page_bindings = {}
        self._results_entry_url = None
        self._open_verified_results()
        self._results_entry_url = page.url
        bills: list[BillRef] = []
        seen_pages: set[str] = set()
        page_ordinal = 1
        while True:
            list_container = page.get_by_test_id("invoice-list")
            try:
                if list_container.count() != 1:
                    raise LayoutChangedError("invoice list container is missing or ambiguous")
                list_container.wait_for(state="visible")
            except Exception as exc:
                if isinstance(exc, LayoutChangedError):
                    raise
                raise LayoutChangedError("invoice list container is missing") from exc
            page_marker = self._page_marker(page, list_container)
            if page_marker in seen_pages:
                raise LayoutChangedError("invoice pagination repeated a page")
            seen_pages.add(page_marker)

            if len(seen_pages) > safety_ceiling:
                raise LayoutChangedError("invoice pagination safety ceiling exceeded")
            rows = page.get_by_test_id("invoice-row")
            row_count = rows.count()
            if row_count == 0:
                if not self._visible(page, page.get_by_test_id("invoice-list-empty")):
                    raise LayoutChangedError("invoice list has neither rows nor an empty marker")
            for row in rows.all():
                filename = row.get_attribute("data-filename")
                if not filename:
                    raise LayoutChangedError("invoice row has no filename identity")
                if row.get_by_role("button", name="Download", exact=True).count() != 1:
                    raise LayoutChangedError("invoice row has an ambiguous download control")
                if filename in self._page_bindings:
                    raise LayoutChangedError("invoice filename is duplicated across the inventory")
                bill = BillRef(filename=filename, page_url=page.url)
                self._page_bindings[filename] = (page_ordinal, page_marker)
                bills.append(bill)
                if len(bills) > safety_ceiling:
                    raise LayoutChangedError("inventory safety ceiling exceeded")

            next_button = page.get_by_role("button", name="Next page", exact=True)
            if next_button.count() != 1:
                raise LayoutChangedError("invoice pagination control is missing or ambiguous")
            if next_button.is_disabled():
                return bills
            old_marker = page_marker
            old_url = page.url
            next_button.click()
            try:
                page.wait_for_function(
                    """([selector, old_marker, old_url]) => {
                        const list = document.querySelector(selector);
                        const marker = list?.getAttribute('data-page') || window.location.href;
                        return marker !== old_marker || window.location.href !== old_url;
                    }""",
                    arg=["[data-testid='invoice-list']", old_marker, old_url],
                )
            except Exception as exc:
                raise LayoutChangedError("invoice pagination did not advance") from exc
            page_ordinal += 1

    def download(self, bill: BillRef, destination: Path) -> str:
        page = self._require_page()
        try:
            binding = self._page_bindings.get(bill.filename)
            if binding is None or self._results_entry_url is None:
                raise LayoutChangedError("invoice page binding is missing or expired")
            self._open_verified_results(entry_url=self._results_entry_url)
            self._restore_page(binding)
            rows = page.get_by_test_id("invoice-row")
            matching_rows = []
            for row in rows.all():
                if row.get_attribute("data-filename") == bill.filename:
                    matching_rows.append(row)
            if len(matching_rows) != 1:
                raise LayoutChangedError("invoice row identity is missing or ambiguous")
            download_controls = matching_rows[0].get_by_role("button", name="Download", exact=True)
            if download_controls.count() != 1:
                raise LayoutChangedError("invoice row download control is missing or ambiguous")
            with page.expect_download() as download_info:
                download_controls.click()
            download = download_info.value
            if download.failure():
                raise DownloadError("browser download failed")
            suggested_filename = download.suggested_filename
            if not suggested_filename:
                raise DownloadError("browser did not provide a suggested filename", retryable=False)
            download.save_as(destination)
            return suggested_filename
        except (LayoutChangedError, DownloadError):
            raise
        except Exception as exc:
            if self._looks_like_timeout(exc):
                raise DownloadError("browser download timed out") from exc
            raise DownloadError("browser download could not be completed") from exc

    def _open_verified_results(self, entry_url: str | None = None) -> Any:
        page = self._require_page()
        try:
            if entry_url is not None:
                page.goto(entry_url, wait_until="domcontentloaded")
            account_control = page.get_by_label("Tenant/account", exact=True)
            if account_control.count() != 1:
                billing_manager = page.get_by_role("link", name="Billing Manager", exact=True)
                if billing_manager.count() != 1:
                    raise LayoutChangedError("Billing Manager control is missing or ambiguous")
                billing_manager.click()
                eb_bill = page.get_by_role("link", name="EB Bill", exact=True)
                if eb_bill.count() != 1:
                    raise LayoutChangedError("EB Bill control is missing or ambiguous")
                eb_bill.click()
                account_control = page.get_by_label("Tenant/account", exact=True)
            if account_control.count() != 1:
                raise LayoutChangedError("tenant/account selector is missing or ambiguous")
            options = account_control.locator("option")
            option_texts = options.all_text_contents()
            matches = [index for index, text in enumerate(option_texts) if text.strip() == self.config.account_identity]
            if len(matches) != 1:
                raise LayoutChangedError("intended tenant/account identity is missing or ambiguous")
            option_value = options.nth(matches[0]).get_attribute("value")
            if not option_value:
                raise LayoutChangedError("intended tenant/account option has no stable value")
            account_control.select_option(value=option_value)
            self._verify_account_binding(page, account_control)

            search = page.get_by_role("button", name="Search", exact=True)
            if search.count() != 1:
                raise LayoutChangedError("Search control is missing or ambiguous")
            search.click()
            state = page.get_by_test_id("invoice-results-state")
            if state.count() != 1:
                raise LayoutChangedError("invoice result state marker is missing or ambiguous")
            page.wait_for_function(
                "selector => document.querySelector(selector)?.getAttribute('data-state') === 'post-search'",
                arg="[data-testid='invoice-results-state']",
            )
            if state.get_attribute("data-state") != "post-search":
                raise LayoutChangedError("invoice results are not confirmed post-search")
            self._verify_account_binding(page, account_control)
            list_container = page.get_by_test_id("invoice-list")
            if list_container.count() != 1:
                raise LayoutChangedError("invoice list container is missing or ambiguous")
            list_container.wait_for(state="visible")
            return list_container
        except LayoutChangedError:
            raise
        except Exception as exc:
            raise LayoutChangedError("tenant/account search result contract changed") from exc

    def _verify_account_binding(self, page: Any, account_control: Any) -> None:
        checked = account_control.locator("option:checked")
        if checked.count() != 1:
            raise LayoutChangedError("selected tenant/account identity is missing or ambiguous")
        selected_text = checked.first.text_content()
        if selected_text is None or selected_text.strip() != self.config.account_identity:
            raise LayoutChangedError("selected tenant/account identity does not match configuration")
        displayed = page.get_by_test_id("selected-account")
        if displayed.count() != 1:
            raise LayoutChangedError("displayed tenant/account identity is missing or ambiguous")
        displayed_text = displayed.first.text_content()
        if displayed_text is None or displayed_text.strip() != self.config.account_identity:
            raise LayoutChangedError("displayed tenant/account identity does not match configuration")

    @staticmethod
    def _page_marker(page: Any, list_container: Any) -> str:
        return list_container.get_attribute("data-page") or page.url

    def _restore_page(self, binding: tuple[int, str]) -> None:
        page = self._require_page()
        page_ordinal, expected_marker = binding
        if page_ordinal < 1:
            raise LayoutChangedError("invoice page binding has an invalid ordinal")
        list_container = page.get_by_test_id("invoice-list")
        if list_container.count() != 1:
            raise LayoutChangedError("invoice list container is missing or ambiguous")
        current_marker = self._page_marker(page, list_container)
        seen_markers = {current_marker}
        for _ in range(1, page_ordinal):
            next_button = page.get_by_role("button", name="Next page", exact=True)
            if next_button.count() != 1 or next_button.is_disabled():
                raise LayoutChangedError("invoice page binding cannot be replayed")
            old_marker = current_marker
            old_url = page.url
            next_button.click()
            try:
                page.wait_for_function(
                    """([selector, old_marker, old_url]) => {
                        const list = document.querySelector(selector);
                        const marker = list?.getAttribute('data-page') || window.location.href;
                        return marker !== old_marker || window.location.href !== old_url;
                    }""",
                    arg=["[data-testid='invoice-list']", old_marker, old_url],
                )
            except Exception as exc:
                raise LayoutChangedError("invoice page replay did not advance") from exc
            list_container = page.get_by_test_id("invoice-list")
            current_marker = self._page_marker(page, list_container)
            if current_marker == old_marker or current_marker in seen_markers:
                raise LayoutChangedError("invoice page replay repeated or lost its marker")
            seen_markers.add(current_marker)
        if current_marker != expected_marker:
            raise LayoutChangedError("invoice page replay reached an unexpected marker")

    def _require_page(self) -> Any:
        if self.page is None:
            raise DependencyError("browser page is not open")
        return self.page

    @staticmethod
    def _visible(page: Any, locator: Any) -> bool:
        try:
            return locator.is_visible(timeout=250)
        except Exception:
            return False

    @staticmethod
    def _looks_like_timeout(error: Exception) -> bool:
        return "timeout" in str(error).casefold() or error.__class__.__name__.endswith("TimeoutError")
