"""U-ING: ingest is byte-identical to main@488884d, and the v2 VALIDATED gate
computes the immutable XB-MN-1 identity once (W-G2-149 sections 2.1-2.3, 3)."""

import unittest

from xb_member_gateway.canonical import build_source_event, canonical_json, canonicalize_source_event
from xb_member_gateway.models import JobState, SourceAdmissionMode, source_cursor_v2
from xb_member_gateway.repository import InMemoryRepository, RepositoryError, SourceConflict

try:
    from .v2_support import CUTOVER, FORM, NOW, SESSION, load_vectors, make_app, make_event, make_repository, make_service, open_gate, policy, source_event
except ImportError:  # discovered as a top-level module
    from v2_support import CUTOVER, FORM, NOW, SESSION, load_vectors, make_app, make_event, make_repository, make_service, open_gate, policy, source_event


CORPUS = load_vectors()["ingest_corpus"]


class IngestByteIdentityTests(unittest.TestCase):
    """Golden canonical JSON and payload_hash values were generated with the
    main@488884d canonicaliser; the v2 gateway must reproduce them exactly."""

    def test_canonical_payload_and_hash_match_the_main_corpus(self):
        self.assertGreaterEqual(len(CORPUS), 7)
        for entry in CORPUS:
            with self.subTest(entry["id"]):
                event = build_source_event(
                    response_id=entry["id"], create_time="2026-09-30T01:00:00.123456789Z", request_id="req-" + entry["id"],
                    form_alias="member_registration", mapping_version="member-intake.v1", payload=entry["payload"],
                )
                self.assertEqual(canonical_json(event["payload"]), entry["canonical_json"])
                self.assertEqual(event["payload_hash"], entry["payload_hash"])
                self.assertEqual(canonicalize_source_event(event).payload_hash, entry["payload_hash"])

    def test_repository_stores_the_same_canonical_payload_and_receipt_fingerprint(self):
        repository = InMemoryRepository(source_cutover_watermark="2026-09-30T00:00:00Z", source_form_id=FORM)
        for entry in CORPUS:
            with self.subTest(entry["id"]):
                event = canonicalize_source_event(build_source_event(
                    response_id=entry["id"], create_time="2026-09-30T01:00:00.123456789Z", request_id="req-" + entry["id"],
                    form_alias="member_registration", mapping_version="member-intake.v1", payload=entry["payload"],
                ))
                outcome = repository.ingest_source_event(event, policy=policy(), now=NOW, initial_window_max=None)
                stored = dict(outcome.job.member_payload)
                self.assertEqual(stored.pop("create_time"), "2026-09-30T01:00:00.123456Z")
                self.assertEqual(canonical_json(stored), entry["canonical_json"])
                receipt = repository.handling_receipt(entry["id"])
                self.assertEqual((receipt.payload_fingerprint, receipt.create_time_exact), (entry["payload_hash"], "2026-09-30T01:00:00.123456789Z"))

    def test_first_member_allowance_counts_accepted_members_only(self):
        """The 0 -> 1 first_member guard counts accepted members; a
        REJECTED_VALIDATION job is receipted but never consumes it."""

        repository = make_repository()

        def cursor_state():
            cursor, _, _ = repository.get_source_cursor("member_registration", "member-intake.v1")
            view = source_cursor_v2(cursor, admission_mode=SourceAdmissionMode.FIRST_MEMBER, epoch=None, open_page=None)
            return cursor.initial_window_admission_count, view["accepted_member_allowance_remaining"]

        long_name = "N" * 101
        # 1-3. A validation rejection is handled/receipted, does not count,
        # and leaves the allowance available.
        rejected = repository.ingest_source_event(source_event("fm-rejected", pdpa_acknowledged=False, phone="81230001"), policy=policy(), now=NOW, initial_window_max=1)
        self.assertEqual((rejected.job.state, rejected.job.outcome_reason), (JobState.REJECTED_VALIDATION, "pdpa_not_acknowledged"))
        self.assertIsNotNone(repository.handling_receipt("fm-rejected"))
        self.assertEqual(cursor_state(), (0, 1))
        # 4. A later valid member consumes the 0 -> 1 allowance.
        accepted = repository.ingest_source_event(source_event("fm-accepted", phone="81230002"), policy=policy(), now=NOW, initial_window_max=1)
        self.assertEqual(accepted.job.state, JobState.QUEUED)
        self.assertEqual(cursor_state(), (1, 0))
        # 5. A second valid member loses atomically: no receipt, no job.
        with self.assertRaisesRegex(SourceConflict, "initial_source_window_exhausted"):
            repository.ingest_source_event(source_event("fm-loser", phone="81230003"), policy=policy(), now=NOW, initial_window_max=1)
        self.assertIsNone(repository.handling_receipt("fm-loser"))
        self.assertEqual(len(repository._jobs), 2)
        # 6. A validation rejection is still recordable after consumption.
        late = repository.ingest_source_event(source_event("fm-late-rejected", name=long_name, phone="81230004"), policy=policy(), now=NOW, initial_window_max=1)
        self.assertEqual((late.job.state, late.job.outcome_reason), (JobState.REJECTED_VALIDATION, "name_exceeds_autocount_limit"))
        self.assertIsNotNone(repository.handling_receipt("fm-late-rejected"))
        self.assertEqual(cursor_state(), (1, 0))
        # 7. Replay of accepted and rejected responses is idempotent.
        for response_id, changes, job in (
            ("fm-accepted", {"phone": "81230002"}, accepted.job),
            ("fm-rejected", {"pdpa_acknowledged": False, "phone": "81230001"}, rejected.job),
            ("fm-late-rejected", {"name": long_name, "phone": "81230004"}, late.job),
        ):
            replay = repository.ingest_source_event(source_event(response_id, **changes), policy=policy(), now=NOW, initial_window_max=1)
            self.assertEqual((replay.replayed, replay.job.job_id), (True, job.job_id), response_id)
        self.assertEqual(cursor_state(), (1, 0))
        # 8. Source identity / fingerprint conflicts stay fail-closed.
        with self.assertRaisesRegex(SourceConflict, "^(request|source)_identity_payload_conflict$"):
            repository.ingest_source_event(source_event("fm-rejected", pdpa_acknowledged=False, phone="81239999"), policy=policy(), now=NOW, initial_window_max=1)

    def test_first_member_guard_and_cursor_are_unchanged(self):
        repository = make_repository()
        first = repository.ingest_source_event(source_event("first-a"), policy=policy(), now=NOW, initial_window_max=1)
        with self.assertRaisesRegex(SourceConflict, "initial_source_window_exhausted"):
            repository.ingest_source_event(source_event("first-b", phone="81234568"), policy=policy(), now=NOW, initial_window_max=1)
        cursor, _, _ = repository.get_source_cursor("member_registration", "member-intake.v1")
        self.assertEqual((cursor.initial_window_admission_count, cursor.state_version, cursor.production_cutover_exact), (1, 1, CUTOVER))
        self.assertIsNone(repository.handling_receipt("first-b"))
        replay = repository.ingest_source_event(source_event("first-a"), policy=policy(), now=NOW, initial_window_max=1)
        self.assertEqual((replay.replayed, replay.job.job_id), (True, first.job.job_id))

    def test_policy_is_required_and_fails_before_any_write(self):
        repository = make_repository()
        with self.assertRaisesRegex(RepositoryError, "admission_policy_required"):
            repository.ingest_source_event(source_event(), policy=None, now=NOW)
        self.assertIsNone(repository.handling_receipt("v2-response-001"))


