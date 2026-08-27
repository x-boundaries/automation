from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from energygrid_bill_downloader import cli
from energygrid_bill_downloader import portal as portal_module
from energygrid_bill_downloader.config import load_runtime_config
from energygrid_bill_downloader.cli import main
from energygrid_bill_downloader.errors import (
    PORTAL_LAYOUT_CHANGED,
    AppError,
    DownloadError,
    InvalidPdfError,
    LayoutChangedError,
    LoginError,
)
from energygrid_bill_downloader.portal import PlaywrightPortal
from energygrid_bill_downloader.publication import validate_pdf
from tests.fixtures.synthetic_portal import SyntheticBill, SyntheticPortalServer, write_config

try:
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised when optional test dependency is absent
    sync_playwright = None


def runtime_credentials() -> dict[str, str]:
    return {
        "ENERGYGRID_USERNAME": "synthetic-" + uuid.uuid4().hex,
        "ENERGYGRID_PASSWORD": "synthetic-" + uuid.uuid4().hex,
    }


@unittest.skipUnless(sync_playwright is not None, "Playwright Python package is not installed")
class SyntheticPortalTests(unittest.TestCase):
    def config_for(self, server: SyntheticPortalServer, root: Path):
        raw = {
            "portal_url": server.base_url,
            "archive_root": str(root / "archive"),
            "account_identity": "SYNTHETIC-INTENDED-ACCOUNT",
            "state_path": str(root / "state" / "state.sqlite3"),
            "temp_root": str(root / "temp"),
            "log_root": str(root / "logs"),
            "timeout_seconds": 5,
            "inventory_safety_ceiling": 20,
        }
        config = load_runtime_config(raw, checkout_root=Path.cwd())
        config.archive_root.mkdir()
        config.preflight()
        return config

    def with_credentials(self):
        old = {name: os.environ.get(name) for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")}
        values = runtime_credentials()
        os.environ.update(values)
        return old, values

    @staticmethod
    def restore_credentials(old: dict[str, str | None]) -> None:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_login_inventory_pagination_and_download_are_synthetic(self) -> None:
        bills = [
            SyntheticBill("2026-05-01_account_a.pdf"),
            SyntheticBill("2026-06-01_account_b.pdf"),
            SyntheticBill("2026-07-01_account_c.pdf"),
        ]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills, page_size=2) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    inventory = portal.inventory(20)
                    self.assertEqual([item.filename for item in inventory], [item.filename for item in bills])
                    target = root / "download.bin"
                    suggested = portal.download(inventory[0], target)
                    self.assertEqual(suggested, bills[0].filename)
                    self.assertEqual(target.read_bytes(), bills[0].payload)
            finally:
                self.restore_credentials(old)

    def test_empty_inventory_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer([]) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    self.assertEqual(portal.inventory(20), [])
            finally:
                self.restore_credentials(old)

    def test_login_failure_is_distinct_from_selector_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(login_success=False) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    with self.assertRaises(LoginError):
                        portal.login()
            finally:
                self.restore_credentials(old)

        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(variant="missing_login") as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    with self.assertRaises(LayoutChangedError):
                        portal.login()
            finally:
                self.restore_credentials(old)

    def test_public_page_is_semantics_gated_and_exposes_an_exact_login_button(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer() as server:
            root = Path(directory)
            config = self.config_for(server, root)
            with PlaywrightPortal(config) as portal:
                page = portal.page
                page.goto(config.portal_url, wait_until="domcontentloaded")

                # Pre-activation the only exposed control is the semantics gate.
                self.assertEqual(
                    page.get_by_role("button", name="Enable accessibility", exact=True).count(), 1
                )
                self.assertEqual(page.locator("flt-semantics-placeholder").count(), 1)
                self.assertEqual(page.get_by_role("button", name="Login", exact=True).count(), 0)

                login_entry = portal._enter_public_semantics(page)

                # Exactly one dispatch, placeholder gone, Login is a button.
                self.assertEqual(server.activation_count, 1)
                self.assertEqual(page.locator("flt-semantics-placeholder").count(), 0)
                self.assertEqual(login_entry.count(), 1)
                self.assertTrue(login_entry.is_visible())
                self.assertTrue(login_entry.is_enabled())
                # The superseded pre-auth link contract must not reappear.
                self.assertEqual(page.get_by_role("link", name="Login", exact=True).count(), 0)

    def test_activation_affordance_drift_fails_closed_before_dispatch(self) -> None:
        for variant in (
            "missing_activation",
            "ambiguous_activation",
            "hidden_activation",
            "disabled_activation",
        ):
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
                    variant=variant
                ) as server:
                    root = Path(directory)
                    config = self.config_for(server, root)
                    old, _values = self.with_credentials()
                    try:
                        with PlaywrightPortal(config) as portal:
                            with self.assertRaises(LayoutChangedError):
                                portal.login()
                    finally:
                        self.restore_credentials(old)
                    # Fail closed means the gate was never dispatched at all.
                    self.assertEqual(server.activation_count, 0)

    def test_post_activation_contract_drift_fails_closed_after_one_dispatch(self) -> None:
        for variant in (
            "placeholder_persists",
            "missing_login",
            "ambiguous_login",
            "hidden_login",
            "disabled_login",
            "wrong_role_login",
        ):
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
                    variant=variant
                ) as server:
                    root = Path(directory)
                    config = self.config_for(server, root)
                    old, _values = self.with_credentials()
                    try:
                        with PlaywrightPortal(config) as portal:
                            with self.assertRaises(LayoutChangedError):
                                portal.login()
                    finally:
                        self.restore_credentials(old)
                    # Activation is attempted at most once per login attempt.
                    self.assertEqual(server.activation_count, 1)

    def test_ui_drift_and_ambiguous_controls_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(variant="missing_invoice_list") as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    with self.assertRaises(LayoutChangedError):
                        portal.inventory(20)
            finally:
                self.restore_credentials(old)

        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [SyntheticBill("2026-05-01_account_a.pdf")], variant="ambiguous_download"
        ) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    with self.assertRaises(LayoutChangedError):
                        portal.inventory(20)
            finally:
                self.restore_credentials(old)


    def test_download_payload_validation_and_page_ceiling(self) -> None:
        for mode in ("error", "html", "zero", "truncated"):
            bill = SyntheticBill("2026-05-07_account_ref.pdf", mode=mode)
            with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer([bill]) as server:
                root = Path(directory)
                config = self.config_for(server, root)
                old, _values = self.with_credentials()
                try:
                    with PlaywrightPortal(config) as portal:
                        portal.login()
                        reference = portal.inventory(20)[0]
                        target = root / "download.bin"
                        if mode == "error":
                            with self.assertRaises(DownloadError):
                                portal.download(reference, target)
                        else:
                            portal.download(reference, target)
                            with self.assertRaises(InvalidPdfError):
                                validate_pdf(target)
                finally:
                    self.restore_credentials(old)

        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [SyntheticBill("2026-05-08_account_ref.pdf")], next_loop=True
        ) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    with self.assertRaises(LayoutChangedError):
                        portal.inventory(2)
            finally:
                self.restore_credentials(old)

    def test_browser_suggested_filename_mismatch_fails_closed(self) -> None:
        bill = SyntheticBill(
            "2026-05-18_account_identity.pdf",
            suggested_filename="2026-05-18_other_identity.pdf",
        )
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer([bill]) as server:
            root = Path(directory)
            (root / "archive").mkdir()
            config_path = root / "config.json"
            write_config(config_path, server, root)
            old, _values = self.with_credentials()
            try:
                result = main(["run", "--config", str(config_path)])
                self.assertEqual(result, 20)
                self.assertFalse((root / "archive" / bill.filename).exists())
                self.assertEqual(server.download_counts[bill.filename], 1)
            finally:
                self.restore_credentials(old)

    def test_cli_run_logs_in_and_reconciles_synthetic_bill(self) -> None:
        bill = SyntheticBill("2026-05-09_account_ref.pdf")
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer([bill]) as server:
            root = Path(directory)
            (root / "archive").mkdir()
            config_path = root / "config.json"
            write_config(config_path, server, root)
            old, _values = self.with_credentials()
            try:
                first = main(["run", "--config", str(config_path)])
                self.assertEqual(first, 0)
                second = main(["run", "--config", str(config_path)])
                self.assertEqual(second, 0)
                self.assertTrue((root / "archive" / bill.filename).exists())
            finally:
                self.restore_credentials(old)

    def test_stable_url_client_side_pagination_restores_later_page_download(self) -> None:
        bills = [
            SyntheticBill("2026-05-01_SYNTHETIC-A.pdf"),
            SyntheticBill("2026-06-01_SYNTHETIC-B.pdf"),
            SyntheticBill("2026-07-01_SYNTHETIC-C.pdf"),
        ]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            bills, page_size=2, client_side_pagination=True
        ) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    inventory = portal.inventory(20)
                    self.assertEqual([bill.filename for bill in inventory], [bill.filename for bill in bills])
                    self.assertEqual(len({bill.page_url for bill in inventory}), 1)
                    target = root / "later-page.bin"
                    suggested = portal.download(inventory[-1], target)
                    self.assertEqual(suggested, bills[-1].filename)
                    self.assertEqual(target.read_bytes(), bills[-1].payload)
            finally:
                self.restore_credentials(old)

    def test_url_addressable_later_page_download_remains_supported(self) -> None:
        bills = [
            SyntheticBill("2026-08-01_SYNTHETIC-A.pdf"),
            SyntheticBill("2026-08-02_SYNTHETIC-B.pdf"),
            SyntheticBill("2026-08-03_SYNTHETIC-C.pdf"),
        ]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills, page_size=2) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    inventory = portal.inventory(20)
                    self.assertIn("page=2", inventory[-1].page_url)
                    target = root / "url-page.bin"
                    self.assertEqual(portal.download(inventory[-1], target), bills[-1].filename)
            finally:
                self.restore_credentials(old)

    def test_wrong_default_account_is_not_authoritative(self) -> None:
        default = "SYNTHETIC-DEFAULT-ACCOUNT"
        intended = "SYNTHETIC-INTENDED-ACCOUNT"
        wrong = SyntheticBill("2026-09-01_SYNTHETIC-WRONG.pdf")
        right = SyntheticBill("2026-09-02_SYNTHETIC-RIGHT.pdf")
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [wrong],
            account_options=[default, intended],
            default_account=default,
            account_bills={default: [wrong], intended: [right]},
        ) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    inventory = portal.inventory(20)
                    self.assertEqual([bill.filename for bill in inventory], [right.filename])
                    target = root / "intended.bin"
                    portal.download(inventory[0], target)
                    self.assertEqual(server.download_counts.get(wrong.filename, 0), 0)
                    self.assertEqual(target.read_bytes(), right.payload)
            finally:
                self.restore_credentials(old)

    def test_intended_account_absent_fails_closed(self) -> None:
        default = "SYNTHETIC-DEFAULT-ACCOUNT"
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [SyntheticBill("2026-09-03_SYNTHETIC-WRONG.pdf")],
            account_options=[default],
            default_account=default,
        ) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    with self.assertRaises(LayoutChangedError):
                        portal.inventory(20)
            finally:
                self.restore_credentials(old)

    def test_ambiguous_intended_account_fails_closed(self) -> None:
        intended = "SYNTHETIC-INTENDED-ACCOUNT"
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [SyntheticBill("2026-09-04_SYNTHETIC-AMBIGUOUS.pdf")],
            account_options=[intended, intended],
            default_account=intended,
        ) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    with self.assertRaises(LayoutChangedError):
                        portal.inventory(20)
            finally:
                self.restore_credentials(old)

    def test_selected_displayed_account_mismatch_fails_closed(self) -> None:
        bill = SyntheticBill("2026-09-05_SYNTHETIC-MISMATCH.pdf")
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [bill], variant="account_mismatch"
        ) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    with self.assertRaises(LayoutChangedError):
                        portal.inventory(20)
            finally:
                self.restore_credentials(old)

    def test_pre_search_blank_state_requires_explicit_search(self) -> None:
        bill = SyntheticBill("2026-09-06_SYNTHETIC-PRESEARCH.pdf")
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer([bill]) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    inventory = portal.inventory(20)
                    self.assertEqual([item.filename for item in inventory], [bill.filename])
            finally:
                self.restore_credentials(old)

    def test_verified_post_search_empty_state_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer([]) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    self.assertEqual(portal.inventory(20), [])
                    self.assertEqual(server.search_count, 1)
            finally:
                self.restore_credentials(old)


