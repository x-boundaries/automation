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
        self.assertIn("sha256 = [string]$entry[0].sha256", self.installer)
        self.assertIn("Assert-XbStagedPackageIdentity", self.installer)
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
            "New-ScheduledTaskSettingsSet",
            "-Disable",
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
        canonical = self.template["source_provenance"]
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
        self.assertIn("binding_output_preimage_exists", self.renderer)
        self.assertIn("binding_deployment_admission_mismatch", self.renderer)
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
    def test_task_duration_registration_is_initially_disabled_and_invalid_contract_rolls_back(self):
        command = rf'''
. '{INSTALLER}' -LibraryOnly
$global:registered = $false; $global:unregistered = $false; $global:invalid = $false; $global:first_error = $null
$WorkerAccount = 'DOMAIN\worker'
$secure = [Security.SecureString]::new(); foreach($char in 'test-only'.ToCharArray()){{$secure.AppendChar($char)}}; $secure.MakeReadOnly()
$TaskCredential = [Management.Automation.PSCredential]::new($WorkerAccount, $secure)
function New-ScheduledTaskAction {{ param($Execute,$Argument); [pscustomobject]@{{ Arguments=$Argument }} }}
function New-ScheduledTaskSettingsSet {{ param($MultipleInstances,$ExecutionTimeLimit,$RestartCount,$StartWhenAvailable,[switch]$Disable); [pscustomobject]@{{ MultipleInstances=$MultipleInstances; ExecutionTimeLimit=$ExecutionTimeLimit; RestartCount=$RestartCount; StartWhenAvailable=$StartWhenAvailable; InitiallyDisabled=[bool]$Disable }} }}
function New-ScheduledTaskPrincipal {{ [pscustomobject]@{{}} }}
function New-ScheduledTask {{ param($Action,$Settings,$Principal); [pscustomobject]@{{ Actions=@($Action); Settings=$Settings; Triggers=@() }} }}
function Register-ScheduledTask {{ param($TaskPath,$TaskName,$InputObject,$User,$Password,[switch]$Force); $global:registered=$true; $global:registeredTask=$InputObject }}
function Get-ScheduledTask {{ [pscustomobject]@{{ State='Disabled'; Triggers=@(); Actions=$global:registeredTask.Actions; Settings=$global:registeredTask.Settings }} }}
function Unregister-ScheduledTask {{ $global:unregistered=$true }}
try {{ Register-XbWorkerScheduledTask -LauncherPath 'C:\Program Files\X-Boundaries\MemberGatewayWorker\launch_ac2_member_gateway_worker.ps1' }} catch {{ $global:first_error=$_.Exception.Message }}
$accepted = $global:registered -and $global:registeredTask.Settings.InitiallyDisabled -and $global:registeredTask.Settings.ExecutionTimeLimit -eq [TimeSpan]::FromMinutes(10)
function Get-ScheduledTask {{ [pscustomobject]@{{ State='Disabled'; Triggers=@(); Actions=$global:registeredTask.Actions; Settings=[pscustomobject]@{{MultipleInstances='IgnoreNew';ExecutionTimeLimit='PT11M';RestartCount=0;StartWhenAvailable=$false}} }} }}
try {{ Register-XbWorkerScheduledTask -LauncherPath 'x'; }} catch {{ $global:invalid=$true; if($global:XbTaskCreated){{Unregister-ScheduledTask}} }}
[pscustomobject]@{{accepted=$accepted;invalid=$global:invalid;rolled_back=$global:unregistered;registered=$global:registered;task_created=$global:XbTaskCreated;initially_disabled=$global:registeredTask.Settings.InitiallyDisabled;limit=[string]$global:registeredTask.Settings.ExecutionTimeLimit;first_error=$global:first_error}} | ConvertTo-Json -Compress
'''
        result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-Command", command], cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(observed["accepted"], observed)
        self.assertTrue(observed["invalid"], observed)
        self.assertTrue(observed["rolled_back"], observed)

    def test_launcher_concurrently_drains_and_reaps_failure_cases(self):
        cases = {
            "stderr_saturation": ("[Console]::Error.Write(('PRIVATE-ERR-' + ('x' * 1048576))); [Console]::Out.Write('{\"status\":\"disabled\",\"writes\":0,\"dispatch_fence\":false}')", 0),
            "malformed": ("[Console]::Out.Write('not-json')", 1),
            "nonzero": ("[Console]::Out.Write('{\"status\":\"disabled\",\"writes\":0,\"dispatch_fence\":false}'); exit 7", 1),
        }
        for label, (body, expected) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(prefix="xb-launch-case-") as temp:
                root = Path(temp); install = root / "install"; runtime = root / "runtime"; install.mkdir()
                (install / "ac2_member_gateway_worker.ps1").write_text(body, encoding="utf-8")
                result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-File", str(LAUNCHER), "-Mode", "DisabledProof", "-InstallRoot", str(install), "-RuntimeRoot", str(runtime), "-ExecutionTimeoutMilliseconds", "5000"], cwd=ROOT, capture_output=True, text=True, check=False, timeout=20)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("PRIVATE-ERR", result.stdout + result.stderr)
                output = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(output["terminal_status"], "disabled_proof_pass" if expected == 0 else "launcher_failed")

        with tempfile.TemporaryDirectory(prefix="xb-launch-hang-") as temp:
            root = Path(temp); install = root / "install"; runtime = root / "runtime"; install.mkdir(); pid_path = root / "pid.txt"
            (install / "ac2_member_gateway_worker.ps1").write_text(f"$PID | Set-Content -LiteralPath '{pid_path}'; Start-Sleep -Seconds 30", encoding="utf-8")
            result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-File", str(LAUNCHER), "-Mode", "DisabledProof", "-InstallRoot", str(install), "-RuntimeRoot", str(runtime), "-ExecutionTimeoutMilliseconds", "500"], cwd=ROOT, capture_output=True, text=True, check=False, timeout=20)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            pid = int(pid_path.read_text().strip())
            probe = subprocess.run(["powershell", "-NoProfile", "-Command", f"if(Get-Process -Id {pid} -ErrorAction SilentlyContinue){{exit 1}}"], check=False)
            self.assertEqual(probe.returncode, 0, "timed-out owned child must be reaped")

    def test_uninstall_ownership_failures_leave_all_state_untouched(self):
        package_names = ("ac2_member_gateway_worker.ps1", "ac2_member_gateway_worker_lib.ps1", "ac2_member_gateway_autocount_adapter.ps1", "launch_ac2_member_gateway_worker.ps1", "test_ac2_member_gateway_autocount_dependencies.ps1")
        for label in ("malformed", "partial", "unexpected", "replaced-task"):
            with self.subTest(label=label), tempfile.TemporaryDirectory(prefix="xb-uninstall-") as temp:
                root = Path(temp); install = root / "install"; runtime = root / "runtime"; install.mkdir()
                for child in ("config", "secrets", "logs", "rollback"): (runtime / child).mkdir(parents=True)
                entries = []
                for name in package_names:
                    data = ("owned-" + name).encode(); (install / name).write_bytes(data); entries.append({"name": name, "sha256": hashlib.sha256(data).hexdigest()})
                manifest = {"schema_version":"xb.member.gateway.worker.installation.v1", "reviewed_source":{"commit":"1"*40,"tree":"2"*40}, "install_root":"C:\\Program Files\\X-Boundaries\\MemberGatewayWorker\\", "runtime_root":"C:\\ProgramData\\X-Boundaries\\MemberGatewayWorker\\", "package_files":entries, "task":{"path":"\\X-Boundaries\\","name":"AC2 Member Gateway Worker","enabled":False,"trigger_count":0,"action_mode":"DisabledProof","production_switches":[],"multiple_instances":"IgnoreNew","execution_time_limit":"PT10M","restart_count":0,"start_when_available":False}, "rollback_owned_roots":["config","secrets","logs","rollback"]}
                manifest_path = install / "installation-manifest.json"
                if label == "malformed": manifest_path.write_text("{", encoding="utf-8")
                else:
                    if label == "partial": manifest["package_files"] = manifest["package_files"][:-1]
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                if label == "unexpected": (install / "foreign.txt").write_text("foreign", encoding="utf-8")
                before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                bad_action = "-Mode Production" if label == "replaced-task" else "-Mode DisabledProof"
                command = f". '{INSTALLER}' -LibraryOnly; $InstallRoot='{install}'; $RuntimeRoot='{runtime}'; function Get-ScheduledTask {{ [pscustomobject]@{{State='Disabled';Triggers=@();Actions=@([pscustomobject]@{{Arguments='{bad_action}'}});Settings=[pscustomobject]@{{MultipleInstances='IgnoreNew';ExecutionTimeLimit='PT10M';RestartCount=0;StartWhenAvailable=$false}}}} }}; try {{ Assert-XbUninstallOwnership; exit 9 }} catch {{ exit 0 }}"
                result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-Command", command], cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                self.assertEqual(after, before)

    def test_validate_only_manifest_and_disabled_proof(self):
        with tempfile.TemporaryDirectory(prefix="xb-reviewed-package-") as temp:
            checkout = Path(temp) / "repo"
            scripts = checkout / "scripts"
            scripts.mkdir(parents=True)
            for name in (
                "install_ac2_member_gateway_worker.ps1", "ac2_member_gateway_worker.ps1",
                "ac2_member_gateway_worker_lib.ps1", "ac2_member_gateway_autocount_adapter.ps1",
                "launch_ac2_member_gateway_worker.ps1", "test_ac2_member_gateway_autocount_dependencies.ps1",
            ):
                shutil.copy(SCRIPTS / name, scripts / name)
            subprocess.run(["git", "init"], cwd=checkout, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "WJ"], cwd=checkout, check=True)
            subprocess.run(["git", "config", "user.email", "10020253+weijunswj@users.noreply.github.com"], cwd=checkout, check=True)
            subprocess.run(["git", "add", "scripts"], cwd=checkout, check=True)
            subprocess.run(["git", "commit", "-m", "reviewed package"], cwd=checkout, check=True, capture_output=True)
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
            tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=checkout, text=True).strip()
            names = (
                "ac2_member_gateway_worker.ps1", "ac2_member_gateway_worker_lib.ps1",
                "ac2_member_gateway_autocount_adapter.ps1", "launch_ac2_member_gateway_worker.ps1",
                "test_ac2_member_gateway_autocount_dependencies.ps1",
            )
            identity = {
                "schema_version": "xb.member.gateway.worker.reviewed-package.v1",
                "source": {"commit": commit, "tree": tree},
                "package_files": [
                    {"name": name, "sha256": hashlib.sha256((scripts / name).read_bytes()).hexdigest(),
                     "git_blob": subprocess.check_output(["git", "rev-parse", f"HEAD:scripts/{name}"], cwd=checkout, text=True).strip()}
                    for name in names
                ],
            }
            identity_path = checkout / "reviewed-package.json"
            identity_path.write_text(json.dumps(identity), encoding="utf-8")
            validate = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-File", str(scripts / INSTALLER.name), "-Operation", "ValidateOnly", "-ReviewedPackageManifestPath", str(identity_path)],
                cwd=checkout, capture_output=True, text=True, check=False, timeout=120,
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

    def test_reviewed_package_rejects_changed_missing_and_staged_mismatch(self):
        names = ("ac2_member_gateway_worker.ps1", "ac2_member_gateway_worker_lib.ps1", "ac2_member_gateway_autocount_adapter.ps1", "launch_ac2_member_gateway_worker.ps1", "test_ac2_member_gateway_autocount_dependencies.ps1")
        with tempfile.TemporaryDirectory(prefix="xb-package-negative-") as temp:
            checkout = Path(temp) / "repo"; scripts = checkout / "scripts"; scripts.mkdir(parents=True)
            shutil.copy(INSTALLER, scripts / INSTALLER.name)
            for name in names: shutil.copy(SCRIPTS / name, scripts / name)
            subprocess.run(["git", "init"], cwd=checkout, check=True, capture_output=True); subprocess.run(["git", "config", "user.name", "WJ"], cwd=checkout, check=True); subprocess.run(["git", "config", "user.email", "10020253+weijunswj@users.noreply.github.com"], cwd=checkout, check=True)
            subprocess.run(["git", "add", "scripts"], cwd=checkout, check=True); subprocess.run(["git", "commit", "-m", "reviewed"], cwd=checkout, check=True, capture_output=True)
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip(); tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=checkout, text=True).strip()
            identity = {"schema_version":"xb.member.gateway.worker.reviewed-package.v1","source":{"commit":commit,"tree":tree},"package_files":[{"name":name,"sha256":hashlib.sha256((scripts/name).read_bytes()).hexdigest(),"git_blob":subprocess.check_output(["git","rev-parse",f"HEAD:scripts/{name}"],cwd=checkout,text=True).strip()} for name in names]}
            identity_path = checkout / "identity.json"
            def run(candidate):
                identity_path.write_text(json.dumps(candidate), encoding="utf-8")
                return subprocess.run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-NonInteractive","-File",str(scripts/INSTALLER.name),"-Operation","ValidateOnly","-ReviewedPackageManifestPath",str(identity_path)],cwd=checkout,capture_output=True,text=True,check=False,timeout=120)
            missing = json.loads(json.dumps(identity)); missing["package_files"] = missing["package_files"][:-1]
            self.assertEqual(run(missing).returncode, 1)
            (scripts / names[0]).write_text("changed", encoding="utf-8")
            self.assertEqual(run(identity).returncode, 1)
            shutil.copy(SCRIPTS / names[0], scripts / names[0])
            identity_path.write_text(json.dumps(identity), encoding="utf-8")
            stage = checkout / "stage"; stage.mkdir()
            for name in names: shutil.copy(scripts / name, stage / name)
            (stage / names[-1]).write_text("staged mismatch", encoding="utf-8")
            command = f". '{scripts / INSTALLER.name}' -LibraryOnly; $i=Get-Content -Raw '{identity_path}'|ConvertFrom-Json; try{{Assert-XbStagedPackageIdentity -StageRoot '{stage}' -ReviewedIdentity $i;exit 9}}catch{{exit 0}}"
            staged = subprocess.run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-NonInteractive","-Command",command],cwd=checkout,check=False)
            self.assertEqual(staged.returncode, 0)

    def test_renderer_template_validation_only(self):
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-File", str(RENDERER), "-ValidateTemplateOnly"],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "template_valid")
        self.assertTrue(output["source_provenance_workflow_match"])

    def test_renderer_rejects_closed_schema_fixed_value_and_placeholder_relocation_drift(self):
        original = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        cases = {}
        extra = json.loads(json.dumps(original)); extra["unexpected"] = True; cases["extra"] = extra
        active = json.loads(json.dumps(original)); active["posture"]["active"] = True; cases["active"] = active
        cred = json.loads(json.dumps(original)); cred["credentials"]["gateway_source_bearer"]["type"] = "httpBasicAuth"; cases["credential"] = cred
        role = json.loads(json.dumps(original)); role["credentials"]["gateway_source_bearer"]["nodes"][0] = "Wrong node"; cases["node-role"] = role
        relocated = json.loads(json.dumps(original)); relocated["endpoints"]["forms_api_url"], relocated["endpoints"]["source_cursor_url"] = relocated["endpoints"]["source_cursor_url"], relocated["endpoints"]["forms_api_url"]; cases["placeholder-location"] = relocated
        temp_path = ROOT / ".tmp" / "member-gateway-g3" / "template-under-test.json"
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            for label, candidate in cases.items():
                with self.subTest(label=label):
                    temp_path.write_text(json.dumps(candidate), encoding="utf-8")
                    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-File", str(RENDERER), "-TemplatePath", temp_path.relative_to(ROOT).as_posix(), "-ValidateTemplateOnly"], cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        finally:
            temp_path.unlink(missing_ok=True)

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
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip()
            parent = subprocess.check_output(["git", "rev-parse", "HEAD^"], cwd=ROOT, text=True).strip()
            command += ["-AdmittedDeploymentCommit", commit, "-AdmittedDeploymentTree", tree, "-AdmittedDeploymentParent", parent]
            try:
                result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                manifest = json.loads(output_path.read_text(encoding="utf-8-sig"))
                self.assertEqual(manifest["source"]["question_map_sha256"], accepted_hash)
                self.assertEqual(manifest["credentials"]["gateway_source_bearer"]["type"], "httpBearerAuth")
                self.assertFalse(manifest["posture"]["active"])
            finally:
                output_path.unlink(missing_ok=True)

    def test_renderer_preflight_and_acl_failures_never_overwrite_or_leave_private_output(self):
        output_path = ROOT / ".tmp/member-gateway-g3/member_forms_gateway_ingest.binding.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"sentinel-do-not-overwrite")
        common = ["-FormsApiUrl", "https://forms.googleapis.com/v1/forms/form_ref_01/responses", "-SourceCursorUrl", "https://gateway.invalid/v1/source/cursor", "-GatewayIngestUrl", "https://gateway.invalid/v1/source-events", "-PageCheckpointUrl", "https://gateway.invalid/v1/source/cursor/page", "-FormIdReference", "form_ref_01"]
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(); tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip(); parent = subprocess.check_output(["git", "rev-parse", "HEAD^"], cwd=ROOT, text=True).strip()
        with tempfile.TemporaryDirectory(prefix="xb-render-failure-") as temp:
            questions = {"name":"q1","phone":"q2","email":"q3","birthday_month":"q4","marketing_consent":"q5","pdpa_acknowledged":"q6"}
            qpath = Path(temp) / "q.json"; qpath.write_text(json.dumps(questions), encoding="utf-8")
            qhash = hashlib.sha256(json.dumps(questions, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            args = common + ["-QuestionMapPath", str(qpath), "-AcceptedQuestionMapSha256", qhash, "-CursorBindingReference", "c", "-WatermarkBindingReference", "w", "-GoogleCredentialReference", "g", "-GatewayCredentialReference", "b", "-TargetProjectReference", "p", "-TargetWorkflowReference", "t", "-TargetPreimageReference", "r", "-AdmittedDeploymentCommit", commit, "-AdmittedDeploymentTree", tree, "-AdmittedDeploymentParent", parent]
            result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-File", str(RENDERER), *args], cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(output_path.read_bytes(), b"sentinel-do-not-overwrite")
            output_path.unlink()
            ps_args = " ".join(value if value.startswith("-") else "'" + value.replace("'", "''") + "'" for value in args)
            command = f". '{RENDERER}' {ps_args}; function Set-XbPrivateAcl {{ throw 'forced_acl_failure' }}; try {{ Invoke-XbBindingRenderer }} catch {{ }}; if(Test-Path -LiteralPath '{output_path}'){{exit 2}}; if(Get-ChildItem -LiteralPath '{output_path.parent}' -Filter '.binding-stage-*' -Force){{exit 3}}"
            failed_acl = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-Command", command], cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
            self.assertEqual(failed_acl.returncode, 0, failed_acl.stdout + failed_acl.stderr)
            for mode, expected in (("nonignored", "binding_output_not_ignored"), ("tracked", "binding_output_tracked")):
                mock_git = "function git { if($args -contains 'check-ignore'){ $global:LASTEXITCODE=" + ("1" if mode == "nonignored" else "0") + "; return }; if($args -contains 'ls-files'){ $global:LASTEXITCODE=0; " + ("return" if mode == "nonignored" else "Write-Output '.tmp/member-gateway-g3/member_forms_gateway_ingest.binding.json'; return") + " }; & git.exe @args; $global:LASTEXITCODE=$LASTEXITCODE }"
                safety_command = f". '{RENDERER}' {ps_args}; {mock_git}; try {{ Invoke-XbBindingRenderer; exit 4 }} catch {{ if($_.Exception.Message -cne '{expected}'){{Write-Error $_; exit 5}} }}; if(Test-Path -LiteralPath '{output_path}'){{exit 6}}"
                safety = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-Command", safety_command], cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
                self.assertEqual(safety.returncode, 0, f"{mode}: {safety.stdout}{safety.stderr}")


if __name__ == "__main__":
    unittest.main()
