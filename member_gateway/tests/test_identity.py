"""U-MN / U-NC / U-LIM: XB-MN-1 base, name component, AutoCount limits and
synthetic markers (W-G2-149 sections 2.1-2.3, amendment #155:5890550314),
driven by ``tests/fixtures/xb_mn_1_vectors.v1.fixture``. Also the relocated
v1 eligibility source predicates at the VALIDATED gate."""

import unittest

from xb_member_gateway.admission import (
    SOURCE_GATE_REASONS, VALIDATION_REJECTION_REASONS, AdmissionPolicy, AdmissionRejected,
    source_gate_reason, validate_for_queue,
)
from xb_member_gateway.canonical import CanonicalizationError, canonical_phone
from xb_member_gateway.identity import (
    NAME_COMPONENT_RE, IdentityConfigError, IdentityRejected, base_member_no, derive_identity,
    name_component, utf16_len,
)
from xb_member_gateway.models import JobRecord
from xb_member_gateway.results import REASON_CODES

try:
    from .v2_support import load_vectors, payload
except ImportError:  # discovered as a top-level module
    from v2_support import load_vectors, payload


VECTORS = load_vectors()


def expand(value):
    if isinstance(value, dict):
        return value["repeat"] * value["count"] + value.get("suffix", "")
    return value


class XbMn1BaseTests(unittest.TestCase):
    """U-MN: base = ASCII digits of NFKC(canonical phone), 6..15 digits."""

    def test_base_vectors_through_the_unchanged_ingest_canonicaliser(self):
        self.assertGreaterEqual(len(VECTORS["base_vectors"]), 17)
        for vector in VECTORS["base_vectors"]:
            with self.subTest(vector["id"]):
                if "base" in vector:
                    phone = canonical_phone(vector["phone_input"])
                    self.assertEqual(base_member_no(phone), vector["base"])
                    # No prefix inferred, stripped or added; leading zeroes kept.
                    self.assertEqual(phone, vector["base"])
                else:
                    with self.assertRaises(CanonicalizationError) as caught:
                        canonical_phone(vector["phone_input"])
                    self.assertEqual(caught.exception.code if hasattr(caught.exception, "code") else str(caught.exception), vector["ingest_rejection"])

    def test_full_width_digits_are_refused_at_ingest_never_rewritten(self):
        vector = next(item for item in VECTORS["base_vectors"] if item["id"] == "U-MN-13-full-width-digits")
        with self.assertRaisesRegex(CanonicalizationError, "phone_contains_letters_or_unsupported_characters"):
            canonical_phone(vector["phone_input"])

    def test_defensive_base_range_is_rejected_at_validated(self):
        for vector in VECTORS["direct_base_vectors"]:
            with self.subTest(vector["id"]):
                with self.assertRaises(IdentityRejected) as caught:
                    base_member_no(vector["phone"])
                self.assertEqual(caught.exception.reason, vector["identity_rejection"])

    def test_production_base_starting_000_is_ordinary(self):
        identity = derive_identity(payload(phone="00012345678"), "production")
        self.assertEqual(identity.base_member_no, "00012345678")


class NameComponentTests(unittest.TestCase):
    """U-NC: NFKD, drop Mn, ASCII-only upper map, truncate to 20 - len(base)."""

    def test_name_vectors(self):
        for vector in VECTORS["name_vectors"]:
            with self.subTest(vector["id"]):
                component = name_component(vector["name"], vector["base"])
                self.assertEqual(component, vector["component"])
                self.assertRegex(component, NAME_COMPONENT_RE)
                self.assertLessEqual(len(vector["base"] + component), 20)
                self.assertFalse(any(character.isdigit() for character in component))

    def test_room_is_five_or_fourteen_letters_at_the_base_extremes(self):
        self.assertEqual(len(name_component("Abcdefghijklmnopqrstuvwxyz", "1" * 15)), 5)
        self.assertEqual(len(name_component("Abcdefghijklmnopqrstuvwxyz", "1" * 6)), 14)

    def test_non_latin_names_produce_an_empty_component(self):
        for name in ("\u9648\u5927\u6587", "\u0928\u092e\u0938\u094d\u0924\u0947", "\u041c\u0430\u0440\u0438\u044f"):
            with self.subTest(name=ascii(name)):
                self.assertEqual(name_component(name, "91234567"), "")


