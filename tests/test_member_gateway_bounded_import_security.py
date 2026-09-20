"""Direct offline security regressions for the bounded member-gateway importer."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "n8n-workflows/scripts/import-member-forms-gateway-bounded.ps1"
CANONICAL_WORKFLOW = ROOT / "n8n-workflows/member_forms_gateway_ingest.workflow.json"
MANIFEST_TEMPLATE = ROOT / "config/member_forms_gateway_bounded_import.v2.template.json"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes().decode("utf-8"))


class MemberGatewayBoundedImportSecurityTests(unittest.TestCase):
    TEST_GATEWAY_ORIGIN = "https://reviewed.gateway.internal:443"

    @classmethod
    def setUpClass(cls) -> None:
        cls.pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if not cls.pwsh:
            raise unittest.SkipTest("PowerShell is required for bounded importer tests")

    def setUp(self) -> None:
        self.case_root = Path(tempfile.mkdtemp(prefix="bounded-import-security-"))
        self.addCleanup(lambda: shutil.rmtree(self.case_root, ignore_errors=True))
        self.operations_root = ".n8n-local/member-gateway-bounded-import/operations"
        self.private_manifest_path = ROOT / ".n8n-local/member-gateway-bounded-import" / f"test-binding-{uuid4().hex}.json"
        self.manifest_path = self.private_manifest_path
        self.addCleanup(lambda path=self.private_manifest_path: path.unlink(missing_ok=True))
        self.fixture_path = self.case_root / "fixture.json"
        self.manifest = self._make_manifest()
        self.repository_identity = self._repository_identity()
        _write_json(self.manifest_path, self.manifest)

    def _make_manifest(self) -> dict[str, Any]:
        manifest = copy.deepcopy(_read_json(MANIFEST_TEMPLATE))
        manifest["project"]["id"] = "project-security-001"
        manifest["workflow"]["id"] = "workflow-security-001"
        manifest["form"]["id"] = "form-security-001"
        manifest["endpoints"]["forms_responses"] = "https://forms.googleapis.com:443/v1/forms/form-security-001/responses"
        manifest["security"]["approved_gateway_origin"] = self.TEST_GATEWAY_ORIGIN
        manifest["endpoints"]["source_cursor"] = f"{self.TEST_GATEWAY_ORIGIN}/v1/source/cursor-security"
        manifest["endpoints"]["gateway_ingest"] = f"{self.TEST_GATEWAY_ORIGIN}/v1/source-events-security"
        manifest["endpoints"]["page_checkpoint"] = f"{self.TEST_GATEWAY_ORIGIN}/v1/source/cursor-security/page"
        manifest["question_mapping"] = {
            "name": "question-name-001",
            "phone": "question-phone-001",
            "email": "question-email-001",
            "birthday_month": "question-birthday-001",
            "marketing_consent": "question-consent-001",
            "pdpa_acknowledged": "question-pdpa-001",
        }
        manifest["credential_roles"]["google_forms_oauth"]["credential_name"] = "google_forms_test_credential"
        manifest["credential_roles"]["google_forms_oauth"]["credential_id"] = "google-credential-id-001"
        manifest["credential_roles"]["gateway_bearer"]["credential_name"] = "gateway_test_credential"
        manifest["credential_roles"]["gateway_bearer"]["credential_id"] = "gateway-credential-id-001"
        watermark = "2026-09-18T00:00:00Z"
        manifest["cursor_expectation"]["watermark"] = watermark
        manifest["cursor_expectation"]["watermark_digest"] = hashlib.sha256(
            f"xb.member.gateway.watermark.v1|google_forms|member_registration|member-intake.v1|{watermark}".encode("utf-8")
        ).hexdigest()
        return manifest

    def _make_cursor(self) -> dict[str, Any]:
        return {
            "schema_version": "xb.member.gateway.source_cursor.v1",
            "source_system": "google_forms",
            "form_alias": "member_registration",
            "mapping_version": "member-intake.v1",
            "watermark": "2026-09-18T00:00:00Z",
            "last_admitted_create_time": None,
            "last_admitted_response_id": None,
            "state_version": 0,
            "scan_lower_bound": "2026-09-18T00:00:00Z",
            "resume_page_token": None,
            "initial_window_admission_count": 0,
        }

    @staticmethod
    def _repository_identity() -> dict[str, str | None]:
        def rev_parse(argument: str) -> str | None:
            completed = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "--verify", argument],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if completed.returncode != 0:
                return None
            return completed.stdout.strip()

        raw_workflow = CANONICAL_WORKFLOW.read_bytes()
        workflow_blob = hashlib.sha1(
            b"blob " + str(len(raw_workflow)).encode("ascii") + b"\0" + raw_workflow
        ).hexdigest()
        return {
            "head": rev_parse("HEAD"),
            "tree": rev_parse("HEAD^{tree}"),
            "parent": rev_parse("HEAD^"),
            "workflow_blob": workflow_blob,
        }

    def _metadata(self) -> list[dict[str, Any]]:
        return [
            {
                "projectId": self.manifest["project"]["id"],
                "projectName": self.manifest["project"]["name"],
                "id": self.manifest["workflow"]["id"],
                "name": self.manifest["workflow"]["name"],
                "isArchived": False,
            }
        ]

    def _make_fixture(self, *, preimage: str = "existing", dispatch_mode: str = "success") -> dict[str, Any]:
        canonical = _read_json(CANONICAL_WORKFLOW)
        fixture: dict[str, Any] = {
            "schema_version": "xb.member.gateway.bounded_import.fixture.v1",
            "repository": copy.deepcopy(self.repository_identity),
            "cursor": self._make_cursor(),
            "metadata": self._metadata() if preimage == "existing" else [],
            "workflow": canonical if preimage == "existing" else None,
            "after_metadata": self._metadata(),
            "after_workflow": canonical,
            "dispatch_mode": dispatch_mode,
            "custody_mode": "normal",
        }
        _write_json(self.fixture_path, fixture)
        return fixture

    def _operation_id(self, label: str) -> str:
        operation_id = f"security-{label}-{uuid4().hex[:12]}"
        self.addCleanup(lambda: shutil.rmtree(self._operation_path(operation_id), ignore_errors=True))
        return operation_id

    def _build_command(
        self,
        mode: str,
        operation_id: str,
        *,
        extra_args: tuple[str, ...] = (),
        confirm: bool = True,
    ) -> tuple[list[str], dict[str, str]]:
        command = [
            self.pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(SCRIPT),
            "-Mode",
            mode,
            "-RepoRoot",
            str(ROOT),
            "-OperationId",
            operation_id,
            "-BindingManifestFile",
            str(self.manifest_path),
            "-OperationsRoot",
            self.operations_root,
            "-FixtureFile",
            str(self.fixture_path),
            "-TestOnly",
        ]
        if mode == "Apply" and confirm:
            command.append("-ConfirmBoundedApply")
        command.extend(extra_args)
        environment = os.environ.copy()
        for name in (
            "N8N_WORKFLOW_HOOK_SCRIPT",
            "N8N_WORKFLOW_HOOK_AUTOLOAD",
            "N8N_WORKFLOW_VALIDATION_RULES",
            "N8N_WORKFLOW_VALIDATION_RULES_AUTOLOAD",
        ):
            environment.pop(name, None)
        return command, environment

    def _run(
        self,
        mode: str,
        operation_id: str,
        *,
        expect_success: bool = True,
        extra_args: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
        confirm: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command, environment = self._build_command(mode, operation_id, extra_args=extra_args, confirm=confirm)
        if extra_env:
            environment.update(extra_env)
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            timeout=90,
        )
        if expect_success:
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"bounded importer failed\nstdout={completed.stdout}\nstderr={completed.stderr}",
            )
        else:
            self.assertNotEqual(
                completed.returncode,
                0,
                msg=f"bounded importer unexpectedly succeeded\nstdout={completed.stdout}",
            )
        return completed

    def _start(
        self,
        mode: str,
        operation_id: str,
        *,
        extra_args: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
        confirm: bool = True,
    ) -> subprocess.Popen[str]:
        command, environment = self._build_command(mode, operation_id, extra_args=extra_args, confirm=confirm)
        if extra_env:
            environment.update(extra_env)
        return subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
        )

    def _operation_path(self, operation_id: str) -> Path:
        return ROOT / self.operations_root / operation_id

    def _poisoned_command(self, label: str) -> tuple[Path, Path]:
        marker = self.case_root / f"{label}.invoked"
        command = self.case_root / f"{label}.cmd"
        command.write_text(
            "@echo off\r\n"
            f'> "{marker}" echo invoked\r\n'
            "exit /b 0\r\n",
            encoding="ascii",
        )
        return command, marker

    def _capture(self, label: str, *, preimage: str = "existing", dispatch_mode: str = "success") -> tuple[str, dict[str, Any]]:
        operation_id = self._operation_id(label)
        fixture = self._make_fixture(preimage=preimage, dispatch_mode=dispatch_mode)
        self._run("CapturePlan", operation_id)
        operation_path = self._operation_path(operation_id)
        prepared = _read_json(operation_path / "prepared.workflow.json")
        fixture["after_workflow"] = prepared
        _write_json(self.fixture_path, fixture)
        return operation_id, fixture

    def _assert_persisted_artifacts_are_strict_utf8_lf(self, operation_id: str) -> None:
        operation_path = self._operation_path(operation_id)
        for path in operation_path.rglob("*"):
            if not path.is_file():
                continue
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), path)
            self.assertNotIn(b"\r", raw, path)
            self.assertTrue(raw.endswith(b"\n"), path)
            self.assertNotIn(b".tmp", path.name.encode("ascii", "ignore"), path)

    def _apply_successfully(self, operation_id: str) -> dict[str, Any]:
        completed = self._run("Apply", operation_id)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_existing_target_capture_apply_and_completed_retry_are_no_replay(self) -> None:
        operation_id, _ = self._capture("existing-success")
        result = self._apply_successfully(operation_id)
        self.assertEqual(result["status"], "applied_and_verified")
        self.assertEqual(result["mutation_attempted"], 1)
        operation_path = self._operation_path(operation_id)
        self.assertEqual(
            {path.name for path in operation_path.iterdir()},
            {
                "binding.json",
                "cursor-state.json",
                "mutation-intent.json",
                "plan.json",
                "prepared.workflow.json",
                "preimage.workflow.json",
                "dispatch-ownership.json",
                "dispatch-receipt.json",
                "completion-receipt.json",
            },
        )
        dispatch_before = (operation_path / "dispatch-receipt.json").read_bytes()
        retry = self._apply_successfully(operation_id)
        self.assertEqual(retry["status"], "no_op_success")
        self.assertEqual(retry["mutation_attempted"], 0)
        self.assertEqual(dispatch_before, (operation_path / "dispatch-receipt.json").read_bytes())
        self._assert_persisted_artifacts_are_strict_utf8_lf(operation_id)

    def test_absent_preimage_has_complete_metadata_receipt_and_no_replay(self) -> None:
        operation_id, _ = self._capture("absent-success", preimage="absent")
        result = self._apply_successfully(operation_id)
        self.assertEqual(result["status"], "applied_and_verified")
        completion = _read_json(self._operation_path(operation_id) / "completion-receipt.json")
        self.assertEqual(completion["ownership"], "created")
        retry = self._apply_successfully(operation_id)
        self.assertEqual(retry["status"], "no_op_success")
        self.assertEqual(retry["mutation_attempted"], 0)

    def test_ambiguous_dispatch_reconciles_to_exact_target_without_replay(self) -> None:
        operation_id, fixture = self._capture("ambiguous-reconcile", dispatch_mode="ambiguous")
        self._run("Apply", operation_id, expect_success=False)
        operation_path = self._operation_path(operation_id)
        self.assertTrue((operation_path / "dispatch-receipt.json").is_file())
        self.assertFalse((operation_path / "completion-receipt.json").exists())
        dispatch_before = (operation_path / "dispatch-receipt.json").read_bytes()
        fixture["dispatch_mode"] = "success"
        _write_json(self.fixture_path, fixture)
        result = self._apply_successfully(operation_id)
        self.assertEqual(result["status"], "completed_existing_dispatch")
        self.assertEqual(result["mutation_attempted"], 0)
        reconciled = _read_json(operation_path / "dispatch-receipt.json")
        self.assertNotEqual(dispatch_before, (operation_path / "dispatch-receipt.json").read_bytes())
        self.assertEqual(reconciled["dispatch_state"], "dispatched")
        self.assertEqual(reconciled["outcome"], "completed")
        retry = self._apply_successfully(operation_id)
        self.assertEqual(retry["status"], "no_op_success")
        self.assertEqual(retry["mutation_attempted"], 0)

    def test_ambiguous_dispatch_does_not_replay_when_target_is_still_preimage(self) -> None:
        operation_id, fixture = self._capture("ambiguous-no-replay", dispatch_mode="ambiguous")
        original = _read_json(CANONICAL_WORKFLOW)
        fixture["after_workflow"] = original
        _write_json(self.fixture_path, fixture)
        self._run("Apply", operation_id, expect_success=False)
        dispatch_before = (self._operation_path(operation_id) / "dispatch-receipt.json").read_bytes()
        self._run("Apply", operation_id, expect_success=False)
        self.assertEqual(dispatch_before, (self._operation_path(operation_id) / "dispatch-receipt.json").read_bytes())
        self.assertFalse((self._operation_path(operation_id) / "completion-receipt.json").exists())

    def test_interrupted_dispatch_persists_pending_marker_and_reconciles_without_replay(self) -> None:
        operation_id, fixture = self._capture("interrupted-dispatch", dispatch_mode="interrupt_after_start")
        self._run("Apply", operation_id, expect_success=False)
        operation_path = self._operation_path(operation_id)
        pending = _read_json(operation_path / "dispatch-receipt.json")
        self.assertEqual(pending["dispatch_state"], "dispatching")
        self.assertEqual(pending["outcome"], "pending")
        self.assertFalse(pending["replay_allowed"])
        self.assertFalse((operation_path / "completion-receipt.json").exists())
        fixture["dispatch_mode"] = "success"
        _write_json(self.fixture_path, fixture)
        result = self._apply_successfully(operation_id)
        self.assertEqual(result["status"], "completed_existing_dispatch")
        self.assertEqual(result["mutation_attempted"], 0)
        reconciled = _read_json(operation_path / "dispatch-receipt.json")
        self.assertEqual(reconciled["dispatch_state"], "dispatched")
        self.assertEqual(reconciled["outcome"], "completed")
        self.assertFalse(reconciled["replay_allowed"])
        retry = self._apply_successfully(operation_id)
        self.assertEqual(retry["status"], "no_op_success")
        self.assertEqual(retry["mutation_attempted"], 0)

    def test_pre_dispatch_failure_can_retry_only_after_fresh_evidence(self) -> None:
        operation_id, fixture = self._capture("pre-dispatch-retry", dispatch_mode="pre_dispatch_failure")
        self._run("Apply", operation_id, expect_success=False)
        operation_path = self._operation_path(operation_id)
        receipt = _read_json(operation_path / "dispatch-receipt.json")
        self.assertEqual(receipt["dispatch_state"], "not_dispatched")
        self.assertEqual(receipt["outcome"], "pre_dispatch_failure")
        self.assertTrue(receipt["replay_allowed"])
        fixture["dispatch_mode"] = "success"
        _write_json(self.fixture_path, fixture)
        result = self._apply_successfully(operation_id)
        self.assertEqual(result["mutation_attempted"], 1)

    def test_concurrent_admission_has_one_operation_scoped_dispatch_owner(self) -> None:
        operation_id, fixture = self._capture("concurrent-admission", dispatch_mode="concurrent_success")
        barrier = self.case_root / "admission-barrier"
        dispatch_markers = self.case_root / "dispatch-markers"
        fixture["admission_barrier_directory"] = str(barrier)
        fixture["dispatch_marker_directory"] = str(dispatch_markers)
        _write_json(self.fixture_path, fixture)

        processes = [self._start("Apply", operation_id), self._start("Apply", operation_id)]
        results = [process.communicate(timeout=90) for process in processes]
        self.assertEqual(len(list(barrier.glob("*.marker"))), 2)
        dispatch_count = len(list(dispatch_markers.glob("*.marker")))
        self.assertLessEqual(dispatch_count, 1)
        self.assertEqual(dispatch_count, 1)
        payloads = []
        for stdout, stderr in results:
            if stdout.strip():
                payloads.append(json.loads(stdout.strip().splitlines()[-1]))
        self.assertEqual(sum(payload.get("status") == "applied_and_verified" for payload in payloads), 1)
        self.assertEqual(sum(process.returncode == 0 for process in processes), 1)

    def test_testonly_dispatch_is_synthetic_and_fixture_modes_fail_closed(self) -> None:
        operation_id, _ = self._capture("testonly-native-poison", dispatch_mode="success")
        poisoned_n8n, n8n_marker = self._poisoned_command("testonly-native-poison")
        result = self._run(
            "Apply",
            operation_id,
            confirm=False,
            extra_args=("-N8nExecutable", str(poisoned_n8n)),
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "applied_and_verified")
        self.assertFalse(n8n_marker.exists())
        retry = self._run(
            "Apply",
            operation_id,
            confirm=False,
            extra_args=("-N8nExecutable", str(poisoned_n8n)),
        )
        retry_payload = json.loads(retry.stdout.strip().splitlines()[-1])
        self.assertEqual(retry_payload["status"], "no_op_success")
        self.assertFalse(n8n_marker.exists())

        cases = (
            ("testonly-supplied-container-visible", "container_visible_import", False),
            ("testonly-named-container-visible", "container_visible_import", True),
            ("testonly-future-mode", "future_dispatch_mode", False),
        )
        for label, dispatch_mode, named_container in cases:
            with self.subTest(label=label):
                invalid_operation_id, fixture = self._capture(label, dispatch_mode="success")
                fixture["dispatch_mode"] = dispatch_mode
                _write_json(self.fixture_path, fixture)
                poisoned_n8n, n8n_marker = self._poisoned_command(f"{label}-n8n")
                extra_args = ["-N8nExecutable", str(poisoned_n8n)]
                docker_marker: Path | None = None
                if named_container:
                    poisoned_docker, docker_marker = self._poisoned_command(f"{label}-docker")
                    extra_args.extend(("-N8nContainer", "fake-container", "-DockerExecutable", str(poisoned_docker)))
                completed = self._run(
                    "Apply",
                    invalid_operation_id,
                    expect_success=False,
                    confirm=False,
                    extra_args=tuple(extra_args),
                )
                self.assertIn("fixture_dispatch_mode_invalid", completed.stderr)
                self.assertFalse(n8n_marker.exists())
                if docker_marker is not None:
                    self.assertFalse(docker_marker.exists())
                self.assertFalse((self._operation_path(invalid_operation_id) / "completion-receipt.json").exists())

    def test_testonly_process_boundaries_reject_direct_calls(self) -> None:
        command_text = (
            f'. "{SCRIPT}"; $script:BoundedTestOnly = $true; '
            'try { Invoke-BoundedProcess -Command "poison" -Arguments @(); } '
            'catch { Write-Output ("PROCESS=" + $_.Exception.Message) }; '
            'try { Invoke-BoundedExternalCommand "list:workflow" @(); } '
            'catch { Write-Output ("EXTERNAL=" + $_.Exception.Message) }'
        )
        completed = self._run_ps_command(command_text)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PROCESS=test_only_process_forbidden", completed.stdout)
        self.assertIn("EXTERNAL=test_only_external_command_forbidden", completed.stdout)

    def test_testonly_identity_paths_do_not_spawn_git(self) -> None:
        operation_id = self._operation_id("testonly-no-git")
        fixture = self._make_fixture()
        git_marker = self.case_root / "git.invoked"
        fake_git = self.case_root / "git.cmd"
        fake_git.write_text(
            "@echo off\r\n"
            f'> "{git_marker}" echo invoked\r\n'
            "exit /b 99\r\n",
            encoding="ascii",
        )
        poisoned_path = str(self.case_root)
        if os.name == "nt":
            path_separator = ";"
        else:
            path_separator = os.pathsep
        environment = {"PATH": poisoned_path + path_separator + os.environ.get("PATH", "")}
        self._run("CapturePlan", operation_id, extra_env=environment)
        fixture["after_workflow"] = _read_json(self._operation_path(operation_id) / "prepared.workflow.json")
        _write_json(self.fixture_path, fixture)
        self._run("Apply", operation_id, confirm=False, extra_env=environment)
        self.assertFalse(git_marker.exists())

    def test_cursor_identity_watermark_and_preimage_mismatch_block_before_dispatch(self) -> None:
        cases = (
            ("cursor-identity", lambda fixture: fixture["cursor"].update({"source_system": "wrong_source"})),
            ("watermark", lambda fixture: fixture["cursor"].update({"watermark": "2026-09-18T00:00:01Z"})),
            (
                "preimage",
                lambda fixture: fixture["workflow"].update({"description": "tampered preimage"}),
            ),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                operation_id, fixture = self._capture(label)
                mutate(fixture)
                _write_json(self.fixture_path, fixture)
                self._run("Apply", operation_id, expect_success=False)
                self.assertFalse((self._operation_path(operation_id) / "dispatch-receipt.json").exists())

    def test_cursor_digest_and_state_version_references_block_before_dispatch(self) -> None:
        operation_id, _ = self._capture("cursor-digest")
        cursor_state_path = self._operation_path(operation_id) / "cursor-state.json"
        cursor_state = _read_json(cursor_state_path)
        cursor_state["cursor_digest"] = "0" * 64
        _write_json(cursor_state_path, cursor_state)
        self._run("Apply", operation_id, expect_success=False)
        self.assertFalse((self._operation_path(operation_id) / "dispatch-receipt.json").exists())

        operation_id, fixture = self._capture("cursor-state-version")
        fixture["cursor"]["state_version"] = 1
        _write_json(self.fixture_path, fixture)
        self._run("Apply", operation_id, expect_success=False)
        self.assertFalse((self._operation_path(operation_id) / "dispatch-receipt.json").exists())

    def test_endpoint_and_credential_relationships_fail_closed(self) -> None:
        operation_id = self._operation_id("endpoint-relationship")
        self._make_fixture()
        self.manifest["endpoints"]["forms_responses"] = "https://forms.googleapis.com:443/v1/forms/other-form/responses"
        _write_json(self.manifest_path, self.manifest)
        completed = self._run("CapturePlan", operation_id, expect_success=False)
        self.assertIn("binding_form_endpoint_mismatch", completed.stderr)

        operation_id = self._operation_id("credential-relationship")
        self.manifest = self._make_manifest()
        self.manifest["credential_roles"]["gateway_bearer"]["credential_type"] = "googleOAuth2Api"
        _write_json(self.manifest_path, self.manifest)
        self._make_fixture()
        completed = self._run("CapturePlan", operation_id, expect_success=False)
        self.assertIn("binding_credential_type_invalid", completed.stderr)

        operation_id = self._operation_id("foreign-checkpoint-origin")
        self.manifest = self._make_manifest()
        self.manifest["endpoints"]["page_checkpoint"] = "https://foreign.example.com/v1/source/cursor-security/page"
        _write_json(self.manifest_path, self.manifest)
        self._make_fixture()
        completed = self._run("CapturePlan", operation_id, expect_success=False)
        self.assertIn("binding_gateway_origin_invalid", completed.stderr)

        operation_id = self._operation_id("foreign-forms-origin")
        self.manifest = self._make_manifest()
        self.manifest["endpoints"]["forms_responses"] = "https://foreign.example.com:443/v1/forms/form-security-001/responses"
        _write_json(self.manifest_path, self.manifest)
        self._make_fixture()
        completed = self._run("CapturePlan", operation_id, expect_success=False)
        self.assertIn("binding_forms_origin_invalid", completed.stderr)

        for label, endpoint, expected in (
            ("forms-wrong-port", "https://forms.googleapis.com:8443/v1/forms/form-security-001/responses", "binding_forms_origin_invalid"),
            ("forms-wrong-version", "https://forms.googleapis.com:443/v2/forms/form-security-001/responses", "binding_form_endpoint_mismatch"),
            ("forms-host-case", "https://FORMS.GOOGLEAPIS.COM:443/v1/forms/form-security-001/responses", "binding_form_endpoint_mismatch"),
            ("forms-host-dot", "https://forms.googleapis.com.:443/v1/forms/form-security-001/responses", "binding_forms_origin_invalid"),
        ):
            with self.subTest(label=label):
                operation_id = self._operation_id(label)
                self.manifest = self._make_manifest()
                self.manifest["endpoints"]["forms_responses"] = endpoint
                _write_json(self.manifest_path, self.manifest)
                self._make_fixture()
                completed = self._run("CapturePlan", operation_id, expect_success=False)
                self.assertIn(expected, completed.stderr)

    def test_reviewed_gateway_origin_is_canonical_manifest_authority(self) -> None:
        operation_id = self._operation_id("reviewed-origin-pass")
        self._make_fixture()
        completed = self._run("CapturePlan", operation_id)
        self.assertIn("capture_plan_complete", completed.stdout)

        endpoint_cases = (
            (
                "endpoint-foreign-host",
                lambda manifest: manifest["endpoints"].update(
                    gateway_ingest="https://other.gateway.internal:443/v1/source-events-security"
                ),
                "binding_gateway_origin_invalid",
            ),
            (
                "manifest-origin-mismatch",
                lambda manifest: manifest["security"].update(
                    approved_gateway_origin="https://other.gateway.internal:443"
                ),
                "binding_gateway_origin_invalid",
            ),
        )
        for label, mutate, expected in endpoint_cases:
            with self.subTest(label=label):
                operation_id = self._operation_id(label)
                self.manifest = self._make_manifest()
                mutate(self.manifest)
                _write_json(self.manifest_path, self.manifest)
                self._make_fixture()
                completed = self._run("CapturePlan", operation_id, expect_success=False)
                self.assertIn(expected, completed.stderr)

        origin_cases = (
            ("origin-http", "http://reviewed.gateway.internal:443", "binding_security_invalid"),
            ("origin-wrong-port", "https://reviewed.gateway.internal:8443", "binding_security_invalid"),
            ("origin-userinfo", "https://user:pass@reviewed.gateway.internal:443", "binding_security_invalid"),
            ("origin-path", "https://reviewed.gateway.internal:443/private", "binding_security_invalid"),
            ("origin-query", "https://reviewed.gateway.internal:443?private=1", "binding_security_invalid"),
            ("origin-fragment", "https://reviewed.gateway.internal:443#private", "binding_security_invalid"),
        )
        for label, origin, expected in origin_cases:
            with self.subTest(label=label):
                operation_id = self._operation_id(label)
                self.manifest = self._make_manifest()
                self.manifest["security"]["approved_gateway_origin"] = origin
                _write_json(self.manifest_path, self.manifest)
                self._make_fixture()
                completed = self._run("CapturePlan", operation_id, expect_success=False)
                self.assertIn(expected, completed.stderr)

    def _add_resolved_credential_ids(self, workflow: dict[str, Any]) -> dict[str, Any]:
        resolved = copy.deepcopy(workflow)
        for role in self.manifest["credential_roles"].values():
            for node_name in role["node_names"]:
                node = next(node for node in resolved["nodes"] if node["name"] == node_name)
                credential_type = role["credential_type"]
                credential = node["credentials"][credential_type]
                node["credentials"][credential_type] = {
                    "id": role["credential_id"],
                    "name": credential["name"],
                }
        return resolved

    def test_resolved_credential_readback_is_canonicalised_without_dropping_binding(self) -> None:
        operation_id, fixture = self._capture("resolved-credentials")
        fixture["after_workflow"] = self._add_resolved_credential_ids(fixture["after_workflow"])
        _write_json(self.fixture_path, fixture)
        result = self._apply_successfully(operation_id)
        self.assertEqual(result["status"], "applied_and_verified")

    def test_mismatched_resolved_credential_is_rejected(self) -> None:
        operation_id, fixture = self._capture("mismatched-resolved-credential")
        fixture["after_workflow"] = self._add_resolved_credential_ids(fixture["after_workflow"])
        forms_node = next(
            node for node in fixture["after_workflow"]["nodes"]
            if node["name"] == "Google Forms single page (configured outside repo)"
        )
        forms_node["credentials"]["googleOAuth2Api"]["name"] = "wrong-credential"
        _write_json(self.fixture_path, fixture)
        completed = self._run("Apply", operation_id, expect_success=False)
        self.assertIn("credential_name_mismatch", completed.stderr)

    def test_different_resolved_credential_id_with_same_name_is_rejected(self) -> None:
        operation_id, fixture = self._capture("mismatched-resolved-credential-id")
        forms_node = next(
            node for node in fixture["after_workflow"]["nodes"]
            if node["name"] == "Google Forms single page (configured outside repo)"
        )
        forms_node["credentials"]["googleOAuth2Api"]["id"] = "different-credential-object-001"
        _write_json(self.fixture_path, fixture)
        completed = self._run("Apply", operation_id, expect_success=False)
        self.assertIn("credential_id_mismatch", completed.stderr)

    def test_conflicting_workflow_identity_is_not_treated_as_absent(self) -> None:
        operation_id = self._operation_id("identity-conflict")
        fixture = self._make_fixture()
        fixture["metadata"].append(
            {
                "projectId": "different-project",
                "projectName": self.manifest["project"]["name"],
                "id": self.manifest["workflow"]["id"],
                "name": self.manifest["workflow"]["name"],
                "isArchived": False,
            }
        )
        _write_json(self.fixture_path, fixture)
        completed = self._run("CapturePlan", operation_id, expect_success=False)
        self.assertIn("target_identity_conflict", completed.stderr)

    def test_apply_rechecks_private_acl_custody_before_retry(self) -> None:
        operation_id, fixture = self._capture("acl-retry")
        fixture["custody_mode"] = "acl_failure"
        _write_json(self.fixture_path, fixture)
        completed = self._run("Apply", operation_id, expect_success=False)
        self.assertIn("private_acl_verification_failed", completed.stderr)
        self.assertFalse((self._operation_path(operation_id) / "dispatch-receipt.json").exists())

    def test_reviewed_workflow_blob_is_rechecked_on_apply(self) -> None:
        operation_id, _ = self._capture("reviewed-workflow-blob")
        original = CANONICAL_WORKFLOW.read_bytes()
        try:
            CANONICAL_WORKFLOW.write_bytes(original + b"\n")
            completed = self._run("Apply", operation_id, expect_success=False)
            self.assertIn("repository_workflow_blob_mismatch", completed.stderr)
        finally:
            CANONICAL_WORKFLOW.write_bytes(original)

    def test_manifest_custody_is_established_before_semantic_parsing(self) -> None:
        outside_manifest = self.case_root / "outside-binding.json"
        outside_manifest.write_text("{not-json", encoding="utf-8")
        self.manifest_path = outside_manifest
        operation_id = self._operation_id("manifest-out-of-custody")
        self._make_fixture()
        completed = self._run("CapturePlan", operation_id, expect_success=False)
        self.assertIn("binding_manifest_path_not_private", completed.stderr)
        self.assertNotIn("binding_shape_invalid", completed.stderr)

        self.manifest_path = self.private_manifest_path
        self.private_manifest_path.write_text("{not-json", encoding="utf-8")
        fixture = self._make_fixture()
        fixture["custody_mode"] = "manifest_acl_failure"
        _write_json(self.fixture_path, fixture)
        operation_id = self._operation_id("manifest-acl-before-parse")
        completed = self._run("CapturePlan", operation_id, expect_success=False)
        self.assertIn("private_acl_verification_failed", completed.stderr)
        self.assertNotIn("json_invalid", completed.stderr)

    def test_manifest_reparse_path_is_rejected_before_semantic_parsing(self) -> None:
        outside_manifest = self.case_root / "reparse-target.json"
        outside_manifest.write_text("{not-json", encoding="utf-8")
        link = ROOT / ".n8n-local/member-gateway-bounded-import" / f"test-manifest-link-{uuid4().hex}.json"
        try:
            try:
                os.symlink(outside_manifest, link)
            except (OSError, NotImplementedError):
                junction = subprocess.run(
                    ["cmd.exe", "/c", "mklink", str(link), str(outside_manifest)],
                    capture_output=True,
                    text=True,
                )
                if junction.returncode != 0:
                    self.skipTest(f"manifest reparse point unavailable: {junction.stderr}")
            self.addCleanup(lambda path=link: path.unlink(missing_ok=True))
            self.manifest_path = link
            operation_id = self._operation_id("manifest-reparse-before-parse")
            self._make_fixture()
            completed = self._run("CapturePlan", operation_id, expect_success=False)
            self.assertIn("unsafe_link", completed.stderr)
            self.assertNotIn("json_invalid", completed.stderr)
        finally:
            link.unlink(missing_ok=True)

    def test_prepared_transport_bindings_and_process_contract_are_explicit(self) -> None:
        operation_id, _ = self._capture("transport-bindings")
        prepared_path = self._operation_path(operation_id) / "prepared.workflow.json"
        prepared = _read_json(prepared_path)
        prepared_text = prepared_path.read_text(encoding="utf-8")
        self.assertIn(self.manifest["endpoints"]["source_cursor"], prepared_text)
        self.assertIn(self.manifest["endpoints"]["page_checkpoint"], prepared_text)
        self.assertNotIn("https://gateway.example.com/v1/source/cursor/page", prepared_text)
        nodes = {node["name"]: node for node in prepared["nodes"]}
        self.assertEqual(
            nodes["Google Forms single page (configured outside repo)"]["credentials"]["googleOAuth2Api"]["id"],
            self.manifest["credential_roles"]["google_forms_oauth"]["credential_id"],
        )
        self.assertEqual(nodes["Google Forms single page (configured outside repo)"]["parameters"]["authentication"], "predefinedCredentialType")
        self.assertEqual(nodes["Google Forms single page (configured outside repo)"]["parameters"]["nodeCredentialType"], "googleOAuth2Api")
        for name in (
            "Read durable source cursor",
            "Protected XB Gateway ingest (configured outside repo)",
            "Commit durable page checkpoint",
        ):
            self.assertEqual(nodes[name]["parameters"]["authentication"], "genericCredentialType")
            self.assertEqual(nodes[name]["parameters"]["genericAuthType"], "httpBearerAuth")
            self.assertEqual(
                nodes[name]["credentials"]["httpBearerAuth"]["id"],
                self.manifest["credential_roles"]["gateway_bearer"]["credential_id"],
            )
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Get-BoundedCursorRequestUri", source)
        self.assertIn('Invoke-BoundedExternalCommand "list:workflow" @()', source)
        self.assertNotIn('list:workflow" @("--output=json")', source)
        self.assertIn('"--input=$preparedPath"', source)
        self.assertNotIn('"--input=-"', source)
        self.assertIn("ReadToEndAsync()", source)

    def test_supported_n8n_list_and_import_command_shapes_are_consumed_without_json_assumptions(self) -> None:
        fake = self.case_root / "fake-n8n.cmd"
        fake.write_text(
            "@echo off\r\n"
            "if \"%~1\"==\"list:workflow\" goto list\r\n"
            "if \"%~1\"==\"import:workflow\" goto import\r\n"
            "exit /b 9\r\n"
            ":list\r\n"
            "echo workflow-security-001^|Member Gateway - Google Forms durable source adapter ^(inactive^)\r\n"
            "exit /b 0\r\n"
            ":import\r\n"
            "echo import-ok:%*\r\n"
            "exit /b 0\r\n",
            encoding="ascii",
        )
        command_text = (
            f'. "{SCRIPT}"; $script:N8nExecutable = "{fake}"; '
            '$list = Invoke-BoundedExternalCommand "list:workflow" @(); '
            '$rows = ConvertFrom-BoundedWorkflowListText $list.stdout; '
            'Write-Output ("LIST=" + $rows[0].id + "|" + $rows[0].name); '
            '$import = Invoke-BoundedExternalCommand "import:workflow" @("--input=C:\\prepared.workflow.json", "--projectId=project-security-001", "--activeState=false"); '
            'Write-Output ("IMPORT=" + $import.stdout.Trim())'
        )
        completed = self._run_ps_command(command_text)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("LIST=workflow-security-001|Member Gateway - Google Forms durable source adapter (inactive)", completed.stdout)
        self.assertIn("IMPORT=import-ok:import:workflow --input=C:\\prepared.workflow.json --projectId=project-security-001 --activeState=false", completed.stdout)

    def _run_container_transport(
        self,
        operation_id: str,
        *,
        extra_env: dict[str, str],
        expect_success: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "FAKE_OPERATION_ID": operation_id,
            "FAKE_REPO_ROOT": str(ROOT),
            "FAKE_OPERATIONS_ROOT": self.operations_root,
            "FAKE_CUSTODY_OPERATIONS_ROOT": str(self.case_root / ("custody-" + operation_id)),
        }
        environment.update(extra_env)
        command_text = """
