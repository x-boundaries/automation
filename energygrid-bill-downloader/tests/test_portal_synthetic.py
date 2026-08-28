from __future__ import annotations

import contextlib
from dataclasses import dataclass
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


@contextlib.contextmanager
def fast_portal_recovery(attempts_ms=(0, 50, 150), deadline_seconds: float = 2.0):
    """Shrink the shared recovery ladder for browser-backed drift cases.

    A drifted control is drifted for good, so these cases only need the
    fail-closed outcome -- and a real 60-second window per variant would
    dominate the suite. The committed production ladder and its hard deadline
    are asserted separately, against the constants themselves, so shrinking
    them here cannot hide a widened ceiling.
    """

    with mock.patch.object(portal_module, "PORTAL_RECOVERY_ATTEMPTS_MS", tuple(attempts_ms)), \
            mock.patch.object(
                portal_module, "PORTAL_RECOVERY_DEADLINE_SECONDS", deadline_seconds
            ):
        yield


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
                with PlaywrightPortal(config) as portal, fast_portal_recovery():
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
                        with PlaywrightPortal(config) as portal, fast_portal_recovery():
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
                        with PlaywrightPortal(config) as portal, fast_portal_recovery():
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
                with PlaywrightPortal(config) as portal, fast_portal_recovery():
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
        enabled_error: Exception | None = None,
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
        self._enabled_error = enabled_error
        self._fill_error = fill_error
        self._journal = journal
        self._label = label
        self._clock: "FakePage | None" = None
        # Set by the page when it hands this locator out as the login entry, so
        # the one real entry click is what ends the entry stage.
        self._on_click_hook = None
        self.dispatched = 0
        self.clicks = 0
        self.trial_clicks = 0
        self.trial_timeouts: list[int | None] = []
        self.enabled_checks = 0
        self.enabled_timeouts: list[int | None] = []
        self.fills = 0
        self.waits = 0
        self.wait_timeouts: list[int | None] = []

    def _record(self, action: str) -> None:
        if self._journal is not None:
            self._journal.append(self._label + ":" + action)

    @property
    def first(self) -> "FakeLocator":
        return self

    def wait_for(self, state: str | None = None, timeout: int | None = None) -> None:
        """Model a state wait whose timeout is live, like the real locator's.

        A postcondition probe that leaves the timeout implicit inherits
        `page.set_default_timeout()`, so a failing wait is charged whatever it
        was actually given -- the page default when it was given nothing.
        """
        self.waits += 1
        self.wait_timeouts.append(timeout)
        self._record("wait_for")
        if self._wait_error is not None:
            if self._clock is not None:
                spent = timeout if timeout is not None else self._clock.default_timeout_ms
                self._clock.charge_probe_ms(spent)
            raise self._wait_error

    def count(self) -> int:
        if self._state_error is not None:
            raise self._state_error
        return self._count

    def is_visible(self, timeout: int | None = None) -> bool:
        return self._visible

    def is_enabled(self, timeout: int | None = None) -> bool:
        """Model the real signature: unlike `is_visible`, this one waits.

        For pinned Playwright 1.61.0 `locator.is_enabled(timeout=...)` carries
        a live timeout whose default follows `page.set_default_timeout()`,
        while `locator.is_visible()`'s timeout option is ignored. So a
        readiness check with no explicit timeout inherits the page default,
        and a check that has to wait is charged that whole amount -- which is
        exactly what an unbounded readiness probe costs a bounded recovery.
        """
        self.enabled_checks += 1
        self.enabled_timeouts.append(timeout)
        if self._enabled_error is not None:
            if self._clock is not None:
                spent = timeout if timeout is not None else self._clock.default_timeout_ms
                self._clock.charge_probe_ms(spent)
            raise self._enabled_error
        return self._enabled

    def dispatch_event(self, name: str) -> None:
        self.dispatched += 1
        self._record("dispatch_event")
        if self._dispatch_error is not None:
            raise self._dispatch_error

    def click(self, trial: bool = False, timeout: int | None = None) -> None:
        """Only a normal click submits; a trial click proves actionability.

        The two failure hooks are separate so a transient actionability
        timeout can be modelled without also breaking the one real submit,
        and so a case that breaks the real submit is not intercepted by the
        trial that precedes it.

        A trial that fails is charged its whole budget against the clock,
        which is the worst case and the only one worth bounding. A trial with
        no explicit timeout is charged the page default instead -- that is
        exactly what an unbounded probe inherits, and the deadline cannot
        interrupt a Playwright call that is already blocking.
        """
        if trial:
            self.trial_clicks += 1
            self.trial_timeouts.append(timeout)
            self._record("click_trial")
            if self._trial_click_error is not None:
                if self._clock is not None:
                    spent = timeout if timeout is not None else self._clock.default_timeout_ms
                    self._clock.charge_probe_ms(spent)
                raise self._trial_click_error
            return
        self.clicks += 1
        self._record("click")
        if self._on_click_hook is not None:
            self._on_click_hook()
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
        activations: list[FakeLocator] | None = None,
        placeholders: list[FakeLocator] | None = None,
        entries: list[FakeLocator] | None = None,
        username_fields: list[FakeLocator] | None = None,
        password_fields: list[FakeLocator] | None = None,
        billing_managers: list[FakeLocator] | None = None,
        journal: list[str] | None = None,
        default_timeout_ms: int = 5_000,
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
        # Successive resolutions of one surface, so a caller that re-resolves at
        # every recovery checkpoint can be handed a different locator each time.
        # The last entry repeats, which models a surface that stays as it is.
        self.activations = activations
        self.placeholders = placeholders
        self.entries = entries
        self.username_fields = username_fields
        self.password_fields = password_fields
        self.billing_managers = billing_managers
        self.journal = journal
        self.goto_calls = 0
        self.login_lookups = 0
        self.alert_lookups = 0
        # The Login role serves the semantics-gate entry and then the submit
        # control. The one real entry click is what moves the page on, so a
        # re-resolved entry is never mistaken for the submit control.
        self.entry_phase = True
        self.entry_lookups = 0
        self.submit_lookups = 0
        self._sequence_lookups: dict[str, int] = {}
        # What `page.set_default_timeout()` would have installed. An
        # unbounded call inherits it, so it is what a missing explicit
        # timeout costs.
        self.default_timeout_ms = default_timeout_ms
        self.waited_ms: list[int] = []
        # Every simulated cost in the order it was incurred, so a case can
        # prove a probe never outlasted the budget remaining at that moment.
        self.ledger: list[tuple[str, int]] = []
        for sequence in (
            self.submits,
            self.activations,
            self.placeholders,
            self.entries,
            self.username_fields,
            self.password_fields,
            self.billing_managers,
        ):
            for locator in sequence or ():
                locator._clock = self
        if self.submit is not None:
            self.submit._clock = self

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.goto_calls += 1
        if self.journal is not None:
            self.journal.append("page:goto")
        if self.goto_error is not None:
            raise self.goto_error

    def _from_sequence(self, key: str, sequence: list[FakeLocator]) -> FakeLocator:
        index = self._sequence_lookups.get(key, 0)
        self._sequence_lookups[key] = index + 1
        return sequence[min(index, len(sequence) - 1)]

    def _end_entry_stage(self) -> None:
        self.entry_phase = False

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        if name == "Enable accessibility":
            if self.activations is not None:
                return self._from_sequence("activation", self.activations)
            return self.activation
        if name == "Login":
            self.login_lookups += 1
            if self.entry_phase:
                self.entry_lookups += 1
                locator = (
                    self._from_sequence("entry", self.entries)
                    if self.entries is not None
                    else self.login_entry
                )
                locator._on_click_hook = self._end_entry_stage
                return locator
            self.submit_lookups += 1
            if self.submits is not None:
                return self._from_sequence("submit", self.submits)
            if self.submit is not None:
                return self.submit
            return self.login_entry
        if role == "alert":
            self.alert_lookups += 1
            return FakeLocator(visible=self.alert_visible)
        if name == "Billing Manager":
            if self.billing_managers is not None:
                return self._from_sequence("billing_manager", self.billing_managers)
            return self.billing_manager if self.billing_manager is not None else FakeLocator()
        raise AssertionError(f"unexpected role lookup: {role}/{name}")

    def wait_for_timeout(self, milliseconds: int) -> None:
        """Record a browser-event-loop yield instead of spending the time."""
        self.waited_ms.append(int(milliseconds))
        self.ledger.append(("yield", int(milliseconds)))
        if self.journal is not None:
            self.journal.append("page:wait_for_timeout")

    def charge_probe_ms(self, milliseconds: int) -> None:
        """Charge a bounded actionability probe against the same clock."""
        self.ledger.append(("probe", int(milliseconds)))

    def simulated_elapsed_ms(self) -> int:
        """Total simulated recovery cost: yields and probes alike."""
        return sum(cost for _kind, cost in self.ledger)

    def simulated_monotonic(self) -> float:
        """A clock that advances only by what this page was actually asked to spend.

        Patched over `portal.time.monotonic`, this makes a deadline-bounded
        loop terminate deterministically and lets a test assert the simulated
        elapsed recovery without any real waiting. Probes count as well as
        yields, so a probe that parks cannot be free.
        """
        return self.simulated_elapsed_ms() / 1000.0

    def locator(self, selector: str):
        assert selector == "flt-semantics-placeholder", selector
        if self.placeholders is not None:
            return self._from_sequence("placeholder", self.placeholders)
        return self.placeholder

    def get_by_label(self, name: str, exact: bool = False):
        if self.label_error is not None:
            raise self.label_error
        if name == "Username":
            if self.username_fields is not None:
                return self._from_sequence("username", self.username_fields)
            return self.username_field if self.username_field is not None else FakeLocator()
        if name == "Password":
            if self.password_fields is not None:
                return self._from_sequence("password", self.password_fields)
            return self.password_field if self.password_field is not None else FakeLocator()
        raise AssertionError(f"unexpected label lookup: {name}")