# ---- DL-XB-141-OBS-001: pre-auth failure localisation ---- #
#
# portal.py deliberately raises generic messages, so the CLI needs a mapping to
# turn them into support references. These cases drive the COMMITTED portal
# branches with a minimal locator double and feed whatever message each branch
# actually raises into the COMMITTED mapping. The wording therefore stays owned
# by portal.py: reword a branch there and the coverage case fails rather than
# the failure silently degrading to the generic reference. No browser, no
# server, and no credentials are needed to prove this.


class FakeLocator:
    """The slice of the Playwright locator surface the login path touches.

    The `*_error` hooks let one login step fail generically while every other
    step stays healthy, which is what proves each step carries its own
    reference. `journal` records interactions in the order they happen, so the
    successful path can be asserted as a sequence and not only as counts.
    """

    def __init__(
        self,
        *,
        count: int = 1,
        visible: bool = True,
        enabled: bool = True,
        wait_error: Exception | None = None,
        state_error: Exception | None = None,
        dispatch_error: Exception | None = None,
        click_error: Exception | None = None,
        trial_click_error: Exception | None = None,
        fill_error: Exception | None = None,
        journal: list[str] | None = None,
        label: str = "locator",
    ) -> None:
        self._count = count
        self._visible = visible
        self._enabled = enabled
        self._wait_error = wait_error
        self._state_error = state_error
        self._dispatch_error = dispatch_error
        self._click_error = click_error
        self._trial_click_error = trial_click_error
        self._fill_error = fill_error
        self._journal = journal
        self._label = label
        self.dispatched = 0
        self.clicks = 0
        self.trial_clicks = 0
        self.fills = 0
        self.waits = 0

    def _record(self, action: str) -> None:
        if self._journal is not None:
            self._journal.append(self._label + ":" + action)

    @property
    def first(self) -> "FakeLocator":
        return self

    def wait_for(self, state: str | None = None) -> None:
        self.waits += 1
        self._record("wait_for")
        if self._wait_error is not None:
            raise self._wait_error

    def count(self) -> int:
        if self._state_error is not None:
            raise self._state_error
        return self._count

    def is_visible(self, timeout: int | None = None) -> bool:
        return self._visible

    def is_enabled(self) -> bool:
        return self._enabled

    def dispatch_event(self, name: str) -> None:
        self.dispatched += 1
        self._record("dispatch_event")
        if self._dispatch_error is not None:
            raise self._dispatch_error

    def click(self, trial: bool = False) -> None:
        """Only a normal click submits; a trial click proves actionability.

        The two failure hooks are separate so a transient actionability
        timeout can be modelled without also breaking the one real submit,
        and so a case that breaks the real submit is not intercepted by the
        trial that precedes it.
        """
        if trial:
            self.trial_clicks += 1
            self._record("click_trial")
            if self._trial_click_error is not None:
                raise self._trial_click_error
            return
        self.clicks += 1
        self._record("click")
        if self._click_error is not None:
            raise self._click_error

    def fill(self, value: str) -> None:
        self.fills += 1
        self._record("fill")
        if self._fill_error is not None:
            raise self._fill_error


