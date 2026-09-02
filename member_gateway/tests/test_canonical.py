import unittest

from xb_member_gateway.canonical import (
    CanonicalizationError,
    build_source_event,
    canonical_json,
    canonicalize_source_event,
    dedupe_poll_responses,
    derive_register_and_expiry,
    iter_forms_pages,
)


class CanonicalContractTests(unittest.TestCase):
    def event(self, **changes):
        payload = {
            "name": "  Jose\u0301\r\n  Tan ",
            "phone": "+65 8123 4567",
            "email": "  MEMBER@Example.COM ",
            "birthday_month": "january",
            "marketing_consent": "Yes",
            "pdpa_acknowledged": "I agree",
        }
        payload.update(changes.pop("payload", {}))
        return build_source_event(
            response_id=changes.pop("response_id", "resp-001"),
            request_id=changes.pop("request_id", "req-001"),
            create_time=changes.pop("create_time", "2026-01-01T16:30:00+00:00"),
            form_alias=changes.pop("form_alias", "member_registration"),
            mapping_version=changes.pop("mapping_version", "member-intake.v1"),
            payload=payload,
        )

    def test_canonical_hash_and_timezone_date(self):
        raw = self.event()
        event = canonicalize_source_event(raw)
        self.assertEqual(event.payload["name"], "José Tan")
        self.assertEqual(event.payload["phone"], "6581234567")
        self.assertEqual(event.payload["email"], "member@example.com")
        self.assertTrue(event.payload["pdpa_acknowledged"])
        self.assertEqual(event.payload_hash, raw["payload_hash"])
        self.assertEqual(derive_register_and_expiry(event.create_time), ("2026-01-02", "2028-01-01"))
        self.assertEqual(canonical_json({"b": 1, "a": "x"}), '{"a":"x","b":1}')

    def test_leap_day_date_arithmetic_is_calendar_safe(self):
        self.assertEqual(derive_register_and_expiry("2024-02-28T16:00:00Z"), ("2024-02-29", "2026-02-27"))
        self.assertEqual(derive_register_and_expiry("2024-02-29T16:00:00Z"), ("2024-03-01", "2026-02-28"))

    def test_marketing_must_be_explicit(self):
        with self.assertRaises(CanonicalizationError):
            canonicalize_source_event(self.event(payload={"marketing_consent": "maybe"}))

    def test_page_tokens_and_poll_overlap(self):
        pages = {
            None: {"responses": [{"response_id": "r1"}], "next_page_token": "page-2"},
            "page-2": {"responses": [{"response_id": "r1"}, {"response_id": "r2"}]},
        }
        rows = list(iter_forms_pages(lambda token: pages[token]))
        self.assertEqual([row["response_id"] for row in rows], ["r1", "r1", "r2"])
        self.assertEqual([row["response_id"] for row in dedupe_poll_responses(rows)], ["r1", "r2"])

    def test_repeated_page_token_fails_closed(self):
        with self.assertRaises(CanonicalizationError):
            list(iter_forms_pages(lambda token: {"responses": [], "next_page_token": "same"}, max_pages=3))

    def test_event_shape_and_hash_are_closed(self):
        raw = self.event()
        raw["unexpected"] = True
        with self.assertRaises(CanonicalizationError):
            canonicalize_source_event(raw)
        changed = self.event(payload={"name": "Another Name"})
        changed["payload_hash"] = self.event()["payload_hash"]
        with self.assertRaises(CanonicalizationError):
            canonicalize_source_event(changed)


if __name__ == "__main__":
    unittest.main()