class FakeConfig:
    """Only the field the login path reads; the URL is never fetched."""

    portal_url = "http://127.0.0.1:1/synthetic"
    timeout_seconds = 5


# Each case is a control that stays in one non-interactive state for the whole
# bounded recovery window, so what it proves is the fail-closed classification
# at the deadline rather than any single probe.
# (case id, locator keyword arguments, expected support reference)
UNREADY_CONTROL_CASES = (
    ("not_appear", {"count": 0}, "NOT_APPEAR"),
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
        "EG_LOGIN_SUBMIT_DISPATCH_UNCERTAIN",
    ),
    (
        "billing_manager_wait",
        lambda exc: {"billing_manager": FakeLocator(wait_error=exc)},
        "EG_LOGIN_BILLING_MANAGER_WAIT_FAILED",
    ),
)

# The committed order of portal interactions for one successful login attempt.
# Every control that is about to be clicked for real is proven actionable by
# one trial click first; readiness itself is proven by current-state reads that
# dispatch nothing. A healthy portal is ready at the immediate checkpoint, so it
# costs no extra resolution and no event-loop yield.
SUCCESSFUL_LOGIN_SEQUENCE = [
    "page:goto",
    "activation:dispatch_event",
    "placeholder:wait_for",
    "login_entry:click_trial",
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
        """A visible alert cannot override the uncertain submit-click boundary."""
        for case_id, break_step, layout_ref in LOGIN_STEP_CASES:
            with self.subTest(case=case_id):
                overrides = break_step(step_failure())
                overrides["alert_visible"] = True
                error = self.run_login(login_page(**overrides))
                if case_id == "login_submit":
                    # Once normal submit.click() begins, an alert cannot prove
                    # whether the click's outcome is safe to repeat.
                    self.assertIsInstance(error, LayoutChangedError)
                    self.assert_maps_non_generically(error, layout_ref)
                else:
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
        self.assertEqual(locators["login_entry"].trial_clicks, 1)
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

        # Submit readiness uses the same bounded primitive but has its own
        # public-safe vocabulary. Every case must finish before a normal click.
        for _case_id, kwargs, _outcome in UNREADY_CONTROL_CASES:
            submit = FakeLocator(**kwargs)
            error = self.run_login(login_page(submits=[submit]))
            record(error)
            self.assertEqual(submit.clicks, 0)

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


# Asserted independently of the production constants, so a case fails if the
# committed ceiling or per-probe cap is widened rather than silently tracking it.
RECOVERY_CEILING_MS = 60_000
MAX_TRIAL_PROBE_MS = 1_000


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

    def test_persistent_ambiguity_fails_closed_without_ever_interacting(self) -> None:
        """Ambiguity is re-checked but never acted on, and never survives.

        DL-XB-141-PORTAL-RESILIENCE-002 supersedes the rule that any momentary
        second match is immediately terminal: a mid-transition frame can show
        two matches. What it does not permit is acting on the ambiguity, so the
        control is looked at again from a fresh locator and never clicked,
        never narrowed with `.first`, and never reached by a weaker selector.
        """
        ambiguous = FakeLocator(count=2, label="ambiguous")
        page = login_page(submits=[ambiguous])

        error = self.attempt_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.status, PORTAL_LAYOUT_CHANGED)
        self.assert_submit_stage(
            error,
            "login submit control is missing or ambiguous",
            "EG_LOGIN_SUBMIT_AMBIGUOUS",
        )
        self.assertTrue(page.waited_ms, "transient ambiguity is re-checked, not terminal on sight")
        self.assertGreater(page.login_lookups, 3, "every re-check resolved a fresh locator")
        self.assertEqual(ambiguous.trial_clicks, 0)
        self.assertEqual(ambiguous.clicks, 0)
        self.assert_within_budget(page)

    def test_recovery_is_bounded_by_a_monotonic_deadline(self) -> None:
        """A control that never becomes ready still ends inside the ceiling."""
        never = FakeLocator(count=0, label="never")
        page = login_page(submits=[never])

        error = self.attempt_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assert_submit_stage(
            error,
            "login submit control did not appear",
            "EG_LOGIN_SUBMIT_NOT_APPEAR",
        )
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
        self.assert_submit_stage(
            error,
            "login submit control could not be resolved",
            "EG_LOGIN_SUBMIT_UNRESOLVED",
        )
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
        self.assert_submit_stage(
            error,
            "login submit dispatch outcome uncertain",
            "EG_LOGIN_SUBMIT_DISPATCH_UNCERTAIN",
        )
        self.assertEqual(failing.clicks, 1, "an ambiguous post-dispatch outcome is never re-submitted")

    def test_a_submit_click_exception_is_uncertain_even_with_a_visible_alert(self) -> None:
        """An alert cannot prove a normal click's outcome after invocation."""
        failing = FakeLocator(click_error=synthetic_timeout(), label="failing")
        page = login_page(submits=[failing], alert_visible=True)

        error = self.attempt_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assert_submit_stage(
            error,
            "login submit dispatch outcome uncertain",
            "EG_LOGIN_SUBMIT_DISPATCH_UNCERTAIN",
        )
        self.assertEqual(failing.clicks, 1)

    def test_a_long_page_timeout_cannot_extend_the_bounded_recovery(self) -> None:
        """A trial probe must not inherit `page.set_default_timeout()`.

        `RuntimeConfig` permits `timeout_seconds` up to `MAX_TIMEOUT_SECONDS`,
        so an unbounded probe can park for minutes, and the monotonic deadline
        cannot interrupt a Playwright call that is already blocking. Each
        probe therefore carries its own small explicit timeout and is charged
        against the same ceiling as the yields.
        """
        probe = FakeLocator(trial_click_error=synthetic_timeout(), label="probe")
        page = login_page(submits=[probe], default_timeout_ms=300_000)

        error = self.attempt_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assert_submit_stage(
            error,
            "login submit control is hidden or disabled",
            "EG_LOGIN_SUBMIT_NOT_READY",
        )

        self.assertTrue(probe.trial_timeouts, "the actionability probe must actually run")
        self.assertTrue(
            all(value is not None for value in probe.trial_timeouts),
            "every trial click must carry an explicit timeout",
        )
        self.assertLessEqual(
            max(probe.trial_timeouts),
            MAX_TRIAL_PROBE_MS,
            "a probe stays far below any configured page default",
        )
        self.assert_within_budget(page)
        self.assertGreater(page.login_lookups, 3, "each probe used a freshly resolved locator")
        self.assertEqual(probe.clicks, 0, "recovery exhausted, so nothing was submitted")

    def test_a_bounded_probe_that_later_succeeds_still_submits_exactly_once(self) -> None:
        """Bounding the probe must not cost the recovery its one real submit."""
        slow = FakeLocator(trial_click_error=synthetic_timeout(), label="slow")
        ready = FakeLocator(label="ready")
        page = login_page(submits=[slow, ready], default_timeout_ms=300_000)

        self.assertIsNone(self.attempt_login(page))
        self.assertEqual(slow.clicks, 0, "a control that never proved actionable is not submitted")
        self.assertEqual(ready.clicks, 1)
        self.assertLessEqual(max(slow.trial_timeouts), MAX_TRIAL_PROBE_MS)
        self.assert_within_budget(page)

    def test_an_enabled_readiness_check_is_explicitly_bounded(self) -> None:
        """The readiness probe must not inherit `page.set_default_timeout()`.

        `locator.is_enabled()` is not `locator.is_visible()`: its timeout is
        live and defaults to the page default, so an unbounded readiness check
        can park for minutes inside a recovery region that is supposed to be
        bounded. It therefore carries its own small explicit timeout and is
        charged against the same ceiling as the trials and the yields.
        """
        stalling = FakeLocator(enabled_error=synthetic_timeout(), label="stalling")
        page = login_page(submits=[stalling], default_timeout_ms=300_000)

        error = self.attempt_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assert_submit_stage(
            error,
            "login submit control is hidden or disabled",
            "EG_LOGIN_SUBMIT_NOT_READY",
        )

        self.assertTrue(stalling.enabled_timeouts, "the readiness check must actually run")
        self.assertTrue(
            all(value is not None for value in stalling.enabled_timeouts),
            "every enabled check must carry an explicit timeout",
        )
        self.assertLessEqual(
            max(stalling.enabled_timeouts),
            MAX_TRIAL_PROBE_MS,
            "a readiness check stays far below any configured page default",
        )
        self.assert_within_budget(page)
        self.assertEqual(stalling.clicks, 0, "recovery exhausted, so nothing was submitted")
        self.assertEqual(stalling.trial_clicks, 0, "a control that is not enabled is never probed")

    def test_enabled_readiness_timeout_re_resolves_and_then_submits_once(self) -> None:
        """A readiness timeout is transient: look again with a new locator."""
        stalling = FakeLocator(enabled_error=synthetic_timeout(), label="stalling")
        ready = FakeLocator(label="ready")
        page = login_page(submits=[stalling, ready], default_timeout_ms=300_000)

        self.assertIsNone(self.attempt_login(page))
        self.assertEqual(stalling.enabled_checks, 1)
        self.assertEqual(stalling.clicks, 0, "the locator that timed out is never submitted")
        # The checkpoints are elapsed offsets, not additive sleeps: a probe that
        # already spent the first checkpoint's worth of budget has nothing left
        # to yield before the next look, so the retry is immediate.
        self.assertEqual(page.waited_ms, [], "a probe that already elapsed past a checkpoint adds no sleep")
        self.assertGreaterEqual(page.login_lookups, 3, "the retry resolved a fresh locator")
        self.assertEqual(ready.trial_clicks, 1)
        self.assertEqual(ready.clicks, 1)
        self.assertLessEqual(max(stalling.enabled_timeouts), MAX_TRIAL_PROBE_MS)
        self.assert_within_budget(page)

    def test_a_non_timeout_enabled_failure_is_not_treated_as_transient(self) -> None:
        """Only a timeout is transient; a real readiness failure stops recovery."""
        broken = FakeLocator(enabled_error=step_failure(), label="broken")
        page = login_page(submits=[broken])

        error = self.attempt_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assert_submit_stage(
            error,
            "login submit control could not be resolved",
            "EG_LOGIN_SUBMIT_UNRESOLVED",
        )
        self.assertEqual(broken.enabled_checks, 1, "a real failure is not retried")
        self.assertEqual(page.waited_ms, [], "no yield follows a terminal readiness failure")
        self.assertEqual(broken.trial_clicks, 0)
        self.assertEqual(broken.clicks, 0)
        self.assertNotIn("hunter2", error.message)
        self.assertNotIn("portal.example.invalid", error.message)

    def test_ambiguity_and_invisibility_never_reach_the_enabled_check(self) -> None:
        """The cheap short-circuits still run before the one waiting check."""
        ambiguous = FakeLocator(count=2, label="ambiguous")
        self.assertIsInstance(self.attempt_login(login_page(submits=[ambiguous])), LayoutChangedError)
        self.assertEqual(ambiguous.enabled_checks, 0, "ambiguity is terminal before any wait")

        hidden = FakeLocator(visible=False, label="hidden")
        page = login_page(submits=[hidden])
        error = self.attempt_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assert_submit_stage(
            error,
            "login submit control is hidden or disabled",
            "EG_LOGIN_SUBMIT_NOT_READY",
        )
        self.assertEqual(hidden.enabled_checks, 0, "an invisible control is not asked for state")
        self.assertEqual(hidden.clicks, 0)
        self.assert_within_budget(page)

    def assert_within_budget(self, page: FakePage) -> None:
        """No probe outlasts the budget left when it started, nor the ceiling."""
        elapsed = 0
        for kind, cost in page.ledger:
            if kind == "probe":
                self.assertLessEqual(
                    cost,
                    RECOVERY_CEILING_MS - elapsed,
                    "a probe may not outlast the remaining recovery budget",
                )
            elapsed += cost
        self.assertLessEqual(
            elapsed, RECOVERY_CEILING_MS, "probes and yields share one hard ceiling"
        )

    def assert_submit_stage(
        self, error: AppError, expected_message: str, expected_ref: str
    ) -> None:
        """The submit outcome maps to its exact fixed message and reference."""
        self.assertEqual(error.message, expected_message)
        self.assertEqual(cli.support_ref_for(error), expected_ref)



