"""PostgreSQL boundary evidence for the source cursor and F1 result arity.

Two layers:

* ``BoundaryCursor`` is a scripted fake used for unit isolation. Unlike the
  earlier fake it can no longer hide a placeholder/parameter mismatch: every
  statement must have exactly as many ``%s`` placeholders as parameters, and,
  when psycopg is importable, every statement is also adapted through
  psycopg's real query converter (the same code path ``cursor.execute`` uses).
* ``RealPostgresTestCase`` runs the real ``PostgresRepository`` through
  psycopg against a disposable local PostgreSQL named only by
  ``XB_MEMBER_GATEWAY_TEST_DATABASE_URL``. It applies migrations 0001-0005 to
  a freshly dropped schema, so it refuses any non-loopback host. Without that
  variable the real-database tests skip and report themselves as skipped.
"""

import os
import re
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from xb_member_gateway.canonical import build_source_event, canonicalize_source_event
from xb_member_gateway.models import SourceAdmissionMode
from xb_member_gateway.repository import PostgresRepository, RepositoryError, SourceConflict

try:  # pragma: no cover - availability depends on the environment
    import psycopg
    from psycopg._queries import PostgresQuery
    from psycopg.adapt import Transformer
except ImportError:  # pragma: no cover
    psycopg = None


ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = sorted((ROOT / "member_gateway/migrations").glob("000[1-5]_*.sql"))
TEST_DSN_ENV = "XB_MEMBER_GATEWAY_TEST_DATABASE_URL"
HASH = "sha256:" + "a" * 64
CUTOVER = "2026-09-15T00:00:00Z"
CUTOVER_DT = datetime(2026, 9, 15, tzinfo=timezone.utc)
FORM = "synthetic-form"
PLACEHOLDER_RE = re.compile(r"%s")
# The exact pre-repair statement: 11 columns, 12 placeholders.
F1_DEFECTIVE_RESULTS_INSERT = (
    "INSERT INTO xb_member_gateway.results(job_id,result_hash,status,member_no,dispatch_fence_id,"
    "save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at) "
    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)


def result_params():
    return (
        "job-" + "1" * 32, HASH, "CREATED_VERIFIED", "6581234567", uuid.uuid4(), 1,
        True, True, False, None, datetime(2026, 9, 15, 1, tzinfo=timezone.utc),
    )


class PlaceholderArityError(AssertionError):
    pass


def assert_driver_accepts(query, params):
    """Fail on any placeholder/parameter mismatch, via psycopg when present."""

    placeholders = len(PLACEHOLDER_RE.findall(query.replace("%%", "")))
    if placeholders != len(params):
        raise PlaceholderArityError(f"placeholders={placeholders} params={len(params)}")
    if psycopg is not None:
        PostgresQuery(Transformer()).convert(query, params)


class BoundaryCursor:
    def __init__(self, connection):
        self.connection = connection
        self.query = ""
        self.params = ()
        self.rowcount = 1
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        assert_driver_accepts(query, params)
        self.query, self.params = " ".join(query.split()), tuple(params)
        self.connection.queries.append((self.query, self.params))
        self.rowcount = self.connection.rowcount_for(self.query)
        self._rows = list(self.connection.rows_for(self.query, self.params))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class ScriptedConnection:
    """Returns scripted rows for the source-admission statements."""

    def __init__(self, *, count=0, receipt=None, window_open=True, cutover=CUTOVER, form_id=FORM):
        self.count = count
        self.receipt = receipt
        self.window_open = window_open
        self.cutover = cutover
        self.form_id = form_id
        self.queries = []
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return BoundaryCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass

    def rowcount_for(self, query):
        if query.startswith("UPDATE xb_member_gateway.source_ingest_cursors SET initial_window_admission_count"):
            return 1 if self.window_open else 0
        return 1

    def rows_for(self, query, params):
        if "FROM xb_member_gateway.source_ingest_cursors" in query:
            yield ("google_forms", "member_registration", "member-intake.v1", CUTOVER_DT, self.cutover, self.form_id, 0, self.count)
        elif "FROM xb_member_gateway.source_handling_receipts" in query and self.receipt is not None:
            yield self.receipt


