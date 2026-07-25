import builtins
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

    def _reservations(self):
        return sorted(f for f in os.listdir(self.tmp) if f.endswith(approval.RESERVATION_SUFFIX))

    def _approval_id(self):
        entries = [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        approved = [e for e in entries if e.get("event") == "decision" and e.get("approval_id")]
        return approved[-1]["approval_id"]

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

    def test_publication_failure_after_reservation_blocks_approval(self):
        # Amendment 4 case 3: publication now happens strictly AFTER the durable reservation
        # is confirmed, so a publication failure publishes nothing yet permanently blocks the
        # approval. It must never be reported as an ordinary retryable error.
        self._approve()
        with mock.patch("os.link", side_effect=OSError("simulated publication failure")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "publication_failed_after_reservation")
        self.assertEqual(summary["publication"], "not_published")
        self.assertEqual(summary["reservation"], "confirmed_durable")
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertFalse(self.package.exists(), "final path must be absent when no competitor exists")
        self.assertEqual(self._stray_temps(), [], "temporary file must be cleaned")
        self.assertEqual(self._build_events(), [], "no build ledger event on failure")
        # The durable reservation survives and is unmatched, so it keeps the approval closed.
        self.assertEqual(len(self._reservations()), 1)

    def test_final_path_race_preserves_competitor_and_blocks_approval(self):
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
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        self.assertEqual(json.loads(out)["publication"], "not_published")
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

    def test_publication_failure_with_forced_unlink_failure_blocks_and_reports_both(self):
        self._approve()
        with mock.patch("os.link", side_effect=OSError("simulated publication failure")), \
                mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "publication_failed_after_reservation")
        self.assertEqual(summary["publication"], "not_published")
        self.assertEqual(summary["temp_cleanup"], "failed")
        self.assertIs(summary["manual_cleanup_required"], True)
        self.assertIs(summary["approval_blocked"], True)
        self.assertFalse(self.package.exists(), "the builder created no final path")
        self.assertTrue(self._stray_temps(), "the complete-but-unpublished temp remains")
        self.assertEqual(self._build_events(), [])
        self.assertEqual(self._cleanup_incomplete_events(), [])

    def test_race_with_forced_unlink_failure_preserves_competitor_and_blocks_approval(self):
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
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "publication_failed_after_reservation")
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


class _LostAppendHandle:
    """Stand-in for the ledger append handle that fails at exactly one persistence stage and
    never lets the appended bytes reach the file.

    Modelling a LOST append - rather than one that happens to land anyway - is deliberate:
    it is the unsafe direction the durable single-use guarantee has to cover. Opening in
    append mode truncates nothing, so closing an untouched handle leaves the ledger
    byte-identical.
    """

    def __init__(self, handle, stage):
        self._handle = handle
        self._stage = stage

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._handle.close()
        except OSError:
            pass
        return False

    def write(self, data):
        if self._stage == "write":
            raise OSError("simulated ledger write failure")
        return len(data)  # accepted, but never handed to the real file

    def flush(self):
        if self._stage == "flush":
            raise OSError("simulated ledger flush failure")
        return None

    def fileno(self):
        return self._handle.fileno()