class FakePage:
    """A page whose only job is to hand the login path the locators under test.

    The portal resolves the semantics-gate Login entry and the login submit
    control through the same role and name, so this page hands the first such
    lookup to `login_entry` and any later one to `submit`. That mirrors the
    committed sequence while still letting a click failure be attributed to the
    step that made it. Every optional slot defaults to a healthy locator.
    """

    def __init__(
        self,
        activation: FakeLocator,
        placeholder: FakeLocator,
        login_entry: FakeLocator,
        *,
        label_error: Exception | None = None,
        alert_visible: bool = False,
        goto_error: Exception | None = None,
        submit: FakeLocator | None = None,
        submits: list[FakeLocator] | None = None,
        username_field: FakeLocator | None = None,
        password_field: FakeLocator | None = None,
        billing_manager: FakeLocator | None = None,
        journal: list[str] | None = None,
    ) -> None:
        self.activation = activation
        self.placeholder = placeholder
        self.login_entry = login_entry
        self.label_error = label_error
        self.alert_visible = alert_visible
        self.goto_error = goto_error
        self.submit = submit
        # Successive submit-stage resolutions, so a re-resolving caller can be
        # handed a different locator each time. The last entry repeats once the
        # sequence is exhausted, which models a control that stays as it is.
        self.submits = submits
        self.username_field = username_field
        self.password_field = password_field
        self.billing_manager = billing_manager
        self.journal = journal
        self.goto_calls = 0
        self.login_lookups = 0
        self.alert_lookups = 0
        self.waited_ms: list[int] = []

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.goto_calls += 1
        if self.journal is not None:
            self.journal.append("page:goto")
        if self.goto_error is not None:
            raise self.goto_error

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        if name == "Enable accessibility":
            return self.activation
        if name == "Login":
            self.login_lookups += 1
            if self.login_lookups > 1 and self.submits is not None:
                return self.submits[min(self.login_lookups - 2, len(self.submits) - 1)]
            if self.login_lookups > 1 and self.submit is not None:
                return self.submit
            return self.login_entry
        if role == "alert":
            self.alert_lookups += 1
            return FakeLocator(visible=self.alert_visible)
        if name == "Billing Manager":
            return self.billing_manager if self.billing_manager is not None else FakeLocator()
        raise AssertionError(f"unexpected role lookup: {role}/{name}")

    def wait_for_timeout(self, milliseconds: int) -> None:
        """Record a browser-event-loop yield instead of spending the time."""
        self.waited_ms.append(int(milliseconds))
        if self.journal is not None:
            self.journal.append("page:wait_for_timeout")

    def simulated_monotonic(self) -> float:
        """A clock that advances only by the yields this page was asked for.

        Patched over `portal.time.monotonic`, this makes a deadline-bounded
        loop terminate deterministically and lets a test assert the simulated
        elapsed recovery without any real waiting.
        """
        return sum(self.waited_ms) / 1000.0

    def locator(self, selector: str):
        assert selector == "flt-semantics-placeholder", selector
        return self.placeholder

    def get_by_label(self, name: str, exact: bool = False):
        if self.label_error is not None:
            raise self.label_error
        if name == "Username":
            return self.username_field if self.username_field is not None else FakeLocator()
        if name == "Password":
            return self.password_field if self.password_field is not None else FakeLocator()
        raise AssertionError(f"unexpected label lookup: {name}")


