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

function New-FakeMemberTable {
    param([string[]]$Extra = @())
    $table = New-Object System.Data.DataTable
    foreach ($field in @(
        "MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress",
        "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual"
    ) + $Extra) {
        [void]$table.Columns.Add($field, [object])
    }
    return ,$table
}

if ($Op -eq "boundary") {
    $entityTable = New-FakeMemberTable
    $entityRow = $entityTable.NewRow()
    [void]$entityTable.Rows.Add($entityRow)
    $browse = New-FakeMemberTable -Extra @("Guid", "CreatedUserID", "CreatedTime")
    $existing = $browse.NewRow()
    $existing["MemberNo"] = "81230000"; $existing["MobilePhone"] = "81230000"; $existing["Name"] = "Synthetic Beta"
    $existing["EmailAddress"] = "beta@example.test"; $existing["IsActive"] = "T"
    $existing["Guid"] = [Guid]"AABBCCDD-0000-4000-8000-000000000001"; $existing["CreatedUserID"] = " STAFF_PLACEHOLDER "
    $existing["CreatedTime"] = [datetime]::new(2026, 1, 2, 3, 4, 5)
    [void]$browse.Rows.Add($existing)
    $state = @{ entity = [pscustomobject]@{ Row = $entityRow }; readback = $null; saves = 0; browse = $browse }
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
    [void]($command | Add-Member -MemberType ScriptMethod -Name LoadBrowseTable -Value {
        return ,$this.State.browse
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
            DatabaseName = "BOOK_PLACEHOLDER"
            LoginUserId = "IU_PLACEHOLDER"
        }
    }.GetNewClosure()

    $session = New-XbAutoCountSession -EnableProductionAdapter -SessionFactory $sessionFactory
    $member = @{
        MemberNo = "6590000001"
        MemberType = "Default"
        Name = "Synthetic Alpha"
        MobilePhone = "6590000001"
        EmailAddress = "alpha@example.invalid"
        DOB = "2000-03-01"
        RegisterDate = "2026-07-01"
        ExpiryDate = "2028-06-30"
        OpeningPoints = 0
    }
    $prepared = New-XbAutoCountMemberEntity -Session $session -Member $member -EnableProductionAdapter -MemberCommandFactory $memberFactory
    $savesBeforeSave = $state.saves
    $saved = Invoke-XbAutoCountSaveMember -Prepared $prepared -EnableProductionAdapter
    $readback = Get-XbAutoCountMember -Session $session -MemberNo "6590000001" -MemberCommandFactory $memberFactory
    $exact = Compare-XbAutoCountMemberReadBack -Expected $prepared.Expected -Actual $readback
    # A SQL-backed readback may carry the column scale for the numeric field.
    $state.entity.Row["OpeningPoints"] = [decimal]"0.00"
    $scaled = Compare-XbAutoCountMemberReadBack -Expected $prepared.Expected -Actual $state.entity
    $state.entity.Row["OpeningPoints"] = [decimal]"1.00"
    $scaledDifferent = Compare-XbAutoCountMemberReadBack -Expected $prepared.Expected -Actual $state.entity
    $state.entity.Row["OpeningPoints"] = [decimal]"0"

    $state.entity.Row["Name"] = "Synthetic Mismatch"
    $mismatch = Compare-XbAutoCountMemberReadBack -Expected $prepared.Expected -Actual $state.entity
    $absent = Compare-XbAutoCountMemberReadBack -Expected $prepared.Expected -Actual $null
    $managed = "not_raised"
    try { [void](Get-XbAutoCountMemberAssignments -Member @{ MemberNo = "1"; IsActive = "F" }) } catch { $managed = [string]$_.Exception.Message }
    $probeRows = Get-XbAutoCountMemberProbeRows -Session $session -MemberCommandFactory $memberFactory
    $audit = Get-XbAutoCountMemberAudit -Entity $probeRows[0]
    $browse.Columns.Remove("CreatedTime")
    $missing = "not_raised"
    try { [void](Get-XbAutoCountMemberProbeRows -Session $session -MemberCommandFactory $memberFactory) } catch { $missing = [string]$_.Exception.Message }

    [pscustomobject]@{
        session_binding_valid = ($null -ne $session.UserSession -and $null -ne $session.DBSetting)
        session_database = $session.DatabaseName
        session_user = $session.LoginUserId
        saves_before_save = $savesBeforeSave
        save_count = $state.saves
        save_returned = $saved
        expected_is_active = $prepared.Expected.IsActive
        expected_individual = $prepared.Expected.Individual
        expected_mobile = $prepared.Expected.MobilePhone
        exact_found = $exact.Found
        exact_match = $exact.Match
        scaled_zero_match = $scaled.Match
        scaled_one_match = $scaledDifferent.Match
        scaled_one_fields = @($scaledDifferent.Mismatches)
        mismatch_found = $mismatch.Found
        mismatch_match = $mismatch.Match
        mismatch_fields = @($mismatch.Mismatches)
        absent_found = $absent.Found
        absent_match = $absent.Match
        absent_evidence = @($absent.Mismatches)
        managed_input = $managed
        probe_row_count = @($probeRows).Count
        probe_row_fields = @($probeRows[0].PSObject.Properties.Name)
        audit_guid = $audit.Guid
        audit_user = $audit.CreatedUserID
        audit_time = $audit.CreatedTime.ToString("yyyy-MM-ddTHH:mm:ss")
        probe_missing_column = $missing
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
        cls.pwsh = shutil.which("powershell") or shutil.which("pwsh")

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
        self.assertEqual(result["session_database"], "BOOK_PLACEHOLDER")
        self.assertEqual(result["session_user"], "IU_PLACEHOLDER")
        # NewMember + field fill performs no write; the single save call does.
        self.assertEqual(result["saves_before_save"], 0)
        self.assertEqual(result["save_count"], 1)
        self.assertTrue(result["save_returned"])
        self.assertEqual(result["expected_is_active"], "T")
        self.assertEqual(result["expected_individual"], "T")
        self.assertEqual(result["expected_mobile"], "6590000001")

        self.assertTrue(result["exact_found"])
        self.assertTrue(result["exact_match"])
        # Numbers compare by value: a readback of 0.00 equals the assigned 0,
        # while a different value (1.00) is still a mismatch.
        self.assertTrue(result["scaled_zero_match"])
        self.assertFalse(result["scaled_one_match"])
        self.assertIn("OpeningPoints", result["scaled_one_fields"])

        self.assertTrue(result["mismatch_found"])
        self.assertFalse(result["mismatch_match"])
        self.assertIn("Name", result["mismatch_fields"])

        self.assertFalse(result["absent_found"])
        self.assertFalse(result["absent_match"])
        self.assertEqual(result["absent_evidence"], ["record_absent"])
        self.assertEqual(result["managed_input"], "adapter_managed_defaults_are_not_caller_inputs")

    def test_probe_rows_and_readback_audit_columns(self):
        result = self.run_probe()
        self.assertEqual(result["probe_row_count"], 1)
        self.assertEqual(
            result["probe_row_fields"],
            ["MemberNo", "MobilePhone", "Name", "EmailAddress", "IsActive", "Guid", "CreatedUserID", "CreatedTime"],
        )
        self.assertEqual(result["audit_guid"], "aabbccdd-0000-4000-8000-000000000001")
        self.assertEqual(result["audit_user"], "STAFF_PLACEHOLDER")
        self.assertEqual(result["audit_time"], "2026-01-02T03:04:05")
        self.assertEqual(result["probe_missing_column"], "probe_columns_missing")

    def test_adapter_managed_values_are_not_caller_inputs(self):
        source = ADAPTER.read_text(encoding="utf-8")
        self.assertIn('throw "adapter_managed_defaults_are_not_caller_inputs"', source)
        self.assertEqual(source.count("$command.SaveMember($entity)"), 1)
        self.assertEqual(source.count("SaveMember("), 1)
        self.assertIn("$command.LoadBrowseTable()", source)
        self.assertNotIn("DeleteMember", source)


if __name__ == "__main__":
    unittest.main()
