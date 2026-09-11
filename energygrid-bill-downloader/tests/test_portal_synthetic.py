from __future__ import annotations

import contextlib
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse
import uuid

from energygrid_bill_downloader import cli
from energygrid_bill_downloader import portal as portal_module
from energygrid_bill_downloader.config import load_runtime_config
from energygrid_bill_downloader.cli import main
from energygrid_bill_downloader.errors import (
    ACTION_REQUIRED,
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


    # ---- DL-XB-141-EMS-ENTRY-MINIMAL-REPAIR-G2-136 ---- #

    def test_login_lands_on_a_surface_with_no_business_navigation(self) -> None:
        """The corrected topology: EMS only, and no EMS actuation from login."""
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer() as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    page = portal.page
                    self.assertEqual(
                        page.get_by_role("button", name="EMS", exact=True).count(), 1
                    )
                    self.assertEqual(
                        page.get_by_role("link", name="Billing Manager", exact=True).count(),
                        0,
                    )
                    self.assertEqual(
                        page.get_by_role("link", name="EB Bill", exact=True).count(), 0
                    )
                    self.assertEqual(
                        server.ems_actuation_count,
                        0,
                        "proving a landing never enters the application",
                    )
            finally:
                self.restore_credentials(old)

    def test_inventory_enters_the_application_once_and_download_adds_none(self) -> None:
        bills = [
            SyntheticBill("2026-10-01_SYNTHETIC-A.pdf"),
            SyntheticBill("2026-10-02_SYNTHETIC-B.pdf"),
            SyntheticBill("2026-10-03_SYNTHETIC-C.pdf"),
        ]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            bills, page_size=2
        ) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    portal.login()
                    self.assertEqual(server.ems_actuation_count, 0)
                    inventory = portal.inventory(20)
                    self.assertEqual(
                        server.ems_actuation_count, 1, "exactly one application entry"
                    )
                    target = root / "download.bin"
                    portal.download(inventory[-1], target)
                    self.assertEqual(
                        server.ems_actuation_count,
                        1,
                        "a restored results address re-enters nothing",
                    )
                    self.assertEqual(target.read_bytes(), bills[-1].payload)
            finally:
                self.restore_credentials(old)

    def test_the_login_diagnostic_never_actuates_ems(self) -> None:
        """The diagnostic observes a landing; it never enters the application."""
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer() as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal:
                    result = portal.login_diagnostic()
                    self.assertTrue(result.submit_dispatched)
            finally:
                self.restore_credentials(old)
            self.assertEqual(server.ems_actuation_count, 0)

    def test_a_disabled_ems_authenticates_but_never_enters_the_application(self) -> None:
        """Authentication reads a count and a visibility; the entry needs more."""
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [SyntheticBill("2026-10-04_SYNTHETIC-DISABLED.pdf")], variant="disabled_ems"
        ) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal, fast_portal_recovery():
                    portal.login()
                    with self.assertRaises(LayoutChangedError) as caught:
                        portal.inventory(20)
            finally:
                self.restore_credentials(old)
            self.assertEqual(
                caught.exception.message, "EMS application entry control is not ready"
            )
            self.assertEqual(
                cli.support_ref_for(caught.exception), "EG_NAV_EMS_ENTRY_NOT_READY"
            )
            self.assertEqual(server.ems_actuation_count, 0)

    def test_an_inert_ems_entry_fails_downstream_after_exactly_one_actuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [SyntheticBill("2026-10-05_SYNTHETIC-INERT.pdf")], variant="inert_ems"
        ) as server:
            root = Path(directory)
            config = self.config_for(server, root)
            old, _values = self.with_credentials()
            try:
                with PlaywrightPortal(config) as portal, fast_portal_recovery():
                    portal.login()
                    with self.assertRaises(LayoutChangedError) as caught:
                        portal.inventory(20)
            finally:
                self.restore_credentials(old)
            self.assertEqual(
                caught.exception.message,
                "Billing Manager navigation control is not ready",
            )
            self.assertEqual(
                server.ems_actuation_count, 1, "a consumed entry is never re-attempted"
            )

    def test_downstream_navigation_gaps_still_follow_one_ems_actuation(self) -> None:
        """`missing_billing_manager` and `missing_eb_bill` stay downstream failures."""
        for variant, message in (
            ("missing_billing_manager", "Billing Manager navigation control is not ready"),
            ("missing_eb_bill", "EB Bill navigation control is not ready"),
        ):
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
                    [SyntheticBill("2026-10-06_SYNTHETIC-GAP.pdf")], variant=variant
                ) as server:
                    root = Path(directory)
                    config = self.config_for(server, root)
                    old, _values = self.with_credentials()
                    try:
                        with PlaywrightPortal(config) as portal, fast_portal_recovery():
                            portal.login()
                            with self.assertRaises(LayoutChangedError) as caught:
                                portal.inventory(20)
                    finally:
                        self.restore_credentials(old)
                    self.assertEqual(caught.exception.message, message)
                    self.assertEqual(server.ems_actuation_count, 1)


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
        type_error: Exception | None = None,
        focused: bool = True,
        focus_after_looks: int = 0,
        focus_error: Exception | None = None,
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
        self._type_error = type_error
        self._focused = focused
        self._focus_after_looks = focus_after_looks
        self._focus_error = focus_error
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
        self.typed = 0
        self.type_delays: list[int | None] = []
        self.focus_checks = 0
        self.evaluate_timeouts: list[int | None] = []
        # Whether the last focus look proved this locator owns focus. Only ever
        # a boolean: the fake is handed no credential value either.
        self._focus_proven = False
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

    def evaluate(self, expression: str, timeout: int | None = None) -> bool:
        """Model the boolean-only focus predicate the credential gate evaluates.

        The production predicate compares node identity against the document's
        active element and returns nothing but a boolean, so this returns a
        boolean and is handed no value to inspect. `focus_after_looks` models an
        editing host that takes focus a little after the click rather than
        instantly; `focused=False` models one that never takes it at all.

        A focus probe that stalls is charged its whole explicit budget, exactly
        like the enabled and trial probes, so an unbounded one cannot be free.
        """
        self.focus_checks += 1
        self.evaluate_timeouts.append(timeout)
        self._record("evaluate")
        if self._focus_error is not None:
            if self._clock is not None:
                spent = timeout if timeout is not None else self._clock.default_timeout_ms
                self._clock.charge_probe_ms(spent)
            raise self._focus_error
        self._focus_proven = self._focused and self.focus_checks > self._focus_after_looks
        return self._focus_proven

    def press_sequentially(self, value: str, delay: int | None = None) -> None:
        """Model typed credential entry: real key events, not value assignment.

        The production path types because the portal's editing host ignores an
        assigned value. Counting the typed entries separately keeps "entered
        once" assertable without ever holding the credential itself.
        """
        self.typed += 1
        self.type_delays.append(delay)
        self._record("press_sequentially")
        if self._type_error is not None:
            raise self._type_error


class FocusHostField(FakeLocator):
    """A credential field whose editing host loses keys sent before it focuses.

    This is the Run123 regression model, expressed in booleans only. The owner
    observed unattended password entry appearing to receive one fewer character
    than expected; a host like this one is what that looks like -- an entry that
    begins before focus is owned loses its leading key event. The value itself
    is never held, measured or compared; only whether the entry began focused.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.complete_entries = 0
        self.leading_key_dropped = False

    def press_sequentially(self, value: str, delay: int | None = None) -> None:
        started_focused = self._focus_proven
        super().press_sequentially(value, delay)
        if started_focused:
            self.complete_entries += 1
        else:
            self.leading_key_dropped = True


class FakePage:
    """A page whose only job is to hand the login path the locators under test.

    The portal resolves the semantics-gate Login entry and the login submit
    control through the same role and name, so this page hands the first such
    lookup to `login_entry` and any later one to `submit`. That mirrors the
    committed sequence while still letting a click failure be attributed to the
    step that made it. Every optional slot defaults to a healthy locator.

    The one real submit click also opens the landing stage
    (DL-XB-141-AUTH-LANDING-NAV-SEPARATION-G2-001). A healthy landing serves
    exactly one visible `EMS` control and reports every retained login witness
    absent by an exact zero count, which is what a clean authenticated landing
    is. `landing=False` models a landing that never renders the authentication
    witness, and `retained_login=True` models a login route that survives the
    submit -- the contradiction that must never authenticate.
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
        ems: FakeLocator | None = None,
        emss: list[FakeLocator] | None = None,
        rejection: FakeLocator | None = None,
        landing: bool = True,
        retained_login: bool = False,
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
        self.ems = ems
        self.emss = emss
        # An explicit strict rejection witness, so a case can model a rejection
        # reader that FAILS rather than one that reports absence.
        self.rejection = rejection
        self.landing = landing
        self.retained_login = retained_login
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
        # The landing stage opens at the one real submit dispatch, so nothing
        # before that boundary can be read as post-submit evidence.
        self.landing_phase = False
        self.entry_lookups = 0
        self.submit_lookups = 0
        self.landing_lookups = 0
        self.ems_lookups = 0
        self._sequence_lookups: dict[str, int] = {}
        # What `page.set_default_timeout()` would have installed. An
        # unbounded call inherits it, so it is what a missing explicit
        # timeout costs.
        self.default_timeout_ms = default_timeout_ms
        # Only ever compared with itself: the diagnostic reports whether the
        # address moved and never what either address was.
        self.url = "http://127.0.0.1:1/synthetic"
        self.waited_ms: list[int] = []
        # Every simulated cost in the order it was incurred, so a case can
        # prove a probe never outlasted the budget remaining at that moment.
        self.ledger: list[tuple[str, int]] = []
        for sequence in (
            self.submits,
            self.emss,
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

    def _end_submit_stage(self) -> None:
        self.landing_phase = True

    @staticmethod
    def _absent() -> FakeLocator:
        """A control the surface positively does not have: an exact zero count."""
        return FakeLocator(count=0, visible=False)

    def _landing_absent(self) -> bool:
        """Whether a retained login witness is gone, as the landing stage requires."""
        return self.landing_phase and not self.retained_login

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        if name == "EMS":
            # The authentication witness. It exists only on the landing, so a
            # look before the one submit dispatch is an exact zero count.
            self.ems_lookups += 1
            if not self.landing_phase:
                return self._absent()
            if self.emss is not None:
                return self._from_sequence("ems", self.emss)
            if self.ems is not None:
                return self.ems
            return FakeLocator() if self.landing else self._absent()
        if name == "Enable accessibility":
            if self._landing_absent():
                return self._absent()
            if self.activations is not None:
                return self._from_sequence("activation", self.activations)
            return self.activation
        if name == "Login":
            self.login_lookups += 1
            if self._landing_absent():
                # The login route is gone, which is what the landing requires.
                self.landing_lookups += 1
                return self._absent()
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
            locator = self.login_entry
            if self.submits is not None:
                locator = self._from_sequence("submit", self.submits)
            elif self.submit is not None:
                locator = self.submit
            # The one real submit click is what opens the landing stage.
            locator._on_click_hook = self._end_submit_stage
            return locator
        if role == "alert":
            self.alert_lookups += 1
            if self.rejection is not None:
                return self.rejection
            # A surface with no rejection reports an exact zero count, so its
            # absence is positively readable rather than merely invisible.
            return FakeLocator(count=1 if self.alert_visible else 0, visible=self.alert_visible)
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
        if self._landing_absent():
            # The credential route is gone on a clean authenticated landing.
            return self._absent()
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


def login_page(*, page_cls: type[FakePage] = FakePage, **overrides) -> FakePage:
    """A page whose login path succeeds unless `overrides` break exactly one step.

    `page_cls` lets a case supply a `FakePage` subclass -- a page whose
    event-loop yield fails, for instance -- without restating the healthy
    login surface every other case relies on.
    """
    slots = {"activation": FakeLocator(), "placeholder": FakeLocator(), "login_entry": FakeLocator()}
    for name in tuple(slots):
        if name in overrides:
            slots[name] = overrides.pop(name)
    return page_cls(slots["activation"], slots["placeholder"], slots["login_entry"], **overrides)


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
        lambda exc: {"username_field": FakeLocator(type_error=exc)},
        "EG_LOGIN_USERNAME_FILL_FAILED",
    ),
    (
        "password_fill",
        lambda exc: {"password_field": FakeLocator(type_error=exc)},
        "EG_LOGIN_PASSWORD_FILL_FAILED",
    ),
    (
        "login_submit",
        lambda exc: {"submit": FakeLocator(click_error=exc)},
        "EG_LOGIN_SUBMIT_DISPATCH_UNCERTAIN",
    ),
    # Billing Manager is no longer an authentication oracle. What ends the
    # login sequence now is positive authentication proof, so the step that can
    # fail here is the authenticated landing, and it carries its own reference.
    (
        "authenticated_landing",
        lambda exc: {"landing": False},
        "EG_LOGIN_AUTHENTICATION_UNPROVED",
    ),
)

# The committed order of portal interactions for one successful login attempt.
# Every control that is about to be clicked for real is proven actionable by
# one trial click first; readiness itself is proven by current-state reads that
# dispatch nothing. A healthy portal is ready at the immediate checkpoint, so
# every non-credential surface costs no extra resolution and no event-loop
# yield.
#
# Each credential field is the one exception, and deliberately so: one click,
# then one boolean focus proof at the committed settle floor, then one typed
# entry. The focus gate declines the ladder's immediate look, so a healthy
# field still pays exactly one bounded yield -- see `CREDENTIAL_FOCUS_YIELDS`.
SUCCESSFUL_LOGIN_SEQUENCE = [
    "page:goto",
    "activation:dispatch_event",
    "placeholder:wait_for",
    "login_entry:click_trial",
    "login_entry:click",
    "username:click",
    "page:wait_for_timeout",
    "username:evaluate",
    "username:press_sequentially",
    "password:click",
    "page:wait_for_timeout",
    "password:evaluate",
    "password:press_sequentially",
    "submit:click_trial",
    "submit:click",
]
# Nothing follows the one submit click. The authentication proof that ends
# `login()` is pure observation: it resolves fresh exact locators and reads
# counts and visibility, so it dispatches nothing at all and therefore adds no
# interaction to the committed sequence.

