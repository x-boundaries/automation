"""#226 G3 deterministic core: plan/command contract, pre-generated Drive file
ID idempotency, receipt verification, Drive-before-email and no-resend.

Synthetic only: the fake Drive service models the n8n Drive workflow and the
relevant Google semantics; nothing contacts Google, n8n, SMTP or EnergyGrid.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from energygrid_bill_downloader.config import RUNTIME_V3_SCHEMA, DeliverySettings, DualRuntimeConfig
from energygrid_bill_downloader.drive import DriveClient
from energygrid_bill_downloader.errors import RunLockedError, StateError
from energygrid_bill_downloader.invoice import Stream
from energygrid_bill_downloader.orchestration import ALLOWED_COMMAND_LINES, CoreContext, compute_status, run_command
from energygrid_bill_downloader.state import StateV3Store

from fixtures.synthetic_delivery import synthetic_pdf
from fixtures.synthetic_dual_stream import (
    SYNTHETIC_FOLDERS,
    FakeDelivery,
    FakeDriveService,
    SyntheticAdapter,
    bind_synthetic_drive,
    candidate,
    create_v3_database,
    snapshot,
    synthetic_drive_settings,
    test_stream_entries,
)


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def event(self, phase, status=None, **fields):
        self.events.append((phase, status, fields))


class CoreHarness:
    """Simulates the supervisor/Claude loop: plan -> run next argv -> repeat."""

    def __init__(self, test: unittest.TestCase, *, bound=("EB_BILL", "TENANT_BILL"), drive_bound=("EB_BILL", "TENANT_BILL")) -> None:
        self.test = test
        self.temp = tempfile.TemporaryDirectory()
        test.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.archive = root / "archive"
        self.archive.mkdir()
        self.state_path = root / "state" / "state.sqlite3"
        self.state_path.parent.mkdir()
        create_v3_database(self.state_path, bound=tuple(Stream(item) for item in bound))
        bind_synthetic_drive(self.state_path, drive_bound)
        self.config = DualRuntimeConfig(
            archive_root=self.archive, state_path=self.state_path, temp_root=root / "temp", log_root=root / "logs",
            streams=test_stream_entries(bound=tuple(Stream(item) for item in bound)),
            drive=synthetic_drive_settings(streams=drive_bound),
            delivery=DeliverySettings(url="http://127.0.0.1:5678/webhook/SYNTHETIC", auth_header_name="X-Synthetic",
                                      auth_token_env="ENERGYGRID_DELIVERY_TOKEN", max_pdf_bytes=1_000_000, timeout_seconds=5),
            schema=RUNTIME_V3_SCHEMA,
        )
        self.config.preflight()
        self.drive = FakeDriveService()
        self.delivery = FakeDelivery()
        self.eb = [candidate(Stream.EB_BILL, name="eb-old.pdf", invoice_date="2026-09-01"),
                   candidate(Stream.EB_BILL, name="eb-latest.pdf", invoice_date="2026-10-01")]
        self.tenant = [candidate(Stream.TENANT_BILL, name="tenant-latest.pdf", invoice_date="2026-10-02")]
        self.adapters = None
        self.outputs: list[str] = []
        self.logger = RecordingLogger()

    def new_adapters(self):
        payloads = {item.source_filename: synthetic_pdf(item.source_filename.encode()) for item in (*self.eb, *self.tenant)}
        self.adapters = {
            "EB_BILL": SyntheticAdapter(snapshot(Stream.EB_BILL, tuple(self.eb)), payloads),
            "TENANT_BILL": SyntheticAdapter(snapshot(Stream.TENANT_BILL, tuple(self.tenant)), payloads),
        }
        return self.adapters

    def core(self, run_id: str) -> CoreContext:
        return CoreContext(
            self.config, run_id=run_id, logger=self.logger, adapters_factory=self.new_adapters,
            drive_client=DriveClient(self.config.drive, post_once=self.drive.post_once,
                                     environ={"ENERGYGRID_DRIVE_TOKEN": "synthetic-drive-token"}),
            delivery_client=self.delivery,
        )

    def run(self, command: str, stream: str | None = None, *, run_id: str) -> tuple[dict, int]:
        document, code = run_command(command, stream, self.core(run_id))
        self.outputs.append(json.dumps(document, sort_keys=True))
        return document, code

    def loop(self, run_id: str, *, limit: int = 30) -> list[tuple[str, int]]:
        """Follow `plan.next.argv` until the plan is terminal or a command fails."""
        steps = []
        for _ in range(limit):
            plan, code = self.run("plan", run_id=run_id)
            self.test.assertEqual(0, code)
            argv = plan["next"]["argv"]
            if argv is None:
                return steps
            self.test.assertIn(argv, ALLOWED_COMMAND_LINES)
            parts = argv.split()
            command = parts[1]
            stream = parts[3] if len(parts) == 4 else None
            _document, code = self.run(command, stream, run_id=run_id)
            steps.append((argv, code))
            if code != 0:
                return steps
        raise AssertionError("plan did not converge")

    def status(self, run_id: str) -> dict:
        document, code = self.run("status", run_id=run_id)
        self.test.assertEqual(0, code)
        return document

    def operation(self, stream: str) -> dict | None:
        with StateV3Store(self.state_path, read_only=True) as state:
            invoice_id = state.stream(stream)["watermark_invoice_id"]
            if invoice_id is None:
                return None
            binding = state.active_binding(stream)
            return state.drive_operation_for(invoice_id, binding["binding_id"])

    def dispatches(self, stream: str) -> list[dict]:
        operation = self.operation(stream)
        with StateV3Store(self.state_path, read_only=True) as state:
            return state.drive_dispatches(operation["operation_id"])

    def drive_until(self, run_id: str, stream: str, command: str) -> None:
        """Advance one stream until its planned action is `command` (not executing it)."""
        for _ in range(10):
            plan, _code = self.run("plan", run_id=run_id)
            action = plan["streams"][stream]["action"]
            mapping = {"ACQUIRE_LATEST": "acquire", "DRIVE_PREPARE": "drive-intent", "DRIVE_UPLOAD": "drive-upload",
                       "DRIVE_RECONCILE": "drive-reconcile", "EMAIL_DELIVER": "deliver", "EMAIL_RECONCILE": "deliver"}
            if mapping.get(action) == command:
                return
            next_command = mapping[action]
            self.test.assertEqual(0, self.run(next_command, None if next_command == "acquire" else stream, run_id=run_id)[1])
        raise AssertionError("did not reach the requested action")


def run_id() -> str:
    return str(uuid.uuid4())


class CoreHappyPathTests(unittest.TestCase):
    def test_full_sequence_verifies_drive_before_one_email_per_stream_then_replay_is_no_work(self) -> None:
        harness = CoreHarness(self)
        first = run_id()
        steps = harness.loop(first)
        self.assertTrue(all(code == 0 for _argv, code in steps), steps)
        self.assertEqual([
            "egcore.cmd acquire",
            "egcore.cmd drive-intent --stream EB_BILL", "egcore.cmd drive-upload --stream EB_BILL",
            "egcore.cmd deliver --stream EB_BILL",
            "egcore.cmd drive-intent --stream TENANT_BILL", "egcore.cmd drive-upload --stream TENANT_BILL",
            "egcore.cmd deliver --stream TENANT_BILL",
        ], [argv for argv, _code in steps])
        self.assertEqual(["EB_BILL", "TENANT_BILL"], harness.delivery.sent)
        self.assertEqual(["eb-latest.pdf"], harness.adapters["EB_BILL"].acquire_calls)
        for stream in ("EB_BILL", "TENANT_BILL"):
            operation = harness.operation(stream)
            self.assertEqual("DRIVE_VERIFIED", operation["state"])
            self.assertEqual("SHA256", operation["verification_method"])
            self.assertEqual(operation["reserved_remote_file_id"], operation["remote_file_id"])
            self.assertEqual(1, operation["upload_attempt_count"])
            properties = json.loads(operation["app_properties_json"])
            self.assertEqual(["egApp", "egBnd", "egInv", "egOp", "egSchema", "egSha", "egStream"], sorted(properties))
        status = harness.status(first)
        self.assertEqual("COMPLETED", status["business_outcome"])
        self.assertTrue(status["terminal"])
        self.assertFalse(status["uncertainty_outstanding"])

        second = run_id()
        creates_before = list(harness.drive.creates)
        steps = harness.loop(second)
        self.assertEqual([("egcore.cmd acquire", 0)], steps)
        self.assertEqual([], harness.adapters["EB_BILL"].acquire_calls, "zero FETCH on replay")
        self.assertEqual(creates_before, harness.drive.creates, "zero upload on replay")
        self.assertEqual(["EB_BILL", "TENANT_BILL"], harness.delivery.sent, "zero resend on replay")
        status = harness.status(second)
        self.assertEqual("NO_WORK", status["business_outcome"])
        self.assertTrue(all(item["fully_handled"] for item in status["streams"].values()))

    def test_outputs_carry_no_ids_dates_paths_or_tokens(self) -> None:
        harness = CoreHarness(self)
        harness.loop(run_id())
        joined = "\n".join(harness.outputs)
        forbidden = [*SYNTHETIC_FOLDERS.values(), "synthDriveFile", "2026-", "eb-latest", str(harness.archive),
                     "synthetic-drive-token", "egdb3-", "egmail-v1-", "SYNTHETIC-ENERGYGRID-NAMESPACE"]
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, joined)
        operation = harness.operation("EB_BILL")
        self.assertNotIn(operation["operation_id"], joined)
        self.assertNotIn(operation["invoice_id"], joined)


class PregeneratedIdTests(unittest.TestCase):
    def test_reservation_happens_once_and_is_frozen_before_the_dispatch_marker(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        observed: list[tuple] = []
        original = harness.drive.post_once

        def spy(url, authorization, body, timeout, *, content_type):
            from fixtures.synthetic_dual_stream import parse_multipart

            metadata, _pdf = parse_multipart(body, content_type)
            if metadata["mode"] == "UPLOAD_IF_ABSENT":
                # At the moment the upload leaves the core, the reserved ID and the
                # dispatch marker carrying that same ID are already durable.
                operation = harness.operation("EB_BILL")
                observed.append((operation["reserved_remote_file_id"], [d["reserved_remote_file_id"] for d in harness.dispatches("EB_BILL")],
                                 metadata["reserved_file_id"]))
            return original(url, authorization, body, timeout, content_type=content_type)

        harness.drive.post_once = spy
        _document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual(0, code)
        self.assertEqual(["RESERVE_ID", "UPLOAD_IF_ABSENT"], harness.drive.calls)
        self.assertEqual(1, len(harness.drive.generated))
        reserved = harness.drive.generated[0]
        self.assertEqual([(reserved, [reserved], reserved)], observed)
        self.assertEqual([reserved], harness.drive.creates)

    def test_invalid_or_lost_reservation_uploads_nothing_and_a_later_call_may_reserve_again(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.drop_reservation_response = True
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((10, "RESERVATION_UNAVAILABLE", False), (code, document["outcome"], document["mutated"]))
        operation = harness.operation("EB_BILL")
        self.assertIsNone(operation["reserved_remote_file_id"])
        self.assertEqual(0, operation["upload_attempt_count"])
        self.assertEqual([], harness.dispatches("EB_BILL"))
        self.assertEqual({}, harness.drive.files)
        harness.drive.drop_reservation_response = False
        self.assertEqual(0, harness.run("drive-upload", "EB_BILL", run_id=current)[1])
        operation = harness.operation("EB_BILL")
        self.assertEqual(harness.drive.generated[1], operation["reserved_remote_file_id"])
        self.assertEqual("DRIVE_VERIFIED", operation["state"])

    def test_crash_after_reservation_reuses_the_same_frozen_id(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        operation = harness.operation("EB_BILL")
        with StateV3Store(harness.state_path) as state:
            self.assertTrue(state.reserve_drive_file_id(operation["operation_id"], "synthReservedBeforeCrash", current, "2026-10-06T00:00:00+00:00"))
        # Process dies here: no dispatch marker, no upload. The next run reuses the ID.
        later = run_id()
        harness.loop(later)
        operation = harness.operation("EB_BILL")
        self.assertEqual("synthReservedBeforeCrash", operation["reserved_remote_file_id"])
        self.assertEqual("synthReservedBeforeCrash", operation["remote_file_id"])
        self.assertEqual([], harness.drive.generated, "no second ID was generated")
        self.assertEqual(["synthReservedBeforeCrash"], harness.drive.creates)

    def test_crash_after_dispatch_marker_before_post_reconciles_then_retries_same_id_next_run(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        first = run_id()
        harness.drive_until(first, "EB_BILL", "drive-upload")
        operation = harness.operation("EB_BILL")
        with StateV3Store(harness.state_path) as state:
            state.reserve_drive_file_id(operation["operation_id"], "synthReservedMarkerCrash", first, "2026-10-06T00:00:00+00:00")
            state.begin_drive_dispatch(operation["operation_id"], first, "2026-10-06T00:00:01+00:00")
        plan, _ = harness.run("plan", run_id=first)
        self.assertEqual("DRIVE_RECONCILE", plan["streams"]["EB_BILL"]["action"])
        document, code = harness.run("drive-reconcile", "EB_BILL", run_id=first)
        self.assertEqual((20, "EG_DRIVE_RETRY_NEXT_RUN"), (code, document["support_ref"]))
        self.assertEqual("RECOVERED_MARKER", harness.dispatches("EB_BILL")[0]["outcome"])
        self.assertEqual("DRIVE_UPLOAD_UNCERTAIN", harness.operation("EB_BILL")["state"])
        self.assertEqual("DRIVE_UNCERTAIN", harness.status(first)["business_outcome"])
        second = run_id()
        steps = harness.loop(second)
        self.assertIn(("egcore.cmd drive-reconcile --stream EB_BILL", 0), steps)
        operation = harness.operation("EB_BILL")
        self.assertEqual(("DRIVE_VERIFIED", 2), (operation["state"], operation["upload_attempt_count"]))
        self.assertEqual(["synthReservedMarkerCrash", "synthReservedMarkerCrash"],
                         [item["reserved_remote_file_id"] for item in harness.dispatches("EB_BILL")])
        self.assertEqual(["synthReservedMarkerCrash"], harness.drive.creates)
        self.assertEqual(["EB_BILL"], harness.delivery.sent)

    def test_lost_upload_response_after_successful_create_reconciles_by_reserved_id(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.lose_response_after_create = True
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((20, "DRIVE_UPLOAD_UNCERTAIN"), (code, document["outcome"]))
        self.assertEqual("NO_VALID_RESULT", harness.dispatches("EB_BILL")[0]["outcome"])
        self.assertEqual([], harness.delivery.sent, "no email while Drive is uncertain")
        harness.drive.lose_response_after_create = False
        steps = harness.loop(current)
        self.assertEqual("egcore.cmd drive-reconcile --stream EB_BILL", steps[0][0])
        operation = harness.operation("EB_BILL")
        self.assertEqual("DRIVE_VERIFIED", operation["state"])
        self.assertEqual(1, operation["upload_attempt_count"])
        self.assertEqual(1, len(harness.drive.creates))
        self.assertEqual(["EB_BILL"], harness.delivery.sent)

    def test_timeout_then_same_id_retry_answered_409_is_read_back_and_verified_without_duplicate(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        first = run_id()
        harness.drive_until(first, "EB_BILL", "drive-upload")
        harness.drive.lose_response_after_create = True
        harness.run("drive-upload", "EB_BILL", run_id=first)
        harness.drive.lose_response_after_create = False
        reserved = harness.operation("EB_BILL")["reserved_remote_file_id"]
        # Next run: an eventually-consistent lookup misses the object, so the
        # core sees a positively complete NOT_FOUND and authorises one retry.
        hidden = harness.drive.files.pop(reserved)
        second = run_id()
        self.assertEqual(0, harness.run("acquire", run_id=second)[1])
        document, code = harness.run("drive-reconcile", "EB_BILL", run_id=second)
        self.assertEqual((0, "RETRY_AUTHORISED"), (code, document["outcome"]))
        harness.drive.files[reserved] = hidden
        harness.drive.stale_lookup_once = True
        document, code = harness.run("drive-upload", "EB_BILL", run_id=second)
        self.assertEqual((0, "DRIVE_VERIFIED"), (code, document["outcome"]))
        self.assertEqual(1, harness.drive.create_conflicts, "the same-ID create was answered 409")
        self.assertEqual([reserved], harness.drive.creates, "no second Drive file")
        self.assertEqual([reserved, reserved], [d["reserved_remote_file_id"] for d in harness.dispatches("EB_BILL")])
        self.assertEqual(reserved, harness.operation("EB_BILL")["remote_file_id"])

    def test_409_readback_with_conflicting_bytes_is_conflict_not_verified(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.mutate_after_create = lambda item: item.update(bytes=item["bytes"] + b"x", sha256="0" * 64)
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((20, "DRIVE_CONFLICT", "EG_DRIVE_IDENTITY_BYTES_MISMATCH"), (code, document["outcome"], document["support_ref"]))
        self.assertEqual([], harness.delivery.sent)

    def test_reserved_object_metadata_conflicts_fail_closed(self) -> None:
        cases = {
            "EG_DRIVE_IDENTITY_OP_MISMATCH": lambda item: item["appProperties"].update(egOp="00000000-0000-4000-8000-000000000000"),
            "EG_DRIVE_IDENTITY_MOVED": lambda item: item.update(parents=["synthOtherFolder000001"]),
            "EG_DRIVE_IDENTITY_TRASHED": lambda item: item.update(trashed=True),
            "EG_DRIVE_MIME_MISMATCH": lambda item: item.update(mimeType="application/octet-stream"),
        }
        for expected, mutate in cases.items():
            with self.subTest(expected=expected):
                harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
                current = run_id()
                harness.drive_until(current, "EB_BILL", "drive-upload")
                harness.drive.mutate_after_create = mutate
                document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
                self.assertEqual((20, "DRIVE_CONFLICT", expected), (code, document["outcome"], document["support_ref"]))
                self.assertEqual("DRIVE_CONFLICT", harness.operation("EB_BILL")["state"])
                harness.loop(current)
                self.assertEqual([], harness.delivery.sent)

    def test_remote_result_with_a_different_file_id_fails_closed(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.mint_different_id = True
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((20, "DRIVE_CONFLICT", "EG_DRIVE_REMOTE_ID_MISMATCH"), (code, document["outcome"], document["support_ref"]))
        operation = harness.operation("EB_BILL")
        self.assertIsNone(operation["remote_file_id"])
        harness.loop(current)
        self.assertEqual([], harness.delivery.sent)

    def test_verified_receipt_cannot_bind_any_id_but_the_reserved_one(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        harness.loop(run_id())
        operation = harness.operation("EB_BILL")
        with closing(sqlite3.connect(harness.state_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE energygrid_drive_operation_v3 SET remote_file_id='synthOtherFile0000001' WHERE operation_id=?",
                                   (operation["operation_id"],))

    def test_two_concurrent_reservations_cannot_freeze_two_ids_and_frozen_id_cannot_be_replaced(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        operation = harness.operation("EB_BILL")
        with StateV3Store(harness.state_path) as first, StateV3Store(harness.state_path) as second:
            self.assertTrue(first.reserve_drive_file_id(operation["operation_id"], "synthConcurrentIdA001", current, "t"))
            self.assertFalse(second.reserve_drive_file_id(operation["operation_id"], "synthConcurrentIdB001", current, "t"))
        self.assertEqual("synthConcurrentIdA001", harness.operation("EB_BILL")["reserved_remote_file_id"])
        with closing(sqlite3.connect(harness.state_path)) as connection:
            for statement in (
                "UPDATE energygrid_drive_operation_v3 SET reserved_remote_file_id='synthConcurrentIdB001' WHERE operation_id=?",
                "UPDATE energygrid_drive_operation_v3 SET reserved_remote_file_id=NULL,reserved_run_id=NULL,reserved_at_utc=NULL WHERE operation_id=?",
            ):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(statement, (operation["operation_id"],))
        with StateV3Store(harness.state_path) as state:
            state.begin_drive_dispatch(operation["operation_id"], current, "t")
            with self.assertRaises(StateError):
                state.finish_drive_operation(operation["operation_id"], run_id=current, timestamp="t",
                                             new_state="DRIVE_UPLOAD_INTENT", authorise_retry=True)
        with closing(sqlite3.connect(harness.state_path)) as connection, self.assertRaises(sqlite3.DatabaseError):
            connection.execute("INSERT INTO energygrid_drive_dispatch_v3 (operation_id,attempt_no,reserved_remote_file_id,dispatch_run_id,dispatched_at_utc) VALUES (?,2,'synthConcurrentIdB001',?,'t')",
                               (operation["operation_id"], current))

    def test_dispatch_requires_a_reserved_id(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        operation = harness.operation("EB_BILL")
        with StateV3Store(harness.state_path) as state, self.assertRaises(StateError):
            state.begin_drive_dispatch(operation["operation_id"], current, "t")


class VerificationTests(unittest.TestCase):
    def upload(self, **switches) -> tuple[CoreHarness, dict, int, str]:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        for key, value in switches.items():
            setattr(harness.drive, key, value)
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        return harness, document, code, current

    def test_md5_and_size_fallback_when_sha256_is_absent(self) -> None:
        harness, document, code, _ = self.upload(report_sha256=False)
        self.assertEqual((0, "DRIVE_VERIFIED"), (code, document["outcome"]))
        operation = harness.operation("EB_BILL")
        self.assertEqual(("MD5_SIZE", None), (operation["verification_method"], operation["remote_sha256"]))

    def test_md5_mismatch_without_sha256_is_conflict(self) -> None:
        harness, document, code, _ = self.upload(report_sha256=False,
                                                 mutate_after_create=lambda item: item.update(md5="f" * 32))
        self.assertEqual((20, "EG_DRIVE_IDENTITY_BYTES_MISMATCH"), (code, document["support_ref"]))

    def test_sha256_mismatch_is_conflict_even_when_md5_matches(self) -> None:
        harness, document, code, _ = self.upload(mutate_after_create=lambda item: item.update(sha256="e" * 64))
        self.assertEqual((20, "DRIVE_CONFLICT"), (code, document["outcome"]))

    def test_no_checksum_holds_never_verifies_and_never_emails_then_later_reconcile_verifies(self) -> None:
        harness, document, code, current = self.upload(report_sha256=False, report_md5=False)
        self.assertEqual((20, "HOLD", "EG_DRIVE_VERIFICATION_UNAVAILABLE"), (code, document["outcome"], document["support_ref"]))
        harness.loop(current)
        self.assertEqual([], harness.delivery.sent, "size alone never authorises email")
        self.assertEqual("DRIVE_CONFLICT", harness.status(current)["business_outcome"])
        harness.drive.report_md5 = True
        later = run_id()
        steps = harness.loop(later)
        self.assertIn(("egcore.cmd drive-reconcile --stream EB_BILL", 0), steps)
        self.assertEqual(("DRIVE_VERIFIED", "MD5_SIZE"), (harness.operation("EB_BILL")["state"], harness.operation("EB_BILL")["verification_method"]))
        self.assertEqual(["EB_BILL"], harness.delivery.sent)
        self.assertEqual(1, len(harness.drive.creates), "the HOLD path never re-uploads")

    def test_http_200_with_a_disagreeing_n8n_outcome_is_never_success(self) -> None:
        harness, document, code, _ = self.upload(n8n_outcome_override="FOUND_EXACT", mutate_after_create=lambda item: item.update(trashed=True))
        self.assertEqual((20, "DRIVE_UPLOAD_UNCERTAIN", "EG_DRIVE_RESULT_DISAGREES"), (code, document["outcome"], document["support_ref"]))

    def test_incomplete_search_is_never_not_found_and_never_authorises_retry(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        first = run_id()
        harness.drive_until(first, "EB_BILL", "drive-upload")
        harness.drive.fail_create_before_google = True
        self.assertEqual(20, harness.run("drive-upload", "EB_BILL", run_id=first)[1])
        harness.drive.fail_create_before_google = False
        harness.drive.incomplete_search = True
        second = run_id()
        self.assertEqual(0, harness.run("acquire", run_id=second)[1])
        document, code = harness.run("drive-reconcile", "EB_BILL", run_id=second)
        self.assertEqual((20, "EG_DRIVE_RECONCILE_UNAVAILABLE"), (code, document["support_ref"]))
        operation = harness.operation("EB_BILL")
        self.assertEqual(("DRIVE_UPLOAD_UNCERTAIN", None), (operation["state"], operation["retry_authorised_run_id"]))
        plan, _ = harness.run("plan", run_id=second)
        self.assertEqual("HOLD", plan["streams"]["EB_BILL"]["action"], "one reconcile per operation per run")

    def test_name_occupied_by_unrelated_file_is_conflict_before_any_create(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.put_file(file_id="synthUnrelatedFile0001", name="2026-10-01.pdf", parent=SYNTHETIC_FOLDERS["EB_BILL"],
                               payload=b"%PDF-1.4 other", app_properties={})
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((20, "EG_DRIVE_NAME_OCCUPIED"), (code, document["support_ref"]))
        self.assertEqual([], harness.drive.creates, "no overwrite, suffix or variant")

    def test_retry_limit_is_two_attempts_then_hold(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        runs = [run_id() for _ in range(3)]
        harness.drive_until(runs[0], "EB_BILL", "drive-upload")
        harness.drive.fail_create_before_google = True
        harness.run("drive-upload", "EB_BILL", run_id=runs[0])
        harness.run("acquire", run_id=runs[1])
        self.assertEqual("RETRY_AUTHORISED", harness.run("drive-reconcile", "EB_BILL", run_id=runs[1])[0]["outcome"])
        self.assertEqual(20, harness.run("drive-upload", "EB_BILL", run_id=runs[1])[1])
        harness.run("acquire", run_id=runs[2])
        document, code = harness.run("drive-reconcile", "EB_BILL", run_id=runs[2])
        self.assertEqual((20, "EG_DRIVE_RETRY_EXHAUSTED"), (code, document["support_ref"]))
        self.assertEqual(2, harness.operation("EB_BILL")["upload_attempt_count"])

    def test_destination_change_holds_without_upload(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.folder_state = "FAIL"
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((20, "EG_DRIVE_DESTINATION_CHANGED"), (code, document["support_ref"]))
        self.assertEqual([], harness.drive.creates)


class CommandContractTests(unittest.TestCase):
    def test_unplanned_and_malformed_commands_are_refused_without_mutation(self) -> None:
        harness = CoreHarness(self)
        current = run_id()
        for command, stream in (("drive-upload", "EB_BILL"), ("deliver", "EB_BILL"), ("drive-intent", "TENANT_BILL")):
            with self.subTest(command=command):
                document, code = harness.run(command, stream, run_id=current)
                self.assertEqual((64, "REFUSED", False), (code, document["outcome"], document["mutated"]))
        for command, stream in (("plan", "EB_BILL"), ("deliver", None), ("deliver", "OTHER"), ("submit-drive-result", None), ("migrate-state", None)):
            with self.subTest(command=command, stream=stream):
                self.assertEqual(64, harness.run(command, stream, run_id=current)[1])
        self.assertEqual(0, harness.run("acquire", run_id=current)[1])
        self.assertEqual(64, harness.run("acquire", run_id=current)[1], "acquire runs once per run")

    def test_drive_before_email_is_enforced_by_plan_and_by_sqlite(self) -> None:
        harness = CoreHarness(self)
        current = run_id()
        harness.run("acquire", run_id=current)
        self.assertEqual(64, harness.run("deliver", "EB_BILL", run_id=current)[1])
        with StateV3Store(harness.state_path) as state:
            invoice = state.invoice(state.stream("EB_BILL")["watermark_invoice_id"])
            metadata = {"schema": "energygrid.invoice_delivery.v1", "stream": "EB_BILL", "bill_date": invoice["bill_date"],
                        "attachment_name": invoice["canonical_filename"], "pdf_byte_size": invoice["byte_size"], "pdf_sha256": invoice["sha256"]}
            with self.assertRaises(StateError):
                state.prepare_delivery(invoice_id=invoice["invoice_id"], metadata=metadata, run_id=current,
                                       timestamp="t", delivery_id="egmail-v1-" + "0" * 32)
        self.assertEqual([], harness.delivery.sent)

    def test_email_uncertainty_never_resends_and_never_uploads(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        harness.delivery.outcome = "DELIVERY_OUTCOME_UNCERTAIN"
        current = run_id()
        harness.loop(current)
        creates = list(harness.drive.creates)
        self.assertEqual(["EB_BILL"], harness.delivery.sent)
        self.assertEqual("EMAIL_UNCERTAIN", harness.status(current)["business_outcome"])
        later = run_id()
        harness.loop(later)
        self.assertEqual(64, harness.run("deliver", "EB_BILL", run_id=later)[1])
        self.assertEqual(["EB_BILL"], harness.delivery.sent, "no resend")
        self.assertEqual(creates, harness.drive.creates, "email uncertainty never re-uploads")

    def test_drive_uncertainty_is_independent_of_the_other_stream(self) -> None:
        harness = CoreHarness(self)
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.lose_response_after_create = True
        harness.run("drive-upload", "EB_BILL", run_id=current)
        harness.drive.lose_response_after_create = False
        harness.drive.drop_reconcile_response = True
        # EB's reconcile is unavailable this run; the plan then moves on to Tenant.
        harness.loop(current)
        harness.loop(current)
        self.assertEqual("DRIVE_UPLOAD_UNCERTAIN", harness.operation("EB_BILL")["state"])
        self.assertIn("TENANT_BILL", harness.delivery.sent)
        self.assertNotIn("EB_BILL", harness.delivery.sent)

    def test_binding_mismatch_holds_before_any_network(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        from dataclasses import replace

        settings = harness.config.drive
        other = replace(settings.bindings["EB_BILL"], folder_id="synthDifferentFolder01")
        harness.config = replace(harness.config, drive=replace(settings, bindings={"EB_BILL": other, "TENANT_BILL": None}))
        current = run_id()
        harness.run("acquire", run_id=current)
        plan, _ = harness.run("plan", run_id=current)
        self.assertEqual(("HOLD", "EG_DRIVE_BINDING_MISMATCH"), (plan["streams"]["EB_BILL"]["action"], plan["streams"]["EB_BILL"]["support_ref"]))
        self.assertEqual([], harness.drive.calls)

    def test_unbound_drive_stream_holds_and_status_is_not_success(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",))
        current = run_id()
        harness.loop(current)
        status = harness.status(current)
        self.assertEqual("EG_DRIVE_BINDING_UNBOUND", status["streams"]["TENANT_BILL"]["support_ref"])
        self.assertEqual("HOLD", status["business_outcome"])
        self.assertTrue(status["streams"]["EB_BILL"]["fully_handled"])

    def test_run_lock_contention_is_retryable_without_mutation(self) -> None:
        harness = CoreHarness(self)

        class Held:
            def __init__(self, _path):
                pass

            def __enter__(self):
                raise RunLockedError()

            def __exit__(self, *args):
                return None

        core = harness.core(run_id())
        core.lock_factory = Held
        document, code = run_command("acquire", None, core)
        self.assertEqual((10, "RUN_IN_PROGRESS", False), (code, document["outcome"], document["mutated"]))

    def test_status_before_acquire_is_incomplete_not_success(self) -> None:
        harness = CoreHarness(self)
        status = harness.status(run_id())
        self.assertEqual(("INCOMPLETE", False), (status["business_outcome"], status["terminal"]))


if __name__ == "__main__":
    unittest.main()
