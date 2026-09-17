import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INSTALLER = SCRIPTS / "install_ac2_member_gateway_worker.ps1"
LAUNCHER = SCRIPTS / "launch_ac2_member_gateway_worker.ps1"
PROBE = SCRIPTS / "test_ac2_member_gateway_autocount_dependencies.ps1"
RENDERER = SCRIPTS / "render_member_forms_gateway_binding.ps1"
IMPORTER = ROOT / "n8n-workflows/scripts/import-n8n-workflows-live.ps1"
TEMPLATE = ROOT / "config/member_forms_gateway_ingest.binding.template.json"
WORKFLOW = ROOT / "n8n-workflows/member_forms_gateway_ingest.workflow.json"


class WorkerDeploymentStaticTests(unittest.TestCase):
    def setUp(self):
        self.installer = INSTALLER.read_text(encoding="utf-8")
        self.launcher = LAUNCHER.read_text(encoding="utf-8")
        self.probe = PROBE.read_text(encoding="utf-8")

    def test_package_file_set_is_exact_and_manifest_is_deterministic(self):
        expected = {
            "ac2_member_gateway_worker.ps1",
            "ac2_member_gateway_worker_lib.ps1",
            "ac2_member_gateway_autocount_adapter.ps1",
            "launch_ac2_member_gateway_worker.ps1",
            "test_ac2_member_gateway_autocount_dependencies.ps1",
        }
        block = self.installer.split("$packageFiles = @(", 1)[1].split(")", 1)[0]
        self.assertEqual(set(re.findall(r'"([^"]+\.ps1)"', block)), expected)
        self.assertIn("Get-XbFileSha256 -Path $path", self.installer)
        self.assertNotIn("Get-Date", self.installer)
        self.assertNotIn("utc_timestamp", self.installer)

    def test_disabled_proof_is_secret_free_and_cannot_enable_production(self):
        guard = self.launcher.index('if ($LauncherMode -eq "Production")')
        disabled_comment = self.launcher.index("DisabledProof never enters this branch")
        self.assertLess(disabled_comment, guard)
        for marker in ("worker-token.clixml", "autocount-password.clixml"):
            self.assertGreater(self.launcher.index(marker), guard)
        task_action = re.search(r"New-ScheduledTaskAction[^\n]+", self.installer).group(0)
        self.assertIn("-Mode DisabledProof", task_action)
        self.assertNotIn("EnableProductionWorker", task_action)
        self.assertNotIn("EnableProductionAdapter", task_action)

    def test_secrets_are_child_process_only_and_never_args_logs_or_persistent_env(self):
        self.assertIn('EnvironmentVariables["XB_MEMBER_GATEWAY_WORKER_TOKEN"]', self.launcher)
        self.assertIn('EnvironmentVariables["XB_AC2_PASSWORD"]', self.launcher)
        self.assertIn('EnvironmentVariables.Remove("XB_AC2_SESSION_FACTORY")', self.launcher)
        self.assertNotIn("SetEnvironmentVariable", self.launcher)
        self.assertNotRegex(self.launcher, r"arguments\s*\+=.*(?:workerToken|ac2Password)")
        log_block = self.launcher.split("$line = [ordered]@{", 1)[1].split("}", 1)[0]
        self.assertEqual(
            set(re.findall(r"^\s*([a-z_]+)\s*=", log_block, re.MULTILINE)),
            {"utc_timestamp", "run_id", "launcher_mode", "exit_code", "terminal_status", "write_count", "support_reference"},
        )
        for forbidden in ("stdout", "stderr", "url", "path", "command", "credential", "member", "autocount"):
            self.assertNotIn(forbidden, log_block.lower())

    def test_recovery_bearer_is_absent_from_normal_package_surfaces(self):
        text = self.installer + self.launcher + self.probe
        for marker in ("RECOVERY", "writer_termination_recovery", "recovery bearer"):
            self.assertNotIn(marker.lower(), text.lower())

    def test_dependency_probe_is_reflection_only_and_cannot_authenticate_or_contact(self):
        self.assertIn("ReflectionOnlyLoadFrom", self.probe)
        assembly_block = self.probe.split("$requiredAssemblies = @(", 1)[1].split(")", 1)[0]
        self.assertEqual(set(re.findall(r'"(AutoCount[^\"]*\.dll)"', assembly_block)), {
            "AutoCount.dll", "AutoCount.Accounting.dll", "AutoCount.Invoicing.dll", "AutoCount.ImportExport.dll", "AutoCount.Tools.dll"
        })
        for forbidden in ("Invoke-RestMethod", "Invoke-WebRequest", "Authenticate", '"Login"', "CreateAutoCountDefaultDBSetting", "GetEnvironmentVariable", "Import-Clixml"):
            self.assertNotIn(forbidden, self.probe)
        self.assertNotRegex(self.probe, r"\.Invoke\(")
        self.assertIn('"Create", "GetMember", "NewMember", "SaveMember"', self.probe)

    def test_task_and_acl_contracts_are_structural_and_fail_closed(self):
        for marker in (
            '$taskPath = "\\X-Boundaries\\"',
            '$taskName = "AC2 Member Gateway Worker"',
            "Disable-ScheduledTask",
            "Triggers).Count -ne 0",
            '"IgnoreNew"',
            '"PT10M"',
            "RestartCount -ne 0",
            "StartWhenAvailable",
            'ValidateSet("Install", "Config", "Secrets", "Logs", "Rollback")',
            '"${WorkerAccount}:(OI)(CI)RX"',
            '"${WorkerAccount}:(OI)(CI)R"',
            '"${WorkerAccount}:(OI)(CI)M"',
        ):
            self.assertIn(marker, self.installer)
        self.assertNotRegex(self.installer, r"S-1-5-21-[0-9-]+")
        self.assertNotIn("New-Service", self.installer)

    def test_rollback_is_slice_owned_and_refuses_preimages(self):
        for marker in ("install_preimage_exists", "runtime_preimage_exists", "task_preimage_exists"):
            self.assertIn(marker, self.installer)
        self.assertIn('@("config", "secrets", "logs", "rollback")', self.installer)
        for forbidden in ("AutoCount.dll", "trusted root", "postgres", "firewall", "cursor", "gateway/ingress"):
            self.assertNotIn(forbidden.lower(), self.installer.lower())

    def test_log_rotation_has_daily_30_day_and_100_mib_bounds(self):
        self.assertIn('"launcher-{0}.jsonl"', self.launcher)
        self.assertIn("AddDays(-30)", self.launcher)
        self.assertIn("100MB", self.launcher)
        self.assertIn("Sort-Object LastWriteTimeUtc, Name", self.launcher)