. '__SCRIPT__'
$OperationId = $env:FAKE_OPERATION_ID
$script:BoundedRepoRoot = $env:FAKE_REPO_ROOT
$sourceOperationsRoot = Join-Path $script:BoundedRepoRoot $env:FAKE_OPERATIONS_ROOT
$script:BoundedOperationsRoot = $env:FAKE_CUSTODY_OPERATIONS_ROOT
[void][System.IO.Directory]::CreateDirectory($script:BoundedOperationsRoot)
$sourceOperationPath = Join-Path $sourceOperationsRoot $OperationId
$targetOperationPath = Join-Path $script:BoundedOperationsRoot $OperationId
[void][System.IO.Directory]::CreateDirectory($targetOperationPath)
foreach ($sourceFile in @(Get-ChildItem -LiteralPath $sourceOperationPath -File)) {
    Copy-Item -LiteralPath $sourceFile.FullName -Destination (Join-Path $targetOperationPath $sourceFile.Name) -Force
}
$script:BoundedTestOnly = $false
$N8nContainer = "fake-container"
$script:BoundedDockerExecutable = "docker"
$script:FakeRoot = [System.IO.Path]::GetFullPath($env:FAKE_CONTAINER_ROOT)
[void][System.IO.Directory]::CreateDirectory((Join-Path $script:FakeRoot "tmp"))
$script:FakeEntries = @{}

