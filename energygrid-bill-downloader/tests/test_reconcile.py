from __future__ import annotations

import contextlib
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
    ACTION_REQUIRED,
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
        reserved_names = (
            "..\\escape.pdf",
            "CON.extra.pdf",
            "NUL.extra.pdf",
            "COM1.extra.pdf",
            "COM¹.pdf",
            "LPT².pdf",
            "con.Extra.PDF",
            "lPt².PdF",
            "CONIN$.extra.pdf",
            "conout$.PDF",
            "CON .extra.pdf",
        )
        state_writes: list[str] = []

        def write_spy(name: str):
            original = getattr(StateStore, name)

            def record_write(state, *args, **kwargs):
                state_writes.append(name)
                return original(state, *args, **kwargs)

            return record_write

        with contextlib.ExitStack() as stack:
            for method_name in ("mark_seen", "record_archived", "record_failure"):
                stack.enter_context(mock.patch.object(StateStore, method_name, new=write_spy(method_name)))
            for unsafe_name in reserved_names:
                with self.subTest(name=unsafe_name):
                    bills = [
                        SyntheticBill("2026-09-03_safe.pdf"),
                        SyntheticBill("2026-09-04_other.pdf", suggested_filename=unsafe_name),
                    ]
                    portal = FakePortal(bills)
                    with self.assertRaises(AppError) as caught:
                        self.run_once(portal)
                    self.assertEqual(caught.exception.status, "ACTION_REQUIRED")
                    self.assertEqual(2, portal.download_calls)
                    self.assertEqual(self.records(), {})
                    self.assertEqual(self.archive_files(), [])
                    self.assertEqual(self.owned_temps(), [])
        self.assertEqual([], state_writes)

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


class DualStreamReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        from energygrid_bill_downloader.config import DeliverySettings, DriveSettings, DualRuntimeConfig
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_dual_stream import create_v2_database, test_stream_entries

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "archive"
        self.drive_root = self.root / "drive"
        self.archive.mkdir()
        self.drive_root.mkdir()
        self.temp_root = self.root / "temp"
        self.log_root = self.root / "logs"
        self.state_path = self.root / "state" / "state.sqlite3"
        self.state_path.parent.mkdir()
        self.streams = test_stream_entries()
        create_v2_database(self.state_path)
        self.config = DualRuntimeConfig(
            archive_root=self.archive,
            state_path=self.state_path,
            temp_root=self.temp_root,
            log_root=self.log_root,
            streams=self.streams,
            drive=DriveSettings(mode="local_stage", root=self.drive_root, binding_id="SYNTHETIC_DRIVE_BINDING"),
            delivery=DeliverySettings(
                url="http://127.0.0.1:5678/webhook/SYNTHETIC",
                auth_header_name="X-Synthetic-Delivery",
                auth_token_env="ENERGYGRID_DELIVERY_TOKEN",
                max_pdf_bytes=1_000_000,
                timeout_seconds=5,
            ),
        )
        self.config.preflight()

    def adapters(self, *, tie_eb: bool = False, eb_candidates=None):
        from energygrid_bill_downloader.invoice import Stream
        from fixtures.synthetic_delivery import synthetic_pdf
        from fixtures.synthetic_dual_stream import SyntheticAdapter, candidate, snapshot

        eb = tuple(eb_candidates) if eb_candidates is not None else (
            candidate(Stream.EB_BILL, name="eb-old.pdf", invoice_date="2026-09-01"),
            candidate(Stream.EB_BILL, name="eb-latest.pdf", invoice_date="2026-10-01"),
        )
        if tie_eb:
            eb = (
                candidate(Stream.EB_BILL, name="eb-latest-a.pdf", invoice_date="2026-10-01"),
                candidate(Stream.EB_BILL, name="eb-latest-b.pdf", invoice_date="2026-10-01"),
            )
        tenant = (
            candidate(Stream.TENANT_BILL, name="tenant-old.pdf", invoice_date="2026-10-01"),
            candidate(Stream.TENANT_BILL, name="tenant-latest.pdf", invoice_date="2026-10-02"),
        )
        payloads = {item.source_filename: synthetic_pdf(item.source_filename.encode()) for item in (*eb, *tenant)}
        return {
            "EB_BILL": SyntheticAdapter(snapshot(Stream.EB_BILL, eb), payloads),
            "TENANT_BILL": SyntheticAdapter(snapshot(Stream.TENANT_BILL, tenant), payloads),
        }

    def fake_delivery(self, calls: list[str]):
        from energygrid_bill_downloader.delivery import DELIVERY_SCHEMA, DeliveryOutcome
        from energygrid_bill_downloader.publication import validate_pdf

        class FakeDelivery:
            def deliver(inner_self, state, invoice, archive_path, run_id, *, logger=None):
                from energygrid_bill_downloader.delivery import DELIVERY_SCHEMA

                calls.append(invoice["stream"])
                info = validate_pdf(archive_path)
                delivery_id = "egmail-v1-" + uuid.uuid4().hex
                metadata = {
                    "schema": DELIVERY_SCHEMA,
                    "stream": invoice["stream"],
                    "bill_date": invoice["bill_date"],
                    "attachment_name": invoice["canonical_filename"],
                    "pdf_byte_size": info.byte_size,
                    "pdf_sha256": info.sha256,
                }
                row, _ = state.prepare_delivery(
                    invoice_id=invoice["invoice_id"], metadata=metadata,
                    run_id=run_id, timestamp="2026-10-02T00:00:03+00:00", delivery_id=delivery_id,
                )
                if not state.claim_delivery_dispatch(delivery_id, run_id, "2026-10-02T00:00:04+00:00"):
                    raise AssertionError("synthetic dispatch marker was not acquired")
                if not state.record_delivery_outcome(
                    delivery_id, run_id, "2026-10-02T00:00:05+00:00", state="DELIVERED",
                    evidence="VALIDATED_N8N_RESULT", support_ref="EG_SYNTHETIC_MAIL_ACCEPTED",
                    accepted_at_utc="2026-10-02T00:00:05+00:00",
                ):
                    raise AssertionError("synthetic result was not committed")
                return DeliveryOutcome("DELIVERED", row["delivery_id"], "EG_SYNTHETIC_MAIL_ACCEPTED", True)

        return FakeDelivery()

    def seed_migrated_archived_latest(
        self, *, name: str = "eb-latest.pdf", invoice_date: str = "2026-10-01", label: str = "state",
        verified_archive: bool = True,
    ):
        import sqlite3
        from dataclasses import replace
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.state import StateV2Store, migrate_state_database
        from fixtures.synthetic_delivery import synthetic_pdf
        from fixtures.synthetic_dual_stream import candidate

        legacy_path = self.root / f"migrated-state-{label}" / "state.sqlite3"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        payload = synthetic_pdf(name.encode())
        payload_path = self.root / f"migrated-payload-{label}.pdf"
        payload_path.write_bytes(payload)
        info = validate_pdf(payload_path)
        with contextlib.closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute(
                "CREATE TABLE bills (filename_key TEXT PRIMARY KEY,portal_filename TEXT NOT NULL,first_seen_at_utc TEXT NOT NULL,last_seen_at_utc TEXT NOT NULL,archived_at_utc TEXT,byte_size INTEGER,sha256 TEXT,status TEXT NOT NULL,last_error_class TEXT,last_error_at_utc TEXT,attempt_count INTEGER NOT NULL DEFAULT 0,completion_source TEXT)"
            )
            connection.execute("PRAGMA user_version=1")
            connection.execute(
                "INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    name, name, f"{invoice_date}T00:00:00+00:00", f"{invoice_date}T00:00:01+00:00",
                    f"{invoice_date}T00:00:02+00:00" if verified_archive else None,
                    info.byte_size if verified_archive else None, info.sha256 if verified_archive else None,
                    "ARCHIVED" if verified_archive else "SEEN", None, None,
                    1 if verified_archive else 0, "downloaded" if verified_archive else None,
                ),
            )
            connection.commit()
        entry = self.streams[Stream.EB_BILL.value]
        item = candidate(Stream.EB_BILL, name=name, invoice_date=invoice_date)
        mapping = [{
            "legacy_filename_key": name, "stream": Stream.EB_BILL.value,
            "source_namespace": entry.source_namespace, "source_filename": name,
            "raw_date": invoice_date, "date_profile": entry.date_profile,
            "evidence_ref": entry.evidence_ref,
        }]
        self.assertEqual("MIGRATED", migrate_state_database(
            legacy_path, apply=True, streams=self.streams, mapping_entries=mapping,
        )["status"])
        old_archive = self.archive / name
        old_archive.write_bytes(payload)
        self.config = replace(self.config, state_path=legacy_path)
        with StateV2Store(legacy_path, read_only=True) as state:
            retained = state.resolve_latest_candidate(item)
            self.assertIsNotNone(retained)
            retained_id = retained["invoice_id"]
        return item, old_archive, payload, info, retained_id

    def seed_migrated_delivered_latest(self, *, label: str, invoice_date: str = "2026-10-01"):
        from energygrid_bill_downloader.drive import DriveStager
        from energygrid_bill_downloader.state import StateV2Store

        item, old_archive, payload, info, invoice_id = self.seed_migrated_archived_latest(
            label=label, invoice_date=invoice_date,
        )
        calls: list[str] = []
        with StateV2Store(self.config.state_path) as state:
            invoice = state.invoice(invoice_id)
            recovered, action = reconcile_module._ensure_archive_available(
                self.config, state, invoice, item, object(), str(uuid.uuid4()), RecordingLogger(),
            )
            self.assertEqual((info, "REUSED"), (recovered, action))
            invoice = state.invoice(invoice_id)
            DriveStager(self.archive, self.drive_root, self.config.drive.binding_id).stage(
                state, invoice, self.archive / invoice["archive_relpath"], str(uuid.uuid4()),
            )
            invoice = state.invoice(invoice_id)
            outcome = self.fake_delivery(calls).deliver(
                state, invoice, self.archive / invoice["archive_relpath"], str(uuid.uuid4()),
            )
            self.assertEqual("DELIVERED", outcome.state)
            delivery = state.delivery(outcome.delivery_id)
            invoice = state.invoice(invoice_id)
            drive_history = state.file_operation_history(
                invoice_id, "DRIVE_STAGE", self.config.drive.binding_id, invoice["archive_relpath"],
            )
        self.assertEqual(["EB_BILL"], calls)
        return item, old_archive, payload, info, invoice_id, delivery, drive_history, invoice["archive_relpath"]

    def register_unresolved_legacy_row(self, state, item, *, legacy_key: str | None = None) -> None:
        invoice_id = str(uuid.uuid4())
        timestamp = "2026-10-02T00:00:00+00:00"
        retained_key = legacy_key if legacy_key is not None else item.source_invoice_key
        with state.transaction() as connection:
            connection.execute(
                "INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (retained_key, item.source_filename, timestamp, timestamp, None, None, None, "SEEN", None, None, 0, None),
            )
            connection.execute(
                "INSERT INTO energygrid_invoice_v2 (invoice_id,legacy_filename_key,classification,legacy_path,archive_state,migration_state,drive_state,created_at_utc,created_run_id) "
                "VALUES (?,?,'UNCLASSIFIED',?,'UNVERIFIED','UNCLASSIFIED','NOT_STAGED',?,?)",
                (invoice_id, retained_key, item.source_filename, timestamp, "00000000-0000-0000-0000-000000000000"),
            )

    def test_migrated_historical_date_drift_holds_before_any_shortcut_or_mutation(self) -> None:
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_dual_stream import SyntheticAdapter, candidate, snapshot

        item, old_archive, payload, _info, retained_id = self.seed_migrated_archived_latest()
        changed = candidate(Stream.EB_BILL, name=item.source_filename, invoice_date="2026-10-02")
        adapters = {
            "EB_BILL": SyntheticAdapter(snapshot(Stream.EB_BILL, (changed,)), {changed.source_filename: payload}),
            "TENANT_BILL": SyntheticAdapter(snapshot(Stream.TENANT_BILL, ())),
        }
        calls: list[str] = []
        class MustNotSend:
            def deliver(inner_self, *args, **kwargs):
                calls.append("called")
                raise AssertionError("identity drift reached email")

        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=MustNotSend()):
            with StateV2Store(self.config.state_path) as state:
                invoice_before = state.invoice(retained_id)
                stream_before = state.stream(Stream.EB_BILL.value)
                summary = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()))
                detail = next(row for row in summary.stream_results if row["stream"] == Stream.EB_BILL.value)
                self.assertEqual("LATEST_IDENTITY_CONFLICT", detail["status"])
                self.assertEqual("EG_LATEST_IDENTITY_FACTS_CHANGED", detail["support_ref"])
                self.assertEqual(invoice_before, state.invoice(retained_id))
                self.assertEqual(stream_before, state.stream(Stream.EB_BILL.value))
                self.assertEqual([], state.file_operation_history(retained_id, "LEGACY_MOVE", None, "EB Bill/2026-10-01.pdf"))
        self.assertEqual(0, summary.fetch_count)
        self.assertEqual([], adapters["EB_BILL"].acquire_calls)
        self.assertEqual([], adapters["TENANT_BILL"].acquire_calls)
        self.assertEqual([], calls)
        self.assertEqual(payload, old_archive.read_bytes())
        self.assertFalse((self.archive / "EB Bill" / "2026-10-01.pdf").exists())
        self.assertFalse(any(self.drive_root.rglob("*.pdf")))

    def test_migrated_delivered_archive_repairs_drive_once_and_replays_without_effects(self) -> None:
        import json
        import re
        from energygrid_bill_downloader.cli import SafeLogger
        from energygrid_bill_downloader.delivery import DELIVERY_RESULT_SCHEMA, DeliveryClient
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_dual_stream import SyntheticAdapter, snapshot

        item, old_archive, payload, info, retained_id = self.seed_migrated_archived_latest()
        posts: list[str] = []

        def post_once(url, authorization, body, timeout, *, content_type):
            posts.append("POST")
            delivery_id = re.search(rb'"delivery_id":"(egmail-v1-[0-9a-f]{32})"', body).group(1).decode()
            response = json.dumps({
                "schema": DELIVERY_RESULT_SCHEMA, "delivery_id": delivery_id,
                "outcome": "DELIVERED", "duplicate": False, "support_ref": "EG_SYNTHETIC_ACCEPTED",
            }, separators=(",", ":")).encode()
            return 200, response

        client = DeliveryClient(
            self.config.delivery, post_once=post_once,
            environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"},
        )
        adapters = {
            "EB_BILL": SyntheticAdapter(snapshot(Stream.EB_BILL, (item,))),
            "TENANT_BILL": SyntheticAdapter(snapshot(Stream.TENANT_BILL, ())),
        }
        recovery_run_id = str(uuid.uuid4())
        recovery_logger = SafeLogger(self.log_root / "recovery", recovery_run_id)
        with StateV2Store(self.config.state_path) as state:
            with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=client):
                recovery = reconcile_dual_stream(
                    self.config, adapters, state, recovery_logger, recovery_run_id,
                )
            invoice = state.invoice(retained_id)
            self.assertIsNotNone(invoice)
            assert invoice is not None
            self.assertEqual((info.byte_size, info.sha256), (invoice["byte_size"], invoice["sha256"]))
            delivered_row = state.delivery_for_invoice(retained_id)
            self.assertIsNotNone(delivered_row)
            assert delivered_row is not None
            delivery_id = delivered_row["delivery_id"]
            prior_drive = state.file_operation_history(
                retained_id, "DRIVE_STAGE", self.config.drive.binding_id, invoice["archive_relpath"],
            )
            self.assertEqual(1, len(prior_drive))

        self.assertEqual(0, recovery.fetch_count)
        self.assertEqual(1, recovery.archive_reused_count)
        self.assertEqual(1, recovery.drive_staged_count)
        self.assertEqual(1, recovery.delivered_count)
        self.assertEqual([], adapters["EB_BILL"].acquire_calls)
        self.assertEqual([], adapters["TENANT_BILL"].acquire_calls)
        self.assertEqual(["POST"], posts)
        recovery_events = [json.loads(line) for line in recovery_logger.log_path.read_text(encoding="utf-8").splitlines()]
        eb_recovery_events = [event for event in recovery_events if event.get("stream") == "EB_BILL"]
        self.assertEqual(recovery_run_id, eb_recovery_events[0]["run_id"])
        self.assertEqual([
            "inventory_started", "inventory_result", "latest_selection", "selection_committed",
            "fetch_decision", "archive_started", "archive_result", "drive_started", "drive_result",
            "delivery_intent", "dispatch_start", "delivery_outcome", "stream_complete",
        ], [event["phase"] for event in eb_recovery_events])
        self.assertEqual(["RECOVER"], [event["status"] for event in eb_recovery_events if event["phase"] == "fetch_decision"])
        self.assertEqual(["LEGACY_MOVE"], [event["status"] for event in eb_recovery_events if event["phase"] == "archive_started"])

        drive_target = self.drive_root / "EB Bill" / "2026-10-01.pdf"
        self.assertEqual(payload, (self.archive / "EB Bill" / "2026-10-01.pdf").read_bytes())
        self.assertFalse(old_archive.exists())
        drive_target.unlink()
        repair_run_id = str(uuid.uuid4())
        repair_logger = SafeLogger(self.log_root / "repair", repair_run_id)
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=client):
            with StateV2Store(self.config.state_path) as state:
                repair = reconcile_dual_stream(self.config, adapters, state, repair_logger, repair_run_id)
                repaired_invoice = state.invoice(retained_id)
                repaired_delivery = state.delivery(delivery_id)
                repaired_history = state.file_operation_history(
                    retained_id, "DRIVE_STAGE", self.config.drive.binding_id, repaired_invoice["archive_relpath"],
                )
        self.assertEqual(0, repair.fetch_count)
        self.assertEqual(1, repair.archive_reused_count)
        self.assertEqual(1, repair.drive_staged_count)
        self.assertEqual(1, repair.delivered_count)
        self.assertEqual([], adapters["EB_BILL"].acquire_calls)
        self.assertEqual([], adapters["TENANT_BILL"].acquire_calls)
        self.assertEqual(["POST"], posts)
        self.assertEqual(delivered_row, repaired_delivery)
        self.assertEqual(2, len(repaired_history))
        retained_history = next(row for row in repaired_history if row["operation_id"] == prior_drive[0]["operation_id"])
        repair_history = [row for row in repaired_history if row["operation_id"] != prior_drive[0]["operation_id"]]
        self.assertEqual(prior_drive[0], retained_history)
        self.assertEqual((1, "COMMITTED"), (len(repair_history), repair_history[0]["state"]))
        self.assertEqual(payload, drive_target.read_bytes())
        repair_events = [json.loads(line) for line in repair_logger.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(event["run_id"] == repair_run_id for event in repair_events))
        eb_repair_events = [event for event in repair_events if event.get("stream") == "EB_BILL"]
        self.assertEqual([
            "inventory_started", "inventory_result", "latest_selection", "selection_committed",
            "fetch_decision", "archive_started", "archive_result", "drive_started", "drive_result",
            "delivery_intent", "delivery_no_send", "stream_complete",
        ], [event["phase"] for event in eb_repair_events])
        self.assertEqual(["REUSE"], [event["status"] for event in repair_events if event["phase"] == "fetch_decision"])
        self.assertEqual(["REPAIRED"], [event["status"] for event in repair_events if event["phase"] == "drive_result"])
        self.assertEqual(["ALREADY_DELIVERED"], [event["status"] for event in repair_events if event["phase"] == "delivery_no_send"])

        repeat_run_id = str(uuid.uuid4())
        repeat_logger = SafeLogger(self.log_root / "repeat", repeat_run_id)
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=client):
            with StateV2Store(self.config.state_path) as state:
                repeat = reconcile_dual_stream(self.config, adapters, state, repeat_logger, repeat_run_id)
                self.assertEqual(retained_id, state.stream(Stream.EB_BILL.value)["watermark_invoice_id"])
                self.assertEqual(delivery_id, state.delivery_for_invoice(retained_id)["delivery_id"])
                self.assertEqual(2, len(state.file_operation_history(
                    retained_id, "DRIVE_STAGE", self.config.drive.binding_id, "EB Bill/2026-10-01.pdf",
                )))
        repeat_events = [json.loads(line) for line in repeat_logger.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(2, repeat.handled_count)  # the second stream has an empty, complete snapshot
        self.assertEqual(0, repeat.fetch_count)
        self.assertTrue(all(event["run_id"] == repeat_run_id for event in repeat_events))
        eb_repeat_events = [event for event in repeat_events if event.get("stream") == "EB_BILL"]
        self.assertEqual([
            "inventory_started", "inventory_result", "latest_selection",
            "fetch_decision", "delivery_no_send", "stream_complete",
        ], [event["phase"] for event in eb_repeat_events])
        self.assertEqual(["NO_FETCH"], [event["status"] for event in repeat_events if event["phase"] == "fetch_decision"])
        self.assertEqual(["ALREADY_DELIVERED"], [event["status"] for event in repeat_events if event["phase"] == "delivery_no_send"])
        self.assertEqual(["POST"], posts)

    def test_migrated_delivered_repair_holds_on_archive_or_drive_authority_drift(self) -> None:
        from dataclasses import replace
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_dual_stream import SyntheticAdapter, snapshot

        original_drive = self.config.drive
        for day, case in enumerate(("missing_archive", "conflicting_archive", "changed_binding"), start=1):
            with self.subTest(case=case):
                self.config = replace(self.config, drive=original_drive)

                invoice_date = f"2026-10-0{day}"
                item, _old_archive, payload, _info, invoice_id, delivered_row, drive_history, relpath = (
                    self.seed_migrated_delivered_latest(label=f"repair-hold-{case}", invoice_date=invoice_date)
                )
                archive_target = self.archive / relpath
                drive_target = self.drive_root / relpath
                if case == "missing_archive":
                    archive_target.unlink()
                elif case == "conflicting_archive":
                    archive_target.write_bytes(b"corrupt canonical archive")
                else:
                    drive_target.unlink()
                    self.config = replace(
                        self.config,
                        drive=replace(original_drive, binding_id="CHANGED_SYNTHETIC_BINDING"),
                    )

                adapters = {
                    "EB_BILL": SyntheticAdapter(snapshot(Stream.EB_BILL, (item,)), {item.source_filename: payload}),
                    "TENANT_BILL": SyntheticAdapter(snapshot(Stream.TENANT_BILL, ())),
                }
                email_calls: list[str] = []

                class MustNotSend:
                    def deliver(inner_self, *args, **kwargs):
                        email_calls.append("called")
                        raise AssertionError("authority drift reached email")

                run_id = str(uuid.uuid4())
                with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=MustNotSend()):
                    with StateV2Store(self.config.state_path) as state:
                        summary = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), run_id)
                        invoice_after = state.invoice(invoice_id)
                        delivery_after = state.delivery(delivered_row["delivery_id"])
                        history_after = state.file_operation_history(
                            invoice_id, "DRIVE_STAGE", original_drive.binding_id, relpath,
                        )

                expected_status = "DRIVE_FAILURE" if case == "changed_binding" else "ARCHIVE_RECOVERY_HOLD"
                self.assertEqual(expected_status, summary.stream_results[0]["status"])
                self.assertEqual(0, summary.fetch_count)
                self.assertEqual([], adapters["EB_BILL"].acquire_calls)
                self.assertEqual([], adapters["TENANT_BILL"].acquire_calls)
                self.assertEqual([], email_calls)
                self.assertEqual(delivered_row, delivery_after)
                self.assertEqual(drive_history, history_after)
                self.assertEqual("COMMITTED", invoice_after["archive_state"])
                if case == "missing_archive":
                    self.assertFalse(archive_target.exists())
                elif case == "conflicting_archive":
                    self.assertEqual(b"corrupt canonical archive", archive_target.read_bytes())
                else:
                    self.assertFalse(drive_target.exists())
                self.config = replace(self.config, drive=original_drive)

    def test_delivered_replay_holds_on_drive_target_junction_without_fetch_or_send(self) -> None:
        import os
        import subprocess
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_dual_stream import SyntheticAdapter, snapshot

        item, _old_archive, _payload, _info, invoice_id, delivery_row, drive_history, relpath = (
            self.seed_migrated_delivered_latest(label="delivered-drive-junction")
        )
        real_drive_dir = self.root / "drive-target-data"
        drive_parent = self.drive_root / "EB Bill"
        drive_parent.rename(real_drive_dir)
        environment = os.environ.copy()
        environment["CODEX_TEST_JUNCTION_PATH"] = str(drive_parent)
        environment["CODEX_TEST_JUNCTION_TARGET"] = str(real_drive_dir)
        created = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "New-Item -ItemType Junction -Path $env:CODEX_TEST_JUNCTION_PATH -Target $env:CODEX_TEST_JUNCTION_TARGET -ErrorAction Stop | Out-Null"],
            capture_output=True, text=True, env=environment, check=False,
        )
        if created.returncode != 0:
            self.skipTest("directory junctions are unavailable in this Windows test environment")

        adapters = {
            "EB_BILL": SyntheticAdapter(snapshot(Stream.EB_BILL, (item,))),
            "TENANT_BILL": SyntheticAdapter(snapshot(Stream.TENANT_BILL, ())),
        }
        email_calls: list[str] = []

        class MustNotSend:
            def deliver(inner_self, *args, **kwargs):
                email_calls.append("called")
                raise AssertionError("delivered replay reached email through a Drive junction")

        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=MustNotSend()):
            with StateV2Store(self.config.state_path) as state:
                summary = reconcile_dual_stream(
                    self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()),
                )
                invoice_after = state.invoice(invoice_id)
                delivery_after = state.delivery(delivery_row["delivery_id"])
                history_after = state.file_operation_history(
                    invoice_id, "DRIVE_STAGE", self.config.drive.binding_id, relpath,
                )

        self.assertEqual("DRIVE_FAILURE", summary.stream_results[0]["status"])
        self.assertEqual(0, summary.fetch_count)
        self.assertEqual([], adapters["EB_BILL"].acquire_calls)
        self.assertEqual([], adapters["TENANT_BILL"].acquire_calls)
        self.assertEqual([], email_calls)
        self.assertEqual(delivery_row, delivery_after)
        self.assertEqual(drive_history, history_after)
        self.assertEqual("COMMITTED", invoice_after["archive_state"])
        self.assertEqual("DRIVE_STAGED", invoice_after["drive_state"])
        self.assertTrue((real_drive_dir / Path(relpath).name).is_file())

    def test_lifecycle_interruption_restart_matrix_preserves_last_stage_evidence(self) -> None:
        from contextlib import ExitStack
        from dataclasses import replace
        from energygrid_bill_downloader.drive import DriveStager
        from energygrid_bill_downloader import drive as drive_module
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_dual_stream import SyntheticAdapter, create_v2_database, snapshot

        cases = (
            ("selection_before", ("latest_selection", "SELECTED")),
            ("selection_after", ("latest_selection", "SELECTED")),
            ("fetch_validation_before", ("fetch_completed", "SUCCESS")),
            ("fetch_validation_after", ("fetch_completed", "SUCCESS")),
            ("archive_prepare_before", ("fetch_completed", "SUCCESS")),
            ("archive_prepare_after", ("fetch_completed", "SUCCESS")),
            ("archive_publish_before", ("fetch_completed", "SUCCESS")),
            ("archive_publish_after", ("fetch_completed", "SUCCESS")),
            ("archive_commit_before", ("fetch_completed", "SUCCESS")),
            ("archive_commit_after", ("fetch_completed", "SUCCESS")),
            ("drive_copy_before", ("drive_started", "STARTED")),
            ("drive_copy_after", ("drive_started", "STARTED")),
            ("drive_commit_after", ("drive_started", "STARTED")),
            ("drive_stage_before", ("archive_result", "COMMITTED")),
            ("drive_stage_after", ("drive_result", "STAGED")),
            ("terminal_observation_before", ("drive_result", "STAGED")),
            ("terminal_observation_after", ("stream_complete", "DELIVERED")),
        )
        original_config = self.config

        def stop_once(original, *, after: bool, matches=lambda _args, _kwargs: True):
            triggered = False

            def call(*args, **kwargs):
                nonlocal triggered
                if not triggered and matches(args, kwargs):
                    triggered = True
                    if after:
                        original(*args, **kwargs)
                    raise KeyboardInterrupt("synthetic lifecycle interruption")
                return original(*args, **kwargs)

            return call

        for case, expected_last in cases:
            with self.subTest(boundary=case):
                phase_root = self.root / f"r20-{case}"
                archive = phase_root / "archive"
                drive_root = phase_root / "drive"
                temp_root = phase_root / "temp"
                log_root = phase_root / "logs"
                state_path = phase_root / "state" / "state.sqlite3"
                archive.mkdir(parents=True)
                drive_root.mkdir()
                state_path.parent.mkdir()
                create_v2_database(state_path)
                self.config = replace(
                    original_config,
                    archive_root=archive,
                    state_path=state_path,
                    temp_root=temp_root,
                    log_root=log_root,
                    drive=replace(original_config.drive, root=drive_root),
                )
                self.config.preflight()
                adapters = self.adapters()
                adapters["TENANT_BILL"] = SyntheticAdapter(snapshot(Stream.TENANT_BILL, ()))
                calls: list[str] = []
                logger = RecordingLogger()

                class InterruptAtTerminal(RecordingLogger):
                    def event(inner_self, phase, status=None, **fields):
                        if phase == "stream_complete" and fields.get("stream") == "EB_BILL":
                            if case == "terminal_observation_after":
                                super(InterruptAtTerminal, inner_self).event(phase, status=status, **fields)
                            raise KeyboardInterrupt("synthetic terminal observation interruption")
                        super(InterruptAtTerminal, inner_self).event(phase, status=status, **fields)

                if case.startswith("terminal_observation_"):
                    logger = InterruptAtTerminal()

                with StateV2Store(state_path) as state:
                    with ExitStack() as stack:
                        if case.startswith("selection_"):
                            stack.enter_context(mock.patch.object(
                                state, "accept_latest",
                                new=stop_once(state.accept_latest, after=case.endswith("after")),
                            ))
                        elif case.startswith("fetch_validation_"):
                            original = reconcile_module.validate_pdf
                            stack.enter_context(mock.patch.object(
                                reconcile_module,
                                "validate_pdf",
                                new=stop_once(
                                    original,
                                    after=case.endswith("after"),
                                    matches=lambda args, _kwargs: Path(args[0]).is_relative_to(temp_root),
                                ),
                            ))
                        elif case.startswith("archive_prepare_"):
                            stack.enter_context(mock.patch.object(
                                state,
                                "start_file_operation",
                                new=stop_once(
                                    state.start_file_operation,
                                    after=case.endswith("after"),
                                    matches=lambda _args, kwargs: kwargs.get("kind") == "ARCHIVE_PUBLISH",
                                ),
                            ))
                            if case.endswith("after"):
                                stack.enter_context(mock.patch.object(reconcile_module, "cleanup_run_directory"))
                        elif case.startswith("archive_publish_"):
                            stack.enter_context(mock.patch.object(
                                reconcile_module,
                                "publish_no_replace",
                                new=stop_once(reconcile_module.publish_no_replace, after=case.endswith("after")),
                            ))
                        elif case.startswith("archive_commit_"):
                            stack.enter_context(mock.patch.object(
                                state,
                                "complete_file_operation",
                                new=stop_once(state.complete_file_operation, after=case.endswith("after")),
                            ))
                        elif case == "drive_commit_after":
                            stack.enter_context(mock.patch.object(
                                state,
                                "update_invoice_file_state",
                                new=stop_once(
                                    state.update_invoice_file_state,
                                    after=False,
                                    matches=lambda _args, kwargs: kwargs.get("drive_state") == "DRIVE_STAGED",
                                ),
                            ))
                        elif case.startswith("drive_copy_"):
                            stack.enter_context(mock.patch.object(
                                drive_module,
                                "_copy_exclusive",
                                new=stop_once(drive_module._copy_exclusive, after=case.endswith("after")),
                            ))
                        elif case.startswith("drive_stage_"):
                            original = DriveStager.stage
                            triggered = False

                            def stage_and_interrupt(stager, *args, **kwargs):
                                nonlocal triggered
                                if not triggered:
                                    triggered = True
                                    if case.endswith("after"):
                                        original(stager, *args, **kwargs)
                                    raise KeyboardInterrupt("synthetic Drive stage interruption")
                                return original(stager, *args, **kwargs)

                            stack.enter_context(mock.patch.object(DriveStager, "stage", new=stage_and_interrupt))

                        stack.enter_context(mock.patch(
                            "energygrid_bill_downloader.delivery.DeliveryClient",
                            return_value=self.fake_delivery(calls),
                        ))
                        with self.assertRaises(KeyboardInterrupt):
                            reconcile_dual_stream(self.config, adapters, state, logger, str(uuid.uuid4()))

                eb_events = [event for event in logger.events if event[2].get("stream") == "EB_BILL"]
                self.assertTrue(eb_events)
                self.assertEqual(expected_last, eb_events[-1][:2])
                if case in {"selection_after", "archive_commit_after", "drive_commit_after", "drive_stage_after"}:
                    from energygrid_bill_downloader.state import migrate_state_database
                    self.assertEqual(
                        {"status": "RESUMABLE_V2", "legacy_rows": 0},
                        migrate_state_database(state_path, apply=True),
                    )

                restart_logger = RecordingLogger()
                with mock.patch(
                    "energygrid_bill_downloader.delivery.DeliveryClient",
                    return_value=self.fake_delivery(calls),
                ):
                    with StateV2Store(state_path) as state:
                        restarted = reconcile_dual_stream(
                            self.config, adapters, state, restart_logger, str(uuid.uuid4()),
                        )
                        invoice = state.resolve_latest_candidate(adapters["EB_BILL"]._inventory.candidates[-1])
                        self.assertIsNotNone(invoice)
                        archive_history = state.file_operation_history(
                            invoice["invoice_id"], "ARCHIVE_PUBLISH", None, invoice["archive_relpath"],
                        )
                        drive_history = state.file_operation_history(
                            invoice["invoice_id"], "DRIVE_STAGE", self.config.drive.binding_id, invoice["archive_relpath"],
                        )
                        drive_history.extend(state.active_file_operations(invoice["invoice_id"], {"DRIVE_STAGE"}))
                        delivery_row = state.delivery_for_invoice(invoice["invoice_id"])

                self.assertEqual(1, len(archive_history))
                self.assertEqual("COMMITTED", archive_history[0]["state"])
                self.assertEqual(2 if case in {
                    "fetch_validation_before", "fetch_validation_after", "archive_prepare_before",
                } else 1, len(adapters["EB_BILL"].acquire_calls))
                if case == "drive_copy_before":
                    self.assertEqual("DRIVE_FAILURE", restarted.stream_results[0]["status"])
                    self.assertEqual((1, "HOLD"), (len(drive_history), drive_history[0]["state"]))
                    self.assertIsNone(delivery_row)
                    self.assertEqual([], calls)
                else:
                    expected_status = "ALREADY_HANDLED" if case.startswith("terminal_observation_") else "DELIVERED"
                    self.assertEqual(expected_status, restarted.stream_results[0]["status"])
                    self.assertEqual((1, "COMMITTED"), (len(drive_history), drive_history[0]["state"]))
                    self.assertEqual("DELIVERED", delivery_row["state"])
                    self.assertEqual(["EB_BILL"], calls)
                self.config = original_config

    def test_archive_recovery_hold_is_stream_local_while_peer_completes(self) -> None:
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_delivery import synthetic_pdf
        adapters = self.adapters()
        eb_item = adapters["EB_BILL"]._inventory.candidates[-1]
        with StateV2Store(self.state_path) as state:
            eb_invoice_id = state.accept_latest(eb_item, str(uuid.uuid4()), "2026-10-02T00:00:00+00:00")
            eb_invoice = state.invoice(eb_invoice_id)
            source_dir = self.temp_root / "archive-recovery-both"
            source_dir.mkdir(parents=True)
            source = source_dir / "selected.pdf"
            source_payload = synthetic_pdf(b"prepared source")
            source.write_bytes(source_payload)
            source_info = validate_pdf(source)
            target = self.archive / eb_invoice["archive_relpath"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(synthetic_pdf(b"conflicting archive target"))
            operation = state.start_file_operation(
                operation_id=str(uuid.uuid4()), invoice_id=eb_invoice_id, kind="ARCHIVE_PUBLISH",
                private_path_ref=str(source), target_relpath=eb_invoice["archive_relpath"], binding_id=None,
                info=source_info, source_role="SOURCE_ACQUISITION", run_id=str(uuid.uuid4()),
                timestamp="2026-10-02T00:00:01+00:00",
            )
            tenant_payload = synthetic_pdf(b"tenant latest")
            adapters["TENANT_BILL"]._files["tenant-latest.pdf"] = tenant_payload
            delivery_calls: list[str] = []
            logger = RecordingLogger()
            with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=self.fake_delivery(delivery_calls)):
                summary = reconcile_dual_stream(self.config, adapters, state, logger, str(uuid.uuid4()))
            self.assertEqual("HOLD", state.active_file_operations(eb_invoice_id, {"ARCHIVE_PUBLISH"})[0]["state"])
            self.assertEqual(operation["operation_id"], state.active_file_operations(eb_invoice_id, {"ARCHIVE_PUBLISH"})[0]["operation_id"])
            tenant_invoice = state.resolve_latest_candidate(adapters["TENANT_BILL"]._inventory.candidates[-1])
            self.assertIsNotNone(tenant_invoice)
            self.assertEqual("DELIVERED", state.delivery_for_invoice(tenant_invoice["invoice_id"])["state"])

        self.assertEqual("ARCHIVE_RECOVERY_HOLD", summary.stream_results[0]["status"])
        self.assertEqual("DELIVERED", summary.stream_results[1]["status"])
        self.assertEqual(1, summary.fetch_count)
        self.assertEqual([], adapters["EB_BILL"].acquire_calls)
        self.assertEqual(["tenant-latest.pdf"], adapters["TENANT_BILL"].acquire_calls)
        self.assertEqual(["TENANT_BILL"], delivery_calls)
        self.assertEqual(source_payload, source.read_bytes())
        self.assertNotEqual(source_payload, target.read_bytes())
        eb_events = [event for event in logger.events if event[2].get("stream") == "EB_BILL"]
        self.assertEqual("stream_complete", eb_events[-1][0])
        self.assertEqual("ARCHIVE_RECOVERY_HOLD", eb_events[-1][1])
        self.assertRegex(eb_events[-1][2]["support_ref"], r"^EG_[A-Z0-9_]+$")

    def test_shared_state_authority_error_aborts_before_other_stream_effects(self) -> None:
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store

        adapters = self.adapters()
        fetches_before = {name: list(adapter.acquire_calls) for name, adapter in adapters.items()}
        with StateV2Store(self.state_path) as state:
            with mock.patch.object(state, "accept_latest", side_effect=StateError("synthetic shared DB authority failure")):
                with self.assertRaises(StateError):
                    reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()))
            self.assertEqual(0, state.connection.execute("SELECT COUNT(*) FROM energygrid_invoice_v2").fetchone()[0])
            self.assertEqual(0, state.connection.execute("SELECT COUNT(*) FROM energygrid_file_operation_v2").fetchone()[0])
            self.assertEqual(0, state.connection.execute("SELECT COUNT(*) FROM energygrid_delivery_v1").fetchone()[0])
        self.assertEqual(fetches_before, {name: list(adapter.acquire_calls) for name, adapter in adapters.items()})
        self.assertFalse(any(self.archive.rglob("*.pdf")))
        self.assertFalse(any(self.drive_root.rglob("*.pdf")))

    def test_latest_only_per_stream_and_fully_handled_rerun_has_no_effects(self) -> None:
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store

        adapters = self.adapters()
        first_calls: list[str] = []
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=self.fake_delivery(first_calls)):
            with StateV2Store(self.state_path) as state:
                first = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()))
        self.assertEqual(2, first.downloaded_count)
        self.assertEqual(2, first.delivered_count)
        self.assertEqual(["EB_BILL", "TENANT_BILL"], first_calls)
        self.assertEqual(["eb-latest.pdf"], adapters["EB_BILL"].acquire_calls)
        self.assertEqual(["tenant-latest.pdf"], adapters["TENANT_BILL"].acquire_calls)
        archive_paths = sorted(path.relative_to(self.archive).as_posix() for path in self.archive.rglob("*.pdf"))
        drive_paths = sorted(path.relative_to(self.drive_root).as_posix() for path in self.drive_root.rglob("*.pdf"))
        self.assertEqual(["EB Bill/2026-10-01.pdf", "Tenant Bill/2026-10-02.pdf"], archive_paths)
        self.assertEqual(archive_paths, drive_paths)

        second_calls: list[str] = []
        class MustNotSend:
            def deliver(inner_self, *args):
                second_calls.append("called")
                raise AssertionError("fully handled invoice was dispatched again")

        adapters = self.adapters()
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=MustNotSend()):
            with StateV2Store(self.state_path) as state:
                second = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()))
        self.assertEqual(2, second.handled_count)
        self.assertEqual(2, second.archive_reused_count)
        self.assertEqual([], second_calls)
        self.assertEqual([], adapters["EB_BILL"].acquire_calls)
        self.assertEqual([], adapters["TENANT_BILL"].acquire_calls)

    def test_both_stream_snapshots_finish_before_any_acquisition_or_delivery(self) -> None:
        from energygrid_bill_downloader.errors import SourceContractError
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store

        adapters = self.adapters()
        events: list[str] = []
        for stream_name, adapter in adapters.items():
            original_inventory = adapter.inventory
            original_acquire = adapter.acquire

            def inventory(safety_ceiling, *, name=stream_name, original=original_inventory):
                events.append("inventory:" + name)
                return original(safety_ceiling)

            def acquire(item, destination, *, name=stream_name, original=original_acquire):
                events.append("acquire:" + name)
                return original(item, destination)

            adapter.inventory = inventory
            adapter.acquire = acquire

        original_tenant_inventory = adapters["TENANT_BILL"].inventory

        def failed_tenant_inventory(safety_ceiling):
            original_tenant_inventory(safety_ceiling)
            raise SourceContractError("EG_SYNTHETIC_INVENTORY_FAILURE")

        adapters["TENANT_BILL"].inventory = failed_tenant_inventory
        delivered: list[str] = []

        class RecordingDelivery:
            def deliver(inner_self, state, invoice, archive_path, run_id, *, logger=None):
                events.append("delivery:" + invoice["stream"])
                delivered.append(invoice["stream"])
                return self.fake_delivery([]).deliver(state, invoice, archive_path, run_id)

        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=RecordingDelivery()):
            with StateV2Store(self.state_path) as state:
                summary = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()))

        self.assertEqual(["inventory:EB_BILL", "inventory:TENANT_BILL"], events[:2])
        self.assertGreater(events.index("acquire:EB_BILL"), events.index("inventory:TENANT_BILL"))
        self.assertEqual(["EB_BILL"], delivered)
        self.assertEqual("DELIVERED", summary.stream_results[0]["status"])
        self.assertEqual("SOURCE_FAILURE", summary.stream_results[1]["status"])

    def test_tied_latest_holds_only_that_stream_and_other_stream_continues(self) -> None:
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store

        adapters = self.adapters(tie_eb=True)
        calls: list[str] = []
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=self.fake_delivery(calls)):
            with StateV2Store(self.state_path) as state:
                summary = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()))
        self.assertEqual("LATEST_AMBIGUOUS", summary.stream_results[0]["status"])
        self.assertEqual("DELIVERED", summary.stream_results[1]["status"])
        self.assertEqual([], adapters["EB_BILL"].acquire_calls)
        self.assertEqual(["tenant-latest.pdf"], adapters["TENANT_BILL"].acquire_calls)
        self.assertEqual(["TENANT_BILL"], calls)

    def test_unresolved_legacy_identity_blocks_duplicate_fetch_only_in_its_stream(self) -> None:
        from dataclasses import replace
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_dual_stream import candidate, create_v2_database

        original_config = self.config
        cases = (
            ("casefold", "eb-latest.pdf", "EB-LATEST.PDF"),
            ("unicode_nfc", "\u00c9B-Latest.pdf", "E\u0301B-LATEST.PDF"),
        )
        for label, source_filename, legacy_key in cases:
            with self.subTest(normalization=label):
                case_root = self.root / f"r03-{label}"
                archive = case_root / "archive"
                drive_root = case_root / "drive"
                state_path = case_root / "state.sqlite3"
                archive.mkdir(parents=True)
                drive_root.mkdir()
                create_v2_database(state_path)
                self.config = replace(
                    original_config,
                    archive_root=archive,
                    state_path=state_path,
                    temp_root=case_root / "temp",
                    log_root=case_root / "logs",
                    drive=replace(original_config.drive, root=drive_root),
                )
                self.config.preflight()
                latest = candidate(Stream.EB_BILL, name=source_filename, invoice_date="2026-10-01")
                adapters = self.adapters(eb_candidates=(latest,))
                calls: list[str] = []
                with StateV2Store(state_path) as state:
                    self.register_unresolved_legacy_row(state, latest, legacy_key=legacy_key)
                    with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=self.fake_delivery(calls)):
                        summary = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()))
                    self.assertEqual("EG_LEGACY_IDENTITY_UNRESOLVED", summary.stream_results[0]["support_ref"])
                    self.assertEqual("LATEST_IDENTITY_CONFLICT", summary.stream_results[0]["status"])
                    self.assertEqual(0, state._conn().execute(
                        "SELECT COUNT(*) FROM energygrid_invoice_v2 WHERE classification='CLASSIFIED' AND source_invoice_key=?",
                        (latest.source_invoice_key,),
                    ).fetchone()[0])
                    self.assertEqual("DELIVERED", summary.stream_results[1]["status"])
                    listed = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()), list_only=True)
                    self.assertEqual("LATEST_IDENTITY_CONFLICT", listed.stream_results[0]["status"])
                    self.assertEqual(0, listed.stream_results[0]["handled_count"])
                    self.assertEqual(1, listed.stream_results[1]["handled_count"])
                    self.assertEqual(0, listed.fetch_count)
                self.assertEqual([], adapters["EB_BILL"].acquire_calls)
                self.assertEqual(["tenant-latest.pdf"], adapters["TENANT_BILL"].acquire_calls)
                self.assertEqual(["TENANT_BILL"], calls)
                self.assertFalse((archive / "EB Bill" / "2026-10-01.pdf").exists())
                self.assertEqual(ACTION_REQUIRED, summary.status)
                self.assertEqual(20, summary.exit_code)
        self.config = original_config

    def run_chronology_drift_case(self, drift_stream: str) -> None:
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_delivery import synthetic_pdf
        from fixtures.synthetic_dual_stream import SyntheticAdapter, candidate, snapshot

        initial = self.adapters()
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=self.fake_delivery([])):
            with StateV2Store(self.state_path) as state:
                first = reconcile_dual_stream(self.config, initial, state, RecordingLogger(), str(uuid.uuid4()))
                self.assertEqual(0, first.exit_code)
                old_drift_watermark = state.stream(drift_stream)["watermark_day"]

        if drift_stream == "EB_BILL":
            drift = candidate(Stream.EB_BILL, name="eb-latest.pdf", invoice_date="2026-10-03")
            peer = candidate(Stream.TENANT_BILL, name="tenant-next.pdf", invoice_date="2026-10-04")
            snapshot_items = (drift,)
            peer_items = (peer,)
        else:
            drift = candidate(Stream.TENANT_BILL, name="tenant-latest.pdf", invoice_date="2026-10-03")
            peer = candidate(Stream.EB_BILL, name="eb-next.pdf", invoice_date="2026-10-04")
            snapshot_items = (drift,)
            peer_items = (peer,)
        drift_stream_enum = Stream(drift_stream)
        peer_stream_enum = Stream.TENANT_BILL if drift_stream == "EB_BILL" else Stream.EB_BILL
        new_adapters = {
            drift_stream: SyntheticAdapter(snapshot(drift_stream_enum, snapshot_items), {drift.source_filename: synthetic_pdf(b"drift")}),
            peer_stream_enum.value: SyntheticAdapter(snapshot(peer_stream_enum, peer_items), {peer.source_filename: synthetic_pdf(b"peer")}),
        }
        calls: list[str] = []
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=self.fake_delivery(calls)):
            with StateV2Store(self.state_path) as state:
                result = reconcile_dual_stream(self.config, new_adapters, state, RecordingLogger(), str(uuid.uuid4()))
                drift_detail = next(item for item in result.stream_results if item["stream"] == drift_stream)
                peer_detail = next(item for item in result.stream_results if item["stream"] == peer_stream_enum.value)
                self.assertEqual("LATEST_IDENTITY_CONFLICT", drift_detail["status"])
                self.assertEqual("EG_LATEST_IDENTITY_FACTS_CHANGED", drift_detail["support_ref"])
                self.assertEqual("DELIVERED", peer_detail["status"])
                self.assertEqual(peer.day_ordinal, state.stream(peer_stream_enum.value)["watermark_day"])
                self.assertEqual(old_drift_watermark, state.stream(drift_stream)["watermark_day"])
        self.assertEqual([], new_adapters[drift_stream].acquire_calls)
        self.assertEqual([peer.source_filename], new_adapters[peer_stream_enum.value].acquire_calls)
        self.assertEqual([peer_stream_enum.value], calls)
        self.assertEqual(ACTION_REQUIRED, result.status)
        self.assertEqual(20, result.exit_code)
        with StateV2Store(self.state_path) as state:
            listed = reconcile_dual_stream(self.config, new_adapters, state, RecordingLogger(), str(uuid.uuid4()), list_only=True)
            drift_listed = next(row for row in listed.stream_results if row["stream"] == drift_stream)
            self.assertEqual("LATEST_IDENTITY_CONFLICT", drift_listed["status"])
            self.assertEqual(0, drift_listed["handled_count"])
            self.assertEqual(0, listed.fetch_count)
        self.assertEqual([], new_adapters[drift_stream].acquire_calls)
        self.assertEqual([peer.source_filename], new_adapters[peer_stream_enum.value].acquire_calls)

    def test_candidate_drift_holds_eb_before_handled_shortcut_and_tenant_continues(self) -> None:
        self.run_chronology_drift_case("EB_BILL")

    def test_candidate_drift_holds_tenant_before_handled_shortcut_and_eb_continues(self) -> None:
        self.run_chronology_drift_case("TENANT_BILL")

    def run_evidence_binding_drift_case(self, drift_stream: str) -> None:
        from energygrid_bill_downloader.invoice import DATE_PROFILE_ISO_V1, Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_delivery import synthetic_pdf
        from fixtures.synthetic_dual_stream import SyntheticAdapter, SYNTHETIC_NAMESPACE, candidate, snapshot

        initial = self.adapters()
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=self.fake_delivery([])):
            with StateV2Store(self.state_path) as state:
                self.assertEqual(0, reconcile_dual_stream(
                    self.config, initial, state, RecordingLogger(), str(uuid.uuid4()),
                ).exit_code)

        if drift_stream == "EB_BILL":
            stream = Stream.EB_BILL
            current_name = "eb-latest.pdf"
            peer_stream = Stream.TENANT_BILL
            peer_name = "tenant-next.pdf"
        else:
            stream = Stream.TENANT_BILL
            current_name = "tenant-latest.pdf"
            peer_stream = Stream.EB_BILL
            peer_name = "eb-next.pdf"
        drift = candidate(stream, name=current_name, invoice_date="2026-10-02" if stream is Stream.TENANT_BILL else "2026-10-01")
        from energygrid_bill_downloader.invoice import Candidate
        drift = Candidate.create(
            stream=stream, source_namespace=SYNTHETIC_NAMESPACE, source_filename=current_name,
            raw_date=drift.raw_date, date_profile=DATE_PROFILE_ISO_V1,
            evidence_ref="EG_CHANGED_SOURCE_EVIDENCE", fetch_handle={"synthetic": True},
        )
        peer = candidate(peer_stream, name=peer_name, invoice_date="2026-10-04")
        adapters = {
            drift_stream: SyntheticAdapter(snapshot(stream, (drift,)), {current_name: synthetic_pdf(b"drifted evidence")}),
            peer_stream.value: SyntheticAdapter(snapshot(peer_stream, (peer,)), {peer_name: synthetic_pdf(b"peer")}),
        }
        calls: list[str] = []
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=self.fake_delivery(calls)):
            with StateV2Store(self.state_path) as state:
                result = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()))
                drift_detail = next(row for row in result.stream_results if row["stream"] == drift_stream)
                peer_detail = next(row for row in result.stream_results if row["stream"] == peer_stream.value)
                self.assertEqual(("LATEST_IDENTITY_CONFLICT", "EG_LATEST_IDENTITY_FACTS_CHANGED"), (
                    drift_detail["status"], drift_detail["support_ref"],
                ))
                self.assertEqual("DELIVERED", peer_detail["status"])
        self.assertEqual([], adapters[drift_stream].acquire_calls)
        self.assertEqual([peer_name], adapters[peer_stream.value].acquire_calls)
        self.assertEqual([peer_stream.value], calls)
        self.assertEqual(ACTION_REQUIRED, result.status)
        self.assertEqual(20, result.exit_code)

    def test_evidence_binding_drift_holds_eb_and_tenant_completes(self) -> None:
        self.run_evidence_binding_drift_case("EB_BILL")

    def test_evidence_binding_drift_holds_tenant_and_eb_completes(self) -> None:
        self.run_evidence_binding_drift_case("TENANT_BILL")

    def test_dual_summary_mixes_handled_and_new_delivery_with_exact_counts(self) -> None:
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_delivery import synthetic_pdf
        from fixtures.synthetic_dual_stream import SyntheticAdapter, candidate, snapshot

        first_adapters = self.adapters()
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=self.fake_delivery([])):
            with StateV2Store(self.state_path) as state:
                self.assertEqual(0, reconcile_dual_stream(
                    self.config, first_adapters, state, RecordingLogger(), str(uuid.uuid4()),
                ).exit_code)
        handled = candidate(Stream.EB_BILL, name="eb-latest.pdf", invoice_date="2026-10-01")
        new = candidate(Stream.TENANT_BILL, name="tenant-next.pdf", invoice_date="2026-10-04")
        adapters = {
            "EB_BILL": SyntheticAdapter(snapshot(Stream.EB_BILL, (handled,))),
            "TENANT_BILL": SyntheticAdapter(snapshot(Stream.TENANT_BILL, (new,)), {new.source_filename: synthetic_pdf(b"tenant next")}),
        }
        calls: list[str] = []
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=self.fake_delivery(calls)):
            with StateV2Store(self.state_path) as state:
                summary = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()))
        self.assertEqual((0, "DOWNLOADED"), (summary.exit_code, summary.status))
        self.assertEqual((1, 1, 2, 2), (
            summary.fetch_count, summary.downloaded_count, summary.delivered_count, summary.handled_count,
        ))
        self.assertEqual(["ALREADY_HANDLED", "DELIVERED"], [row["status"] for row in summary.stream_results])
        self.assertEqual([], adapters["EB_BILL"].acquire_calls)
        self.assertEqual([new.source_filename], adapters["TENANT_BILL"].acquire_calls)
        self.assertEqual(["TENANT_BILL"], calls)

    def test_dual_summary_reports_both_holds_and_mixed_retryability(self) -> None:
        from energygrid_bill_downloader.errors import DownloadError, SourceContractError, SourceTransportError
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store
        from fixtures.synthetic_dual_stream import SyntheticAdapter, candidate, snapshot

        eb_candidates = (
            candidate(Stream.EB_BILL, name="eb-tie-a.pdf", invoice_date="2026-10-01"),
            candidate(Stream.EB_BILL, name="eb-tie-b.pdf", invoice_date="2026-10-01"),
        )
        tenant_candidates = (
            candidate(Stream.TENANT_BILL, name="tenant-tie-a.pdf", invoice_date="2026-10-02"),
            candidate(Stream.TENANT_BILL, name="tenant-tie-b.pdf", invoice_date="2026-10-02"),
        )
        both_held = {
            "EB_BILL": SyntheticAdapter(snapshot(Stream.EB_BILL, eb_candidates)),
            "TENANT_BILL": SyntheticAdapter(snapshot(Stream.TENANT_BILL, tenant_candidates)),
        }
        with StateV2Store(self.state_path) as state:
            tied = reconcile_dual_stream(self.config, both_held, state, RecordingLogger(), str(uuid.uuid4()))
        self.assertEqual(["LATEST_AMBIGUOUS", "LATEST_AMBIGUOUS"], [row["status"] for row in tied.stream_results])
        self.assertEqual(20, tied.exit_code)
        self.assertEqual(0, tied.fetch_count)

        failed = self.adapters()
        failed["EB_BILL"].inventory = lambda _ceiling: (_ for _ in ()).throw(SourceTransportError("synthetic retryable"))
        failed["TENANT_BILL"].inventory = lambda _ceiling: (_ for _ in ()).throw(SourceContractError("EG_SYNTHETIC_CONTRACT_FAILURE"))
        with StateV2Store(self.state_path) as state:
            mixed = reconcile_dual_stream(self.config, failed, state, RecordingLogger(), str(uuid.uuid4()))
        self.assertEqual(["SOURCE_FAILURE", "SOURCE_FAILURE"], [row["status"] for row in mixed.stream_results])
        self.assertEqual(20, mixed.exit_code)
        self.assertEqual(2, mixed.failure_count)

    def test_prepared_archive_recovery_matrix_uses_only_proven_source_or_target(self) -> None:
        import sqlite3
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import _ensure_archive_available
        from energygrid_bill_downloader.state import StateV2Store, StreamStateConflictError
        from fixtures.synthetic_delivery import synthetic_pdf
        from fixtures.synthetic_dual_stream import candidate

        cases = (
            "source_only", "target_only", "both", "both_different_bytes", "neither", "wrong_source_bytes", "wrong_target_bytes",
            "wrong_target_path", "wrong_binding", "wrong_role", "wrong_hash", "wrong_size", "wrong_source_root",
        )
        with StateV2Store(self.state_path) as state:
            for day, label in enumerate(cases, start=1):
                date = f"2026-11-{day:02d}"
                item = candidate(Stream.EB_BILL, name=f"archive-{label}.pdf", invoice_date=date)
                invoice_id = state.accept_latest(item, "00000000-0000-0000-0000-000000000001", f"{date}T00:00:00+00:00")
                invoice = state.invoice(invoice_id)
                payload = synthetic_pdf(label.encode())
                payload_path = self.root / f"{label}.pdf"
                payload_path.write_bytes(payload)
                info = validate_pdf(payload_path)
                source_dir = self.temp_root / f"archive-{label}"
                source_dir.mkdir(parents=True, exist_ok=True)
                source = source_dir / "selected.pdf"
                private_path = self.root / "outside-temp" / f"{label}.pdf" if label == "wrong_source_root" else source
                target = self.archive / invoice["archive_relpath"]
                target.parent.mkdir(parents=True, exist_ok=True)
                operation_relpath = invoice["archive_relpath"] + ".wrong" if label == "wrong_target_path" else invoice["archive_relpath"]
                source_role = "WRONG_ROLE" if label == "wrong_role" else "SOURCE_ACQUISITION"
                binding_id = "WRONG_ARCHIVE_BINDING" if label == "wrong_binding" else None
                expected = info
                if label == "wrong_hash":
                    expected = FileInfo(info.byte_size, "0" * 64)
                elif label == "wrong_size":
                    expected = FileInfo(info.byte_size + 1, info.sha256)
                operation = state.start_file_operation(
                    operation_id=str(uuid.uuid4()), invoice_id=invoice_id, kind="ARCHIVE_PUBLISH",
                    private_path_ref=str(private_path), target_relpath=operation_relpath, binding_id=binding_id,
                    info=expected, source_role=source_role, run_id="00000000-0000-0000-0000-000000000001",
                    timestamp=f"{date}T00:00:01+00:00",
                )
                if label in {"source_only", "both", "both_different_bytes", "wrong_source_bytes", "wrong_hash", "wrong_size", "wrong_source_root"}:
                    private_path.parent.mkdir(parents=True, exist_ok=True)
                    private_path.write_bytes(payload if label != "wrong_source_bytes" else b"wrong source bytes")
                if label == "both_different_bytes":
                    target.write_bytes(synthetic_pdf(b"different archive target"))
                elif label in {"target_only", "both"}:
                    target.write_bytes(payload)
                elif label == "wrong_target_bytes":
                    target.write_bytes(b"wrong target bytes")

                if label in {"source_only", "target_only"}:
                    recovered, action = _ensure_archive_available(
                        self.config, state, invoice, item, object(), "00000000-0000-0000-0000-000000000001", RecordingLogger(),
                    )
                    self.assertEqual(info, recovered)
                    self.assertEqual("REUSED", action)
                    self.assertEqual("COMMITTED", state.invoice(invoice_id)["archive_state"])
                    self.assertEqual(payload, target.read_bytes())
                    if label == "source_only":
                        self.assertFalse(source.exists())
                else:
                    files_before = {
                        path: (path.exists() or path.is_symlink(), path.read_bytes() if path.is_file() else None)
                        for path in {source, private_path, target}
                    }
                    with self.assertRaises(StreamStateConflictError):
                        _ensure_archive_available(
                            self.config, state, invoice, item, object(), "00000000-0000-0000-0000-000000000001", RecordingLogger(),
                        )
                    held = state.active_file_operations(invoice_id, {"ARCHIVE_PUBLISH"})
                    self.assertEqual((1, "HOLD"), (len(held), held[0]["state"]))
                    self.assertEqual(label in {"both", "both_different_bytes", "wrong_source_bytes", "wrong_hash", "wrong_size"}, source.exists())
                    if label == "wrong_source_root":
                        self.assertTrue(private_path.exists())
                        self.assertFalse(source.exists())
                    self.assertEqual(label in {"both", "both_different_bytes", "wrong_target_bytes"}, target.exists())
                    with self.assertRaises(StreamStateConflictError):
                        _ensure_archive_available(
                            self.config, state, invoice, item, object(), "00000000-0000-0000-0000-000000000002", RecordingLogger(),
                        )
                    self.assertEqual("HOLD", state.active_file_operations(invoice_id, {"ARCHIVE_PUBLISH"})[0]["state"])
                    files_after = {
                        path: (path.exists() or path.is_symlink(), path.read_bytes() if path.is_file() else None)
                        for path in {source, private_path, target}
                    }
                    self.assertEqual(files_before, files_after)

    def test_archive_equal_bytes_without_ownership_and_cross_invoice_owner_hold(self) -> None:
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import _ensure_archive_available
        from energygrid_bill_downloader.state import StateV2Store, StreamStateConflictError
        from fixtures.synthetic_delivery import synthetic_pdf
        from fixtures.synthetic_dual_stream import candidate

        with StateV2Store(self.state_path) as state:
            unowned = candidate(Stream.EB_BILL, name="unowned-equal.pdf", invoice_date="2026-12-20")
            unowned_id = state.accept_latest(unowned, "00000000-0000-0000-0000-000000000001", "2026-12-20T00:00:00+00:00")
            unowned_invoice = state.invoice(unowned_id)
            unowned_path = self.archive / unowned_invoice["archive_relpath"]
            unowned_path.parent.mkdir(parents=True, exist_ok=True)
            unowned_payload = synthetic_pdf(b"same bytes are not ownership")
            unowned_path.write_bytes(unowned_payload)
            with self.assertRaises(StreamStateConflictError):
                _ensure_archive_available(self.config, state, unowned_invoice, unowned, object(), "00000000-0000-0000-0000-000000000001", RecordingLogger())
            self.assertEqual(unowned_payload, unowned_path.read_bytes())
            self.assertEqual([], state.file_operation_history(unowned_id, "ARCHIVE_PUBLISH", None, unowned_invoice["archive_relpath"]))

            owner = candidate(Stream.EB_BILL, name="owner-operation.pdf", invoice_date="2026-12-21")
            target_owner = candidate(Stream.TENANT_BILL, name="target-operation.pdf", invoice_date="2026-12-21")
            owner_id = state.accept_latest(owner, "00000000-0000-0000-0000-000000000001", "2026-12-21T00:00:00+00:00")
            target_id = state.accept_latest(target_owner, "00000000-0000-0000-0000-000000000001", "2026-12-21T00:00:00+00:00")
            owner_invoice = state.invoice(owner_id)
            target_invoice = state.invoice(target_id)
            info_path = self.root / "cross-owner.pdf"
            info_path.write_bytes(synthetic_pdf(b"cross owner"))
            operation = state.start_file_operation(
                operation_id=str(uuid.uuid4()), invoice_id=owner_id, kind="ARCHIVE_PUBLISH",
                private_path_ref=str(self.temp_root / "cross-owner-source.pdf"),
                target_relpath=target_invoice["archive_relpath"], binding_id=None,
                info=validate_pdf(info_path), source_role="SOURCE_ACQUISITION",
                run_id="00000000-0000-0000-0000-000000000001", timestamp="2026-12-21T00:00:01+00:00",
            )
            with self.assertRaises(StreamStateConflictError):
                _ensure_archive_available(self.config, state, target_invoice, target_owner, object(), "00000000-0000-0000-0000-000000000001", RecordingLogger())
            held = state.active_file_operations(owner_id, {"ARCHIVE_PUBLISH"})
            self.assertEqual((operation["operation_id"], "HOLD"), (held[0]["operation_id"], held[0]["state"]))

    def test_historical_legacy_latest_moves_through_journal_without_fetch(self) -> None:
        import sqlite3
        from dataclasses import replace
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store, migrate_state_database
        from fixtures.synthetic_delivery import synthetic_pdf
        from fixtures.synthetic_dual_stream import SyntheticAdapter, snapshot, test_stream_entries

        legacy_path = self.root / "legacy-state.sqlite3"
        legacy_name = "eb-latest.pdf"
        payload = synthetic_pdf(legacy_name.encode())
        info_path = self.root / "legacy-payload.pdf"
        info_path.write_bytes(payload)
        info = validate_pdf(info_path)
        with contextlib.closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute(
                "CREATE TABLE bills (filename_key TEXT PRIMARY KEY,portal_filename TEXT NOT NULL,first_seen_at_utc TEXT NOT NULL,last_seen_at_utc TEXT NOT NULL,archived_at_utc TEXT,byte_size INTEGER,sha256 TEXT,status TEXT NOT NULL,last_error_class TEXT,last_error_at_utc TEXT,attempt_count INTEGER NOT NULL DEFAULT 0,completion_source TEXT)"
            )
            connection.execute("PRAGMA user_version=1")
            connection.execute(
                "INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (legacy_name, legacy_name, "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:01+00:00", info.byte_size, info.sha256, "ARCHIVED", None, None, 1, "downloaded"),
            )
            connection.commit()
        streams = test_stream_entries()
        migrate_state_database(
            legacy_path, apply=True, streams=streams, mapping_entries=[{
                "legacy_filename_key": legacy_name, "stream": Stream.EB_BILL.value,
                "source_namespace": streams[Stream.EB_BILL.value].source_namespace,
                "source_filename": legacy_name, "raw_date": "2026-10-01",
                "date_profile": streams[Stream.EB_BILL.value].date_profile,
                "evidence_ref": streams[Stream.EB_BILL.value].evidence_ref,
            }],
        )
        old_archive = self.archive / legacy_name
        old_archive.write_bytes(payload)
        self.config = replace(self.config, state_path=legacy_path)
        adapters = self.adapters()
        adapters["TENANT_BILL"] = SyntheticAdapter(snapshot(Stream.TENANT_BILL, ()))
        calls: list[str] = []
        with StateV2Store(legacy_path) as state:
            retained = state.resolve_latest_candidate(adapters["EB_BILL"]._inventory.candidates[-1])
            self.assertIsNotNone(retained)
            retained_id = retained["invoice_id"]
            with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=self.fake_delivery(calls)):
                summary = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()))
            self.assertEqual(retained_id, state.stream("EB_BILL")["watermark_invoice_id"])
            self.assertEqual("COMPLETE", state.invoice(retained_id)["migration_state"])
            self.assertEqual("COMMITTED", state.invoice(retained_id)["archive_state"])
            self.assertEqual("LEGACY_MOVE", state.file_operation_history(retained_id, "LEGACY_MOVE", None, "EB Bill/2026-10-01.pdf")[0]["kind"])
        self.assertEqual([], adapters["EB_BILL"].acquire_calls)
        self.assertFalse(old_archive.exists())
        self.assertEqual(payload, (self.archive / "EB Bill" / "2026-10-01.pdf").read_bytes())
        self.assertEqual(0, summary.fetch_count)
        self.assertEqual(["EB_BILL"], calls)

    def test_legacy_move_authority_matrix_requires_exact_source_owner_and_binding(self) -> None:
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.reconcile import _ensure_archive_available
        from energygrid_bill_downloader.state import StateV2Store, StreamStateConflictError
        from fixtures.synthetic_delivery import synthetic_pdf
        from fixtures.synthetic_dual_stream import candidate

        cases = (
            "source_only", "target_only", "both_equal", "both_different", "neither", "wrong_owner",
            "wrong_target_path", "wrong_private_path", "wrong_binding", "wrong_role", "wrong_hash", "wrong_size",
        )
        for index, label in enumerate(cases, start=1):
            with self.subTest(case=label):
                name = f"legacy-{label}.pdf"
                date = f"2026-12-{index:02d}"
                item, old_source, payload, info, invoice_id = self.seed_migrated_archived_latest(
                    name=name, invoice_date=date, label=label,
                )
                target = self.archive / "EB Bill" / f"{date}.pdf"
                target.parent.mkdir(parents=True, exist_ok=True)
                with StateV2Store(self.config.state_path) as state:
                    invoice = state.invoice(invoice_id)
                    operation_owner = invoice_id
                    operation_target = invoice["archive_relpath"]
                    if label == "wrong_owner":
                        other = candidate(Stream.TENANT_BILL, name="different-owner.pdf", invoice_date="2026-12-20")
                        operation_owner = state.accept_latest(other, "00000000-0000-0000-0000-000000000001", "2026-12-20T00:00:00+00:00")
                    private_path = old_source
                    if label == "wrong_private_path":
                        private_path = self.archive / f"unrelated-{label}.pdf"
                        private_path.write_bytes(payload)
                    if label == "wrong_target_path":
                        operation_target += ".wrong"
                    binding_id = "WRONG_LEGACY_BINDING" if label == "wrong_binding" else None
                    source_role = "WRONG_ROLE" if label == "wrong_role" else "LEGACY_ARCHIVE"
                    expected = info
                    if label == "wrong_hash":
                        expected = FileInfo(info.byte_size, "0" * 64)
                    elif label == "wrong_size":
                        expected = FileInfo(info.byte_size + 1, info.sha256)
                    operation = state.start_file_operation(
                        operation_id=str(uuid.uuid4()), invoice_id=operation_owner, kind="LEGACY_MOVE",
                        private_path_ref=str(private_path), target_relpath=operation_target,
                        binding_id=binding_id, info=expected, source_role=source_role,
                        run_id="00000000-0000-0000-0000-000000000001", timestamp=f"{date}T00:00:03+00:00",
                    )
                    if label == "target_only":
                        old_source.unlink()
                        target.write_bytes(payload)
                    elif label in {"both_equal", "wrong_owner"}:
                        target.write_bytes(payload)
                    elif label == "both_different":
                        target.write_bytes(synthetic_pdf(b"different legacy target"))
                    elif label == "neither":
                        old_source.unlink()

                    if label in {"source_only", "target_only"}:
                        recovered, action = _ensure_archive_available(
                            self.config, state, invoice, item, object(), str(uuid.uuid4()), RecordingLogger(),
                        )
                        self.assertEqual((info, "REUSED"), (recovered, action))
                        self.assertEqual(payload, target.read_bytes())
                        self.assertFalse(old_source.exists())
                        committed = state.file_operation_history(
                            invoice_id, "LEGACY_MOVE", None, invoice["archive_relpath"],
                        )
                        self.assertEqual(1, len(committed))
                        self.assertEqual(operation["operation_id"], committed[0]["operation_id"])
                    else:
                        with self.assertRaises(StreamStateConflictError):
                            _ensure_archive_available(
                                self.config, state, invoice, item, object(), str(uuid.uuid4()), RecordingLogger(),
                            )
                        held = state.active_file_operations(operation_owner, {"LEGACY_MOVE"})
                        self.assertTrue(any(row["operation_id"] == operation["operation_id"] and row["state"] == "HOLD" for row in held))
                        if label in {"both_equal", "both_different", "wrong_owner"}:
                            self.assertTrue(old_source.exists())
                            self.assertTrue(target.exists())
                        elif label == "neither":
                            self.assertFalse(old_source.exists())
                            self.assertFalse(target.exists())
                        else:
                            self.assertTrue(old_source.exists())
                            self.assertFalse(target.exists())
                        with self.assertRaises(StreamStateConflictError):
                            _ensure_archive_available(
                                self.config, state, invoice, item, object(), str(uuid.uuid4()), RecordingLogger(),
                            )
                        self.assertTrue(any(
                            row["operation_id"] == operation["operation_id"] and row["state"] == "HOLD"
                            for row in state.active_file_operations(operation_owner, {"LEGACY_MOVE"})
                        ))

        item, old_source, payload, _info, invoice_id = self.seed_migrated_archived_latest(
            name="legacy-unowned-equal.pdf", label="unowned-equal", verified_archive=False,
        )
        with StateV2Store(self.config.state_path) as state:
            invoice = state.invoice(invoice_id)
            target = self.archive / invoice["archive_relpath"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            with self.assertRaises(StreamStateConflictError):
                _ensure_archive_available(self.config, state, invoice, item, object(), str(uuid.uuid4()), RecordingLogger())
            self.assertEqual(payload, target.read_bytes())
            self.assertEqual([], state.file_operation_history(invoice_id, "LEGACY_MOVE", None, invoice["archive_relpath"]))
            self.assertEqual([], state.active_file_operations(invoice_id, {"LEGACY_MOVE"}))
        self.assertTrue(old_source.exists())

    def test_archive_publish_and_legacy_move_reparse_sources_are_held(self) -> None:
        import os
        import subprocess
        from energygrid_bill_downloader.invoice import Stream
        from energygrid_bill_downloader.errors import ConfigError
        from energygrid_bill_downloader.reconcile import _canonical_path, _ensure_archive_available
        from energygrid_bill_downloader.state import StateV2Store, StreamStateConflictError
        from fixtures.synthetic_delivery import synthetic_pdf
        from fixtures.synthetic_dual_stream import candidate

        def make_junction(link: Path, target: Path) -> None:
            environment = os.environ.copy()
            environment["CODEX_TEST_JUNCTION_PATH"] = str(link)
            environment["CODEX_TEST_JUNCTION_TARGET"] = str(target)
            created = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "New-Item -ItemType Junction -Path $env:CODEX_TEST_JUNCTION_PATH -Target $env:CODEX_TEST_JUNCTION_TARGET -ErrorAction Stop | Out-Null"],
                capture_output=True, text=True, env=environment, check=False,
            )
            if created.returncode != 0:
                self.skipTest("directory junctions are unavailable in this Windows test environment")

        real_archive_dir = self.archive / "archive-publish-real-target"
        real_archive_dir.mkdir()
        canary = real_archive_dir / "keep.txt"
        canary.write_bytes(b"archive target canary")
        destination_parent = self.archive / "EB Bill"
        make_junction(destination_parent, real_archive_dir)
        try:
            item = candidate(Stream.EB_BILL, name="archive-target-reparse.pdf", invoice_date="2026-11-24")
            fetch_calls: list[str] = []

            class MustNotFetch:
                def acquire(self, selected, path):
                    fetch_calls.append(selected.source_filename)
                    raise AssertionError("fetch called before archive destination reparse check")

            with StateV2Store(self.state_path) as state:
                invoice_id = state.accept_latest(
                    item, "00000000-0000-0000-0000-000000000001", "2026-11-24T00:00:00+00:00",
                )
                invoice = state.invoice(invoice_id)
                self.assertEqual(
                    destination_parent / invoice["canonical_filename"],
                    _canonical_path(self.archive, invoice["stream"], invoice["canonical_filename"]),
                )
                temp_before = sorted(
                    path.relative_to(self.temp_root).as_posix() for path in self.temp_root.rglob("*")
                ) if self.temp_root.exists() else []
                with self.assertRaises(ConfigError):
                    _ensure_archive_available(
                        self.config, state, invoice, item, MustNotFetch(), str(uuid.uuid4()), RecordingLogger(),
                    )
                self.assertEqual([], fetch_calls)
                self.assertEqual([], state.active_file_operations(invoice_id, {"ARCHIVE_PUBLISH"}))
                self.assertEqual([], state.file_operation_history(
                    invoice_id, "ARCHIVE_PUBLISH", None, invoice["archive_relpath"],
                ))
                temp_after = sorted(
                    path.relative_to(self.temp_root).as_posix() for path in self.temp_root.rglob("*")
                ) if self.temp_root.exists() else []
                self.assertEqual(temp_before, temp_after)
                self.assertFalse((destination_parent / invoice["canonical_filename"]).exists())
            self.assertEqual(b"archive target canary", canary.read_bytes())
        finally:
            destination_parent.rmdir()

        with StateV2Store(self.state_path) as state:
            item = candidate(Stream.EB_BILL, name="archive-reparse.pdf", invoice_date="2026-11-25")
            invoice_id = state.accept_latest(item, "00000000-0000-0000-0000-000000000001", "2026-11-25T00:00:00+00:00")
            invoice = state.invoice(invoice_id)
            payload = synthetic_pdf(b"archive reparse")
            info_path = self.root / "archive-reparse-payload.pdf"
            info_path.write_bytes(payload)
            info = validate_pdf(info_path)
            real_dir = self.temp_root / "archive-real-source"
            real_dir.mkdir(parents=True, exist_ok=True)
            (real_dir / "payload.pdf").write_bytes(payload)
            junction = self.temp_root / "archive-source-junction"
            make_junction(junction, real_dir)
            operation = state.start_file_operation(
                operation_id=str(uuid.uuid4()), invoice_id=invoice_id, kind="ARCHIVE_PUBLISH",
                private_path_ref=str(junction / "payload.pdf"), target_relpath=invoice["archive_relpath"],
                binding_id=None, info=info, source_role="SOURCE_ACQUISITION", run_id="00000000-0000-0000-0000-000000000001",
                timestamp="2026-11-25T00:00:01+00:00",
            )
            target = self.archive / invoice["archive_relpath"]
            with self.assertRaises(StreamStateConflictError):
                _ensure_archive_available(self.config, state, invoice, item, object(), str(uuid.uuid4()), RecordingLogger())
            self.assertEqual("HOLD", state.active_file_operations(invoice_id, {"ARCHIVE_PUBLISH"})[0]["state"])
            self.assertEqual(payload, (real_dir / "payload.pdf").read_bytes())
            self.assertFalse(target.exists())
            junction.rmdir()

        item, old_source, payload, info, invoice_id = self.seed_migrated_archived_latest(
            name="legacy-reparse.pdf", invoice_date="2026-11-26", label="reparse-legacy",
        )
        real_legacy_dir = self.archive / "legacy-reparse-target"
        real_legacy_dir.mkdir(parents=True, exist_ok=True)
        (real_legacy_dir / "payload.pdf").write_bytes(payload)
        old_source.unlink()
        make_junction(old_source, real_legacy_dir)
        with StateV2Store(self.config.state_path) as state:
            invoice = state.invoice(invoice_id)
            operation = state.start_file_operation(
                operation_id=str(uuid.uuid4()), invoice_id=invoice_id, kind="LEGACY_MOVE",
                private_path_ref=str(old_source), target_relpath=invoice["archive_relpath"],
                binding_id=None, info=info, source_role="LEGACY_ARCHIVE", run_id="00000000-0000-0000-0000-000000000001",
                timestamp="2026-11-26T00:00:03+00:00",
            )
            with self.assertRaises(StreamStateConflictError):
                _ensure_archive_available(self.config, state, invoice, item, object(), str(uuid.uuid4()), RecordingLogger())
            self.assertEqual("HOLD", state.active_file_operations(invoice_id, {"LEGACY_MOVE"})[0]["state"])
            self.assertTrue(hasattr(old_source, "is_junction") and old_source.is_junction())
            self.assertFalse((self.archive / invoice["archive_relpath"]).exists())
            self.assertEqual(payload, (real_legacy_dir / "payload.pdf").read_bytes())
            old_source.rmdir()

    def test_runid_stage_events_follow_real_reconciliation_boundaries(self) -> None:
        import json
        import re
        from energygrid_bill_downloader.cli import SafeLogger
        from energygrid_bill_downloader.delivery import DELIVERY_RESULT_SCHEMA, DeliveryClient
        from energygrid_bill_downloader.state import StateV2Store

        adapters = self.adapters()
        run_id = str(uuid.uuid4())
        logger = SafeLogger(self.log_root, run_id)

        def post_once(url, authorization, body, timeout, *, content_type):
            delivery_id = re.search(rb'"delivery_id":"(egmail-v1-[0-9a-f]{32})"', body).group(1).decode()
            result = json.dumps({
                "schema": DELIVERY_RESULT_SCHEMA, "delivery_id": delivery_id,
                "outcome": "DELIVERED", "duplicate": False, "support_ref": "EG_SYNTHETIC_ACCEPTED",
            }, separators=(",", ":")).encode()
            return 200, result

        client = DeliveryClient(
            self.config.delivery,
            post_once=post_once,
            environ={"ENERGYGRID_DELIVERY_TOKEN": "PRIVATE-TOKEN-CANARY"},
        )
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=client):
            with StateV2Store(self.state_path) as state:
                summary = reconcile_module.reconcile_dual_stream(self.config, adapters, state, logger, run_id)
        events = [json.loads(line) for line in logger.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual("DOWNLOADED", summary.status)
        self.assertEqual(0, summary.exit_code)
        self.assertTrue(all(event["run_id"] == run_id for event in events))
        self.assertTrue(all(event.get("stream") in {"EB_BILL", "TENANT_BILL"} for event in events))
        for stream in ("EB_BILL", "TENANT_BILL"):
            phases = [event["phase"] for event in events if event.get("stream") == stream]
            self.assertEqual([
                "inventory_started", "inventory_result", "latest_selection", "selection_committed",
                "fetch_decision", "archive_started", "fetch_started", "fetch_completed",
                "archive_result", "drive_started", "drive_result", "delivery_intent",
                "dispatch_start", "delivery_outcome", "stream_complete",
            ], phases)
        rendered = logger.log_path.read_text(encoding="utf-8")
        for private in (
            "PRIVATE-TOKEN-CANARY", self.config.delivery.url, "eb-latest.pdf", "tenant-latest.pdf",
            str(self.archive), "SYNTHETIC-ENERGYGRID-NAMESPACE",
        ):
            self.assertNotIn(private, rendered)

    def test_logger_callback_failure_preserves_reconciliation_effects(self) -> None:
        from energygrid_bill_downloader.delivery import DELIVERY_RESULT_SCHEMA, DeliveryClient
        from energygrid_bill_downloader.state import StateV2Store
        import json
        import re

        class BrokenLogger:
            def event(self, phase, status=None, **fields):
                raise OSError("private logger canary")

        adapters = self.adapters()
        posts: list[str] = []

        def post_once(url, authorization, body, timeout, *, content_type):
            posts.append("POST")
            delivery_id = re.search(rb'"delivery_id":"(egmail-v1-[0-9a-f]{32})"', body).group(1).decode()
            result = json.dumps({
                "schema": DELIVERY_RESULT_SCHEMA, "delivery_id": delivery_id,
                "outcome": "DELIVERED", "duplicate": False, "support_ref": "EG_SYNTHETIC_ACCEPTED",
            }, separators=(",", ":")).encode()
            return 200, result

        client = DeliveryClient(self.config.delivery, post_once=post_once, environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"})
        with mock.patch("energygrid_bill_downloader.delivery.DeliveryClient", return_value=client):
            with StateV2Store(self.state_path) as state:
                summary = reconcile_module.reconcile_dual_stream(self.config, adapters, state, BrokenLogger(), str(uuid.uuid4()))
                self.assertEqual(2, state.connection.execute("SELECT COUNT(*) FROM energygrid_delivery_v1 WHERE state='DELIVERED'").fetchone()[0])
        self.assertEqual("DOWNLOADED", summary.status)
        self.assertEqual(0, summary.exit_code)
        self.assertEqual(2, len(posts))
        self.assertEqual(["eb-latest.pdf"], adapters["EB_BILL"].acquire_calls)
        self.assertEqual(["tenant-latest.pdf"], adapters["TENANT_BILL"].acquire_calls)

    def test_dual_stage_log_names_statuses_and_fields_are_closed_and_bounded(self) -> None:
        logger = RecordingLogger()
        safe = reconcile_module._install_observational_logger(logger)
        safe.event(
            "inventory_result", status="READY", stream="EB_BILL", inventory_count=2,
            support_ref="EG_SYNTHETIC_READY", source_filename="PRIVATE-FILENAME-CANARY",
        )
        safe.event("PRIVATE_PHASE_CANARY", status="READY", stream="EB_BILL")
        safe.event("inventory_result", status="PRIVATE_STATUS_CANARY", stream="EB_BILL")
        safe.event("inventory_result", status="FAILED", stream="EB_BILL", support_ref="PRIVATE-REF-CANARY")
        safe.event("inventory_result", status="READY", stream="EB_BILL", inventory_count=reconcile_module.MAX_INVENTORY_CEILING + 1)
        self.assertEqual(2, len(logger.events))
        phase, status, fields = logger.events[0]
        self.assertEqual(("inventory_result", "READY"), (phase, status))
        self.assertEqual({"stream": "EB_BILL", "inventory_count": 2, "support_ref": "EG_SYNTHETIC_READY"}, fields)
        self.assertEqual(("inventory_result", "READY", {"stream": "EB_BILL"}), logger.events[1])

    def test_list_only_reads_snapshot_without_lock_state_or_file_writes(self) -> None:
        from energygrid_bill_downloader.reconcile import reconcile_dual_stream
        from energygrid_bill_downloader.state import StateV2Store

        adapters = self.adapters()
        before_db = self.state_path.stat().st_mtime_ns
        before_files = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        with StateV2Store(self.state_path, read_only=True) as state:
            summary = reconcile_dual_stream(self.config, adapters, state, RecordingLogger(), str(uuid.uuid4()), list_only=True)
        after_files = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.assertEqual(before_files, after_files)
        self.assertEqual(before_db, self.state_path.stat().st_mtime_ns)
        self.assertEqual(0, summary.downloaded_count)
        self.assertEqual([], adapters["EB_BILL"].acquire_calls)
        self.assertEqual([], adapters["TENANT_BILL"].acquire_calls)


if __name__ == "__main__":
    unittest.main()
