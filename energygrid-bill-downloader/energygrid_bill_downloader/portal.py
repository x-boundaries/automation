from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from .config import RuntimeConfig
from .errors import (
    DOWNLOAD_FAILED,
    AppError,
    DependencyError,
    DownloadError,
    LayoutChangedError,
    LoginError,
)


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

# ---- historical business navigation names (navigation diagnostic only) ---- #
#
# The historical EMS / Billing Manager / EB Bill link route. Since DL-XB-199
# the production path no longer uses any of these: they remain because the
# separate navigation diagnostic still observes them, and that diagnostic is
# deliberately not rewritten in this lineage. `EMS_ENTRY_NAV_NAME` and
# `AUTHENTICATION_WITNESS_NAME` currently carry the same text and stay
# deliberately separate symbols, because they answer different questions -- one
# is the evidence a landing is authenticated, the other is the control the
# diagnostic clicks -- and either may move without the other.
EMS_ENTRY_NAV_NAME = "EMS"
BILLING_MANAGER_NAV_NAME = "Billing Manager"
EB_BILL_NAV_NAME = "EB Bill"

NAV_EMS_ENTRY_NOT_READY_MESSAGE = "EMS application entry control is not ready"
NAV_EMS_ENTRY_UNCERTAIN_MESSAGE = "EMS application entry dispatch outcome uncertain"

# ---- production single-surface contract (DL-XB-199, G2-076) ---- #
#
# The exact accessible names the production path resolves. `EB_BILL_TAB_NAME`
# is the live `tab` and stays a separate symbol from the diagnostic's
# historical `EB_BILL_NAV_NAME` link, although both carry the same text.
EB_BILL_TAB_NAME = "EB Bill"
TENANT_BILL_TAB_NAME = "Tenant Bill"
SEARCH_BUTTON_NAME = "Search"
DOWNLOAD_BUTTON_NAME = "Download"

# Inspection-only pagination/completeness sentinels. Any exact button or link
# with one of these names fails the results closed; none is ever clicked, and
# their absence is NOT a proof that all invoice history is rendered
# (PRE_SCHEDULER_RESULT_COMPLETENESS_EVIDENCE_REQUIRED=YES).
RESULTS_PAGINATION_SENTINEL_NAMES = ("Next page", "Next", "Previous page", "Load more")

# A normalised row accessible text longer than this is unreadable, not usable.
RESULTS_ROW_TEXT_MAX_CHARS = 4096

# Fixed public-safe messages. Each names the contract that failed and never
# what was observed: no row text, digest, filename, account text or address.
EB_BILL_TAB_NOT_READY_MESSAGE = "EB Bill tab is not ready"
EB_BILL_TAB_UNCERTAIN_MESSAGE = "EB Bill tab dispatch outcome uncertain"
EB_BILL_TAB_UNPROVED_MESSAGE = "EB Bill tab selection was not proven"
ACCOUNT_WITNESS_UNPROVED_MESSAGE = "configured account witness was not proven"
ACCOUNT_WITNESS_AMBIGUOUS_MESSAGE = "configured account witness is ambiguous"
SEARCH_NOT_READY_MESSAGE = "Search control is not ready"
SEARCH_UNCERTAIN_MESSAGE = "Search dispatch outcome uncertain"
RESULTS_TOPOLOGY_MESSAGE = "portal page topology is not unique"
RESULTS_UNSETTLED_MESSAGE = "invoice results table did not settle"
RESULTS_HEADER_ONLY_MESSAGE = "invoice results table has no invoice rows"
RESULTS_PAGINATION_MESSAGE = "invoice results expose a pagination sentinel"
RESULTS_ROWCOUNT_MESSAGE = "invoice results row count is contradictory"
RESULTS_ROW_IDENTITY_MESSAGE = "invoice results row identity is invalid"
RESULTS_CEILING_MESSAGE = "invoice results safety ceiling exceeded"
RESULTS_INVENTORY_CONSUMED_MESSAGE = "invoice results inventory was already taken"

# Download-time messages. These end one row's acquisition inside reconcile and
# are reported by status only; they are never a terminal run reference.
RESULTS_SURFACE_CHANGED_MESSAGE = "invoice results surface changed"
RESULTS_LATCHED_MESSAGE = "invoice downloads are latched after an earlier failure"
RESULTS_ROW_HANDLE_MESSAGE = "invoice row handle is not valid for this inventory"
RESULTS_FILENAME_CHANGED_MESSAGE = "invoice download filename changed across attempts"
DOWNLOAD_UNCERTAIN_MESSAGE = "invoice download dispatch outcome uncertain"

# Returns one boolean per matched node: true when it sits outside every table,
# grid, row and cell semantic. It reads no text and returns nothing else.
_OUTSIDE_RESULTS_PREDICATE = """
(nodes) => nodes.map((node) => !node.closest(
  'table, thead, tbody, tfoot, tr, td, th, [role="table"], [role="grid"], '
  + '[role="treegrid"], [role="rowgroup"], [role="row"], [role="cell"], '
  + '[role="gridcell"], [role="columnheader"], [role="rowheader"]'
))
"""

_TAB_SELECTED = "selected"
_TAB_UNSELECTED = "unselected"
_TAB_ABSENT = "absent"
_TAB_AMBIGUOUS = "ambiguous"
_TAB_CONTRADICTORY = "contradictory"

# A bounded attribute read that timed out. Distinct from `None`, which is a
# positively read absent attribute.
_UNREAD = object()


def _normalise_surface_text(text: str) -> str:
    """NFC, collapse whitespace, strip. Deliberately no casefold."""

    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def _uncertain_download() -> AppError:
    """The one non-retryable uncertain-dispatch download failure."""

    return AppError(DOWNLOAD_UNCERTAIN_MESSAGE, status=DOWNLOAD_FAILED, retryable=False)

# ---- bounded navigation diagnostic ---- #
#
# This operation is deliberately separate from the production results route.
# It proves the authenticated landing once, consumes at most one EMS entry
# dispatch, and then observes a fixed public-safe surface without actuating any
# downstream control. The 60-second deadline is shared by every checkpoint;
# each event-loop yield is independently capped so a page default timeout can
# never turn one probe into an unbounded wait.
NAVIGATION_DIAGNOSTIC_CHECKPOINTS_MS = (0, 250, 1000, 5000, 10000, 30000, 45000)
NAVIGATION_DIAGNOSTIC_DEADLINE_SECONDS = 60.0

NAVIGATION_DIAGNOSTIC_CONFIG_STATE = "CONFIGURATION"
NAVIGATION_DIAGNOSTIC_AUTHENTICATION_STATE = "AUTHENTICATION"
NAVIGATION_DIAGNOSTIC_PRE_EMS_TOPOLOGY_STATE = "PRE_EMS_TOPOLOGY"
NAVIGATION_DIAGNOSTIC_EMS_READINESS_STATE = "EMS_READINESS"
NAVIGATION_DIAGNOSTIC_EMS_DISPATCH_STATE = "EMS_DISPATCH"
NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_STATE = "POST_EMS_OBSERVATION"
NAVIGATION_DIAGNOSTIC_OUTPUT_VALIDATION_STATE = "OUTPUT_VALIDATION"
NAVIGATION_DIAGNOSTIC_COMPLETE_STATE = "COMPLETE"
NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE = "ACTION_REQUIRED"
NAVIGATION_DIAGNOSTIC_STATES = (
    NAVIGATION_DIAGNOSTIC_CONFIG_STATE,
    NAVIGATION_DIAGNOSTIC_AUTHENTICATION_STATE,
    NAVIGATION_DIAGNOSTIC_PRE_EMS_TOPOLOGY_STATE,
    NAVIGATION_DIAGNOSTIC_EMS_READINESS_STATE,
    NAVIGATION_DIAGNOSTIC_EMS_DISPATCH_STATE,
    NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_STATE,
    NAVIGATION_DIAGNOSTIC_OUTPUT_VALIDATION_STATE,
    NAVIGATION_DIAGNOSTIC_COMPLETE_STATE,
    NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
)

NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_PAGES = "PRE_EMS_MULTIPLE_PAGES"
NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_FRAMES = "PRE_EMS_MULTIPLE_FRAMES"
NAVIGATION_DIAGNOSTIC_AUTHENTICATION_NOT_PROVEN = "AUTHENTICATION_NOT_PROVEN"
NAVIGATION_DIAGNOSTIC_EMS_NOT_READY = "EMS_NOT_READY"
NAVIGATION_DIAGNOSTIC_EMS_DISPATCH_UNCERTAIN = "EMS_DISPATCH_UNCERTAIN"
NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_PAGES = "POST_EMS_MULTIPLE_PAGES"
NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_FRAMES = "POST_EMS_MULTIPLE_FRAMES"
NAVIGATION_DIAGNOSTIC_POST_EMS_CROSS_ORIGIN = "POST_EMS_CROSS_ORIGIN"
NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_ROUTE_PROVEN = (
    "POST_EMS_EB_BILL_ROUTE_PROVEN"
)
NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_LINK_READY = (
    "POST_EMS_BILLING_MANAGER_LINK_READY"
)
NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_BUTTON_READY = (
    "POST_EMS_BILLING_MANAGER_BUTTON_READY"
)
NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_LINK_READY = "POST_EMS_EB_BILL_LINK_READY"
NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_BUTTON_READY = (
    "POST_EMS_EB_BILL_BUTTON_READY"
)
NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED = "POST_EMS_WINDOW_EXHAUSTED"
NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE = (
    "POST_EMS_OBSERVATION_UNREADABLE"
)
NAVIGATION_DIAGNOSTIC_OUTPUT_REJECTED = "OUTPUT_REJECTED"
NAVIGATION_DIAGNOSTIC_CONFIGURATION_FAILED = "CONFIGURATION_FAILED"

NAVIGATION_DIAGNOSTIC_RESULT_IDENTIFIERS = (
    NAVIGATION_DIAGNOSTIC_AUTHENTICATION_NOT_PROVEN,
    NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_PAGES,
    NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_FRAMES,
    NAVIGATION_DIAGNOSTIC_EMS_NOT_READY,
    NAVIGATION_DIAGNOSTIC_EMS_DISPATCH_UNCERTAIN,
    NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_PAGES,
    NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_FRAMES,
    NAVIGATION_DIAGNOSTIC_POST_EMS_CROSS_ORIGIN,
    NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_ROUTE_PROVEN,
    NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_LINK_READY,
    NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_BUTTON_READY,
    NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_LINK_READY,
    NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_BUTTON_READY,
    NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED,
    NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE,
    NAVIGATION_DIAGNOSTIC_OUTPUT_REJECTED,
    NAVIGATION_DIAGNOSTIC_CONFIGURATION_FAILED,
)

NAVIGATION_DIAGNOSTIC_COMPLETE_RESULTS = frozenset(
    {
        NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_ROUTE_PROVEN,
        NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_LINK_READY,
        NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_BUTTON_READY,
        NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_LINK_READY,
        NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_BUTTON_READY,
        NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED,
    }
)

NAVIGATION_DIAGNOSTIC_CONTROL_SPECS = (
    ("button", EMS_ENTRY_NAV_NAME, "button_ems"),
    ("link", EMS_ENTRY_NAV_NAME, "link_ems"),
    ("link", BILLING_MANAGER_NAV_NAME, "link_billing_manager"),
    ("button", BILLING_MANAGER_NAV_NAME, "button_billing_manager"),
    ("link", EB_BILL_NAV_NAME, "link_eb_bill"),
    ("button", EB_BILL_NAV_NAME, "button_eb_bill"),
)

NAVIGATION_DIAGNOSTIC_PAGE_TOPOLOGY_MESSAGE = (
    "navigation diagnostic page topology is not unique"
)
NAVIGATION_DIAGNOSTIC_FRAME_TOPOLOGY_MESSAGE = (
    "navigation diagnostic frame topology is not unique"
)
NAVIGATION_DIAGNOSTIC_CROSS_ORIGIN_MESSAGE = (
    "navigation diagnostic crossed origin boundary"
)
NAVIGATION_DIAGNOSTIC_OBSERVATION_UNREADABLE_MESSAGE = (
    "navigation diagnostic observation was unreadable"
)
NAVIGATION_DIAGNOSTIC_OUTPUT_REJECTED_MESSAGE = (
    "navigation diagnostic output was rejected"
)

_NAVIGATION_DIAGNOSTIC_COUNT_GT_ONE = ">1"

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
class InvoiceRow:
    """Opaque pre-download handle: no filename, row text or digest.

    `binding` is valid only for the exact inventory episode of the portal
    instance that issued it, and it is excluded from `repr` so no debug surface
    can display it. The authoritative invoice name exists only after Download.
    """

    ordinal: int
    binding: object = field(repr=False)


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


def _unobserved_navigation_control(role: str, name: str) -> dict[str, Any]:
    """Return one fixed control witness without retaining a locator or text."""

    return {
        "role": role,
        "name": name,
        "count": None,
        "visible": None,
        "enabled": None,
        "trial_actionable": None,
    }


def unobserved_navigation_pre_ems() -> dict[str, Any]:
    """Return the complete pre-EMS evidence shape with no observations."""

    return {
        "context_pages": None,
        "bound_page_frames": None,
        "ems": _unobserved_navigation_control("button", EMS_ENTRY_NAV_NAME),
    }


def unobserved_navigation_post_ems() -> dict[str, Any]:
    """Return the complete post-EMS evidence shape with no observations."""

    return {
        "context_pages": None,
        "bound_page_frames": None,
        "route_changed": None,
        "same_origin": None,
        "eb_bill_route_proven": None,
        "controls": {
            key: _unobserved_navigation_control(role, name)
            for role, name, key in NAVIGATION_DIAGNOSTIC_CONTROL_SPECS
        },
    }


@dataclass(frozen=True)
class NavigationDiagnosticResult:
    """The bounded post-login navigation observation.

    The evidence members contain only the fixed public-safe shape returned by
    the portal helpers. ``failure`` is retained for CLI reference mapping only;
    its message is never emitted.
    """

    result: str = NAVIGATION_DIAGNOSTIC_AUTHENTICATION_NOT_PROVEN
    status: str = NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE
    authentication_proven: bool = False
    ems_dispatch_attempted: bool = False
    ems_dispatch_uncertain: bool = False
    pre_ems: dict[str, Any] = field(default_factory=unobserved_navigation_pre_ems)
    post_ems: dict[str, Any] = field(default_factory=unobserved_navigation_post_ems)
    failure: Any = None


class _NavigationObservationUnreadable(Exception):
    """An observation could not be classified without exposing its cause."""


class _NavigationDeadlineExhausted(Exception):
    """The one diagnostic deadline is spent before another operation starts."""


class _NavigationFreshnessChanged(Exception):
    """The bound page, frame topology or origin changed during observation."""

    def __init__(
        self,
        result: str,
        page_count: int | str | None = None,
        frame_count: int | str | None = None,
    ) -> None:
        super().__init__("navigation diagnostic freshness changed")
        self.result = result
        self.page_count = page_count
        self.frame_count = frame_count


class _NavigationControlNotReady(Exception):
    """The exact diagnostic control did not become ready in its window."""