# ---- DL-XB-141-PORTAL-RESILIENCE-002: the shared readiness primitive ---- #
#
# These cases drive the committed recovery mechanism itself rather than one
# surface that happens to use it, because what the owner approved is one
# pattern reused everywhere: retry readiness, never the real action. The clock
# and the checkpoint ladder are simulated, so the production 60-second contract
# is proven without anything actually waiting.


@dataclass(frozen=True)
class Frame:
    """One observation of a control, as a single fresh resolution sees it."""

    count: int = 1
    visible: bool = True
    enabled: bool = True
    enabled_timeout: bool = False
    actionable: bool = True


READY_FRAME = Frame()


class RecoveryClock:
    """Simulated elapsed time: only what the portal actually asked to spend.

    Yields and bounded probes are charged to the same ledger, so a probe that
    parks cannot be free and one ceiling covers both.
    """

    def __init__(self, default_timeout_ms: int = 300_000) -> None:
        self.default_timeout_ms = default_timeout_ms
        self.ledger: list[tuple[str, int]] = []

    def charge_yield(self, milliseconds: int) -> None:
        self.ledger.append(("yield", int(milliseconds)))

    def charge_probe(self, milliseconds: int | None) -> None:
        """Charge a probe its whole budget: the page default if it had none."""
        spent = self.default_timeout_ms if milliseconds is None else milliseconds
        self.ledger.append(("probe", int(spent)))

    @property
    def yields(self) -> list[int]:
        return [cost for kind, cost in self.ledger if kind == "yield"]

    @property
    def probes(self) -> list[int]:
        return [cost for kind, cost in self.ledger if kind == "probe"]

    def elapsed_ms(self) -> int:
        return sum(cost for _kind, cost in self.ledger)

    def monotonic(self) -> float:
        return self.elapsed_ms() / 1000.0


@contextlib.contextmanager
def simulated_clock(clock: RecoveryClock):
    """Patch the clock the recovery reads so a deadline loop is deterministic.

    `create=True` keeps these cases failing on behaviour rather than on the
    absence of a clock, which is what makes them meaningful regressions against
    a revision with no bounded recovery at all.
    """

    with mock.patch.object(portal_module, "time", create=True) as fake:
        fake.monotonic.side_effect = clock.monotonic
        yield


