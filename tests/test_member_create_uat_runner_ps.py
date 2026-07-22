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
        Write-Output (Get-CreateUatTerminalCode -Ctx $ht)
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

    def test_runner_never_assigns_expiry_date(self):
        import re
        source = RUNNER.read_text(encoding="utf-8")
        # ExpiryDate may appear in explanatory comments, but must never be assigned
        # to the member row or read from the member payload for assignment.
        self.assertIsNone(re.search(r"ExpiryDate\s*=", source), "ExpiryDate must never be assigned")
        self.assertNotIn("member_payload.ExpiryDate", source)

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
    def _terminal(self, **ctx):
        base = {
            "PackageFingerprintProblem": False, "PackageValid": True, "ApprovalExpired": False,
            "ForWrite": False, "WriteConfirmed": False, "BusinessConfirmed": False,
            "LockAcquired": True, "AlreadyConsumed": False, "MemberExistsInitial": False,
            "MemberExistsRecheck": False, "SaveOutcome": "not_attempted", "ReadbackMatch": False,
        }
        base.update(ctx)
        ctx_path = self.tmp / "ctx.json"
        ctx_path.write_text(json.dumps(base), encoding="utf-8")
        proc = self._ps(self.probe, "-Lib", str(LIB), "-Op", "terminal", "-CtxJson", str(ctx_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_terminal_state_matrix(self):
        self.assertEqual(self._terminal(), "DRY_RUN_VALIDATED")
        self.assertEqual(self._terminal(PackageFingerprintProblem=True), "SOURCE_FINGERPRINT_MISMATCH")
        self.assertEqual(self._terminal(PackageValid=False), "FAILED_BEFORE_WRITE")
        self.assertEqual(self._terminal(ApprovalExpired=True), "APPROVAL_INVALID")
        self.assertEqual(self._terminal(ForWrite=True, WriteConfirmed=False), "WRITE_NOT_CONFIRMED")
        self.assertEqual(self._terminal(ForWrite=True, WriteConfirmed=True, BusinessConfirmed=False), "OPERATOR_CONFIG_REQUIRED")
        self.assertEqual(self._terminal(LockAcquired=False), "EXECUTION_LOCKED")
        self.assertEqual(self._terminal(AlreadyConsumed=True), "PACKAGE_ALREADY_CONSUMED")
        self.assertEqual(self._terminal(MemberExistsInitial=True), "BLOCKED_MEMBER_EXISTS")
        self.assertEqual(
            self._terminal(ForWrite=True, WriteConfirmed=True, BusinessConfirmed=True, MemberExistsRecheck=True),
            "BLOCKED_MEMBER_EXISTS",
        )
        self.assertEqual(
            self._terminal(ForWrite=True, WriteConfirmed=True, BusinessConfirmed=True, SaveOutcome="uncertain"),
            "WRITE_OUTCOME_UNCERTAIN",
        )
        self.assertEqual(
            self._terminal(ForWrite=True, WriteConfirmed=True, BusinessConfirmed=True, SaveOutcome="confirmed", ReadbackMatch=True),
            "CREATED_VERIFIED",
        )
        self.assertEqual(
            self._terminal(ForWrite=True, WriteConfirmed=True, BusinessConfirmed=True, SaveOutcome="confirmed", ReadbackMatch=False),
            "CREATED_READBACK_MISMATCH",
        )

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

    # ---- Runner refusal / mismatch paths (pre-AutoCount) ---- #
    def _run_runner(self, package_path, *extra):
        state = self.tmp / "runstate"
        state.mkdir(exist_ok=True)
        cmd = [
            PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(RUNNER),
            "-PackagePath", str(package_path), "-StateDir", str(state),
            "-BusinessConfigPath", str(BUSINESS_CONFIG), *extra,
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

    def test_write_mode_business_unconfirmed_stops_before_autocount(self):
        pkg = fx.build_valid_package()
        pkg_path = self.tmp / "pkg_opcfg.json"
        fx.write_package(pkg_path, pkg)
        proc = self._run_runner(
            pkg_path, "-EnableMemberCreateUat", "-ConfirmAutoCountWrite", "-ConfirmExactlyOneMember",
            "-ConfirmDryRunPassed", "-ConfirmNoExistingMemberUpdate",
        )
        result = json.loads(proc.stdout)
        self.assertEqual(result["terminal_code"], "OPERATOR_CONFIG_REQUIRED")
        self.assertFalse(result["authentication_success"], "AutoCount must never be contacted when business is unconfirmed")
        self.assertFalse(result["business_confirmed"])

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
        proc = self._run_runner(
            pkg_path, "-EnableMemberCreateUat", "-ConfirmAutoCountWrite", "-ConfirmExactlyOneMember",
            "-ConfirmDryRunPassed", "-ConfirmNoExistingMemberUpdate",
        )
        self.assertNotIn("6590000001", proc.stdout)
        self.assertNotIn("Synthetic Alpha", proc.stdout)
        self.assertNotIn("synthetic.alpha@example.invalid", proc.stdout)


if __name__ == "__main__":
    unittest.main()
