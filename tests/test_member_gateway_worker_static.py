import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_FILES = (
    ROOT / "scripts/ac2_member_gateway_worker.ps1",
    ROOT / "scripts/ac2_member_gateway_worker_lib.ps1",
    ROOT / "scripts/ac2_member_gateway_autocount_adapter.ps1",
)


class MemberGatewayWorkerStaticTests(unittest.TestCase):
    def test_worker_is_outbound_only_and_has_no_forbidden_surfaces(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in WORKER_FILES)
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
        self.assertIn("Invoke-RestMethod", text)
        self.assertIn("GatewayBaseUrl -notmatch '^https://'", text)
        self.assertIn("if (-not $EnableProductionWorker)", text)

    def test_worker_session_and_probe_references_are_public_safe_and_run_scoped(self):
        worker = (ROOT / "scripts/ac2_member_gateway_worker.ps1").read_text(encoding="utf-8")
        library = (ROOT / "scripts/ac2_member_gateway_worker_lib.ps1").read_text(encoding="utf-8")
        text = worker + "\n" + library
        self.assertIn("X-XB-Worker-Session", library)
        self.assertIn("New-XbMemberGatewayWorkerSession", library)
        self.assertIn("New-XbMemberGatewayProbeReference", library)
        self.assertIn("'^ws-[0-9a-f]{32}$'", library)
        self.assertIn("'^[0-9a-f]{32}$'", library)
        self.assertNotIn("local-read-only", text)
        self.assertNotIn("-WorkerSession $WorkerId", text)
        for private_identity in (
            "MachineName",
            "ComputerName",
            "COMPUTERNAME",
            "UserName",
            "USERNAME",
            "USERDOMAIN",
            "WindowsIdentity",
            "SecurityIdentifier",
        ):
            self.assertNotIn(private_identity, text)

    def test_writer_registration_precedes_payload_release_and_unconfirmed_is_quarantined(self):
        library = (ROOT / "scripts/ac2_member_gateway_worker_lib.ps1").read_text(
            encoding="utf-8"
        )
        start = library.index("function Start-XbMemberGatewayChildWriter")
        release = library.index("function Release-XbMemberGatewayChildWriterPayload")
        protected = library.index("function Invoke-XbMemberGatewayProtectedWrite")
        start_block = library[start:release]
        protected_block = library[protected:]
        self.assertNotIn("StandardInput.WriteLine", start_block)
        self.assertNotIn("$Payload", start_block)
        self.assertLess(protected_block.index("writer/register"), protected_block.index("Release-XbMemberGatewayChildWriterPayload"))
        self.assertIn("writer_termination_unconfirmed", protected_block)
        self.assertIn("writer_termination_confirmed:", protected_block)
        self.assertIn("writer/quarantine", library)
        self.assertIn("writer_payload_missing", (ROOT / "scripts/ac2_member_gateway_worker.ps1").read_text(encoding="utf-8"))

    def test_adapter_uses_only_the_reviewed_member_create_surface(self):
        adapter = (ROOT / "scripts/ac2_member_gateway_autocount_adapter.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "[AutoCount.BonusPoint.Member.MemberCommand]::Create",
            adapter,
        )
        self.assertIn("$command.NewMember($false)", adapter)
        self.assertIn("$command.GetMember(", adapter)
        self.assertEqual(adapter.count("$command.SaveMember($entity)"), 1)
        self.assertIn("adapter_managed_defaults_are_not_caller_inputs", adapter)
        self.assertNotIn("UPDATE ", adapter.upper())
        self.assertNotIn("DELETE ", adapter.upper())

    def test_adapter_exposes_reviewed_session_and_exact_readback_seams(self):
        adapter = (ROOT / "scripts/ac2_member_gateway_autocount_adapter.ps1").read_text(
            encoding="utf-8"
        )
        for marker in (
            "CreateAutoCountDefaultDBSetting",
            "UserSession",
            "Authenticate",
            "-Name \"Login\"",
            "System.Data.DataRow",
            "SessionFactory",
            "ReadBackFound",
        ):
            self.assertIn(marker, adapter)
        self.assertNotIn("autocount_session_factory_binding_required", adapter)

        worker_lib = (ROOT / "scripts/ac2_member_gateway_worker_lib.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$readbackFound", worker_lib)
        self.assertNotIn("readback_found = $true", worker_lib)

    def test_powershell_parser_accepts_new_worker_files_when_available(self):
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if pwsh is None:
            self.skipTest("PowerShell parser unavailable on this runner")
        command = (
            "& { param([string]$path) "
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$path, [ref]$tokens, [ref]$errors) > $null; "
            "if ($errors.Count -gt 0) { exit 1 } }"
        )
        for path in WORKER_FILES:
            result = subprocess.run(
                [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command, str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, path.name)


if __name__ == "__main__":
    unittest.main()
