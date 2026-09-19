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
        _write_json(self.manifest_path, self.manifest)

    def _make_manifest(self) -> dict[str, Any]:
        manifest = copy.deepcopy(_read_json(MANIFEST_TEMPLATE))
        manifest["project"]["id"] = "project-security-001"
        manifest["workflow"]["id"] = "workflow-security-001"
        manifest["form"]["id"] = "form-security-001"
        manifest["endpoints"]["forms_responses"] = "https://forms.googleapis.com:443/v1/forms/form-security-001/responses"
        manifest["endpoints"]["source_cursor"] = "https://gateway.example.com/v1/source/cursor-security"
        manifest["endpoints"]["gateway_ingest"] = "https://gateway.example.com/v1/source-events-security"
        manifest["endpoints"]["page_checkpoint"] = "https://gateway.example.com/v1/source/cursor-security/page"
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

    def _build_command(self, mode: str, operation_id: str, *, extra_args: tuple[str, ...] = ()) -> tuple[list[str], dict[str, str]]:
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
        if mode == "Apply":
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
    ) -> subprocess.CompletedProcess[str]:
        command, environment = self._build_command(mode, operation_id, extra_args=extra_args)
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

    def _start(self, mode: str, operation_id: str, *, extra_args: tuple[str, ...] = (), extra_env: dict[str, str] | None = None) -> subprocess.Popen[str]:
        command, environment = self._build_command(mode, operation_id, extra_args=extra_args)
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

    def test_container_import_consumer_reads_exact_prepared_bytes_and_cleans_up(self) -> None:
        operation_id, fixture = self._capture("container-visible-import", dispatch_mode="container_visible_import")
        fake_docker_py = self.case_root / "fake-docker.py"
        fake_docker_cmd = self.case_root / "docker.cmd"
        container_root = self.case_root / "container-root"
        consumer_marker = self.case_root / "consumer.marker"
        cleanup_marker = self.case_root / "cleanup.marker"
        fake_docker_py.write_text(
            '''import hashlib
import os
import shutil
import sys
from pathlib import Path


args = sys.argv[1:]
root = Path(os.environ["FAKE_CONTAINER_ROOT"])


def resolve_container_path(path: str) -> Path:
    return root.joinpath(path.lstrip("/").replace("/", os.sep))


if args and args[0] == "inspect":
    if args[1] == "--format={{.Id}}":
        print("container-id-001")
        raise SystemExit(0)
    if args[1] == "--format={{.Image}}":
        print("image-id-001")
        raise SystemExit(0)
    raise SystemExit(10)

if args and args[0] == "cp":
    destination = args[2]
    separator = destination.find(":")
    if separator < 1:
        raise SystemExit(11)
    target = resolve_container_path(destination[separator + 1 :])
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args[1], target)
    raise SystemExit(0)

if args and args[0] == "exec":
    offset = 1
    if args[offset] == "-i":
        offset += 1
    if args[offset] == "-u":
        if args[offset + 1] != "0":
            raise SystemExit(12)
        offset += 2
    if args[offset] != "fake-container":
        raise SystemExit(12)
    command = args[offset + 1]
    command_arguments = args[offset + 2 :]
    if command == "id":
        print("1000")
        raise SystemExit(0)
    if command == "stat":
        target = command_arguments[-1]
        if target == "/tmp":
            print("1777:0:0:directory")
            raise SystemExit(0)
        path = resolve_container_path(target)
        if path.is_dir():
            print("700:1000:1000:directory")
            raise SystemExit(0)
        if path.is_file():
            print(f"600:1000:1000:regular file:{path.stat().st_size}")
            raise SystemExit(0)
        raise SystemExit(14)
    if command == "mkdir":
        resolve_container_path(command_arguments[-1]).mkdir(parents=True, exist_ok=False)
        raise SystemExit(0)
    if command in {"chown", "chmod"}:
        if not resolve_container_path(command_arguments[-1]).exists():
            raise SystemExit(14)
        raise SystemExit(0)
    if command == "sha256sum":
        path = resolve_container_path(command_arguments[-1])
        if not path.is_file():
            raise SystemExit(14)
        print(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {command_arguments[-1]}")
        raise SystemExit(0)
    if command == "test":
        if command_arguments[:2] != ["!", "-e"]:
            raise SystemExit(13)
        raise SystemExit(0 if not resolve_container_path(command_arguments[2]).exists() else 1)
    if command == "n8n":
        input_arguments = [argument for argument in command_arguments if argument.startswith("--input=")]
        if len(input_arguments) != 1:
            raise SystemExit(13)
        container_file = resolve_container_path(input_arguments[0][8:])
        if not container_file.is_file():
            raise SystemExit(14)
        actual_hash = hashlib.sha256(container_file.read_bytes()).hexdigest()
        if actual_hash != os.environ["EXPECTED_PREPARED_SHA"]:
            raise SystemExit(15)
        Path(os.environ["FAKE_CONSUMER_MARKER"]).write_text("container-read-ok", encoding="utf-8")
        raise SystemExit(0)
    if command == "rm":
        if os.environ.get("FAKE_CLEANUP_FAIL") == "1":
            raise SystemExit(17)
        container_directory = resolve_container_path(command_arguments[-1])
        if container_directory.exists():
            shutil.rmtree(container_directory)
        Path(os.environ["FAKE_CLEANUP_MARKER"]).write_text("container-cleaned", encoding="utf-8")
        raise SystemExit(0)

raise SystemExit(16)
''',
            encoding="utf-8",
        )
        fake_docker_cmd.write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0fake-docker.py" %*\r\nexit /b %ERRORLEVEL%\r\n',
            encoding="ascii",
        )
        prepared_path = self._operation_path(operation_id) / "prepared.workflow.json"
        extra_env = {
            "FAKE_CONTAINER_ROOT": str(container_root),
            "EXPECTED_PREPARED_SHA": hashlib.sha256(prepared_path.read_bytes()).hexdigest(),
            "FAKE_CONSUMER_MARKER": str(consumer_marker),
            "FAKE_CLEANUP_MARKER": str(cleanup_marker),
        }
        result = self._run(
            "Apply",
            operation_id,
            extra_args=("-N8nContainer", "fake-container", "-DockerExecutable", str(fake_docker_cmd)),
            extra_env=extra_env,
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "applied_and_verified")
        self.assertEqual(consumer_marker.read_text(encoding="utf-8"), "container-read-ok")
        self.assertEqual(cleanup_marker.read_text(encoding="utf-8"), "container-cleaned")
        if container_root.exists():
            self.assertEqual([path for path in container_root.rglob("*") if path.is_file()], [])
        custody = _read_json(self._operation_path(operation_id) / "container-custody.json")
        self.assertEqual(custody["container_id"], "container-id-001")
        self.assertEqual(custody["image_id"], "image-id-001")
        self.assertEqual(custody["tmp_mode"], "1777")
        self.assertEqual(custody["import_uid"], "1000")
        self.assertTrue(custody["stage_verified"])
        self.assertTrue(custody["mutation_possible"])
        self.assertEqual(custody["cleanup_state"], "cleaned")
        self.assertTrue(custody["cleanup_verified"])

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
            "https://gateway.example.com/v1/source/cursor-security?form_alias=member_registration&mapping_version=member-intake.v1",
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
        self.manifest["endpoints"]["gateway_ingest"] = "https://gateway.example.com/v1/source-events-changed"
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

    def _run_ps_command(self, command_text: str) -> subprocess.CompletedProcess[str]:
        encoded = base64.b64encode(command_text.encode("utf-16le")).decode("ascii")
        return subprocess.run(
            [self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
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
