import copy
import unittest
import uuid
from datetime import datetime, timezone

from xb_member_gateway.models import JobState, ResultRecord, ResultStatus
from xb_member_gateway.notifications import build_welcome_message, welcome_message_hash
from xb_member_gateway.repository import PostgresRepository, ResultConflict, public_fence_id

try:
    from .test_postgres_cursor import assert_driver_accepts
except ImportError:  # discovered as a top-level module
    from test_postgres_cursor import assert_driver_accepts


NOW = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)


class UniqueProjectionViolation(RuntimeError):
    pass


class ProjectionStore:
    """Small stateful PostgreSQL-shaped fake that enforces results.job_id uniqueness."""

    def __init__(self):
        self.job_id = "job-projection-001"
        self.member_no = "6590000001"
        self.fence_id = str(uuid.uuid4())
        self.public_fence = public_fence_id(self.fence_id)
        self.old_hash = "sha256:" + "a" * 64
        self.new_hash = "sha256:" + "b" * 64
        self.case_id = str(uuid.uuid4())
        self.recipient = "projection@example.test"
        self.job_row = (
            self.job_id, "request-001", "hmac-v1:" + "0" * 64, "response-001",
            self.old_hash, "member.create", {"email": self.recipient}, NOW, JobState.WRITE_OUTCOME_UNCERTAIN.value,
            12, 1, 3, None, None, None, self.member_no, "allocation-ref-001",
            "intent-001", self.fence_id, 1, ResultStatus.WRITE_OUTCOME_UNCERTAIN.value,
            "save_outcome_uncertain", "google_forms", "member_registration",
            "member-intake.v1", NOW,
        )
        self.result_projection = {
            "result_hash": self.old_hash,
            "status": ResultStatus.WRITE_OUTCOME_UNCERTAIN.value,
            "member_no": self.member_no,
            "dispatch_fence_id": self.fence_id,
            "save_invocation_count": 1,
            "readback_found": False,
            "readback_match": False,
            "reconciliation_required": True,
            "error_code": "save_outcome_uncertain",
        }
        self.result_events = [copy.deepcopy(self.result_projection)]
        self.hold = (
            str(uuid.uuid4()), self.job_id, self.fence_id, 1, "worker-a", "host-a",
            "exec-projection-001", self.member_no, "CLEARED", 3, 4321, NOW,
            "process_exit", "evidence-projection", NOW, NOW, NOW, NOW, None, NOW,
        )
        self.statements = []
        self.force_stale_cas = False
        self.case_state = "ABSENT"
        self.check = ("absent", False, False)
        self.outbox = []
        self.outbox_events = []

    def snapshot(self):
        return copy.deepcopy((self.job_row, self.result_projection, self.result_events, self.hold, self.outbox, self.outbox_events))

    def restore(self, snapshot):
        self.job_row, self.result_projection, self.result_events, self.hold, self.outbox, self.outbox_events = copy.deepcopy(snapshot)

    def outbox_row(self):
        values = self.outbox[0]
        return (values[0], values[1], values[2], values[3], values[4], values[5], values[6], "PENDING", 0, 0, values[7], None, None, values[8], None, None, values[9], values[10])


class ProjectionConnection:
    def __init__(self, store):
        self.store = store
        self.snapshot = store.snapshot()

    def cursor(self):
        return ProjectionCursor(self.store)

    def commit(self):
        self.snapshot = self.store.snapshot()

    def rollback(self):
        self.store.restore(self.snapshot)

    def close(self):
        return None


