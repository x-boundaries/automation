import base64
import contextlib
import importlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


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


def write_fake_powershell(directory, *, status="ok", hits_file=None, delay_seconds=0):
    """Write a platform-appropriate directly-executable fake lookup executable.

    Windows uses a .cmd batch file; POSIX uses an executable Python script with
    a shebang and 0o755 permissions. Both accept and ignore the PowerShell-style
    arguments the Gate 4 runner passes, optionally record one hit line per
    invocation, optionally sleep, and print the exact sanitized JSON response.
    """
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
    payload_json = json.dumps(payload, sort_keys=True)
    if os.name == "nt":
        exe = directory / "fake-powershell.cmd"
        lines = ["@echo off"]
        if hits_file is not None:
            lines.append(f'echo hit>> "{hits_file}"')
        if delay_seconds:
            lines.append(f"ping -n {delay_seconds + 1} 127.0.0.1 >nul")
        lines.append(f"echo {payload_json}")
        exe.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    else:
        exe = directory / "fake-powershell"
        body = [
            "#!/usr/bin/env python3",
            "import sys, time",
            "_ = sys.argv[1:]  # PowerShell-style arguments are accepted and ignored",
        ]
        if hits_file is not None:
            body.append(
                f"open({str(hits_file)!r}, 'a', encoding='utf-8').write('hit\\n')"
            )
        if delay_seconds:
            body.append(f"time.sleep({delay_seconds})")
        body.append(f"print({payload_json!r})")
        exe.write_text("\n".join(body) + "\n", encoding="utf-8")
        exe.chmod(0o755)
    return exe


def load_handoff_module():
    """Import the runner module for direct unit-level classification tests."""
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    return importlib.import_module("member_lookup_shared_folder_handoff")


def fake_lstat_result(st_mode, *, st_size=1, st_file_attributes=None):
    """A minimal lstat-shaped object for deterministic reparse-point modelling."""
    result = types.SimpleNamespace(st_mode=st_mode, st_size=st_size)
    if st_file_attributes is not None:
        result.st_file_attributes = st_file_attributes
    return result


REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


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

    def fake_ps(self, *, status="ok", count_hits=True, delay_seconds=0):
        exe = write_fake_powershell(
            self.vm_local,
            status=status,
            hits_file=str(self.hits) if count_hits else None,
            delay_seconds=delay_seconds,
        )
        script = self.vm_local / "fake-lookup-script.ps1"
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

    def make_symlink(self, link_path, target):
        try:
            os.symlink(str(target), str(link_path))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not supported in this environment")

    def make_junction(self, link_path, target):
        if os.name != "nt":
            self.skipTest("junction creation is Windows-only")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest("junction creation not supported in this environment")

    def assert_blocked_before_lookup(self, completed):
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(evidence["ac2_lookup_invoked"], "false")
        self.assertEqual(self.hit_count(), 0)
        return evidence


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
            extra=self.fake_ps(delay_seconds=1)
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

    def test_preexisting_zero_byte_final_file_blocks_lookup(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        self.final.write_text("", encoding="utf-8")
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(evidence["ac2_lookup_invoked"], "false")
        self.assertEqual(self.hit_count(), 0)
        # The empty artifact is untouched: still present, still zero bytes.
        self.assertTrue(self.final.is_file())
        self.assertEqual(self.final.stat().st_size, 0)
        self.assertFalse(self.staging.exists())

    def test_preexisting_directory_at_final_path_blocks_lookup(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        self.final.mkdir()
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(evidence["ac2_lookup_invoked"], "false")
        self.assertEqual(self.hit_count(), 0)
        self.assertTrue(self.final.is_dir())

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


class BoundaryClassificationTests(SharedFolderHandoffBase):
    def test_preexisting_temp_regular_file_blocks_lookup(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        tmp = self.outbox / PUBLISH_TMP_FILENAME
        tmp.write_text("pre-existing-temp\n", encoding="utf-8")
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assert_blocked_before_lookup(completed)
        self.assertEqual(tmp.read_text(encoding="utf-8"), "pre-existing-temp\n")
        self.assertFalse(self.final.exists())

    def test_preexisting_temp_symlink_blocks_lookup(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        tmp = self.outbox / PUBLISH_TMP_FILENAME
        self.make_symlink(tmp, self.final)
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assert_blocked_before_lookup(completed)
        # The link itself is untouched and the final name was never created.
        self.assertTrue(tmp.is_symlink())
        self.assertFalse(self.final.exists())

    def test_symlinked_pending_queue_rejected(self):
        # The target contains a perfectly valid canonical Gate 4A row, and the
        # symlinked pending entry must still never trigger a lookup.
        real_queue = self.vm_local / "real_canonical_queue.jsonl"
        write_jsonl(real_queue, [canonical_queue_row()])
        self.make_symlink(self.pending, real_queue)
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assert_blocked_before_lookup(completed)
        self.assertTrue(self.pending.is_symlink())
        self.assertFalse(self.final.exists())

    def test_broken_symlink_pending_rejected(self):
        self.make_symlink(self.pending, self.vm_local / "does-not-exist.jsonl")
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assert_blocked_before_lookup(completed)

    def test_directory_at_pending_path_rejected(self):
        self.pending.mkdir()
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assert_blocked_before_lookup(completed)
        self.assertTrue(self.pending.is_dir())


class ShareDirectoryBoundaryTests(SharedFolderHandoffBase):
    """The share root, inbox, and outbox entries must be genuine directories.

    A symlinked/junction/reparse-point directory entry is rejected fail-closed
    before the claim and before any lookup, and is never followed or resolved
    -- even when the redirect target contains a perfectly valid layout.
    """

    def build_real_share(self, base_name):
        real_share = Path(self._tmp.name) / base_name
        (real_share / "inbox").mkdir(parents=True)
        (real_share / "outbox").mkdir()
        write_jsonl(real_share / "inbox" / PENDING_FILENAME, [canonical_queue_row()])
        return real_share

    def run_with_share_root(self, share_root):
        arguments = self.base_args(extra=self.fake_ps())
        index = arguments.index("--share-root")
        arguments[index + 1] = str(share_root)
        return self.run_cli(arguments)

    def assert_blocked_before_claim(self, completed):
        self.assert_blocked_before_lookup(completed)
        self.assertFalse(self.claim.exists())
        self.assertFalse(self.staging.exists())

    def test_symlinked_share_root_rejected(self):
        real_share = self.build_real_share("real_share_for_root_link")
        link = Path(self._tmp.name) / "share_root_link"
        self.make_symlink(link, real_share)
        self.assert_blocked_before_claim(self.run_with_share_root(link))
        self.assertTrue(link.is_symlink())
        self.assertFalse((real_share / "outbox" / RESULT_FILENAME).exists())

    def test_symlinked_inbox_directory_rejected(self):
        real_inbox = Path(self._tmp.name) / "real_inbox_elsewhere"
        write_jsonl(real_inbox / PENDING_FILENAME, [canonical_queue_row()])
        self.inbox.rmdir()
        self.make_symlink(self.inbox, real_inbox)
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assert_blocked_before_claim(completed)
        self.assertTrue(self.inbox.is_symlink())
        self.assertFalse(self.final.exists())

    def test_symlinked_outbox_directory_rejected(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        real_outbox = Path(self._tmp.name) / "real_outbox_elsewhere"
        real_outbox.mkdir()
        self.outbox.rmdir()
        self.make_symlink(self.outbox, real_outbox)
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assert_blocked_before_claim(completed)
        self.assertTrue(self.outbox.is_symlink())
        # The final result name was never created through the redirect.
        self.assertFalse((real_outbox / RESULT_FILENAME).exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behaviour")
    def test_junction_share_root_rejected(self):
        real_share = self.build_real_share("real_share_for_root_junction")
        junction = Path(self._tmp.name) / "share_root_junction"
        self.make_junction(junction, real_share)
        self.assert_blocked_before_claim(self.run_with_share_root(junction))
        # The junction entry itself is untouched and was never followed.
        self.assertTrue(junction.exists())
        self.assertFalse((real_share / "outbox" / RESULT_FILENAME).exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behaviour")
    def test_junction_inbox_directory_rejected(self):
        real_inbox = Path(self._tmp.name) / "real_inbox_junction_target"
        write_jsonl(real_inbox / PENDING_FILENAME, [canonical_queue_row()])
        self.inbox.rmdir()
        self.make_junction(self.inbox, real_inbox)
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assert_blocked_before_claim(completed)
        self.assertFalse(self.final.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behaviour")
    def test_junction_outbox_directory_rejected(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        real_outbox = Path(self._tmp.name) / "real_outbox_junction_target"
        real_outbox.mkdir()
        self.outbox.rmdir()
        self.make_junction(self.outbox, real_outbox)
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assert_blocked_before_claim(completed)
        self.assertFalse((real_outbox / RESULT_FILENAME).exists())

    def run_main_in_process(self, arguments):
        """Run main() in-process so resolver invocations can be instrumented."""
        module = load_handoff_module()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module.main(arguments)
        return code, parse_evidence(buffer.getvalue())

    def forbid_resolution(self, module):
        """Patch the resolving containment helper so any call fails the test."""
        return mock.patch.object(
            module,
            "is_inside",
            side_effect=AssertionError(
                "is_inside (Path.resolve) must not run for a rejected share root"
            ),
        )

    def test_symlinked_share_root_rejected_before_resolution(self):
        real_share = self.build_real_share("real_share_for_root_link_ordering")
        link = Path(self._tmp.name) / "share_root_link_ordering"
        self.make_symlink(link, real_share)
        module = load_handoff_module()
        arguments = self.base_args(extra=self.fake_ps())
        arguments[arguments.index("--share-root") + 1] = str(link)
        with self.forbid_resolution(module):
            code, evidence = self.run_main_in_process(arguments)
        self.assertEqual(code, 2)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(evidence["ac2_lookup_invoked"], "false")
        self.assertEqual(self.hit_count(), 0)
        self.assertFalse(self.claim.exists())
        self.assertTrue(link.is_symlink())

    @unittest.skipUnless(os.name == "nt", "Windows junction behaviour")
    def test_junction_share_root_rejected_before_resolution(self):
        real_share = self.build_real_share("real_share_for_root_junction_ordering")
        junction = Path(self._tmp.name) / "share_root_junction_ordering"
        self.make_junction(junction, real_share)
        module = load_handoff_module()
        arguments = self.base_args(extra=self.fake_ps())
        arguments[arguments.index("--share-root") + 1] = str(junction)
        with self.forbid_resolution(module):
            code, evidence = self.run_main_in_process(arguments)
        self.assertEqual(code, 2)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(self.hit_count(), 0)
        self.assertFalse(self.claim.exists())
        self.assertFalse((real_share / "outbox" / RESULT_FILENAME).exists())

    def test_reparse_share_root_rejected_before_resolution_mocked(self):
        # Deterministic on hosts without symlink or junction privileges: the
        # share root lstat reports a reparse-point directory while every other
        # path behaves normally, and the resolving helper must never run.
        write_jsonl(self.pending, [canonical_queue_row()])
        module = load_handoff_module()
        real_lstat = os.lstat
        share_root_text = str(self.share_root)

        def reparse_root_lstat(path, *args, **kwargs):
            if str(path) == share_root_text:
                return fake_lstat_result(
                    stat.S_IFDIR, st_file_attributes=REPARSE_POINT_ATTRIBUTE
                )
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(module.os, "lstat", side_effect=reparse_root_lstat), \
                self.forbid_resolution(module):
            code, evidence = self.run_main_in_process(
                self.base_args(extra=self.fake_ps())
            )
        self.assertEqual(code, 2)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(self.hit_count(), 0)
        self.assertFalse(self.claim.exists())

    def test_plain_share_root_still_runs_outside_share_check(self):
        # A legitimate plain-directory share root must still perform the
        # outside-share containment validation (which resolves paths) and
        # refuse VM-local state placed inside the share.
        write_jsonl(self.pending, [canonical_queue_row()])
        module = load_handoff_module()
        arguments = self.base_args(extra=self.fake_ps())
        arguments[arguments.index("--claim-json") + 1] = str(
            self.share_root / "claim.json"
        )
        with mock.patch.object(
            module,
            "vm_local_state_outside_share",
            wraps=module.vm_local_state_outside_share,
        ) as containment:
            code, evidence = self.run_main_in_process(arguments)
        self.assertEqual(code, 2)
        self.assertTrue(containment.called)
        self.assertEqual(evidence["status"], "refused")
        self.assertEqual(evidence["vm_local_state_outside_share"], "false")
        self.assertEqual(self.hit_count(), 0)

    def forbid_filesystem_classification(self, module):
        """Patch lstat so any classification attempt fails the test."""
        return mock.patch.object(
            module.os,
            "lstat",
            side_effect=AssertionError(
                "no lstat may run for a non-normal share-root pathname"
            ),
        )

    def assert_rejected_root_evidence(self, code, evidence, supplied_root):
        self.assertEqual(code, 2)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(evidence["ac2_lookup_invoked"], "false")
        self.assertEqual(self.hit_count(), 0)
        self.assertFalse(self.claim.exists())
        self.assertFalse(self.staging.exists())
        # The sanitised aggregate evidence never exposes the supplied path.
        self.assertNotIn(str(supplied_root), "\n".join(f"{k} = {v}" for k, v in evidence.items()))

    def test_share_root_path_normality_contract(self):
        module = load_handoff_module()
        normal = module.share_root_path_is_normal
        # The real (absolute, normal) test share root is accepted as-is.
        self.assertTrue(normal(str(self.share_root)))
        # Non-normal traversal and relative forms are rejected as pure text.
        self.assertFalse(normal(str(self.share_root) + os.sep + ".."))
        self.assertFalse(normal(".."))
        self.assertFalse(normal(f"relative{os.sep}share"))
        if os.name == "nt":
            self.assertTrue(normal("C:\\xb_share\\member_lookup"))
            self.assertTrue(normal("X:\\xb_member_lookup_handoff"))
            self.assertTrue(normal("C:\\"))
            # A UNC \\host\share prefix is the trusted anchor; components
            # after it must still be normal.
            self.assertTrue(normal("\\\\host\\share\\member_lookup"))
            self.assertFalse(normal("\\\\host\\share\\member_lookup\\.."))
            self.assertFalse(normal("C:\\share\\..\\share"))
            self.assertFalse(normal("C:\\share\\."))
            self.assertFalse(normal("C:\\share\\\\double_separator"))
            self.assertFalse(normal("C:relative_to_drive"))
        else:
            self.assertTrue(normal("/srv/xb_share"))
            self.assertFalse(normal("/srv/xb_share/.."))
            self.assertFalse(normal("/srv/./xb_share"))
            self.assertFalse(normal("//srv/xb_share"))

    def test_parent_traversal_share_root_rejected_without_any_lstat(self):
        # Plain '..' traversal must be rejected as pure text: no lstat, no
        # containment resolution, no claim, no lookup.
        write_jsonl(self.pending, [canonical_queue_row()])
        module = load_handoff_module()
        unsafe_root = f"{self.share_root}{os.sep}inbox{os.sep}.."
        arguments = self.base_args(extra=self.fake_ps())
        arguments[arguments.index("--share-root") + 1] = unsafe_root
        with self.forbid_filesystem_classification(module), \
                self.forbid_resolution(module):
            code, evidence = self.run_main_in_process(arguments)
        self.assert_rejected_root_evidence(code, evidence, unsafe_root)

    def test_parent_traversal_share_root_rejected_end_to_end(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        unsafe_root = f"{self.share_root}{os.sep}inbox{os.sep}.."
        arguments = self.base_args(extra=self.fake_ps())
        arguments[arguments.index("--share-root") + 1] = unsafe_root
        completed = self.run_cli(arguments)
        self.assert_blocked_before_claim(completed)
        self.assertNotIn(unsafe_root, completed.stdout)

    def test_symlink_prefix_parent_traversal_rejected(self):
        # <symlink-to-real-share-subdirectory>/.. reaches the real share root
        # only by traversing the symlinked prefix; it must be rejected as pure
        # text before any lstat can perform that traversal.
        real_share = self.build_real_share("real_share_for_link_prefix")
        link = Path(self._tmp.name) / "link_prefix_to_share_subdir"
        self.make_symlink(link, real_share / "inbox")
        module = load_handoff_module()
        unsafe_root = f"{link}{os.sep}.."
        arguments = self.base_args(extra=self.fake_ps())
        arguments[arguments.index("--share-root") + 1] = unsafe_root
        with self.forbid_filesystem_classification(module), \
                self.forbid_resolution(module):
            code, evidence = self.run_main_in_process(arguments)
        self.assert_rejected_root_evidence(code, evidence, unsafe_root)
        self.assertFalse((real_share / "outbox" / RESULT_FILENAME).exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behaviour")
    def test_junction_prefix_parent_traversal_rejected(self):
        real_share = self.build_real_share("real_share_for_junction_prefix")
        junction = Path(self._tmp.name) / "junction_prefix_to_share_subdir"
        self.make_junction(junction, real_share / "inbox")
        module = load_handoff_module()
        unsafe_root = f"{junction}{os.sep}.."
        arguments = self.base_args(extra=self.fake_ps())
        arguments[arguments.index("--share-root") + 1] = unsafe_root
        with self.forbid_filesystem_classification(module), \
                self.forbid_resolution(module):
            code, evidence = self.run_main_in_process(arguments)
        self.assert_rejected_root_evidence(code, evidence, unsafe_root)
        self.assertFalse((real_share / "outbox" / RESULT_FILENAME).exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behaviour")
    def test_redirected_intermediate_component_rejected_junction(self):
        # No '..' involved: an intermediate junction component in an otherwise
        # normal absolute path must be detected by the component walk and the
        # entries beneath it must never be accessed.
        real_parent = Path(self._tmp.name) / "real_parent_for_intermediate"
        real_parent.mkdir()
        real_share = real_parent / "real_share"
        (real_share / "inbox").mkdir(parents=True)
        (real_share / "outbox").mkdir()
        write_jsonl(real_share / "inbox" / PENDING_FILENAME, [canonical_queue_row()])
        junction = Path(self._tmp.name) / "junction_intermediate"
        self.make_junction(junction, real_parent)
        completed = self.run_with_share_root(junction / "real_share")
        self.assert_blocked_before_claim(completed)
        self.assertFalse((real_share / "outbox" / RESULT_FILENAME).exists())

    def test_redirected_intermediate_component_rejected_mocked(self):
        # Deterministic on hosts without symlink or junction privileges: an
        # intermediate ancestor of a normal absolute share-root path reports a
        # reparse point; the walk must reject there and never lstat anything
        # at or below the share root, and the resolver must never run.
        write_jsonl(self.pending, [canonical_queue_row()])
        module = load_handoff_module()
        real_lstat = os.lstat
        redirected_ancestor = str(self.share_root.parent)
        observed_lstat_paths = []

        def reparse_ancestor_lstat(path, *args, **kwargs):
            observed_lstat_paths.append(str(path))
            if str(path) == redirected_ancestor:
                return fake_lstat_result(
                    stat.S_IFDIR, st_file_attributes=REPARSE_POINT_ATTRIBUTE
                )
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(module.os, "lstat", side_effect=reparse_ancestor_lstat), \
                self.forbid_resolution(module):
            code, evidence = self.run_main_in_process(
                self.base_args(extra=self.fake_ps())
            )
        self.assert_rejected_root_evidence(code, evidence, self.share_root)
        # The walk stopped at the redirected intermediate component: nothing
        # at or below the share root was ever classified.
        self.assertIn(redirected_ancestor, observed_lstat_paths)
        self.assertNotIn(str(self.share_root), observed_lstat_paths)

    def test_valid_share_root_components_accepted(self):
        module = load_handoff_module()
        # The real (plain, absolute, drive-style on Windows) share root passes
        # the full component walk, so legitimate supported paths keep working.
        self.assertTrue(
            module.share_root_components_are_plain_directories(self.share_root)
        )

    def test_reparse_point_directory_entry_rejected_by_classifier(self):
        # Deterministic reparse-point modelling on every platform: a directory
        # entry carrying FILE_ATTRIBUTE_REPARSE_POINT (symlink, junction, or
        # other redirection) must be rejected without being followed.
        module = load_handoff_module()
        reparse_dir = fake_lstat_result(
            stat.S_IFDIR, st_file_attributes=REPARSE_POINT_ATTRIBUTE
        )
        with mock.patch.object(module.os, "lstat", return_value=reparse_dir):
            self.assertFalse(module.directory_entry_is_plain_directory("any-share-dir"))
        plain_dir = fake_lstat_result(stat.S_IFDIR, st_file_attributes=0)
        with mock.patch.object(module.os, "lstat", return_value=plain_dir):
            self.assertTrue(module.directory_entry_is_plain_directory("any-share-dir"))


class ClaimDurabilityTests(SharedFolderHandoffBase):
    def assert_unconfirmed_claim_durability_blocks_lookup(self, fault_point):
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(
            self.base_args(extra=self.fake_ps()),
            env={"SHARED_FOLDER_TEST_FAULT_INJECT": fault_point},
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["claim_durability_unconfirmed"], "true")
        # Acquisition is complete only once the entry is durable, so an
        # unconfirmed claim never counts as acquired and never permits a lookup.
        self.assertEqual(evidence["claim_acquired"], "false")
        self.assert_no_lookup_and_no_outbox(evidence)
        self.assertFalse((self.outbox / PUBLISH_TMP_FILENAME).exists())
        self.assertFalse(self.staging.exists())
        # The indeterminate claim entry is left in place: never deleted,
        # repaired, retried, or recreated.
        self.assertTrue(self.claim.exists())

        # The retained entry blocks the next run as a preexisting claim.
        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        blocked = parse_evidence(rerun.stdout)
        self.assertEqual(blocked["status"], "needs_fix")
        self.assertEqual(blocked["preexisting_claim_detected"], "true")
        self.assertEqual(self.hit_count(), 0)
        self.assertTrue(self.claim.exists())

        # Documented operator recovery: remove the stale claim by hand, then a
        # clean run completes normally.
        self.claim.unlink()
        recovered = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        final_evidence = parse_evidence(recovered.stdout)
        self.assertEqual(final_evidence["status"], "ok")
        self.assertEqual(final_evidence["claim_durability_unconfirmed"], "false")
        self.assertEqual(self.hit_count(), 1)

    def test_unconfirmed_directory_durability_blocks_lookup(self):
        self.assert_unconfirmed_claim_durability_blocks_lookup("fail_claim_durability")

    def test_unconfirmed_claim_file_fsync_blocks_lookup(self):
        # Distinct from the parent-directory durability fault: the claim file
        # itself fails to fsync after the exclusive entry was created. The
        # failure must land in the same fail-closed durability_unconfirmed
        # path instead of escaping as a traceback.
        self.assert_unconfirmed_claim_durability_blocks_lookup("fail_claim_file_fsync")


class ClaimStateDirectoryTests(SharedFolderHandoffBase):
    """The VM-local claim-state directory is an operator setup prerequisite:
    it must already exist as a plain directory and is never created, repaired,
    or bootstrapped by the runner."""

    def run_with_claim_path(self, claim_path):
        arguments = self.base_args(extra=self.fake_ps())
        arguments[arguments.index("--claim-json") + 1] = str(claim_path)
        return self.run_cli(arguments)

    def test_missing_claim_state_parent_blocks_before_claim_and_lookup(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        missing_parent = self.vm_local / "state_dir_never_created"
        claim_path = missing_parent / "claim.json"
        completed = self.run_with_claim_path(claim_path)
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["claim_acquired"], "false")
        self.assertEqual(evidence["preexisting_claim_detected"], "false")
        self.assert_no_lookup_and_no_outbox(evidence)
        self.assertFalse(self.staging.exists())
        # No claim was created, the missing directory was NOT silently
        # created, and the private path never appears in the evidence.
        self.assertFalse(claim_path.exists())
        self.assertFalse(missing_parent.exists())
        self.assertNotIn(str(claim_path), completed.stdout)

    def test_claim_state_parent_regular_file_blocks_before_claim(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        bogus_parent = self.vm_local / "state_entry_is_a_file"
        bogus_parent.write_text("not-a-directory\n", encoding="utf-8")
        completed = self.run_with_claim_path(bogus_parent / "claim.json")
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["claim_acquired"], "false")
        self.assert_no_lookup_and_no_outbox(evidence)
        # The invalid entry is untouched.
        self.assertEqual(
            bogus_parent.read_text(encoding="utf-8"), "not-a-directory\n"
        )

    def test_valid_preexisting_claim_state_parent_supports_full_run(self):
        # The operator-provisioned state directory (created in setUp, before
        # execution) supports the complete claim/lookup/publish cycle.
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["claim_acquired"], "true")
        self.assertEqual(self.hit_count(), 1)
        self.assertFalse(self.claim.exists())


class ReparsePointClassificationTests(unittest.TestCase):
    """Deterministic Windows reparse-attribute modelling for the fixed final
    and publication-temp artifact classifier (and the pending validator it
    mirrors), independent of platform privileges."""

    def setUp(self):
        self.module = load_handoff_module()

    def classify_with(self, lstat_result):
        with mock.patch.object(self.module.os, "lstat", return_value=lstat_result):
            return self.module.classify_final_path("any-final-or-temp-path")

    def test_reparse_point_final_artifact_is_blocked(self):
        reparse_file = fake_lstat_result(
            stat.S_IFREG, st_size=42, st_file_attributes=REPARSE_POINT_ATTRIBUTE
        )
        self.assertEqual(self.classify_with(reparse_file), "blocked")

    def test_zero_byte_reparse_point_artifact_is_blocked(self):
        reparse_file = fake_lstat_result(
            stat.S_IFREG, st_size=0, st_file_attributes=REPARSE_POINT_ATTRIBUTE
        )
        self.assertEqual(self.classify_with(reparse_file), "blocked")

    def test_plain_regular_nonempty_file_still_classified_eligible(self):
        plain_file = fake_lstat_result(stat.S_IFREG, st_size=42, st_file_attributes=0)
        self.assertEqual(self.classify_with(plain_file), "regular_nonempty")

    def test_reparse_point_pending_entry_still_rejected(self):
        reparse_file = fake_lstat_result(
            stat.S_IFREG, st_size=42, st_file_attributes=REPARSE_POINT_ATTRIBUTE
        )
        with mock.patch.object(self.module.os, "lstat", return_value=reparse_file):
            self.assertFalse(
                self.module.pending_entry_is_plain_regular_file("any-pending-path")
            )


class PublicationPreflightTests(SharedFolderHandoffBase):
    def probe_paths(self):
        module = load_handoff_module()
        return (
            self.outbox / module.PREFLIGHT_PROBE_SRC_FILENAME,
            self.outbox / module.PREFLIGHT_PROBE_LINK_FILENAME,
        )

    @unittest.skipIf(os.name == "nt", "POSIX-only publication preflight")
    def test_preflight_failure_blocks_lookup_posix(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(
            self.base_args(extra=self.fake_ps()),
            env={"SHARED_FOLDER_TEST_FAULT_INJECT": "fail_publication_preflight"},
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["posix_publication_preflight"], "unsupported")
        # Zero AC2 lookups were consumed and nothing was staged or published.
        self.assert_no_lookup_and_no_outbox(evidence)
        self.assertFalse((self.outbox / PUBLISH_TMP_FILENAME).exists())
        self.assertFalse(self.staging.exists())
        # A determinate capability-unproven refusal releases the claim normally.
        self.assertFalse(self.claim.exists())

    @unittest.skipIf(os.name == "nt", "POSIX-only publication preflight")
    def test_preflight_cleanup_failure_retains_claim_and_blocks_rerun(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(
            self.base_args(extra=self.fake_ps()),
            env={"SHARED_FOLDER_TEST_FAULT_INJECT": "fail_preflight_cleanup"},
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["posix_publication_preflight"], "cleanup_unconfirmed")
        # Capability was proven but cleanup is indeterminate: zero lookups,
        # nothing staged or published, and the execution claim is deliberately
        # retained so the state cannot progress automatically.
        self.assert_no_lookup_and_no_outbox(evidence)
        self.assertFalse(self.staging.exists())
        self.assertTrue(self.claim.exists())

        # A rerun must not silently recreate probes and continue: it stays
        # blocked on the retained claim with zero lookups.
        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        blocked = parse_evidence(rerun.stdout)
        self.assertEqual(blocked["status"], "needs_fix")
        self.assertEqual(blocked["preexisting_claim_detected"], "true")
        self.assertEqual(self.hit_count(), 0)
        self.assertFalse(self.final.exists())

    @unittest.skipUnless(os.name == "nt", "Windows publication path must be unaffected")
    def test_preflight_injection_is_noop_on_windows(self):
        # The preflight never runs on Windows, so the injected preflight fault
        # must not weaken or exercise the Windows publication path.
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(
            self.base_args(extra=self.fake_ps()),
            env={"SHARED_FOLDER_TEST_FAULT_INJECT": "fail_publication_preflight"},
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["posix_publication_preflight"], "not_run")
        self.assertEqual(self.hit_count(), 1)
        self.assertEqual(nonblank_lines(self.final), 1)

    @unittest.skipIf(os.name == "nt", "POSIX-only publication preflight")
    def test_preflight_unit_success_cleans_probes_durably(self):
        module = load_handoff_module()
        probe_src, probe_link = self.probe_paths()
        self.assertEqual(
            module.posix_publication_capability_preflight(self.outbox), "capable"
        )
        self.assertFalse(probe_src.exists())
        self.assertFalse(probe_link.exists())

    def test_preflight_unit_link_unsupported_fails_closed(self):
        module = load_handoff_module()
        probe_src, probe_link = self.probe_paths()
        with mock.patch.object(
            module.os, "link", side_effect=OSError(1, "hard links unsupported")
        ):
            self.assertEqual(
                module.posix_publication_capability_preflight(self.outbox),
                "unsupported",
            )
        # Nothing is auto-repaired: the probe the preflight created stays for
        # operator inspection (and blocks the next exclusive create), and the
        # never-created link name stays absent.
        self.assertTrue(probe_src.exists())
        self.assertEqual(probe_src.stat().st_size, 0)
        self.assertFalse(probe_link.exists())

    def test_preflight_unit_first_directory_fsync_failure_is_unsupported(self):
        module = load_handoff_module()
        probe_src, probe_link = self.probe_paths()
        with mock.patch.object(
            module, "fsync_directory", side_effect=OSError(1, "dir fsync unsupported")
        ):
            self.assertEqual(
                module.posix_publication_capability_preflight(self.outbox),
                "unsupported",
            )
        # Capability was never proven; both probes remain in place for operator
        # inspection and block the next run's exclusive create.
        self.assertTrue(probe_src.exists())
        self.assertTrue(probe_link.exists())

    def test_preflight_unit_cleanup_unlink_failure_is_cleanup_unconfirmed(self):
        module = load_handoff_module()
        probe_src, probe_link = self.probe_paths()
        with mock.patch.object(module, "fsync_directory", return_value=None), \
                mock.patch.object(
                    module.os, "unlink", side_effect=OSError(1, "unlink failed")
                ):
            self.assertEqual(
                module.posix_publication_capability_preflight(self.outbox),
                "cleanup_unconfirmed",
            )
        # Nothing is retried or repaired; the probes stay for inspection.
        self.assertTrue(probe_src.exists())
        self.assertTrue(probe_link.exists())

    def test_preflight_unit_final_fsync_failure_is_cleanup_unconfirmed(self):
        module = load_handoff_module()
        probe_src, probe_link = self.probe_paths()
        with mock.patch.object(
            module,
            "fsync_directory",
            side_effect=[None, OSError(1, "final dir fsync failed")],
        ):
            self.assertEqual(
                module.posix_publication_capability_preflight(self.outbox),
                "cleanup_unconfirmed",
            )
        # This is exactly why the caller must retain the execution claim: the
        # probes were already unlinked before the durability confirmation
        # failed, so leftover probe entries cannot block the next run.
        self.assertFalse(probe_src.exists())
        self.assertFalse(probe_link.exists())

    def test_preflight_unit_file_fsync_unsupported_fails_closed(self):
        # A POSIX-mounted outbox without working file fsync must fail the
        # preflight before capability is considered proven (and therefore
        # before any Gate 4 delegation could consume a lookup).
        module = load_handoff_module()
        probe_src, probe_link = self.probe_paths()
        with mock.patch.object(
            module.os, "fsync", side_effect=OSError(1, "file fsync unsupported")
        ):
            self.assertEqual(
                module.posix_publication_capability_preflight(self.outbox),
                "unsupported",
            )
        # Nothing is deleted: the created probe stays blocking and inspectable,
        # and the link stage was never reached.
        self.assertTrue(probe_src.exists())
        self.assertFalse(probe_link.exists())

    @unittest.skipIf(os.name == "nt", "POSIX-only publication preflight")
    def test_preflight_file_fsync_failure_blocks_lookup_posix(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(
            self.base_args(extra=self.fake_ps()),
            env={"SHARED_FOLDER_TEST_FAULT_INJECT": "fail_preflight_file_fsync"},
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["posix_publication_preflight"], "unsupported")
        self.assert_no_lookup_and_no_outbox(evidence)
        self.assertFalse(self.staging.exists())
        # Determinate capability-unproven refusal: claim released, retained
        # probe blocks the rerun's exclusive create.
        self.assertFalse(self.claim.exists())
        probe_src, _ = self.probe_paths()
        self.assertTrue(probe_src.exists())

        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        blocked = parse_evidence(rerun.stdout)
        self.assertEqual(blocked["status"], "needs_fix")
        self.assertEqual(blocked["posix_publication_preflight"], "unsupported")
        self.assertEqual(self.hit_count(), 0)

    def replace_probe_on_first_directory_fsync(self, module, probe_to_replace):
        """fsync_directory stand-in that simulates the other share participant
        replacing a probe entry between capability proof and cleanup."""
        state = {"calls": 0}

        def fake_fsync_directory(directory):
            state["calls"] += 1
            if state["calls"] == 1:
                probe_to_replace.unlink()
                probe_to_replace.write_text(
                    "foreign-replacement-not-owned-by-preflight\n", encoding="utf-8"
                )

        return mock.patch.object(module, "fsync_directory", fake_fsync_directory)

    def test_preflight_unit_replaced_link_probe_never_unlinked(self):
        module = load_handoff_module()
        probe_src, probe_link = self.probe_paths()
        with self.replace_probe_on_first_directory_fsync(module, probe_link):
            self.assertEqual(
                module.posix_publication_capability_preflight(self.outbox),
                "cleanup_unconfirmed",
            )
        # The replacement entry is byte-for-byte untouched: identity
        # verification refused to unlink an object the preflight did not
        # create, and nothing was repaired, replaced, renamed, or deleted.
        self.assertEqual(
            probe_link.read_text(encoding="utf-8"),
            "foreign-replacement-not-owned-by-preflight\n",
        )
        # The owned source probe was never reached and stays in place.
        self.assertTrue(probe_src.exists())
        self.assertEqual(probe_src.stat().st_size, 0)

    def test_preflight_unit_replaced_src_probe_never_unlinked(self):
        module = load_handoff_module()
        probe_src, probe_link = self.probe_paths()
        with self.replace_probe_on_first_directory_fsync(module, probe_src):
            self.assertEqual(
                module.posix_publication_capability_preflight(self.outbox),
                "cleanup_unconfirmed",
            )
        # The owned link probe was legitimately removed first; the foreign
        # replacement at the source name is byte-for-byte untouched.
        self.assertFalse(probe_link.exists())
        self.assertEqual(
            probe_src.read_text(encoding="utf-8"),
            "foreign-replacement-not-owned-by-preflight\n",
        )

    @unittest.skipIf(os.name == "nt", "POSIX-only publication preflight")
    def test_preflight_replaced_probe_retains_claim_and_blocks_rerun_posix(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        module = load_handoff_module()
        probe_src, probe_link = self.probe_paths()
        buffer = io.StringIO()
        with self.replace_probe_on_first_directory_fsync(module, probe_link):
            with contextlib.redirect_stdout(buffer):
                code = module.main(self.base_args(extra=self.fake_ps()))
        evidence = parse_evidence(buffer.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["posix_publication_preflight"], "cleanup_unconfirmed")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(self.hit_count(), 0)
        # The foreign replacement is untouched and the claim is retained.
        self.assertEqual(
            probe_link.read_text(encoding="utf-8"),
            "foreign-replacement-not-owned-by-preflight\n",
        )
        self.assertTrue(self.claim.exists())

        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        blocked = parse_evidence(rerun.stdout)
        self.assertEqual(blocked["status"], "needs_fix")
        self.assertEqual(blocked["preexisting_claim_detected"], "true")
        self.assertEqual(self.hit_count(), 0)

    def test_preflight_unit_preexisting_probe_entry_fails_closed(self):
        module = load_handoff_module()
        probe_src, probe_link = self.probe_paths()
        probe_src.write_text("not-created-by-preflight\n", encoding="utf-8")
        self.assertEqual(
            module.posix_publication_capability_preflight(self.outbox), "unsupported"
        )
        # The pre-existing entry was not created by the preflight and is never
        # cleaned up, truncated, or followed.
        self.assertEqual(
            probe_src.read_text(encoding="utf-8"), "not-created-by-preflight\n"
        )
        self.assertFalse(probe_link.exists())


class CorruptedEncodingTests(SharedFolderHandoffBase):
    def complete_successful_run(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(self.hit_count(), 1)

    def assert_controlled_needs_fix(self, completed):
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        # Determinate corrupted state: the claim is released normally.
        self.assertFalse(self.claim.exists())
        self.assertEqual(self.hit_count(), 1)
        return evidence

    def test_non_utf8_final_outbox_is_controlled_needs_fix(self):
        self.complete_successful_run()
        corrupt = b"\xff\xfe\x00garbage\xff"
        self.final.write_bytes(corrupt)
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assert_controlled_needs_fix(completed)
        self.assertEqual(self.final.read_bytes(), corrupt)

    def test_non_utf8_staging_is_controlled_needs_fix(self):
        self.complete_successful_run()
        corrupt = b"\xff\xfe\xfdnot-utf8\xff"
        self.staging.write_bytes(corrupt)
        completed = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assert_controlled_needs_fix(completed)
        self.assertEqual(self.staging.read_bytes(), corrupt)


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

    def test_claim_release_failure_never_reports_success(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(
            self.base_args(extra=self.fake_ps()),
            env={"SHARED_FOLDER_TEST_FAULT_INJECT": "fail_claim_release"},
        )
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["claim_release_failed"], "true")
        self.assertNotIn("status = ok", completed.stdout)
        self.assertNotIn("status = already_processed", completed.stdout)
        # The lookup and publication themselves completed; only the release failed.
        self.assertEqual(evidence["outbox_published"], "true")
        self.assertEqual(self.hit_count(), 1)
        self.assertEqual(nonblank_lines(self.final), 1)
        self.assertTrue(self.claim.exists())

        # The retained claim blocks the next invocation without a second lookup.
        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        blocked = parse_evidence(rerun.stdout)
        self.assertEqual(blocked["status"], "needs_fix")
        self.assertEqual(blocked["preexisting_claim_detected"], "true")
        self.assertEqual(self.hit_count(), 1)

        # Documented operator recovery: remove the stale claim by hand, then the
        # completed published run is recognised idempotently with zero lookups.
        self.claim.unlink()
        recovered = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        final_evidence = parse_evidence(recovered.stdout)
        self.assertEqual(final_evidence["status"], "already_processed")
        self.assertEqual(final_evidence["claim_release_failed"], "false")
        self.assertEqual(self.hit_count(), 1)

    def test_publication_durability_failure_never_reports_ok(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        completed = self.run_cli(
            self.base_args(extra=self.fake_ps()),
            env={"SHARED_FOLDER_TEST_FAULT_INJECT": "fail_publication_durability"},
        )
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["outbox_published"], "false")
        self.assertNotIn("status = ok", completed.stdout)
        # The lookup ran once; the final name was never committed; the temp file
        # and staged result stay for operator diagnosis (no silent repair).
        self.assertEqual(self.hit_count(), 1)
        self.assertFalse(self.final.exists())
        self.assertTrue((self.outbox / PUBLISH_TMP_FILENAME).exists())
        self.assertEqual(nonblank_lines(self.staging), 1)
        # The claim is deliberately retained after an unconfirmed commit so the
        # state cannot progress automatically.
        self.assertTrue(self.claim.exists())

        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        blocked = parse_evidence(rerun.stdout)
        self.assertEqual(blocked["status"], "needs_fix")
        self.assertEqual(blocked["preexisting_claim_detected"], "true")
        self.assertEqual(self.hit_count(), 1)

    def test_late_outbox_write_is_never_overwritten(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        arguments = [sys.executable, str(SCRIPT)] + self.base_args(
            extra=self.fake_ps(delay_seconds=3)
        )
        process = subprocess.Popen(
            arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        # Wait until the lookup has started (hit recorded), then simulate the
        # other share participant creating the fixed result filename mid-lookup,
        # after the runner's pre-lookup outbox inspection.
        deadline = time.time() + 60
        while self.hit_count() == 0 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.hit_count(), 1)
        late_text = '{"late": "written-by-other-share-participant"}\n'
        self.final.write_text(late_text, encoding="utf-8")

        out, err = process.communicate(timeout=120)
        self.assertEqual(process.returncode, 2, out + err)
        evidence = parse_evidence(out)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["outbox_published"], "false")
        # The late file is byte-for-byte unchanged and the staged result stays
        # available for operator diagnosis.
        self.assertEqual(self.final.read_text(encoding="utf-8"), late_text)
        self.assertEqual(nonblank_lines(self.staging), 1)
        self.assertEqual(self.hit_count(), 1)

        # Rerun: the mismatched outbox stays needs_fix with no second lookup.
        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        self.assertEqual(parse_evidence(rerun.stdout)["status"], "needs_fix")
        self.assertEqual(self.hit_count(), 1)
        self.assertEqual(self.final.read_text(encoding="utf-8"), late_text)

    def test_temp_entry_created_before_exclusive_creation_fails_closed(self):
        write_jsonl(self.pending, [canonical_queue_row()])
        arguments = [sys.executable, str(SCRIPT)] + self.base_args(
            extra=self.fake_ps(delay_seconds=3)
        )
        process = subprocess.Popen(
            arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        # Wait until the lookup has started, then create the fixed temp entry
        # (after the runner's pre-lookup temp classification) so the exclusive
        # create must refuse it.
        deadline = time.time() + 60
        while self.hit_count() == 0 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.hit_count(), 1)
        tmp = self.outbox / PUBLISH_TMP_FILENAME
        late_temp = "late-temp-entry\n"
        tmp.write_text(late_temp, encoding="utf-8")

        out, err = process.communicate(timeout=120)
        self.assertEqual(process.returncode, 2, out + err)
        self.assertNotIn("Traceback", err)
        evidence = parse_evidence(out)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["outbox_published"], "false")
        # The pre-existing temp entry is byte-for-byte unchanged (no truncation,
        # no follow), and the final result was never exposed.
        self.assertEqual(tmp.read_text(encoding="utf-8"), late_temp)
        self.assertFalse(self.final.exists())
        self.assertEqual(nonblank_lines(self.staging), 1)
        self.assertEqual(self.hit_count(), 1)

        # Rerun: the pre-lookup temp classification blocks before Gate 4 and no
        # second lookup occurs.
        rerun = self.run_cli(self.base_args(extra=self.fake_ps()))
        self.assertEqual(rerun.returncode, 2)
        blocked = parse_evidence(rerun.stdout)
        self.assertEqual(blocked["status"], "needs_fix")
        self.assertEqual(blocked["lookup_attempt_count"], "0")
        self.assertEqual(self.hit_count(), 1)
        self.assertEqual(tmp.read_text(encoding="utf-8"), late_temp)

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
        self.assertEqual(evidence["claim_durability_unconfirmed"], "false")
        self.assertEqual(evidence["claim_release_failed"], "false")
        self.assertEqual(
            evidence["posix_publication_preflight"],
            "not_run" if os.name == "nt" else "capable",
        )
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
        self.assertIn("claim_release_failed", text)
        self.assertIn("no-replace", text)
        self.assertIn("never overwritten", text)
        self.assertIn("MoveFileExW", text)
        self.assertIn("write-through", text)
        self.assertIn("directory fsync", text)
        self.assertIn("zero-byte", text)
        self.assertIn("non-UTF-8", text)
        self.assertIn("reparse", text)
        self.assertIn("junction", text)
        self.assertIn("intermediate", text)
        self.assertIn("anchor", text)
        self.assertIn("preflight", text)
        self.assertIn("claim_durability_unconfirmed", text)
        self.assertIn("posix_publication_preflight", text)
        self.assertIn("cleanup_unconfirmed", text)
        self.assertIn("device/inode", text)
        self.assertIn("setup prerequisite", text)
        self.assertIn("file fsync", text)
        self.assertIn("O_EXCL", text)
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
        # The retained publication temp artifact and the preflight probe names
        # are ignored both at the generic locations and under repo-local
        # autocount_outputs/**/ paths, matching the other bridge artifacts.
        for name in (
            "member_lookup_bridge_gate5a_result_copy.jsonl.tmp",
            "member_lookup_bridge_gate5a_result_copy.jsonl.preflight_probe_src.tmp",
            "member_lookup_bridge_gate5a_result_copy.jsonl.preflight_probe_link.tmp",
        ):
            self.assertIn(f"\n{name}\n", text)
            self.assertIn(f"\nautocount_outputs/**/{name}\n", text)

    def test_readme_mentions_handoff_script_and_runbook(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("scripts/member_lookup_shared_folder_handoff.py", text)
        self.assertIn(
            "docs/autocount2-automation/member_intake_shared_folder_lookup_bridge_runbook.md",
            text,
        )


if __name__ == "__main__":
    unittest.main()