class ScriptedLocator:
    """One resolution of a scripted control, frozen at the frame it observed."""

    def __init__(self, control: "ScriptedControl", frame: Frame) -> None:
        self.control = control
        self.frame = frame

    def count(self) -> int:
        return self.frame.count

    def is_visible(self, timeout: int | None = None) -> bool:
        return self.frame.visible

    def is_enabled(self, timeout: int | None = None) -> bool:
        self.control.enabled_timeouts.append(timeout)
        if self.frame.enabled_timeout:
            self.control.clock.charge_probe(timeout)
            raise synthetic_timeout()
        return self.frame.enabled

    def click(self, trial: bool = False, timeout: int | None = None) -> None:
        if trial:
            self.control.trial_clicks += 1
            self.control.trial_timeouts.append(timeout)
            if not self.frame.actionable:
                self.control.clock.charge_probe(timeout)
                raise synthetic_timeout()
            return
        self.control.clicks += 1


class ScriptedControl:
    """A control whose observable state advances with every fresh resolution.

    `frames` is consumed one entry per resolution and the last entry repeats,
    so a surface that settles after a known number of fresh looks is
    deterministic -- and "a freshly resolved locator is what found it" becomes
    directly countable.
    """

    def __init__(self, clock: RecoveryClock, frames) -> None:
        self.clock = clock
        self.frames = list(frames)
        self.resolutions = 0
        self.clicks = 0
        self.trial_clicks = 0
        self.enabled_timeouts: list[int | None] = []
        self.trial_timeouts: list[int | None] = []

    def resolve(self) -> ScriptedLocator:
        frame = self.frames[min(self.resolutions, len(self.frames) - 1)]
        self.resolutions += 1
        return ScriptedLocator(self, frame)


class RecoveryPage:
    """A page whose only job is to hand a fresh scripted locator to each look."""

    def __init__(self, control: ScriptedControl, clock: RecoveryClock) -> None:
        self.control = control
        self.clock = clock

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.clock.charge_yield(milliseconds)

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        return self.control.resolve()


class PortalReadinessRecoveryTests(unittest.TestCase):
    """One reusable readiness primitive, independent of any single surface."""

    def resolve(self, frames, **kwargs):
        """Recover readiness for a scripted control and report the outcome."""

        clock = RecoveryClock()
        control = ScriptedControl(clock, frames)
        page = RecoveryPage(control, clock)
        portal = PlaywrightPortal(FakeConfig(), headed=False)
        portal.page = page
        error: AppError | None = None
        locator = None
        with simulated_clock(clock):
            try:
                locator = portal._resolve_ready_control(
                    page,
                    lambda: page.get_by_role("button", name="Synthetic", exact=True),
                    "synthetic control",
                    **kwargs,
                )
            except AppError as exc:
                error = exc
        return control, clock, locator, error

    def assert_bounded(self, clock: RecoveryClock, control: ScriptedControl) -> None:
        """Every probe is explicit, capped, and inside the budget it started with."""

        elapsed = 0
        for kind, cost in clock.ledger:
            if kind == "probe":
                self.assertLessEqual(cost, MAX_TRIAL_PROBE_MS, "a probe stays inside the shared cap")
                self.assertLessEqual(
                    cost,
                    RECOVERY_CEILING_MS - elapsed,
                    "a probe may not outlast the remaining recovery budget",
                )
            elapsed += cost
        self.assertLessEqual(
            elapsed, RECOVERY_CEILING_MS, "probes and yields share one hard ceiling"
        )
        for value in control.enabled_timeouts + control.trial_timeouts:
            self.assertIsNotNone(value, "every waiting probe carries an explicit timeout")
            self.assertNotEqual(value, 0, "a zero timeout would mean waiting forever")
            self.assertLessEqual(value, MAX_TRIAL_PROBE_MS)

    def assert_deadline_respected(self, clock: RecoveryClock, control: ScriptedControl) -> None:
        """A surface that never settles still ends inside the hard ceiling."""

        attempts = len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS)
        self.assertEqual(
            control.resolutions, attempts, "every checkpoint resolved its own fresh locator"
        )
        self.assertLessEqual(clock.elapsed_ms(), RECOVERY_CEILING_MS)
        self.assertTrue(clock.yields, "a bounded window yields between looks")
        self.assertEqual(clock.yields, sorted(clock.yields), "the elapsed ladder increases")
        self.assert_bounded(clock, control)

    def test_the_committed_recovery_contract_is_the_owner_approved_one(self) -> None:
        """The production ladder, ceiling and probe cap, asserted literally.

        The browser-backed drift cases shrink these constants so the suite does
        not spend a real minute per variant, so the committed values are pinned
        here rather than inferred from any case's behaviour.
        """
        self.assertEqual(
            portal_module.PORTAL_RECOVERY_CHECKPOINTS_MS, (250, 1000, 5000, 10000, 30000)
        )
        self.assertEqual(portal_module.PORTAL_RECOVERY_DEADLINE_SECONDS, 60.0)
        self.assertEqual(portal_module.MAX_PORTAL_PROBE_TIMEOUT_MS, MAX_TRIAL_PROBE_MS)

        attempts = portal_module.PORTAL_RECOVERY_ATTEMPTS_MS
        self.assertEqual(
            attempts[0], 0, "the first look is immediate, so a healthy portal pays nothing"
        )
        self.assertEqual(attempts[1:-1], portal_module.PORTAL_RECOVERY_CHECKPOINTS_MS)
        self.assertEqual(
            list(attempts), sorted(attempts), "the checkpoints are increasing elapsed offsets"
        )
        self.assertLess(
            attempts[-1], RECOVERY_CEILING_MS, "a final fresh look happens inside the deadline"
        )
        self.assertGreaterEqual(
            RECOVERY_CEILING_MS - attempts[-1],
            2 * MAX_TRIAL_PROBE_MS,
            "the final look leaves room for the bounded probes it will run",
        )

    def test_an_absent_control_that_appears_later_is_recovered(self) -> None:
        control, clock, locator, error = self.resolve([Frame(count=0), READY_FRAME])
        self.assertIsNone(error)
        self.assertIsNotNone(locator)
        self.assertEqual(control.resolutions, 2, "the second look resolved a fresh locator")
        self.assertEqual(clock.yields, [250], "the first elapsed checkpoint is a quarter second")
        self.assert_bounded(clock, control)

    def test_a_hidden_control_that_becomes_visible_is_recovered(self) -> None:
        control, clock, locator, error = self.resolve(
            [Frame(visible=False), Frame(visible=False), READY_FRAME]
        )
        self.assertIsNone(error)
        self.assertIsNotNone(locator)
        self.assertEqual(control.resolutions, 3)
        self.assertEqual(
            len(control.enabled_timeouts),
            1,
            "an invisible control is never asked for its enabled state",
        )
        self.assert_bounded(clock, control)

    def test_a_disabled_control_that_becomes_enabled_is_recovered(self) -> None:
        control, clock, locator, error = self.resolve([Frame(enabled=False), READY_FRAME])
        self.assertIsNone(error)
        self.assertIsNotNone(locator)
        self.assertEqual(len(control.enabled_timeouts), 2, "each look asked afresh")
        self.assert_bounded(clock, control)

    def test_an_enabled_probe_timeout_is_transient_not_terminal(self) -> None:
        control, clock, locator, error = self.resolve([Frame(enabled_timeout=True), READY_FRAME])
        self.assertIsNone(error)
        self.assertIsNotNone(locator)
        self.assertEqual(clock.probes, [MAX_TRIAL_PROBE_MS], "the stalled check was charged its cap")
        self.assert_bounded(clock, control)

    def test_a_trial_actionability_timeout_that_later_clears_is_recovered(self) -> None:
        control, clock, locator, error = self.resolve(
            [Frame(actionable=False), Frame(actionable=False), READY_FRAME],
            require_trial_actionable=True,
        )
        self.assertIsNone(error)
        self.assertIsNotNone(locator)
        self.assertEqual(control.trial_clicks, 3, "actionability was re-proven, never assumed")
        self.assertEqual(control.clicks, 0, "the primitive never dispatches the real action")
        self.assert_bounded(clock, control)

    def test_transient_ambiguity_is_re_resolved_and_never_interacted_with(self) -> None:
        """Two matches may be a mid-transition frame; acting on them never is."""
        control, clock, locator, error = self.resolve(
            [Frame(count=2), Frame(count=2), READY_FRAME], require_trial_actionable=True
        )
        self.assertIsNone(error)
        self.assertIsNotNone(locator)
        self.assertEqual(control.resolutions, 3)
        self.assertEqual(
            control.trial_clicks, 1, "only the unambiguous look was probed for actionability"
        )
        self.assertEqual(
            len(control.enabled_timeouts), 1, "an ambiguous look is never asked for state"
        )
        self.assertEqual(control.clicks, 0)
        self.assert_bounded(clock, control)

    def test_persistent_ambiguity_fails_closed_at_the_deadline(self) -> None:
        control, clock, locator, error = self.resolve(
            [Frame(count=2)], require_trial_actionable=True
        )
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "synthetic control is missing or ambiguous")
        self.assertIsNone(locator)
        self.assertEqual(control.trial_clicks, 0)
        self.assertEqual(control.clicks, 0)
        self.assert_deadline_respected(clock, control)

    def test_persistent_absence_fails_closed_at_the_deadline(self) -> None:
        control, clock, locator, error = self.resolve([Frame(count=0)])
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "synthetic control did not appear")
        self.assertIsNone(locator)
        self.assert_deadline_respected(clock, control)

    def test_a_control_that_never_becomes_actionable_fails_closed(self) -> None:
        control, clock, locator, error = self.resolve(
            [Frame(actionable=False)], require_trial_actionable=True
        )
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "synthetic control is hidden or disabled")
        self.assertIsNone(locator)
        self.assertEqual(control.clicks, 0, "an unactionable control is never really clicked")
        self.assert_deadline_respected(clock, control)

    def test_an_unresolvable_locator_is_drift_and_is_not_waited_out(self) -> None:
        """A locator that cannot resolve at all never becomes correct by waiting."""

        clock = RecoveryClock()
        page = RecoveryPage(ScriptedControl(clock, [READY_FRAME]), clock)

        def broken():
            raise RuntimeError("strict mode violation")

        portal = PlaywrightPortal(FakeConfig(), headed=False)
        portal.page = page
        with simulated_clock(clock):
            with self.assertRaises(LayoutChangedError) as caught:
                portal._resolve_ready_control(page, broken, "synthetic control")
        self.assertEqual(caught.exception.message, "synthetic control could not be resolved")
        self.assertEqual(clock.yields, [], "drift is terminal on sight, not retried")

    def test_a_long_page_default_cannot_extend_the_shared_window(self) -> None:
        """No probe may inherit `page.set_default_timeout()` inside a recovery.

        The simulated clock charges an implicit probe the 300 s page default,
        so a single unbounded readiness check would blow the ceiling on its
        first attempt instead of ending inside it.
        """

        control, clock, _locator, error = self.resolve([Frame(enabled_timeout=True)])
        self.assertIsInstance(error, LayoutChangedError)
        self.assertTrue(control.enabled_timeouts)
        self.assertTrue(all(value == MAX_TRIAL_PROBE_MS for value in control.enabled_timeouts))
        self.assert_deadline_respected(clock, control)