def source_event(response_id="forms-two", create_time="2026-09-15T00:00:01.123456789Z"):
    return canonicalize_source_event(
        build_source_event(
            response_id=response_id, create_time=create_time, request_id=f"request-{response_id}",
            form_alias="member_registration", mapping_version="member-intake.v1",
            payload={"name": "Synthetic Member", "phone": "81234567", "email": "synthetic@example.test", "birthday_month": "January", "marketing_consent": "No", "pdpa_acknowledged": True},
        )
    )


class DriverBoundaryTests(unittest.TestCase):
    """F1 negative and positive controls through the same boundary."""

    def test_defective_results_insert_is_rejected_and_repaired_statement_accepted(self):
        params = result_params()
        with self.assertRaises(PlaceholderArityError):
            assert_driver_accepts(F1_DEFECTIVE_RESULTS_INSERT, params)
        assert_driver_accepts(PostgresRepository._RESULTS_INSERT, params)
        self.assertEqual(PostgresRepository._RESULTS_INSERT.count("%s"), 11)

    @unittest.skipIf(psycopg is None, "psycopg driver unavailable")
    def test_real_psycopg_adaptation_rejects_defective_and_accepts_repaired(self):
        params = result_params()
        with self.assertRaisesRegex(psycopg.ProgrammingError, "12 placeholders but 11 parameters"):
            PostgresQuery(Transformer()).convert(F1_DEFECTIVE_RESULTS_INSERT, params)
        converted = PostgresQuery(Transformer())
        converted.convert(PostgresRepository._RESULTS_INSERT, params)
        self.assertEqual(len(converted.params), 11)

    def test_repository_source_contains_no_defective_results_insert(self):
        text = (ROOT / "member_gateway/src/xb_member_gateway/repository.py").read_text(encoding="utf-8")
        self.assertNotIn("VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)\", params", text)
        self.assertNotIn("VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)\", event_params", text)
        for statement in re.findall(r'"(INSERT INTO xb_member_gateway\.[a-z_]+\([^)]*\) VALUES\([^)]*\))', text):
            columns = statement.split("(", 1)[1].split(")", 1)[0].count(",") + 1
            values = statement.rsplit("VALUES(", 1)[1].rstrip(")").split(",")
            self.assertEqual(columns, len(values), statement[:80])


class PostgresCursorTests(unittest.TestCase):
    def repository(self, connection):
        return PostgresRepository(connection_factory=lambda: connection, reference_key=b"synthetic")

    def test_unseen_admission_writes_exact_time_receipt_and_atomic_guard(self):
        connection = ScriptedConnection()
        outcome = self.repository(connection).ingest_source_event(source_event(), initial_window_max=1)
        self.assertFalse(outcome.replayed)
        self.assertTrue(connection.committed)
        guard = [params for query, params in connection.queries if query.startswith("UPDATE xb_member_gateway.source_ingest_cursors")]
        self.assertEqual(len(guard), 1)
        receipt = next(params for query, params in connection.queries if query.startswith("INSERT INTO xb_member_gateway.source_handling_receipts"))
        self.assertIn("2026-09-15T00:00:01.123456789Z", receipt)
        response = next(params for query, params in connection.queries if query.startswith("INSERT INTO xb_member_gateway.source_responses"))
        self.assertIn("2026-09-15T00:00:01.123456789Z", response)
        self.assertEqual(outcome.job.member_payload["create_time"], "2026-09-15T00:00:01.123456Z")

    def test_losing_first_member_admission_rolls_back_without_receipt(self):
        connection = ScriptedConnection(count=1, window_open=False)
        with self.assertRaisesRegex(SourceConflict, "initial_source_window_exhausted"):
            self.repository(connection).ingest_source_event(source_event(), initial_window_max=1)
        self.assertTrue(connection.rolled_back)
        for table in ("jobs", "source_handling_receipts", "source_responses"):
            self.assertFalse(any(query.startswith(f"INSERT INTO xb_member_gateway.{table}") for query, _ in connection.queries), table)

    def test_continuous_admission_does_not_touch_the_guard(self):
        connection = ScriptedConnection(count=1)
        self.repository(connection).ingest_source_event(source_event(), initial_window_max=None)
        self.assertFalse(any(query.startswith("UPDATE xb_member_gateway.source_ingest_cursors") for query, _ in connection.queries))

    def test_no_correctness_path_reads_the_deprecated_admitted_tuple(self):
        text = (ROOT / "member_gateway/src/xb_member_gateway/repository.py").read_text(encoding="utf-8")
        for deprecated in ("last_admitted_create_time", "last_admitted_response_id", "resume_page_token", "source_event_behind_cursor", "source_ingest_page_receipts", "source_position"):
            self.assertNotIn(deprecated, text)

    def test_pre_cutover_and_uninitialized_cutover_fail_before_any_write(self):
        connection = ScriptedConnection()
        with self.assertRaisesRegex(SourceConflict, "source_event_before_cutover"):
            self.repository(connection).ingest_source_event(source_event(create_time="2026-09-14T23:59:59.999Z"))
        self.assertFalse(any(query.startswith("INSERT") for query, _ in connection.queries))
        connection = ScriptedConnection(cutover=None)
        with self.assertRaisesRegex(SourceConflict, "source_production_cutover_uninitialized"):
            self.repository(connection).ingest_source_event(source_event())

    def test_same_instant_different_exact_string_conflicts(self):
        receipt = ("forms-two", "hmac-v1:" + "0" * 64, "member_registration", FORM, "member-intake.v1", "2026-09-15T00:00:01Z", source_event(create_time="2026-09-15T00:00:01Z").payload_hash, "ACCEPTED", "job-x", None, 1, CUTOVER_DT)
        connection = ScriptedConnection(receipt=receipt)
        with self.assertRaisesRegex(SourceConflict, "source_identity_payload_conflict"):
            self.repository(connection).ingest_source_event(source_event(create_time="2026-09-15T00:00:01.000Z"))
        self.assertTrue(connection.rolled_back)


