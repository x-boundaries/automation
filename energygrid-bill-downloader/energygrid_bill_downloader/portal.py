from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RuntimeConfig
from .errors import DependencyError, DownloadError, LayoutChangedError, LoginError


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
        try:
            page.goto(self.config.portal_url, wait_until="domcontentloaded")
            page.get_by_role("link", name="Login", exact=True).click()
            page.get_by_label("Username", exact=True).fill(username)
            page.get_by_label("Password", exact=True).fill(password)
            page.get_by_role("button", name="Login", exact=True).click()
            page.get_by_role("link", name="Billing Manager", exact=True).wait_for(state="visible")
        except Exception as exc:
            if self._visible(page, page.get_by_role("alert")):
                raise LoginError("portal rejected the login") from exc
            raise LayoutChangedError("required login control is missing or ambiguous") from exc

    def inventory(self, safety_ceiling: int) -> list[BillRef]:
        page = self._require_page()
        try:
            page.get_by_role("link", name="Billing Manager", exact=True).click()
            page.get_by_role("link", name="EB Bill", exact=True).click()
        except Exception as exc:
            raise LayoutChangedError("Billing Manager or EB Bill navigation changed") from exc

        bills: list[BillRef] = []
        seen_pages: set[str] = set()
        while True:
            list_container = page.get_by_test_id("invoice-list")
            try:
                list_container.wait_for(state="visible")
            except Exception as exc:
                raise LayoutChangedError("invoice list container is missing") from exc
            page_marker = list_container.get_attribute("data-page") or page.url
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
                bills.append(BillRef(filename=filename, page_url=page.url))
                if len(bills) > safety_ceiling:
                    raise LayoutChangedError("inventory safety ceiling exceeded")

            next_button = page.get_by_role("button", name="Next page", exact=True)
            if next_button.count() != 1:
                raise LayoutChangedError("invoice pagination control is missing or ambiguous")
            if next_button.is_disabled():
                return bills
            old_marker = page_marker
            next_button.click()
            try:
                page.wait_for_function(
                    "([selector, old]) => document.querySelector(selector)?.getAttribute('data-page') !== old",
                    arg=["[data-testid='invoice-list']", old_marker],
                )
            except Exception as exc:
                raise LayoutChangedError("invoice pagination did not advance") from exc

    def download(self, bill: BillRef, destination: Path) -> str:
        page = self._require_page()
        try:
            page.goto(bill.page_url, wait_until="domcontentloaded")
            rows = page.get_by_test_id("invoice-row")
            matching_rows = []
            for row in rows.all():
                if row.get_attribute("data-filename") == bill.filename:
                    matching_rows.append(row)
            if len(matching_rows) != 1:
                raise LayoutChangedError("invoice row identity is missing or ambiguous")
            with page.expect_download() as download_info:
                matching_rows[0].get_by_role("button", name="Download", exact=True).click()
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
