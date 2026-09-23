"""Fixed-cutover, receipt-driven source admission and the epoch/page protocol.

Covers the G1-124 control matrix through the gateway service and HTTP router
on the in-memory repository. PostgreSQL statement shape for the same paths is
covered in test_postgres_cursor.
"""

import json
import unittest

from xb_member_gateway.api import GatewayApp, GatewayService
from xb_member_gateway.auth import SOURCE_SCOPES, StaticAuthenticator, principal
from xb_member_gateway.canonical import build_source_event, canonical_json
from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.crypto import payload_hash
from xb_member_gateway.repository import InMemoryRepository, SourceConflict, SourceRestartReasonInvalid


CUTOVER = "2026-09-15T00:00:00Z"
FORM = "synthetic-form"
BINDING = {"form_alias": "member_registration", "mapping_version": "member-intake.v1"}
PAYLOAD = {"name": "Synthetic Member", "phone": "81234567", "email": "synthetic@example.test", "birthday_month": "January", "marketing_consent": "No", "pdpa_acknowledged": True}


def event(response_id="forms-a", create_time="2026-09-15T00:00:00.100Z", request_id=None, **payload):
    return build_source_event(
        response_id=response_id, create_time=create_time, request_id=request_id or f"request-{response_id}-{sum(map(ord, create_time + str(sorted(payload.items()))))}",
        form_alias="member_registration", mapping_version="member-intake.v1",
        payload={**PAYLOAD, **payload},
    )


def rejection(response_id="forms-bad", create_time="2026-09-15T00:00:01Z", error_code="phone_shape_invalid"):
    raw = {"name": "Synthetic", "phone": "not-a-phone", "email": "bad@example.test", "birthday_month": "May", "marketing_consent": "No", "pdpa_acknowledged": "I agree"}
    return {
        "schema_version": "xb.member.source_rejection.v1", "source_system": "google_forms",
        "form_alias": "member_registration", "response_id": response_id, "create_time": create_time,
        "mapping_version": "member-intake.v1", "request_id": f"reject-{response_id}",
        "payload_hash": payload_hash(canonical_json(raw)), "error_code": error_code, "operation": "member.create",
    }


def item(source):
    return {"response_id": source["response_id"], "create_time": source["create_time"], "payload_hash": source["payload_hash"]}


