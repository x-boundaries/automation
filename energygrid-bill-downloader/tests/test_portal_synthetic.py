from __future__ import annotations

import contextlib
import dataclasses
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
from tests.fixtures.synthetic_portal import SyntheticBill, SyntheticPortalServer, synthetic_pdf, write_config

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
    def config_for(self, server: SyntheticPortalServer, root: Path, **overrides):
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
        raw.update(overrides)
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

    # ---- DL-XB-199: the live single-surface, download-first production path ---- #

    def portal_run(self, server: SyntheticPortalServer, root: Path, body, **config_overrides):
        """Log in against `server` and hand `body` the portal; restore credentials."""

        config = self.config_for(server, root, **config_overrides)
        old, _values = self.with_credentials()
        try:
            with PlaywrightPortal(config) as portal:
                portal.login()
                return body(portal)
        finally:
            self.restore_credentials(old)

    def expect_inventory_failure(self, server: SyntheticPortalServer, root: Path, message: str) -> None:
        def body(portal):
            with fast_portal_recovery(), self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(20)
            return caught.exception

        error = self.portal_run(server, root, body)
        self.assertEqual(error.message, message)
        self.assertIn(error.message, cli.SUPPORT_REFS_BY_MESSAGE)

    def assert_no_business_detour(self, server: SyntheticPortalServer) -> None:
        self.assertEqual(server.ems_actuation_count, 0, "zero EMS dispatch")
        self.assertEqual(server.billing_manager_actuation_count, 0, "zero Billing Manager dispatch")
        self.assertEqual(server.pagination_clicks, 0, "sentinels are never clicked")

    def test_login_lands_on_the_single_surface_without_any_business_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer() as server:
            root = Path(directory)

            def body(portal):
                page = portal.page
                self.assertEqual(page.get_by_role("tab", name="EB Bill", exact=True).count(), 1)
                self.assertEqual(page.get_by_role("tab", name="Tenant Bill", exact=True).count(), 1)
                self.assertEqual(page.get_by_role("button", name="EMS", exact=True).count(), 1)

            self.portal_run(server, root, body)
            self.assert_no_business_detour(server)
            self.assertEqual(server.eb_bill_tab_clicks, 0, "proving a landing clicks nothing")
            self.assertEqual(server.search_count, 0)

    def test_the_production_path_clicks_the_tab_once_searches_once_and_downloads_every_row(self) -> None:
        bills = [
            SyntheticBill("2026-05-01_account_a.pdf", payload=synthetic_pdf(b"bill-a")),
            SyntheticBill("2026-06-01_account_b.pdf", payload=synthetic_pdf(b"bill-b")),
            SyntheticBill("2026-07-01_account_c.pdf", payload=synthetic_pdf(b"bill-c")),
        ]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills) as server:
            root = Path(directory)

            def body(portal):
                inventory = portal.inventory(20)
                self.assertEqual([row.ordinal for row in inventory], [0, 1, 2])
                self.assertEqual(server.eb_bill_tab_clicks, 1)
                self.assertEqual(server.search_count, 1)
                self.assertEqual(server.download_clicks, 0, "inventory downloads nothing")
                names = []
                for row in inventory:
                    target = root / f"row-{row.ordinal}.bin"
                    names.append(portal.download(row, target))
                    self.assertEqual(target.read_bytes(), bills[row.ordinal].payload)
                return names

            names = self.portal_run(server, root, body)
            self.assertEqual(names, [bill.filename for bill in bills])
            self.assertEqual(server.download_order, [0, 1, 2], "exactly one Download per row, in order")
            self.assertEqual(server.search_count, 1)
            self.assert_no_business_detour(server)

    # ---- DL-XB-199 G3-084: the real PlaywrightPortal pre-dispatch boundary ---- #
    #
    # Web Amendment 3: fake pages alone are not production-boundary evidence.
    # These drive the real browser through the real PlaywrightPortal against
    # the synthetic server and prove, by the server's own counters, that no
    # Download is ever dispatched by the diagnostic or by a failed proof.

    @staticmethod
    def spy_pre_dispatch(calls: list[int]):
        original = PlaywrightPortal._pre_dispatch

        def spy(self, row, trace):
            calls.append(row.ordinal)
            return original(self, row, trace)

        return mock.patch.object(PlaywrightPortal, "_pre_dispatch", spy)

    def test_real_download_preflight_diagnostic_passes_every_row_with_zero_download(self) -> None:
        bills = [SyntheticBill(f"2026-09-0{index}_preflight.pdf") for index in range(1, 4)]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills) as server:
            root = Path(directory)
            config_path = root / "config.json"
            write_config(config_path, server, root)
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            calls: list[int] = []
            old, _values = self.with_credentials()
            out, err = io.StringIO(), io.StringIO()
            try:
                with self.spy_pre_dispatch(calls), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exit_code = main([cli.DOWNLOAD_PREFLIGHT_DIAGNOSTIC_COMMAND, "--config", str(config_path)])
            finally:
                self.restore_credentials(old)
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            lines = out.getvalue().strip().splitlines()
            self.assertEqual(len(lines), 1, "exactly one document")
            document = json.loads(lines[0])
            self.assertEqual(exit_code, 0)
            self.assertEqual(err.getvalue(), "")
            self.assertEqual(document["schema"], cli.DOWNLOAD_PREFLIGHT_DIAGNOSTIC_SCHEMA)
            self.assertEqual(document["result"], cli.PREFLIGHT_ALL_ROWS_PASSED)
            self.assertEqual((document["inventory_count"], document["rows_passed"]), (3, 3))
            self.assertIs(document["download_dispatched"], False)
            self.assertIsNone(document["failure"])
            self.assertEqual(calls, [0, 1, 2], "the shared production proof, once per row")
            self.assertEqual(server.download_clicks, 0, "zero Download dispatch")
            self.assertEqual(server.download_order, [], "zero download served")
            self.assertEqual(server.search_count, 1)
            self.assertEqual(before, after, "no state, log, temp or archive artefact")
            self.assert_no_business_detour(server)

    def test_real_typed_pre_dispatch_failure_dispatches_no_download_on_either_path(self) -> None:
        inject_sentinel = """() => {
            const control = document.createElement('button');
            control.type = 'button';
            control.textContent = 'Load more';
            document.getElementById('results').appendChild(control);
        }"""
        for path in ("diagnostic", "production"):
            with self.subTest(path=path):
                bills = [SyntheticBill(f"2026-09-1{index}_typed.pdf") for index in range(1, 3)]
                with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills) as server:
                    root = Path(directory)
                    calls: list[int] = []

                    def body(portal):
                        rows = portal.inventory(20)
                        portal.page.evaluate(inject_sentinel)
                        if path == "diagnostic":
                            result = portal.download_preflight(rows)
                            self.assertEqual(result.rows_passed, 0)
                            self.assertFalse(result.download_dispatched)
                            return result.failure, portal._latched
                        with self.assertRaises(portal_module.DownloadPreflightError) as caught:
                            portal.download(rows[0], root / "x.bin")
                        self.assertEqual(caught.exception.message, portal_module.RESULTS_SURFACE_CHANGED_MESSAGE)
                        self.assertEqual(caught.exception.status, PORTAL_LAYOUT_CHANGED)
                        return caught.exception.evidence, portal._latched

                    with self.spy_pre_dispatch(calls):
                        evidence, latched = self.portal_run(server, root, body)
                    self.assertEqual(calls, [0])
                    self.assertEqual(evidence.reason_code, "RESULTS_PAGINATION_PRESENT")
                    self.assertEqual(evidence.last_checkpoint, "RESULTS_SURFACE")
                    self.assertIs(evidence.window_expired, False)
                    self.assertEqual(evidence.row_ordinal, 0)
                    self.assertTrue(latched)
                    self.assertEqual(server.download_clicks, 0, "zero Download dispatch")
                    self.assertEqual(server.download_order, [])

    def test_real_expired_window_failure_dispatches_no_download(self) -> None:
        hide_witness = """() => {
            for (const node of document.querySelectorAll('.account-witness')) node.style.display = 'none';
        }"""
        bills = [SyntheticBill("2026-09-21_window.pdf"), SyntheticBill("2026-09-22_window.pdf")]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills) as server:
            root = Path(directory)

            def body(portal):
                rows = portal.inventory(20)
                portal.page.evaluate(hide_witness)
                with fast_portal_recovery():
                    return portal.download_preflight(rows)

            result = self.portal_run(server, root, body)
            self.assertEqual(result.rows_passed, 0)
            self.assertEqual(result.failure.reason_code, "WITNESS_ABSENT")
            self.assertEqual(result.failure.last_checkpoint, "ACCOUNT_WITNESS")
            self.assertIs(result.failure.window_expired, True)
            self.assertGreaterEqual(result.failure.not_ready_looks, 1)
            self.assertEqual(server.download_clicks, 0)
            self.assertEqual(server.download_order, [])

    def test_real_production_download_still_dispatches_through_the_shared_proof(self) -> None:
        bills = [
            SyntheticBill("2026-09-31_shared_a.pdf", payload=synthetic_pdf(b"shared-a")),
            SyntheticBill("2026-09-32_shared_b.pdf", payload=synthetic_pdf(b"shared-b")),
        ]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills) as server:
            root = Path(directory)
            calls: list[int] = []

            def body(portal):
                inventory = portal.inventory(20)
                names = [portal.download(row, root / f"row-{row.ordinal}.bin") for row in inventory]
                for row in inventory:
                    self.assertEqual((root / f"row-{row.ordinal}.bin").read_bytes(), bills[row.ordinal].payload)
                return names, portal._latched

            with self.spy_pre_dispatch(calls):
                names, latched = self.portal_run(server, root, body)
            self.assertEqual(names, [bill.filename for bill in bills])
            self.assertEqual(calls, [0, 1], "every production Download passes the shared proof first")
            self.assertEqual(server.download_clicks, 2)
            self.assertEqual(server.download_order, [0, 1])
            self.assertFalse(latched)

    def test_an_already_selected_eb_bill_tab_is_never_clicked(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [SyntheticBill("2026-05-02_preselected.pdf")], variant="eb_bill_preselected"
        ) as server:
            inventory = self.portal_run(server, Path(directory), lambda portal: portal.inventory(20))
            self.assertEqual(len(inventory), 1)
            self.assertEqual(server.eb_bill_tab_clicks, 0)
            self.assertEqual(server.search_count, 1)

    def test_a_link_only_eb_bill_fails_closed_without_any_click(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [SyntheticBill("2026-05-03_link.pdf")], variant="eb_bill_link_only"
        ) as server:
            self.expect_inventory_failure(server, Path(directory), portal_module.EB_BILL_TAB_NOT_READY_MESSAGE)
            self.assertEqual(server.eb_bill_tab_clicks, 0, "no link fallback")
            self.assertEqual(server.search_count, 0)
            self.assert_no_business_detour(server)

    def test_an_eb_bill_click_that_selects_nothing_is_unproved_after_one_click(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [SyntheticBill("2026-05-04_inert.pdf")], variant="tab_inert"
        ) as server:
            self.expect_inventory_failure(server, Path(directory), portal_module.EB_BILL_TAB_UNPROVED_MESSAGE)
            self.assertEqual(server.eb_bill_tab_clicks, 1, "the click is never re-sent")
            self.assertEqual(server.search_count, 0)

    def test_account_witness_absent_duplicate_or_mismatched_fails_before_search(self) -> None:
        for kwargs, message in (
            ({"variant": "account_absent"}, portal_module.ACCOUNT_WITNESS_UNPROVED_MESSAGE),
            ({"variant": "account_duplicate"}, portal_module.ACCOUNT_WITNESS_AMBIGUOUS_MESSAGE),
            ({"account_text": "SYNTHETIC-OTHER-ACCOUNT"}, portal_module.ACCOUNT_WITNESS_UNPROVED_MESSAGE),
            # Occurrences inside the results rows never satisfy the witness.
            ({"variant": "account_absent,account_in_rows"}, portal_module.ACCOUNT_WITNESS_UNPROVED_MESSAGE),
        ):
            with self.subTest(**kwargs):
                with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
                    [SyntheticBill("2026-05-05_account.pdf")], **kwargs
                ) as server:
                    self.expect_inventory_failure(server, Path(directory), message)
                    self.assertEqual(server.search_count, 0)

    def test_account_text_inside_rows_does_not_disturb_the_one_outside_witness(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [SyntheticBill("2026-05-06_rows.pdf"), SyntheticBill("2026-05-07_rows.pdf")],
            variant="account_in_rows",
        ) as server:
            inventory = self.portal_run(server, Path(directory), lambda portal: portal.inventory(20))
            self.assertEqual(len(inventory), 2)

    def test_a_delayed_search_and_delayed_results_settle_with_one_search(self) -> None:
        for kwargs in ({"search_delay_ms": 400}, {"results_delay_ms": 400}):
            with self.subTest(**kwargs):
                with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
                    [SyntheticBill("2026-05-08_delayed.pdf")], **kwargs
                ) as server:
                    inventory = self.portal_run(server, Path(directory), lambda portal: portal.inventory(20))
                    self.assertEqual(len(inventory), 1)
                    self.assertEqual(server.search_count, 1)

    def test_a_second_inventory_fails_without_redispatching_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
            [SyntheticBill("2026-05-09_once.pdf")]
        ) as server:

            def body(portal):
                portal.inventory(20)
                with self.assertRaises(LayoutChangedError) as caught:
                    portal.inventory(20)
                return caught.exception

            error = self.portal_run(server, Path(directory), body)
            self.assertEqual(error.message, portal_module.RESULTS_INVENTORY_CONSUMED_MESSAGE)
            self.assertEqual(server.search_count, 1)
            self.assertEqual(server.eb_bill_tab_clicks, 1)

    def test_a_header_only_table_is_a_layout_change_not_no_new_bills(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer([]) as server:
            self.expect_inventory_failure(server, Path(directory), portal_module.RESULTS_HEADER_ONLY_MESSAGE)
            self.assertEqual(server.search_count, 1)

    def test_results_shape_drift_fails_before_any_download(self) -> None:
        for variant in ("two_tables", "header_missing", "ambiguous_download", "missing_download"):
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
                    [SyntheticBill("2026-05-10_shape.pdf"), SyntheticBill("2026-05-11_shape.pdf")],
                    variant=variant,
                ) as server:
                    self.expect_inventory_failure(
                        server, Path(directory), portal_module.RESULTS_UNSETTLED_MESSAGE
                    )
                    self.assertEqual(server.download_clicks, 0)

    def test_duplicate_row_identity_fails_before_any_download(self) -> None:
        bills = [
            SyntheticBill("2026-05-12_dup.pdf", row_text="Same private row"),
            SyntheticBill("2026-05-13_dup.pdf", row_text="Same private row"),
        ]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills) as server:
            self.expect_inventory_failure(server, Path(directory), portal_module.RESULTS_ROW_IDENTITY_MESSAGE)
            self.assertEqual(server.download_clicks, 0)

    def test_every_pagination_sentinel_fails_without_any_click(self) -> None:
        for role in ("button", "link"):
            for name in ("Next page", "Next", "Previous page", "Load more"):
                with self.subTest(role=role, name=name):
                    with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
                        [SyntheticBill("2026-05-14_page.pdf")], pagination_sentinel=(role, name)
                    ) as server:
                        self.expect_inventory_failure(
                            server, Path(directory), portal_module.RESULTS_PAGINATION_MESSAGE
                        )
                        self.assertEqual(server.pagination_clicks, 0)
                        self.assertEqual(server.download_clicks, 0)

    def test_aria_rowcount_must_agree_with_the_rendered_rows(self) -> None:
        bills = [SyntheticBill("2026-05-15_count.pdf"), SyntheticBill("2026-05-16_count.pdf")]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills, aria_rowcount="3") as server:
            inventory = self.portal_run(server, Path(directory), lambda portal: portal.inventory(20))
            self.assertEqual(len(inventory), 2, "header plus two rows agrees with aria-rowcount 3")
        for declared in ("7", "-1", "many"):
            with self.subTest(declared=declared):
                with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
                    bills, aria_rowcount=declared
                ) as server:
                    self.expect_inventory_failure(server, Path(directory), portal_module.RESULTS_ROWCOUNT_MESSAGE)

    def test_the_inventory_safety_ceiling_is_enforced(self) -> None:
        bills = [SyntheticBill(f"2026-05-2{index}_ceiling.pdf") for index in range(3)]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills) as server:

            def body(portal):
                with self.assertRaises(LayoutChangedError) as caught:
                    portal.inventory(2)
                return caught.exception

            error = self.portal_run(server, Path(directory), body)
            self.assertEqual(error.message, portal_module.RESULTS_CEILING_MESSAGE)
            self.assertEqual(server.download_clicks, 0)

    def test_row_drift_after_a_download_latches_every_later_row(self) -> None:
        for variant in ("reorder_after_first_download", "text_drift_after_first_download"):
            with self.subTest(variant=variant):
                bills = [SyntheticBill(f"2026-06-0{index}_drift.pdf") for index in range(1, 4)]
                with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(
                    bills, variant=variant
                ) as server:
                    root = Path(directory)

                    def body(portal):
                        inventory = portal.inventory(20)
                        self.assertEqual(portal.download(inventory[0], root / "first.bin"), bills[0].filename)
                        with self.assertRaises(LayoutChangedError) as drifted:
                            portal.download(inventory[1], root / "second.bin")
                        with self.assertRaises(LayoutChangedError) as latched:
                            portal.download(inventory[2], root / "third.bin")
                        return drifted.exception, latched.exception

                    drifted, latched = self.portal_run(server, root, body)
                    self.assertEqual(drifted.message, portal_module.RESULTS_SURFACE_CHANGED_MESSAGE)
                    self.assertEqual(latched.message, portal_module.RESULTS_LATCHED_MESSAGE)
                    self.assertEqual(server.download_clicks, 1, "zero later Download dispatch")

    def test_an_uncertain_download_dispatches_once_and_latches(self) -> None:
        cases = (
            ({"variant": "tab_lost_on_download"}, "success"),
            ({"variant": "popup_on_download"}, "success"),
            ({}, "inert"),
        )
        for kwargs, mode in cases:
            with self.subTest(mode=mode, **kwargs):
                bills = [
                    SyntheticBill("2026-06-11_uncertain.pdf", mode=mode),
                    SyntheticBill("2026-06-12_uncertain.pdf"),
                ]
                with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills, **kwargs) as server:
                    root = Path(directory)

                    def body(portal):
                        inventory = portal.inventory(20)
                        with self.assertRaises(AppError) as uncertain:
                            portal.download(inventory[0], root / "first.bin")
                        with self.assertRaises(LayoutChangedError) as latched:
                            portal.download(inventory[1], root / "second.bin")
                        return uncertain.exception, latched.exception

                    uncertain, latched = self.portal_run(server, root, body, timeout_seconds=2)
                    self.assertEqual(uncertain.status, "DOWNLOAD_FAILED")
                    self.assertFalse(uncertain.retryable)
                    self.assertEqual(uncertain.message, portal_module.DOWNLOAD_UNCERTAIN_MESSAGE)
                    self.assertEqual(latched.message, portal_module.RESULTS_LATCHED_MESSAGE)
                    self.assertEqual(server.download_clicks, 1)

    def test_rows_carry_no_filename_and_the_name_is_learned_only_at_download(self) -> None:
        bill = SyntheticBill("row-never-shows-this.pdf", suggested_filename="2026-06-21_authoritative.pdf")
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer([bill]) as server:
            root = Path(directory)

            def body(portal):
                inventory = portal.inventory(20)
                self.assertNotIn("filename", {name for name in vars(inventory[0])})
                self.assertNotIn("binding", repr(inventory[0]))
                page = portal.page
                self.assertEqual(page.locator("[role='table'] [data-testid]").count(), 0)
                self.assertEqual(page.locator("[role='table'] [data-filename]").count(), 0)
                self.assertEqual(page.locator("[role='table'] [href]").count(), 0)
                self.assertEqual(page.locator("select").count(), 0, "no tenant selector exists")
                return portal.download(inventory[0], root / "row.bin")

            self.assertEqual(self.portal_run(server, root, body), "2026-06-21_authoritative.pdf")

    def test_download_payload_validation(self) -> None:
        for mode in ("html", "zero", "truncated"):
            with self.subTest(mode=mode):
                bill = SyntheticBill("2026-05-07_account_ref.pdf", mode=mode)
                with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer([bill]) as server:
                    root = Path(directory)
                    target = root / "download.bin"

                    def body(portal):
                        return portal.download(portal.inventory(20)[0], target)

                    self.assertEqual(self.portal_run(server, root, body), bill.filename)
                    with self.assertRaises(InvalidPdfError):
                        validate_pdf(target)

    def cli_run(self, server: SyntheticPortalServer, root: Path, command: str = "run") -> tuple[int, dict]:
        config_path = root / "config.json"
        if not config_path.exists():
            (root / "archive").mkdir()
            write_config(config_path, server, root)
        old, _values = self.with_credentials()
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                result = main([command, "--config", str(config_path)])
        finally:
            self.restore_credentials(old)
        return result, json.loads(stdout.getvalue().strip().splitlines()[-1])

    def state_records(self, root: Path) -> dict:
        from energygrid_bill_downloader.state import StateStore

        with StateStore(root / "state" / "state.sqlite3") as state:
            return {record.filename_key: record for record in state.records()}

    def test_cli_run_downloads_every_row_and_a_rerun_publishes_nothing(self) -> None:
        bills = [
            SyntheticBill("2026-05-09_account_ref.pdf", payload=synthetic_pdf(b"run-a")),
            SyntheticBill("2026-05-10_account_ref.pdf", payload=synthetic_pdf(b"run-b")),
        ]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills) as server:
            root = Path(directory)
            first, first_summary = self.cli_run(server, root)
            self.assertEqual(first, 0)
            self.assertEqual(first_summary["status"], "DOWNLOADED")
            self.assertEqual(first_summary["downloaded_count"], 2)
            before = self.state_records(root)
            stats = {bill.filename: (root / "archive" / bill.filename).stat().st_mtime_ns for bill in bills}

            second, second_summary = self.cli_run(server, root)
            self.assertEqual(second, 0)
            self.assertEqual(second_summary["status"], "ALREADY_PRESENT")
            self.assertEqual(second_summary["present_count"], 2)
            self.assertEqual(second_summary["downloaded_count"], 0)
            self.assertEqual(server.download_counts, {bill.filename: 2 for bill in bills})
            after = self.state_records(root)
            self.assertEqual(set(after), set(before))
            for key, record in after.items():
                for name in ("status", "sha256", "byte_size", "archived_at_utc", "completion_source"):
                    self.assertEqual(getattr(record, name), getattr(before[key], name))
            for bill in bills:
                self.assertEqual((root / "archive" / bill.filename).stat().st_mtime_ns, stats[bill.filename])
            self.assertEqual(list((root / "temp").glob("run-*")), [])
            self.assert_no_business_detour(server)

    def test_cli_list_downloads_nothing_and_writes_no_state(self) -> None:
        bills = [SyntheticBill("2026-05-11_list.pdf"), SyntheticBill("2026-05-12_list.pdf")]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills) as server:
            root = Path(directory)
            result, summary = self.cli_run(server, root, command="list")
            self.assertEqual(result, 20)
            self.assertEqual(summary["status"], ACTION_REQUIRED)
            self.assertEqual(summary["inventory_count"], 2)
            self.assertEqual(summary["present_count"], 0)
            self.assertEqual(server.download_clicks, 0)
            self.assertEqual(server.search_count, 1)
            self.assertEqual(self.state_records(root), {})

    def test_cli_duplicate_normalized_filenames_publish_nothing(self) -> None:
        bills = [SyntheticBill("Invoice.pdf"), SyntheticBill("invoice.PDF")]
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer(bills) as server:
            root = Path(directory)
            result, summary = self.cli_run(server, root)
            self.assertEqual(result, 20)
            self.assertEqual(summary["status"], PORTAL_LAYOUT_CHANGED)
            self.assertEqual(list((root / "archive").iterdir()), [])
            self.assertEqual(self.state_records(root), {})
            self.assertEqual(list((root / "temp").glob("run-*")), [])

    def test_cli_header_only_surface_fails_closed_with_its_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory, SyntheticPortalServer([]) as server:
            root = Path(directory)
            with fast_portal_recovery():
                result, summary = self.cli_run(server, root)
            self.assertEqual(result, 20)
            self.assertEqual(summary["status"], PORTAL_LAYOUT_CHANGED)
            log_text = "".join(path.read_text(encoding="utf-8") for path in (root / "logs").glob("*.jsonl"))
            self.assertIn("EG_NAV_RESULTS_HEADER_ONLY", log_text)
            self.assertNotIn("SYNTHETIC-INTENDED-ACCOUNT", log_text)

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
        # contract with its own declared references, reachable only from the
        # production `inventory()`, and is proven complete by
        # `SingleSurfaceSourceIsolationTests` instead.
        live = (
            set(cli.SUPPORT_REFS_BY_MESSAGE.values())
            - cli.RETIRED_SUPPORT_REFS
            - cli.NAVIGATION_SUPPORT_REFS
            - cli.NAVIGATION_DIAGNOSTIC_SUPPORT_REFS
            - cli.DIAGNOSTIC_EMS_ENTRY_SUPPORT_REFS
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



# ---- DL-XB-199: the single-surface production path, deterministically ---- #
#
# These cases drive the committed `inventory()` and `download()` against a
# scripted surface: the exact EB Bill / Tenant Bill tabs, the account witness,
# the Search button, the one role table and its Download controls. The clock is
# simulated, so the shared 60-second recovery contract is exercised without any
# real waiting, and every real dispatch is counted.


class ResultsConfig:
    """Only the fields the results route reads; no URL is ever fetched."""

    portal_url = "http://127.0.0.1:1/synthetic"
    timeout_seconds = 5
    account_identity = "SYNTHETIC-INTENDED-ACCOUNT"


PRIVATE_ROW_TEXT = "PRIVATE-ROW 2026-09 SGD 123.45 ACCT-778899"


class SurfaceState:
    """The scripted single surface. Everything a test may vary lives here."""

    def __init__(self, rows=None, **overrides) -> None:
        self.eb_tab_role = "tab"
        self.eb_tab_count = 1
        self.eb_selected = False
        self.tenant_count = 1
        self.tenant_selected = True
        self.tab_click_selects = True
        self.tab_click_error: Exception | None = None
        self.tab_actionable = True
        # Witness flags: True means outside the results semantics.
        self.witnesses = [True]
        self.search_absent_looks = 0
        self.search_click_error: Exception | None = None
        self.results_absent_looks = 0
        self.tables = 1
        self.header_text = "Invoice Action"
        self.header_columnheaders = 2
        self.rows = list(rows if rows is not None else ["Synthetic invoice 1", "Synthetic invoice 2"])
        self.download_buttons: dict[int, int] = {}
        self.stray_download_buttons = 0
        self.pagination: set[tuple[str, str]] = set()
        self.aria_rowcount: str | None = None
        self.snapshot_error: Exception | None = None
        self.extra_pages = 0
        # G3-084 pre-dispatch reachability switches. None/defaults leave the
        # surface exactly as before.
        self.witness_flag_override: list | None = None
        self.rows_absent = False
        self.header_download_buttons = 0
        self.row_columnheaders: dict[int, int] = {}
        self.rowcount_timeout = False
        # Applied only to the freshly resolved Download control and the final
        # one-shot recheck, never to the results read; `control_ordinals`
        # narrows them to some rows (None means every row).
        self.control_ordinals: set[int] | None = None
        self.control_count: int | None = None
        self.control_visible = True
        self.control_enabled = True
        self.control_enabled_error: Exception | None = None
        self.control_actionable = True
        self.control_trial_error: Exception | None = None
        self.recheck: str | None = None
        for key, value in overrides.items():
            if not hasattr(self, key):
                raise AttributeError(key)
            setattr(self, key, value)


class SurfaceLocator:
    """One fresh resolution of a scripted element set."""

    def __init__(
        self, page: "FakeSurfacePage", kind: str, index: int | None = None, purpose: str = "read"
    ) -> None:
        self.page = page
        self.kind = kind
        self.index = index
        # "read": reached through the results read; "control": reached through
        # the separate fresh resolution of the Download control / recheck.
        self.purpose = purpose

    def _controlled(self) -> bool:
        state = self.page.state
        return self.purpose == "control" and (
            state.control_ordinals is None or (self.index or 0) - 1 in state.control_ordinals
        )

    # -- resolution -- #

    def count(self) -> int:
        page, state = self.page, self.page.state
        if self.kind == "tab:eb":
            return state.eb_tab_count if state.eb_tab_role == "tab" else 0
        if self.kind == "tab:tenant":
            return state.tenant_count
        if self.kind == "search":
            return 0 if page.search_looks < state.search_absent_looks else 1
        if self.kind == "table":
            return 0 if page.results_looks < state.results_absent_looks else state.tables
        if self.kind == "rows":
            return 0 if state.rows_absent else 1 + len(state.rows)
        if self.kind == "columnheader":
            if self.index == 0:
                return state.header_columnheaders
            return state.row_columnheaders.get(self.index - 1, 0)
        if self.kind == "row-download":
            if self.index == 0:
                return state.header_download_buttons
            if self._controlled() and state.control_count is not None:
                return state.control_count
            return state.download_buttons.get(self.index - 1, 1)
        if self.kind == "all-downloads":
            return sum(state.download_buttons.get(i, 1) for i in range(len(state.rows))) + state.stray_download_buttons
        if self.kind == "witness":
            return len(state.witnesses)
        if self.kind.startswith("sentinel:"):
            return 1 if tuple(self.kind.split(":", 2)[1:]) in state.pagination else 0
        return 0

    def nth(self, index: int) -> "SurfaceLocator":
        kind = {"rows": "row", "witness": "witness-item"}.get(self.kind, self.kind)
        return SurfaceLocator(self.page, kind, index, self.purpose)

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False) -> "SurfaceLocator":
        self.page.role_lookups.append((role, name, exact))
        if self.kind == "table" and role == "row":
            return SurfaceLocator(self.page, "rows", purpose=self.purpose)
        if self.kind == "row" and role == "columnheader":
            return SurfaceLocator(self.page, "columnheader", self.index, self.purpose)
        if self.kind == "row" and role == "button" and name == "Download" and exact:
            return SurfaceLocator(self.page, "row-download", self.index, self.purpose)
        return SurfaceLocator(self.page, "nothing")

    # -- inspection -- #

    def is_visible(self, timeout: int | None = None) -> bool:
        if self.kind == "row-download" and self._controlled():
            return self.page.state.control_visible
        return True

    def is_enabled(self, timeout: int | None = None) -> bool:
        self.page.probe_timeouts.append(timeout)
        if self.kind == "row-download" and self._controlled():
            if self.page.state.control_enabled_error is not None:
                raise self.page.state.control_enabled_error
            return self.page.state.control_enabled
        return True

    def get_attribute(self, name: str, timeout: int | None = None):
        self.page.probe_timeouts.append(timeout)
        state = self.page.state
        if self.kind == "tab:eb" and name == "aria-selected":
            return "true" if state.eb_selected else "false"
        if self.kind == "tab:tenant" and name == "aria-selected":
            return "true" if state.tenant_selected else "false"
        if self.kind == "table" and name == "aria-rowcount":
            if state.rowcount_timeout:
                raise synthetic_timeout()
            return state.aria_rowcount
        raise AssertionError(f"unexpected attribute read {self.kind}:{name}")

    def aria_snapshot(self, timeout: int | None = None) -> str:
        self.page.probe_timeouts.append(timeout)
        state = self.page.state
        if self._controlled() and state.recheck is not None:
            if state.recheck == "timeout":
                raise synthetic_timeout()
            if state.recheck == "invalid":
                raise RuntimeError("unreadable ACCT-778899")
            return '- row "a different private row":\n  - button "Download"'
        if state.snapshot_error is not None:
            raise state.snapshot_error
        if self.index == 0:
            return f'- row "{state.header_text}"'
        return f'- row "{state.rows[self.index - 1]}":\n  - button "Download"'

    def evaluate_all(self, expression: str, arg=None):
        self.page.evaluations.append(expression)
        if self.page.state.witness_flag_override is not None:
            return list(self.page.state.witness_flag_override)
        return list(self.page.state.witnesses)

    # -- actions -- #

    def click(self, trial: bool = False, timeout: int | None = None) -> None:
        page, state = self.page, self.page.state
        if trial:
            page.trial_clicks.append(self.kind)
            if self.kind == "tab:eb" and not state.tab_actionable:
                raise synthetic_timeout()
            if self.kind == "row-download" and self._controlled():
                if state.control_trial_error is not None:
                    raise state.control_trial_error
                if not state.control_actionable:
                    raise synthetic_timeout()
            return
        page.clicks.append(self.kind)
        if self.kind == "tab:eb":
            if state.tab_click_error is not None:
                raise state.tab_click_error
            if state.tab_click_selects:
                state.eb_selected = True
                state.tenant_selected = False
            return
        if self.kind == "search":
            if state.search_click_error is not None:
                raise state.search_click_error
            page.searched = True
            return
        if self.kind == "row-download":
            page.download_dispatches.append(self.index - 1)
            page.fire_download(self.index - 1)
            return
        raise AssertionError(f"unexpected click on {self.kind}")


