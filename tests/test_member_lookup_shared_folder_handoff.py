import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "member_lookup_shared_folder_handoff.py"
README = ROOT / "README.md"
GITIGNORE = ROOT / ".gitignore"
DOCS = ROOT / "docs" / "autocount2-automation"
SHARED_FOLDER_RUNBOOK = DOCS / "member_intake_shared_folder_lookup_bridge_runbook.md"
PENDING_FILENAME = "member_lookup_bridge_gate4a_pending_queue.jsonl"
RESULT_FILENAME = "member_lookup_bridge_gate5a_result_copy.jsonl"
PUBLISH_TMP_FILENAME = RESULT_FILENAME + ".tmp"

SOURCE_LABEL = "google_sheets_uat_gate4a"
MEMBER_DIGITS = "123456"

CANONICAL_HASH_FIELDS = (
    "intake_source",
    "source_reference",
    "source_row_ref",
    "row_number",
    "intake_id",
    "state",
    "submitted_member_no_base64_utf8",
    "pdpa_status",
)


def fnv1a_hex(text):
    hash_value = 0x811C9DC5
    for char in text:
        hash_value ^= ord(char)
        hash_value = (hash_value * 0x01000193) & 0xFFFFFFFF
    return format(hash_value, "08x")


def canonical_payload_hash(row):
    canonical = {field: row.get(field) for field in CANONICAL_HASH_FIELDS}
    serialized = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
    return "fnv1a_" + fnv1a_hex(serialized)


def canonical_queue_row(*, row_number=2, member_digits=MEMBER_DIGITS, **overrides):
    """Build a self-consistent canonical Gate 4A queue row (valid identity by default).

    Overrides are applied AFTER the canonical hash/job_id are computed, so overriding a
    hashed field leaves a stale hash (used for tamper tests).
    """
    encoded = base64.b64encode(member_digits.encode("utf-8")).decode("ascii")
    source_row_ref = f"row_{row_number}"
    intake_id = f"gate4a_{source_row_ref}"
    hash_source = {
        "intake_source": SOURCE_LABEL,
        "source_reference": SOURCE_LABEL,
        "source_row_ref": source_row_ref,
        "row_number": row_number,
        "intake_id": intake_id,
        "state": "PENDING_LOOKUP",
        "submitted_member_no_base64_utf8": encoded,
        "pdpa_status": "yes",
    }
    payload_hash = canonical_payload_hash(hash_source)
    row = {
        "job_id": f"gate4a_{payload_hash}",
        "intake_source": SOURCE_LABEL,
        "source_reference": SOURCE_LABEL,
        "source_row_ref": source_row_ref,
        "row_number": row_number,
        "intake_id": intake_id,
        "state": "PENDING_LOOKUP",
        "submitted_member_no_base64_utf8": encoded,
        "consent_status": "marketing_consent_not_queued",
        "pdpa_status": "yes",
        "payload_hash": payload_hash,
        "attempt": 0,
        "max_attempts": 1,
        "created_at": "safe-created-at",
        "updated_at": "safe-created-at",
        "timeout_at": "safe-timeout-at",
    }
    row.update(overrides)
    return row


def gate3c_shaped_row():
    """A row the generic Gate 3C worker contract accepts but Gate 4A must reject."""
    encoded = base64.b64encode(b"FIXTURE").decode("ascii")
    return {
        "job_id": "job-synthetic-001",
        "intake_source": "google_sheets_uat",
        "source_reference": "uat-queue-row-002",
        "source_row_ref": "row-002",
        "row_number": 2,
        "intake_id": "intake-synthetic-001",
        "state": "PENDING_LOOKUP",
        "submitted_member_no_base64_utf8": encoded,
        "consent_status": "acknowledged",
        "pdpa_status": "yes",
        "attempt": 0,
        "max_attempts": 3,
        "payload_hash": "hash-synthetic",
        "created_at": "fixture-created-at",
        "updated_at": "fixture-updated-at",
        "lease_owner": "fixture-bridge",
        "lease_expires_at": "fixture-lease-expires-at",
        "timeout_at": "fixture-timeout-at",
        "last_error_code": None,
    }