function Get-FakeKey {
    param([Parameter(Mandatory)][string]$Path)
    $value = "/" + $Path.TrimStart([char]47).Replace([string][char]92, "/")
    while ($value.Contains("//")) { $value = $value.Replace("//", "/") }
    return $value
}

function Get-FakePath {
    param([Parameter(Mandatory)][string]$Path)
    $relative = (Get-FakeKey $Path).TrimStart([char]47).Replace("/", [string][System.IO.Path]::DirectorySeparatorChar)
    return Join-Path $script:FakeRoot $relative
}

function Get-FakeEntry {
    param([Parameter(Mandatory)][string]$Path)
    $key = Get-FakeKey $Path
    if (-not $script:FakeEntries.ContainsKey($key)) { return $null }
    return $script:FakeEntries[$key]
}

function Set-FakeEntry {
    param([string]$Path, [string]$Mode, [string]$Uid, [string]$Gid, [string]$Type)
    $script:FakeEntries[(Get-FakeKey $Path)] = [ordered]@{ mode = $Mode; uid = $Uid; gid = $Gid; type = $Type }
}

function Get-FakeResult {
    param([int]$ExitCode, [string]$Stdout = "")
    return [pscustomobject]([ordered]@{ exit_code = $ExitCode; stdout = $Stdout; stderr = "" })
}