class ProjectionCursor:
    def __init__(self, store):
        self.store = store
        self.rows = []
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, params=None):
        # Every statement passes the same arity/adaptation boundary psycopg
        # applies, so a placeholder mismatch can no longer disappear here.
        assert_driver_accepts(statement, tuple(params or ()))
        normalized = " ".join(statement.split()).upper()
        self.store.statements.append(statement)
        self.rows = []
        self.rowcount = -1

        if "SELECT STATE_VERSION FROM XB_MEMBER_GATEWAY.WRITER_TERMINATION_GATE" in normalized:
            self.rows = [(0,)]
            return
        if "FROM XB_MEMBER_GATEWAY.JOBS J" in normalized:
            self.rows = [self.store.job_row]
            return
        if "SELECT FENCE_ID,MEMBER_NO FROM XB_MEMBER_GATEWAY.DISPATCH_FENCES" in normalized:
            self.rows = [(self.store.fence_id, self.store.member_no)]
            return
        if "SELECT HOLD_ID,JOB_ID,FENCE_ID,ATTEMPT_COUNT,WORKER_SESSION,HOST_BINDING,EXECUTION_ID" in normalized:
            self.rows = [self.store.hold]
            return
        if "SELECT RESULT_HASH,STATUS FROM XB_MEMBER_GATEWAY.RESULTS" in normalized:
            projection = self.store.result_projection
            self.rows = [(projection["result_hash"], projection["status"])] if projection else []
            return
        if "SELECT JOB_ID,MEMBER_NO,CASE_STATE FROM XB_MEMBER_GATEWAY.RECONCILIATION_CASES" in normalized:
            self.rows = [(self.store.job_id, self.store.member_no, self.store.case_state)]
            return
        if "SELECT LOOKUP_STATUS,READBACK_FOUND,READBACK_MATCH FROM XB_MEMBER_GATEWAY.RECONCILIATION_CHECKS" in normalized:
            self.rows = [self.store.check]
            return
        if normalized.startswith("INSERT INTO XB_MEMBER_GATEWAY.WELCOME_EMAIL_OUTBOX"):
            if self.store.result_projection is None or self.store.result_projection["status"] != ResultStatus.CREATED_VERIFIED.value:
                raise RuntimeError("welcome_email_requires_created_verified")
            if self.store.outbox:
                raise UniqueProjectionViolation("welcome_email_outbox_job_id_unique")
            self.store.outbox.append(tuple(params))
            self.rowcount = 1
            return
        if normalized.startswith("INSERT INTO XB_MEMBER_GATEWAY.WELCOME_EMAIL_EVENTS"):
            self.store.outbox_events.append(tuple(params))
            self.rowcount = 1
            return
        if "FROM XB_MEMBER_GATEWAY.WELCOME_EMAIL_OUTBOX WHERE JOB_ID" in normalized:
            self.rows = [self.store.outbox_row()] if self.store.outbox else []
            return
        if normalized.startswith("INSERT INTO XB_MEMBER_GATEWAY.RESULT_EVENTS"):
            values = tuple(params)
            self.store.result_events.append({
                "result_hash": values[1],
                "status": values[2],
                "member_no": values[3],
                "dispatch_fence_id": values[4],
                "save_invocation_count": values[5],
                "readback_found": values[6],
                "readback_match": values[7],
                "reconciliation_required": values[8],
                "error_code": values[9],
            })
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE XB_MEMBER_GATEWAY.RESULTS SET") and "RETURNING RESULT_ID" in normalized:
            values = tuple(params)
            matches = (
                not self.store.force_stale_cas
                and self.store.result_projection is not None
                and self.store.result_projection["result_hash"] == values[11]
                and self.store.result_projection["status"] == values[12]
                and values[10] == self.store.job_id
            )
            if matches:
                self.store.result_projection = {
                    "result_hash": values[0],
                    "status": values[1],
                    "member_no": values[2],
                    "dispatch_fence_id": values[3],
                    "save_invocation_count": values[4],
                    "readback_found": values[5],
                    "readback_match": values[6],
                    "reconciliation_required": values[7],
                    "error_code": values[8],
                }
                self.rows = [(1,)]
                self.rowcount = 1
            return
        if normalized.startswith("INSERT INTO XB_MEMBER_GATEWAY.RESULTS"):
            if self.store.result_projection is not None:
                raise UniqueProjectionViolation("results_job_id_unique")
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE XB_MEMBER_GATEWAY.JOBS SET STATE=%S"):
            values = tuple(params)
            row = list(self.store.job_row)
            row[8] = values[0]
            row[9] = int(row[9]) + 1
            self.store.job_row = tuple(row)
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE XB_MEMBER_GATEWAY.WRITER_EXECUTION_HOLDS SET LIFECYCLE='CLEARED'"):
            row = list(self.store.hold)
            row[8] = "CLEARED"
            row[19] = NOW
            self.store.hold = tuple(row)
            self.rowcount = 1
            return
        self.rowcount = 1

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


def final_result(store, status=ResultStatus.CONFIRMED_NOT_CREATED):
    positive = status == ResultStatus.CREATED_VERIFIED
    return ResultRecord(
        job_id=store.job_id,
        result_hash=store.new_hash,
        status=status,
        member_no=store.member_no,
        dispatch_fence_id=store.public_fence,
        save_invocation_count=1,
        readback_found=positive,
        readback_match=positive,
        reconciliation_required=not positive,
        error_code=None if positive else "confirmed_absent_manual_followup",
        acknowledged_at="2026-09-01T01:00:00Z",
    )


def exact_match(store):
    store.case_state = "EXACT_MATCH"
    store.check = ("exact_match", True, True)