# What one healthy login now spends before any other surface can lag: the
# credential focus gate declines the immediate look, so each of the two fields
# yields once at the committed floor before its focus is proven. Cases that
# assert "nothing else was waited out" compare against this rather than an
# empty list.
CREDENTIAL_FOCUS_YIELDS = [portal_module.LOGIN_FOCUS_SETTLE_FLOOR_MS] * 2


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
            for name in ("activation", "placeholder", "login_entry", "submit", "username", "password")
        }
        page = FakePage(
            locators["activation"],
            locators["placeholder"],
            locators["login_entry"],
            submit=locators["submit"],
            username_field=locators["username"],
            password_field=locators["password"],
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
        self.assertEqual(page.entry_lookups, 1, "the entry control resolves as before")
        self.assertEqual(page.submit_lookups, 1, "the submit control resolves as before")
        self.assertEqual(locators["activation"].dispatched, 1)
        self.assertEqual(locators["login_entry"].clicks, 1)
        self.assertEqual(locators["login_entry"].trial_clicks, 1)
        self.assertEqual(locators["submit"].clicks, 1)
        self.assertEqual(locators["submit"].trial_clicks, 1)
        self.assertEqual(locators["username"].typed, 1)
        self.assertEqual(locators["password"].typed, 1)
        # The landing is proven from the authentication witness alone, and the
        # retained login route is proven gone by an exact zero count.
        # Two looks: the count question and the visibility question each
        # resolve a fresh exact locator, as everywhere else on this ladder.
        self.assertEqual(page.ems_lookups, 2)
        self.assertGreaterEqual(page.landing_lookups, 1)
        # Each credential field is focused exactly once and its focus proven
        # exactly once, and neither proof is read before the settle floor.
        self.assertEqual(locators["username"].clicks, 1)
        self.assertEqual(locators["password"].clicks, 1)
        self.assertEqual(locators["username"].focus_checks, 1)
        self.assertEqual(locators["password"].focus_checks, 1)
        # A healthy non-credential control needs no recovery, so the only cost
        # is the committed post-click focus floor, once per credential field.
        self.assertEqual(page.waited_ms, CREDENTIAL_FOCUS_YIELDS)
        # The strict rejection witness IS part of the authentication proof, so a
        # success reads it exactly once and proves its absence by an exact zero
        # count. It never takes the rejection classification arm.
        self.assertEqual(page.alert_lookups, 1)

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

        # The login half of the vocabulary. Business navigation is a separate
        # contract with its own declared references, reachable only from
        # `_open_verified_results()`, and is proven complete by
        # `BusinessNavigationReferenceTests` instead.
        live = (
            set(cli.SUPPORT_REFS_BY_MESSAGE.values())
            - cli.RETIRED_SUPPORT_REFS
            - cli.NAVIGATION_SUPPORT_REFS
        )
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

# The committed ladder, as the elapsed yields one exhausted recovery window
# spends. A navigation that has to reach the outer application pays exactly this
# once, in `_settle_eb_bill_entry()`: a direct or restored EB Bill entry that is
# still rendering must be given the committed window before the run may conclude
# it does not exist. It is one window on the shared ladder, spent at most once
# per navigation, and no downstream surface pays anything for it.
ENTRY_SETTLE_YIELDS = [250, 750, 4000, 5000, 20000, 28000]
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
        self.assertEqual(page.waited_ms, CREDENTIAL_FOCUS_YIELDS)
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
        self.assertEqual(
            page.waited_ms,
            CREDENTIAL_FOCUS_YIELDS,
            "a probe that already elapsed past a checkpoint adds no sleep",
        )
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
        self.assertEqual(
            page.waited_ms,
            CREDENTIAL_FOCUS_YIELDS,
            "no yield follows a terminal readiness failure",
        )
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
        href: str | None = None,
        click_error: Exception | None = None,
        matches: int = 1,
        state_error: Exception | None = None,
    ) -> None:
        self._page = page
        self._key = key
        self._present = present
        # How many exact matches this resolution reports, and a state read that
        # cannot answer at all. Ambiguity and unreadability are what a routing
        # decision must never be derived from, so they are first-class knobs.
        self._matches = matches
        self._state_error = state_error
        self._visible = visible
        self._enabled = enabled
        self._disabled = disabled
        self._actionable = actionable
        self._on_click = on_click
        # A navigation link's own target, which is what route proof compares
        # the current address against. `None` models a link with no target.
        self._href = href
        self._click_error = click_error

    def count(self) -> int:
        if self._state_error is not None:
            raise self._state_error
        return self._matches if self._present else 0

    def is_visible(self, timeout: int | None = None) -> bool:
        if self._state_error is not None:
            raise self._state_error
        return self._visible

    def is_enabled(self, timeout: int | None = None) -> bool:
        self._page.probe_timeouts.append(timeout)
        return self._enabled

    def is_disabled(self, timeout: int | None = None) -> bool:
        self._page.probe_timeouts.append(timeout)
        return self._disabled

    def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        assert name == "href", name
        self._page.probe_timeouts.append(timeout)
        self._page.href_reads.append(self._key)
        if self._page.href_read_error is not None:
            raise self._page.href_read_error
        return self._href

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
        if self._click_error is not None:
            # A dispatch whose outcome cannot be established. It may well have
            # landed, which is exactly why it must never be sent again.
            raise self._click_error
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
        start_route: str = "landing",
        ems_present: bool = True,
        ems_delay: int = 0,
        ems_visible_delay: int = 0,
        ems_actionable: bool = True,
        ems_actionable_delay: int = 0,
        ems_matches: int = 1,
        ems_state_error: Exception | None = None,
        ems_click_error: Exception | None = None,
        ems_click_inert: bool = False,
        eb_bill_href: str = "/eb-bill",
        eb_bill_actionable: bool = True,
        eb_bill_on_app: bool = False,
        eb_bill_visible_delay: int = 0,
        eb_bill_actionable_delay: int = 0,
        eb_bill_matches: int = 1,
        eb_bill_state_error: Exception | None = None,
        eb_bill_click_error: Exception | None = None,
        eb_bill_click_inert: bool = False,
        billing_manager_actionable: bool = True,
        billing_manager_click_error: Exception | None = None,
        href_read_error: Exception | None = None,
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
        # The authenticated landing is where a run now starts, and it carries no
        # business navigation at all. These knobs vary the EMS application entry
        # exactly as the EB Bill knobs vary the entry after it: whether it
        # exists, when it becomes usable, whether it is ambiguous or unreadable,
        # and how its one real click ends.
        self.ems_present = ems_present
        self.ems_delay = ems_delay
        self.ems_visible_delay = ems_visible_delay
        self.ems_actionable = ems_actionable
        self.ems_actionable_delay = ems_actionable_delay
        self.ems_matches = ems_matches
        self.ems_state_error = ems_state_error
        self.ems_click_error = ems_click_error
        # A click that lands and opens nothing. The entry is consumed and the
        # surface stays put, which is a distinct outcome from a click that
        # raised and from one that was never ready.
        self.ems_click_inert = ems_click_inert
        self.eb_bill_href = eb_bill_href
        self.eb_bill_actionable = eb_bill_actionable
        # A surface that offers BOTH entries: the outer Billing Manager and a
        # direct EB Bill on the same route. That is the shape a settling direct
        # entry has, and the only shape on which choosing the outer path can be
        # observed as a mis-route rather than as the only option.
        self.eb_bill_on_app = eb_bill_on_app
        # Existence settles before usability. Each of these keeps the count at
        # exactly one while the control is still hidden, or still refuses a
        # trial action, for that many fresh looks.
        self.eb_bill_visible_delay = eb_bill_visible_delay
        self.eb_bill_actionable_delay = eb_bill_actionable_delay
        self.eb_bill_matches = eb_bill_matches
        self.eb_bill_state_error = eb_bill_state_error
        self.eb_bill_click_error = eb_bill_click_error
        # A click that lands and changes nothing: the postcondition route never
        # becomes proven, which is a distinct failure from a click that raised.
        self.eb_bill_click_inert = eb_bill_click_inert
        self.billing_manager_actionable = billing_manager_actionable
        self.billing_manager_click_error = billing_manager_click_error
        self.href_read_error = href_read_error
        self.href_reads: list[str] = []
        self.route = start_route
        # Real EMS actuations, counted wherever they happen. Downstream routes
        # keep the control, so a second one would be visible here rather than
        # silently absorbed.
        self.ems_actuations = 0
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

    def _actuate_ems(self) -> None:
        """Consume one real EMS actuation, from whichever surface sent it."""
        self.ems_actuations += 1
        if self.ems_click_inert:
            return
        # Only the landing has anywhere to go. On application chrome the
        # control is still there and still counts, and still opens nothing.
        if self.route == "landing":
            self.route = "app"

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

    # Each route has its own address, and only the EB Bill results route
    # carries query parameters -- which is what route proof must ignore.
    ROUTE_PATHS = {
        # The authenticated landing. It is not the business surface: the EMS
        # application entry is what reaches the outer application route below.
        "landing": "/landing",
        "app": "/app",
        "billing": "/billing",
        # A Tenant-Bill-like route that also renders a tenant/account selector.
        # Its selector is exactly the evidence the retired shortcut mistook for
        # proof that EB Bill was already active.
        "tenant_bill": "/tenant-bill",
        "results": "/eb-bill",
    }

    @property
    def url(self) -> str:
        path = self.ROUTE_PATHS[self.route]
        if self.route != "results":
            return "http://synthetic.invalid" + path
        return (
            "http://synthetic.invalid"
            + path
            + f"?page={self.page_index + 1}&account={self.account}&searched=1#invoices"
        )

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.events.append("goto")
        self.route = "results"
        self.searched = False
        self.post_search_pending = 0
        for key in ("account", "invoice-list", "search", "eb_bill"):
            self.reset_looks(key)
        index = int(parse_qs(urlparse(url).query).get("page", ["1"])[0]) - 1
        self.page_index = max(0, min(index, len(self.pages) - 1))

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.clock.charge_yield(milliseconds)

    def get_by_label(self, name: str, exact: bool = False):
        assert name == "Tenant/account", name
        looks = self.bump("account")
        # The selector renders on more than one route, which is why its presence
        # was never proof of the EB Bill route.
        return _AccountControl(
            self,
            self.route in ("results", "tenant_bill") and looks > self.account_delay,
        )

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        if name == "EMS":
            looks = self.bump("ems")
            # Present on every authenticated surface, exactly as the observed
            # application chrome keeps it, so a second actuation from anywhere
            # would be counted rather than fail as a missing control.
            return _ResultsControl(
                self,
                "ems",
                present=self.ems_present and looks > self.ems_delay,
                visible=looks > self.ems_visible_delay,
                actionable=(
                    self.ems_actionable and looks > self.ems_actionable_delay
                ),
                click_error=self.ems_click_error,
                matches=self.ems_matches,
                state_error=self.ems_state_error,
                on_click=self._actuate_ems,
            )
        if name == "Billing Manager":
            looks = self.bump("billing_manager")
            return _ResultsControl(
                self,
                "billing_manager",
                present=self.route == "app" and looks > self.billing_manager_delay,
                actionable=self.billing_manager_actionable,
                click_error=self.billing_manager_click_error,
                href="/billing",
                on_click=self._open_billing,
            )
        if name == "EB Bill":
            looks = self.bump("eb_bill")
            return _ResultsControl(
                self,
                "eb_bill",
                # The EB Bill nav entry lives on every route that has one,
                # including the results route itself, so route identity can be
                # proven from the control's own target. `eb_bill_on_app` adds it
                # to the outer application route as well, which is where a
                # direct entry and the Billing Manager entry coexist.
                present=(
                    self.route in ("billing", "tenant_bill", "results")
                    or (self.eb_bill_on_app and self.route == "app")
                )
                and looks > self.eb_bill_delay,
                visible=looks > self.eb_bill_visible_delay,
                actionable=(
                    self.eb_bill_actionable and looks > self.eb_bill_actionable_delay
                ),
                click_error=self.eb_bill_click_error,
                href=self.eb_bill_href,
                on_click=None if self.eb_bill_click_inert else self._open_results,
                matches=self.eb_bill_matches,
                state_error=self.eb_bill_state_error,
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
        self.assertEqual(
            page.events,
            ["ems", "billing_manager", "eb_bill", "select_account", "search"],
        )
        self.assertFalse(
            page.rows_read_before_search,
            "an empty EB Bill surface before Search is never read as no invoices",
        )
        self.assertEqual(
            page.clicks,
            {"ems": 1, "billing_manager": 1, "eb_bill": 1, "search": 1},
        )
        self.assertEqual(page.ems_actuations, 1, "the application is entered once")
        self.assertEqual(
            clock.yields,
            ENTRY_SETTLE_YIELDS,
            "only the bounded entry settle pays; every settled surface after it is free",
        )
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
        portal, page, clock = self.route(start_route="results", post_search_delay=10 ** 6)
        with simulated_clock(clock):
            with self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(20)

        self.assertEqual(caught.exception.message, "invoice results are not confirmed post-search")
        self.assertEqual(page.clicks["search"], 1, "a postcondition timeout never duplicates the action")
        self.assertLessEqual(clock.elapsed_ms(), RECOVERY_CEILING_MS)

    def test_pagination_that_never_advances_never_re_clicks(self) -> None:
        portal, page, clock = self.route(
            start_route="results",
            pages=(("2026-01-01_a.pdf",), ("2026-02-01_b.pdf",)),
            advance_delay=10 ** 6,
        )
        with simulated_clock(clock):
            with self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(20)

        self.assertEqual(caught.exception.message, "invoice pagination did not advance")
        self.assertEqual(page.clicks["next_page"], 1, "a slow transition is never re-requested")
        self.assertLessEqual(clock.elapsed_ms(), RECOVERY_CEILING_MS)

    def test_end_of_inventory_is_still_a_disabled_next_page(self) -> None:
        """A disabled Next page stays the committed end signal, not lag."""
        portal, page, clock = self.route(start_route="results")
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
        portal, page, clock = self.route(
            start_route="results", state_attribute_stalls=10 ** 6
        )
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
            start_route="results",
            state_attribute_error=RuntimeError("synthetic structural failure"),
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



# ---- DL-XB-141-AUTH-LANDING-NAV-SEPARATION-G2-001: business navigation ---- #
#
# Contract B. `_open_verified_results()` owns the Billing Manager / EB Bill
# route, and route identity is PROVEN rather than inferred. The retired
# shortcut read the presence of the tenant/account selector as proof that EB
# Bill was already active; that selector renders on more than one route, so it
# never proved anything of the kind. These cases drive the real route against
# the settling fake and assert what was dispatched, what was not, and which
# failure each surface produces.


class BusinessNavigationTests(unittest.TestCase):
    """The route is proven, the clicks are one-shot, and the failures are distinct."""

    def route(self, **kwargs):
        clock = RecoveryClock()
        page = FakeResultsPage(clock=clock, **kwargs)
        portal = PlaywrightPortal(ResultsConfig(), headed=False)
        portal.page = page
        return portal, page, clock

    def open_route(self, **kwargs):
        """Run the real navigation contract and return what it did."""
        portal, page, clock = self.route(**kwargs)
        error = None
        with simulated_clock(clock):
            try:
                portal._open_eb_bill_route(page)
            except AppError as exc:
                error = exc
        return page, error

    # ---- N01-N04: how few clicks each starting route needs ---- #

    def test_a_tenant_account_selector_never_skips_eb_bill(self) -> None:
        """N01. The retired shortcut's exact evidence, on a Tenant-Bill route.

        The selector is present and resolvable, and the route is NOT EB Bill.
        The old inference stopped here; the proven contract navigates.
        """
        page, error = self.open_route(start_route="tenant_bill")
        self.assertIsNone(error)
        self.assertEqual(page.get_by_label("Tenant/account").count(), 1)
        self.assertEqual(page.route, "results")
        self.assertEqual(page.clicks, {"eb_bill": 1}, "one EB Bill click, and no more")
        self.assertNotIn("billing_manager", page.clicks)

    def test_an_already_proven_route_dispatches_nothing(self) -> None:
        """N02. Route proof is the only thing that may skip a navigation click."""
        page, error = self.open_route(start_route="results")
        self.assertIsNone(error)
        self.assertEqual(page.clicks, {})
        self.assertEqual(page.events, [])
        self.assertTrue(page.href_reads, "the route was proven, not assumed")

    def test_direct_eb_bill_availability_needs_no_billing_manager_click(self) -> None:
        """N03. At most one EB Bill click, and never a Billing Manager click."""
        page, error = self.open_route(start_route="billing")
        self.assertIsNone(error)
        self.assertEqual(page.clicks, {"eb_bill": 1})
        self.assertEqual(page.route, "results")

    def test_the_outer_app_route_dispatches_each_control_once(self) -> None:
        """N04. Billing Manager then EB Bill, each exactly once."""
        page, error = self.open_route(start_route="app")
        self.assertIsNone(error)
        self.assertEqual(page.events, ["billing_manager", "eb_bill"])
        self.assertEqual(page.clicks, {"billing_manager": 1, "eb_bill": 1})

    # ---- N05-N09: the three failure classes, per control ---- #

    def test_a_billing_manager_readiness_failure_is_distinct(self) -> None:
        """N05. It fails before any dispatch, with its own reference."""
        page, error = self.open_route(start_route="app", billing_manager_actionable=False)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "Billing Manager navigation control is not ready")
        self.assertEqual(
            cli.support_ref_for(error), "EG_NAV_BILLING_MANAGER_NOT_READY"
        )
        self.assertEqual(page.clicks.get("billing_manager", 0), 0)

    def test_a_billing_manager_click_exception_is_uncertain_and_terminal(self) -> None:
        """N06. It may have landed, so it is never sent again."""
        page, error = self.open_route(
            start_route="app",
            billing_manager_click_error=RuntimeError(STEP_FAILURE_TEXT),
        )
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(
            error.message, "Billing Manager navigation dispatch outcome uncertain"
        )
        self.assertEqual(
            cli.support_ref_for(error), "EG_NAV_BILLING_MANAGER_DISPATCH_UNCERTAIN"
        )
        self.assertEqual(page.clicks["billing_manager"], 1, "never retried")
        self.assertNotIn("hunter2", error.message)

    def test_an_eb_bill_readiness_failure_is_distinct(self) -> None:
        """N07. Readiness is proven before the one dispatch.

        The outer route, so the EB Bill entry reached after the one Billing
        Manager click is unambiguously the dispatch target: it never becomes
        actionable, and it is never clicked. A never-usable DIRECT entry is a
        different case, and belongs to the entry decision rather than to this
        dispatch (N16).
        """
        page, error = self.open_route(start_route="app", eb_bill_actionable=False)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "EB Bill navigation control is not ready")
        self.assertEqual(cli.support_ref_for(error), "EG_NAV_EB_BILL_NOT_READY")
        self.assertEqual(page.clicks.get("eb_bill", 0), 0)
        self.assertEqual(page.clicks["billing_manager"], 1)

    def test_an_eb_bill_click_exception_is_uncertain_and_terminal(self) -> None:
        """N08. An uncertain EB Bill dispatch is terminal, with no retry."""
        page, error = self.open_route(
            start_route="billing", eb_bill_click_error=RuntimeError(STEP_FAILURE_TEXT)
        )
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "EB Bill navigation dispatch outcome uncertain")
        self.assertEqual(
            cli.support_ref_for(error), "EG_NAV_EB_BILL_DISPATCH_UNCERTAIN"
        )
        self.assertEqual(page.clicks["eb_bill"], 1, "never retried")
        self.assertNotIn("portal.example.invalid", error.message)

    def test_a_dispatched_click_without_route_proof_fails_closed(self) -> None:
        """N09. The click landed and the route never became proven."""
        page, error = self.open_route(start_route="billing", eb_bill_click_inert=True)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "EB Bill results route was not proven")
        self.assertEqual(cli.support_ref_for(error), "EG_NAV_RESULTS_ROUTE_UNPROVED")
        self.assertEqual(page.clicks["eb_bill"], 1, "a missing postcondition never re-clicks")

    def test_the_three_navigation_failure_classes_are_all_distinct(self) -> None:
        """One control, three outcomes, three references."""
        references = set()
        for kwargs in (
            {"start_route": "app", "eb_bill_actionable": False},
            {"start_route": "billing", "eb_bill_click_error": synthetic_timeout()},
            {"start_route": "billing", "eb_bill_click_inert": True},
        ):
            _page, error = self.open_route(**kwargs)
            references.add(cli.support_ref_for(error))
        self.assertEqual(len(references), 3)

    # ---- N10-N13: the selector, the proof, and its privacy ---- #

    def test_no_generic_or_narrowed_navigation_selector_exists(self) -> None:
        """N10. Exact role and name, never `.first`, never generic text."""
        seen: list[tuple] = []

        class SelectorRecorder(FakeResultsPage):
            def get_by_role(self, role, name=None, exact=False):
                seen.append((role, name, exact))
                return super().get_by_role(role, name=name, exact=exact)

            def get_by_text(self, *args, **kwargs):
                raise AssertionError("navigation never resolves a control by text")

        clock = RecoveryClock()
        page = SelectorRecorder(clock=clock, start_route="app")
        portal = PlaywrightPortal(ResultsConfig(), headed=False)
        portal.page = page
        with simulated_clock(clock):
            portal._open_eb_bill_route(page)
        for role, name, exact in seen:
            with self.subTest(control=name):
                self.assertIn(name, ("Billing Manager", "EB Bill"))
                self.assertTrue(exact, "an exact name match is never weakened")
        source = pathlib_read_portal_source()
        navigation = source[source.index("def _open_eb_bill_route") : source.index("def _await_post_search_state")]
        self.assertNotIn(".first", navigation)
        self.assertNotIn("get_by_text", navigation)

    def test_a_saved_results_route_restores_without_a_navigation_click(self) -> None:
        """N11. The saved verified-results address is recognised as the route."""
        portal, page, clock = self.route(pages=(("a.pdf", "b.pdf"), ("c.pdf",)))
        with tempfile.TemporaryDirectory() as directory:
            with simulated_clock(clock):
                inventory = portal.inventory(20)
                before = dict(page.clicks)
                portal.download(inventory[0], Path(directory) / "download.bin")
        self.assertEqual(
            page.clicks.get("billing_manager", 0),
            before.get("billing_manager", 0),
            "restoration re-enters no Billing Manager",
        )
        self.assertEqual(
            page.clicks.get("eb_bill", 0),
            before.get("eb_bill", 0),
            "restoration re-enters no EB Bill",
        )
        self.assertIn("goto", page.events)

    def test_route_proof_ignores_only_query_and_fragment(self) -> None:
        """N12. Query and fragment are ignored; nothing else is."""
        portal, page, _clock = self.route(start_route="results")
        # The results address carries page, account, searched and a fragment.
        self.assertIn("?", page.url)
        self.assertIn("#", page.url)
        self.assertTrue(portal._eb_bill_route_proven(page))
        for case, href in (
            ("different_path", "/tenant-bill"),
            ("deeper_path", "/eb-bill/extra"),
            ("cross_origin", "http://other.invalid/eb-bill"),
            ("cross_scheme", "https://synthetic.invalid/eb-bill"),
            ("empty_target", ""),
        ):
            with self.subTest(case=case):
                page.eb_bill_href = href
                self.assertFalse(portal._eb_bill_route_proven(page))
        # A trailing slash is the same route, and an unreadable target is not.
        page.eb_bill_href = "/eb-bill/"
        self.assertTrue(portal._eb_bill_route_proven(page))
        page.href_read_error = RuntimeError(STEP_FAILURE_TEXT)
        self.assertFalse(portal._eb_bill_route_proven(page))

    def test_route_proof_emits_nothing_but_a_boolean(self) -> None:
        """N13. Neither address leaves the proof, on success or on failure."""
        portal, page, _clock = self.route(start_route="results")
        self.assertIs(portal._eb_bill_route_proven(page), True)
        page.eb_bill_href = "/tenant-bill"
        self.assertIs(portal._eb_bill_route_proven(page), False)
        _failed, error = self.open_route(start_route="billing", eb_bill_click_inert=True)
        for fragment in ("http", "synthetic.invalid", "/eb-bill", "?", "#"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, error.message)
        self.assertNotIn("http", cli.support_ref_for(error))

    def test_an_ambiguous_eb_bill_control_never_proves_the_route(self) -> None:
        """Ambiguity is drift, and drift is never route proof."""
        portal, page, clock = self.route(start_route="results", eb_bill_matches=2)
        self.assertFalse(portal._eb_bill_route_proven(page))
        with simulated_clock(clock):
            with self.assertRaises(LayoutChangedError):
                portal._settle_eb_bill_entry(page)
        self.assertEqual(page.clicks, {}, "an ambiguous entry dispatches nothing")

    # ---- N14-N21: the entry decision is settled, never guessed ---- #
    #
    # The repaired defect: the entry decision used to be taken from one
    # immediate look -- `count() == 1` chose the direct path and anything else
    # chose Billing Manager. A surface that is still settling cannot be read
    # that way, in either direction. These cases put a direct entry on the same
    # route as the Billing Manager entry, so choosing the outer path is
    # observable as a mis-route rather than as the only option available, and
    # then vary WHEN and WHETHER that direct entry becomes genuinely usable.

    def test_a_settling_direct_eb_bill_entry_is_never_mis_routed(self) -> None:
        """N14. The direct entry appears after the immediate look, and is used.

        Both entries exist on this route. The direct EB Bill entry has simply
        not rendered yet at the first look, and it becomes available at a later
        committed checkpoint. Deciding from the immediate look sends the run
        through Billing Manager even though the direct route was about to prove
        itself, so a single Billing Manager click here is the defect.
        """
        page, error = self.open_route(
            start_route="app", eb_bill_on_app=True, eb_bill_delay=2
        )
        self.assertIsNone(error)
        self.assertEqual(
            page.clicks.get("billing_manager", 0),
            0,
            "a settling direct entry is never routed through Billing Manager",
        )
        self.assertEqual(page.clicks, {"eb_bill": 1}, "one EB Bill click, and no more")
        self.assertEqual(page.route, "results")

    def test_a_settling_restored_route_needs_no_navigation_click(self) -> None:
        """N15. The saved address IS the route; its own entry settles after `goto()`.

        Restoration re-enters the navigation contract, and the exact control
        route proof is read from has not rendered yet at that moment. The route
        is nonetheless already correct, so giving proof its bounded chance is
        what keeps restoration free of navigation entirely.
        """
        portal, page, clock = self.route(
            pages=(("a.pdf", "b.pdf"), ("c.pdf",)), eb_bill_delay=2
        )
        with tempfile.TemporaryDirectory() as directory:
            with simulated_clock(clock):
                inventory = portal.inventory(20)
                before = dict(page.clicks)
                portal.download(inventory[0], Path(directory) / "download.bin")
        self.assertIn("goto", page.events)
        for control in ("billing_manager", "eb_bill"):
            with self.subTest(control=control):
                self.assertEqual(
                    page.clicks.get(control, 0),
                    before.get(control, 0),
                    "a settling restored route re-enters no navigation",
                )

    def test_a_hidden_direct_entry_is_not_ready_merely_because_it_exists(self) -> None:
        """N16. Exactly one exact match, hidden: existence is not usability.

        The count is one from the first look, so the retired discriminator
        called this a direct route immediately. It stays hidden for the whole
        bounded window, so the direct entry was never usable and the outer
        route is the contract-conformant path -- which is only reached at all
        if readiness, not existence, decided.
        """
        page, error = self.open_route(
            start_route="app", eb_bill_on_app=True, eb_bill_visible_delay=10 ** 6
        )
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "EB Bill navigation control is not ready")
        self.assertEqual(cli.support_ref_for(error), "EG_NAV_EB_BILL_NOT_READY")
        self.assertEqual(
            page.clicks.get("billing_manager", 0),
            1,
            "a never-usable direct entry does not claim the direct path",
        )
        self.assertEqual(page.clicks.get("eb_bill", 0), 0)

    def test_a_direct_entry_that_becomes_visible_later_is_used_directly(self) -> None:
        """N17. The same hidden entry, this time settling inside the window."""
        page, error = self.open_route(
            start_route="app", eb_bill_on_app=True, eb_bill_visible_delay=2
        )
        self.assertIsNone(error)
        self.assertEqual(page.clicks, {"eb_bill": 1})
        self.assertEqual(page.clicks.get("billing_manager", 0), 0)
        self.assertEqual(page.route, "results")

    def test_a_direct_entry_is_clicked_only_once_it_is_actionable(self) -> None:
        """N18. Present and visible at once, trial-actionable only later.

        Nothing is dispatched while actionability is still being proven: the
        trial check runs repeatedly and the real click happens once, after it
        finally passes.
        """
        page, error = self.open_route(
            start_route="app", eb_bill_on_app=True, eb_bill_actionable_delay=2
        )
        self.assertIsNone(error)
        self.assertEqual(page.clicks, {"eb_bill": 1})
        self.assertEqual(page.clicks.get("billing_manager", 0), 0)
        self.assertGreater(
            page.trial_clicks["eb_bill"],
            1,
            "actionability was re-proven, and only the real click was one-shot",
        )

    def test_an_ambiguous_direct_entry_fails_closed_without_a_fallback(self) -> None:
        """N19. Two exact matches: neither narrowed, nor masked by Billing Manager.

        Ambiguity is structural uncertainty about which control the route even
        is. Turning it into "no direct entry" would send the run through the
        outer application and hide the drift behind a route that happens to
        work, so it fails closed with nothing dispatched at all.
        """
        page, error = self.open_route(
            start_route="app", eb_bill_on_app=True, eb_bill_matches=2
        )
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "EB Bill navigation control is not ready")
        self.assertEqual(cli.support_ref_for(error), "EG_NAV_EB_BILL_NOT_READY")
        self.assertEqual(page.clicks, {}, "ambiguity is never routed around")

    def test_an_unreadable_direct_entry_fails_closed_without_dispatching(self) -> None:
        """N20. A state that cannot be read is drift, not evidence of absence."""
        page, error = self.open_route(
            start_route="app",
            eb_bill_on_app=True,
            eb_bill_state_error=RuntimeError(STEP_FAILURE_TEXT),
        )
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "EB Bill navigation control is not ready")
        self.assertEqual(page.clicks, {}, "an unreadable entry dispatches nothing")
        self.assertNotIn("hunter2", error.message)
        self.assertNotIn("portal.example.invalid", error.message)

    def test_no_entry_observation_ever_dispatches_a_navigation_click(self) -> None:
        """N21. The mutation boundary: the settled decision is observation only."""
        for kwargs in (
            {"start_route": "results", "eb_bill_delay": 2},
            {"start_route": "app", "eb_bill_on_app": True, "eb_bill_delay": 2},
            {"start_route": "app", "eb_bill_on_app": True, "eb_bill_matches": 2},
            {
                "start_route": "app",
                "eb_bill_on_app": True,
                "eb_bill_visible_delay": 10 ** 6,
            },
        ):
            with self.subTest(**kwargs):
                portal, page, clock = self.route(**kwargs)
                with simulated_clock(clock):
                    try:
                        portal._settle_eb_bill_entry(page)
                    except LayoutChangedError:
                        pass
                self.assertEqual(page.clicks, {}, "the entry decision clicks nothing")
                self.assertEqual(page.events, [])

    def test_only_the_outer_conclusion_costs_a_bounded_window(self) -> None:
        """N22. What the settled entry decision is allowed to cost, per surface.

        A decisive surface pays nothing: an already-proven route and a directly
        usable entry are both answered at the immediate look, so the healthy
        interaction is still free. Only concluding that no direct entry exists
        requires the committed window -- that conclusion cannot be reached from
        one look without mis-routing a settling surface -- and it costs exactly
        one window, once, with the ready outer controls after it paying nothing.
        """
        for case, kwargs, expected_yields, expected_clicks in (
            ("already_proven", {"start_route": "results"}, [], {}),
            ("direct_ready", {"start_route": "billing"}, [], {"eb_bill": 1}),
            (
                "outer_required",
                {"start_route": "app"},
                ENTRY_SETTLE_YIELDS,
                {"billing_manager": 1, "eb_bill": 1},
            ),
        ):
            with self.subTest(case=case):
                portal, page, clock = self.route(**kwargs)
                with simulated_clock(clock):
                    portal._open_eb_bill_route(page)
                self.assertEqual(page.clicks, expected_clicks)
                self.assertEqual(clock.yields, expected_yields)
                self.assertLessEqual(
                    clock.elapsed_ms(),
                    RECOVERY_CEILING_MS,
                    "the entry decision is one bounded window, never two",
                )

    # ---- the downstream contracts stay exactly as they were ---- #

    def test_the_downstream_sequence_is_unchanged_after_route_proof(self) -> None:
        portal, page, clock = self.route()
        with simulated_clock(clock):
            inventory = portal.inventory(20)
        self.assertEqual([bill.filename for bill in inventory], ["2026-01-01_synthetic.pdf"])
        self.assertEqual(
            page.events,
            ["ems", "billing_manager", "eb_bill", "select_account", "search"],
        )
        self.assertFalse(page.rows_read_before_search)