class N8nDeploymentStaticTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.renderer = RENDERER.read_text(encoding="utf-8")
        self.importer = IMPORTER.read_text(encoding="utf-8")
        self.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))

    def test_credential_reference_contract_is_exact_and_deployment_only(self):
        credentials = self.template["credentials"]
        self.assertEqual(credentials["google_forms_responses_read"]["type"], "googleOAuth2Api")
        self.assertEqual(credentials["google_forms_responses_read"]["nodes"], ["Google Forms single page (configured outside repo)"])
        self.assertEqual(credentials["gateway_source_bearer"]["type"], "httpBearerAuth")
        self.assertEqual(credentials["gateway_source_bearer"]["nodes"], [
            "Read durable source cursor",
            "Protected XB Gateway ingest (configured outside repo)",
            "Commit durable page checkpoint",
        ])
        self.assertFalse(any("credentials" in node for node in self.workflow["nodes"]))
        self.assertNotIn("Authorization", WORKFLOW.read_text(encoding="utf-8"))

    def test_binding_template_and_renderer_are_canonical_and_closed(self):
        canonical = self.template["canonical"]
        self.assertEqual(canonical["commit"], "52308eebb95d9bc4f5a09fbe036c197a5e95b176")
        self.assertEqual(canonical["tree"], "3ab7e3363b7189d7d1bb79ec8a1988eb23e1c7b3")
        self.assertEqual(canonical["parent"], "1052f40e62a5ff5884c8ba49aac8114cc634b81c")
        self.assertEqual(canonical["workflow_blob"], "4eeff984fb8a9a2fb569c1648e8dd4350c45b2fe")
        self.assertIn('$expectedOutput = ".tmp/member-gateway-g3/member_forms_gateway_ingest.binding.json"', self.renderer)
        self.assertIn("binding_placeholder_duplicate", self.renderer)
        self.assertIn("binding_placeholder_set_invalid", self.renderer)
        self.assertIn("binding_question_key_set_invalid", self.renderer)
        self.assertIn("binding_question_map_hash_mismatch", self.renderer)
        self.assertIn("binding_output_not_ignored", self.renderer)
        self.assertIn("binding_output_acl_permissive", self.renderer)
        self.assertIn("binding_secret_content_detected", self.renderer)

    def test_posture_contract_remains_inactive_manual_private_and_retention_free(self):
        posture = self.template["posture"]
        self.assertEqual(posture, {
            "active": False,
            "trigger": "manual_only",
            "available_in_mcp": False,
            "save_manual_executions": False,
            "save_success_execution": "none",
            "save_error_execution": "none",
            "static_data": None,
            "pin_data_present": False,
        })
        self.assertFalse(self.workflow["active"])
        self.assertIsNone(self.workflow["staticData"])
        self.assertNotIn("pinData", self.workflow)
        self.assertFalse(self.workflow["settings"]["availableInMCP"])

    def test_single_workflow_mode_is_exact_and_broad_mode_remains_separate(self):
        for marker in (
            "WorkflowFile and WorkflowDir are mutually exclusive",
            "Get-SingleCanonicalWorkflowFile",
            '"member_forms_gateway_ingest.workflow.json"',
            "immediate canonical child",
            "SingleTargetAmbiguous",
            "SingleTargetArchived",
            "SingleTargetActiveOrScheduled",
            "preimage was captured",
            "Assert-BoundedImportedWorkflow",
            "Bounded import readback id mismatch",
        ):
            self.assertIn(marker, self.importer)
        selection = "$workflowFiles = @(if ($SingleWorkflowMode) { Get-SingleCanonicalWorkflowFile $WorkflowDirPath } else { Get-RootWorkflowFiles $WorkflowDirPath })"
        self.assertIn(selection, self.importer)
        self.assertIn("RestartContainerAfterImport is forbidden in bounded single-workflow mode", self.importer)

    def test_no_business_execution_surface_is_added(self):
        text = self.renderer + TEMPLATE.read_text(encoding="utf-8")
        for forbidden in ("Invoke-RestMethod", "Invoke-WebRequest", "SaveMember", "job claim", "source execution", "n8n import:workflow"):
            self.assertNotIn(forbidden.lower(), text.lower())


