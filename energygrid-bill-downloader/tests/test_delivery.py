from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from unittest import mock
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
from fixtures.synthetic_dual_stream import candidate, create_v2_database


RUN1 = "00000000-0000-0000-0000-000000000001"
RUN2 = "00000000-0000-0000-0000-000000000002"


class RecordingStageLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, dict[str, object]]] = []

    def event(self, phase: str, status: str | None = None, **fields: object) -> None:
        self.events.append((phase, status, fields))


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
            binding_id=None, info=self.info, source_role="SOURCE_ACQUISITION", run_id=RUN1,
            timestamp="2026-10-02T00:00:00+00:00",
        )
        state.complete_file_operation(archive_op["operation_id"], "2026-10-02T00:00:01+00:00", evidence_ref="EG_ARCHIVE_HASH_VERIFIED")
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

    def test_dispatch_stage_follows_committed_marker_and_replay_logs_no_send(self) -> None:
        logger = RecordingStageLogger()
        posts: list[str] = []
        state_ref = {}

        def post_once(url, authorization, body, timeout, *, content_type):
            row = state_ref["state"].delivery_for_invoice(state_ref["invoice"]["invoice_id"])
            self.assertIsNotNone(row["dispatch_started_at_utc"])
            self.assertEqual(RUN1, row["dispatch_run_id"])
            self.assertEqual(("dispatch_start", "COMMITTED"), (logger.events[-1][0], logger.events[-1][1]))
            posts.append("POST")
            delivery_id = __import__("re").search(rb'"delivery_id":"(egmail-v1-[0-9a-f]{32})"', body).group(1).decode()
            return 200, self.result(delivery_id)

        with StateV2Store(self.state_path) as state:
            invoice = self.seed_handled_archive(state)
            state_ref.update(state=state, invoice=invoice)
            client = DeliveryClient(self.settings, post_once=post_once, environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"})
            first = client.deliver(state, invoice, self.archive_path, RUN1, logger=logger)
            event_index = len(logger.events)
            second = client.deliver(state, invoice, self.archive_path, RUN2, logger=logger)
            self.assertEqual("DELIVERED", first.state)
            self.assertEqual("DELIVERED", second.state)
            self.assertEqual(["delivery_intent", "dispatch_start", "delivery_outcome"], [phase for phase, _status, _fields in logger.events[:event_index]])
            self.assertEqual(["delivery_intent", "delivery_no_send"], [phase for phase, _status, _fields in logger.events[event_index:]])
            self.assertEqual("ALREADY_DELIVERED", logger.events[-1][1])
            self.assertEqual(1, len(posts))

    def test_failed_dispatch_marker_never_emits_start_or_posts(self) -> None:
        logger = RecordingStageLogger()
        posts: list[str] = []

        def post_once(url, authorization, body, timeout, *, content_type):
            posts.append("POST")
            raise AssertionError("a rejected dispatch marker reached POST")

        with StateV2Store(self.state_path) as state:
            invoice = self.seed_handled_archive(state)
            client = DeliveryClient(self.settings, post_once=post_once, environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"})
            with mock.patch.object(state, "claim_delivery_dispatch", return_value=False):
                outcome = client.deliver(state, invoice, self.archive_path, RUN1, logger=logger)
            row = state.delivery_for_invoice(invoice["invoice_id"])

        self.assertEqual("REQUEST_REJECTED", outcome.state)
        self.assertIsNone(row["dispatch_started_at_utc"])
        self.assertNotIn(("dispatch_start", "COMMITTED", {"stream": "EB_BILL"}), logger.events)
        self.assertEqual(["delivery_intent", "delivery_no_send"], [event[0] for event in logger.events])
        self.assertEqual([], posts)

    def test_outcome_persistence_failure_is_uncertain_and_replay_never_posts(self) -> None:
        logger = RecordingStageLogger()
        posts: list[str] = []

        def post_once(url, authorization, body, timeout, *, content_type):
            posts.append("POST")
            delivery_id = __import__("re").search(rb'"delivery_id":"(egmail-v1-[0-9a-f]{32})"', body).group(1).decode()
            return 200, self.result(delivery_id)

        with StateV2Store(self.state_path) as state:
            invoice = self.seed_handled_archive(state)
            client = DeliveryClient(self.settings, post_once=post_once, environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"})
            with mock.patch.object(state, "record_delivery_outcome", side_effect=StateError("synthetic outcome persistence failure")):
                first = client.deliver(state, invoice, self.archive_path, RUN1, logger=logger)
            row_after_failure = state.delivery_for_invoice(invoice["invoice_id"])
            second = client.deliver(state, invoice, self.archive_path, RUN2, logger=logger)
            row_after_replay = state.delivery_for_invoice(invoice["invoice_id"])

        self.assertEqual("DELIVERY_OUTCOME_UNCERTAIN", first.state)
        self.assertTrue(first.dispatched)
        self.assertEqual("DELIVERY_OUTCOME_UNCERTAIN", second.state)
        self.assertFalse(second.dispatched)
        self.assertIsNotNone(row_after_failure["dispatch_started_at_utc"])
        self.assertEqual("DELIVERY_OUTCOME_UNCERTAIN", row_after_replay["state"])
        self.assertEqual(["POST"], posts)
        self.assertEqual(("delivery_outcome", "DELIVERY_OUTCOME_UNCERTAIN"), logger.events[2][:2])
        self.assertEqual(("delivery_no_send", "DELIVERY_OUTCOME_UNCERTAIN"), logger.events[-1][:2])

    def test_delivery_boundary_interruptions_preserve_last_evidence_and_replay_authority(self) -> None:
        cases = (
            "intent_before", "intent_after", "marker_before", "marker_after",
            "post_before", "post_after", "outcome_before", "outcome_after",
        )
        original_paths = (self.archive, self.drive_root, self.state_path, self.archive_path, self.info)

        for case in cases:
            with self.subTest(boundary=case):
                phase_root = self.root / f"r20-{case}"
                self.archive = phase_root / "archive"
                self.drive_root = phase_root / "drive"
                self.state_path = phase_root / "state" / "state.sqlite3"
                self.archive_path = self.archive / "EB Bill" / "2026-10-01.pdf"
                self.archive_path.parent.mkdir(parents=True)
                self.drive_root.mkdir(parents=True)
                self.state_path.parent.mkdir(parents=True)
                create_v2_database(self.state_path)
                self.archive_path.write_bytes(self.payload)
                self.info = validate_pdf(self.archive_path)
                posts: list[str] = []
                logger = RecordingStageLogger()

                def post_once(url, authorization, body, timeout, *, content_type):
                    if case == "post_before":
                        raise KeyboardInterrupt("synthetic interruption before POST")
                    posts.append("POST")
                    if case == "post_after":
                        raise KeyboardInterrupt("synthetic interruption after POST")
                    delivery_id = __import__("re").search(rb'"delivery_id":"(egmail-v1-[0-9a-f]{32})"', body).group(1).decode()
                    return 200, self.result(delivery_id)

                def stop_once(original, *, after: bool):
                    def call(*args, **kwargs):
                        if after:
                            original(*args, **kwargs)
                        raise KeyboardInterrupt(f"synthetic interruption at {case}")
                    return call

                with StateV2Store(self.state_path) as state:
                    invoice = self.seed_handled_archive(state)
                    client = DeliveryClient(
                        self.settings, post_once=post_once,
                        environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"},
                    )
                    target_method = {
                        "intent": "prepare_delivery",
                        "marker": "claim_delivery_dispatch",
                        "outcome": "record_delivery_outcome",
                    }.get(case.split("_", 1)[0])
                    if target_method:
                        original = getattr(state, target_method)
                        with mock.patch.object(
                            state, target_method, new=stop_once(original, after=case.endswith("after")),
                        ):
                            with self.assertRaises(KeyboardInterrupt):
                                client.deliver(state, invoice, self.archive_path, RUN1, logger=logger)
                    else:
                        with self.assertRaises(KeyboardInterrupt):
                            client.deliver(state, invoice, self.archive_path, RUN1, logger=logger)
                    row_after_interrupt = state.delivery_for_invoice(invoice["invoice_id"])

                if case.startswith("intent_"):
                    self.assertEqual([], logger.events)
                elif case.startswith("marker_"):
                    self.assertEqual("delivery_intent", logger.events[-1][0])
                else:
                    self.assertEqual("dispatch_start", logger.events[-1][0])

                restart_logger = RecordingStageLogger()
                with StateV2Store(self.state_path) as state:
                    invoice = state.invoice(invoice["invoice_id"])
                    replay = DeliveryClient(
                        self.settings, post_once=post_once,
                        environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"},
                    ).deliver(state, invoice, self.archive_path, RUN2, logger=restart_logger)
                    final_row = state.delivery_for_invoice(invoice["invoice_id"])

                if case in {"intent_before", "intent_after", "marker_before"}:
                    self.assertEqual("DELIVERED", replay.state)
                    self.assertEqual("DELIVERED", final_row["state"])
                    self.assertEqual(["POST"], posts)
                elif case in {"marker_after", "post_before", "post_after", "outcome_before"}:
                    self.assertEqual("DELIVERY_OUTCOME_UNCERTAIN", replay.state)
                    self.assertEqual("DELIVERY_OUTCOME_UNCERTAIN", final_row["state"])
                    self.assertEqual(["POST"] if case in {"post_after", "outcome_before"} else [], posts)
                else:
                    self.assertEqual("DELIVERED", replay.state)
                    self.assertEqual("DELIVERED", final_row["state"])
                    self.assertEqual(["POST"], posts)
                self.archive, self.drive_root, self.state_path, self.archive_path, self.info = original_paths

    def test_observer_failure_at_each_delivery_stage_preserves_effects_and_replay(self) -> None:
        from fixtures.synthetic_dual_stream import create_v2_database

        for phase_to_fail in ("delivery_intent", "dispatch_start", "delivery_outcome", "delivery_no_send"):
            with self.subTest(phase=phase_to_fail):
                phase_root = self.root / f"observer-{phase_to_fail}"
                database = phase_root / "state.sqlite3"
                database.parent.mkdir(parents=True)
                create_v2_database(database)
                original_paths = (self.archive, self.drive_root, self.archive_path)
                self.archive = phase_root / "archive"
                self.drive_root = phase_root / "drive"
                self.archive_path = self.archive / "EB Bill" / "2026-10-01.pdf"
                self.archive_path.parent.mkdir(parents=True)
                self.drive_root.mkdir()
                self.archive_path.write_bytes(self.payload)

                class SelectedFailureLogger:
                    def event(inner_self, phase, status=None, **fields):
                        if phase == phase_to_fail:
                            raise OSError("synthetic observer failure")

                posts: list[str] = []

                def post_once(url, authorization, body, timeout, *, content_type):
                    posts.append("POST")
                    delivery_id = __import__("re").search(rb'"delivery_id":"(egmail-v1-[0-9a-f]{32})"', body).group(1).decode()
                    return 200, self.result(delivery_id)

                with StateV2Store(database) as state:
                    invoice = self.seed_handled_archive(state)
                    client = DeliveryClient(
                        self.settings, post_once=post_once,
                        environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"},
                    )
                    first = client.deliver(state, invoice, self.archive_path, RUN1, logger=SelectedFailureLogger())
                    second = client.deliver(state, invoice, self.archive_path, RUN2, logger=SelectedFailureLogger())
                    row = state.delivery_for_invoice(invoice["invoice_id"])

                self.assertEqual(("DELIVERED", "DELIVERED"), (first.state, second.state))
                self.assertEqual("DELIVERED", row["state"])
                self.assertEqual(["POST"], posts)
                self.archive, self.drive_root, self.archive_path = original_paths

    def test_logger_failure_cannot_change_delivery_or_replay_behavior(self) -> None:
        class BrokenLogger:
            def event(self, phase: str, status: str | None = None, **fields: object) -> None:
                raise OSError("private logger failure")

        posts = 0

        def post_once(url, authorization, body, timeout, *, content_type):
            nonlocal posts
            posts += 1
            delivery_id = __import__("re").search(rb'"delivery_id":"(egmail-v1-[0-9a-f]{32})"', body).group(1).decode()
            return 200, self.result(delivery_id)

        with StateV2Store(self.state_path) as state:
            invoice = self.seed_handled_archive(state)
            client = DeliveryClient(self.settings, post_once=post_once, environ={"ENERGYGRID_DELIVERY_TOKEN": "SYNTHETIC_TOKEN"})
            first = client.deliver(state, invoice, self.archive_path, RUN1, logger=BrokenLogger())
            second = client.deliver(state, invoice, self.archive_path, RUN2, logger=BrokenLogger())
            self.assertEqual(("DELIVERED", "DELIVERED"), (first.state, second.state))
            self.assertEqual(1, posts)
            self.assertIsNotNone(state.delivery_for_invoice(invoice["invoice_id"])["dispatch_started_at_utc"])

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