class ValidatedGateTests(unittest.TestCase):
    def setUp(self):
        self.repository = make_repository()

    def ingest(self, response_id, *, mode="production", **changes):
        return self.repository.ingest_source_event(source_event(response_id, **changes), policy=policy(mode), now=NOW, initial_window_max=None).job

    def test_valid_job_reaches_queued_with_immutable_identity(self):
        job = self.ingest("gate-ok", name="Tan Ah Kow", phone="+65 9123 4567")
        self.assertEqual(job.state, JobState.QUEUED)
        self.assertEqual((job.member_no_rule, job.base_member_no, job.name_component), ("XB-MN-1", "6591234567", "TANAHKOW"))
        self.assertEqual(job.state_version, 2)
        self.assertIsNone(job.outcome_reason)

    def test_identity_rejections_become_rejected_validation_with_reason(self):
        cases = {
            "gate-name": ({"name": "a" * 101}, "name_exceeds_autocount_limit"),
            "gate-email": ({"email": "a" * 188 + "@example.test"}, "email_exceeds_autocount_limit"),
            "gate-synthetic-name": ({"name": "ZZTEST Someone"}, "synthetic_in_production"),
            "gate-synthetic-email": ({"email": "someone@example.invalid"}, "synthetic_in_production"),
        }
        for response_id, (changes, reason) in cases.items():
            with self.subTest(response_id):
                job = self.ingest(response_id, **changes)
                self.assertEqual((job.state, job.outcome_reason), (JobState.REJECTED_VALIDATION, reason))
                self.assertIsNone(job.base_member_no)
                self.assertEqual(self.repository.handling_receipt(response_id).outcome.value, "ACCEPTED")

    def test_test_book_mode_requires_fully_synthetic_requests(self):
        rejected = self.ingest("gate-test-real", mode="test")
        self.assertEqual((rejected.state, rejected.outcome_reason), (JobState.REJECTED_VALIDATION, "test_book_requires_synthetic"))
        accepted = self.ingest("gate-test-synth", mode="test", name="ZZTEST Alice", email="alice@example.invalid", phone="00012345678")
        self.assertEqual((accepted.state, accepted.base_member_no), (JobState.QUEUED, "00012345678"))

    def test_pdpa_false_never_reaches_queued_and_is_never_claimed(self):
        job = self.ingest("gate-pdpa", pdpa_acknowledged=False)
        self.assertEqual((job.state, job.outcome_reason), (JobState.REJECTED_VALIDATION, "pdpa_not_acknowledged"))
        outcome = self.repository.claim_job(SESSION, gate=open_gate(), now=NOW)
        self.assertEqual((outcome.claimed, outcome.reason), (False, "no_eligible_job"))

    def test_replay_never_recomputes_the_identity(self):
        first = self.ingest("gate-immutable", name="Tan Ah Kow")
        replay = self.repository.ingest_source_event(source_event("gate-immutable", name="Tan Ah Kow"), policy=policy("test"), now=NOW, initial_window_max=None)
        self.assertTrue(replay.replayed)
        self.assertEqual(
            (replay.job.state, replay.job.base_member_no, replay.job.name_component, replay.job.state_version),
            (first.state, first.base_member_no, first.name_component, first.state_version),
        )

    def test_service_response_shape_is_unchanged_and_carries_no_identity(self):
        service = make_service(self.repository)
        response = make_app(service).handle("POST", "/v1/source-events", headers={"Authorization": "Bearer source"}, body=make_event("gate-api"))
        self.assertEqual(response.status, 202)
        self.assertEqual(set(response.body), {"schema_version", "job_id", "operation", "state", "replayed", "source_response_ref", "payload_hash"})
        self.assertEqual(response.body["state"], "QUEUED")
        rejected = make_app(service).handle("POST", "/v1/source-events", headers={"Authorization": "Bearer source"}, body=make_event("gate-api-pdpa", pdpa_acknowledged=False, phone="81234569"))
        self.assertEqual((rejected.status, rejected.body["state"]), (202, "REJECTED_VALIDATION"))

    def test_missing_book_mode_fails_closed_at_the_service_before_any_write(self):
        from xb_member_gateway.api import GatewayService
        from xb_member_gateway.config import GatewayConfig
        config = GatewayConfig(source_cutover_watermark=CUTOVER, source_production_cutover_exact=CUTOVER, source_form_id=FORM, source_admission_mode="continuous")
        service = GatewayService(config, self.repository, adapter_ready=True, clock=NOW)
        with self.assertRaisesRegex(Exception, "member_book_mode_invalid"):
            service.ingest(make_event("gate-no-mode"))
        self.assertIsNone(self.repository.handling_receipt("gate-no-mode"))


if __name__ == "__main__":
    unittest.main()
