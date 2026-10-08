"""Shopify M1 worker/adapter behaviour under real PowerShell (synthetic fakes only).

No AutoCount assembly, gateway, Shopify or network is touched: MemberCommand,
the gateway and the child writer are in-process fakes.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "scripts/ac2_member_gateway_autocount_adapter.ps1"
LIB = ROOT / "scripts/ac2_member_gateway_worker_lib.ps1"
WORKER = ROOT / "scripts/ac2_member_gateway_worker.ps1"
PII = ("Synthetic Shopify Alpha", "15550100101", "synthetic.alpha@example.test")

ADAPTER_PROBE = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Adapter,
    [Parameter(Mandatory)][string]$Worker,
    [Parameter(Mandatory)][ValidateSet("create", "rejects", "next", "legacy")][string]$Op
)
$ErrorActionPreference = "Stop"
. $Adapter
# Load only the real create function from the worker script (its top level
# would otherwise run the production entry point).
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Worker, [ref]$tokens, [ref]$errors)
$fn = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq "Invoke-XbMemberGatewayCreateMember" }, $true)
. ([scriptblock]::Create($fn.Extent.Text))

function New-FakeTable {
    $table = New-Object System.Data.DataTable
    foreach ($field in @("MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress", "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual")) {
        [void]$table.Columns.Add($field, [object])
    }
    return ,$table
}
function New-FakeCommand {
    param($State)
    $command = [pscustomobject]@{ State = $State }
    $command | Add-Member -MemberType ScriptMethod -Name NewMember -Value {
        param([bool]$Unused)
        $table = New-FakeTable
        $row = $table.NewRow()
        foreach ($c in $table.Columns) { $row[$c.ColumnName] = [System.DBNull]::Value }
        [void]$table.Rows.Add($row)
        $this.State.entity = [pscustomobject]@{ Row = $row }
        return $this.State.entity
    }
    $command | Add-Member -MemberType ScriptMethod -Name SaveMember -Value { param($Entity) $this.State.saves++; $this.State.saved = $Entity }
    $command | Add-Member -MemberType ScriptMethod -Name GetMember -Value { param([string]$MemberNo) if ($this.State.members.ContainsKey($MemberNo)) { return $this.State.members[$MemberNo] } return $this.State.saved }
    $command | Add-Member -MemberType ScriptMethod -Name GetNextMemberNo -Value { if ($this.State.next_error) { throw "vendor detail Synthetic Shopify Alpha" } return $this.State.next }
    $command | Add-Member -MemberType ScriptMethod -Name LoadBrowseTable -Value { if ($this.State.browse_error) { throw "browse failed" } return ,$this.State.browse }
    return $command
}
$state = @{ saves = 0; saved = $null; entity = $null; members = @{}; next = "M000123"; next_error = $false; browse = (New-FakeTable); browse_error = $false }
$command = New-FakeCommand -State $state
$factory = { param($Session) return $command }.GetNewClosure()
$session = [pscustomobject]@{ UserSession = "fake"; DBSetting = "fake"; MemberCommandFactory = $factory }

if ($Op -eq "create") {
    $job = [pscustomobject]@{ source_system = "shopify"; create_payload = [pscustomobject]@{ name = "Synthetic Shopify Alpha"; mobile_phone = $null; email_address = $null; register_date = "2026-10-06"; expiry_date = "2028-10-05" } }
    $outcome = Invoke-XbMemberGatewayCreateMember -Session $session -Job $job -Allocation ([pscustomobject]@{ member_no = "M000123" }) -EnableProductionAdapter -MemberCommandFactory $factory
    $row = $state.entity.Row
    $nullFields = @(foreach ($f in @("DOB", "MobilePhone", "EmailAddress")) { if ($row[$f] -is [System.DBNull]) { $f } })
    $row["MobilePhone"] = ""
    $empty = Compare-XbAutoCountMemberReadBack -Expected (New-XbAutoCountShopifyExpectedRecord -MemberNo "M000123" -CreatePayload $job.create_payload) -Actual $state.entity -FieldProfile "shopify_m1"
    $row["MobilePhone"] = [System.DBNull]::Value
    $row["DOB"] = [datetime]"2000-03-01"
    $dob = Compare-XbAutoCountMemberReadBack -Expected (New-XbAutoCountShopifyExpectedRecord -MemberNo "M000123" -CreatePayload $job.create_payload) -Actual $state.entity -FieldProfile "shopify_m1"
    $with = [pscustomobject]@{ name = "Synthetic Shopify Alpha"; mobile_phone = "15550100101"; email_address = "synthetic.alpha@example.test"; register_date = "2026-10-06"; expiry_date = "2028-10-05" }
    $state.saves = 0
    $second = Invoke-XbMemberGatewayCreateMember -Session $session -Job ([pscustomobject]@{ source_system = "shopify"; create_payload = $with }) -Allocation ([pscustomobject]@{ member_no = "M000124" }) -EnableProductionAdapter -MemberCommandFactory $factory
    [pscustomobject]@{
        status = $outcome.status; save_count_first = 1; readback_match = $outcome.readback_match
        null_fields = $nullFields
        member_type = [string]$row["MemberType"]; opening = [string]$row["OpeningPoints"]; active = [string]$row["IsActive"]; individual = [string]$row["Individual"]
        register = ([datetime]$row["RegisterDate"]).ToString("yyyy-MM-dd"); expiry = ([datetime]$row["ExpiryDate"]).ToString("yyyy-MM-dd")
        empty_match = $empty.Match; empty_mismatches = @($empty.Mismatches)
        dob_match = $dob.Match; dob_mismatches = @($dob.Mismatches)
        second_status = $second.status; second_saves = $state.saves
        second_phone = [string]$state.entity.Row["MobilePhone"]
    } | ConvertTo-Json -Compress
}
if ($Op -eq "rejects") {
    $base = @{ MemberNo = "M1"; Name = "Synthetic Shopify Alpha"; RegisterDate = "2026-10-06"; ExpiryDate = "2028-10-05" }
    $cases = [ordered]@{
        member_type = @{ MemberType = "Gold" }; opening = @{ OpeningPoints = 50 }; active = @{ IsActive = "F" }; individual = @{ Individual = "F" }
        dob = @{ DOB = "2000-01-01" }; empty_phone = @{ MobilePhone = "" }; blank_email = @{ EmailAddress = "  " }
        no_name = @{ Name = "" }; bad_date = @{ RegisterDate = "06/10/2026" }; expiry_first = @{ ExpiryDate = "2026-10-05" }
    }
    $results = [ordered]@{}
    foreach ($key in $cases.Keys) {
        $member = $base.Clone()
        foreach ($field in $cases[$key].Keys) { $member[$field] = $cases[$key][$field] }
        try {
            [void](New-XbAutoCountMember -Session $session -Member $member -EnableProductionAdapter -MemberCommandFactory $factory -FieldProfile "shopify_m1")
            $results[$key] = "ACCEPTED"
        } catch { $results[$key] = $_.Exception.Message }
    }
    $results.saves = $state.saves
    [pscustomobject]$results | ConvertTo-Json -Compress
}
if ($Op -eq "next") {
    $first = Get-XbAutoCountNextMemberNo -Session $session -MemberCommandFactory $factory
    $state.next = ""
    try { [void](Get-XbAutoCountNextMemberNo -Session $session -MemberCommandFactory $factory); $empty = "ACCEPTED" } catch { $empty = $_.Exception.Message }
    $state.next_error = $true
    try { [void](Get-XbAutoCountNextMemberNo -Session $session -MemberCommandFactory $factory); $thrown = "ACCEPTED" } catch { $thrown = $_.Exception.Message }
    [pscustomobject]@{ first = $first; empty = $empty; thrown = $thrown } | ConvertTo-Json -Compress
}
if ($Op -eq "legacy") {
    function Add-Row($phone, $email, $name) {
        $row = $state.browse.NewRow()
        foreach ($c in $state.browse.Columns) { $row[$c.ColumnName] = [System.DBNull]::Value }
        if ($null -ne $phone) { $row["MobilePhone"] = $phone }
        if ($null -ne $email) { $row["EmailAddress"] = $email }
        $row["Name"] = $name
        [void]$state.browse.Rows.Add($row)
    }
    $phone = "15550100101"; $email = "synthetic.alpha@example.test"
    $out = [ordered]@{}
    Add-Row "+1 (999) 000-0000" "other@example.test" "Synthetic Shopify Alpha"
    $out.name_only = Test-XbAutoCountLegacyMemberCandidate -Session $session -MobilePhone $phone -EmailAddress $email -MemberCommandFactory $factory
    $out.no_contacts = Test-XbAutoCountLegacyMemberCandidate -Session $session -MobilePhone $null -EmailAddress $null -MemberCommandFactory $factory
    Add-Row "+1 (555) 010-0101" $null "Legacy"
    $out.mobile = Test-XbAutoCountLegacyMemberCandidate -Session $session -MobilePhone $phone -EmailAddress $null -MemberCommandFactory $factory
    $state.browse = New-FakeTable
    Add-Row $null " Synthetic.Alpha@Example.TEST " "Legacy"
    $out.email = Test-XbAutoCountLegacyMemberCandidate -Session $session -MobilePhone $null -EmailAddress $email -MemberCommandFactory $factory
    $state.browse = New-FakeTable
    $state.members[$phone] = [pscustomobject]@{ Row = $null }
    $out.member_no = Test-XbAutoCountLegacyMemberCandidate -Session $session -MobilePhone $phone -EmailAddress $null -MemberCommandFactory $factory
    $state.members.Clear()
    $state.browse_error = $true
    $out.failure = Test-XbAutoCountLegacyMemberCandidate -Session $session -MobilePhone $phone -EmailAddress $email -MemberCommandFactory $factory
    [pscustomobject]$out | ConvertTo-Json -Compress -Depth 4
}
"""

