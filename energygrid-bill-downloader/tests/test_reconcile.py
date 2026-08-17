from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import uuid

from energygrid_bill_downloader.config import RuntimeConfig
from energygrid_bill_downloader.errors import AppError, DownloadError, StateError
from energygrid_bill_downloader.portal import BillRef
from energygrid_bill_downloader.publication import FileInfo, filename_key, validate_pdf
from energygrid_bill_downloader.reconcile import reconcile_inventory
from energygrid_bill_downloader.state import StateStore
from tests.fixtures.synthetic_portal import SyntheticBill, synthetic_pdf


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, dict[str, object]]] = []

    def event(self, phase: str, status: str | None = None, **fields: object) -> None:
        self.events.append((phase, status, fields))


class FakePortal:
    def __init__(
        self, bills: list[SyntheticBill], failure_mode: str | None = None, payload_overrides: dict[str, bytes] | None = None
    ) -> None:
        self.bills = bills
        self.failure_mode = failure_mode
        self.payload_overrides = payload_overrides or {}
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
        destination.write_bytes(self.payload_overrides.get(bill.filename, matching.payload))
        return matching.filename


class FailingArchiveCommitState(StateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.fail_next_archive_commit = True

    def record_archived(
        self,
        filename_key: str,
        portal_filename: str,
        info: FileInfo,
        completion_source: str = "downloaded",
        now: str | None = None,
    ) -> None:
        if self.fail_next_archive_commit and completion_source == "downloaded":
            self.fail_next_archive_commit = False
            raise StateError("synthetic archive-state commit failure")
        super().record_archived(filename_key, portal_filename, info, completion_source, now)


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

    def run_once(self, portal: FakePortal, *, list_only: bool = False, state_factory: type[StateStore] = StateStore):
        logger = RecordingLogger()
        with state_factory(self.config.state_path) as state:
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
    def test_multiple_new_bills_archive_all_and_record_all(self) -> None:
        bills = [
            SyntheticBill("2026-05-11_account_a.pdf"),
            SyntheticBill("2026-05-12_account_b.pdf"),
        ]
        summary, _ = self.run_once(FakePortal(bills))
        self.assertEqual(summary.status, "DOWNLOADED")
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(summary.downloaded_count, len(bills))
        self.assertEqual(summary.failure_count, 0)
        with StateStore(self.config.state_path) as state:
            records = {record.filename_key: record for record in state.records()}
        self.assertEqual(len(records), len(bills))
        for bill in bills:
            self.assertTrue((self.archive / bill.filename).exists())
            self.assertEqual(records[filename_key(bill.filename)].status, "ARCHIVED")

    def test_suggested_filename_identity_mismatch_fails_closed(self) -> None:
        bill = SyntheticBill("2026-05-13_account_ref.pdf")
        portal = FakePortal([bill], failure_mode="wrong-name")
        summary, _ = self.run_once(portal)
        self.assertEqual(summary.status, "PORTAL_LAYOUT_CHANGED")
        self.assertEqual(summary.exit_code, 20)
        self.assertFalse((self.archive / bill.filename).exists())

    def test_publication_state_commit_failure_preserves_final_for_next_run(self) -> None:
        bill = SyntheticBill("2026-05-14_account_ref.pdf")
        first, _ = self.run_once(FakePortal([bill]), state_factory=FailingArchiveCommitState)
        final = self.archive / bill.filename
        self.assertEqual(first.status, "STATE_INCONSISTENT")
        self.assertEqual(first.exit_code, 20)
        self.assertTrue(final.exists())
        original_info = validate_pdf(final)

        second_portal = FakePortal([bill])
        second, _ = self.run_once(second_portal)
        self.assertEqual(second.status, "ALREADY_PRESENT")
        self.assertEqual(second_portal.download_calls, 0)
        self.assertEqual(validate_pdf(final), original_info)
        with StateStore(self.config.state_path) as state:
            record = state.get(filename_key(bill.filename))
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, "PRESENT_RECONCILED")

    def test_retained_conflict_does_not_block_later_invoice(self) -> None:
        first_bill = SyntheticBill("2026-05-15_account_conflict.pdf")
        second_bill = SyntheticBill("2026-05-16_account_new.pdf")
        first_final = self.archive / first_bill.filename
        first_final.write_bytes(first_bill.payload)
        with StateStore(self.config.state_path) as state:
            state.mark_seen(filename_key(first_bill.filename), first_bill.filename)
            state.record_archived(filename_key(first_bill.filename), first_bill.filename, validate_pdf(first_final))
        first_final.unlink()
        unrelated = self.config.temp_root / "unrelated-artifact"
        unrelated.mkdir()
        marker = unrelated / "keep.txt"
        marker.write_text("synthetic", encoding="utf-8")

        portal = FakePortal(
            [first_bill, second_bill],
            payload_overrides={first_bill.filename: synthetic_pdf(b"changed-first-invoice")},
        )
        summary, _ = self.run_once(portal)
        self.assertEqual(summary.status, "ARCHIVE_CONFLICT")
        self.assertEqual(summary.exit_code, 20)
        self.assertEqual(summary.downloaded_count, 1)
        self.assertEqual(summary.failure_count, 1)
        self.assertFalse(first_final.exists())
        self.assertTrue((self.archive / second_bill.filename).exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "synthetic")
        retained = [path for path in self.config.temp_root.iterdir() if path.name.startswith("run-")]
        self.assertEqual(len(retained), 1)
        self.assertRegex(retained[0].name, r"^run-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        with StateStore(self.config.state_path) as state:
            first_record = state.get(filename_key(first_bill.filename))
            second_record = state.get(filename_key(second_bill.filename))
        self.assertIsNotNone(first_record)
        self.assertIsNotNone(second_record)
        assert first_record is not None and second_record is not None
        self.assertEqual(first_record.status, "CONFLICT")
        self.assertEqual(second_record.status, "ARCHIVED")

    def test_archived_state_missing_final_redownload_hash_mismatch_is_conflict(self) -> None:
        bill = SyntheticBill("2026-05-17_account_repair.pdf")
        final = self.archive / bill.filename
        final.write_bytes(bill.payload)
        with StateStore(self.config.state_path) as state:
            state.mark_seen(filename_key(bill.filename), bill.filename)
            state.record_archived(filename_key(bill.filename), bill.filename, validate_pdf(final))
        final.unlink()
        portal = FakePortal([bill], payload_overrides={bill.filename: synthetic_pdf(b"different-repair")})
        summary, _ = self.run_once(portal)
        self.assertEqual(summary.status, "ARCHIVE_CONFLICT")
        self.assertEqual(summary.exit_code, 20)
        self.assertFalse(final.exists())
        self.assertEqual(len(list(self.config.temp_root.glob("run-*"))), 1)
        with StateStore(self.config.state_path) as state:
            record = state.get(filename_key(bill.filename))
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, "CONFLICT")


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
