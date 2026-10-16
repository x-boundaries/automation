from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.request

from energygrid_bill_downloader import config as config_module
from energygrid_bill_downloader.connector_actions import (
    ACTION_DRIVE_UPLOAD,
    ACTION_GMAIL_SEND,
    ACTIONS,
    OUTCOME_REJECTED,
    OUTCOME_VERIFIED,
    ActionRequest,
    FakeDriveConnector,
    FakeGmailConnector,
    FakeProviderLedger,
    SimulatedCrash,
    require_fake_connectors,
)
from energygrid_bill_downloader.config import OFFLINE_FAKE_PROVIDER_PROFILE, assert_offline_fake_profile
from energygrid_bill_downloader.errors import OfflineV4Error
from energygrid_bill_downloader.invoice import Stream
from energygrid_bill_downloader.state import V4_ACTIONS, V4_STREAMS
from energygrid_bill_downloader.orchestration import run_offline_dual_stream_v4
from energygrid_bill_downloader.connector_actions import require_fake_adapters
from fixtures.synthetic_dual_stream import SyntheticAdapter

PACKAGE = Path(__file__).resolve().parents[1] / "energygrid_bill_downloader"
# Modules that make up the offline v4 surface. Each one must stay free of provider reach.
V4_MODULES = ("naming.py", "connector_actions.py")
# Names that would mean a real provider, network or process call. Matched exactly.
FORBIDDEN_NAMES = frozenset({
    "smtplib", "socket", "subprocess", "urllib", "urlopen", "requests", "http", "ssl", "Popen",
    "googleapiclient", "google", "drive", "delivery", "http_source", "portal", "notify", "n8n",
    "DirectHttpSource", "DeliveryClient", "create_connection", "webhook", "playwright",
})
PDF_A = b"%PDF-1.4\nA\n%%EOF\n"


class OfflineFakeAdapter(SyntheticAdapter):
    """A test double declared as an offline fake, the only kind the offline core accepts."""

    OFFLINE_FAKE = True


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request(action: str, payload: bytes = PDF_A, *, drive_ref: str | None = None) -> ActionRequest:
    return ActionRequest(
        intent_id="egt4-" + "0" * 32,
        action=action,
        stream="EB_BILL",
        display_filename="OCT 2026 - invoice.pdf",
        sha256=sha(payload),
        byte_size=len(payload),
        payload=payload,
        drive_provider_ref=drive_ref,
    )


