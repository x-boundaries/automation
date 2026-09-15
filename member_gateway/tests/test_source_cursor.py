import unittest

from xb_member_gateway.api import GatewayService
from xb_member_gateway.canonical import build_source_event, canonicalize_source_event
from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.repository import InMemoryRepository, SourceConflict


WATERMARK = "2026-09-15T00:00:00Z"


def event(response_id="forms-equal", create_time=WATERMARK, request_id="request-1", name="Synthetic Member"):
    return build_source_event(
        response_id=response_id, create_time=create_time, request_id=request_id,
        form_alias="member_registration", mapping_version="member-intake.v1",
        payload={"name": name, "phone": "81234567", "email": "synthetic@example.test", "birthday_month": "January", "marketing_consent": "No", "pdpa_acknowledged": True},
    )


class SourceCursorTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryRepository(source_cutover_watermark=WATERMARK)
        self.config = GatewayConfig(source_cutover_watermark=WATERMARK, production_activation_enabled=False, kill_switch_enabled=True)
        self.service = GatewayService(self.config, self.repository, adapter_ready=True)

    def test_cutover_equality_is_included_and_pre_watermark_is_rejected_without_cursor_move(self):
        before = self.repository.get_source_cursor("member_registration", "member-intake.v1")
        with self.assertRaisesRegex(SourceConflict, "source_event_before_watermark"):
            self.service.ingest(event(create_time="2026-09-14T23:59:59Z"))
        self.assertEqual(self.repository.get_source_cursor("member_registration", "member-intake.v1"), before)
        admitted = self.service.ingest(event())
        self.assertFalse(admitted["replayed"])
        after = self.repository.get_source_cursor("member_registration", "member-intake.v1")
        self.assertEqual(after.last_admitted_create_time, WATERMARK)
        self.assertEqual(after.last_admitted_response_id, "forms-equal")
        self.assertEqual(after.initial_window_admission_count, 1)

    def test_durable_admission_overlap_resume_and_terminal_inclusive_restart(self):
        source = event()
        self.service.ingest(source)
        cursor = self.repository.get_source_cursor("member_registration", "member-intake.v1")
        first = self.repository.checkpoint_source_page(
            "member_registration", "member-intake.v1", expected_state_version=cursor.state_version,
            current_page_token=None, next_page_token="opaque-page-2", scan_lower_bound=WATERMARK,
            terminal=False, responses=[{"response_id": source["response_id"], "create_time": source["create_time"], "payload_hash": source["payload_hash"]}],
        )
        self.assertEqual(first.resume_page_token, "opaque-page-2")
        terminal = self.repository.checkpoint_source_page(
            "member_registration", "member-intake.v1", expected_state_version=first.state_version,
            current_page_token="opaque-page-2", next_page_token=None, scan_lower_bound=WATERMARK,
            terminal=True, responses=[],
        )
        self.assertIsNone(terminal.resume_page_token)
        self.assertEqual(terminal.scan_lower_bound, WATERMARK)
        replay = self.service.ingest({**source, "request_id": "request-overlap"})
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.repository.all_jobs()), 1)

    def test_partial_page_and_token_failures_do_not_advance(self):
        source = event()
        self.service.ingest(source)
        cursor = self.repository.get_source_cursor("member_registration", "member-intake.v1")
        with self.assertRaisesRegex(SourceConflict, "source_page_response_not_admitted"):
            self.repository.checkpoint_source_page(
                "member_registration", "member-intake.v1", expected_state_version=cursor.state_version,
                current_page_token=None, next_page_token="page-2", scan_lower_bound=WATERMARK,
                terminal=False, responses=[{"response_id": "unseen", "create_time": WATERMARK, "payload_hash": source["payload_hash"]}],
            )
        self.assertEqual(self.repository.get_source_cursor("member_registration", "member-intake.v1"), cursor)
        for invalid in ("", "bad\nvalue", "x" * 1025):
            with self.assertRaisesRegex(ValueError, "forms_page_token_invalid"):
                self.repository.checkpoint_source_page(
                    "member_registration", "member-intake.v1", expected_state_version=cursor.state_version,
                    current_page_token=None, next_page_token=invalid, scan_lower_bound=WATERMARK,
                    terminal=False, responses=[],
                )

    def test_reordered_unseen_and_hash_conflict_fail_closed_and_cursor_never_regresses(self):
        later = event(response_id="forms-later", create_time="2026-09-15T00:00:01Z")
        self.service.ingest(later)
        cursor = self.repository.get_source_cursor("member_registration", "member-intake.v1")
        with self.assertRaisesRegex(SourceConflict, "source_event_behind_cursor"):
            self.service.ingest(event(response_id="forms-unseen", request_id="request-2"))
        conflict = event(response_id="forms-later", create_time="2026-09-15T00:00:01Z", request_id="request-3", name="Changed")
        with self.assertRaisesRegex(SourceConflict, "source_identity_payload_conflict"):
            self.service.ingest(conflict)
        self.assertEqual(self.repository.get_source_cursor("member_registration", "member-intake.v1"), cursor)

    def test_initial_window_is_exactly_one_new_response(self):
        self.service.ingest(event())
        with self.assertRaisesRegex(SourceConflict, "initial_source_window_exhausted"):
            self.service.ingest(event(response_id="forms-second", create_time="2026-09-15T00:00:01Z", request_id="request-2"))


if __name__ == "__main__":
    unittest.main()