def real_postgres_dsn():
    dsn = os.environ.get(TEST_DSN_ENV)
    if not dsn:
        return None
    if urlsplit(dsn).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("refusing_non_loopback_test_database")
    return dsn


@unittest.skipIf(psycopg is None or real_postgres_dsn() is None, f"disposable PostgreSQL not configured ({TEST_DSN_ENV})")
class RealPostgresTestCase(unittest.TestCase):
    """Fresh schema per test on a disposable loopback PostgreSQL."""

    reference_key = b"synthetic-real-postgres-reference-key"

    def setUp(self):
        self.dsn = real_postgres_dsn()
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS xb_member_gateway CASCADE")
            for path in MIGRATIONS:
                connection.execute(path.read_text(encoding="utf-8"))
        self.repository = PostgresRepository(self.dsn, reference_key=self.reference_key)

    def sql(self, statement, params=()):
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            rows = connection.execute(statement, params)
            try:
                return rows.fetchall()
            except psycopg.ProgrammingError:
                return []

    def seed_cursor(self, cutover=CUTOVER, form_id=FORM):
        self.sql(
            "INSERT INTO xb_member_gateway.source_ingest_cursors(source_system,form_alias,mapping_version,watermark,scan_lower_bound,production_cutover_exact,form_id) VALUES('google_forms','member_registration','member-intake.v1',%s,%s,%s,%s)",
            (CUTOVER_DT, CUTOVER_DT, cutover, form_id),
        )


