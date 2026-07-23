import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import member_create_uat_contract as contract  # noqa: E402
import _create_uat_fixtures as fx  # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "member_create_uat_package.schema.json"
BUSINESS_CONFIG = ROOT / "config" / "member_create_uat_business_confirmation.json"


class CanonicalJsonTests(unittest.TestCase):
    def test_sorts_keys_and_uses_minimal_separators(self):
        self.assertEqual(contract.canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')

    def test_escapes_non_ascii_and_control_chars_like_python_json(self):
        self.assertEqual(contract.canonical_json("陈"), '"\\u9648"')
        self.assertEqual(contract.canonical_json("\x7f"), '"\\u007f"')
        self.assertEqual(contract.canonical_json("\t"), '"\\t"')
        self.assertEqual(contract.canonical_json("~"), '"~"')

    def test_deterministic_regardless_of_insertion_order(self):
        one = contract.canonical_json({"x": [1, 2], "y": {"m": 1, "n": 2}})
        two = contract.canonical_json({"y": {"n": 2, "m": 1}, "x": [1, 2]})
        self.assertEqual(one, two)


class IdentityTests(unittest.TestCase):
    def test_source_record_id_is_stable_and_hash_shaped(self):
        a = contract.source_record_id("6590000001")
        b = contract.source_record_id("6590000001")
        self.assertEqual(a, b)
        self.assertRegex(a, r"^srcrec_[0-9a-f]{64}$")

    def test_source_record_id_rejects_non_canonical_member_no(self):
        with self.assertRaises(contract.ContractError):
            contract.source_record_id("not a number!!")

    def test_fingerprint_changes_when_any_approved_field_changes(self):
        pkg = fx.build_valid_package()
        base = pkg["source_fingerprint"]
        changed = contract.source_fingerprint(
            contract.build_fingerprint_fields(
                {**pkg["member_payload"], "Name": "Different"}, pkg["desired_business_fields"]
            )
        )
        self.assertNotEqual(base, changed)


class PayloadHashTests(unittest.TestCase):
    def test_valid_package_passes(self):
        ok, reasons = contract.validate_package(fx.build_valid_package())
        self.assertTrue(ok, reasons)

    def test_tampering_member_payload_trips_integrity_and_fingerprint(self):
        pkg = fx.build_valid_package()
        pkg["member_payload"]["Name"] = "Tampered"
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("payload_hash_integrity_failed", reasons)
        self.assertIn("source_fingerprint_recompute_mismatch", reasons)

    def test_bound_hash_must_equal_payload_hash(self):
        pkg = fx.build_valid_package()
        pkg["approval"]["bound_package_payload_hash"] = "sha256:" + ("1" * 64)
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("approval_binding_mismatch", reasons)


class PackageValidationRejectionTests(unittest.TestCase):
    def test_extra_top_level_field_rejected(self):
        pkg = fx.build_valid_package()
        pkg["surprise"] = 1
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertEqual(reasons, ["top_level_field_set_mismatch"])

    def test_expiry_date_in_member_payload_rejected(self):
        pkg = fx.build_valid_package()
        pkg["member_payload"]["ExpiryDate"] = "2028-06-30"
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("member_payload_field_set_mismatch", reasons)

    def test_mobile_phone_must_be_blank(self):
        pkg = fx.build_valid_package({"MobilePhone": "90000000"})
        # rebuild hash after change so we isolate the field rule, not integrity
        pkg["payload_hash"] = contract.compute_payload_hash(pkg)
        pkg["approval"]["bound_package_payload_hash"] = pkg["payload_hash"]
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("mobile_phone_not_blank", reasons)

    def test_opening_points_must_be_zero(self):
        pkg = fx.build_valid_package({"OpeningPoints": 5})
        pkg["payload_hash"] = contract.compute_payload_hash(pkg)
        pkg["approval"]["bound_package_payload_hash"] = pkg["payload_hash"]
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("opening_points_not_zero", reasons)

    def test_dob_must_be_month_sentinel(self):
        pkg = fx.build_valid_package({"DOB": "1990-03-15"})
        pkg["payload_hash"] = contract.compute_payload_hash(pkg)
        pkg["approval"]["bound_package_payload_hash"] = pkg["payload_hash"]
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("dob_invalid", reasons)

    def test_non_object_rejected(self):
        ok, reasons = contract.validate_package(["not", "an", "object"])
        self.assertFalse(ok)
        self.assertEqual(reasons, ["package_not_object"])


class BusinessGateTests(unittest.TestCase):
    def test_committed_config_is_all_false_fail_closed(self):
        confirmations = contract.load_business_confirmation(BUSINESS_CONFIG)
        all_confirmed, unconfirmed = contract.business_fields_confirmed(confirmations)
        self.assertFalse(all_confirmed)
        self.assertEqual(sorted(unconfirmed), sorted(contract.BUSINESS_CONFIRMATION_REQUIRED))

    def test_partial_confirmation_still_blocks(self):
        confirmations = {
            "MemberType": {"confirmed": True},
            "RegisterDate": {"confirmed": True},
            "ExpiryDate": {"confirmed": False},
            "OpeningPoints": {"confirmed": True},
        }
        all_confirmed, unconfirmed = contract.business_fields_confirmed(confirmations)
        self.assertFalse(all_confirmed)
        self.assertEqual(unconfirmed, ["ExpiryDate"])

    def test_missing_config_fails_closed(self):
        with self.assertRaises(contract.ContractError):
            contract.load_business_confirmation(ROOT / "config" / "does_not_exist.json")


class PathSafetyTests(unittest.TestCase):
    def test_relative_path_rejected(self):
        with self.assertRaises(contract.ContractError):
            contract.assert_safe_local_path("relative/thing.json")

    def test_parent_traversal_rejected(self):
        bad = str(ROOT / ".." / "escape.json")
        with self.assertRaises(contract.ContractError):
            contract.assert_safe_local_path(bad)

    def test_unsafe_basename_rejected(self):
        with self.assertRaises(contract.ContractError):
            contract.assert_safe_local_path(str(ROOT / "bad name*.json"))


class RedactionTests(unittest.TestCase):
    def test_mask_member_no(self):
        self.assertEqual(contract.mask_member_no("6590000001"), "65***1")
        self.assertEqual(contract.mask_member_no("12"), "***")
        self.assertEqual(contract.mask_member_no(None), "***")

    def test_redact_removes_secrets(self):
        self.assertEqual(contract.redact("server=SECRET here", ["SECRET"]), "server=<redacted> here")


class SchemaSyncTests(unittest.TestCase):
    """The language-neutral schema and the Python contract must not drift."""

    def setUp(self):
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_schema_version_matches(self):
        self.assertEqual(self.schema["properties"]["schema_version"]["const"], contract.SCHEMA_VERSION)

    def test_assignable_fields_enum_matches(self):
        enum = self.schema["properties"]["assignable_fields"]["items"]["enum"]
        self.assertEqual(sorted(enum), sorted(contract.ASSIGNABLE_FIELDS))

    def test_member_payload_required_matches_assignable(self):
        required = self.schema["properties"]["member_payload"]["required"]
        self.assertEqual(sorted(required), sorted(contract.ASSIGNABLE_FIELDS))

    def test_member_payload_forbids_additional_properties(self):
        self.assertFalse(self.schema["properties"]["member_payload"]["additionalProperties"])

    def test_expiry_date_not_assignable(self):
        enum = self.schema["properties"]["assignable_fields"]["items"]["enum"]
        self.assertNotIn("ExpiryDate", enum)
        self.assertIn("ExpiryDate", self.schema["properties"]["desired_business_fields"]["required"])

    def test_jsonschema_validation_when_available(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        jsonschema.validate(fx.build_valid_package(), self.schema)
        bad = fx.build_valid_package()
        bad["member_payload"]["ExpiryDate"] = "2028-06-30"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, self.schema)


def _write_flags(**over):
    base = dict(
        mode="write", package_fingerprint_problem=False, package_structural_valid=True,
        approval_not_expired=True, write_confirmed=True, business_confirmed=True,
        lock_acquired=True, recovery_state="none", execution_error=False,
        member_exists_initial=False, member_exists_recheck=False,
        save_member_attempted=True, save_member_confirmed=True, save_outcome="confirmed",
        readback_found=True, readback_match=True,
    )
    base.update(over)
    return base


class TerminalStateTests(unittest.TestCase):
    def test_created_verified_when_all_consistent(self):
        code, contr = contract.recompute_terminal_state(_write_flags())
        self.assertEqual(code, "CREATED_VERIFIED")
        self.assertEqual(contr, [])

    def test_recovery_states_map_deterministically(self):
        blocked = dict(save_member_attempted=False, save_member_confirmed=False, save_outcome="not_attempted",
                       readback_found=False, readback_match=False)
        for state, expected in (
            ("terminal_exists", "PACKAGE_ALREADY_CONSUMED"),
            ("consumed_no_terminal", "WRITE_OUTCOME_UNCERTAIN"),
            ("intent_no_consumed", "FAILED_BEFORE_WRITE"),
            ("malformed", "WRITE_OUTCOME_UNCERTAIN"),
        ):
            code, contr = contract.recompute_terminal_state(_write_flags(recovery_state=state, **blocked))
            self.assertEqual(code, expected, state)
            self.assertEqual(contr, [])

    def test_execution_error_maps_by_attempt(self):
        code, _ = contract.recompute_terminal_state(
            _write_flags(execution_error=True, save_member_attempted=False, save_member_confirmed=False,
                         save_outcome="not_attempted", readback_found=False, readback_match=False))
        self.assertEqual(code, "FAILED_BEFORE_WRITE")
        code2, _ = contract.recompute_terminal_state(
            _write_flags(execution_error=True, save_member_confirmed=False, save_outcome="uncertain",
                         readback_found=False, readback_match=False))
        self.assertEqual(code2, "WRITE_OUTCOME_UNCERTAIN")

    def test_contradiction_detected(self):
        # Confirmed save reported in dry-run mode is impossible.
        _, contr = contract.recompute_terminal_state(
            dict(mode="dry-run", package_structural_valid=True, package_fingerprint_problem=False,
                 approval_not_expired=True, lock_acquired=True, recovery_state="none", execution_error=False,
                 member_exists_initial=False, member_exists_recheck=False, write_confirmed=False,
                 business_confirmed=False, save_member_attempted=True, save_member_confirmed=True,
                 save_outcome="confirmed", readback_found=True, readback_match=True))
        self.assertTrue(contr)

    def test_created_verified_requires_readback_match(self):
        code, _ = contract.recompute_terminal_state(_write_flags(readback_match=False))
        self.assertEqual(code, "CREATED_READBACK_MISMATCH")
        code2, _ = contract.recompute_terminal_state(
            _write_flags(readback_found=False, readback_match=False))
        self.assertEqual(code2, "WRITE_OUTCOME_UNCERTAIN")


class SchemaEquivalentValidationTests(unittest.TestCase):
    """Finding 6: intended values, approval ordering, and exact sets."""

    def _rebuild_hash(self, pkg):
        pkg["payload_hash"] = contract.compute_payload_hash(pkg)
        pkg["approval"]["bound_package_payload_hash"] = pkg["payload_hash"]
        return pkg

    def test_wrong_intended_member_type_rejected(self):
        pkg = fx.build_valid_package({"MemberType": "Gold"})
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("member_payload_MemberType_not_intended", reasons)

    def test_wrong_intended_expiry_date_rejected(self):
        pkg = fx.build_valid_package()
        pkg["desired_business_fields"]["ExpiryDate"] = "2099-01-01"
        self._rebuild_hash(pkg)
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("desired_ExpiryDate_not_intended", reasons)

    def test_approval_expiry_before_approved_at_rejected(self):
        pkg = fx.build_valid_package()
        pkg["approval"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        self._rebuild_hash(pkg)
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("approval_expiry_not_after_approved_at", reasons)

    def test_extra_approval_field_rejected(self):
        pkg = fx.build_valid_package()
        pkg["approval"]["sneaky"] = 1
        self._rebuild_hash(pkg)
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("approval_field_set_mismatch", reasons)


class TerminalCodeTests(unittest.TestCase):
    def test_required_codes_present(self):
        for code in (
            "DRY_RUN_VALIDATED",
            "BLOCKED_MEMBER_EXISTS",
            "APPROVAL_INVALID",
            "SOURCE_FINGERPRINT_MISMATCH",
            "CREATED_VERIFIED",
            "CREATED_READBACK_MISMATCH",
            "FAILED_BEFORE_WRITE",
            "WRITE_OUTCOME_UNCERTAIN",
            "OPERATOR_CONFIG_REQUIRED",
        ):
            self.assertIn(code, contract.TERMINAL_CODES)


if __name__ == "__main__":
    unittest.main()
