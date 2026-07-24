"""PowerShell-side tests for the single-member creation UAT runner.

These tests execute real PowerShell (pwsh or Windows powershell) when available and
skip cleanly when it is not, so the suite still runs on hosts without PowerShell.
They cover, per the approved design:

* AST-based structural assertions on the runner (the only SaveMember invocation is
  behind one narrowly scoped function, called at most once, never inside a loop);
* the pure terminal-state transition logic, exercised without AutoCount;
* byte-identical cross-language payload_hash / fingerprint / source_record_id
  agreement between Python and PowerShell;
* the exclusive execution lock blocking a concurrent/replayed attempt;
* the runner's refusal and package-mismatch paths, which run before any AutoCount
  assembly is loaded (so no AutoCount is ever contacted).

The runner's actual AutoCount interaction is never executed here (there is no
AutoCount and no live environment); it is proven later during the operator UAT.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import _create_uat_fixtures as fx  # noqa: E402

RUNNER = SCRIPTS / "ac2_member_create_uat_runner.ps1"
LIB = SCRIPTS / "member_create_uat_runner_lib.ps1"
BUSINESS_CONFIG = ROOT / "config" / "member_create_uat_business_confirmation.json"


def find_powershell():
    for exe in ("pwsh", "powershell", "powershell.exe"):
        found = shutil.which(exe)
        if found:
            return found
    return None


PS = find_powershell()

INSPECTOR = r"""
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Path)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errs)
$funcs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
$narrow = $funcs | Where-Object { $_.Name -eq 'Invoke-CreateUatSaveMemberOnce' } | Select-Object -First 1
$calls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) |
    Where-Object { $_.GetCommandName() -eq 'Invoke-CreateUatSaveMemberOnce' })
$invokes = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true) |
    Where-Object { $_.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
        $_.Member.Value -eq 'Invoke' -and $_.Expression.Extent.Text -match 'SaveMemberMethod' })