# ---- DL-XB-141-EMS-ENTRY-MINIMAL-REPAIR-G2-136: the application entry ---- #
#
# The repaired defect: the authenticated landing was treated as the business
# surface. It is not. Login proves the landing and stops; the first business
# entry opens the EMS application with exactly one real click, and everything
# after it is the existing EB Bill route, unchanged. These cases fix that one
# click -- how it is resolved, when it is allowed, and every way it can end.


class EmsApplicationEntryTests(unittest.TestCase):
    """One exact EMS click, once per run, and never from a restored address."""

    def route(self, **kwargs):
        clock = RecoveryClock()
        page = FakeResultsPage(clock=clock, **kwargs)
        portal = PlaywrightPortal(ResultsConfig(), headed=False)
        portal.page = page
        return portal, page, clock

    def enter(self, **kwargs):
        """Run the real first-entry contract and return what it did."""
        portal, page, clock = self.route(**kwargs)
        error = None
        with simulated_clock(clock):
            try:
                portal._open_verified_results()
            except AppError as exc:
                error = exc
        return page, error

    # ---- E01: the landing is not the business surface ---- #

    def test_the_authenticated_landing_carries_no_business_navigation(self) -> None:
        """E01. EMS is there; Billing Manager and EB Bill are not."""
        _portal, page, _clock = self.route()
        self.assertEqual(page.route, "landing")
        self.assertEqual(page.get_by_role("button", name="EMS", exact=True).count(), 1)
        self.assertEqual(
            page.get_by_role("link", name="Billing Manager", exact=True).count(), 0
        )
        self.assertEqual(page.get_by_role("link", name="EB Bill", exact=True).count(), 0)

    def test_the_owner_confirmed_entry_sequence_starts_with_one_ems_click(self) -> None:
        """E02. EMS, then Billing Manager, EB Bill, account and Search."""
        portal, page, clock = self.route()
        with simulated_clock(clock):
            inventory = portal.inventory(20)

        self.assertEqual([bill.filename for bill in inventory], ["2026-01-01_synthetic.pdf"])
        self.assertEqual(
            page.events,
            ["ems", "billing_manager", "eb_bill", "select_account", "search"],
        )
        self.assertEqual(page.ems_actuations, 1)
        self.assertEqual(page.clicks["ems"], 1)

    # ---- E03-E06: the three failure classes, on the entry itself ---- #

    def test_an_ems_readiness_failure_fails_before_any_dispatch(self) -> None:
        """E03. Not actionable is proven first, so nothing is ever clicked."""
        page, error = self.enter(ems_actionable=False)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "EMS application entry control is not ready")
        self.assertEqual(cli.support_ref_for(error), "EG_NAV_EMS_ENTRY_NOT_READY")
        self.assertEqual(page.clicks, {}, "a readiness failure dispatches nothing")
        self.assertEqual(page.ems_actuations, 0)
        self.assertGreater(page.trial_clicks["ems"], 1, "actionability was re-proven")

    def test_a_duplicate_ems_entry_fails_closed_without_narrowing(self) -> None:
        """E04. Two exact matches: neither is chosen, and the selector holds."""
        page, error = self.enter(ems_matches=2)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "EMS application entry control is not ready")
        self.assertEqual(page.clicks, {}, "ambiguity is never narrowed to one control")
        self.assertEqual(page.ems_actuations, 0)

    def test_an_unreadable_ems_entry_fails_closed_without_dispatching(self) -> None:
        """E05. A state that cannot be read is drift, not evidence of readiness."""
        page, error = self.enter(ems_state_error=RuntimeError(STEP_FAILURE_TEXT))
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "EMS application entry control is not ready")
        self.assertEqual(page.clicks, {})
        self.assertNotIn("hunter2", error.message)
        self.assertNotIn("portal.example.invalid", error.message)

    def test_an_ems_click_exception_is_uncertain_and_terminal(self) -> None:
        """E06. It may have landed, so it is never sent again."""
        page, error = self.enter(ems_click_error=RuntimeError(STEP_FAILURE_TEXT))
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(
            error.message, "EMS application entry dispatch outcome uncertain"
        )
        self.assertEqual(
            cli.support_ref_for(error), "EG_NAV_EMS_ENTRY_DISPATCH_UNCERTAIN"
        )
        self.assertEqual(page.clicks["ems"], 1, "never retried, never re-resolved")
        self.assertEqual(page.clicks.get("billing_manager", 0), 0)
        self.assertEqual(page.clicks.get("eb_bill", 0), 0)
        self.assertNotIn("hunter2", error.message)

    def test_an_inert_ems_click_stops_at_the_existing_downstream_failure(self) -> None:
        """E07. The click landed and opened nothing: downstream says so.

        The entry was consumed, so the contract does not look for it again. The
        surface simply never became the application, which the existing Billing
        Manager readiness failure already names exactly.
        """
        page, error = self.enter(ems_click_inert=True)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "Billing Manager navigation control is not ready")
        self.assertEqual(
            cli.support_ref_for(error), "EG_NAV_BILLING_MANAGER_NOT_READY"
        )
        self.assertEqual(page.ems_actuations, 1, "exactly one actuation, and no retry")
        self.assertEqual(page.clicks["ems"], 1)

    # ---- E08: readiness still recovers, and still clicks once ---- #

    def test_a_delayed_ems_entry_is_recovered_and_clicked_once(self) -> None:
        """E08. The shared bounded ladder owns the waiting, not a second one."""
        portal, page, clock = self.route(ems_delay=3, ems_actionable_delay=1)
        with simulated_clock(clock):
            inventory = portal.inventory(20)

        self.assertEqual(len(inventory), 1)
        self.assertEqual(page.clicks["ems"], 1)
        self.assertEqual(page.ems_actuations, 1)
        self.assertGreater(page.looks["ems"], 3, "each look resolved a fresh locator")
        self.assertTrue(clock.yields, "the delay was waited out, not polled tightly")

    def test_an_ems_entry_that_never_settles_costs_exactly_one_window(self) -> None:
        """E08b. The entry gets one bounded window, and then fails closed."""
        portal, page, clock = self.route(ems_visible_delay=10 ** 6)
        with simulated_clock(clock):
            with self.assertRaises(LayoutChangedError) as caught:
                portal._enter_ems_application(page)

        self.assertEqual(
            caught.exception.message, "EMS application entry control is not ready"
        )
        self.assertEqual(page.clicks, {}, "an unsettled entry dispatches nothing")
        self.assertLessEqual(clock.elapsed_ms(), RECOVERY_CEILING_MS)

    # ---- E09: the selector is exact, and stays exact ---- #

    def test_the_ems_entry_selector_is_exact_and_never_weakened(self) -> None:
        """E09. Role button, name EMS, exact -- and no second mechanism."""
        seen: list[tuple] = []

        class SelectorRecorder(FakeResultsPage):
            def get_by_role(self, role, name=None, exact=False):
                seen.append((role, name, exact))
                return super().get_by_role(role, name=name, exact=exact)

            def get_by_text(self, *args, **kwargs):
                raise AssertionError("the entry never resolves a control by text")

        clock = RecoveryClock()
        page = SelectorRecorder(clock=clock)
        portal = PlaywrightPortal(ResultsConfig(), headed=False)
        portal.page = page
        with simulated_clock(clock):
            portal._enter_ems_application(page)

        self.assertEqual(
            [entry for entry in seen if entry[1] == "EMS"],
            [("button", "EMS", True)],
            "one exact resolution, by role and name only",
        )
        source = pathlib_read_portal_source()
        entry = source[
            source.index("def _enter_ems_application") : source.index(
                "def _open_eb_bill_route"
            )
        ]
        for forbidden in (".first", "get_by_text", "dispatch_event", "press("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, entry)
        self.assertEqual(entry.count("control.click()"), 1, "one dispatch, and only one")

    # ---- E10: a restored results address never re-enters the application ---- #

    def test_a_restored_results_address_never_re_actuates_ems(self) -> None:
        """E10. Inventory then download is one EMS actuation in total."""
        portal, page, clock = self.route(pages=(("a.pdf", "b.pdf"), ("c.pdf",)))
        with tempfile.TemporaryDirectory() as directory:
            with simulated_clock(clock):
                inventory = portal.inventory(20)
                self.assertEqual(page.ems_actuations, 1)
                portal.download(inventory[0], Path(directory) / "download.bin")

        self.assertEqual(
            page.ems_actuations, 1, "a restored address is already inside the application"
        )
        self.assertEqual(page.clicks["ems"], 1)
        self.assertIn("goto", page.events)

    # ---- E11: the two EMS symbols are separate, and must stay in step ---- #

    def test_the_authentication_witness_and_the_business_entry_stay_in_step(self) -> None:
        """E11. Same text today, separate symbols, and drift is caught here.

        Authentication reads a witness; business navigation clicks a control.
        They are declared apart so either can move on its own -- and this case
        is what makes moving one of them a decision rather than an accident.
        """
        self.assertEqual(portal_module.AUTHENTICATION_WITNESS_ROLE, "button")
        self.assertEqual(portal_module.AUTHENTICATION_WITNESS_NAME, "EMS")
        self.assertEqual(portal_module.EMS_ENTRY_NAV_NAME, "EMS")
        self.assertEqual(
            portal_module.EMS_ENTRY_NAV_NAME,
            portal_module.AUTHENTICATION_WITNESS_NAME,
            "the landing witness and the application entry currently name one control",
        )
        source = pathlib_read_portal_source()
        self.assertIn('EMS_ENTRY_NAV_NAME = "EMS"', source)
        self.assertIn('AUTHENTICATION_WITNESS_NAME = "EMS"', source)
        entry = source[
            source.index("def _enter_ems_application") : source.index(
                "def _open_eb_bill_route"
            )
        ]
        self.assertIn("EMS_ENTRY_NAV_NAME", entry)
        self.assertNotIn(
            "AUTHENTICATION_WITNESS_NAME",
            entry,
            "business navigation never clicks the authentication witness symbol",
        )


