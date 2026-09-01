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
    [Parameter(Mandatory)][ValidateSet("exact", "mismatch", "absent")][string]$Outcome,
    [int]$DelayMilliseconds = 0,
    [switch]$HeartbeatFailure,
    [switch]$ChildWriter
)
$ErrorActionPreference = "Stop"
. $Lib

if ($ChildWriter) {
    try {
        $null = [Console]::In.ReadLine()
        if ($DelayMilliseconds -gt 0) { Start-Sleep -Milliseconds $DelayMilliseconds }
        $childResult = $null
        if ($Outcome -eq "exact") {
            $childResult = [pscustomobject]@{ save_invocation_count = 1; readback_found = $true; readback_match = $true }
        } elseif ($Outcome -eq "mismatch") {
            $childResult = [pscustomobject]@{ save_invocation_count = 1; readback_found = $true; readback_match = $false }
        } else {
            $childResult = [pscustomobject]@{ save_invocation_count = 1; readback_found = $false; readback_match = $false }
        }
        $childResult | ConvertTo-Json -Compress
        exit 0
    } catch {
        exit 1
    }
}

$state = @{ result = $null; paths = @(); sessions = @(); probe_refs = @(); heartbeat_count = 0; state_version = 10; writer_pid = 0 }
$state.attempt_started_at = [DateTimeOffset]::UtcNow.ToString("o")
$gateway = {
    param($Base, $Path, $Method, $Body, $WorkerSession, $Timeout)
    $thisState = $state
    $thisState.paths += $Path
    $thisState.sessions += $WorkerSession
    if ($Path -like "*/allocation/probe" -or $Path -like "*/allocation/recheck") {
        $thisState.probe_refs += [string]$Body.probe_reference
    }
    if ($Path -eq "/readyz") {
        return [pscustomobject]@{ ready = $true; lease_seconds = 8; heartbeat_seconds = 1; execution_deadline_seconds = 5 }
    }
    if ($Path -eq "/v1/worker/claim") {
        return [pscustomobject]@{
            claimed = $true
            job = [pscustomobject]@{ job_id = "job-1"; payload_hash = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"; member_payload = [pscustomobject]@{ create_time = "2026-08-30T01:00:00Z" } }
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
    if ($Path -like "*/status") {
        return [pscustomobject]@{ state = "WRITING"; state_version = $thisState.state_version; attempt_started_at = $thisState.attempt_started_at; lease_expires_at = [DateTimeOffset]::UtcNow.AddSeconds(8).ToString("o") }
    }
    if ($Path -like "*/lease") {
        if ($HeartbeatFailure -and $thisState.heartbeat_count -gt 0) { throw "synthetic_heartbeat_failure" }
        $thisState.heartbeat_count++
        $thisState.state_version++
        return [pscustomobject]@{ state_version = $thisState.state_version; lease_expires_at = [DateTimeOffset]::UtcNow.AddSeconds(8).ToString("o") }
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
    throw "inline_writer_not_allowed"
}.GetNewClosure()

$writerProcessFactory = {
    param($Payload)
    $process = Start-XbMemberGatewayChildWriter -ScriptPath $PSCommandPath -Payload $Payload -Arguments @("-Lib", $Lib, "-Outcome", $Outcome, "-DelayMilliseconds", [string]$DelayMilliseconds, "-ChildWriter")
    $state.writer_pid = $process.Id
    return $process
}.GetNewClosure()

$result = Invoke-XbMemberGatewayWorkerCycle -GatewayBaseUrl "https://gateway.example.test" -WorkerId "synthetic-worker" -EnableProductionWorker -EnableProductionAdapter -ProbeMember $probe -CreateMember $create -GatewayRequest $gateway -WriterProcessFactory $writerProcessFactory
$writerExitConfirmed = $false
if ($state.writer_pid -gt 0) {
    try { Get-Process -Id $state.writer_pid -ErrorAction Stop | Out-Null } catch { $writerExitConfirmed = $true }
}
[pscustomobject]@{
    cycle_status = $result.status
    writes = $result.writes
    result_status = $state.result.status
    readback_found = $state.result.readback_found
    readback_match = $state.result.readback_match
    error_code = $state.result.error_code
    heartbeat_count = $state.heartbeat_count
    writer_exit_confirmed = $writerExitConfirmed
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

    def run_probe(self, outcome, delay_milliseconds=0, heartbeat_failure=False):
        command = [
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
            ]
        if delay_milliseconds:
            command.extend(["-DelayMilliseconds", str(delay_milliseconds)])
        if heartbeat_failure:
            command.append("-HeartbeatFailure")
        proc = subprocess.run(
            command,
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

    def test_supervisor_renews_before_deadline_and_terminates_hung_writer(self):
        slow = self.run_probe("exact", delay_milliseconds=1500)
        self.assertEqual(slow["cycle_status"], "CREATED_VERIFIED")
        self.assertGreaterEqual(slow["heartbeat_count"], 2)
        self.assertTrue(slow["writer_exit_confirmed"])

        hung = self.run_probe("exact", delay_milliseconds=10000)
        self.assertEqual(hung["cycle_status"], "WRITE_OUTCOME_UNCERTAIN")
        self.assertEqual(hung["result_status"], "WRITE_OUTCOME_UNCERTAIN")
        self.assertEqual(hung["error_code"], "save_outcome_uncertain")
        self.assertGreaterEqual(hung["heartbeat_count"], 2)
        self.assertTrue(hung["writer_exit_confirmed"])

    def test_heartbeat_failure_terminates_writer_and_stays_uncertain(self):
        failed = self.run_probe("exact", delay_milliseconds=3000, heartbeat_failure=True)
        self.assertEqual(failed["cycle_status"], "WRITE_OUTCOME_UNCERTAIN")
        self.assertEqual(failed["result_status"], "WRITE_OUTCOME_UNCERTAIN")
        self.assertEqual(failed["error_code"], "save_outcome_uncertain")
        self.assertEqual(failed["heartbeat_count"], 1)
        self.assertTrue(failed["writer_exit_confirmed"])


if __name__ == "__main__":
    unittest.main()
