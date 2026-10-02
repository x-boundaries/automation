from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from energygrid_bill_downloader.errors import ArchiveConflictError
from energygrid_bill_downloader.invoice import Stream
from energygrid_bill_downloader.publication import FileInfo, validate_pdf
from energygrid_bill_downloader.state import StateV2Store
from energygrid_bill_downloader.drive import DriveStager
from fixtures.synthetic_delivery import synthetic_pdf
from fixtures.synthetic_http_source import SYNTHETIC_EVIDENCE, SYNTHETIC_NAMESPACE, candidate, create_v2_database


class DriveStagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "archive"
        self.drive_root = self.root / "drive"
        self.archive.mkdir()
        self.drive_root.mkdir()
        self.state_path = self.root / "state" / "state.sqlite3"
        self.state_path.parent.mkdir()
        create_v2_database(self.state_path)
        self.stream_candidate = candidate(Stream.EB_BILL, name="eb-invoice-1.pdf", invoice_date="2026-10-01")
        self.payload = synthetic_pdf()
        self.archive_path = self.archive / "EB Bill" / "2026-10-01.pdf"
        self.archive_path.parent.mkdir()
        self.archive_path.write_bytes(self.payload)
        self.info = validate_pdf(self.archive_path)

    def seed_invoice(self, state: StateV2Store) -> dict:
        invoice_id = state.accept_latest(self.stream_candidate, "00000000-0000-0000-0000-000000000001", "2026-10-02T00:00:00+00:00")
        invoice = state.invoice(invoice_id)
        self.assertIsNotNone(invoice)
        operation = state.start_file_operation(
            operation_id=str(uuid.uuid4()), invoice_id=invoice_id, kind="ARCHIVE_PUBLISH",
            private_path_ref=str(self.root / "archive-temp.pdf"), target_relpath="EB Bill/2026-10-01.pdf",
            binding_id=None, info=self.info, source_role="SYNTHETIC_SOURCE",
            run_id="00000000-0000-0000-0000-000000000001", timestamp="2026-10-02T00:00:00+00:00",
        )
        state.complete_file_operation(operation["operation_id"], "2026-10-02T00:00:01+00:00", evidence_ref="EG_SYNTHETIC_ARCHIVE_COMMIT")
        state.update_invoice_file_state(
            invoice_id, archive_state="COMMITTED", byte_size=self.info.byte_size,
            sha256=self.info.sha256, archived_at_utc="2026-10-02T00:00:01+00:00",
        )
        return state.invoice(invoice_id)

    def test_copies_exact_archive_bytes_and_freezes_stage_facts(self) -> None:
        with StateV2Store(self.state_path) as state:
            invoice = self.seed_invoice(state)
            stager = DriveStager(self.archive, self.drive_root, "SYNTHETIC_DRIVE_BINDING")
            info = stager.stage(state, invoice, self.archive_path, "00000000-0000-0000-0000-000000000001")
            target = self.drive_root / "EB Bill" / "2026-10-01.pdf"
            self.assertEqual(self.info, info)
            self.assertEqual(self.payload, target.read_bytes())
            staged = state.invoice(invoice["invoice_id"])
            self.assertEqual("DRIVE_STAGED", staged["drive_state"])
            self.assertEqual(self.info.sha256, staged["drive_sha256"])
            self.assertEqual(info, stager.stage(state, staged, self.archive_path, "00000000-0000-0000-0000-000000000001"))

    def test_equal_unjournaled_destination_is_not_adopted(self) -> None:
        with StateV2Store(self.state_path) as state:
            invoice = self.seed_invoice(state)
            target = self.drive_root / "EB Bill" / "2026-10-01.pdf"
            target.parent.mkdir()
            target.write_bytes(self.payload)
            stager = DriveStager(self.archive, self.drive_root, "SYNTHETIC_DRIVE_BINDING")
            with self.assertRaises(ArchiveConflictError):
                stager.stage(state, invoice, self.archive_path, "00000000-0000-0000-0000-000000000001")
            self.assertEqual(self.payload, target.read_bytes())
            self.assertIsNone(state.file_operation(invoice["invoice_id"], "DRIVE_STAGE", "SYNTHETIC_DRIVE_BINDING", "EB Bill/2026-10-01.pdf"))

    def test_prepared_copy_is_recovered_from_its_exact_private_path(self) -> None:
        with StateV2Store(self.state_path) as state:
            invoice = self.seed_invoice(state)
            target = self.drive_root / "EB Bill" / "2026-10-01.pdf"
            target.parent.mkdir()
            temp_path = target.with_name(".2026-10-01.pdf.synthetic.tmp")
            temp_path.write_bytes(self.payload)
            state.start_file_operation(
                operation_id=str(uuid.uuid4()), invoice_id=invoice["invoice_id"], kind="DRIVE_STAGE",
                private_path_ref=str(temp_path), target_relpath="EB Bill/2026-10-01.pdf",
                binding_id="SYNTHETIC_DRIVE_BINDING", info=self.info, source_role="ARCHIVE_COMMITTED",
                run_id="00000000-0000-0000-0000-000000000001", timestamp="2026-10-02T00:00:02+00:00",
            )
            stager = DriveStager(self.archive, self.drive_root, "SYNTHETIC_DRIVE_BINDING")
            stager.stage(state, invoice, self.archive_path, "00000000-0000-0000-0000-000000000001")
            self.assertEqual(self.payload, target.read_bytes())
            self.assertEqual("DRIVE_STAGED", state.invoice(invoice["invoice_id"])["drive_state"])


if __name__ == "__main__":
    unittest.main()
