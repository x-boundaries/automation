"""Worker v2 cycle (W-G2-149 section 4.5, row K-CY) and the X-CC golden
fixture (claim v2 consumed -> result v2 posted) built by running the real
primitive in the child. The gateway is a scriptblock double; no network.
Synthetic data only.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts/ac2_member_gateway_worker_lib.ps1"
ADAPTER = ROOT / "scripts/ac2_member_gateway_autocount_adapter.ps1"
FIXTURES = ROOT / "tests/fixtures/ac2_member_primitive"
FAKE_CHILD = FIXTURES / "fake_primitive_child.ps1"
WRAPPER_CHILD = FIXTURES / "primitive_child_wrapper.ps1"
GOLDEN = FIXTURES / "cross_contract_cases.v1.fixture"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
NORMALISED_RELEASE = "0" * 64

NOW = "2026-09-30T02:00:30Z"
LEASE_EXPIRES = "2026-09-30T02:10:00Z"
JOB_ID = "job-" + "B" * 24
LEASE_ID = "lease-" + "c" * 32
PROD_BOOK = "BOOK_PROD_PLACEHOLDER"
IU = "IU_PLACEHOLDER"
PROD_CONFIG = {"DatabaseName": PROD_BOOK, "LoginUserId": IU, "IntegrationUserId": IU, "ProductionBook": PROD_BOOK, "TestBookAllowlist": ["BOOK_TEST_PLACEHOLDER"]}

FORBIDDEN_PATH_FRAGMENTS = ("/lease", "heartbeat", "precheck", "allocation", "write-intent", "dispatch-fence", "/writer/", "/v1/", "reconcile", "/status")


def claim_body(**overrides):
    request = {
        "rule": "XB-MN-1",
        "base_member_no": "91234567",
        "name_component": "FIXTUREPERSO",
        "phone": "9123 4567",
        "name": "Fixture Person",
        "email": "fixture.person@example.test",
        "MemberType": "Default",
        "DOB": "2000-05-01",
        "RegisterDate": "2026-09-30",
        "ExpiryDate": "2028-09-29",
        "OpeningPoints": 0,
        "IsActive": True,
        "Individual": True,
    }
    request.update(overrides.pop("request", {}))
    body = {
        "schema_version": "xb.member.gateway.worker_claim.v2",
        "claimed": True,
        "job_id": JOB_ID,
        "attempt_no": 1,
        "lease_id": LEASE_ID,
        "state_version": 7,
        "lease_expires_at": LEASE_EXPIRES,
        "first_claimed_at": "2026-09-30T02:00:00Z",
        "server_time_utc": "2026-09-30T02:00:00Z",
        "request": request,
    }
    body.update(overrides)
    return body


def primitive_output(**overrides):
    value = {
        "outcome": "CREATED_VERIFIED", "rule": "R1", "branch": "BASE", "member_no": "91234567",
        "member_guid": "0f1e2d3c-4b5a-4968-8778-a1b2c3d4e5f6", "save_invoked": True, "save_invocation_count": 1,
        "readback": {"found": True, "match": True, "created_by_integration_user": True}, "reason_code": None,
        "dq_flags": [], "primitive": {"release_sha256": "__RELEASE__", "rule_version": "XB-MN-1"}, "error_code": None,
    }
    value.update(overrides)
    return value


READY = {"ready": True, "reasons": [], "dispatch_enabled": True, "server_time_utc": NOW}

CYCLE_CASES = [
    {"name": "kcy_created", "child_mode": "emit", "child_output": primitive_output(), "inherit_worker_fault": True,
     "claim": claim_body(request={"name": "Fixture Person \u9648"})},
    {"name": "kcy_disabled", "disabled": True},
    {"name": "kcy_idle", "claim": {"schema_version": "xb.member.gateway.worker_claim.v2", "claimed": False, "reason": "no_eligible_job", "server_time_utc": NOW}},
    {"name": "kcy_not_ready", "ready": {**READY, "ready": False}},
    {"name": "kcy_dispatch_disabled", "ready": {**READY, "dispatch_enabled": False}},
    {"name": "kcy_clock_skew", "ready": {**READY, "server_time_utc": "2026-09-30T01:57:00Z"}},
    {"name": "kcy_mutex_held", "mutex_held_by_other_process": True},
    {"name": "kcy_claim_invalid", "claim": claim_body(lease_id="lease-XYZ")},
    {"name": "kcy_deadline_kill", "child_mode": "sleep", "deadline_seconds": 2},
    {"name": "kcy_unconfirmed_exit", "child_mode": "sleep", "deadline_seconds": 1, "kill_noop": True, "kill_wait_ms": 300},
    {"name": "kcy_garbage_output", "child_mode": "garbage"},
    {"name": "kcy_silent_exit", "child_mode": "silent_exit"},
    {"name": "kcy_two_lines", "child_mode": "two_lines", "child_output": primitive_output()},
    {"name": "kcy_wrong_release", "child_mode": "emit", "child_output": primitive_output(primitive={"release_sha256": "f" * 64, "rule_version": "XB-MN-1"})},
    {"name": "kcy_extra_key", "child_mode": "emit", "child_output": {**primitive_output(), "extra": 1}},
    {"name": "kcy_incoherent_count", "child_mode": "emit", "child_output": primitive_output(save_invocation_count=0)},
    {"name": "kcy_launch_failed", "missing_primitive": True},
    {"name": "kcy_result_409", "child_mode": "emit", "child_output": primitive_output(), "result_responses": ["gateway_http_409"]},
    {"name": "kcy_result_5xx_retry", "child_mode": "emit", "child_output": primitive_output(), "result_responses": ["gateway_http_503", "gateway_transport_failed", "ok"]},
    {"name": "kcy_result_transport_exhausted", "child_mode": "emit", "child_output": primitive_output(), "result_responses": ["gateway_transport_failed"] * 5},
    {"name": "kcy_result_4xx_final", "child_mode": "emit", "child_output": primitive_output(), "result_responses": ["gateway_http_400", "ok"]},
    {"name": "kcy_result_after_lease", "child_mode": "emit", "child_output": primitive_output(), "claim": claim_body(lease_expires_at="2026-09-30T02:00:10Z")},
    {"name": "kcy_worker_fault_refused_in_production", "child_mode": "emit", "child_output": primitive_output(), "worker_fault": "skip_result_post"},
    {"name": "kcy_worker_fault_guarded_skip", "book": "test", "child_mode": "emit", "child_output": primitive_output(), "worker_fault": "skip_result_post",
     "claim": claim_body(request={"name": "ZZTEST Fixture", "email": "zztest.fixture@example.invalid", "base_member_no": "00091234567", "phone": "00091234567", "name_component": "ZZTESTFIX"}),
     "fault_guard": {"DatabaseName": "BOOK_TEST_PLACEHOLDER", "ProductionBook": PROD_BOOK, "TestBookAllowlist": ["BOOK_TEST_PLACEHOLDER"]}},
]

for entry in CYCLE_CASES:
    entry.setdefault("claim", claim_body())

OTHER_ROW = {"MemberNo": "91234567", "Name": "Lim Mei Ling", "EmailAddress": "other.person@example.test"}
CROSS_CASES = [
    {"name": "R1_create_base", "fake_case": {"config": PROD_CONFIG}},
    {"name": "R4_create_name_appended", "fake_case": {"config": PROD_CONFIG, "rows": [OTHER_ROW]}},
    {"name": "R2c_link_existing", "fake_case": {"config": PROD_CONFIG, "rows": [{"MemberNo": "91234567", "Name": "person fixture", "EmailAddress": "old.fixture@example.test", "Guid": "3a3a3a3a-4b4b-4c4c-8d8d-5e5e5e5e5e5e"}]}},
    {"name": "R0_prior_attempt", "claim": claim_body(attempt_no=2), "fake_case": {"config": PROD_CONFIG, "rows": [{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1, "Guid": "6b6b6b6b-7c7c-4d7d-8e8e-9f9f9f9f9f9f"}]}},
    {"name": "R0_final_proof_rejected", "claim": claim_body(attempt_no=2),
     "fake_case": {"config": PROD_CONFIG, "rows": [{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}], "final_readback_mode": "throw"},
     "result_responses": ["gateway_http_503", "ok"]},
    {"name": "R0_final_field_mismatch", "claim": claim_body(attempt_no=2),
     "fake_case": {"config": PROD_CONFIG, "rows": [{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}], "final_readback_mutations": {"Name": "Changed final name"}},
     "result_responses": ["gateway_http_503", "ok"]},
    {"name": "MUTEX_BUSY", "fake_case": {"config": PROD_CONFIG}, "hold_mutex_on_claim": True},
]
for entry in CROSS_CASES:
    entry.setdefault("claim", claim_body())
    entry["primitive"] = "wrapper"

HARNESS = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Lib,
    [Parameter(Mandatory)][string]$Adapter,
    [Parameter(Mandatory)][string]$FakeChild,
    [Parameter(Mandatory)][string]$WrapperChild,
    [Parameter(Mandatory)][string]$CasesPath,
    [Parameter(Mandatory)][string]$WorkRoot,
    [Parameter(Mandatory)][string]$OutPath
)
$ErrorActionPreference = "Stop"
. $Lib
. $Adapter
# Hostile transport condition: a console input encoding WITH a UTF-8 BOM
# preamble, as on the hosted runner. The worker must still hand the child a
# preamble-free stdin whose byte 0 is "{", and restore this encoding.
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($true)
if ([Console]::InputEncoding.GetPreamble().Length -ne 3) { throw "hostile_console_encoding_not_set" }
$release = Get-XbAc2ReleaseIdentity -PackageRoot (Split-Path -Parent $Lib)
$interpreter = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
$now = [DateTimeOffset]::Parse("2026-09-30T02:00:30Z", [Globalization.CultureInfo]::InvariantCulture)

function Start-XbMutexHolder {
    param([string]$Name)
    $script = "`$m = New-Object System.Threading.Mutex(`$false, '$Name'); [void]`$m.WaitOne(); [Console]::Out.WriteLine('held'); [Console]::Out.Flush(); Start-Sleep -Seconds 60"
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $interpreter
    $info.Arguments = '-NoLogo -NoProfile -NonInteractive -Command "' + $script.Replace('"', '\"') + '"'
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::Start($info)
    if ($process.StandardOutput.ReadLine() -ne "held") { throw "holder_failed" }
    return $process
}

function Get-XbCaseValue { param($Object, [string]$Name, $Default = $null) $p = $Object.PSObject.Properties[$Name]; if ($null -eq $p) { return $Default }; return $p.Value }

$cases = [IO.File]::ReadAllText($CasesPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
$results = [ordered]@{}
foreach ($case in $cases) {
    $name = [string]$case.name
    $mutexName = "Global\XB-AC2-MemberCreate-test-" + [Guid]::NewGuid().ToString("N")
    $tracePath = Join-Path $WorkRoot ($name + ".trace.json")
    $primitiveTracePath = Join-Path $WorkRoot ($name + ".primitive-invocations.txt")
    $calls = New-Object System.Collections.Generic.List[object]
    $ctx = @{
        ready = (Get-XbCaseValue $case "ready" ([pscustomobject]@{ ready = $true; reasons = @(); dispatch_enabled = $true; server_time_utc = "2026-09-30T02:00:30Z" }))
        claim = (Get-XbCaseValue $case "claim" $null)
        responses = [System.Collections.Generic.Queue[string]]::new([string[]]@(Get-XbCaseValue $case "result_responses" @("ok")))
        hold = [bool](Get-XbCaseValue $case "hold_mutex_on_claim" $false)
        mutex_name = $mutexName
        held = $null
        calls = $calls
    }
    $gateway = {
        param($Base, $Path, $Method, $Body, $WorkerSession)
        $ctx.calls.Add([ordered]@{ path = $Path; method = $Method; body = $Body; session = $WorkerSession })
        if ($Path -eq "/readyz") { return $ctx.ready }
        if ($Path -eq "/v2/worker/claim") {
            if ($ctx.hold) { $ctx.held = New-Object System.Threading.Mutex($false, $ctx.mutex_name); [void]$ctx.held.WaitOne() }
            return $ctx.claim
        }
        if ($Path -like "/v2/jobs/*/result") {
            $next = $(if ($ctx.responses.Count -gt 0) { $ctx.responses.Dequeue() } else { "ok" })
            if ($next -ne "ok") { throw $next }
            return [pscustomobject]@{ accepted = $true }
        }
        throw "gateway_http_404"
    }.GetNewClosure()

    $childEnvironment = @{
        XB_AC2_PASSWORD_ENV_VAR = "XB_TEST_CHILD_PASSWORD"
        XB_TEST_CHILD_PASSWORD = "synthetic-child-password"
        XB_TEST_CHILD_MODE = [string](Get-XbCaseValue $case "child_mode" "emit")
        XB_TEST_CHILD_TRACE = $tracePath
        XB_TEST_PRIMITIVE_TRACE = $primitiveTracePath
        XB_TEST_UTC_NOW = "2026-09-30T02:00:30Z"
        XB_TEST_MUTEX_NAME = $mutexName
        XB_TEST_MUTEX_WAIT_MS = "300"
    }
    $childOutput = Get-XbCaseValue $case "child_output" $null
    if ($null -ne $childOutput) { $childEnvironment.XB_TEST_CHILD_OUTPUT = ($childOutput | ConvertTo-Json -Depth 8 -Compress).Replace("__RELEASE__", $release) }
    $fakeCase = Get-XbCaseValue $case "fake_case" $null
    if ($null -ne $fakeCase) { $childEnvironment.XB_TEST_FAKE_CASE = ($fakeCase | ConvertTo-Json -Depth 8 -Compress) }
    $primitivePath = $(if ((Get-XbCaseValue $case "primitive" "fake") -eq "wrapper") { $WrapperChild } else { $FakeChild })
    if ([bool](Get-XbCaseValue $case "missing_primitive" $false)) { $primitivePath = Join-Path $WorkRoot "absent_primitive.ps1" }
    $killAction = $(if ([bool](Get-XbCaseValue $case "kill_noop" $false)) { { param($Process) } } else { { param($Process) $Process.Kill() } })
    $faultGuard = Get-XbCaseValue $case "fault_guard" $null
    if ([bool](Get-XbCaseValue $case "inherit_worker_fault" $false)) { [Environment]::SetEnvironmentVariable("XB_WORKER_FAULT", "skip_result_post", "Process") }
    $holder = $null
    if ([bool](Get-XbCaseValue $case "mutex_held_by_other_process" $false)) { $holder = Start-XbMutexHolder -Name $mutexName }
    $session = New-XbMemberGatewayWorkerSession
    $cycle = $null
    $cycleError = $null
    $cycleErrorStack = $null
    try {
        $cycle = Invoke-XbMemberGatewayWorkerCycle -GatewayBaseUrl "https://gateway.example.test" -EnableProductionWorker:(-not [bool](Get-XbCaseValue $case "disabled" $false)) -EnableProductionAdapter -Book ([string](Get-XbCaseValue $case "book" "production")) -ReleaseSha256 $release -PrimitiveScriptPath $primitivePath -ChildEnvironment $childEnvironment -FaultGuardConfig $faultGuard -WorkerFault ([string](Get-XbCaseValue $case "worker_fault" "")) -WorkerSession $session -GatewayRequest $gateway -MutexName $mutexName -DeadlineSeconds ([int](Get-XbCaseValue $case "deadline_seconds" 60)) -KillWaitMilliseconds ([int](Get-XbCaseValue $case "kill_wait_ms" 30000)) -KillAction $killAction -UtcNow { $now }.GetNewClosure()
    }
    catch { $cycleError = [string]$_.Exception.Message; $cycleErrorStack = ([string]$_.Exception.ToString() + "`n" + [string]$_.InvocationInfo.PositionMessage + "`n" + [string]$_.ScriptStackTrace) }
    $consolePreambleAfter = [Console]::InputEncoding.GetPreamble().Length
    [Environment]::SetEnvironmentVariable("XB_WORKER_FAULT", $null, "Process")
    if ($null -ne $holder) { if (-not $holder.HasExited) { $holder.Kill() }; [void]$holder.WaitForExit(20000) }
    if ($null -ne $ctx.held) { $ctx.held.ReleaseMutex(); $ctx.held.Dispose() }
    $trace = $null
    $primitiveInvocations = 0
    if (Test-Path -LiteralPath $primitiveTracePath) { $primitiveInvocations = @(Get-Content -LiteralPath $primitiveTracePath).Count }
    $childAlive = $null
    if (Test-Path -LiteralPath $tracePath) {
        $trace = [IO.File]::ReadAllText($tracePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
        $childAlive = $false
        try { $live = Get-Process -Id ([int]$trace.pid) -ErrorAction Stop; $childAlive = (-not $live.HasExited) } catch { $childAlive = $false }
        if ($childAlive) { Stop-Process -Id ([int]$trace.pid) -Force; Start-Sleep -Milliseconds 300 }
    }
    $results[$name] = [ordered]@{
        cycle = $cycle
        error = $cycleError
        error_stack = $cycleErrorStack
        calls = $calls.ToArray()
        session = $session
        trace = $trace
        primitive_invocations = $primitiveInvocations
        child_alive_after_cycle = $childAlive
        console_preamble_after_cycle = $consolePreambleAfter
    }
}
$payload = [ordered]@{ release = $release; cases = $results } | ConvertTo-Json -Depth 12
[IO.File]::WriteAllText($OutPath, $payload, [Text.UTF8Encoding]::new($false))
"""