class FakeConfig:
    """Only the field the login path reads; the URL is never fetched."""

    portal_url = "http://127.0.0.1:1/synthetic"
    timeout_seconds = 5


# (case id, locator keyword arguments, expected support reference)
UNREADY_CONTROL_CASES = (
    ("not_appear", {"wait_error": RuntimeError("locator never attached")}, "NOT_APPEAR"),
    ("unresolved", {"state_error": RuntimeError("strict mode violation")}, "UNRESOLVED"),
    ("ambiguous", {"count": 2}, "AMBIGUOUS"),
    ("hidden", {"visible": False}, "NOT_READY"),
    ("disabled", {"enabled": False}, "NOT_READY"),
)

# Deliberately carries a credential-shaped token and a URL, so any case that
# lets raw exception text reach a reference or a message fails loudly.
STEP_FAILURE_TEXT = "synthetic step failure: password=hunter2 at https://portal.example.invalid/x"


def step_failure() -> RuntimeError:
    """A fresh generic exception, of the kind only the broad arm can classify."""
    return RuntimeError(STEP_FAILURE_TEXT)


def login_page(**overrides) -> FakePage:
    """A page whose login path succeeds unless `overrides` break exactly one step."""
    slots = {"activation": FakeLocator(), "placeholder": FakeLocator(), "login_entry": FakeLocator()}
    for name in tuple(slots):
        if name in overrides:
            slots[name] = overrides.pop(name)
    return FakePage(slots["activation"], slots["placeholder"], slots["login_entry"], **overrides)