class PlaywrightPortal:
    """The only module that knows the portal DOM contract."""

    def __init__(self, config: RuntimeConfig, headed: bool = False) -> None:
        self.config = config
        self.headed = headed
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        # Private one-session results binding (DL-XB-199). The key, the frozen
        # row digests and the learned filenames never leave this instance.
        self._row_identity_key = secrets.token_bytes(32)
        self._inventory_taken = False
        self._inventory_binding: object | None = None
        self._frozen_rows: tuple[bytes, ...] | None = None
        self._safety_ceiling = 0
        self._row_filenames: dict[int, str] = {}
        self._latched = False

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
        # Drop the session binding; a closed instance can never download.
        self._row_identity_key = secrets.token_bytes(32)
        self._inventory_binding = None
        self._frozen_rows = None
        self._row_filenames = {}

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

    # ---- bounded navigation diagnostic ---- #

    @staticmethod
    def _navigation_capped_count(count: Any) -> int | str:
        """Cap a page, frame or locator count to the public-safe vocabulary."""

        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise _NavigationObservationUnreadable
        return _NAVIGATION_DIAGNOSTIC_COUNT_GT_ONE if count > 1 else count

    def _navigation_topology(
        self, page: Any
    ) -> tuple[int | str | None, int | str | None, str | None]:
        """Read only the bound page topology, without switching or traversing."""

        try:
            context = self.context
            if context is None:
                raise RuntimeError
            pages = context.pages
            page_count = self._navigation_capped_count(len(pages))
        except Exception:
            return None, None, "page"
        if page_count == _NAVIGATION_DIAGNOSTIC_COUNT_GT_ONE:
            return page_count, None, "page"
        if page_count == 1:
            try:
                if pages[0] is not page:
                    return page_count, None, "page"
            except Exception:
                return page_count, None, "page"
        try:
            frame_count = self._navigation_capped_count(len(page.frames))
        except Exception:
            return page_count, None, "frame"
        return page_count, frame_count, None

    @staticmethod
    def _navigation_origin(address: str) -> tuple[str, str, int | None] | None:
        """Return normalized origin identity without retaining the address."""

        try:
            parsed = urlparse(address)
            scheme = parsed.scheme.casefold()
            hostname = parsed.hostname
            if not scheme or not hostname:
                return None
            hostname = hostname.casefold()
            port = parsed.port
            if port is None:
                if scheme == "http":
                    port = 80
                elif scheme == "https":
                    port = 443
            return scheme, hostname, port
        except Exception:
            return None

    @classmethod
    def _navigation_same_origin(cls, before: str, after: str) -> bool | None:
        left = cls._navigation_origin(before)
        right = cls._navigation_origin(after)
        if left is None or right is None:
            return None
        return left == right

    @staticmethod
    def _navigation_remaining_ms(deadline: float) -> int:
        """Return the remaining diagnostic budget from one absolute deadline."""

        return int((deadline - time.monotonic()) * 1000)

    def _navigation_probe_timeout(self, deadline: float) -> int:
        """Cap one diagnostic operation without starting it after exhaustion."""

        remaining_ms = self._navigation_remaining_ms(deadline)
        if remaining_ms <= 0:
            return 0
        return min(MAX_PORTAL_PROBE_TIMEOUT_MS, remaining_ms)

    def _navigation_freshness(
        self,
        page: Any,
        expected_origin: tuple[str, str, int | None],
        deadline: float,
        *,
        post_ems: bool = False,
    ) -> int:
        """Prove the bound surface is still safe and return one probe timeout.

        This is diagnostic-only. It never switches pages or traverses frames,
        and it returns no address or page object to the evidence document.
        """

        if self._navigation_remaining_ms(deadline) <= 0:
            raise _NavigationDeadlineExhausted
        page_count, frame_count, topology_error = self._navigation_topology(page)
        if topology_error == "page" or page_count != 1:
            result = (
                NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_PAGES
                if post_ems
                else NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_PAGES
            )
            raise _NavigationFreshnessChanged(result, page_count, frame_count)
        if topology_error == "frame" or frame_count != 1:
            result = (
                NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_FRAMES
                if post_ems
                else NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_FRAMES
            )
            raise _NavigationFreshnessChanged(result, page_count, frame_count)
        current_url = self._current_url(page)
        current_origin = (
            None if current_url is None else self._navigation_origin(current_url)
        )
        if current_origin is None:
            result = (
                NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE
                if post_ems
                else NAVIGATION_DIAGNOSTIC_EMS_NOT_READY
            )
            raise _NavigationFreshnessChanged(result, page_count, frame_count)
        if current_origin != expected_origin:
            result = (
                NAVIGATION_DIAGNOSTIC_POST_EMS_CROSS_ORIGIN
                if post_ems
                else NAVIGATION_DIAGNOSTIC_EMS_NOT_READY
            )
            raise _NavigationFreshnessChanged(result, page_count, frame_count)
        remaining_ms = self._navigation_remaining_ms(deadline)
        if remaining_ms <= 0:
            raise _NavigationDeadlineExhausted
        return min(MAX_PORTAL_PROBE_TIMEOUT_MS, remaining_ms)

    @staticmethod
    def _navigation_ready_control_observation(
        role: str, name: str
    ) -> dict[str, Any]:
        return {
            "role": role,
            "name": name,
            "count": 1,
            "visible": True,
            "enabled": True,
            "trial_actionable": True,
        }

    @staticmethod
    def _navigation_control_ready(observation: Mapping[str, Any]) -> bool:
        return (
            observation.get("count") == 1
            and observation.get("visible") is True
            and observation.get("enabled") is True
            and observation.get("trial_actionable") is True
        )

    def _navigation_resolve_ready_control(
        self,
        page: Any,
        role: str,
        name: str,
        start: float,
        deadline: float,
        expected_origin: tuple[str, str, int | None],
    ) -> tuple[Any, int]:
        """Resolve one exact control under the shared diagnostic deadline."""

        for target_ms in NAVIGATION_DIAGNOSTIC_CHECKPOINTS_MS:
            if not self._navigation_wait_to_checkpoint(
                page, start, deadline, target_ms
            ):
                raise _NavigationDeadlineExhausted
            self._navigation_freshness(page, expected_origin, deadline)
            try:
                locator = page.get_by_role(role, name=name, exact=True)
                count = self._navigation_capped_count(locator.count())
            except Exception as exc:
                raise _NavigationObservationUnreadable from exc
            self._navigation_freshness(page, expected_origin, deadline)
            if count != 1:
                continue

            timeout_ms = self._navigation_probe_timeout(deadline)
            if timeout_ms <= 0:
                raise _NavigationDeadlineExhausted
            try:
                visible = bool(locator.is_visible(timeout=timeout_ms))
            except Exception as exc:
                raise _NavigationObservationUnreadable from exc
            self._navigation_freshness(page, expected_origin, deadline)
            if not visible:
                continue

            remaining_ms = self._navigation_remaining_ms(deadline)
            if remaining_ms <= 0:
                raise _NavigationDeadlineExhausted
            try:
                enabled = bool(self._probe_enabled(locator, remaining_ms))
            except Exception as exc:
                raise _NavigationObservationUnreadable from exc
            self._navigation_freshness(page, expected_origin, deadline)
            if not enabled:
                continue

            remaining_ms = self._navigation_remaining_ms(deadline)
            if remaining_ms <= 0:
                raise _NavigationDeadlineExhausted
            try:
                actionable = bool(self._probe_actionable(locator, remaining_ms))
            except Exception as exc:
                raise _NavigationObservationUnreadable from exc
            self._navigation_freshness(page, expected_origin, deadline)
            if not actionable:
                continue

            # This is the action-boundary guard. The returned timeout is the
            # only value used by the immediately following normal click: no
            # wait, locator resolution or additional probe may be inserted.
            return locator, self._navigation_freshness(
                page, expected_origin, deadline
            )
        raise _NavigationControlNotReady

    def _observe_navigation_control(
        self,
        page: Any,
        role: str,
        name: str,
        deadline: float,
        expected_origin: tuple[str, str, int | None],
    ) -> dict[str, Any]:
        """Read one fixed control; this helper can never issue a normal click."""

        self._navigation_freshness(
            page, expected_origin, deadline, post_ems=True
        )
        try:
            locator = page.get_by_role(role, name=name, exact=True)
            count = self._navigation_capped_count(locator.count())
        except Exception as exc:
            raise _NavigationObservationUnreadable from exc
        self._navigation_freshness(
            page, expected_origin, deadline, post_ems=True
        )
        if count == 0:
            return {
                "role": role,
                "name": name,
                "count": 0,
                "visible": False,
                "enabled": False,
                "trial_actionable": False,
            }
        if count == _NAVIGATION_DIAGNOSTIC_COUNT_GT_ONE:
            return {
                "role": role,
                "name": name,
                "count": count,
                "visible": None,
                "enabled": None,
                "trial_actionable": None,
            }
        timeout_ms = self._navigation_probe_timeout(deadline)
        if timeout_ms <= 0:
            raise _NavigationDeadlineExhausted
        try:
            visible = bool(locator.is_visible(timeout=timeout_ms))
        except Exception as exc:
            raise _NavigationObservationUnreadable from exc
        self._navigation_freshness(
            page, expected_origin, deadline, post_ems=True
        )
        if not visible:
            return {
                "role": role,
                "name": name,
                "count": 1,
                "visible": False,
                "enabled": False,
                "trial_actionable": False,
            }
        remaining_ms = self._navigation_remaining_ms(deadline)
        if remaining_ms <= 0:
            raise _NavigationDeadlineExhausted
        try:
            enabled = bool(self._probe_enabled(locator, remaining_ms))
        except Exception as exc:
            raise _NavigationObservationUnreadable from exc
        self._navigation_freshness(
            page, expected_origin, deadline, post_ems=True
        )
        if not enabled:
            return {
                "role": role,
                "name": name,
                "count": 1,
                "visible": True,
                "enabled": False,
                "trial_actionable": False,
            }
        remaining_ms = self._navigation_remaining_ms(deadline)
        if remaining_ms <= 0:
            raise _NavigationDeadlineExhausted
        try:
            actionable = bool(self._probe_actionable(locator, remaining_ms))
        except Exception as exc:
            raise _NavigationObservationUnreadable from exc
        self._navigation_freshness(
            page, expected_origin, deadline, post_ems=True
        )
        return {
            "role": role,
            "name": name,
            "count": 1,
            "visible": True,
            "enabled": True,
            "trial_actionable": actionable,
        }

    @staticmethod
    def _navigation_failure(result: str) -> LayoutChangedError:
        messages = {
            NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_PAGES: NAVIGATION_DIAGNOSTIC_PAGE_TOPOLOGY_MESSAGE,
            NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_FRAMES: NAVIGATION_DIAGNOSTIC_FRAME_TOPOLOGY_MESSAGE,
            NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_PAGES: NAVIGATION_DIAGNOSTIC_PAGE_TOPOLOGY_MESSAGE,
            NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_FRAMES: NAVIGATION_DIAGNOSTIC_FRAME_TOPOLOGY_MESSAGE,
            NAVIGATION_DIAGNOSTIC_POST_EMS_CROSS_ORIGIN: NAVIGATION_DIAGNOSTIC_CROSS_ORIGIN_MESSAGE,
            NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE: NAVIGATION_DIAGNOSTIC_OBSERVATION_UNREADABLE_MESSAGE,
        }
        return LayoutChangedError(
            messages.get(result, NAVIGATION_DIAGNOSTIC_OBSERVATION_UNREADABLE_MESSAGE)
        )

    def navigation_diagnostic(self) -> NavigationDiagnosticResult:
        """Run the bounded post-login navigation observation and stop.

        This method deliberately does not call the production results opener,
        Billing Manager/EB Bill dispatch helpers, inventory or download paths.
        The canonical login is the only authentication precondition and is
        invoked exactly once. The one normal EMS click is the sole navigation
        dispatch owned by this diagnostic; every later control read uses only a
        trial actionability probe.
        """

        # Keep the state names explicit at the operation boundary. The CLI's
        # output builder owns OUTPUT_VALIDATION; no state is inferred from an
        # exception or emitted as free-form text.
        pre_ems = unobserved_navigation_pre_ems()
        post_ems = unobserved_navigation_post_ems()
        try:
            self.login()
        except AppError as exc:
            return NavigationDiagnosticResult(
                result=NAVIGATION_DIAGNOSTIC_AUTHENTICATION_NOT_PROVEN,
                status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                pre_ems=pre_ems,
                post_ems=post_ems,
                failure=exc,
            )
        except Exception:
            return NavigationDiagnosticResult(
                result=NAVIGATION_DIAGNOSTIC_AUTHENTICATION_NOT_PROVEN,
                status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                pre_ems=pre_ems,
                post_ems=post_ems,
                failure=LayoutChangedError(AUTHENTICATION_UNPROVED_MESSAGE),
            )

        start = time.monotonic()
        deadline = start + NAVIGATION_DIAGNOSTIC_DEADLINE_SECONDS
        try:
            page = self._require_page()
        except AppError as exc:
            return NavigationDiagnosticResult(
                result=NAVIGATION_DIAGNOSTIC_AUTHENTICATION_NOT_PROVEN,
                status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                authentication_proven=True,
                pre_ems=pre_ems,
                post_ems=post_ems,
                failure=exc,
            )
        page_count, frame_count, topology_error = self._navigation_topology(page)
        pre_ems["context_pages"] = page_count
        pre_ems["bound_page_frames"] = frame_count
        if topology_error == "page" or page_count != 1:
            return NavigationDiagnosticResult(
                result=NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_PAGES,
                status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                authentication_proven=True,
                pre_ems=pre_ems,
                post_ems=post_ems,
                failure=self._navigation_failure(NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_PAGES),
            )
        if topology_error == "frame" or frame_count != 1:
            return NavigationDiagnosticResult(
                result=NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_FRAMES,
                status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                authentication_proven=True,
                pre_ems=pre_ems,
                post_ems=post_ems,
                failure=self._navigation_failure(NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_FRAMES),
            )
        pre_url = self._current_url(page)
        if pre_url is None or self._navigation_origin(pre_url) is None:
            return NavigationDiagnosticResult(
                result=NAVIGATION_DIAGNOSTIC_AUTHENTICATION_NOT_PROVEN,
                status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                authentication_proven=True,
                pre_ems=pre_ems,
                post_ems=post_ems,
                failure=self._navigation_failure(
                    NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE
                ),
            )

        expected_origin = self._navigation_origin(pre_url)
        if expected_origin is None:
            return NavigationDiagnosticResult(
                result=NAVIGATION_DIAGNOSTIC_EMS_NOT_READY,
                status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                authentication_proven=True,
                pre_ems=pre_ems,
                post_ems=post_ems,
                failure=LayoutChangedError(NAV_EMS_ENTRY_NOT_READY_MESSAGE),
            )
        try:
            self._navigation_freshness(page, expected_origin, deadline)
            ems, ems_timeout_ms = self._navigation_resolve_ready_control(
                page,
                "button",
                EMS_ENTRY_NAV_NAME,
                start,
                deadline,
                expected_origin,
            )
        except _NavigationFreshnessChanged as exc:
            pre_ems["context_pages"] = exc.page_count
            pre_ems["bound_page_frames"] = exc.frame_count
            if exc.result in {
                NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_PAGES,
                NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_FRAMES,
            }:
                return NavigationDiagnosticResult(
                    result=exc.result,
                    status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                    authentication_proven=True,
                    pre_ems=pre_ems,
                    post_ems=post_ems,
                    failure=self._navigation_failure(exc.result),
                )
            return NavigationDiagnosticResult(
                result=NAVIGATION_DIAGNOSTIC_EMS_NOT_READY,
                status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                authentication_proven=True,
                pre_ems=pre_ems,
                post_ems=post_ems,
                failure=LayoutChangedError(NAV_EMS_ENTRY_NOT_READY_MESSAGE),
            )
        except (
            _NavigationControlNotReady,
            _NavigationDeadlineExhausted,
            _NavigationObservationUnreadable,
        ):
            return NavigationDiagnosticResult(
                result=NAVIGATION_DIAGNOSTIC_EMS_NOT_READY,
                status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                authentication_proven=True,
                pre_ems=pre_ems,
                post_ems=post_ems,
                failure=LayoutChangedError(NAV_EMS_ENTRY_NOT_READY_MESSAGE),
            )
        except Exception:
            return NavigationDiagnosticResult(
                result=NAVIGATION_DIAGNOSTIC_EMS_NOT_READY,
                status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                authentication_proven=True,
                pre_ems=pre_ems,
                post_ems=post_ems,
                failure=LayoutChangedError(NAV_EMS_ENTRY_NOT_READY_MESSAGE),
            )
        pre_ems["ems"] = self._navigation_ready_control_observation(
            "button", EMS_ENTRY_NAV_NAME
        )

        # `ems_timeout_ms` and `ems` came from the final freshness guard in
        # `_navigation_resolve_ready_control`. Keep the next operation as the
        # single normal EMS dispatch: no locator resolution, wait or probe is
        # allowed between that guard and this click.
        try:
            ems.click(timeout=ems_timeout_ms)
        except Exception:
            return NavigationDiagnosticResult(
                result=NAVIGATION_DIAGNOSTIC_EMS_DISPATCH_UNCERTAIN,
                status=NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
                authentication_proven=True,
                ems_dispatch_attempted=True,
                ems_dispatch_uncertain=True,
                pre_ems=pre_ems,
                post_ems=post_ems,
                failure=LayoutChangedError(NAV_EMS_ENTRY_UNCERTAIN_MESSAGE),
            )

        try:
            result, post_ems = self._observe_navigation_post_ems(
                page,
                pre_url,
                expected_origin,
                start,
                deadline,
            )
        except Exception:
            result = NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE
            post_ems = unobserved_navigation_post_ems()
        complete = result in NAVIGATION_DIAGNOSTIC_COMPLETE_RESULTS
        state = (
            NAVIGATION_DIAGNOSTIC_COMPLETE_STATE
            if complete
            else NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE
        )
        return NavigationDiagnosticResult(
            result=result,
            status=state,
            authentication_proven=True,
            ems_dispatch_attempted=True,
            ems_dispatch_uncertain=False,
            pre_ems=pre_ems,
            post_ems=post_ems,
            failure=None if complete else self._navigation_failure(result),
        )

    def _navigation_wait_to_checkpoint(
        self, page: Any, start: float, deadline: float, target_ms: int
    ) -> bool:
        """Yield in <=1s slices until one exact checkpoint is due."""

        while True:
            now = time.monotonic()
            remaining_ms = int((deadline - now) * 1000)
            elapsed_ms = int((now - start) * 1000)
            if remaining_ms <= 0:
                return False
            pending_ms = target_ms - elapsed_ms
            if pending_ms <= 0:
                return True
            wait_ms = min(pending_ms, remaining_ms, MAX_PORTAL_PROBE_TIMEOUT_MS)
            try:
                page.wait_for_timeout(wait_ms)
            except Exception as exc:
                raise _NavigationObservationUnreadable from exc
            if self._navigation_remaining_ms(deadline) <= 0:
                return False

    def _navigation_diagnostic_eb_bill_route_proven(
        self,
        page: Any,
        expected_origin: tuple[str, str, int | None],
        deadline: float,
    ) -> bool:
        """Prove the EB Bill route with diagnostic-only freshness guards."""

        self._navigation_freshness(
            page, expected_origin, deadline, post_ems=True
        )
        try:
            control = self._eb_bill_locator(page)
        except Exception as exc:
            raise _NavigationObservationUnreadable from exc

        if self._navigation_remaining_ms(deadline) <= 0:
            raise _NavigationDeadlineExhausted
        try:
            count = self._navigation_capped_count(control.count())
        except (_NavigationDeadlineExhausted, _NavigationObservationUnreadable):
            raise
        except Exception as exc:
            if self._navigation_remaining_ms(deadline) <= 0:
                raise _NavigationDeadlineExhausted from exc
            raise _NavigationObservationUnreadable from exc

        # The count is only useful if the same bound surface remains fresh
        # immediately after that one observation. A terminal result here must
        # prevent both attribute access and all later control probes.
        self._navigation_freshness(
            page, expected_origin, deadline, post_ems=True
        )
        if count != 1:
            return False

        try:
            control = self._eb_bill_locator(page)
        except Exception as exc:
            raise _NavigationObservationUnreadable from exc
        timeout_ms = self._navigation_freshness(
            page, expected_origin, deadline, post_ems=True
        )
        try:
            target = control.get_attribute("href", timeout=timeout_ms)
        except Exception as exc:
            if self._navigation_remaining_ms(deadline) <= 0:
                raise _NavigationDeadlineExhausted from exc
            raise _NavigationObservationUnreadable from exc

        # This is the final page/frame/origin guard for route evidence. The
        # address values remain local and never enter the public document.
        self._navigation_freshness(
            page, expected_origin, deadline, post_ems=True
        )
        current = self._current_url(page)
        if not target or not current:
            return False
        try:
            resolved = urlparse(urljoin(str(current), str(target)))
            here = urlparse(str(current))
        except Exception:
            return False
        if not resolved.scheme or resolved.scheme != here.scheme:
            return False
        if not resolved.netloc or resolved.netloc != here.netloc:
            return False
        return self._route_path(resolved.path) == self._route_path(here.path)

    def _observe_navigation_post_ems(
        self,
        page: Any,
        pre_url: str,
        expected_origin: tuple[str, str, int | None],
        start: float,
        deadline: float,
    ) -> tuple[str, dict[str, Any]]:
        """Observe one fixed surface at the committed post-EMS checkpoints."""

        post = unobserved_navigation_post_ems()
        for checkpoint_ms in NAVIGATION_DIAGNOSTIC_CHECKPOINTS_MS:
            if not self._navigation_wait_to_checkpoint(
                page, start, deadline, checkpoint_ms
            ):
                return NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED, post
            try:
                self._navigation_freshness(
                    page, expected_origin, deadline, post_ems=True
                )
            except _NavigationDeadlineExhausted:
                return NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED, post
            except _NavigationFreshnessChanged as exc:
                post["context_pages"] = exc.page_count
                post["bound_page_frames"] = exc.frame_count
                return exc.result, post
            current_url = self._current_url(page)
            if current_url is None:
                return NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE, unobserved_navigation_post_ems()
            route_changed = current_url != pre_url
            same_origin = self._navigation_same_origin(pre_url, current_url)
            post["route_changed"] = route_changed
            post["same_origin"] = same_origin
            if same_origin is None:
                return NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE, unobserved_navigation_post_ems()
            if same_origin is False:
                return NAVIGATION_DIAGNOSTIC_POST_EMS_CROSS_ORIGIN, post
            remaining_ms = self._navigation_remaining_ms(deadline)
            if remaining_ms <= 0:
                return NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED, post
            try:
                route_proven = self._navigation_diagnostic_eb_bill_route_proven(
                    page, expected_origin, deadline
                )
            except _NavigationDeadlineExhausted:
                return NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED, post
            except _NavigationFreshnessChanged as exc:
                post["context_pages"] = exc.page_count
                post["bound_page_frames"] = exc.frame_count
                return exc.result, post
            except _NavigationObservationUnreadable:
                return NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE, unobserved_navigation_post_ems()
            except Exception:
                return NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE, unobserved_navigation_post_ems()
            post["eb_bill_route_proven"] = route_proven
            if route_proven:
                return NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_ROUTE_PROVEN, post
            controls = post["controls"]
            try:
                for role, name, key in NAVIGATION_DIAGNOSTIC_CONTROL_SPECS:
                    observation = self._observe_navigation_control(
                        page, role, name, deadline, expected_origin
                    )
                    controls[key] = observation
                    if self._navigation_control_ready(observation):
                        self._navigation_freshness(
                            page, expected_origin, deadline, post_ems=True
                        )
                        result_by_key = {
                            "link_billing_manager": NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_LINK_READY,
                            "button_billing_manager": NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_BUTTON_READY,
                            "link_eb_bill": NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_LINK_READY,
                            "button_eb_bill": NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_BUTTON_READY,
                        }
                        if key in result_by_key:
                            return result_by_key[key], post
            except _NavigationDeadlineExhausted:
                return NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED, post
            except _NavigationFreshnessChanged as exc:
                post["context_pages"] = exc.page_count
                post["bound_page_frames"] = exc.frame_count
                return exc.result, post
            except _NavigationObservationUnreadable:
                return NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE, unobserved_navigation_post_ems()
        return NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED, post

    # ---- inventory ---- #
    #
    # DL-XB-199 download-first production path (G2-076 as adjudicated by Web).
    # The live portal serves one surface: the authenticated landing carries an
    # exact `tab "EB Bill"`, a visible account witness, an exact `button
    # "Search"` and, after one Search, one role table whose invoice rows each
    # carry exactly one Download button. Nothing on this path touches EMS,
    # Billing Manager, a link, a tenant selector, a test id, a filename
    # attribute or an address. The separate navigation diagnostic above keeps
    # its own historical helpers and is never called from here.

    def inventory(self, safety_ceiling: int) -> list[InvoiceRow]:
        """Bind the settled results table once and return opaque row handles.

        One portal instance owns exactly one inventory episode. The episode is
        consumed before anything is inspected, so a failed inventory cannot be
        retried on the same instance and a second call never re-dispatches
        Search. The handles carry an ordinal and an opaque binding only: no
        filename, no row text and no digest ever leaves this class.
        """

        if self._inventory_taken:
            raise LayoutChangedError(RESULTS_INVENTORY_CONSUMED_MESSAGE)
        self._inventory_taken = True
        page = self._require_page()
        self._require_unique_page(page, RESULTS_TOPOLOGY_MESSAGE)
        self._select_eb_bill_tab(page)
        self._await_account_witness(page)
        self._dispatch_search(page)
        frozen = self._await_settled_results(page, safety_ceiling)
        # Inspection-only re-proof on the settled surface: nothing is clicked,
        # and an unreadable answer fails closed like a wrong one.
        self._require_unique_page(page, RESULTS_TOPOLOGY_MESSAGE)
        try:
            selected = self._eb_bill_tab_state(page, MAX_PORTAL_PROBE_TIMEOUT_MS) == _TAB_SELECTED
        except Exception as exc:
            raise LayoutChangedError(EB_BILL_TAB_UNPROVED_MESSAGE) from exc
        if not selected:
            raise LayoutChangedError(EB_BILL_TAB_UNPROVED_MESSAGE)
        try:
            witnessed = self._account_witness_count(page) == 1
        except Exception as exc:
            raise LayoutChangedError(ACCOUNT_WITNESS_UNPROVED_MESSAGE) from exc
        if not witnessed:
            raise LayoutChangedError(ACCOUNT_WITNESS_UNPROVED_MESSAGE)
        binding = object()
        self._inventory_binding = binding
        self._frozen_rows = frozen
        self._safety_ceiling = safety_ceiling
        return [InvoiceRow(ordinal=index, binding=binding) for index in range(len(frozen) - 1)]

    def _require_unique_page(self, page: Any, message: str) -> None:
        """Require the browser context to hold exactly the one bound page."""

        try:
            pages = list(self.context.pages)
            unique = len(pages) == 1 and pages[0] is page
        except Exception as exc:
            raise LayoutChangedError(message) from exc
        if not unique:
            raise LayoutChangedError(message)

    # ---- EB Bill tab ---- #

    def _eb_bill_tab_state(self, page: Any, remaining_ms: int) -> str:
        """Read the exact EB Bill / Tenant Bill tab state. Inspection only.

        Selection is proven from `aria-selected` on the exact accessible tabs.
        EB Bill must read exactly `"true"` and Tenant Bill, when present, must
        not. Anything unreadable or contradictory is reported as such and is
        never promoted to either answer.
        """

        tab = page.get_by_role("tab", name=EB_BILL_TAB_NAME, exact=True)
        count = int(tab.count())
        if count == 0:
            return _TAB_ABSENT
        if count > 1:
            return _TAB_AMBIGUOUS
        eb_selected = self._read_attribute(tab, "aria-selected", remaining_ms)
        tenant = page.get_by_role("tab", name=TENANT_BILL_TAB_NAME, exact=True)
        tenant_count = int(tenant.count())
        if tenant_count > 1:
            return _TAB_CONTRADICTORY
        tenant_selected = (
            self._read_attribute(tenant, "aria-selected", remaining_ms)
            if tenant_count == 1
            else "false"
        )
        if eb_selected is _UNREAD or tenant_selected is _UNREAD:
            return _TAB_CONTRADICTORY
        if eb_selected == "true":
            return _TAB_SELECTED if tenant_selected != "true" else _TAB_CONTRADICTORY
        return _TAB_UNSELECTED

    def _read_attribute(self, locator: Any, name: str, remaining_ms: int) -> Any:
        """Read one attribute within the probe cap; a timeout is `_UNREAD`."""

        try:
            return locator.get_attribute(name, timeout=self._probe_timeout_ms(remaining_ms))
        except Exception as exc:
            if not self._looks_like_timeout(exc):
                raise
            return _UNREAD

    def _select_eb_bill_tab(self, page: Any) -> None:
        """Select the exact EB Bill tab with at most one click.

        One bounded, mutation-free observation settles what the tab offers. An
        already selected tab costs zero clicks. An unselected tab is clicked
        exactly once after it is proven visible, enabled and trial-actionable,
        and the selection is then proven rather than clicked again. There is no
        link, EMS, Billing Manager or generic-text fallback, and an ambiguous
        match is never narrowed.
        """

        def probe(remaining_ms: int) -> tuple[str, Any]:
            state = self._eb_bill_tab_state(page, remaining_ms)
            if state == _TAB_SELECTED:
                return _PORTAL_READY, None
            if state == _TAB_ABSENT:
                return _PORTAL_ABSENT, None
            if state == _TAB_AMBIGUOUS:
                return _PORTAL_AMBIGUOUS, None
            if state != _TAB_UNSELECTED:
                return _PORTAL_NOT_READY, None
            control = page.get_by_role("tab", name=EB_BILL_TAB_NAME, exact=True)
            if not bool(control.is_visible()):
                return _PORTAL_NOT_READY, None
            if not self._probe_enabled(control, remaining_ms):
                return _PORTAL_NOT_READY, None
            if not self._probe_actionable(control, remaining_ms):
                return _PORTAL_NOT_READY, None
            return _PORTAL_READY, control

        try:
            control = self._recover(
                page,
                probe,
                "EB Bill tab",
                messages=_uniform_messages(EB_BILL_TAB_NOT_READY_MESSAGE),
            )
        except Exception as exc:
            # Nothing has been dispatched, so the pre-dispatch message is the
            # whole truth whichever probe failed. The cause is chained only.
            raise LayoutChangedError(EB_BILL_TAB_NOT_READY_MESSAGE) from exc
        if control is None:
            return
        # The dispatch boundary: once the click begins its outcome is uncertain
        # and it is never sent again.
        try:
            control.click()
        except Exception as exc:
            raise LayoutChangedError(EB_BILL_TAB_UNCERTAIN_MESSAGE) from exc

        def selected(remaining_ms: int) -> bool:
            return self._eb_bill_tab_state(page, remaining_ms) == _TAB_SELECTED

        try:
            self._await_condition(
                page,
                selected,
                "EB Bill tab selection",
                messages=_uniform_messages(EB_BILL_TAB_UNPROVED_MESSAGE),
            )
        except Exception as exc:
            raise LayoutChangedError(EB_BILL_TAB_UNPROVED_MESSAGE) from exc

    # ---- account witness ---- #

    def _account_witness_count(self, page: Any) -> int:
        """Count visible exact configured-account text outside the results.

        The configured identity is used only as the lookup key. The page
        returns one boolean per match -- whether it sits outside table, grid,
        row and cell semantics -- so no text is read back, and an occurrence
        inside the results never counts as the account witness.
        """

        witnesses = page.get_by_text(self.config.account_identity, exact=True)
        count = int(witnesses.count())
        outside = witnesses.evaluate_all(_OUTSIDE_RESULTS_PREDICATE)
        if not isinstance(outside, list) or len(outside) != count:
            raise LayoutChangedError(ACCOUNT_WITNESS_UNPROVED_MESSAGE)
        visible = 0
        for index, flag in enumerate(outside):
            if flag is True and bool(witnesses.nth(index).is_visible()):
                visible += 1
        return visible

    def _await_account_witness(self, page: Any) -> None:
        """Settle on exactly one visible configured account witness."""

        def probe(_remaining_ms: int) -> tuple[str, Any]:
            try:
                count = self._account_witness_count(page)
            except LayoutChangedError:
                return _PORTAL_NOT_READY, None
            except Exception as exc:
                raise _ControlUnresolved(exc) from exc
            if count == 1:
                return _PORTAL_READY, None
            return (_PORTAL_AMBIGUOUS if count > 1 else _PORTAL_ABSENT), None

        self._recover(
            page,
            probe,
            "account witness",
            messages={
                _PORTAL_ABSENT: ACCOUNT_WITNESS_UNPROVED_MESSAGE,
                _PORTAL_AMBIGUOUS: ACCOUNT_WITNESS_AMBIGUOUS_MESSAGE,
                _PORTAL_NOT_READY: ACCOUNT_WITNESS_UNPROVED_MESSAGE,
                _PORTAL_UNRESOLVED: ACCOUNT_WITNESS_UNPROVED_MESSAGE,
            },
        )

    # ---- Search ---- #

    def _dispatch_search(self, page: Any) -> None:
        """Resolve the exact Search button on the bounded ladder; click once.

        Search may appear after the first readable EB Bill surface, so early
        absence is transient. The one real click is the dispatch boundary and
        is never retried: a slow result is settled for, not re-requested.
        """

        try:
            search = self._resolve_ready_control(
                page,
                lambda: page.get_by_role("button", name=SEARCH_BUTTON_NAME, exact=True),
                "Search control",
                require_trial_actionable=True,
                messages=_uniform_messages(SEARCH_NOT_READY_MESSAGE),
            )
        except Exception as exc:
            raise LayoutChangedError(SEARCH_NOT_READY_MESSAGE) from exc
        try:
            search.click()
        except Exception as exc:
            raise LayoutChangedError(SEARCH_UNCERTAIN_MESSAGE) from exc

    # ---- results table ---- #

    def _row_identity(self, row: Any, remaining_ms: int) -> bytes | None:
        """Return the private per-session digest of one row, or None if lagging.

        The accessible snapshot is normalised (NFC, whitespace collapsed,
        stripped, no casefold) and digested with this portal's own random key
        immediately, so the raw text never outlives this call.
        """

        try:
            raw = row.aria_snapshot(timeout=self._probe_timeout_ms(remaining_ms))
        except Exception as exc:
            if self._looks_like_timeout(exc):
                return None
            raise LayoutChangedError(RESULTS_ROW_IDENTITY_MESSAGE) from exc
        if not isinstance(raw, str):
            raise LayoutChangedError(RESULTS_ROW_IDENTITY_MESSAGE)
        normalised = _normalise_surface_text(raw)
        if len(normalised) > RESULTS_ROW_TEXT_MAX_CHARS:
            raise LayoutChangedError(RESULTS_ROW_IDENTITY_MESSAGE)
        if not normalised:
            return None
        return hmac.new(self._row_identity_key, normalised.encode("utf-8"), hashlib.sha256).digest()

    def _pagination_sentinel_present(self, page: Any) -> bool:
        """Report any exact pagination/completeness control. Never clicked."""

        for role in ("button", "link"):
            for name in RESULTS_PAGINATION_SENTINEL_NAMES:
                if int(page.get_by_role(role, name=name, exact=True).count()) != 0:
                    return True
        return False

    def _read_results_surface(
        self, page: Any, remaining_ms: int, safety_ceiling: int
    ) -> tuple[str, Any]:
        """Read the whole results surface once. Inspection only.

        Returns `(_PORTAL_READY, digests)` for a structurally valid surface --
        the header digest first, then one digest per invoice row in table
        order -- or `(verdict, message)` for a surface that is not (yet)
        valid. Contradictions that waiting cannot repair raise at once.
        """

        if self._pagination_sentinel_present(page):
            raise LayoutChangedError(RESULTS_PAGINATION_MESSAGE)
        tables = page.get_by_role("table")
        table_count = int(tables.count())
        if table_count != 1:
            verdict = _PORTAL_ABSENT if table_count == 0 else _PORTAL_AMBIGUOUS
            return verdict, RESULTS_UNSETTLED_MESSAGE
        rows = tables.get_by_role("row")
        row_count = int(rows.count())
        if row_count == 0:
            return _PORTAL_ABSENT, RESULTS_UNSETTLED_MESSAGE
        header = rows.nth(0)
        if int(header.get_by_role("columnheader").count()) < 1 or int(
            header.get_by_role("button", name=DOWNLOAD_BUTTON_NAME, exact=True).count()
        ) != 0:
            return _PORTAL_NOT_READY, RESULTS_UNSETTLED_MESSAGE
        invoice_count = row_count - 1
        if invoice_count == 0:
            return _PORTAL_NOT_READY, RESULTS_HEADER_ONLY_MESSAGE
        if invoice_count > safety_ceiling:
            raise LayoutChangedError(RESULTS_CEILING_MESSAGE)
        for index in range(1, row_count):
            row = rows.nth(index)
            if int(row.get_by_role("columnheader").count()) != 0 or int(
                row.get_by_role("button", name=DOWNLOAD_BUTTON_NAME, exact=True).count()
            ) != 1:
                return _PORTAL_NOT_READY, RESULTS_UNSETTLED_MESSAGE
        if int(
            page.get_by_role("button", name=DOWNLOAD_BUTTON_NAME, exact=True).count()
        ) != invoice_count:
            return _PORTAL_NOT_READY, RESULTS_UNSETTLED_MESSAGE
        declared = self._read_attribute(tables, "aria-rowcount", remaining_ms)
        if declared is _UNREAD:
            return _PORTAL_NOT_READY, RESULTS_UNSETTLED_MESSAGE
        if declared is not None:
            try:
                declared_count = int(str(declared).strip())
            except ValueError as exc:
                raise LayoutChangedError(RESULTS_ROWCOUNT_MESSAGE) from exc
            if declared_count != row_count:
                return _PORTAL_NOT_READY, RESULTS_ROWCOUNT_MESSAGE
        digests: list[bytes] = []
        for index in range(row_count):
            digest = self._row_identity(rows.nth(index), remaining_ms)
            if digest is None:
                return _PORTAL_NOT_READY, RESULTS_ROW_IDENTITY_MESSAGE
            digests.append(digest)
        if len(set(digests)) != len(digests):
            return _PORTAL_NOT_READY, RESULTS_ROW_IDENTITY_MESSAGE
        return _PORTAL_READY, tuple(digests)

    def _await_settled_results(self, page: Any, safety_ceiling: int) -> tuple[bytes, ...]:
        """Settle on two consecutive identical valid readings of the results.

        A header-only table is never an empty answer: it stays transient and,
        if it persists, fails closed as a layout change until a real empty
        state is separately evidenced. Pagination sentinels are inspection
        only, and their absence is not a proof of result completeness.
        """

        last: dict[str, Any] = {"message": RESULTS_UNSETTLED_MESSAGE, "digests": None}

        def probe(remaining_ms: int) -> tuple[str, Any]:
            try:
                verdict, value = self._read_results_surface(page, remaining_ms, safety_ceiling)
            except LayoutChangedError:
                raise
            except Exception as exc:
                raise _ControlUnresolved(exc) from exc
            if verdict != _PORTAL_READY:
                last["message"] = value
                last["digests"] = None
                return verdict, None
            if last["digests"] == value:
                return _PORTAL_READY, value
            last["digests"] = value
            last["message"] = RESULTS_UNSETTLED_MESSAGE
            return _PORTAL_NOT_READY, None

        try:
            return self._recover(page, probe, "invoice results", classified=False)
        except _PortalNotSettled as exc:
            raise LayoutChangedError(last["message"]) from exc
        except LayoutChangedError:
            raise
        except Exception as exc:
            raise LayoutChangedError(RESULTS_UNSETTLED_MESSAGE) from exc

    # ---- download ---- #

    def download(self, row: InvoiceRow, destination: Path) -> str:
        """Download one frozen row on the same settled surface; return its name.

        Before the one dispatch the whole frozen surface is re-proven: page
        topology, the selected EB Bill tab, the account witness, pagination
        absence and the exact ordered row snapshot. Any drift latches the
        portal so no later Download is ever dispatched. The browser's
        suggested filename is returned only after a successful save and is the
        only authoritative invoice name.

        A positively observed browser failure or save failure is retryable by
        the caller within its existing bound. An uncertain dispatch -- a click
        that raised, no download event in time, a new page, or a lost tab
        selection -- latches and is never retried.
        """

        page = self._require_page()
        if self._latched:
            raise LayoutChangedError(RESULTS_LATCHED_MESSAGE)
        frozen = self._frozen_rows
        if (
            not isinstance(row, InvoiceRow)
            or frozen is None
            or self._inventory_binding is None
            or row.binding is not self._inventory_binding
            or isinstance(row.ordinal, bool)
            or not isinstance(row.ordinal, int)
            or not 0 <= row.ordinal < len(frozen) - 1
        ):
            raise LayoutChangedError(RESULTS_ROW_HANDLE_MESSAGE)
        try:
            control = self._reprove_frozen_surface(page, frozen, row.ordinal)
            rows = page.get_by_role("table").get_by_role("row")
            if self._row_identity(rows.nth(row.ordinal + 1), MAX_PORTAL_PROBE_TIMEOUT_MS) != frozen[
                row.ordinal + 1
            ]:
                raise LayoutChangedError(RESULTS_SURFACE_CHANGED_MESSAGE)
        except Exception as exc:
            self._latched = True
            raise LayoutChangedError(RESULTS_SURFACE_CHANGED_MESSAGE) from exc

        # The dispatch boundary. From here an exception cannot prove whether
        # the browser acted, so it is uncertain, latched and never retried.
        try:
            with page.expect_download(timeout=self.config.timeout_seconds * 1000) as download_info:
                control.click(timeout=MAX_PORTAL_PROBE_TIMEOUT_MS)
            download = download_info.value
        except Exception as exc:
            self._latched = True
            raise _uncertain_download() from exc

        try:
            failure = download.failure()
        except Exception as exc:
            self._latched = True
            raise _uncertain_download() from exc
        if failure:
            self._require_post_download_surface(page)
            raise DownloadError("browser download failed")
        suggested_filename = getattr(download, "suggested_filename", None)
        if not isinstance(suggested_filename, str) or not suggested_filename:
            self._latched = True
            raise DownloadError("browser did not provide a suggested filename", retryable=False)
        previous = self._row_filenames.get(row.ordinal)
        if previous is not None and previous != suggested_filename:
            self._latched = True
            raise LayoutChangedError(RESULTS_FILENAME_CHANGED_MESSAGE)
        self._row_filenames[row.ordinal] = suggested_filename
        try:
            download.save_as(destination)
        except Exception as exc:
            self._require_post_download_surface(page)
            raise DownloadError("browser download could not be saved") from exc
        self._require_post_download_surface(page)
        return suggested_filename

    def _require_post_download_surface(self, page: Any) -> None:
        """After a download event, the page and the EB Bill selection must hold."""

        try:
            self._require_unique_page(page, RESULTS_TOPOLOGY_MESSAGE)
            selected = self._eb_bill_tab_state(page, MAX_PORTAL_PROBE_TIMEOUT_MS) == _TAB_SELECTED
        except Exception as exc:
            self._latched = True
            raise _uncertain_download() from exc
        if not selected:
            self._latched = True
            raise _uncertain_download()

    def _reprove_frozen_surface(self, page: Any, frozen: tuple[bytes, ...], ordinal: int) -> Any:
        """Re-prove the frozen surface and return the row's ready Download control.

        Rendering lag -- a table or control momentarily absent or not yet
        actionable -- gets the shared bounded window. A readable surface that
        differs from the frozen one in any way is drift and fails at once.
        """

        ceiling = max(self._safety_ceiling, len(frozen) - 1)

        def probe(remaining_ms: int) -> tuple[str, Any]:
            self._require_unique_page(page, RESULTS_SURFACE_CHANGED_MESSAGE)
            state = self._eb_bill_tab_state(page, remaining_ms)
            if state == _TAB_UNSELECTED or state == _TAB_AMBIGUOUS:
                raise LayoutChangedError(RESULTS_SURFACE_CHANGED_MESSAGE)
            if state != _TAB_SELECTED:
                return _PORTAL_NOT_READY, None
            witnesses = self._account_witness_count(page)
            if witnesses > 1:
                raise LayoutChangedError(RESULTS_SURFACE_CHANGED_MESSAGE)
            if witnesses != 1:
                return _PORTAL_NOT_READY, None
            verdict, value = self._read_results_surface(page, remaining_ms, ceiling)
            if verdict != _PORTAL_READY:
                return _PORTAL_NOT_READY, None
            if value != frozen:
                raise LayoutChangedError(RESULTS_SURFACE_CHANGED_MESSAGE)
            control = (
                page.get_by_role("table")
                .get_by_role("row")
                .nth(ordinal + 1)
                .get_by_role("button", name=DOWNLOAD_BUTTON_NAME, exact=True)
            )
            if int(control.count()) != 1 or not bool(control.is_visible()):
                return _PORTAL_NOT_READY, None
            if not self._probe_enabled(control, remaining_ms):
                return _PORTAL_NOT_READY, None
            if not self._probe_actionable(control, remaining_ms):
                return _PORTAL_NOT_READY, None
            return _PORTAL_READY, control

        return self._recover(
            page,
            probe,
            "frozen invoice results",
            messages=_uniform_messages(RESULTS_SURFACE_CHANGED_MESSAGE),
        )

    # ---- diagnostic-owned route helpers ---- #
    #
    # Used only by the separate navigation diagnostic above. The production
    # path never calls them.

    @staticmethod
    def _eb_bill_locator(page: Any) -> Any:
        """Resolve the one exact EB Bill control. Never narrowed, never weakened."""

        return page.get_by_role("link", name=EB_BILL_NAV_NAME, exact=True)

    @staticmethod
    def _route_path(path: str) -> str:
        """Normalise one route path for comparison, without exposing it."""

        return path.rstrip("/") or "/"

    def _require_page(self) -> Any:
        if self.page is None:
            raise DependencyError("browser page is not open")
        return self.page

    @staticmethod
    def _looks_like_timeout(error: Exception) -> bool:
        return "timeout" in str(error).casefold() or error.__class__.__name__.endswith("TimeoutError")
