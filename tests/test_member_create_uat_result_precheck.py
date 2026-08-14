import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import member_create_uat_result_precheck as precheck  # noqa: E402
import member_create_uat_contract as contract  # noqa: E402
import _create_uat_fixtures as fx  # noqa: E402


def sample_result(**overrides):
    pkg = fx.build_valid_package()
    result = {
        "mode": "write",
        "runtime_location": "autocount_vm",
        "terminal_code": "CREATED_VERIFIED",
        "package_structural_valid": True,
        "package_fingerprint_problem": False,
        "approval_not_expired": True,
        "write_confirmed": True,
        "business_confirmed": True,
        "lock_acquired": True,
        "recovery_state": "none",
        "execution_error": False,
        "authentication_success": True,
        "member_command_found": True,
        "get_member_found": True,
        "member_exists_initial": False,
        "new_member_success": True,
        "assignment_success": True,
        "assigned_field_count": 11,
        "expiry_date_assigned": True,
        "member_exists_recheck": False,
        "write_intent_recorded": True,
        "consumed_marker_written": True,
        "save_member_attempted": True,
        "save_member_confirmed": True,
        "save_outcome": "confirmed",
        "readback_found": True,
        "readback_match": True,
        "masked_member_no": "65***1",
        "operation_id": pkg["operation_id"],
        "source_record_id": pkg["source_record_id"],
        "source_fingerprint": pkg["source_fingerprint"],
        "error": None,
    }
    result.update(overrides)
    return result, pkg


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = precheck.main(argv)
    return code, buf.getvalue()


class ResultPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.result_path = self.tmp / "member_create_uat_result.json"

    def _write(self, result, *, bom=False):
        text = json.dumps(result)
        if bom:
            self.result_path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
        else:
            self.result_path.write_text(text, encoding="utf-8")

    def test_valid_result_passes_with_identity_revalidation(self):
        result, pkg = sample_result()
        self._write(result)
        code, out = run(["--result-json", str(self.result_path),
                         "--expect-source-record-id", pkg["source_record_id"],
                         "--expect-source-fingerprint", pkg["source_fingerprint"]])
        self.assertEqual(code, 0, out)
        self.assertIn("status = ok", out)
        self.assertIn("identity_revalidated = ok", out)
        self.assertIn("fingerprint_revalidated = ok", out)

    def test_bom_prefixed_file_is_tolerated(self):
        result, _ = sample_result()
        self._write(result, bom=True)
        code, out = run(["--result-json", str(self.result_path)])
        self.assertEqual(code, 0, out)

    def test_forbidden_pii_field_rejected(self):
        result, _ = sample_result()
        result["Name"] = "Leaked Name"
        self._write(result)
        code, out = run(["--result-json", str(self.result_path)])
        self.assertEqual(code, 2)
        self.assertIn("pii_free = false", out)

    def test_unknown_terminal_code_rejected(self):
        result, _ = sample_result(terminal_code="MADE_UP_CODE")
        self._write(result)
        code, out = run(["--result-json", str(self.result_path)])
        self.assertEqual(code, 2)
        self.assertIn("terminal_code_valid = false", out)

    def test_identity_mismatch_rejected(self):
        result, _ = sample_result()
        self._write(result)
        code, out = run(["--result-json", str(self.result_path),
                         "--expect-source-record-id", "srcrec_" + ("0" * 64)])
        self.assertEqual(code, 2)
        self.assertIn("identity_revalidated = mismatch", out)

    def test_fingerprint_mismatch_rejected(self):
        result, pkg = sample_result()
        self._write(result)
        code, out = run(["--result-json", str(self.result_path),
                         "--expect-source-fingerprint", "fp_" + ("0" * 64)])
        self.assertEqual(code, 2)
        self.assertIn("fingerprint_revalidated = mismatch", out)

    def test_malformed_masked_member_no_rejected(self):
        result, _ = sample_result(masked_member_no="6590000001")
        self._write(result)
        code, out = run(["--result-json", str(self.result_path)])
        self.assertEqual(code, 2)
        self.assertIn("masked_member_no_ok = false", out)

    def test_missing_file_reports_shape_error(self):
        code, out = run(["--result-json", str(self.tmp / "nope.json")])
        self.assertEqual(code, 2)
        self.assertIn("shape_error = result_file_missing", out)

    def test_terminal_code_not_matching_flags_rejected(self):
        # Flags say CREATED_VERIFIED but the stored code claims DRY_RUN_VALIDATED.
        result, _ = sample_result(terminal_code="DRY_RUN_VALIDATED")
        self._write(result)
        code, out = run(["--result-json", str(self.result_path)])
        self.assertEqual(code, 2)
        self.assertIn("terminal_code_recomputed_ok = false", out)

    def test_contradictory_flags_rejected(self):
        # save_member_confirmed with no attempt is impossible.
        result, _ = sample_result(save_member_attempted=False)
        self._write(result)
        code, out = run(["--result-json", str(self.result_path)])
        self.assertEqual(code, 2)
        self.assertIn("state_contradiction_count = ", out)
        self.assertNotIn("state_contradiction_count = 0", out)

    def test_recovery_result_recomputes_ok(self):
        # A recovery re-run stops before assignment, so it never assigned ExpiryDate and
        # its assigned_field_count is 0. Because no save was attempted, the ExpiryDate /
        # count guards do not fire and the result is accepted.
        result, pkg = sample_result(
            terminal_code="WRITE_OUTCOME_UNCERTAIN", recovery_state="consumed_no_terminal",
            save_member_attempted=False, save_member_confirmed=False, save_outcome="not_attempted",
            readback_found=False, readback_match=False,
            expiry_date_assigned=False, assigned_field_count=0,
        )
        self._write(result)
        code, out = run(["--result-json", str(self.result_path)])
        self.assertEqual(code, 0, out)
        self.assertIn("terminal_code_recomputed_ok = true", out)

    def test_all_terminal_codes_accepted_in_vocabulary(self):
        for code_value in contract.TERMINAL_CODES:
            result, _ = sample_result(terminal_code=code_value)
            self._write(result)
            _, out = run(["--result-json", str(self.result_path)])
            self.assertIn("terminal_code_valid = true", out, code_value)


