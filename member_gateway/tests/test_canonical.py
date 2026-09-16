import unittest

from xb_member_gateway.canonical import (
    CanonicalizationError,
    build_source_event,
    canonical_json,
    canonical_phone,
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


class OpaqueDigitPhoneTests(unittest.TestCase):
    """The phone is the member's own digits: no country code is required or added."""

    def test_local_and_country_prefixed_digits_are_preserved_and_distinct(self):
        self.assertEqual(canonical_phone("91234567"), "91234567")
        self.assertEqual(canonical_phone("6591234567"), "6591234567")
        self.assertNotEqual(canonical_phone("91234567"), canonical_phone("6591234567"))

    def test_international_digits_are_accepted_without_country_interpretation(self):
        self.assertEqual(canonical_phone("+44 7700 900123"), "447700900123")
        self.assertEqual(canonical_phone("14155552671"), "14155552671")

    def test_presentation_characters_are_stripped_without_meaning(self):
        for supplied in ("+65 9123 4567", "(65) 9123-4567", "65.9123.4567", "+6591234567"):
            with self.subTest(supplied=supplied):
                self.assertEqual(canonical_phone(supplied), "6591234567")

    def test_leading_zero_survives_canonicalization(self):
        self.assertEqual(canonical_phone("0912345"), "0912345")
        self.assertEqual(canonical_phone("+0044 7700 900123"), "00447700900123")

    def test_letters_extensions_and_unsupported_punctuation_are_rejected(self):
        for supplied in (
            "9123456x",
            "91234567 x123",
            "91234567 ext123",
            "91234567#123",
            "91234567,123",
            "91234567/123",
            "9123\t4567",
            "9123\n4567",
            "\uff19\uff11\uff12\uff13\uff14\uff15\uff16\uff17",
        ):
            with self.subTest(supplied=supplied):
                with self.assertRaises(CanonicalizationError):
                    canonical_phone(supplied)

    def test_plus_is_allowed_once_and_only_in_the_lead_position(self):
        self.assertEqual(canonical_phone(" +6591234567 "), "6591234567")
        for supplied in ("++6591234567", "65+91234567", "6591234567+"):
            with self.subTest(supplied=supplied):
                with self.assertRaises(CanonicalizationError):
                    canonical_phone(supplied)

    def test_empty_and_separator_only_input_is_rejected(self):
        for supplied in ("", "   ", "+", "()", "- - -", "..."):
            with self.subTest(supplied=supplied):
                with self.assertRaises(CanonicalizationError):
                    canonical_phone(supplied)

    def test_accepted_length_range_is_six_to_fifteen_digits(self):
        self.assertEqual(canonical_phone("1" * 6), "1" * 6)
        self.assertEqual(canonical_phone("1" * 15), "1" * 15)
        for supplied in ("1" * 5, "1" * 16, "1" * 20):
            with self.subTest(length=len(supplied)):
                with self.assertRaises(CanonicalizationError):
                    canonical_phone(supplied)


if __name__ == "__main__":
    unittest.main()
