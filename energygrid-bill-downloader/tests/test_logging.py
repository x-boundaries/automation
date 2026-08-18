from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
import uuid

from energygrid_bill_downloader.cli import SafeLogger, redact_sensitive


class LoggingTests(unittest.TestCase):
    def test_sensitive_values_are_redacted_and_private_fields_are_not_logged(self) -> None:
        username = "synthetic-" + uuid.uuid4().hex
        password = "synthetic-" + uuid.uuid4().hex
        old_username = os.environ.get("ENERGYGRID_USERNAME")
        old_password = os.environ.get("ENERGYGRID_PASSWORD")
        os.environ["ENERGYGRID_USERNAME"] = username
        os.environ["ENERGYGRID_PASSWORD"] = password
        try:
            message = f"password={password}; cookie=synthetic-session; username={username}"
            redacted = redact_sensitive(message)
            self.assertNotIn(username, redacted)
            self.assertNotIn(password, redacted)
            self.assertNotIn("synthetic-session", redacted)
        finally:
            if old_username is None:
                os.environ.pop("ENERGYGRID_USERNAME", None)
            else:
                os.environ["ENERGYGRID_USERNAME"] = old_username

        with tempfile.TemporaryDirectory() as directory:
            logger = SafeLogger(Path(directory), str(uuid.uuid4()))
            logger.event(
                "run_complete",
                status="NO_NEW_BILLS",
                inventory_count=0,
                filename="private-filename.pdf",
                support_ref="synthetic-ref",
            )
            content = logger.log_path.read_text(encoding="utf-8")
            self.assertNotIn("private-filename.pdf", content)
            self.assertIn("synthetic-ref", content)
            self.assertNotIn("cookie", content.casefold())


if __name__ == "__main__":
    unittest.main()