class SourceCursorTests(unittest.TestCase):
    def make(self, mode="first_member", repository=None):
        self.repository = repository or InMemoryRepository(source_cutover_watermark=CUTOVER, source_form_id=FORM)
        self.config = GatewayConfig(
            source_cutover_watermark=CUTOVER, source_production_cutover_exact=CUTOVER,
            source_admission_mode=mode, source_form_id=FORM,
        )
        self.service = GatewayService(self.config, self.repository, adapter_ready=True)
        self.app = GatewayApp(self.service, StaticAuthenticator({"source": principal("configured-source", SOURCE_SCOPES)}))
        return self.service

    def setUp(self):
        self.make()

    def begin(self):
        return self.service.begin_source_epoch(dict(BINDING))

    def run_page(self, items, *, next_token=None, handle=True):
        """One OPEN -> receipts -> COMMIT cycle; returns the commit envelope."""
        cursor = self.begin()
        epoch = cursor["active_epoch"]
        opened = self.service.open_source_page(epoch["epoch_id"], {
            "expected_epoch_state_version": epoch["epoch_state_version"],
            "request_page_token": epoch["current_page_token"], "next_page_token": next_token,
            "terminal": next_token is None, "items": [item(entry) for entry in items],
        })
        if handle:
            for entry in items:
                if entry.get("schema_version") == "xb.member.source_rejection.v1":
                    self.service.reject_source(entry)
                else:
                    self.service.ingest(entry)
        return self.service.commit_source_page(opened["page_id"], {"expected_epoch_state_version": opened["epoch_state_version"]})

    # --- exact createTime ---------------------------------------------------

    def test_zero_three_six_nine_fraction_forms_round_trip_byte_identically(self):
        self.make("continuous")
        for index, created in enumerate(("2026-09-15T00:00:05Z", "2026-09-15T00:00:05.120Z", "2026-09-15T00:00:05.123456Z", "2026-09-15T00:00:05.123456789Z")):
            with self.subTest(created=created):
                self.service.ingest(event(f"forms-width-{index}", created))
                self.assertEqual(self.repository.handling_receipt(f"forms-width-{index}").create_time_exact, created)
        job = self.repository.all_jobs()[-1]
        # The worker derivative is truncated to microseconds, never rounded.
        self.assertEqual(job.member_payload["create_time"], "2026-09-15T00:00:05.123456Z")

    def test_invalid_widths_offsets_and_naive_times_are_rejected_at_the_api(self):
        for created in ("2026-09-15T00:00:05.1Z", "2026-09-15T00:00:05.1234567Z", "2026-09-15T00:00:05+00:00", "2026-09-15T00:00:05", "2026-09-15T08:00:05+08:00"):
            with self.subTest(created=created):
                response = self.app.handle("POST", "/v1/source-events", headers={"Authorization": "Bearer source"}, body=json.dumps(event("forms-bad-time", created)))
                self.assertEqual(response.status, 400)
                self.assertIn(response.body["error_code"], {"create_time_invalid", "create_time_timezone_required"})
        self.assertIsNone(self.repository.handling_receipt("forms-bad-time"))

    def test_same_instant_different_exact_string_is_immutable_conflict(self):
        self.make("continuous")
        self.service.ingest(event("forms-same", "2026-09-15T00:00:07Z"))
        with self.assertRaisesRegex(SourceConflict, "source_identity_payload_conflict"):
            self.service.ingest(event("forms-same", "2026-09-15T00:00:07.000Z"))
        self.assertEqual(self.repository.handling_receipt("forms-same").create_time_exact, "2026-09-15T00:00:07Z")

    # --- fixed cutover / ordering independence -----------------------------

    def test_reverse_lexical_ids_and_fractions_are_both_admitted_once(self):
        self.make("continuous")
        later = self.service.ingest(event("forms-aaa", "2026-09-15T00:00:00.200Z"))
        earlier = self.service.ingest(event("forms-zzz", "2026-09-15T00:00:00.100Z"))
        self.assertFalse(later["replayed"] or earlier["replayed"])
        self.assertTrue(self.service.ingest(event("forms-zzz", "2026-09-15T00:00:00.100Z", request_id="replay-z"))["replayed"])
        self.assertEqual(len(self.repository.all_jobs()), 2)

    def test_cutover_is_inclusive_fixed_and_never_advances(self):
        self.make("continuous")
        with self.assertRaisesRegex(SourceConflict, "source_event_before_cutover"):
            self.service.ingest(event("forms-early", "2026-09-14T23:59:59.999999999Z"))
        self.assertIsNone(self.repository.handling_receipt("forms-early"))
        self.service.ingest(event("forms-equal", CUTOVER))
        self.run_page([event("forms-late", "2026-09-20T00:00:00Z")])
        self.run_page([])
        cursor = self.service.source_cursor("member_registration", "member-intake.v1")
        self.assertEqual(cursor["production_cutover_exact"], CUTOVER)
        self.assertEqual(cursor["filter_exact"], f"timestamp >= {CUTOVER}")
        for deprecated in ("last_admitted_create_time", "last_admitted_response_id", "resume_page_token", "watermark", "scan_lower_bound"):
            self.assertNotIn(deprecated, cursor)
        # Admission after completed epochs still only compares with the cutover.
        self.assertFalse(self.service.ingest(event("forms-back", "2026-09-15T00:00:00.001Z"))["replayed"])

    def test_arrival_during_pagination_is_recovered_by_a_later_fixed_bound_epoch(self):
        self.make("continuous")
        first = self.run_page([event("forms-one", "2026-09-16T00:00:00Z")])
        self.assertEqual(first["epoch_status"], "COMPLETED")
        # A response Google did not return in that traversal is found by the
        # next epoch, which repeats the same inclusive cutover filter.
        second = self.run_page([event("forms-one", "2026-09-16T00:00:00Z")], next_token="tok-1")
        cursor = self.service.source_cursor("member_registration", "member-intake.v1")
        self.assertNotEqual(cursor["active_epoch"]["epoch_id"], first["epoch_id"])
        self.assertEqual(cursor["active_epoch"]["predecessor_epoch_id"], first["epoch_id"])
        self.assertEqual(second["epoch_status"], "ACTIVE")
        final = self.run_page([event("forms-arrived", "2026-09-15T12:00:00Z")])
        self.assertEqual(final["epoch_status"], "COMPLETED")
        self.assertIsNotNone(self.repository.handling_receipt("forms-arrived"))

    # --- epoch / page protocol --------------------------------------------

    def test_empty_terminal_page_commits_and_completes_without_moving_cutover(self):
        committed = self.run_page([])
        self.assertEqual((committed["epoch_status"], committed["item_count"]), ("COMPLETED", 0))
        self.assertEqual(self.service.source_cursor(**BINDING)["production_cutover_exact"], CUTOVER)

    def test_page_commit_requires_a_matching_receipt_for_every_item(self):
        self.make("continuous")
        source = event("forms-unreceipted", "2026-09-15T00:00:03Z")
        cursor = self.begin()["active_epoch"]
        opened = self.service.open_source_page(cursor["epoch_id"], {"expected_epoch_state_version": 0, "request_page_token": None, "next_page_token": "tok-a", "terminal": False, "items": [item(source)]})
        with self.assertRaisesRegex(SourceConflict, "source_page_item_unreceipted"):
            self.service.commit_source_page(opened["page_id"], {"expected_epoch_state_version": opened["epoch_state_version"]})
        still = self.service.source_cursor(**BINDING)["active_epoch"]
        self.assertIsNone(still["current_page_token"])
        self.assertEqual(still["open_page"]["page_id"], opened["page_id"])
        # A receipt whose fingerprint differs from the opened item cannot satisfy it.
        self.service.ingest(event("forms-unreceipted", "2026-09-15T00:00:03Z", name="Other Name"))
        with self.assertRaisesRegex(SourceConflict, "source_page_item_receipt_mismatch"):
            self.service.commit_source_page(opened["page_id"], {"expected_epoch_state_version": opened["epoch_state_version"]})

    def test_open_is_idempotent_and_commit_replays_after_lost_acknowledgement(self):
        self.make("continuous")
        source = event("forms-crash-after-receipt", "2026-09-15T00:00:04Z")
        epoch = self.begin()["active_epoch"]
        body = {"expected_epoch_state_version": 0, "request_page_token": None, "next_page_token": "tok-b", "terminal": False, "items": [item(source)]}
        opened = self.service.open_source_page(epoch["epoch_id"], body)
        reopened = self.service.open_source_page(epoch["epoch_id"], body)
        self.assertEqual((reopened["page_id"], reopened["replayed"]), (opened["page_id"], True))
        with self.assertRaisesRegex(SourceConflict, "source_page_already_open"):
            self.service.open_source_page(epoch["epoch_id"], {**body, "next_page_token": "tok-other"})
        self.service.ingest(source)
        commit = {"expected_epoch_state_version": opened["epoch_state_version"]}
        first = self.service.commit_source_page(opened["page_id"], commit)
        again = self.service.commit_source_page(opened["page_id"], commit)
        self.assertEqual((again["replayed"], again["epoch_state_version"]), (True, first["epoch_state_version"]))
        self.assertEqual(len([row for row in self.repository.page_items() if row["page_id"] == opened["page_id"]]), 1)

    def test_cas_mismatch_unexpected_token_and_terminal_shape_fail_closed(self):
        epoch = self.begin()["active_epoch"]
        base = {"request_page_token": None, "next_page_token": None, "terminal": True, "items": []}
        with self.assertRaisesRegex(SourceConflict, "source_epoch_state_version_mismatch"):
            self.service.open_source_page(epoch["epoch_id"], {**base, "expected_epoch_state_version": 7})
        with self.assertRaisesRegex(SourceConflict, "source_page_token_unexpected"):
            self.service.open_source_page(epoch["epoch_id"], {**base, "expected_epoch_state_version": 0, "request_page_token": "stale"})
        with self.assertRaisesRegex(SourceConflict, "source_page_terminal_invalid"):
            self.service.open_source_page(epoch["epoch_id"], {**base, "expected_epoch_state_version": 0, "terminal": False})
        response = self.app.handle("POST", f"/v1/source/epochs/{epoch['epoch_id']}/pages/open", headers={"Authorization": "Bearer source"}, body=json.dumps({**base, "expected_epoch_state_version": 9}))
        self.assertEqual((response.status, response.body["error_code"]), (409, "source_epoch_state_version_mismatch"))

    def test_token_repetition_is_rejected_only_within_the_same_epoch(self):
        self.make("continuous")
        self.run_page([], next_token="tok-1", handle=True)
        epoch = self.service.source_cursor(**BINDING)["active_epoch"]
        with self.assertRaisesRegex(SourceConflict, "source_page_token_repeated_in_epoch"):
            self.service.open_source_page(epoch["epoch_id"], {"expected_epoch_state_version": epoch["epoch_state_version"], "request_page_token": "tok-1", "next_page_token": "tok-1", "terminal": False, "items": []})
        restarted = self.service.restart_source_epoch(epoch["epoch_id"], {"expected_epoch_state_version": epoch["epoch_state_version"], "restart_reason": "token_invalidated"})
        successor = restarted["active_epoch"]
        self.assertEqual((successor["restart_reason"], successor["predecessor_epoch_id"]), ("token_invalidated", epoch["epoch_id"]))
        # The same opaque token in a successor epoch is not a global conflict.
        opened = self.service.open_source_page(successor["epoch_id"], {"expected_epoch_state_version": 0, "request_page_token": None, "next_page_token": "tok-1", "terminal": False, "items": []})
        self.assertEqual(opened["page_state"], "OPEN")

    def test_restart_only_for_allowlisted_reasons_and_replays_prior_receipts(self):
        self.make("continuous")
        source = event("forms-restart", "2026-09-15T00:00:09Z")
        self.run_page([source], next_token="tok-r")
        epoch = self.service.source_cursor(**BINDING)["active_epoch"]
        for reason in ("oauth_failure", "mapping_drift", "schema_invalid", ""):
            with self.subTest(reason=reason):
                with self.assertRaises(SourceRestartReasonInvalid):
                    self.service.restart_source_epoch(epoch["epoch_id"], {"expected_epoch_state_version": epoch["epoch_state_version"], "restart_reason": reason})
        response = self.app.handle("POST", f"/v1/source/epochs/{epoch['epoch_id']}/restart", headers={"Authorization": "Bearer source"}, body=json.dumps({"expected_epoch_state_version": epoch["epoch_state_version"], "restart_reason": "oauth_failure"}))
        self.assertEqual((response.status, response.body["error_code"]), (422, "source_restart_reason_invalid"))
        restarted = self.service.restart_source_epoch(epoch["epoch_id"], {"expected_epoch_state_version": epoch["epoch_state_version"], "restart_reason": "ambiguous_crashed_attempt"})
        self.assertEqual(restarted["production_cutover_exact"], CUTOVER)
        self.assertIsNone(restarted["active_epoch"]["current_page_token"])
        replay = self.run_page([source])
        self.assertEqual(replay["epoch_status"], "COMPLETED")
        self.assertEqual(len(self.repository.all_jobs()), 1)

    def test_crash_before_receipt_leaves_token_and_restart_recovers(self):
        self.make("continuous")
        source = event("forms-crash-before", "2026-09-15T00:00:10Z")
        epoch = self.begin()["active_epoch"]
        self.service.open_source_page(epoch["epoch_id"], {"expected_epoch_state_version": 0, "request_page_token": None, "next_page_token": None, "terminal": True, "items": [item(source)]})
        resumed = self.begin()
        self.assertTrue(resumed["resumed"])
        self.assertIsNotNone(resumed["active_epoch"]["open_page"])
        restarted = self.service.restart_source_epoch(epoch["epoch_id"], {"expected_epoch_state_version": resumed["active_epoch"]["epoch_state_version"], "restart_reason": "ambiguous_crashed_attempt"})
        self.assertIsNone(restarted["active_epoch"]["open_page"])
        self.assertEqual(self.run_page([source])["epoch_status"], "COMPLETED")

    # --- rejections / identity / mapping -----------------------------------

    def test_valid_customer_rejection_is_receipted_without_consuming_allowance(self):
        bad = rejection()
        committed = self.run_page([bad])
        self.assertEqual(committed["epoch_status"], "COMPLETED")
        receipt = self.repository.handling_receipt("forms-bad")
        self.assertEqual((receipt.outcome.value, receipt.job_id), ("REJECTED", None))
        self.assertEqual(self.service.source_cursor(**BINDING)["accepted_member_allowance_remaining"], 1)
        self.assertTrue(self.service.reject_source(bad)["replayed"])
        with self.assertRaisesRegex(SourceConflict, "source_identity_payload_conflict"):
            self.service.reject_source({**bad, "error_code": "email_invalid"})
        stored = json.dumps(self.repository.snapshot_state()["_rejections"], default=str)
        for private in ("not-a-phone", "bad@example.test", "Synthetic"):
            self.assertNotIn(private, stored)
        # Mapping/identity/timestamp failures are never rejections.
        for code in ("forms_question_mapping_unknown", "response_id_invalid", "create_time_invalid"):
            response = self.app.handle("POST", "/v1/source-rejections", headers={"Authorization": "Bearer source"}, body=json.dumps({**rejection("forms-other"), "error_code": code}))
            self.assertEqual((response.status, response.body["error_code"]), (400, "source_rejection_code_invalid"))

    def test_invalid_response_id_and_mapping_drift_get_no_receipt_or_progress(self):
        epoch = self.begin()["active_epoch"]
        with self.assertRaisesRegex(SourceConflict, "source_page_items_invalid"):
            self.service.open_source_page(epoch["epoch_id"], {"expected_epoch_state_version": 0, "request_page_token": None, "next_page_token": None, "terminal": True, "items": [{"response_id": "bad id!", "create_time": CUTOVER, "payload_hash": "sha256:" + "a" * 64}]})
        drifted = event("forms-drift")
        drifted["mapping_version"] = "member-intake.v2"
        response = self.app.handle("POST", "/v1/source-events", headers={"Authorization": "Bearer source"}, body=json.dumps(drifted))
        self.assertEqual((response.status, response.body["error_code"]), (422, "mapping_version_not_allowlisted"))
        self.assertIsNone(self.repository.handling_receipt("forms-drift"))
        self.assertIsNone(self.service.source_cursor(**BINDING)["active_epoch"]["open_page"])

    def test_changed_payload_for_same_response_conflicts_and_blocks_commit(self):
        self.make("continuous")
        original = event("forms-edit", "2026-09-15T00:00:11Z")
        self.service.ingest(original)
        edited = event("forms-edit", "2026-09-15T00:00:11Z", name="Edited Name")
        with self.assertRaisesRegex(SourceConflict, "source_identity_payload_conflict"):
            self.service.ingest(edited)
        epoch = self.begin()["active_epoch"]
        opened = self.service.open_source_page(epoch["epoch_id"], {"expected_epoch_state_version": 0, "request_page_token": None, "next_page_token": None, "terminal": True, "items": [item(edited)]})
        with self.assertRaisesRegex(SourceConflict, "source_page_item_receipt_mismatch"):
            self.service.commit_source_page(opened["page_id"], {"expected_epoch_state_version": opened["epoch_state_version"]})
        # Unchanged payload with a new request (for example a changed
        # lastSubmittedTime observation) replays the original job only.
        self.assertTrue(self.service.ingest(event("forms-edit", "2026-09-15T00:00:11Z", request_id="later-observation"))["replayed"])
        self.assertEqual(len(self.repository.all_jobs()), 1)

    # --- admission modes ---------------------------------------------------

    def test_first_member_admits_exactly_one_and_loser_stays_discoverable(self):
        first = event("forms-first", "2026-09-15T00:00:01Z")
        second = event("forms-second", "2026-09-15T00:00:02Z")
        self.assertEqual(self.begin()["page_size"], 1)
        self.run_page([first], next_token="tok-1")
        cursor = self.service.source_cursor(**BINDING)
        self.assertEqual((cursor["accepted_member_count"], cursor["accepted_member_allowance_remaining"]), (1, 0))
        epoch = cursor["active_epoch"]
        opened = self.service.open_source_page(epoch["epoch_id"], {"expected_epoch_state_version": epoch["epoch_state_version"], "request_page_token": "tok-1", "next_page_token": None, "terminal": True, "items": [item(second)]})
        with self.assertRaisesRegex(SourceConflict, "initial_source_window_exhausted"):
            self.service.ingest(second)
        self.assertIsNone(self.repository.handling_receipt("forms-second"))
        with self.assertRaisesRegex(SourceConflict, "source_page_item_unreceipted"):
            self.service.commit_source_page(opened["page_id"], {"expected_epoch_state_version": opened["epoch_state_version"]})
        # A valid customer rejection may still checkpoint in first_member mode.
        self.assertFalse(self.service.reject_source(rejection("forms-reject-late"))["replayed"])
        # Separate authority later switches only the mode: a new epoch from the
        # same cutover abandons the first_member epoch and rediscovers the loser.
        self.make("continuous", repository=self.repository)
        switched = self.begin()
        self.assertFalse(switched["resumed"])
        self.assertEqual((switched["admission_mode"], switched["production_cutover_exact"]), ("continuous", CUTOVER))
        self.assertIsNone(switched["accepted_member_allowance_remaining"])
        self.assertEqual(self.repository.snapshot_state()["_epochs"][epoch["epoch_id"]].abandon_reason, "admission_mode_changed")
        replay = self.run_page([first], next_token="tok-2")
        self.assertEqual(replay["epoch_status"], "ACTIVE")
        final = self.run_page([second])
        self.assertEqual(final["epoch_status"], "COMPLETED")
        self.assertEqual(len(self.repository.all_jobs()), 2)

    def test_mode_is_bound_by_config_not_production_activation(self):
        self.repository.set_control("production_activation_enabled", True)
        self.service.ingest(event("forms-mode-a", "2026-09-15T00:00:01Z"))
        with self.assertRaisesRegex(SourceConflict, "initial_source_window_exhausted"):
            self.service.ingest(event("forms-mode-b", "2026-09-15T00:00:02Z"))

    def test_epoch_binding_mismatch_and_uninitialized_cutover_fail_closed(self):
        self.config = GatewayConfig(source_cutover_watermark=CUTOVER, source_production_cutover_exact=CUTOVER, source_admission_mode="first_member", source_form_id="another-form")
        service = GatewayService(self.config, self.repository, adapter_ready=True)
        with self.assertRaisesRegex(SourceConflict, "source_form_binding_mismatch"):
            service.begin_source_epoch(dict(BINDING))
        empty = InMemoryRepository(source_cutover_watermark=None)
        empty.initialize_source_cursor("google_forms", "member_registration", "member-intake.v1", CUTOVER, production_cutover_exact=CUTOVER, form_id=None)
        with self.assertRaisesRegex(SourceConflict, "source_production_cutover_uninitialized"):
            GatewayService(self.config, empty, adapter_ready=True).ingest(event())


if __name__ == "__main__":
    unittest.main()
