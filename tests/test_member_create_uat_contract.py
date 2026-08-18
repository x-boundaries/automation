import copy
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock
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

    def test_member_payload_requires_expiry_date(self):
        # ExpiryDate is now an active assignable field: a package whose member_payload
        # omits it no longer matches the exact assignable field set.
        pkg = fx.build_valid_package()
        del pkg["member_payload"]["ExpiryDate"]
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
    def test_committed_config_is_all_confirmed(self):
        # All four business decisions are now explicitly recorded by the owner.
        confirmations = contract.load_business_confirmation(BUSINESS_CONFIG)
        all_confirmed, unconfirmed = contract.business_fields_confirmed(confirmations)
        self.assertTrue(all_confirmed)
        self.assertEqual(unconfirmed, [])

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


class FailClosedStatTests(unittest.TestCase):
    """Amendment 9: path-component classification must distinguish absent from unclassifiable.

    ``is_reparse_point`` answers False both for a path that does not exist and for one that could
    not be classified at all. That is safe only where existence and type were already established
    elsewhere, and is exactly the "treat a classification error as safe" defect that trusted-parent
    admission must not have. ``lstat_no_follow`` therefore returns None ONLY for a provable
    absence and lets every other ``OSError`` propagate.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def test_an_absent_path_is_reported_as_absent(self):
        self.assertIsNone(contract.lstat_no_follow(self.tmp / "not_there"))

    def test_an_existing_directory_is_reported(self):
        info = contract.lstat_no_follow(self.tmp)
        self.assertIsNotNone(info)
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertFalse(contract.stat_is_reparse_point(info))

    def test_an_existing_file_is_reported(self):
        target = self.tmp / "plain.bin"
        target.write_bytes(b"x")
        info = contract.lstat_no_follow(target)
        self.assertIsNotNone(info)
        self.assertTrue(stat.S_ISREG(info.st_mode))

    def test_a_classification_error_propagates_rather_than_answering_absent(self):
        real_lstat = os.lstat

        def refusing(path, *args, **kwargs):
            raise PermissionError(13, "synthetic classification failure")

        with mock.patch("os.lstat", refusing):
            with self.assertRaises(PermissionError):
                contract.lstat_no_follow(self.tmp)
        # The permissive predicate, by contrast, answers False - which is why it must never be
        # used for component admission.
        with mock.patch("os.lstat", refusing):
            self.assertFalse(contract.is_reparse_point(self.tmp))
        self.assertIs(os.lstat, real_lstat)

    def test_the_reparse_predicate_reads_an_already_obtained_stat(self):
        info = os.lstat(self.tmp)
        self.assertFalse(contract.stat_is_reparse_point(info))
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

        class _Shim:
            st_mode = info.st_mode
            st_file_attributes = reparse

        self.assertTrue(contract.stat_is_reparse_point(_Shim()))

    def test_a_real_symlink_is_detected_without_being_followed(self):
        target = self.tmp / "real_dir"
        target.mkdir()
        link = self.tmp / "link_dir"
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks unavailable: {type(error).__name__}")
        info = contract.lstat_no_follow(link)
        self.assertIsNotNone(info)
        self.assertTrue(contract.stat_is_reparse_point(info),
                        "the link itself is classified, not its target")


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

    def test_expiry_date_now_assignable(self):
        enum = self.schema["properties"]["assignable_fields"]["items"]["enum"]
        self.assertIn("ExpiryDate", enum)
        self.assertIn("ExpiryDate", self.schema["properties"]["member_payload"]["required"])
        self.assertIn("ExpiryDate", self.schema["properties"]["desired_business_fields"]["required"])

    def test_schema_version_is_v2(self):
        self.assertEqual(self.schema["properties"]["schema_version"]["const"], "member_create_uat_package/v2")

    def test_jsonschema_validation_when_available(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        jsonschema.validate(fx.build_valid_package(), self.schema)
        # A v2 package whose member_payload omits the now-required ExpiryDate is invalid.
        bad = fx.build_valid_package()
        del bad["member_payload"]["ExpiryDate"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, self.schema)


class SchemaExpiryConstTests(unittest.TestCase):
    """Finding 1: the source-of-truth JSON Schema enforces the exact ExpiryDate value
    via const in both member_payload and desired_business_fields, not merely a date
    shape, so it agrees with the Python/PowerShell exact-value validation."""

    def setUp(self):
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_schema_declares_expiry_const_in_both_places(self):
        mp = self.schema["properties"]["member_payload"]["properties"]["ExpiryDate"]
        db = self.schema["properties"]["desired_business_fields"]["properties"]["ExpiryDate"]
        self.assertEqual(mp.get("const"), "2028-06-30")
        self.assertEqual(db.get("const"), "2028-06-30")
        # Must not fall back to only a permissive date regex.
        self.assertNotIn("pattern", mp)
        self.assertNotIn("pattern", db)

    def test_intended_expiry_passes_real_jsonschema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        jsonschema.validate(fx.build_valid_package(), self.schema)

    def test_other_expiry_in_member_payload_fails_real_jsonschema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        bad = fx.build_valid_package()
        bad["member_payload"]["ExpiryDate"] = "2029-06-30"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, self.schema)

    def test_other_expiry_in_desired_fails_real_jsonschema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        bad = fx.build_valid_package()
        bad["desired_business_fields"]["ExpiryDate"] = "2029-06-30"
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
        expiry_date_assigned=True, assigned_field_count=contract.EXPECTED_ASSIGNED_FIELD_COUNT,
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


class ExpiryDateActivationTests(unittest.TestCase):
    """A. Main UAT contract activation for ExpiryDate.

    ExpiryDate is now an ACTIVE assignable field with the exact value 2028-06-30: the
    code-level capability flag is True, ExpiryDate is in ASSIGNABLE_FIELDS and the
    member_payload, the never-assign set is empty, and every business confirmation is
    True. It remains in the intended assignment and read-back contracts.
    """

    LIB_PS = ROOT / "scripts" / "member_create_uat_runner_lib.ps1"

    def _rebuild_hash(self, pkg):
        pkg["payload_hash"] = contract.compute_payload_hash(pkg)
        pkg["approval"]["bound_package_payload_hash"] = pkg["payload_hash"]
        return pkg

    # ---- A1: intended assignment contract includes ExpiryDate ---- #
    def test_expiry_date_in_intended_assignment_fields(self):
        self.assertIn("ExpiryDate", contract.INTENDED_ASSIGNMENT_FIELDS)
        # The intended set is exactly the active whitelist plus the never-assign set,
        # keeping the active/intended relationship explicit for a clean follow-up flip.
        self.assertEqual(
            sorted(contract.INTENDED_ASSIGNMENT_FIELDS),
            sorted(tuple(contract.ASSIGNABLE_FIELDS) + tuple(contract.NEVER_ASSIGN_FIELDS)),
        )

    # ---- A2: read-back comparison contract includes ExpiryDate ---- #
    def test_expiry_date_in_readback_verification_fields(self):
        self.assertIn("ExpiryDate", contract.READBACK_VERIFICATION_FIELDS)

    # ---- A3: exact intended value is 2028-06-30 ---- #
    def test_expiry_intended_value_is_exact(self):
        self.assertEqual(contract.EXPIRYDATE_INTENDED_VALUE, "2028-06-30")
        self.assertEqual(contract.INTENDED_BUSINESS_VALUES["ExpiryDate"], "2028-06-30")

    # ---- A5: code-level capability flag is now True and mirrored ---- #
    def test_capability_flag_is_true_and_active(self):
        self.assertTrue(contract.EXPIRYDATE_ASSIGNMENT_IMPLEMENTED)
        # ExpiryDate is now in the ACTIVE assignment whitelist and out of never-assign.
        self.assertIn("ExpiryDate", contract.ASSIGNABLE_FIELDS)
        self.assertNotIn("ExpiryDate", contract.NEVER_ASSIGN_FIELDS)
        self.assertEqual(contract.NEVER_ASSIGN_FIELDS, ())

    def test_powershell_lib_mirrors_intended_contract_and_capability_flag(self):
        lib = self.LIB_PS.read_text(encoding="utf-8")
        self.assertIn("$script:CreateUatIntendedAssignmentFields", lib)
        self.assertIn("$script:CreateUatReadbackVerificationFields", lib)
        self.assertIn('$script:CreateUatExpiryDateIntendedValue = "2028-06-30"', lib)
        # The capability flag must now be explicitly true in the PowerShell source, in
        # exact agreement with the Python EXPIRYDATE_ASSIGNMENT_IMPLEMENTED constant.
        self.assertRegex(lib, r"\$script:CreateUatExpiryDateAssignmentImplemented\s*=\s*\$true")
        self.assertNotRegex(lib, r"\$script:CreateUatExpiryDateAssignmentImplemented\s*=\s*\$false")

    # ---- A4: missing / malformed / different / extra ExpiryDate fail closed ---- #
    def test_missing_expiry_date_fails_closed(self):
        pkg = fx.build_valid_package()
        del pkg["desired_business_fields"]["ExpiryDate"]
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("desired_business_fields_mismatch", reasons)

    def test_malformed_expiry_date_fails_closed(self):
        pkg = fx.build_valid_package()
        pkg["desired_business_fields"]["ExpiryDate"] = "2028-13-40"
        self._rebuild_hash(pkg)
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("desired_expiry_date_invalid", reasons)

    def test_different_expiry_date_fails_closed(self):
        pkg = fx.build_valid_package()
        pkg["desired_business_fields"]["ExpiryDate"] = "2099-01-01"
        self._rebuild_hash(pkg)
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("desired_ExpiryDate_not_intended", reasons)

    def test_wrong_member_payload_expiry_date_fails_closed(self):
        # ExpiryDate is now in the active payload; a value other than the intended
        # 2028-06-30 fails closed on the intended-value check.
        pkg = fx.build_valid_package()
        pkg["member_payload"]["ExpiryDate"] = "2099-01-01"
        self._rebuild_hash(pkg)
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("member_payload_ExpiryDate_not_intended", reasons)

    def test_unknown_extra_field_in_active_payload_fails_closed(self):
        # An undeclared extra field still fails the exact field-set check.
        pkg = fx.build_valid_package()
        pkg["member_payload"]["SurpriseField"] = "x"
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("member_payload_field_set_mismatch", reasons)

    # ---- A2/read-back: an ExpiryDate mismatch cannot yield CREATED_VERIFIED ---- #
    def test_readback_expiry_mismatch_cannot_be_created_verified(self):
        # With ExpiryDate in the read-back verification set, a mismatch drives the
        # terminal code to CREATED_READBACK_MISMATCH, never CREATED_VERIFIED.
        code, contr = contract.recompute_terminal_state(_write_flags(readback_match=False))
        self.assertEqual(code, "CREATED_READBACK_MISMATCH")
        self.assertNotEqual(code, "CREATED_VERIFIED")
        self.assertEqual(contr, [])

    # ---- A6: all four business confirmations are true and exact ---- #
    def test_committed_business_confirmations_all_true_including_expiry(self):
        data = json.loads(BUSINESS_CONFIG.read_text(encoding="utf-8"))
        confirmations = data["confirmations"]
        self.assertEqual(sorted(confirmations), sorted(contract.BUSINESS_CONFIRMATION_REQUIRED))
        for field in contract.BUSINESS_CONFIRMATION_REQUIRED:
            self.assertIs(confirmations[field]["confirmed"], True, field)
        self.assertIs(confirmations["ExpiryDate"]["confirmed"], True)


class OldPackageRejectionTests(unittest.TestCase):
    """D. A package built under the previous (v1) contract must be refused fail-closed."""

    def _rebuild_hash(self, pkg):
        pkg["payload_hash"] = contract.compute_payload_hash(pkg)
        pkg["approval"]["bound_package_payload_hash"] = pkg["payload_hash"]
        return pkg

    def test_v1_schema_version_rejected(self):
        pkg = fx.build_valid_package()
        pkg["schema_version"] = "member_create_uat_package/v1"
        self._rebuild_hash(pkg)
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("schema_version_mismatch", reasons)

    def test_old_eight_field_payload_shape_rejected(self):
        # An old-shape package: 8-field member_payload / assignable_fields without ExpiryDate.
        pkg = fx.build_valid_package()
        del pkg["member_payload"]["ExpiryDate"]
        pkg["assignable_fields"] = [f for f in pkg["assignable_fields"] if f != "ExpiryDate"]
        self._rebuild_hash(pkg)
        ok, reasons = contract.validate_package(pkg)
        self.assertFalse(ok)
        self.assertIn("member_payload_field_set_mismatch", reasons)
        self.assertIn("assignable_fields_mismatch", reasons)


class PayloadHashExpiryTests(unittest.TestCase):
    """F/6. Changing ExpiryDate changes both the payload hash and the fingerprint."""

    def test_payload_hash_and_fingerprint_change_with_expiry_date(self):
        base = fx.build_valid_package()
        changed = fx.build_valid_package()
        changed["member_payload"]["ExpiryDate"] = "2029-06-30"
        changed["desired_business_fields"]["ExpiryDate"] = "2029-06-30"
        changed["source_fingerprint"] = contract.source_fingerprint(
            contract.build_fingerprint_fields(changed["member_payload"], changed["desired_business_fields"])
        )
        changed["approval"]["source_fingerprint"] = changed["source_fingerprint"]
        changed["payload_hash"] = contract.compute_payload_hash(changed)
        changed["approval"]["bound_package_payload_hash"] = changed["payload_hash"]
        self.assertNotEqual(base["source_fingerprint"], changed["source_fingerprint"])
        self.assertNotEqual(base["payload_hash"], changed["payload_hash"])


class ExpiryDateTerminalGuardTests(unittest.TestCase):
    """G. Real-write success is impossible without ExpiryDate assigned and the full count."""

    def test_expected_count_is_eleven_and_derived(self):
        self.assertEqual(contract.EXPECTED_ASSIGNED_FIELD_COUNT, 11)
        self.assertEqual(
            contract.EXPECTED_ASSIGNED_FIELD_COUNT,
            len(contract.ASSIGNABLE_FIELDS) + len(contract.RUNNER_ACTIVATION_FIELDS),
        )

    def test_created_verified_requires_expiry_assigned(self):
        _, contr = contract.recompute_terminal_state(_write_flags(expiry_date_assigned=False))
        self.assertIn("expiry_date_not_assigned", contr)

    def test_created_verified_requires_expected_field_count(self):
        _, contr = contract.recompute_terminal_state(_write_flags(assigned_field_count=10))
        self.assertIn("assigned_field_count_stale", contr)

    def test_clean_created_verified_has_no_contradiction(self):
        code, contr = contract.recompute_terminal_state(_write_flags())
        self.assertEqual(code, "CREATED_VERIFIED")
        self.assertEqual(contr, [])

    def test_confirmed_save_then_readback_not_found_is_honest(self):
        # A confirmed save with read-back not found stays WRITE_OUTCOME_UNCERTAIN,
        # never CREATED_VERIFIED, and is not itself a contradiction.
        code, contr = contract.recompute_terminal_state(
            _write_flags(readback_found=False, readback_match=False)
        )
        self.assertEqual(code, "WRITE_OUTCOME_UNCERTAIN")
        self.assertEqual(contr, [])


class StrictTerminalBooleanTypeTests(unittest.TestCase):
    """Closed-PR #113 finding PRRT_kwDOSbJI_s6UdzWO.

    A boolean-valued terminal-state flag must be an ACTUAL boolean. `bool("false")` is True, so
    before this gate a staged result carrying `"expiry_date_assigned": "false"` recomputed to
    CREATED_VERIFIED with no contradiction at all, and the same coercion applied to every other
    two-state proof in the table. The originating exploit and the whole same-root class are
    covered here, on both the contradiction surface and the recompute surface.
    """

    # Every substitute the finding admits: the originating string, its inverse, false- and
    # true-shaped numbers, JSON null, and container types.
    SUBSTITUTES = ("false", "true", "", 0, 1, 0.0, None, [], {}, ["true"], {"value": True})

    def test_the_boolean_subset_is_exact_and_exhaustive(self):
        self.assertEqual(
            set(contract.TERMINAL_STATE_BOOLEAN_FLAGS)
            | set(contract.TERMINAL_STATE_NON_BOOLEAN_FLAGS),
            set(contract.TERMINAL_STATE_FLAGS),
            "the boolean and non-boolean subsets must together be exactly the flag set",
        )
        self.assertEqual(
            set(contract.TERMINAL_STATE_BOOLEAN_FLAGS)
            & set(contract.TERMINAL_STATE_NON_BOOLEAN_FLAGS),
            set(),
            "no flag may be declared both boolean and non-boolean",
        )
        # The non-boolean remainder keeps its existing enum/integer contract, unchanged.
        self.assertEqual(
            contract.TERMINAL_STATE_NON_BOOLEAN_FLAGS,
            ("mode", "recovery_state", "save_outcome", "assigned_field_count"),
        )
        self.assertIn("expiry_date_assigned", contract.TERMINAL_STATE_BOOLEAN_FLAGS)

    def test_the_originating_exploit_fails_closed(self):
        flags = _write_flags(expiry_date_assigned="false")
        self.assertEqual(
            contract.terminal_state_boolean_type_violations(flags),
            ["expiry_date_assigned_not_boolean"],
        )
        code, contr = contract.recompute_terminal_state(flags)
        self.assertIsNone(code, "no terminal code may be derived from an unvalidated flag")
        self.assertIn("expiry_date_assigned_not_boolean", contr)
        self.assertNotEqual(code, "CREATED_VERIFIED")

    def test_every_substitute_for_every_boolean_flag_fails_closed(self):
        """The same-root class, not one field: each boolean flag, each substitute type."""
        for name in contract.TERMINAL_STATE_BOOLEAN_FLAGS:
            for substitute in self.SUBSTITUTES:
                with self.subTest(flag=name, substitute=repr(substitute)):
                    flags = _write_flags(**{name: substitute})
                    self.assertEqual(
                        contract.terminal_state_boolean_type_violations(flags),
                        ["%s_not_boolean" % name],
                    )
                    code, contr = contract.recompute_terminal_state(flags)
                    self.assertIsNone(code)
                    self.assertIn("%s_not_boolean" % name, contr)

    def test_a_same_root_flag_other_than_expiry_date_assigned_is_covered(self):
        """The decisive control: patching only the originating field would leave these open."""
        for name in ("readback_match", "readback_found", "lock_acquired",
                     "save_member_confirmed", "package_structural_valid"):
            with self.subTest(flag=name):
                code, contr = contract.recompute_terminal_state(_write_flags(**{name: "false"}))
                self.assertIsNone(code)
                self.assertEqual(contr, ["%s_not_boolean" % name])

    def test_the_type_gate_runs_before_any_coercion(self):
        """A non-boolean flag suppresses the ordinary table reasons rather than being coerced."""
        flags = _write_flags(expiry_date_assigned="false", save_outcome="not_attempted")
        contr = contract.terminal_state_contradictions(flags)
        self.assertEqual(contr, ["expiry_date_assigned_not_boolean"])
        self.assertNotIn("not_attempted_but_attempted", contr)

    def test_several_non_boolean_flags_are_all_named(self):
        contr = contract.terminal_state_contradictions(
            _write_flags(expiry_date_assigned="false", readback_match=1)
        )
        self.assertEqual(contr, ["readback_match_not_boolean", "expiry_date_assigned_not_boolean"])

    def test_real_booleans_are_unaffected(self):
        """The preserved contract: genuine booleans still drive the canonical table exactly."""
        code, contr = contract.recompute_terminal_state(_write_flags())
        self.assertEqual(code, "CREATED_VERIFIED")
        self.assertEqual(contr, [])
        code, contr = contract.recompute_terminal_state(_write_flags(expiry_date_assigned=False))
        self.assertEqual(code, "CREATED_VERIFIED")
        self.assertEqual(contr, ["expiry_date_not_assigned"])

    def test_an_absent_flag_key_keeps_its_existing_fail_closed_behaviour(self):
        """Omission is not a substitute: it stays falsy in the table, exactly as before."""
        flags = _write_flags()
        del flags["expiry_date_assigned"]
        self.assertEqual(contract.terminal_state_boolean_type_violations(flags), [])
        code, contr = contract.recompute_terminal_state(flags)
        self.assertEqual(code, "CREATED_VERIFIED")
        self.assertIn("expiry_date_not_assigned", contr)

    def test_no_flag_value_is_ever_echoed_in_a_reason(self):
        sentinel = "MEMBER-90000001-SYNTHETIC"
        contr = contract.terminal_state_contradictions(
            _write_flags(expiry_date_assigned=sentinel)
        )
        self.assertEqual(contr, ["expiry_date_assigned_not_boolean"])
        for reason in contr:
            self.assertNotIn(sentinel, reason)


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