# ---- DL-XB-141-PORTAL-RESILIENCE-002: post-action settling downstream ---- #
#
# The results route is where a duplicated dispatch is most expensive: a second
# Search discards the first result, a second Next page skips a page of
# inventory, and a second Download re-bills the portal. These cases drive the
# committed `inventory()` and `download()` with a page whose surfaces settle
# only after a known number of fresh looks, and assert both that the flow
# recovers and that every real dispatch happened exactly once.


class ResultsConfig:
    """Only the fields the results route reads; no URL is ever fetched."""

    portal_url = "http://127.0.0.1:1/synthetic"
    timeout_seconds = 5
    account_identity = "SYNTHETIC-INTENDED-ACCOUNT"


class _ResultsControl:
    """One resolution of a results-route control."""

    def __init__(
        self,
        page: "FakeResultsPage",
        key: str,
        *,
        present: bool = True,
        visible: bool = True,
        enabled: bool = True,
        disabled: bool = False,
        actionable: bool = True,
        on_click=None,
    ) -> None:
        self._page = page
        self._key = key
        self._present = present
        self._visible = visible
        self._enabled = enabled
        self._disabled = disabled
        self._actionable = actionable
        self._on_click = on_click

    def count(self) -> int:
        return 1 if self._present else 0

    def is_visible(self, timeout: int | None = None) -> bool:
        return self._visible

    def is_enabled(self, timeout: int | None = None) -> bool:
        self._page.probe_timeouts.append(timeout)
        return self._enabled

    def is_disabled(self, timeout: int | None = None) -> bool:
        self._page.probe_timeouts.append(timeout)
        return self._disabled

    def click(self, trial: bool = False, timeout: int | None = None) -> None:
        if trial:
            self._page.probe_timeouts.append(timeout)
            self._page.trial_clicks[self._key] = self._page.trial_clicks.get(self._key, 0) + 1
            if not self._actionable:
                self._page.clock.charge_probe(timeout)
                raise synthetic_timeout()
            return
        self._page.clicks[self._key] = self._page.clicks.get(self._key, 0) + 1
        self._page.events.append(self._key)
        if self._on_click is not None:
            self._on_click()


class _TextMarker:
    """A read-only identity marker the account binding compares against."""

    def __init__(self, text: str, present: bool = True) -> None:
        self._text = text
        self._present = present

    def count(self) -> int:
        return 1 if self._present else 0

    @property
    def first(self) -> "_TextMarker":
        return self

    def text_content(self) -> str:
        return self._text


class _Option:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_attribute(self, name: str) -> str | None:
        return self._value if name == "value" else None


class _Options:
    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    def all_text_contents(self) -> list[str]:
        return list(self._texts)

    def nth(self, index: int) -> _Option:
        return _Option(f"synthetic-account-{index}")


class _AccountControl:
    """The tenant/account selector, whose identity contract stays terminal."""

    def __init__(self, page: "FakeResultsPage", present: bool) -> None:
        self._page = page
        self._present = present

    def count(self) -> int:
        return 1 if self._present else 0

    def is_visible(self, timeout: int | None = None) -> bool:
        return True

    def is_enabled(self, timeout: int | None = None) -> bool:
        self._page.probe_timeouts.append(timeout)
        return True

    def locator(self, selector: str):
        if selector == "option":
            return _Options(list(self._page.account_options))
        if selector == "option:checked":
            return _TextMarker(self._page.selected_account)
        raise AssertionError(f"unexpected selector: {selector}")

    def select_option(self, value: str | None = None) -> None:
        self._page.selected_account = self._page.account
        self._page.events.append("select_account")


class _StateMarker:
    """The result-state marker, which reports pre-search until it settles.

    `get_attribute` auto-waits in Playwright, so the fake takes the timeout the
    production probe must pass and records it separately from every other
    probe. A stalled read is charged the timeout it was actually given, which is
    what makes an omitted argument cost the whole page default instead of
    nothing.
    """

    def __init__(self, page: "FakeResultsPage") -> None:
        self._page = page

    def count(self) -> int:
        return 1

    def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        assert name == "data-state", name
        self._page.state_attribute_timeouts.append(timeout)
        self._page.probe_timeouts.append(timeout)
        if self._page.state_attribute_error is not None:
            raise self._page.state_attribute_error
        if self._page.state_attribute_stalls > 0:
            self._page.state_attribute_stalls -= 1
            self._page.clock.charge_probe(timeout)
            raise synthetic_timeout()
        if self._page.post_search_pending > 0:
            self._page.post_search_pending -= 1
            return "pre-search"
        return "post-search" if self._page.searched else "pre-search"


class _ListContainer:
    def __init__(self, page: "FakeResultsPage", present: bool) -> None:
        self._page = page
        self._present = present

    def count(self) -> int:
        return 1 if self._present else 0

    def is_visible(self, timeout: int | None = None) -> bool:
        return True

    def get_attribute(self, name: str) -> str | None:
        assert name == "data-page", name
        return f"page-{self._page.page_index + 1}"


class _Row:
    def __init__(self, page: "FakeResultsPage", filename: str) -> None:
        self._page = page
        self._filename = filename

    def get_attribute(self, name: str) -> str | None:
        return self._filename if name == "data-filename" else None

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        assert name == "Download", name
        looks = self._page.bump("download")
        filename = self._filename
        return _ResultsControl(
            self._page,
            "download",
            present=looks > self._page.download_delay,
            on_click=lambda: setattr(self._page, "last_download", filename),
        )