class FakeDownload:
    def __init__(self, page: "FakeSurfacePage", outcome: dict) -> None:
        self.page = page
        self.outcome = outcome
        self.suggested_filename = outcome.get("name")

    def failure(self):
        return "net::ERR_SYNTHETIC" if self.outcome.get("kind") == "failure" else None

    def save_as(self, destination) -> None:
        if self.outcome.get("kind") == "save_error":
            raise RuntimeError("synthetic save failure at https://portal.example.invalid/private")
        Path(destination).write_bytes(self.outcome.get("payload", synthetic_pdf()))
        after = self.outcome.get("after")
        if after is not None:
            after(self.page.state)


class FakeDownloadInfo:
    def __init__(self, page: "FakeSurfacePage") -> None:
        self.page = page

    @property
    def value(self) -> FakeDownload:
        if self.page.pending_download is None:
            raise synthetic_timeout()
        return self.page.pending_download


class FakeContext:
    def __init__(self, page: "FakeSurfacePage") -> None:
        self.page = page

    @property
    def pages(self):
        return [self.page] + [object() for _ in range(self.page.state.extra_pages)]


class FakeSurfacePage:
    """A page whose surfaces settle after scripted looks; dispatches are counted."""

    def __init__(self, state: SurfaceState, clock: RecoveryClock, downloads=None) -> None:
        self.state = state
        self.clock = clock
        # Per-row per-attempt download outcomes; the last entry repeats.
        self.downloads: dict[int, list[dict]] = downloads or {}
        self.role_lookups: list[tuple[str, str | None, bool]] = []
        self.text_lookups: list[tuple[str, bool]] = []
        self.clicks: list[str] = []
        self.trial_clicks: list[str] = []
        self.download_dispatches: list[int] = []
        self.probe_timeouts: list[int | None] = []
        self.evaluations: list[str] = []
        self.expect_download_timeouts: list[int | None] = []
        self.pending_download: FakeDownload | None = None
        self.search_looks = 0
        self.results_looks = 0
        self.searched = False
        # A results read always inspects the pagination sentinels first, so a
        # table lookup right after them is a read; any other is the separate
        # Download-control / recheck resolution.
        self.after_sentinels = False

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.clock.charge_yield(milliseconds)

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False) -> SurfaceLocator:
        self.role_lookups.append((role, name, exact))
        if role == "tab" and name == "EB Bill" and exact:
            return SurfaceLocator(self, "tab:eb")
        if role == "tab" and name == "Tenant Bill" and exact:
            return SurfaceLocator(self, "tab:tenant")
        if role == "button" and name == "Search" and exact:
            locator = SurfaceLocator(self, "search")
            self.search_looks += 1
            return locator
        if role == "table" and name is None:
            purpose = "read" if self.after_sentinels else "control"
            self.after_sentinels = False
            if self.searched:
                self.results_looks += 1
                return SurfaceLocator(self, "table", purpose=purpose)
            return SurfaceLocator(self, "nothing")
        if role == "button" and name == "Download" and exact:
            return SurfaceLocator(self, "all-downloads")
        if role in ("button", "link") and exact and name in portal_module.RESULTS_PAGINATION_SENTINEL_NAMES:
            self.after_sentinels = True
            return SurfaceLocator(self, f"sentinel:{role}:{name}")
        return SurfaceLocator(self, "nothing")

    def get_by_text(self, text: str, exact: bool = False) -> SurfaceLocator:
        self.text_lookups.append((text, exact))
        if text == ResultsConfig.account_identity and exact:
            return SurfaceLocator(self, "witness")
        return SurfaceLocator(self, "nothing")

    def fire_download(self, row: int) -> None:
        script = self.downloads.get(row, [{"kind": "ok"}])
        attempt = self.download_dispatches.count(row) - 1
        outcome = dict(script[min(attempt, len(script) - 1)])
        kind = outcome.get("kind", "ok")
        outcome.setdefault("name", f"2026-09-0{row + 1}_synthetic.pdf")
        if kind == "click_error":
            raise RuntimeError("synthetic click failure password=hunter2")
        if kind == "no_event":
            return
        if kind == "popup":
            self.state.extra_pages += 1
        if kind == "tab_lost":
            self.state.eb_selected = False
            self.state.tenant_selected = True
        self.pending_download = FakeDownload(self, outcome)

    @contextlib.contextmanager
    def expect_download(self, timeout: int | None = None):
        self.expect_download_timeouts.append(timeout)
        self.pending_download = None
        yield FakeDownloadInfo(self)
        if self.pending_download is None:
            raise synthetic_timeout()