class FakeConnectorTests(unittest.TestCase):
    def test_drive_fake_verifies_exact_bytes_and_records_one_object(self) -> None:
        ledger = FakeProviderLedger()
        connector = FakeDriveConnector(ledger)
        outcome = connector.perform(request(ACTION_DRIVE_UPLOAD))
        self.assertEqual(OUTCOME_VERIFIED, outcome.outcome)
        self.assertEqual(sha(PDF_A), outcome.provider_sha256)
        self.assertEqual(1, ledger.count(ACTION_DRIVE_UPLOAD))
        self.assertEqual("OCT 2026 - invoice.pdf", ledger.effects[0]["display_filename"])

    def test_drive_fake_rejects_bytes_that_do_not_match_the_archive_identity(self) -> None:
        ledger = FakeProviderLedger()
        bad = ActionRequest(
            intent_id="egt4-" + "1" * 32, action=ACTION_DRIVE_UPLOAD, stream="EB_BILL",
            display_filename="OCT 2026 - invoice.pdf", sha256="f" * 64, byte_size=len(PDF_A),
            payload=PDF_A, drive_provider_ref=None,
        )
        outcome = FakeDriveConnector(ledger).perform(bad)
        self.assertEqual(OUTCOME_REJECTED, outcome.outcome)
        self.assertEqual(0, len(ledger.effects))

    def test_gmail_fake_refuses_to_attach_without_a_verified_drive_object(self) -> None:
        ledger = FakeProviderLedger()
        outcome = FakeGmailConnector(ledger).perform(request(ACTION_GMAIL_SEND, drive_ref=None))
        self.assertEqual(OUTCOME_REJECTED, outcome.outcome)
        self.assertEqual(0, ledger.count(ACTION_GMAIL_SEND))

    def test_gmail_fake_attaches_the_same_bytes_as_the_verified_drive_object(self) -> None:
        ledger = FakeProviderLedger()
        drive = FakeDriveConnector(ledger).perform(request(ACTION_DRIVE_UPLOAD))
        gmail = FakeGmailConnector(ledger).perform(request(ACTION_GMAIL_SEND, drive_ref=drive.provider_ref))
        self.assertEqual(OUTCOME_VERIFIED, gmail.outcome)
        self.assertEqual(sha(PDF_A), gmail.provider_sha256)
        self.assertEqual(
            [sha(PDF_A), sha(PDF_A)],
            [entry["sha256"] for entry in ledger.effects],
        )

    def test_gmail_fake_refuses_a_drive_reference_whose_bytes_differ(self) -> None:
        ledger = FakeProviderLedger()
        drive = FakeDriveConnector(ledger).perform(request(ACTION_DRIVE_UPLOAD, PDF_A))
        other = b"%PDF-1.4\nB\n%%EOF\n"
        outcome = FakeGmailConnector(ledger).perform(request(ACTION_GMAIL_SEND, other, drive_ref=drive.provider_ref))
        self.assertEqual(OUTCOME_REJECTED, outcome.outcome)
        self.assertEqual(1, len(ledger.effects))

    def test_simulated_crash_happens_after_the_effect_and_is_not_an_exception(self) -> None:
        ledger = FakeProviderLedger()
        connector = FakeDriveConnector(ledger, crash_after_effect=True)
        with self.assertRaises(SimulatedCrash):
            connector.perform(request(ACTION_DRIVE_UPLOAD))
        self.assertEqual(1, ledger.count(ACTION_DRIVE_UPLOAD))
        self.assertFalse(issubclass(SimulatedCrash, Exception))

    def test_a_replayed_provider_object_is_not_recorded_twice(self) -> None:
        ledger = FakeProviderLedger()
        connector = FakeDriveConnector(ledger)
        first = connector.perform(request(ACTION_DRIVE_UPLOAD))
        second = connector.perform(request(ACTION_DRIVE_UPLOAD))
        self.assertEqual(first.provider_ref, second.provider_ref)
        self.assertEqual(1, ledger.count(ACTION_DRIVE_UPLOAD))

    def test_action_names_agree_between_the_facade_and_the_state_schema(self) -> None:
        self.assertEqual(V4_ACTIONS, ACTIONS)
        self.assertEqual(("EB_BILL", "TENANT_BILL"), V4_STREAMS)
        self.assertEqual(("EB_BILL", "TENANT_BILL"), tuple(item.value for item in Stream))


