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
    [Parameter(Mandatory)][ValidateSet("boundary", "decimal")][string]$Op
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

if ($Op -eq "decimal") {
    # SQL-like store: typed columns, and GetMember returns a freshly reloaded
    # row whose OpeningPoints passes through SQL decimal(19,2), as AC2 does.
    $fields = @("MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress", "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual")
    function New-SqlLikeEntity {
        $table = New-Object System.Data.DataTable
        foreach ($field in $fields) {
            $type = if ($field -eq "OpeningPoints") { [decimal] } elseif ($field -in @("DOB", "RegisterDate", "ExpiryDate")) { [datetime] } else { [string] }
            [void]$table.Columns.Add($field, $type)
        }
        $row = $table.NewRow()
        foreach ($field in $fields) { $row[$field] = [System.DBNull]::Value }
        [void]$table.Rows.Add($row)
        return [pscustomobject]@{ Row = $row }
    }
    function ConvertTo-FixtureDecimal([string]$Text) { return [decimal]::Parse($Text, [System.Globalization.CultureInfo]::InvariantCulture) }
    function Get-SqlDecimal19x2([decimal]$Value) {
        return [System.Data.SqlTypes.SqlDecimal]::ConvertToPrecScale([System.Data.SqlTypes.SqlDecimal]::new($Value), 19, 2).Value
    }
    $state = @{ saved = $null }
    $command = [pscustomobject]@{ State = $state }
    [void]($command | Add-Member -MemberType ScriptMethod -Name NewMember -Value { param([bool]$Unused) return (New-SqlLikeEntity) })
    [void]($command | Add-Member -MemberType ScriptMethod -Name SaveMember -Value { param($Entity) $this.State.saved = $Entity })
    [void]($command | Add-Member -MemberType ScriptMethod -Name GetMember -Value {
        param([string]$MemberNo)
        $fresh = New-SqlLikeEntity
        foreach ($field in $fields) {
            $value = $this.State.saved.Row[$field]
            if ($field -eq "OpeningPoints" -and $value -isnot [System.DBNull]) { $value = Get-SqlDecimal19x2 $value }
            $fresh.Row[$field] = $value
        }
        return $fresh
    })
    $memberFactory = { param($Session) return $command }.GetNewClosure()
    $session = [pscustomobject]@{ UserSession = "fake"; DBSetting = "fake"; MemberCommandFactory = $memberFactory }
    function Get-Check($Expected, $FieldProfile, $Opening, [switch]$DropExpected) {
        $entity = New-SqlLikeEntity
        foreach ($field in $fields) { $entity.Row[$field] = $state.reloaded.Row[$field] }
        $entity.Row["OpeningPoints"] = $Opening
        $copy = [ordered]@{}
        foreach ($key in $Expected.Keys) { if (-not ($DropExpected -and $key -eq "OpeningPoints")) { $copy[$key] = $Expected[$key] } }
        $check = Compare-XbAutoCountMemberReadBack -Expected $copy -Actual $entity -FieldProfile $FieldProfile
        return "{0}:{1}" -f $check.Match, (@($check.Mismatches) -join ",")
    }
    $result = [ordered]@{}
    $cases = [ordered]@{
        sql_null = [System.DBNull]::Value
        non_zero_0_01 = (Get-SqlDecimal19x2 (ConvertTo-FixtureDecimal "0.01"))
        non_zero_50_00 = (Get-SqlDecimal19x2 (ConvertTo-FixtureDecimal "50"))
        zero_scale_0 = [decimal]0
        zero_scale_4 = (ConvertTo-FixtureDecimal "0.0000")
    }
    $shopifyMember = @{ MemberNo = "M000901"; Name = "Synthetic Decimal Alpha"; RegisterDate = "2026-10-15"; ExpiryDate = "2028-10-14" }
    $formsMember = @{ MemberNo = "6590000901"; MemberType = "Default"; Name = "Synthetic Decimal Beta"; MobilePhone = ""; EmailAddress = "beta@example.invalid"; DOB = "2000-03-01"; RegisterDate = "2026-10-15"; ExpiryDate = "2028-10-14"; OpeningPoints = 0 }
    foreach ($fieldProfile in @("shopify_m1", "forms_v1")) {
        $member = if ($fieldProfile -eq "shopify_m1") { $shopifyMember } else { $formsMember }
        $created = New-XbAutoCountMember -Session $session -Member $member -EnableProductionAdapter -MemberCommandFactory $memberFactory -FieldProfile $fieldProfile
        $state.reloaded = $command.GetMember([string]$member.MemberNo)
        $raw = $state.reloaded.Row["OpeningPoints"]
        $check = Compare-XbAutoCountMemberReadBack -Expected $created.Expected -Actual $created.ReadBack -FieldProfile $fieldProfile
        $profileResult = [ordered]@{
            fresh_object = (-not [object]::ReferenceEquals($state.reloaded.Row, $state.saved.Row))
            reloaded_text = [System.Convert]::ToString($raw, [System.Globalization.CultureInfo]::InvariantCulture)
            expected_opening = $created.Expected.OpeningPoints
            readback_opening = $created.ReadBack.OpeningPoints
            create = "{0}:{1}" -f $check.Match, (@($check.Mismatches) -join ",")
            missing_expected = Get-Check $created.Expected $fieldProfile $raw -DropExpected
        }
        foreach ($name in $cases.Keys) { $profileResult[$name] = Get-Check $created.Expected $fieldProfile $cases[$name] }
        $result[$fieldProfile] = $profileResult
    }
    # The reconcile path builds its expectation separately from the create.
    $reconcileExpected = New-XbAutoCountShopifyExpectedRecord -MemberNo "M000901" -CreatePayload ([pscustomobject]@{ name = "Synthetic Decimal Alpha"; mobile_phone = $null; email_address = $null; register_date = "2026-10-15"; expiry_date = "2028-10-14" })
    $state.saved = $null
    [void](New-XbAutoCountMember -Session $session -Member $shopifyMember -EnableProductionAdapter -MemberCommandFactory $memberFactory -FieldProfile "shopify_m1")
    $reconcile = Compare-XbAutoCountMemberReadBack -Expected $reconcileExpected -Actual $command.GetMember("M000901") -FieldProfile "shopify_m1"
    $result.reconcile = "{0}:{1}" -f $reconcile.Match, (@($reconcile.Mismatches) -join ",")
    # Canonical scale applies to OpeningPoints only.
    $result.other_field_decimal = Convert-XbAutoCountNormalizedValue -Value (ConvertTo-FixtureDecimal "0.00") -Field "Name"
    $result.opening_fraction = Convert-XbAutoCountNormalizedValue -Value (ConvertTo-FixtureDecimal "12.50") -Field "OpeningPoints"
    $result | ConvertTo-Json -Compress -Depth 4
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

    def run_probe(self, op="boundary"):
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
                op,
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

    def test_opening_points_sql_decimal_scale_is_not_a_readback_mismatch(self):
        # AC2 stores OpeningPoints as decimal(19,2), so the assigned [decimal]0
        # reloads as 0.00. Only the scale is canonical; values stay strict.
        result = self.run_probe("decimal")
        for profile in ("shopify_m1", "forms_v1"):
            with self.subTest(profile=profile):
                checks = result[profile]
                self.assertTrue(checks["fresh_object"])
                self.assertEqual(checks["reloaded_text"], "0.00")
                self.assertEqual((checks["expected_opening"], checks["readback_opening"]), ("0", "0"))
                self.assertEqual(checks["create"], "True:")
                self.assertEqual(checks["zero_scale_0"], "True:")
                self.assertEqual(checks["zero_scale_4"], "True:")
                for case in ("sql_null", "non_zero_0_01", "non_zero_50_00", "missing_expected"):
                    self.assertEqual(checks[case], "False:OpeningPoints", case)
        self.assertEqual(result["reconcile"], "True:")
        self.assertEqual(result["other_field_decimal"], "0.00")
        self.assertEqual(result["opening_fraction"], "12.5")

    def test_adapter_managed_values_are_not_caller_inputs(self):
        source = ADAPTER.read_text(encoding="utf-8")
        self.assertIn('throw "adapter_managed_defaults_are_not_caller_inputs"', source)
        self.assertEqual(source.count("$command.SaveMember($entity)"), 1)