def surface_portal(state: SurfaceState | None = None, downloads=None):
    clock = RecoveryClock()
    page = FakeSurfacePage(state or SurfaceState(), clock, downloads)
    portal = PlaywrightPortal(ResultsConfig(), headed=False)
    portal.page = page
    portal.context = FakeContext(page)
    return portal, page, clock


class SingleSurfaceNavigationTests(unittest.TestCase):
    """The exact EB Bill tab, the account witness and one Search."""

    def inventory(self, state: SurfaceState | None = None, **kwargs):
        portal, page, clock = surface_portal(state, **kwargs)
        error = None
        rows = None
        with simulated_clock(clock):
            try:
                rows = portal.inventory(20)
            except AppError as exc:
                error = exc
        return portal, page, clock, rows, error

    def test_an_unselected_tab_is_clicked_once_and_its_selection_proven(self) -> None:
        _portal, page, _clock, rows, error = self.inventory()
        self.assertIsNone(error)
        self.assertEqual(len(rows), 2)
        self.assertEqual(page.clicks, ["tab:eb", "search"])
        self.assertTrue(page.state.eb_selected)
        self.assertFalse(page.state.tenant_selected)

    def test_an_already_selected_tab_costs_zero_clicks(self) -> None:
        state = SurfaceState(eb_selected=True, tenant_selected=False)
        _portal, page, _clock, rows, error = self.inventory(state)
        self.assertIsNone(error)
        self.assertEqual(page.clicks, ["search"])
        self.assertNotIn("tab:eb", page.trial_clicks)

    def test_a_link_only_eb_bill_is_never_used(self) -> None:
        state = SurfaceState(eb_tab_role="link")
        _portal, page, clock, _rows, error = self.inventory(state)
        self.assertEqual(error.message, portal_module.EB_BILL_TAB_NOT_READY_MESSAGE)
        self.assertEqual(page.clicks, [])
        self.assertNotIn("link", {role for role, _name, _exact in page.role_lookups})
        self.assertLessEqual(clock.elapsed_ms(), RECOVERY_CEILING_MS)

    def test_ambiguous_or_unactionable_tabs_fail_closed_before_dispatch(self) -> None:
        for overrides in ({"eb_tab_count": 2}, {"tab_actionable": False}, {"tenant_count": 2}):
            with self.subTest(**overrides):
                _portal, page, _clock, _rows, error = self.inventory(SurfaceState(**overrides))
                self.assertEqual(error.message, portal_module.EB_BILL_TAB_NOT_READY_MESSAGE)
                self.assertEqual(page.clicks, [])

    def test_a_tab_click_exception_is_uncertain_and_never_resent(self) -> None:
        state = SurfaceState(tab_click_error=RuntimeError("synthetic password=hunter2"))
        _portal, page, _clock, _rows, error = self.inventory(state)
        self.assertEqual(error.message, portal_module.EB_BILL_TAB_UNCERTAIN_MESSAGE)
        self.assertEqual(page.clicks, ["tab:eb"])
        self.assertNotIn("hunter2", error.message)

    def test_a_tab_click_that_selects_nothing_fails_as_unproved(self) -> None:
        _portal, page, _clock, _rows, error = self.inventory(SurfaceState(tab_click_selects=False))
        self.assertEqual(error.message, portal_module.EB_BILL_TAB_UNPROVED_MESSAGE)
        self.assertEqual(page.clicks, ["tab:eb"])

    def test_both_tabs_selected_is_a_contradiction_never_a_selection(self) -> None:
        state = SurfaceState(eb_selected=True, tenant_selected=True)
        _portal, page, _clock, _rows, error = self.inventory(state)
        self.assertEqual(error.message, portal_module.EB_BILL_TAB_NOT_READY_MESSAGE)
        self.assertEqual(page.clicks, [])

    def test_the_account_witness_must_be_exactly_one_outside_the_results(self) -> None:
        cases = (
            ([], portal_module.ACCOUNT_WITNESS_UNPROVED_MESSAGE),
            ([True, True], portal_module.ACCOUNT_WITNESS_AMBIGUOUS_MESSAGE),
            ([False, False], portal_module.ACCOUNT_WITNESS_UNPROVED_MESSAGE),
        )
        for witnesses, message in cases:
            with self.subTest(witnesses=witnesses):
                _portal, page, _clock, _rows, error = self.inventory(SurfaceState(witnesses=witnesses))
                self.assertEqual(error.message, message)
                self.assertNotIn("search", page.clicks)
        _portal, page, _clock, rows, error = self.inventory(SurfaceState(witnesses=[False, True, False]))
        self.assertIsNone(error, "row occurrences are ignored, one outside witness passes")
        self.assertEqual(page.text_lookups[0], (ResultsConfig.account_identity, True))

    def test_a_delayed_search_is_recovered_and_dispatched_once(self) -> None:
        _portal, page, _clock, rows, error = self.inventory(SurfaceState(search_absent_looks=3))
        self.assertIsNone(error)
        self.assertEqual(page.clicks.count("search"), 1)

    def test_a_search_that_never_appears_fails_before_dispatch(self) -> None:
        _portal, page, clock, _rows, error = self.inventory(SurfaceState(search_absent_looks=10_000))
        self.assertEqual(error.message, portal_module.SEARCH_NOT_READY_MESSAGE)
        self.assertNotIn("search", page.clicks)
        self.assertLessEqual(clock.elapsed_ms(), 2 * RECOVERY_CEILING_MS)

    def test_a_search_click_exception_is_uncertain_and_never_resent(self) -> None:
        state = SurfaceState(search_click_error=RuntimeError("synthetic"))
        _portal, page, _clock, _rows, error = self.inventory(state)
        self.assertEqual(error.message, portal_module.SEARCH_UNCERTAIN_MESSAGE)
        self.assertEqual(page.clicks.count("search"), 1)

    def test_delayed_results_settle_without_a_second_search(self) -> None:
        _portal, page, _clock, rows, error = self.inventory(SurfaceState(results_absent_looks=4))
        self.assertIsNone(error)
        self.assertEqual(page.clicks.count("search"), 1)

    def test_a_second_inventory_fails_without_any_dispatch(self) -> None:
        portal, page, clock = surface_portal()
        with simulated_clock(clock):
            portal.inventory(20)
            clicks = list(page.clicks)
            lookups = len(page.role_lookups)
            with self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(20)
        self.assertEqual(caught.exception.message, portal_module.RESULTS_INVENTORY_CONSUMED_MESSAGE)
        self.assertEqual(page.clicks, clicks)
        self.assertEqual(len(page.role_lookups), lookups, "nothing is even inspected")

    def test_a_failed_inventory_is_also_consumed(self) -> None:
        portal, page, clock = surface_portal(SurfaceState(witnesses=[]))
        with simulated_clock(clock):
            with self.assertRaises(LayoutChangedError):
                portal.inventory(20)
            with self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(20)
        self.assertEqual(caught.exception.message, portal_module.RESULTS_INVENTORY_CONSUMED_MESSAGE)

    def test_an_unreadable_final_re_proof_fails_closed_without_private_text(self) -> None:
        cases = (
            ("_eb_bill_tab_state", portal_module.EB_BILL_TAB_UNPROVED_MESSAGE),
            ("_account_witness_count", portal_module.ACCOUNT_WITNESS_UNPROVED_MESSAGE),
        )
        for target, message in cases:
            with self.subTest(target=target):
                portal, page, clock = surface_portal()
                original = getattr(PlaywrightPortal, target)

                def flaky(self, *args, _original=original, _page=page):
                    # Only the inspection-only re-proof after the results settled.
                    if _page.searched and _page.results_looks >= 2:
                        raise RuntimeError("unreadable ACCT-778899")
                    return _original(self, *args)

                with mock.patch.object(PlaywrightPortal, target, flaky), simulated_clock(clock):
                    with self.assertRaises(LayoutChangedError) as caught:
                        portal.inventory(20)
                self.assertEqual(caught.exception.message, message)
                self.assertNotIn("ACCT", caught.exception.message)
                self.assertEqual(page.clicks.count("search"), 1)

    def test_a_second_page_fails_the_topology_before_anything_is_clicked(self) -> None:
        _portal, page, _clock, _rows, error = self.inventory(SurfaceState(extra_pages=1))
        self.assertEqual(error.message, portal_module.RESULTS_TOPOLOGY_MESSAGE)
        self.assertEqual(page.clicks, [])

    def test_no_ems_billing_manager_link_or_selector_is_ever_resolved(self) -> None:
        _portal, page, _clock, _rows, error = self.inventory()
        self.assertIsNone(error)
        names = {name for _role, name, _exact in page.role_lookups}
        self.assertTrue(names.isdisjoint({"EMS", "Billing Manager"}))
        self.assertNotIn("link", {role for role, name, _exact in page.role_lookups if name not in portal_module.RESULTS_PAGINATION_SENTINEL_NAMES})
        self.assertTrue(all(exact for _role, name, exact in page.role_lookups if name is not None))


class SingleSurfaceResultsTests(unittest.TestCase):
    """The one role table: shape, private identity, sentinels and ceiling."""

    def inventory(self, state: SurfaceState, ceiling: int = 20):
        portal, page, clock = surface_portal(state)
        with simulated_clock(clock):
            try:
                return portal, page, portal.inventory(ceiling), None
            except AppError as exc:
                return portal, page, None, exc

    def test_the_header_is_excluded_and_handles_are_opaque(self) -> None:
        portal, _page, rows, error = self.inventory(SurfaceState(rows=[PRIVATE_ROW_TEXT, "Synthetic invoice 2"]))
        self.assertIsNone(error)
        self.assertEqual([row.ordinal for row in rows], [0, 1])
        self.assertEqual({field.name for field in dataclasses.fields(rows[0])}, {"ordinal", "binding"})
        for row in rows:
            self.assertEqual(repr(row), f"InvoiceRow(ordinal={row.ordinal})")
            self.assertIs(row.binding, portal._inventory_binding)
        self.assertEqual(len(portal._frozen_rows), 3, "header plus two invoice rows")

    def test_structural_drift_fails_before_any_download(self) -> None:
        cases = (
            {"tables": 2},
            {"header_columnheaders": 0},
            {"download_buttons": {0: 2}},
            {"download_buttons": {1: 0}},
            {"stray_download_buttons": 1},
        )
        for overrides in cases:
            with self.subTest(**{key: str(value) for key, value in overrides.items()}):
                _portal, page, _rows, error = self.inventory(SurfaceState(**overrides))
                self.assertEqual(error.message, portal_module.RESULTS_UNSETTLED_MESSAGE)
                self.assertEqual(page.download_dispatches, [])

    def test_a_header_only_table_fails_closed(self) -> None:
        _portal, page, _rows, error = self.inventory(SurfaceState(rows=[]))
        self.assertEqual(error.message, portal_module.RESULTS_HEADER_ONLY_MESSAGE)

    def test_duplicate_empty_oversized_or_unreadable_row_identity_fails(self) -> None:
        cases = (
            ({"rows": ["Same", "Same"]}, portal_module.RESULTS_ROW_IDENTITY_MESSAGE),
            ({"rows": ["Same  row\ntext", "Same row text"]}, portal_module.RESULTS_ROW_IDENTITY_MESSAGE),
            ({"header_text": "Synthetic invoice 1", "rows": ["x"]}, None),
            ({"rows": ["x" * 5000]}, portal_module.RESULTS_ROW_IDENTITY_MESSAGE),
            ({"snapshot_error": RuntimeError("unreadable ACCT-778899")}, portal_module.RESULTS_ROW_IDENTITY_MESSAGE),
            ({"snapshot_error": synthetic_timeout()}, portal_module.RESULTS_ROW_IDENTITY_MESSAGE),
        )
        for overrides, message in cases:
            with self.subTest(case=repr(overrides)[:40]):
                _portal, page, rows, error = self.inventory(SurfaceState(**overrides))
                if message is None:
                    self.assertIsNone(error)
                    continue
                self.assertEqual(error.message, message)
                self.assertNotIn("ACCT", error.message)
                self.assertEqual(page.download_dispatches, [])

    def test_an_empty_row_identity_is_never_usable(self) -> None:
        portal, _page, _clock = surface_portal()

        class BlankRow:
            def aria_snapshot(self, timeout=None):
                return "   \n\t "

        self.assertIsNone(portal._row_identity(BlankRow(), 1000))
        portal, _page, clock = surface_portal()
        with mock.patch.object(SurfaceLocator, "aria_snapshot", lambda self, timeout=None: " \n "):
            with simulated_clock(clock), self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(20)
        self.assertEqual(caught.exception.message, portal_module.RESULTS_ROW_IDENTITY_MESSAGE)

    def test_normalisation_is_nfc_whitespace_and_case_preserving(self) -> None:
        normalise = portal_module._normalise_surface_text
        self.assertEqual(normalise("  Café\n\tBill  "), "Café Bill")
        self.assertNotEqual(normalise("Bill"), normalise("bill"))

    def test_every_pagination_sentinel_fails_closed_and_is_never_clicked(self) -> None:
        for role in ("button", "link"):
            for name in ("Next page", "Next", "Previous page", "Load more"):
                with self.subTest(role=role, name=name):
                    _portal, page, _rows, error = self.inventory(SurfaceState(pagination={(role, name)}))
                    self.assertEqual(error.message, portal_module.RESULTS_PAGINATION_MESSAGE)
                    self.assertFalse(any(kind.startswith("sentinel") for kind in page.clicks + page.trial_clicks))

    def test_the_no_pagination_current_surface_passes(self) -> None:
        _portal, _page, rows, error = self.inventory(SurfaceState())
        self.assertIsNone(error)
        self.assertEqual(len(rows), 2)

    def test_aria_rowcount_is_checked_when_present(self) -> None:
        _portal, _page, rows, error = self.inventory(SurfaceState(aria_rowcount="3"))
        self.assertIsNone(error)
        for declared in ("4", "2", "-1", "lots"):
            with self.subTest(declared=declared):
                _portal, _page, _rows, error = self.inventory(SurfaceState(aria_rowcount=declared))
                self.assertEqual(error.message, portal_module.RESULTS_ROWCOUNT_MESSAGE)

    def test_the_safety_ceiling_fails_before_any_download(self) -> None:
        _portal, page, _rows, error = self.inventory(SurfaceState(rows=["a", "b", "c"]), ceiling=2)
        self.assertEqual(error.message, portal_module.RESULTS_CEILING_MESSAGE)

    def test_the_identity_is_a_keyed_session_digest_never_raw_text(self) -> None:
        first, _page, _rows, _error = self.inventory(SurfaceState(rows=[PRIVATE_ROW_TEXT]))
        second, _page, _rows, _error = self.inventory(SurfaceState(rows=[PRIVATE_ROW_TEXT]))
        self.assertNotEqual(first._frozen_rows, second._frozen_rows, "a fresh key per portal instance")
        for digest in first._frozen_rows:
            self.assertEqual(len(digest), 32)
            self.assertNotIn(PRIVATE_ROW_TEXT.encode("utf-8"), digest)
        self.assertNotIn(PRIVATE_ROW_TEXT, repr(vars(first)))

    def test_close_drops_the_session_binding(self) -> None:
        portal, _page, rows, _error = self.inventory(SurfaceState())
        key = portal._row_identity_key
        portal.page = None
        portal.close()
        self.assertIsNone(portal._frozen_rows)
        self.assertIsNone(portal._inventory_binding)
        self.assertNotEqual(portal._row_identity_key, key)
        with self.assertRaises(AppError):
            portal.download(rows[0], Path("unused.bin"))