@unittest.skipUnless(os.name == "nt", "Windows PowerShell behavior is Windows-only")
@unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell is required")
class WorkerDeploymentPowerShellTests(unittest.TestCase):
    def test_validate_only_manifest_and_disabled_proof(self):
        validate = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-File", str(INSTALLER), "-Operation", "ValidateOnly"],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
        )
        self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)
        manifest = json.loads(validate.stdout)
        self.assertEqual(manifest["task"]["trigger_count"], 0)
        self.assertFalse(manifest["task"]["enabled"])
        self.assertEqual(manifest["task"]["action_mode"], "DisabledProof")

        with tempfile.TemporaryDirectory(prefix="xb-worker-proof-") as temp:
            root = Path(temp)
            install = root / "install"
            runtime = root / "runtime"
            install.mkdir()
            for name in ("ac2_member_gateway_worker.ps1", "ac2_member_gateway_worker_lib.ps1", "ac2_member_gateway_autocount_adapter.ps1"):
                shutil.copy(SCRIPTS / name, install / name)
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-File", str(LAUNCHER), "-Mode", "DisabledProof", "-InstallRoot", str(install), "-RuntimeRoot", str(runtime)],
                cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            output = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(output["terminal_status"], "disabled_proof_pass")
            self.assertEqual(output["write_count"], 0)
            self.assertFalse((runtime / "config").exists())
            self.assertFalse((runtime / "secrets").exists())
            logs = list((runtime / "logs").glob("launcher-*.jsonl"))
            self.assertEqual(len(logs), 1)
            event = json.loads(logs[0].read_text(encoding="utf-8-sig").strip())
            self.assertEqual(set(event), {"utc_timestamp", "run_id", "launcher_mode", "exit_code", "terminal_status", "write_count", "support_reference"})

    def test_renderer_template_validation_only(self):
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-File", str(RENDERER), "-ValidateTemplateOnly"],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "template_valid")
        self.assertTrue(output["canonical_workflow_match"])

    def test_renderer_writes_only_private_ignored_closed_manifest(self):
        questions = {
            "name": "q_name_ref", "phone": "q_phone_ref", "email": "q_email_ref",
            "birthday_month": "q_birth_ref", "marketing_consent": "q_marketing_ref",
            "pdpa_acknowledged": "q_pdpa_ref",
        }
        canonical = json.dumps(questions, sort_keys=True, separators=(",", ":"))
        accepted_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        output_path = ROOT / ".tmp/member-gateway-g3/member_forms_gateway_ingest.binding.json"
        with tempfile.TemporaryDirectory(prefix="xb-binding-input-") as temp:
            question_path = Path(temp) / "questions.json"
            question_path.write_text(json.dumps(questions), encoding="utf-8")
            command = [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-File", str(RENDERER),
                "-FormsApiUrl", "https://forms.googleapis.com/v1/forms/form_ref_01/responses",
                "-SourceCursorUrl", "https://gateway.invalid/v1/source/cursor",
                "-GatewayIngestUrl", "https://gateway.invalid/v1/source-events",
                "-PageCheckpointUrl", "https://gateway.invalid/v1/source/cursor/page",
                "-FormIdReference", "form_ref_01", "-QuestionMapPath", str(question_path),
                "-AcceptedQuestionMapSha256", accepted_hash,
                "-CursorBindingReference", "cursor_ref_01", "-WatermarkBindingReference", "watermark_ref_01",
                "-GoogleCredentialReference", "google_cred_ref_01", "-GatewayCredentialReference", "gateway_cred_ref_01",
                "-TargetProjectReference", "project_ref_01", "-TargetWorkflowReference", "workflow_ref_01",
                "-TargetPreimageReference", "preimage_absent_ref_01",
            ]
            try:
                result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                manifest = json.loads(output_path.read_text(encoding="utf-8-sig"))
                self.assertEqual(manifest["source"]["question_map_sha256"], accepted_hash)
                self.assertEqual(manifest["credentials"]["gateway_source_bearer"]["type"], "httpBearerAuth")
                self.assertFalse(manifest["posture"]["active"])
            finally:
                output_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
