import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

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

    def test_completed_package_is_single_object(self):
        # Truthful claim: the COMPLETED published file is one JSON object. Atomic
        # visibility is proven separately in AtomicPublicationTests.
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


class AtomicPublicationTests(unittest.TestCase):
    """Amendment 2/3: the builder publishes the package atomically (a reader never sees a
    partial file at the final path, a crash cannot leave a partial final package), remains
    strictly no-clobber, AND is truthful about temporary-file cleanup - a failed temp
    unlink is never swallowed. Pre-publication and post-publication cleanup failures are
    handled separately."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.form = self.tmp / "form.csv"
        self.rows = self.tmp / "decision_rows.csv"
        self.ledger = self.tmp / "member_create_uat_ledger.jsonl"
        self.package = self.tmp / "member_create_uat_package_v2.json"
        fx.write_form_csv(self.form)
        fx.write_decision_rows(self.rows)

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = approval.main(argv)
        return code, buf.getvalue()

    def _approve(self):
        return self._run(["approve", "--reviewer", "digital", "--input", str(self.form),
                          "--decision-rows", str(self.rows), "--row-number", "2", "--ledger", str(self.ledger)])

    def _build(self, extra=None):
        argv = ["build-package", "--input", str(self.form), "--decision-rows", str(self.rows),
                "--row-number", "2", "--ledger", str(self.ledger), "--package-out", str(self.package)]
        return self._run(argv + (extra or []))

    def _events_of(self, event):
        if not self.ledger.exists():
            return []
        return [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines()
                if l.strip() and json.loads(l).get("event") == event]

    def _build_events(self):
        return self._events_of("build")

    def _cleanup_incomplete_events(self):
        return self._events_of("build_cleanup_incomplete")

    def _stray_temps(self):
        return [f for f in os.listdir(self.tmp) if "mcuat_pkg" in f]

    def test_publication_is_atomic_final_absent_and_temp_complete_before_link(self):
        # At the moment of publication (os.link), the final path must not yet exist and
        # the temporary file must already contain the complete, valid single JSON object.
        self._approve()
        captured = {}
        real_link = os.link

        def spy_link(src, dst):
            captured["final_absent"] = not os.path.lexists(dst)
            captured["temp_obj"] = json.loads(Path(src).read_text(encoding="utf-8"))
            return real_link(src, dst)

        with mock.patch("os.link", spy_link):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertTrue(captured["final_absent"], "final path must be absent until atomic publication")
        self.assertIsInstance(captured["temp_obj"], dict)
        self.assertEqual(captured["temp_obj"]["approval"]["decision"], "approved")
        self.assertTrue(self.package.is_file(), "publication exposes the complete package atomically")
        self.assertEqual(self._stray_temps(), [], "no stale temporary package after success")
        self.assertEqual(len(self._build_events()), 1)

    def test_temp_write_failure_leaves_no_final_no_temp_no_ledger(self):
        self._approve()
        with mock.patch("os.fsync", side_effect=OSError("simulated fsync failure")):
            code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertFalse(self.package.exists(), "final path must be absent on temp-write failure")
        self.assertEqual(self._stray_temps(), [], "temporary file must be cleaned")
        self.assertEqual(self._build_events(), [], "no build ledger event on failure")

    def test_publication_failure_leaves_no_final_no_temp_no_ledger(self):
        self._approve()
        with mock.patch("os.link", side_effect=OSError("simulated publication failure")):
            code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertFalse(self.package.exists(), "final path must be absent when no competitor exists")
        self.assertEqual(self._stray_temps(), [], "temporary file must be cleaned")
        self.assertEqual(self._build_events(), [], "no build ledger event on failure")

    def test_final_path_race_preserves_competitor_and_fails_closed(self):
        self._approve()
        real_link = os.link
        sentinel = "COMPETING-SENTINEL-DO-NOT-OVERWRITE\n"

        def racing_link(src, dst):
            # A competitor creates the final path immediately before our publication.
            with open(dst, "x", encoding="utf-8") as fh:
                fh.write(sentinel)
            return real_link(src, dst)  # now fails FileExistsError against the competitor

        with mock.patch("os.link", racing_link):
            code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertEqual(self.package.read_text(encoding="utf-8"), sentinel,
                         "the competing final file must be preserved byte-for-byte")
        self.assertEqual(self._stray_temps(), [], "only the builder's temporary file is removed")
        self.assertEqual(self._build_events(), [], "no build ledger event on a publication collision")

    def test_completed_package_has_restrictive_permissions_where_supported(self):
        if os.name != "posix":
            self.skipTest("POSIX permission bits are not applicable on this platform")
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertEqual(oct(self.package.stat().st_mode & 0o777), "0o600")

    # ---- Amendment 3: pre-publication failure PLUS a forced temp-cleanup failure ---- #
    def test_temp_write_failure_with_forced_unlink_failure_is_cleanup_incomplete(self):
        self._approve()
        with mock.patch("os.fsync", side_effect=OSError("simulated write failure")), \
                mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_CLEANUP_INCOMPLETE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "cleanup_incomplete")
        self.assertNotEqual(summary["status"], "ok")
        self.assertEqual(summary["publication"], "not_published")
        self.assertEqual(summary["temp_cleanup"], "failed")
        # The original write failure stays distinguishable from the cleanup failure.
        self.assertEqual(summary["original_failure"], "package_write_or_publication_failed")
        self.assertTrue(summary["stale_temp_basename"].endswith(".tmp"))
        self.assertFalse(self.package.exists(), "no final on pre-publication failure")
        self.assertTrue(self._stray_temps(), "the operation-owned temp remains")
        self.assertEqual(self._build_events(), [], "no successful build event")
        self.assertEqual(self._cleanup_incomplete_events(), [], "not published => no publication event")

    def test_publication_failure_with_forced_unlink_failure_is_cleanup_incomplete(self):
        self._approve()
        with mock.patch("os.link", side_effect=OSError("simulated publication failure")), \
                mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_CLEANUP_INCOMPLETE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "cleanup_incomplete")
        self.assertEqual(summary["publication"], "not_published")
        self.assertFalse(self.package.exists(), "the builder created no final path")
        self.assertTrue(self._stray_temps(), "the complete-but-unpublished temp remains")
        self.assertEqual(self._build_events(), [])
        self.assertEqual(self._cleanup_incomplete_events(), [])

    def test_race_with_forced_unlink_failure_preserves_competitor_and_is_cleanup_incomplete(self):
        self._approve()
        real_link = os.link
        sentinel = "COMPETING-SENTINEL-DO-NOT-OVERWRITE\n"

        def racing_link(src, dst):
            with open(dst, "x", encoding="utf-8") as fh:
                fh.write(sentinel)
            return real_link(src, dst)  # FileExistsError against the competitor

        with mock.patch("os.link", racing_link), \
                mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_CLEANUP_INCOMPLETE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "cleanup_incomplete")
        self.assertEqual(summary["publication"], "not_published")
        self.assertEqual(self.package.read_text(encoding="utf-8"), sentinel,
                         "the competing final file must be preserved byte-for-byte, not deleted or mutated")
        self.assertTrue(self._stray_temps(), "the builder's own temp remains")
        self.assertEqual(self._build_events(), [])
        self.assertEqual(self._cleanup_incomplete_events(), [])

    # ---- Amendment 3: post-publication cleanup failure (committed boundary crossed) ---- #
    def test_post_publication_unlink_failure_publishes_and_ledgers_cleanup_incomplete(self):
        self._approve()
        with mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_CLEANUP_INCOMPLETE, out)
        summary = json.loads(out)
        self.assertNotEqual(summary["status"], "ok", "must not report ordinary success")
        self.assertEqual(summary["status"], "cleanup_incomplete")
        self.assertEqual(summary["publication"], "succeeded")
        self.assertEqual(summary["temp_cleanup"], "failed")
        self.assertIs(summary["manual_cleanup_required"], True)
        self.assertIs(summary["do_not_retry"], True)
        # The final package exists, complete and valid.
        self.assertTrue(self.package.is_file(), "the published final package is preserved")
        pkg = json.loads(self.package.read_text(encoding="utf-8"))
        valid, reasons = contract.validate_package(pkg)
        self.assertTrue(valid, reasons)
        # The temporary link remains (cleanup failed).
        self.assertTrue(self._stray_temps(), "the temporary link remains on a cleanup failure")
        # Exactly one durable cleanup-incomplete publication event; no normal build event.
        ci = self._cleanup_incomplete_events()
        self.assertEqual(len(ci), 1)
        self.assertEqual(self._build_events(), [])
        # The event binds the exact published package and approval transaction.
        event = ci[0]
        self.assertEqual(event["operation_id"], pkg["operation_id"])
        self.assertEqual(event["bound_package_payload_hash"], pkg["payload_hash"])
        self.assertEqual(event["package_file_name"], self.package.name)
        self.assertEqual(event["source_record_id"], pkg["source_record_id"])
        self.assertIs(event.get("cleanup_incomplete"), True)

    def test_second_build_after_cleanup_incomplete_fails_closed(self):
        self._approve()
        with mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            code1, out1 = self._build()
        self.assertEqual(code1, approval.EXIT_CLEANUP_INCOMPLETE, out1)
        self.assertEqual(len(self._cleanup_incomplete_events()), 1)
        first_bytes = self.package.read_bytes()

        # A second plain build with the same approval must fail closed (single-use guard).
        code2, out2 = self._build()
        self.assertEqual(code2, 2, out2)
        summary2 = json.loads(out2)
        self.assertEqual(summary2["status"], "error")
        self.assertIn("must not be retried", summary2["error"])

        # Even --rebuild cannot bypass the published-but-unclean guard.
        code3, out3 = self._build(extra=["--rebuild"])
        self.assertEqual(code3, 2, out3)
        self.assertIn("must not be retried", json.loads(out3)["error"])

        # No second package minted, no new build event, first package unchanged.
        self.assertEqual(len(self._cleanup_incomplete_events()), 1)
        self.assertEqual(self._build_events(), [])
        self.assertEqual(self.package.read_bytes(), first_bytes)

    # ---- Amendment 3: marker failure is not a package cleanup failure ---- #
    def test_marker_failure_after_clean_publication_is_still_success(self):
        self._approve()
        with mock.patch.object(approval, "_write_private_marker", side_effect=OSError("marker failure")):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["temp_cleanup"], "complete")
        self.assertTrue(self.package.is_file())
        pkg = json.loads(self.package.read_text(encoding="utf-8"))
        valid, reasons = contract.validate_package(pkg)
        self.assertTrue(valid, reasons)
        # A marker failure never becomes a temporary-package cleanup failure.
        self.assertEqual(self._stray_temps(), [], "temp cleanup still completes despite marker failure")
        self.assertEqual(len(self._build_events()), 1)
        self.assertEqual(self._cleanup_incomplete_events(), [])

    # ---- Amendment 3: no cleanup path ever sweeps unrelated temporaries ---- #
    def test_unrelated_temp_files_are_never_swept(self):
        self._approve()
        unrelated = self.tmp / ".mcuat_pkg_unrelated_sentinel.tmp"
        unrelated.write_text("UNRELATED-DO-NOT-SWEEP\n", encoding="utf-8")
        code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertTrue(unrelated.is_file(), "an unrelated temporary must never be swept")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "UNRELATED-DO-NOT-SWEEP\n")


if __name__ == "__main__":
    unittest.main()
