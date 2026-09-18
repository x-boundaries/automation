"""Direct offline security regressions for the bounded member-gateway importer."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
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
        self.case_root = Path(tempfile.mkdtemp(prefix="bounded-import-security-", dir=ROOT))
        self.addCleanup(lambda: shutil.rmtree(self.case_root, ignore_errors=True))
        self.operations_root = (
            self.case_root.relative_to(ROOT).as_posix()
            + "/.n8n-local/member-gateway-bounded-import/operations"
        )
        self.manifest_path = self.case_root / "binding.json"
        self.fixture_path = self.case_root / "fixture.json"
        self.manifest = self._make_manifest()
        _write_json(self.manifest_path, self.manifest)

    def _make_manifest(self) -> dict[str, Any]:
        manifest = copy.deepcopy(_read_json(MANIFEST_TEMPLATE))
        manifest["project"]["id"] = "project-security-001"
        manifest["workflow"]["id"] = "workflow-security-001"
        manifest["form"]["id"] = "form-security-001"
        manifest["question_mapping"] = {
            "name": "question-name-001",
            "phone": "question-phone-001",
            "email": "question-email-001",
            "birthday_month": "question-birthday-001",
            "marketing_consent": "question-consent-001",
            "pdpa_acknowledged": "question-pdpa-001",
        }
        manifest["credential_roles"]["google_forms_oauth"]["credential_name"] = "google_forms_test_credential"
        manifest["credential_roles"]["gateway_bearer"]["credential_name"] = "gateway_test_credential"
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
        return f"security-{label}-{uuid4().hex[:12]}"

    def _run(self, mode: str, operation_id: str, *, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
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
        environment = os.environ.copy()
        for name in (
            "N8N_WORKFLOW_HOOK_SCRIPT",
            "N8N_WORKFLOW_HOOK_AUTOLOAD",
            "N8N_WORKFLOW_VALIDATION_RULES",
            "N8N_WORKFLOW_VALIDATION_RULES_AUTOLOAD",
        ):
            environment.pop(name, None)
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
        self.assertEqual(dispatch_before, (operation_path / "dispatch-receipt.json").read_bytes())

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

    def test_pre_dispatch_failure_can_retry_only_after_fresh_evidence(self) -> None:
        operation_id, fixture = self._capture("pre-dispatch-retry", dispatch_mode="pre_dispatch_failure")
        self._run("Apply", operation_id, expect_success=False)
        operation_path = self._operation_path(operation_id)
        self.assertFalse((operation_path / "dispatch-receipt.json").exists())
        fixture["dispatch_mode"] = "success"
        _write_json(self.fixture_path, fixture)
        result = self._apply_successfully(operation_id)
        self.assertEqual(result["mutation_attempted"], 1)

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
        self.assertNotIn(".tmp", source)

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
