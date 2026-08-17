from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
import uuid

from energygrid_bill_downloader.config import load_runtime_config
from energygrid_bill_downloader.cli import main
from energygrid_bill_downloader.errors import DownloadError, InvalidPdfError, LayoutChangedError, LoginError
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

if __name__ == "__main__":
    unittest.main()