class ConnectorBoundaryTests(unittest.TestCase):
    def test_only_declared_offline_fake_adapters_are_accepted(self) -> None:
        require_fake_adapters({"EB_BILL": OfflineFakeAdapter(None), "TENANT_BILL": OfflineFakeAdapter(None)})
        with self.assertRaises(OfflineV4Error) as caught:
            require_fake_adapters({"EB_BILL": SyntheticAdapter(None)})
        self.assertEqual("EG_V4_ADAPTER_NOT_FAKE", caught.exception.support_ref)
        with self.assertRaises(OfflineV4Error) as caught:
            require_fake_adapters({"EB_BILL": mock.Mock()})
        self.assertEqual("EG_V4_ADAPTER_NOT_FAKE", caught.exception.support_ref)
        with self.assertRaises(OfflineV4Error) as caught:
            require_fake_adapters({"OTHER": OfflineFakeAdapter(None)})
        self.assertEqual("EG_V4_ADAPTER_SET_INVALID", caught.exception.support_ref)

    def test_only_the_two_fake_classes_are_accepted(self) -> None:
        ledger = FakeProviderLedger()
        require_fake_connectors({ACTION_DRIVE_UPLOAD: FakeDriveConnector(ledger), ACTION_GMAIL_SEND: FakeGmailConnector(ledger)})
        with self.assertRaises(OfflineV4Error) as caught:
            require_fake_connectors({ACTION_DRIVE_UPLOAD: mock.Mock(), ACTION_GMAIL_SEND: FakeGmailConnector(ledger)})
        self.assertEqual("EG_V4_CONNECTOR_NOT_FAKE", caught.exception.support_ref)

    def test_a_subclass_of_a_fake_is_refused_because_the_check_is_exact(self) -> None:
        class Shadow(FakeDriveConnector):
            pass

        ledger = FakeProviderLedger()
        with self.assertRaises(OfflineV4Error) as caught:
            require_fake_connectors({ACTION_DRIVE_UPLOAD: Shadow(ledger), ACTION_GMAIL_SEND: FakeGmailConnector(ledger)})
        self.assertEqual("EG_V4_CONNECTOR_NOT_FAKE", caught.exception.support_ref)

    def test_missing_or_extra_actions_are_refused(self) -> None:
        ledger = FakeProviderLedger()
        with self.assertRaises(OfflineV4Error) as caught:
            require_fake_connectors({ACTION_DRIVE_UPLOAD: FakeDriveConnector(ledger)})
        self.assertEqual("EG_V4_CONNECTOR_SET_INVALID", caught.exception.support_ref)

    def test_every_live_profile_is_refused_and_live_enablement_is_off(self) -> None:
        self.assertIs(False, config_module.LIVE_PROVIDER_PROFILE_ENABLED)
        assert_offline_fake_profile(OFFLINE_FAKE_PROVIDER_PROFILE)
        for profile in ("LIVE", "PRODUCTION", "", None, "OFFLINE_FAKE_V4 "):
            with self.subTest(profile=profile), self.assertRaises(OfflineV4Error) as caught:
                assert_offline_fake_profile(profile)
            self.assertEqual("EG_V4_LIVE_PROFILE_REFUSED", caught.exception.support_ref)


class StaticNoProviderProofTests(unittest.TestCase):
    """Source-level proof: the v4 surface imports no provider, network or process module."""

    ALLOWED_IMPORTS = {
        "naming.py": {"__future__", "re", "unicodedata", "datetime", ".errors", ".publication"},
        "connector_actions.py": {"__future__", "hashlib", "dataclasses", ".errors"},
    }

    def _tree(self, name: str) -> ast.Module:
        return ast.parse((PACKAGE / name).read_text(encoding="utf-8"), filename=name)

    def test_v4_modules_import_only_the_reviewed_modules(self) -> None:
        for name in V4_MODULES:
            with self.subTest(module=name):
                imported: set[str] = set()
                for node in ast.walk(self._tree(name)):
                    if isinstance(node, ast.Import):
                        imported.update(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        imported.add("." * node.level + (node.module or ""))
                self.assertLessEqual(imported, self.ALLOWED_IMPORTS[name] | {"typing"})

    def test_v4_modules_reference_no_provider_network_or_process_name(self) -> None:
        for name in V4_MODULES:
            with self.subTest(module=name):
                used = set()
                for node in ast.walk(self._tree(name)):
                    if isinstance(node, ast.Name):
                        used.add(node.id)
                    elif isinstance(node, ast.Attribute):
                        used.add(node.attr)
                    elif isinstance(node, ast.alias):
                        used.add(node.name.split(".")[0])
                self.assertEqual(set(), used & FORBIDDEN_NAMES)

    def test_the_v4_orchestration_and_admission_functions_reach_no_provider_name(self) -> None:
        targets = {
            "orchestration.py": {"run_offline_dual_stream_v4", "_v4_iso", "_v4_rotated", "_v4_stream_step", "_v4_perform",
                                 "_v4_drain", "_v4_stream_outcome", "_v4_overall"},
            "reconcile.py": {"admit_latest_offline_v4", "_v4_support_ref", "_v4_admission_status",
                             "_adopt_predecessor_latest"},
            "state.py": {"StateV4Store", "migrate_state_database_v4", "_migrate_state_database_v4_locked",
                         "_apply_state_database_v4", "predecessor_digest", "_predecessor_digest",
                         "_inspect_v4_database", "_reference_v4_manifest", "_apply_v4_schema", "_move_intent",
                         "_v4_receipt_target", "_v4_receipt_digest", "_v4_invoice_key", "_v4_intent_id",
                         "_v4_archive_relpath", "_refusal_from_sqlite", "_fetch_dicts",
                         "_v4_identity_refusals", "_v4_presentation_conflicts", "_backup_state_file_exclusive",
                         "_v4_added_keys", "_v4_cutover_blockers", "_classify_predecessor_latest", "_insert_admission",
                         "assert_lease_held", "acquire_lease", "release_lease", "check_admissible",
                         "admit_invoice", "record_receipt", "seal_dispatch", "recover_unfinished",
                         "adopted_predecessor", "predecessor_latest", "adopt_predecessor_latest"},
        }
        for module, names in targets.items():
            tree = ast.parse((PACKAGE / module).read_text(encoding="utf-8"), filename=module)
            found: dict[str, set[str]] = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
                    used = set()
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Name):
                            used.add(inner.id)
                        elif isinstance(inner, ast.Attribute):
                            used.add(inner.attr)
                        elif isinstance(inner, (ast.Import, ast.ImportFrom)):
                            used.update(alias.name.split(".")[0] for alias in inner.names)
                            if isinstance(inner, ast.ImportFrom) and inner.module:
                                used.add(inner.module.split(".")[0])
                    found[node.name] = used
            self.assertEqual(names, set(found), module)
            for name, used in found.items():
                with self.subTest(module=module, function=name):
                    self.assertEqual(set(), used & FORBIDDEN_NAMES)


