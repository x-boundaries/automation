from __future__ import annotations

from datetime import date
import contextlib
import hashlib
from unittest import mock
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


@contextlib.contextmanager
def leased(path: Path, owner: str = "test-session", now_ms: int = 1_000):
    """A store with the v4 lease held, as the offline run holds it. Writes are fenced."""
    with StateV4Store(path) as store:
        store.acquire_lease(owner, now_ms=now_ms, ttl_ms=10**9)
        try:
            yield store
        finally:
            store.release_lease(owner)


def fixture_text() -> str:
    return FIXTURE.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")


def create_predecessor(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.executescript(fixture_text())
    finally:
        connection.close()


def insert_predecessor(path: Path, *, source_filename: str, day: str, stream: str = "EB_BILL") -> None:
    """A CLASSIFIED predecessor invoice row, written through the real v2 table and its guards.

    The database must hold bound streams (see create_v3_database). Synthetic data only.
    """
    import uuid

    from fixtures.synthetic_dual_stream import SYNTHETIC_EVIDENCE, SYNTHETIC_NAMESPACE

    folder = "EB Bill" if stream == "EB_BILL" else "Tenant Bill"
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO energygrid_invoice_v2 (invoice_id, classification, stream, source_namespace, source_invoice_key, "
            "source_filename, raw_date, date_profile, bill_date, day_ordinal, evidence_ref, canonical_filename, "
            "archive_relpath, archive_state, migration_state, drive_state, created_at_utc, created_run_id) "
            "VALUES (?, 'CLASSIFIED', ?, ?, ?, ?, ?, 'INVOICE_DATE_ISO_V1', ?, ?, ?, ?, ?, 'ABSENT', 'NOT_REQUIRED', "
            "'NOT_STAGED', ?, ?)",
            (
                "egpre-" + uuid.uuid4().hex, stream, SYNTHETIC_NAMESPACE, filename_key(source_filename),
                source_filename, day, day, date.fromisoformat(day).toordinal(), SYNTHETIC_EVIDENCE,
                f"{day}.pdf", f"{folder}/{day}.pdf", NOW, RUN_ID,
            ),
        )
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
        with leased(self.path) as store:
            self.assertEqual("COMPLETE_V4", store.validation_status)
            self.assertEqual(0, store.count("energygrid_v4_invoice"))

    def test_open_v3_work_blocks_the_migration_and_leaves_the_database_at_v3(self) -> None:
        connection = raw(self.path)
        connection.execute(
            "INSERT INTO energygrid_invoice_v2 (invoice_id, classification, archive_state, migration_state, drive_state, "
            "created_at_utc, created_run_id) VALUES ('egopen-1', 'UNCLASSIFIED', 'UNVERIFIED', 'UNCLASSIFIED', "
            "'NOT_STAGED', ?, ?)",
            (NOW, RUN_ID),
        )
        connection.close()
        with self.assertRaises(OfflineV4Error) as caught:
            migrate_state_database_v4(self.path, apply=True)
        self.assertEqual("EG_V4_PREDECESSOR_NOT_COMPLETE_V3", caught.exception.support_ref)
        connection = raw(self.path)
        try:
            self.assertEqual(3, connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()

    def test_an_extra_trigger_on_a_predecessor_table_changes_the_digest(self) -> None:
        before = predecessor_digest(self.path)
        migrate_state_database_v4(self.path, apply=True)
        self.assertEqual(before, predecessor_digest(self.path))
        connection = raw(self.path)
        try:
            connection.execute(
                "CREATE TRIGGER energygrid_v4_probe BEFORE INSERT ON energygrid_stream_v2 BEGIN SELECT 1; END"
            )
        finally:
            connection.close()
        self.assertNotEqual(before, predecessor_digest(self.path))

    def test_the_migration_checks_and_backup_run_inside_the_write_lock(self) -> None:
        from energygrid_bill_downloader import state as state_module

        seen: list[tuple[str, bool]] = []
        real_inspect = state_module._inspect_v3_database
        real_backup = state_module._backup_state_file_exclusive

        def inspect(connection):
            seen.append(("inspect", connection.in_transaction))
            return real_inspect(connection)

        def backup(path, label):
            # A second writer must be refused while the migration holds the lock.
            other = sqlite3.connect(path, timeout=0, isolation_level=None)
            try:
                try:
                    other.execute("BEGIN IMMEDIATE")
                    seen.append(("backup-unlocked", False))
                except sqlite3.OperationalError:
                    seen.append(("backup-locked", True))
            finally:
                other.close()
            return real_backup(path, label)

        with mock.patch.object(state_module, "_inspect_v3_database", side_effect=inspect), \
                mock.patch.object(state_module, "_backup_state_file_exclusive", side_effect=backup):
            migrate_state_database_v4(self.path, apply=True)
        self.assertEqual([("inspect", True), ("backup-locked", True)], seen)

    def assert_still_v3(self) -> None:
        connection = raw(self.path)
        try:
            self.assertEqual(3, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'energygrid_v4_%'").fetchone()[0])
        finally:
            connection.close()

    def test_a_failed_v4_inspection_rolls_the_migration_back_to_v3(self) -> None:
        from energygrid_bill_downloader import state as state_module

        with mock.patch.object(state_module, "_inspect_v4_database", return_value="INCOMPATIBLE_V4"):
            with self.assertRaises(OfflineV4Error) as caught:
                migrate_state_database_v4(self.path, apply=True)
        self.assertEqual("EG_V4_SCHEMA_VALIDATION_FAILED", caught.exception.support_ref)
        self.assert_still_v3()

    def test_a_changed_predecessor_digest_rolls_the_migration_back_to_v3(self) -> None:
        from energygrid_bill_downloader import state as state_module

        real_digest = state_module._predecessor_digest
        calls = {"count": 0}

        def changed_after_the_migration(connection):
            calls["count"] += 1
            return real_digest(connection) if calls["count"] == 1 else "0" * 64

        with mock.patch.object(state_module, "_predecessor_digest", side_effect=changed_after_the_migration):
            with self.assertRaises(OfflineV4Error) as caught:
                migrate_state_database_v4(self.path, apply=True)
        self.assertEqual("EG_V4_PREDECESSOR_CHANGED", caught.exception.support_ref)
        self.assert_still_v3()

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
        with leased(self.path) as store:
            invoice, created = admit(store)
            self.assertTrue(created)
            self.assertEqual("EB Bill/2026-10-01.pdf", invoice["archive_relpath"])
            self.assertEqual("OCT 2026 - invoice-001.pdf", invoice["display_filename"])
            intents = self.intents(store, invoice)
            self.assertEqual({"DRIVE_UPLOAD", "GMAIL_SEND"}, set(intents))
            self.assertEqual({"PENDING"}, {row["state"] for row in intents.values()})
            self.assertEqual({SHA_A}, {row["payload_sha256"] for row in intents.values()})

    def test_identical_readmission_is_idempotent_and_writes_nothing(self) -> None:
        with leased(self.path) as store:
            first, _ = admit(store)
            counts = [store.count(t) for t in ("energygrid_v4_invoice", "energygrid_v4_intent", "energygrid_v4_transition")]
            second, created = admit(store)
            self.assertFalse(created)
            self.assertEqual(first["invoice_key"], second["invoice_key"])
            self.assertEqual(counts, [store.count(t) for t in ("energygrid_v4_invoice", "energygrid_v4_intent", "energygrid_v4_transition")])

    def test_different_content_for_the_same_identity_is_a_conflict(self) -> None:
        with leased(self.path) as store:
            admit(store)
            with self.assertRaises(OfflineV4Error) as caught:
                admit(store, sha=SHA_B)
            self.assertEqual("EG_V4_INVOICE_IDENTITY_CONFLICT", caught.exception.support_ref)
            self.assertEqual(1, store.count("energygrid_v4_invoice"))

    def test_display_name_collision_within_a_stream_is_refused(self) -> None:
        with leased(self.path) as store:
            admit(store, name="a.pdf", invoice_date=date(2026, 10, 1))
            with self.assertRaises(OfflineV4Error) as caught:
                admit(store, name="OCT 2026 - a.pdf", invoice_date=date(2026, 10, 15), sha=SHA_B)
            self.assertEqual("EG_V4_DISPLAY_NAME_COLLISION", caught.exception.support_ref)
            self.assertEqual(1, store.count("energygrid_v4_invoice"))

    def test_same_display_name_in_the_other_stream_is_not_a_collision(self) -> None:
        with leased(self.path) as store:
            admit(store, name="a.pdf", invoice_date=date(2026, 10, 1), stream="EB_BILL")
            invoice, created = admit(store, name="a.pdf", invoice_date=date(2026, 10, 2), stream="TENANT_BILL", sha=SHA_B)
            self.assertTrue(created)
            self.assertEqual("Tenant Bill/2026-10-02.pdf", invoice["archive_relpath"])

    def test_two_invoices_on_the_same_date_in_one_stream_collide_on_the_archive_path(self) -> None:
        with leased(self.path) as store:
            admit(store, name="first.pdf", invoice_date=date(2026, 10, 1))
            with self.assertRaises(OfflineV4Error) as caught:
                admit(store, name="second.pdf", invoice_date=date(2026, 10, 1), sha=SHA_B)
            self.assertEqual("EG_V4_ARCHIVE_PATH_COLLISION", caught.exception.support_ref)

    def test_check_admissible_reports_the_same_identity_without_needing_bytes(self) -> None:
        with leased(self.path) as store:
            admit(store)
            decision = store.check_admissible(
                stream="EB_BILL", source_namespace="SYN-NS", source_invoice_key=filename_key("invoice-001.pdf"),
                source_filename="invoice-001.pdf", canonical_filename="2026-10-01.pdf",
                invoice_date=date(2026, 10, 1),
            )
            self.assertEqual(("ALREADY_ADMITTED", "OCT 2026 - invoice-001.pdf"), decision)

    def test_an_older_identity_than_the_newest_admission_is_a_regression(self) -> None:
        with leased(self.path) as store:
            admit(store, name="newer.pdf", invoice_date=date(2026, 10, 5))
            with self.assertRaises(OfflineV4Error) as caught:
                admit(store, name="older.pdf", invoice_date=date(2026, 10, 1), sha=SHA_B)
            self.assertEqual("EG_V4_SOURCE_REGRESSION", caught.exception.support_ref)
            self.assertEqual(1, store.count("energygrid_v4_invoice"))

    def test_admission_sequence_is_monotonic_across_streams(self) -> None:
        with leased(self.path) as store:
            first, _ = admit(store, name="one.pdf", invoice_date=date(2026, 10, 1), stream="EB_BILL")
            second, _ = admit(store, name="two.pdf", invoice_date=date(2026, 10, 2), stream="TENANT_BILL")
            self.assertLess(first["admission_seq"], second["admission_seq"])


SYNTH_NS = "SYNTHETIC-ENERGYGRID-NAMESPACE"


PREDECESSOR_RUN = "00000000-0000-4000-8000-00000000c001"
PREDECESSOR_SCENARIOS = (
    "ADOPTABLE", "DELIVERED", "DRIVE_PARTIAL", "DRIVE_VERIFIED_NOT_SENT", "DELIVERY_UNCERTAIN",
    "DELIVERY_REJECTED", "ARCHIVE_NOT_COMMITTED",
    # #226 v4 correction (M3): a v3 PENDING_SEND delivery, prepared only or with its dispatch claimed.
    "DELIVERY_PREPARED", "DELIVERY_DISPATCHED",
)
LATEST_SOURCE = {"EB_BILL": ("Invoice EB Sep.pdf", "2026-09-01"), "TENANT_BILL": ("Invoice TN Sep.pdf", "2026-09-02")}
OLDER_SOURCE = {"EB_BILL": ("Invoice EB Aug.pdf", "2026-08-01"), "TENANT_BILL": ("Invoice TN Aug.pdf", "2026-08-02")}
PINNED_PREDECESSOR_DIGEST = "c7bbba34210619b32afbd4eb2321d4eff304de09ec6244b4a8e6643f143983ba"
T_ACCEPT = "2026-09-05T00:00:00+00:00"
T_ARCHIVE = "2026-09-05T00:00:01+00:00"
T_DRIVE = ("2026-09-05T00:00:02+00:00", "2026-09-05T00:00:03+00:00", "2026-09-05T00:00:04+00:00",
           "2026-09-05T00:00:05+00:00")
T_MAIL = ("2026-09-05T00:00:06+00:00", "2026-09-05T00:00:07+00:00", "2026-09-05T00:00:08+00:00")


def predecessor_pdf(label: str) -> bytes:
    return f"%PDF-1.4\n{label}\n%%EOF\n".encode("ascii")


def _deterministic_uuid(label: str) -> str:
    return "00000000-0000-4000-8000-" + hashlib.sha256(label.encode("ascii")).hexdigest()[:12]


def _commit_predecessor_archive(state, archive_root: Path, invoice_id: str, payload: bytes) -> None:
    from energygrid_bill_downloader.publication import validate_pdf

    invoice = state.invoice(invoice_id)
    path = archive_root / invoice["archive_relpath"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    info = validate_pdf(path)
    operation = _deterministic_uuid("archive:" + invoice_id)
    state.start_file_operation(
        operation_id=operation, invoice_id=invoice_id, kind="ARCHIVE_PUBLISH", private_path_ref="EG_SYNTHETIC_TEMP",
        target_relpath=invoice["archive_relpath"], binding_id=None, info=info, source_role="SOURCE_ACQUISITION",
        run_id=PREDECESSOR_RUN, timestamp=T_ARCHIVE,
    )
    state.complete_file_operation(operation, T_ARCHIVE, evidence_ref="EG_SYNTHETIC_ARCHIVE_COMMIT")
    state.update_invoice_file_state(
        invoice_id, archive_state="COMMITTED", byte_size=info.byte_size, sha256=info.sha256, archived_at_utc=T_ARCHIVE,
    )


def _predecessor_drive(state, invoice: dict, *, verified: bool) -> None:
    import json as _json

    from energygrid_bill_downloader.state import canonical_json
    from fixtures.synthetic_dual_stream import SYNTHETIC_ACCOUNT, SYNTHETIC_FOLDERS, SYNTHETIC_ROOT

    stream = invoice["stream"]
    binding = state.active_binding(stream) or state.insert_binding(
        stream=stream, account_ref=SYNTHETIC_ACCOUNT, root_folder_id=SYNTHETIC_ROOT, folder_id=SYNTHETIC_FOLDERS[stream],
        chain_sha256="0" * 64, run_id=PREDECESSOR_RUN, timestamp=T_DRIVE[0],
    )
    operation_id = _deterministic_uuid("drive:" + invoice["invoice_id"])
    operation, _ = state.create_drive_intent(
        invoice=invoice, binding=binding, local_md5=invoice["_md5"], operation_id=operation_id,
        run_id=PREDECESSOR_RUN, timestamp=T_DRIVE[0],
    )
    file_id = "synthPredecessor" + operation_id.replace("-", "")[-16:]
    state.reserve_drive_file_id(operation_id, file_id, PREDECESSOR_RUN, T_DRIVE[1])
    state.begin_drive_dispatch(operation_id, PREDECESSOR_RUN, T_DRIVE[2])
    if not verified:
        return
    resource = {
        "id": file_id, "name": operation["remote_name"], "mimeType": "application/pdf",
        "parents": [operation["folder_id"]], "size": str(operation["local_byte_size"]),
        "md5Checksum": operation["local_md5"], "sha256Checksum": operation["local_sha256"],
        "appProperties": _json.loads(operation["app_properties_json"]), "trashed": False,
    }
    receipt = {
        "remote_file_id": file_id, "remote_parent_id": operation["folder_id"],
        "remote_name_observed": operation["remote_name"], "remote_mime_type": "application/pdf",
        "remote_size": operation["local_byte_size"], "remote_sha256": operation["local_sha256"],
        "remote_md5": operation["local_md5"], "accepted_app_properties_json": operation["app_properties_json"],
        "verification_method": "SHA256",
        "receipt_sha256": hashlib.sha256(canonical_json(resource).encode("ascii")).hexdigest(),
    }
    state.finish_drive_operation(
        operation_id, run_id=PREDECESSOR_RUN, timestamp=T_DRIVE[3], new_state="DRIVE_VERIFIED",
        dispatch_outcome="VALID_RESULT", dispatch_support_ref="EG_DRIVE_RESULT", receipt=receipt,
    )


def _predecessor_delivery(state, invoice: dict, outcome: str) -> None:
    from energygrid_bill_downloader.delivery import DELIVERY_SCHEMA

    delivery_id = "egmail-v1-" + hashlib.sha256(("delivery:" + invoice["invoice_id"]).encode("ascii")).hexdigest()[:32]
    metadata = {
        "schema": DELIVERY_SCHEMA, "stream": invoice["stream"], "bill_date": invoice["bill_date"],
        "attachment_name": invoice["canonical_filename"], "pdf_byte_size": invoice["byte_size"],
        "pdf_sha256": invoice["sha256"],
    }
    state.prepare_delivery(invoice_id=invoice["invoice_id"], metadata=metadata, run_id=PREDECESSOR_RUN,
                           timestamp=T_MAIL[0], delivery_id=delivery_id)
    state.claim_delivery_dispatch(delivery_id, PREDECESSOR_RUN, T_MAIL[1])
    state.record_delivery_outcome(
        delivery_id, PREDECESSOR_RUN, T_MAIL[2], state=outcome, evidence="VALIDATED_N8N_RESULT",
        support_ref="EG_SYNTHETIC_MAIL", accepted_at_utc=T_MAIL[2] if outcome == "DELIVERED" else None,
    )


def build_predecessor_v3(path: Path, archive_root: Path, scenarios: dict) -> dict:
    """A bound complete-v3 database built only through the frozen v3 store API, with synthetic data.

    Each stream gets an older archived-but-undelivered invoice (never a backfill candidate) and a
    current latest in the named scenario. Identifiers and timestamps are deterministic.
    """
    from energygrid_bill_downloader.invoice import Stream
    from fixtures.synthetic_dual_stream import candidate, create_v3_database

    create_v3_database(path)
    built: dict = {}
    with StateV3Store(path) as state:
        for stream_name, scenario in scenarios.items():
            if scenario not in PREDECESSOR_SCENARIOS:
                raise ValueError(scenario)
            stream = Stream(stream_name)
            older_name, older_day = OLDER_SOURCE[stream_name]
            latest_name, latest_day = LATEST_SOURCE[stream_name]
            older = state.accept_latest(candidate(stream, name=older_name, invoice_date=older_day), PREDECESSOR_RUN, T_ACCEPT)
            _commit_predecessor_archive(state, archive_root, older, predecessor_pdf(older_name))
            latest = state.accept_latest(candidate(stream, name=latest_name, invoice_date=latest_day), PREDECESSOR_RUN, T_ACCEPT)
            if scenario != "ARCHIVE_NOT_COMMITTED":
                _commit_predecessor_archive(state, archive_root, latest, predecessor_pdf(latest_name))
                invoice = state.invoice(latest)
                invoice["_md5"] = hashlib.md5(predecessor_pdf(latest_name)).hexdigest()
                if scenario != "ADOPTABLE":
                    _predecessor_drive(state, invoice, verified=scenario != "DRIVE_PARTIAL")
                if scenario in ("DELIVERED", "DELIVERY_UNCERTAIN", "DELIVERY_REJECTED"):
                    outcome = {"DELIVERED": "DELIVERED", "DELIVERY_UNCERTAIN": "DELIVERY_OUTCOME_UNCERTAIN",
                               "DELIVERY_REJECTED": "REQUEST_REJECTED"}[scenario]
                    _predecessor_delivery(state, invoice, outcome)
                if scenario in ("DELIVERY_PREPARED", "DELIVERY_DISPATCHED"):
                    _predecessor_pending_delivery(state, invoice, dispatched=scenario == "DELIVERY_DISPATCHED")
            built[stream_name] = {"older": older, "latest": latest, "scenario": scenario}
    return built


def _predecessor_pending_delivery(state, invoice: dict, *, dispatched: bool) -> None:
    """A v3 PENDING_SEND delivery (Drive is verified first, as v3 requires), optionally with a claimed dispatch."""
    from energygrid_bill_downloader.delivery import DELIVERY_SCHEMA

    delivery_id = "egmail-v1-" + hashlib.sha256(("delivery:" + invoice["invoice_id"]).encode("ascii")).hexdigest()[:32]
    metadata = {
        "schema": DELIVERY_SCHEMA, "stream": invoice["stream"], "bill_date": invoice["bill_date"],
        "attachment_name": invoice["canonical_filename"], "pdf_byte_size": invoice["byte_size"],
        "pdf_sha256": invoice["sha256"],
    }
    state.prepare_delivery(invoice_id=invoice["invoice_id"], metadata=metadata, run_id=PREDECESSOR_RUN,
                           timestamp=T_MAIL[0], delivery_id=delivery_id)
    if dispatched:
        state.claim_delivery_dispatch(delivery_id, PREDECESSOR_RUN, T_MAIL[1])


# -- frozen predecessor evidence for the #226 v4 correction (M1, M2, M4, M5) ------------------------

def _predecessor_rows(path: Path, sql: str, parameters: tuple = ()) -> list[dict]:
    connection = raw(path)
    try:
        cursor = connection.execute(sql, parameters)
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def inject_frozen_v2_evidence(path: Path, statements: tuple, triggers: tuple) -> None:
    """Write historical v2 rows that the shipped v3 schema now refuses to create.

    Synthetic only. Each named v3 guard trigger is dropped and recreated from its exact stored SQL in the
    same transaction, so the schema is unchanged; callers assert the shipped v3 inspector still accepts the
    database. This stands for v2-era evidence preserved across the v2 -> v3 migration.
    """
    connection = raw(path)
    try:
        saved = [
            connection.execute("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)).fetchone()[0]
            for name in triggers
        ]
        connection.execute("BEGIN IMMEDIATE")
        for name in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        for statement, parameters in statements:
            connection.execute(statement, parameters)
        for sql in saved:
            connection.execute(sql)
        connection.execute("COMMIT")
    finally:
        connection.close()


def add_v2_drive_stage(path: Path, invoice_id: str, op_state: str) -> None:
    """One historical v2 DRIVE_STAGE file operation for the invoice, with the invoice still NOT_STAGED."""
    invoice = _predecessor_rows(path, "SELECT * FROM energygrid_invoice_v2 WHERE invoice_id=?", (invoice_id,))[0]
    stamp = "2026-09-05T00:00:09+00:00"
    inject_frozen_v2_evidence(
        path,
        ((
            "INSERT INTO energygrid_file_operation_v2 (operation_id, invoice_id, kind, state, private_path_ref, "
            "target_relpath, binding_id, expected_size, expected_sha256, source_role, run_id, prepared_at_utc, "
            "committed_at_utc, evidence_ref, support_ref) VALUES (?,?,'DRIVE_STAGE',?,'EG_SYNTHETIC_TEMP',?,"
            "'synthV2Binding',?,?,'ARCHIVE_COMMITTED',?,?,?,?,?)",
            (
                _deterministic_uuid("v2-stage:" + invoice_id + op_state), invoice_id, op_state, invoice["archive_relpath"],
                invoice["byte_size"], invoice["sha256"], PREDECESSOR_RUN, stamp,
                stamp if op_state == "COMMITTED" else None,
                "EG_SYNTHETIC_V2_STAGE" if op_state == "COMMITTED" else None,
                "EG_SYNTHETIC_V2_HOLD" if op_state == "HOLD" else None,
            ),
        ),),
        ("energygrid_file_operation_drive_stage_retired_v3",),
    )


def set_v2_drive_state(path: Path, invoice_id: str, drive_state: str) -> None:
    """A historical v2 drive_state other than NOT_STAGED on the invoice, with no DRIVE_STAGE operation."""
    inject_frozen_v2_evidence(
        path,
        (("UPDATE energygrid_invoice_v2 SET drive_state=? WHERE invoice_id=?", (drive_state, invoice_id)),),
        ("energygrid_invoice_drive_legacy_freeze_v3",),
    )


def v3_status(path: Path) -> str:
    connection = raw(path)
    try:
        return _inspect_v3_database(connection)
    finally:
        connection.close()


def insert_predecessor_on_day(path: Path, *, source_filename: str, bill_date: str, day_ordinal: int,
                              stream: str = "EB_BILL") -> str:
    """A CLASSIFIED v3 row whose day_ordinal is forced (M2). Inconsistent with its bill_date on purpose.

    A valid v3 database cannot hold two CLASSIFIED invoices of one stream on one day (the archive path
    index is unique), so the latest-day ambiguity guard is defence in depth. This row reaches it.
    """
    import uuid

    from fixtures.synthetic_dual_stream import SYNTHETIC_EVIDENCE, SYNTHETIC_NAMESPACE

    folder = "EB Bill" if stream == "EB_BILL" else "Tenant Bill"
    invoice_id = "egpre-" + uuid.uuid4().hex
    connection = raw(path)
    try:
        connection.execute(
            "INSERT INTO energygrid_invoice_v2 (invoice_id, classification, stream, source_namespace, source_invoice_key, "
            "source_filename, raw_date, date_profile, bill_date, day_ordinal, evidence_ref, canonical_filename, "
            "archive_relpath, archive_state, migration_state, drive_state, created_at_utc, created_run_id) "
            "VALUES (?, 'CLASSIFIED', ?, ?, ?, ?, ?, 'INVOICE_DATE_ISO_V1', ?, ?, ?, ?, ?, 'ABSENT', 'NOT_REQUIRED', "
            "'NOT_STAGED', ?, ?)",
            (
                invoice_id, stream, SYNTHETIC_NAMESPACE, filename_key(source_filename), source_filename, bill_date,
                bill_date, day_ordinal, SYNTHETIC_EVIDENCE, f"{bill_date}.pdf", f"{folder}/{bill_date}.pdf", NOW, RUN_ID,
            ),
        )
    finally:
        connection.close()
    return invoice_id


class PredecessorBuilderTests(unittest.TestCase):
    def test_the_built_predecessor_has_the_pinned_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pinned = root / "pinned.sqlite"
            create_predecessor(pinned)
            built = root / "built.sqlite"
            build_predecessor_v3(built, root / "archive", {"EB_BILL": "ADOPTABLE", "TENANT_BILL": "DELIVERED"})
            pinned_conn = raw(pinned)
            built_conn = raw(built)
            try:
                from energygrid_bill_downloader.state import _schema_manifest

                self.assertEqual(_schema_manifest(pinned_conn), _schema_manifest(built_conn))
            finally:
                pinned_conn.close()
                built_conn.close()

    def test_the_built_predecessor_rows_match_the_pinned_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            built = root / "built.sqlite"
            build_predecessor_v3(built, root / "archive",
                                 {"EB_BILL": "ADOPTABLE", "TENANT_BILL": "DELIVERED"})
            self.assertEqual(PINNED_PREDECESSOR_DIGEST, predecessor_digest(built))


class PredecessorAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        from fixtures.synthetic_dual_stream import create_v3_database

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state.sqlite"
        create_v3_database(self.path)
        migrate_state_database_v4(self.path, apply=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_an_identity_that_v3_already_holds_is_never_readmitted(self) -> None:
        insert_predecessor(self.path, source_filename="Invoice.pdf", day="2026-10-01")
        with leased(self.path) as store:
            with self.assertRaises(OfflineV4Error) as caught:
                admit(store, namespace=SYNTH_NS, name="Invoice.pdf", invoice_date=date(2026, 10, 1))
            self.assertEqual("EG_V4_PREDECESSOR_IDENTITY_PRESENT", caught.exception.support_ref)
            with self.assertRaises(OfflineV4Error) as caught:
                store.check_admissible(
                    stream="EB_BILL", source_namespace="SYNTHETIC-ENERGYGRID-NAMESPACE",
                    source_invoice_key=filename_key("Invoice.pdf"), source_filename="Invoice.pdf",
                    canonical_filename="2026-10-01.pdf", invoice_date=date(2026, 10, 1),
                )
            self.assertEqual("EG_V4_PREDECESSOR_IDENTITY_PRESENT", caught.exception.support_ref)
            self.assertEqual(0, store.count("energygrid_v4_invoice"))

    def test_a_different_identity_on_a_predecessor_archive_date_is_refused(self) -> None:
        insert_predecessor(self.path, source_filename="first.pdf", day="2026-10-01")
        with leased(self.path) as store:
            with self.assertRaises(OfflineV4Error) as caught:
                admit(store, namespace=SYNTH_NS, name="second.pdf", invoice_date=date(2026, 10, 1))
            self.assertEqual("EG_V4_PREDECESSOR_ARCHIVE_PRESENT", caught.exception.support_ref)

    def test_an_identity_older_than_the_predecessor_newest_is_a_regression(self) -> None:
        insert_predecessor(self.path, source_filename="x.pdf", day="2026-10-10")
        with leased(self.path) as store:
            with self.assertRaises(OfflineV4Error) as caught:
                admit(store, namespace=SYNTH_NS, name="y.pdf", invoice_date=date(2026, 10, 1))
            self.assertEqual("EG_V4_SOURCE_REGRESSION", caught.exception.support_ref)

    def test_a_newer_identity_is_admitted_after_the_predecessor(self) -> None:
        insert_predecessor(self.path, source_filename="x.pdf", day="2026-10-01")
        with leased(self.path) as store:
            _invoice, created = admit(store, namespace=SYNTH_NS, name="y.pdf", invoice_date=date(2026, 10, 5))
            self.assertTrue(created)


class ImmutabilityAndTransitionTriggerTests(_V4Base):
    def setUp(self) -> None:
        super().setUp()
        with leased(self.path) as store:
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

    def test_insert_or_replace_cannot_overwrite_any_immutable_row(self) -> None:
        def replace_with_change(table: str, key_column: str, key_value, changed: str, value,
                                tokens: tuple = ("EG_V4_IMMUTABLE_ROW",)) -> None:
            connection = raw(self.path)
            try:
                columns = [item[1] for item in connection.execute(f"PRAGMA table_info({table})")]
                row = dict(zip(columns, connection.execute(
                    f"SELECT * FROM {table} WHERE {key_column}=?", (key_value,)).fetchone()))
                row[changed] = value
                placeholders = ",".join("?" for _ in columns)
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                        tuple(row[c] for c in columns),
                    )
                self.assertTrue(any(token in str(caught.exception) for token in tokens), str(caught.exception))
            finally:
                connection.close()

        invoice_key = self.invoice["invoice_key"]
        replace_with_change("energygrid_v4_invoice", "invoice_key", invoice_key, "sha256", SHA_B)
        replace_with_change("energygrid_v4_intent", "intent_id", self.gmail, "payload_sha256", SHA_B)
        replace_with_change("energygrid_v4_dispatch", "intent_id", self.drive, "run_id", RUN_ID_2,
                            tokens=("EG_V4_IMMUTABLE_ROW", "EG_V4_DISPATCH_NOT_PENDING"))
        connection = raw(self.path)
        seq = connection.execute("SELECT MIN(transition_seq) FROM energygrid_v4_transition").fetchone()[0]
        receipt = connection.execute("SELECT MIN(receipt_seq) FROM energygrid_v4_receipt").fetchone()[0]
        connection.close()
        replace_with_change("energygrid_v4_transition", "transition_seq", seq, "to_state", "HOLD")
        replace_with_change("energygrid_v4_receipt", "receipt_seq", receipt, "outcome", "REJECTED")
        check = raw(self.path)
        try:
            self.assertEqual(SHA_A, check.execute(
                "SELECT sha256 FROM energygrid_v4_invoice WHERE invoice_key=?", (invoice_key,)).fetchone()[0])
        finally:
            check.close()

    def test_insert_or_replace_cannot_admit_a_second_row_on_a_unique_key(self) -> None:
        connection = raw(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                connection.execute(
                    "INSERT OR REPLACE INTO energygrid_v4_invoice (invoice_key, stream, source_namespace, "
                    "source_invoice_key, invoice_date, canonical_filename, archive_relpath, display_filename, "
                    "display_key, sha256, byte_size, admission_seq, admitted_run_id, admitted_at_utc) VALUES "
                    "('egi4-" + "f" * 32 + "', 'EB_BILL', 'SYN-NS', 'other.pdf', '2026-10-01', '2026-10-01.pdf', "
                    "'EB Bill/2026-10-01.pdf', 'OCT 2026 - other.pdf', 'oct 2026 - other.pdf', ?, 10, 99, ?, ?)",
                    (SHA_B, RUN_ID, NOW),
                )
            self.assertIn("EG_V4_IMMUTABLE_ROW", str(caught.exception))
        finally:
            connection.close()
        check = raw(self.path)
        try:
            self.assertEqual(1, check.execute("SELECT COUNT(*) FROM energygrid_v4_invoice").fetchone()[0])
        finally:
            check.close()

    def test_a_status_row_cannot_be_inserted_or_replaced_outside_admission(self) -> None:
        self._expect(
            "INSERT OR REPLACE INTO energygrid_v4_intent_status (intent_id, state, version, support_ref, "
            "updated_run_id, updated_at_utc) VALUES (?, 'VERIFIED', 1, NULL, ?, ?)",
            "EG_V4_ILLEGAL_TRANSITION",
            (self.gmail, RUN_ID, NOW),
        )
        self._expect(
            "INSERT OR REPLACE INTO energygrid_v4_intent_status (intent_id, state, version, support_ref, "
            "updated_run_id, updated_at_utc) VALUES (?, 'PENDING', 1, NULL, ?, ?)",
            "EG_V4_ILLEGAL_TRANSITION",
            (self.gmail, RUN_ID, NOW),
        )

    def test_verified_status_needs_a_receipt_whose_sha_is_the_archive_sha(self) -> None:
        with leased(self.path) as store:
            store.seal_dispatch(self.gmail, run_id=RUN_ID, now_iso=NOW)
        connection = raw(self.path)
        try:
            connection.execute(
                "INSERT INTO energygrid_v4_receipt (intent_id, digest, outcome, provider_ref, provider_sha256, run_id, at_utc) "
                "VALUES (?,?,?,?,?,?,?)",
                (self.gmail, "d" * 64, "VERIFIED", "fake-wrong-sha", SHA_B, RUN_ID, NOW),
            )
        finally:
            connection.close()
        self._expect(
            "UPDATE energygrid_v4_intent_status SET state='VERIFIED', version=version+1 WHERE intent_id=?",
            "EG_V4_VERIFIED_WITHOUT_RECEIPT",
            (self.gmail,),
        )

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
        with leased(self.path) as store:
            store.seal_dispatch(self.gmail, run_id=RUN_ID, now_iso=NOW)
        self._expect(
            "UPDATE energygrid_v4_intent_status SET state='VERIFIED', version=version+1 WHERE intent_id=?",
            "EG_V4_VERIFIED_WITHOUT_RECEIPT",
            (self.gmail,),
        )

    def test_gmail_cannot_be_dispatched_before_drive_is_verified(self) -> None:
        with leased(self.path) as store:
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

    def test_a_session_whose_lease_was_taken_over_cannot_write_again(self) -> None:
        with StateV4Store(self.path) as first, StateV4Store(self.path) as second:
            first.acquire_lease("session-a", now_ms=1_000, ttl_ms=10_000)
            second.acquire_lease("session-b", now_ms=20_000, ttl_ms=10_000)
            with self.assertRaises(OfflineV4Error) as caught:
                first.set_cursor("EB_BILL", now_iso=NOW)
            self.assertEqual("EG_V4_LEASE_LOST", caught.exception.support_ref)
            self.assertIsNone(second.cursor())
            first.release_lease("session-a")
            with self.assertRaises(OfflineV4Error) as caught:
                first.acquire_lease("session-a", now_ms=21_000, ttl_ms=10_000)
            self.assertEqual("EG_V4_LEASE_HELD", caught.exception.support_ref)
            second.set_cursor("TENANT_BILL", now_iso=NOW)
            self.assertEqual("TENANT_BILL", second.cursor())

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
        with leased(self.path) as store:
            for args in (("", 1, 1), ("x" * 129, 1, 1), ("ok", 1, 0), ("ok", "1", 1)):
                with self.subTest(args=args), self.assertRaises(OfflineV4Error) as caught:
                    store.acquire_lease(args[0], now_ms=args[1], ttl_ms=args[2])
                self.assertEqual("EG_V4_LEASE_INVALID", caught.exception.support_ref)

    def test_cursor_round_trips_and_rejects_unknown_streams(self) -> None:
        with leased(self.path) as store:
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
        self.store.acquire_lease("test-session", now_ms=1_000, ttl_ms=10**9)
        self.invoice, _ = admit(self.store)
        self.drive = self.intents(self.store, self.invoice)["DRIVE_UPLOAD"]["intent_id"]

    def tearDown(self) -> None:
        self.store.release_lease("test-session")
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


def _run_lock_is_free(directory: Path) -> bool:
    """True when the shipped single-run lock on this directory can be taken now (it is released at once)."""
    from energygrid_bill_downloader.errors import RunLockedError
    from energygrid_bill_downloader.run_lock import RunLock

    try:
        with RunLock(directory):
            return True
    except RunLockedError:
        return False


def _v4_object_count(path: Path) -> int:
    return len(_predecessor_rows(path, "SELECT name FROM sqlite_schema WHERE name LIKE 'energygrid_v4_%'"))


def _user_version(path: Path) -> int:
    return _predecessor_rows(path, "PRAGMA user_version")[0]["user_version"]


class _BuiltPredecessorBase(unittest.TestCase):
    """A bound v3 database built through the frozen v3 API: EB latest ADOPTABLE, Tenant latest DELIVERED."""

    SCENARIOS = {"EB_BILL": "ADOPTABLE", "TENANT_BILL": "DELIVERED"}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "state.sqlite"
        self.archive = self.dir / "archive"
        self.built = build_predecessor_v3(self.path, self.archive, self.SCENARIOS)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def backups(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir() if ".v4-migration-" in p.name)

    def assert_refused_unchanged(self, call, support_ref: str, before_digest: str) -> OfflineV4Error:
        with self.assertRaises(OfflineV4Error) as caught:
            call()
        self.assertEqual(support_ref, caught.exception.support_ref)
        self.assertEqual(3, _user_version(self.path))
        self.assertEqual(0, _v4_object_count(self.path))
        self.assertEqual([], self.backups())
        self.assertEqual(before_digest, predecessor_digest(self.path))
        self.assertTrue(_run_lock_is_free(self.dir))
        return caught.exception


class MigrationBlockerTests(_BuiltPredecessorBase):
    """M5a-M5i: each local v3 blocker refuses plan and apply on its own (#226 v4 correction F2/F6)."""

    def _inflight_invoice(self) -> str:
        from energygrid_bill_downloader.invoice import Stream
        from fixtures.synthetic_dual_stream import candidate

        with StateV3Store(self.path) as state:
            return state.accept_latest(
                candidate(Stream.EB_BILL, name="Invoice EB Oct.pdf", invoice_date="2026-10-01"), PREDECESSOR_RUN, T_ACCEPT,
            )

    def _archive_state(self, value: str) -> None:
        invoice_id = self._inflight_invoice()
        with StateV3Store(self.path) as state:
            state.update_invoice_file_state(invoice_id, archive_state=value)

    def _migration_state(self, value: str) -> None:
        connection = raw(self.path)
        try:
            connection.execute("UPDATE energygrid_invoice_v2 SET migration_state=? WHERE invoice_id=?",
                               (value, self.built["EB_BILL"]["older"]))
        finally:
            connection.close()

    def _stream_hold(self) -> None:
        connection = raw(self.path)
        try:
            connection.execute("UPDATE energygrid_stream_v2 SET admission='HOLD' WHERE stream='EB_BILL'")
        finally:
            connection.close()

    def _file_operation(self, value: str) -> None:
        from energygrid_bill_downloader.publication import validate_pdf

        invoice_id = self._inflight_invoice()
        staged = self.dir / "inflight.pdf"
        staged.write_bytes(predecessor_pdf("Invoice EB Oct.pdf"))
        with StateV3Store(self.path) as state:
            invoice = state.invoice(invoice_id)
            operation = _deterministic_uuid("inflight:" + invoice_id)
            state.start_file_operation(
                operation_id=operation, invoice_id=invoice_id, kind="ARCHIVE_PUBLISH", private_path_ref="EG_SYNTHETIC_TEMP",
                target_relpath=invoice["archive_relpath"], binding_id=None, info=validate_pdf(staged),
                source_role="SOURCE_ACQUISITION", run_id=PREDECESSOR_RUN, timestamp=T_ARCHIVE,
            )
            if value == "HOLD":
                state.hold_file_operation(operation, "EG_SYNTHETIC_HOLD")
        staged.unlink()

    def _check_case(self, maker: str, value) -> None:
        getattr(self, maker)(*(() if value is None else (value,)))
        # The shipped v3 inspector still accepts the database, so only the v4 blocker clause can refuse it.
        self.assertIn(v3_status(self.path), {"COMPLETE_V3", "RESUMABLE_V3"})
        before = predecessor_digest(self.path)
        for apply in (False, True):
            with self.subTest(apply=apply):
                self.assert_refused_unchanged(
                    lambda: migrate_state_database_v4(self.path, apply=apply), "EG_V4_PREDECESSOR_NOT_COMPLETE_V3", before,
                )

    def test_m5a_archive_prepared(self) -> None:
        self._check_case("_archive_state", "PREPARED")

    def test_m5b_archive_conflict(self) -> None:
        self._check_case("_archive_state", "CONFLICT")

    def test_m5c_archive_unverified(self) -> None:
        self._check_case("_archive_state", "UNVERIFIED")

    def test_m5d_migration_unclassified(self) -> None:
        self._check_case("_migration_state", "UNCLASSIFIED")

    def test_m5e_migration_move_planned(self) -> None:
        self._check_case("_migration_state", "MOVE_PLANNED")

    def test_m5f_migration_hold(self) -> None:
        self._check_case("_migration_state", "HOLD")

    def test_m5g_stream_admission_hold(self) -> None:
        self._check_case("_stream_hold", None)

    def test_m5h_non_drive_stage_file_operation_prepared(self) -> None:
        self._check_case("_file_operation", "PREPARED")

    def test_m5i_non_drive_stage_file_operation_hold(self) -> None:
        self._check_case("_file_operation", "HOLD")

    def test_m5_positive_the_clean_database_plans_and_migrates(self) -> None:
        before = predecessor_digest(self.path)
        self.assertEqual({"status": "PLAN_V4", "predecessor_digest": before}, migrate_state_database_v4(self.path))
        self.assertEqual({"status": "MIGRATED_V4", "predecessor_digest": before},
                         migrate_state_database_v4(self.path, apply=True))
        self.assertEqual(before, predecessor_digest(self.path))
        self.assertEqual(1, len(self.backups()))

    def test_m5_positive_drive_stage_prepared_or_hold_is_not_a_blocker_but_the_classifier_holds(self) -> None:
        for op_state in ("PREPARED", "HOLD"):
            with self.subTest(op_state=op_state):
                self.tearDown()
                self.setUp()
                add_v2_drive_stage(self.path, self.built["EB_BILL"]["latest"], op_state)
                self.assertIn(v3_status(self.path), {"COMPLETE_V3", "RESUMABLE_V3"})
                before = predecessor_digest(self.path)
                self.assertEqual("PLAN_V4", migrate_state_database_v4(self.path)["status"])
                self.assertEqual("MIGRATED_V4", migrate_state_database_v4(self.path, apply=True)["status"])
                with StateV4Store(self.path) as store:
                    latest = store.predecessor_latest("EB_BILL")
                self.assertEqual(("HOLD", "EG_V4_PREDECESSOR_DRIVE_EVIDENCE_PRESENT"),
                                 (latest["status"], latest["support_ref"]))
                self.assertEqual(before, predecessor_digest(self.path))


class PredecessorClassifierTests(_BuiltPredecessorBase):
    """M1-M4 at the classifier and adoption boundary. Orchestration-level runs are in test_orchestration_v4."""

    def classify(self, stream: str = "EB_BILL") -> dict:
        with StateV4Store(self.path) as store:
            return store.predecessor_latest(stream)

    def test_m1_v2_drive_stage_operation_alone_holds_the_latest(self) -> None:
        for op_state in ("COMMITTED", "PREPARED", "HOLD"):
            with self.subTest(op_state=op_state):
                self.tearDown()
                self.setUp()
                add_v2_drive_stage(self.path, self.built["EB_BILL"]["latest"], op_state)
                self.assertIn(v3_status(self.path), {"COMPLETE_V3", "RESUMABLE_V3"})
                invoice = _predecessor_rows(self.path, "SELECT drive_state FROM energygrid_invoice_v2 WHERE invoice_id=?",
                                            (self.built["EB_BILL"]["latest"],))[0]
                self.assertEqual("NOT_STAGED", invoice["drive_state"])
                migrate_state_database_v4(self.path, apply=True)
                result = self.classify()
                self.assertEqual(("HOLD", "EG_V4_PREDECESSOR_DRIVE_EVIDENCE_PRESENT"), (result["status"], result["support_ref"]))
                self.assertEqual(self.built["EB_BILL"]["latest"], result["invoice"]["invoice_id"])

    def test_m1_v2_drive_state_alone_holds_the_latest(self) -> None:
        for drive_state in ("PREPARED", "CONFLICT"):
            with self.subTest(drive_state=drive_state):
                self.tearDown()
                self.setUp()
                set_v2_drive_state(self.path, self.built["EB_BILL"]["latest"], drive_state)
                self.assertIn(v3_status(self.path), {"COMPLETE_V3", "RESUMABLE_V3"})
                self.assertEqual([], _predecessor_rows(
                    self.path, "SELECT 1 FROM energygrid_file_operation_v2 WHERE kind='DRIVE_STAGE'"))
                migrate_state_database_v4(self.path, apply=True)
                result = self.classify()
                self.assertEqual(("HOLD", "EG_V4_PREDECESSOR_DRIVE_EVIDENCE_PRESENT"), (result["status"], result["support_ref"]))

    def test_m1_positive_the_clean_latest_is_adoptable(self) -> None:
        migrate_state_database_v4(self.path, apply=True)
        result = self.classify()
        self.assertEqual(("ADOPTABLE", None), (result["status"], result["support_ref"]))
        self.assertEqual(self.built["EB_BILL"]["latest"], result["invoice"]["invoice_id"])

    def test_m2_two_classified_invoices_on_the_latest_day_hold_with_no_invoice(self) -> None:
        migrate_state_database_v4(self.path, apply=True)
        latest_day = date.fromisoformat(LATEST_SOURCE["EB_BILL"][1]).toordinal()
        insert_predecessor_on_day(self.path, source_filename="Invoice EB Sep B.pdf", bill_date="2026-08-31",
                                  day_ordinal=latest_day)
        result = self.classify()
        self.assertEqual({"status": "HOLD", "support_ref": "EG_V4_PREDECESSOR_LATEST_AMBIGUOUS", "invoice": None}, result)

    def test_m2_positive_distinct_latest_dates_resolve_and_an_older_day_tie_does_not_block(self) -> None:
        migrate_state_database_v4(self.path, apply=True)
        older_day = date.fromisoformat("2026-07-01").toordinal()
        insert_predecessor_on_day(self.path, source_filename="Old A.pdf", bill_date="2026-07-01", day_ordinal=older_day)
        insert_predecessor_on_day(self.path, source_filename="Old B.pdf", bill_date="2026-07-02", day_ordinal=older_day)
        result = self.classify()
        self.assertEqual(("ADOPTABLE", self.built["EB_BILL"]["latest"]), (result["status"], result["invoice"]["invoice_id"]))
        newer = insert_predecessor_on_day(self.path, source_filename="Newer.pdf", bill_date="2026-09-20",
                                          day_ordinal=date.fromisoformat("2026-09-20").toordinal())
        result = self.classify()
        self.assertEqual(newer, result["invoice"]["invoice_id"])
        self.assertNotEqual("EG_V4_PREDECESSOR_LATEST_AMBIGUOUS", result["support_ref"])

    def test_m3_a_claimed_but_unsettled_delivery_holds_with_the_dispatch_reference(self) -> None:
        self.tearDown()
        self.SCENARIOS = {"EB_BILL": "DELIVERY_DISPATCHED", "TENANT_BILL": "DELIVERED"}
        self.setUp()
        delivery = _predecessor_rows(self.path, "SELECT state, dispatch_started_at_utc FROM energygrid_delivery_v1 "
                                                "WHERE invoice_id=?", (self.built["EB_BILL"]["latest"],))[0]
        self.assertEqual("PENDING_SEND", delivery["state"])
        self.assertIsNotNone(delivery["dispatch_started_at_utc"])
        migrate_state_database_v4(self.path, apply=True)
        result = self.classify()
        self.assertEqual(("HOLD", "EG_V4_PREDECESSOR_DELIVERY_DISPATCHED"), (result["status"], result["support_ref"]))

    def test_m3_positive_an_undispatched_pending_send_is_not_a_dispatch_hold(self) -> None:
        # v3 accepts a PENDING_SEND delivery only after a DRIVE_VERIFIED receipt, so the undispatched case is
        # classified past the dispatch guard and held by the Drive-verified rule, never as dispatched.
        self.tearDown()
        self.SCENARIOS = {"EB_BILL": "DELIVERY_PREPARED", "TENANT_BILL": "DELIVERED"}
        self.setUp()
        migrate_state_database_v4(self.path, apply=True)
        result = self.classify()
        self.assertEqual(("HOLD", "EG_V4_PREDECESSOR_DRIVE_VERIFIED_NOT_SENT"), (result["status"], result["support_ref"]))

    def _adopt(self, store, **overrides):
        latest = store.predecessor_latest("EB_BILL")["invoice"] or {}
        arguments = {
            "invoice_id": self.built["EB_BILL"]["latest"],
            "display_filename": "SEP 2026 - Invoice EB Sep.pdf",
            "sha256": latest.get("sha256"),
            "byte_size": latest.get("byte_size"),
            "run_id": RUN_ID,
            "now_iso": NOW,
        }
        arguments.update(overrides)
        return store.adopt_predecessor_latest("EB_BILL", **arguments)

    def test_m4_stale_or_mismatched_adoption_is_refused_with_no_admission(self) -> None:
        migrate_state_database_v4(self.path, apply=True)
        before = predecessor_digest(self.path)
        with leased(self.path) as store:
            frozen = store.predecessor_latest("EB_BILL")["invoice"]
            cases = (
                ({"invoice_id": self.built["EB_BILL"]["older"]}, "EG_V4_PREDECESSOR_ADOPTION_STALE"),
                ({"sha256": SHA_B}, "EG_V4_PREDECESSOR_ARCHIVE_IDENTITY_MISMATCH"),
                ({"byte_size": frozen["byte_size"] + 1}, "EG_V4_PREDECESSOR_ARCHIVE_IDENTITY_MISMATCH"),
            )
            for overrides, support_ref in cases:
                with self.subTest(overrides=overrides), self.assertRaises(OfflineV4Error) as caught:
                    self._adopt(store, **overrides)
                self.assertEqual(support_ref, caught.exception.support_ref)
                self.assertEqual(0, store.count("energygrid_v4_invoice"))
                self.assertEqual(0, store.count("energygrid_v4_intent"))
        self.assertEqual(before, predecessor_digest(self.path))

    def test_m4_a_classification_that_changes_before_the_fenced_write_is_stale(self) -> None:
        migrate_state_database_v4(self.path, apply=True)
        with leased(self.path) as store:
            frozen = store.predecessor_latest("EB_BILL")
            self.assertEqual("ADOPTABLE", frozen["status"])
            # Between the read and the fenced write the stream's latest changes (a newer v3 row appears).
            insert_predecessor(self.path, source_filename="Invoice EB Sep 20.pdf", day="2026-09-20")
            with self.assertRaises(OfflineV4Error) as caught:
                self._adopt(store, sha256=frozen["invoice"]["sha256"], byte_size=frozen["invoice"]["byte_size"])
            self.assertEqual("EG_V4_PREDECESSOR_ADOPTION_STALE", caught.exception.support_ref)
            self.assertEqual(0, store.count("energygrid_v4_invoice"))

    def test_m4_positive_a_matching_adoption_succeeds_once_and_a_repeat_is_idempotent(self) -> None:
        migrate_state_database_v4(self.path, apply=True)
        with leased(self.path) as store:
            self.assertTrue(self._adopt(store))
            counts = [store.count(t) for t in ("energygrid_v4_invoice", "energygrid_v4_intent", "energygrid_v4_transition")]
            self.assertFalse(self._adopt(store))
            self.assertEqual(counts, [store.count(t) for t in ("energygrid_v4_invoice", "energygrid_v4_intent",
                                                                "energygrid_v4_transition")])
            adopted = store.adopted_predecessor("EB_BILL", self.built["EB_BILL"]["latest"])
            self.assertEqual("EB Bill/2026-09-01.pdf", adopted["archive_relpath"])
            self.assertIsNone(store.adopted_predecessor("TENANT_BILL", self.built["EB_BILL"]["latest"]))


class MigrationRunLockTests(_BuiltPredecessorBase):
    """M9, M9b: the v4 apply holds the shipped v3 single-run lock (#226 v4 correction F1)."""

    def test_m9_apply_is_refused_while_a_cooperating_v3_writer_holds_the_run_lock(self) -> None:
        from energygrid_bill_downloader.errors import RUN_IN_PROGRESS
        from energygrid_bill_downloader.run_lock import RunLock

        before = predecessor_digest(self.path)
        state_bytes = self.path.read_bytes()
        with RunLock(self.path.parent):  # exactly as cli.py and orchestration.py hold it around StateV3Store
            with self.assertRaises(OfflineV4Error) as caught:
                migrate_state_database_v4(self.path, apply=True)
            self.assertEqual("EG_V4_RUN_LOCKED", caught.exception.support_ref)
            self.assertEqual(10, caught.exception.exit_code)
            self.assertEqual(RUN_IN_PROGRESS, caught.exception.status)
        self.assertEqual(state_bytes, self.path.read_bytes())
        self.assertEqual(3, _user_version(self.path))
        self.assertEqual(0, _v4_object_count(self.path))
        self.assertEqual([], self.backups())
        self.assertEqual(before, predecessor_digest(self.path))

    def test_m9_the_lock_identity_is_the_resolved_state_directory(self) -> None:
        from energygrid_bill_downloader.run_lock import RunLock

        (self.dir / "sub").mkdir()
        unresolved = self.dir / "sub" / ".." / "state.sqlite"
        with RunLock(self.dir):
            with self.assertRaises(OfflineV4Error) as caught:
                migrate_state_database_v4(unresolved, apply=True)
        self.assertEqual("EG_V4_RUN_LOCKED", caught.exception.support_ref)
        self.assertEqual(0, _v4_object_count(self.path))

    def test_m9_plan_stays_lock_free_and_read_only(self) -> None:
        from energygrid_bill_downloader.run_lock import RunLock

        state_bytes = self.path.read_bytes()
        with RunLock(self.path.parent):
            self.assertEqual("PLAN_V4", migrate_state_database_v4(self.path)["status"])
        self.assertEqual(state_bytes, self.path.read_bytes())

    def test_m9_positive_an_unheld_apply_migrates_and_fresh_legacy_writers_are_then_refused(self) -> None:
        from energygrid_bill_downloader.run_lock import RunLock
        from energygrid_bill_downloader.state import StateStore

        self.assertEqual("MIGRATED_V4", migrate_state_database_v4(self.path, apply=True)["status"])
        with RunLock(self.path.parent):
            with self.assertRaises(StateError):
                with StateV3Store(self.path):
                    pass
            with self.assertRaises(StateError):
                with StateStore(self.path):
                    pass
        self.assertEqual(4, _user_version(self.path))

    def test_m9_the_lock_is_held_from_before_the_sqlite_open_until_after_the_final_check(self) -> None:
        from energygrid_bill_downloader import state as state_module

        held: list[tuple[str, bool]] = []
        real_connect = sqlite3.connect
        real_inspect_v4 = state_module._inspect_v4_database

        def connect(*args, **kwargs):
            if args and str(args[0]) == str(self.path):
                held.append(("connect", not _run_lock_is_free(self.dir)))
            return real_connect(*args, **kwargs)

        def inspect_v4(connection):
            held.append(("inspect_v4", not _run_lock_is_free(self.dir)))
            return real_inspect_v4(connection)

        with mock.patch.object(state_module.sqlite3, "connect", side_effect=connect), \
                mock.patch.object(state_module, "_inspect_v4_database", side_effect=inspect_v4):
            migrate_state_database_v4(self.path, apply=True)
        self.assertEqual([("connect", True), ("inspect_v4", True)], held)
        self.assertTrue(_run_lock_is_free(self.dir))

    def test_m9b_the_lock_is_released_after_success_and_after_every_refusal(self) -> None:
        from energygrid_bill_downloader import state as state_module

        # Blocker refusal.
        connection = raw(self.path)
        connection.execute("UPDATE energygrid_stream_v2 SET admission='HOLD' WHERE stream='EB_BILL'")
        connection.close()
        with self.assertRaises(OfflineV4Error):
            migrate_state_database_v4(self.path, apply=True)
        self.assertTrue(_run_lock_is_free(self.dir))
        # Missing database.
        with self.assertRaises(OfflineV4Error) as caught:
            migrate_state_database_v4(self.dir / "absent.sqlite", apply=True)
        self.assertEqual("EG_V4_PREDECESSOR_MISSING", caught.exception.support_ref)
        self.assertTrue(_run_lock_is_free(self.dir))
        # A failure after the schema apply, before the commit.
        self.tearDown()
        self.setUp()
        with mock.patch.object(state_module, "_inspect_v4_database", return_value="INCOMPATIBLE_V4"):
            with self.assertRaises(OfflineV4Error) as caught:
                migrate_state_database_v4(self.path, apply=True)
        self.assertEqual("EG_V4_SCHEMA_VALIDATION_FAILED", caught.exception.support_ref)
        self.assertTrue(_run_lock_is_free(self.dir))
        self.assertEqual(3, _user_version(self.path))
        # An unexpected exception inside the apply.
        with mock.patch.object(state_module, "_backup_state_file_exclusive", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                migrate_state_database_v4(self.path, apply=True)
        self.assertTrue(_run_lock_is_free(self.dir))
        # Success.
        self.assertEqual("MIGRATED_V4", migrate_state_database_v4(self.path, apply=True)["status"])
        self.assertTrue(_run_lock_is_free(self.dir))


class ShippedV3LockStaticProofTests(unittest.TestCase):
    """M9c: every shipped v3 store and migration entry point runs inside the single-run lock."""

    PACKAGE = Path(__file__).resolve().parents[1] / "energygrid_bill_downloader"
    WRITERS = {"StateV2Store", "StateV3Store", "migrate_state_database", "migrate_state_database_v3", "migrate"}

    @staticmethod
    def _callee(call) -> str | None:
        import ast

        if isinstance(call.func, ast.Name):
            return call.func.id
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        return None

    def _call_sites(self, module: str) -> list[tuple[int, str, str]]:
        import ast

        tree = ast.parse((self.PACKAGE / module).read_text(encoding="utf-8"), filename=module)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        sites = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or self._callee(node) not in self.WRITERS:
                continue
            plan = any(k.arg == "apply" and isinstance(k.value, ast.Constant) and k.value.value is False
                       for k in node.keywords)
            lock = None
            scope = parents.get(node)
            while scope is not None and lock is None:
                if isinstance(scope, ast.With):
                    for item in scope.items:
                        expr = item.context_expr
                        if (isinstance(expr, ast.Call) and self._callee(expr) in {"RunLock", "lock_factory"}
                                and [ast.unparse(a) for a in expr.args] == ["config.state_path.parent"]):
                            lock = ast.unparse(expr)
                scope = parents.get(scope)
            sites.append((node.lineno, self._callee(node), "PLAN_READ_ONLY" if plan else (lock or "UNLOCKED")))
        return sites

    def test_m9c_shipped_v3_entry_points_are_enclosed_by_the_run_lock(self) -> None:
        cli = [(name, lock) for _line, name, lock in self._call_sites("cli.py")]
        orchestration = [(name, lock) for _line, name, lock in self._call_sites("orchestration.py")]
        self.assertEqual(
            sorted([
                ("StateV3Store", "RunLock(config.state_path.parent)"),
                ("migrate", "RunLock(config.state_path.parent)"),
                ("migrate", "PLAN_READ_ONLY"),
            ]),
            sorted(cli),
        )
        self.assertEqual([("StateV3Store", "core.lock_factory(config.state_path.parent)")], orchestration)

    def test_m9c_the_shipped_lock_factory_default_is_the_run_lock(self) -> None:
        import ast

        tree = ast.parse((self.PACKAGE / "orchestration.py").read_text(encoding="utf-8"))
        assigned = [ast.unparse(node.value) for node in ast.walk(tree)
                    if isinstance(node, ast.Assign) and [ast.unparse(t) for t in node.targets] == ["lock_factory"]]
        self.assertEqual(["RunLock"], assigned)

    def test_m9c_config_state_paths_are_resolved_and_v4_locks_the_resolved_parent(self) -> None:
        import ast

        from energygrid_bill_downloader.config import require_external

        unresolved = Path(tempfile.gettempdir()) / "x" / ".." / "state.sqlite"
        self.assertEqual(unresolved.resolve(), require_external(unresolved, None, "state_path"))
        tree = ast.parse((self.PACKAGE / "state.py").read_text(encoding="utf-8"))
        locks = [ast.unparse(node) for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and self._callee(node) == "RunLock"]
        self.assertEqual(["RunLock(path.resolve(strict=False).parent)"], locks)

    def test_m9c_the_v1_store_cannot_open_a_v3_or_v4_database(self) -> None:
        from energygrid_bill_downloader.state import StateStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            create_predecessor(path)
            with self.assertRaises(StateError):
                with StateStore(path):
                    pass
            with self.assertRaises(StateError):
                with StateStore(path, read_only=True):
                    pass


if __name__ == "__main__":
    unittest.main()
