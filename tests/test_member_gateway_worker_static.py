import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
# The installable release package (installer $packageFiles / adapter release list).
PACKAGE_FILES = (
    "ac2_member_gateway_worker.ps1",
    "ac2_member_gateway_worker_lib.ps1",
    "ac2_member_gateway_autocount_adapter.ps1",
    "launch_ac2_member_gateway_worker.ps1",
    "test_ac2_member_gateway_autocount_dependencies.ps1",
    "ac2_member_create_primitive.ps1",
)
WORKER_FILES = tuple(SCRIPTS / name for name in PACKAGE_FILES)
CHANGED_SCRIPTS = WORKER_FILES + (SCRIPTS / "install_ac2_member_gateway_worker.ps1", SCRIPTS / "ac2_member_test_cleanup.ps1")

# v1 routes and removed mechanisms (W-G2-149 section 2.8 / 4.5 / 11).
REMOVED_ROUTE_STRINGS = (
    "/v1/worker/claim", "/precheck", "/lease", "/allocation/", "allocation/candidate", "allocation/probe",
    "allocation/recheck", "/write-intent", "/dispatch-fence", "/writer/register", "/writer/termination",
    "/writer/quarantine", "/writer/recover", "/reconcile", "xb.member.gateway.result.v1",
)
REMOVED_SYMBOLS = (
    "Invoke-XbMemberGatewayProtectedWrite", "Release-XbMemberGatewayChildWriterPayload",
    "Confirm-XbMemberGatewayWriterTermination", "Start-XbMemberGatewayChildWriter",
    "Stop-XbMemberGatewayWriterProcess", "Get-XbMemberGatewayWorkerHostBinding", "Get-XbMemberGatewayTiming",
    "New-XbMemberGatewayProbeReference", "Read-XbMemberGatewayWriterOutcome", "Get-XbMemberGatewayProcessIdentity",
    "Invoke-XbMemberGatewayCreateMember", "New-XbAutoCountMember ", "ChildExternalWrite", "WriteDeadlineUtc",
    "XB_MEMBER_GATEWAY_WORKER_HOST_BINDING\", \"Process", "worker_host_binding", "heartbeat_seconds",
    "member_no_allocation_exhausted", "dispatch_fence_id",
)


