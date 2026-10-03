from __future__ import annotations

from pathlib import Path
import unittest

from energygrid_bill_downloader.errors import SourceContractError
from energygrid_bill_downloader.http_source import ListedBill
from energygrid_bill_downloader.invoice import Candidate, DATE_PROFILE_ISO_V1, DirectHttpAdapter, InventorySnapshot, Stream, parse_invoice_date
from energygrid_bill_downloader.publication import filename_key
from fixtures.synthetic_dual_stream import SYNTHETIC_EVIDENCE, SYNTHETIC_NAMESPACE, candidate


class InvoiceIdentityTests(unittest.TestCase):
    def test_date_profile_accepts_only_exact_ascii_gregorian_date(self) -> None:
        self.assertEqual("2024-02-29", parse_invoice_date("2024-02-29", DATE_PROFILE_ISO_V1).isoformat())
        for value in ("2023-02-29", "2026-1-01", "2026-01-01T00:00:00Z", "２０２６-01-01", " 2026-01-01"):
            with self.subTest(value=value), self.assertRaises(SourceContractError):
                parse_invoice_date(value, DATE_PROFILE_ISO_V1)

    def test_candidate_derives_canonical_filename_from_explicit_date(self) -> None:
        item = candidate(invoice_date="2026-02-03")
        self.assertEqual("2026-02-03.pdf", item.canonical_filename)
        self.assertEqual(filename_key("invoice-001.pdf"), item.source_invoice_key)

    def test_direct_candidate_construction_cannot_bypass_validation(self) -> None:
        with self.assertRaises(SourceContractError):
            Candidate(
                stream=Stream.EB_BILL,
                source_namespace=SYNTHETIC_NAMESPACE,
                source_invoice_key="wrong",
                source_filename="invoice.pdf",
                raw_date="2026-01-01",
                date_profile=DATE_PROFILE_ISO_V1,
                invoice_date=parse_invoice_date("2026-01-01", DATE_PROFILE_ISO_V1),
                day_ordinal=123,
                canonical_filename="2026-01-01.pdf",
                evidence_ref=SYNTHETIC_EVIDENCE,
                fetch_handle=None,
            )

    def test_unsafe_or_reserved_source_filenames_are_rejected(self) -> None:
        for name in ("../invoice.pdf", "CON.pdf", "CON.statement.pdf", "COM1.pdf", "COM¹.invoice.pdf", "invoice.pdf ", "invoice.txt"):
            with self.subTest(name=name), self.assertRaises(SourceContractError):
                candidate(name=name)

    def test_inventory_rejects_mixed_bindings_and_duplicate_normalized_identity(self) -> None:
        first = candidate(name="Cafe\u0301.pdf")
        duplicate = candidate(name="Caf\u00e9.pdf")
        with self.assertRaises(SourceContractError):
            InventorySnapshot(
                stream=Stream.EB_BILL,
                source_namespace=SYNTHETIC_NAMESPACE,
                date_profile=DATE_PROFILE_ISO_V1,
                completeness_witness="EG_SYNTHETIC_COMPLETE_SNAPSHOT",
                candidates=(first, duplicate),
            )

    def test_inventory_requires_tuple_and_completeness_witness(self) -> None:
        with self.assertRaises(SourceContractError):
            InventorySnapshot(Stream.EB_BILL, SYNTHETIC_NAMESPACE, DATE_PROFILE_ISO_V1, "", ())
        with self.assertRaises(SourceContractError):
            InventorySnapshot(Stream.EB_BILL, SYNTHETIC_NAMESPACE, DATE_PROFILE_ISO_V1, "complete", [])

    def test_direct_http_acquire_accepts_only_candidates_from_the_current_snapshot(self) -> None:
        class Source:
            def __init__(self, rows):
                self.rows = rows
                self.downloaded = []

            def inventory(self, _safety_ceiling):
                return self.rows

            def download(self, row, _destination):
                self.downloaded.append(row)
                return row.filename

        first_row = ListedBill(0, "eb-001.pdf", "2026-10-01")
        source = Source([first_row])
        adapter = DirectHttpAdapter(
            source,
            stream=Stream.EB_BILL,
            source_namespace=SYNTHETIC_NAMESPACE,
            evidence_ref=SYNTHETIC_EVIDENCE,
        )
        first_snapshot = adapter.inventory(10)
        issued = first_snapshot.candidates[0]
        self.assertEqual("eb-001.pdf", adapter.acquire(issued, Path("unused")))
        self.assertEqual([first_row], source.downloaded)

        forged = Candidate.create(
            stream=Stream.EB_BILL,
            source_namespace=SYNTHETIC_NAMESPACE,
            source_filename=first_row.filename,
            raw_date=first_row.date,
            date_profile=DATE_PROFILE_ISO_V1,
            evidence_ref=SYNTHETIC_EVIDENCE,
            fetch_handle=first_row,
        )
        with self.assertRaises(SourceContractError):
            adapter.acquire(forged, Path("unused"))

        source.rows = [ListedBill(0, "eb-002.pdf", "2026-10-02")]
        adapter.inventory(10)
        with self.assertRaises(SourceContractError):
            adapter.acquire(issued, Path("unused"))
        self.assertEqual([first_row], source.downloaded)


if __name__ == "__main__":
    unittest.main()