CYCLE_PROBE = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Lib,
    [Parameter(Mandatory)][ValidateSet("create", "review", "reconcile", "collision", "unmarked")][string]$Op,
    [string]$Lookup = "exact",
    [switch]$ChildWriter
)
$ErrorActionPreference = "Stop"
. $Lib
if ($ChildWriter) {
    $line = [Console]::In.ReadLine()
    $payload = $line | ConvertFrom-Json
    $ok = ($payload.job.create_payload.name -eq "Synthetic Shopify Alpha" -and $payload.allocation.member_no -eq "M000202")
    [pscustomobject]@{ save_invocation_count = 1; readback_found = $ok; readback_match = $ok } | ConvertTo-Json -Compress
    exit 0
}
$state = @{ paths = @(); bodies = @(); args = @(); next = @("M000201", "M000202"); next_index = 0; result = $null }
$createPayload = [pscustomobject]@{ name = "Synthetic Shopify Alpha"; mobile_phone = "15550100101"; email_address = "synthetic.alpha@example.test"; register_date = "2026-10-06"; expiry_date = "2028-10-05" }
$started = [DateTimeOffset]::UtcNow.ToString("o")
$gateway = {
    param($Base, $Path, $Method, $Body, $WorkerSession, $Timeout)
    $s = $state
    $s.paths += $Path
    if ($null -ne $Body) { $s.bodies += ($Body | ConvertTo-Json -Compress -Depth 6) }
    if ($Path -eq "/readyz") { return [pscustomobject]@{ ready = $true; lease_seconds = 20; heartbeat_seconds = 1; execution_deadline_seconds = 10 } }
    if ($Path -eq "/v1/worker/claim") { return [pscustomobject]@{ claimed = $true; job = [pscustomobject]@{ job_id = "job-1"; attempt = 1; source_system = "shopify"; payload_hash = "sha256:" + ("a" * 64); create_payload = $createPayload } } }
    if ($Path -like "*/shopify/legacy-precheck") { return [pscustomobject]@{ state = $(if ($Body.outcome -eq "NO_CANDIDATE") { "PRECHECKING" } else { "MANUAL_REVIEW" }) } }
    if ($Path -like "*/allocation/candidate") { return [pscustomobject]@{ bound = $false; generator = "member_command_get_next_member_no" } }
    if ($Path -like "*/allocation/probe") {
        if ($Body.status -eq "FREE" -and $Body.candidate -eq "M000201" -and $Op -eq "collision") { return [pscustomobject]@{ bound = $false; generator = "member_command_get_next_member_no"; candidates_remaining = 2; gateway_bound_collision = $true } }
        if ($Body.status -eq "FREE" -and $Body.candidate -eq "M000201" -and $Op -eq "unmarked") { return [pscustomobject]@{ bound = $false; generator = "member_command_get_next_member_no"; candidates_remaining = 2 } }
        if ($Body.status -eq "FREE") { return [pscustomobject]@{ bound = $true; member_no = $Body.candidate } }
        return [pscustomobject]@{ bound = $false; generator = "member_command_get_next_member_no" }
    }
    if ($Path -like "*/allocation/recheck") { return [pscustomobject]@{ state = "ALLOCATION_BOUND" } }
    if ($Path -like "*/dispatch-fence") { return [pscustomobject]@{ state = "WRITING"; dispatch_fence_id = "fence-1234567890abcdef"; execution_id = "exec-1234567890abcdef" } }
    if ($Path -like "*/status") { return [pscustomobject]@{ state = "WRITING"; state_version = 10; attempt = 1; attempt_started_at = $started } }
    if ($Path -like "*/writer/register") { return [pscustomobject]@{ registered = $true; lifecycle = "REGISTERED" } }
    if ($Path -like "*/writer/termination") { return [pscustomobject]@{ termination_confirmed = $true; lifecycle = "TERMINATION_CONFIRMED" } }
    if ($Path -like "*/lease") { return [pscustomobject]@{ state_version = 11; lease_expires_at = [DateTimeOffset]::UtcNow.AddSeconds(20).ToString("o") } }
    if ($Path -like "*/result") { $s.result = $Body; return [pscustomobject]@{ accepted = $true } }
    if ($Path -eq "/v1/worker/reconcile/claim") { return [pscustomobject]@{ claimed = $true; job = [pscustomobject]@{ job_id = "job-9"; member_no = "M000900"; create_payload = $createPayload } } }
    if ($Path -like "*/reconcile") { $s.result = $Body; return [pscustomobject]@{ state = "X" } }
    return [pscustomobject]@{}
}.GetNewClosure()
$probe = { param([string]$Candidate) if ($Candidate -eq "M000201" -and $Op -eq "create") { [pscustomobject]@{ status = "OCCUPIED" } } else { [pscustomobject]@{ status = "FREE" } } }.GetNewClosure()
$next = { $value = $state.next[$state.next_index]; $state.next_index++; return $value }.GetNewClosure()
$legacyOutcome = if ($Op -eq "review") { "CANDIDATE_FOUND" } else { "NO_CANDIDATE" }
$legacy = { param($Payload) [pscustomobject]@{ outcome = $legacyOutcome; phone_member_no_hit = ($legacyOutcome -eq "CANDIDATE_FOUND"); mobile_phone_hit = $false; email_hit = $false } }.GetNewClosure()
$factory = {
    param($Payload, $StopAt)
    $arguments = @("-Lib", $Lib, "-Op", "create", "-ChildWriter")
    $state.args += ($arguments -join " ")
    Start-XbMemberGatewayChildWriter -ScriptPath $PSCommandPath -Arguments $arguments
}.GetNewClosure()
if ($Op -eq "reconcile") {
    $reconcile = {
        param($MemberNo, $CreatePayload)
        if ($Lookup -eq "error") { throw "vendor detail Synthetic Shopify Alpha" }
        [pscustomobject]@{ found = ($Lookup -ne "absent"); match = ($Lookup -eq "exact") }
    }.GetNewClosure()
    $cycle = Invoke-XbMemberGatewayShopifyReconcileCycle -GatewayBaseUrl "https://gateway.example.test" -EnableProductionWorker -ReconcileMember $reconcile -GatewayRequest $gateway
} else {
    try {
        $cycle = Invoke-XbMemberGatewayWorkerCycle -GatewayBaseUrl "https://gateway.example.test" -WorkerId "synthetic" -WorkerHostBinding "host-test" -EnableProductionWorker -EnableProductionAdapter -ProbeMember $probe -CreateMember { throw "inline_writer_not_allowed" } -GatewayRequest $gateway -WriterProcessFactory $factory -NextMemberNo $next -LegacyPrecheck $legacy
    } catch {
        if ($Op -ne "unmarked") { throw }
        $cycle = [pscustomobject]@{ error = $_.Exception.Message }
    }
}
[pscustomobject]@{
    cycle = ($cycle | ConvertTo-Json -Compress)
    paths = [string]::Join(",", [string[]]$state.paths)
    bodies = [string]::Join("|", [string[]]$state.bodies)
    args = [string]::Join("|", [string[]]$state.args)
    result = if ($null -eq $state.result) { $null } else { $state.result | ConvertTo-Json -Compress }
    generator_calls = $state.next_index
} | ConvertTo-Json -Compress
"""


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "no PowerShell executable available")
class ShopifyPowerShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = Path(tempfile.mkdtemp(prefix="xb-shopify-ps-"))
        (cls.temp / "adapter_probe.ps1").write_text(ADAPTER_PROBE, encoding="utf-8")
        (cls.temp / "cycle_probe.ps1").write_text(CYCLE_PROBE, encoding="utf-8")
        cls.pwsh = shutil.which("pwsh") or shutil.which("powershell")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp, ignore_errors=True)

    def run_ps(self, script, *arguments):
        proc = subprocess.run(
            [self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.temp / script), *arguments],
            capture_output=True, text=True, check=False, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc, json.loads(proc.stdout.strip().splitlines()[-1])

    def adapter(self, op):
        return self.run_ps("adapter_probe.ps1", "-Adapter", str(ADAPTER), "-Worker", str(WORKER), "-Op", op)[1]

    def test_shopify_create_assigns_only_contract_fields_with_true_nulls(self):
        result = self.adapter("create")
        self.assertEqual((result["status"], result["readback_match"]), ("CREATED_VERIFIED", True))
        self.assertEqual(sorted(result["null_fields"]), ["DOB", "EmailAddress", "MobilePhone"])
        self.assertEqual((result["member_type"], result["opening"], result["active"], result["individual"]), ("Default", "0", "T", "T"))
        self.assertEqual((result["register"], result["expiry"]), ("2026-10-06", "2028-10-05"))
        self.assertFalse(result["empty_match"])
        self.assertEqual(result["empty_mismatches"], ["MobilePhone"])
        self.assertFalse(result["dob_match"])
        self.assertEqual(result["dob_mismatches"], ["DOB"])
        self.assertEqual((result["second_status"], result["second_saves"], result["second_phone"]), ("CREATED_VERIFIED", 1, "15550100101"))

    def test_caller_cannot_override_defaults_or_supply_dob_empty_or_bad_values(self):
        result = self.adapter("rejects")
        for key in ("member_type", "opening", "active", "individual"):
            self.assertEqual(result[key], "adapter_managed_defaults_are_not_caller_inputs", key)
        self.assertEqual(result["dob"], "member_field_not_allowed")
        self.assertEqual(result["empty_phone"], "empty_string_not_null_equivalent")
        self.assertEqual(result["blank_email"], "empty_string_not_null_equivalent")
        self.assertEqual(result["no_name"], "member_field_required")
        self.assertEqual(result["bad_date"], "member_date_invalid")
        self.assertEqual(result["expiry_first"], "member_expiry_before_register")
        self.assertEqual(result["saves"], 0)

    def test_get_next_member_no_is_official_and_fails_closed_without_detail(self):
        result = self.adapter("next")
        self.assertEqual(result, {"first": "M000123", "empty": "member_no_generator_failed", "thrown": "member_no_generator_failed"})

    def test_legacy_precheck_candidates_failures_and_name_only(self):
        result = self.adapter("legacy")
        self.assertEqual(result["name_only"]["outcome"], "NO_CANDIDATE")
        self.assertEqual(result["no_contacts"]["outcome"], "NO_CANDIDATE")
        self.assertEqual((result["mobile"]["outcome"], result["mobile"]["mobile_phone_hit"]), ("CANDIDATE_FOUND", True))
        self.assertEqual((result["email"]["outcome"], result["email"]["email_hit"]), ("CANDIDATE_FOUND", True))
        self.assertEqual((result["member_no"]["outcome"], result["member_no"]["phone_member_no_hit"]), ("CANDIDATE_FOUND", True))
        self.assertEqual(result["failure"], {"outcome": "LOOKUP_FAILED", "phone_member_no_hit": False, "mobile_phone_hit": False, "email_hit": False})

    def cycle(self, op, lookup="exact"):
        return self.run_ps("cycle_probe.ps1", "-Lib", str(LIB), "-Op", op, "-Lookup", lookup)

    def assert_private(self, text):
        for value in PII:
            self.assertNotIn(value, text)

    def test_shopify_cycle_prechecks_reallocates_and_releases_payload_on_stdin_only(self):
        proc, result = self.cycle("create")
        cycle = json.loads(result["cycle"])
        self.assertEqual((cycle["status"], cycle["writes"]), ("CREATED_VERIFIED", 1))
        paths = result["paths"].split(",")
        self.assertLess(paths.index("/v1/jobs/job-1/shopify/legacy-precheck"), paths.index("/v1/jobs/job-1/allocation/candidate"))
        self.assertEqual(result["generator_calls"], 2)
        self.assertEqual(json.loads(result["result"])["member_no"], "M000202")
        # Protected values never reach gateway request bodies, child CLI
        # arguments, the cycle output, stdout or stderr.
        self.assert_private(result["bodies"])
        self.assert_private(result["args"])
        self.assert_private(result["cycle"])
        self.assert_private(proc.stdout)
        self.assert_private(proc.stderr)

    def test_gateway_bound_collision_on_free_probe_requests_next_generated_candidate(self):
        proc, result = self.cycle("collision")
        cycle = json.loads(result["cycle"])
        self.assertEqual((cycle["status"], cycle["writes"]), ("CREATED_VERIFIED", 1))
        self.assertEqual(result["generator_calls"], 2)
        self.assertEqual(json.loads(result["result"])["member_no"], "M000202")
        self.assertEqual(result["paths"].split(",").count("/v1/jobs/job-1/allocation/probe"), 2)
        self.assert_private(result["bodies"] + result["cycle"] + proc.stdout + proc.stderr)

    def test_unbound_free_probe_without_gateway_collision_fails_closed(self):
        _, result = self.cycle("unmarked")
        self.assertEqual(json.loads(result["cycle"]), {"error": "allocation_probe_not_positive_free"})
        self.assertEqual(result["generator_calls"], 1)
        self.assertNotIn("dispatch-fence", result["paths"])
        self.assertIsNone(result["result"])

    def test_shopify_precheck_review_stops_before_allocation(self):
        _, result = self.cycle("review")
        self.assertEqual(json.loads(result["cycle"]), {"status": "MANUAL_REVIEW", "writes": 0, "dispatch_fence": False})
        self.assertNotIn("allocation", result["paths"])
        self.assertEqual(result["generator_calls"], 0)

    def test_reconcile_cycle_uses_bound_member_no_and_never_writes(self):
        for lookup, expected in (("exact", ("exact_match", True, True, None)), ("absent", ("absent", False, False, "confirmed_absent_manual_followup")), ("mismatch", ("mismatch", True, False, "readback_mismatch_manual_review")), ("error", ("ambiguous", False, False, "reconciliation_lookup_uncertain"))):
            proc, result = self.cycle("reconcile", lookup)
            body = json.loads(result["result"])
            self.assertEqual(body["member_no"], "M000900")
            self.assertEqual((body["lookup_status"], body["readback_found"], body["readback_match"], body["error_code"]), expected)
            self.assertEqual(json.loads(result["cycle"])["writes"], 0)
            self.assertNotIn("dispatch-fence", result["paths"])
            self.assert_private(result["bodies"] + result["cycle"] + proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
