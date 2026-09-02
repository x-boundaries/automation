import copy
import unittest
from datetime import datetime, timedelta, timezone

from xb_member_gateway.models import JobState, WriterHoldState
from xb_member_gateway.repository import PostgresRepository, WriterTerminationConflict, public_fence_id


NOW = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
PROCESS_START = "2026-09-01T01:00:01Z"


class QuarantineStore:
    """Stateful PostgreSQL-shaped hold fixture for transaction and SQL assertions."""

    def __init__(self, lifecycle=WriterHoldState.REGISTERED.value):
        self.job_id = "job-postgres-quarantine-001"
        self.member_no = "6590000001"
        self.fence_id = "00000000-0000-0000-0000-000000000001"
        self.public_fence = public_fence_id(self.fence_id)
        self.execution_id = "exec-postgres-quarantine-001"
        self.job_row = (
            self.job_id, "request-001", "source-ref-001", "response-001",
            "sha256:" + "a" * 64, "member.create", {}, NOW,
            JobState.WRITING.value, 4, 1, 3, None, "worker-a",
            NOW + timedelta(seconds=600), self.member_no, "probe-001", "intent-001",
            self.fence_id, 0, None, None, "google_forms", "member_registration",
            "member-intake.v1", NOW, lifecycle,
        )
        self.hold = (
            "hold-postgres-quarantine-001", self.job_id, self.fence_id, 1,
            "worker-a", "host-a", self.execution_id, self.member_no, lifecycle, 3,
            4321 if lifecycle != WriterHoldState.PENDING.value else None,
            PROCESS_START if lifecycle != WriterHoldState.PENDING.value else None,
            None, None, NOW, NOW,
            NOW if lifecycle != WriterHoldState.PENDING.value else None,
            None, None, None,
        )
        self.statements = []

    def snapshot(self):
        return copy.deepcopy((self.job_row, self.hold))

    def restore(self, snapshot):
        self.job_row, self.hold = copy.deepcopy(snapshot)


class QuarantineConnection:
    def __init__(self, store):
        self.store = store
        self.snapshot = store.snapshot()

    def cursor(self):
        return QuarantineCursor(self.store)

    def commit(self):
        self.snapshot = self.store.snapshot()

    def rollback(self):
        self.store.restore(self.snapshot)

    def close(self):
        return None


class QuarantineCursor:
    def __init__(self, store):
        self.store = store
        self.rows = []
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, params=None):
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
        if normalized.startswith("SELECT HOLD_ID,JOB_ID,FENCE_ID,ATTEMPT_COUNT,WORKER_SESSION,HOST_BINDING,EXECUTION_ID"):
            self.rows = [self.store.hold]
            return
        if normalized.startswith("UPDATE XB_MEMBER_GATEWAY.WRITER_EXECUTION_HOLDS SET LIFECYCLE='QUARANTINED'"):
            values = tuple(params)
            row = list(self.store.hold)
            row[8] = WriterHoldState.QUARANTINED.value
            row[9] = int(row[9]) + 1
            row[12] = "quarantine"
            if "PROCESS_PID=COALESCE" in normalized:
                row[10] = values[0] if values[0] is not None else row[10]
                row[11] = values[1] if values[1] is not None else row[11]
                row[13], row[18], row[15] = values[2], values[3], values[4]
            else:
                row[13], row[18], row[15] = values[0], values[1], values[2]
            self.store.hold = tuple(row)
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE XB_MEMBER_GATEWAY.JOBS SET STATE=%S"):
            values = tuple(params)
            row = list(self.store.job_row)
            row[8] = values[0]
            row[9] = int(row[9]) + 1
            row[21] = values[2]
            self.store.job_row = tuple(row)
            self.rowcount = 1
            return
        self.rowcount = 1

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class PostgresWriterQuarantineTests(unittest.TestCase):
    def repository(self, store):
        return PostgresRepository(connection_factory=lambda: QuarantineConnection(store))

    def quarantine(self, repository, *, pid=None, process_start_time=None, evidence_reference="quarantine-test"):
        return repository.quarantine_writer_execution(
            "job-postgres-quarantine-001", fence_id="fence-00000000000000000000000000000001",
            attempt=1, worker_session="worker-a", host_binding="host-a",
            execution_id="exec-postgres-quarantine-001", pid=pid,
            process_start_time=process_start_time, evidence_reference=evidence_reference,
            now=NOW,
        )

    def test_registered_quarantine_rejects_wrong_pid(self):
        store = QuarantineStore()
        repository = self.repository(store)
        before = store.hold

        with self.assertRaisesRegex(WriterTerminationConflict, "writer_process_identity_mismatch"):
            self.quarantine(repository, pid=4322, process_start_time=PROCESS_START)

        self.assertEqual(store.hold, before)

    def test_registered_quarantine_rejects_same_pid_with_different_start(self):
        store = QuarantineStore()
        repository = self.repository(store)
        before = store.hold

        with self.assertRaisesRegex(WriterTerminationConflict, "writer_process_identity_mismatch"):
            self.quarantine(repository, pid=4321, process_start_time="2026-09-01T01:00:02Z")

        self.assertEqual(store.hold, before)

    def test_registered_quarantine_omitted_identity_preserves_registered_pair(self):
        store = QuarantineStore()
        hold = self.quarantine(self.repository(store))

        self.assertEqual(hold.state, WriterHoldState.QUARANTINED)
        self.assertEqual((hold.pid, hold.process_start_time), (4321, PROCESS_START))
        self.assertEqual((store.hold[10], store.hold[11]), (4321, PROCESS_START))

    def test_registered_quarantine_accepts_matching_pair_without_identity_update_sql(self):
        store = QuarantineStore()
        hold = self.quarantine(self.repository(store), pid=4321, process_start_time=PROCESS_START, evidence_reference="matching-test")

        self.assertEqual(hold.state, WriterHoldState.QUARANTINED)
        self.assertEqual((store.hold[10], store.hold[11]), (4321, PROCESS_START))
        updates = [statement.upper() for statement in store.statements if "SET LIFECYCLE='QUARANTINED'" in statement.upper()]
        self.assertEqual(len(updates), 1)
        self.assertNotIn("PROCESS_PID", updates[0])
        self.assertNotIn("PROCESS_START_AT", updates[0])

    def test_repeated_quarantine_rejects_mismatched_identity(self):
        store = QuarantineStore()
        repository = self.repository(store)
        self.quarantine(repository, pid=4321, process_start_time=PROCESS_START)
        before = store.hold

        with self.assertRaisesRegex(WriterTerminationConflict, "writer_process_identity_mismatch"):
            self.quarantine(repository, pid=4322, process_start_time=PROCESS_START, evidence_reference="repeat-wrong-pid")

        self.assertEqual(store.hold, before)

    def test_pending_quarantine_remains_unregistered(self):
        store = QuarantineStore(WriterHoldState.PENDING.value)
        hold = self.quarantine(self.repository(store))

        self.assertEqual(hold.state, WriterHoldState.QUARANTINED)
        self.assertIsNone(hold.pid)
        self.assertIsNone(hold.process_start_time)


if __name__ == "__main__":
    unittest.main()