class StrictBooleanFlagPrecheckTests(unittest.TestCase):
    """Closed-PR #113 finding PRRT_kwDOSbJI_s6UdzWO, at the precheck surface.

    A staged result carrying a non-boolean state flag previously produced
    `status = ok` for an otherwise valid CREATED_VERIFIED result, because Python coerced the
    nonempty string "false" to True. Every substitute must now be refused.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.result_path = self.tmp / "member_create_uat_result.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, **overrides):
        result, _ = sample_result(**overrides)
        self.result_path.write_text(json.dumps(result), encoding="utf-8")
        return run(["--result-json", str(self.result_path)])

    def test_the_originating_expiry_date_assigned_string_is_refused(self):
        code, out = self._run(expiry_date_assigned="false")
        self.assertEqual(code, 2, out)
        self.assertIn("status = needs_fix", out)
        self.assertIn("terminal_code_recomputed_ok = false", out)
        self.assertNotIn("state_contradiction_count = 0", out)

    def test_every_substitute_type_is_refused(self):
        for substitute in ("false", "true", "", 0, 1, 0.0, None, [], {}):
            with self.subTest(substitute=repr(substitute)):
                code, out = self._run(expiry_date_assigned=substitute)
                self.assertEqual(code, 2, out)
                self.assertIn("status = needs_fix", out)

    def test_same_root_boolean_flags_are_refused_too(self):
        for name in ("readback_match", "readback_found", "lock_acquired", "write_confirmed",
                     "business_confirmed", "save_member_attempted", "save_member_confirmed",
                     "package_structural_valid", "package_fingerprint_problem",
                     "approval_not_expired", "execution_error", "member_exists_initial",
                     "member_exists_recheck"):
            with self.subTest(flag=name):
                code, out = self._run(**{name: "false"})
                self.assertEqual(code, 2, out)
                self.assertIn("status = needs_fix", out)

    def test_a_clean_boolean_result_is_still_accepted(self):
        """The preserved contract: real booleans keep passing exactly as before."""
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("status = ok", out)
        self.assertIn("terminal_code_recomputed_ok = true", out)

    def test_no_flag_value_is_echoed_into_the_aggregate_evidence(self):
        code, out = self._run(expiry_date_assigned="MEMBER-90000001-SYNTHETIC")
        self.assertEqual(code, 2, out)
        self.assertNotIn("MEMBER-90000001-SYNTHETIC", out)


if __name__ == "__main__":
    unittest.main()
