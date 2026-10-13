from __future__ import annotations

from datetime import date, datetime
import unittest

from energygrid_bill_downloader.errors import OfflineV4Error
from energygrid_bill_downloader.naming import (
    DISPLAY_NAME_MAX_LENGTH,
    MONTH_ABBREVIATIONS,
    display_filename,
    display_key,
    month_prefix,
)
from fixtures.synthetic_dual_stream import candidate


class DisplayFilenameTests(unittest.TestCase):
    def test_every_month_uses_the_uppercase_three_letter_abbreviation(self) -> None:
        self.assertEqual(
            ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
            MONTH_ABBREVIATIONS,
        )
        for month in range(1, 13):
            with self.subTest(month=month):
                invoice_date = date(2026, month, 15)
                expected = f"{MONTH_ABBREVIATIONS[month - 1]} 2026 - Invoice 001.pdf"
                self.assertEqual(expected, display_filename("Invoice 001.pdf", invoice_date))

    def test_prefix_is_month_then_four_digit_year_with_fixed_separator(self) -> None:
        self.assertEqual("OCT 2026 - ", month_prefix(date(2026, 10, 13)))
        self.assertEqual("JAN 2030 - ", month_prefix(date(2030, 1, 1)))

    def test_source_name_is_kept_verbatim_after_the_prefix(self) -> None:
        self.assertEqual(
            "OCT 2026 - Acme Inv #12 (rev).pdf",
            display_filename("Acme Inv #12 (rev).pdf", date(2026, 10, 1)),
        )

    def test_extension_is_preserved_exactly(self) -> None:
        self.assertEqual("OCT 2026 - invoice.PDF", display_filename("invoice.PDF", date(2026, 10, 1)))

    def test_already_prefixed_name_is_not_prefixed_again(self) -> None:
        self.assertEqual(
            "OCT 2026 - invoice.pdf",
            display_filename("OCT 2026 - invoice.pdf", date(2026, 10, 1)),
        )

    def test_already_prefixed_name_in_other_letter_case_is_normalised_to_uppercase(self) -> None:
        self.assertEqual(
            "OCT 2026 - invoice.pdf",
            display_filename("oct 2026 - invoice.pdf", date(2026, 10, 1)),
        )

    def test_doubled_prefix_is_refused(self) -> None:
        with self.assertRaises(OfflineV4Error) as caught:
            display_filename("OCT 2026 - OCT 2026 - invoice.pdf", date(2026, 10, 1))
        self.assertEqual("EG_V4_DISPLAY_PREFIX_CONFLICT", caught.exception.support_ref)

    def test_other_month_prefix_is_a_conflict_not_a_second_prefix(self) -> None:
        with self.assertRaises(OfflineV4Error) as caught:
            display_filename("SEP 2026 - invoice.pdf", date(2026, 10, 1))
        self.assertEqual("EG_V4_DISPLAY_PREFIX_CONFLICT", caught.exception.support_ref)

    def test_other_year_prefix_is_a_conflict(self) -> None:
        with self.assertRaises(OfflineV4Error) as caught:
            display_filename("OCT 2025 - invoice.pdf", date(2026, 10, 1))
        self.assertEqual("EG_V4_DISPLAY_PREFIX_CONFLICT", caught.exception.support_ref)

    def test_text_that_is_not_a_month_prefix_is_content_and_gets_one_prefix(self) -> None:
        self.assertEqual(
            "OCT 2026 - XYZ 2026 - invoice.pdf",
            display_filename("XYZ 2026 - invoice.pdf", date(2026, 10, 1)),
        )

    def test_invalid_source_and_date_types_are_refused(self) -> None:
        cases = (
            (lambda: display_filename("", date(2026, 10, 1)), "EG_V4_DISPLAY_SOURCE_INVALID"),
            (lambda: display_filename(None, date(2026, 10, 1)), "EG_V4_DISPLAY_SOURCE_INVALID"),
            (lambda: display_filename("invoice.pdf", "2026-10-01"), "EG_V4_DISPLAY_DATE_INVALID"),
            (lambda: display_filename("invoice.pdf", datetime(2026, 10, 1)), "EG_V4_DISPLAY_DATE_INVALID"),
        )
        for call, support_ref in cases:
            with self.subTest(support_ref=support_ref), self.assertRaises(OfflineV4Error) as caught:
                call()
            self.assertEqual(support_ref, caught.exception.support_ref)

    def test_names_with_path_or_control_characters_are_refused(self) -> None:
        for source in ("a/b.pdf", "a\\b.pdf", "a:b.pdf", "a*b.pdf", "a\x01b.pdf", "a?b.pdf"):
            with self.subTest(source=source), self.assertRaises(OfflineV4Error) as caught:
                display_filename(source, date(2026, 10, 1))
            self.assertEqual("EG_V4_DISPLAY_NAME_INVALID", caught.exception.support_ref)

    def test_name_without_a_pdf_extension_or_with_an_empty_stem_is_refused(self) -> None:
        for source in ("invoice.txt", ".pdf", "OCT 2026 - .pdf", "   .pdf", "invoice.pdf."):
            with self.subTest(source=source), self.assertRaises(OfflineV4Error) as caught:
                display_filename(source, date(2026, 10, 1))
            self.assertEqual("EG_V4_DISPLAY_NAME_INVALID", caught.exception.support_ref)

    def test_length_limit_is_inclusive_and_never_truncates(self) -> None:
        prefix_length = len(month_prefix(date(2026, 10, 1)))
        fits = "a" * (DISPLAY_NAME_MAX_LENGTH - prefix_length - len(".pdf")) + ".pdf"
        self.assertEqual(DISPLAY_NAME_MAX_LENGTH, len(display_filename(fits, date(2026, 10, 1))))
        too_long = fits[:-len(".pdf")] + "a.pdf"
        with self.assertRaises(OfflineV4Error) as caught:
            display_filename(too_long, date(2026, 10, 1))
        self.assertEqual("EG_V4_DISPLAY_NAME_INVALID", caught.exception.support_ref)


class DisplayKeyTests(unittest.TestCase):
    def test_collision_key_ignores_letter_case_and_unicode_composition(self) -> None:
        self.assertEqual(display_key("OCT 2026 - Invoice.pdf"), display_key("oct 2026 - invoice.pdf"))
        self.assertEqual(display_key("Café.pdf"), display_key("Café.pdf"))

    def test_collision_key_refuses_empty_input(self) -> None:
        with self.assertRaises(OfflineV4Error):
            display_key("")


class CanonicalIdentityTests(unittest.TestCase):
    def test_archive_identity_stays_date_only_while_presentation_gets_the_prefix(self) -> None:
        item = candidate(name="Invoice 001.PDF", invoice_date="2026-10-01")
        self.assertEqual("2026-10-01.pdf", item.canonical_filename)
        self.assertEqual("OCT 2026 - Invoice 001.PDF", display_filename(item.source_filename, item.invoice_date))


if __name__ == "__main__":
    unittest.main()