$saveInsideNarrow = [bool]$narrow
if ($narrow) {
    foreach ($si in $invokes) {
        if ($si.Extent.StartOffset -lt $narrow.Extent.StartOffset -or $si.Extent.EndOffset -gt $narrow.Extent.EndOffset) {
            $saveInsideNarrow = $false
        }
    }
}
$loopNames = 'WhileStatementAst', 'ForStatementAst', 'DoWhileStatementAst', 'DoUntilStatementAst', 'ForEachStatementAst'
$callInLoop = $false
foreach ($c in $calls) {
    $p = $c.Parent
    while ($null -ne $p) { if ($loopNames -contains $p.GetType().Name) { $callInLoop = $true }; $p = $p.Parent }
}
[pscustomobject]@{
    parseErrors      = @($errs).Count
    funcExists       = [bool]$narrow
    callCount        = $calls.Count
    saveInvokeCount  = $invokes.Count
    saveInsideNarrow = $saveInsideNarrow
    callInLoop       = $callInLoop
} | ConvertTo-Json -Compress
"""

PROBE = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Lib,
    [Parameter(Mandatory)][string]$Op,
    [string]$Package,
    [string]$CtxJson,
    [string]$Dir
)
$ErrorActionPreference = 'Stop'
. $Lib
switch ($Op) {
    'hash' {
        $pkg = ConvertFrom-CreateUatJson -Raw (Get-Content -LiteralPath $Package -Raw -Encoding UTF8)
        $v = Test-CreateUatPackage -Package $pkg
        [pscustomobject]@{
            payload_hash = Get-CreateUatPayloadHash -Package $pkg
            fingerprint  = Get-CreateUatSourceFingerprint -Package $pkg
            srid         = Get-CreateUatSourceRecordId -MemberNo ([string]$pkg.member_payload.MemberNo)
            valid        = $v.Valid
            fpProblem    = $v.FingerprintProblem
        } | ConvertTo-Json -Compress
    }
    'terminal' {
        $ctx = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $ht = @{}
        foreach ($p in $ctx.PSObject.Properties) { $ht[$p.Name] = $p.Value }
        $contr = @(Get-CreateUatStateContradictions -Flags $ht)
        Write-Output ((Get-CreateUatTerminalCode -Flags $ht) + '|' + $contr.Count)
    }
    'lock' {
        $lock = Join-Path $Dir 'create_uat.lock'
        $first = New-CreateUatExclusiveLock -LockPath $lock
        $second = New-CreateUatExclusiveLock -LockPath $lock
        [pscustomobject]@{
            firstAcquired  = ($null -ne $first)
            secondBlocked  = ($null -eq $second)
        } | ConvertTo-Json -Compress
        Remove-CreateUatExclusiveLock -LockStream $first -LockPath $lock
    }
    'business' {
        $cfg = ConvertFrom-CreateUatJson -Raw (Get-Content -LiteralPath $Package -Raw -Encoding UTF8)
        $gate = Get-CreateUatBusinessGate -ConfigObject $cfg
        [pscustomobject]@{ confirmed = $gate.Confirmed; reasons = ($gate.Reasons -join ',') } | ConvertTo-Json -Compress
    }
    'durable' {
        $p = Join-Path $Dir 'artifact.marker'
        Write-CreateUatDurableArtifact -Path $p -Content 'first'
        $second_threw = $false
        try { Write-CreateUatDurableArtifact -Path $p -Content 'second' } catch { $second_threw = $true }
        [pscustomobject]@{ exists = (Test-Path -LiteralPath $p); noClobber = $second_threw } | ConvertTo-Json -Compress
    }
    'readback' {
        # Assigned uses the same typed values the runner assigns; readback mimics
        # AutoCount's returned representations (DateTime, Decimal, DBNull, string).
        $assigned = [ordered]@{
            MemberNo = '6590000001'; MemberType = 'Default'; Name = 'Synthetic Alpha'
            MobilePhone = ''; EmailAddress = 'a@example.invalid'
            DOB = [datetime]::ParseExact('2000-03-01', 'yyyy-MM-dd', [System.Globalization.CultureInfo]::InvariantCulture)
            RegisterDate = [datetime]::ParseExact('2026-07-01', 'yyyy-MM-dd', [System.Globalization.CultureInfo]::InvariantCulture)
            OpeningPoints = [decimal]0; IsActive = 'T'; Individual = 'T'
        }
        $good = @{
            MemberNo = '6590000001'; MemberType = 'Default'; Name = 'Synthetic Alpha'
            MobilePhone = [System.DBNull]::Value; EmailAddress = 'a@example.invalid'
            DOB = [datetime]'2000-03-01T00:00:00'; RegisterDate = [datetime]'2026-07-01T00:00:00'
            OpeningPoints = [decimal]0.0; IsActive = $true; Individual = 'T'
        }
        $bad = @{}
        foreach ($k in $good.Keys) { $bad[$k] = $good[$k] }
        $bad['Name'] = 'Different Name'
        $mGood = Test-CreateUatReadbackMatch -Assigned $assigned -Readback $good
        $mBad = Test-CreateUatReadbackMatch -Assigned $assigned -Readback $bad
        [pscustomobject]@{ goodMatch = $mGood.Match; badMatch = $mBad.Match; badMismatches = ($mBad.Mismatches -join ',') } | ConvertTo-Json -Compress
    }
    'expirycontract' {
        [pscustomobject]@{
            intendedHasExpiry     = ($script:CreateUatIntendedAssignmentFields -contains 'ExpiryDate')
            readbackHasExpiry     = ($script:CreateUatReadbackVerificationFields -contains 'ExpiryDate')
            activeHasExpiry       = ($script:CreateUatAssignableFields -contains 'ExpiryDate')
            capabilityImplemented = [bool]$script:CreateUatExpiryDateAssignmentImplemented
            intendedValue         = [string]$script:CreateUatExpiryDateIntendedValue
        } | ConvertTo-Json -Compress
    }
    'readbackexpiry' {
        # ExpiryDate is part of the read-back verification set. A matching ExpiryDate
        # (date vs string representation) normalises to a match; a wrong ExpiryDate
        # makes the whole comparison fail so it can never yield CREATED_VERIFIED.
        $assigned = [ordered]@{
            MemberNo = '659EXPIRY01'; MemberType = 'Default'; Name = 'Synthetic Expiry'
            EmailAddress = 'e@example.invalid'
            RegisterDate = [datetime]::ParseExact('2026-07-01', 'yyyy-MM-dd', [System.Globalization.CultureInfo]::InvariantCulture)
            ExpiryDate = [datetime]::ParseExact('2028-06-30', 'yyyy-MM-dd', [System.Globalization.CultureInfo]::InvariantCulture)
        }
        $good = @{
            MemberNo = '659EXPIRY01'; MemberType = 'Default'; Name = 'Synthetic Expiry'
            EmailAddress = 'e@example.invalid'
            RegisterDate = [datetime]'2026-07-01T00:00:00'; ExpiryDate = [datetime]'2028-06-30T00:00:00'
        }
        $bad = @{}
        foreach ($k in $good.Keys) { $bad[$k] = $good[$k] }
        $bad['ExpiryDate'] = [datetime]'2099-01-01T00:00:00'
        $mGood = Test-CreateUatReadbackMatch -Assigned $assigned -Readback $good
        $mBad = Test-CreateUatReadbackMatch -Assigned $assigned -Readback $bad
        [pscustomobject]@{ goodMatch = $mGood.Match; badMatch = $mBad.Match; badMismatches = ($mBad.Mismatches -join ',') } | ConvertTo-Json -Compress
    }
}
"""


