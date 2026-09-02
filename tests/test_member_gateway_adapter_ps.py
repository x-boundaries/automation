import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "scripts/ac2_member_gateway_autocount_adapter.ps1"

PROBE = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Adapter,
    [Parameter(Mandatory)][ValidateSet("boundary")][string]$Op
)
$ErrorActionPreference = "Stop"
. $Adapter

function New-FakeMemberEntity {
    $table = New-Object System.Data.DataTable
    foreach ($field in @(
        "MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress",
        "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual"
    )) {
        [void]$table.Columns.Add($field, [object])
    }
    $row = $table.NewRow()
    [void]$table.Rows.Add($row)
    return [pscustomobject]@{ Row = $row }
}

if ($Op -eq "boundary") {
    $state = @{ entity = New-FakeMemberEntity; readback = $null; saves = 0 }
    $command = [pscustomobject]@{ State = $state }
    [void]($command | Add-Member -MemberType ScriptMethod -Name NewMember -Value {
        param([bool]$Unused)
        return $this.State.entity
    })
    [void]($command | Add-Member -MemberType ScriptMethod -Name SaveMember -Value {
        param($Entity)
        $this.State.saves = [int]$this.State.saves + 1
        $this.State.readback = $Entity
    })
    [void]($command | Add-Member -MemberType ScriptMethod -Name GetMember -Value {
        param([string]$MemberNo)
        return $this.State.readback
    })

    $memberFactory = {
        param($Session)
        return $command
    }.GetNewClosure()
    $sessionFactory = {
        [pscustomobject]@{
            UserSession = [pscustomobject]@{ Kind = "fake-user-session" }
            DBSetting = [pscustomobject]@{ Kind = "fake-db-setting" }
            MemberCommandFactory = $memberFactory
        }
    }.GetNewClosure()

    $session = New-XbAutoCountSession -EnableProductionAdapter -SessionFactory $sessionFactory
    $member = @{
        MemberNo = "6590000001"
        MemberType = "Default"
        Name = "Synthetic Alpha"
        MobilePhone = ""
        EmailAddress = "alpha@example.invalid"
        DOB = "2000-03-01"
        RegisterDate = "2026-07-01"
        ExpiryDate = "2028-06-30"
        OpeningPoints = 0
    }
    $outcome = New-XbAutoCountMember -Session $session -Member $member -EnableProductionAdapter -MemberCommandFactory $memberFactory
    $exact = Compare-XbAutoCountMemberReadBack -Expected $outcome.Expected -Actual $outcome.ReadBack

    $state.entity.Row["Name"] = "Synthetic Mismatch"
    $mismatch = Compare-XbAutoCountMemberReadBack -Expected $outcome.Expected -Actual $state.entity
    $absent = Compare-XbAutoCountMemberReadBack -Expected $outcome.Expected -Actual $null

    [pscustomobject]@{
        session_binding_valid = ($null -ne $session.UserSession -and $null -ne $session.DBSetting)
        save_count = $state.saves
        outcome_readback_found = $outcome.ReadBackFound
        expected_is_active = $outcome.Expected.IsActive
        expected_individual = $outcome.Expected.Individual
        exact_found = $exact.Found
        exact_match = $exact.Match
        mismatch_found = $mismatch.Found
        mismatch_match = $mismatch.Match
        mismatch_fields = @($mismatch.Mismatches)
        absent_found = $absent.Found
        absent_match = $absent.Match
        absent_evidence = @($absent.Mismatches)
    } | ConvertTo-Json -Compress
}
"""

@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "no PowerShell executable available")
class MemberGatewayAdapterPowerShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = Path(tempfile.mkdtemp(prefix="xb-member-gateway-ps-"))
        cls.probe = cls.temp_dir / "probe.ps1"
        cls.probe.write_text(PROBE, encoding="utf-8")
        cls.pwsh = shutil.which("pwsh") or shutil.which("powershell")

    def run_probe(self):
        proc = subprocess.run(
            [
                self.pwsh,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.probe),
                "-Adapter",
                str(ADAPTER),
                "-Op",
                "boundary",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_reviewed_fake_session_and_effective_readback_states(self):
        result = self.run_probe()
        self.assertTrue(result["session_binding_valid"])
        self.assertEqual(result["save_count"], 1)
        self.assertTrue(result["outcome_readback_found"])
        self.assertEqual(result["expected_is_active"], "T")
        self.assertEqual(result["expected_individual"], "T")

        self.assertTrue(result["exact_found"])
        self.assertTrue(result["exact_match"])

        self.assertTrue(result["mismatch_found"])
        self.assertFalse(result["mismatch_match"])
        self.assertIn("Name", result["mismatch_fields"])

        self.assertFalse(result["absent_found"])
        self.assertFalse(result["absent_match"])
        self.assertEqual(result["absent_evidence"], ["record_absent"])

    def test_adapter_managed_values_are_not_caller_inputs(self):
        source = ADAPTER.read_text(encoding="utf-8")
        self.assertIn('throw "adapter_managed_defaults_are_not_caller_inputs"', source)
        self.assertEqual(source.count("$command.SaveMember($entity)"), 1)