def _normalised_result(body_json: str) -> dict:
    body = json.loads(body_json)
    body["primitive"]["release_sha256"] = NORMALISED_RELEASE
    return body


@unittest.skipUnless(Path(POWERSHELL).exists(), "Windows PowerShell 5.1 is required")
class MemberGatewayWorkerCyclePowerShellTests(unittest.TestCase):
    report: dict

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-v2-") as temp:
            root = Path(temp)
            harness = root / "harness.ps1"
            harness.write_text(HARNESS, encoding="utf-8")
            cases = root / "cases.json"
            cases.write_text(json.dumps(CYCLE_CASES + CROSS_CASES, ensure_ascii=False), encoding="utf-8")
            out = root / "out.json"
            completed = subprocess.run(
                [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness),
                 "-Lib", str(LIB), "-Adapter", str(ADAPTER), "-FakeChild", str(FAKE_CHILD), "-WrapperChild", str(WRAPPER_CHILD),
                 "-CasesPath", str(cases), "-WorkRoot", str(root), "-OutPath", str(out)],
                capture_output=True, text=True, timeout=900, env=os.environ.copy(),
            )
            if completed.returncode != 0 or not out.exists():
                raise AssertionError(completed.stdout[-4000:] + completed.stderr[-4000:])
            cls.report = json.loads(out.read_text(encoding="utf-8-sig"))

    def case(self, name):
        entry = self.report["cases"][name]
        self.assertIsNone(entry["error"], (name, entry["error"], entry["error_stack"]))
        calls = entry["calls"] or []
        for call in calls:
            for fragment in FORBIDDEN_PATH_FRAGMENTS:
                self.assertNotIn(fragment, call["path"], (name, call["path"]))
            self.assertEqual(call["session"], entry["session"])
        self.assertRegex(entry["session"], r"^ws-[0-9a-f]{32}$")
        return entry

    def paths(self, entry):
        return [(call["method"], call["path"]) for call in (entry["calls"] or [])]

    def posted(self, entry):
        return [json.loads(call["body"]) for call in (entry["calls"] or []) if call["path"].endswith("/result")]

    def test_created_cycle_is_exactly_readyz_claim_result(self):
        entry = self.case("kcy_created")
        self.assertEqual(self.paths(entry), [("GET", "/readyz"), ("POST", "/v2/worker/claim"), ("POST", f"/v2/jobs/{JOB_ID}/result")])
        self.assertEqual(entry["cycle"]["status"], "result_accepted")
        self.assertEqual(entry["cycle"]["writes"], 1)
        body = self.posted(entry)[0]
        expected = {"schema_version": "xb.member.gateway.result.v2", "job_id": JOB_ID, "attempt_no": 1, "lease_id": LEASE_ID, "state_version": 7,
                    **primitive_output(primitive={"release_sha256": self.report["release"], "rule_version": "XB-MN-1"})}
        self.assertEqual(body, expected)
        self.assertTrue(entry["calls"][2]["body"].isascii())
        trace = entry["trace"]
        self.assertEqual(trace["interpreter"].lower(), POWERSHELL.lower())
        self.assertIn("-NoProfile", trace["command_line"])
        self.assertEqual(trace["book"], "production")
        self.assertTrue(trace["production_adapter"])
        self.assertTrue(trace["password_present"])
        self.assertFalse(trace["password_on_command_line"])
        self.assertFalse(trace["worker_fault_present"])
        self.assertTrue(trace["request_line_ascii"])
        # Byte 0 / first character is the JSON object, never a preamble.
        self.assertEqual(trace["request_line"][:1], "{")
        self.assertNotIn("\ufeff", trace["request_line"])
        self.assertEqual(trace["decoded_name"], "Fixture Person \u9648")
        # The caller's (hostile, BOM-carrying) console encoding is restored.
        self.assertEqual(entry["console_preamble_after_cycle"], 3)
        request = json.loads(trace["request_line"])
        self.assertEqual(set(request), set(claim_body()["request"]) | {"job_id", "attempt_no", "first_claimed_at", "server_time_utc"})
        self.assertEqual(request["job_id"], JOB_ID)
        self.assertFalse(entry["child_alive_after_cycle"])

    def test_disabled_makes_no_call(self):
        entry = self.case("kcy_disabled")
        self.assertEqual(entry["cycle"], {"status": "disabled", "writes": 0})
        self.assertEqual(self.paths(entry), [])

    def test_stops_before_claim(self):
        for name, status in (("kcy_not_ready", "gateway_not_ready"), ("kcy_dispatch_disabled", "dispatch_disabled"), ("kcy_clock_skew", "clock_skew"), ("kcy_mutex_held", "mutex_held")):
            with self.subTest(name=name):
                entry = self.case(name)
                self.assertEqual(entry["cycle"]["status"], status)
                self.assertEqual(self.paths(entry), [("GET", "/readyz")])
                self.assertIsNone(entry["trace"])

    def test_idle_and_invalid_claims_launch_nothing(self):
        for name, status in (("kcy_idle", "idle"), ("kcy_claim_invalid", "claim_invalid")):
            with self.subTest(name=name):
                entry = self.case(name)
                self.assertEqual(entry["cycle"]["status"], status)
                self.assertEqual(self.paths(entry), [("GET", "/readyz"), ("POST", "/v2/worker/claim")])
                self.assertIsNone(entry["trace"])

    def assert_synthesised(self, name, outcome, reason, *, save_invoked):
        entry = self.case(name)
        body = self.posted(entry)
        self.assertEqual(len(body), 1, name)
        body = body[0]
        self.assertEqual(
            {key: body[key] for key in ("outcome", "reason_code", "rule", "branch", "member_no", "member_guid", "readback", "save_invoked", "save_invocation_count", "dq_flags", "error_code")},
            {"outcome": outcome, "reason_code": reason, "rule": "NONE", "branch": "NONE", "member_no": None, "member_guid": None, "readback": None,
             "save_invoked": save_invoked, "save_invocation_count": 1 if save_invoked else 0, "dq_flags": [], "error_code": None},
            name,
        )
        self.assertEqual(body["primitive"], {"release_sha256": self.report["release"], "rule_version": "XB-MN-1"})
        return entry

    def test_every_launched_child_received_a_preamble_free_request_line(self):
        launched = 0
        for name, entry in self.report["cases"].items():
            trace = entry["trace"]
            if not isinstance(trace, dict) or "request_line" not in trace:
                continue
            launched += 1
            self.assertTrue(trace["request_line_ascii"], name)
            self.assertEqual(trace["request_line"][:1], "{", name)
            self.assertEqual(entry["console_preamble_after_cycle"], 3, name)
        self.assertGreater(launched, 0)

    def test_deadline_kill_and_unconfirmed_exit_are_uncertain(self):
        entry = self.assert_synthesised("kcy_deadline_kill", "OUTCOME_UNCERTAIN", "child_deadline_exceeded", save_invoked=True)
        self.assertFalse(entry["child_alive_after_cycle"])
        entry = self.assert_synthesised("kcy_unconfirmed_exit", "OUTCOME_UNCERTAIN", "child_termination_unconfirmed", save_invoked=True)
        self.assertTrue(entry["child_alive_after_cycle"])

    def test_unreadable_primitive_output_is_uncertain(self):
        for name in ("kcy_garbage_output", "kcy_silent_exit", "kcy_two_lines", "kcy_wrong_release", "kcy_extra_key", "kcy_incoherent_count"):
            with self.subTest(name=name):
                self.assert_synthesised(name, "OUTCOME_UNCERTAIN", "primitive_output_invalid", save_invoked=True)

    def test_launch_failure_is_failed_before_write(self):
        self.assert_synthesised("kcy_launch_failed", "FAILED_BEFORE_WRITE", "primitive_launch_failed", save_invoked=False)

    def test_result_post_retry_and_discard_rules(self):
        expectations = {
            "kcy_result_409": ("result_conflict_discarded", 1),
            "kcy_result_5xx_retry": ("result_accepted", 3),
            "kcy_result_transport_exhausted": ("result_unacknowledged", 3),
            "kcy_result_4xx_final": ("result_rejected", 1),
            "kcy_result_after_lease": ("result_lease_expired", 0),
        }
        for name, (status, posts) in expectations.items():
            with self.subTest(name=name):
                entry = self.case(name)
                self.assertEqual((entry["cycle"]["status"], entry["cycle"]["posts"]), (status, posts))
                bodies = [call["body"] for call in entry["calls"] if call["path"].endswith("/result")]
                self.assertEqual(len(bodies), posts)
                self.assertEqual(len(set(bodies)), min(posts, 1))

    def test_worker_fault_hook_guard(self):
        entry = self.assert_synthesised("kcy_worker_fault_refused_in_production", "FAILED_BEFORE_WRITE", "fault_injection_refused", save_invoked=False)
        self.assertIsNone(entry["trace"])
        entry = self.case("kcy_worker_fault_guarded_skip")
        self.assertEqual(entry["cycle"]["status"], "result_skipped_by_fault")
        self.assertEqual(self.paths(entry), [("GET", "/readyz"), ("POST", "/v2/worker/claim")])
        self.assertEqual(entry["trace"]["book"], "test")

    # ---- X-CC golden fixture ------------------------------------------
    def test_cross_contract_golden_fixture(self):
        cases = []
        for spec in CROSS_CASES:
            entry = self.case(spec["name"])
            self.assertEqual(self.paths(entry)[:2], [("GET", "/readyz"), ("POST", "/v2/worker/claim")])
            posted = [call["body"] for call in entry["calls"] if call["path"].endswith("/result")]
            expected_posts = 2 if spec["name"] in ("R0_final_proof_rejected", "R0_final_field_mismatch") else 1
            self.assertEqual(len(posted), expected_posts, spec["name"])
            self.assertEqual(len(set(posted)), 1, spec["name"])
            self.assertEqual(entry["primitive_invocations"], 1, spec["name"])
            self.assertEqual(json.loads(posted[0])["primitive"]["release_sha256"], self.report["release"])
            cases.append({"name": spec["name"], "claim": spec["claim"], "result": _normalised_result(posted[0])})
        outcomes = {case["name"]: (case["result"]["outcome"], case["result"]["rule"]) for case in cases}
        self.assertEqual(outcomes, {
            "R1_create_base": ("CREATED_VERIFIED", "R1"),
            "R4_create_name_appended": ("CREATED_VERIFIED", "R4"),
            "R2c_link_existing": ("LINKED_EXISTING", "R2c"),
            "R0_prior_attempt": ("CREATED_VERIFIED_PRIOR_ATTEMPT", "R0"),
            "R0_final_proof_rejected": ("MANUAL_REVIEW", "R0"),
            "R0_final_field_mismatch": ("MANUAL_REVIEW", "R0"),
            "MUTEX_BUSY": ("MUTEX_BUSY", "NONE"),
        })
        document = {
            "schema_version": "xb.ac2.member_primitive.cross_contract_cases.v1",
            "description": "Claim v2 bodies the worker consumed and the exact result v2 bodies it posted, produced by the real worker cycle and the real primitive against the in-memory AutoCount double (tests/test_member_gateway_worker_cycle_ps.py). Synthetic data only.",
            "normalised_fields": {"result.primitive.release_sha256": "replaced by 64 zeros; the live value is the release identity of the scripts under test"},
            "cases": cases,
        }
        # A missing golden fixture is a failure, never a silent regeneration.
        if os.environ.get("XB_REGENERATE_PRIMITIVE_FIXTURES") == "1":
            GOLDEN.write_text(json.dumps(document, indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n")
        self.assertEqual(json.loads(GOLDEN.read_text(encoding="utf-8")), document)


class MemberGatewayWorkerLibrarySourceTests(unittest.TestCase):
    def test_absolute_interpreter_and_no_heartbeat(self):
        library = LIB.read_text(encoding="utf-8")
        self.assertIn('$script:XbWindowsPowerShellPath = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"', library)
        self.assertIn("$startInfo.FileName = $script:XbWindowsPowerShellPath", library)
        self.assertIn('"-NoProfile"', library)
        self.assertNotRegex(library, re.compile(r"heartbeat_seconds|/lease\"|Invoke-XbMemberGatewayProtectedWrite", re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
