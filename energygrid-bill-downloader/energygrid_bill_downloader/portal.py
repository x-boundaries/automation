from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from .config import RuntimeConfig
from .errors import DependencyError, DownloadError, LayoutChangedError, LoginError


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

# Credential entry is typed, not assigned. What was observed, on the same
# canonical path, is an A/B contract: with the prior assignment/`fill()` entry the
# canonical Login control was stably absent, while user-like, event-producing
# typed entry produced exactly one canonical Login control that was visible,
# enabled and `trial=True` actionable. The internal reason for that difference
# was not measured. The working hypothesis is that the portal's Flutter
# text-editing host only adopts a value it observed being edited, so an assigned
# value leaves the widget empty and the form incomplete; that hypothesis, the
# exact host lifecycle or DOM replacement mechanism, and treating an incomplete
# form as the sole cause of `EG_LOGIN_SUBMIT_NOT_APPEAR`, are inference rather
# than established portal fact. The delay is a per-key pacing hint for that host,
# never a settle or a retry budget.
LOGIN_KEY_ENTRY_DELAY_MS = 25

# The credential focus/text-editing-host settle floor.
#
# Run123 was adjudicable, unattended and complete, and the owner observed that
# password entry appeared to receive one fewer character than expected. The
# matching gap is that a credential field was proven visible and enabled,
# clicked, and then typed into immediately: nothing proved that the field --
# or the editing host the click hands off to -- actually owned focus before
# the first key event, so the leading key can land nowhere. Visible and
# enabled is therefore NOT a sufficient precondition for typing. That the lost
# character is caused by this handoff remains the leading hypothesis and not a
# proved portal fact; the gate is correct regardless, because typing into an
# unfocused field is never right.
#
# The floor is the FIRST COMMITTED recovery checkpoint, not a new independent
# sleep and not a budget of its own: the focus proof simply is not consulted at
# the ladder's immediate look, because a click's focus handoff has not
# meaningfully happened yet and an immediate answer would be read as settled
# state. Everything after the floor -- the later checkpoints, the one monotonic
# deadline, the fresh re-resolution at every look -- is the existing shared
# contract unchanged.
LOGIN_FOCUS_SETTLE_FLOOR_MS = PORTAL_RECOVERY_CHECKPOINTS_MS[0]

# Boolean-only focus ownership. The predicate compares node identity against
# the document's active element, following shadow roots so a host that renders
# its real editable inside one still reports the containing target as the owner.
# It reads no value, no attribute and no text, returns nothing but a boolean,
# and therefore cannot expose or compare a credential character.
_FOCUS_OWNERSHIP_PREDICATE = """
(node) => {
  const owner = node.ownerDocument;
  if (!owner) { return false; }
  let active = owner.activeElement;
  if (!active) { return false; }
  while (active.shadowRoot && active.shadowRoot.activeElement) {
    active = active.shadowRoot.activeElement;
  }
  return active === node || node.contains(active);
}
"""

# ---- bounded login diagnostic vocabulary ---- #
#
# DL-XB-141-RUNTIME-005-SOURCE-DURABILITY-A1. The diagnostic observes a fixed
# public-safe witness set and nothing else: no page text, HTML, DOM or
# accessibility-tree dump, no attribute outside these observations, no
# screenshot, trace, storage or network capture, and no URL. Every witness is a
# count, a boolean, or null when it could not be read; null is never evidence.

# The Flutter host and render surfaces. The first two are the semantics
# surface; the rest are the non-semantics render shell. Both halves together
# are the allowlisted shell/render witness set.
DIAGNOSTIC_SEMANTICS_HOST_TAGS = ("flt-semantics-host", "flt-semantics")
DIAGNOSTIC_RENDER_SHELL_TAGS = (
    "flt-glass-pane",
    "flt-text-editing-host",
    "flt-scene-host",
    "canvas",
)
DIAGNOSTIC_HOST_TAGS = DIAGNOSTIC_SEMANTICS_HOST_TAGS + DIAGNOSTIC_RENDER_SHELL_TAGS

# Every known post-submit application or public control. A shell-only
# classification requires all of them absent BY COUNT, so one counted control -
# including the public `Enable accessibility` gate and the authenticated-landing
# EMS control - blocks it.
DIAGNOSTIC_CONTROL_WITNESSES = (
    "ems",
    "billing_manager",
    "username",
    "password",
    "login",
    "enable_accessibility",
)

# ---- authentication proof (DL-XB-141-AUTH-LANDING-NAV-SEPARATION-G2-001) ---- #
#
# Authentication and business navigation are two independent contracts. Billing
# Manager is NOT an authentication oracle: Run125 bound the authenticated
# landing to exactly one witness, and nothing else establishes authentication.
#
# The witnesses below are the whole authentication evidence set. They are exact
# counts, booleans and nulls, so no page text, URL or credential derivative can
# reach a decision or an output surface.
AUTHENTICATION_WITNESS_ROLE = "button"
AUTHENTICATION_WITNESS_NAME = "EMS"

# Every witness that must be positively ABSENT BY EXACT ZERO COUNT for a clean
# authenticated landing. A retained login route, a retained public
# accessibility gate, or a rendered rejection contradicts authentication.
AUTHENTICATION_RETAINED_WITNESSES = (
    "username",
    "password",
    "login",
    "enable_accessibility",
)

# The three authentication outcomes. Nothing else is ever reported, and the
# unproved outcome is the fail-closed default rather than an error case.
AUTHENTICATED = "AUTHENTICATED"
REJECTED = "REJECTED"
AUTHENTICATION_UNPROVED = "AUTHENTICATION_UNPROVED"
AUTHENTICATION_OUTCOMES = (AUTHENTICATED, REJECTED, AUTHENTICATION_UNPROVED)

# The one message a bounded authentication window that never proved the landing
# reports. It names the contract that failed, never what was observed.
AUTHENTICATION_UNPROVED_MESSAGE = "authenticated landing was not proven after login"

AUTHENTICATED_LANDING_PROVEN = "AUTHENTICATED_LANDING_PROVEN"
BILLING_MANAGER_VISIBLE = "BILLING_MANAGER_VISIBLE"
VISIBLE_ALERT = "VISIBLE_ALERT"
LOGIN_ROUTE_PERSISTED_OR_RETURNED = "LOGIN_ROUTE_PERSISTED_OR_RETURNED"
SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS = "SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS"
FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT = (
    "FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT"
)
FLUTTER_SHELL_DISAPPEARED_AFTER_SUBMIT = "FLUTTER_SHELL_DISAPPEARED_AFTER_SUBMIT"

