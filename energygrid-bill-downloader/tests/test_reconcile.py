from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import uuid

from energygrid_bill_downloader.config import RuntimeConfig
from energygrid_bill_downloader.errors import DownloadError, AppError
from energygrid_bill_downloader.portal import BillRef
from energygrid_bill_downloader.publication import filename_key, validate_pdf
from energygrid_bill_downloader.reconcile import reconcile_inventory
from energygrid_bill_downloader.state import StateStore
from tests.fixtures.synthetic_portal import SyntheticBill, synthetic_pdf


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, dict[str, object]]] = []

    def event(self, phase: str, status: str | None = None, **fields: object) -> None:
        self.events.append((phase, status, fields))


class FakePortal:
    def __init__(self, bills: list[SyntheticBill], failure_mode: str | None = None) -> None:
        self.bills = bills
        self.failure_mode = failure_mode
        self.download_calls = 0

    def inventory(self, safety_ceiling: int) -> list[BillRef]:
        if len(self.bills) > safety_ceiling:
            raise AppError("synthetic ceiling", status="PORTAL_LAYOUT_CHANGED", exit_code=20)
        return [BillRef(bill.filename, "http://synthetic.invalid/eb-bill") for bill in self.bills]

    def download(self, bill: BillRef, destination: Path) -> str:
        self.download_calls += 1
        if self.failure_mode == "retryable":
            raise DownloadError("synthetic network failure")
        if self.failure_mode == "wrong-name":
            destination.write_bytes(synthetic_pdf())
            return "other.pdf"
        matching = next(item for item in self.bills if item.filename == bill.filename)
        destination.write_bytes(matching.payload)
        return matching.filename


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.archive = self.root / "archive"
        self.archive.mkdir()
        self.config = RuntimeConfig(
            portal_url="http://127.0.0.1:1",
            archive_root=self.archive,
            state_path=self.root / "state" / "state.sqlite3",
            temp_root=self.root / "temp",
            log_root=self.root / "logs",
            max_attempts=2,
            inventory_safety_ceiling=10,
        )
        self.config.preflight()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_once(self, portal: FakePortal, *, list_only: bool = False):
        logger = RecordingLogger()
        with StateStore(self.config.state_path) as state:
            summary = reconcile_inventory(
                self.config,
                portal,
                state,
                logger,
                str(uuid.uuid4()),
                list_only=list_only,
            )
        return summary, logger

    def test_new_bill_then_repeated_run_is_idempotent(self) -> None:
        bill = SyntheticBill("2026-05-01_account_ref.pdf")
        first_portal = FakePortal([bill])
        first, _ = self.run_once(first_portal)
        self.assertEqual(first.status, "DOWNLOADED")
        self.assertEqual(first.downloaded_count, 1)
        second_portal = FakePortal([bill])
        second, _ = self.run_once(second_portal)
        self.assertEqual(second.status, "ALREADY_PRESENT")
        self.assertEqual(second.present_count, 1)
        self.assertEqual(second_portal.download_calls, 0)

    def test_existing_file_without_state_is_reconciled(self) -> None:
        bill = SyntheticBill("2026-05-02_account_ref.pdf")
        (self.archive / bill.filename).write_bytes(bill.payload)
        summary, _ = self.run_once(FakePortal([bill]))
        self.assertEqual(summary.status, "ALREADY_PRESENT")
        with StateStore(self.config.state_path) as state:
            record = state.get(filename_key(bill.filename))
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, "PRESENT_RECONCILED")

    def test_missing_final_file_is_repaired_from_archived_state(self) -> None:
        bill = SyntheticBill("2026-05-03_account_ref.pdf")
        final = self.archive / bill.filename
        final.write_bytes(bill.payload)
        with StateStore(self.config.state_path) as state:
            state.mark_seen(filename_key(bill.filename), bill.filename)
            state.record_archived(filename_key(bill.filename), bill.filename, validate_pdf(final))
        final.unlink()
        summary, _ = self.run_once(FakePortal([bill]))
        self.assertEqual(summary.status, "DOWNLOADED")
        self.assertTrue(final.exists())

    def test_conflicting_existing_file_fails_closed(self) -> None:
        bill = SyntheticBill("2026-05-04_account_ref.pdf")
        final = self.archive / bill.filename
        final.write_bytes(bill.payload)
        with StateStore(self.config.state_path) as state:
            state.mark_seen(filename_key(bill.filename), bill.filename)
            state.record_archived(filename_key(bill.filename), bill.filename, validate_pdf(final))
        final.write_bytes(synthetic_pdf(b"different"))
        summary, _ = self.run_once(FakePortal([bill]))
        self.assertEqual(summary.status, "ARCHIVE_CONFLICT")
        self.assertEqual(FakePortal([bill]).download_calls, 0)

    def test_duplicate_and_unsafe_names_fail_closed(self) -> None:
        duplicate = [
            SyntheticBill("Invoice.pdf"),
            SyntheticBill("invoice.PDF"),
        ]
        with self.assertRaises(AppError):
            self.run_once(FakePortal(duplicate))
        with self.assertRaises(AppError):
            self.run_once(FakePortal([SyntheticBill("..\\escape.pdf")]))

    def test_download_failure_is_recorded_without_fake_success(self) -> None:
        bill = SyntheticBill("2026-05-05_account_ref.pdf")
        portal = FakePortal([bill], failure_mode="retryable")
        summary, logger = self.run_once(portal)
        self.assertEqual(summary.status, "DOWNLOAD_FAILED")
        self.assertEqual(summary.exit_code, 10)
        self.assertEqual(portal.download_calls, 2)
        self.assertTrue(any(phase == "download_retry" for phase, _status, _fields in logger.events))
        self.assertFalse((self.archive / bill.filename).exists())

    def test_list_only_does_not_download_and_is_action_required_for_unresolved_bill(self) -> None:
        bill = SyntheticBill("2026-05-06_account_ref.pdf")
        portal = FakePortal([bill])
        summary, _ = self.run_once(portal, list_only=True)
        self.assertEqual(summary.status, "ACTION_REQUIRED")
        self.assertEqual(portal.download_calls, 0)


    def test_reconciled_file_hash_change_fails_closed(self) -> None:
        bill = SyntheticBill("2026-05-10_account_ref.pdf")
        final = self.archive / bill.filename
        final.write_bytes(bill.payload)
        first, _ = self.run_once(FakePortal([bill]))
        self.assertEqual(first.status, "ALREADY_PRESENT")
        final.write_bytes(synthetic_pdf(b"changed"))
        second, _ = self.run_once(FakePortal([bill]))
        self.assertEqual(second.status, "ARCHIVE_CONFLICT")
        self.assertEqual(second.downloaded_count, 0)

if __name__ == "__main__":
    unittest.main()
