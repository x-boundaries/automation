from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from energygrid_bill_downloader.errors import OfflineV4Error, StateError
from energygrid_bill_downloader.naming import display_filename
from energygrid_bill_downloader.publication import filename_key
from energygrid_bill_downloader.state import (
    StateV3Store,
    StateV4Store,
    _inspect_v3_database,
    migrate_state_database_v4,
    predecessor_digest,
)


# SHA-pinned predecessor fixture: generated once from the unmodified frozen-main v3 code
# (see its header). The pin is over LF-normalised bytes so every checkout style agrees.
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "predecessor_v3_schema.sql"
FIXTURE_SHA256 = "e2d786a3839fac7dcb4c59e0381b9f236d9bb65d5a5c7c07899a23c9ddb28100"
RUN_ID = "11111111-2222-4333-8444-555555555555"
RUN_ID_2 = "11111111-2222-4333-8444-666666666666"
NOW = "2026-10-13T00:00:00Z"
SHA_A = "a" * 64
SHA_B = "b" * 64


def fixture_text() -> str:
    return FIXTURE.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")


def create_predecessor(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.executescript(fixture_text())
    finally:
        connection.close()


def raw(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def admit(store: StateV4Store, *, name: str = "invoice-001.pdf", invoice_date: date = date(2026, 10, 1),
          stream: str = "EB_BILL", namespace: str = "SYN-NS", sha: str = SHA_A, size: int = 10, run: str = RUN_ID):
    return store.admit_invoice(
        stream=stream,
        source_namespace=namespace,
        source_invoice_key=filename_key(name),
        invoice_date=invoice_date,
        display_filename=display_filename(name, invoice_date),
        sha256=sha,
        byte_size=size,
        run_id=run,
        now_iso=NOW,
    )


class _V4Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "state.sqlite"
        create_predecessor(self.path)
        migrate_state_database_v4(self.path, apply=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def intents(self, store: StateV4Store, invoice: dict) -> dict:
        return store.intents_for_invoice(invoice["invoice_key"])


class PredecessorFixtureTests(unittest.TestCase):
    def test_fixture_bytes_match_the_pinned_sha256(self) -> None:
        self.assertEqual(FIXTURE_SHA256, hashlib.sha256(FIXTURE.read_bytes().replace(b"\r\n", b"\n")).hexdigest())

    def test_fixture_is_a_complete_v3_database_for_the_frozen_inspector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v3.sqlite"
            create_predecessor(path)
            connection = raw(path)
            try:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                self.assertEqual("COMPLETE_V3", _inspect_v3_database(connection))
            finally:
                connection.close()
            with StateV3Store(path) as store:
                self.assertEqual("COMPLETE_V3", store.validation_status)


class MigrationTests(_V4Base):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "state.sqlite"
        create_predecessor(self.path)

    def test_plan_is_read_only_and_leaves_the_v3_database_untouched(self) -> None:
        before = self.path.read_bytes()
        plan = migrate_state_database_v4(self.path)
        self.assertEqual("PLAN_V4", plan["status"])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["state.sqlite"])

    def test_apply_adds_only_v4_objects_and_keeps_every_predecessor_object_and_row(self) -> None:
        def predecessor_objects(path: Path) -> list[tuple]:
            connection = raw(path)
            try:
                return connection.execute(
                    "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' "
                    "AND name NOT LIKE 'energygrid_v4_%' ORDER BY type, name"
                ).fetchall()
            finally:
                connection.close()

        before_objects = predecessor_objects(self.path)
        before_digest = predecessor_digest(self.path)
        result = migrate_state_database_v4(self.path, apply=True)
        self.assertEqual("MIGRATED_V4", result["status"])
        self.assertEqual(before_digest, predecessor_digest(self.path))
        self.assertEqual(before_objects, predecessor_objects(self.path))
        with StateV4Store(self.path) as store:
            self.assertEqual("COMPLETE_V4", store.validation_status)
            self.assertEqual(0, store.count("energygrid_v4_invoice"))

    def test_the_v3_store_refuses_a_migrated_database_instead_of_reading_it_as_v3(self) -> None:
        migrate_state_database_v4(self.path, apply=True)
        with self.assertRaises(StateError):
            with StateV3Store(self.path):
                pass

    def test_apply_writes_an_exclusive_backup_before_changing_the_database(self) -> None:
        migrate_state_database_v4(self.path, apply=True)
        backups = [p for p in self.dir.iterdir() if ".v4-migration-" in p.name]
        self.assertEqual(1, len(backups))
        connection = raw(backups[0])
        try:
            self.assertEqual(3, connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()

    def test_a_second_apply_on_a_v4_database_is_refused(self) -> None:
        migrate_state_database_v4(self.path, apply=True)
        with self.assertRaises(OfflineV4Error) as caught:
            migrate_state_database_v4(self.path, apply=True)
        self.assertEqual("EG_V4_PREDECESSOR_NOT_COMPLETE_V3", caught.exception.support_ref)

    def test_missing_and_non_v3_predecessors_are_refused(self) -> None:
        with self.assertRaises(OfflineV4Error) as caught:
            migrate_state_database_v4(self.dir / "absent.sqlite", apply=True)
        self.assertEqual("EG_V4_PREDECESSOR_MISSING", caught.exception.support_ref)
        v2_only = self.dir / "v2.sqlite"
        connection = sqlite3.connect(v2_only, isolation_level=None)
        connection.execute("CREATE TABLE unrelated (x INTEGER)")
        connection.execute("PRAGMA user_version=2")
        connection.close()
        with self.assertRaises(OfflineV4Error) as caught:
            migrate_state_database_v4(v2_only, apply=True)
        self.assertEqual("EG_V4_PREDECESSOR_NOT_COMPLETE_V3", caught.exception.support_ref)

    def test_relative_migration_path_is_refused(self) -> None:
        with self.assertRaises(StateError):
            migrate_state_database_v4(Path("relative-state.sqlite"), apply=True)

    def test_the_v4_store_refuses_a_plain_v3_database(self) -> None:
        with self.assertRaises(OfflineV4Error) as caught:
            with StateV4Store(self.path):
                pass
        self.assertEqual("EG_V4_STATE_NOT_COMPLETE_V4", caught.exception.support_ref)


class AdmissionTests(_V4Base):
    def test_admission_creates_one_pending_drive_intent_and_one_pending_gmail_intent(self) -> None:
        with StateV4Store(self.path) as store:
            invoice, created = admit(store)
            self.assertTrue(created)
            self.assertEqual("EB Bill/2026-10-01.pdf", invoice["archive_relpath"])
            self.assertEqual("OCT 2026 - invoice-001.pdf", invoice["display_filename"])
            intents = self.intents(store, invoice)
            self.assertEqual({"DRIVE_UPLOAD", "GMAIL_SEND"}, set(intents))
            self.assertEqual({"PENDING"}, {row["state"] for row in intents.values()})
            self.assertEqual({SHA_A}, {row["payload_sha256"] for row in intents.values()})

    def test_identical_readmission_is_idempotent_and_writes_nothing(self) -> None:
        with StateV4Store(self.path) as store:
            first, _ = admit(store)
            counts = [store.count(t) for t in ("energygrid_v4_invoice", "energygrid_v4_intent", "energygrid_v4_transition")]
            second, created = admit(store)
            self.assertFalse(created)
            self.assertEqual(first["invoice_key"], second["invoice_key"])
            self.assertEqual(counts, [store.count(t) for t in ("energygrid_v4_invoice", "energygrid_v4_intent", "energygrid_v4_transition")])

    def test_different_content_for_the_same_identity_is_a_conflict(self) -> None:
        with StateV4Store(self.path) as store:
            admit(store)
            with self.assertRaises(OfflineV4Error) as caught:
                admit(store, sha=SHA_B)
            self.assertEqual("EG_V4_INVOICE_IDENTITY_CONFLICT", caught.exception.support_ref)
            self.assertEqual(1, store.count("energygrid_v4_invoice"))

    def test_display_name_collision_within_a_stream_is_refused(self) -> None:
        with StateV4Store(self.path) as store:
            admit(store, name="a.pdf", invoice_date=date(2026, 10, 1))
            with self.assertRaises(OfflineV4Error) as caught:
                admit(store, name="OCT 2026 - a.pdf", invoice_date=date(2026, 10, 15), sha=SHA_B)
            self.assertEqual("EG_V4_DISPLAY_NAME_COLLISION", caught.exception.support_ref)
            self.assertEqual(1, store.count("energygrid_v4_invoice"))

    def test_same_display_name_in_the_other_stream_is_not_a_collision(self) -> None:
        with StateV4Store(self.path) as store:
            admit(store, name="a.pdf", invoice_date=date(2026, 10, 1), stream="EB_BILL")
            invoice, created = admit(store, name="a.pdf", invoice_date=date(2026, 10, 2), stream="TENANT_BILL", sha=SHA_B)
            self.assertTrue(created)
            self.assertEqual("Tenant Bill/2026-10-02.pdf", invoice["archive_relpath"])

    def test_two_invoices_on_the_same_date_in_one_stream_collide_on_the_archive_path(self) -> None:
        with StateV4Store(self.path) as store:
            admit(store, name="first.pdf", invoice_date=date(2026, 10, 1))
            with self.assertRaises(OfflineV4Error) as caught:
                admit(store, name="second.pdf", invoice_date=date(2026, 10, 1), sha=SHA_B)
            self.assertEqual("EG_V4_ARCHIVE_PATH_COLLISION", caught.exception.support_ref)

    def test_check_admissible_reports_the_same_identity_without_needing_bytes(self) -> None:
        with StateV4Store(self.path) as store:
            admit(store)
            decision = store.check_admissible(
                stream="EB_BILL", source_namespace="SYN-NS", source_invoice_key=filename_key("invoice-001.pdf"),
                display_filename="OCT 2026 - invoice-001.pdf", canonical_filename="2026-10-01.pdf",
            )
            self.assertEqual("ALREADY_ADMITTED", decision)

    def test_admission_sequence_is_monotonic_across_streams(self) -> None:
        with StateV4Store(self.path) as store:
            first, _ = admit(store, name="one.pdf", invoice_date=date(2026, 10, 1), stream="EB_BILL")
            second, _ = admit(store, name="two.pdf", invoice_date=date(2026, 10, 2), stream="TENANT_BILL")
            self.assertLess(first["admission_seq"], second["admission_seq"])


class ImmutabilityAndTransitionTriggerTests(_V4Base):
    def setUp(self) -> None:
        super().setUp()
        with StateV4Store(self.path) as store:
            self.invoice, _ = admit(store)
            intents = self.intents(store, self.invoice)
            self.drive = intents["DRIVE_UPLOAD"]["intent_id"]
            self.gmail = intents["GMAIL_SEND"]["intent_id"]
            store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
            store.record_receipt(self.drive, outcome="VERIFIED", provider_ref="fake-d", provider_sha256=SHA_A,
                                 run_id=RUN_ID, now_iso=NOW)

    def _expect(self, sql: str, token: str, parameters: tuple = ()) -> None:
        connection = raw(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                connection.execute(sql, parameters)
            self.assertIn(token, str(caught.exception))
        finally:
            connection.close()

    def test_historical_v4_rows_cannot_be_updated_or_deleted(self) -> None:
        self._expect("UPDATE energygrid_v4_invoice SET sha256=?", "EG_V4_IMMUTABLE_ROW", (SHA_B,))
        self._expect("DELETE FROM energygrid_v4_invoice", "EG_V4_IMMUTABLE_ROW")
        self._expect("UPDATE energygrid_v4_intent SET action='GMAIL_SEND'", "EG_V4_IMMUTABLE_ROW")
        self._expect("DELETE FROM energygrid_v4_intent", "EG_V4_IMMUTABLE_ROW")
        self._expect("UPDATE energygrid_v4_receipt SET outcome='REJECTED'", "EG_V4_IMMUTABLE_ROW")
        self._expect("DELETE FROM energygrid_v4_receipt", "EG_V4_IMMUTABLE_ROW")
        self._expect("UPDATE energygrid_v4_transition SET to_state='HOLD'", "EG_V4_IMMUTABLE_ROW")
        self._expect("DELETE FROM energygrid_v4_dispatch", "EG_V4_IMMUTABLE_ROW")

    def test_status_cannot_skip_a_state_or_a_version(self) -> None:
        self._expect(
            "UPDATE energygrid_v4_intent_status SET state='VERIFIED', version=version+1 WHERE intent_id=?",
            "EG_V4_",
            (self.gmail,),
        )
        self._expect(
            "UPDATE energygrid_v4_intent_status SET state='HOLD', version=version+2 WHERE intent_id=?",
            "EG_V4_ILLEGAL_TRANSITION",
            (self.gmail,),
        )

    def test_dispatch_cannot_be_entered_without_a_seal_row(self) -> None:
        self._expect(
            "UPDATE energygrid_v4_intent_status SET state='DISPATCHED', version=version+1 WHERE intent_id=?",
            "EG_V4_DISPATCH_NOT_SEALED",
            (self.gmail,),
        )

    def test_verified_cannot_be_entered_without_a_verified_receipt(self) -> None:
        with StateV4Store(self.path) as store:
            store.seal_dispatch(self.gmail, run_id=RUN_ID, now_iso=NOW)
        self._expect(
            "UPDATE energygrid_v4_intent_status SET state='VERIFIED', version=version+1 WHERE intent_id=?",
            "EG_V4_VERIFIED_WITHOUT_RECEIPT",
            (self.gmail,),
        )

    def test_gmail_cannot_be_dispatched_before_drive_is_verified(self) -> None:
        with StateV4Store(self.path) as store:
            second_invoice, _ = admit(store, name="other.pdf", invoice_date=date(2026, 10, 2), sha=SHA_B)
            second_gmail = self.intents(store, second_invoice)["GMAIL_SEND"]["intent_id"]
            with self.assertRaises(OfflineV4Error) as caught:
                store.seal_dispatch(second_gmail, run_id=RUN_ID, now_iso=NOW)
            self.assertEqual("EG_V4_DRIVE_NOT_VERIFIED", caught.exception.support_ref)
            self.assertIsNone(store.dispatch_row(second_gmail))
            self.assertEqual("PENDING", store.intent(second_gmail)["state"])

    def test_a_receipt_cannot_be_written_without_a_dispatch_seal(self) -> None:
        self._expect(
            "INSERT INTO energygrid_v4_receipt (intent_id, digest, outcome, provider_ref, provider_sha256, run_id, at_utc) "
            "VALUES (?,?,?,?,?,?,?)",
            "EG_V4_OUTCOME_WITHOUT_DISPATCH",
            (self.gmail, "c" * 64, "VERIFIED", "fake-x", SHA_A, RUN_ID, NOW),
        )

    def test_a_dispatch_cannot_be_sealed_again_once_the_intent_has_left_pending(self) -> None:
        self._expect(
            "INSERT INTO energygrid_v4_dispatch (intent_id, run_id, sealed_at_utc) VALUES (?,?,?)",
            "EG_V4_DISPATCH_NOT_PENDING",
            (self.drive, RUN_ID, NOW),
        )


class LeaseAndCursorTests(_V4Base):
    def test_a_second_session_is_refused_while_the_lease_is_held_and_mutates_nothing(self) -> None:
        with StateV4Store(self.path) as first, StateV4Store(self.path) as second:
            first.acquire_lease("session-a", now_ms=1_000, ttl_ms=60_000)
            with self.assertRaises(OfflineV4Error) as caught:
                second.acquire_lease("session-b", now_ms=2_000, ttl_ms=60_000)
            self.assertEqual("EG_V4_LEASE_HELD", caught.exception.support_ref)
            self.assertEqual(10, caught.exception.exit_code)
            first.release_lease("session-a")
            second.acquire_lease("session-b", now_ms=3_000, ttl_ms=60_000)

    def test_an_expired_lease_can_be_taken_over_after_a_crash(self) -> None:
        with StateV4Store(self.path) as first, StateV4Store(self.path) as second:
            first.acquire_lease("crashed", now_ms=1_000, ttl_ms=10_000)
            second.acquire_lease("restart", now_ms=11_001, ttl_ms=10_000)

    def test_only_the_owner_can_release_the_lease(self) -> None:
        with StateV4Store(self.path) as first, StateV4Store(self.path) as second:
            first.acquire_lease("owner", now_ms=1_000, ttl_ms=60_000)
            second.release_lease("someone-else")
            with self.assertRaises(OfflineV4Error):
                second.acquire_lease("intruder", now_ms=2_000, ttl_ms=60_000)

    def test_lease_inputs_are_validated(self) -> None:
        with StateV4Store(self.path) as store:
            for args in (("", 1, 1), ("x" * 129, 1, 1), ("ok", 1, 0), ("ok", "1", 1)):
                with self.subTest(args=args), self.assertRaises(OfflineV4Error) as caught:
                    store.acquire_lease(args[0], now_ms=args[1], ttl_ms=args[2])
                self.assertEqual("EG_V4_LEASE_INVALID", caught.exception.support_ref)

    def test_cursor_round_trips_and_rejects_unknown_streams(self) -> None:
        with StateV4Store(self.path) as store:
            self.assertIsNone(store.cursor())
            store.set_cursor("TENANT_BILL", now_iso=NOW)
            self.assertEqual("TENANT_BILL", store.cursor())
            with self.assertRaises(OfflineV4Error):
                store.set_cursor("OTHER", now_iso=NOW)


class ProviderOutcomeTests(_V4Base):
    def setUp(self) -> None:
        super().setUp()
        self.store = StateV4Store(self.path)
        self.store.__enter__()
        self.invoice, _ = admit(self.store)
        self.drive = self.intents(self.store, self.invoice)["DRIVE_UPLOAD"]["intent_id"]

    def tearDown(self) -> None:
        self.store.close()
        super().tearDown()

    def _record(self, outcome: str, sha: str = SHA_A, ref: str = "fake-d") -> str:
        return self.store.record_receipt(self.drive, outcome=outcome, provider_ref=ref, provider_sha256=sha,
                                         run_id=RUN_ID, now_iso=NOW)

    def _state(self) -> str:
        return self.store.intent(self.drive)["state"]

    def test_an_outcome_before_dispatch_is_refused_and_writes_nothing(self) -> None:
        with self.assertRaises(OfflineV4Error) as caught:
            self._record("VERIFIED")
        self.assertEqual("EG_V4_OUTCOME_WITHOUT_DISPATCH", caught.exception.support_ref)
        self.assertEqual(0, self.store.count("energygrid_v4_receipt"))

    def test_seal_is_single_use_and_only_from_pending(self) -> None:
        self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        with self.assertRaises(OfflineV4Error) as caught:
            self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self.assertEqual("EG_V4_DISPATCH_NOT_PENDING", caught.exception.support_ref)

    def test_a_matching_verified_outcome_moves_to_verified_once(self) -> None:
        self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self.assertEqual("VERIFIED", self._record("VERIFIED"))
        self.assertEqual("VERIFIED", self._state())
        self.assertEqual("fake-d", self.store.verified_provider_ref(self.drive))
        self.assertEqual(1, len(self.store.receipts(self.drive)))

    def test_a_replayed_identical_outcome_is_ignored(self) -> None:
        self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self._record("VERIFIED")
        before = (self.store.count("energygrid_v4_receipt"), self.store.count("energygrid_v4_transition"))
        self.assertEqual("REPLAY", self._record("VERIFIED"))
        self.assertEqual(before, (self.store.count("energygrid_v4_receipt"), self.store.count("energygrid_v4_transition")))
        self.assertEqual("VERIFIED", self._state())

    def test_a_verified_outcome_with_the_wrong_sha_holds_the_intent(self) -> None:
        self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self.assertEqual("HOLD", self._record("VERIFIED", sha=SHA_B))
        self.assertEqual("EG_V4_PROVIDER_SHA_MISMATCH", self.store.intent(self.drive)["support_ref"])

    def test_a_rejected_outcome_holds_the_intent(self) -> None:
        self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self.assertEqual("HOLD", self._record("REJECTED", sha=""))
        self.assertEqual("EG_V4_PROVIDER_REJECTED", self.store.intent(self.drive)["support_ref"])

    def test_an_unknown_outcome_is_terminal_for_dispatch(self) -> None:
        self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self.assertEqual("UNKNOWN", self._record("UNKNOWN", sha=""))
        with self.assertRaises(OfflineV4Error) as caught:
            self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self.assertEqual("EG_V4_DISPATCH_NOT_PENDING", caught.exception.support_ref)

    def test_a_late_verified_proof_with_the_exact_sha_resolves_unknown(self) -> None:
        self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self._record("UNKNOWN", sha="")
        self.assertEqual("VERIFIED", self._record("VERIFIED", ref="fake-late"))
        self.assertEqual("VERIFIED", self._state())

    def test_a_conflicting_outcome_after_verified_holds_and_keeps_both_receipts(self) -> None:
        self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self._record("VERIFIED")
        self.assertEqual("HOLD", self._record("REJECTED", sha=""))
        self.assertEqual("EG_V4_CONFLICTING_OUTCOME", self.store.intent(self.drive)["support_ref"])
        self.assertEqual(2, len(self.store.receipts(self.drive)))

    def test_recovery_marks_a_dispatch_without_receipt_unknown_and_leaves_verified_alone(self) -> None:
        self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self.assertEqual(1, self.store.recover_unfinished(RUN_ID_2, now_iso=NOW))
        self.assertEqual("UNKNOWN", self._state())
        self.assertEqual("EG_V4_DISPATCH_OUTCOME_UNKNOWN", self.store.intent(self.drive)["support_ref"])
        self.assertEqual(0, self.store.recover_unfinished(RUN_ID_2, now_iso=NOW))

    def test_recovery_leaves_a_verified_intent_untouched(self) -> None:
        self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self._record("VERIFIED")
        self.assertEqual(0, self.store.recover_unfinished(RUN_ID_2, now_iso=NOW))
        self.assertEqual("VERIFIED", self._state())

    def test_a_pending_intent_held_before_dispatch_records_no_receipt(self) -> None:
        self.store.hold_pending(self.drive, support_ref="EG_V4_ARCHIVE_UNREADABLE", run_id=RUN_ID, now_iso=NOW)
        self.assertEqual("HOLD", self._state())
        with self.assertRaises(OfflineV4Error):
            self._record("VERIFIED")
        self.assertEqual(0, self.store.count("energygrid_v4_receipt"))

    def test_transitions_record_each_state_change_in_order(self) -> None:
        self.store.seal_dispatch(self.drive, run_id=RUN_ID, now_iso=NOW)
        self._record("VERIFIED")
        path = [(row["from_state"], row["to_state"]) for row in self.store.transitions(self.drive)]
        self.assertEqual([(None, "PENDING"), ("PENDING", "DISPATCHED"), ("DISPATCHED", "VERIFIED")], path)


if __name__ == "__main__":
    unittest.main()