class _Rows:
    def __init__(self, page: "FakeResultsPage", filenames: list[str]) -> None:
        self._page = page
        self._filenames = filenames

    def count(self) -> int:
        return len(self._filenames)

    def all(self) -> list[_Row]:
        return [_Row(self._page, name) for name in self._filenames]


class _Download:
    def __init__(self, filename: str | None) -> None:
        self._filename = filename

    def failure(self):
        return None

    @property
    def suggested_filename(self) -> str | None:
        return self._filename

    def save_as(self, destination) -> None:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.7\nsynthetic\n%%EOF\n")


class _DownloadInfo:
    def __init__(self, page: "FakeResultsPage") -> None:
        self._page = page

    @property
    def value(self) -> _Download:
        return _Download(self._page.last_download)


class FakeResultsPage:
    """The results route as the portal drives it, with settling knobs.

    Every knob counts fresh looks rather than milliseconds, so a delayed
    transition is deterministic and costs no real time. `clicks` and `events`
    are what the single-dispatch and owner-sequence cases assert; no browser,
    no server and no credentials are involved.
    """

    def __init__(
        self,
        *,
        pages=(("2026-01-01_synthetic.pdf",),),
        account: str = "SYNTHETIC-INTENDED-ACCOUNT",
        billing_manager_delay: int = 0,
        eb_bill_delay: int = 0,
        account_delay: int = 0,
        post_search_delay: int = 0,
        list_delay: int = 0,
        advance_delay: int = 0,
        download_delay: int = 0,
        state_attribute_stalls: int = 0,
        state_attribute_error: Exception | None = None,
        clock: RecoveryClock | None = None,
    ) -> None:
        self.clock = clock or RecoveryClock()
        self.pages = [list(names) for names in pages]
        self.account = account
        self.account_options = [account]
        self.selected_account = account
        self.billing_manager_delay = billing_manager_delay
        self.eb_bill_delay = eb_bill_delay
        self.account_delay = account_delay
        self.post_search_delay = post_search_delay
        self.list_delay = list_delay
        self.advance_delay = advance_delay
        self.download_delay = download_delay
        self.state_attribute_stalls = state_attribute_stalls
        self.state_attribute_error = state_attribute_error
        self.route = "app"
        self.page_index = 0
        self.searched = False
        self.post_search_pending = 0
        self.last_download: str | None = None
        self.looks: dict[str, int] = {}
        self.clicks: dict[str, int] = {}
        self.trial_clicks: dict[str, int] = {}
        self.probe_timeouts: list[int | None] = []
        # Kept apart from `probe_timeouts` so a case can require the state
        # marker's own attribute read to be bounded, rather than passing
        # because some unrelated probe happened to be.
        self.state_attribute_timeouts: list[int | None] = []
        self.events: list[str] = []
        self.rows_read_before_search = False
        self._advance_after = 0
        self._advance_target: int | None = None

    # -- fixture helpers -- #

    def bump(self, key: str) -> int:
        self.looks[key] = self.looks.get(key, 0) + 1
        return self.looks[key]

    def reset_looks(self, key: str) -> None:
        self.looks[key] = 0

    def delay_download(self, looks: int) -> None:
        """Delay the download control from the next look onward."""
        self.download_delay = looks
        self.reset_looks("download")

    def _open_billing(self) -> None:
        self.route = "billing"

    def _open_results(self) -> None:
        self.route = "results"

    def _run_search(self) -> None:
        self.searched = True
        self.post_search_pending = self.post_search_delay
        self.page_index = 0
        self.reset_looks("invoice-list")

    def _has_next(self) -> bool:
        return self.page_index + 1 < len(self.pages)

    def _click_next(self) -> None:
        self._advance_after = self.advance_delay
        self._advance_target = self.page_index + 1

    # -- the slice of the page surface the results route touches -- #

    @property
    def url(self) -> str:
        return f"http://synthetic.invalid/results?page={self.page_index + 1}"

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.events.append("goto")
        self.route = "results"
        self.searched = False
        self.post_search_pending = 0
        for key in ("account", "invoice-list", "search"):
            self.reset_looks(key)
        index = int(url.rsplit("=", 1)[1]) - 1
        self.page_index = max(0, min(index, len(self.pages) - 1))

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.clock.charge_yield(milliseconds)

    def get_by_label(self, name: str, exact: bool = False):
        assert name == "Tenant/account", name
        looks = self.bump("account")
        return _AccountControl(self, self.route == "results" and looks > self.account_delay)

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        if name == "Billing Manager":
            looks = self.bump("billing_manager")
            return _ResultsControl(
                self,
                "billing_manager",
                present=self.route == "app" and looks > self.billing_manager_delay,
                on_click=self._open_billing,
            )
        if name == "EB Bill":
            looks = self.bump("eb_bill")
            return _ResultsControl(
                self,
                "eb_bill",
                present=self.route == "billing" and looks > self.eb_bill_delay,
                on_click=self._open_results,
            )
        if name == "Search":
            self.bump("search")
            return _ResultsControl(
                self, "search", present=self.route == "results", on_click=self._run_search
            )
        if name == "Next page":
            self.bump("next_page")
            return _ResultsControl(
                self,
                "next_page",
                present=True,
                disabled=not self._has_next(),
                on_click=self._click_next,
            )
        raise AssertionError(f"unexpected role lookup: {role}/{name}")

    def get_by_test_id(self, test_id: str):
        if test_id == "invoice-list":
            looks = self.bump("invoice-list")
            return _ListContainer(self, looks > self.list_delay)
        if test_id == "invoice-row":
            if not self.searched:
                self.rows_read_before_search = True
            return _Rows(self, list(self.pages[self.page_index]) if self.searched else [])
        if test_id == "invoice-list-empty":
            empty = not (self.searched and self.pages[self.page_index])
            return _ResultsControl(self, "invoice-list-empty", present=empty, visible=empty)
        if test_id == "invoice-results-state":
            self.bump("results_state")
            return _StateMarker(self)
        if test_id == "selected-account":
            return _TextMarker(self.selected_account)
        raise AssertionError(f"unexpected test id: {test_id}")

    def wait_for_function(self, script: str, arg=None, timeout: int | None = None) -> None:
        self.probe_timeouts.append(timeout)
        if self._advance_after > 0:
            self._advance_after -= 1
            self.clock.charge_probe(timeout)
            raise synthetic_timeout()
        if self._advance_target is not None:
            self.page_index = self._advance_target
            self._advance_target = None
            self.reset_looks("invoice-list")

    def expect_download(self):
        page = self

        @contextlib.contextmanager
        def manager():
            yield _DownloadInfo(page)

        return manager()


