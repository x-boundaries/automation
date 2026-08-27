from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RuntimeConfig
from .errors import DependencyError, DownloadError, LayoutChangedError, LoginError


# The fixed marker for the login submit stage. Defined once so the recovery
# below and the step marker in `login()` can never drift apart, which is what
# keeps a proven pre-submit failure mapping to the same support reference.
LOGIN_SUBMIT_STAGE = "login submission did not complete"

# One shared bounded readiness and post-action settling contract for every
# portal surface (DL-XB-141-PORTAL-RESILIENCE-002, superseding the login-only
# ladder of DL-XB-141-LOGIN-RECOVERY-001). The portal is eventually consistent:
# a control can be briefly unresolvable, ambiguous, hidden, disabled or
# not-yet-actionable while a Flutter route settles, and a blocking action
# started inside that window spends the whole page timeout instead of looking
# again.
#
# The checkpoints are ABSOLUTE ELAPSED offsets, not additive sleeps: the first
# look is immediate, so a healthy interaction pays nothing, and a later look
# happens once that much time has passed since the recovery began. A probe that
# already consumed a checkpoint therefore yields nothing extra.
PORTAL_RECOVERY_CHECKPOINTS_MS = (250, 1000, 5000, 10000, 30000)
PORTAL_RECOVERY_DEADLINE_SECONDS = 60.0

# Every probe that can wait is bounded explicitly, and they share one cap so a
# second waiting call cannot appear with a budget of its own. Without a timeout
# argument a trial click, an enabled check or a `wait_for` inherits
# `page.set_default_timeout()`, and `RuntimeConfig` allows `timeout_seconds` up
# to MAX_TIMEOUT_SECONDS, so one probe could park for minutes -- a monotonic
# deadline cannot interrupt a call that is already blocking. The cap is
# deliberately small: recovery is supposed to yield and re-resolve, not sit
# inside a single Playwright wait.
MAX_PORTAL_PROBE_TIMEOUT_MS = 1000

# Room for the bounded probes the final look will run, so that last fresh check
# completes inside the hard deadline instead of straddling it.
_PORTAL_FINAL_CHECK_MARGIN_MS = 2 * MAX_PORTAL_PROBE_TIMEOUT_MS
PORTAL_RECOVERY_ATTEMPTS_MS = (
    (0,)
    + PORTAL_RECOVERY_CHECKPOINTS_MS
    + (int(PORTAL_RECOVERY_DEADLINE_SECONDS * 1000) - _PORTAL_FINAL_CHECK_MARGIN_MS,)
)

# What a checkpoint observed. Only READY ends a recovery successfully; the rest
# are transient inside the window and name the fail-closed message once the
# deadline passes. UNRESOLVED is the exception: a locator that cannot be
# resolved at all is drift, and drift never becomes correct by waiting.
_PORTAL_READY = "ready"
_PORTAL_ABSENT = "absent"
_PORTAL_AMBIGUOUS = "ambiguous"
_PORTAL_NOT_READY = "not ready"
_PORTAL_UNRESOLVED = "unresolved"

_PORTAL_FAILURE_SUFFIXES = {
    _PORTAL_ABSENT: "did not appear",
    _PORTAL_AMBIGUOUS: "is missing or ambiguous",
    _PORTAL_NOT_READY: "is hidden or disabled",
    _PORTAL_UNRESOLVED: "could not be resolved",
}


def _uniform_messages(text: str) -> dict[str, str]:
    """Report one committed message whatever the recovery last observed."""

    return {verdict: text for verdict in _PORTAL_FAILURE_SUFFIXES}


class _ControlUnresolved(Exception):
    """A locator that could not be resolved at all: drift, not lag."""

    def __init__(self, cause: Exception) -> None:
        super().__init__("portal control could not be resolved")
        self.cause = cause


