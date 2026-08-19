from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
import uuid

from energygrid_bill_downloader import cli
from energygrid_bill_downloader.config import load_runtime_config
from energygrid_bill_downloader.cli import main
from energygrid_bill_downloader.errors import (
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
    """The slice of the Playwright locator surface the login path touches."""

    def __init__(
        self,
        *,
        count: int = 1,
        visible: bool = True,
        enabled: bool = True,
        wait_error: Exception | None = None,
        state_error: Exception | None = None,
    ) -> None:
        self._count = count
        self._visible = visible
        self._enabled = enabled
        self._wait_error = wait_error
        self._state_error = state_error
        self.dispatched = 0
        self.clicks = 0

    @property
    def first(self) -> "FakeLocator":
        return self

    def wait_for(self, state: str | None = None) -> None:
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

    def click(self) -> None:
        self.clicks += 1

    def fill(self, value: str) -> None:
        return None


class FakePage:
    """A page whose only job is to hand the login path the locators under test."""

    def __init__(
        self,
        activation: FakeLocator,
        placeholder: FakeLocator,
        login_entry: FakeLocator,
        *,
        label_error: Exception | None = None,
        alert_visible: bool = False,
    ) -> None:
        self.activation = activation
        self.placeholder = placeholder
        self.login_entry = login_entry
        self.label_error = label_error
        self.alert_visible = alert_visible
        self.goto_calls = 0

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.goto_calls += 1

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        if name == "Enable accessibility":
            return self.activation
        if name == "Login":
            return self.login_entry
        if role == "alert":
            return FakeLocator(visible=self.alert_visible)
        if name == "Billing Manager":
            return FakeLocator()
        raise AssertionError(f"unexpected role lookup: {role}/{name}")

    def locator(self, selector: str):
        assert selector == "flt-semantics-placeholder", selector
        return self.placeholder

    def get_by_label(self, name: str, exact: bool = False):
        if self.label_error is not None:
            raise self.label_error
        return FakeLocator()


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

    def test_downstream_login_control_fallback_is_referenced(self) -> None:
        """The generic arm of `login()`, reached once the semantics gate is open."""
        page = FakePage(
            FakeLocator(),
            FakeLocator(),
            FakeLocator(),
            label_error=RuntimeError("Username field is gone"),
            alert_visible=False,
        )
        error = self.run_login(page)
        self.assertIsInstance(error, LayoutChangedError)
        self.assert_maps_non_generically(error, "EG_LOGIN_REQUIRED_CONTROL_UNRESOLVED")

    def test_portal_rejection_stays_distinct_from_layout_drift(self) -> None:
        """Same broken step, but a visible alert means credentials, not layout."""
        page = FakePage(
            FakeLocator(),
            FakeLocator(),
            FakeLocator(),
            label_error=RuntimeError("Username field is gone"),
            alert_visible=True,
        )
        error = self.run_login(page)
        self.assertIsInstance(error, LoginError)
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_PORTAL_REJECTED")

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

        broken_form = RuntimeError("Username field is gone")
        record(self.run_login(FakePage(FakeLocator(), FakeLocator(), FakeLocator(), label_error=broken_form)))
        record(
            self.run_login(
                FakePage(
                    FakeLocator(), FakeLocator(), FakeLocator(), label_error=broken_form, alert_visible=True
                )
            )
        )
        record(self.run_login(FakePage(FakeLocator(), FakeLocator(), FakeLocator()), credentials=False))

        self.assertEqual(
            reached,
            set(cli.SUPPORT_REFS_BY_MESSAGE.values()),
            "the committed reference vocabulary and the reachable login branches must match",
        )


if __name__ == "__main__":
    unittest.main()
