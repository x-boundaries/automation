from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from energygrid_bill_downloader import portal as portal_module
from energygrid_bill_downloader import reconcile as reconcile_module
from energygrid_bill_downloader import state as state_module
from energygrid_bill_downloader.config import RuntimeConfig
from energygrid_bill_downloader.errors import (
    ARCHIVE_CONFLICT,
    DOWNLOAD_FAILED,
    AppError,
    DownloadError,
    LayoutChangedError,
    StateError,
)
from energygrid_bill_downloader.portal import InvoiceRow
from energygrid_bill_downloader.publication import FileInfo, filename_key, validate_pdf
from energygrid_bill_downloader.reconcile import reconcile_inventory
from energygrid_bill_downloader.state import StateStore
from tests.fixtures.synthetic_portal import SyntheticBill, synthetic_pdf


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, dict[str, object]]] = []

    def event(self, phase: str, status: str | None = None, **fields: object) -> None:
        self.events.append((phase, status, fields))


def uncertain() -> AppError:
    return AppError("synthetic uncertain dispatch", status=DOWNLOAD_FAILED, retryable=False)


class FakePortal:
    """A download-first portal double: opaque rows, names only at Download.

    `scripts` maps a row ordinal to the per-attempt outcomes of that row: an
    exception instance is raised, anything else downloads normally. The last
    entry repeats. `observer` runs before every download so a case can assert
    what was (not) written before Phase A finished.
    """

    def __init__(
        self,
        bills: list[SyntheticBill],
        *,
        scripts: dict[int, list[object]] | None = None,
        payload_overrides: dict[int, bytes] | None = None,
        observer=None,
    ) -> None:
        self.bills = bills
        self.scripts = scripts or {}
        self.payload_overrides = payload_overrides or {}
        self.observer = observer
        self.binding = object()
        self.inventory_calls = 0
        self.download_log: list[int] = []
        self.destinations: list[Path] = []

    @property
    def download_calls(self) -> int:
        return len(self.download_log)

    def inventory(self, safety_ceiling: int) -> list[InvoiceRow]:
        self.inventory_calls += 1
        if len(self.bills) > safety_ceiling:
            raise LayoutChangedError("synthetic ceiling")
        return [InvoiceRow(ordinal=index, binding=self.binding) for index in range(len(self.bills))]

    def download(self, row: InvoiceRow, destination: Path) -> str:
        if row.binding is not self.binding:
            raise AssertionError("reconcile passed a foreign row handle")
        if self.observer is not None:
            self.observer(row)
        attempt = self.download_log.count(row.ordinal)
        self.download_log.append(row.ordinal)
        self.destinations.append(destination)
        script = self.scripts.get(row.ordinal, [None])
        outcome = script[min(attempt, len(script) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        bill = self.bills[row.ordinal]
        destination.write_bytes(self.payload_overrides.get(row.ordinal, bill.payload))
        return bill.suggested_filename or bill.filename


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

    def records(self) -> dict[str, state_module.BillRecord]:
        with StateStore(self.config.state_path) as state:
            return {record.filename_key: record for record in state.records()}

    def owned_temps(self) -> list[Path]:
        return [path for path in self.config.temp_root.iterdir() if path.name.startswith("run-")]

    def archive_files(self) -> list[str]:
        return sorted(path.name for path in self.archive.iterdir())

    def seed_archived(self, bill: SyntheticBill) -> Path:
        final = self.archive / bill.filename
        final.write_bytes(bill.payload)
        with StateStore(self.config.state_path) as state:
            state.mark_seen(filename_key(bill.filename), bill.filename)
            state.record_archived(filename_key(bill.filename), bill.filename, validate_pdf(final))
        return final

    # ---- Phase A / Phase B happy paths ---- #

    def test_multiple_new_bills_archive_all_and_record_all(self) -> None:
        bills = [
            SyntheticBill("2026-05-11_account_a.pdf"),
            SyntheticBill("2026-05-12_account_b.pdf"),
        ]
        portal = FakePortal(bills)
        summary, _ = self.run_once(portal)
        self.assertEqual(summary.status, "DOWNLOADED")
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(summary.downloaded_count, len(bills))
        self.assertEqual(summary.failure_count, 0)
        self.assertEqual(portal.inventory_calls, 1)
        self.assertEqual(portal.download_log, [0, 1], "rows are acquired in frozen order")
        records = self.records()
        self.assertEqual(len(records), len(bills))
        for bill in bills:
            self.assertTrue((self.archive / bill.filename).exists())
            self.assertEqual(records[filename_key(bill.filename)].status, "ARCHIVED")
        self.assertEqual(self.owned_temps(), [])

    def test_every_row_is_acquired_before_any_state_write_or_publication(self) -> None:
        bills = [SyntheticBill(f"2026-06-0{index}_order.pdf") for index in range(1, 4)]
        observed: list[tuple[int, int, int]] = []

        def observer(row: InvoiceRow) -> None:
            observed.append((row.ordinal, len(self.records()), len(self.archive_files())))

        summary, _ = self.run_once(FakePortal(bills, observer=observer))
        self.assertEqual(summary.status, "DOWNLOADED")
        self.assertEqual(observed, [(0, 0, 0), (1, 0, 0), (2, 0, 0)])

    def test_each_row_gets_its_own_owned_temp_directory(self) -> None:
        bills = [SyntheticBill("2026-06-11_a.pdf"), SyntheticBill("2026-06-12_b.pdf")]
        portal = FakePortal(bills)
        self.run_once(portal)
        parents = [destination.parent for destination in portal.destinations]
        self.assertEqual(len(set(parents)), 2)
        for parent in parents:
            self.assertEqual(parent.parent.resolve(), self.config.temp_root.resolve())
            self.assertRegex(parent.name, r"^run-[0-9a-f-]{36}$")
        self.assertEqual(self.owned_temps(), [])

    def test_filename_is_learned_only_from_the_download(self) -> None:
        bill = SyntheticBill("placeholder-never-used.pdf", suggested_filename="2026-06-21_authoritative.pdf")
        self.assertFalse(hasattr(InvoiceRow(ordinal=0, binding=object()), "filename"))
        summary, _ = self.run_once(FakePortal([bill]))
        self.assertEqual(summary.status, "DOWNLOADED")
        self.assertEqual(self.archive_files(), ["2026-06-21_authoritative.pdf"])
        self.assertIn(filename_key("2026-06-21_authoritative.pdf"), self.records())

    def test_different_filenames_with_identical_bytes_are_both_archived(self) -> None:
        payload = synthetic_pdf(b"same-bytes")
        bills = [
            SyntheticBill("2026-07-01_first.pdf", payload=payload),
            SyntheticBill("2026-07-02_second.pdf", payload=payload),
        ]
        summary, _ = self.run_once(FakePortal(bills))
        self.assertEqual(summary.status, "DOWNLOADED")
        self.assertEqual(summary.downloaded_count, 2)
        self.assertEqual(summary.failure_count, 0)
        records = self.records()
        self.assertEqual(records[filename_key(bills[0].filename)].sha256, records[filename_key(bills[1].filename)].sha256)

    def test_the_same_hash_under_another_existing_key_is_not_a_conflict(self) -> None:
        payload = synthetic_pdf(b"shared-hash")
        existing = SyntheticBill("2026-07-10_existing.pdf", payload=payload)
        self.seed_archived(existing)
        fresh = SyntheticBill("2026-07-11_fresh.pdf", payload=payload)
        summary, _ = self.run_once(FakePortal([fresh]))
        self.assertEqual(summary.status, "DOWNLOADED")
        self.assertEqual(summary.failure_count, 0)
        records = self.records()
        self.assertEqual(records[filename_key(fresh.filename)].status, "ARCHIVED")
        self.assertEqual(records[filename_key(existing.filename)].status, "ARCHIVED")

    def test_reconcile_never_scans_state_for_cross_key_identity(self) -> None:
        bill = SyntheticBill("2026-07-12_no_scan.pdf")
        with mock.patch.object(StateStore, "records", side_effect=AssertionError("cross-key scan")):
            summary, _ = self.run_once(FakePortal([bill]))
        self.assertEqual(summary.status, "DOWNLOADED")

    # ---- Web clarification 1: semantic rerun idempotency ---- #

    def test_rerun_publishes_nothing_and_keeps_archived_integrity_fields(self) -> None:
        bill = SyntheticBill("2026-05-01_account_ref.pdf")
        with mock.patch.object(state_module, "utc_now", return_value="2026-09-25T00:00:00+00:00"):
            first, _ = self.run_once(FakePortal([bill]))
        self.assertEqual(first.status, "DOWNLOADED")
        before = self.records()
        final = self.archive / bill.filename
        final_stat = final.stat()

        second_portal = FakePortal([bill])
        with mock.patch.object(state_module, "utc_now", return_value="2026-09-26T00:00:00+00:00"), \
                mock.patch.object(reconcile_module, "publish_no_replace", side_effect=AssertionError("republished")):
            second, _ = self.run_once(second_portal)
        self.assertEqual(second.status, "ALREADY_PRESENT")
        self.assertEqual(second.exit_code, 0)
        self.assertEqual(second.present_count, 1)
        self.assertEqual(second.downloaded_count, 0)
        self.assertEqual(second_portal.download_calls, 1, "download-first reruns still download")

        after = self.records()
        self.assertEqual(set(after), set(before), "no new filename key")
        key = filename_key(bill.filename)
        for name in ("status", "sha256", "byte_size", "archived_at_utc", "completion_source"):
            with self.subTest(field=name):
                self.assertEqual(getattr(after[key], name), getattr(before[key], name))
        self.assertEqual(after[key].status, "ARCHIVED")
        # The accepted observational refresh.
        self.assertEqual(before[key].last_seen_at_utc, "2026-09-25T00:00:00+00:00")
        self.assertEqual(after[key].last_seen_at_utc, "2026-09-26T00:00:00+00:00")
        self.assertEqual(final.stat().st_mtime_ns, final_stat.st_mtime_ns)
        self.assertEqual(self.archive_files(), [bill.filename])
        self.assertEqual(self.owned_temps(), [], "the fresh temp is discarded")

    # ---- Web clarification 2: failure_count counts rows that actually failed ---- #

    def test_one_terminal_phase_a_row_counts_one_failure_not_n(self) -> None:
        bills = [SyntheticBill(f"2026-08-0{index}_row.pdf") for index in range(1, 5)]
        portal = FakePortal(bills, scripts={1: [DownloadError("synthetic network failure")]})
        summary, logger = self.run_once(portal)
        self.assertEqual(portal.download_log, [0, 1, 1], "retry only that row, then stop")
        self.assertEqual(summary.inventory_count, 4)
        self.assertEqual(summary.downloaded_count, 0)
        self.assertEqual(summary.present_count, 0)
        self.assertEqual(summary.failure_count, 1)
        self.assertEqual(summary.status, "DOWNLOAD_FAILED")
        self.assertEqual(summary.exit_code, 10)
        self.assertEqual(summary.as_dict()["failure_classes"], ["DOWNLOAD_FAILED"])
        self.assertEqual(
            sorted(summary.as_dict()),
            sorted(
                [
                    "run_id",
                    "status",
                    "exit_code",
                    "inventory_count",
                    "downloaded_count",
                    "present_count",
                    "failure_count",
                    "failure_classes",
                ]
            ),
            "no new summary field",
        )
        self.assertEqual(self.records(), {}, "Phase B never began")
        self.assertEqual(self.archive_files(), [])
        self.assertEqual(self.owned_temps(), [])
        self.assertEqual([phase for phase, *_ in logger.events].count("invoice_failure"), 1)

    def test_an_uncertain_download_dispatches_once_and_stops_later_rows(self) -> None:
        bills = [SyntheticBill(f"2026-08-1{index}_row.pdf") for index in range(1, 4)]
        portal = FakePortal(bills, scripts={0: [uncertain()]})
        summary, logger = self.run_once(portal)
        self.assertEqual(portal.download_log, [0])
        self.assertEqual(summary.failure_count, 1)
        self.assertEqual(summary.status, "DOWNLOAD_FAILED")
        self.assertFalse(any(phase == "download_retry" for phase, *_ in logger.events))
        self.assertEqual(self.records(), {})
        self.assertEqual(self.owned_temps(), [])

    def test_a_drift_latch_stops_later_rows(self) -> None:
        bills = [SyntheticBill(f"2026-08-2{index}_row.pdf") for index in range(1, 4)]
        portal = FakePortal(bills, scripts={1: [LayoutChangedError("synthetic drift")]})
        summary, _ = self.run_once(portal)
        self.assertEqual(portal.download_log, [0, 1])
        self.assertEqual(summary.status, "PORTAL_LAYOUT_CHANGED")
        self.assertEqual(summary.exit_code, 20)
        self.assertEqual(summary.failure_count, 1)
        self.assertEqual(self.records(), {})
        self.assertEqual(self.archive_files(), [])
        self.assertEqual(self.owned_temps(), [])

    # ---- retry versus uncertainty ---- #

    def test_a_known_failure_retries_only_that_row_within_max_attempts(self) -> None:
        bills = [SyntheticBill("2026-08-31_a.pdf"), SyntheticBill("2026-08-31_b.pdf")]
        portal = FakePortal(bills, scripts={0: [DownloadError("synthetic network failure"), None]})
        summary, logger = self.run_once(portal)
        self.assertEqual(summary.status, "DOWNLOADED")
        self.assertEqual(portal.download_log, [0, 0, 1])
        retries = [fields for phase, _status, fields in logger.events if phase == "download_retry"]
        self.assertEqual(retries, [{"attempt": 1}])

    def test_download_failure_is_recorded_without_fake_success(self) -> None:
        bill = SyntheticBill("2026-05-05_account_ref.pdf")
        portal = FakePortal([bill], scripts={0: [DownloadError("synthetic network failure")]})
        summary, logger = self.run_once(portal)
        self.assertEqual(summary.status, "DOWNLOAD_FAILED")
        self.assertEqual(summary.exit_code, 10)
        self.assertEqual(portal.download_calls, 2)
        self.assertTrue(any(phase == "download_retry" for phase, _status, _fields in logger.events))
        self.assertFalse((self.archive / bill.filename).exists())

    def test_an_invalid_pdf_is_terminal_in_phase_a(self) -> None:
        bills = [SyntheticBill("2026-09-01_bad.pdf"), SyntheticBill("2026-09-02_next.pdf")]
        portal = FakePortal(bills, payload_overrides={0: b"<html>not a bill</html>"})
        summary, _ = self.run_once(portal)
        self.assertEqual(summary.status, "INVALID_PDF")
        self.assertEqual(portal.download_log, [0])
        self.assertEqual(summary.failure_count, 1)
        self.assertEqual(self.records(), {})
        self.assertEqual(self.owned_temps(), [])

    # ---- whole-run filename contract ---- #

    def test_duplicate_normalized_filename_keys_fail_before_any_state_or_publication(self) -> None:
        duplicate = [SyntheticBill("Invoice.pdf"), SyntheticBill("invoice.PDF")]
        with self.assertRaises(AppError) as caught:
            self.run_once(FakePortal(duplicate))
        self.assertEqual(caught.exception.status, "PORTAL_LAYOUT_CHANGED")
        self.assertEqual(self.records(), {})
        self.assertEqual(self.archive_files(), [])
        self.assertEqual(self.owned_temps(), [])

    def test_an_unsafe_suggested_filename_fails_before_any_state_or_publication(self) -> None:
        bills = [SyntheticBill("2026-09-03_safe.pdf"), SyntheticBill("..\\escape.pdf")]
        with self.assertRaises(AppError) as caught:
            self.run_once(FakePortal(bills))
        self.assertEqual(caught.exception.status, "ACTION_REQUIRED")
        self.assertEqual(self.records(), {})
        self.assertEqual(self.archive_files(), [])
        self.assertEqual(self.owned_temps(), [])

    def test_an_interruption_during_phase_a_leaves_no_state_publication_or_temp(self) -> None:
        bills = [SyntheticBill("2026-09-04_a.pdf"), SyntheticBill("2026-09-04_b.pdf")]
        portal = FakePortal(bills, scripts={1: [KeyboardInterrupt()]})
        with self.assertRaises(KeyboardInterrupt):
            self.run_once(portal)
        self.assertEqual(self.records(), {})
        self.assertEqual(self.archive_files(), [])
        self.assertEqual(self.owned_temps(), [])

    # ---- Phase B: existing filename-keyed semantics ---- #

    def test_an_already_present_archive_discards_the_fresh_temp(self) -> None:
        bill = SyntheticBill("2026-09-05_present.pdf")
        final = self.seed_archived(bill)
        original = final.read_bytes()
        portal = FakePortal([bill])
        summary, _ = self.run_once(portal)
        self.assertEqual(summary.status, "ALREADY_PRESENT")
        self.assertEqual(summary.present_count, 1)
        self.assertEqual(portal.download_calls, 1)
        self.assertEqual(final.read_bytes(), original)
        self.assertEqual(self.owned_temps(), [])

    def test_existing_file_without_state_is_reconciled(self) -> None:
        bill = SyntheticBill("2026-05-02_account_ref.pdf")
        (self.archive / bill.filename).write_bytes(bill.payload)
        summary, _ = self.run_once(FakePortal([bill]))
        self.assertEqual(summary.status, "ALREADY_PRESENT")
        record = self.records()[filename_key(bill.filename)]
        self.assertEqual(record.status, "PRESENT_RECONCILED")
        self.assertEqual(self.owned_temps(), [])

    def test_missing_final_file_is_repaired_when_the_same_key_hash_matches(self) -> None:
        bill = SyntheticBill("2026-05-03_account_ref.pdf")
        final = self.seed_archived(bill)
        final.unlink()
        summary, _ = self.run_once(FakePortal([bill]))
        self.assertEqual(summary.status, "DOWNLOADED")
        self.assertTrue(final.exists())
        self.assertEqual(self.owned_temps(), [])

    def test_archived_state_missing_final_redownload_hash_mismatch_is_conflict(self) -> None:
        bill = SyntheticBill("2026-05-17_account_repair.pdf")
        final = self.seed_archived(bill)
        final.unlink()
        portal = FakePortal([bill], payload_overrides={0: synthetic_pdf(b"different-repair")})
        summary, _ = self.run_once(portal)
        self.assertEqual(summary.status, "ARCHIVE_CONFLICT")
        self.assertEqual(summary.exit_code, 20)
        self.assertFalse(final.exists())
        self.assertEqual(len(self.owned_temps()), 1, "same-key repair evidence is retained")
        self.assertEqual(self.records()[filename_key(bill.filename)].status, "CONFLICT")

    def test_retained_conflict_does_not_block_later_invoice(self) -> None:
        first_bill = SyntheticBill("2026-05-15_account_conflict.pdf")
        second_bill = SyntheticBill("2026-05-16_account_new.pdf")
        first_final = self.seed_archived(first_bill)
        first_final.unlink()
        unrelated = self.config.temp_root / "unrelated-artifact"
        unrelated.mkdir()
        marker = unrelated / "keep.txt"
        marker.write_text("synthetic", encoding="utf-8")

        portal = FakePortal(
            [first_bill, second_bill],
            payload_overrides={0: synthetic_pdf(b"changed-first-invoice")},
        )
        summary, _ = self.run_once(portal)
        self.assertEqual(summary.status, "ARCHIVE_CONFLICT")
        self.assertEqual(summary.exit_code, 20)
        self.assertEqual(summary.downloaded_count, 1)
        self.assertEqual(summary.failure_count, 1)
        self.assertFalse(first_final.exists())
        self.assertTrue((self.archive / second_bill.filename).exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "synthetic")
        retained = self.owned_temps()
        self.assertEqual(len(retained), 1)
        self.assertRegex(retained[0].name, r"^run-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        records = self.records()
        self.assertEqual(records[filename_key(first_bill.filename)].status, "CONFLICT")
        self.assertEqual(records[filename_key(second_bill.filename)].status, "ARCHIVED")

    def test_conflicting_existing_file_fails_closed_and_cleans_the_fresh_temp(self) -> None:
        bill = SyntheticBill("2026-05-04_account_ref.pdf")
        final = self.seed_archived(bill)
        final.write_bytes(synthetic_pdf(b"different"))
        summary, _ = self.run_once(FakePortal([bill]))
        self.assertEqual(summary.status, "ARCHIVE_CONFLICT")
        self.assertEqual(final.read_bytes(), synthetic_pdf(b"different"))
        self.assertEqual(self.owned_temps(), [])

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
        self.assertEqual(second_portal.download_calls, 1)
        self.assertEqual(validate_pdf(final), original_info)
        self.assertEqual(self.records()[filename_key(bill.filename)].status, "PRESENT_RECONCILED")
        self.assertEqual(self.owned_temps(), [])

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

    # ---- list-only and empty inventory ---- #

    def test_list_only_downloads_nothing_writes_nothing_and_is_action_required(self) -> None:
        bills = [SyntheticBill("2026-05-06_account_ref.pdf"), SyntheticBill("2026-05-07_account_ref.pdf")]
        (self.archive / bills[0].filename).write_bytes(bills[0].payload)
        portal = FakePortal(bills)
        summary, _ = self.run_once(portal, list_only=True)
        self.assertEqual(summary.status, "ACTION_REQUIRED")
        self.assertEqual(summary.exit_code, 20)
        self.assertEqual(summary.inventory_count, 2)
        self.assertEqual(summary.present_count, 0)
        self.assertEqual(summary.failure_count, 0)
        self.assertEqual(portal.download_calls, 0)
        self.assertEqual(self.records(), {})
        self.assertFalse(self.config.temp_root.exists() and self.owned_temps())

    def test_an_empty_stub_inventory_is_still_no_new_bills(self) -> None:
        portal = FakePortal([])
        summary, _ = self.run_once(portal)
        self.assertEqual(summary.status, "NO_NEW_BILLS")
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(portal.download_calls, 0)

    # ---- privacy ---- #

    def test_no_filename_or_private_value_reaches_logs_summary_or_repr(self) -> None:
        private_name = "2026-09-09_PRIVATE-ACCOUNT-9911.pdf"
        bills = [
            SyntheticBill(private_name),
            SyntheticBill("2026-09-10_other.pdf", payload=b"<html>PRIVATE-ACCOUNT-9911</html>"),
        ]
        summary, logger = self.run_once(FakePortal(bills))
        emitted = repr(logger.events) + repr(summary.as_dict())
        self.assertNotIn("PRIVATE-ACCOUNT-9911", emitted)
        self.assertNotIn("2026-09-09", emitted)
        for _phase, _status, fields in logger.events:
            # `row_ordinal` is the accepted G2-083 enrichment: a bounded int.
            self.assertTrue(set(fields) <= {"inventory_count", "attempt", "row_ordinal"})
            if "row_ordinal" in fields:
                self.assertIs(type(fields["row_ordinal"]), int)
        acquisition = reconcile_module._Acquisition(
            ordinal=0,
            run_dir=self.root,
            temp_path=self.root / "download.bin",
            filename=private_name,
            key=filename_key(private_name),
            final_path=self.archive / private_name,
            info=FileInfo(byte_size=1, sha256="0" * 64),
        )
        self.assertNotIn("PRIVATE", repr(acquisition))
        self.assertNotIn("binding", repr(InvoiceRow(ordinal=3, binding=object())))


def preflight_error(reason="CONTROL_NOT_ENABLED", checkpoint="CONTROL_ENABLED", **overrides):
    evidence = portal_module.DownloadPreflightEvidence(
        row_ordinal=overrides.pop("row_ordinal", 1),
        reason_code=reason,
        last_checkpoint=checkpoint,
        window_expired=overrides.pop("window_expired", True),
        not_ready_looks=7,
        elapsed_bucket="LT_60S",
    )
    return portal_module.DownloadPreflightError(portal_module.RESULTS_SURFACE_CHANGED_MESSAGE, evidence)


class InvoiceFailureEnrichmentTests(unittest.TestCase):
    """DL-XB-199 G2-083: additive, closed, validated invoice_failure fields."""

    setUp = ReconcileTests.setUp
    tearDown = ReconcileTests.tearDown
    run_once = ReconcileTests.run_once
    records = ReconcileTests.records
    archive_files = ReconcileTests.archive_files
    owned_temps = ReconcileTests.owned_temps
    seed_archived = ReconcileTests.seed_archived

    @staticmethod
    def failures(logger) -> list[tuple[str | None, dict]]:
        return [(status, fields) for phase, status, fields in logger.events if phase == "invoice_failure"]

    def test_a_phase_a_preflight_failure_carries_its_row_reason_and_checkpoint(self) -> None:
        bills = [SyntheticBill(f"2026-10-0{index}_row.pdf") for index in range(1, 4)]
        portal = FakePortal(bills, scripts={1: [preflight_error()]})
        summary, logger = self.run_once(portal)
        self.assertEqual(
            self.failures(logger),
            [
                (
                    "PORTAL_LAYOUT_CHANGED",
                    {
                        "row_ordinal": 1,
                        "preflight_reason": "CONTROL_NOT_ENABLED",
                        "preflight_checkpoint": "CONTROL_ENABLED",
                    },
                )
            ],
        )
        # Existing status/exit/summary semantics are unchanged.
        self.assertEqual(portal.download_log, [0, 1])
        self.assertEqual((summary.status, summary.exit_code, summary.failure_count), ("PORTAL_LAYOUT_CHANGED", 20, 1))
        self.assertEqual(summary.as_dict()["failure_classes"], ["PORTAL_LAYOUT_CHANGED"])
        self.assertEqual(self.records(), {})
        self.assertEqual(self.archive_files(), [])

    def test_a_post_dispatch_or_plain_failure_carries_only_its_row(self) -> None:
        bills = [SyntheticBill(f"2026-10-1{index}_row.pdf") for index in range(1, 3)]
        for error in (uncertain(), LayoutChangedError("synthetic drift"), DownloadError("synthetic", retryable=False)):
            with self.subTest(error=type(error).__name__):
                summary, logger = self.run_once(FakePortal(bills, scripts={0: [error]}))
                self.assertEqual(self.failures(logger), [(error.status, {"row_ordinal": 0})])

    def test_a_phase_b_failure_carries_its_acquisition_row(self) -> None:
        bill = SyntheticBill("2026-10-21_conflict.pdf")
        (self.archive / bill.filename).write_bytes(b"not a pdf at all")
        other = SyntheticBill("2026-10-22_fine.pdf")
        summary, logger = self.run_once(FakePortal([other, bill]))
        self.assertEqual(self.failures(logger), [(ARCHIVE_CONFLICT, {"row_ordinal": 1})])
        self.assertEqual(summary.status, ARCHIVE_CONFLICT)

    def test_invalid_values_are_omitted_never_coerced_or_logged_raw(self) -> None:
        hostile = preflight_error(reason="PRIVATE ACCT-778899", checkpoint="https://portal.example.invalid")
        self.assertEqual(reconcile_module._failure_enrichment(hostile, 2), {"row_ordinal": 2})
        half = preflight_error(checkpoint="NOT_A_CHECKPOINT")
        self.assertEqual(
            reconcile_module._failure_enrichment(half, 2),
            {"row_ordinal": 2, "preflight_reason": "CONTROL_NOT_ENABLED"},
        )
        for ordinal in (True, -1, 100_000, 1.0, "1", None):
            with self.subTest(ordinal=repr(ordinal)):
                self.assertEqual(reconcile_module._failure_enrichment(uncertain(), ordinal), {})
        # A LayoutChangedError that is not a preflight error never gains reason fields.
        self.assertEqual(reconcile_module._failure_enrichment(LayoutChangedError("x"), 0), {"row_ordinal": 0})
        bills = [SyntheticBill("2026-10-31_row.pdf")]
        _summary, logger = self.run_once(FakePortal(bills, scripts={0: [hostile]}))
        self.assertNotIn("ACCT", repr(logger.events))
        self.assertNotIn("portal.example", repr(logger.events))

    def test_every_real_reason_and_checkpoint_is_accepted(self) -> None:
        for reason in portal_module.DOWNLOAD_PREFLIGHT_REASONS:
            checkpoint = portal_module.DOWNLOAD_PREFLIGHT_REASON_CHECKPOINTS.get(reason, "CONTROL_ACTIONABLE")
            with self.subTest(reason=reason):
                self.assertEqual(
                    reconcile_module._failure_enrichment(preflight_error(reason, checkpoint), 0),
                    {"row_ordinal": 0, "preflight_reason": reason, "preflight_checkpoint": checkpoint},
                )


if __name__ == "__main__":
    unittest.main()