function Test-FakeReadable {
    param([string]$Path, [string]$Uid, [string]$Gid)
    $entry = Get-FakeEntry $Path
    if ($null -eq $entry -or [string]$entry.type -cne "regular file") { return $false }
    $mode = [Convert]::ToInt32([string]$entry.mode, 8)
    if ([string]$entry.uid -ceq $Uid) { $bits = ($mode -shr 6) -band 7 }
    elseif ([string]$entry.gid -ceq $Gid) { $bits = ($mode -shr 3) -band 7 }
    else { $bits = $mode -band 7 }
    return (($bits -band 4) -ne 0)
}

$tmpMode = if ($env:FAKE_TMP_MODE) { $env:FAKE_TMP_MODE } else { "1777" }
$tmpUid = if ($env:FAKE_TMP_UID) { $env:FAKE_TMP_UID } else { "0" }
$tmpGid = if ($env:FAKE_TMP_GID) { $env:FAKE_TMP_GID } else { "0" }
Set-FakeEntry "/tmp" $tmpMode $tmpUid $tmpGid "directory"

function Invoke-BoundedProcess {
    param(
        [Parameter(Mandatory)][string]$Command,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Arguments,
        [string]$InputText = ""
    )
    if ($env:FAKE_PROCESS_CALLS) { Add-Content -LiteralPath $env:FAKE_PROCESS_CALLS -Value (($Arguments -join " ")) }
    $script:BoundedTestOnly = $true
    $arguments = @($Arguments)
    if ($arguments.Count -eq 0) { return Get-FakeResult 13 }

    if ($arguments[0] -ceq "inspect") {
        if ($arguments[1] -ceq "--format={{.Id}}") { return Get-FakeResult 0 ("container-id-001" + [string][char]10) }
        if ($arguments[1] -ceq "--format={{.Image}}") { return Get-FakeResult 0 ("image-id-001" + [string][char]10) }
        return Get-FakeResult 10
    }

    if ($arguments[0] -ceq "cp") {
        $destination = [string]$arguments[2]
        $target = $destination.Substring($destination.IndexOf(":") + 1)
        $targetPath = Get-FakePath $target
        [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($targetPath))
        Copy-Item -LiteralPath $arguments[1] -Destination $targetPath -Force
        Set-FakeEntry $target "644" "0" "0" "regular file"
        return Get-FakeResult 0
    }

    if ($arguments[0] -ceq "exec") {
        $index = 1
        if ($arguments[$index] -ceq "-i") { $index++ }
        $importUid = if ($env:FAKE_IMPORT_UID) { $env:FAKE_IMPORT_UID } else { "1000" }
        $importGid = if ($env:FAKE_IMPORT_GID) { $env:FAKE_IMPORT_GID } else { "1000" }
        $execUid = $importUid
        if ($arguments[$index] -ceq "-u") { $execUid = "0"; $index += 2 }
        $commandName = [string]$arguments[$index + 1]
        $commandArguments = @()
        if ($index + 2 -lt $arguments.Count) { $commandArguments = @($arguments[($index + 2)..($arguments.Count - 1)]) }

        if ($commandName -ceq "id") {
            if ($commandArguments[0] -ceq "-u") { return Get-FakeResult 0 ($importUid + [string][char]10) }
            if ($commandArguments[0] -ceq "-g") { return Get-FakeResult 0 ($importGid + [string][char]10) }
            return Get-FakeResult 13
        }
        if ($commandName -ceq "stat") {
            $target = [string]$commandArguments[-1]
            $entry = Get-FakeEntry $target
            if ($null -eq $entry) { return Get-FakeResult 14 }
            if ([string]$entry.type -ceq "directory") {
                return Get-FakeResult 0 (("{0}:{1}:{2}:directory" -f $entry.mode, $entry.uid, $entry.gid) + [string][char]10)
            }
            $path = Get-FakePath $target
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return Get-FakeResult 14 }
            $size = [System.IO.File]::ReadAllBytes($path).Length
            return Get-FakeResult 0 (("{0}:{1}:{2}:regular file:{3}" -f $entry.mode, $entry.uid, $entry.gid, $size) + [string][char]10)
        }
        if ($commandName -ceq "mkdir") {
            $target = [string]$commandArguments[-1]
            [void][System.IO.Directory]::CreateDirectory((Get-FakePath $target))
            Set-FakeEntry $target ([string]$commandArguments[1]) "0" "0" "directory"
            return Get-FakeResult 0
        }
        if ($commandName -ceq "chown") {
            $target = [string]$commandArguments[1]
            $entry = Get-FakeEntry $target
            if ($null -eq $entry) { return Get-FakeResult 14 }
            $owner = [string]$commandArguments[0]
            if ([string]$entry.type -ceq "directory" -and $env:FAKE_CHOWN_DIRECTORY_OWNER) { $owner = $env:FAKE_CHOWN_DIRECTORY_OWNER }
            if ([string]$entry.type -ceq "regular file" -and $env:FAKE_CHOWN_FILE_OWNER) { $owner = $env:FAKE_CHOWN_FILE_OWNER }
            $parts = $owner -split ":", 2
            $entry.uid = $parts[0]; $entry.gid = $parts[1]
            return Get-FakeResult 0
        }
        if ($commandName -ceq "chmod") {
            $target = [string]$commandArguments[1]
            $entry = Get-FakeEntry $target
            if ($null -eq $entry) { return Get-FakeResult 14 }
            $mode = [string]$commandArguments[0]
            if ([string]$entry.type -ceq "directory" -and $env:FAKE_CHMOD_DIRECTORY_MODE) { $mode = $env:FAKE_CHMOD_DIRECTORY_MODE }
            if ([string]$entry.type -ceq "regular file" -and $env:FAKE_CHMOD_FILE_MODE) { $mode = $env:FAKE_CHMOD_FILE_MODE }
            $entry.mode = $mode
            return Get-FakeResult 0
        }
        if ($commandName -ceq "sha256sum") {
            $target = [string]$commandArguments[0]
            $path = Get-FakePath $target
            $entry = Get-FakeEntry $target
            if ($null -eq $entry -or -not (Test-Path -LiteralPath $path -PathType Leaf)) { return Get-FakeResult 14 }
            $digest = Get-BoundedSha256File $path
            if ($env:FAKE_HASH_MISMATCH -eq "1") { $digest = "0" * 64 }
            if ($env:FAKE_MUTATE_AFTER_HASH -eq "unreadable") { $entry.mode = "000" }
            return Get-FakeResult 0 (("{0}  {1}" -f $digest, $target) + [string][char]10)
        }
        if ($commandName -ceq "test") {
            $testCode = if (Test-Path -LiteralPath (Get-FakePath $commandArguments[2])) { 1 } else { 0 }
            return Get-FakeResult $testCode
        }
        if ($commandName -ceq "n8n") {
            $inputArgument = @($commandArguments | Where-Object { $_.StartsWith("--input=") })[0]
            $target = $inputArgument.Substring(8)
            $path = Get-FakePath $target
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or -not (Test-FakeReadable $target $importUid $importGid)) { return Get-FakeResult 18 }
            if ((Get-BoundedSha256File $path) -cne $env:EXPECTED_PREPARED_SHA) { return Get-FakeResult 15 }
            [System.IO.File]::WriteAllText($env:FAKE_CONSUMER_MARKER, "container-read-ok")
            $count = if (Test-Path -LiteralPath $env:FAKE_CONSUMER_COUNT) { [int][System.IO.File]::ReadAllText($env:FAKE_CONSUMER_COUNT) } else { 0 }
            [System.IO.File]::WriteAllText($env:FAKE_CONSUMER_COUNT, [string]($count + 1))
            return Get-FakeResult 0
        }
        if ($commandName -ceq "rm") {
            if ($env:FAKE_CLEANUP_FAIL -eq "1") { return Get-FakeResult 17 }
            $target = [string]$commandArguments[-1]
            Remove-Item -LiteralPath (Get-FakePath $target) -Recurse -Force -ErrorAction SilentlyContinue
            $directoryKey = Get-FakeKey $target
            foreach ($key in @($script:FakeEntries.Keys)) {
                if ($key -ceq $directoryKey -or $key.StartsWith($directoryKey + "/", [StringComparison]::Ordinal)) { [void]$script:FakeEntries.Remove($key) }
            }
            [System.IO.File]::WriteAllText($env:FAKE_CLEANUP_MARKER, "container-cleaned")
            return Get-FakeResult 0
        }
        return Get-FakeResult 16
    }
    return Get-FakeResult 16
}