@unittest.skipIf(PS is None, "no PowerShell executable available")
class PowerShellRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.inspector = cls.tmp / "inspector.ps1"
        cls.inspector.write_text(INSPECTOR, encoding="utf-8")
        cls.probe = cls.tmp / "probe.ps1"
        cls.probe.write_text(PROBE, encoding="utf-8")

    def _ps(self, script, *args):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc

    # ---- AST structural assertions ---- #
    def test_ast_single_save_invocation_behind_one_narrow_function(self):
        proc = self._ps(self.inspector, "-Path", str(RUNNER))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        info = json.loads(proc.stdout)
        self.assertEqual(info["parseErrors"], 0)
        self.assertTrue(info["funcExists"], "Invoke-CreateUatSaveMemberOnce must exist")
        self.assertEqual(info["saveInvokeCount"], 1, "exactly one SaveMember .Invoke call site")
        self.assertTrue(info["saveInsideNarrow"], "the SaveMember call must be inside the narrow function")
        self.assertEqual(info["callCount"], 1, "the narrow function is called exactly once")
        self.assertFalse(info["callInLoop"], "no retry loop may enclose the SaveMember call")

    def test_runner_assigns_expiry_date_from_payload(self):
        import re
        source = RUNNER.read_text(encoding="utf-8")
        # ExpiryDate is now an active assignable field: it is read from the member payload
        # and assigned into the in-memory assignment set exactly once.
        self.assertIn("member_payload.ExpiryDate", source)
        self.assertIsNotNone(
            re.search(r"ExpiryDate\s*=\s*\[datetime\]::ParseExact", source),
            "ExpiryDate must be parsed and assigned",
        )
        self.assertEqual(
            len(re.findall(r"ExpiryDate\s*=\s*\[datetime\]::ParseExact", source)), 1,
            "ExpiryDate must be assigned exactly once",
        )

    def test_runner_declares_all_five_write_confirmation_switches(self):
        source = RUNNER.read_text(encoding="utf-8")
        for switch in (
            "EnableMemberCreateUat",
            "ConfirmAutoCountWrite",
            "ConfirmExactlyOneMember",
            "ConfirmDryRunPassed",
            "ConfirmNoExistingMemberUpdate",
        ):
            self.assertIn(switch, source)

    # ---- Pure terminal-state transitions (no AutoCount) ---- #
    def _terminal(self, **flags):
        # Snake_case flags matching the runner result; returns (code, contradiction_count).
        base = {
            "mode": "dry-run", "package_fingerprint_problem": False, "package_structural_valid": True,
            "approval_not_expired": True, "write_confirmed": False, "business_confirmed": False,
            "lock_acquired": True, "recovery_state": "none", "execution_error": False,
            "member_exists_initial": False, "member_exists_recheck": False,
            "save_member_attempted": False, "save_member_confirmed": False,
            "save_outcome": "not_attempted", "readback_found": False, "readback_match": False,
        }
        base.update(flags)
        ctx_path = self.tmp / "ctx.json"
        ctx_path.write_text(json.dumps(base), encoding="utf-8")
        proc = self._ps(self.probe, "-Lib", str(LIB), "-Op", "terminal", "-CtxJson", str(ctx_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        code, contr = proc.stdout.strip().split("|")
        return code, int(contr)

    def _w(self, **over):
        # A consistent WRITE-mode confirmed-save flag set, then apply overrides. ExpiryDate
        # is an active field, so a real save carries expiry_date_assigned and the full
        # expected assigned-field count (9 assignable + 2 activation = 11).
        base = dict(
            mode="write", write_confirmed=True, business_confirmed=True, lock_acquired=True,
            save_member_attempted=True, save_member_confirmed=True, save_outcome="confirmed",
            readback_found=True, readback_match=True,
            expiry_date_assigned=True, assigned_field_count=11,
        )
        base.update(over)
        return base

    def test_terminal_state_matrix(self):
        # Each case is a self-consistent flag set; assert code AND zero contradictions.
        self.assertEqual(self._terminal(), ("DRY_RUN_VALIDATED", 0))
        self.assertEqual(self._terminal(package_fingerprint_problem=True), ("SOURCE_FINGERPRINT_MISMATCH", 0))
        self.assertEqual(self._terminal(package_structural_valid=False), ("FAILED_BEFORE_WRITE", 0))
        self.assertEqual(self._terminal(approval_not_expired=False), ("APPROVAL_INVALID", 0))
        self.assertEqual(self._terminal(mode="write", write_confirmed=False), ("WRITE_NOT_CONFIRMED", 0))
        self.assertEqual(self._terminal(mode="write", write_confirmed=True, business_confirmed=False), ("OPERATOR_CONFIG_REQUIRED", 0))
        self.assertEqual(self._terminal(lock_acquired=False), ("EXECUTION_LOCKED", 0))
        self.assertEqual(self._terminal(recovery_state="terminal_exists"), ("PACKAGE_ALREADY_CONSUMED", 0))
        self.assertEqual(self._terminal(recovery_state="consumed_no_terminal"), ("WRITE_OUTCOME_UNCERTAIN", 0))
        self.assertEqual(self._terminal(recovery_state="intent_no_consumed"), ("FAILED_BEFORE_WRITE", 0))
        self.assertEqual(self._terminal(recovery_state="malformed"), ("WRITE_OUTCOME_UNCERTAIN", 0))
        self.assertEqual(self._terminal(member_exists_initial=True), ("BLOCKED_MEMBER_EXISTS", 0))
        self.assertEqual(self._terminal(**self._w(member_exists_recheck=True, save_member_attempted=False, save_member_confirmed=False, save_outcome="not_attempted", readback_found=False, readback_match=False)), ("BLOCKED_MEMBER_EXISTS", 0))
        self.assertEqual(self._terminal(**self._w(save_member_confirmed=False, save_outcome="uncertain", readback_found=False, readback_match=False)), ("WRITE_OUTCOME_UNCERTAIN", 0))
        self.assertEqual(self._terminal(**self._w()), ("CREATED_VERIFIED", 0))
        self.assertEqual(self._terminal(**self._w(readback_match=False)), ("CREATED_READBACK_MISMATCH", 0))
        self.assertEqual(self._terminal(**self._w(readback_found=False, readback_match=False)), ("WRITE_OUTCOME_UNCERTAIN", 0))
        # A contradictory set is flagged (confirmed save in dry-run mode).
        _, contr = self._terminal(mode="dry-run", save_member_attempted=True, save_member_confirmed=True, save_outcome="confirmed", readback_found=True, readback_match=True)
        self.assertGreater(contr, 0)

    def test_expiry_and_count_guards(self):
        # A confirmed-save flag set missing the ExpiryDate assignment is a contradiction.
        _, contr = self._terminal(**self._w(expiry_date_assigned=False))
        self.assertGreater(contr, 0)
        # A stale assigned-field count (not 11) with a save attempt is a contradiction.
        _, contr2 = self._terminal(**self._w(assigned_field_count=10))
        self.assertGreater(contr2, 0)
        # The clean confirmed set (ExpiryDate assigned, count 11) verifies with none.
        code, contr3 = self._terminal(**self._w())
        self.assertEqual(code, "CREATED_VERIFIED")
        self.assertEqual(contr3, 0)

    # ---- Cross-language hash agreement ---- #
    def _cross_language_hash(self, payload_overrides=None):
        pkg = fx.build_valid_package(payload_overrides)
        pkg_path = self.tmp / "xlpkg.json"
        fx.write_package(pkg_path, pkg)
        proc = self._ps(self.probe, "-Lib", str(LIB), "-Op", "hash", "-Package", str(pkg_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ps = json.loads(proc.stdout)
        self.assertEqual(ps["payload_hash"], pkg["payload_hash"])
        self.assertEqual(ps["fingerprint"], pkg["source_fingerprint"])
        self.assertEqual(ps["srid"], pkg["source_record_id"])
        self.assertTrue(ps["valid"], proc.stdout)

    def test_cross_language_hash_ascii(self):
        self._cross_language_hash()

    def test_cross_language_hash_non_ascii_name(self):
        # CJK name stresses the ensure_ascii escaping agreement.
        self._cross_language_hash({"Name": "X陈 Zoe"})

    # ---- Exclusive lock ---- #
    def test_exclusive_lock_blocks_concurrent_attempt(self):
        lock_dir = self.tmp / "lockdir"
        lock_dir.mkdir(exist_ok=True)
        proc = self._ps(self.probe, "-Lib", str(LIB), "-Op", "lock", "-Dir", str(lock_dir))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        info = json.loads(proc.stdout)
        self.assertTrue(info["firstAcquired"])
        self.assertTrue(info["secondBlocked"])

    # ---- Business gate (finding 1): pinned config + ExpiryDate capability ---- #
    def _business(self, config_obj):
        cfg_path = self.tmp / "biz.json"
        cfg_path.write_text(json.dumps(config_obj), encoding="utf-8")
        proc = self._ps(self.probe, "-Lib", str(LIB), "-Op", "business", "-Package", str(cfg_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_repo_config_is_confirmed_and_capability_proven(self):
        cfg = json.loads(BUSINESS_CONFIG.read_text(encoding="utf-8"))
        gate = self._business(cfg)
        self.assertTrue(gate["confirmed"], gate["reasons"])
        # The capability is now proven, so the block reason must be gone.
        self.assertNotIn("expiry_date_capability_unproven", gate["reasons"])

    def test_all_true_config_now_confirms(self):
        cfg = {
            "schema_version": "member_create_uat_business_confirmation/v1",
            "confirmations": {
                f: {"confirmed": True, "reason": "x"}
                for f in ("MemberType", "RegisterDate", "ExpiryDate", "OpeningPoints")
            },
        }
        gate = self._business(cfg)
        self.assertTrue(gate["confirmed"], "four true booleans now confirm because ExpiryDate is proven")

    def test_partial_confirmation_still_blocks(self):
        # A false confirmation still fails closed even though the capability is proven.
        cfg = {
            "schema_version": "member_create_uat_business_confirmation/v1",
            "confirmations": {
                "MemberType": {"confirmed": True, "reason": "x"},
                "RegisterDate": {"confirmed": True, "reason": "x"},
                "ExpiryDate": {"confirmed": False, "reason": "x"},
                "OpeningPoints": {"confirmed": True, "reason": "x"},
            },
        }
        gate = self._business(cfg)
        self.assertFalse(gate["confirmed"])
        self.assertIn("confirmation_ExpiryDate_not_confirmed", gate["reasons"])

    def test_wrong_schema_version_rejected(self):
        cfg = {"schema_version": "wrong/v9", "confirmations": {}}
        gate = self._business(cfg)
        self.assertFalse(gate["confirmed"])
        self.assertIn("schema_version_mismatch", gate["reasons"])

    # ---- Durable write (finding 2) ---- #
    def test_durable_artifact_never_overwrites(self):
        d = self.tmp / "durable"
        d.mkdir(exist_ok=True)
        proc = self._ps(self.probe, "-Lib", str(LIB), "-Op", "durable", "-Dir", str(d))
        info = json.loads(proc.stdout)
        self.assertTrue(info["exists"])
        self.assertTrue(info["noClobber"], "a second durable write to an existing path must fail closed")

    # ---- Read-back (finding 3) ---- #
    def test_readback_normalizes_date_decimal_blank_bool(self):
        proc = self._ps(self.probe, "-Lib", str(LIB), "-Op", "readback", "-Dir", str(self.tmp))
        info = json.loads(proc.stdout)
        self.assertTrue(info["goodMatch"], "date/decimal/blank/bool representations must normalise to a match")
        self.assertFalse(info["badMatch"])
        self.assertIn("Name", info["badMismatches"])

    # ---- ExpiryDate prepared contract (PowerShell library mirror) ---- #
    def test_lib_active_contract_now_includes_expiry(self):
        proc = self._ps(self.probe, "-Lib", str(LIB), "-Op", "expirycontract")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        info = json.loads(proc.stdout)
        self.assertTrue(info["intendedHasExpiry"], "intended assignment contract must include ExpiryDate")
        self.assertTrue(info["readbackHasExpiry"], "read-back verification contract must include ExpiryDate")
        self.assertTrue(info["activeHasExpiry"], "active assignable whitelist must now include ExpiryDate")
        self.assertTrue(info["capabilityImplemented"], "capability flag must now be true")
        self.assertEqual(info["intendedValue"], "2028-06-30")

    def test_readback_expiry_mismatch_fails_and_cannot_verify(self):
        proc = self._ps(self.probe, "-Lib", str(LIB), "-Op", "readbackexpiry", "-Dir", str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        info = json.loads(proc.stdout)
        self.assertTrue(info["goodMatch"], "a correct ExpiryDate read-back must normalise to a match")
        self.assertFalse(info["badMatch"], "a wrong ExpiryDate read-back must not match")
        self.assertIn("ExpiryDate", info["badMismatches"])
        # A non-match feeds readback_match=False, which the terminal table maps to
        # CREATED_READBACK_MISMATCH (never CREATED_VERIFIED).
        code, contr = self._terminal(**self._w(readback_match=False))
        self.assertEqual(code, "CREATED_READBACK_MISMATCH")
        self.assertEqual(contr, 0)

    # ---- Durable recovery (finding 2): none permits an automatic second save ---- #
    def _seed_marker(self, state_dir, name, package):
        marker = {
            "operation_id": package["operation_id"], "approval_id": package["approval"]["approval_id"],
            "payload_hash": package["payload_hash"], "source_record_id": package["source_record_id"],
            "source_fingerprint": package["source_fingerprint"], "recorded_at_utc": "2026-07-23T00:00:00Z",
        }
        (Path(state_dir) / name).write_text(json.dumps(marker), encoding="utf-8")

    def test_recovery_consumed_without_terminal_is_uncertain(self):
        pkg = fx.build_valid_package()
        pkg_path = self.tmp / "rec_pkg.json"
        fx.write_package(pkg_path, pkg)
        state = self.tmp / "rec_state1"
        state.mkdir(exist_ok=True)
        self._seed_marker(state, f"consumed_{pkg['source_record_id']}.marker", pkg)
        proc = self._run_runner(pkg_path, state_dir=state)  # dry-run reaches recovery without AutoCount
        result = json.loads(proc.stdout)
        self.assertEqual(result["recovery_state"], "consumed_no_terminal")
        self.assertEqual(result["terminal_code"], "WRITE_OUTCOME_UNCERTAIN")
        self.assertFalse(result["authentication_success"])

    def test_recovery_intent_without_consumed_is_failed_before_write(self):
        pkg = fx.build_valid_package()
        pkg_path = self.tmp / "rec_pkg2.json"
        fx.write_package(pkg_path, pkg)
        state = self.tmp / "rec_state2"
        state.mkdir(exist_ok=True)
        self._seed_marker(state, f"write_intent_{pkg['operation_id']}.marker", pkg)
        proc = self._run_runner(pkg_path, state_dir=state)
        result = json.loads(proc.stdout)
        self.assertEqual(result["recovery_state"], "intent_no_consumed")
        self.assertEqual(result["terminal_code"], "FAILED_BEFORE_WRITE")

    def test_recovery_terminal_exists_is_package_already_consumed(self):
        pkg = fx.build_valid_package()
        pkg_path = self.tmp / "rec_pkg3.json"
        fx.write_package(pkg_path, pkg)
        state = self.tmp / "rec_state3"
        state.mkdir(exist_ok=True)
        (state / f"result_{pkg['operation_id']}.json").write_text(json.dumps({"terminal_code": "CREATED_VERIFIED"}), encoding="utf-8")
        proc = self._run_runner(pkg_path, state_dir=state)
        result = json.loads(proc.stdout)
        self.assertEqual(result["recovery_state"], "terminal_exists")
        self.assertEqual(result["terminal_code"], "PACKAGE_ALREADY_CONSUMED")

    # ---- Runner refusal / mismatch paths (pre-AutoCount) ---- #
    def _run_runner(self, package_path, *extra, state_dir=None):
        state = Path(state_dir) if state_dir is not None else (self.tmp / "runstate")
        state.mkdir(exist_ok=True)
        # The business config is PINNED inside the runner; there is no override flag.
        cmd = [
            PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(RUNNER),
            "-PackagePath", str(package_path), "-StateDir", str(state), *extra,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc

    def test_write_mode_without_all_confirmations_refuses(self):
        pkg = fx.build_valid_package()
        pkg_path = self.tmp / "pkg_refuse.json"
        fx.write_package(pkg_path, pkg)
        proc = self._run_runner(pkg_path, "-EnableMemberCreateUat", "-ConfirmAutoCountWrite")
        result = json.loads(proc.stdout)
        self.assertEqual(result["terminal_code"], "WRITE_NOT_CONFIRMED")

    def test_write_mode_confirmed_still_never_contacts_autocount_here(self):
        # The committed business config is now confirmed, so write mode passes the
        # business gate. -AcRoot is pointed at a nonexistent directory so the runner can
        # never load an AutoCount assembly or authenticate on ANY host (there is no live
        # AutoCount in dev/CI): it must stop before any write, never attempt SaveMember,
        # and classify honestly. This proves the flip did not open a live-write path.
        pkg = fx.build_valid_package()
        pkg_path = self.tmp / "pkg_confirmed.json"
        fx.write_package(pkg_path, pkg)
        bogus_ac = str(self.tmp / "no_such_autocount_root")
        proc = self._run_runner(
            pkg_path, "-EnableMemberCreateUat", "-ConfirmAutoCountWrite", "-ConfirmExactlyOneMember",
            "-ConfirmDryRunPassed", "-ConfirmNoExistingMemberUpdate", "-AcRoot", bogus_ac,
        )
        result = json.loads(proc.stdout)
        self.assertTrue(result["business_confirmed"], "business gate now passes")
        self.assertFalse(result["authentication_success"], "AutoCount must never authenticate here")
        self.assertFalse(result["save_member_attempted"], "no SaveMember may be attempted")
        self.assertFalse(result["save_member_confirmed"])
        self.assertEqual(result["save_outcome"], "not_attempted")
        self.assertEqual(result["terminal_code"], "FAILED_BEFORE_WRITE")

    def test_tampered_package_is_source_fingerprint_mismatch(self):
        pkg = fx.build_valid_package()
        pkg["member_payload"]["Name"] = "Tampered After Build"
        pkg_path = self.tmp / "pkg_tampered.json"
        fx.write_package(pkg_path, pkg)
        proc = self._run_runner(pkg_path)
        result = json.loads(proc.stdout)
        self.assertEqual(result["terminal_code"], "SOURCE_FINGERPRINT_MISMATCH")

    def test_runner_output_is_pii_free(self):
        pkg = fx.build_valid_package()
        pkg_path = self.tmp / "pkg_pii.json"
        fx.write_package(pkg_path, pkg)
        # -AcRoot is a nonexistent directory so this write-mode run (the business gate now
        # passes) still stops before any AutoCount load/authentication on every host.
        bogus_ac = str(self.tmp / "no_such_autocount_root")
        proc = self._run_runner(
            pkg_path, "-EnableMemberCreateUat", "-ConfirmAutoCountWrite", "-ConfirmExactlyOneMember",
            "-ConfirmDryRunPassed", "-ConfirmNoExistingMemberUpdate", "-AcRoot", bogus_ac,
        )
        self.assertNotIn("6590000001", proc.stdout)
        self.assertNotIn("Synthetic Alpha", proc.stdout)
        self.assertNotIn("synthetic.alpha@example.invalid", proc.stdout)


if __name__ == "__main__":
    unittest.main()
