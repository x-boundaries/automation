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

    def test_validate_package_for_write_is_operator_config_required(self):
        self._approve()
        self._build()
        code, out = run(["validate-package", "--package", str(self.package), "--for-write",
                         "--business-config", str(ROOT / "config" / "member_create_uat_business_confirmation.json")])
        self.assertEqual(code, 2)
        self.assertIn("OPERATOR_CONFIG_REQUIRED", out)

    def test_package_written_atomically_is_single_object(self):
        self._approve()
        self._build()
        text = self.package.read_text(encoding="utf-8")
        obj = json.loads(text)
        self.assertIsInstance(obj, dict)
        self.assertEqual(obj["approval"]["decision"], "approved")


if __name__ == "__main__":
    unittest.main()