try {
    $operationPath = Join-Path $script:BoundedOperationsRoot $OperationId
    $script:BoundedTestOnly = $true
    $state = Get-BoundedOperationState $operationPath
    $script:BoundedTestOnly = $false
    $context = [pscustomobject]([ordered]@{
        operations_path = $script:BoundedOperationsRoot
        operation_path = $operationPath
        operation_id = [string]$state.plan.operation_id
        operation_identity = [string]$state.plan.operation_identity
        plan_digest = [string](Get-BoundedPlanDigest $operationPath)
        prepared_workflow_digest = [string]$state.plan.identity_seed.prepared_workflow_digest
    })
    $preparedPath = Join-Path $operationPath "prepared.workflow.json"
    $result = Invoke-BoundedExternalCommand "import:workflow" @(
        "--input=$preparedPath",
        "--projectId=$([string]$state.intent.project.id)",
        "--activeState=false"
    ) -ContainerInputKey $OperationId -ContainerEvidenceContext $context
    if ([int]$result.exit_code -ne 0) { Stop-Bounded "n8n_command_failed" }
    Write-Output ("RESULT=" + ($result | ConvertTo-Json -Compress))
    exit 0
} catch {
    Write-Error ("bounded_test:" + [string]$_.Exception.Message)
    exit 1
}
""".replace("__SCRIPT__", str(SCRIPT).replace("'", "''"))
        completed = self._run_ps_command(command_text, extra_env=environment)
        if expect_success:
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        else:
            self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return completed

    def test_container_permission_model_and_cleanup_finality(self) -> None:
        happy_operation_id, _ = self._capture("container-visible-import", dispatch_mode="success")
        prepared_path = self._operation_path(happy_operation_id) / "prepared.workflow.json"
        case_dir = self.case_root / "container-visible-import"
        environment = {
            "FAKE_CONTAINER_ROOT": str(case_dir / "container-root"),
            "EXPECTED_PREPARED_SHA": hashlib.sha256(prepared_path.read_bytes()).hexdigest(),
            "FAKE_CONSUMER_MARKER": str(case_dir / "consumer.marker"),
            "FAKE_CONSUMER_COUNT": str(case_dir / "consumer.count"),
            "FAKE_CLEANUP_MARKER": str(case_dir / "cleanup.marker"),
            "FAKE_PROCESS_CALLS": str(case_dir / "process.calls"),
        }
        result = self._run_container_transport(happy_operation_id, extra_env=environment)
        self.assertIn("RESULT=", result.stdout)
        self.assertEqual((case_dir / "consumer.marker").read_text(encoding="utf-8"), "container-read-ok")
        self.assertEqual((case_dir / "consumer.count").read_text(encoding="utf-8"), "1")
        self.assertEqual((case_dir / "cleanup.marker").read_text(encoding="utf-8"), "container-cleaned")
        self.assertGreater(len((case_dir / "process.calls").read_text(encoding="utf-8").splitlines()), 0)
        self.assertEqual([path for path in (case_dir / "container-root").rglob("*") if path.is_file()], [])
        custody_operation_path = self.case_root / ("custody-" + happy_operation_id) / happy_operation_id
        custody = _read_json(custody_operation_path / "container-custody.json")
        for key, expected in {
            "container_id": "container-id-001",
            "image_id": "image-id-001",
            "tmp_mode": "1777",
            "tmp_uid": "0",
            "tmp_gid": "0",
            "import_uid": "1000",
            "import_gid": "1000",
            "stage_directory_mode": "700",
            "stage_directory_uid": "1000",
            "stage_directory_gid": "1000",
            "stage_file_mode": "600",
            "stage_file_uid": "1000",
            "stage_file_gid": "1000",
        }.items():
            self.assertEqual(custody[key], expected)
        for key in ("stage_verified", "mutation_possible", "cleanup_verified"):
            self.assertTrue(custody[key])
        self.assertEqual(custody["cleanup_state"], "cleaned")

        def run_failed_case(label: str, faults: dict[str, str], expected: str) -> tuple[Path, Path, str]:
            operation_id, _ = self._capture(label, dispatch_mode="success")
            case_dir = self.case_root / label
            prepared = self._operation_path(operation_id) / "prepared.workflow.json"
            environment = {
                "FAKE_CONTAINER_ROOT": str(case_dir / "container-root"),
                "EXPECTED_PREPARED_SHA": hashlib.sha256(prepared.read_bytes()).hexdigest(),
                "FAKE_CONSUMER_MARKER": str(case_dir / "consumer.marker"),
                "FAKE_CONSUMER_COUNT": str(case_dir / "consumer.count"),
                "FAKE_CLEANUP_MARKER": str(case_dir / "cleanup.marker"),
                "FAKE_PROCESS_CALLS": str(case_dir / "process.calls"),
            }
            environment.update(faults)
            completed = self._run_container_transport(operation_id, extra_env=environment, expect_success=False)
            self.assertIn(expected, completed.stderr)
            return (
                self.case_root / ("custody-" + operation_id) / operation_id,
                Path(environment["FAKE_CONTAINER_ROOT"]),
                Path(environment["FAKE_CONSUMER_COUNT"]),
            )

        for label, faults, expected in (
            ("tmp-not-sticky", {"FAKE_TMP_MODE": "0777"}, "n8n_container_tmp_invalid"),
            ("tmp-wrong-owner", {"FAKE_TMP_UID": "1000"}, "n8n_container_tmp_invalid"),
            ("stage-directory-wrong-owner", {"FAKE_CHOWN_DIRECTORY_OWNER": "2000:2000"}, "n8n_container_stage_verification_failed"),
            ("stage-directory-wrong-mode", {"FAKE_CHMOD_DIRECTORY_MODE": "750"}, "n8n_container_stage_verification_failed"),
            ("stage-file-wrong-owner", {"FAKE_CHOWN_FILE_OWNER": "2000:2000"}, "n8n_container_stage_verification_failed"),
            ("stage-file-wrong-mode", {"FAKE_CHMOD_FILE_MODE": "640"}, "n8n_container_stage_verification_failed"),
            ("root-import-identity", {"FAKE_IMPORT_UID": "0", "FAKE_IMPORT_GID": "0"}, "container_import_root"),
            ("import-user-unable-to-read", {"FAKE_MUTATE_AFTER_HASH": "unreadable"}, "n8n_command_failed"),
            ("hash-mismatch", {"FAKE_HASH_MISMATCH": "1"}, "n8n_container_content_mismatch"),
        ):
            with self.subTest(label=label):
                run_failed_case(label, faults, expected)

        with self.subTest(label="cleanup-failure-no-replay"):
            operation_path, container_root, count_path = run_failed_case(
                "cleanup-failure-no-replay",
                {"FAKE_CLEANUP_FAIL": "1"},
                "n8n_container_input_cleanup_failed",
            )
            custody_path = operation_path / "container-custody.json"
            custody = _read_json(custody_path)
            self.assertEqual(custody["cleanup_state"], "failed")
            self.assertFalse(custody["cleanup_verified"])
            self.assertTrue((container_root / "tmp").exists())
            self.assertEqual(count_path.read_text(encoding="utf-8"), "1")
            check = (
                f". '{SCRIPT}'; "
                f"$custody = Read-BoundedJsonFile '{custody_path}'; "
                f"$plan = Read-BoundedJsonFile '{operation_path / 'plan.json'}'; "
                "try { Assert-BoundedContainerCustody $custody $plan; Assert-BoundedContainerCleanupVerified $custody; exit 0 } "
                "catch { Write-Output $_.Exception.Message; exit 1 }"
            )
            retry = self._run_ps_command(check)
            self.assertNotEqual(retry.returncode, 0)
            self.assertIn("container_cleanup_not_verified", retry.stdout + retry.stderr)
            self.assertEqual(count_path.read_text(encoding="utf-8"), "1")

        custody_path = custody_operation_path / "container-custody.json"
        original_custody = _read_json(custody_path)
        for label, cleanup_state, cleanup_verified, expected in (
            ("existing-completion-pending-custody", "pending", False, "container_cleanup_not_verified"),
            ("existing-completion-unverified-custody", "cleaned", False, "container_cleanup_unverified"),
        ):
            with self.subTest(label=label):
                mutated = copy.deepcopy(original_custody)
                mutated["cleanup_state"] = cleanup_state
                mutated["cleanup_verified"] = cleanup_verified
                _write_json(custody_path, mutated)
                check = (
                    f". '{SCRIPT}'; "
                    f"$custody = Read-BoundedJsonFile '{custody_path}'; "
                    f"$plan = Read-BoundedJsonFile '{custody_path.parent / 'plan.json'}'; "
                    "try { Assert-BoundedContainerCustody $custody $plan; Assert-BoundedContainerCleanupVerified $custody; exit 0 } "
                    "catch { Write-Output $_.Exception.Message; exit 1 }"
                )
                completed = self._run_ps_command(check)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(expected, completed.stdout + completed.stderr)
                _write_json(custody_path, original_custody)


    def test_completion_claims_are_typed_and_bound_to_immutable_plan(self) -> None:
        operation_id, _ = self._capture("completion-claims")
        self._apply_successfully(operation_id)
        completion_path = self._operation_path(operation_id) / "completion-receipt.json"
        completion = _read_json(completion_path)
        original_completion = copy.deepcopy(completion)
        completion["original_preimage_state"] = "absent"
        completion["ownership"] = "created"
        _write_json(completion_path, completion)
        completed = self._run("Apply", operation_id, expect_success=False)
        self.assertIn("completion_preimage_state_mismatch", completed.stderr)
        completion = original_completion
        completion["complete"] = "false"
        _write_json(completion_path, completion)
        completed = self._run("Apply", operation_id, expect_success=False)
        self.assertIn("completion_receipt_state_invalid", completed.stderr)

    def test_private_root_acceptance_is_path_exact_not_substring_based(self) -> None:
        fake_root = Path(tempfile.mkdtemp(prefix="bounded-guard-exact-"))
        self.addCleanup(lambda: shutil.rmtree(fake_root, ignore_errors=True))
        subprocess.run(["git", "init", "-q", str(fake_root)], check=True, capture_output=True, text=True)
        nested = fake_root / "nested" / ".n8n-local/member-gateway-bounded-import/operations"
        self._run_private_guard(fake_root, nested, expected="private_path_not_canonical")

    def test_cursor_request_binds_required_parameters_without_overlap(self) -> None:
        command_text = (
            f'. "{SCRIPT}"; '
            f'$manifest = Read-BoundedJsonFile "{self.manifest_path}"; '
            'Assert-BoundedManifest $manifest; '
            'Get-BoundedCursorRequestUri $manifest'
        )
        completed = self._run_ps_command(command_text)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn(
            "https://reviewed.gateway.internal/v1/source/cursor-security?form_alias=member_registration&mapping_version=member-intake.v1",
            completed.stdout,
        )

        self.manifest["endpoints"]["source_cursor"] += "?form_alias=already-present"
        _write_json(self.manifest_path, self.manifest)
        collision_command = (
            f'. "{SCRIPT}"; '
            f'$manifest = Read-BoundedJsonFile "{self.manifest_path}"; '
            'try { Get-BoundedCursorRequestUri $manifest; exit 0 } '
            'catch { Write-Output $_.Exception.Message; exit 1 }'
        )
        completed = self._run_ps_command(collision_command)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("cursor_query_collision", completed.stdout + completed.stderr)

    def test_operation_identity_mismatch_blocks_before_dispatch(self) -> None:
        operation_id, _ = self._capture("identity-mismatch")
        plan_path = self._operation_path(operation_id) / "plan.json"
        plan = _read_json(plan_path)
        plan["identity_seed"]["watermark_value"] = "2026-09-18T00:00:01Z"
        _write_json(plan_path, plan)
        self._run("Apply", operation_id, expect_success=False)
        self.assertFalse((self._operation_path(operation_id) / "dispatch-receipt.json").exists())

    def test_deleted_plan_receipt_empty_orphan_corrupt_and_extra_material_fail_closed(self) -> None:
        operation_id, _ = self._capture("deleted-plan")
        (self._operation_path(operation_id) / "plan.json").unlink()
        self._run("Apply", operation_id, expect_success=False)

        operation_id, _ = self._capture("deleted-receipt")
        self._apply_successfully(operation_id)
        (self._operation_path(operation_id) / "dispatch-receipt.json").unlink()
        self._run("Apply", operation_id, expect_success=False)

        orphan_id = self._operation_id("empty-orphan")
        orphan_path = self._operation_path(orphan_id)
        orphan_path.mkdir(parents=True)
        self._make_fixture()
        self._run("Apply", orphan_id, expect_success=False)

        operation_id, _ = self._capture("corrupt-binding")
        _write_json(self._operation_path(operation_id) / "binding.json", {"corrupt": True})
        self._run("Apply", operation_id, expect_success=False)

        operation_id, _ = self._capture("extra-material")
        _write_json(self._operation_path(operation_id) / "unexpected.json", {"unexpected": True})
        self._run("Apply", operation_id, expect_success=False)

    def test_partial_create_missing_evidence_fails_and_valid_partial_create_does_not_replay(self) -> None:
        operation_id, _ = self._capture("partial-create", preimage="absent")
        (self._operation_path(operation_id) / "absence-evidence.json").unlink()
        self._run("Apply", operation_id, expect_success=False)

        operation_id, fixture = self._capture("partial-create-valid", preimage="absent", dispatch_mode="ambiguous")
        self._run("Apply", operation_id, expect_success=False)
        fixture["dispatch_mode"] = "success"
        _write_json(self.fixture_path, fixture)
        result = self._apply_successfully(operation_id)
        self.assertEqual(result["status"], "completed_existing_dispatch")
        self.assertEqual(result["mutation_attempted"], 0)

        operation_id, fixture = self._capture("partial-create-mismatch", preimage="absent", dispatch_mode="ambiguous")
        fixture["after_metadata"] = []
        fixture["after_workflow"] = None
        _write_json(self.fixture_path, fixture)
        self._run("Apply", operation_id, expect_success=False)
        dispatch_before = (self._operation_path(operation_id) / "dispatch-receipt.json").read_bytes()
        self._run("Apply", operation_id, expect_success=False)
        self.assertEqual(dispatch_before, (self._operation_path(operation_id) / "dispatch-receipt.json").read_bytes())
        self.assertFalse((self._operation_path(operation_id) / "completion-receipt.json").exists())

    def test_partial_retry_rederives_changed_preparation_inputs_and_rejects_them(self) -> None:
        operation_id, fixture = self._capture("partial-fresh-preparation", dispatch_mode="ambiguous")
        self._run("Apply", operation_id, expect_success=False)
        dispatch_before = (self._operation_path(operation_id) / "dispatch-receipt.json").read_bytes()
        self.manifest["question_mapping"]["name"] = "question-name-changed-002"
        _write_json(self.manifest_path, self.manifest)
        completed = self._run("Apply", operation_id, expect_success=False)
        self.assertIn("binding_digest_mismatch", completed.stderr)
        self.assertEqual(dispatch_before, (self._operation_path(operation_id) / "dispatch-receipt.json").read_bytes())

    def test_completed_noop_retry_rederives_changed_preparation_inputs_and_rejects_them(self) -> None:
        operation_id, _ = self._capture("completed-fresh-preparation")
        self._apply_successfully(operation_id)
        self.manifest["endpoints"]["gateway_ingest"] = f"{self.TEST_GATEWAY_ORIGIN}/v1/source-events-changed"
        _write_json(self.manifest_path, self.manifest)
        completed = self._run("Apply", operation_id, expect_success=False)
        self.assertIn("binding_digest_mismatch", completed.stderr)

    def test_completed_operation_is_noop_only_for_exact_target(self) -> None:
        operation_id, fixture = self._capture("completed-target")
        self._apply_successfully(operation_id)
        fixture["after_workflow"]["settings"]["executionOrder"] = "v2"
        _write_json(self.fixture_path, fixture)
        self._run("Apply", operation_id, expect_success=False)
        self.assertTrue((self._operation_path(operation_id) / "completion-receipt.json").is_file())

    def test_acl_failure_leaves_no_populated_private_output(self) -> None:
        operation_id = self._operation_id("acl-failure")
        self._make_fixture()
        fixture = _read_json(self.fixture_path)
        fixture["custody_mode"] = "acl_failure"
        _write_json(self.fixture_path, fixture)
        self._run("CapturePlan", operation_id, expect_success=False)
        private_root = ROOT / self.operations_root
        if private_root.exists():
            self.assertEqual([path for path in private_root.rglob("*") if path.is_file()], [])

    def test_acl_proof_precedes_first_populated_write(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        staging_function = source.split("function New-BoundedStagingRoot", 1)[1].split("function Write-BoundedCreateNewBytes", 1)[0]
        publish_function = source.split("function Publish-BoundedPlanArtifacts", 1)[1].split("function Write-BoundedOperationReceipt", 1)[0]
        self.assertIn("Set-BoundedProtectedAcl $stage", staging_function)
        self.assertLess(publish_function.index("$stage = New-BoundedStagingRoot"), publish_function.index("Write-BoundedCreateNewText"))
        self.assertNotIn(".tmp/", source)

    def _run_private_guard(self, root: Path, candidate: Path, *, expected: str) -> None:
        command_text = (
            f". '{SCRIPT}'; $script:BoundedTestOnly = $false; "
            f"try {{ Assert-BoundedPrivateDestination '{root}' '{candidate}'; exit 0 }} "
            f"catch {{ Write-Output $_.Exception.Message; exit 1 }}"
        )
        encoded = base64.b64encode(command_text.encode("utf-16le")).decode("ascii")
        completed = subprocess.run(
            [self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn(expected, completed.stdout + completed.stderr)

    def _run_ps_command(
        self,
        command_text: str,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        encoded = base64.b64encode(command_text.encode("utf-16le")).decode("ascii")
        environment = os.environ.copy()
        for name in (
            "N8N_WORKFLOW_HOOK_SCRIPT",
            "N8N_WORKFLOW_HOOK_AUTOLOAD",
            "N8N_WORKFLOW_VALIDATION_RULES",
            "N8N_WORKFLOW_VALIDATION_RULES_AUTOLOAD",
        ):
            environment.pop(name, None)
        if extra_env:
            environment.update(extra_env)
        return subprocess.run(
            [self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            timeout=30,
        )

    def test_private_destination_guards_reject_outside_nonignored_tracked_and_reparse_paths(self) -> None:
        fake_root = Path(tempfile.mkdtemp(prefix="bounded-guard-"))
        self.addCleanup(lambda: shutil.rmtree(fake_root, ignore_errors=True))
        subprocess.run(["git", "init", "-q", str(fake_root)], check=True, capture_output=True, text=True)
        canonical = fake_root / ".n8n-local/member-gateway-bounded-import/operations"

        self._run_private_guard(fake_root, fake_root / "outside" / "operations", expected="private_path_not_canonical")

        (fake_root / ".gitignore").write_text("", encoding="utf-8")
        self._run_private_guard(fake_root, canonical, expected="private_path_not_ignored")

        (fake_root / ".gitignore").write_text(".n8n-local/\n", encoding="utf-8")
        tracked = canonical / "tracked-marker"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("tracked\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(fake_root), "add", "-f", str(tracked.relative_to(fake_root))], check=True)
        self._run_private_guard(fake_root, canonical, expected="private_path_tracked")

        real_private = fake_root / "real-private"
        real_private.mkdir()
        shutil.rmtree(fake_root / ".n8n-local")
        link = fake_root / ".n8n-local"
        try:
            os.symlink(real_private, link, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            junction = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(link), str(real_private)],
                capture_output=True,
                text=True,
            )
            if junction.returncode != 0:
                self.assertIn("ReparsePoint", SCRIPT.read_text(encoding="utf-8"))
                self.skipTest(f"directory reparse point unavailable for direct test: {error}; {junction.stderr}")
        self._run_private_guard(fake_root, canonical, expected="unsafe_link")


if __name__ == "__main__":
    unittest.main()