class PortalResultsSettlingTests(unittest.TestCase):
    """One dispatch, then settle: the results route under eventual consistency."""

    def route(self, **kwargs):
        clock = RecoveryClock()
        page = FakeResultsPage(clock=clock, **kwargs)
        portal = PlaywrightPortal(ResultsConfig(), headed=False)
        portal.page = page
        return portal, page, clock

    def assert_probes_bounded(self, page: FakeResultsPage) -> None:
        """Every waiting call on this route carried an explicit small timeout."""

        self.assertTrue(page.probe_timeouts, "the route must actually probe")
        for value in page.probe_timeouts:
            self.assertIsNotNone(value, "no probe may inherit the page default")
            self.assertNotEqual(value, 0, "a zero timeout would mean waiting forever")
            self.assertLessEqual(value, MAX_TRIAL_PROBE_MS)

    def assert_state_attribute_bounded(self, page: FakeResultsPage) -> None:
        """The state marker's own attribute read is explicitly bounded.

        Asserted against the state-marker evidence specifically, so dropping
        the timeout from that one call fails here even though other probes on
        the route are still bounded.
        """

        self.assertTrue(
            page.state_attribute_timeouts, "the state marker must actually be read"
        )
        for value in page.state_attribute_timeouts:
            self.assertIsNotNone(
                value, "the attribute read may not inherit the page default"
            )
            self.assertNotEqual(value, 0, "a zero timeout would mean waiting forever")
            self.assertLessEqual(value, MAX_TRIAL_PROBE_MS)

    def assert_charged_probes_fit_the_ceiling(self, clock: RecoveryClock) -> None:
        """No charged probe outlasted the budget remaining when it started."""

        elapsed = 0
        for kind, cost in clock.ledger:
            if kind == "probe":
                self.assertLessEqual(cost, MAX_TRIAL_PROBE_MS)
                self.assertLessEqual(
                    cost,
                    RECOVERY_CEILING_MS - elapsed,
                    "a probe may not outlast the remaining recovery budget",
                )
            elapsed += cost
        self.assertLessEqual(elapsed, RECOVERY_CEILING_MS)

    def test_the_owner_confirmed_downstream_sequence_is_preserved(self) -> None:
        """Billing Manager, EB Bill, account, Search, and only then invoices."""
        portal, page, clock = self.route()
        with simulated_clock(clock):
            inventory = portal.inventory(20)

        self.assertEqual([bill.filename for bill in inventory], ["2026-01-01_synthetic.pdf"])
        self.assertEqual(page.events, ["billing_manager", "eb_bill", "select_account", "search"])
        self.assertFalse(
            page.rows_read_before_search,
            "an empty EB Bill surface before Search is never read as no invoices",
        )
        self.assertEqual(page.clicks, {"billing_manager": 1, "eb_bill": 1, "search": 1})
        self.assertEqual(clock.yields, [], "a settled route pays nothing for recovery")
        self.assert_probes_bounded(page)

    def test_a_delayed_billing_manager_is_recovered_and_clicked_once(self) -> None:
        portal, page, clock = self.route(billing_manager_delay=3)
        with simulated_clock(clock):
            inventory = portal.inventory(20)

        self.assertEqual(len(inventory), 1)
        self.assertEqual(page.clicks["billing_manager"], 1)
        self.assertGreater(page.looks["billing_manager"], 3, "each look resolved a fresh locator")
        self.assertTrue(clock.yields)
        self.assert_probes_bounded(page)

    def test_a_delayed_eb_bill_surface_is_recovered_and_clicked_once(self) -> None:
        portal, page, clock = self.route(eb_bill_delay=3)
        with simulated_clock(clock):
            inventory = portal.inventory(20)

        self.assertEqual(len(inventory), 1)
        self.assertEqual(page.clicks["billing_manager"], 1, "a slow EB Bill never re-opens Billing")
        self.assertEqual(page.clicks["eb_bill"], 1)
        self.assert_probes_bounded(page)

    def test_a_delayed_account_surface_settles_before_selection(self) -> None:
        portal, page, clock = self.route(account_delay=3)
        with simulated_clock(clock):
            inventory = portal.inventory(20)

        self.assertEqual(len(inventory), 1)
        self.assertEqual(page.clicks["eb_bill"], 1, "a slow account surface never re-clicks EB Bill")
        self.assertEqual(page.events.count("select_account"), 1)
        self.assert_probes_bounded(page)

    def test_a_delayed_post_search_state_is_settled_not_re_searched(self) -> None:
        portal, page, clock = self.route(post_search_delay=4)
        with simulated_clock(clock):
            inventory = portal.inventory(20)

        self.assertEqual(len(inventory), 1)
        self.assertEqual(page.clicks["search"], 1, "a slow result is never re-requested")
        self.assertTrue(clock.yields)
        self.assert_probes_bounded(page)

    def test_a_delayed_invoice_list_becomes_visible_without_re_searching(self) -> None:
        portal, page, clock = self.route(list_delay=3)
        with simulated_clock(clock):
            inventory = portal.inventory(20)

        self.assertEqual(len(inventory), 1)
        self.assertEqual(page.clicks["search"], 1)
        self.assert_probes_bounded(page)

    def test_a_delayed_pagination_advance_is_waited_for_not_re_clicked(self) -> None:
        portal, page, clock = self.route(
            pages=(("2026-01-01_a.pdf",), ("2026-02-01_b.pdf",)), advance_delay=4
        )
        with simulated_clock(clock):
            inventory = portal.inventory(20)

        self.assertEqual([bill.filename for bill in inventory], ["2026-01-01_a.pdf", "2026-02-01_b.pdf"])
        self.assertEqual(
            page.clicks["next_page"], 1, "one real click per intended page transition"
        )
        self.assertTrue(clock.yields)
        self.assert_probes_bounded(page)

    def test_a_post_search_state_that_never_settles_never_re_searches(self) -> None:
        portal, page, clock = self.route(post_search_delay=10 ** 6)
        with simulated_clock(clock):
            with self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(20)

        self.assertEqual(caught.exception.message, "invoice results are not confirmed post-search")
        self.assertEqual(page.clicks["search"], 1, "a postcondition timeout never duplicates the action")
        self.assertLessEqual(clock.elapsed_ms(), RECOVERY_CEILING_MS)

    def test_pagination_that_never_advances_never_re_clicks(self) -> None:
        portal, page, clock = self.route(
            pages=(("2026-01-01_a.pdf",), ("2026-02-01_b.pdf",)), advance_delay=10 ** 6
        )
        with simulated_clock(clock):
            with self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(20)

        self.assertEqual(caught.exception.message, "invoice pagination did not advance")
        self.assertEqual(page.clicks["next_page"], 1, "a slow transition is never re-requested")
        self.assertLessEqual(clock.elapsed_ms(), RECOVERY_CEILING_MS)

    def test_end_of_inventory_is_still_a_disabled_next_page(self) -> None:
        """A disabled Next page stays the committed end signal, not lag."""
        portal, page, clock = self.route()
        with simulated_clock(clock):
            inventory = portal.inventory(20)

        self.assertEqual(len(inventory), 1)
        self.assertEqual(page.clicks.get("next_page", 0), 0)
        self.assertEqual(clock.yields, [], "the end of inventory is read once and trusted")

    def test_a_confirmed_post_search_empty_surface_is_authoritative(self) -> None:
        portal, page, clock = self.route(pages=([],))
        with simulated_clock(clock):
            self.assertEqual(portal.inventory(20), [])
        self.assertEqual(page.clicks["search"], 1)
        self.assertFalse(page.rows_read_before_search)

    def test_a_stalled_post_search_attribute_read_is_recovered(self) -> None:
        """A marker that detaches mid-read is lag, and is re-read afresh.

        `count()` can see exactly one marker and the surface can still rerender
        before the attribute is read, which is when a Playwright attribute read
        starts waiting. That wait is bounded, so the checkpoint ends and the
        next one resolves the marker again.
        """
        portal, page, clock = self.route(state_attribute_stalls=2)
        with simulated_clock(clock):
            inventory = portal.inventory(20)

        self.assertEqual([bill.filename for bill in inventory], ["2026-01-01_synthetic.pdf"])
        self.assertEqual(page.clicks["search"], 1, "a stalled read never re-searches")
        self.assertGreaterEqual(
            page.looks["results_state"], 3, "each read resolved a fresh marker"
        )
        self.assertEqual(len(page.state_attribute_timeouts), 3)
        self.assert_state_attribute_bounded(page)
        self.assert_charged_probes_fit_the_ceiling(clock)

    def test_a_post_search_attribute_read_cannot_extend_the_shared_window(self) -> None:
        """No attribute read may inherit `page.set_default_timeout()`.

        The simulated clock charges a stalled read exactly the timeout it was
        given, so an unbounded one would be charged the 300 s page default and
        blow the ceiling on its first checkpoint instead of ending inside it.
        """
        portal, page, clock = self.route(state_attribute_stalls=10 ** 6)
        with simulated_clock(clock):
            with self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(20)

        self.assertEqual(caught.exception.message, "invoice results are not confirmed post-search")
        self.assertEqual(page.clicks["search"], 1, "a deadline is never a second Search")
        self.assertEqual(
            len(page.state_attribute_timeouts),
            len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS),
            "the marker was re-read at every checkpoint",
        )
        self.assert_state_attribute_bounded(page)
        self.assert_charged_probes_fit_the_ceiling(clock)

    def test_a_non_timeout_post_search_attribute_failure_is_terminal(self) -> None:
        """A structural read failure is not the lag this recovery waits for."""
        portal, page, clock = self.route(
            state_attribute_error=RuntimeError("synthetic structural failure")
        )
        with simulated_clock(clock):
            with self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(20)

        self.assertEqual(
            caught.exception.message, "tenant/account search result contract changed"
        )
        self.assertEqual(
            len(page.state_attribute_timeouts), 1, "a real failure is not re-probed"
        )
        self.assertEqual(page.clicks["search"], 1, "a real failure never re-searches")
        self.assertEqual(clock.yields, [], "a real failure is not waited out")

    def test_the_post_search_attribute_read_carries_its_own_timeout(self) -> None:
        """The bound is asserted on this read, not on the route in general."""
        portal, page, clock = self.route()
        with simulated_clock(clock):
            portal.inventory(20)

        self.assert_state_attribute_bounded(page)
        self.assertEqual(len(page.state_attribute_timeouts), 1, "a settled marker is read once")

    def test_a_delayed_download_control_is_dispatched_exactly_once(self) -> None:
        portal, page, clock = self.route()
        with tempfile.TemporaryDirectory() as directory:
            with simulated_clock(clock):
                inventory = portal.inventory(20)
                page.delay_download(3)
                target = Path(directory) / "download.bin"
                suggested = portal.download(inventory[0], target)

            self.assertEqual(suggested, "2026-01-01_synthetic.pdf")
            self.assertTrue(target.exists())
        self.assertEqual(page.clicks["download"], 1)
        self.assertGreater(page.looks["download"], 3, "readiness was re-resolved, not the click")
        self.assert_probes_bounded(page)

    def test_a_download_control_that_never_settles_is_never_clicked(self) -> None:
        portal, page, clock = self.route()
        with tempfile.TemporaryDirectory() as directory:
            with simulated_clock(clock):
                inventory = portal.inventory(20)
                page.delay_download(10 ** 6)
                with self.assertRaises(LayoutChangedError) as caught:
                    portal.download(inventory[0], Path(directory) / "download.bin")

        self.assertEqual(
            caught.exception.message, "invoice row download control is missing or ambiguous"
        )
        self.assertEqual(page.clicks.get("download", 0), 0)



