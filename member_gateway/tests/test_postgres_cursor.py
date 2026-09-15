import unittest
from datetime import datetime, timezone

from xb_member_gateway.repository import PostgresRepository, SourceConflict


HASH = "sha256:" + "a" * 64
WATERMARK = datetime(2026, 9, 15, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.query = ""
        self.params = ()
        self.rowcount = 1

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, query, params=()):
        self.query, self.params = " ".join(query.split()), params
        self.connection.queries.append((self.query, params))
    def fetchone(self):
        if "FROM xb_member_gateway.source_ingest_cursors" in self.query:
            return ("google_forms", "member_registration", "member-intake.v1", WATERMARK, WATERMARK, "forms-one", 1, WATERMARK, None, 1)
        if "FROM xb_member_gateway.source_ingest_page_receipts" in self.query:
            return None
        if "FROM xb_member_gateway.source_responses" in self.query:
            return None if self.connection.missing_response else (HASH, WATERMARK)
        return None
    def fetchall(self): return []


class Connection:
    def __init__(self, *, missing_response=False):
        self.missing_response = missing_response
        self.queries = []
        self.committed = False
        self.rolled_back = False
    def cursor(self): return Cursor(self)
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True
    def close(self): pass


class PostgresCursorTests(unittest.TestCase):
    def test_checkpoint_is_cas_and_appends_receipt_in_one_transaction(self):
        connection = Connection()
        repository = PostgresRepository(connection_factory=lambda: connection, reference_key=b"synthetic")
        result = repository.checkpoint_source_page(
            "member_registration", "member-intake.v1", expected_state_version=1,
            current_page_token=None, next_page_token="next-page", scan_lower_bound="2026-09-15T00:00:00Z",
            terminal=False, responses=[{"response_id": "forms-one", "create_time": "2026-09-15T00:00:00Z", "payload_hash": HASH}],
        )
        self.assertTrue(connection.committed)
        self.assertEqual(result.state_version, 2)
        sql = "\n".join(query for query, _ in connection.queries)
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("UPDATE xb_member_gateway.source_ingest_cursors", sql)
        self.assertIn("INSERT INTO xb_member_gateway.source_ingest_page_receipts", sql)

    def test_partial_page_failure_rolls_back_before_token_update(self):
        connection = Connection(missing_response=True)
        repository = PostgresRepository(connection_factory=lambda: connection, reference_key=b"synthetic")
        with self.assertRaisesRegex(SourceConflict, "source_page_response_not_admitted"):
            repository.checkpoint_source_page(
                "member_registration", "member-intake.v1", expected_state_version=1,
                current_page_token=None, next_page_token="next-page", scan_lower_bound="2026-09-15T00:00:00Z",
                terminal=False, responses=[{"response_id": "forms-one", "create_time": "2026-09-15T00:00:00Z", "payload_hash": HASH}],
            )
        self.assertTrue(connection.rolled_back)
        self.assertFalse(any(query.startswith("UPDATE xb_member_gateway.source_ingest_cursors") for query, _ in connection.queries))


if __name__ == "__main__":
    unittest.main()