# Priority order. The first satisfied classification wins; anything
# contradictory, ambiguous or insufficient yields no classification at all.
#
# `AUTHENTICATED_LANDING_PROVEN` leads, because a clean authentication witness
# set is the strongest positive evidence the surface can carry and cannot be
# improved on by looking again. `AUTHENTICATION_UNPROVED` is last of the
# observational arms and needs its own positive evidence: the authentication
# witness must have been READ and positively counted while the landing stayed
# unproven. It is therefore never reached by an empty or unreadable surface,
# which still fails closed with no classification at all.
#
# The six historical classifications are retained exactly, in their original
# relative order. `BILLING_MANAGER_VISIBLE` and the shell classifications
# remain observations about the surface and never imply authentication.
LOGIN_DIAGNOSTIC_CLASSIFICATIONS = (
    AUTHENTICATED_LANDING_PROVEN,
    BILLING_MANAGER_VISIBLE,
    VISIBLE_ALERT,
    LOGIN_ROUTE_PERSISTED_OR_RETURNED,
    SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS,
    FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT,
    AUTHENTICATION_UNPROVED,
    FLUTTER_SHELL_DISAPPEARED_AFTER_SUBMIT,
)

# How a classification settles the bounded post-submit window
# (DL-XB-141-ASTRA-SIMPLIFY-001). This splits the vocabulary above by settling
# behaviour only: nothing is added, removed or reordered.
#
# Only a positive Billing Manager and a decisive visible alert are terminal on
# sight, because neither can be improved on by looking again. The route and
# shell classifications name what one checkpoint saw while a Flutter route was
# still settling, so concluding them on sight ended the observation before a
# later Billing Manager could appear. They stay provisional for the whole
# window and are concluded only from the final observation.
#
# `FLUTTER_SHELL_DISAPPEARED_AFTER_SUBMIT` belongs to neither group: the
# classifier never produces it, and it is concluded only from the
# throughout-window absence test.
# A clean authenticated landing joins the terminal group: it is the positive
# proof the whole observation exists to find, and a later look cannot improve
# on it. A contradictory or incomplete authentication reading is provisional
# like every other unsettled surface and is concluded only from the final look.
DIAGNOSTIC_IMMEDIATE_CLASSIFICATIONS = (
    AUTHENTICATED_LANDING_PROVEN,
    BILLING_MANAGER_VISIBLE,
    VISIBLE_ALERT,
)
DIAGNOSTIC_CONTINUABLE_CLASSIFICATIONS = (
    LOGIN_ROUTE_PERSISTED_OR_RETURNED,
    SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS,
    FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT,
    AUTHENTICATION_UNPROVED,
)

# ---- business navigation (Contract B) ---- #
#
# The exact controls `_open_verified_results()` owns after authentication. They
# are business navigation, never authentication evidence, and each message
# below distinguishes a pre-dispatch readiness failure from an uncertain
# dispatch and from a postcondition that never became proven.
#
# The authenticated landing is not itself the business surface
# (DL-XB-141-EMS-ENTRY-MINIMAL-REPAIR-G2-136): entering the EMS application is
# the first business navigation step, and it is named here rather than borrowed
# from the authentication witness. `EMS_ENTRY_NAV_NAME` and
# `AUTHENTICATION_WITNESS_NAME` currently carry the same text and stay
# deliberately separate symbols, because they answer different questions -- one
# is the evidence a landing is authenticated, the other is the control that is
# clicked -- and either may move without the other.
EMS_ENTRY_NAV_NAME = "EMS"
BILLING_MANAGER_NAV_NAME = "Billing Manager"
EB_BILL_NAV_NAME = "EB Bill"

NAV_EMS_ENTRY_NOT_READY_MESSAGE = "EMS application entry control is not ready"
NAV_EMS_ENTRY_UNCERTAIN_MESSAGE = "EMS application entry dispatch outcome uncertain"
NAV_BILLING_MANAGER_NOT_READY_MESSAGE = "Billing Manager navigation control is not ready"
NAV_BILLING_MANAGER_UNCERTAIN_MESSAGE = "Billing Manager navigation dispatch outcome uncertain"
NAV_EB_BILL_NOT_READY_MESSAGE = "EB Bill navigation control is not ready"
NAV_EB_BILL_UNCERTAIN_MESSAGE = "EB Bill navigation dispatch outcome uncertain"
NAV_RESULTS_ROUTE_UNPROVED_MESSAGE = "EB Bill results route was not proven"

# What one bounded EB Bill entry observation concluded. Only these three are
# routing decisions. Ambiguity and an unreadable state are deliberately absent:
# they are drift, they never become a decision, and they fail closed.
_EB_BILL_ENTRY_ROUTE_PROVEN = "route proven"
_EB_BILL_ENTRY_DIRECT_READY = "direct ready"
_EB_BILL_ENTRY_OUTER = "outer required"

# The one-shot submit boundary, reported rather than inferred.
SUBMIT_DISPATCHED = "DISPATCHED"
SUBMIT_DISPATCH_UNCERTAIN = "DISPATCH_UNCERTAIN"
SUBMIT_NOT_DISPATCHED = "NOT_DISPATCHED"
SUBMIT_OUTCOMES = (SUBMIT_DISPATCHED, SUBMIT_DISPATCH_UNCERTAIN, SUBMIT_NOT_DISPATCHED)


def unobserved_login_witnesses(include_url: bool = False) -> dict[str, Any]:
    """Return the witness shape with nothing observed.

    The document shape never varies with what happened, so a failure before any
    observation still reports the same closed keys with null values rather than
    a different, shorter object.
    """

    observation: dict[str, Any] = {
        "hosts": {tag: None for tag in DIAGNOSTIC_HOST_TAGS},
        "semantics_placeholder": {"count": None, "present": None},
        "ems": {"count": None, "visible": None},
        "username": {"count": None, "visible": None},
        "password": {"count": None, "visible": None},
        "login": {"count": None, "visible": None, "actionable": None},
        "enable_accessibility": {"count": None, "visible": None, "actionable": None},
        # The strict rejection witness. A reader failure is null, and null is
        # never read as "rejection absent" anywhere.
        "rejection": {"count": None, "visible": None},
        "billing_manager": {"count": None, "visible": None},
        "visible_alert": None,
    }
    if include_url:
        observation["url_changed"] = None
    return observation

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


@dataclass
class _LoginProgress:
    """How far one canonical login sequence got.

    `stage_failure` names the step that is about to run, so a generic failure
    reports which one failed instead of one message for the whole sequence. It
    is a fixed internal marker: never a selector, URL, credential, account
    identity, or anything read back from the page.

    `submit_dispatched` is flipped at the explicit one-shot boundary - the real
    submit invocation - and never by anything that only proved readiness.
    """

    stage_failure: str = "portal navigation did not complete"
    submit_dispatched: bool = False