class _LedgerAppendFailure:
    """Force a real ledger-persistence failure at exactly one stage of ``append_ledger``.

    Only the ledger's own append handle is affected: the package temporary file, the durable
    reservation, the private marker and every other file operation run normally, so the
    failure is produced at the true stage rather than by mocking an aggregate result.
    """

    STAGES = ("open", "write", "flush", "fsync")

    def __init__(self, ledger_path, stage):
        assert stage in self.STAGES
        self.ledger_path = str(ledger_path)
        self.stage = stage
        self._patches = []

    def __enter__(self):
        real_open = builtins.open
        real_fsync = os.fsync
        ledger_fds = set()

        def guarded_open(file, mode="r", *args, **kwargs):
            if str(file) != self.ledger_path or "a" not in str(mode):
                return real_open(file, mode, *args, **kwargs)
            if self.stage == "open":
                raise OSError("simulated ledger open failure")
            handle = real_open(file, mode, *args, **kwargs)
            ledger_fds.add(handle.fileno())
            if self.stage == "fsync":
                # write/flush are neutralised so the bytes never land, then fsync fails.
                return _LostAppendHandle(handle, "fsync")
            return _LostAppendHandle(handle, self.stage)

        def guarded_fsync(fd):
            if self.stage == "fsync" and fd in ledger_fds:
                raise OSError("simulated ledger fsync failure")
            return real_fsync(fd)

        self._patches = [
            mock.patch("builtins.open", guarded_open),
            mock.patch("os.fsync", guarded_fsync),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        return False


class DurableReservationTests(unittest.TestCase):
    """Amendment 4: a durable, exclusive publication reservation is confirmed BEFORE any
    final package is published, so the single-use approval boundary survives a ledger
    persistence failure.

    Every failure below is forced at its real stage. No test asserts an aggregate mock
    result, and no test touches AutoCount, the VM, n8n, SMB, real member data or any
    credential.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.form = self.tmp / "form.csv"
        self.rows = self.tmp / "decision_rows.csv"
        self.ledger = self.tmp / "member_create_uat_ledger.jsonl"
        self.package = self.tmp / "member_create_uat_package_v2.json"
        fx.write_form_csv(self.form)
        fx.write_decision_rows(self.rows)

    # ---- helpers ---- #
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = approval.main(argv)
        return code, buf.getvalue()

    def _approve(self):
        return self._run(["approve", "--reviewer", "digital", "--input", str(self.form),
                          "--decision-rows", str(self.rows), "--row-number", "2",
                          "--ledger", str(self.ledger)])

    def _build(self, extra=None):
        argv = ["build-package", "--input", str(self.form), "--decision-rows", str(self.rows),
                "--row-number", "2", "--ledger", str(self.ledger), "--package-out", str(self.package)]
        return self._run(argv + (extra or []))

    def _entries(self):
        if not self.ledger.exists():
            return []
        return [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _events_of(self, event):
        return [e for e in self._entries() if e.get("event") == event]

    def _reservations(self):
        return sorted(f for f in os.listdir(self.tmp) if f.endswith(approval.RESERVATION_SUFFIX))

    def _stray_temps(self):
        return [f for f in os.listdir(self.tmp) if "mcuat_pkg" in f]

    def _approval_id(self):
        approved = [e for e in self._entries()
                    if e.get("event") == "decision" and e.get("approval_id")]
        return approved[-1]["approval_id"]

    def _slot(self, attempt=1):
        return approval.reservation_path(self.tmp, self._approval_id(), attempt)

    def _fresh_out(self, name):
        return ["--package-out", str(self.tmp / name)]

    # ------------------------------------------------------------------ #
    # 1-4. Reservation creation and durability failures: nothing is published
    # ------------------------------------------------------------------ #
    def test_reservation_exclusive_create_failure_publishes_nothing(self):
        self._approve()
        real_os_open = os.open

        def guarded(path, flags, *args, **kwargs):
            if str(path).endswith(approval.RESERVATION_SUFFIX):
                raise OSError("simulated reservation exclusive-create failure")
            return real_os_open(path, flags, *args, **kwargs)

        with mock.patch("os.open", guarded):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_RESERVATION_INCOMPLETE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "reservation_incomplete")
        self.assertEqual(summary["reservation"], "not_created")
        self.assertEqual(summary["reservation_durability"], "unconfirmed")
        self.assertEqual(summary["publication"], "not_published")
        # Nothing published, nothing reserved, no build event, temp truthfully cleaned.
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._events_of("build"), [])
        self.assertEqual(self._stray_temps(), [])

    def test_reservation_lost_to_concurrent_owner_publishes_nothing(self):
        # A competing attempt wins the slot inside the race window (between slot survey and
        # exclusive create), so O_EXCL fails closed and no package is published.
        self._approve()
        slot = self._slot()
        real_chmod = os.chmod

        def racing_chmod(path, mode, *args, **kwargs):
            if not slot.exists():
                slot.write_text("{}\n", encoding="utf-8")  # concurrent owner takes the slot
            return real_chmod(path, mode, *args, **kwargs)

        with mock.patch("os.chmod", racing_chmod):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_RESERVATION_INCOMPLETE, out)
        self.assertEqual(json.loads(out)["reservation"], "not_created")
        self.assertFalse(self.package.exists(), "the loser of the reservation race publishes nothing")
        self.assertEqual(self._events_of("build"), [])

    def test_reservation_write_failure_is_uncertain_and_publishes_nothing(self):
        self._approve()
        real_fdopen = os.fdopen
        calls = {"n": 0}

        def guarded_fdopen(fd, *args, **kwargs):
            calls["n"] += 1
            handle = real_fdopen(fd, *args, **kwargs)
            # Call 1 is the package temporary; call 2 is the reservation.
            return _LostAppendHandle(handle, "write") if calls["n"] == 2 else handle

        with mock.patch("os.fdopen", guarded_fdopen):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_RESERVATION_INCOMPLETE, out)
        summary = json.loads(out)
        self.assertEqual(summary["reservation"], "uncertain")
        self.assertEqual(summary["publication"], "not_published")
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertFalse(self.package.exists())
        self.assertEqual(self._events_of("build"), [])
        # The uncertain entry is neither deleted nor recreated: it keeps the approval closed.
        self.assertEqual(len(self._reservations()), 1)

    def test_reservation_fsync_failure_is_uncertain_and_publishes_nothing(self):
        self._approve()
        real_fsync = os.fsync
        calls = {"n": 0}

        def guarded_fsync(fd):
            calls["n"] += 1
            # Call 1 is the package temporary's fsync; call 2 is the reservation's.
            if calls["n"] == 2:
                raise OSError("simulated reservation fsync failure")
            return real_fsync(fd)

        with mock.patch("os.fsync", guarded_fsync):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_RESERVATION_INCOMPLETE, out)
        self.assertEqual(json.loads(out)["reservation"], "uncertain")
        self.assertFalse(self.package.exists(), "no package may be published without a durable reservation")
        self.assertEqual(self._events_of("build"), [])
        self.assertEqual(len(self._reservations()), 1)

    def test_reservation_parent_directory_durability_failure_publishes_nothing(self):
        # Exercises the POSIX directory-entry fsync branch. os.O_DIRECTORY is supplied where
        # the platform lacks it so the same real code path runs everywhere.
        self._approve()
        dir_flag = getattr(os, "O_DIRECTORY", 0x10000)
        real_os_open = os.open

        def guarded(path, flags, *args, **kwargs):
            if flags & dir_flag:
                raise OSError("simulated reservation directory fsync failure")
            return real_os_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, "O_DIRECTORY", dir_flag, create=True), \
                mock.patch("os.open", guarded):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_RESERVATION_INCOMPLETE, out)
        self.assertEqual(json.loads(out)["reservation"], "uncertain")
        self.assertFalse(self.package.exists())
        self.assertEqual(self._events_of("build"), [])

    def test_unsafe_reservation_path_is_a_not_created_failure(self):
        # An unsafe reservation path must surface as a typed reservation failure, not as a
        # bare contract error that would escape the publication writer's handlers and leave
        # the operation-owned temporary file uncleaned.
        with self.assertRaises(approval.ReservationError) as caught:
            approval.write_reservation("relative_not_absolute.reservation", {})
        self.assertEqual(caught.exception.state, "not_created")

    def test_reparse_point_reservation_slot_publishes_nothing_and_cleans_temp(self):
        # The real path-safety rejection inside write_reservation runs, and the real
        # ContractError -> ReservationError conversion with it. Only the reparse-point
        # DETECTOR is forced, so the test is deterministic on platforms where an
        # unprivileged process cannot create a symlink.
        self._approve()
        slot = self._slot()
        real_detector = contract.is_reparse_point

        def detector(path):
            return str(path) == str(slot) or real_detector(path)

        with mock.patch.object(contract, "is_reparse_point", detector):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_RESERVATION_INCOMPLETE, out)
        summary = json.loads(out)
        self.assertEqual(summary["reservation"], "not_created")
        self.assertFalse(self.package.exists(), "nothing may be published without a safe reservation")
        self.assertEqual(self._events_of("build"), [])
        # The temporary file is still cleaned truthfully: the conversion keeps the failure
        # inside the publication writer's typed handling instead of escaping it.
        self.assertEqual(summary["temp_cleanup"], "complete")
        self.assertEqual(self._stray_temps(), [], "the operation-owned temporary is still cleaned")
        self.assertEqual(self._reservations(), [], "no reservation entry was created")

    def test_reservation_durability_mode_is_reported_not_assumed(self):
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        expected = "file_and_directory_fsync" if hasattr(os, "O_DIRECTORY") else "file_fsync_only"
        self.assertEqual(json.loads(out)["reservation_durability"], expected)

    # ------------------------------------------------------------------ #
    # 5-8. Clean publication, then each ledger persistence stage fails
    # ------------------------------------------------------------------ #
    def _assert_published_but_ledger_incomplete(self, out, code, *, cleanup):
        summary = json.loads(out)
        self.assertEqual(code, approval.EXIT_LEDGER_RECORD_INCOMPLETE, out)
        self.assertEqual(summary["status"], "ledger_record_incomplete")
        self.assertNotEqual(summary["status"], "ok")
        self.assertEqual(summary["event"], "none", "no build event may be claimed")
        self.assertEqual(summary["publication"], "succeeded")
        self.assertEqual(summary["ledger_record"], "incomplete")
        self.assertEqual(summary["reservation"], "confirmed_durable")
        self.assertEqual(summary["temp_cleanup"], cleanup)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        # The published package is complete, valid and recorded by a durable reservation.
        self.assertTrue(self.package.is_file(), "the published final package is preserved")
        pkg = json.loads(self.package.read_text(encoding="utf-8"))
        valid, reasons = contract.validate_package(pkg)
        self.assertTrue(valid, reasons)
        self.assertEqual(len(self._reservations()), 1)
        # No ledger build event of either kind exists.
        self.assertEqual(self._events_of("build"), [])
        self.assertEqual(self._events_of("build_cleanup_incomplete"), [])
        return summary

    def test_clean_publication_then_ledger_open_failure(self):
        self._approve()
        with _LedgerAppendFailure(self.ledger, "open"):
            code, out = self._build()
        self._assert_published_but_ledger_incomplete(out, code, cleanup="complete")
        self.assertEqual(self._stray_temps(), [], "temporary cleanup completed and is reported so")

    def test_clean_publication_then_ledger_write_failure(self):
        self._approve()
        with _LedgerAppendFailure(self.ledger, "write"):
            code, out = self._build()
        self._assert_published_but_ledger_incomplete(out, code, cleanup="complete")

    def test_clean_publication_then_ledger_flush_failure(self):
        self._approve()
        with _LedgerAppendFailure(self.ledger, "flush"):
            code, out = self._build()
        self._assert_published_but_ledger_incomplete(out, code, cleanup="complete")

    def test_clean_publication_then_ledger_fsync_failure(self):
        self._approve()
        with _LedgerAppendFailure(self.ledger, "fsync"):
            code, out = self._build()
        self._assert_published_but_ledger_incomplete(out, code, cleanup="complete")

    # ------------------------------------------------------------------ #
    # 9. Published + cleanup incomplete + each ledger persistence failure
    # ------------------------------------------------------------------ #
    def _published_cleanup_incomplete_with_ledger_failure(self, stage):
        self._approve()
        with _LedgerAppendFailure(self.ledger, stage), \
                mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            return self._build()

    def test_published_cleanup_incomplete_then_ledger_open_failure(self):
        code, out = self._published_cleanup_incomplete_with_ledger_failure("open")
        summary = self._assert_published_but_ledger_incomplete(out, code, cleanup="failed")
        # All three facts stay distinguishable.
        self.assertIs(summary["manual_cleanup_required"], True)
        self.assertTrue(summary["stale_temp_basename"].endswith(".tmp"))
        self.assertTrue(self._stray_temps(), "the operation-owned temporary alias remains")

    def test_published_cleanup_incomplete_then_ledger_write_failure(self):
        code, out = self._published_cleanup_incomplete_with_ledger_failure("write")
        summary = self._assert_published_but_ledger_incomplete(out, code, cleanup="failed")
        self.assertIs(summary["manual_cleanup_required"], True)

    def test_published_cleanup_incomplete_then_ledger_flush_failure(self):
        code, out = self._published_cleanup_incomplete_with_ledger_failure("flush")
        self._assert_published_but_ledger_incomplete(out, code, cleanup="failed")

    def test_published_cleanup_incomplete_then_ledger_fsync_failure(self):
        code, out = self._published_cleanup_incomplete_with_ledger_failure("fsync")
        self._assert_published_but_ledger_incomplete(out, code, cleanup="failed")

    # ------------------------------------------------------------------ #
    # 10-13. The unmatched reservation blocks every second build
    # ------------------------------------------------------------------ #
    def _publish_then_lose_ledger(self):
        """Reach the case-4 state: published cleanly, durable build event lost."""
        self._approve()
        with _LedgerAppendFailure(self.ledger, "write"):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_LEDGER_RECORD_INCOMPLETE, out)
        self.assertEqual(self._events_of("build"), [])
        return self.package.read_bytes()

    def _assert_reservation_blocked(self, code, out):
        self.assertEqual(code, approval.EXIT_RESERVATION_INCOMPLETE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "reservation_blocked")
        self.assertEqual(summary["reservation"], "unreconciled")
        self.assertEqual(summary["publication"], "not_attempted")
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["rebuild_blocked"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertEqual(
            [r["reservation_status"] for r in summary["unreconciled_reservations"]], ["unmatched"]
        )
        return summary

    def test_second_plain_build_after_unmatched_reservation_fails_closed(self):
        published = self._publish_then_lose_ledger()
        fresh = self.tmp / "member_create_uat_package_v2_second.json"
        code, out = self._build(extra=self._fresh_out(fresh.name))
        self._assert_reservation_blocked(code, out)
        self.assertFalse(fresh.exists(), "no second package may be minted at a fresh absent path")
        self.assertEqual(self.package.read_bytes(), published, "published bytes are unchanged")

    def test_second_rebuild_after_unmatched_reservation_fails_closed(self):
        published = self._publish_then_lose_ledger()
        fresh = self.tmp / "member_create_uat_package_v2_rebuild.json"
        code, out = self._build(extra=["--rebuild"] + self._fresh_out(fresh.name))
        self._assert_reservation_blocked(code, out)
        self.assertFalse(fresh.exists(), "--rebuild must not bypass an unmatched reservation")
        self.assertEqual(self.package.read_bytes(), published)

    def test_blocked_approval_cannot_build_at_any_fresh_path(self):
        published = self._publish_then_lose_ledger()
        for index, extra in enumerate((None, ["--rebuild"])):
            fresh = self.tmp / f"member_create_uat_package_v2_alt{index}.json"
            code, out = self._build(extra=(extra or []) + self._fresh_out(fresh.name))
            self._assert_reservation_blocked(code, out)
            self.assertFalse(fresh.exists())
        self.assertEqual(self.package.read_bytes(), published)
        self.assertEqual(self._events_of("build"), [])
        self.assertEqual(len(self._reservations()), 1, "no extra reservation slot was consumed")

    def test_uncertain_reservation_also_blocks_plain_build_and_rebuild(self):
        # An uncertain (possibly partial) reservation is malformed on re-read, and must block
        # just as firmly as an unmatched one - it is never repaired or ignored.
        self._approve()
        real_fdopen = os.fdopen
        calls = {"n": 0}

        def guarded_fdopen(fd, *args, **kwargs):
            calls["n"] += 1
            handle = real_fdopen(fd, *args, **kwargs)
            return _LostAppendHandle(handle, "write") if calls["n"] == 2 else handle

        with mock.patch("os.fdopen", guarded_fdopen):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_RESERVATION_INCOMPLETE, out)

        for index, extra in enumerate((None, ["--rebuild"])):
            fresh = self.tmp / f"member_create_uat_package_v2_unc{index}.json"
            code, out = self._build(extra=(extra or []) + self._fresh_out(fresh.name))
            self.assertEqual(code, approval.EXIT_RESERVATION_INCOMPLETE, out)
            summary = json.loads(out)
            self.assertEqual(summary["status"], "reservation_blocked")
            self.assertEqual(
                [r["reservation_status"] for r in summary["unreconciled_reservations"]], ["malformed"]
            )
            self.assertFalse(fresh.exists())

    def test_foreign_reservation_blocks_the_approval(self):
        # A well-formed reservation bound to a different approval/source record must never be
        # treated as this approval's own reconciled state.
        self._approve()
        record = {
            "schema_version": approval.RESERVATION_SCHEMA_VERSION,
            "reserved_at": "2026-07-25T00:00:00+00:00",
            "attempt": 1,
            "approval_id": "appr_" + ("0" * 32),
            "source_record_id": "srcrec_" + ("0" * 64),
            "source_fingerprint": "fp_" + ("0" * 64),
            "operation_id": "mcuat_" + ("0" * 32),
            "bound_package_payload_hash": "sha256:" + ("0" * 64),
            "package_file_name": "someone_else.json",
        }
        self._slot().write_text(json.dumps(record), encoding="utf-8")
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_RESERVATION_INCOMPLETE, out)
        self.assertEqual(
            [r["reservation_status"] for r in json.loads(out)["unreconciled_reservations"]],
            ["foreign"],
        )
        self.assertFalse(self.package.exists())

    # ------------------------------------------------------------------ #
    # 14. Competing and historical packages are never touched
    # ------------------------------------------------------------------ #
    def test_historical_and_competing_packages_unchanged_after_ledger_failure(self):
        historical = self.tmp / "member_create_uat_package_v1.json"
        historical.write_text("HISTORICAL-V1-EVIDENCE-DO-NOT-TOUCH\n", encoding="utf-8")
        competing = self.tmp / "member_create_uat_package_v2_other.json"
        competing.write_text("COMPETING-DO-NOT-TOUCH\n", encoding="utf-8")
        before = (historical.read_bytes(), competing.read_bytes())

        published = self._publish_then_lose_ledger()

        self.assertEqual((historical.read_bytes(), competing.read_bytes()), before)
        self.assertEqual(self.package.read_bytes(), published)
        # A blocked retry also leaves them untouched.
        self._build(extra=self._fresh_out("member_create_uat_package_v2_retry.json"))
        self.assertEqual((historical.read_bytes(), competing.read_bytes()), before)

    # ------------------------------------------------------------------ #
    # 15. No broad reservation, state-directory or temporary-file sweep
    # ------------------------------------------------------------------ #
    def test_no_directory_sweep_occurs_on_any_path(self):
        # Reservation slots are found by direct, bounded path lookup. Any directory listing,
        # scan or glob anywhere in the build would raise here.
        self._approve()
        other_reservation = self.tmp / (
            approval.RESERVATION_PREFIX + "appr_" + ("f" * 32) + ".1" + approval.RESERVATION_SUFFIX
        )
        other_reservation.write_text("UNRELATED-RESERVATION-DO-NOT-TOUCH\n", encoding="utf-8")
        unrelated_temp = self.tmp / ".mcuat_pkg_unrelated_sentinel.tmp"
        unrelated_temp.write_text("UNRELATED-DO-NOT-SWEEP\n", encoding="utf-8")

        def no_sweep(*args, **kwargs):
            raise AssertionError("the build must never list, scan or glob a directory")

        with mock.patch("os.listdir", no_sweep), mock.patch("os.scandir", no_sweep), \
                mock.patch("glob.glob", no_sweep), \
                mock.patch.object(Path, "iterdir", no_sweep), \
                mock.patch.object(Path, "glob", no_sweep):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        # Unrelated reservation and temporary are byte-for-byte untouched.
        self.assertEqual(other_reservation.read_text(encoding="utf-8"),
                         "UNRELATED-RESERVATION-DO-NOT-TOUCH\n")
        self.assertEqual(unrelated_temp.read_text(encoding="utf-8"), "UNRELATED-DO-NOT-SWEEP\n")

    # ------------------------------------------------------------------ #
    # 16-17. Clean success, and clean fully reconciled --rebuild
    # ------------------------------------------------------------------ #
    def test_clean_publication_emits_exactly_one_build_event_and_exits_zero(self):
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["event"], "build")
        self.assertEqual(summary["temp_cleanup"], "complete")
        self.assertEqual(summary["ledger_record"], "complete")
        self.assertEqual(summary["reservation"], "confirmed_durable")
        builds = self._events_of("build")
        self.assertEqual(len(builds), 1)
        self.assertEqual(self._events_of("build_cleanup_incomplete"), [])
        self.assertEqual(self._stray_temps(), [])
        # Exactly one reservation, reconciled to that build event by every binding field.
        self.assertEqual(len(self._reservations()), 1)
        record = json.loads(self._slot().read_text(encoding="utf-8"))
        for field in approval.RESERVATION_BINDING_FIELDS:
            self.assertEqual(record[field], builds[0][field], field)
        self.assertEqual(builds[0]["reservation_file_name"], self._slot().name)
        self.assertEqual(
            approval.reservation_reconciliation(
                self._entries(), record, self._approval_id(), record["source_record_id"]
            ),
            approval._ReservationStatus.RECONCILED_BUILD,
        )

    def test_clean_reconciled_rebuild_remains_safe(self):
        self._approve()
        self.assertEqual(self._build()[0], 0)
        first = self.package.read_bytes()
        alt = self.tmp / "member_create_uat_package_v2b.json"
        code, out = self._build(extra=["--rebuild"] + self._fresh_out(alt.name))
        self.assertEqual(code, 0, out)
        self.assertTrue(alt.is_file())
        self.assertEqual(self.package.read_bytes(), first, "the earlier package is untouched")
        # Two clean build events, each reconciled to its own reservation slot.
        builds = self._events_of("build")
        self.assertEqual(len(builds), 2)
        self.assertEqual(len(self._reservations()), 2)
        self.assertNotEqual(builds[0]["operation_id"], builds[1]["operation_id"])
        self.assertEqual(
            sorted(b["reservation_file_name"] for b in builds),
            sorted([self._slot(1).name, self._slot(2).name]),
        )

    def test_reservation_contains_no_member_data_or_credentials(self):
        self._approve()
        self.assertEqual(self._build()[0], 0)
        text = self._slot().read_text(encoding="utf-8")
        for secret in ("90000001", "6590000001", "Synthetic Alpha",
                       "synthetic.alpha@example.invalid", "2000-01-01"):
            self.assertNotIn(secret, text)
        record = json.loads(text)
        self.assertEqual(record["schema_version"], approval.RESERVATION_SCHEMA_VERSION)
        self.assertEqual(sorted(record), sorted(
            ("schema_version", "reserved_at", "attempt") + approval.RESERVATION_BINDING_FIELDS
        ))
        # No absolute path is stored: the binding is the output BASENAME only.
        self.assertEqual(record["package_file_name"], self.package.name)
        self.assertNotIn(str(self.tmp), text)

    # ------------------------------------------------------------------ #
    # 18. A fresh approval is mechanically distinguishable from the blocked one
    # ------------------------------------------------------------------ #
    def test_fresh_approval_is_distinguishable_from_blocked_prior_approval(self):
        published = self._publish_then_lose_ledger()
        blocked_id = self._approval_id()
        blocked_slot = self._slot()
        blocked_bytes = blocked_slot.read_bytes()

        # A deliberate fresh reviewer decision mints a distinct approval id, so it reserves a
        # distinct slot and is not blocked by the prior approval's unmatched reservation.
        self.assertEqual(self._approve()[0], 0)
        fresh_id = self._approval_id()
        self.assertNotEqual(fresh_id, blocked_id)

        fresh = self.tmp / "member_create_uat_package_v2_fresh.json"
        code, out = self._build(extra=self._fresh_out(fresh.name))
        self.assertEqual(code, 0, out)
        self.assertTrue(fresh.is_file())
        # The blocked approval's reservation is untouched, and the two are distinct slots.
        self.assertEqual(blocked_slot.read_bytes(), blocked_bytes)
        self.assertNotEqual(blocked_slot.name, self._slot().name)
        self.assertEqual(self.package.read_bytes(), published, "the first package is preserved")
        # The new build event binds the fresh approval only.
        builds = self._events_of("build")
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["approval_id"], fresh_id)


if __name__ == "__main__":
    unittest.main()