class BusinessNavigationReferenceTests(unittest.TestCase):
    """Completeness in both directions for the navigation vocabulary."""

    NAVIGATION_BRANCHES = (
        {"start_route": "app", "billing_manager_actionable": False},
        {
            "start_route": "app",
            "billing_manager_click_error": RuntimeError(STEP_FAILURE_TEXT),
        },
        {"start_route": "app", "eb_bill_actionable": False},
        {"start_route": "billing", "eb_bill_click_error": RuntimeError(STEP_FAILURE_TEXT)},
        {"start_route": "billing", "eb_bill_click_inert": True},
    )

    # The EMS application entry is reached from `_open_verified_results()`
    # rather than from `_open_eb_bill_route()`, so its two references are driven
    # through the entry point that owns them.
    EMS_ENTRY_BRANCHES = (
        {"ems_actionable": False},
        {"ems_click_error": RuntimeError(STEP_FAILURE_TEXT)},
    )

    def drive(self, kwargs, entry: str):
        """Run one navigation branch through the entry point that owns it."""
        clock = RecoveryClock()
        page = FakeResultsPage(clock=clock, **kwargs)
        portal = PlaywrightPortal(ResultsConfig(), headed=False)
        portal.page = page
        with simulated_clock(clock):
            with self.assertRaises(LayoutChangedError) as caught:
                if entry == "ems":
                    portal._open_verified_results()
                else:
                    portal._open_eb_bill_route(page)
        return page, caught.exception

    def branches(self):
        for kwargs in self.NAVIGATION_BRANCHES:
            yield kwargs, "eb_bill_route"
        for kwargs in self.EMS_ENTRY_BRANCHES:
            yield kwargs, "ems"

    def test_every_navigation_reference_is_reachable_and_none_is_unmapped(self) -> None:
        reached: set[str] = set()
        for kwargs, entry in self.branches():
            _page, error = self.drive(kwargs, entry)
            self.assertIn(error.message, cli.SUPPORT_REFS_BY_MESSAGE, error.message)
            reached.add(cli.support_ref_for(error))
            # Nothing the raised exception carried may survive into a message.
            self.assertNotIn("hunter2", error.message)
            self.assertNotIn("portal.example.invalid", error.message)
        self.assertEqual(
            reached,
            set(cli.NAVIGATION_SUPPORT_REFS),
            "the declared navigation vocabulary and its reachable branches must match",
        )
        self.assertTrue(reached.isdisjoint(cli.RETIRED_SUPPORT_REFS))

    def test_a_navigation_failure_never_reports_a_login_reference(self) -> None:
        """Business navigation after a proven landing is never a login failure."""
        for kwargs, entry in self.branches():
            _page, error = self.drive(kwargs, entry)
            reference = cli.support_ref_for(error)
            with self.subTest(reference=reference):
                self.assertFalse(reference.startswith("EG_LOGIN_"))
                self.assertNotEqual(reference, cli.UNCLASSIFIED_SUPPORT_REF)


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

    def test_lagging_credential_fields_are_each_typed_once(self) -> None:
        absent_username = FakeLocator(count=0, label="absent_username")
        username = FakeLocator(label="username")
        hidden_password = FakeLocator(visible=False, label="hidden_password")
        password = FakeLocator(label="password")
        page = login_page(
            username_fields=[absent_username, username],
            password_fields=[hidden_password, password],
        )

        self.assertIsNone(self.attempt(page))
        self.assertEqual(absent_username.typed, 0)
        self.assertEqual(username.typed, 1, "a credential value is never appended twice")
        self.assertEqual(hidden_password.typed, 0)
        self.assertEqual(password.typed, 1)

    def test_each_credential_is_focused_proven_then_typed_never_assigned(self) -> None:
        """The observed entry contract: focused, PROVEN focused, then typed.

        Observed live: assignment-based credential entry left the canonical
        Login control stably absent, while user-like typed entry on the same
        path produced exactly one visible, enabled, actionable Login control.
        The internal reason for that difference was not measured, so any
        editing-widget or incomplete-form account of it stays hypothesis. Each
        field is therefore clicked once, its focus proven once, and typed once,
        in that order, and nothing is assigned.
        """
        journal: list[str] = []
        username = FakeLocator(journal=journal, label="username")
        password = FakeLocator(journal=journal, label="password")
        page = login_page(username_field=username, password_field=password, journal=journal)

        self.assertIsNone(self.attempt(page))
        for field, name in ((username, "username"), (password, "password")):
            self.assertEqual(field.clicks, 1, f"{name} is focused exactly once")
            self.assertEqual(field.typed, 1, f"{name} is typed exactly once")
            self.assertEqual(
                field.type_delays,
                [portal_module.LOGIN_KEY_ENTRY_DELAY_MS],
                f"{name} is typed with the per-key pacing the editing host needs",
            )
        credential_steps = [step for step in journal if step.startswith(("username:", "password:"))]
        self.assertEqual(
            credential_steps,
            [
                "username:click",
                "username:evaluate",
                "username:press_sequentially",
                "password:click",
                "password:evaluate",
                "password:press_sequentially",
            ],
            "proven focus separates the one click from the one typed entry",
        )

    def test_a_delayed_authenticated_landing_never_re_submits(self) -> None:
        """A landing slow to render the witness is waited for, not re-submitted."""
        submit = FakeLocator(label="submit")
        pending = FakeLocator(count=0, visible=False, label="pending_landing")
        settled = FakeLocator(label="ems")
        page = login_page(submits=[submit], emss=[pending, pending, settled])

        self.assertIsNone(self.attempt(page))
        self.assertEqual(submit.clicks, 1, "a slow landing never re-submits the login")
        self.assertTrue(page.waited_ms, "the lagging landing cost a bounded yield")

    def test_a_landing_that_never_proves_never_re_submits(self) -> None:
        submit = FakeLocator(label="submit")
        never = FakeLocator(count=0, visible=False, label="never_landing")
        page = login_page(submits=[submit], emss=[never])

        error = self.attempt(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(
            error.message, "authenticated landing was not proven after login"
        )
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_AUTHENTICATION_UNPROVED")
        self.assertEqual(submit.clicks, 1, "an unproved landing never duplicates the submit")

    def test_a_settled_rejection_stops_the_post_login_window_early(self) -> None:
        """A visible rejection is an answer, so the window does not run its course.

        Waiting the whole minute out could not change the outcome and would
        delay every rejected credential run by that minute. The authentication
        witness must be positively ABSENT for this to be a rejection at all.
        """
        submit = FakeLocator(label="submit")
        page = login_page(submits=[submit], landing=False, alert_visible=True)

        error = self.attempt(page)
        self.assertIsInstance(error, LoginError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_PORTAL_REJECTED")
        self.assertEqual(submit.clicks, 1, "a rejection is never re-submitted")
        self.assertEqual(
            page.waited_ms,
            CREDENTIAL_FOCUS_YIELDS,
            "a settled rejection is not waited out beyond the credential focus floor",
        )

    def test_an_ambiguous_authentication_witness_never_re_submits(self) -> None:
        """Two EMS matches are re-checked, never acted on and never authenticated."""
        submit = FakeLocator(label="submit")
        ambiguous = FakeLocator(count=2, label="ambiguous_ems")
        page = login_page(submits=[submit], emss=[ambiguous])

        error = self.attempt(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_AUTHENTICATION_UNPROVED")
        self.assertEqual(submit.clicks, 1)
        self.assertEqual(ambiguous.clicks, 0, "an ambiguous witness is never interacted with")


# ---- DL-XB-141-AUTH-LANDING-NAV-SEPARATION-G2-001: authentication ---- #
#
# Run125 is consumed, adjudicable and PASS, and it bound the authenticated
# landing to exactly one witness: role `button`, name `EMS`, exact. These cases
# hold the whole authentication contract against that witness set and nothing
# else. They need no browser, no server and no credential: what is under test
# is what the counts and booleans are allowed to mean.


class AuthenticationProofTests(unittest.TestCase):
    """Contract A: only a clean EMS witness set authenticates."""

    def outcome(self, **overrides) -> str:
        observation = witnesses(**overrides)
        return PlaywrightPortal(FakeConfig(), headed=False)._authentication_outcome(
            observation
        )

    # ---- A01-A03: what authentication is ---- #

    def test_a_clean_unique_visible_witness_authenticates(self) -> None:
        """A01. One visible EMS and every retained witness at an exact zero."""
        self.assertEqual(self.outcome(ems=(1, True)), portal_module.AUTHENTICATED)

    def test_authentication_does_not_need_billing_manager(self) -> None:
        """A02. Billing Manager is absent, and the landing still authenticates."""
        observation = witnesses(ems=(1, True), billing_manager=(0, False))
        portal = PlaywrightPortal(FakeConfig(), headed=False)
        self.assertEqual(
            portal._authentication_outcome(observation), portal_module.AUTHENTICATED
        )

    def test_authentication_survives_an_unreadable_billing_manager(self) -> None:
        """A03. An unreadable Billing Manager is not authentication evidence."""
        self.assertEqual(
            self.outcome(ems=(1, True), billing_manager=(None, None)),
            portal_module.AUTHENTICATED,
        )

    # ---- A04-A08: what can never authenticate ---- #

    def test_no_prohibited_substitute_can_authenticate(self) -> None:
        """A04-A08. None of the corroborating observations is ever proof.

        URL movement, a vanished login route, a Flutter semantics host, a
        render shell, and a visible Billing Manager are each tried alone and
        then all together. Without EMS none of them authenticates.
        """
        substitutes = {
            "url_movement": {},
            "login_controls_gone": {},
            "semantics_host": {"hosts": {"flt-semantics-host": 1}},
            "render_shell": {"hosts": {"flt-glass-pane": 1, "canvas": 2}},
            "billing_manager": {"billing_manager": (1, True)},
        }
        for case, override in substitutes.items():
            with self.subTest(case=case):
                self.assertEqual(
                    self.outcome(**override), portal_module.AUTHENTICATION_UNPROVED
                )
        combined = witnesses(
            hosts={"flt-semantics-host": 1, "flt-glass-pane": 1},
            billing_manager=(1, True),
        )
        # The URL-changed observation exists on the post-submit shape and is
        # deliberately not part of the authentication witness set at all.
        combined["url_changed"] = True
        portal = PlaywrightPortal(FakeConfig(), headed=False)
        self.assertEqual(
            portal._authentication_outcome(combined),
            portal_module.AUTHENTICATION_UNPROVED,
            "every corroborating observation together is still not proof",
        )

    # ---- A09-A13: fail-closed readings ---- #

    def test_every_unusable_witness_reading_fails_closed(self) -> None:
        """A09-A12. Missing, duplicate, hidden and unreadable all fail closed."""
        for case, override in (
            ("missing", {"ems": (0, False)}),
            ("duplicate", {"ems": (2, True)}),
            ("hidden", {"ems": (1, False)}),
            ("unreadable_count", {"ems": (None, None)}),
            ("unreadable_visibility", {"ems": (1, None)}),
        ):
            with self.subTest(case=case):
                self.assertEqual(
                    self.outcome(**override), portal_module.AUTHENTICATION_UNPROVED
                )

    def test_a_rejection_reader_failure_never_becomes_rejection_absent(self) -> None:
        """A13. The strict witness is null, so neither arm can be satisfied."""
        self.assertEqual(
            self.outcome(ems=(1, True), rejection=(None, None)),
            portal_module.AUTHENTICATION_UNPROVED,
            "an unreadable rejection is never read as absent",
        )
        self.assertEqual(
            self.outcome(ems=(0, False), rejection=(None, None)),
            portal_module.AUTHENTICATION_UNPROVED,
            "an unreadable rejection is never read as a rejection either",
        )
        # An ambiguous rejection is unreadable in the same way: strict mode
        # cannot answer the visibility question for more than one match.
        self.assertEqual(
            self.outcome(ems=(1, True), rejection=(2, None)),
            portal_module.AUTHENTICATION_UNPROVED,
        )

    # ---- A14-A17: contradictions, and the one true rejection ---- #

    def test_no_contradiction_ever_authenticates(self) -> None:
        """A14-A16. EMS beside a retained login, gate or rejection is unproved."""
        for case, override in (
            ("login", {"login": (1, True, True)}),
            ("hidden_login", {"login": (1, False, False)}),
            ("username", {"username": (1, True)}),
            ("password", {"password": (1, True)}),
            ("enable_accessibility", {"enable_accessibility": (1, True, True)}),
            ("rejection", {"alert": True}),
        ):
            with self.subTest(case=case):
                self.assertEqual(
                    self.outcome(ems=(1, True), **override),
                    portal_module.AUTHENTICATION_UNPROVED,
                )

    def test_a_visible_rejection_with_the_witness_absent_rejects(self) -> None:
        """A17. The one reading that establishes rejection."""
        self.assertEqual(
            self.outcome(ems=(0, False), alert=True), portal_module.REJECTED
        )
        # And an unreadable witness count is not "positively absent".
        self.assertEqual(
            self.outcome(ems=(None, None), alert=True),
            portal_module.AUTHENTICATION_UNPROVED,
        )

    def test_the_witness_identity_is_exactly_the_run125_locator(self) -> None:
        """The bound witness, stated as the exact role and name it must be."""
        self.assertEqual(portal_module.AUTHENTICATION_WITNESS_ROLE, "button")
        self.assertEqual(portal_module.AUTHENTICATION_WITNESS_NAME, "EMS")
        looked_up: list[tuple] = []

        class WitnessRecorder(FakePage):
            def get_by_role(self, role, name=None, exact=False):
                looked_up.append((role, name, exact))
                return super().get_by_role(role, name=name, exact=exact)

        page = login_page(page_cls=WitnessRecorder)
        portal = PlaywrightPortal(FakeConfig(), headed=False)
        portal.page = page
        portal._observe_authentication_witnesses(page, 1000)
        self.assertIn(("button", "EMS", True), looked_up)


class AuthenticatedLandingWindowTests(unittest.TestCase):
    """Contract A end to end, through the real `login()` and its bounded window."""

    def attempt(self, page: FakePage):
        portal = PlaywrightPortal(FakeConfig(), headed=False)
        portal.page = page
        old = {
            name: os.environ.get(name)
            for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")
        }
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

    def test_a_clean_landing_returns_without_touching_billing_manager(self) -> None:
        """`login() -> None` means the landing was proven, and nothing more."""
        billing_manager = FakeLocator(count=0, visible=False, label="no_billing")
        page = login_page(billing_manager=billing_manager)
        self.assertIsNone(self.attempt(page))
        self.assertEqual(billing_manager.clicks, 0)
        self.assertEqual(billing_manager.waits, 0)

    def test_a_retained_login_route_beside_the_witness_fails_closed(self) -> None:
        page = login_page(retained_login=True)
        error = self.attempt(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_AUTHENTICATION_UNPROVED")

    def test_a_hidden_witness_fails_closed(self) -> None:
        page = login_page(ems=FakeLocator(visible=False, label="hidden_ems"))
        error = self.attempt(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_AUTHENTICATION_UNPROVED")

    def test_an_unreadable_witness_fails_closed(self) -> None:
        page = login_page(
            ems=FakeLocator(state_error=RuntimeError("strict mode violation"))
        )
        error = self.attempt(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_AUTHENTICATION_UNPROVED")

    def test_an_unreadable_rejection_beside_a_clean_witness_fails_closed(self) -> None:
        """The strict reader failing is never "rejection absent"."""
        page = login_page(
            rejection=FakeLocator(state_error=RuntimeError("strict mode violation"))
        )
        error = self.attempt(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_AUTHENTICATION_UNPROVED")

    def test_a_witness_beside_a_visible_rejection_is_not_a_rejection(self) -> None:
        """The contradiction fails closed rather than reporting a rejection."""
        page = login_page(alert_visible=True)
        error = self.attempt(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_AUTHENTICATION_UNPROVED")
        self.assertNotEqual(cli.support_ref_for(error), "EG_LOGIN_PORTAL_REJECTED")

    def test_the_authentication_window_stays_inside_the_shared_deadline(self) -> None:
        page = login_page(landing=False)
        error = self.attempt(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertLessEqual(
            page.simulated_elapsed_ms() / 1000.0,
            portal_module.PORTAL_RECOVERY_DEADLINE_SECONDS,
            "the authentication window reuses the shared ceiling and never raises it",
        )


# ---- DL-XB-141-PASSWORD-FOCUS-SETTLE-001: the credential focus gate ---- #
#
# Run123 was adjudicable, unattended and complete, and the owner observed that
# unattended password entry appeared to receive one fewer character than
# expected. The matching code gap was that a credential field proven visible
# and enabled was clicked and then typed into immediately, with nothing proving
# that the field -- or the editing host the click hands focus to -- had actually
# taken focus before the first key event.
#
# These cases drive the committed `login()` and `login_diagnostic()`, so what
# they prove is the production path: proven focus separates the one click from
# the one typed entry, focus that never settles types nothing and submits
# nothing, and no case anywhere holds, measures or emits a credential value.
# The handoff race remains the leading hypothesis rather than a proved portal
# fact; typing into an unfocused field is wrong either way.


class CredentialFocusSettleTests(unittest.TestCase):
    """Credential keys are never sent until the target is proven focused."""

    def run_login(self, page: FakePage) -> tuple[AppError | None, dict[str, str]]:
        """Run the committed `login()` on a simulated clock.

        The synthetic credential values are returned so a case can prove no
        output surface carries them; nothing in the portal is ever asked for a
        value, and no case asserts a length.
        """

        portal = PlaywrightPortal(FakeConfig(), headed=False)
        portal.page = page
        old = {name: os.environ.get(name) for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")}
        values = runtime_credentials()
        os.environ.update(values)
        try:
            with mock.patch.object(portal_module, "time", create=True) as clock:
                clock.monotonic.side_effect = page.simulated_monotonic
                portal.login()
        except AppError as exc:
            return exc, values
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        return None, values

    def run_diagnostic(self, page: FakePage):
        """Run the committed bounded diagnostic against `page`."""

        portal = PlaywrightPortal(FakeConfig(), headed=False)
        portal.page = page
        old = {name: os.environ.get(name) for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")}
        os.environ.update(runtime_credentials())
        try:
            with mock.patch.object(portal_module, "time", create=True) as clock:
                clock.monotonic.side_effect = page.simulated_monotonic
                return portal.login_diagnostic()
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def assert_typed_once_at_the_committed_pacing(self, field: FakeLocator, name: str) -> None:
        """One typed entry, at the established per-key pacing, after one click."""

        self.assertEqual(field.clicks, 1, f"{name} takes exactly one normal focus click")
        self.assertEqual(field.typed, 1, f"{name} is typed exactly once")
        self.assertEqual(
            field.type_delays,
            [portal_module.LOGIN_KEY_ENTRY_DELAY_MS],
            f"{name} keeps the established 25 ms per-key pacing",
        )

    def test_the_settle_floor_is_a_committed_checkpoint_not_a_new_sleep(self) -> None:
        """The gate reuses the recovery timing contract instead of adding one."""

        self.assertEqual(
            portal_module.LOGIN_FOCUS_SETTLE_FLOOR_MS,
            portal_module.PORTAL_RECOVERY_CHECKPOINTS_MS[0],
            "the floor is the ladder's first committed checkpoint",
        )
        self.assertEqual(
            portal_module.PORTAL_RECOVERY_DEADLINE_SECONDS,
            60.0,
            "the gate must not move the shared deadline",
        )
        # The floored ladder is the committed one minus its earliest looks: no
        # checkpoint is added, and the window still ends where it always did.
        floored = PlaywrightPortal._recovery_attempts_ms(
            portal_module.LOGIN_FOCUS_SETTLE_FLOOR_MS
        )
        self.assertEqual(
            floored,
            tuple(
                ms
                for ms in portal_module.PORTAL_RECOVERY_ATTEMPTS_MS
                if ms >= portal_module.LOGIN_FOCUS_SETTLE_FLOOR_MS
            ),
        )
        self.assertNotIn(0, floored, "the immediate look is declined, not repurposed")
        self.assertEqual(floored[-1], portal_module.PORTAL_RECOVERY_ATTEMPTS_MS[-1])
        self.assertEqual(
            PlaywrightPortal._recovery_attempts_ms(0),
            portal_module.PORTAL_RECOVERY_ATTEMPTS_MS,
            "every other caller keeps the committed ladder unchanged",
        )

    def test_a_stably_focused_username_is_typed_exactly_once(self) -> None:
        """The healthy case: focus is owned, proven once, and typed once."""

        username = FocusHostField(label="username")
        page = login_page(username_field=username)

        error, _values = self.run_login(page)
        self.assertIsNone(error)
        self.assert_typed_once_at_the_committed_pacing(username, "username")
        self.assertEqual(username.focus_checks, 1, "a settled field needs one focus proof")
        self.assertEqual(username.complete_entries, 1)
        self.assertFalse(username.leading_key_dropped)

    def test_a_stably_focused_password_is_typed_exactly_once(self) -> None:
        """The same contract for the field the observation was made on."""

        password = FocusHostField(label="password")
        submit = FakeLocator(label="submit")
        page = login_page(password_field=password, submits=[submit])

        error, _values = self.run_login(page)
        self.assertIsNone(error)
        self.assert_typed_once_at_the_committed_pacing(password, "password")
        self.assertEqual(password.focus_checks, 1)
        self.assertEqual(password.complete_entries, 1)
        self.assertFalse(password.leading_key_dropped)
        # The one-shot submit boundary is untouched by the gate.
        self.assertEqual(submit.trial_clicks, 1)
        self.assertEqual(submit.clicks, 1)

    def test_a_password_host_slow_to_focus_receives_no_key_until_it_settles(self) -> None:
        """The regression case, on the observed Username-to-Password handoff.

        The host reports unfocused for the first two looks and focused after.
        No key event may be sent while it is unfocused: the modelled host loses
        the leading key, which is exactly the one-fewer-character the owner
        observed. The corrected path types only once focus is proven, so the
        entry is complete and dispatched exactly once.
        """

        password = FocusHostField(focus_after_looks=2, label="password")
        page = login_page(password_field=password)

        error, _values = self.run_login(page)
        self.assertIsNone(error)
        self.assertEqual(password.focus_checks, 3, "the gate looked again instead of typing")
        self.assert_typed_once_at_the_committed_pacing(password, "password")
        self.assertFalse(
            password.leading_key_dropped,
            "no key event may be sent into a host that has not taken focus",
        )
        self.assertEqual(password.complete_entries, 1, "one complete entry, once focus settled")
        self.assertTrue(page.waited_ms, "an unsettled focus costs bounded yields, not keys")

    def test_a_locator_replaced_during_the_handoff_is_freshly_re_resolved(self) -> None:
        """The gate follows a replaced input rather than trusting a stale handle.

        The field the click was sent to is discarded by the host and replaced by
        the one that actually owns focus. Because every look re-resolves the
        exact labelled target, the replacement is what is proven and what is
        typed -- and the stale handle is never typed into.
        """

        clicked = FocusHostField(focused=False, label="clicked_password")
        replacement = FocusHostField(label="replacement_password")
        page = login_page(password_fields=[clicked, clicked, replacement])

        error, _values = self.run_login(page)
        self.assertIsNone(error)
        self.assertEqual(clicked.clicks, 1, "the one focus click went to the field that resolved")
        self.assertEqual(clicked.typed, 0, "a stale handle is never typed into")
        self.assertFalse(clicked.leading_key_dropped)
        self.assertEqual(replacement.typed, 1)
        self.assertEqual(replacement.type_delays, [portal_module.LOGIN_KEY_ENTRY_DELAY_MS])
        self.assertEqual(replacement.complete_entries, 1)
        self.assertFalse(replacement.leading_key_dropped)
        self.assertEqual(
            replacement.clicks, 0, "exactly one normal focus click per field, never a second"
        )

    def test_visible_and_enabled_but_unfocused_is_not_sufficient(self) -> None:
        """Readiness is not focus, and a ready-but-unfocused field is not typed."""

        password = FocusHostField(focused=True, focus_after_looks=1, label="password")
        page = login_page(password_field=password)

        error, _values = self.run_login(page)
        self.assertIsNone(error)
        # The field was visible and enabled from the first look, so readiness
        # alone would have typed immediately; the gate did not.
        self.assertTrue(password.enabled_checks, "readiness is still proven as before")
        self.assertEqual(password.focus_checks, 2, "readiness did not stand in for focus")
        self.assertEqual(password.typed, 1)
        self.assertFalse(password.leading_key_dropped)

    def test_focus_that_never_settles_fails_closed_before_typing_or_submit(self) -> None:
        """Fail-closed: no credential key for that field, and no Login submit."""

        password = FocusHostField(focused=False, label="password")
        submit = FakeLocator(label="submit")
        page = login_page(password_field=password, submits=[submit])

        error, values = self.run_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "login password entry did not complete")
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_PASSWORD_FILL_FAILED")
        self.assertEqual(error.status, PORTAL_LAYOUT_CHANGED)
        self.assertEqual(password.typed, 0, "an unfocused field is never typed into")
        self.assertFalse(password.leading_key_dropped)
        self.assertEqual(password.clicks, 1, "still exactly one normal focus click")
        self.assertEqual(submit.clicks, 0, "no Login submit follows a failed credential entry")
        self.assertEqual(submit.trial_clicks, 0)
        self.assertEqual(
            password.focus_checks,
            len(PlaywrightPortal._recovery_attempts_ms(portal_module.LOGIN_FOCUS_SETTLE_FLOOR_MS)),
            "focus was re-proven at every remaining checkpoint before failing closed",
        )
        self.assert_no_credential_value(error.message, values)

    def test_an_unsettled_username_focus_never_reaches_the_password_field(self) -> None:
        """The first field failing closed stops the sequence where it stands."""

        username = FocusHostField(focused=False, label="username")
        password = FocusHostField(label="password")
        submit = FakeLocator(label="submit")
        page = login_page(username_field=username, password_field=password, submits=[submit])

        error, values = self.run_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(error.message, "login username entry did not complete")
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_USERNAME_FILL_FAILED")
        self.assertEqual(username.typed, 0)
        self.assertEqual(password.clicks, 0, "the password field is never even reached")
        self.assertEqual(password.typed, 0)
        self.assertEqual(submit.clicks, 0)
        self.assert_no_credential_value(error.message, values)

    def test_the_diagnostic_reports_nothing_dispatched_when_focus_never_settles(self) -> None:
        """The bounded diagnostic fails closed on the same gate, and observes nothing.

        Its vocabulary, submit semantics and post-submit observation timing are
        unchanged: nothing was sent, so there is nothing to observe, and the
        unobserved witness shape is what is reported.
        """

        password = FocusHostField(focused=False, label="password")
        submit = FakeLocator(label="submit")
        page = login_page(password_field=password, submits=[submit])

        result = self.run_diagnostic(page)
        self.assertFalse(result.submit_dispatched)
        self.assertEqual(result.submit_outcome, portal_module.SUBMIT_NOT_DISPATCHED)
        self.assertIsNone(result.classification)
        self.assertEqual(submit.clicks, 0)
        self.assertEqual(password.typed, 0)
        self.assertIsInstance(result.failure, LayoutChangedError)
        self.assertEqual(cli.support_ref_for(result.failure), "EG_LOGIN_PASSWORD_FILL_FAILED")
        self.assertEqual(
            result.post_submit, portal_module.unobserved_login_witnesses(include_url=True)
        )

    def test_the_focus_probe_is_bounded_and_a_real_failure_is_not_transient(self) -> None:
        """A focus probe carries its own small explicit timeout, like every other."""

        stalling = FocusHostField(focus_error=synthetic_timeout(), label="stalling_password")
        page = login_page(password_field=stalling, default_timeout_ms=300_000)

        error, _values = self.run_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assertTrue(stalling.evaluate_timeouts, "the focus probe must actually run")
        self.assertTrue(
            all(value is not None for value in stalling.evaluate_timeouts),
            "every focus probe must carry an explicit timeout",
        )
        self.assertNotIn(0, stalling.evaluate_timeouts, "a zero timeout would wait forever")
        self.assertLessEqual(
            max(stalling.evaluate_timeouts),
            MAX_TRIAL_PROBE_MS,
            "a focus probe stays far below any configured page default",
        )
        self.assertEqual(stalling.typed, 0)

        broken = FocusHostField(focus_error=step_failure(), label="broken_password")
        error, values = self.run_login(login_page(password_field=broken))
        self.assertIsInstance(error, LayoutChangedError)
        self.assertEqual(broken.focus_checks, 1, "only a timeout is transient")
        self.assertEqual(broken.typed, 0)
        self.assertNotIn("hunter2", error.message)
        self.assertNotIn("portal.example.invalid", error.message)
        self.assert_no_credential_value(error.message, values)

    def test_the_focus_predicate_cannot_read_a_credential(self) -> None:
        """The predicate is a boolean identity comparison and nothing more."""

        predicate = portal_module._FOCUS_OWNERSHIP_PREDICATE
        for forbidden in (
            ".value",
            "textContent",
            "innerText",
            "innerHTML",
            "outerHTML",
            "length",
            "getAttribute",
            "screenshot",
        ):
            self.assertNotIn(
                forbidden, predicate, f"the focus predicate must never reach for {forbidden}"
            )
        self.assertIn("activeElement", predicate, "focus ownership is what it proves")
        # Only a literal True is proof: a predicate that answers anything else
        # is "not yet", never a pass.
        portal = PlaywrightPortal(FakeConfig(), headed=False)
        for answer in (None, "true", 1, {}):
            with self.subTest(answer=answer):
                locator = mock.Mock()
                locator.evaluate.return_value = answer
                self.assertFalse(portal._probe_focus_owned(locator, 1_000))

    def assert_no_credential_value(self, text: str, values: dict[str, str]) -> None:
        """No credential value, and no fragment of one, may reach an output surface."""

        for name, value in values.items():
            self.assertNotIn(value, text, f"{name} must never appear in an output surface")
            # The random half of each synthetic value: a partial echo is a leak too.
            self.assertNotIn(value.split("-", 1)[1], text)


# ---- DL-XB-141-RUNTIME-005-SOURCE-DURABILITY-A1: the bounded login diagnostic ---- #
#
# The diagnostic reuses the canonical pre-submit sequence and the one canonical
# submit. These cases hold that reuse, the one-shot submit boundary, the closed
# observation allowlist, the fail-closed classification matrix, and the fact
# that no business path is reachable from it.


def pathlib_read_portal_source() -> str:
    """Return the committed portal source, for source-shape assertions."""

    return Path(portal_module.__file__).read_text(encoding="utf-8")


def witnesses(
    *,
    hosts=None,
    placeholder: int = 0,
    billing_manager=(0, False),
    username=(0, False),
    password=(0, False),
    login=(0, False, False),
    enable_accessibility=(0, False, False),
    ems=(0, False),
    rejection=None,
    alert: bool = False,
) -> dict:
    """Build one observation in exactly the shape the portal produces.

    `rejection` defaults to the strict witness a surface with `alert` either
    has or positively lacks. A case that needs an UNREADABLE rejection reader,
    or one whose count and visibility disagree, states it explicitly.
    """

    host_counts = {tag: 0 for tag in portal_module.DIAGNOSTIC_HOST_TAGS}
    host_counts.update(hosts or {})
    if rejection is None:
        rejection = (1, True) if alert else (0, False)
    return {
        "hosts": host_counts,
        "semantics_placeholder": {
            "count": placeholder,
            "present": None if placeholder is None else placeholder > 0,
        },
        "ems": {"count": ems[0], "visible": ems[1]},
        "username": {"count": username[0], "visible": username[1]},
        "password": {"count": password[0], "visible": password[1]},
        "login": {"count": login[0], "visible": login[1], "actionable": login[2]},
        "enable_accessibility": {
            "count": enable_accessibility[0],
            "visible": enable_accessibility[1],
            "actionable": enable_accessibility[2],
        },
        "rejection": {"count": rejection[0], "visible": rejection[1]},
        "billing_manager": {"count": billing_manager[0], "visible": billing_manager[1]},
        "visible_alert": alert,
    }


SHELL_ONLY = witnesses(hosts={"flt-glass-pane": 1})
SEMANTICS_ONLY = witnesses(hosts={"flt-semantics-host": 1, "flt-glass-pane": 1})
NOTHING_AT_ALL = witnesses()
# The login route a post-submit Flutter route settles away from, and the two
# outcomes it may reach.
LOGIN_ROUTE_ONLY = witnesses(hosts={"flt-semantics-host": 1}, password=(1, True))
BILLING_MANAGER_READY = witnesses(
    hosts={"flt-semantics-host": 1}, billing_manager=(1, True)
)
ALERT_SURFACE = witnesses(
    hosts={"flt-semantics-host": 1},
    username=(1, True),
    login=(1, True, True),
    alert=True,
)
# The clean authenticated landing Run125 bound: exactly one visible EMS witness
# with every retained login, accessibility and rejection witness at an exact
# zero count. It deliberately carries NO Billing Manager, because Billing
# Manager is not an authentication oracle.
AUTHENTICATED_LANDING = witnesses(hosts={"flt-semantics-host": 1}, ems=(1, True))


class WitnessLocator:
    """A locator that reports one fixed witness and is never really clicked."""

    def __init__(self, count, visible, actionable) -> None:
        self._count = count
        self._visible = visible
        self._actionable = actionable

    def count(self) -> int:
        if self._count is None:
            raise RuntimeError("synthetic unreadable locator")
        return self._count

    def is_visible(self, timeout: int | None = None) -> bool:
        if self._visible is None:
            raise RuntimeError("synthetic unreadable locator")
        return bool(self._visible)

    def click(self, trial: bool = False, timeout: int | None = None) -> None:
        if not trial:
            raise AssertionError("observation must never dispatch a real click")
        if not self._actionable:
            raise SyntheticTimeoutError("synthetic actionability timeout")


class WitnessPage:
    """A page that serves only the fixed diagnostic witness lookups.

    One state is served for a whole observation pass; the next checkpoint's
    event-loop yield is what advances to the next state, so a surface can be
    made to change between checkpoints exactly as a real one would.
    """

    def __init__(self, states, urls=None) -> None:
        self._states = list(states)
        self._urls = list(urls) if urls is not None else None
        self._index = 0
        self.entry_url = "http://127.0.0.1:1/synthetic"
        self.lookups: list[str] = []
        self.waited_ms: list[int] = []

    @property
    def state(self) -> dict:
        return self._states[min(self._index, len(self._states) - 1)]

    @property
    def url(self) -> str:
        if self._urls is None:
            return self.entry_url
        return self._urls[min(self._index, len(self._urls) - 1)]

    @property
    def observations(self) -> int:
        return len(
            [name for name in self.lookups if name == "locator:flt-semantics-host"]
        )

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waited_ms.append(int(milliseconds))
        self._index += 1

    def simulated_monotonic(self) -> float:
        return sum(self.waited_ms) / 1000.0

    def locator(self, selector: str):
        self.lookups.append("locator:" + selector)
        if selector == "flt-semantics-placeholder":
            return WitnessLocator(self.state["semantics_placeholder"]["count"], False, False)
        return WitnessLocator(self.state["hosts"][selector], False, False)

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        self.lookups.append(f"role:{role}:{name}")
        if role == "alert":
            # The strict rejection witness: an exact count, a visibility, or an
            # unreadable null. A reader failure is never "rejection absent".
            witness = self.state["rejection"]
            return WitnessLocator(witness["count"], witness["visible"], False)
        key = {
            "Billing Manager": "billing_manager",
            "Login": "login",
            "Enable accessibility": "enable_accessibility",
            "EMS": "ems",
        }[name]
        witness = self.state[key]
        return WitnessLocator(
            witness["count"], witness["visible"], witness.get("actionable", False)
        )

    def get_by_label(self, name: str, exact: bool = False):
        self.lookups.append("label:" + name)
        witness = self.state[{"Username": "username", "Password": "password"}[name]]
        return WitnessLocator(witness["count"], witness["visible"], False)


# Exactly the lookups one observation pass is permitted to make. Anything else -
# page text, HTML, a DOM or accessibility-tree dump, an attribute, a screenshot,
# a trace, storage, or network - would show up here as an extra entry.
ALLOWED_OBSERVATION_LOOKUPS = [
    "locator:flt-semantics-host",
    "locator:flt-semantics",
    "locator:flt-glass-pane",
    "locator:flt-text-editing-host",
    "locator:flt-scene-host",
    "locator:canvas",
    "locator:flt-semantics-placeholder",
    # The shared authentication witness set, read in one place for both the
    # normal login path and the diagnostic.
    "role:button:EMS",
    "label:Username",
    "label:Password",
    "role:button:Login",
    "role:button:Enable accessibility",
    "role:alert:None",
    # The Billing Manager observation, which remains an observation only.
    "role:link:Billing Manager",
]


@contextlib.contextmanager
def stubbed_observation(*observations):
    """Serve fixed observations so a sequence case needs no witness surface."""

    seen: list[int] = []

    def fake(self, page, remaining_ms, entry_url=None):
        index = min(len(seen), len(observations) - 1)
        seen.append(index)
        observation = dict(observations[index])
        if entry_url is not None:
            observation["url_changed"] = False
        return observation

    with mock.patch.object(PlaywrightPortal, "_observe_login_witnesses", fake):
        yield seen


class LoginDiagnosticSequenceTests(unittest.TestCase):
    """The diagnostic runs the canonical sequence once, and stops at the submit."""

    def run_diagnostic(self, page, observations=(SEMANTICS_ONLY,)):
        portal = PlaywrightPortal(FakeConfig(), headed=True)
        portal.page = page
        old = {
            name: os.environ.get(name)
            for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")
        }
        os.environ.update(runtime_credentials())
        try:
            with stubbed_observation(*observations):
                with mock.patch.object(portal_module, "time", create=True) as clock:
                    clock.monotonic.side_effect = page.simulated_monotonic
                    return portal.login_diagnostic()
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_the_canonical_pre_submit_sequence_is_reused_verbatim(self) -> None:
        """The diagnostic dispatches the same steps `login()` does, in the same order."""
        journal: list[str] = []
        page = login_page(
            activation=FakeLocator(journal=journal, label="activation"),
            placeholder=FakeLocator(journal=journal, label="placeholder"),
            login_entry=FakeLocator(journal=journal, label="login_entry"),
            username_field=FakeLocator(journal=journal, label="username"),
            password_field=FakeLocator(journal=journal, label="password"),
            submit=FakeLocator(journal=journal, label="submit"),
            journal=journal,
        )

        result = self.run_diagnostic(page)
        self.assertEqual(result.submit_outcome, portal_module.SUBMIT_DISPATCHED)
        # The committed successful sequence in full: `login()` now ends at the
        # one submit dispatch too, because the authentication proof that
        # follows it dispatches nothing.
        dispatched = len(SUCCESSFUL_LOGIN_SEQUENCE)
        self.assertEqual(journal[:dispatched], SUCCESSFUL_LOGIN_SEQUENCE)
        # Everything the bounded observation adds is a bare event-loop yield.
        # The provisional surface it is given keeps the window looking, and
        # looking dispatches nothing at all.
        self.assertEqual(
            [step for step in journal[dispatched:] if step != "page:wait_for_timeout"],
            [],
            "the observation only yields the event loop; it dispatches nothing",
        )

    def test_the_semantics_gate_and_login_entry_are_each_dispatched_once(self) -> None:
        activation = FakeLocator(label="activation")
        entry = FakeLocator(label="entry")
        # A distinct submit locator, so the shared Login role cannot let the one
        # submit be counted as a second entry click.
        page = login_page(
            activation=activation, login_entry=entry, submit=FakeLocator(label="submit")
        )

        self.run_diagnostic(page)
        self.assertEqual(activation.dispatched, 1, "exactly one semantics activation")
        self.assertEqual(entry.clicks, 1, "exactly one Login-entry click")
        self.assertEqual(entry.trial_clicks, 1, "proven actionable before the one click")

    def test_the_credentials_are_typed_once_each_with_the_committed_pacing(self) -> None:
        username = FakeLocator(label="username")
        password = FakeLocator(label="password")
        page = login_page(username_field=username, password_field=password)

        self.run_diagnostic(page)
        for field in (username, password):
            self.assertEqual(field.typed, 1)
            self.assertEqual(field.type_delays, [portal_module.LOGIN_KEY_ENTRY_DELAY_MS])

    def test_the_submit_is_dispatched_exactly_once_and_never_retried(self) -> None:
        submit = FakeLocator(label="submit")
        page = login_page(submit=submit)

        result = self.run_diagnostic(page)
        self.assertEqual(submit.clicks, 1, "one real submit, never retried")
        self.assertEqual(submit.trial_clicks, 1)
        self.assertTrue(result.submit_dispatched)
        self.assertEqual(result.submit_outcome, portal_module.SUBMIT_DISPATCHED)

    def test_a_dispatch_exception_is_uncertain_and_is_never_re_sent(self) -> None:
        submit = FakeLocator(click_error=step_failure(), label="submit")
        page = login_page(submit=submit)

        result = self.run_diagnostic(page)
        self.assertTrue(result.submit_dispatched)
        self.assertEqual(result.submit_outcome, portal_module.SUBMIT_DISPATCH_UNCERTAIN)
        self.assertEqual(submit.clicks, 1, "an uncertain dispatch is never re-sent")
        self.assertIsNone(result.failure, "an uncertain dispatch is reported, not raised")

    def test_a_dispatch_uncertain_run_may_still_classify(self) -> None:
        """The page is the only remaining evidence, and it is still read."""
        page = login_page(submit=FakeLocator(click_error=step_failure(), label="submit"))

        result = self.run_diagnostic(page, observations=(SEMANTICS_ONLY,))
        self.assertEqual(
            result.classification,
            portal_module.SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS,
        )
        self.assertEqual(
            result.submit_outcome,
            portal_module.SUBMIT_DISPATCH_UNCERTAIN,
            "observation never promotes an uncertain dispatch to a proven one",
        )

    def test_the_result_carries_the_authentication_verdict_it_observed(self) -> None:
        """The verdict comes from the settled post-submit observation only."""
        page = login_page()
        result = self.run_diagnostic(page, observations=(AUTHENTICATED_LANDING,))
        self.assertEqual(
            result.classification, portal_module.AUTHENTICATED_LANDING_PROVEN
        )
        self.assertEqual(result.authentication_outcome, portal_module.AUTHENTICATED)

        rejected = witnesses(hosts={"flt-semantics-host": 1}, alert=True)
        result = self.run_diagnostic(login_page(), observations=(rejected,))
        self.assertEqual(result.classification, portal_module.VISIBLE_ALERT)
        self.assertEqual(result.authentication_outcome, portal_module.REJECTED)

        result = self.run_diagnostic(login_page(), observations=(SEMANTICS_ONLY,))
        self.assertEqual(
            result.classification,
            portal_module.SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS,
        )
        self.assertEqual(
            result.authentication_outcome, portal_module.AUTHENTICATION_UNPROVED
        )

    def test_a_result_with_nothing_dispatched_is_never_authenticated(self) -> None:
        """Nothing was sent, so no authentication evidence exists."""
        submit = FakeLocator(count=0, label="absent_submit")
        result = self.run_diagnostic(login_page(submits=[submit]))
        self.assertIs(result.submit_dispatched, False)
        self.assertEqual(
            result.authentication_outcome, portal_module.AUTHENTICATION_UNPROVED
        )

    def test_a_pre_dispatch_readiness_failure_reports_not_dispatched(self) -> None:
        submit = FakeLocator(count=0, label="absent_submit")
        page = login_page(submits=[submit])

        result = self.run_diagnostic(page)
        self.assertFalse(result.submit_dispatched)
        self.assertEqual(result.submit_outcome, portal_module.SUBMIT_NOT_DISPATCHED)
        self.assertIsNone(result.classification)
        self.assertEqual(submit.clicks, 0)
        self.assertEqual(
            cli.support_ref_for(result.failure), "EG_LOGIN_SUBMIT_NOT_APPEAR"
        )

    def test_an_earlier_step_failure_keeps_its_own_bounded_reference(self) -> None:
        page = login_page(username_field=FakeLocator(type_error=step_failure()))

        result = self.run_diagnostic(page)
        self.assertFalse(result.submit_dispatched)
        self.assertEqual(result.submit_outcome, portal_module.SUBMIT_NOT_DISPATCHED)
        ref = cli.support_ref_for(result.failure)
        self.assertEqual(ref, "EG_LOGIN_USERNAME_FILL_FAILED")
        self.assertNotIn(STEP_FAILURE_TEXT, repr(result.failure.message))

    def test_missing_runtime_credentials_fail_closed_before_any_navigation(self) -> None:
        page = login_page()
        portal = PlaywrightPortal(FakeConfig(), headed=True)
        portal.page = page
        old = {
            name: os.environ.get(name)
            for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")
        }
        for name in old:
            os.environ.pop(name, None)
        try:
            with self.assertRaises(LoginError):
                portal.login_diagnostic()
        finally:
            for name, value in old.items():
                if value is not None:
                    os.environ[name] = value
        self.assertEqual(page.goto_calls, 0)

    def test_no_business_path_is_reachable_from_the_diagnostic(self) -> None:
        """Every post-login application behaviour is made to explode, and none runs."""
        forbidden = (
            "_await_authenticated_landing",
            "inventory",
            "download",
            "_open_verified_results",
            "_open_eb_bill_route",
            "_eb_bill_route_proven",
            "_settle_eb_bill_entry",
            "_observe_eb_bill_entry",
            "_dispatch_billing_manager",
            "_dispatch_eb_bill",
            "_await_invoice_list",
            "_advance_page",
            "_restore_page",
            "_resolve_pagination_control",
        )

        def explode(*_args, **_kwargs):
            raise AssertionError("the diagnostic reached a business path")

        page = login_page()
        with contextlib.ExitStack() as stack:
            for name in forbidden:
                stack.enter_context(mock.patch.object(PlaywrightPortal, name, explode))
            result = self.run_diagnostic(page)
        self.assertEqual(
            result.classification,
            portal_module.SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS,
        )

    def test_the_normal_login_path_still_ends_at_the_authenticated_landing(self) -> None:
        """The shared refactor must not have moved `login()` off its own contract."""
        journal: list[str] = []
        page = login_page(
            activation=FakeLocator(journal=journal, label="activation"),
            placeholder=FakeLocator(journal=journal, label="placeholder"),
            login_entry=FakeLocator(journal=journal, label="login_entry"),
            username_field=FakeLocator(journal=journal, label="username"),
            password_field=FakeLocator(journal=journal, label="password"),
            submit=FakeLocator(journal=journal, label="submit"),
            journal=journal,
        )
        portal = PlaywrightPortal(FakeConfig(), headed=False)
        portal.page = page
        old = {
            name: os.environ.get(name)
            for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")
        }
        os.environ.update(runtime_credentials())
        try:
            with mock.patch.object(portal_module, "time", create=True) as clock:
                clock.monotonic.side_effect = page.simulated_monotonic
                portal.login()
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertEqual(journal, SUCCESSFUL_LOGIN_SEQUENCE)


class LoginDiagnosticObservationTests(unittest.TestCase):
    """The observation reads a closed allowlist and stays inside the shared deadline."""

    def observe(self, page):
        portal = PlaywrightPortal(FakeConfig(), headed=True)
        portal.page = page
        return portal._observe_login_witnesses(page, portal_module.MAX_PORTAL_PROBE_TIMEOUT_MS)

    def test_the_observation_reads_exactly_the_allowlisted_witnesses(self) -> None:
        page = WitnessPage([SEMANTICS_ONLY])
        self.observe(page)
        # The visibility and actionability witnesses resolve a fresh locator per
        # question, so compare the distinct lookups in first-seen order.
        seen: list[str] = []
        for name in page.lookups:
            if name not in seen:
                seen.append(name)
        self.assertEqual(seen, ALLOWED_OBSERVATION_LOOKUPS)

    def test_the_observation_reproduces_the_surface_it_was_given(self) -> None:
        state = witnesses(
            hosts={"flt-semantics-host": 1, "flt-glass-pane": 2, "canvas": 1},
            placeholder=1,
            billing_manager=(1, True),
            username=(1, False),
            password=(0, False),
            login=(1, True, True),
            enable_accessibility=(0, False, False),
            alert=False,
        )
        self.assertEqual(self.observe(WitnessPage([state])), state)

    def test_every_observed_value_is_a_count_a_boolean_or_null(self) -> None:
        observed = self.observe(WitnessPage([SEMANTICS_ONLY]))

        def check(value) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertIsInstance(key, str)
                    check(item)
                return
            self.assertIsInstance(
                value, (bool, int, type(None)), "no free-form value may be observed"
            )
            self.assertNotIsInstance(value, str)

        check(observed)

    def test_an_unreadable_witness_is_null_rather_than_a_guess(self) -> None:
        state = witnesses(hosts={"flt-semantics-host": None}, billing_manager=(None, None))
        observed = self.observe(WitnessPage([state]))
        self.assertIsNone(observed["hosts"]["flt-semantics-host"])
        self.assertIsNone(observed["billing_manager"]["count"])
        self.assertIsNone(observed["billing_manager"]["visible"])

    def test_only_a_url_changed_boolean_is_ever_reported(self) -> None:
        portal = PlaywrightPortal(FakeConfig(), headed=True)
        moved = WitnessPage([SEMANTICS_ONLY], urls=["http://127.0.0.1:1/after"])
        observed = portal._observe_login_witnesses(
            moved, portal_module.MAX_PORTAL_PROBE_TIMEOUT_MS, entry_url=moved.entry_url
        )
        self.assertIs(observed["url_changed"], True)
        stayed = WitnessPage([SEMANTICS_ONLY])
        observed = portal._observe_login_witnesses(
            stayed, portal_module.MAX_PORTAL_PROBE_TIMEOUT_MS, entry_url=stayed.entry_url
        )
        self.assertIs(observed["url_changed"], False)
        for document in (observed,):
            self.assertNotIn("url", document)
            self.assertNotIn("http", repr(document))

    def test_the_shared_authentication_witnesses_are_read_in_one_place(self) -> None:
        """The login path and the diagnostic cannot carry two witness sets."""
        page = WitnessPage([AUTHENTICATED_LANDING])
        portal = PlaywrightPortal(FakeConfig(), headed=True)
        portal.page = page
        shared = portal._observe_authentication_witnesses(
            page, portal_module.MAX_PORTAL_PROBE_TIMEOUT_MS
        )
        observed = self.observe(WitnessPage([AUTHENTICATED_LANDING]))
        for name in ("ems", "username", "password", "login", "enable_accessibility", "rejection"):
            with self.subTest(witness=name):
                self.assertEqual(shared[name], observed[name])
        self.assertEqual(
            set(shared),
            {"ems", "username", "password", "login", "enable_accessibility", "rejection"},
            "the authentication reader reads the authentication witnesses and no more",
        )

    def test_the_historical_alert_boolean_is_derived_from_the_strict_witness(self) -> None:
        """`visible_alert` is preserved, and absence is never proven from it."""
        for case, override, expected_alert in (
            ("visible", {"rejection": (1, True)}, True),
            ("absent", {"rejection": (0, False)}, False),
            ("hidden", {"rejection": (1, False)}, False),
            ("unreadable", {"rejection": (None, None)}, False),
        ):
            with self.subTest(case=case):
                count, visible = override["rejection"]
                observed = self.observe(WitnessPage([witnesses(**override)]))
                self.assertIs(observed["visible_alert"], expected_alert)
                self.assertEqual(
                    observed["rejection"], {"count": count, "visible": visible}
                )

    def test_the_unobserved_shape_is_the_same_closed_shape(self) -> None:
        observed = self.observe(WitnessPage([SEMANTICS_ONLY]))
        unobserved = portal_module.unobserved_login_witnesses()
        self.assertEqual(set(observed), set(unobserved))
        self.assertEqual(set(observed["hosts"]), set(unobserved["hosts"]))
        with_url = portal_module.unobserved_login_witnesses(include_url=True)
        self.assertEqual(set(with_url) - set(unobserved), {"url_changed"})


class LoginDiagnosticClassificationTests(unittest.TestCase):
    """The fail-closed classification matrix, arm by arm."""

    def classify(self, observation):
        return PlaywrightPortal(FakeConfig(), headed=True)._classify_post_submit(observation)

    def settle(self, states, pre_submit=NOTHING_AT_ALL, urls=None):
        """Run the whole bounded post-submit window against a witness surface."""
        page = WitnessPage(states, urls=urls)
        portal = PlaywrightPortal(FakeConfig(), headed=True)
        portal.page = page
        with mock.patch.object(portal_module, "time", create=True) as clock:
            clock.monotonic.side_effect = page.simulated_monotonic
            observation, classification = portal._observe_after_submit(
                page, pre_submit, page.entry_url
            )
        return page, observation, classification

    # ---- the six accepted classifications ---- #

    def test_billing_manager_visible(self) -> None:
        state = witnesses(hosts={"flt-semantics-host": 1}, billing_manager=(1, True))
        self.assertEqual(self.classify(state), portal_module.BILLING_MANAGER_VISIBLE)
        _page, _observed, classification = self.settle([state])
        self.assertEqual(classification, portal_module.BILLING_MANAGER_VISIBLE)

    def test_visible_alert_outranks_route_and_shell_inference(self) -> None:
        self.assertEqual(self.classify(ALERT_SURFACE), portal_module.VISIBLE_ALERT)
        page, _observed, classification = self.settle([ALERT_SURFACE])
        self.assertEqual(classification, portal_module.VISIBLE_ALERT)
        self.assertEqual(
            page.waited_ms, [], "a decisive alert is terminal on sight, not waited out"
        )

    def test_login_route_persisted_or_returned(self) -> None:
        self.assertEqual(
            self.classify(LOGIN_ROUTE_ONLY),
            portal_module.LOGIN_ROUTE_PERSISTED_OR_RETURNED,
        )
        page, _observed, classification = self.settle([LOGIN_ROUTE_ONLY])
        self.assertEqual(classification, portal_module.LOGIN_ROUTE_PERSISTED_OR_RETURNED)
        self.assertEqual(
            page.observations,
            len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS),
            "a persistent login route is concluded from the final look, not the first",
        )

    def test_semantics_host_present_without_app_controls(self) -> None:
        state = witnesses(hosts={"flt-semantics": 1, "flt-glass-pane": 1})
        self.assertEqual(
            self.classify(state),
            portal_module.SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS,
        )
        page, _observed, classification = self.settle([state])
        self.assertEqual(
            classification, portal_module.SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS
        )
        self.assertEqual(
            page.observations,
            len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS),
            "a persistent semantics host is concluded from the final look",
        )

    def test_flutter_render_shell_present_semantics_host_absent(self) -> None:
        state = witnesses(hosts={"flt-glass-pane": 1, "canvas": 2})
        self.assertEqual(
            self.classify(state),
            portal_module.FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT,
        )
        page, _observed, classification = self.settle([state])
        self.assertEqual(
            classification,
            portal_module.FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT,
        )
        self.assertEqual(
            page.observations,
            len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS),
            "a persistent render shell is concluded from the final look",
        )

    def test_flutter_shell_disappeared_after_submit(self) -> None:
        page, _observed, classification = self.settle(
            [NOTHING_AT_ALL], pre_submit=SHELL_ONLY
        )
        self.assertEqual(
            classification, portal_module.FLUTTER_SHELL_DISAPPEARED_AFTER_SUBMIT
        )
        self.assertEqual(
            page.observations,
            len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS),
            "disappearance is only concluded after the whole window ran",
        )

    def test_every_accepted_classification_has_coverage(self) -> None:
        """The declared vocabulary, in priority order.

        The six historical observational classifications are retained exactly,
        in their original relative order, and the two authentication
        classifications are added around them: the proven landing leads, and
        the positively-counted-but-unproved witness is the last observational
        arm before the throughout-window disappearance verdict.
        """
        self.assertEqual(
            portal_module.LOGIN_DIAGNOSTIC_CLASSIFICATIONS,
            (
                "AUTHENTICATED_LANDING_PROVEN",
                "BILLING_MANAGER_VISIBLE",
                "VISIBLE_ALERT",
                "LOGIN_ROUTE_PERSISTED_OR_RETURNED",
                "SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS",
                "FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT",
                "AUTHENTICATION_UNPROVED",
                "FLUTTER_SHELL_DISAPPEARED_AFTER_SUBMIT",
            ),
        )
        # The six historical classifications survive, in their original order.
        historical = (
            "BILLING_MANAGER_VISIBLE",
            "VISIBLE_ALERT",
            "LOGIN_ROUTE_PERSISTED_OR_RETURNED",
            "SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS",
            "FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT",
            "FLUTTER_SHELL_DISAPPEARED_AFTER_SUBMIT",
        )
        declared = portal_module.LOGIN_DIAGNOSTIC_CLASSIFICATIONS
        self.assertEqual(
            tuple(name for name in declared if name in historical), historical
        )

    # ---- settling: only two outcomes are terminal on sight ---- #
    #
    # DL-XB-141-ASTRA-SIMPLIFY-001. A post-submit Flutter route settles through
    # the login route it came from and through a bare shell, so concluding
    # either on sight ended the observation before Billing Manager appeared.

    def test_the_settling_split_covers_the_declared_vocabulary_in_order(self) -> None:
        """The split is over the declared vocabulary; it reorders nothing.

        A clean authenticated landing joins the terminal group, because a later
        look cannot improve on positive authentication proof. A contradictory
        or incomplete authentication reading is provisional like every other
        unsettled surface.
        """
        self.assertEqual(
            portal_module.DIAGNOSTIC_IMMEDIATE_CLASSIFICATIONS,
            ("AUTHENTICATED_LANDING_PROVEN", "BILLING_MANAGER_VISIBLE", "VISIBLE_ALERT"),
        )
        self.assertEqual(
            portal_module.DIAGNOSTIC_CONTINUABLE_CLASSIFICATIONS,
            (
                "LOGIN_ROUTE_PERSISTED_OR_RETURNED",
                "SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS",
                "FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT",
                "AUTHENTICATION_UNPROVED",
            ),
        )
        self.assertEqual(
            portal_module.DIAGNOSTIC_IMMEDIATE_CLASSIFICATIONS
            + portal_module.DIAGNOSTIC_CONTINUABLE_CLASSIFICATIONS
            + (portal_module.FLUTTER_SHELL_DISAPPEARED_AFTER_SUBMIT,),
            portal_module.LOGIN_DIAGNOSTIC_CLASSIFICATIONS,
        )

    def test_a_transient_login_route_does_not_end_the_window(self) -> None:
        """The reported defect: the login route is seen first, Billing Manager later."""
        page, observed, classification = self.settle(
            [LOGIN_ROUTE_ONLY, LOGIN_ROUTE_ONLY, BILLING_MANAGER_READY]
        )
        self.assertEqual(classification, portal_module.BILLING_MANAGER_VISIBLE)
        self.assertEqual(page.observations, 3, "Billing Manager is still terminal")
        self.assertEqual(
            observed,
            BILLING_MANAGER_READY | {"url_changed": False},
            "the reported observation is the one that settled, not an earlier look",
        )

    def test_a_transient_semantics_host_does_not_end_the_window(self) -> None:
        page, _observed, classification = self.settle(
            [SEMANTICS_ONLY, SEMANTICS_ONLY, BILLING_MANAGER_READY]
        )
        self.assertEqual(classification, portal_module.BILLING_MANAGER_VISIBLE)
        self.assertEqual(page.observations, 3)

    def test_a_transient_render_shell_does_not_end_the_window(self) -> None:
        page, _observed, classification = self.settle(
            [SHELL_ONLY, SHELL_ONLY, BILLING_MANAGER_READY]
        )
        self.assertEqual(classification, portal_module.BILLING_MANAGER_VISIBLE)
        self.assertEqual(page.observations, 3)

    def test_a_transient_route_never_outranks_a_later_alert(self) -> None:
        page, _observed, classification = self.settle([SHELL_ONLY, ALERT_SURFACE])
        self.assertEqual(classification, portal_module.VISIBLE_ALERT)
        self.assertEqual(page.observations, 2)

    def test_a_billing_manager_on_the_last_look_still_wins(self) -> None:
        """A login route for all but the final checkpoint is still superseded."""
        looks = len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS)
        page, _observed, classification = self.settle(
            [LOGIN_ROUTE_ONLY] * (looks - 1) + [BILLING_MANAGER_READY]
        )
        self.assertEqual(classification, portal_module.BILLING_MANAGER_VISIBLE)
        self.assertEqual(page.observations, looks)
        self.assertLessEqual(
            page.simulated_monotonic(),
            portal_module.PORTAL_RECOVERY_DEADLINE_SECONDS,
            "waiting a transient route out never raises the shared ceiling",
        )

    def test_a_provisional_reading_is_never_kept_for_an_unreadable_final_look(
        self,
    ) -> None:
        """A route seen earlier is evidence about then, not about the final surface."""
        unreadable = witnesses(hosts={"flt-semantics-host": None, "flt-glass-pane": 1})
        page, _observed, classification = self.settle([LOGIN_ROUTE_ONLY, unreadable])
        self.assertIsNone(
            classification, "an unreadable final look classifies as nothing at all"
        )
        self.assertEqual(
            page.observations, len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS)
        )

    # ---- DL-XB-141-AUTH-LANDING-NAV-SEPARATION-G2-001 ---- #

    def test_a_clean_authenticated_landing_classifies_and_settles_at_once(self) -> None:
        """D04. The proven landing leads the priority order and is terminal."""
        self.assertEqual(
            self.classify(AUTHENTICATED_LANDING),
            portal_module.AUTHENTICATED_LANDING_PROVEN,
        )
        page, _observed, classification = self.settle([AUTHENTICATED_LANDING])
        self.assertEqual(classification, portal_module.AUTHENTICATED_LANDING_PROVEN)
        self.assertEqual(
            page.waited_ms, [], "positive authentication proof is not waited out"
        )

    def test_the_landing_classification_needs_no_billing_manager(self) -> None:
        """A02 at the classifier: Billing Manager is not an authentication oracle."""
        self.assertEqual(AUTHENTICATED_LANDING["billing_manager"]["count"], 0)
        self.assertEqual(
            self.classify(AUTHENTICATED_LANDING),
            portal_module.AUTHENTICATED_LANDING_PROVEN,
        )
        # And a Billing Manager on its own still classifies as it always did,
        # while saying nothing at all about authentication.
        self.assertEqual(
            self.classify(BILLING_MANAGER_READY), portal_module.BILLING_MANAGER_VISIBLE
        )

    def test_a_clean_rejection_classifies_as_the_historical_alert(self) -> None:
        """D05. The rejection reading keeps its historical classification."""
        rejection = witnesses(hosts={"flt-semantics-host": 1}, alert=True)
        self.assertEqual(self.classify(rejection), portal_module.VISIBLE_ALERT)
        portal = PlaywrightPortal(FakeConfig(), headed=True)
        self.assertEqual(
            portal._authentication_outcome(rejection), portal_module.REJECTED
        )

    def test_a_counted_witness_that_never_proves_classifies_as_unproved(self) -> None:
        """The last observational arm, and only from positive evidence."""
        for case, override in (
            ("duplicate", {"ems": (2, True)}),
            ("hidden", {"ems": (1, False)}),
            ("contradicted_by_a_hidden_login", {"ems": (1, True), "login": (1, False, False)}),
        ):
            with self.subTest(case=case):
                state = witnesses(hosts={"flt-semantics-host": 1}, **override)
                self.assertEqual(
                    self.classify(state), portal_module.AUTHENTICATION_UNPROVED
                )
        # An unreadable witness is not evidence about authentication, and an
        # absent one is not either: both still classify as nothing at all.
        for case, override in (
            ("unreadable", {"ems": (None, None)}),
            ("absent", {"ems": (0, False)}),
        ):
            with self.subTest(case=case):
                self.assertIsNone(
                    self.classify(witnesses(billing_manager=(1, False), **override))
                )

    def test_a_counted_witness_blocks_a_shell_classification(self) -> None:
        """EMS is an application control, so it blocks a shell-only reading."""
        self.assertIn("ems", portal_module.DIAGNOSTIC_CONTROL_WITNESSES)
        state = witnesses(hosts={"flt-semantics-host": 1}, ems=(1, False))
        self.assertNotEqual(
            self.classify(state),
            portal_module.SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS,
        )

    def test_a_transient_unproved_witness_does_not_end_the_window(self) -> None:
        """A landing that settles late is still reached inside the window."""
        early = witnesses(hosts={"flt-semantics-host": 1}, ems=(2, True))
        page, _observed, classification = self.settle(
            [early, early, AUTHENTICATED_LANDING]
        )
        self.assertEqual(classification, portal_module.AUTHENTICATED_LANDING_PROVEN)
        self.assertEqual(page.observations, 3)

    def test_a_historical_shell_reading_carries_an_unproved_outcome(self) -> None:
        """D06. Diagnostically complete, and still not authenticated."""
        portal = PlaywrightPortal(FakeConfig(), headed=True)
        for state in (SEMANTICS_ONLY, SHELL_ONLY, BILLING_MANAGER_READY, LOGIN_ROUTE_ONLY):
            with self.subTest(state=self.classify(state)):
                self.assertIn(state["ems"]["count"], (0,))
                self.assertEqual(
                    portal._authentication_outcome(state),
                    portal_module.AUTHENTICATION_UNPROVED,
                )
                self.assertIn(
                    self.classify(state), portal_module.LOGIN_DIAGNOSTIC_CLASSIFICATIONS
                )

    # ---- Web's visibility and absence clarification ---- #

    def test_a_hidden_billing_manager_does_not_classify(self) -> None:
        self.assertIsNone(self.classify(witnesses(billing_manager=(1, False))))

    def test_multiple_billing_manager_matches_do_not_classify(self) -> None:
        self.assertIsNone(self.classify(witnesses(billing_manager=(2, True))))

    def test_hidden_login_controls_do_not_prove_the_login_route(self) -> None:
        """Counted but hidden is ambiguous evidence, and ambiguity fails closed."""
        state = witnesses(
            hosts={"flt-semantics-host": 1},
            username=(1, False),
            password=(1, False),
            login=(1, False, False),
        )
        self.assertIsNone(self.classify(state))

    def test_a_counted_enable_accessibility_blocks_every_shell_classification(self) -> None:
        blocked = witnesses(
            hosts={"flt-semantics-host": 1, "flt-glass-pane": 1},
            enable_accessibility=(1, False, False),
        )
        self.assertIsNone(self.classify(blocked))
        _page, _observed, classification = self.settle([blocked], pre_submit=SHELL_ONLY)
        self.assertIsNone(
            classification, "a counted public gate also blocks disappearance"
        )

    def test_each_known_control_blocks_a_shell_classification_by_count(self) -> None:
        for name, override in (
            ("billing_manager", {"billing_manager": (1, False)}),
            ("username", {"username": (1, False)}),
            ("password", {"password": (1, False)}),
            ("login", {"login": (1, False, False)}),
            ("enable_accessibility", {"enable_accessibility": (1, False, False)}),
        ):
            with self.subTest(control=name):
                state = witnesses(hosts={"flt-semantics-host": 1}, **override)
                self.assertIsNone(self.classify(state))

    def test_an_unreadable_host_count_blocks_a_shell_classification(self) -> None:
        state = witnesses(hosts={"flt-semantics-host": None, "flt-glass-pane": 1})
        self.assertIsNone(self.classify(state))

    def test_shell_disappearance_requires_a_positive_pre_submit_witness(self) -> None:
        _page, _observed, classification = self.settle(
            [NOTHING_AT_ALL], pre_submit=NOTHING_AT_ALL
        )
        self.assertIsNone(
            classification, "a shell that was never established cannot disappear"
        )

    def test_shell_disappearance_requires_the_shell_to_stay_gone(self) -> None:
        """A shell that comes back classifies as present, never as disappeared."""
        page, _observed, classification = self.settle(
            [NOTHING_AT_ALL, NOTHING_AT_ALL, SHELL_ONLY], pre_submit=SHELL_ONLY
        )
        self.assertEqual(
            classification,
            portal_module.FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT,
        )
        self.assertEqual(
            page.observations,
            len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS),
            "a returning shell is provisional, so the whole window still ran",
        )

    def test_a_control_seen_earlier_blocks_a_later_disappearance_verdict(self) -> None:
        page, _observed, classification = self.settle(
            [witnesses(login=(1, False, False)), NOTHING_AT_ALL], pre_submit=SHELL_ONLY
        )
        self.assertIsNone(classification)
        self.assertEqual(page.observations, len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS))

    def test_a_surface_that_never_settles_fails_closed(self) -> None:
        never = witnesses(billing_manager=(1, False))
        page, observed, classification = self.settle([never], pre_submit=SHELL_ONLY)
        self.assertIsNone(classification)
        self.assertEqual(observed, never | {"url_changed": False})

    def test_the_post_submit_window_stays_inside_the_shared_deadline(self) -> None:
        page, _observed, classification = self.settle(
            [witnesses(billing_manager=(1, False))], pre_submit=SHELL_ONLY
        )
        self.assertIsNone(classification)
        self.assertEqual(page.observations, len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS))
        self.assertLessEqual(
            page.simulated_monotonic(),
            portal_module.PORTAL_RECOVERY_DEADLINE_SECONDS,
            "the diagnostic reuses the shared 60-second ceiling and never raises it",
        )
        self.assertEqual(portal_module.PORTAL_RECOVERY_DEADLINE_SECONDS, 60.0)

    def test_a_settled_surface_ends_the_window_immediately(self) -> None:
        page, _observed, classification = self.settle(
            [witnesses(billing_manager=(1, True))]
        )
        self.assertEqual(classification, portal_module.BILLING_MANAGER_VISIBLE)
        self.assertEqual(page.waited_ms, [], "a settled answer is not waited out")


# The private, hostile text an unexpected infrastructure failure is made to
# carry. Nothing in it may reach stdout, stderr or the emitted document.
UNEXPECTED_OBSERVATION_TEXT = (
    "synthetic private hostile value: password=hunter2 at "
    "https://portal.example.invalid/session for SYNTHETIC-INTENDED-ACCOUNT"
)
UNEXPECTED_OBSERVATION_FRAGMENTS = (
    "synthetic private hostile value",
    "hunter2",
    "https://",
    "portal.example.invalid",
    "SYNTHETIC-INTENDED-ACCOUNT",
    "Traceback",
    "RuntimeError",
)


class UnobservablePage(FakePage):
    """A page whose event-loop yield fails once the one Login submit is sent.

    This is the reported shape verbatim: the shared recovery yields with
    `page.wait_for_timeout(...)` outside its probe handling, so an ordinary
    exception raised there escapes the post-submit observation entirely rather
    than being absorbed as "not settled yet". The failure is armed by the one
    real submit click, so nothing before the dispatch boundary is disturbed.
    """

    @property
    def submit_clicks(self) -> int:
        return sum(locator.clicks for locator in self._submit_locators())

    def _submit_locators(self) -> list[FakeLocator]:
        locators = list(self.submits or ())
        if self.submit is not None:
            locators.append(self.submit)
        return locators

    def wait_for_timeout(self, milliseconds: int) -> None:
        if self.submit_clicks:
            raise RuntimeError(UNEXPECTED_OBSERVATION_TEXT)
        super().wait_for_timeout(milliseconds)


def real_diagnostic_portal(page: FakePage):
    """The real portal, driven against a fake page instead of a browser.

    Only browser acquisition is replaced. `login_diagnostic()` itself, the
    canonical pre-submit sequence, the one normal submit and the post-submit
    observation are the production ones, so a case exercises the real CLI
    boundary end to end without Playwright, a browser or a portal.
    """

    class RealDiagnosticPortal(PlaywrightPortal):
        def __init__(self, config, headed: bool = False) -> None:
            super().__init__(config, headed=headed)
            self.page = page

        def __enter__(self) -> "RealDiagnosticPortal":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    return RealDiagnosticPortal


class LoginDiagnosticUnexpectedFailureTests(unittest.TestCase):
    """An unexpected ordinary exception still yields one truthful document.

    The diagnostic contract is fail-closed on output as well as on outcome:
    whatever goes wrong, exactly one bounded `energygrid.login_diagnostic.v1`
    document is emitted, the already-established submit truth is preserved, and
    no traceback or free-form exception text reaches any output surface.
    """

    def write_diagnostic_config(self, root: Path) -> Path:
        (root / "archive").mkdir()
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    # Never contacted: the portal is a fake page throughout.
                    "portal_url": "http://127.0.0.1:1/synthetic",
                    "account_identity": "SYNTHETIC-INTENDED-ACCOUNT",
                    "archive_root": str(root / "archive"),
                    "state_path": str(root / "state" / "state.sqlite3"),
                    "temp_root": str(root / "temp"),
                    "log_root": str(root / "logs"),
                    "timeout_seconds": 5,
                    "max_attempts": 2,
                    "inventory_safety_ceiling": 50,
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def run_cli_diagnostic(self, page: FakePage, observations=(NOTHING_AT_ALL,)):
        """Run the real `login-diagnostic` command against `page`."""

        out = io.StringIO()
        err = io.StringIO()
        old = {
            name: os.environ.get(name)
            for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD")
        }
        credentials = runtime_credentials()
        os.environ.update(credentials)
        try:
            with tempfile.TemporaryDirectory() as name:
                root = Path(name)
                config_path = self.write_diagnostic_config(root)
                with mock.patch.object(cli, "PlaywrightPortal", real_diagnostic_portal(page)), \
                        stubbed_observation(*observations), \
                        mock.patch.object(portal_module, "time", create=True) as clock, \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    clock.monotonic.side_effect = page.simulated_monotonic
                    exit_code = main(["login-diagnostic", "--config", str(config_path)])
                emitted = out.getvalue() + err.getvalue()
                self.assertNotIn(str(root), emitted, "no filesystem path is ever emitted")
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        return exit_code, out.getvalue(), err.getvalue(), credentials

    def one_document(self, out: str) -> dict:
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1, "exactly one document")
        return json.loads(lines[0])

    def assert_nothing_private_escaped(self, out: str, err: str, credentials: dict) -> None:
        emitted = out + err
        self.assertEqual(err, "", "no stderr surface at all, traceback or otherwise")
        for fragment in UNEXPECTED_OBSERVATION_FRAGMENTS + tuple(credentials.values()):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, emitted)
        self.assertNotIn(STEP_FAILURE_TEXT, emitted)

    def assert_unclassified_envelope(self, document: dict) -> None:
        self.assertEqual(document["schema"], cli.LOGIN_DIAGNOSTIC_SCHEMA)
        self.assertEqual(document["status"], ACTION_REQUIRED)
        self.assertIsNone(document["classification"])
        self.assertEqual(
            document["support_ref"], cli.DIAGNOSTIC_UNCLASSIFIED_SUPPORT_REF
        )
        self.assertEqual(
            document["post_submit"],
            portal_module.unobserved_login_witnesses(include_url=True),
            "an observation that could not be completed reports the unobserved shape",
        )

    # ---- after the one submit has already been dispatched ---- #

    def test_an_unexpected_post_submit_failure_keeps_a_proven_dispatch(self) -> None:
        """A proven dispatch stays proven: the observation failed, not the submit."""
        submit = FakeLocator(label="submit")
        page = login_page(page_cls=UnobservablePage, submit=submit)

        exit_code, out, err, credentials = self.run_cli_diagnostic(page)

        document = self.one_document(out)
        self.assertEqual(exit_code, 20)
        self.assert_unclassified_envelope(document)
        self.assertIs(document["submit_dispatched"], True)
        self.assertEqual(document["submit_outcome"], portal_module.SUBMIT_DISPATCHED)
        self.assertEqual(submit.clicks, 1, "one real submit, never retried")
        self.assert_nothing_private_escaped(out, err, credentials)

    def test_an_unexpected_post_submit_failure_keeps_an_uncertain_dispatch(self) -> None:
        """An uncertain dispatch stays uncertain: it is never promoted or demoted."""
        submit = FakeLocator(click_error=step_failure(), label="submit")
        page = login_page(page_cls=UnobservablePage, submit=submit)

        exit_code, out, err, credentials = self.run_cli_diagnostic(page)

        document = self.one_document(out)
        self.assertEqual(exit_code, 20)
        self.assert_unclassified_envelope(document)
        self.assertIs(document["submit_dispatched"], True)
        self.assertEqual(
            document["submit_outcome"], portal_module.SUBMIT_DISPATCH_UNCERTAIN
        )
        self.assertEqual(submit.clicks, 1, "an uncertain dispatch is never re-sent")
        self.assert_nothing_private_escaped(out, err, credentials)

    def test_the_failed_observation_never_reaches_a_business_path(self) -> None:
        """The envelope is a return, not a new route into the application."""

        def explode(*_args, **_kwargs):
            raise AssertionError("the diagnostic reached a business path")

        submit = FakeLocator(label="submit")
        page = login_page(page_cls=UnobservablePage, submit=submit)
        with contextlib.ExitStack() as stack:
            for name in (
                "_await_authenticated_landing",
                "_open_verified_results",
                "_open_eb_bill_route",
                "inventory",
                "download",
            ):
                stack.enter_context(mock.patch.object(PlaywrightPortal, name, explode))
            exit_code, out, _err, _credentials = self.run_cli_diagnostic(page)
        self.assertEqual(exit_code, 20)
        self.assertIs(self.one_document(out)["submit_dispatched"], True)

    # ---- before anything was dispatched ---- #

    def test_an_unexpected_pre_dispatch_failure_reports_not_dispatched(self) -> None:
        """Nothing was sent, so the closed unobserved document says exactly that."""
        submit = FakeLocator(label="submit")
        page = login_page(page_cls=UnobservablePage, submit=submit)

        def unexpected(*_args, **_kwargs):
            raise RuntimeError(UNEXPECTED_OBSERVATION_TEXT)

        with mock.patch.object(PlaywrightPortal, "_require_page", unexpected):
            exit_code, out, err, credentials = self.run_cli_diagnostic(page)

        document = self.one_document(out)
        self.assertEqual(exit_code, 20)
        self.assertEqual(document["schema"], cli.LOGIN_DIAGNOSTIC_SCHEMA)
        self.assertEqual(document["status"], ACTION_REQUIRED)
        self.assertIsNone(document["classification"])
        self.assertIs(document["submit_dispatched"], False)
        self.assertEqual(document["submit_outcome"], portal_module.SUBMIT_NOT_DISPATCHED)
        self.assertEqual(document["support_ref"], cli.UNCLASSIFIED_SUPPORT_REF)
        self.assertEqual(
            document["pre_submit"], portal_module.unobserved_login_witnesses()
        )
        self.assertEqual(
            document["post_submit"],
            portal_module.unobserved_login_witnesses(include_url=True),
        )
        self.assertEqual(submit.clicks, 0, "nothing was ever dispatched")
        self.assertEqual(page.goto_calls, 0)
        self.assert_nothing_private_escaped(out, err, credentials)


if __name__ == "__main__":
    unittest.main()