@dataclass(frozen=True)
class LoginDiagnosticResult:
    """The bounded observation of one login attempt.

    `failure` is the caller's classification key only: the CLI maps it to a
    bounded support reference and drops it, exactly as it does for a run. It is
    never part of the emitted document, so no exception text can reach an
    output surface. It is set only when the submit was never dispatched.
    """

    classification: str | None
    submit_dispatched: bool
    submit_outcome: str
    pre_submit: dict[str, Any]
    post_submit: dict[str, Any]
    failure: Any = None
    # The authentication contract's own verdict, independent of every
    # observational classification. It defaults to the fail-closed outcome, so
    # a result built before any authentication evidence exists reports
    # "unproved" rather than an absent field.
    authentication_outcome: str = AUTHENTICATION_UNPROVED


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
        settle_floor_ms: int = 0,
    ) -> Any:
        """Run `probe` at bounded elapsed checkpoints until it reports ready.

        `probe` is handed the remaining budget and must only inspect: it may
        never dispatch an externally meaningful action, because a recovery can
        run it many times. One monotonic deadline covers the whole window, so a
        surface that never settles fails closed rather than holding.

        `settle_floor_ms` declines the earliest looks for a probe whose answer
        is not yet meaningful that soon. It never adds a checkpoint, extends the
        window or moves the deadline.
        """

        start = time.monotonic()
        deadline = start + PORTAL_RECOVERY_DEADLINE_SECONDS
        verdict = _PORTAL_ABSENT
        for target_ms in self._recovery_attempts_ms(settle_floor_ms):
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
    def _recovery_attempts_ms(settle_floor_ms: int) -> tuple[int, ...]:
        """Return the committed ladder without the looks earlier than the floor.

        The default floor of 0 -- every caller but the credential focus gate --
        returns the committed ladder exactly as it is. A floor is only ever one
        of the committed checkpoints, so the first consulted look is a real
        ladder offset rather than a bespoke sleep. If a floor would remove every
        look, the ladder's final look is kept: a gate must still get one bounded
        chance to prove its condition before it fails closed.
        """

        attempts = tuple(ms for ms in PORTAL_RECOVERY_ATTEMPTS_MS if ms >= settle_floor_ms)
        return attempts or PORTAL_RECOVERY_ATTEMPTS_MS[-1:]

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
        require_focus_owned: bool = False,
        settle_floor_ms: int = 0,
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
            if require_focus_owned and not self._probe_focus_owned(locator, remaining_ms):
                return _PORTAL_NOT_READY, None
            return _PORTAL_READY, locator

        return self._recover(
            page,
            probe,
            description,
            messages=messages,
            classified=classified,
            settle_floor_ms=settle_floor_ms,
        )

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

    def _probe_focus_owned(self, locator: Any, remaining_ms: int) -> bool:
        """Prove the freshly resolved target owns focus, as a boolean only.

        Node identity is compared against the document's active element, so a
        focused descendant of the target counts and nothing else does. No value,
        attribute or text is read, returned or retained, so no credential
        character can reach a comparison, a log or an output surface.

        Only a literal `True` is proof. A predicate that could not answer in
        time is "not yet", never a pass; and, as everywhere else on this ladder,
        a non-timeout failure is a real failure and reaches the caller intact.
        """

        try:
            owned = locator.evaluate(
                _FOCUS_OWNERSHIP_PREDICATE, timeout=self._probe_timeout_ms(remaining_ms)
            )
        except Exception as exc:
            if not self._looks_like_timeout(exc):
                raise
            return False
        return owned is True

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
        """End at positively proven authenticated landing, and nothing further.

        A normal return means exactly one thing: the authentication witness set
        was read and proved a clean authenticated landing. It does not mean the
        application was entered, that Billing Manager exists, or that any
        business route is reachable -- `_open_verified_results()` owns all of
        that (DL-XB-141-AUTH-LANDING-NAV-SEPARATION-G2-001). Billing Manager is
        no longer consulted here at all, so a Billing Manager that never
        appears is a navigation failure rather than a login failure.
        """

        username, password = self._require_runtime_credentials()
        page = self._require_page()
        progress = _LoginProgress()
        try:
            self._enter_login_credentials(page, username, password, progress)
            # Readiness failures are proven before the normal submit call. Keep
            # this fallback pre-dispatch so an unexpected readiness exception
            # cannot be mistaken for an uncertain click outcome.
            progress.stage_failure = "login submit control could not be resolved"
            self._submit_login(page, progress)
            progress.stage_failure = AUTHENTICATION_UNPROVED_MESSAGE
            self._await_authenticated_landing(page)
        except (LayoutChangedError, LoginError):
            # A proven pre-auth contract failure must not be reclassified as a
            # credential rejection just because the page also renders an alert,
            # and a rejection the authentication window proved positively keeps
            # its own classification instead of being re-derived from an alert.
            raise
        except Exception as exc:
            raise self._classify_login_stage_failure(page, progress) from exc

    @staticmethod
    def _require_runtime_credentials() -> tuple[str, str]:
        """Return the injected credential pair, or fail closed without one."""

        username = os.environ.get("ENERGYGRID_USERNAME")
        password = os.environ.get("ENERGYGRID_PASSWORD")
        if not username or not password:
            raise LoginError("runtime credentials are unavailable")
        return username, password

    def _enter_login_credentials(
        self, page: Any, username: str, password: str, progress: _LoginProgress
    ) -> None:
        """Run the canonical pre-submit login sequence, up to but not including submit.

        This is the ONE place the pre-submit sequence exists. The bounded login
        diagnostic reuses it rather than carrying a second set of selectors or
        credential-entry semantics, so there is no path on which the two could
        drift apart. The sequence itself is unchanged -- the Login entry is
        resolved and clicked in two statements rather than one so that a
        generic failure inside the semantics gate stays distinguishable from a
        failure to click what it returned.
        """

        progress.stage_failure = "portal navigation did not complete"
        page.goto(self.config.portal_url, wait_until="domcontentloaded")
        progress.stage_failure = "Flutter semantics activation dispatch did not complete"
        login_entry = self._enter_public_semantics(page)
        progress.stage_failure = "post-activation Login control click did not complete"
        login_entry.click()
        progress.stage_failure = "login username entry did not complete"
        self._fill_login_field(page, "Username", username)
        progress.stage_failure = "login password entry did not complete"
        self._fill_login_field(page, "Password", password)

    def _classify_login_stage_failure(self, page: Any, progress: _LoginProgress) -> Exception:
        """Name the failed step, unless the page has settled on a rejection.

        The rejection witness is the strict one: only an unambiguous positively
        visible rejection outranks the step marker. A rejection that could not
        be read is null, so it names the step that failed rather than being
        read as either a rejection or its absence.
        """

        if self._positively_visible(self._rejection_witness(page)):
            return LoginError("portal rejected the login")
        return LayoutChangedError(progress.stage_failure)

    def _rejection_witness(self, page: Any) -> dict[str, Any]:
        """Read the strict rejection witness: exact count, visibility, or null."""

        return self._visibility_witness(lambda: page.get_by_role("alert"))

    def _fill_login_field(self, page: Any, label: str, value: str) -> None:
        """Type one exact labelled credential field after proving it ready and focused.

        The field is focused, the focus is PROVEN, and only then is it typed.
        The established entry evidence is the observed A/B contract (see
        `LOGIN_KEY_ENTRY_DELAY_MS`): assignment-based entry left the canonical
        Login control stably absent, while user-like typed entry on the same
        path produced exactly one visible, enabled, actionable canonical Login
        control. Why assignment fails is inferred rather than measured -- the
        hypothesis is that the editing host ignores a value it did not observe
        being edited, leaving a form that renders no submit control, which would
        surface at the submit step rather than here. That mechanism is unchanged
        here: the same single typed entry, at the same per-key pacing.

        What is added is the focus gate between the one click and the first key
        event (see `LOGIN_FOCUS_SETTLE_FLOOR_MS`). Readiness alone -- exactly
        one match, visible, enabled -- does not establish that the field owns
        focus, and a key event sent into an unfocused field is simply lost. The
        gate therefore re-resolves the exact labelled target at each look rather
        than trusting the handle the click was sent to, and requires boolean
        focus ownership on top of readiness. It starts at the ladder's first
        committed checkpoint, so a focus answer read before the handoff could
        have happened is not mistaken for a settled one.

        Focus that never settles inside the bounded window fails closed: no
        credential key is sent for this field, and because the caller aborts,
        no Login submit is dispatched either. Readiness and focus are recovered;
        the entry is not. Typing may have partially committed before raising, so
        it is dispatched exactly once and never retried, and the failure goes to
        the caller's classification. The value is never logged, echoed, read
        back, measured, or exposed to the focus predicate.
        """

        field = self._resolve_ready_control(
            page,
            lambda: page.get_by_label(label, exact=True),
            f"login {label} field",
            classified=False,
        )
        field.click()
        focused = self._resolve_ready_control(
            page,
            lambda: page.get_by_label(label, exact=True),
            f"login {label} field focus",
            require_focus_owned=True,
            settle_floor_ms=LOGIN_FOCUS_SETTLE_FLOOR_MS,
            messages=_uniform_messages(f"login {label} field did not take focus"),
            classified=False,
        )
        focused.press_sequentially(value, delay=LOGIN_KEY_ENTRY_DELAY_MS)

    def _submit_login(self, page: Any, progress: _LoginProgress) -> None:
        """Click the canonical Login control exactly once, after proving it ready.

        The selector never changes, and there is no alternate or fallback
        selector. Exactly one normal click is ever dispatched: a submit that
        has already been sent may have landed even if the call raises, so
        retrying it could duplicate the submission; that failure goes to the
        caller's classification instead.
        """

        # Keep pre-dispatch readiness separate from the one real click. Trial
        # actionability proves the locator without submitting anything; a
        # non-timeout probe failure is therefore still pre-dispatch.
        try:
            submit = self._resolve_ready_control(
                page,
                lambda: page.get_by_role("button", name="Login", exact=True),
                "login submit control",
                require_trial_actionable=True,
            )
        except LayoutChangedError:
            raise
        except Exception as exc:
            raise LayoutChangedError("login submit control could not be resolved") from exc

        # This is the explicit dispatch boundary. Once normal click invocation
        # begins, an exception cannot prove whether the browser acted, so the
        # outcome is uncertain and the call must never be retried. The flag is
        # raised before invocation starts, so an exception thrown by the call
        # itself can never be read back as "never dispatched".
        progress.submit_dispatched = True
        try:
            submit.click()
        except Exception as exc:
            raise LayoutChangedError("login submit dispatch outcome uncertain") from exc

    def _await_authenticated_landing(self, page: Any) -> None:
        """Settle on positively proven authentication, or fail closed.

        This replaces the retired Billing Manager login wait. It submits
        nothing again, clicks nothing at all, and reads only the authentication
        witness set on the existing shared bounded ladder with freshly resolved
        exact locators at every look.

        Only three things can end the window. A clean authenticated landing
        returns. A positively visible rejection with the authentication witness
        positively absent raises the existing portal-rejected contract on
        sight, because a later look could not improve on it. Everything else --
        a missing, duplicate, hidden or unreadable authentication witness, a
        retained login route or accessibility gate, unreadable rejection
        evidence, or any contradictory combination -- stays provisional inside
        the window and fails closed as unproved once it is spent. URL movement,
        a vanished Login control, Flutter shell or semantics state, Billing
        Manager visibility and owner observation are never consulted here, so
        none of them can stand in for the proof.
        """

        def condition(remaining_ms: int) -> bool:
            observation = self._observe_authentication_witnesses(page, remaining_ms)
            outcome = self._authentication_outcome(observation)
            if outcome == AUTHENTICATED:
                return True
            if outcome == REJECTED:
                # The existing credential-rejection contract, reached from
                # positive evidence rather than from a generic alert read.
                raise LoginError("portal rejected the login")
            return False

        self._await_condition(
            page,
            condition,
            "authenticated landing",
            messages=_uniform_messages(AUTHENTICATION_UNPROVED_MESSAGE),
        )

    def _authentication_outcome(self, observation: Mapping[str, Any]) -> str:
        """Classify authentication from the exact witness set, fail-closed.

        Every arm needs positive evidence. A witness that could not be read is
        null, and null satisfies neither a positive test nor an absence test,
        so an unreadable surface is unproved rather than either authenticated
        or rejected. In particular a strict rejection-reader failure can never
        become "rejection absent".
        """

        ems = observation["ems"]
        rejection = observation["rejection"]
        if (
            ems["count"] == 1
            and ems["visible"] is True
            and rejection["count"] == 0
            and all(
                observation[name]["count"] == 0
                for name in AUTHENTICATION_RETAINED_WITNESSES
            )
        ):
            return AUTHENTICATED
        # A rejection may only be concluded while the authentication witness is
        # positively ABSENT by an exact zero count. Any counted, hidden or
        # unreadable authentication witness alongside a rejection is a
        # contradiction, and a contradiction is never a settled answer.
        if self._positively_visible(rejection) and ems["count"] == 0:
            return REJECTED
        return AUTHENTICATION_UNPROVED

    def _observe_authentication_witnesses(
        self, page: Any, remaining_ms: int
    ) -> dict[str, Any]:
        """Read the authentication witness set from fresh exact locators.

        This is the ONE place the authentication witnesses are read. The normal
        login path and the bounded diagnostic share it, so neither can carry a
        second witness set or a second notion of what authentication means.
        Nothing read here is text: the result carries counts, booleans and
        nulls only, and no URL is read or reported.
        """

        return {
            "ems": self._visibility_witness(
                lambda: page.get_by_role(
                    AUTHENTICATION_WITNESS_ROLE,
                    name=AUTHENTICATION_WITNESS_NAME,
                    exact=True,
                )
            ),
            "username": self._visibility_witness(
                lambda: page.get_by_label("Username", exact=True)
            ),
            "password": self._visibility_witness(
                lambda: page.get_by_label("Password", exact=True)
            ),
            "login": self._actionable_witness(
                lambda: page.get_by_role("button", name="Login", exact=True), remaining_ms
            ),
            "enable_accessibility": self._actionable_witness(
                lambda: page.get_by_role("button", name="Enable accessibility", exact=True),
                remaining_ms,
            ),
            "rejection": self._rejection_witness(page),
        }

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

    # ---- bounded login diagnostic ---- #

    def login_diagnostic(self) -> LoginDiagnosticResult:
        """Observe one login attempt and stop, without entering the application.

        This is the whole diagnostic operation. It reuses the canonical
        pre-submit sequence and the one canonical submit, then performs a
        bounded read-only observation and returns. It never reaches
        `_await_authenticated_landing`, `_open_verified_results`, the EB Bill
        route proof, the Billing Manager click, the EB Bill click, tenant or
        account selection, Search, inventory, pagination, download,
        publication, archive, state, temp cleanup or reconciliation: none of
        them is called from here or from anything this calls. Business
        navigation is therefore never tested by the diagnostic at all.
        """

        username, password = self._require_runtime_credentials()
        page = self._require_page()
        progress = _LoginProgress()
        pre_submit = unobserved_login_witnesses()
        entry_url: str | None = None
        try:
            self._enter_login_credentials(page, username, password, progress)
            # Read-only, and placed between credential entry and submit
            # readiness so a shell witness that was genuinely present before the
            # submit can be told apart from one that was never established.
            pre_submit = self._observe_login_witnesses(page, MAX_PORTAL_PROBE_TIMEOUT_MS)
            entry_url = self._current_url(page)
            progress.stage_failure = "login submit control could not be resolved"
            self._submit_login(page, progress)
        except LayoutChangedError as exc:
            failure: Exception | None = exc
        except Exception as exc:
            failure = self._classify_login_stage_failure(page, progress)
            failure.__cause__ = exc
        else:
            failure = None

        if not progress.submit_dispatched:
            # Nothing was sent, so there is nothing to observe the effect of.
            return LoginDiagnosticResult(
                classification=None,
                submit_dispatched=False,
                submit_outcome=SUBMIT_NOT_DISPATCHED,
                pre_submit=pre_submit,
                post_submit=unobserved_login_witnesses(include_url=True),
                failure=failure,
                authentication_outcome=AUTHENTICATION_UNPROVED,
            )

        # A dispatch whose outcome is uncertain is still observed, because the
        # page is the only remaining evidence. It is never promoted to a proven
        # successful dispatch by anything the observation finds.
        outcome = SUBMIT_DISPATCHED if failure is None else SUBMIT_DISPATCH_UNCERTAIN
        try:
            post_submit, classification = self._observe_after_submit(page, pre_submit, entry_url)
        except Exception:
            # The submit outcome was settled at the dispatch boundary and stays
            # exactly as it was settled. An observation that could not be
            # completed is evidence about the observation, never about what was
            # sent: it can neither promote an uncertain dispatch to a proven one
            # nor demote a proven one, and the Login control is never touched
            # again. The unobserved shape is reported rather than a partial one,
            # and nothing derived from the exception is retained, so no
            # free-form text can reach an output surface. `BaseException` is
            # deliberately outside this arm: an interrupt or an interpreter exit
            # is not a portal condition and must not become a diagnostic result.
            post_submit = unobserved_login_witnesses(include_url=True)
            classification = None
        return LoginDiagnosticResult(
            classification=classification,
            submit_dispatched=True,
            submit_outcome=outcome,
            pre_submit=pre_submit,
            post_submit=post_submit,
            failure=None,
            # The authentication verdict is read from the settled post-submit
            # observation and from nothing else. An unobserved shape carries
            # null witnesses, which is unproved, so a failed observation cannot
            # report authentication either way.
            authentication_outcome=self._authentication_outcome(post_submit),
        )

    def _observe_after_submit(
        self, page: Any, pre_submit: dict[str, Any], entry_url: str | None
    ) -> tuple[dict[str, Any], str | None]:
        """Watch the post-submit surface on the existing bounded ladder.

        The same monotonic deadline every other portal recovery uses bounds this
        one: no new polling loop, no larger timeout, and fresh locators at every
        checkpoint. Only a positive Billing Manager or a decisive visible alert
        ends the window early; a login route or a bare Flutter shell is a
        provisional reading of a route that may still be settling, so the window
        keeps looking and a later Billing Manager supersedes it. Once the window
        is spent the outcome is the throughout-window disappearance verdict when
        that contract is met, and otherwise the classification of the final
        observation, which still fails closed on unreadable or ambiguous
        evidence.
        """

        state: dict[str, Any] = {
            "observation": unobserved_login_witnesses(include_url=True),
            "shell_absent_throughout": True,
            "controls_absent_throughout": True,
        }

        def probe(remaining_ms: int) -> tuple[str, Any]:
            observation = self._observe_login_witnesses(page, remaining_ms, entry_url=entry_url)
            state["observation"] = observation
            if not self._shell_witnesses_absent(observation):
                state["shell_absent_throughout"] = False
            if not self._app_controls_absent(observation):
                state["controls_absent_throughout"] = False
            classification = self._classify_post_submit(observation)
            if classification in DIAGNOSTIC_IMMEDIATE_CLASSIFICATIONS:
                return _PORTAL_READY, classification
            # Every other reading is provisional, so it is kept as the current
            # observation and the window looks again with fresh locators rather
            # than settling here. Nothing about the surface is asserted by
            # continuing, and the shared deadline is not extended.
            return _PORTAL_ABSENT, None

        try:
            classification = self._recover(
                page,
                probe,
                "login diagnostic observation",
                messages=_uniform_messages("login diagnostic observation did not settle"),
                classified=False,
            )
        except _PortalNotSettled:
            # Disappearance is only ever concluded from a shell that was
            # positively there beforehand and never came back while the whole
            # window ran. A shell that was never established cannot disappear.
            if (
                self._any_shell_witness(pre_submit)
                and state["shell_absent_throughout"]
                and state["controls_absent_throughout"]
            ):
                classification = FLUTTER_SHELL_DISAPPEARED_AFTER_SUBMIT
            else:
                # No Billing Manager and no decisive alert arrived inside the
                # window, so the outcome is whatever the final bounded
                # observation still supports under the unchanged classifier and
                # its unchanged priority. A persistent login route or shell
                # state is concluded here rather than on first sight, and an
                # unreadable or ambiguous final reading classifies as nothing.
                classification = self._classify_post_submit(state["observation"])
        return state["observation"], classification

    def _classify_post_submit(self, observation: dict[str, Any]) -> str | None:
        """Return the first satisfied classification, or none at all.

        Every arm needs positive evidence. A locator that could not be read is
        null, and null never satisfies either a positive or an absence test, so
        an unreadable surface fails closed rather than classifying.
        """

        if self._authentication_outcome(observation) == AUTHENTICATED:
            return AUTHENTICATED_LANDING_PROVEN
        billing_manager = observation["billing_manager"]
        if billing_manager["count"] == 1 and billing_manager["visible"] is True:
            return BILLING_MANAGER_VISIBLE
        if observation["visible_alert"] is True:
            return VISIBLE_ALERT
        # A counted but hidden login control is ambiguous evidence, not a route
        # witness: at least one of the three must be positively visible.
        if any(
            self._positively_visible(observation[name])
            for name in ("username", "password", "login")
        ):
            return LOGIN_ROUTE_PERSISTED_OR_RETURNED
        if not self._app_controls_absent(observation):
            return self._unproved_authentication_classification(observation)
        hosts = observation["hosts"]
        if any(hosts[tag] is None for tag in DIAGNOSTIC_HOST_TAGS):
            return None
        if any(hosts[tag] > 0 for tag in DIAGNOSTIC_SEMANTICS_HOST_TAGS):
            return SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS
        if any(hosts[tag] > 0 for tag in DIAGNOSTIC_RENDER_SHELL_TAGS):
            return FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT
        return self._unproved_authentication_classification(observation)

    @staticmethod
    def _unproved_authentication_classification(
        observation: Mapping[str, Any],
    ) -> str | None:
        """Classify a positively counted authentication witness that never proved.

        This is the last observational arm and it needs its own positive
        evidence: the authentication witness must have been READ and counted at
        least once while the landing stayed unproven -- a duplicate, a hidden
        one, or one standing beside a retained login control or a rejection.
        An authentication witness that is absent by an exact zero count, or one
        that could not be read at all, is not evidence about authentication and
        still classifies as nothing at all.
        """

        count = observation["ems"]["count"]
        if count is None or count == 0:
            return None
        return AUTHENTICATION_UNPROVED

    @staticmethod
    def _positively_visible(witness: dict[str, Any]) -> bool:
        return bool(witness["count"]) and witness["visible"] is True

    @staticmethod
    def _app_controls_absent(observation: dict[str, Any]) -> bool:
        """Report every known post-submit control absent by an exact zero count."""

        return all(observation[name]["count"] == 0 for name in DIAGNOSTIC_CONTROL_WITNESSES)

    @staticmethod
    def _shell_witnesses_absent(observation: dict[str, Any]) -> bool:
        return all(observation["hosts"][tag] == 0 for tag in DIAGNOSTIC_HOST_TAGS)

    @staticmethod
    def _any_shell_witness(observation: dict[str, Any]) -> bool:
        return any((observation["hosts"][tag] or 0) > 0 for tag in DIAGNOSTIC_HOST_TAGS)

    def _observe_login_witnesses(
        self, page: Any, remaining_ms: int, entry_url: str | None = None
    ) -> dict[str, Any]:
        """Read the fixed public-safe witness set from fresh locators.

        Nothing outside this set is read, and nothing read here is text: the
        result carries counts, booleans and nulls only.
        """

        # Read in the committed order: the shell surfaces, then the shared
        # authentication witness set, then the Billing Manager observation.
        hosts = {tag: self._witness_count(page, tag) for tag in DIAGNOSTIC_HOST_TAGS}
        placeholder = self._presence_witness(page, "flt-semantics-placeholder")
        authentication = self._observe_authentication_witnesses(page, remaining_ms)
        observation: dict[str, Any] = {
            "hosts": hosts,
            "semantics_placeholder": placeholder,
            **authentication,
            "billing_manager": self._visibility_witness(
                lambda: page.get_by_role("link", name="Billing Manager", exact=True)
            ),
            # Preserved historical observation, now DERIVED from the strict
            # rejection witness rather than read again: an unambiguous
            # positively visible rejection is true, and anything else -- absent,
            # hidden, ambiguous or unreadable -- is false, exactly as the
            # original boolean behaved. Absence is never proven from it; that is
            # what the strict `rejection` witness is for.
            "visible_alert": self._positively_visible(authentication["rejection"]),
        }
        if entry_url is not None:
            observation["url_changed"] = self._url_changed(page, entry_url)
        return observation

    @staticmethod
    def _witness_count(page: Any, selector: str) -> int | None:
        try:
            return int(page.locator(selector).count())
        except Exception:
            return None

    def _presence_witness(self, page: Any, selector: str) -> dict[str, Any]:
        count = self._witness_count(page, selector)
        return {"count": count, "present": None if count is None else count > 0}

    @staticmethod
    def _visibility_witness(factory: Callable[[], Any]) -> dict[str, Any]:
        try:
            count = int(factory().count())
        except Exception:
            return {"count": None, "visible": None}
        if count == 0:
            return {"count": 0, "visible": False}
        try:
            visible: bool | None = bool(factory().is_visible())
        except Exception:
            visible = None
        return {"count": count, "visible": visible}

    def _actionable_witness(
        self, factory: Callable[[], Any], remaining_ms: int
    ) -> dict[str, Any]:
        witness = self._visibility_witness(factory)
        actionable: bool | None = None
        if witness["visible"] is False:
            actionable = False
        elif witness["visible"] is True:
            try:
                actionable = bool(self._probe_actionable(factory(), remaining_ms))
            except Exception:
                actionable = None
        return {
            "count": witness["count"],
            "visible": witness["visible"],
            "actionable": actionable,
        }

    @staticmethod
    def _current_url(page: Any) -> str | None:
        """Hold the entry address internally so only a boolean is ever reported."""

        try:
            return str(page.url)
        except Exception:
            return None

    def _url_changed(self, page: Any, entry_url: str | None) -> bool | None:
        current = self._current_url(page)
        if entry_url is None or current is None:
            return None
        return current != entry_url

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
        """Own the business route to verified results, after authentication.

        `login()` proves authentication and stops there
        (DL-XB-141-AUTH-LANDING-NAV-SEPARATION-G2-001). Everything from the EMS
        application entry to the confirmed post-search invoice list belongs
        here, so an EMS entry, Billing Manager or EB Bill that never becomes
        usable is a navigation failure and is never reported as a login failure.

        The authenticated landing is not the business surface
        (DL-XB-141-EMS-ENTRY-MINIMAL-REPAIR-G2-136). A first entry therefore
        opens the EMS application exactly once and then hands the route
        straight to `_open_eb_bill_route()`, which stays authoritative for
        everything after it. A restored results address is already inside the
        application, so that arm never actuates EMS at all: re-entering it would
        be a second real click that navigates nothing.
        """

        page = self._require_page()
        try:
            if entry_url is not None:
                page.goto(entry_url, wait_until="domcontentloaded")
            else:
                self._enter_ems_application(page)

            def account_locator() -> Any:
                return page.get_by_label("Tenant/account", exact=True)

            self._open_eb_bill_route(page)
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

    def _enter_ems_application(self, page: Any) -> None:
        """Prove the EMS application entry ready, then click it exactly once.

        This is the structural twin of `_dispatch_billing_manager()`, and
        deliberately nothing more. The control is resolved freshly here rather
        than reusing the locator the authentication witness was read through:
        that witness answered whether the landing was authenticated, which is
        not evidence that a business control is usable now.

        Readiness is proven on the existing shared bounded ladder -- exactly one
        exact match, visible, enabled and trial-actionable -- so an absent,
        duplicate, hidden, disabled or unreadable entry fails closed before
        anything is dispatched. An ambiguous match is never narrowed to one of
        its matches, and the selector is never weakened to another role, name
        or generic text.

        Proving readiness is classified here, not left to the caller. The shared
        ladder deliberately lets a non-timeout failure out of its enabled and
        trial-actionability probes intact, because a state that cannot be read
        is drift rather than lag and must never become a retry. Intact, however,
        it is also unclassified: it would leave this method as a bare browser
        exception and reach `_open_verified_results()`, whose generic arm would
        report a tenant/account contract failure for something that happened on
        the landing before any dispatch at all. So every way readiness can end
        without a proven control -- absent, duplicate, hidden, disabled,
        unreadable enabled-state, unreadable actionability -- is committed to
        the one pre-dispatch classification, with zero EMS clicks and therefore
        zero downstream dispatch. Only the shared ladder decides how long to
        wait; this adds no window, no look and no retry of its own.

        The normal click is the dispatch boundary, and is deliberately outside
        that normalisation. Once it begins, an exception cannot prove whether
        the browser acted, so the outcome is uncertain and terminal: there is no
        second EMS click, no re-resolution, no re-login and no fallback opener.
        A successful click hands the route to `_open_eb_bill_route()`
        immediately, so no second navigation or recovery system exists here.
        """

        try:
            control = self._resolve_ready_control(
                page,
                lambda: page.get_by_role("button", name=EMS_ENTRY_NAV_NAME, exact=True),
                "EMS application entry control",
                require_trial_actionable=True,
                messages=_uniform_messages(NAV_EMS_ENTRY_NOT_READY_MESSAGE),
            )
        except Exception as exc:
            # Nothing has been dispatched at this point, so the committed
            # pre-dispatch message is the whole truth regardless of which probe
            # failed or how. The cause is chained, never surfaced.
            raise LayoutChangedError(NAV_EMS_ENTRY_NOT_READY_MESSAGE) from exc
        try:
            control.click()
        except Exception as exc:
            raise LayoutChangedError(NAV_EMS_ENTRY_UNCERTAIN_MESSAGE) from exc

    def _open_eb_bill_route(self, page: Any) -> None:
        """Put the page on the exact EB Bill route, dispatching as little as possible.

        The retired shortcut inferred the route from the presence of the
        tenant/account selector. That inference was invalid: a selector renders
        on more than one route, so its presence never proved that EB Bill was
        active. Route identity is now proven positively, and only route proof
        can skip a navigation click.

        Which entry this surface offers is settled first, by one bounded
        mutation-free observation, and only then is anything dispatched. That
        ordering is the correction: an immediate single look cannot tell a
        surface that has no direct EB Bill entry from one that has not finished
        rendering it yet, so deciding from that look mis-routes a direct or
        restored EB Bill surface through the outer application -- which is
        exactly what a saved results address re-entering this method after
        `goto()` looks like while it settles.

        Three cases, in order:

        1. the exact EB Bill route becomes proven -- nothing is dispatched;
        2. the exact EB Bill control becomes positively usable -- exactly one
           EB Bill click, with no Billing Manager click at all;
        3. the direct control is positively absent or never usable inside the
           bounded window -- exactly one Billing Manager click, then the
           bounded wait for EB Bill readiness, then exactly one EB Bill click.

        Ambiguous and unreadable entry states are none of the three and fail
        closed instead of becoming a routing decision. No click is ever
        retried, no alternate opener or generic-text selector exists, and an
        ambiguous match is never narrowed to one of its matches.
        """

        entry = self._settle_eb_bill_entry(page)
        if entry == _EB_BILL_ENTRY_ROUTE_PROVEN:
            return
        if entry == _EB_BILL_ENTRY_OUTER:
            self._dispatch_billing_manager(page)
        self._dispatch_eb_bill(page)

    def _settle_eb_bill_entry(self, page: Any) -> str:
        """Settle which EB Bill entry this surface offers, dispatching nothing.

        One bounded observation answers both questions the routing decision
        needs -- whether the exact route is already proven, and whether the
        exact direct control is genuinely usable -- so a settling surface
        spends the committed recovery window once rather than once per
        question.

        The window ends in exactly one of three states. Route proof and a
        positively usable direct control are each decisive the moment they are
        observed. A direct control that is merely absent, or present but not
        yet usable, is an ordinary not-yet state: it keeps being re-observed at
        the committed checkpoints and only becomes "not directly available"
        once the window is genuinely exhausted. Ambiguity and an unreadable
        state are neither -- they are drift, and drift is never narrowed into a
        direct path nor masked behind the outer one.
        """

        # `_recover` reports only that its window expired, and here two
        # expiries mean opposite things, so the last verdict it observed is
        # what separates "no direct entry" from fail-closed drift.
        observed = [_PORTAL_ABSENT]

        def probe(remaining_ms: int) -> tuple[str, Any]:
            try:
                verdict, entry = self._observe_eb_bill_entry(page, remaining_ms)
            except _ControlUnresolved:
                observed[0] = _PORTAL_UNRESOLVED
                raise
            observed[0] = verdict
            return verdict, entry

        try:
            return self._recover(
                page,
                probe,
                "EB Bill navigation entry",
                messages=_uniform_messages(NAV_EB_BILL_NOT_READY_MESSAGE),
                classified=False,
            )
        except _PortalNotSettled as exc:
            if observed[0] in (_PORTAL_ABSENT, _PORTAL_NOT_READY):
                return _EB_BILL_ENTRY_OUTER
            raise LayoutChangedError(NAV_EB_BILL_NOT_READY_MESSAGE) from exc

    def _observe_eb_bill_entry(self, page: Any, remaining_ms: int) -> tuple[str, Any]:
        """Observe the EB Bill entry once. Inspection only, never a dispatch.

        This runs at every checkpoint of the bounded window, so nothing here
        may be externally meaningful: no navigation, and no real click. Only
        Playwright's own no-op trial actionability check is used, which is the
        same predicate `_resolve_ready_control()` proves readiness with.

        Readiness is proven, not inferred from existence. Exactly one exact
        match is necessary and not sufficient: while a route settles, that one
        match can still be hidden, disabled or unactionable, and none of those
        is a usable direct entry. More than one exact match is ambiguity, and a
        state that cannot be read at all is drift; neither is narrowed here.
        """

        if self._eb_bill_route_proven(page, remaining_ms):
            return _PORTAL_READY, _EB_BILL_ENTRY_ROUTE_PROVEN
        try:
            control = self._eb_bill_locator(page)
            count = int(control.count())
        except Exception as exc:
            raise _ControlUnresolved(exc) from exc
        if count == 0:
            return _PORTAL_ABSENT, None
        if count > 1:
            return _PORTAL_AMBIGUOUS, None
        try:
            visible = bool(control.is_visible())
        except Exception as exc:
            raise _ControlUnresolved(exc) from exc
        if not visible:
            return _PORTAL_NOT_READY, None
        if not self._probe_enabled(control, remaining_ms):
            return _PORTAL_NOT_READY, None
        if not self._probe_actionable(control, remaining_ms):
            return _PORTAL_NOT_READY, None
        return _PORTAL_READY, _EB_BILL_ENTRY_DIRECT_READY

    @staticmethod
    def _eb_bill_locator(page: Any) -> Any:
        """Resolve the one exact EB Bill control. Never narrowed, never weakened."""

        return page.get_by_role("link", name=EB_BILL_NAV_NAME, exact=True)

    def _dispatch_billing_manager(self, page: Any) -> None:
        """Prove the Billing Manager entry ready, then click it exactly once."""

        control = self._resolve_ready_control(
            page,
            lambda: page.get_by_role("link", name=BILLING_MANAGER_NAV_NAME, exact=True),
            "Billing Manager navigation control",
            require_trial_actionable=True,
            messages=_uniform_messages(NAV_BILLING_MANAGER_NOT_READY_MESSAGE),
        )
        # The explicit dispatch boundary. Once the normal click begins an
        # exception cannot prove whether the browser acted, so the outcome is
        # uncertain and terminal, and the click is never sent again.
        try:
            control.click()
        except Exception as exc:
            raise LayoutChangedError(NAV_BILLING_MANAGER_UNCERTAIN_MESSAGE) from exc

    def _dispatch_eb_bill(self, page: Any) -> None:
        """Prove EB Bill ready, click it once, then prove the route it opened.

        The three failures stay distinct: a control that never becomes ready
        fails before any dispatch, a click whose outcome cannot be established
        is terminal and is never retried, and a dispatched click whose route
        postcondition never becomes proven fails as an unproven results route
        rather than being clicked again.
        """

        control = self._resolve_ready_control(
            page,
            lambda: self._eb_bill_locator(page),
            "EB Bill navigation control",
            require_trial_actionable=True,
            messages=_uniform_messages(NAV_EB_BILL_NOT_READY_MESSAGE),
        )
        try:
            control.click()
        except Exception as exc:
            raise LayoutChangedError(NAV_EB_BILL_UNCERTAIN_MESSAGE) from exc
        self._await_condition(
            page,
            lambda remaining_ms: self._eb_bill_route_proven(page, remaining_ms),
            "EB Bill results route",
            messages=_uniform_messages(NAV_RESULTS_ROUTE_UNPROVED_MESSAGE),
        )

    def _eb_bill_route_proven(
        self, page: Any, remaining_ms: int = MAX_PORTAL_PROBE_TIMEOUT_MS
    ) -> bool:
        """Report whether the page is on the exact EB Bill route, as a boolean only.

        The proof is internal and privacy-closed. Two addresses are compared
        inside this method and neither is returned, logged, raised, retained or
        placed on any diagnostic or public-safe surface: the only thing that
        leaves is one boolean.

        Equivalence is same-origin plus path. The exact EB Bill control's own
        target is resolved against the current address, so a relative target is
        same-origin by construction and a cross-origin target can never match.
        Query and fragment are ignored -- and only they -- so an already-valid
        saved results address that carries page, account and search parameters
        is still recognised as the EB Bill route.

        Anything that cannot be read or parsed is not proof: an absent,
        ambiguous or unreadable control, a missing target, or an unparseable
        address all fail closed as an unproven route.
        """

        try:
            control = self._eb_bill_locator(page)
            if int(control.count()) != 1:
                return False
            target = control.get_attribute(
                "href", timeout=self._probe_timeout_ms(remaining_ms)
            )
            current = page.url
            if not target or not current:
                return False
            resolved = urlparse(urljoin(str(current), str(target)))
            here = urlparse(str(current))
        except Exception:
            return False
        if not resolved.scheme or resolved.scheme != here.scheme:
            return False
        if not resolved.netloc or resolved.netloc != here.netloc:
            return False
        return self._route_path(resolved.path) == self._route_path(here.path)

    @staticmethod
    def _route_path(path: str) -> str:
        """Normalise one route path for comparison, without exposing it."""

        return path.rstrip("/") or "/"

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