# Every operation the broad generic arm of `login()` can currently cover, with
# the one page override that breaks it and the reference it must now report.
# (case id, override builder, expected support reference)
LOGIN_STEP_CASES = (
    (
        "portal_navigation",
        lambda exc: {"goto_error": exc},
        "EG_LOGIN_NAVIGATION_FAILED",
    ),
    (
        "semantics_activation_dispatch",
        lambda exc: {"activation": FakeLocator(dispatch_error=exc)},
        "EG_LOGIN_SEMANTICS_ACTIVATION_DISPATCH_FAILED",
    ),
    (
        "post_activation_entry_click",
        lambda exc: {"login_entry": FakeLocator(click_error=exc)},
        "EG_LOGIN_ENTRY_CLICK_FAILED",
    ),
    (
        "username_fill",
        lambda exc: {"username_field": FakeLocator(fill_error=exc)},
        "EG_LOGIN_USERNAME_FILL_FAILED",
    ),
    (
        "password_fill",
        lambda exc: {"password_field": FakeLocator(fill_error=exc)},
        "EG_LOGIN_PASSWORD_FILL_FAILED",
    ),
    (
        "login_submit",
        lambda exc: {"submit": FakeLocator(click_error=exc)},
        "EG_LOGIN_SUBMIT_FAILED",
    ),
    (
        "billing_manager_wait",
        lambda exc: {"billing_manager": FakeLocator(wait_error=exc)},
        "EG_LOGIN_BILLING_MANAGER_WAIT_FAILED",
    ),
)

# The committed order of portal interactions for one successful login attempt.
# A healthy submit control is proven actionable by one trial click and is then
# clicked once for real; it costs no extra resolution and no event-loop yield,
# so an unlagged portal keeps the sequence it always had plus that one proof.
SUCCESSFUL_LOGIN_SEQUENCE = [
    "page:goto",
    "activation:wait_for",
    "activation:dispatch_event",
    "placeholder:wait_for",
    "login_entry:wait_for",
    "login_entry:click",
    "username:fill",
    "password:fill",
    "submit:click_trial",
    "submit:click",
    "billing_manager:wait_for",
]


