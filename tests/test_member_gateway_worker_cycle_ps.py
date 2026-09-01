import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts/ac2_member_gateway_worker_lib.ps1"

PROBE = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Lib,
    [Parameter(Mandatory)][ValidateSet("exact", "mismatch", "absent")][string]$Outcome
)
$ErrorActionPreference = "Stop"
. $Lib

$state = @{ result = $null; paths = @(); sessions = @(); probe_refs = @() }
$gateway = {
    param($Base, $Path, $Method, $Body, $WorkerSession)
    $thisState = $state
    $thisState.paths += $Path
    $thisState.sessions += $WorkerSession
    if ($Path -like "*/allocation/probe" -or $Path -like "*/allocation/recheck") {
        $thisState.probe_refs += [string]$Body.probe_reference
    }
    if ($Path -eq "/readyz") { return [pscustomobject]@{ ready = $true } }
    if ($Path -eq "/v1/worker/claim") {
        return [pscustomobject]@{
            claimed = $true
            job = [pscustomobject]@{ job_id = "job-1"; payload_hash = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
        }
    }
    if ($Path -like "*/allocation/candidate") {
        return [pscustomobject]@{ bound = $false; candidate = "6590000001" }
    }
    if ($Path -like "*/allocation/probe") {
        return [pscustomobject]@{ bound = $true; member_no = "6590000001" }
    }
    if ($Path -like "*/allocation/recheck") { return [pscustomobject]@{ state = "ALLOCATION_BOUND" } }
    if ($Path -like "*/dispatch-fence") {
        return [pscustomobject]@{ state = "WRITING"; dispatch_fence_id = "fence-1234567890abcdef" }
    }
    if ($Path -like "*/result") {
        $thisState.result = $Body
        return [pscustomobject]@{ accepted = $true }
    }
    return [pscustomobject]@{}
}.GetNewClosure()

$probe = {
    param([string]$Candidate)
    [pscustomobject]@{ status = "FREE" }
}
$create = {
    param($Job, $Allocation)
    if ($Outcome -eq "exact") {
        [pscustomobject]@{ save_invocation_count = 1; readback_found = $true; readback_match = $true }
    } elseif ($Outcome -eq "mismatch") {
        [pscustomobject]@{ save_invocation_count = 1; readback_found = $true; readback_match = $false }
    } else {
        [pscustomobject]@{ save_invocation_count = 1; readback_found = $false; readback_match = $false }
    }
}.GetNewClosure()

$result = Invoke-XbMemberGatewayWorkerCycle -GatewayBaseUrl "https://gateway.example.test" -WorkerId "synthetic-worker" -EnableProductionWorker -EnableProductionAdapter -ProbeMember $probe -CreateMember $create -GatewayRequest $gateway
[pscustomobject]@{
    cycle_status = $result.status
    writes = $result.writes
    result_status = $state.result.status
    readback_found = $state.result.readback_found
    readback_match = $state.result.readback_match
    error_code = $state.result.error_code
    session_valid = (@($state.sessions | Where-Object { $_ -notmatch '^ws-[0-9a-f]{32}$' }).Count -eq 0)
    session_count = @($state.sessions | Select-Object -Unique).Count
    probe_refs_distinct = (@($state.probe_refs | Select-Object -Unique).Count -eq 2)
} | ConvertTo-Json -Compress
"""

@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "no PowerShell executable available")
class MemberGatewayWorkerCyclePowerShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = Path(tempfile.mkdtemp(prefix="xb-member-worker-ps-"))
        cls.probe = cls.temp_dir / "probe.ps1"
        cls.probe.write_text(PROBE, encoding="utf-8")
        cls.pwsh = shutil.which("pwsh") or shutil.which("powershell")

    def run_probe(self, outcome):
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
                "-Lib",
                str(LIB),
                "-Outcome",
                outcome,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_worker_propagates_exact_mismatch_and_absent_readback(self):
        exact = self.run_probe("exact")
        self.assertEqual(exact["cycle_status"], "CREATED_VERIFIED")
        self.assertEqual(exact["result_status"], "CREATED_VERIFIED")
        self.assertTrue(exact["readback_found"])
        self.assertTrue(exact["readback_match"])
        self.assertIsNone(exact["error_code"])

        mismatch = self.run_probe("mismatch")
        self.assertEqual(mismatch["cycle_status"], "CREATED_READBACK_MISMATCH")
        self.assertEqual(mismatch["result_status"], "CREATED_READBACK_MISMATCH")
        self.assertTrue(mismatch["readback_found"])
        self.assertFalse(mismatch["readback_match"])
        self.assertEqual(mismatch["error_code"], "readback_mismatch_manual_review")

        absent = self.run_probe("absent")
        self.assertEqual(absent["cycle_status"], "WRITE_OUTCOME_UNCERTAIN")
        self.assertEqual(absent["result_status"], "WRITE_OUTCOME_UNCERTAIN")
        self.assertFalse(absent["readback_found"])
        self.assertFalse(absent["readback_match"])
        self.assertEqual(absent["error_code"], "readback_absent")
        self.assertEqual(absent["writes"], 1)

        self.assertTrue(exact["session_valid"])
        self.assertEqual(exact["session_count"], 1)
        self.assertTrue(exact["probe_refs_distinct"])


if __name__ == "__main__":
    unittest.main()