def result_row_for(job, *, state="READY_FOR_CREATE_REVIEW", member_exists=False):
    return {
        "job_id": job["job_id"],
        "intake_source": job["intake_source"],
        "source_reference": job["source_reference"],
        "source_row_ref": job["source_row_ref"],
        "row_number": job["row_number"],
        "state": state,
        "status": "ok",
        "authentication_success": True,
        "user_session_available": True,
        "member_command_found": True,
        "get_member_found": True,
        "submitted_member_no_status": "canonical_65_mobile",
        "normalized_member_no_length": 10,
        "member_exists": member_exists,
        "member_found_by": "MemberCommand.GetMember" if member_exists else None,
        "manual_review_required": False,
        "warning_count": 0,
        "error_code": None,
        "consent_status": job["consent_status"],
        "pdpa_status": job["pdpa_status"],
        "attempt": 0,
        "dry_run_only": True,
        "final_write_automation": False,
        "result_created_at": "fixture-time",
        "result_applied_at": None,
    }


def marker_json(job_id, *, marker_type="processed", state="READY_FOR_CREATE_REVIEW", payload_hash=None):
    return {
        "marker_type": marker_type,
        "job_id": job_id,
        "payload_hash": payload_hash,
        "state": state,
        "status": "ok",
        "error_code": None,
        "dry_run_only": True,
        "final_write_automation": False,
        "marked_at": "fixture-time",
    }


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def write_fake_powershell(path, *, status="ok", hits_file=None, delay=False):
    payload = {
        "status": status,
        "authentication_success": True,
        "user_session_available": True,
        "member_command_found": True,
        "get_member_found": True,
        "submitted_member_no_status": "canonical_65_mobile",
        "normalized_member_no_length": 10,
        "member_exists": False,
        "member_found_by": None,
        "manual_review_required": False,
        "warning_count": 0,
        "error": None if status == "ok" else {"type": "mock_lookup_error"},
    }
    lines = ["@echo off"]
    if hits_file is not None:
        lines.append(f'echo hit>> "{hits_file}"')
    if delay:
        lines.append("ping -n 2 127.0.0.1 >nul")
    lines.append(f"echo {json.dumps(payload, sort_keys=True)}")
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def parse_evidence(text):
    return dict(
        tuple(line.split(" = ", 1)) for line in text.splitlines() if " = " in line
    )


def nonblank_lines(path):
    p = Path(path)
    if not p.exists():
        return 0
    return len([line for line in p.read_text(encoding="utf-8").splitlines() if line.strip()])


class SharedFolderHandoffBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.share_root = base / "share"
        self.inbox = self.share_root / "inbox"
        self.outbox = self.share_root / "outbox"
        self.inbox.mkdir(parents=True)
        self.outbox.mkdir(parents=True)
        self.vm_local = base / "vm_local"
        self.vm_local.mkdir()
        self.claim = self.vm_local / "member_lookup_bridge_shared_folder_claim.json"
        self.staging = self.vm_local / "member_lookup_bridge_shared_folder_staging_results.jsonl"
        self.processed = self.vm_local / "member_lookup_bridge_shared_folder_processed"
        self.failed = self.vm_local / "member_lookup_bridge_shared_folder_failed"
        self.pending = self.inbox / PENDING_FILENAME
        self.final = self.outbox / RESULT_FILENAME
        self.hits = self.vm_local / "lookup_hits.txt"

    def fake_ps(self, *, status="ok", count_hits=True, delay=False):
        exe = self.vm_local / "fake-powershell.cmd"
        script = self.vm_local / "fake-lookup-script.ps1"
        write_fake_powershell(
            exe,
            status=status,
            hits_file=str(self.hits) if count_hits else None,
            delay=delay,
        )
        script.write_text("# fake read-only lookup script path only\n", encoding="utf-8")
        return ["--powershell-exe", str(exe), "--lookup-script", str(script)]

    def base_args(self, *, opt_in=True, ps_opt_in=True, extra=None):
        arguments = []
        if opt_in:
            arguments.append("--enable-shared-folder-handoff-review")
        if ps_opt_in:
            arguments.append("--enable-powershell-lookup")
        arguments += [
            "--share-root",
            str(self.share_root),
            "--claim-json",
            str(self.claim),
            "--staging-results-jsonl",
            str(self.staging),
            "--processed-dir",
            str(self.processed),
            "--failed-dir",
            str(self.failed),
        ]
        if extra:
            arguments += extra
        return arguments

    def run_cli(self, arguments, *, env=None):
        run_env = dict(os.environ)
        if env:
            run_env.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + arguments,
            text=True,
            capture_output=True,
            check=False,
            env=run_env,
        )

    def hit_count(self):
        return nonblank_lines(self.hits)

    def assert_no_lookup_and_no_outbox(self, evidence):
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(evidence["ac2_lookup_invoked"], "false")
        self.assertEqual(self.hit_count(), 0)
        self.assertFalse(self.final.exists())