class RuntimeNoProviderProofTests(unittest.TestCase):
    """Runtime proof: a complete offline run opens no socket, starts no process and calls no real provider."""

    def test_full_offline_run_makes_no_network_process_or_real_provider_call(self) -> None:
        from fixtures.synthetic_dual_stream import candidate, snapshot
        from energygrid_bill_downloader.state import StateV4Store, migrate_state_database_v4
        from test_state_v4 import create_predecessor

        calls: list[str] = []

        def recorder(label: str):
            def fail(*args, **kwargs):
                calls.append(label)
                raise AssertionError(f"offline v4 must not call {label}")
            return fail

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state.sqlite"
            create_predecessor(db)
            migrate_state_database_v4(db, apply=True)
            archive = root / "archive"
            archive.mkdir()
            temp = root / "temp"
            temp.mkdir()
            eb = candidate(Stream.EB_BILL, name="Invoice EB.pdf", invoice_date="2026-10-01")
            tn = candidate(Stream.TENANT_BILL, name="Invoice TN.pdf", invoice_date="2026-10-02")
            adapters = {
                "EB_BILL": OfflineFakeAdapter(snapshot(Stream.EB_BILL, (eb,)), {"Invoice EB.pdf": PDF_A}),
                "TENANT_BILL": OfflineFakeAdapter(snapshot(Stream.TENANT_BILL, (tn,)), {"Invoice TN.pdf": PDF_A}),
            }
            ledger = FakeProviderLedger()
            connectors = {ACTION_DRIVE_UPLOAD: FakeDriveConnector(ledger), ACTION_GMAIL_SEND: FakeGmailConnector(ledger)}
            patches = [
                mock.patch.object(socket.socket, "connect", recorder("socket.connect")),
                mock.patch.object(socket, "create_connection", recorder("socket.create_connection")),
                mock.patch.object(urllib.request, "urlopen", recorder("urllib.urlopen")),
                mock.patch.object(subprocess, "Popen", recorder("subprocess.Popen")),
                mock.patch.object(subprocess, "run", recorder("subprocess.run")),
                mock.patch.object(os, "system", recorder("os.system")),
            ]
            for patch in patches:
                patch.start()
            try:
                with StateV4Store(db) as store:
                    result = run_offline_dual_stream_v4(
                        state=store, adapters=adapters, connectors=connectors, archive_root=archive, temp_root=temp,
                        run_id="11111111-2222-4333-8444-555555555555", owner_token="runtime-proof",
                        provider_profile=OFFLINE_FAKE_PROVIDER_PROFILE, now_ms=1_000_000,
                    )
            finally:
                for patch in reversed(patches):
                    patch.stop()
            self.assertEqual([], calls)
            self.assertEqual(4, len(ledger.effects))
            self.assertEqual(
                ["DRIVE_UPLOAD", "DRIVE_UPLOAD", "GMAIL_SEND", "GMAIL_SEND"],
                [entry["action"] for entry in ledger.effects],
            )
            self.assertEqual("energygrid.offline.v4.result", result["schema"])

    def test_migration_and_adoption_of_a_real_adoptable_predecessor_make_no_provider_call(self) -> None:
        """An ADOPTABLE v3 latest is migrated and adopted with no network, process or provider reach."""
        from fixtures.synthetic_dual_stream import candidate, snapshot
        from energygrid_bill_downloader.state import StateV4Store, migrate_state_database_v4, predecessor_digest
        from test_state_v4 import LATEST_SOURCE, build_predecessor_v3, predecessor_pdf

        calls: list[str] = []

        def recorder(label: str):
            def fail(*args, **kwargs):
                calls.append(label)
                raise AssertionError(f"offline v4 must not call {label}")
            return fail

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state.sqlite"
            archive = root / "archive"
            temp = root / "temp"
            temp.mkdir()
            built = build_predecessor_v3(db, archive, {"EB_BILL": "ADOPTABLE", "TENANT_BILL": "DELIVERED"})
            adapters = {}
            for stream in ("EB_BILL", "TENANT_BILL"):
                name, day = LATEST_SOURCE[stream]
                item = candidate(Stream(stream), name=name, invoice_date=day)
                adapters[stream] = OfflineFakeAdapter(snapshot(Stream(stream), (item,)), {name: predecessor_pdf(name)})
            ledger = FakeProviderLedger()
            connectors = {ACTION_DRIVE_UPLOAD: FakeDriveConnector(ledger), ACTION_GMAIL_SEND: FakeGmailConnector(ledger)}
            seen: dict = {}

            def at_after_acquisition(name: str) -> None:
                if name != "after_acquisition":
                    return
                with StateV4Store(db) as view:
                    seen["effects"] = len(ledger.effects)
                    seen["acquire_calls"] = {s: list(a.acquire_calls) for s, a in adapters.items()}
                    seen["admissions"] = [(row["stream"], row["predecessor_invoice_id"]) for s in ("EB_BILL", "TENANT_BILL")
                                          for row in view.invoices_for_stream(s)]
                seen["digest"] = predecessor_digest(db)

            patches = [
                mock.patch.object(socket.socket, "connect", recorder("socket.connect")),
                mock.patch.object(socket, "create_connection", recorder("socket.create_connection")),
                mock.patch.object(urllib.request, "urlopen", recorder("urllib.urlopen")),
                mock.patch.object(subprocess, "Popen", recorder("subprocess.Popen")),
                mock.patch.object(subprocess, "run", recorder("subprocess.run")),
                mock.patch.object(os, "system", recorder("os.system")),
            ]
            for patch in patches:
                patch.start()
            try:
                before = predecessor_digest(db)
                self.assertEqual("MIGRATED_V4", migrate_state_database_v4(db, apply=True)["status"])
                with StateV4Store(db) as store:
                    result = run_offline_dual_stream_v4(
                        state=store, adapters=adapters, connectors=connectors, archive_root=archive, temp_root=temp,
                        run_id="11111111-2222-4333-8444-555555555556", owner_token="runtime-adoption-proof",
                        provider_profile=OFFLINE_FAKE_PROVIDER_PROFILE, now_ms=1_000_000, checkpoint=at_after_acquisition,
                    )
            finally:
                for patch in reversed(patches):
                    patch.stop()
            self.assertEqual([], calls)
            self.assertEqual(0, seen["effects"])
            self.assertEqual({"EB_BILL": [], "TENANT_BILL": []}, seen["acquire_calls"])
            self.assertEqual([("EB_BILL", built["EB_BILL"]["latest"])], seen["admissions"])
            self.assertEqual(before, seen["digest"])
            self.assertEqual("ADOPTED", result["acquisition"]["EB_BILL"]["predecessor"])
            self.assertEqual(["DRIVE_UPLOAD", "GMAIL_SEND"], [entry["action"] for entry in ledger.effects])
            self.assertEqual(before, predecessor_digest(db))


if __name__ == "__main__":
    unittest.main()