class _PortalNotSettled(Exception):
    """A bounded readiness or postcondition window that never materialised.

    Raised instead of `LayoutChangedError` where the caller owns the
    classification -- `login()` names the step that failed and lets a visible
    alert outrank it -- so a recovery never pre-empts that decision.
    """


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

    # ---- shared bounded recovery ---- #

    def _probe_timeout_ms(self, remaining_ms: int) -> int:
        """Bound one waiting Playwright call by the cap and by what is left."""

        # Never zero: in Playwright a zero timeout means "wait forever", which
        # is the opposite of what a bounded probe is for.
        return max(1, min(remaining_ms, MAX_PORTAL_PROBE_TIMEOUT_MS))

    def _recover(
        self,
        page: Any,
        probe: Callable[[int], tuple[str, Any]],
        description: str,
        *,
        messages: Mapping[str, str] | None = None,
        classified: bool = True,
    ) -> Any:
        """Run `probe` at bounded elapsed checkpoints until it reports ready.

        `probe` is handed the remaining budget and must only inspect: it may
        never dispatch an externally meaningful action, because a recovery can
        run it many times. One monotonic deadline covers the whole window, so a
        surface that never settles fails closed rather than holding.
        """

        start = time.monotonic()
        deadline = start + PORTAL_RECOVERY_DEADLINE_SECONDS
        verdict = _PORTAL_ABSENT
        for target_ms in PORTAL_RECOVERY_ATTEMPTS_MS:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            pending_ms = min(target_ms - int((time.monotonic() - start) * 1000), remaining_ms)
            if pending_ms > 0:
                page.wait_for_timeout(pending_ms)
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    break
            try:
                verdict, value = probe(remaining_ms)
            except _ControlUnresolved as exc:
                raise self._recovery_failure(
                    description, messages, _PORTAL_UNRESOLVED, classified
                ) from exc.cause
            if verdict == _PORTAL_READY:
                return value
        raise self._recovery_failure(description, messages, verdict, classified)

    @staticmethod
    def _recovery_failure(
        description: str,
        messages: Mapping[str, str] | None,
        verdict: str,
        classified: bool,
    ) -> Exception:
        """Build the fail-closed error for a recovery that ran out of budget."""

        text = (messages or {}).get(verdict) or f"{description} {_PORTAL_FAILURE_SUFFIXES[verdict]}"
        return LayoutChangedError(text) if classified else _PortalNotSettled(text)

    def _resolve_ready_control(
        self,
        page: Any,
        locator_factory: Callable[[], Any],
        description: str,
        *,
        require_enabled: bool = True,
        require_trial_actionable: bool = False,
        messages: Mapping[str, str] | None = None,
        classified: bool = True,
    ) -> Any:
        """Return a freshly resolved control once it is proven interactable.

        The factory -- not a locator object -- is what this takes, because the
        failure being recovered from is a stale handle held across a long
        auto-wait while a route is still settling. Every checkpoint therefore
        builds a new locator and inspects it without starting a wait that could
        swallow the whole page timeout.

        More than one exact match is treated as transient, never as usable: it
        is never interacted with, never narrowed with `.first`, and the
        selector is never weakened. Ambiguity that survives the deadline is
        drift and fails closed.
        """

        def probe(remaining_ms: int) -> tuple[str, Any]:
            try:
                locator = locator_factory()
                count = locator.count()
            except Exception as exc:
                raise _ControlUnresolved(exc) from exc
            if count == 0:
                return _PORTAL_ABSENT, None
            if count > 1:
                return _PORTAL_AMBIGUOUS, None
            try:
                visible = locator.is_visible()
            except Exception as exc:
                raise _ControlUnresolved(exc) from exc
            if not visible:
                return _PORTAL_NOT_READY, None
            if require_enabled and not self._probe_enabled(locator, remaining_ms):
                return _PORTAL_NOT_READY, None
            if require_trial_actionable and not self._probe_actionable(locator, remaining_ms):
                return _PORTAL_NOT_READY, None
            return _PORTAL_READY, locator

        return self._recover(page, probe, description, messages=messages, classified=classified)

    def _probe_enabled(self, locator: Any, remaining_ms: int) -> bool:
        """Report enabled-ness without letting the check outlast the budget.

        `locator.is_enabled(timeout=...)` is not `locator.is_visible()`: its
        timeout is live and defaults to `page.set_default_timeout()`, so left
        implicit it could hold a bounded recovery for minutes.
        """

        try:
            return bool(locator.is_enabled(timeout=self._probe_timeout_ms(remaining_ms)))
        except Exception as exc:
            # Only a timeout is transient. Anything else is a real failure and
            # must reach the caller unaltered rather than becoming a retry.
            if not self._looks_like_timeout(exc):
                raise
            return False

    def _probe_actionable(self, locator: Any, remaining_ms: int) -> bool:
        """Prove Playwright can act on the control without acting on it."""

        try:
            locator.click(trial=True, timeout=self._probe_timeout_ms(remaining_ms))
        except Exception as exc:
            if not self._looks_like_timeout(exc):
                raise
            return False
        return True

    def _await_condition(
        self,
        page: Any,
        condition: Callable[[int], bool],
        description: str,
        *,
        messages: Mapping[str, str] | None = None,
        classified: bool = True,
    ) -> None:
        """Settle a postcondition on the same ladder as control readiness.

        This is what follows a single dispatched action: the expected next
        state is waited for and re-resolved, and the action is never sent again
        because the first transition looked slow.
        """

        def probe(remaining_ms: int) -> tuple[str, Any]:
            return (_PORTAL_READY if condition(remaining_ms) else _PORTAL_ABSENT), None

        self._recover(page, probe, description, messages=messages, classified=classified)

    # ---- login ---- #

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
            self._fill_login_field(page, "Username", username)
            stage_failure = "login password entry did not complete"
            self._fill_login_field(page, "Password", password)
            stage_failure = LOGIN_SUBMIT_STAGE
            self._submit_login(page)
            stage_failure = "Billing Manager entry did not appear after login"
            self._await_billing_manager(page)
        except LayoutChangedError:
            # A proven pre-auth contract failure must not be reclassified as a
            # credential rejection just because the page also renders an alert.
            raise
        except Exception as exc:
            if self._visible(page, page.get_by_role("alert")):
                raise LoginError("portal rejected the login") from exc
            raise LayoutChangedError(stage_failure) from exc

    def _fill_login_field(self, page: Any, label: str, value: str) -> None:
        """Fill one exact labelled credential field after proving it ready.

        Readiness is recovered; the fill is not. A `fill()` that raises may
        already have committed part of its value, so it is dispatched once and
        the failure goes to the caller's classification. The value is never
        logged, echoed, or read back.
        """

        field = self._resolve_ready_control(
            page,
            lambda: page.get_by_label(label, exact=True),
            f"login {label} field",
            classified=False,
        )
        field.fill(value)

    def _submit_login(self, page: Any) -> None:
        """Click the canonical Login control exactly once, after proving it ready.

        The selector never changes, and there is no alternate or fallback
        selector. Exactly one normal click is ever dispatched: a submit that
        has already been sent may have landed even if the call raises, so
        retrying it could duplicate the submission; that failure goes to the
        caller's classification instead.
        """

        submit = self._resolve_ready_control(
            page,
            lambda: page.get_by_role("button", name="Login", exact=True),
            "login submit control",
            require_trial_actionable=True,
            messages=_uniform_messages(LOGIN_SUBMIT_STAGE),
        )
        submit.click()

    def _await_billing_manager(self, page: Any) -> None:
        """Settle on the post-login surface without submitting anything again.

        The failure is left unclassified on purpose: `login()` owns the step
        marker for this stage and lets a visible alert outrank it, so a slow
        surface must not pre-empt a credential rejection.
        """

        def condition(remaining_ms: int) -> bool:
            if self._entry_is_visible(page, remaining_ms):
                return True
            # A visible alert is a settled answer, not lag. Spending the rest of
            # the window on it could not change the outcome and would delay a
            # credential rejection by a minute, so the window stops here and the
            # caller classifies it exactly as it always has.
            if self._visible(page, page.get_by_role("alert")):
                raise _PortalNotSettled("Billing Manager entry did not appear after login")
            return False

        self._await_condition(
            page,
            condition,
            "Billing Manager entry",
            messages=_uniform_messages("Billing Manager control is missing or ambiguous"),
            classified=False,
        )

    def _entry_is_visible(self, page: Any, remaining_ms: int) -> bool:
        """Report whether exactly one Billing Manager entry is visible yet."""

        entry = page.get_by_role("link", name="Billing Manager", exact=True)
        if entry.count() != 1:
            return False
        try:
            entry.wait_for(state="visible", timeout=self._probe_timeout_ms(remaining_ms))
        except Exception:
            return False
        return True

    def _enter_public_semantics(self, page: Any) -> Any:
        """Open the public Flutter semantics gate and return the Login entry.

        The public pre-auth page is a Flutter surface whose only initially
        exposed control is the accessibility semantics activation button.
        Application semantics -- and with them the Login entry -- appear only
        after exactly one dispatched click. Every precondition is proven before
        that dispatch, so a changed public contract fails closed without the
        page being interacted with at all.
        """

        activation = self._resolve_ready_control(
            page,
            lambda: page.get_by_role("button", name="Enable accessibility", exact=True),
            "Flutter semantics activation control",
        )

        # Exactly one activation per login attempt: no retry, no fallback
        # activation mechanism, and no second dispatch on any path below, not
        # even when the placeholder is slow to detach.
        activation.dispatch_event("click")

        self._await_condition(
            page,
            lambda remaining_ms: self._placeholder_detached(page, remaining_ms),
            "Flutter semantics placeholder",
            messages=_uniform_messages(
                "Flutter semantics placeholder remained after activation"
            ),
        )

        return self._resolve_ready_control(
            page,
            lambda: page.get_by_role("button", name="Login", exact=True),
            "post-activation Login control",
            require_trial_actionable=True,
        )

    def _placeholder_detached(self, page: Any, remaining_ms: int) -> bool:
        """Report whether the semantics placeholder has gone, within one probe."""

        try:
            page.locator("flt-semantics-placeholder").wait_for(
                state="detached", timeout=self._probe_timeout_ms(remaining_ms)
            )
        except Exception:
            # Attachment is re-probed from a fresh locator each checkpoint, so
            # any failure to observe detachment is simply "not yet"; the
            # deadline is what turns persistence into a fail-closed.
            return False
        return True

    # ---- inventory ---- #

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
            list_container = self._await_invoice_list(page)
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

            _control, disabled = self._resolve_pagination_control(
                page, "invoice pagination control is missing or ambiguous"
            )
            if disabled:
                # A disabled Next page is the committed end-of-inventory
                # signal, not lag: the results are already confirmed
                # post-search and this page's list is visible, so it is read
                # once and trusted rather than waited out.
                return bills
            self._advance_page(page, page_marker, page.url, "invoice pagination did not advance")
            page_ordinal += 1

    def _await_invoice_list(self, page: Any) -> Any:
        """Settle on the one visible invoice list container for this page."""

        return self._resolve_ready_control(
            page,
            lambda: page.get_by_test_id("invoice-list"),
            "invoice list container",
            require_enabled=False,
            messages={
                _PORTAL_ABSENT: "invoice list container is missing",
                _PORTAL_AMBIGUOUS: "invoice list container is missing or ambiguous",
                _PORTAL_NOT_READY: "invoice list container is missing",
                _PORTAL_UNRESOLVED: "invoice list container is missing",
            },
        )

    def _resolve_pagination_control(self, page: Any, missing_message: str) -> tuple[Any, bool]:
        """Return the unique settled Next-page control and whether it is disabled.

        Enabled-ness is deliberately not part of readiness here, because a
        disabled control is a legitimate answer rather than a lagging one.
        """

        control = self._resolve_ready_control(
            page,
            lambda: page.get_by_role("button", name="Next page", exact=True),
            "invoice pagination control",
            require_enabled=False,
            messages=_uniform_messages(missing_message),
        )
        try:
            disabled = bool(control.is_disabled(timeout=MAX_PORTAL_PROBE_TIMEOUT_MS))
        except Exception as exc:
            raise LayoutChangedError("invoice pagination control state could not be read") from exc
        return control, disabled

    def _advance_page(self, page: Any, old_marker: str, old_url: str, failure: str) -> None:
        """Click Next page exactly once and settle on the advanced page.

        The click is proven actionable on a freshly resolved control first, and
        it is never sent again: advancement that looks slow is waited for, not
        re-requested, because a second click would skip a page of inventory.
        """

        control = self._resolve_ready_control(
            page,
            lambda: page.get_by_role("button", name="Next page", exact=True),
            "invoice pagination control",
            require_trial_actionable=True,
            messages=_uniform_messages(failure),
        )
        control.click()
        self._await_condition(
            page,
            lambda remaining_ms: self._page_advanced(page, old_marker, old_url, remaining_ms),
            "invoice pagination",
            messages=_uniform_messages(failure),
        )

    def _page_advanced(self, page: Any, old_marker: str, old_url: str, remaining_ms: int) -> bool:
        """Report whether the marker or the URL has moved on, within one probe."""

        try:
            page.wait_for_function(
                """([selector, old_marker, old_url]) => {
                    const list = document.querySelector(selector);
                    const marker = list?.getAttribute('data-page') || window.location.href;
                    return marker !== old_marker || window.location.href !== old_url;
                }""",
                arg=["[data-testid='invoice-list']", old_marker, old_url],
                timeout=self._probe_timeout_ms(remaining_ms),
            )
        except Exception:
            return False
        return True

    # ---- download ---- #

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
            # Only the control is re-resolved, not the row: the row's identity
            # has already been proven, and recovering it too could settle on a
            # different row than the one that was proven.
            download_control = self._resolve_ready_control(
                page,
                lambda: matching_rows[0].get_by_role("button", name="Download", exact=True),
                "invoice row download control",
                require_trial_actionable=True,
                messages=_uniform_messages("invoice row download control is missing or ambiguous"),
            )
            with page.expect_download() as download_info:
                # Exactly one real download dispatch per attempt. An ambiguous
                # or timed-out outcome is classified below, never re-clicked.
                download_control.click()
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

    # ---- verified results route ---- #

    def _open_verified_results(self, entry_url: str | None = None) -> Any:
        page = self._require_page()
        try:
            if entry_url is not None:
                page.goto(entry_url, wait_until="domcontentloaded")

            def account_locator() -> Any:
                return page.get_by_label("Tenant/account", exact=True)

            if account_locator().count() != 1:
                # Each click is dispatched once, and the readiness recovery for
                # the next surface is that click's postcondition: Billing
                # Manager settles into EB Bill, and EB Bill into the
                # tenant/account selector.
                billing_manager = self._resolve_ready_control(
                    page,
                    lambda: page.get_by_role("link", name="Billing Manager", exact=True),
                    "Billing Manager control",
                    require_trial_actionable=True,
                    messages=_uniform_messages("Billing Manager control is missing or ambiguous"),
                )
                billing_manager.click()
                eb_bill = self._resolve_ready_control(
                    page,
                    lambda: page.get_by_role("link", name="EB Bill", exact=True),
                    "EB Bill control",
                    require_trial_actionable=True,
                    messages=_uniform_messages("EB Bill control is missing or ambiguous"),
                )
                eb_bill.click()
            account_control = self._resolve_ready_control(
                page,
                account_locator,
                "tenant/account selector",
                messages=_uniform_messages("tenant/account selector is missing or ambiguous"),
            )
            # Account identity is a configuration contract, not a timing
            # question: exactly one configured option, a stable option value and
            # a matching read-back all stay terminal on failure.
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

            search = self._resolve_ready_control(
                page,
                lambda: page.get_by_role("button", name="Search", exact=True),
                "Search control",
                require_trial_actionable=True,
                messages=_uniform_messages("Search control is missing or ambiguous"),
            )
            # Exactly one Search dispatch. A slow result is settled for below,
            # never re-requested: an empty surface before the confirmed
            # post-search state is not an answer about invoices at all.
            search.click()
            self._await_post_search_state(page)
            self._verify_account_binding(page, account_control)
            return self._await_invoice_list(page)
        except LayoutChangedError:
            raise
        except Exception as exc:
            raise LayoutChangedError("tenant/account search result contract changed") from exc

    def _await_post_search_state(self, page: Any) -> None:
        """Settle on the confirmed post-search result state after one Search."""

        def probe(remaining_ms: int) -> tuple[str, Any]:
            state = page.get_by_test_id("invoice-results-state")
            if state.count() != 1:
                return _PORTAL_AMBIGUOUS, None
            if self._probe_result_state(state, remaining_ms) != "post-search":
                return _PORTAL_NOT_READY, None
            return _PORTAL_READY, None

        self._recover(
            page,
            probe,
            "invoice result state marker",
            messages={
                _PORTAL_ABSENT: "invoice result state marker is missing or ambiguous",
                _PORTAL_AMBIGUOUS: "invoice result state marker is missing or ambiguous",
                _PORTAL_NOT_READY: "invoice results are not confirmed post-search",
                _PORTAL_UNRESOLVED: "invoice result state marker is missing or ambiguous",
            },
        )

    def _probe_result_state(self, state: Any, remaining_ms: int) -> str | None:
        """Read the result-state marker without outlasting the shared budget.

        `locator.get_attribute()` auto-waits, so left implicit it inherits
        `page.set_default_timeout()` -- and the marker can detach between the
        count above and this read while the surface rerenders, which is exactly
        when that wait would start. A blocking call the monotonic deadline
        cannot interrupt is what would make the ceiling nominal rather than
        hard, so this read is bounded like every other probe.
        """

        try:
            return state.get_attribute(
                "data-state", timeout=self._probe_timeout_ms(remaining_ms)
            )
        except Exception as exc:
            # Only a timeout is the rerender lag this recovery exists for.
            # Anything else is a real failure and must reach the caller
            # unaltered rather than becoming another checkpoint.
            if not self._looks_like_timeout(exc):
                raise
            return None

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
        list_container = self._await_invoice_list(page)
        current_marker = self._page_marker(page, list_container)
        seen_markers = {current_marker}
        for _ in range(1, page_ordinal):
            _control, disabled = self._resolve_pagination_control(
                page, "invoice page binding cannot be replayed"
            )
            if disabled:
                raise LayoutChangedError("invoice page binding cannot be replayed")
            old_marker = current_marker
            self._advance_page(page, old_marker, page.url, "invoice page replay did not advance")
            list_container = self._await_invoice_list(page)
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