class MemberGatewayWorkerStaticTests(unittest.TestCase):
    def package_text(self):
        return {path.name: path.read_text(encoding="utf-8") for path in WORKER_FILES}

    def test_worker_is_outbound_only_and_has_no_forbidden_surfaces(self):
        text = "\n".join(self.package_text().values())
        for pattern in (
            r"\bHttpListener\b",
            r"\bSystem\.Net\.HttpListener\b",
            r"\bInvoke-Sqlcmd\b",
            r"\bNew-PSSession\b",
            r"\bEnter-PSSession\b",
            r"\bInvoke-Command\b",
            r"\bRegister-ScheduledTask\b",
            r"\bStart-ScheduledTask\b",
            r"\bschtasks(?:\.exe)?\b",
            r"\bStart-Job\b",
            r"-Parallel\b",
            r"\bInvoke-WmiMethod\b",
        ):
            self.assertIsNone(re.search(pattern, text, re.IGNORECASE), pattern)
        self.assertIn("Invoke-WebRequest", text)
        self.assertIn("GatewayBaseUrl -notmatch '^https://'", text)
        self.assertIn("if (-not $EnableProductionWorker)", text)

    def test_exactly_one_save_call_site_in_the_installable_package(self):
        texts = self.package_text()
        counts = {name: text.count("SaveMember(") for name, text in texts.items()}
        self.assertEqual(sum(counts.values()), 1, counts)
        self.assertEqual(counts["ac2_member_gateway_autocount_adapter.ps1"], 1)
        self.assertEqual(texts["ac2_member_create_primitive.ps1"].count("Invoke-XbAutoCountSaveMember -Prepared"), 1)
        for name, text in texts.items():
            self.assertNotIn("DeleteMember", text, name)

    def test_delete_member_appears_only_in_the_cleanup_script(self):
        hits = []
        for path in sorted(SCRIPTS.glob("*.ps1")):
            if "DeleteMember(" in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(path.name)
        # Pre-existing UAT/probe scripts are outside this lane and never installed;
        # among the member-write v2 scripts only the cleanup script deletes.
        v2_hits = [name for name in hits if name in {path.name for path in CHANGED_SCRIPTS}]
        self.assertEqual(v2_hits, ["ac2_member_test_cleanup.ps1"])
        installer = (SCRIPTS / "install_ac2_member_gateway_worker.ps1").read_text(encoding="utf-8")
        self.assertNotIn('"ac2_member_test_cleanup.ps1"', installer)

    def test_no_removed_route_or_symbol_remains(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in CHANGED_SCRIPTS)
        for fragment in REMOVED_ROUTE_STRINGS + REMOVED_SYMBOLS:
            self.assertNotIn(fragment, text, fragment)
        library = (SCRIPTS / "ac2_member_gateway_worker_lib.ps1").read_text(encoding="utf-8")
        self.assertIn('"/v2/worker/claim"', library)
        self.assertIn('"/v2/jobs/{0}/result"', library)
        self.assertIn('"/readyz"', library)

    def test_worker_session_is_public_safe_and_run_scoped(self):
        worker = (SCRIPTS / "ac2_member_gateway_worker.ps1").read_text(encoding="utf-8")
        library = (SCRIPTS / "ac2_member_gateway_worker_lib.ps1").read_text(encoding="utf-8")
        text = worker + "\n" + library
        self.assertIn("X-XB-Worker-Session", library)
        self.assertIn("New-XbMemberGatewayWorkerSession", library)
        self.assertIn("'^ws-[0-9a-f]{32}$'", library)
        self.assertNotIn("local-read-only", text)
        for private_identity in (
            "MachineName", "ComputerName", "COMPUTERNAME", "UserName", "USERNAME", "USERDOMAIN",
            "WindowsIdentity", "SecurityIdentifier",
        ):
            self.assertNotIn(private_identity, text)

    def test_worker_infers_nothing_about_the_phone(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in WORKER_FILES)
        for forbidden in ("'65'", '"65"', "65[89]", "canonical_phone", "MobilePhone -replace"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_adapter_uses_only_the_reviewed_member_surface(self):
        adapter = (SCRIPTS / "ac2_member_gateway_autocount_adapter.ps1").read_text(encoding="utf-8")
        self.assertIn("[AutoCount.BonusPoint.Member.MemberCommand]::Create", adapter)
        self.assertIn("$command.NewMember($false)", adapter)
        self.assertIn("$command.GetMember(", adapter)
        self.assertIn("$command.LoadBrowseTable()", adapter)
        self.assertEqual(adapter.count("$command.SaveMember($entity)"), 1)
        self.assertIn("adapter_managed_defaults_are_not_caller_inputs", adapter)
        self.assertNotIn("UPDATE ", adapter.upper())
        self.assertNotIn("DELETE ", adapter.upper())
        for marker in ("CreateAutoCountDefaultDBSetting", "UserSession", "Authenticate", "-Name \"Login\"", "System.Data.DataRow", "SessionFactory"):
            self.assertIn(marker, adapter)

    def test_secrets_never_on_a_command_line(self):
        library = (SCRIPTS / "ac2_member_gateway_worker_lib.ps1").read_text(encoding="utf-8")
        start = library.index("function Invoke-XbAc2PrimitiveChild")
        block = library[start:library.index("function New-XbMemberGatewayResultBody")]
        self.assertIn('$arguments = @("-NoLogo", "-NoProfile", "-NonInteractive", "-File", $PrimitiveScriptPath, "-Book", $Book)', block)
        self.assertIn("$startInfo.EnvironmentVariables[[string]$key]", block)
        worker = (SCRIPTS / "ac2_member_gateway_worker.ps1").read_text(encoding="utf-8")
        self.assertIn("[Environment]::SetEnvironmentVariable($passwordEnvironmentVariable, $null, \"Process\")", worker)

    def test_scripts_are_ascii(self):
        for path in CHANGED_SCRIPTS:
            self.assertTrue(path.read_bytes().isascii(), path.name)

    def test_powershell_parser_accepts_changed_scripts(self):
        pwsh = shutil.which("powershell") or shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell parser unavailable on this runner")
        command = (
            "& { param([string]$path) "
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$path, [ref]$tokens, [ref]$errors) > $null; "
            "if ($errors.Count -gt 0) { exit 1 } }"
        )
        for path in CHANGED_SCRIPTS:
            result = subprocess.run(
                [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command, str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, path.name)


class PrimitiveChildEnvironmentTests(unittest.TestCase):
    def test_child_never_inherits_the_gateway_bearer_or_worker_fault(self):
        library = (ROOT / "scripts/ac2_member_gateway_worker_lib.ps1").read_text(encoding="utf-8")
        self.assertIn('[void]$startInfo.EnvironmentVariables.Remove("XB_MEMBER_GATEWAY_WORKER_TOKEN")', library)
        self.assertIn('[void]$startInfo.EnvironmentVariables.Remove("XB_WORKER_FAULT")', library)

if __name__ == "__main__":
    unittest.main()