class OptInAndModeTests(SharedFolderHandoffBase):
    def test_refuses_without_handoff_opt_in(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(self.base_args(opt_in=False, extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "refused")
        self.assert_no_lookup_and_no_outbox(evidence)
        self.assertFalse(self.claim.exists())

    def test_refuses_without_powershell_opt_in(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(self.base_args(ps_opt_in=False, extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "refused")
        self.assertEqual(evidence["powershell_lookup_enabled"], "false")
        self.assert_no_lookup_and_no_outbox(evidence)

    def test_no_mock_route_exists(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("--lookup-mode", source)
        self.assertNotIn("--fixture-mock-results", source)
        self.assertNotIn("mock_lookup", source)
        self.assertIn('LOOKUP_MODE = "powershell"', source)
        write_jsonl(self.pending, [canonical_queue_row()])
        for mock_flag in (["--lookup-mode", "mock"], ["--fixture-mock-results", str(self.staging)]):
            completed = self.run_cli(self.base_args(extra=mock_flag))
            self.assertNotEqual(completed.returncode, 0)
            self.assertNotIn("status = ok", completed.stdout)
        self.assertEqual(self.hit_count(), 0)

    def test_no_configurable_batch_size(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("--approved-batch-size", source)
        self.assertIn("APPROVED_BATCH_SIZE = 1", source)
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(self.base_args(extra=["--approved-batch-size", "2"]))
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("status = ok", completed.stdout)
        self.assertEqual(self.hit_count(), 0)


class LayoutAndPlacementTests(SharedFolderHandoffBase):
    def test_refuses_vm_local_state_inside_share(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        replacements = {
            "--claim-json": self.share_root / "claim.json",
            "--staging-results-jsonl": self.share_root / "staging.jsonl",
            "--processed-dir": self.share_root / "processed",
            "--failed-dir": self.share_root / "failed",
        }
        for flag, inside_path in replacements.items():
            arguments = self.base_args(extra=self.fake_ps())
            index = arguments.index(flag)
            arguments[index + 1] = str(inside_path)
            completed = self.run_cli(arguments)
            self.assertEqual(completed.returncode, 2, flag)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "refused", flag)
            self.assertEqual(evidence["vm_local_state_outside_share"], "false", flag)
        self.assertEqual(self.hit_count(), 0)
        self.assertFalse(self.final.exists())

    def test_needs_fix_when_share_layout_incomplete(self):
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assert_no_lookup_and_no_outbox(evidence)

        write_jsonl(self.pending, [canonical_queue_row()])
        self.outbox.rmdir()
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(self.hit_count(), 0)


class CanonicalValidationTests(SharedFolderHandoffBase):
    def assert_rejected_before_lookup(self, rows):
        write_jsonl(self.pending, rows)
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assert_no_lookup_and_no_outbox(evidence)
        return evidence

    def test_gate3c_shaped_row_rejected_before_lookup(self):
        self.assert_rejected_before_lookup([gate3c_shaped_row()])

    def test_invalid_base64_rejected(self):
        self.assert_rejected_before_lookup(
            [canonical_queue_row(submitted_member_no_base64_utf8="!!!not-base64!!!")]
        )

    def test_nonnumeric_decoded_member_rejected(self):
        encoded = base64.b64encode(b"65ABC12345").decode("ascii")
        self.assert_rejected_before_lookup(
            [canonical_queue_row(submitted_member_no_base64_utf8=encoded)]
        )

    def test_payload_hash_tamper_rejected(self):
        tampered = canonical_queue_row()
        tampered["row_number"] = 3
        tampered["source_row_ref"] = "row_3"
        tampered["intake_id"] = "gate4a_row_3"
        self.assert_rejected_before_lookup([tampered])

    def test_job_identity_tamper_rejected(self):
        self.assert_rejected_before_lookup([canonical_queue_row(job_id="gate4a_deadbeef")])

    def test_retry_metadata_drift_rejected(self):
        for overrides in ({"attempt": 1}, {"max_attempts": 3}, {"consent_status": "acknowledged"}):
            with self.subTest(overrides=overrides):
                self.setUp()
                self.assert_rejected_before_lookup([canonical_queue_row(**overrides)])

    def test_two_row_queue_rejected(self):
        self.assert_rejected_before_lookup(
            [canonical_queue_row(row_number=2), canonical_queue_row(row_number=3)]
        )


class ClaimAndConcurrencyTests(SharedFolderHandoffBase):
    def test_preexisting_claim_blocks_without_lookup(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        self.claim.write_text('{"claim_type": "exclusive_execution"}\n', encoding="utf-8")
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["preexisting_claim_detected"], "true")
        self.assertEqual(evidence["claim_acquired"], "false")
        self.assert_no_lookup_and_no_outbox(evidence)
        self.assertTrue(self.claim.exists())

    def test_concurrent_invocations_produce_at_most_one_lookup(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        arguments = [sys.executable, str(SCRIPT)] + self.base_args(
            extra=self.fake_ps(delay=True)
        )
        first = subprocess.Popen(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first_out, _ = first.communicate(timeout=120)
        second_out, _ = second.communicate(timeout=120)
        self.assertLessEqual(self.hit_count(), 1)
        ok_count = sum(1 for out in (first_out, second_out) if "status = ok" in out)
        self.assertLessEqual(ok_count, 1)
        self.assertLessEqual(nonblank_lines(self.final), 1)


class MarkerResultStateTests(SharedFolderHandoffBase):
    def seed_marker(self, job, *, marker_type="processed"):
        directory = self.processed if marker_type == "processed" else self.failed
        directory.mkdir(parents=True, exist_ok=True)
        marker = marker_json(job["job_id"], marker_type=marker_type, payload_hash=job["payload_hash"])
        (directory / f"{job['job_id']}.json").write_text(
            json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8"
        )

    def run_needs_fix_without_lookup(self):
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(self.hit_count(), 0)
        return evidence

    def test_marker_without_result_needs_fix(self):
        job = canonical_queue_row()
        write_jsonl(self.pending, [job])
        self.seed_marker(job)
        self.run_needs_fix_without_lookup()
        self.assertFalse(self.final.exists())

    def test_result_without_marker_needs_fix(self):
        job = canonical_queue_row()
        write_jsonl(self.pending, [job])
        write_jsonl(self.staging, [result_row_for(job)])
        self.run_needs_fix_without_lookup()
        self.assertFalse(self.final.exists())

    def test_corrupted_result_with_valid_marker_needs_fix(self):
        job = canonical_queue_row()
        write_jsonl(self.pending, [job])
        self.seed_marker(job)
        self.staging.write_text("{not valid json\n", encoding="utf-8")
        self.run_needs_fix_without_lookup()
        self.assertFalse(self.final.exists())

    def test_failed_marker_never_becomes_success(self):
        job = canonical_queue_row()
        write_jsonl(self.pending, [job])
        self.seed_marker(job, marker_type="dead_letter")
        write_jsonl(self.staging, [result_row_for(job)])
        self.run_needs_fix_without_lookup()
        self.assertFalse(self.final.exists())

    def complete_successful_run(self):
        job = canonical_queue_row()
        write_jsonl(self.pending, [job])
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(parse_evidence(completed.stdout)["status"], "ok")
        self.assertEqual(self.hit_count(), 1)
        return job

    def test_rerun_reports_already_processed(self):
        self.complete_successful_run()
        outbox_before = self.final.read_text(encoding="utf-8")
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "already_processed")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(evidence["ac2_lookup_invoked"], "false")
        self.assertEqual(self.hit_count(), 1)
        self.assertEqual(self.final.read_text(encoding="utf-8"), outbox_before)

    def test_stale_other_job_outbox_needs_fix(self):
        self.complete_successful_run()
        other = canonical_queue_row(row_number=9)
        stale_text = json.dumps(result_row_for(other), sort_keys=True) + "\n"
        self.final.write_text(stale_text, encoding="utf-8")
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(self.hit_count(), 1)
        self.assertEqual(self.final.read_text(encoding="utf-8"), stale_text)

    def test_preseeded_nonempty_outbox_never_overwritten(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        self.final.write_text('{"stale": true}\n', encoding="utf-8")
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(self.hit_count(), 0)
        self.assertEqual(self.final.read_text(encoding="utf-8"), '{"stale": true}\n')


class FaultInjectionTests(SharedFolderHandoffBase):
    def run_with_fault(self, point, *, env_name="SHARED_FOLDER_TEST_FAULT_INJECT"):
        write_jsonl(self.pending, [canonical_queue_row()])
        return self.run_cli(self.base_args(extra=self.fake_ps()), env={env_name: point})

    def test_fault_after_claim_blocks_rerun(self):
        completed = self.run_with_fault("after_claim")
        self.assertEqual(completed.returncode, 9)
        self.assertTrue(self.claim.exists())
        self.assertEqual(self.hit_count(), 0)
        self.assertFalse(self.final.exists())
        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        evidence = parse_evidence(rerun.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["preexisting_claim_detected"], "true")
        self.assertEqual(self.hit_count(), 0)

    def test_fault_before_publication_leaves_no_final_result(self):
        completed = self.run_with_fault("after_staging_before_publish")
        self.assertEqual(completed.returncode, 9)
        self.assertEqual(self.hit_count(), 1)
        self.assertFalse(self.final.exists())
        self.assertFalse((self.outbox / PUBLISH_TMP_FILENAME).exists())
        self.assertTrue(self.claim.exists())

        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        self.assertEqual(parse_evidence(rerun.stdout)["preexisting_claim_detected"], "true")
        self.assertEqual(self.hit_count(), 1)

        # Documented operator recovery: after reviewing the interrupted state the
        # operator removes the stale claim. The incomplete publication must still
        # be needs_fix and must never relaunch a lookup or auto-publish.
        self.claim.unlink()
        recovered = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(recovered.returncode, 2)
        evidence = parse_evidence(recovered.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(self.hit_count(), 1)
        self.assertFalse(self.final.exists())

    def test_fault_after_publication_detected_without_second_lookup(self):
        completed = self.run_with_fault("after_publish_before_claim_cleanup")
        self.assertEqual(completed.returncode, 9)
        self.assertEqual(self.hit_count(), 1)
        self.assertEqual(nonblank_lines(self.final), 1)
        self.assertTrue(self.claim.exists())

        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        self.assertEqual(parse_evidence(rerun.stdout)["preexisting_claim_detected"], "true")
        self.assertEqual(self.hit_count(), 1)

        self.claim.unlink()
        recovered = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        evidence = parse_evidence(recovered.stdout)
        self.assertEqual(evidence["status"], "already_processed")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(self.hit_count(), 1)

    def test_gate4_fault_after_result_before_marker_never_relaunches(self):
        completed = self.run_with_fault(
            "after_result_before_marker", env_name="GATE4_TEST_FAULT_INJECT"
        )
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["ac2_lookup_invoked"], "true")
        self.assertEqual(self.hit_count(), 1)
        self.assertFalse(self.final.exists())
        self.assertFalse(self.claim.exists())

        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        recovered = parse_evidence(rerun.stdout)
        self.assertEqual(recovered["status"], "needs_fix")
        self.assertEqual(recovered["lookup_attempt_count"], "0")
        self.assertEqual(self.hit_count(), 1)
        self.assertFalse(self.final.exists())


class SuccessAndEvidenceTests(SharedFolderHandoffBase):
    def test_success_publishes_exactly_one_row_atomically(self):
        job = canonical_queue_row()
        write_jsonl(self.pending, [job])
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["gate"], "shared_folder_lookup_bridge_handoff")
        self.assertEqual(evidence["transport"], "private_host_vm_shared_folder")
        self.assertEqual(evidence["n8n_runtime_location"], "main_physical_pc")
        self.assertEqual(evidence["runtime_location"], "autocount_vm_bridge_worker")
        self.assertEqual(evidence["lookup_mode"], "powershell")
        self.assertEqual(evidence["powershell_lookup_enabled"], "true")
        self.assertEqual(evidence["ac2_lookup_invoked"], "true")
        self.assertEqual(evidence["approved_batch_size"], "1")
        self.assertEqual(evidence["queue_rows_read_count"], "1")
        self.assertEqual(evidence["lookup_attempt_count"], "1")
        self.assertEqual(evidence["lookup_success_count"], "1")
        self.assertEqual(evidence["lookup_error_count"], "0")
        self.assertEqual(evidence["review_rows_written_count"], "1")
        routing = [
            int(evidence["lookup_existing_member_review_count"]),
            int(evidence["lookup_manual_review_count"]),
            int(evidence["lookup_ready_for_create_review_count"]),
        ]
        self.assertEqual(sum(routing), 1)
        self.assertEqual(evidence["claim_acquired"], "true")
        self.assertEqual(evidence["outbox_published"], "true")
        self.assertEqual(evidence["vm_local_state_outside_share"], "true")
        self.assertEqual(evidence["member_create_or_update_invoked"], "false")
        self.assertEqual(evidence["autocount_write_attempted"], "false")
        self.assertEqual(evidence["direct_sql_write_attempted"], "false")
        self.assertEqual(evidence["n8n_result_mapping_run"], "false")
        self.assertEqual(evidence["workflow_activation"], "inactive")
        self.assertEqual(evidence["queue_api_used"], "false")
        self.assertEqual(evidence["tunnel_or_reverse_proxy_used"], "false")
        self.assertEqual(evidence["webhook_used"], "false")
        self.assertEqual(evidence["public_inbound_to_ac2_host"], "false")
        self.assertEqual(evidence["final_write_automation"], "false")

        self.assertEqual(self.hit_count(), 1)
        self.assertEqual(nonblank_lines(self.final), 1)
        self.assertFalse((self.outbox / PUBLISH_TMP_FILENAME).exists())
        self.assertFalse(self.claim.exists())
        published = json.loads(self.final.read_text(encoding="utf-8").strip())
        staged = json.loads(self.staging.read_text(encoding="utf-8").strip())
        self.assertEqual(published, staged)
        self.assertEqual(published["job_id"], job["job_id"])
        self.assertNotIn("submitted_member_no_base64_utf8", published)

    def test_evidence_contains_no_row_or_member_values(self):
        job = canonical_queue_row()
        write_jsonl(self.pending, [job])
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 0)
        for secret in (
            MEMBER_DIGITS,
            job["submitted_member_no_base64_utf8"],
            job["job_id"],
            job["payload_hash"],
        ):
            self.assertNotIn(secret, completed.stdout)


class SharedFolderHandoffDocsTest(unittest.TestCase):
    def test_runbook_documents_hardened_behaviour(self):
        self.assertTrue(SHARED_FOLDER_RUNBOOK.exists())
        text = SHARED_FOLDER_RUNBOOK.read_text(encoding="utf-8")
        self.assertIn(PENDING_FILENAME, text)
        self.assertIn(RESULT_FILENAME, text)
        self.assertIn("member_lookup_shared_folder_handoff.py", text)
        self.assertIn("member_lookup_gate4_real_queue_lookup.py", text)
        self.assertIn("Manual Trigger", text)
        self.assertIn("inactive", text)
        self.assertIn("exactly one", text)
        self.assertIn("exclusive", text)
        self.assertIn("claim", text)
        self.assertIn("staging", text)
        self.assertIn("atomic", text)
        self.assertIn("recovery", text)
        for forbidden in ("member create", "direct SQL"):
            self.assertIn(forbidden, text)

    def test_script_has_no_write_path_tokens(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for token in ("Save" + "Member", "New" + "Member", "Delete" + "Member"):
            self.assertNotIn(token, text)
        self.assertNotIn("INSERT ", text)
        self.assertNotIn("UPDATE ", text)

    def test_gitignore_covers_shared_folder_local_artifacts(self):
        text = GITIGNORE.read_text(encoding="utf-8")
        self.assertIn("member_lookup_bridge_shared_folder_processed/", text)
        self.assertIn("member_lookup_bridge_shared_folder_failed/", text)
        self.assertIn("member_lookup_bridge_shared_folder_claim.json", text)
        self.assertIn("member_lookup_bridge_shared_folder_staging_results.jsonl", text)

    def test_readme_mentions_handoff_script_and_runbook(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("scripts/member_lookup_shared_folder_handoff.py", text)
        self.assertIn(
            "docs/autocount2-automation/member_intake_shared_folder_lookup_bridge_runbook.md",
            text,
        )


if __name__ == "__main__":
    unittest.main()
