from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from energygrid_bill_downloader.errors import ArchiveConflictError, ConfigError, InvalidPdfError
from energygrid_bill_downloader.publication import (
    cleanup_run_directory,
    cleanup_stale_owned_temp,
    create_run_directory,
    filename_key,
    publish_no_replace,
    validate_filename,
    validate_pdf,
)
from tests.fixtures.synthetic_portal import synthetic_pdf


@unittest.skipUnless(os.name == "nt", "the locked publication primitive is Windows-only")
class PublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.archive = self.root / "archive"
        self.archive.mkdir()
        self.temp = self.root / "temp"
        self.temp.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_filename_identity_and_pdf_validation(self) -> None:
        final = validate_filename("2026-05-01_account_ref.pdf", self.archive)
        self.assertEqual(filename_key("Invoice.PDF"), filename_key("invoice.pdf"))
        final.write_bytes(synthetic_pdf())
        info = validate_pdf(final)
        self.assertGreater(info.byte_size, 0)
        self.assertEqual(len(info.sha256), 64)
        for name in ("../escape.pdf", "CON.pdf", "bad.txt", "trailing. ".replace(" ", " ")):
            with self.assertRaises(ConfigError):
                validate_filename(name, self.archive)

    def test_invalid_pdf_is_rejected(self) -> None:
        target = self.archive / "bad.pdf"
        target.write_bytes(b"not a pdf")
        with self.assertRaises(InvalidPdfError):
            validate_pdf(target)

    def test_publication_is_no_replace_and_cleanup_is_owned_only(self) -> None:
        source = self.temp / "download.bin"
        destination = self.archive / "bill.pdf"
        source.write_bytes(synthetic_pdf())
        publish_no_replace(source, destination)
        self.assertFalse(source.exists())
        self.assertTrue(destination.exists())

        with self.assertRaises(ArchiveConflictError):
            second = self.temp / "second.bin"
            second.write_bytes(synthetic_pdf(b"different"))
            publish_no_replace(second, destination)
        self.assertTrue(destination.exists())
        self.assertTrue((self.temp).is_dir())

        run_id = str(uuid.uuid4())
        run_dir = create_run_directory(self.temp, run_id)
        (run_dir / "owned.partial").write_bytes(b"partial")
        cleanup_run_directory(run_dir, self.temp)
        self.assertFalse(run_dir.exists())

        unrelated = self.temp / "do-not-touch"
        unrelated.mkdir()
        (unrelated / "file").write_text("synthetic", encoding="utf-8")
        self.assertEqual(cleanup_stale_owned_temp(self.temp, older_than_seconds=0), 0)
        self.assertTrue(unrelated.exists())
        stale = create_run_directory(self.temp, str(uuid.uuid4()))
        (stale / "crashed.partial").write_bytes(b"partial")
        os.utime(stale, (0, 0))
        self.assertEqual(cleanup_stale_owned_temp(self.temp, older_than_seconds=0), 1)
        self.assertFalse(stale.exists())



if __name__ == "__main__":
    unittest.main()