class PostgresProjectionTests(unittest.TestCase):
    def test_reconciliation_replaces_current_projection_without_unique_job_insert(self):
        store = ProjectionStore()
        repository = PostgresRepository(connection_factory=lambda: ProjectionConnection(store))

        stored, duplicate = repository.acknowledge_result(
            final_result(store),
            require_lease=False,
            reconciliation_case_id=store.case_id,
            now=NOW,
        )

        self.assertFalse(duplicate)
        self.assertEqual(stored.status, ResultStatus.CONFIRMED_NOT_CREATED)
        self.assertEqual(store.result_projection["result_hash"], store.new_hash)
        self.assertEqual(store.result_projection["status"], ResultStatus.CONFIRMED_NOT_CREATED.value)
        self.assertEqual(len(store.result_events), 2)
        self.assertEqual(store.result_events[0]["result_hash"], store.old_hash)
        self.assertEqual(store.result_events[1]["result_hash"], store.new_hash)
        self.assertEqual(sum(1 for statement in store.statements if "RESULTS SET" in statement.upper()), 1)
        self.assertFalse(any("DELETE FROM XB_MEMBER_GATEWAY.RESULTS" in statement.upper() for statement in store.statements))

    def test_stale_compare_and_set_fails_closed_and_rolls_back_event(self):
        store = ProjectionStore()
        store.force_stale_cas = True
        repository = PostgresRepository(connection_factory=lambda: ProjectionConnection(store))

        with self.assertRaises(ResultConflict) as caught:
            repository.acknowledge_result(
                final_result(store),
                require_lease=False,
                reconciliation_case_id=store.case_id,
                now=NOW,
            )

        self.assertEqual(str(caught.exception), "reconciliation_projection_stale")
        self.assertEqual(len(store.result_events), 1)
        self.assertEqual(store.result_projection["result_hash"], store.old_hash)
        self.assertEqual(store.result_projection["status"], ResultStatus.WRITE_OUTCOME_UNCERTAIN.value)
        self.assertEqual(store.outbox, [])

    def test_non_positive_reconciliation_creates_no_welcome_outbox(self):
        store = ProjectionStore()
        repository = PostgresRepository(connection_factory=lambda: ProjectionConnection(store))
        repository.acknowledge_result(final_result(store), require_lease=False, reconciliation_case_id=store.case_id, now=NOW)
        self.assertEqual(store.outbox, [])
        self.assertFalse(any("WELCOME_EMAIL" in statement.upper() for statement in store.statements))

    def test_exact_match_reconciliation_inserts_welcome_outbox_in_the_same_transaction(self):
        store = ProjectionStore()
        exact_match(store)
        repository = PostgresRepository(connection_factory=lambda: ProjectionConnection(store))
        stored, duplicate = repository.acknowledge_result(
            final_result(store, ResultStatus.CREATED_VERIFIED), require_lease=False,
            reconciliation_case_id=store.case_id, now=NOW,
        )
        self.assertEqual((stored.status, duplicate), (ResultStatus.CREATED_VERIFIED, False))
        self.assertEqual(len(store.outbox), 1)
        outbox = store.outbox[0]
        message = build_welcome_message(store.recipient)
        self.assertEqual((outbox[1], outbox[2], outbox[4], outbox[5], outbox[6]), (store.job_id, "response-001", "welcome_v1", store.recipient, welcome_message_hash(message)))
        self.assertEqual(len(store.outbox_events), 1)
        statements = [statement.upper() for statement in store.statements]
        projection = next(index for index, statement in enumerate(statements) if "RESULTS SET" in statement)
        insert = next(index for index, statement in enumerate(statements) if "INSERT INTO XB_MEMBER_GATEWAY.WELCOME_EMAIL_OUTBOX" in statement)
        self.assertLess(projection, insert)

        # Duplicate acknowledgement verifies the existing identity only.
        again, duplicate = repository.acknowledge_result(
            final_result(store, ResultStatus.CREATED_VERIFIED), require_lease=False,
            reconciliation_case_id=store.case_id, now=NOW,
        )
        self.assertTrue(duplicate)
        self.assertEqual(len(store.outbox), 1)

    def test_duplicate_positive_ack_without_outbox_fails_closed_and_never_backfills(self):
        store = ProjectionStore()
        exact_match(store)
        store.result_projection = dict(store.result_projection, result_hash=store.new_hash, status=ResultStatus.CREATED_VERIFIED.value)
        repository = PostgresRepository(connection_factory=lambda: ProjectionConnection(store))
        with self.assertRaisesRegex(ResultConflict, "welcome_outbox_identity_missing"):
            repository.acknowledge_result(final_result(store, ResultStatus.CREATED_VERIFIED), require_lease=False, reconciliation_case_id=store.case_id, now=NOW)
        self.assertEqual(store.outbox, [])

    def test_outbox_insert_failure_rolls_back_the_positive_projection(self):
        store = ProjectionStore()
        exact_match(store)
        store.outbox.append(("welcome-" + "f" * 32, "job-other", "response-other", "ref", "welcome_v1", "x@example.test", "sha256:" + "0" * 64, 3, NOW, NOW, NOW))
        repository = PostgresRepository(connection_factory=lambda: ProjectionConnection(store))
        with self.assertRaises(UniqueProjectionViolation):
            repository.acknowledge_result(final_result(store, ResultStatus.CREATED_VERIFIED), require_lease=False, reconciliation_case_id=store.case_id, now=NOW)
        self.assertEqual(store.result_projection["status"], ResultStatus.WRITE_OUTCOME_UNCERTAIN.value)
        self.assertEqual(len(store.result_events), 1)


if __name__ == "__main__":
    unittest.main()
