import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "member_lookup_gate4_failed_attempt_recovery.py"
GITIGNORE = ROOT / ".gitignore"
README = ROOT / "README.md"
BRIDGE_RUNBOOK = ROOT / "docs" / "autocount2-automation" / "member_intake_local_lookup_bridge_runbook.md"

SOURCE_LABEL = "google_sheets_uat_gate4a"
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

FORBIDDEN_WRITE_TOKENS = [
    "Save" + "Member",
    "New" + "Member",
    "Delete" + "Member",
    "GetNext" + "MemberNo",
]

# Synthetic runtime-only auth env values generated for tests. Never real values.
SYNTHETIC_AUTH_ENV = {
    "AC2_PROBE_SERVER_NAME": "synthetic-recovery-server-value",
    "AC2_PROBE_DATABASE_NAME": "synthetic-recovery-database-value",
    "AC2_PROBE_USER_ID": "synthetic-recovery-user-value",
    "AC2_PROBE_PASSWORD": "synthetic-recovery-password-value",
}

EXPECTED_EVIDENCE_KEYS = [
    "status",
    "gate",
    "runtime_location",
    "execution_mode",
    "lookup_mode",
    "powershell_lookup_enabled",
    "recovery_generation",
    "original_failure_validated",
    "original_artifacts_modified",
    "recovery_paths_isolated",
    "auth_preflight_invoked",
    "auth_preflight_success",
    "allow_root_login_used",
    "ac2_lookup_invoked",
    "approved_batch_size",
    "queue_rows_read_count",
    "lookup_attempt_count",
    "lookup_success_count",
    "lookup_existing_member_review_count",
    "lookup_manual_review_count",
    "lookup_ready_for_create_review_count",
    "lookup_error_count",
    "recovery_result_rows_written_count",
    "recovery_processed_artifact_count",
    "recovery_failed_artifact_count",
    "member_create_or_update_invoked",
    "autocount_write_attempted",
    "direct_sql_write_attempted",
    "n8n_result_mapping_run",
    "workflow_activation",
    "scheduler_enabled",
    "public_inbound_to_ac2_host",
    "final_write_automation",
    "no_row_values_printed",
]


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


def canonical_queue_row(*, row_number=2, member_digits="778899", **overrides):
    """Build a self-consistent canonical Gate 4A queue row from synthetic digits only."""
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


def original_error_result(job, **overrides):
    """The single sanitized LOOKUP_ERROR_REVIEW row left by the original failed attempt."""
    result = {
        "job_id": job["job_id"],
        "intake_source": job["intake_source"],
        "source_reference": job["source_reference"],
        "source_row_ref": job["source_row_ref"],
        "row_number": job["row_number"],
        "state": "LOOKUP_ERROR_REVIEW",
        "status": "error",
        "authentication_success": False,
        "user_session_available": False,
        "member_command_found": False,
        "get_member_found": False,
        "submitted_member_no_status": "already_65_mobile",
        "normalized_member_no_length": 10,
        "member_exists": False,
        "member_found_by": None,
        "manual_review_required": False,
        "warning_count": 0,
        "error_code": "runtimeexception",
        "consent_status": job["consent_status"],
        "pdpa_status": "yes",
        "attempt": 0,
        "dry_run_only": True,
        "final_write_automation": False,
        "result_created_at": "fixture-time",
        "result_applied_at": None,
    }
    result.update(overrides)
    return result


def original_failed_marker(job, **overrides):
    marker = {
        "marker_type": "dead_letter",
        "job_id": job["job_id"],
        "payload_hash": job["payload_hash"],
        "state": "LOOKUP_ERROR_REVIEW",
        "status": "error",
        "error_code": "runtimeexception",
        "dry_run_only": True,
        "final_write_automation": False,
        "marked_at": "fixture-time",
    }
    marker.update(overrides)
    return marker


def success_result_row(job, *, state="READY_FOR_CREATE_REVIEW", member_exists=False):
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
        "pdpa_status": "yes",
        "attempt": 0,
        "dry_run_only": True,
        "final_write_automation": False,
        "result_created_at": "fixture-time",
        "result_applied_at": None,
    }


def auth_probe_payload(**overrides):
    payload = {
        "mode": "session-auth-probe",
        "session_probe_enabled": True,
        "static_auth_success": True,
        "instance_login_success": True,
        "instance_is_login": True,
        "authentication_success": True,
        "user_session_available": True,
        "allow_root_login_requested": False,
        "allow_root_login_set": False,
        "error": None,
    }
    payload.update(overrides)
    return payload


def lookup_payload(*, status="ok", member_exists=False, manual_review_required=False, warning_count=0):
    return {
        "status": status,
        "authentication_success": True,
        "user_session_available": True,
        "member_command_found": True,
        "get_member_found": True,
        "submitted_member_no_status": "canonical_65_mobile",
        "normalized_member_no_length": 10,
        "member_exists": member_exists,
        "member_found_by": "MemberCommand.GetMember" if member_exists else None,
        "manual_review_required": manual_review_required,
        "warning_count": warning_count,
        "error": None if status == "ok" else {"type": "mock_lookup_error"},
    }


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def write_marker_named(directory, filename, marker):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")