class RealPostgresSourceTests(RealPostgresTestCase):
    def test_f1_defective_statement_fails_and_repaired_statement_executes_on_a_real_connection(self):
        params = result_params()
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                with self.assertRaisesRegex(psycopg.ProgrammingError, "12 placeholders but 11 parameters"):
                    cursor.execute(F1_DEFECTIVE_RESULTS_INSERT, params)
            connection.rollback()
            with connection.cursor() as cursor:
                # The repaired statement is accepted by the driver and reaches
                # the server, which then enforces the real foreign keys.
                with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                    cursor.execute(PostgresRepository._RESULTS_INSERT, params)
            connection.rollback()

    def test_migrations_register_five_and_constraints_hold(self):
        versions = {row[0] for row in self.sql("SELECT version FROM xb_member_gateway.schema_migrations")}
        self.assertEqual(versions, {"0001_member_gateway", "0002_result_event_history", "0003_writer_termination_quarantine", "0004_forms_ingest_cursor", "0005_member_vertical_slice"})
        self.seed_cursor()
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "source_production_cutover_immutable"):
            self.sql("UPDATE xb_member_gateway.source_ingest_cursors SET production_cutover_exact='2026-09-16T00:00:00Z'")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.sql("INSERT INTO xb_member_gateway.source_rejections(response_id,rejection_id,source_response_ref,form_alias,form_id,mapping_version,create_time_exact,payload_hash,error_code,request_id) VALUES('r','rejection-" + "0" * 32 + "','hmac-v1:" + "0" * 64 + "','member_registration','f','member-intake.v1','2026-09-15T00:00:00.1Z','" + HASH + "','phone_shape_invalid','q')")

    def test_real_admission_receipt_replay_conflict_and_first_member_guard(self):
        self.seed_cursor()
        first = self.repository.ingest_source_event(source_event("forms-real-a"), initial_window_max=1)
        replay = self.repository.ingest_source_event(source_event("forms-real-a"), initial_window_max=1)
        self.assertEqual((first.replayed, replay.replayed, replay.job.job_id), (False, True, first.job.job_id))
        exact = self.sql("SELECT create_time_exact FROM xb_member_gateway.source_responses WHERE response_id='forms-real-a'")
        self.assertEqual(exact, [("2026-09-15T00:00:01.123456789Z",)])
        with self.assertRaisesRegex(SourceConflict, "source_identity_payload_conflict"):
            self.repository.ingest_source_event(source_event("forms-real-a", "2026-09-15T00:00:01.123Z"))
        with self.assertRaisesRegex(SourceConflict, "initial_source_window_exhausted"):
            self.repository.ingest_source_event(source_event("forms-real-b", "2026-09-15T00:00:02Z"), initial_window_max=1)
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.source_handling_receipts"), [(1,)])
        self.assertEqual(self.sql("SELECT initial_window_admission_count FROM xb_member_gateway.source_ingest_cursors"), [(1,)])
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "source_handling_receipts_append_only"):
            self.sql("DELETE FROM xb_member_gateway.source_handling_receipts")

    def test_real_epoch_open_receipt_commit_restart_and_mode_switch(self):
        self.seed_cursor()
        epoch, resumed = self.repository.begin_source_epoch("member_registration", "member-intake.v1", admission_mode=SourceAdmissionMode.FIRST_MEMBER, form_id=FORM)
        self.assertFalse(resumed)
        event = source_event("forms-real-page")
        item = {"response_id": event.response_id, "create_time": event.create_time, "payload_hash": event.payload_hash}
        page, epoch, _ = self.repository.open_source_page(epoch.epoch_id, expected_epoch_state_version=0, request_page_token=None, next_page_token="tok-1", terminal=False, items=[item])
        with self.assertRaisesRegex(SourceConflict, "source_page_item_unreceipted"):
            self.repository.commit_source_page(page.page_id, expected_epoch_state_version=epoch.epoch_state_version)
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.source_scan_page_items"), [(0,)])
        self.repository.ingest_source_event(event, initial_window_max=1)
        committed, epoch, replayed = self.repository.commit_source_page(page.page_id, expected_epoch_state_version=epoch.epoch_state_version)
        self.assertFalse(replayed)
        again = self.repository.commit_source_page(page.page_id, expected_epoch_state_version=page.opened_state_version)
        self.assertTrue(again[2])
        self.assertEqual(epoch.current_page_token, "tok-1")
        with self.assertRaisesRegex(SourceConflict, "source_page_token_repeated_in_epoch"):
            self.repository.open_source_page(epoch.epoch_id, expected_epoch_state_version=epoch.epoch_state_version, request_page_token="tok-1", next_page_token="tok-1", terminal=False, items=[])
        successor = self.repository.restart_source_epoch(epoch.epoch_id, expected_epoch_state_version=epoch.epoch_state_version, restart_reason="token_invalidated")
        self.assertEqual((successor.predecessor_epoch_id, successor.filter_exact), (epoch.epoch_id, f"timestamp >= {CUTOVER}"))
        page2, successor, _ = self.repository.open_source_page(successor.epoch_id, expected_epoch_state_version=0, request_page_token=None, next_page_token="tok-1", terminal=False, items=[item])
        self.repository.commit_source_page(page2.page_id, expected_epoch_state_version=successor.epoch_state_version)
        switched, resumed = self.repository.begin_source_epoch("member_registration", "member-intake.v1", admission_mode=SourceAdmissionMode.CONTINUOUS, form_id=FORM)
        self.assertFalse(resumed)
        states = dict(self.sql("SELECT epoch_id,abandon_reason FROM xb_member_gateway.source_scan_epochs WHERE status='ABANDONED'"))
        self.assertEqual(states[epoch.epoch_id], "token_invalidated")
        self.assertEqual(states[successor.epoch_id], "admission_mode_changed")
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.source_scan_epochs WHERE status='ACTIVE'"), [(1,)])
        empty, switched, _ = self.repository.open_source_page(switched.epoch_id, expected_epoch_state_version=0, request_page_token=None, next_page_token=None, terminal=True, items=[])
        _, completed, _ = self.repository.commit_source_page(empty.page_id, expected_epoch_state_version=switched.epoch_state_version)
        self.assertEqual(completed.status.value, "COMPLETED")
        self.assertEqual(self.sql("SELECT production_cutover_exact FROM xb_member_gateway.source_ingest_cursors"), [(CUTOVER,)])
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "source_scan_epoch_terminal_immutable"):
            self.sql("UPDATE xb_member_gateway.source_scan_epochs SET epoch_state_version=epoch_state_version+1 WHERE epoch_id=%s", (completed.epoch_id,))

    def test_real_rejection_is_pii_free_and_does_not_consume_allowance(self):
        from xb_member_gateway.canonical import canonicalize_source_rejection
        self.seed_cursor()
        rejection = canonicalize_source_rejection({
            "schema_version": "xb.member.source_rejection.v1", "source_system": "google_forms", "form_alias": "member_registration",
            "response_id": "forms-real-reject", "create_time": "2026-09-15T00:00:03.500Z", "mapping_version": "member-intake.v1",
            "request_id": "reject-real", "payload_hash": HASH, "error_code": "email_invalid", "operation": "member.create",
        })
        outcome = self.repository.reject_source_response(rejection)
        self.assertTrue(self.repository.reject_source_response(rejection).replayed)
        self.assertFalse(outcome.replayed)
        self.assertEqual(self.sql("SELECT initial_window_admission_count FROM xb_member_gateway.source_ingest_cursors"), [(0,)])
        self.assertEqual(self.sql("SELECT outcome,job_id FROM xb_member_gateway.source_handling_receipts"), [("REJECTED", None)])

    def test_real_readiness_fails_closed_for_backfill_and_migration_gaps(self):
        from xb_member_gateway.config import GatewayConfig
        config = GatewayConfig(source_cutover_watermark=CUTOVER, source_production_cutover_exact=CUTOVER, source_form_id=FORM)
        self.seed_cursor()
        self.repository.verify_bootstrap_readiness(config)
        with self.assertRaisesRegex(RepositoryError, "source_production_cutover_mismatch"):
            self.repository.verify_bootstrap_readiness(GatewayConfig(source_cutover_watermark=CUTOVER, source_production_cutover_exact="2026-09-15T00:00:00.000Z", source_form_id=FORM))
        self.sql("INSERT INTO xb_member_gateway.source_responses(response_id,source_response_ref,create_time,mapping_version,payload_hash,canonical_payload) VALUES('legacy','hmac-v1:" + "0" * 64 + "',%s,'member-intake.v1',%s,'{}'::jsonb)", (CUTOVER_DT, HASH))
        with self.assertRaisesRegex(RepositoryError, "source_exact_time_backfill_missing"):
            self.repository.verify_bootstrap_readiness(config)
        self.sql("DELETE FROM xb_member_gateway.schema_migrations WHERE version='0005_member_vertical_slice'")
        with self.assertRaisesRegex(RepositoryError, "required_migrations_missing"):
            self.repository.verify_bootstrap_readiness(config)
        self.assertFalse(self.repository.operator_status()["migrations_ready"])


if __name__ == "__main__":
    unittest.main()