class SingleSurfaceDownloadTests(unittest.TestCase):
    """Retry versus uncertainty, drift latches and filename authority."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def prepared(self, state: SurfaceState | None = None, downloads=None):
        portal, page, clock = surface_portal(state or SurfaceState(rows=["r1", "r2", "r3"]), downloads)
        with simulated_clock(clock):
            rows = portal.inventory(20)
        return portal, page, clock, rows

    def download(self, portal, clock, row, name="download.bin"):
        with simulated_clock(clock):
            return portal.download(row, self.root / name)

    def test_the_suggested_filename_is_returned_only_after_a_saved_download(self) -> None:
        portal, page, clock, rows = self.prepared(
            downloads={0: [{"kind": "ok", "name": "2026-09-30_authoritative.pdf", "payload": synthetic_pdf(b"x")}]}
        )
        self.assertEqual(self.download(portal, clock, rows[0]), "2026-09-30_authoritative.pdf")
        self.assertEqual((self.root / "download.bin").read_bytes(), synthetic_pdf(b"x"))
        self.assertEqual(page.download_dispatches, [0])
        self.assertEqual(page.expect_download_timeouts, [ResultsConfig.timeout_seconds * 1000])

    def test_every_row_downloads_once_in_order(self) -> None:
        portal, page, clock, rows = self.prepared()
        names = [self.download(portal, clock, row, f"row{row.ordinal}.bin") for row in rows]
        self.assertEqual(page.download_dispatches, [0, 1, 2])
        self.assertEqual(len(set(names)), 3)

    def test_a_positively_observed_failure_is_retryable_and_re_proves_the_surface(self) -> None:
        for kind in ("failure", "save_error"):
            with self.subTest(kind=kind):
                portal, page, clock, rows = self.prepared(downloads={0: [{"kind": kind}, {"kind": "ok"}]})
                with self.assertRaises(DownloadError) as caught:
                    self.download(portal, clock, rows[0])
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn("portal.example.invalid", caught.exception.message)
                lookups_before = len(page.role_lookups)
                self.assertTrue(self.download(portal, clock, rows[0]))
                self.assertEqual(page.download_dispatches, [0, 0])
                retry_lookups = page.role_lookups[lookups_before:]
                self.assertIn(("tab", "EB Bill", True), retry_lookups, "the retry re-proves the tab")
                self.assertIn(("table", None, False), retry_lookups, "the retry re-reads the table")

    def test_uncertain_dispatches_latch_and_block_every_later_row(self) -> None:
        for kind in ("click_error", "no_event", "popup", "tab_lost"):
            with self.subTest(kind=kind):
                portal, page, clock, rows = self.prepared(downloads={0: [{"kind": kind}]})
                with self.assertRaises(AppError) as caught:
                    self.download(portal, clock, rows[0])
                self.assertEqual(caught.exception.status, "DOWNLOAD_FAILED")
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.message, portal_module.DOWNLOAD_UNCERTAIN_MESSAGE)
                self.assertNotIn("hunter2", caught.exception.message)
                for row in (rows[0], rows[1]):
                    with self.assertRaises(LayoutChangedError) as latched:
                        self.download(portal, clock, row)
                    self.assertEqual(latched.exception.message, portal_module.RESULTS_LATCHED_MESSAGE)
                self.assertEqual(page.download_dispatches, [0], "exactly one uncertain dispatch")

    def test_a_missing_suggested_filename_latches(self) -> None:
        portal, page, clock, rows = self.prepared(downloads={0: [{"kind": "ok", "name": ""}]})
        with self.assertRaises(DownloadError) as caught:
            self.download(portal, clock, rows[0])
        self.assertFalse(caught.exception.retryable)
        with self.assertRaises(LayoutChangedError):
            self.download(portal, clock, rows[1])
        self.assertEqual(page.download_dispatches, [0])

    def test_a_different_suggested_filename_on_retry_latches(self) -> None:
        portal, page, clock, rows = self.prepared(
            downloads={0: [{"kind": "save_error", "name": "2026-01-01_a.pdf"}, {"kind": "ok", "name": "2026-01-01_b.pdf"}]}
        )
        with self.assertRaises(DownloadError):
            self.download(portal, clock, rows[0])
        with self.assertRaises(LayoutChangedError) as caught:
            self.download(portal, clock, rows[0])
        self.assertEqual(caught.exception.message, portal_module.RESULTS_FILENAME_CHANGED_MESSAGE)
        with self.assertRaises(LayoutChangedError):
            self.download(portal, clock, rows[1])
        self.assertEqual(page.download_dispatches, [0, 0])

    def test_row_reorder_or_text_drift_latches_before_the_next_dispatch(self) -> None:
        mutations = {
            "reorder": lambda state: state.rows.reverse(),
            "text": lambda state: state.rows.__setitem__(1, "r2 (viewed)"),
            "header": lambda state: setattr(state, "header_text", "Invoice Action Status"),
            "row_added": lambda state: state.rows.append("r4"),
        }
        for label, mutate in mutations.items():
            with self.subTest(drift=label):
                portal, page, clock, rows = self.prepared(downloads={0: [{"kind": "ok", "after": mutate}]})
                self.download(portal, clock, rows[0])
                with self.assertRaises(LayoutChangedError) as caught:
                    self.download(portal, clock, rows[1], "second.bin")
                self.assertEqual(caught.exception.message, portal_module.RESULTS_SURFACE_CHANGED_MESSAGE)
                with self.assertRaises(LayoutChangedError) as latched:
                    self.download(portal, clock, rows[2], "third.bin")
                self.assertEqual(latched.exception.message, portal_module.RESULTS_LATCHED_MESSAGE)
                self.assertEqual(page.download_dispatches, [0])

    def test_a_lost_tab_or_account_witness_before_dispatch_latches(self) -> None:
        mutations = {
            "tab": lambda state: (setattr(state, "eb_selected", False), setattr(state, "tenant_selected", True)),
            "witness": lambda state: setattr(state, "witnesses", [True, True]),
            "pagination": lambda state: setattr(state, "pagination", {("button", "Load more")}),
            "topology": lambda state: setattr(state, "extra_pages", 1),
        }
        for label, mutate in mutations.items():
            with self.subTest(drift=label):
                portal, page, clock, rows = self.prepared()
                mutate(page.state)
                with self.assertRaises(LayoutChangedError) as caught:
                    self.download(portal, clock, rows[0])
                self.assertEqual(caught.exception.message, portal_module.RESULTS_SURFACE_CHANGED_MESSAGE)
                self.assertEqual(page.download_dispatches, [])
                self.assertTrue(portal._latched)

    def test_foreign_expired_or_forged_handles_fail_before_any_dispatch(self) -> None:
        portal, page, clock, rows = self.prepared()
        other, _other_page, other_clock, other_rows = self.prepared()
        forged = (
            other_rows[0],
            portal_module.InvoiceRow(ordinal=0, binding=object()),
            portal_module.InvoiceRow(ordinal=3, binding=rows[0].binding),
            portal_module.InvoiceRow(ordinal=-1, binding=rows[0].binding),
            portal_module.InvoiceRow(ordinal=True, binding=rows[0].binding),
            "row-0",
        )
        for handle in forged:
            with self.subTest(handle=repr(handle)):
                with self.assertRaises(LayoutChangedError) as caught:
                    self.download(portal, clock, handle)
                self.assertEqual(caught.exception.message, portal_module.RESULTS_ROW_HANDLE_MESSAGE)
        self.assertEqual(page.download_dispatches, [])
        fresh = PlaywrightPortal(ResultsConfig(), headed=False)
        fresh.page = page
        with self.assertRaises(LayoutChangedError):
            fresh.download(rows[0], self.root / "x.bin")
        self.assertEqual(page.download_dispatches, [])

    def test_download_errors_never_carry_private_values(self) -> None:
        state = SurfaceState(rows=[PRIVATE_ROW_TEXT, "r2"])
        portal, page, clock, rows = self.prepared(
            state, downloads={0: [{"kind": "save_error", "name": "2026-PRIVATE-ACCT-778899.pdf"}]}
        )
        with self.assertRaises(DownloadError) as caught:
            self.download(portal, clock, rows[0])
        text = caught.exception.message + repr(caught.exception.args)
        for private in ("ACCT-778899", PRIVATE_ROW_TEXT, ResultsConfig.account_identity, "portal.example.invalid"):
            self.assertNotIn(private, text)


class DownloadPreDispatchCharacterisationTests(unittest.TestCase):
    """DL-XB-199-DOWNLOAD-PREFLIGHT G3-084 Phase 1: current behaviour, pinned.

    Written and passed against BASE-equivalent source BEFORE the shared
    pre-dispatch refactor. These pins describe what the production path does
    today -- including two deliberate asymmetries that are characterised here
    and must NOT be silently repaired -- so the refactor is proven to be
    behaviour-preserving rather than assumed to be.
    """

    RECORDERS = (
        "role_lookups",
        "text_lookups",
        "clicks",
        "trial_clicks",
        "probe_timeouts",
        "evaluations",
        "expect_download_timeouts",
        "download_dispatches",
    )

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def prepared(self, rows=("r1", "r2", "r3")):
        portal, page, clock = surface_portal(SurfaceState(rows=list(rows)))
        with simulated_clock(clock):
            handles = portal.inventory(20)
        for name in self.RECORDERS:
            getattr(page, name).clear()
        clock.ledger.clear()
        return portal, page, clock, handles

    def download(self, portal, clock, row, name="download.bin"):
        with simulated_clock(clock):
            return portal.download(row, self.root / name)

    def test_the_production_dispatch_golden_is_unchanged(self) -> None:
        """The exact inspection/dispatch sequence of one successful download."""
        portal, page, clock, rows = self.prepared()
        self.assertEqual(self.download(portal, clock, rows[1]), "2026-09-02_synthetic.pdf")
        sentinels = [
            (role, name, True)
            for role in ("button", "link")
            for name in ("Next page", "Next", "Previous page", "Load more")
        ]
        self.assertEqual(
            page.role_lookups,
            [("tab", "EB Bill", True), ("tab", "Tenant Bill", True)]
            + sentinels
            + [("table", None, False), ("row", None, False)]
            + [("columnheader", None, False), ("button", "Download", True)] * 4
            + [("button", "Download", True)]
            + [("table", None, False), ("row", None, False), ("button", "Download", True)]
            + [("table", None, False), ("row", None, False)]
            + [("tab", "EB Bill", True), ("tab", "Tenant Bill", True)],
        )
        self.assertEqual(page.text_lookups, [(ResultsConfig.account_identity, True)])
        self.assertEqual(len(page.evaluations), 1)
        self.assertEqual(page.trial_clicks, ["row-download"])
        self.assertEqual(page.clicks, ["row-download"])
        self.assertEqual(page.probe_timeouts, [portal_module.MAX_PORTAL_PROBE_TIMEOUT_MS] * 11)
        self.assertEqual(page.expect_download_timeouts, [ResultsConfig.timeout_seconds * 1000])
        self.assertEqual(page.download_dispatches, [1])
        self.assertEqual(clock.ledger, [], "a healthy surface pays no recovery wait")
        self.assertFalse(portal._latched)

    def test_a_witness_read_race_is_transient_in_inventory_but_immediate_before_dispatch(self) -> None:
        """Characterised asymmetry: the same read race recovers in one path only."""
        original = SurfaceLocator.evaluate_all

        def racing(looks_to_race):
            calls = {"n": 0}

            def evaluate_all(self, expression, arg=None):
                calls["n"] += 1
                if calls["n"] <= looks_to_race:
                    self.page.evaluations.append(expression)
                    return []
                return original(self, expression, arg)

            return evaluate_all

        # Inventory: the race is a not-ready look and the witness later settles.
        portal, page, clock = surface_portal(SurfaceState(rows=["r1", "r2"]))
        with mock.patch.object(SurfaceLocator, "evaluate_all", racing(1)), simulated_clock(clock):
            rows = portal.inventory(20)
        self.assertEqual(len(rows), 2)
        self.assertGreater(len(clock.yields), 0, "inventory waited and looked again")

        # Pre-dispatch: the same race on the first look fails at once and latches.
        portal, page, clock, rows = self.prepared(rows=("r1", "r2"))
        with mock.patch.object(SurfaceLocator, "evaluate_all", racing(1)):
            with self.assertRaises(LayoutChangedError) as caught:
                self.download(portal, clock, rows[0])
        self.assertEqual(caught.exception.message, portal_module.RESULTS_SURFACE_CHANGED_MESSAGE)
        self.assertEqual(clock.yields, [], "no recovery wait before the immediate failure")
        self.assertTrue(portal._latched)
        self.assertEqual(page.download_dispatches, [])

    def test_the_final_row_recheck_is_one_shot_while_the_ladder_recovers_lag(self) -> None:
        """Characterised asymmetry: a lagging row snapshot is lag in the ladder only."""
        original = SurfaceLocator.aria_snapshot

        def lagging(failing_calls):
            calls = {"n": 0}

            def aria_snapshot(self, timeout=None):
                calls["n"] += 1
                if calls["n"] in failing_calls:
                    self.page.probe_timeouts.append(timeout)
                    raise synthetic_timeout()
                return original(self, timeout)

            return aria_snapshot

        # Header + 3 rows = 4 snapshots per ladder look; the 5th is the final
        # one-shot recheck. A lag inside the first look is recovered.
        portal, page, clock, rows = self.prepared()
        with mock.patch.object(SurfaceLocator, "aria_snapshot", lagging({1})):
            self.download(portal, clock, rows[0])
        self.assertEqual(page.download_dispatches, [0])
        self.assertGreater(len(clock.yields), 0)
        self.assertFalse(portal._latched)

        # The same lag at the one-shot recheck is final: latched, zero dispatch.
        portal, page, clock, rows = self.prepared()
        with mock.patch.object(SurfaceLocator, "aria_snapshot", lagging({5})):
            with self.assertRaises(LayoutChangedError) as caught:
                self.download(portal, clock, rows[0])
        self.assertEqual(caught.exception.message, portal_module.RESULTS_SURFACE_CHANGED_MESSAGE)
        self.assertEqual(clock.yields, [], "the recheck never re-looks")
        self.assertTrue(portal._latched)
        self.assertEqual(page.download_dispatches, [])

    def test_handle_failures_do_not_latch_and_a_latched_portal_is_not_relatched(self) -> None:
        """HANDLE semantics: the latch check precedes the handle check; neither writes."""
        portal, page, clock, rows = self.prepared()
        forged = portal_module.InvoiceRow(ordinal=7, binding=rows[0].binding)
        with self.assertRaises(LayoutChangedError) as caught:
            self.download(portal, clock, forged)
        self.assertEqual(caught.exception.message, portal_module.RESULTS_ROW_HANDLE_MESSAGE)
        self.assertFalse(portal._latched, "an invalid handle never latches")
        self.assertEqual(page.role_lookups, [], "nothing is inspected for a bad handle")
        self.assertTrue(self.download(portal, clock, rows[0]), "a later valid row still dispatches")
        self.assertEqual(page.download_dispatches, [0])

        portal, page, clock, rows = self.prepared()
        portal._latched = True
        writes: list[bool] = []
        original_setattr = PlaywrightPortal.__setattr__

        def recording_setattr(self, name, value):
            if name == "_latched":
                writes.append(value)
            original_setattr(self, name, value)

        with mock.patch.object(PlaywrightPortal, "__setattr__", recording_setattr):
            for handle in (rows[0], forged, "row-0"):
                with self.subTest(handle=repr(handle)):
                    with self.assertRaises(LayoutChangedError) as caught:
                        self.download(portal, clock, handle)
                    self.assertEqual(caught.exception.message, portal_module.RESULTS_LATCHED_MESSAGE)
        self.assertEqual(writes, [], "the latched state is preserved, never re-written")
        self.assertEqual(page.role_lookups, [])
        self.assertEqual(page.download_dispatches, [])

    def test_every_pre_dispatch_surface_failure_latches(self) -> None:
        """Only the surface/recheck family latches; it latches on every member."""
        mutations = {
            "topology": lambda state: setattr(state, "extra_pages", 1),
            "tab": lambda state: (setattr(state, "eb_selected", False), setattr(state, "tenant_selected", True)),
            "witness": lambda state: setattr(state, "witnesses", [True, True]),
            "pagination": lambda state: setattr(state, "pagination", {("link", "Next")}),
            "snapshot": lambda state: state.rows.reverse(),
            "invalid": lambda state: setattr(state, "snapshot_error", RuntimeError("private ACCT-778899")),
        }
        for label, mutate in mutations.items():
            with self.subTest(drift=label):
                portal, page, clock, rows = self.prepared()
                mutate(page.state)
                with self.assertRaises(LayoutChangedError) as caught:
                    self.download(portal, clock, rows[0])
                self.assertEqual(caught.exception.message, portal_module.RESULTS_SURFACE_CHANGED_MESSAGE)
                self.assertEqual(caught.exception.status, PORTAL_LAYOUT_CHANGED)
                self.assertEqual(caught.exception.exit_code, 20)
                self.assertFalse(caught.exception.retryable)
                self.assertTrue(portal._latched)
                self.assertEqual(page.download_dispatches, [])


def _surface_mutation(**changes):
    def mutate(state: SurfaceState) -> None:
        for key, value in changes.items():
            if not hasattr(state, key):
                raise AttributeError(key)
            setattr(state, key, value)

    return mutate


# reason -> the post-inventory surface mutation that reaches it. HANDLE reasons
# are driven by the handle / latch itself, not by the surface.
PREFLIGHT_REASON_SCENARIOS = {
    "TOPOLOGY_NOT_UNIQUE": _surface_mutation(extra_pages=1),
    "TAB_UNSELECTED": _surface_mutation(eb_selected=False, tenant_selected=True),
    "TAB_AMBIGUOUS": _surface_mutation(eb_tab_count=2),
    "TAB_ABSENT": _surface_mutation(eb_tab_count=0),
    "TAB_CONTRADICTORY": _surface_mutation(tenant_selected=True),
    "WITNESS_MULTIPLE": _surface_mutation(witnesses=[True, True]),
    "WITNESS_READ_RACE": _surface_mutation(witness_flag_override=[]),
    "WITNESS_ABSENT": _surface_mutation(witnesses=[False]),
    "RESULTS_PAGINATION_PRESENT": _surface_mutation(pagination={("button", "Load more")}),
    "RESULTS_CEILING_EXCEEDED": lambda state: state.rows.extend(f"extra {i}" for i in range(25)),
    "RESULTS_ROWCOUNT_INVALID": _surface_mutation(aria_rowcount="many"),
    "RESULTS_ROW_SNAPSHOT_INVALID": _surface_mutation(snapshot_error=RuntimeError("private ACCT-778899")),
    "RESULTS_TABLE_ABSENT": _surface_mutation(tables=0),
    "RESULTS_TABLE_AMBIGUOUS": _surface_mutation(tables=2),
    "RESULTS_ROWS_ABSENT": _surface_mutation(rows_absent=True),
    "RESULTS_HEADER_NO_COLUMNHEADER": _surface_mutation(header_columnheaders=0),
    "RESULTS_HEADER_HAS_DOWNLOAD": _surface_mutation(header_download_buttons=1),
    "RESULTS_HEADER_ONLY": _surface_mutation(rows=[]),
    "RESULTS_ROW_HEADER_CELL": _surface_mutation(row_columnheaders={1: 1}),
    "RESULTS_ROW_DOWNLOAD_COUNT": _surface_mutation(download_buttons={1: 2}),
    "RESULTS_DOWNLOAD_COUNT_MISMATCH": _surface_mutation(stray_download_buttons=1),
    "RESULTS_ROWCOUNT_UNREAD": _surface_mutation(rowcount_timeout=True),
    "RESULTS_ROWCOUNT_MISMATCH": _surface_mutation(aria_rowcount="9"),
    "RESULTS_ROW_SNAPSHOT_NOT_READY": _surface_mutation(snapshot_error=synthetic_timeout()),
    "RESULTS_ROW_SNAPSHOT_DUPLICATE": _surface_mutation(rows=["same", "same", "same"]),
    "SNAPSHOT_MISMATCH": lambda state: state.rows.reverse(),
    "CONTROL_COUNT_MISMATCH": _surface_mutation(control_count=0),
    "CONTROL_NOT_VISIBLE": _surface_mutation(control_visible=False),
    "CONTROL_NOT_ENABLED": _surface_mutation(control_enabled=False),
    "CONTROL_NOT_ACTIONABLE": _surface_mutation(control_actionable=False),
    "ROW_RECHECK_NOT_READY": _surface_mutation(recheck="timeout"),
    "ROW_RECHECK_MISMATCH": _surface_mutation(recheck="mismatch"),
    "ROW_RECHECK_INVALID": _surface_mutation(recheck="invalid"),
    "PROBE_ERROR": _surface_mutation(control_trial_error=RuntimeError("private https://portal.example.invalid")),
}


class DownloadPreflightReachabilityTests(unittest.TestCase):
    """Every one of the 36 closed reasons, through production and the diagnostic."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def prepared(self):
        portal, page, clock = surface_portal(SurfaceState(rows=["r1", "r2", "r3"]))
        with simulated_clock(clock):
            rows = portal.inventory(20)
        page.clicks.clear()
        page.trial_clicks.clear()
        return portal, page, clock, rows

    def assert_zero_dispatch(self, page) -> None:
        self.assertEqual(page.download_dispatches, [])
        self.assertNotIn("row-download", page.clicks)
        self.assertEqual(page.expect_download_timeouts, [])

    def assert_evidence(self, evidence, reason: str) -> None:
        kind = portal_module.DOWNLOAD_PREFLIGHT_REASON_KINDS[reason]
        self.assertEqual(evidence.reason_code, reason)
        if reason != portal_module.PREFLIGHT_PROBE_ERROR:
            self.assertEqual(evidence.last_checkpoint, portal_module.DOWNLOAD_PREFLIGHT_REASON_CHECKPOINTS[reason])
        self.assertIn(evidence.last_checkpoint, portal_module.DOWNLOAD_PREFLIGHT_CHECKPOINTS)
        self.assertIs(evidence.window_expired, kind == portal_module.PREFLIGHT_WINDOW)
        if kind == portal_module.PREFLIGHT_WINDOW:
            self.assertEqual(evidence.not_ready_looks, len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS))
        else:
            self.assertEqual(evidence.not_ready_looks, 0)
            self.assertEqual(evidence.elapsed_bucket, "LT_250MS")
        self.assertIn(evidence.elapsed_bucket, portal_module.DOWNLOAD_PREFLIGHT_ELAPSED_BUCKETS)
        self.assertEqual(evidence.snapshot is not None, reason == "SNAPSHOT_MISMATCH")

    def test_the_taxonomy_is_exactly_11_checkpoints_and_36_reasons(self) -> None:
        self.assertEqual(
            portal_module.DOWNLOAD_PREFLIGHT_CHECKPOINTS,
            (
                "HANDLE", "TOPOLOGY", "TAB_STATE", "ACCOUNT_WITNESS", "RESULTS_SURFACE",
                "SNAPSHOT_COMPARE", "CONTROL_RESOLVE", "CONTROL_VISIBLE", "CONTROL_ENABLED",
                "CONTROL_ACTIONABLE", "ROW_IDENTITY_RECHECK",
            ),
        )
        self.assertEqual(len(portal_module.DOWNLOAD_PREFLIGHT_REASONS), 36)
        self.assertEqual(len(set(portal_module.DOWNLOAD_PREFLIGHT_REASONS)), 36)
        self.assertEqual(portal_module.DOWNLOAD_PREFLIGHT_REASONS[-1], "PROBE_ERROR")
        self.assertEqual(
            set(portal_module.DOWNLOAD_PREFLIGHT_REASONS),
            set(PREFLIGHT_REASON_SCENARIOS) | {"PORTAL_LATCHED", "ROW_HANDLE_INVALID"},
        )
        immediate = {
            "PORTAL_LATCHED", "ROW_HANDLE_INVALID", "TOPOLOGY_NOT_UNIQUE", "TAB_UNSELECTED",
            "TAB_AMBIGUOUS", "WITNESS_MULTIPLE", "WITNESS_READ_RACE", "RESULTS_PAGINATION_PRESENT",
            "RESULTS_CEILING_EXCEEDED", "RESULTS_ROWCOUNT_INVALID", "RESULTS_ROW_SNAPSHOT_INVALID",
            "SNAPSHOT_MISMATCH", "ROW_RECHECK_NOT_READY", "ROW_RECHECK_MISMATCH",
            "ROW_RECHECK_INVALID", "PROBE_ERROR",
        }
        for reason, kind in portal_module.DOWNLOAD_PREFLIGHT_REASON_KINDS.items():
            with self.subTest(reason=reason):
                expected = portal_module.PREFLIGHT_IMMEDIATE if reason in immediate else portal_module.PREFLIGHT_WINDOW
                self.assertEqual(kind, expected)

    def test_every_surface_reason_is_reachable_through_production_download(self) -> None:
        for reason, mutate in PREFLIGHT_REASON_SCENARIOS.items():
            with self.subTest(reason=reason):
                portal, page, clock, rows = self.prepared()
                mutate(page.state)
                with simulated_clock(clock), self.assertRaises(portal_module.DownloadPreflightError) as caught:
                    portal.download(rows[0], self.root / "x.bin")
                error = caught.exception
                self.assertIsInstance(error, LayoutChangedError)
                self.assertEqual(error.message, portal_module.RESULTS_SURFACE_CHANGED_MESSAGE)
                self.assertEqual((error.status, error.exit_code, error.retryable), (PORTAL_LAYOUT_CHANGED, 20, False))
                self.assert_evidence(error.evidence, reason)
                self.assertEqual(error.evidence.row_ordinal, 0)
                self.assertTrue(portal._latched, "the surface/recheck family latches")
                self.assert_zero_dispatch(page)

    def test_every_surface_reason_is_reachable_through_the_diagnostic(self) -> None:
        for reason, mutate in PREFLIGHT_REASON_SCENARIOS.items():
            with self.subTest(reason=reason):
                portal, page, clock, rows = self.prepared()
                mutate(page.state)
                with simulated_clock(clock):
                    result = portal.download_preflight(rows)
                self.assertIsInstance(result, portal_module.DownloadPreflightDiagnosticResult)
                self.assertEqual(result.rows_passed, 0, "stops at the first failing row")
                self.assertFalse(result.download_dispatched)
                self.assert_evidence(result.failure, reason)
                self.assertEqual(result.failure.row_ordinal, 0)
                self.assertTrue(portal._latched, "the same production latch effect")
                self.assert_zero_dispatch(page)

    def test_the_handle_reasons_keep_their_exact_latch_semantics(self) -> None:
        portal, page, clock, rows = self.prepared()
        forged = portal_module.InvoiceRow(ordinal=9, binding=rows[0].binding)
        for label, call in (
            ("production", lambda: portal.download(forged, self.root / "x.bin")),
            ("diagnostic", lambda: portal.download_preflight([forged])),
        ):
            with self.subTest(path=label):
                with simulated_clock(clock):
                    if label == "production":
                        with self.assertRaises(portal_module.DownloadPreflightError) as caught:
                            call()
                        evidence = caught.exception.evidence
                        self.assertEqual(caught.exception.message, portal_module.RESULTS_ROW_HANDLE_MESSAGE)
                    else:
                        evidence = call().failure
                self.assert_evidence(evidence, "ROW_HANDLE_INVALID")
                self.assertEqual(evidence.row_ordinal, 9)
                self.assertFalse(portal._latched, "ROW_HANDLE_INVALID never latches")
        for handle in ("row-0", portal_module.InvoiceRow(ordinal=True, binding=rows[0].binding)):
            with simulated_clock(clock), self.assertRaises(portal_module.DownloadPreflightError) as caught:
                portal.download(handle, self.root / "x.bin")
            self.assertIsNone(caught.exception.evidence.row_ordinal, "no ordinal is invented")

        portal._latched = True
        writes: list[bool] = []
        original_setattr = PlaywrightPortal.__setattr__

        def recording_setattr(self, name, value):
            if name == "_latched":
                writes.append(value)
            original_setattr(self, name, value)

        with mock.patch.object(PlaywrightPortal, "__setattr__", recording_setattr), simulated_clock(clock):
            with self.assertRaises(portal_module.DownloadPreflightError) as caught:
                portal.download(rows[0], self.root / "x.bin")
            diagnostic = portal.download_preflight(rows)
        self.assertEqual(caught.exception.message, portal_module.RESULTS_LATCHED_MESSAGE)
        self.assert_evidence(caught.exception.evidence, "PORTAL_LATCHED")
        self.assert_evidence(diagnostic.failure, "PORTAL_LATCHED")
        self.assertEqual(diagnostic.rows_passed, 0)
        self.assertEqual(writes, [], "PORTAL_LATCHED performs no latch write")
        self.assert_zero_dispatch(page)

    def test_control_evidence_reports_how_far_the_last_look_got(self) -> None:
        cases = {
            "CONTROL_COUNT_MISMATCH": {"count_bucket": 0, "visible": None, "enabled": None, "trial_actionability": "NOT_REACHED"},
            "CONTROL_NOT_VISIBLE": {"count_bucket": 1, "visible": False, "enabled": None, "trial_actionability": "NOT_REACHED"},
            "CONTROL_NOT_ENABLED": {"count_bucket": 1, "visible": True, "enabled": False, "trial_actionability": "NOT_REACHED"},
            "CONTROL_NOT_ACTIONABLE": {"count_bucket": 1, "visible": True, "enabled": True, "trial_actionability": "TIMEOUT"},
            "PROBE_ERROR": {"count_bucket": 1, "visible": True, "enabled": True, "trial_actionability": "ERROR"},
            "TAB_ABSENT": {"count_bucket": None, "visible": None, "enabled": None, "trial_actionability": "NOT_REACHED"},
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                portal, page, clock, rows = self.prepared()
                PREFLIGHT_REASON_SCENARIOS[reason](page.state)
                with simulated_clock(clock):
                    evidence = portal.download_preflight(rows).failure
                self.assertEqual(evidence.control.as_public_dict(), expected)
                if reason == "PROBE_ERROR":
                    self.assertEqual(evidence.last_checkpoint, "CONTROL_ACTIONABLE")
        portal, page, clock, rows = self.prepared()
        page.state.control_count = 2
        with simulated_clock(clock):
            evidence = portal.download_preflight(rows).failure
        self.assertEqual(evidence.control.count_bucket, ">1")
        portal, page, clock, rows = self.prepared()
        page.state.control_enabled_error = RuntimeError("private")
        with simulated_clock(clock):
            evidence = portal.download_preflight(rows).failure
        self.assertEqual((evidence.reason_code, evidence.last_checkpoint), ("PROBE_ERROR", "CONTROL_ENABLED"))
        self.assertIsNone(evidence.control.enabled)

    def test_snapshot_evidence_compares_without_any_digest(self) -> None:
        portal, page, clock, rows = self.prepared()
        page.state.rows.reverse()
        with simulated_clock(clock):
            evidence = portal.download_preflight(rows).failure
        self.assertEqual(
            evidence.snapshot.as_public_dict(),
            {"row_count_equal": True, "header_equal": True, "rows_equal_as_set": True, "changed_row_count": 2},
        )
        portal, page, clock, rows = self.prepared()
        page.state.header_text = "Invoice Action Status"
        page.state.rows.append("r4")
        with simulated_clock(clock):
            evidence = portal.download_preflight(rows).failure
        self.assertEqual(
            evidence.snapshot.as_public_dict(),
            {"row_count_equal": False, "header_equal": False, "rows_equal_as_set": False, "changed_row_count": 1},
        )

    def test_the_diagnostic_passes_every_row_and_then_latches(self) -> None:
        portal, page, clock, rows = self.prepared()
        spy_calls: list[int] = []
        original = PlaywrightPortal._pre_dispatch

        def spy(self, row, trace):
            spy_calls.append(row.ordinal)
            return original(self, row, trace)

        latched_during: list[bool] = []
        original_reprove = PlaywrightPortal._reprove_frozen_surface

        def reprove(self, *args, **kwargs):
            latched_during.append(self._latched)
            return original_reprove(self, *args, **kwargs)

        with mock.patch.object(PlaywrightPortal, "_pre_dispatch", spy), \
                mock.patch.object(PlaywrightPortal, "_reprove_frozen_surface", reprove), simulated_clock(clock):
            result = portal.download_preflight(rows)
        self.assertEqual(result, portal_module.DownloadPreflightDiagnosticResult(rows_passed=3))
        self.assertEqual(spy_calls, [0, 1, 2], "the one shared proof, once per row, in order")
        self.assertEqual(latched_during, [False, False, False], "latched only after the whole inspection")
        self.assertTrue(portal._latched)
        self.assertEqual(page.trial_clicks, ["row-download"] * 3, "trial actionability only")
        self.assert_zero_dispatch(page)
        with simulated_clock(clock), self.assertRaises(portal_module.DownloadPreflightError) as caught:
            portal.download(rows[0], self.root / "x.bin")
        self.assertEqual(caught.exception.evidence.reason_code, "PORTAL_LATCHED")
        self.assert_zero_dispatch(page)

    def test_the_diagnostic_stops_at_the_first_failing_row(self) -> None:
        portal, page, clock, rows = self.prepared()
        page.state.control_ordinals = {1}
        page.state.control_visible = False
        calls: list[int] = []
        original = PlaywrightPortal._pre_dispatch

        def spy(self, row, trace):
            calls.append(row.ordinal)
            return original(self, row, trace)

        with mock.patch.object(PlaywrightPortal, "_pre_dispatch", spy), simulated_clock(clock):
            result = portal.download_preflight(rows)
        self.assertEqual(calls, [0, 1], "row 2 is never inspected")
        self.assertEqual(result.rows_passed, 1)
        self.assertEqual((result.failure.row_ordinal, result.failure.reason_code), (1, "CONTROL_NOT_VISIBLE"))
        self.assert_zero_dispatch(page)

    def test_a_production_success_still_dispatches_through_the_shared_proof(self) -> None:
        portal, page, clock, rows = self.prepared()
        calls: list[int] = []
        original = PlaywrightPortal._pre_dispatch

        def spy(self, row, trace):
            calls.append(row.ordinal)
            return original(self, row, trace)

        with mock.patch.object(PlaywrightPortal, "_pre_dispatch", spy), simulated_clock(clock):
            names = [portal.download(row, self.root / f"{row.ordinal}.bin") for row in rows]
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual(page.download_dispatches, [0, 1, 2])
        self.assertEqual(len(set(names)), 3)
        self.assertFalse(portal._latched)

    def test_no_private_value_reaches_the_error_or_its_evidence(self) -> None:
        private = (PRIVATE_ROW_TEXT, "ACCT-778899", ResultsConfig.account_identity, "portal.example.invalid", "SyntheticTimeoutError", "RuntimeError")
        closed = (
            set(portal_module.DOWNLOAD_PREFLIGHT_REASONS)
            | set(portal_module.DOWNLOAD_PREFLIGHT_CHECKPOINTS)
            | set(portal_module.DOWNLOAD_PREFLIGHT_ELAPSED_BUCKETS)
            | set(portal_module.DOWNLOAD_PREFLIGHT_TRIAL_OUTCOMES)
            | {">1"}
        )

        def strings(value):
            if isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
            elif isinstance(value, str):
                yield value

        for reason, mutate in PREFLIGHT_REASON_SCENARIOS.items():
            with self.subTest(reason=reason):
                portal, page, clock = surface_portal(SurfaceState(rows=[PRIVATE_ROW_TEXT, "r2", "r3"]))
                with simulated_clock(clock):
                    rows = portal.inventory(20)
                mutate(page.state)
                with simulated_clock(clock), self.assertRaises(portal_module.DownloadPreflightError) as caught:
                    portal.download(rows[0], self.root / "x.bin")
                public = caught.exception.evidence.as_public_dict()
                text = json.dumps(public) + repr(caught.exception) + repr(caught.exception.evidence) + caught.exception.message
                for fragment in private:
                    self.assertNotIn(fragment, text)
                self.assertTrue(set(strings(public)) <= closed)
                for digest in portal._frozen_rows:
                    self.assertNotIn(digest.hex(), text)

    def test_the_diagnostic_shares_the_production_proof_and_never_dispatches(self) -> None:
        """Static sharing checks: one proof, no second ladder, no Download."""
        source = pathlib_read_portal_source()
        self.assertEqual(source.count("def _pre_dispatch("), 1)
        self.assertEqual(source.count("def _reprove_frozen_surface("), 1)
        download = source[source.index("    def download(self"):source.index("    def _require_post_download_surface")]
        self.assertEqual(download.count("self._pre_dispatch("), 1)
        self.assertEqual(download.count("_reprove_frozen_surface"), 0, "download no longer owns the ladder")
        diagnostic = source[source.index("    def download_preflight("):source.index("    def _reprove_frozen_surface(")]
        self.assertEqual(diagnostic.count("self._pre_dispatch("), 1)
        for forbidden in (
            ".click(", "expect_download", "save_as", "_reprove_frozen_surface(", "_row_identity(",
            "get_by_role", "_read_results_surface", "_account_witness_count", "_eb_bill_tab_state",
            "wait_for_timeout", "_recover(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, diagnostic)
        self.assertEqual(diagnostic.count("self._latched = True"), 1)
        pre = source[source.index("    def _pre_dispatch("):source.index("    def download_preflight(")]
        self.assertNotIn(".click(", pre)
        self.assertNotIn("expect_download", pre)
        self.assertEqual(pre.count("self._latched = True"), 1, "one latch write, on the surface family only")


class LookScript:
    """Scripted surface changes between the recovery looks of one proof.

    `looks[0]` is applied at once; `looks[k]` is applied by the wait that
    precedes look k. The portal is never touched: only the fake page's state
    changes, and only inside `wait_for_timeout`, which is exactly where the
    real surface would re-render between looks.
    """

    def __init__(self, page: FakeSurfacePage, looks: dict) -> None:
        self.page = page
        self.looks = looks
        self.index = 0
        self.original = page.wait_for_timeout
        page.wait_for_timeout = self.wait_for_timeout
        self.apply()

    def apply(self) -> None:
        mutate = self.looks.get(self.index)
        if mutate is not None:
            mutate(self.page.state)

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.original(milliseconds)
        self.index += 1
        self.apply()


LADDER_LOOKS = len(portal_module.PORTAL_RECOVERY_ATTEMPTS_MS)


class DownloadPreflightSurfaceScanTests(unittest.TestCase):
    """DL-XB-199 G3-092: evidence-only `surface_scan` for RESULTS_ROW_DOWNLOAD_COUNT."""

    RECORDERS = DownloadPreDispatchCharacterisationTests.RECORDERS

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def prepared(self, rows=("r1", "r2", "r3", "r4")):
        portal, page, clock = surface_portal(SurfaceState(rows=list(rows)))
        with simulated_clock(clock):
            handles = portal.inventory(20)
        for name in self.RECORDERS:
            getattr(page, name).clear()
        clock.ledger.clear()
        return portal, page, clock, handles

    def run_diagnostic(self, looks=None, *, before_row=None, rows=("r1", "r2", "r3", "r4")):
        portal, page, clock, handles = self.prepared(rows)
        original = PlaywrightPortal._pre_dispatch
        scripts: list[LookScript] = []

        def pre_dispatch(self, row, trace):
            if before_row is not None and row.ordinal == before_row[0]:
                scripts.append(LookScript(page, before_row[1]))
            return original(self, row, trace)

        if looks is not None:
            scripts.append(LookScript(page, looks))
        with mock.patch.object(PlaywrightPortal, "_pre_dispatch", pre_dispatch), simulated_clock(clock):
            result = portal.download_preflight(handles)
        return portal, page, clock, result

    def scan(self, result) -> dict:
        self.assertIsNotNone(result.failure)
        self.assertIsNotNone(result.failure.surface_scan)
        return result.failure.surface_scan.as_public_dict()

    def assert_zero_dispatch(self, portal, page) -> None:
        self.assertEqual(page.download_dispatches, [])
        self.assertNotIn("row-download", page.clicks)
        self.assertEqual(page.expect_download_timeouts, [])
        self.assertTrue(portal._latched, "the production latch effect is unchanged")

    def assert_row_download_count(self, result, *, row_ordinal=0, looks=LADDER_LOOKS) -> None:
        failure = result.failure
        self.assertEqual(failure.reason_code, "RESULTS_ROW_DOWNLOAD_COUNT")
        self.assertEqual(failure.last_checkpoint, "RESULTS_SURFACE")
        self.assertIs(failure.window_expired, True)
        self.assertEqual(failure.not_ready_looks, looks)
        self.assertEqual(failure.row_ordinal, row_ordinal)
        self.assertEqual(result.rows_passed, row_ordinal)
        self.assertIsNone(failure.snapshot)
        self.assertEqual(failure.control, portal_module.DownloadPreflightControl())
        document = cli.download_preflight_document(4, result)
        self.assertEqual(document["result"], "PREFLIGHT_ROW_FAILED", "the v2 validator admits it")
        self.assertEqual(document["failure"]["surface_scan"], failure.surface_scan.as_public_dict())

    # ---- the required matrix ---- #

    def test_01_every_row_with_exactly_one_download_passes_with_no_surface_scan(self) -> None:
        portal, page, _clock, result = self.run_diagnostic()
        self.assertEqual(result, portal_module.DownloadPreflightDiagnosticResult(rows_passed=4))
        self.assertIsNone(result.failure)
        document = cli.download_preflight_document(4, result)
        self.assertEqual((document["result"], document["failure"]), ("PREFLIGHT_ALL_ROWS_PASSED", None))
        self.assertEqual(page.trial_clicks, ["row-download"] * 4)
        self.assert_zero_dispatch(portal, page)

    def test_02_a_row_permanently_without_a_download_is_named_with_bucket_zero(self) -> None:
        for n in range(4):
            with self.subTest(n=n):
                portal, page, _clock, result = self.run_diagnostic({0: _surface_mutation(download_buttons={n: 0})})
                self.assert_row_download_count(result)
                self.assertEqual(
                    self.scan(result),
                    {
                        "offending_surface_row_ordinal": n,
                        "offending_download_count_bucket": 0,
                        "surface_row_count_equal_frozen": True,
                        "offending_witness_looks": LADDER_LOOKS,
                        "offending_row_changed_between_looks": False,
                    },
                )
                self.assert_zero_dispatch(portal, page)

    def test_03_a_row_permanently_with_several_downloads_has_bucket_gt_one(self) -> None:
        for count in (2, 3, 9):
            with self.subTest(count=count):
                portal, page, _clock, result = self.run_diagnostic({0: _surface_mutation(download_buttons={1: count})})
                self.assert_row_download_count(result)
                scan = self.scan(result)
                self.assertEqual(scan["offending_surface_row_ordinal"], 1)
                self.assertEqual(scan["offending_download_count_bucket"], ">1")
                self.assertEqual(scan["offending_witness_looks"], LADDER_LOOKS)
                self.assertIs(scan["offending_row_changed_between_looks"], False)
                self.assert_zero_dispatch(portal, page)

    def test_04_a_moving_offending_row_is_changed_and_counts_only_the_stable_tail(self) -> None:
        cases = {
            # (3,0) x2 then (2,>1) x5
            "row and bucket move": ({0: _surface_mutation(download_buttons={3: 0}),
                                     2: _surface_mutation(download_buttons={2: 2})}, (2, ">1"), 5),
            # (1,0) x6 then (1,>1) x1: same row, bucket moves
            "bucket moves on the last look": ({0: _surface_mutation(download_buttons={1: 0}),
                                               6: _surface_mutation(download_buttons={1: 2})}, (1, ">1"), 1),
            # (0,0), (3,0), (0,0) x5: an earlier witness recurring still counts as changed
            "witness returns": ({0: _surface_mutation(download_buttons={0: 0}),
                                 1: _surface_mutation(download_buttons={3: 0}),
                                 2: _surface_mutation(download_buttons={0: 0})}, (0, 0), 5),
        }
        for label, (looks, (ordinal, bucket), tail) in cases.items():
            with self.subTest(case=label):
                portal, page, _clock, result = self.run_diagnostic(looks)
                self.assert_row_download_count(result)
                scan = self.scan(result)
                self.assertEqual(
                    (scan["offending_surface_row_ordinal"], scan["offending_download_count_bucket"]), (ordinal, bucket)
                )
                self.assertEqual(scan["offending_witness_looks"], tail)
                self.assertIs(scan["offending_row_changed_between_looks"], True)
                self.assert_zero_dispatch(portal, page)

    def test_05_a_bad_count_that_clears_before_the_deadline_passes(self) -> None:
        portal, page, clock, result = self.run_diagnostic(
            {0: _surface_mutation(download_buttons={3: 0}), 3: _surface_mutation(download_buttons={})}
        )
        self.assertEqual(result, portal_module.DownloadPreflightDiagnosticResult(rows_passed=4))
        self.assertIsNone(result.failure, "no failure object, so no surface_scan")
        self.assertEqual(len(clock.yields), 3, "recovered on the fourth look")
        self.assert_zero_dispatch(portal, page)

    def test_06_a_bad_count_that_persists_expires_the_window_after_seven_looks(self) -> None:
        self.assertEqual(LADDER_LOOKS, 7)
        portal, page, clock, result = self.run_diagnostic({0: _surface_mutation(download_buttons={2: 0})})
        self.assert_row_download_count(result, looks=7)
        self.assertEqual(result.failure.elapsed_bucket, "LT_60S")
        self.assertEqual(self.scan(result)["offending_witness_looks"], 7)
        self.assertEqual(len(clock.yields), 6)
        self.assert_zero_dispatch(portal, page)

    def test_07_a_stray_global_download_is_a_count_mismatch_without_surface_scan(self) -> None:
        portal, page, _clock, result = self.run_diagnostic({0: _surface_mutation(stray_download_buttons=1)})
        self.assertEqual(result.failure.reason_code, "RESULTS_DOWNLOAD_COUNT_MISMATCH")
        self.assertIsNone(result.failure.surface_scan)
        self.assertIsNone(cli.download_preflight_document(4, result)["failure"]["surface_scan"])
        self.assert_zero_dispatch(portal, page)
        # An earlier row-count look does not leak into the final, different reason.
        portal, page, _clock, result = self.run_diagnostic(
            {0: _surface_mutation(download_buttons={1: 0}), 2: _surface_mutation(download_buttons={}, stray_download_buttons=1)}
        )
        self.assertEqual(result.failure.reason_code, "RESULTS_DOWNLOAD_COUNT_MISMATCH")
        self.assertIsNone(result.failure.surface_scan)

    def test_08_a_reordered_readable_surface_is_a_snapshot_mismatch_without_surface_scan(self) -> None:
        for label, looks in {
            "immediately": {0: lambda state: state.rows.reverse()},
            "after row-count looks": {0: _surface_mutation(download_buttons={3: 0}),
                                      2: lambda state: (state.rows.reverse(), setattr(state, "download_buttons", {}))},
        }.items():
            with self.subTest(case=label):
                portal, page, _clock, result = self.run_diagnostic(looks)
                failure = result.failure
                self.assertEqual(failure.reason_code, "SNAPSHOT_MISMATCH")
                self.assertIs(failure.window_expired, False)
                self.assertEqual(
                    failure.snapshot.as_public_dict(),
                    {"row_count_equal": True, "header_equal": True, "rows_equal_as_set": True, "changed_row_count": 4},
                )
                self.assertIsNone(failure.surface_scan)
                document = cli.download_preflight_document(4, result)
                self.assertIsNotNone(document["failure"]["snapshot"])
                self.assertIsNone(document["failure"]["surface_scan"])
                self.assert_zero_dispatch(portal, page)

    def test_09_the_handle_ordinal_and_the_offending_surface_ordinal_stay_distinct(self) -> None:
        portal, page, _clock, result = self.run_diagnostic(before_row=(1, {0: _surface_mutation(download_buttons={3: 0})}))
        self.assert_row_download_count(result, row_ordinal=1)
        self.assertEqual(result.failure.row_ordinal, 1, "the handle being proven")
        self.assertEqual(self.scan(result)["offending_surface_row_ordinal"], 3, "the surface row that failed")
        document = cli.download_preflight_document(4, result)
        self.assertEqual(
            (document["failure"]["row_ordinal"], document["failure"]["surface_scan"]["offending_surface_row_ordinal"]),
            (1, 3),
        )
        self.assertEqual(page.trial_clicks, ["row-download"], "handle 0 passed its complete proof first")
        self.assert_zero_dispatch(portal, page)

    def test_10_an_extra_role_row_without_a_download_differs_from_the_frozen_count(self) -> None:
        def insert(state: SurfaceState) -> None:
            state.rows.append("r5")
            state.download_buttons = {4: 0}

        portal, page, _clock, result = self.run_diagnostic({0: insert})
        self.assert_row_download_count(result)
        self.assertEqual(
            self.scan(result),
            {
                "offending_surface_row_ordinal": 4,
                "offending_download_count_bucket": 0,
                "surface_row_count_equal_frozen": False,
                "offending_witness_looks": LADDER_LOOKS,
                "offending_row_changed_between_looks": False,
            },
        )
        self.assert_zero_dispatch(portal, page)

    def test_11_a_mixed_ladder_counts_only_the_stable_row_count_tail(self) -> None:
        cases = {
            "table absent, then row count": {0: _surface_mutation(tables=0),
                                             2: _surface_mutation(tables=1, download_buttons={2: 0})},
            "tab absent, then row count": {0: _surface_mutation(eb_tab_count=0),
                                           3: _surface_mutation(eb_tab_count=1, download_buttons={2: 0})},
            "row count, other reason, same row count": {0: _surface_mutation(download_buttons={2: 0}),
                                                        1: _surface_mutation(tables=0),
                                                        2: _surface_mutation(tables=1)},
        }
        expected_tail = {"table absent, then row count": 5, "tab absent, then row count": 4,
                         "row count, other reason, same row count": 5}
        for label, looks in cases.items():
            with self.subTest(case=label):
                portal, page, _clock, result = self.run_diagnostic(looks)
                self.assert_row_download_count(result)
                scan = self.scan(result)
                self.assertEqual(scan["offending_witness_looks"], expected_tail[label])
                self.assertLess(scan["offending_witness_looks"], result.failure.not_ready_looks)
                self.assertEqual((scan["offending_surface_row_ordinal"], scan["offending_download_count_bucket"]), (2, 0))
                self.assertIs(scan["offending_row_changed_between_looks"], False)
                self.assert_zero_dispatch(portal, page)

    # ---- evidence only: identical operations ---- #

    def operation_log(self, looks, *, traced: bool, before_row=None, production: bool = False) -> dict:
        """Every Playwright-facing call the fake page saw, plus the outcome."""
        portal, page, clock, handles = self.prepared()
        original_read = PlaywrightPortal._read_results_surface
        original_pre = PlaywrightPortal._pre_dispatch
        row_count_reads: list[int | None] = []
        original_count = SurfaceLocator.count

        def count(locator):
            if locator.kind == "row-download" and locator.purpose == "read":
                row_count_reads.append(locator.index)
            return original_count(locator)

        def read(self, page_, remaining_ms, safety_ceiling, *, trace=None, frozen_length=None):
            # The un-enriched baseline: the same read with no trace at all.
            if not traced:
                trace, frozen_length = None, None
            return original_read(self, page_, remaining_ms, safety_ceiling, trace=trace, frozen_length=frozen_length)

        def pre_dispatch(self, row, trace):
            if before_row is not None and row.ordinal == before_row[0]:
                LookScript(page, before_row[1])
            return original_pre(self, row, trace)

        LookScript(page, looks)
        with mock.patch.object(PlaywrightPortal, "_read_results_surface", read), \
                mock.patch.object(PlaywrightPortal, "_pre_dispatch", pre_dispatch), \
                mock.patch.object(SurfaceLocator, "count", count), simulated_clock(clock):
            if production:
                try:
                    outcome = ("dispatched", portal.download(handles[0], self.root / f"{uuid.uuid4().hex}.bin"))
                except LayoutChangedError as exc:
                    outcome = ("raised", type(exc).__name__, exc.message, exc.status, exc.exit_code, exc.retryable)
            else:
                result = portal.download_preflight(handles)
                outcome = ("diagnostic", result.rows_passed, None if result.failure is None else result.failure.row_ordinal)
        log = {name: list(getattr(page, name)) for name in self.RECORDERS}
        log["ledger"] = list(clock.ledger)
        log["row_download_count_reads"] = row_count_reads
        log["latched"] = portal._latched
        log["outcome"] = outcome
        return log

    EQUIVALENCE_SCENARIOS = {
        "all pass": {},
        "row zero persistent": {0: _surface_mutation(download_buttons={2: 0})},
        "row >1 persistent": {0: _surface_mutation(download_buttons={1: 2})},
        "moving witness": {0: _surface_mutation(download_buttons={3: 0}), 2: _surface_mutation(download_buttons={2: 2})},
        "clears before deadline": {0: _surface_mutation(download_buttons={3: 0}), 3: _surface_mutation(download_buttons={})},
        "stray global": {0: _surface_mutation(stray_download_buttons=1)},
        "snapshot": {0: lambda state: state.rows.reverse()},
        "extra row": {0: lambda state: (state.rows.append("r5"), setattr(state, "download_buttons", {4: 0}))},
        "mixed ladder": {0: _surface_mutation(tables=0), 2: _surface_mutation(tables=1, download_buttons={2: 0})},
    }

    def test_the_enrichment_changes_no_playwright_operation(self) -> None:
        for production in (False, True):
            for label, looks in self.EQUIVALENCE_SCENARIOS.items():
                with self.subTest(path="download" if production else "diagnostic", case=label):
                    baseline = self.operation_log(looks, traced=False, production=production)
                    enriched = self.operation_log(looks, traced=True, production=production)
                    self.assertEqual(enriched, baseline)
                    self.assertEqual(enriched["clicks"], ["row-download"] if enriched["outcome"][0] == "dispatched" else [])
                    if not production:
                        self.assertEqual(enriched["download_dispatches"], [])
                        self.assertEqual(enriched["expect_download_timeouts"], [])
                        self.assertTrue(set(enriched["trial_clicks"]) <= {"row-download"})
        handle_case = self.operation_log({}, traced=True, before_row=(1, {0: _surface_mutation(download_buttons={3: 0})}))
        self.assertEqual(
            handle_case,
            self.operation_log({}, traced=False, before_row=(1, {0: _surface_mutation(download_buttons={3: 0})})),
        )

    def test_each_row_download_count_is_read_exactly_once_per_look(self) -> None:
        log = self.operation_log({0: _surface_mutation(download_buttons={3: 0})}, traced=True)
        # Each look reads the header's count (index 0) and then rows 1..4 once
        # each, stopping at the offending row 4, for all seven looks.
        self.assertEqual(log["row_download_count_reads"], [0, 1, 2, 3, 4] * LADDER_LOOKS)
        log = self.operation_log({}, traced=True)
        self.assertEqual(log["row_download_count_reads"], [0, 1, 2, 3, 4] * 4, "one healthy look per handle")

    def test_the_source_keeps_one_download_count_read_per_row_and_the_check_order(self) -> None:
        source = pathlib_read_portal_source()
        read = source[source.index("    def _read_results_surface("):source.index("    def _await_settled_results(")]
        self.assertEqual(read.count(".count()"), 7, "pagination sits outside; table, rows, 2 header, 2 per row, 1 global")
        self.assertEqual(read.count('get_by_role("button", name=DOWNLOAD_BUTTON_NAME, exact=True).count()'), 3)
        order = [
            "self._pagination_sentinel_present(page)",
            'lag("RESULTS_TABLE_ABSENT"',
            'lag("RESULTS_ROWS_ABSENT")',
            'lag("RESULTS_HEADER_NO_COLUMNHEADER")',
            'lag("RESULTS_HEADER_HAS_DOWNLOAD")',
            'lag("RESULTS_HEADER_ONLY")',
            'hard("RESULTS_CEILING_EXCEEDED")',
            'lag("RESULTS_ROW_HEADER_CELL")',
            "download_count = int(",
            "trace.row_download_count(",
            'lag("RESULTS_ROW_DOWNLOAD_COUNT")',
            'lag("RESULTS_DOWNLOAD_COUNT_MISMATCH")',
            'lag("RESULTS_ROWCOUNT_UNREAD")',
            'lag("RESULTS_ROWCOUNT_MISMATCH")',
            'lag("RESULTS_ROW_SNAPSHOT_NOT_READY")',
            'lag("RESULTS_ROW_SNAPSHOT_DUPLICATE")',
        ]
        positions = [read.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        reprove = source[source.index("    def _reprove_frozen_surface("):source.index("    # ---- diagnostic-owned route helpers")]
        self.assertLess(reprove.index("self._read_results_surface("), reprove.index('record.hard("SNAPSHOT_MISMATCH")'))
        self.assertLess(reprove.index('record.hard("SNAPSHOT_MISMATCH")'), reprove.index('record.lag("CONTROL_COUNT_MISMATCH")'))

    def test_the_36_reasons_and_their_order_are_unchanged(self) -> None:
        self.assertEqual(
            portal_module.DOWNLOAD_PREFLIGHT_REASONS,
            (
                "PORTAL_LATCHED", "ROW_HANDLE_INVALID", "TOPOLOGY_NOT_UNIQUE", "TAB_UNSELECTED", "TAB_AMBIGUOUS",
                "TAB_ABSENT", "TAB_CONTRADICTORY", "WITNESS_MULTIPLE", "WITNESS_READ_RACE", "WITNESS_ABSENT",
                "RESULTS_PAGINATION_PRESENT", "RESULTS_CEILING_EXCEEDED", "RESULTS_ROWCOUNT_INVALID",
                "RESULTS_ROW_SNAPSHOT_INVALID", "RESULTS_TABLE_ABSENT", "RESULTS_TABLE_AMBIGUOUS",
                "RESULTS_ROWS_ABSENT", "RESULTS_HEADER_NO_COLUMNHEADER", "RESULTS_HEADER_HAS_DOWNLOAD",
                "RESULTS_HEADER_ONLY", "RESULTS_ROW_HEADER_CELL", "RESULTS_ROW_DOWNLOAD_COUNT",
                "RESULTS_DOWNLOAD_COUNT_MISMATCH", "RESULTS_ROWCOUNT_UNREAD", "RESULTS_ROWCOUNT_MISMATCH",
                "RESULTS_ROW_SNAPSHOT_NOT_READY", "RESULTS_ROW_SNAPSHOT_DUPLICATE", "SNAPSHOT_MISMATCH",
                "CONTROL_COUNT_MISMATCH", "CONTROL_NOT_VISIBLE", "CONTROL_NOT_ENABLED", "CONTROL_NOT_ACTIONABLE",
                "ROW_RECHECK_NOT_READY", "ROW_RECHECK_MISMATCH", "ROW_RECHECK_INVALID", "PROBE_ERROR",
            ),
        )

    def test_only_row_download_count_ever_carries_a_surface_scan(self) -> None:
        for reason, mutate in PREFLIGHT_REASON_SCENARIOS.items():
            with self.subTest(reason=reason):
                portal, page, clock, handles = self.prepared(rows=("r1", "r2", "r3"))
                mutate(page.state)
                with simulated_clock(clock):
                    failure = portal.download_preflight(handles).failure
                self.assertEqual(failure.reason_code, reason)
                self.assertEqual(failure.surface_scan is not None, reason == "RESULTS_ROW_DOWNLOAD_COUNT")
                self.assertTrue(cli._valid_preflight_document(cli.download_preflight_document(3, portal_module.DownloadPreflightDiagnosticResult(rows_passed=0, failure=failure))))

    # ---- privacy and the frozen invoice_failure boundary ---- #

    def test_the_trace_retains_only_closed_integers_booleans_and_buckets(self) -> None:
        portal, page, clock = surface_portal(SurfaceState(rows=[PRIVATE_ROW_TEXT, "r2", "r3", "r4"]))
        with simulated_clock(clock):
            handles = portal.inventory(20)
        traces: list = []
        original = PlaywrightPortal._pre_dispatch

        def pre_dispatch(self, row, trace):
            traces.append(trace)
            return original(self, row, trace)

        page.state.download_buttons = {0: 0}
        with mock.patch.object(PlaywrightPortal, "_pre_dispatch", pre_dispatch), simulated_clock(clock):
            result = portal.download_preflight(handles)
        trace = traces[0]
        for name in ("_row_count_pending", "_row_count_tail", "_row_count_tail_looks", "_row_count_first",
                     "_row_count_changed", "_row_count_equal_frozen"):
            value = getattr(trace, name)
            flat = value if isinstance(value, tuple) else (value,)
            for item in flat:
                with self.subTest(field=name, item=repr(item)):
                    self.assertTrue(item is None or type(item) in (int, bool) or item == ">1")
        public = json.dumps(result.failure.as_public_dict())
        for fragment in (PRIVATE_ROW_TEXT, "ACCT-778899", ResultsConfig.account_identity):
            self.assertNotIn(fragment, public + repr(result.failure) + repr(vars(trace)))
        for digest in portal._frozen_rows:
            self.assertNotIn(digest.hex(), public)
            self.assertNotIn(repr(digest), repr(vars(trace)))

    def test_invoice_failure_enrichment_is_unchanged_by_surface_scan(self) -> None:
        from energygrid_bill_downloader import reconcile

        portal, page, clock, handles = self.prepared()
        page.state.download_buttons = {3: 0}
        with simulated_clock(clock), self.assertRaises(portal_module.DownloadPreflightError) as caught:
            portal.download(handles[0], self.root / "never.bin")
        error = caught.exception
        self.assertEqual(error.message, portal_module.RESULTS_SURFACE_CHANGED_MESSAGE)
        self.assertEqual((error.status, error.exit_code, error.retryable), (PORTAL_LAYOUT_CHANGED, 20, False))
        self.assertIsNotNone(error.evidence.surface_scan, "production carries it privately on the evidence")
        self.assertEqual(
            reconcile._failure_enrichment(error, 0),
            {"row_ordinal": 0, "preflight_reason": "RESULTS_ROW_DOWNLOAD_COUNT", "preflight_checkpoint": "RESULTS_SURFACE"},
        )
        self.assertNotIn("surface_scan", reconcile.__dict__.get("_failure_enrichment").__code__.co_names)
        self.assertEqual(page.download_dispatches, [])
        self.assertTrue(portal._latched)


class SingleSurfaceSourceIsolationTests(unittest.TestCase):
    """The production section depends on none of the retired contracts."""

    def production_source(self) -> str:
        source = pathlib_read_portal_source()
        start = source.index("    # ---- inventory ---- #")
        end = source.index("    # ---- diagnostic-owned route helpers ---- #")
        return source[start:end]

    def test_no_testid_filename_attribute_href_or_selector_dependency(self) -> None:
        body = self.production_source()
        for forbidden in (
            "get_by_test_id",
            "data-filename",
            '"href"',
            "select_option",
            "option",
            "get_by_label",
            "page.url",
            ".first",
            "goto",
            "reload",
            "EMS_ENTRY_NAV_NAME",
            "BILLING_MANAGER_NAV_NAME",
            "EB_BILL_NAV_NAME",
            "_eb_bill_locator",
            "_route_path",
            "_navigation_",
            "text_content",
            "inner_text",
            "inner_html",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_the_production_path_never_calls_the_diagnostic(self) -> None:
        body = self.production_source()
        self.assertNotIn("navigation_diagnostic", body)
        self.assertNotIn("login_diagnostic", body)
        self.assertNotIn("self.login(", body)

    def test_the_diagnostic_still_owns_its_route_helpers(self) -> None:
        source = pathlib_read_portal_source()
        helpers = source[source.index("    # ---- diagnostic-owned route helpers ---- #"):]
        self.assertIn('return page.get_by_role("link", name=EB_BILL_NAV_NAME, exact=True)', helpers)
        self.assertIn('return path.rstrip("/") or "/"', helpers)
        diagnostic = source[source.index("def navigation_diagnostic") : source.index("    # ---- inventory ---- #")]
        self.assertIn("self._eb_bill_locator(page)", diagnostic)

    def test_every_production_reference_is_reachable_and_mapped(self) -> None:
        """The declared production navigation vocabulary equals what inventory raises."""
        reached: set[str] = set()
        scenarios = (
            SurfaceState(eb_tab_role="link"),
            SurfaceState(tab_click_error=RuntimeError("x")),
            SurfaceState(tab_click_selects=False),
            SurfaceState(witnesses=[]),
            SurfaceState(witnesses=[True, True]),
            SurfaceState(search_absent_looks=10_000),
            SurfaceState(search_click_error=RuntimeError("x")),
            SurfaceState(extra_pages=1),
            SurfaceState(tables=0),
            SurfaceState(rows=[]),
            SurfaceState(pagination={("button", "Next")}),
            SurfaceState(aria_rowcount="9"),
            SurfaceState(rows=["dup", "dup"]),
            SurfaceState(rows=["a", "b", "c", "d"]),
        )
        for state in scenarios:
            portal, _page, clock = surface_portal(state)
            with simulated_clock(clock), self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(3)
            self.assertIn(caught.exception.message, cli.SUPPORT_REFS_BY_MESSAGE)
            reached.add(cli.support_ref_for(caught.exception))
        portal, _page, clock = surface_portal()
        with simulated_clock(clock):
            portal.inventory(20)
            with self.assertRaises(LayoutChangedError) as caught:
                portal.inventory(20)
        reached.add(cli.support_ref_for(caught.exception))
        self.assertEqual(reached, set(cli.NAVIGATION_SUPPORT_REFS))
        self.assertTrue(reached.isdisjoint(cli.RETIRED_SUPPORT_REFS))
        self.assertTrue(all(not ref.startswith("EG_LOGIN_") for ref in reached))
        self.assertTrue(reached.isdisjoint(cli.NAVIGATION_DIAGNOSTIC_ALLOWED_SUPPORT_REFS))


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
            "_select_eb_bill_tab",
            "_eb_bill_tab_state",
            "_await_account_witness",
            "_account_witness_count",
            "_dispatch_search",
            "_await_settled_results",
            "_read_results_surface",
            "_reprove_frozen_surface",
            "_row_identity",
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
                "_select_eb_bill_tab",
                "_dispatch_search",
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


# ---- DL-XB-204-NAVIGATION-DIAGNOSTIC-G3-003: bounded post-EMS observation ---- #


class NavigationDiagnosticState:
    def __init__(
        self,
        *,
        matches: int = 0,
        visible: bool = False,
        enabled: bool = False,
        actionable: bool = False,
        href: str | None = None,
        count_error: Exception | None = None,
        visible_error: Exception | None = None,
        enabled_error: Exception | None = None,
        trial_error: Exception | None = None,
        click_error: Exception | None = None,
    ) -> None:
        self.matches = matches
        self.visible = visible
        self.enabled = enabled
        self.actionable = actionable
        self.href = href
        self.count_error = count_error
        self.visible_error = visible_error
        self.enabled_error = enabled_error
        self.trial_error = trial_error
        self.click_error = click_error


def navigation_absent(**kwargs) -> NavigationDiagnosticState:
    return NavigationDiagnosticState(**kwargs)


def navigation_ready(*, href: str | None = None, **kwargs) -> NavigationDiagnosticState:
    return NavigationDiagnosticState(
        matches=1,
        visible=True,
        enabled=True,
        actionable=True,
        href=href,
        **kwargs,
    )


class NavigationDiagnosticLocator:
    def __init__(
        self,
        page: "NavigationDiagnosticPage",
        key: str,
        state: NavigationDiagnosticState,
    ) -> None:
        self.page = page
        self.key = key
        self.state = state

    def count(self) -> int:
        if self.state.count_error is not None:
            raise self.state.count_error
        matches = self.state.matches
        if (
            self.key == "link:EB Bill"
            and (self.page.route_count_change or self.page.route_count_elapsed_ms)
            and not self.page._route_count_changed
        ):
            self.page._route_count_changed = True
            if self.page.route_count_elapsed_ms:
                self.page.clock.charge_yield(self.page.route_count_elapsed_ms)
            if self.page.route_count_change:
                self.page.apply_topology_change(self.page.route_count_change)
        return matches

    def is_visible(self, timeout: int | None = None) -> bool:
        if self.state.visible_error is not None:
            raise self.state.visible_error
        if self.key == "button:EMS" and self.page.ems_readiness_change:
            self.page.apply_topology_change(self.page.ems_readiness_change)
        return self.state.visible

    def is_enabled(self, timeout: int | None = None) -> bool:
        self.page.probe_timeouts.append(timeout)
        if self.state.enabled_error is not None:
            raise self.state.enabled_error
        if self.page.ems_dispatches and self.page.post_probe_change:
            self.page.apply_topology_change(self.page.post_probe_change)
        return self.state.enabled

    def click(self, trial: bool = False, timeout: int | None = None) -> None:
        if trial:
            self.page.trial_clicks[self.key] = self.page.trial_clicks.get(self.key, 0) + 1
            self.page.probe_timeouts.append(timeout)
            if self.state.trial_error is not None:
                raise self.state.trial_error
            if not self.state.actionable:
                self.page.clock.charge_probe(timeout)
                raise synthetic_timeout()
            if self.key == "button:EMS" and self.page.ems_boundary_change:
                self.page.apply_topology_change(self.page.ems_boundary_change)
            return
        self.page.normal_clicks[self.key] = self.page.normal_clicks.get(self.key, 0) + 1
        if self.key == "button:EMS":
            self.page.ems_dispatches += 1
            if self.state.click_error is not None:
                raise self.state.click_error
            if not self.page.ems_inert:
                self.page._url = self.page.post_url
            return
        raise AssertionError("the navigation diagnostic dispatched a downstream control")

    def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        if name != "href":
            raise AssertionError(name)
        self.page.probe_timeouts.append(timeout)
        self.page.attribute_timeouts.append(timeout)
        href = self.state.href
        if (
            self.key == "link:EB Bill"
            and (
                self.page.route_attribute_change
                or self.page.route_attribute_elapsed_ms
            )
            and not self.page._route_attribute_changed
        ):
            self.page._route_attribute_changed = True
            if self.page.route_attribute_elapsed_ms:
                self.page.clock.charge_yield(self.page.route_attribute_elapsed_ms)
            if self.page.route_attribute_change:
                self.page.apply_topology_change(self.page.route_attribute_change)
        self.page.get_attribute_calls += 1
        return href


class NavigationDiagnosticContext:
    def __init__(self, page: "NavigationDiagnosticPage") -> None:
        self.page = page

    @property
    def pages(self) -> list[object]:
        return self.page.context_pages()


class NavigationDiagnosticPage:
    def __init__(
        self,
        *,
        clock: RecoveryClock,
        post_controls: dict[str, list[NavigationDiagnosticState] | NavigationDiagnosticState]
        | None = None,
        ems_state: NavigationDiagnosticState | None = None,
        post_url: str = "http://synthetic.invalid/app",
        pre_context_pages: int = 1,
        post_context_pages: int = 1,
        pre_frames: int = 1,
        post_frames: int = 1,
        ems_inert: bool = False,
        ems_readiness_change: str | None = None,
        ems_boundary_change: str | None = None,
        post_checkpoint_change: str | None = None,
        post_probe_change: str | None = None,
        route_count_change: str | None = None,
        route_count_elapsed_ms: int = 0,
        route_attribute_change: str | None = None,
        route_attribute_elapsed_ms: int = 0,
    ) -> None:
        self.clock = clock
        self.ems_state = ems_state or navigation_ready()
        self.post_controls = post_controls or {}
        self.post_url = post_url
        self.pre_context_pages = pre_context_pages
        self.post_context_pages = post_context_pages
        self.pre_frames = pre_frames
        self.post_frames = post_frames
        self.ems_inert = ems_inert
        self.ems_readiness_change = ems_readiness_change
        self.ems_boundary_change = ems_boundary_change
        self.post_checkpoint_change = post_checkpoint_change
        self.post_probe_change = post_probe_change
        self.route_count_change = route_count_change
        self.route_count_elapsed_ms = route_count_elapsed_ms
        self.route_attribute_change = route_attribute_change
        self.route_attribute_elapsed_ms = route_attribute_elapsed_ms
        self._topology_change: str | None = None
        self._post_checkpoint_changed = False
        self._post_probe_changed = False
        self._route_count_changed = False
        self._route_attribute_changed = False
        self.ems_dispatches = 0
        self.normal_clicks: dict[str, int] = {}
        self.trial_clicks: dict[str, int] = {}
        self.probe_timeouts: list[int | None] = []
        self.attribute_timeouts: list[int | None] = []
        self.get_attribute_calls = 0
        self.waits: list[int] = []
        self.role_lookups: list[tuple[str, str | None, bool]] = []
        self.goto_calls = 0
        self._url = "http://synthetic.invalid/landing"
        self._look_indices: dict[str, int] = {}

    def context_pages(self) -> list[object]:
        count = self.post_context_pages if self.ems_dispatches else self.pre_context_pages
        if self._topology_change == "page":
            count = 2
        return [self] + [object() for _ in range(max(0, count - 1))]

    @property
    def frames(self) -> list[object]:
        count = self.post_frames if self.ems_dispatches else self.pre_frames
        if self._topology_change == "frame":
            count = 2
        return [object() for _ in range(max(0, count))]

    @property
    def url(self) -> str:
        return self._url

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)
        self.clock.charge_yield(milliseconds)
        if (
            self.ems_dispatches
            and self.post_checkpoint_change
            and not self._post_checkpoint_changed
        ):
            self._post_checkpoint_changed = True
            self.apply_topology_change(self.post_checkpoint_change)

    def apply_topology_change(self, change: str) -> None:
        if change in {"page", "frame"}:
            self._topology_change = change
            return
        if change == "origin":
            self._url = "https://other.invalid/changed"
            return
        raise AssertionError(change)

    def goto(self, *_args, **_kwargs) -> None:
        self.goto_calls += 1
        raise AssertionError("navigation diagnostic must not reload or navigate")

    def _state_for(self, key: str) -> NavigationDiagnosticState:
        if key == "button:EMS" and not self.ems_dispatches:
            return self.ems_state
        configured = self.post_controls.get(
            {
                "button:EMS": "button_ems",
                "link:EMS": "link_ems",
                "link:Billing Manager": "link_billing_manager",
                "button:Billing Manager": "button_billing_manager",
                "link:EB Bill": "link_eb_bill",
                "button:EB Bill": "button_eb_bill",
            }[key]
        )
        if configured is None:
            return NavigationDiagnosticState()
        if isinstance(configured, list):
            index = self._look_indices.get(key, 0)
            self._look_indices[key] = index + 1
            return configured[min(index, len(configured) - 1)]
        return configured

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        self.role_lookups.append((role, name, exact))
        if (role, name) not in {
            ("button", "EMS"),
            ("link", "EMS"),
            ("link", "Billing Manager"),
            ("button", "Billing Manager"),
            ("link", "EB Bill"),
            ("button", "EB Bill"),
        }:
            raise AssertionError(f"unexpected role lookup: {role}/{name}")
        return NavigationDiagnosticLocator(self, f"{role}:{name}", self._state_for(f"{role}:{name}"))

    def get_by_text(self, *_args, **_kwargs):
        raise AssertionError("generic text selectors are forbidden")


def navigation_portal(**kwargs):
    clock = RecoveryClock()
    page = NavigationDiagnosticPage(clock=clock, **kwargs)
    portal = PlaywrightPortal(ResultsConfig(), headed=True)
    portal.page = page
    portal.context = NavigationDiagnosticContext(page)
    portal.login = mock.Mock()
    return portal, page, clock


class NavigationDiagnosticStateMachineTests(unittest.TestCase):
    def run_navigation(self, **kwargs):
        portal, page, clock = navigation_portal(**kwargs)
        with simulated_clock(clock):
            result = portal.navigation_diagnostic()
        return result, portal, page, clock

    def assert_no_downstream_dispatch(self, page: NavigationDiagnosticPage) -> None:
        self.assertEqual(page.ems_dispatches, 1)
        self.assertEqual(
            page.normal_clicks,
            {"button:EMS": 1},
            "only the single EMS entry dispatch is permitted",
        )
        self.assertEqual(page.goto_calls, 0)

    def assert_complete_navigation_document(
        self, result, expected_result: str
    ) -> None:
        self.assertEqual(result.result, expected_result)
        self.assertEqual(result.status, portal_module.NAVIGATION_DIAGNOSTIC_COMPLETE_STATE)
        document = cli.navigation_diagnostic_document(result)
        self.assertEqual(document["schema"], cli.NAVIGATION_DIAGNOSTIC_SCHEMA)
        self.assertNotIn("support_ref", document)

    def assert_ready_ems_witness(self, result, key: str, role: str) -> None:
        self.assertEqual(
            result.post_ems["controls"][key],
            {
                "role": role,
                "name": "EMS",
                "count": 1,
                "visible": True,
                "enabled": True,
                "trial_actionable": True,
            },
        )

    def test_ready_button_ems_is_retained_and_does_not_terminate(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            post_controls={"button_ems": navigation_ready()}
        )
        self.assert_complete_navigation_document(
            result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED
        )
        self.assert_no_downstream_dispatch(page)
        self.assert_ready_ems_witness(result, "button_ems", "button")

    def test_ready_link_ems_is_retained_and_does_not_terminate(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            post_controls={"link_ems": navigation_ready()}
        )
        self.assert_complete_navigation_document(
            result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED
        )
        self.assert_no_downstream_dispatch(page)
        self.assert_ready_ems_witness(result, "link_ems", "link")

    def test_inert_ems_consumes_one_dispatch_and_exhausts_the_window(self) -> None:
        result, portal, page, clock = self.run_navigation(ems_inert=True)
        self.assertEqual(result.result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED)
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(page.ems_dispatches, 1)
        self.assert_no_downstream_dispatch(page)
        portal.login.assert_called_once_with()
        self.assertEqual(clock.elapsed_ms(), 45000)
        self.assertTrue(page.waits)
        self.assertLessEqual(max(page.waits), portal_module.MAX_PORTAL_PROBE_TIMEOUT_MS)

    def test_ems_witness_and_delayed_billing_manager_link_are_observed_without_clicking(self) -> None:
        delayed = [navigation_absent(), navigation_ready(href="/billing")]
        result, _portal, page, _clock = self.run_navigation(
            post_controls={
                "button_ems": navigation_ready(),
                "link_billing_manager": delayed,
            }
        )
        self.assert_complete_navigation_document(
            result,
            portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_LINK_READY,
        )
        self.assert_no_downstream_dispatch(page)
        self.assert_ready_ems_witness(result, "button_ems", "button")
        self.assertGreaterEqual(page.trial_clicks.get("link:Billing Manager", 0), 1)

    def test_billing_manager_absent_duplicate_hidden_disabled_and_non_actionable_fail_closed(self) -> None:
        cases = {
            "absent": navigation_absent(),
            "duplicate": navigation_absent(matches=2),
            "hidden": navigation_absent(matches=1),
            "disabled": navigation_absent(matches=1, visible=True, enabled=False),
            "non_actionable": navigation_absent(matches=1, visible=True, enabled=True),
        }
        for name, state in cases.items():
            with self.subTest(case=name):
                result, _portal, page, _clock = self.run_navigation(
                    post_controls={"link_billing_manager": state}
                )
                self.assertEqual(
                    result.result,
                    portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED,
                )
                self.assertEqual(page.normal_clicks, {"button:EMS": 1})

    def test_ems_witness_and_billing_manager_button_evidence_are_observational_only(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            post_controls={
                "button_ems": navigation_ready(),
                "button_billing_manager": [navigation_absent(), navigation_ready()],
            }
        )
        self.assert_complete_navigation_document(
            result,
            portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_BUTTON_READY,
        )
        self.assert_no_downstream_dispatch(page)
        self.assert_ready_ems_witness(result, "button_ems", "button")

    def test_ems_witness_and_delayed_eb_bill_button_evidence_are_observational_only(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            post_controls={
                "button_ems": navigation_ready(),
                "button_eb_bill": [navigation_absent(), navigation_ready()],
            }
        )
        self.assert_complete_navigation_document(
            result,
            portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_BUTTON_READY,
        )
        self.assert_no_downstream_dispatch(page)
        self.assert_ready_ems_witness(result, "button_ems", "button")

    def test_direct_route_and_direct_link_have_their_distinct_positive_results(self) -> None:
        route, _portal, route_page, _clock = self.run_navigation(
            post_url="http://synthetic.invalid/eb-bill",
            post_controls={
                "button_ems": navigation_ready(),
                "link_eb_bill": navigation_ready(href="/eb-bill"),
            },
        )
        self.assert_complete_navigation_document(
            route,
            portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_ROUTE_PROVEN,
        )
        self.assert_no_downstream_dispatch(route_page)

        direct, _portal, direct_page, _clock = self.run_navigation(
            post_controls={
                "button_ems": navigation_ready(),
                # Route proof consumes one lookup before the control
                # observation. The second lookup is the delayed ready witness.
                "link_eb_bill": [
                    navigation_absent(),
                    navigation_absent(),
                    navigation_absent(),
                    navigation_ready(href="/eb-bill"),
                ],
            }
        )
        self.assert_complete_navigation_document(
            direct,
            portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_LINK_READY,
        )
        self.assert_no_downstream_dispatch(direct_page)
        self.assert_ready_ems_witness(direct, "button_ems", "button")

    def test_ems_witness_and_later_eb_bill_route_proof_are_observed(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            post_url="http://synthetic.invalid/eb-bill",
            post_controls={
                "button_ems": navigation_ready(),
                # Each checkpoint consumes one route-count lookup. A proven
                # route then consumes one additional locator lookup for href.
                "link_eb_bill": [
                    navigation_absent(),
                    navigation_absent(),
                    navigation_absent(),
                    navigation_absent(),
                    navigation_ready(href="/eb-bill"),
                    navigation_ready(href="/eb-bill"),
                ],
            }
        )
        self.assert_complete_navigation_document(
            result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_ROUTE_PROVEN
        )
        self.assert_no_downstream_dispatch(page)
        self.assert_ready_ems_witness(result, "button_ems", "button")

    def test_ems_witness_and_downstream_reader_failure_remain_unreadable(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            post_controls={
                "button_ems": navigation_ready(),
                "link_billing_manager": navigation_absent(
                    count_error=RuntimeError("private-reader-failure token=tok_live_abcd")
                ),
            }
        )
        self.assertEqual(
            result.result,
            portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE,
        )
        self.assertEqual(result.status, portal_module.NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE)
        self.assert_no_downstream_dispatch(page)
        self.assertEqual(result.post_ems, portal_module.unobserved_navigation_post_ems())
        document = cli.navigation_diagnostic_document(result)
        self.assertEqual(document["schema"], cli.NAVIGATION_DIAGNOSTIC_SCHEMA)
        self.assertEqual(document["support_ref"], "EG_NAV_DIAGNOSTIC_OBSERVATION_UNREADABLE")

    def test_multiple_pages_or_frames_terminate_without_surface_switching(self) -> None:
        pre_pages, _portal, page, _clock = self.run_navigation(pre_context_pages=2)
        self.assertEqual(pre_pages.result, portal_module.NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_PAGES)
        self.assertEqual(page.ems_dispatches, 0)
        self.assertEqual(page.normal_clicks, {})

        post_frames, _portal, page, _clock = self.run_navigation(post_frames=2)
        self.assertEqual(post_frames.result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_FRAMES)
        self.assertEqual(page.normal_clicks, {"button:EMS": 1})
        self.assertEqual(page.goto_calls, 0)

    def test_cross_origin_terminates_before_controls_are_read(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            post_url="https://other.invalid/landing?token=private"
        )
        self.assertEqual(result.result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_CROSS_ORIGIN)
        self.assertEqual(page.normal_clicks, {"button:EMS": 1})
        self.assertEqual(
            [lookup for lookup in page.role_lookups if lookup[0] != "button" or lookup[1] != "EMS"],
            [],
        )

    def test_ems_exception_is_uncertain_and_terminal_without_retry_or_relogin(self) -> None:
        result, portal, page, _clock = self.run_navigation(
            ems_state=navigation_ready(click_error=RuntimeError("private click failure"))
        )
        self.assertEqual(result.result, portal_module.NAVIGATION_DIAGNOSTIC_EMS_DISPATCH_UNCERTAIN)
        self.assertEqual(result.status, "ACTION_REQUIRED")
        self.assertEqual(page.ems_dispatches, 1)
        self.assertEqual(page.normal_clicks, {"button:EMS": 1})
        portal.login.assert_called_once_with()
        self.assertEqual(page.waits, [])

    def test_exact_selectors_and_no_alternate_opener_are_used(self) -> None:
        result, _portal, page, _clock = self.run_navigation()
        self.assertEqual(result.result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED)
        self.assertTrue(page.role_lookups)
        self.assertTrue(all(exact for _role, _name, exact in page.role_lookups))
        self.assertTrue(all(name in {"EMS", "Billing Manager", "EB Bill"} for _role, name, _exact in page.role_lookups))
        source = pathlib_read_portal_source()
        body = source[source.index("def navigation_diagnostic") : source.index("# ---- inventory ----")]
        for forbidden in ("_open_verified_results", "_open_eb_bill_route", "_dispatch_billing_manager", "_dispatch_eb_bill", "_select_eb_bill_tab", "_dispatch_search", "_await_settled_results", "_reprove_frozen_surface", "inventory(", "download(", ".first", "get_by_text", "reload"):
            self.assertNotIn(forbidden, body)


class NavigationDiagnosticRepair1StateMachineTests(unittest.TestCase):
    def run_navigation(self, **kwargs):
        portal, page, clock = navigation_portal(**kwargs)
        with simulated_clock(clock):
            result = portal.navigation_diagnostic()
        return result, portal, page, clock

    def test_second_page_during_ems_readiness_prevents_dispatch(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            ems_readiness_change="page"
        )
        self.assertEqual(result.result, portal_module.NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_PAGES)
        self.assertEqual(page.ems_dispatches, 0)
        self.assertEqual(page.normal_clicks, {})

    def test_extra_frame_during_ems_readiness_prevents_dispatch(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            ems_readiness_change="frame"
        )
        self.assertEqual(result.result, portal_module.NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_FRAMES)
        self.assertEqual(page.ems_dispatches, 0)
        self.assertEqual(page.normal_clicks, {})

    def test_topology_change_at_ems_action_boundary_prevents_dispatch(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            ems_boundary_change="page"
        )
        self.assertEqual(result.result, portal_module.NAVIGATION_DIAGNOSTIC_PRE_EMS_MULTIPLE_PAGES)
        self.assertEqual(page.ems_dispatches, 0)
        self.assertEqual(page.normal_clicks, {})

    def test_origin_change_at_ems_action_boundary_prevents_dispatch(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            ems_boundary_change="origin"
        )
        self.assertEqual(result.result, portal_module.NAVIGATION_DIAGNOSTIC_EMS_NOT_READY)
        self.assertEqual(page.ems_dispatches, 0)
        self.assertEqual(page.normal_clicks, {})

    def test_change_between_post_checkpoints_stops_later_probes(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            post_checkpoint_change="origin"
        )
        self.assertEqual(result.result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_CROSS_ORIGIN)
        self.assertEqual(page.normal_clicks, {"button:EMS": 1})
        self.assertEqual(page.waits, [250])

    def test_change_during_waiting_probe_invalidates_positive_evidence(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            post_controls={"link_billing_manager": navigation_ready()},
            post_probe_change="page",
        )
        self.assertEqual(result.result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_PAGES)
        self.assertEqual(page.normal_clicks, {"button:EMS": 1})
        self.assertEqual(page.trial_clicks.get("link:Billing Manager", 0), 0)

    def test_one_shared_deadline_clips_probes_and_never_starts_after_exhaustion(self) -> None:
        result, _portal, page, clock = self.run_navigation(
            post_controls={
                "link_billing_manager": navigation_absent(
                    matches=1, visible=True, enabled=True, actionable=False
                )
            }
        )
        self.assertEqual(result.result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED)
        self.assertEqual(page.normal_clicks, {"button:EMS": 1})
        self.assertEqual(clock.elapsed_ms(), 46_000)
        self.assertLess(clock.elapsed_ms(), 60_000)
        self.assertTrue(page.probe_timeouts)
        self.assertLessEqual(
            max(timeout for timeout in page.probe_timeouts if timeout is not None),
            1000,
        )


class NavigationDiagnosticRepair2RouteProofTests(unittest.TestCase):
    def run_navigation(self, **kwargs):
        portal, page, clock = navigation_portal(**kwargs)
        with simulated_clock(clock):
            result = portal.navigation_diagnostic()
        return result, portal, page, clock

    def test_route_count_terminal_failures_never_read_attribute_or_controls(self) -> None:
        cases = {
            "page": (
                portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_PAGES,
                {"route_count_change": "page"},
            ),
            "frame": (
                portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_FRAMES,
                {"route_count_change": "frame"},
            ),
            "origin": (
                portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_CROSS_ORIGIN,
                {"route_count_change": "origin"},
            ),
            "deadline": (
                portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED,
                {"route_count_elapsed_ms": 60_000},
            ),
        }
        for name, (expected, options) in cases.items():
            with self.subTest(case=name):
                result, _portal, page, _clock = self.run_navigation(
                    post_url="http://synthetic.invalid/eb-bill",
                    post_controls={"link_eb_bill": navigation_ready(href="/eb-bill")},
                    **options,
                )
                self.assertEqual(result.result, expected)
                self.assertEqual(page.get_attribute_calls, 0)
                self.assertEqual(page.attribute_timeouts, [])
                self.assertEqual(page.trial_clicks, {"button:EMS": 1})
                self.assertEqual(page.normal_clicks, {"button:EMS": 1})

    def test_fresh_remaining_budget_clips_route_attribute_timeout(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            post_url="http://synthetic.invalid/eb-bill",
            post_controls={"link_eb_bill": navigation_ready(href="/eb-bill")},
            route_count_elapsed_ms=59_500,
        )
        self.assertEqual(
            result.result,
            portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_ROUTE_PROVEN,
        )
        self.assertEqual(page.get_attribute_calls, 1)
        self.assertEqual(page.attribute_timeouts, [500])
        self.assertEqual(page.trial_clicks, {"button:EMS": 1})

    def test_route_attribute_terminal_failures_invalidate_positive_evidence(self) -> None:
        cases = {
            "page": (
                portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_PAGES,
                {"route_attribute_change": "page"},
            ),
            "frame": (
                portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_MULTIPLE_FRAMES,
                {"route_attribute_change": "frame"},
            ),
            "origin": (
                portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_CROSS_ORIGIN,
                {"route_attribute_change": "origin"},
            ),
            "deadline": (
                portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_WINDOW_EXHAUSTED,
                {"route_attribute_elapsed_ms": 60_000},
            ),
        }
        for name, (expected, options) in cases.items():
            with self.subTest(case=name):
                result, _portal, page, _clock = self.run_navigation(
                    post_url="http://synthetic.invalid/eb-bill",
                    post_controls={"link_eb_bill": navigation_ready(href="/eb-bill")},
                    **options,
                )
                self.assertEqual(result.result, expected)
                self.assertEqual(page.get_attribute_calls, 1)
                self.assertEqual(page.trial_clicks, {"button:EMS": 1})
                self.assertEqual(page.normal_clicks, {"button:EMS": 1})

    def test_stable_route_proof_remains_positive_after_final_fresh_guard(self) -> None:
        result, _portal, page, _clock = self.run_navigation(
            post_url="http://synthetic.invalid/eb-bill",
            post_controls={"link_eb_bill": navigation_ready(href="/eb-bill")},
        )
        self.assertEqual(
            result.result,
            portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_EB_BILL_ROUTE_PROVEN,
        )
        self.assertEqual(page.get_attribute_calls, 1)
        self.assertEqual(page.attribute_timeouts, [1000])
        self.assertEqual(page.trial_clicks, {"button:EMS": 1})


class NavigationDiagnosticPrivacyTests(unittest.TestCase):
    HOSTILE = "private-account password=hunter2 token=tok_live_abcd https://evil.invalid/q"

    def test_raw_url_query_href_and_exception_text_never_enter_result_evidence(self) -> None:
        portal, _page, _clock = navigation_portal(
            post_url="http://synthetic.invalid/app?customer=" + self.HOSTILE,
            post_controls={
                "link_billing_manager": navigation_absent(
                    count_error=RuntimeError(self.HOSTILE)
                )
            },
        )
        with simulated_clock(_clock):
            observed = portal.navigation_diagnostic()
        document = cli.navigation_diagnostic_document(observed)
        encoded = json.dumps(document, sort_keys=True)
        for fragment in ("private-account", "hunter2", "tok_live_abcd", "evil.invalid", "https://"):
            self.assertNotIn(fragment, encoded)
        self.assertEqual(observed.result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE)

    def test_unreadable_observation_is_generic_and_fail_closed(self) -> None:
        portal, _page, _clock = navigation_portal(
            post_controls={
                "link_eb_bill": navigation_absent(
                    count_error=RuntimeError(self.HOSTILE)
                )
            }
        )
        with simulated_clock(_clock):
            observed = portal.navigation_diagnostic()
        self.assertEqual(observed.result, portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_OBSERVATION_UNREADABLE)
        self.assertIsInstance(observed.failure, LayoutChangedError)
        self.assertNotIn("hunter2", observed.failure.message)

    def test_diagnostic_source_never_reads_or_emits_private_page_text(self) -> None:
        source = pathlib_read_portal_source()
        body = source[source.index("def navigation_diagnostic") : source.index("# ---- inventory ----")]
        for forbidden in ("text_content", "inner_text", "inner_html", "content()", "screenshot", "storage_state", "trace"):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