def write_raw_file(directory, filename, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(text, encoding="utf-8")


def nonblank_lines(path):
    p = Path(path)
    if not p.exists():
        return 0
    return len([line for line in p.read_text(encoding="utf-8").splitlines() if line.strip()])


def log_lines(path):
    p = Path(path)
    if not p.exists():
        return 0
    return len([line for line in p.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()])


def parse_pairs(text):
    return [tuple(line.split(" = ", 1)) for line in text.splitlines() if " = " in line]


def parse_evidence(text):
    return dict(parse_pairs(text))


def snapshot_original(paths):
    """Hash every original artifact (and record the exact file listing) byte-for-byte."""
    entries = {}
    for key in ("queue", "orig_results"):
        target = paths[key]
        entries[key] = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
    for key in ("orig_processed", "orig_failed"):
        directory = paths[key]
        if not directory.exists():
            entries[key] = None
            continue
        listing = {}
        for item in sorted(directory.rglob("*")):
            relative = str(item.relative_to(directory))
            listing[relative] = hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else "dir"
        entries[key] = listing
    return entries


class Gate4FailedAttemptRecoveryTests(unittest.TestCase):
    maxDiff = None

    def paths(self, tmp_path):
        return {
            "queue": tmp_path / "member_lookup_bridge_gate4_pending_queue.jsonl",
            "orig_results": tmp_path / "member_lookup_bridge_gate4_results.jsonl",
            "orig_processed": tmp_path / "member_lookup_bridge_gate4_processed",
            "orig_failed": tmp_path / "member_lookup_bridge_gate4_failed",
            "rec_results": tmp_path / "member_lookup_bridge_gate4_recovery1_results.jsonl",
            "rec_processed": tmp_path / "member_lookup_bridge_gate4_recovery1_processed",
            "rec_failed": tmp_path / "member_lookup_bridge_gate4_recovery1_failed",
        }

    def seed_original(self, tmp_path, *, row=None, result_overrides=None, marker_overrides=None):
        paths = self.paths(tmp_path)
        row = row or canonical_queue_row()
        write_jsonl(paths["queue"], [row])
        write_jsonl(paths["orig_results"], [original_error_result(row, **(result_overrides or {}))])
        write_marker_named(
            paths["orig_failed"],
            f"{row['job_id']}.json",
            original_failed_marker(row, **(marker_overrides or {})),
        )
        return paths, row

    def fake_ps(self, tmp_path, *, auth=None, auth_raw=None, lookup=None):
        """Write one dispatching fake powershell .cmd covering probe and lookup calls.

        The probe call is recognised by its -JsonOut argument (auth JSON is written to
        that path); every other call is treated as the member lookup (JSON on stdout).
        Each invocation is appended to a per-mode log, and the full argument line is
        appended to an args log so tests can prove -AllowRootLogin is never forwarded.
        """
        exe = tmp_path / "fake-powershell.cmd"
        probe_script = tmp_path / "fake-auth-probe.ps1"
        lookup_script = tmp_path / "fake-lookup-script.ps1"
        probe_script.write_text("# fake auth probe script path only\n", encoding="utf-8")
        lookup_script.write_text("# fake read-only lookup script path only\n", encoding="utf-8")
        auth_log = tmp_path / "auth-invocations.log"
        lookup_log = tmp_path / "lookup-invocations.log"
        args_log = tmp_path / "all-args.log"
        auth_text = auth_raw if auth_raw is not None else json.dumps(auth or auth_probe_payload(), sort_keys=True)
        lookup_text = json.dumps(lookup or lookup_payload(), sort_keys=True)
        lines = [
            "@echo off",
            "setlocal",
            f'>> "{args_log}" echo %*',
            'set "MODE=lookup"',
            'set "OUT="',
            ":parse",
            'if "%~1"=="" goto run',
            'if /I "%~1"=="-JsonOut" (',
            '  set "MODE=auth"',
            '  set "OUT=%~2"',
            ")",
            "shift",
            "goto parse",
            ":run",
            'if "%MODE%"=="auth" (',
            f'  >> "{auth_log}" echo auth-invoked',
            f'  > "%OUT%" echo {auth_text}',
            "  exit /b 0",
            ")",
            f'>> "{lookup_log}" echo lookup-invoked',
            f"echo {lookup_text}",
            "exit /b 0",
        ]
        exe.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        extra = [
            "--powershell-exe",
            str(exe),
            "--auth-probe-script",
            str(probe_script),
            "--lookup-script",
            str(lookup_script),
        ]
        logs = {"auth": auth_log, "lookup": lookup_log, "args": args_log}
        return extra, logs

    def args(self, paths, *, recovery_opt_in=True, confirm_opt_in=True, ps_opt_in=True, extra=None):
        arguments = []
        if recovery_opt_in:
            arguments.append("--enable-gate4-failed-attempt-recovery")
        if confirm_opt_in:
            arguments.append("--confirm-original-evidence-preserved")
        if ps_opt_in:
            arguments.append("--enable-powershell-lookup")
        arguments += [
            "--queue-jsonl",
            str(paths["queue"]),
            "--original-results-jsonl",
            str(paths["orig_results"]),
            "--original-processed-dir",
            str(paths["orig_processed"]),
            "--original-failed-dir",
            str(paths["orig_failed"]),
            "--recovery-results-jsonl",
            str(paths["rec_results"]),
            "--recovery-processed-dir",
            str(paths["rec_processed"]),
            "--recovery-failed-dir",
            str(paths["rec_failed"]),
        ]
        if extra:
            arguments += extra
        return arguments

    def run_cli(self, arguments, *, env_overrides=None, drop_env=()):
        # Start from a copy of the environment with every AC2_PROBE_* value removed so
        # host machines can never leak real values into a test, then install synthetic
        # per-run values generated by this test module.
        run_env = {key: value for key, value in os.environ.items() if not key.startswith("AC2_PROBE_")}
        run_env.update(SYNTHETIC_AUTH_ENV)
        for key in drop_env:
            run_env.pop(key, None)
        if env_overrides:
            run_env.update(env_overrides)
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + arguments,
            text=True,
            capture_output=True,
            check=False,
            env=run_env,
        )

    def assert_no_recovery_artifacts(self, paths):
        self.assertFalse(paths["rec_results"].exists())
        self.assertFalse(paths["rec_processed"].exists())
        self.assertFalse(paths["rec_failed"].exists())

    def assert_original_unchanged(self, before, paths):
        self.assertEqual(before, snapshot_original(paths))

    # --- opt-in refusals --------------------------------------------------

    def test_missing_any_recovery_opt_in_refuses_before_everything(self):
        combos = {
            "no_recovery_opt_in": {"recovery_opt_in": False},
            "no_confirm_opt_in": {"confirm_opt_in": False},
            "no_powershell_opt_in": {"ps_opt_in": False},
            "default_invocation": {"recovery_opt_in": False, "confirm_opt_in": False, "ps_opt_in": False},
        }
        for label, kwargs in combos.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self.seed_original(tmp_path)
                before = snapshot_original(paths)
                extra, logs = self.fake_ps(tmp_path)
                completed = self.run_cli(self.args(paths, extra=extra, **kwargs))
                self.assertEqual(completed.returncode, 2, label)
                evidence = parse_evidence(completed.stdout)
                self.assertEqual(evidence["status"], "refused", label)
                self.assertEqual(evidence["ac2_lookup_invoked"], "false", label)
                self.assertEqual(evidence["auth_preflight_invoked"], "false", label)
                self.assertEqual(log_lines(logs["auth"]), 0, label)
                self.assertEqual(log_lines(logs["lookup"]), 0, label)
                self.assert_no_recovery_artifacts(paths)
                self.assert_original_unchanged(before, paths)

    def test_allow_root_login_flag_is_rejected_by_the_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, _ = self.seed_original(tmp_path)
            extra, logs = self.fake_ps(tmp_path)
            completed = self.run_cli(self.args(paths, extra=extra + ["--allow-root-login"]))
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unrecognized arguments", completed.stderr)
            self.assertEqual(log_lines(logs["auth"]), 0)
            self.assertEqual(log_lines(logs["lookup"]), 0)
            self.assert_no_recovery_artifacts(paths)

    # --- recovery path isolation -------------------------------------------

    def test_recovery_paths_overlapping_original_paths_fail_closed(self):
        def collide_results(paths):
            paths["rec_results"] = paths["orig_results"]

        def collide_failed_dir(paths):
            paths["rec_failed"] = paths["orig_failed"]

        def nest_inside_original_failed(paths):
            paths["rec_processed"] = paths["orig_failed"] / "recovery_nested"

        def collide_queue(paths):
            paths["rec_results"] = paths["queue"]

        for label, mutate in {
            "results_collide": collide_results,
            "failed_dir_collide": collide_failed_dir,
            "nested_inside_original": nest_inside_original_failed,
            "queue_collide": collide_queue,
        }.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self.seed_original(tmp_path)
                before = snapshot_original(paths)
                mutate(paths)
                extra, logs = self.fake_ps(tmp_path)
                completed = self.run_cli(self.args(paths, extra=extra))
                self.assertEqual(completed.returncode, 2, label)
                evidence = parse_evidence(completed.stdout)
                self.assertEqual(evidence["status"], "needs_fix", label)
                self.assertEqual(evidence["recovery_paths_isolated"], "false", label)
                self.assertEqual(evidence["ac2_lookup_invoked"], "false", label)
                self.assertEqual(log_lines(logs["auth"]), 0, label)
                self.assertEqual(log_lines(logs["lookup"]), 0, label)
                original = self.paths(tmp_path)
                self.assert_original_unchanged(before, original)

    # --- strict original-attempt validation ---------------------------------

    def _assert_original_validation_blocks(self, tmp_path, paths, label):
        before = snapshot_original(paths)
        extra, logs = self.fake_ps(tmp_path)
        completed = self.run_cli(self.args(paths, extra=extra))
        self.assertEqual(completed.returncode, 2, label)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix", label)
        self.assertEqual(evidence["original_failure_validated"], "false", label)
        self.assertEqual(evidence["auth_preflight_invoked"], "false", label)
        self.assertEqual(evidence["ac2_lookup_invoked"], "false", label)
        self.assertEqual(evidence["lookup_attempt_count"], "0", label)
        self.assertEqual(log_lines(logs["auth"]), 0, label)
        self.assertEqual(log_lines(logs["lookup"]), 0, label)
        self.assert_no_recovery_artifacts(paths)
        self.assert_original_unchanged(before, paths)
        return evidence

    def test_missing_queue_file_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, _ = self.seed_original(tmp_path)
            paths["queue"].unlink()
            self._assert_original_validation_blocks(tmp_path, paths, "missing_queue")

    def test_invalid_queue_rows_block(self):
        row = canonical_queue_row()
        for label, queue_rows in {
            "two_rows": [row, canonical_queue_row(row_number=3)],
            "tampered_hash": [canonical_queue_row(payload_hash="fnv1a_deadbeef")],
            "nonzero_attempt": [canonical_queue_row(attempt=1)],
        }.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self.seed_original(tmp_path)
                write_jsonl(paths["queue"], queue_rows)
                self._assert_original_validation_blocks(tmp_path, paths, label)

    def test_original_result_missing_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, _ = self.seed_original(tmp_path)
            paths["orig_results"].unlink()
            self._assert_original_validation_blocks(tmp_path, paths, "missing_original_result")

    def test_original_result_multiple_rows_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row = self.seed_original(tmp_path)
            write_jsonl(paths["orig_results"], [original_error_result(row), original_error_result(row)])
            self._assert_original_validation_blocks(tmp_path, paths, "multiple_original_rows")

    def test_original_result_field_variants_block(self):
        variants = {
            "wrong_state_success_row": {"state": "READY_FOR_CREATE_REVIEW", "status": "ok", "error_code": None},
            "not_dry_run": {"dry_run_only": False},
            "final_write_automation_true": {"final_write_automation": True},
            "status_ok_with_error_state": {"status": "ok"},
            "missing_error_code": {"error_code": None},
            "wrong_pdpa": {"pdpa_status": "imported"},
            "applied_at_set": {"result_applied_at": "2026-07-13T00:00:00+00:00"},
        }
        for label, overrides in variants.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self.seed_original(tmp_path, result_overrides=overrides)
                self._assert_original_validation_blocks(tmp_path, paths, label)

    def test_original_result_identity_mismatch_blocks(self):
        variants = {
            "wrong_job_id": {"job_id": "gate4a_fnv1a_00000000"},
            "wrong_row_number": {"row_number": 9},
            "wrong_source_reference": {"source_reference": "safe-wrong-ref"},
            "wrong_attempt": {"attempt": 3},
        }
        for label, overrides in variants.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self.seed_original(tmp_path, result_overrides=overrides)
                self._assert_original_validation_blocks(tmp_path, paths, label)

    def test_original_failed_marker_missing_blocks(self):
        for label, prepare in {
            "dir_absent": lambda paths, row: (paths["orig_failed"] / f"{row['job_id']}.json").unlink()
            or paths["orig_failed"].rmdir(),
            "dir_empty": lambda paths, row: (paths["orig_failed"] / f"{row['job_id']}.json").unlink(),
        }.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, row = self.seed_original(tmp_path)
                prepare(paths, row)
                self._assert_original_validation_blocks(tmp_path, paths, label)

    def test_extra_or_bad_failed_marker_artifacts_block(self):
        row = canonical_queue_row()
        variants = {
            "extra_marker": [(f"{row['job_id']}-copy.json", json.dumps(original_failed_marker(row)))],
            "unrelated_marker": [
                ("gate4a_fnv1a_bbbbbbbb.json", json.dumps(original_failed_marker(row, job_id="gate4a_fnv1a_bbbbbbbb")))
            ],
            "malformed_marker": [(f"{row['job_id']}-x.json", "{not valid json")],
            "non_object_marker": [(f"{row['job_id']}-y.json", '"just-a-string"')],
            "temporary_marker": [(f"{row['job_id']}.json.tmp", json.dumps(original_failed_marker(row)))],
            "unexpected_file": [("operator-notes.txt", "not a marker")],
        }
        for label, files in variants.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, seeded_row = self.seed_original(tmp_path, row=row)
                for filename, text in files:
                    write_raw_file(paths["orig_failed"], filename, text)
                self._assert_original_validation_blocks(tmp_path, paths, label)

    def test_failed_marker_field_mismatches_block(self):
        variants = {
            "wrong_marker_type": {"marker_type": "processed"},
            "wrong_payload_hash": {"payload_hash": "fnv1a_00000000"},
            "missing_payload_hash": {"payload_hash": None},
            "wrong_state": {"state": "READY_FOR_CREATE_REVIEW"},
            "status_mismatch_with_result": {"status": "refused"},
            "error_code_mismatch_with_result": {"error_code": "different_code"},
            "dry_run_false": {"dry_run_only": False},
            "final_write_true": {"final_write_automation": True},
        }
        for label, overrides in variants.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self.seed_original(tmp_path, marker_overrides=overrides)
                self._assert_original_validation_blocks(tmp_path, paths, label)

    def test_original_processed_marker_unexpectedly_present_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row = self.seed_original(tmp_path)
            write_marker_named(
                paths["orig_processed"],
                f"{row['job_id']}.json",
                original_failed_marker(row, marker_type="processed", state="READY_FOR_CREATE_REVIEW"),
            )
            self._assert_original_validation_blocks(tmp_path, paths, "processed_marker_present")

    # --- one-recovery-only state machine ------------------------------------

    def _assert_recovery_state_blocks(self, tmp_path, paths, label):
        before = snapshot_original(paths)
        extra, logs = self.fake_ps(tmp_path)
        completed = self.run_cli(self.args(paths, extra=extra))
        self.assertEqual(completed.returncode, 2, label)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix", label)
        self.assertNotEqual(evidence["status"], "already_processed", label)
        self.assertEqual(evidence["auth_preflight_invoked"], "false", label)
        self.assertEqual(evidence["ac2_lookup_invoked"], "false", label)
        self.assertEqual(evidence["lookup_attempt_count"], "0", label)
        self.assertEqual(log_lines(logs["auth"]), 0, label)
        self.assertEqual(log_lines(logs["lookup"]), 0, label)
        self.assert_original_unchanged(before, paths)
        return evidence

    def test_preexisting_recovery_failed_marker_blocks_second_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row = self.seed_original(tmp_path)
            write_marker_named(paths["rec_failed"], f"{row['job_id']}.json", original_failed_marker(row))
            evidence = self._assert_recovery_state_blocks(tmp_path, paths, "recovery_failed_marker")
            self.assertEqual(evidence["recovery_failed_artifact_count"], "1")
            # The blocking marker is preserved, never cleaned or repaired.
            self.assertEqual(nonblank_lines(paths["rec_failed"] / f"{row['job_id']}.json"), 1)

    def test_partial_recovery_result_without_marker_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row = self.seed_original(tmp_path)
            write_jsonl(paths["rec_results"], [success_result_row(row)])
            evidence = self._assert_recovery_state_blocks(tmp_path, paths, "partial_result_no_marker")
            # No row was written by this run, and the preexisting partial row is preserved.
            self.assertEqual(evidence["recovery_result_rows_written_count"], "0")
            self.assertEqual(nonblank_lines(paths["rec_results"]), 1)

    def test_recovery_processed_marker_without_result_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row = self.seed_original(tmp_path)
            write_marker_named(
                paths["rec_processed"],
                f"{row['job_id']}.json",
                original_failed_marker(row, marker_type="processed", state="READY_FOR_CREATE_REVIEW", status="ok", error_code=None),
            )
            self._assert_recovery_state_blocks(tmp_path, paths, "processed_marker_no_result")

    def test_malformed_or_temp_recovery_artifacts_block(self):
        row = canonical_queue_row()
        variants = {
            "malformed_recovery_result": lambda paths: write_raw_file(
                paths["rec_results"].parent, paths["rec_results"].name, "{not valid json\n"
            ),
            "temp_recovery_marker": lambda paths: write_raw_file(
                paths["rec_processed"], f"{row['job_id']}.json.tmp", json.dumps(original_failed_marker(row))
            ),
            "unrelated_recovery_marker": lambda paths: write_raw_file(
                paths["rec_failed"], "unrelated-notes.txt", "not a marker"
            ),
        }
        for label, prepare in variants.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self.seed_original(tmp_path, row=row)
                prepare(paths)
                self._assert_recovery_state_blocks(tmp_path, paths, label)

    # --- AC2_PROBE_* presence and auth preflight -----------------------------

    def test_missing_or_blank_auth_env_blocks_before_any_process(self):
        cases = [("missing_" + name, {"drop_env": (name,)}) for name in SYNTHETIC_AUTH_ENV]
        cases += [("blank_" + name, {"env_overrides": {name: "   "}}) for name in SYNTHETIC_AUTH_ENV]
        for label, kwargs in cases:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self.seed_original(tmp_path)
                before = snapshot_original(paths)
                extra, logs = self.fake_ps(tmp_path)
                completed = self.run_cli(self.args(paths, extra=extra), **kwargs)
                self.assertEqual(completed.returncode, 2, label)
                evidence = parse_evidence(completed.stdout)
                self.assertEqual(evidence["status"], "needs_fix", label)
                self.assertEqual(evidence["original_failure_validated"], "true", label)
                self.assertEqual(evidence["auth_preflight_invoked"], "false", label)
                self.assertEqual(evidence["auth_preflight_success"], "false", label)
                self.assertEqual(evidence["ac2_lookup_invoked"], "false", label)
                self.assertEqual(log_lines(logs["auth"]), 0, label)
                self.assertEqual(log_lines(logs["lookup"]), 0, label)
                self.assert_no_recovery_artifacts(paths)
                self.assert_original_unchanged(before, paths)

    def test_auth_preflight_failure_stops_before_lookup(self):
        variants = {
            "auth_false": {"auth": auth_probe_payload(authentication_success=False)},
            "session_unavailable": {"auth": auth_probe_payload(user_session_available=False)},
            "instance_login_false": {"auth": auth_probe_payload(instance_login_success=False)},
            "probe_error": {"auth": auth_probe_payload(error={"type": "SyntheticProbeError", "message": "sanitized"})},
            "root_login_requested": {"auth": auth_probe_payload(allow_root_login_requested=True)},
            "malformed_probe_json": {"auth_raw": "{not valid json"},
        }
        for label, fake_kwargs in variants.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self.seed_original(tmp_path)
                before = snapshot_original(paths)
                extra, logs = self.fake_ps(tmp_path, **fake_kwargs)
                completed = self.run_cli(self.args(paths, extra=extra))
                self.assertEqual(completed.returncode, 2, label)
                evidence = parse_evidence(completed.stdout)
                self.assertEqual(evidence["status"], "needs_fix", label)
                self.assertEqual(evidence["auth_preflight_invoked"], "true", label)
                self.assertEqual(evidence["auth_preflight_success"], "false", label)
                self.assertEqual(evidence["ac2_lookup_invoked"], "false", label)
                self.assertEqual(evidence["lookup_attempt_count"], "0", label)
                self.assertEqual(log_lines(logs["auth"]), 1, label)
                self.assertEqual(log_lines(logs["lookup"]), 0, label)
                self.assert_no_recovery_artifacts(paths)
                self.assert_original_unchanged(before, paths)

    def test_missing_lookup_or_probe_script_is_a_precondition_failure(self):
        for label, drop in {"missing_lookup_script": "--lookup-script", "missing_probe_script": "--auth-probe-script"}.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self.seed_original(tmp_path)
                extra, logs = self.fake_ps(tmp_path)
                index = extra.index(drop)
                extra[index + 1] = str(tmp_path / "missing-script.ps1")
                completed = self.run_cli(self.args(paths, extra=extra))
                evidence = parse_evidence(completed.stdout)
                self.assertEqual(evidence["status"], "needs_fix", label)
                self.assertEqual(evidence["auth_preflight_invoked"], "false", label)
                self.assertEqual(evidence["ac2_lookup_invoked"], "false", label)
                self.assertEqual(log_lines(logs["auth"]), 0, label)
                self.assertEqual(log_lines(logs["lookup"]), 0, label)
                self.assert_no_recovery_artifacts(paths)

    # --- successful recovery --------------------------------------------------

    def _run_success(self, tmp_path, **fake_kwargs):
        paths, row = self.seed_original(tmp_path)
        before = snapshot_original(paths)
        extra, logs = self.fake_ps(tmp_path, **fake_kwargs)
        completed = self.run_cli(self.args(paths, extra=extra))
        return paths, row, before, extra, logs, completed

    def test_successful_recovery_runs_exactly_one_preflight_and_one_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row, before, extra, logs, completed = self._run_success(tmp_path)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "ok")
            self.assertEqual(evidence["gate"], "gate4_failed_attempt_recovery_lookup_only")
            self.assertEqual(evidence["lookup_mode"], "powershell")
            self.assertEqual(evidence["recovery_generation"], "1")
            self.assertEqual(evidence["original_failure_validated"], "true")
            self.assertEqual(evidence["original_artifacts_modified"], "false")
            self.assertEqual(evidence["recovery_paths_isolated"], "true")
            self.assertEqual(evidence["auth_preflight_invoked"], "true")
            self.assertEqual(evidence["auth_preflight_success"], "true")
            self.assertEqual(evidence["allow_root_login_used"], "false")
            self.assertEqual(evidence["ac2_lookup_invoked"], "true")
            self.assertEqual(evidence["queue_rows_read_count"], "1")
            self.assertEqual(evidence["lookup_attempt_count"], "1")
            self.assertEqual(evidence["lookup_success_count"], "1")
            self.assertEqual(evidence["lookup_ready_for_create_review_count"], "1")
            self.assertEqual(evidence["lookup_error_count"], "0")
            self.assertEqual(evidence["recovery_result_rows_written_count"], "1")
            self.assertEqual(evidence["recovery_processed_artifact_count"], "1")
            self.assertEqual(evidence["recovery_failed_artifact_count"], "0")
            self.assertEqual(log_lines(logs["auth"]), 1)
            self.assertEqual(log_lines(logs["lookup"]), 1)
            self.assertEqual(nonblank_lines(paths["rec_results"]), 1)
            self.assertEqual(len(list(paths["rec_processed"].glob("*.json"))), 1)
            self.assertFalse(paths["rec_failed"].exists())
            self.assert_original_unchanged(before, paths)

    def test_success_classification_variants(self):
        variants = {
            "existing_member": (lookup_payload(member_exists=True), "lookup_existing_member_review_count"),
            "manual_review": (lookup_payload(manual_review_required=True, warning_count=1), "lookup_manual_review_count"),
            "ready_for_create": (lookup_payload(), "lookup_ready_for_create_review_count"),
        }
        for label, (payload, expected_key) in variants.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, row, before, extra, logs, completed = self._run_success(tmp_path, lookup=payload)
                self.assertEqual(completed.returncode, 0, label + completed.stderr)
                evidence = parse_evidence(completed.stdout)
                self.assertEqual(evidence["status"], "ok", label)
                routing = [
                    int(evidence["lookup_existing_member_review_count"]),
                    int(evidence["lookup_manual_review_count"]),
                    int(evidence["lookup_ready_for_create_review_count"]),
                ]
                self.assertEqual(sum(routing), 1, label)
                self.assertEqual(evidence[expected_key], "1", label)
                self.assert_original_unchanged(before, paths)

    def test_lookup_error_writes_recovery_dead_letter_and_needs_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row, before, extra, logs, completed = self._run_success(
                tmp_path, lookup=lookup_payload(status="error")
            )
            self.assertEqual(completed.returncode, 2)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["auth_preflight_success"], "true")
            self.assertEqual(evidence["ac2_lookup_invoked"], "true")
            self.assertEqual(evidence["lookup_attempt_count"], "1")
            self.assertEqual(evidence["lookup_error_count"], "1")
            self.assertEqual(evidence["lookup_success_count"], "0")
            self.assertEqual(evidence["recovery_result_rows_written_count"], "1")
            self.assertEqual(evidence["recovery_processed_artifact_count"], "0")
            self.assertEqual(evidence["recovery_failed_artifact_count"], "1")
            self.assertEqual(log_lines(logs["lookup"]), 1)
            self.assertEqual(nonblank_lines(paths["rec_results"]), 1)
            self.assertEqual(len(list(paths["rec_failed"].glob("*.json"))), 1)
            self.assertFalse(paths["rec_processed"].exists())
            self.assert_original_unchanged(before, paths)

    def test_no_allow_root_login_is_ever_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row, before, extra, logs, completed = self._run_success(tmp_path)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            args_text = logs["args"].read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("AllowRootLogin", args_text)
            # Both processes were launched, so the args log is non-trivial.
            self.assertEqual(log_lines(logs["args"]), 2)
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("--allow-root-login", source)
        self.assertNotIn("-AllowRootLogin", source)

    # --- durability, idempotency, repeated invocation --------------------------

    def test_result_first_marker_second_fault_stays_needs_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row = self.seed_original(tmp_path)
            before = snapshot_original(paths)
            extra, logs = self.fake_ps(tmp_path)
            faulted = self.run_cli(
                self.args(paths, extra=extra),
                env_overrides={"GATE4_RECOVERY_TEST_FAULT_INJECT": "after_result_before_marker"},
            )
            self.assertEqual(faulted.returncode, 2)
            evidence = parse_evidence(faulted.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["recovery_result_rows_written_count"], "1")
            self.assertEqual(evidence["recovery_processed_artifact_count"], "0")
            self.assertFalse(paths["rec_processed"].exists())
            self.assertEqual(nonblank_lines(paths["rec_results"]), 1)
            self.assert_original_unchanged(before, paths)

            rerun = self.run_cli(self.args(paths, extra=extra))
            rerun_evidence = parse_evidence(rerun.stdout)
            self.assertEqual(rerun_evidence["status"], "needs_fix")
            self.assertNotEqual(rerun_evidence["status"], "already_processed")
            self.assertEqual(rerun_evidence["ac2_lookup_invoked"], "false")
            self.assertEqual(rerun_evidence["auth_preflight_invoked"], "false")
            # Exactly one lookup ever ran across both invocations.
            self.assertEqual(log_lines(logs["lookup"]), 1)
            self.assertEqual(nonblank_lines(paths["rec_results"]), 1)
            self.assert_original_unchanged(before, paths)

    def test_rerun_after_success_is_already_processed_with_zero_lookups(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row, before, extra, logs, first = self._run_success(tmp_path)
            self.assertEqual(parse_evidence(first.stdout)["status"], "ok")

            second = self.run_cli(self.args(paths, extra=extra))
            self.assertEqual(second.returncode, 0, second.stderr)
            evidence = parse_evidence(second.stdout)
            self.assertEqual(evidence["status"], "already_processed")
            self.assertEqual(evidence["lookup_attempt_count"], "0")
            self.assertEqual(evidence["lookup_success_count"], "0")
            self.assertEqual(evidence["recovery_result_rows_written_count"], "0")
            self.assertEqual(evidence["recovery_processed_artifact_count"], "1")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")
            self.assertEqual(evidence["auth_preflight_invoked"], "false")
            # Still exactly one auth preflight and one lookup across both runs.
            self.assertEqual(log_lines(logs["auth"]), 1)
            self.assertEqual(log_lines(logs["lookup"]), 1)
            self.assertEqual(nonblank_lines(paths["rec_results"]), 1)
            self.assert_original_unchanged(before, paths)

    def test_rerun_after_recovery_failure_stays_needs_fix_with_zero_lookups(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row, before, extra, logs, first = self._run_success(
                tmp_path, lookup=lookup_payload(status="error")
            )
            self.assertEqual(parse_evidence(first.stdout)["status"], "needs_fix")
            self.assertEqual(log_lines(logs["lookup"]), 1)

            second = self.run_cli(self.args(paths, extra=extra))
            self.assertEqual(second.returncode, 2)
            evidence = parse_evidence(second.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertNotEqual(evidence["status"], "already_processed")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")
            self.assertEqual(evidence["auth_preflight_invoked"], "false")
            self.assertEqual(log_lines(logs["lookup"]), 1)
            self.assertEqual(nonblank_lines(paths["rec_results"]), 1)
            self.assert_original_unchanged(before, paths)

    def test_no_recovery2_generation_exists_anywhere(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for token in ("recovery2", "recovery3", "RECOVERY_GENERATION + 1", "generation + 1"):
            self.assertNotIn(token, source)
        self.assertIn("RECOVERY_GENERATION = 1", source)

    # --- evidence shape and privacy --------------------------------------------

    def test_evidence_key_order_is_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row, before, extra, logs, completed = self._run_success(tmp_path)
            pairs = parse_pairs(completed.stdout)
            self.assertEqual([key for key, _ in pairs], EXPECTED_EVIDENCE_KEYS)

    def test_no_sensitive_values_in_output_on_any_path(self):
        member_digits = "665544"
        row = canonical_queue_row(member_digits=member_digits)
        runs = {}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, seeded_row = self.seed_original(tmp_path, row=row)
            extra, logs = self.fake_ps(tmp_path)
            runs["success"] = self.run_cli(self.args(paths, extra=extra))
            runs["rerun"] = self.run_cli(self.args(paths, extra=extra))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, seeded_row = self.seed_original(tmp_path, row=row)
            extra, logs = self.fake_ps(tmp_path, lookup=lookup_payload(status="error"))
            runs["lookup_error"] = self.run_cli(self.args(paths, extra=extra))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, seeded_row = self.seed_original(tmp_path, row=row)
            extra, logs = self.fake_ps(tmp_path, auth=auth_probe_payload(authentication_success=False))
            runs["auth_failure"] = self.run_cli(self.args(paths, extra=extra))

        forbidden = [
            member_digits,
            row["submitted_member_no_base64_utf8"],
            row["job_id"],
            row["payload_hash"],
            "submitted_member_no_base64_utf8",
            *SYNTHETIC_AUTH_ENV.values(),
        ]
        for label, completed in runs.items():
            for token in forbidden:
                self.assertNotIn(token, completed.stdout, f"{label}: {token}")
                self.assertNotIn(token, completed.stderr, f"{label}: {token}")

    # --- static guards ------------------------------------------------------------

    def test_script_reuses_gate4_validators_and_has_no_mock_route(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("import member_lookup_gate4_real_queue_lookup as gate4", source)
        for reused in (
            "gate4.inspect_results",
            "gate4.inspect_markers",
            "gate4.marker_dir_empty",
            "gate4.marker_dir_has_exactly_one_clean",
            "gate4.processed_marker_valid",
            "gate4.durable_result_fully_valid",
            "gate4.decoded_member_is_canonical_numeric",
            "gate4.canonical_identity_ok",
            "gate4.durable_append_result",
            "gate4.atomic_write_marker",
        ):
            self.assertIn(reused, source, reused)
        # No weaker parallel inspection implementation.
        self.assertNotIn("def inspect_results", source)
        self.assertNotIn("def inspect_markers", source)
        self.assertNotIn("--lookup-mode", source)
        self.assertNotIn("--fixture-mock-results", source)
        self.assertNotIn("mock_lookup", source)
        self.assertIn('LOOKUP_MODE = "powershell"', source)

    def test_script_never_deletes_or_rewrites_original_artifacts(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for token in (
            ".unlink(",
            "rmtree(",
            ".rmdir(",
            "os.remove(",
            "os.rename(",
            "shutil.move(",
            "shutil.copy",
            ".write_text(",
            ".truncate(",
        ):
            self.assertNotIn(token, source, token)

    def test_script_has_no_write_sql_activation_network_or_secret_surfaces(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, source, token)
        import re as _re

        self.assertIsNone(
            _re.search(r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b", source)
        )
        for token in (
            "scheduleTrigger",
            "cloudflared",
            "Task Scheduler",
            "win32serviceutil",
            "subprocess.Popen",
            "http://",
            "https://",
            "requests.",
            "urllib",
            "socket",
            "n8n_api",
        ):
            self.assertNotIn(token, source, token)
        # No credential store, committed secret, or password argument surface.
        for token in ("password", "Password", "secret", "token=", "connection_string"):
            self.assertNotIn(token, source.replace("AC2_PROBE_PASSWORD", ""), token)

    def test_recovery_artifacts_are_gitignored_and_wired(self):
        gitignore = GITIGNORE.read_text(encoding="utf-8")
        for pattern in [
            "member_lookup_bridge_gate4_recovery1_results.jsonl",
            "member_lookup_bridge_gate4_recovery1_processed/",
            "member_lookup_bridge_gate4_recovery1_failed/",
        ]:
            self.assertIn(pattern, gitignore)
        self.assertIn("scripts/member_lookup_gate4_failed_attempt_recovery.py", README.read_text(encoding="utf-8"))

    def test_runbook_documents_the_recovery_policy(self):
        runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")
        for phrase in [
            "## Gate 4 Failed-Attempt Recovery",
            "the four required `AC2_PROBE_*` runtime environment values were absent",
            "authentication was subsequently proven healthy",
            "byte-for-byte",
            "never deleted, renamed, moved, overwritten, truncated, appended to, or repaired",
            "Exactly one recovery attempt is authorized",
            "recovery1",
            "member_lookup_bridge_gate4_recovery1_results.jsonl",
            "member_lookup_bridge_gate4_recovery1_processed",
            "member_lookup_bridge_gate4_recovery1_failed",
            "scripts/member_lookup_gate4_failed_attempt_recovery.py",
            "--enable-gate4-failed-attempt-recovery",
            "--confirm-original-evidence-preserved",
            "--enable-powershell-lookup",
            "scripts/ac2_session_auth_probe.ps1",
            "without `-AllowRootLogin`",
            "gate = gate4_failed_attempt_recovery_lookup_only",
            "original_artifacts_modified = false",
            "recovery_paths_isolated = true",
            "auth_preflight_invoked",
            "auth_preflight_success",
            "does not approve n8n result mapping",
            "does not approve any AutoCount member create, update, or delete",
            "READY_FOR_CREATE_REVIEW` remains review-only",
        ]:
            self.assertIn(phrase, runbook, phrase)


if __name__ == "__main__":
    unittest.main()
