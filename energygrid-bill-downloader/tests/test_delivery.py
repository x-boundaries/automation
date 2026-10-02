from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from energygrid_bill_downloader.config import DeliverySettings
from energygrid_bill_downloader.delivery import (
    DELIVERY_RESULT_SCHEMA,
    DELIVERY_SCHEMA,
    DeliveryClient,
    build_multipart,
)
from energygrid_bill_downloader.errors import StateError
from energygrid_bill_downloader.drive import DriveStager
from energygrid_bill_downloader.invoice import Stream
from energygrid_bill_downloader.publication import validate_pdf
from energygrid_bill_downloader.state import StateV2Store
from fixtures.synthetic_delivery import synthetic_pdf
from fixtures.synthetic_http_source import candidate, create_v2_database


RUN1 = "00000000-0000-0000-0000-000000000001"
RUN2 = "00000000-0000-0000-0000-000000000002"


class DeliveryClientTests(unittest.TestCase):
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
        self.payload = synthetic_pdf(b"delivery-test")
        self.archive_path = self.archive / "EB Bill" / "2026-10-01.pdf"
        self.archive_path.parent.mkdir()
        self.archive_path.write_bytes(self.payload)
        self.info = validate_pdf(self.archive_path)
        self.settings = DeliverySettings(
            url="http://127.0.0.1:5678/webhook/REPLACE_WITH_PRIVATE_PATH",
            auth_header_name="X-EnergyGrid-Delivery",
            auth_token_env="ENERGYGRID_DELIVERY_TOKEN",
            max_pdf_bytes=1_000_000,
            timeout_seconds=5,
        )

    def seed_handled_archive(self, state: StateV2Store) -> dict:
        item = candidate(Stream.EB_BILL, name="eb-source-1.pdf", invoice_date="2026-10-01")
        invoice_id = state.accept_latest(item, RUN1, "2026-10-02T00:00:00+00:00")
        invoice = state.invoice(invoice_id)
        archive_op = state.start_file_operation(
            operation_id=str(uuid.uuid4()), invoice_id=invoice_id, kind="ARCHIVE_PUBLISH",
            private_path_ref=str(self.root / "not-used-after-commit.tmp"), target_relpath=invoice["archive_relpath"],
            binding_id=None, info=self.info, source_role="SYNTHETIC_SOURCE", run_id=RUN1,
            timestamp="2026-10-02T00:00:00+00:00",
        )
        state.complete_file_operation(archive_op["operation_id"], "2026-10-02T00:00:01+00:00", evidence_ref="EG_SYNTHETIC_ARCHIVE_COMMIT")
        state.update_invoice_file_state(
            invoice_id, archive_state="COMMITTED", byte_size=self.info.byte_size,
            sha256=self.info.sha256, archived_at_utc="2026-10-02T00:00:01+00:00",
        )
        invoice = state.invoice(invoice_id)
        DriveStager(self.archive, self.drive_root, "SYNTHETIC_DRIVE_BINDING").stage(state, invoice, self.archive_path, RUN1)
        return state.invoice(invoice_id)

    def result(self, delivery_id: str, *, outcome: str = "DELIVERED", duplicate: bool = False) -> bytes:
        return json.dumps(
            {
                "schema": DELIVERY_RESULT_SCHEMA,
                "delivery_id": delivery_id,
                "outcome": outcome,
                "duplicate": duplicate,
                "support_ref": "EG_SYNTHETIC_MAIL_RESULT",
            },
            separators=(",", ":"),
        ).encode()

    def test_success_is_one_bounded_post_and_repeat_never_resends(self) -> None:
        calls: list[tuple[str, bytes, str]] = []

        def post_once(url, authorization, body, timeout, *, content_type):
            calls.append((authorization, body, content_type))
            delivery_id = __import__("re").search(rb'"delivery_id":"(egmail-v1-[0-9a-f]{32})"', body).group(1).decode()
            return 200, self.result(delivery_id)

        with StateV2Store(self.state_path) as state:
            invoice = self.seed_handled_archive(state)
            client = DeliveryClient(self.settings, post_once=post_once, environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"})
            first = client.deliver(state, invoice, self.archive_path, RUN1)
            second = client.deliver(state, state.invoice(invoice["invoice_id"]), self.archive_path, RUN2)
            self.assertEqual("DELIVERED", first.state)
            self.assertEqual("DELIVERED", second.state)
            self.assertEqual(1, len(calls))
            self.assertEqual("X-EnergyGrid-Delivery: SYNTHETIC_TOKEN", calls[0][0])
            self.assertEqual(1, calls[0][1].count(b'name="metadata"'))
            self.assertEqual(1, calls[0][1].count(b'name="pdf"'))
            self.assertIn(self.payload, calls[0][1])
            row = state.delivery_for_invoice(invoice["invoice_id"])
            self.assertEqual("DELIVERED", row["state"])
            self.assertTrue(row["delivery_id"].startswith("egmail-v1-"))

    def test_authentication_preparation_failure_does_not_consume_dispatch(self) -> None:
        calls = 0

        def post_once(url, authorization, body, timeout, *, content_type):
            nonlocal calls
            calls += 1
            delivery_id = __import__("re").search(rb'"delivery_id":"(egmail-v1-[0-9a-f]{32})"', body).group(1).decode()
            return 200, self.result(delivery_id)

        with StateV2Store(self.state_path) as state:
            invoice = self.seed_handled_archive(state)
            with self.assertRaises(StateError):
                DeliveryClient(self.settings, post_once=post_once, environ={}).deliver(state, invoice, self.archive_path, RUN1)
            pending = state.delivery_for_invoice(invoice["invoice_id"])
            self.assertIsNone(pending["dispatch_started_at_utc"])
            outcome = DeliveryClient(self.settings, post_once=post_once, environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"}).deliver(state, invoice, self.archive_path, RUN2)
            self.assertEqual("DELIVERED", outcome.state)
            self.assertEqual(1, calls)

    def test_uncertain_post_is_terminal_and_never_retried(self) -> None:
        calls = 0

        def post_once(url, authorization, body, timeout, *, content_type):
            nonlocal calls
            calls += 1
            raise TimeoutError("synthetic transport uncertainty")

        with StateV2Store(self.state_path) as state:
            invoice = self.seed_handled_archive(state)
            client = DeliveryClient(self.settings, post_once=post_once, environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"})
            first = client.deliver(state, invoice, self.archive_path, RUN1)
            second = client.deliver(state, invoice, self.archive_path, RUN2)
            self.assertEqual("DELIVERY_OUTCOME_UNCERTAIN", first.state)
            self.assertEqual("DELIVERY_OUTCOME_UNCERTAIN", second.state)
            self.assertEqual(1, calls)

    def test_multipart_rejects_metadata_or_pdf_mismatch(self) -> None:
        payload = self.payload
        metadata = {
            "schema": DELIVERY_SCHEMA,
            "delivery_id": "egmail-v1-" + "a" * 32,
            "run_id": RUN1,
            "stream": "EB_BILL",
            "bill_date": "2026-10-01",
            "attachment_name": "2026-10-01.pdf",
            "pdf_byte_size": len(payload),
            "pdf_sha256": hashlib.sha256(payload).hexdigest(),
        }
        body, content_type = build_multipart(metadata, payload, 1_000_000)
        self.assertIn(b'name="metadata"', body)
        self.assertIn(b'name="pdf"; filename="2026-10-01.pdf"', body)
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        invalid = dict(metadata, pdf_sha256="0" * 64)
        with self.assertRaises(StateError):
            build_multipart(invalid, payload, 1_000_000)


if __name__ == "__main__":
    unittest.main()