class LimitAndMarkerTests(unittest.TestCase):
    """U-LIM: UTF-16 limits, synthetic markers and the book-mode rule."""

    def test_limit_vectors(self):
        for vector in VECTORS["limit_vectors"]:
            with self.subTest(vector["id"]):
                value = payload(name=expand(vector["name"]), email=expand(vector["email"]), phone=vector.get("phone", "81234567"))
                if vector["rejection"] is None:
                    identity = derive_identity(value, vector["mode"])
                    self.assertEqual(identity.rule, "XB-MN-1")
                else:
                    with self.assertRaises(IdentityRejected) as caught:
                        derive_identity(value, vector["mode"])
                    self.assertEqual(caught.exception.reason, vector["rejection"])

    def test_utf16_length_counts_supplementary_plane_as_two(self):
        self.assertEqual(utf16_len("a"), 1)
        self.assertEqual(utf16_len("\U0001F600"), 2)
        self.assertEqual(utf16_len("\u00e9"), 1)

    def test_nothing_is_truncated_or_rewritten(self):
        long_name = "a" * 101
        with self.assertRaises(IdentityRejected):
            derive_identity(payload(name=long_name), "production")
        self.assertEqual(len(long_name), 101)

    def test_book_mode_is_closed_and_has_no_default(self):
        for mode in (None, "", "PRODUCTION", "either", "prod"):
            with self.subTest(mode=mode):
                with self.assertRaises(IdentityConfigError):
                    derive_identity(payload(), mode)

    def test_gateway_only_reasons_never_appear_in_the_result_reason_enum(self):
        gateway_only = set(SOURCE_GATE_REASONS) | {"phone_digits_out_of_range"}
        self.assertTrue(gateway_only.isdisjoint(REASON_CODES))
        self.assertIn("name_exceeds_autocount_limit", VALIDATION_REJECTION_REASONS)


def job(**changes):
    values = dict(
        job_id="job-" + "1" * 32, request_id="request-1", source_response_ref="hmac-v1:" + "0" * 64,
        response_id="response-1", payload_hash="sha256:" + "0" * 64, operation="member.create",
        member_payload={**payload(), "create_time": "2026-09-20T00:30:00Z"}, created_at="2026-09-20T00:30:00Z",
    )
    values.update(changes)
    return JobRecord(**values)


class RelocatedEligibilityPredicateTests(unittest.TestCase):
    """eligibility.py predicates relocated: every v1 source predicate is a
    bounded REJECTED_VALIDATION reason at VALIDATED (claim-time coverage is in
    test_claim_v2)."""

    policy = AdmissionPolicy("production", ("member_registration",), ("member-intake.v1",))

    def assert_rejected(self, reason, **changes):
        candidate = job(**changes)
        self.assertEqual(source_gate_reason(candidate, self.policy), reason)
        with self.assertRaises(AdmissionRejected) as caught:
            validate_for_queue(candidate, self.policy)
        self.assertEqual(caught.exception.reason, reason)

    def test_each_source_predicate_has_its_own_reason(self):
        base_payload = job().member_payload
        self.assert_rejected("operation_not_member_create", operation="member.update")
        self.assert_rejected("source_system_not_allowlisted", source_system="other_forms")
        self.assert_rejected("form_alias_not_allowlisted", form_alias="other_form")
        self.assert_rejected("mapping_version_not_allowlisted", mapping_version="member-intake.v0")
        self.assert_rejected("response_identity_invalid", response_id="bad response id")
        self.assert_rejected("payload_hash_invalid", payload_hash="sha256:short")
        self.assert_rejected("payload_fields_invalid", member_payload={key: value for key, value in base_payload.items() if key != "birthday_month"})
        self.assert_rejected("payload_fields_invalid", member_payload={**base_payload, "udf": "x"})
        self.assert_rejected("pdpa_not_acknowledged", member_payload={**base_payload, "pdpa_acknowledged": False})
        self.assert_rejected("pdpa_not_acknowledged", member_payload={**base_payload, "pdpa_acknowledged": "yes"})
        self.assert_rejected("marketing_consent_unrecognized", member_payload={**base_payload, "marketing_consent": "Maybe"})

    def test_source_predicates_run_before_identity(self):
        bad = {**job().member_payload, "pdpa_acknowledged": False, "name": "a" * 101}
        with self.assertRaises(AdmissionRejected) as caught:
            validate_for_queue(job(member_payload=bad), self.policy)
        self.assertEqual(caught.exception.reason, "pdpa_not_acknowledged")

    def test_passing_job_gets_the_identity(self):
        identity = validate_for_queue(job(), self.policy)
        self.assertEqual((identity.base_member_no, identity.name_component), ("81234567", "SYNTHETICMEM"))

    def test_policy_requires_a_book_mode_and_allowlists(self):
        with self.assertRaises(ValueError):
            AdmissionPolicy(None, ("member_registration",), ("member-intake.v1",))
        with self.assertRaises(ValueError):
            AdmissionPolicy("production", (), ("member-intake.v1",))


if __name__ == "__main__":
    unittest.main()
