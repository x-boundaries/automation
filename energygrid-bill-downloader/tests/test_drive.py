from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from energygrid_bill_downloader.config import DeliverySettings
from energygrid_bill_downloader.invoice import Stream
from energygrid_bill_downloader.publication import FileInfo, validate_pdf
from energygrid_bill_downloader.state import StateV2Store, StreamStateConflictError
from energygrid_bill_downloader.drive import DriveStager
from fixtures.synthetic_delivery import synthetic_pdf
from fixtures.synthetic_dual_stream import SYNTHETIC_EVIDENCE, SYNTHETIC_NAMESPACE, candidate, create_v2_database


RUN1 = "00000000-0000-0000-0000-000000000001"
RUN2 = "00000000-0000-0000-0000-000000000002"


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

    def seed_invoice_at(self, state: StateV2Store, name: str, invoice_date: str, stream: Stream = Stream.EB_BILL) -> tuple[dict, Path, bytes, FileInfo]:
        item = candidate(stream, name=name, invoice_date=invoice_date)
        payload = synthetic_pdf(name.encode())
        folder = "EB Bill" if stream is Stream.EB_BILL else "Tenant Bill"
        archive_path = self.archive / folder / f"{invoice_date}.pdf"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(payload)
        info = validate_pdf(archive_path)
        invoice_id = state.accept_latest(item, "00000000-0000-0000-0000-000000000001", f"{invoice_date}T00:00:00+00:00")
        invoice = state.invoice(invoice_id)
        operation = state.start_file_operation(
            operation_id=str(uuid.uuid4()), invoice_id=invoice_id, kind="ARCHIVE_PUBLISH",
            private_path_ref=str(self.root / f"{name}.source"), target_relpath=invoice["archive_relpath"],
            binding_id=None, info=info, source_role="SOURCE_ACQUISITION",
            run_id="00000000-0000-0000-0000-000000000001", timestamp=f"{invoice_date}T00:00:01+00:00",
        )
        state.complete_file_operation(operation["operation_id"], f"{invoice_date}T00:00:02+00:00", evidence_ref="EG_ARCHIVE_TEST")
        state.update_invoice_file_state(invoice_id, archive_state="COMMITTED", byte_size=info.byte_size, sha256=info.sha256, archived_at_utc=f"{invoice_date}T00:00:02+00:00")
        return state.invoice(invoice_id), archive_path, payload, info

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
            with self.assertRaises(StreamStateConflictError):
                stager.stage(state, invoice, self.archive_path, "00000000-0000-0000-0000-000000000001")
            self.assertEqual(self.payload, target.read_bytes())
            self.assertIsNone(state.file_operation(invoice["invoice_id"], "DRIVE_STAGE", "SYNTHETIC_DRIVE_BINDING", "EB Bill/2026-10-01.pdf"))

    def test_prepared_copy_is_recovered_from_its_exact_private_path(self) -> None:
        with StateV2Store(self.state_path) as state:
            invoice = self.seed_invoice(state)
            target = self.drive_root / "EB Bill" / "2026-10-01.pdf"
            target.parent.mkdir()
            operation_id = str(uuid.uuid4())
            resolved_target = target.resolve()
            temp_path = resolved_target.with_name(f".{resolved_target.name}.{operation_id}.tmp")
            temp_path.write_bytes(self.payload)
            operation = state.start_file_operation(
                operation_id=operation_id, invoice_id=invoice["invoice_id"], kind="DRIVE_STAGE",
                private_path_ref=str(temp_path), target_relpath="EB Bill/2026-10-01.pdf",
                binding_id="SYNTHETIC_DRIVE_BINDING", info=self.info, source_role="ARCHIVE_COMMITTED",
                run_id="00000000-0000-0000-0000-000000000001", timestamp="2026-10-02T00:00:02+00:00",
            )
            self.assertEqual(temp_path, Path(operation["private_path_ref"]))
            self.assertEqual("ARCHIVE_COMMITTED", operation["source_role"])
            self.assertEqual(self.info.byte_size, operation["expected_size"])
            self.assertEqual(self.info.sha256, operation["expected_sha256"])
            self.assertEqual(temp_path.parent, (self.drive_root / invoice["archive_relpath"]).resolve().parent)
            self.assertEqual(temp_path.name, f".{(self.drive_root / invoice['archive_relpath']).name}.{operation_id}.tmp")
            stager = DriveStager(self.archive, self.drive_root, "SYNTHETIC_DRIVE_BINDING")
            stager.stage(state, invoice, self.archive_path, "00000000-0000-0000-0000-000000000001")
            self.assertEqual(self.payload, target.read_bytes())
            self.assertEqual("DRIVE_STAGED", state.invoice(invoice["invoice_id"])["drive_state"])

    def test_prepared_drive_recovery_matrix_and_no_temp_recreation(self) -> None:
        cases = (
            "source_only", "target_only", "both", "both_different_bytes", "both_hardlink", "neither",
            "wrong_source_bytes", "wrong_target_bytes", "wrong_path", "wrong_target_path", "wrong_binding",
            "wrong_role", "wrong_hash", "wrong_size",
        )
        with StateV2Store(self.state_path) as state:
            for day, label in enumerate(cases, start=1):
                invoice_date = f"2026-11-{day:02d}"
                invoice, archive_path, payload, info = self.seed_invoice_at(state, f"drive-{label}.pdf", invoice_date)
                target = self.drive_root / invoice["archive_relpath"]
                target.parent.mkdir(parents=True, exist_ok=True)
                resolved_target = target.resolve()
                operation_id = str(uuid.uuid4())
                temp_path = resolved_target.with_name(f".{resolved_target.name}.{operation_id}.tmp")
                journal_path = temp_path.with_name(".wrong-private-path.tmp") if label == "wrong_path" else temp_path
                role = "WRONG_ROLE" if label == "wrong_role" else "ARCHIVE_COMMITTED"
                binding_id = "WRONG_DRIVE_BINDING" if label == "wrong_binding" else "SYNTHETIC_DRIVE_BINDING"
                operation_target = invoice["archive_relpath"] + ".wrong" if label == "wrong_target_path" else invoice["archive_relpath"]
                expected = info
                if label == "wrong_hash":
                    expected = FileInfo(info.byte_size, "0" * 64)
                elif label == "wrong_size":
                    expected = FileInfo(info.byte_size + 1, info.sha256)
                state.start_file_operation(
                    operation_id=operation_id, invoice_id=invoice["invoice_id"], kind="DRIVE_STAGE",
                    private_path_ref=str(journal_path), target_relpath=operation_target,
                    binding_id=binding_id, info=expected, source_role=role,
                    run_id="00000000-0000-0000-0000-000000000001", timestamp=f"{invoice_date}T00:00:03+00:00",
                )
                if label in {"source_only", "both", "both_different_bytes", "both_hardlink", "wrong_source_bytes", "wrong_hash", "wrong_size"}:
                    temp_path.write_bytes(payload if label != "wrong_source_bytes" else b"wrong staged source")
                if label == "both_hardlink":
                    os.link(temp_path, target)
                elif label == "both_different_bytes":
                    target.write_bytes(synthetic_pdf(b"different staged target"))
                elif label in {"target_only", "both"}:
                    target.write_bytes(payload)
                elif label == "wrong_target_bytes":
                    target.write_bytes(b"wrong staged target")

                stager = DriveStager(self.archive, self.drive_root, "SYNTHETIC_DRIVE_BINDING")
                if label in {"source_only", "target_only"}:
                    stager.stage(state, invoice, archive_path, "00000000-0000-0000-0000-000000000001")
                    self.assertEqual(payload, target.read_bytes())
                    self.assertEqual("DRIVE_STAGED", state.invoice(invoice["invoice_id"])["drive_state"])
                    if label == "source_only":
                        self.assertFalse(temp_path.exists())
                else:
                    with self.assertRaises(StreamStateConflictError):
                        stager.stage(state, invoice, archive_path, "00000000-0000-0000-0000-000000000001")
                    held = state.active_file_operations(invoice["invoice_id"], {"DRIVE_STAGE"})
                    self.assertEqual((1, "HOLD"), (len(held), held[0]["state"]))
                    self.assertEqual(label in {"both", "both_different_bytes", "both_hardlink", "wrong_source_bytes", "wrong_hash", "wrong_size"}, temp_path.exists())
                    self.assertEqual(label in {"both", "both_different_bytes", "both_hardlink", "wrong_target_bytes"}, target.exists())
                    with self.assertRaises(StreamStateConflictError):
                        stager.stage(state, invoice, archive_path, "00000000-0000-0000-0000-000000000001")
                    self.assertEqual("HOLD", state.active_file_operations(invoice["invoice_id"], {"DRIVE_STAGE"})[0]["state"])

    def test_committed_history_is_not_active_and_delivered_missing_drive_gets_one_repair(self) -> None:
        from energygrid_bill_downloader.delivery import DELIVERY_RESULT_SCHEMA
        from energygrid_bill_downloader.delivery import DeliveryClient
        import json
        import re

        calls: list[str] = []

        def post_once(url, authorization, body, timeout, *, content_type):
            calls.append(authorization)
            delivery_id = re.search(rb'"delivery_id":"(egmail-v1-[0-9a-f]{32})"', body).group(1).decode()
            response = json.dumps({
                "schema": DELIVERY_RESULT_SCHEMA, "delivery_id": delivery_id,
                "outcome": "DELIVERED", "duplicate": False, "support_ref": "EG_SYNTHETIC_ACCEPTED",
            }, separators=(",", ":")).encode()
            return 200, response

        with StateV2Store(self.state_path) as state:
            invoice = self.seed_invoice(state)
            stager = DriveStager(self.archive, self.drive_root, "SYNTHETIC_DRIVE_BINDING")
            stager.stage(state, invoice, self.archive_path, "00000000-0000-0000-0000-000000000001")
            invoice = state.invoice(invoice["invoice_id"])
            client = DeliveryClient(
                DeliverySettings(
                    url="http://127.0.0.1:5678/webhook/synthetic", auth_header_name="X-Test",
                    auth_token_env="TEST_TOKEN", max_pdf_bytes=1_000_000, timeout_seconds=5,
                ),
                post_once=post_once, environ={"TEST_TOKEN": "synthetic"},
            )
            delivered = client.deliver(state, invoice, self.archive_path, RUN1)
            self.assertEqual("DELIVERED", delivered.state)
            delivery_before = state.delivery(delivered.delivery_id)
            old_history = state.file_operation_history(invoice["invoice_id"], "DRIVE_STAGE", "SYNTHETIC_DRIVE_BINDING", invoice["archive_relpath"])
            target = self.drive_root / invoice["archive_relpath"]
            target.unlink()
            self.assertEqual([], state.active_file_operations(invoice["invoice_id"], {"DRIVE_STAGE"}))

            stager.stage(state, invoice, self.archive_path, RUN2)
            new_history = state.file_operation_history(invoice["invoice_id"], "DRIVE_STAGE", "SYNTHETIC_DRIVE_BINDING", invoice["archive_relpath"])
            self.assertEqual(1, len(old_history))
            self.assertEqual(2, len(new_history))
            retained_history = next(operation for operation in new_history if operation["operation_id"] == old_history[0]["operation_id"])
            repair_history = next(operation for operation in new_history if operation["operation_id"] != old_history[0]["operation_id"])
            self.assertEqual(old_history[0], retained_history)
            self.assertEqual("COMMITTED", repair_history["state"])
            self.assertEqual(delivery_before, state.delivery(delivered.delivery_id))
            self.assertEqual("DRIVE_STAGED", state.invoice(invoice["invoice_id"])["drive_state"])
            self.assertEqual(self.payload, target.read_bytes())
            stager.stage(state, state.invoice(invoice["invoice_id"]), self.archive_path, RUN2)
            self.assertEqual(2, len(state.file_operation_history(invoice["invoice_id"], "DRIVE_STAGE", "SYNTHETIC_DRIVE_BINDING", invoice["archive_relpath"])))
            self.assertEqual(delivery_before, state.delivery(delivered.delivery_id))
        self.assertEqual(1, len(calls))

    def test_active_drive_operation_for_another_invoice_blocks_target_owner(self) -> None:
        with StateV2Store(self.state_path) as state:
            owner, _owner_archive, _owner_payload, owner_info = self.seed_invoice_at(
                state, "drive-owner.pdf", "2026-12-01", Stream.EB_BILL,
            )
            target_invoice, target_archive, _target_payload, _target_info = self.seed_invoice_at(
                state, "drive-target.pdf", "2026-12-01", Stream.TENANT_BILL,
            )
            target_path = self.drive_root / target_invoice["archive_relpath"]
            temp_path = self.root / "owner-drive-temp.tmp"
            operation = state.start_file_operation(
                operation_id=str(uuid.uuid4()), invoice_id=owner["invoice_id"], kind="DRIVE_STAGE",
                private_path_ref=str(temp_path), target_relpath=target_invoice["archive_relpath"],
                binding_id="SYNTHETIC_DRIVE_BINDING", info=owner_info, source_role="ARCHIVE_COMMITTED",
                run_id=RUN1, timestamp="2026-12-01T00:00:03+00:00",
            )
            stager = DriveStager(self.archive, self.drive_root, "SYNTHETIC_DRIVE_BINDING")
            with self.assertRaises(StreamStateConflictError):
                stager.stage(state, target_invoice, target_archive, RUN1)
            active = state.active_file_operations(owner["invoice_id"], {"DRIVE_STAGE"})
            self.assertEqual(1, len(active))
            self.assertEqual((operation["operation_id"], "HOLD"), (active[0]["operation_id"], active[0]["state"]))
            self.assertFalse(temp_path.exists())
            self.assertFalse(target_path.exists())
            with self.assertRaises(StreamStateConflictError):
                stager.stage(state, target_invoice, target_archive, RUN2)
            self.assertEqual("HOLD", state.active_file_operations(owner["invoice_id"], {"DRIVE_STAGE"})[0]["state"])

    def test_multiple_committed_drive_history_with_equal_timestamps_does_not_hide_repair(self) -> None:
        with StateV2Store(self.state_path) as state:
            invoice = self.seed_invoice(state)
            timestamp = "2020-12-02T00:00:03+00:00"
            for suffix in ("2", "1"):
                operation = state.start_file_operation(
                    operation_id=f"00000000-0000-4000-8000-00000000000{suffix}",
                    invoice_id=invoice["invoice_id"], kind="DRIVE_STAGE",
                    private_path_ref=str(self.drive_root / f"prior-{suffix}.tmp"),
                    target_relpath=invoice["archive_relpath"], binding_id="SYNTHETIC_DRIVE_BINDING",
                    info=self.info, source_role="ARCHIVE_COMMITTED", run_id=RUN1, timestamp=timestamp,
                )
                state.complete_file_operation(operation["operation_id"], timestamp, evidence_ref="EG_DRIVE_STAGE_HASH_VERIFIED")
            history_before = state.file_operation_history(
                invoice["invoice_id"], "DRIVE_STAGE", "SYNTHETIC_DRIVE_BINDING", invoice["archive_relpath"],
            )
            self.assertEqual(2, len(history_before))
            self.assertEqual(timestamp, history_before[0]["committed_at_utc"])
            self.assertEqual(timestamp, history_before[1]["committed_at_utc"])
            self.assertIsNone(state.file_operation(
                invoice["invoice_id"], "DRIVE_STAGE", "SYNTHETIC_DRIVE_BINDING", invoice["archive_relpath"],
            ))

            stager = DriveStager(self.archive, self.drive_root, "SYNTHETIC_DRIVE_BINDING")
            stager.stage(state, invoice, self.archive_path, RUN2)
            history_after = state.file_operation_history(
                invoice["invoice_id"], "DRIVE_STAGE", "SYNTHETIC_DRIVE_BINDING", invoice["archive_relpath"],
            )
            self.assertEqual(3, len(history_after))
            self.assertEqual(history_before, history_after[:2])
            self.assertEqual("COMMITTED", history_after[2]["state"])
            self.assertEqual(self.payload, (self.drive_root / invoice["archive_relpath"]).read_bytes())

    def test_prepared_drive_reparse_source_is_held_without_publication(self) -> None:
        import os
        import subprocess

        with StateV2Store(self.state_path) as state:
            invoice = self.seed_invoice(state)
            target = self.drive_root / invoice["archive_relpath"]
            target.parent.mkdir(parents=True, exist_ok=True)
            operation_id = str(uuid.uuid4())
            temp_path = target.with_name(f".{target.name}.{operation_id}.tmp")
            try:
                temp_path.symlink_to(self.archive_path)
            except OSError:
                if os.name != "nt":
                    self.skipTest("file symlinks are unavailable in this test environment")
                environment = os.environ.copy()
                environment["CODEX_TEST_JUNCTION_PATH"] = str(temp_path)
                environment["CODEX_TEST_JUNCTION_TARGET"] = str(self.archive_path.parent)
                created = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "New-Item -ItemType Junction -Path $env:CODEX_TEST_JUNCTION_PATH -Target $env:CODEX_TEST_JUNCTION_TARGET -ErrorAction Stop | Out-Null"],
                    capture_output=True, text=True, env=environment, check=False,
                )
                if created.returncode != 0:
                    self.skipTest("neither file symlinks nor directory junctions are available")
            state.start_file_operation(
                operation_id=operation_id, invoice_id=invoice["invoice_id"], kind="DRIVE_STAGE",
                private_path_ref=str(temp_path), target_relpath=invoice["archive_relpath"],
                binding_id="SYNTHETIC_DRIVE_BINDING", info=self.info, source_role="ARCHIVE_COMMITTED",
                run_id=RUN1, timestamp="2026-12-03T00:00:03+00:00",
            )
            stager = DriveStager(self.archive, self.drive_root, "SYNTHETIC_DRIVE_BINDING")
            with self.assertRaises(StreamStateConflictError):
                stager.stage(state, invoice, self.archive_path, RUN2)
            self.assertEqual("HOLD", state.active_file_operations(invoice["invoice_id"], {"DRIVE_STAGE"})[0]["state"])
            self.assertTrue(temp_path.is_symlink() or (hasattr(temp_path, "is_junction") and temp_path.is_junction()))
            self.assertFalse(target.exists())
            if hasattr(temp_path, "is_junction") and temp_path.is_junction():
                temp_path.rmdir()


if __name__ == "__main__":
    unittest.main()
