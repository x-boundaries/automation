import io
import json
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

import member_create_uat_approval as approval  # noqa: E402
import member_create_uat_contract as contract  # noqa: E402
import _create_uat_fixtures as fx  # noqa: E402


def run(argv):
    """Run the CLI, capturing stdout and the exit code."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = approval.main(argv)
    return code, buf.getvalue()


class ApprovalCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.form = self.tmp / "form.csv"
        self.rows = self.tmp / "decision_rows.csv"
        self.ledger = self.tmp / "member_create_uat_ledger.jsonl"
        self.package = self.tmp / "member_create_uat_package.json"
        fx.write_form_csv(self.form)
        fx.write_decision_rows(self.rows)

    def _approve(self, **kw):
        return run(
            ["approve", "--reviewer", "digital", "--input", str(self.form),
             "--decision-rows", str(self.rows), "--row-number", "2", "--ledger", str(self.ledger)]
        )

    def _build(self, extra=None):
        argv = ["build-package", "--input", str(self.form), "--decision-rows", str(self.rows),
                "--row-number", "2", "--ledger", str(self.ledger), "--package-out", str(self.package)]
        return run(argv + (extra or []))

    def test_approve_then_build_produces_valid_package(self):
        code, out = self._approve()
        self.assertEqual(code, 0, out)
        code, out = self._build()
        self.assertEqual(code, 0, out)
        pkg = json.loads(self.package.read_text(encoding="utf-8"))
        ok, reasons = contract.validate_package(pkg)
        self.assertTrue(ok, reasons)

    def test_console_output_is_pii_free(self):
        _, approve_out = self._approve()
        _, build_out = self._build()
        combined = approve_out + build_out
        # Raw member number, canonical member number, name, email never printed.
        self.assertNotIn("90000001", combined)
        self.assertNotIn("6590000001", combined)
        self.assertNotIn("Synthetic Alpha", combined)
        self.assertNotIn("synthetic.alpha@example.invalid", combined)

    def test_reject_blocks_build(self):
        code, _ = run(["reject", "--reviewer", "digital", "--input", str(self.form),
                       "--decision-rows", str(self.rows), "--row-number", "2", "--ledger", str(self.ledger)])
        self.assertEqual(code, 0)
        code, out = self._build()
        self.assertEqual(code, 2)
        self.assertIn("No current approval", out)

    def test_hold_blocks_build(self):
        run(["hold", "--reviewer", "digital", "--input", str(self.form),
             "--decision-rows", str(self.rows), "--row-number", "2", "--ledger", str(self.ledger)])
        code, out = self._build()
        self.assertEqual(code, 2)

    def test_non_ready_decision_state_refused(self):
        fx.write_decision_rows(self.rows, decision="EXISTING_MEMBER_REVIEW")
        code, out = self._approve()
        self.assertEqual(code, 2)
        self.assertIn("READY_FOR_CREATE_REVIEW", out)

    def test_source_change_after_approval_invalidates(self):
        self._approve()
        # Operator edits the form name after approval -> fingerprint drift.
        fx.write_form_csv(self.form, name="Changed Name")
        code, out = self._build()
        self.assertEqual(code, 2)
        self.assertIn("fingerprint mismatch", out)

    def test_expired_approval_refused(self):
        # Approve with a tiny TTL, then hand-edit the ledger expiry into the past.
        self._approve()
        entries = [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        entries[-1]["expires_at"] = "2000-01-01T00:00:00+00:00"
        self.ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        code, out = self._build()
        self.assertEqual(code, 2)
        self.assertIn("expired", out)

    def test_second_build_refused_without_rebuild(self):
        self._approve()
        self.assertEqual(self._build()[0], 0)
        code, out = self._build()
        self.assertEqual(code, 2)
        self.assertIn("already built", out)

    def test_ttl_cannot_exceed_contract_default(self):
        code, out = run(
            ["approve", "--reviewer", "digital", "--input", str(self.form), "--decision-rows", str(self.rows),
             "--row-number", "2", "--ledger", str(self.ledger), "--ttl-hours", str(contract.DEFAULT_APPROVAL_TTL_HOURS + 1)]
        )
        self.assertEqual(code, 2)

    def test_reviewer_id_must_be_non_secret_handle(self):
        # argparse accepts the string; contract pattern is enforced in the built package.
        self._approve()
        self._build()
        pkg = json.loads(self.package.read_text(encoding="utf-8"))
        self.assertRegex(pkg["approval"]["reviewer_id"], r"^[a-z0-9_-]{2,32}$")

    def test_validate_package_for_write_passes_now_business_confirmed(self):
        # All four business confirmations are now recorded, so the laptop-side for-write
        # audit reports write-ready (DRY_RUN_VALIDATED). The actual irreversible write
        # still requires the five VM switches and the separate operator step.
        self._approve()
        self._build()
        code, out = run(["validate-package", "--package", str(self.package), "--for-write",
                         "--business-config", str(ROOT / "config" / "member_create_uat_business_confirmation.json")])
        self.assertEqual(code, 0, out)
        self.assertIn("for_write_ok = true", out)
        self.assertIn("DRY_RUN_VALIDATED", out)

    def test_validate_package_for_write_blocked_when_business_unconfirmed(self):
        # A config with any confirmation false still blocks fail-closed.
        self._approve()
        self._build()
        unconfirmed = self.tmp / "unconfirmed_business.json"
        unconfirmed.write_text(json.dumps({
            "schema_version": contract.BUSINESS_CONFIRMATION_SCHEMA_VERSION,
            "confirmations": {
                "MemberType": {"confirmed": True, "reason": "x"},
                "RegisterDate": {"confirmed": True, "reason": "x"},
                "ExpiryDate": {"confirmed": False, "reason": "x"},
                "OpeningPoints": {"confirmed": True, "reason": "x"},
            },
        }), encoding="utf-8")
        code, out = run(["validate-package", "--package", str(self.package), "--for-write",
                         "--business-config", str(unconfirmed)])
        self.assertEqual(code, 2)
        self.assertIn("OPERATOR_CONFIG_REQUIRED", out)

    def test_package_written_atomically_is_single_object(self):
        self._approve()
        self._build()
        text = self.package.read_text(encoding="utf-8")
        obj = json.loads(text)
        self.assertIsInstance(obj, dict)
        self.assertEqual(obj["approval"]["decision"], "approved")

    # ---- Finding 3: build output must not claim an AutoCount assignment ---- #
    def test_build_summary_does_not_claim_autocount_assignment(self):
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        # The laptop builder only records package payload state, never an assignment.
        self.assertNotIn("expiry_date_assigned", summary)
        self.assertNotIn("expiry_date_assigned", out)
        self.assertIs(summary["expiry_date_in_payload"], True)

    # ---- Finding 2: package output is strictly no-clobber ---- #
    def _ledger_build_events(self):
        if not self.ledger.exists():
            return []
        entries = [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [e for e in entries if e.get("event") == "build"]

    def test_build_refuses_when_output_file_exists_and_preserves_it(self):
        self._approve()
        self.package.write_text("SENTINEL-DO-NOT-OVERWRITE\n", encoding="utf-8")
        before = self.package.read_bytes()
        builds_before = len(self._ledger_build_events())
        code, out = self._build()
        self.assertEqual(code, 2, out)
        # The pre-existing file is byte-for-byte unchanged.
        self.assertEqual(self.package.read_bytes(), before)
        # No build ledger event was appended after the output-path collision.
        self.assertEqual(len(self._ledger_build_events()), builds_before)

    def test_build_refuses_when_output_is_directory(self):
        self._approve()
        self.package.mkdir()
        code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertTrue(self.package.is_dir())
        self.assertEqual(self._ledger_build_events(), [])

    def test_build_refuses_when_output_is_symlink(self):
        self._approve()
        target = self.tmp / "sometarget.txt"
        target.write_text("x", encoding="utf-8")
        try:
            self.package.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a symlink on this platform/privilege")
        code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertEqual(self._ledger_build_events(), [])

    def test_rebuild_cannot_overwrite_existing_package(self):
        self._approve()
        self.assertEqual(self._build()[0], 0)
        before = self.package.read_bytes()
        builds_before = len(self._ledger_build_events())
        # --rebuild to the SAME (existing) path must still refuse fail-closed.
        code, out = self._build(extra=["--rebuild"])
        self.assertEqual(code, 2, out)
        self.assertEqual(self.package.read_bytes(), before)
        self.assertEqual(len(self._ledger_build_events()), builds_before)

    def test_rebuild_to_new_absent_path_succeeds_and_preserves_prior(self):
        self._approve()
        self.assertEqual(self._build()[0], 0)
        first = self.package.read_bytes()
        alt = self.tmp / "member_create_uat_package_v2b.json"
        # A trailing --package-out overrides the helper's default (argparse last-wins).
        code, out = self._build(extra=["--rebuild", "--package-out", str(alt)])
        self.assertEqual(code, 0, out)
        self.assertTrue(alt.is_file())
        # The earlier package is untouched.
        self.assertEqual(self.package.read_bytes(), first)

    # ---- Additional verification: a v1 decision cannot mint a v2 package ---- #
    def test_v1_decision_entry_cannot_build_v2_package(self):
        # A decision recorded under the previous schema version binds a v1-derived
        # source_record_id. Building a v2 package recomputes source_record_id with the
        # current (v2) SCHEMA_VERSION, so the v1 decision never matches and the build is
        # refused. This proves the schema bump mechanically forces a fresh decision.
        canonical_member_no = "6590000001"  # canonical form of the fixture's 90000001
        v2_srid = contract.source_record_id(canonical_member_no)
        v1_srid = "srcrec_" + contract.sha256_hex(
            f"member_create_uat_package/v1|{canonical_member_no}"
        )
        self.assertNotEqual(v1_srid, v2_srid)
        entry = {
            "event": "decision", "recorded_at": "2026-07-24T00:00:00+00:00",
            "reviewer_id": "digital", "decision": "approved",
            "source_record_id": v1_srid, "source_fingerprint": "fp_" + ("0" * 64),
            "row_number_hint": 2, "approval_id": "appr_" + ("0" * 32),
            "approved_at": "2026-07-24T00:00:00+00:00", "expires_at": "2026-07-30T00:00:00+00:00",
        }
        self.ledger.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
        code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertIn("No current approval", out)


if __name__ == "__main__":
    unittest.main()