class PreAuthLoginFailureReferenceTests(unittest.TestCase):
    def portal_for(self, page: FakePage) -> PlaywrightPortal:
        portal = PlaywrightPortal(FakeConfig(), headed=False)
        portal.page = page
        return portal

    def assert_maps_non_generically(self, error: LayoutChangedError, expected_ref: str) -> str:
        """Assert the raised message classifies to `expected_ref`, and return it."""
        ref = cli.support_ref_for(error)
        self.assertEqual(ref, expected_ref, error.message)
        self.assertNotEqual(ref, cli.UNCLASSIFIED_SUPPORT_REF, error.message)
        return error.message

    def test_activation_control_failures_are_individually_referenced(self) -> None:
        seen = []
        for case_id, kwargs, outcome in UNREADY_CONTROL_CASES:
            with self.subTest(case=case_id):
                activation = FakeLocator(**kwargs)
                page = FakePage(activation, FakeLocator(), FakeLocator())
                with self.assertRaises(LayoutChangedError) as caught:
                    self.portal_for(page)._enter_public_semantics(page)
                seen.append(
                    self.assert_maps_non_generically(
                        caught.exception, f"EG_LOGIN_SEMANTICS_ACTIVATION_{outcome}"
                    )
                )
                # Fail closed still means the gate was never dispatched.
                self.assertEqual(activation.dispatched, 0)
        self.assertEqual(len(set(seen)), 4, "hidden and disabled share one reference by design")

    def test_placeholder_persisting_after_activation_is_referenced(self) -> None:
        activation = FakeLocator()
        placeholder = FakeLocator(wait_error=RuntimeError("still attached"))
        page = FakePage(activation, placeholder, FakeLocator())
        with self.assertRaises(LayoutChangedError) as caught:
            self.portal_for(page)._enter_public_semantics(page)
        self.assert_maps_non_generically(caught.exception, "EG_LOGIN_SEMANTICS_PLACEHOLDER_REMAINS")
        self.assertEqual(activation.dispatched, 1)

    def test_post_activation_login_control_failures_are_individually_referenced(self) -> None:
        seen = []
        for case_id, kwargs, outcome in UNREADY_CONTROL_CASES:
            with self.subTest(case=case_id):
                activation = FakeLocator()
                login_entry = FakeLocator(**kwargs)
                page = FakePage(activation, FakeLocator(), login_entry)
                with self.assertRaises(LayoutChangedError) as caught:
                    self.portal_for(page)._enter_public_semantics(page)
                seen.append(
                    self.assert_maps_non_generically(
                        caught.exception, f"EG_LOGIN_POST_ACTIVATION_{outcome}"
                    )
                )
                # Activation is attempted at most once per login attempt.
                self.assertEqual(activation.dispatched, 1)
        self.assertEqual(len(set(seen)), 4, "hidden and disabled share one reference by design")

    def run_login(self, page: FakePage, credentials: bool = True) -> AppError:
        """Run the committed `login()` against `page` and return what it raised."""
        old = {name: os.environ.get(name) for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")}
        if credentials:
            os.environ.update(runtime_credentials())
        else:
            for name in old:
                os.environ.pop(name, None)
        try:
            with self.assertRaises(AppError) as caught:
                self.portal_for(page).login()
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        return caught.exception

    def test_each_generic_login_step_reports_its_own_reference(self) -> None:
        """The broad arm of `login()`, one currently identified step at a time.

        Every case breaks exactly one operation with an ordinary exception, so
        the reference it produces is the only thing that can tell an operator
        where a `PORTAL_LAYOUT_CHANGED` run stopped.
        """
        seen = []
        for case_id, break_step, expected_ref in LOGIN_STEP_CASES:
            with self.subTest(case=case_id):
                error = self.run_login(login_page(**break_step(step_failure())))
                self.assertIsInstance(error, LayoutChangedError)
                seen.append(self.assert_maps_non_generically(error, expected_ref))
                # The class, status and exit semantics of this family are unchanged.
                self.assertEqual(error.status, PORTAL_LAYOUT_CHANGED)
                self.assertEqual(error.exit_code, 20)
                # The retired coarse reference is no longer reachable from here.
                self.assertNotIn(expected_ref, cli.RETIRED_SUPPORT_REFS)
                # Nothing the raised exception carried may survive into the message.
                self.assertNotIn("hunter2", error.message)
                self.assertNotIn("portal.example.invalid", error.message)
                self.assertNotIn(STEP_FAILURE_TEXT, error.message)
        self.assertEqual(len(set(seen)), len(LOGIN_STEP_CASES), "each step needs its own message")

    def test_visible_alert_outranks_every_step_reference(self) -> None:
        """A visible alert still means rejected credentials, at any broken step."""
        for case_id, break_step, layout_ref in LOGIN_STEP_CASES:
            with self.subTest(case=case_id):
                overrides = break_step(step_failure())
                overrides["alert_visible"] = True
                error = self.run_login(login_page(**overrides))
                self.assertIsInstance(error, LoginError)
                self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_PORTAL_REJECTED")
                self.assertNotEqual(cli.support_ref_for(error), layout_ref)
                self.assertNotIn("hunter2", error.message)

    def test_specific_semantics_failures_outrank_the_step_marker(self) -> None:
        """A classified semantics failure keeps its own reference and class.

        These reach `login()` through the same call the activation-dispatch
        marker covers, so they are the case that proves the marker never
        reclassifies a failure the semantics gate already named.
        """
        for case_id, kwargs, outcome in UNREADY_CONTROL_CASES:
            for slot, family in (("activation", "SEMANTICS_ACTIVATION"), ("login_entry", "POST_ACTIVATION")):
                with self.subTest(case=case_id, slot=slot):
                    # A visible alert must not reclassify a proven contract failure.
                    error = self.run_login(login_page(alert_visible=True, **{slot: FakeLocator(**kwargs)}))
                    self.assertIsInstance(error, LayoutChangedError)
                    self.assert_maps_non_generically(error, f"EG_LOGIN_{family}_{outcome}")
                    self.assertEqual(error.status, PORTAL_LAYOUT_CHANGED)

        placeholder_error = self.run_login(
            login_page(placeholder=FakeLocator(wait_error=RuntimeError("still attached")), alert_visible=True)
        )
        self.assertIsInstance(placeholder_error, LayoutChangedError)
        self.assert_maps_non_generically(placeholder_error, "EG_LOGIN_SEMANTICS_PLACEHOLDER_REMAINS")

    def test_successful_login_keeps_its_interaction_order_and_count(self) -> None:
        """The committed sequence: same interactions, same order, none added."""
        journal: list[str] = []
        locators = {
            name: FakeLocator(journal=journal, label=name)
            for name in ("activation", "placeholder", "login_entry", "submit", "username", "password", "billing_manager")
        }
        page = FakePage(
            locators["activation"],
            locators["placeholder"],
            locators["login_entry"],
            submit=locators["submit"],
            username_field=locators["username"],
            password_field=locators["password"],
            billing_manager=locators["billing_manager"],
            journal=journal,
        )
        old = {name: os.environ.get(name) for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")}
        os.environ.update(runtime_credentials())
        try:
            self.portal_for(page).login()
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertEqual(journal, SUCCESSFUL_LOGIN_SEQUENCE)
        self.assertEqual(page.goto_calls, 1)
        self.assertEqual(page.login_lookups, 2, "the entry and the submit control resolve as before")
        self.assertEqual(locators["activation"].dispatched, 1)
        self.assertEqual(locators["login_entry"].clicks, 1)
        self.assertEqual(locators["submit"].clicks, 1)
        self.assertEqual(locators["submit"].trial_clicks, 1)
        self.assertEqual(locators["username"].fills, 1)
        self.assertEqual(locators["password"].fills, 1)
        self.assertEqual(locators["billing_manager"].waits, 1)
        # A healthy control needs no recovery, so nothing is spent waiting.
        self.assertEqual(page.waited_ms, [])
        # A success never consults the alert, so it never takes the rejection arm.
        self.assertEqual(page.alert_lookups, 0)

    def test_credential_absence_is_referenced_without_touching_the_page(self) -> None:
        page = FakePage(FakeLocator(), FakeLocator(), FakeLocator())
        error = self.run_login(page, credentials=False)
        self.assertIsInstance(error, LoginError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_CREDENTIALS_UNAVAILABLE")
        self.assertEqual(page.goto_calls, 0)

    def test_no_committed_login_reference_is_unreachable_and_none_is_unmapped(self) -> None:
        """Completeness in both directions for the pre-auth login vocabulary.

        Every reference the CLI knows about is produced here by a real portal
        branch, and every message those branches raise is known to the CLI. A
        new portal message with no reference, or a reference nothing can raise,
        fails this case.
        """
        reached: set[str] = set()

        def record(error: AppError) -> None:
            self.assertIn(error.message, cli.SUPPORT_REFS_BY_MESSAGE, error.message)
            reached.add(cli.support_ref_for(error))

        for _case_id, kwargs, _outcome in UNREADY_CONTROL_CASES:
            for slot in ("activation", "login_entry"):
                locators = {
                    "activation": FakeLocator(),
                    "placeholder": FakeLocator(),
                    "login_entry": FakeLocator(),
                }
                locators[slot] = FakeLocator(**kwargs)
                page = FakePage(locators["activation"], locators["placeholder"], locators["login_entry"])
                with self.assertRaises(LayoutChangedError) as caught:
                    self.portal_for(page)._enter_public_semantics(page)
                record(caught.exception)

        placeholder_page = FakePage(
            FakeLocator(), FakeLocator(wait_error=RuntimeError("still attached")), FakeLocator()
        )
        with self.assertRaises(LayoutChangedError) as caught:
            self.portal_for(placeholder_page)._enter_public_semantics(placeholder_page)
        record(caught.exception)

        for _case_id, break_step, _expected_ref in LOGIN_STEP_CASES:
            record(self.run_login(login_page(**break_step(step_failure()))))

        record(self.run_login(login_page(goto_error=step_failure(), alert_visible=True)))
        record(self.run_login(login_page(), credentials=False))

        live = set(cli.SUPPORT_REFS_BY_MESSAGE.values()) - cli.RETIRED_SUPPORT_REFS
        self.assertEqual(
            reached,
            live,
            "the live reference vocabulary and the reachable login branches must match",
        )
        # A retired reference stays mapped so old evidence reads, and stays
        # unreachable so no step can quietly fall back onto it again.
        self.assertTrue(cli.RETIRED_SUPPORT_REFS)
        self.assertTrue(cli.RETIRED_SUPPORT_REFS.isdisjoint(reached))
        self.assertTrue(cli.RETIRED_SUPPORT_REFS.issubset(set(cli.SUPPORT_REFS_BY_MESSAGE.values())))


class SyntheticTimeoutError(Exception):
    """Named so the portal's bounded timeout classification recognises it."""


def synthetic_timeout() -> SyntheticTimeoutError:
    return SyntheticTimeoutError("Timeout 30000ms exceeded")


class LoginSubmitRecoveryTests(unittest.TestCase):
    """Bounded pre-submit recovery for the Flutter login route (#141, G3).

    Every case drives the committed `login()` rather than a helper in
    isolation, so what is proven is the production path. The clock the
    recovery reads is replaced by one that advances only by the yields the
    page was actually asked for, which makes a deadline-bounded loop
    terminate deterministically without any real waiting.
    """

    def attempt_login(self, page: FakePage) -> AppError | None:
        """Run `login()` against `page`; return what it raised, or None."""
        portal = PlaywrightPortal(FakeConfig(), headed=False)
        portal.page = page
        old = {name: os.environ.get(name) for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")}
        os.environ.update(runtime_credentials())
        try:
            # `create=True` keeps these cases failing on behaviour rather than
            # on the absence of a clock, which is what makes them meaningful
            # regressions against a revision that has no recovery loop at all.
            with mock.patch.object(portal_module, "time", create=True) as clock:
                clock.monotonic.side_effect = page.simulated_monotonic
                portal.login()
        except AppError as exc:
            return exc
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        return None

    def test_fresh_re_resolution_recovers_a_lagging_login_control(self) -> None:
        """The defect case: the control is unresolvable for a short window.

        Holding the first locator and blocking on it is what consumed the
        whole page timeout in production. Resolving a new locator after a
        yield finds the same canonical control ready.
        """
        lagging = FakeLocator(count=0, click_error=synthetic_timeout(), label="lagging")
        ready = FakeLocator(label="ready")
        page = login_page(submits=[lagging, ready])

        self.assertIsNone(self.attempt_login(page))
        self.assertGreaterEqual(page.login_lookups, 3, "the submit control must be resolved afresh")
        self.assertEqual(lagging.clicks, 0, "a control that is not ready is never clicked")
        self.assertEqual(ready.trial_clicks, 1)
        self.assertEqual(ready.clicks, 1)
        self.assertTrue(page.waited_ms, "recovery yields the browser event loop")

    def test_ambiguous_login_control_fails_immediately_without_retrying(self) -> None:
        """More than one exact Login control is drift, and drift is terminal."""
        ambiguous = FakeLocator(count=2, label="ambiguous")
        page = login_page(submits=[ambiguous])

        error = self.attempt_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.status, PORTAL_LAYOUT_CHANGED)
        self.assertEqual(page.waited_ms, [], "ambiguity is not retryable")
        self.assertEqual(ambiguous.trial_clicks, 0)
        self.assertEqual(ambiguous.clicks, 0)

    def test_recovery_is_bounded_by_a_monotonic_deadline(self) -> None:
        """A control that never becomes ready still ends inside the ceiling."""
        never = FakeLocator(count=0, label="never")
        page = login_page(submits=[never])

        error = self.attempt_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assert_submit_stage(error)
        self.assertEqual(never.clicks, 0)
        self.assertTrue(page.waited_ms)
        self.assertLessEqual(max(page.waited_ms), 30_000, "no single wait may exceed 30 s")
        self.assertLessEqual(sum(page.waited_ms), 60_000, "total recovery stays inside the ceiling")
        self.assertEqual(page.waited_ms, sorted(page.waited_ms), "the backoff increases")
        self.assertGreater(page.login_lookups, 3, "every attempt resolves a new locator")

    def test_transient_trial_failure_is_retried_with_a_fresh_locator(self) -> None:
        """A bounded actionability timeout is transient, not a contract failure."""
        flaky = FakeLocator(trial_click_error=synthetic_timeout(), label="flaky")
        ready = FakeLocator(label="ready")
        page = login_page(submits=[flaky, ready])

        self.assertIsNone(self.attempt_login(page))
        self.assertEqual(flaky.trial_clicks, 1)
        self.assertEqual(flaky.clicks, 0, "a control that never proved actionable is not submitted")
        self.assertEqual(ready.trial_clicks, 1)
        self.assertEqual(ready.clicks, 1)

    def test_a_non_timeout_trial_failure_is_not_treated_as_transient(self) -> None:
        """Only a timeout is transient; anything else stops the recovery."""
        broken = FakeLocator(trial_click_error=step_failure(), label="broken")
        page = login_page(submits=[broken])

        error = self.attempt_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assert_submit_stage(error)
        self.assertEqual(page.waited_ms, [])
        self.assertEqual(broken.clicks, 0)
        self.assertNotIn("hunter2", error.message)
        self.assertNotIn("portal.example.invalid", error.message)

    def test_the_one_normal_submit_click_is_never_retried(self) -> None:
        """A dispatched submit may have landed; clicking again could duplicate it."""
        failing = FakeLocator(click_error=synthetic_timeout(), label="failing")
        page = login_page(submits=[failing])

        error = self.attempt_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assert_submit_stage(error)
        self.assertEqual(failing.clicks, 1, "an ambiguous post-dispatch outcome is never re-submitted")

    def test_a_portal_rejection_after_submission_is_still_a_login_error(self) -> None:
        """Recovery must not turn a credential rejection into drift."""
        failing = FakeLocator(click_error=synthetic_timeout(), label="failing")
        page = login_page(submits=[failing], alert_visible=True)

        error = self.attempt_login(page)
        self.assertIsInstance(error, LoginError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_PORTAL_REJECTED")

    def assert_submit_stage(self, error: AppError) -> None:
        """The submit stage keeps its committed marker and its reference."""
        self.assertEqual(error.message, "login submission did not complete")
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_SUBMIT_FAILED")


if __name__ == "__main__":
    unittest.main()