# ---- DL-XB-141-PORTAL-RESILIENCE-002: single dispatch on the login route ---- #
#
# The pre-auth route is where a duplicated dispatch is least recoverable: a
# second semantics activation, entry click or submit can commit something the
# portal has already accepted. These cases lag one surface at a time and prove
# that recovery only ever re-checks readiness, never re-sends the action.


class PortalLoginDispatchTests(unittest.TestCase):
    """A lagging login route still dispatches each action exactly once."""

    def attempt(self, page: FakePage) -> AppError | None:
        """Run the committed `login()` against `page` on a simulated clock."""

        portal = PlaywrightPortal(FakeConfig(), headed=False)
        portal.page = page
        old = {name: os.environ.get(name) for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")}
        os.environ.update(runtime_credentials())
        try:
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

    def assert_waits_bounded(self, *locators: FakeLocator) -> None:
        """A postcondition probe never inherits the page default timeout."""

        for locator in locators:
            for value in locator.wait_timeouts:
                self.assertIsNotNone(value, "every postcondition wait carries an explicit timeout")
                self.assertNotEqual(value, 0, "a zero timeout would mean waiting forever")
                self.assertLessEqual(value, MAX_TRIAL_PROBE_MS)

    def test_a_lagging_semantics_gate_is_dispatched_exactly_once(self) -> None:
        absent = FakeLocator(count=0, label="absent_gate")
        ready = FakeLocator(label="gate")
        page = login_page(activations=[absent, absent, ready])

        self.assertIsNone(self.attempt(page))
        self.assertEqual(absent.dispatched, 0, "a gate that is not ready is never dispatched")
        self.assertEqual(ready.dispatched, 1, "exactly one activation per login attempt")
        self.assertTrue(page.waited_ms, "the lagging gate cost a bounded yield")

    def test_a_placeholder_slow_to_detach_never_re_dispatches_activation(self) -> None:
        activation = FakeLocator(label="gate")
        stuck = FakeLocator(wait_error=synthetic_timeout(), label="stuck_placeholder")
        gone = FakeLocator(label="placeholder")
        page = login_page(activation=activation, placeholders=[stuck, stuck, gone])

        self.assertIsNone(self.attempt(page))
        self.assertEqual(activation.dispatched, 1, "a slow placeholder is waited out, not re-clicked")
        self.assertEqual(stuck.waits, 2)
        self.assert_waits_bounded(stuck, gone)

    def test_a_placeholder_that_never_detaches_keeps_its_one_dispatch(self) -> None:
        activation = FakeLocator(label="gate")
        stuck = FakeLocator(wait_error=synthetic_timeout(), label="stuck_placeholder")
        page = login_page(activation=activation, placeholders=[stuck])

        error = self.attempt(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(
            cli.support_ref_for(error), "EG_LOGIN_SEMANTICS_PLACEHOLDER_REMAINS"
        )
        self.assertEqual(activation.dispatched, 1)
        self.assertEqual(
            stuck.waits,
            len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS),
            "the placeholder was re-probed at every checkpoint",
        )
        self.assert_waits_bounded(stuck)

    def test_a_lagging_login_entry_is_clicked_exactly_once(self) -> None:
        absent = FakeLocator(count=0, label="absent_entry")
        ready = FakeLocator(label="entry")
        page = login_page(entries=[absent, ready])

        self.assertIsNone(self.attempt(page))
        self.assertEqual(absent.clicks, 0)
        self.assertEqual(ready.clicks, 1, "one real entry click, after one actionability proof")
        self.assertEqual(ready.trial_clicks, 1)

    def test_lagging_credential_fields_are_each_filled_once(self) -> None:
        absent_username = FakeLocator(count=0, label="absent_username")
        username = FakeLocator(label="username")
        hidden_password = FakeLocator(visible=False, label="hidden_password")
        password = FakeLocator(label="password")
        page = login_page(
            username_fields=[absent_username, username],
            password_fields=[hidden_password, password],
        )

        self.assertIsNone(self.attempt(page))
        self.assertEqual(absent_username.fills, 0)
        self.assertEqual(username.fills, 1, "a credential value is never appended twice")
        self.assertEqual(hidden_password.fills, 0)
        self.assertEqual(password.fills, 1)

    def test_a_delayed_billing_manager_postcondition_never_re_submits(self) -> None:
        submit = FakeLocator(label="submit")
        pending = FakeLocator(wait_error=synthetic_timeout(), label="pending_billing")
        settled = FakeLocator(label="billing_manager")
        page = login_page(submits=[submit], billing_managers=[pending, pending, settled])

        self.assertIsNone(self.attempt(page))
        self.assertEqual(submit.clicks, 1, "a slow post-login surface never re-submits the login")
        self.assertEqual(pending.waits, 2)
        self.assert_waits_bounded(pending, settled)

    def test_a_billing_manager_that_never_appears_never_re_submits(self) -> None:
        submit = FakeLocator(label="submit")
        pending = FakeLocator(wait_error=synthetic_timeout(), label="pending_billing")
        page = login_page(submits=[submit], billing_managers=[pending])

        error = self.attempt(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "Billing Manager entry did not appear after login")
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_BILLING_MANAGER_WAIT_FAILED")
        self.assertEqual(submit.clicks, 1, "a postcondition timeout never duplicates the submit")
        self.assert_waits_bounded(pending)

    def test_a_settled_rejection_stops_the_post_login_window_early(self) -> None:
        """A visible alert is an answer, so the window does not run its course.

        Waiting the whole minute out could not change the classification and
        would delay every rejected credential run by that minute.
        """
        submit = FakeLocator(label="submit")
        never = FakeLocator(count=0, label="never_billing")
        page = login_page(submits=[submit], billing_managers=[never], alert_visible=True)

        error = self.attempt(page)
        self.assertIsInstance(error, LoginError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_PORTAL_REJECTED")
        self.assertEqual(submit.clicks, 1, "a rejection is never re-submitted")
        self.assertEqual(page.waited_ms, [], "a settled rejection is not waited out")
        self.assertEqual(never.waits, 0)

    def test_an_ambiguous_post_login_surface_never_re_submits(self) -> None:
        """Two Billing Manager entries are re-checked, never acted on."""
        submit = FakeLocator(label="submit")
        ambiguous = FakeLocator(count=2, label="ambiguous_billing")
        page = login_page(submits=[submit], billing_managers=[ambiguous])

        error = self.attempt(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_BILLING_MANAGER_WAIT_FAILED")
        self.assertEqual(submit.clicks, 1)
        self.assertEqual(ambiguous.waits, 0, "an ambiguous surface is never asked to wait")


if __name__ == "__main__":
    unittest.main()
