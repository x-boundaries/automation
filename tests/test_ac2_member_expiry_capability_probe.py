"""Static, AST, library, and subprocess tests for the synthetic ExpiryDate probe.

No test executes the probe's AutoCount path or contacts AutoCount, n8n, Google Sheets,
or the host/VM. Subprocess runs of the probe are driven so they stop before any
AutoCount assembly load (refusal, pre-AutoCount config failure, or a pre-existing
attempt claim). The pure state/claim/result/fingerprint helpers are exercised directly
against scripts/member_expiry_capability_probe_lib.ps1 without AutoCount.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_member_expiry_capability_probe.ps1"
LIB = ROOT / "scripts" / "member_expiry_capability_probe_lib.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"
RUNBOOK = DOCS / "member_expiry_capability_probe_runbook.md"
WORKFLOW = ROOT / ".github" / "workflows" / "member-create-uat-tests.yml"

WRITE_SWITCHES = (
    "EnableExpiryCapabilityProbe",
    "ConfirmSyntheticExpiryDateTest",
    "ConfirmSingleSyntheticMember",
    "ConfirmAutoCountWrite",
    "ConfirmDryRunPreflightPassed",
    "ConfirmNoUpdateOrDelete",
)


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
$narrow = $funcs | Where-Object { $_.Name -eq 'Invoke-ExpiryProbeSaveMemberOnce' } | Select-Object -First 1
$calls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) |
    Where-Object { $_.GetCommandName() -eq 'Invoke-ExpiryProbeSaveMemberOnce' })
$invokes = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true))
$saveInvokes = @($invokes | Where-Object {
    $_.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
    $_.Member.Value -eq 'Invoke' -and $_.Expression.Extent.Text -match 'saveMemberMethod|SaveMemberMethod' })
$getInvokes = @($invokes | Where-Object {
    $_.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
    $_.Member.Value -eq 'Invoke' -and $_.Expression.Extent.Text -match 'getMemberMethod' })
$forbidden = @($invokes | Where-Object {
    $_.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
    @('DeleteMember','UpdateMember','RemoveMember','Rollback','DeleteMemberType','SaveMemberType') -contains $_.Member.Value })
$saveInsideNarrow = [bool]$narrow
if ($narrow) {
    foreach ($si in $saveInvokes) {
        if ($si.Extent.StartOffset -lt $narrow.Extent.StartOffset -or $si.Extent.EndOffset -gt $narrow.Extent.EndOffset) {
            $saveInsideNarrow = $false
        }
    }
}
$loopNames = 'WhileStatementAst', 'ForStatementAst', 'DoWhileStatementAst', 'DoUntilStatementAst', 'ForEachStatementAst'
$callInLoop = $false
$saveCallOffset = -1
foreach ($c in $calls) {
    if ($saveCallOffset -lt 0) { $saveCallOffset = $c.Extent.StartOffset }
    $p = $c.Parent
    while ($null -ne $p) { if ($loopNames -contains $p.GetType().Name) { $callInLoop = $true }; $p = $p.Parent }
}
$minGet = -1
foreach ($g in $getInvokes) { if ($minGet -lt 0 -or $g.Extent.StartOffset -lt $minGet) { $minGet = $g.Extent.StartOffset } }
$dupBeforeSave = ($minGet -ge 0 -and $saveCallOffset -ge 0 -and $minGet -lt $saveCallOffset)
[pscustomobject]@{
    parseErrors            = @($errs).Count
    funcExists             = [bool]$narrow
    callCount              = $calls.Count
    saveInvokeCount        = $saveInvokes.Count
    saveInsideNarrow       = $saveInsideNarrow
    callInLoop             = $callInLoop
    getMemberInvokeCount   = $getInvokes.Count
    dupCheckBeforeSave     = $dupBeforeSave
    forbiddenMutationCount = $forbidden.Count
} | ConvertTo-Json -Compress
"""

LIBPROBE = r"""
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Lib, [Parameter(Mandatory)][string]$Op,
      [string]$CtxJson, [string]$Text, [string]$Dir)
$ErrorActionPreference = 'Stop'
. $Lib
switch ($Op) {
    'terminal' {
        $ctx = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $code = Get-ExpiryProbeTerminalOutcome -Flags $ctx
        $contr = @(Get-ExpiryProbeStateContradictions -Flags $ctx).Count
        Write-Output ($code + '|' + $contr)
    }
    'exit' { Write-Output ([string](Get-ExpiryProbeExitCode -TerminalOutcome $Text)) }
    'final' {
        # $Text = "underlying|durableRequired(0/1)|evidencePersisted(0/1)"
        $parts = $Text.Split('|')
        $o = Get-ExpiryProbeFinalOutcome -UnderlyingOutcome $parts[0] -DurableRequired ([bool][int]$parts[1]) -EvidencePersisted ([bool][int]$parts[2])
        Write-Output ($o + '|' + (Get-ExpiryProbeExitCode -TerminalOutcome $o))
    }
    'redact' { Write-Output (Get-ExpiryProbePathRedacted -Text $Text) }
    'badclaim' {
        # Inject a durable persistence failure: a path under a non-existent directory
        # cannot be created, so New-ExpiryProbeDurableArtifact must throw and leave no
        # usable artefact (proving failures propagate, never a silent success).
        $bad = Join-Path (Join-Path $Dir 'no_such_dir') 'x.claim'
        $threw = $false
        try { New-ExpiryProbeDurableArtifact -Path $bad -Content 'x' } catch { $threw = $true }
        [pscustomobject]@{ threw = $threw; exists = (Test-Path -LiteralPath $bad) } | ConvertTo-Json -Compress
    }
    'fp' {
        $t1 = Get-ExpiryProbeTargetFingerprint -ServerName 'S' -DatabaseName 'D'
        $t2 = Get-ExpiryProbeTargetFingerprint -ServerName 'S' -DatabaseName 'D'
        $tX = Get-ExpiryProbeTargetFingerprint -ServerName 'S' -DatabaseName 'OTHER'
        # Casing AND surrounding-whitespace variants of the same target must collapse.
        $tUpper = Get-ExpiryProbeTargetFingerprint -ServerName 'SERVER\INSTANCE' -DatabaseName 'AED_DB'
        $tLower = Get-ExpiryProbeTargetFingerprint -ServerName '  server\instance ' -DatabaseName ' aed_db '
        $afU = Get-ExpiryProbeAttemptFingerprint -TargetFingerprint $tUpper -SyntheticFingerprint (Get-ExpiryProbeSyntheticFingerprint -MemberNo 'XBEXPIRYPROBE01') -IntendedExpiry '2028-06-30'
        $afL = Get-ExpiryProbeAttemptFingerprint -TargetFingerprint $tLower -SyntheticFingerprint (Get-ExpiryProbeSyntheticFingerprint -MemberNo 'XBEXPIRYPROBE01') -IntendedExpiry '2028-06-30'
        $sm = Get-ExpiryProbeSyntheticFingerprint -MemberNo 'XBEXPIRYPROBE01'
        $af = Get-ExpiryProbeAttemptFingerprint -TargetFingerprint $t1 -SyntheticFingerprint $sm -IntendedExpiry '2028-06-30'
        [pscustomobject]@{
            targetStable = ($t1 -eq $t2); targetDiffers = ($t1 -ne $tX)
            targetShape = ($t1 -match '^tfp_[0-9a-f]{64}$'); attemptShape = ($af -match '^afp_[0-9a-f]{64}$')
            synthShape = ($sm -match '^smf_[0-9a-f]{64}$')
            caseWsInsensitiveTarget = ($tUpper -eq $tLower); caseWsInsensitiveAttempt = ($afU -eq $afL)
        } | ConvertTo-Json -Compress
    }
    'claim' {
        $p = Join-Path $Dir 'x.claim'
        New-ExpiryProbeDurableArtifact -Path $p -Content 'first'
        $blocked = $false
        try { New-ExpiryProbeDurableArtifact -Path $p -Content 'second' } catch { $blocked = $true }
        $content = Get-Content -LiteralPath $p -Raw
        [pscustomobject]@{ exists = (Test-Path -LiteralPath $p); secondBlocked = $blocked; unchanged = ($content.Trim() -eq 'first') } | ConvertTo-Json -Compress
    }
    'result' {
        $p = Join-Path $Dir 'res.json'
        Write-ExpiryProbeResultAtomic -Path $p -Content 'alpha'
        $blocked = $false
        try { Write-ExpiryProbeResultAtomic -Path $p -Content 'beta' } catch { $blocked = $true }
        $content = Get-Content -LiteralPath $p -Raw
        $tmpLeft = Test-Path -LiteralPath ($p + '.tmp')
        [pscustomobject]@{ exists = (Test-Path -LiteralPath $p); secondBlocked = $blocked; unchanged = ($content.Trim() -eq 'alpha'); tmpLeft = $tmpLeft } | ConvertTo-Json -Compress
    }
}
"""


class ExpiryProbeStaticTests(unittest.TestCase):
    def setUp(self):
        self.script = SCRIPT.read_text(encoding="utf-8")
        self.lib = LIB.read_text(encoding="utf-8")

    def test_probe_and_lib_exist(self):
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(LIB.is_file())
        self.assertIn("member_expiry_capability_probe_lib.ps1", self.script)

    def test_requires_every_write_switch_before_loading_autocount(self):
        for switch in WRITE_SWITCHES:
            self.assertIn(switch, self.script)
        conj = re.search(r"\$allConfirmed\s*=\s*(.+?)\nif \(-not \$allConfirmed\)", self.script, re.S)
        self.assertIsNotNone(conj)
        for switch in WRITE_SWITCHES:
            self.assertIn(switch, conj.group(1))
        # Refusal happens before any assembly load.
        self.assertLess(self.script.index("refused"), self.script.index("LoadFrom"))

    def test_requires_approval_reference_and_state_directory(self):
        self.assertIn("$ApprovalReference", self.script)
        self.assertIn("$StateDirectory", self.script)
        self.assertRegex(self.script, r"-ApprovalReference is required")
        self.assertRegex(self.script, r"A safe absolute -StateDirectory is required")

    def test_uses_save_gated_member_command_flow(self):
        for term in ("AutoCount.BonusPoint.Member.MemberCommand", "MemberCommand.Create",
                     "GetMember", "NewMember", "SaveMember", "CreateAutoCountDefaultDBSetting",
                     "Authenticate", '"Login"'):
            self.assertIn(term, self.script)

    def test_assigns_synthetic_expiry_date_2028_06_30(self):
        self.assertIn('$script:SyntheticExpiryDate = "2028-06-30"', self.script)
        self.assertRegex(self.script, r"ExpiryDate\s*=\s*\[datetime\]::ParseExact\(\$script:SyntheticExpiryDate")
        self.assertIn("expiry_date_assigned", self.script)

    def test_reads_password_from_env_var_only(self):
        self.assertIn("AC2_PROBE_PASSWORD", self.script)
        self.assertIn("[Environment]::GetEnvironmentVariable($PasswordEnvVar)", self.script)
        self.assertNotRegex(self.script, r"(?i)\[string\]\s*\$Password\b")
        self.assertNotRegex(self.script, r"(?i)(Password\s*=|PWD\s*=|User\s+ID\s*=|Server\s*=|Database\s*=)")

    def test_no_update_delete_rollback_or_cleanup_path(self):
        self.assertNotRegex(self.script, r"(?i)\b(DeleteMember|UpdateMember|RemoveMember|DeleteMemberType|SaveMemberType)\b")
        self.assertNotRegex(self.script, r"\.\s*(Delete|Update|Rollback)\s*\(")
        self.assertNotRegex(self.script, r"(?im)^\s*function\s+[A-Za-z-]*(Cleanup|Delete|Rollback|Remove)[A-Za-z-]*")
        self.assertNotRegex(self.script, r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE\s+TABLE|ALTER|DROP|TRUNCATE)\b")

    def test_evidence_is_non_overwriting_no_set_content(self):
        # Prior evidence must never be overwritten: no Set-Content, and no Remove-Item
        # anywhere in the probe (the claim is never deleted).
        self.assertNotIn("Set-Content", self.script)
        self.assertNotIn("Remove-Item", self.script)
        self.assertIn("New-ExpiryProbeDurableArtifact", self.script)
        self.assertIn("Write-ExpiryProbeResultAtomic", self.script)

    def test_claim_created_before_save_in_source_order(self):
        claim_idx = self.script.index("New-ExpiryProbeDurableArtifact -Path $claimPath")
        save_idx = self.script.index("Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod")
        self.assertLess(claim_idx, save_idx)

    def test_readback_failure_is_isolated_from_pre_write_classification(self):
        # The read-back runs in its own try/catch that records a sanitised reason and
        # does NOT rethrow, so a confirmed save with a failed read-back is never routed
        # through the pre-write catch.
        self.assertIn("readback_error", self.script)
        self.assertRegex(self.script, r'save_outcome -eq "confirmed"')

    def test_truthful_exit_and_terminal_from_library(self):
        self.assertIn("Get-ExpiryProbeExitCode", self.script)
        self.assertIn("Get-ExpiryProbeTerminalOutcome", self.script)
        self.assertIn("exit $script:ProbeExitCode", self.script)

    def test_output_is_sanitised_and_masks_member_no(self):
        self.assertIn("Get-ExpiryProbeMaskedMemberNo", self.script)
        self.assertIn("masked_member_no", self.script)
        for repl in ("<synthetic-member-no>", "<synthetic-email>", "<synthetic-name>"):
            self.assertIn(repl, self.script)
        self.assertNotRegex(self.script, r"\$result\.[A-Za-z_]+\s*=\s*\$script:SyntheticMemberNo\b")
        self.assertNotRegex(self.script, r"\$result\.[A-Za-z_]+\s*=\s*\$script:SyntheticName\b")
        self.assertNotRegex(self.script, r"\$result\.[A-Za-z_]+\s*=\s*\$script:SyntheticEmail\b")

    def test_documents_residual_record_and_owner_approval(self):
        self.assertIn("synthetic_member_may_remain", self.script)
        self.assertIn("residual_record_note", self.script)
        self.assertRegex(self.script, r"(?i)owner approval")
        self.assertRegex(self.script, r"(?i)synthetic capability probe")
        self.assertRegex(self.script, r"(?i)form-derived member")

    # ---- Library source guarantees ---- #
    def test_lib_claim_is_exclusive_create_write_through(self):
        self.assertRegex(self.lib, r"FileMode\]::CreateNew")
        self.assertRegex(self.lib, r"FileOptions\]::WriteThrough")
        self.assertRegex(self.lib, r"Flush\(\$true\)")

    def test_lib_result_uses_temp_and_atomic_move_no_overwrite(self):
        self.assertRegex(self.lib, r"\[System\.IO\.File\]::Move")
        self.assertIn(".tmp", self.lib)
        self.assertRegex(self.lib, r"refusing to overwrite evidence")

    def test_lib_never_deletes_claim(self):
        # The only Remove-Item in the library targets the result temp file, never a claim.
        removes = re.findall(r"Remove-Item[^\n]*", self.lib)
        for r in removes:
            self.assertIn("$temp", r, r)
        self.assertNotRegex(self.lib, r"Remove-Item[^\n]*claim")

    def test_lib_flush_fallback_is_narrowed_to_unsupported_runtime(self):
        # A durable-flush failure must propagate; only NotSupportedException may downgrade.
        self.assertRegex(self.lib, r"catch \[System\.NotSupportedException\]\s*\{\s*\$stream\.Flush\(\)")
        self.assertNotRegex(self.lib, r"catch\s*\{\s*\$stream\.Flush\(\)")

    def test_lib_target_fingerprint_is_case_insensitive(self):
        self.assertIn("ToLowerInvariant()", self.lib)

    def test_evidence_persistence_failure_forces_nonzero_exit(self):
        # Verified-but-not-persisted must not exit 0: the exit code is gated on
        # evidence_persisted, and a write failure sets it false.
        self.assertIn("evidence_persisted", self.script)
        self.assertRegex(self.script, r"-EvidencePersisted\s+\$false")
        self.assertRegex(self.script, r"\$result\.evidence_persisted\s*=\s*\$false")

    def test_claim_durability_failure_fails_closed_before_save(self):
        self.assertIn("could not be durably persisted", self.script)

    def test_claim_persistence_failure_is_distinct_from_conflict(self):
        # A post-create persistence failure sets claim_persist_failed (distinct outcome),
        # NOT claim_conflict, and does not imply another save attempt.
        self.assertIn("claim_persist_failed", self.script)
        self.assertIn("post-create-persist-failed", self.script)
        self.assertIn("post-create-persist-failed", self.lib)

    def test_evidence_persisted_is_pessimistic_and_underlying_retained(self):
        self.assertRegex(self.script, r"evidence_persisted\s*=\s*\$false")
        self.assertIn("underlying_terminal_outcome", self.script)
        self.assertIn("EVIDENCE_PERSISTENCE_FAILED", self.lib)

    def test_approval_reference_alphabet_is_strict_and_rejects_target(self):
        self.assertIn("'^[A-Za-z0-9._-]{3,64}$'", self.script)
        # Case-insensitive rejection if the reference embeds the server/database name.
        self.assertRegex(self.script, r"must not contain the server or database")
        self.assertIn("ToLowerInvariant()", self.script)

    def test_sanitizer_redacts_runtime_paths(self):
        self.assertIn("Get-ExpiryProbePathRedacted", self.script)
        self.assertIn("Get-ExpiryProbePathRedacted", self.lib)
        # The state directory, AutoCount root, and JsonOut are redaction inputs.
        self.assertRegex(self.script, r"\$StateDirectory,\s*\$AcRoot,\s*\$JsonOut")

    def test_lib_flush_only_falls_back_on_unsupported_and_tags_persist_failures(self):
        self.assertRegex(self.lib, r"catch \[System\.NotSupportedException\]")
        self.assertRegex(self.lib, r"ExpiryProbePostCreatePersistTag")


@unittest.skipIf(PS is None, "no PowerShell executable available")
class ExpiryProbeLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.harness = cls.tmp / "libprobe.ps1"
        cls.harness.write_text(LIBPROBE, encoding="utf-8")

    def _lib(self, op, **kw):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(self.harness), "-Lib", str(LIB), "-Op", op]
        for k, v in kw.items():
            cmd += ["-" + k, str(v)]
        return subprocess.run(cmd, capture_output=True, text=True)

    def _terminal(self, **flags):
        base = dict(activated=True, claim_conflict=False, claim_persist_failed=False,
                    member_exists_initial=False, member_exists_recheck=False,
                    save_member_attempted=False, save_member_confirmed=False,
                    save_outcome="not_attempted", readback_found=False, expiry_match=False)
        base.update(flags)
        ctx = self.tmp / "ctx.json"
        ctx.write_text(json.dumps(base), encoding="utf-8")
        proc = self._lib("terminal", CtxJson=str(ctx))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        code, contr = proc.stdout.strip().split("|")
        return code, int(contr)

    def test_terminal_state_matrix_is_honest_and_consistent(self):
        self.assertEqual(self._terminal(activated=False), ("REFUSED", 0))
        self.assertEqual(self._terminal(claim_conflict=True), ("ATTEMPT_ALREADY_CLAIMED", 0))
        self.assertEqual(self._terminal(claim_persist_failed=True), ("CLAIM_PERSISTENCE_FAILED", 0))
        self.assertEqual(self._terminal(member_exists_initial=True), ("BLOCKED_MEMBER_EXISTS", 0))
        self.assertEqual(self._terminal(member_exists_recheck=True), ("BLOCKED_MEMBER_EXISTS", 0))
        self.assertEqual(self._terminal(), ("FAILED_BEFORE_WRITE", 0))
        self.assertEqual(self._terminal(save_member_attempted=True, save_outcome="uncertain"),
                         ("WRITE_OUTCOME_UNCERTAIN", 0))
        self.assertEqual(self._terminal(save_member_attempted=True, save_member_confirmed=True,
                                        save_outcome="confirmed", readback_found=False),
                         ("WRITE_CONFIRMED_READBACK_FAILED", 0))
        self.assertEqual(self._terminal(save_member_attempted=True, save_member_confirmed=True,
                                        save_outcome="confirmed", readback_found=True, expiry_match=False),
                         ("EXPIRY_READBACK_MISMATCH", 0))
        self.assertEqual(self._terminal(save_member_attempted=True, save_member_confirmed=True,
                                        save_outcome="confirmed", readback_found=True, expiry_match=True),
                         ("EXPIRY_VERIFIED", 0))

    def test_post_save_readback_failure_is_never_failed_before_write(self):
        # P1: a confirmed save whose read-back throws (readback_found False) must be a
        # distinct post-save state, never FAILED_BEFORE_WRITE.
        code, contr = self._terminal(save_member_attempted=True, save_member_confirmed=True,
                                     save_outcome="confirmed", readback_found=False)
        self.assertEqual(code, "WRITE_CONFIRMED_READBACK_FAILED")
        self.assertNotEqual(code, "FAILED_BEFORE_WRITE")
        self.assertEqual(contr, 0)

    def test_impossible_flag_combinations_are_flagged(self):
        _, c1 = self._terminal(save_member_attempted=True, save_member_confirmed=False, save_outcome="confirmed")
        self.assertGreater(c1, 0)
        _, c2 = self._terminal(save_member_attempted=True, save_outcome="uncertain", readback_found=True)
        self.assertGreater(c2, 0)
        _, c3 = self._terminal(activated=False, save_member_attempted=True)
        self.assertGreater(c3, 0)

    def test_exit_codes_only_verified_is_zero(self):
        for code in ("REFUSED", "ATTEMPT_ALREADY_CLAIMED", "CLAIM_PERSISTENCE_FAILED",
                     "BLOCKED_MEMBER_EXISTS", "FAILED_BEFORE_WRITE", "WRITE_OUTCOME_UNCERTAIN",
                     "WRITE_CONFIRMED_READBACK_FAILED", "EXPIRY_READBACK_MISMATCH",
                     "EVIDENCE_PERSISTENCE_FAILED"):
            proc = self._lib("exit", Text=code)
            self.assertEqual(proc.stdout.strip(), "1", code)
        proc = self._lib("exit", Text="EXPIRY_VERIFIED")
        self.assertEqual(proc.stdout.strip(), "0")

    def _final(self, underlying, durable, persisted):
        proc = self._lib("final", Text=f"{underlying}|{1 if durable else 0}|{1 if persisted else 0}")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outcome, code = proc.stdout.strip().split("|")
        return outcome, int(code)

    def test_evidence_override_gates_success(self):
        # Only a durably persisted verified run exits 0.
        self.assertEqual(self._final("EXPIRY_VERIFIED", True, True), ("EXPIRY_VERIFIED", 0))
        self.assertEqual(self._final("EXPIRY_VERIFIED", True, False), ("EVIDENCE_PERSISTENCE_FAILED", 1))
        # A non-verified underlying is preserved when persisted, and never becomes success.
        self.assertEqual(self._final("FAILED_BEFORE_WRITE", True, True), ("FAILED_BEFORE_WRITE", 1))
        self.assertEqual(self._final("FAILED_BEFORE_WRITE", True, False), ("EVIDENCE_PERSISTENCE_FAILED", 1))
        # Not-durable-required leaves the underlying unchanged.
        self.assertEqual(self._final("REFUSED", False, True), ("REFUSED", 1))

    def test_fingerprints_deterministic_shaped_case_and_whitespace_insensitive(self):
        proc = self._lib("fp")
        info = json.loads(proc.stdout)
        self.assertTrue(info["targetStable"])
        self.assertTrue(info["targetDiffers"])
        self.assertTrue(info["targetShape"])
        self.assertTrue(info["attemptShape"])
        self.assertTrue(info["synthShape"])
        self.assertTrue(info["caseWsInsensitiveTarget"], "casing/whitespace variants must collapse to one target fingerprint")
        self.assertTrue(info["caseWsInsensitiveAttempt"], "attempt claim key must be case/whitespace insensitive on the target")

    def test_path_redaction_masks_private_paths(self):
        proc = self._lib("redact", Text=r"failed at C:\Users\alice\XB\state\r.json and \\HOST\share\x tail")
        out = proc.stdout.strip()
        self.assertIn("<path>", out)
        self.assertNotIn("alice", out)
        self.assertNotIn("HOST", out)
        self.assertIn("tail", out)

    def test_durable_flush_failure_propagates_no_usable_artifact(self):
        d = self.tmp / "badclaimdir"
        d.mkdir(exist_ok=True)
        proc = self._lib("badclaim", Dir=str(d))
        info = json.loads(proc.stdout)
        self.assertTrue(info["threw"], "a durable persistence failure must propagate")
        self.assertFalse(info["exists"], "no usable artefact may remain on failure")

    def test_claim_is_exclusive_create_and_never_deleted(self):
        d = self.tmp / "claimdir"
        d.mkdir(exist_ok=True)
        proc = self._lib("claim", Dir=str(d))
        info = json.loads(proc.stdout)
        self.assertTrue(info["exists"])
        self.assertTrue(info["secondBlocked"], "a second claim on the same path must fail closed (one winner)")
        self.assertTrue(info["unchanged"], "the original claim content must survive a second attempt")

    def test_result_atomic_write_never_overwrites(self):
        d = self.tmp / "resdir"
        d.mkdir(exist_ok=True)
        proc = self._lib("result", Dir=str(d))
        info = json.loads(proc.stdout)
        self.assertTrue(info["exists"])
        self.assertTrue(info["secondBlocked"], "a second result write must not overwrite prior evidence")
        self.assertTrue(info["unchanged"])
        self.assertFalse(info["tmpLeft"], "the temp file must be moved (atomic), leaving no .tmp")


@unittest.skipIf(PS is None, "no PowerShell executable available")
class ExpiryProbeScriptExecutionTests(unittest.TestCase):
    """Runs the probe so it always stops before any AutoCount assembly load."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = dict(os.environ)
        self.env["AC2_PROBE_PASSWORD"] = ""  # force a pre-AutoCount stop for active runs

    def _run(self, *args):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(SCRIPT), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=self.env)

    def _active_args(self, appref="APPROVAL-TEST-001"):
        return ("-EnableExpiryCapabilityProbe", "-ConfirmSyntheticExpiryDateTest",
                "-ConfirmSingleSyntheticMember", "-ConfirmAutoCountWrite",
                "-ConfirmDryRunPreflightPassed", "-ConfirmNoUpdateOrDelete",
                "-ApprovalReference", appref, "-StateDirectory", str(self.tmp),
                "-ServerName", "SYN_SERVER", "-DatabaseName", "SYN_DB", "-UserId", "SYN_USER",
                "-AcRoot", "C:\\NoSuchAcRoot")

    def test_approval_reference_containing_target_fails_closed(self):
        proc = self._run(*self._active_args(appref="XB-SYN_DB-01"))  # embeds the database name
        self.assertNotEqual(proc.returncode, 0)
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "FAILED_BEFORE_WRITE")
        self.assertFalse(r["required_assemblies_loaded"])
        self.assertFalse(r["claim_created"])

    def test_approval_reference_bad_characters_fail_closed(self):
        proc = self._run(*self._active_args(appref="XB:BAD/REF"))  # forbidden characters
        self.assertNotEqual(proc.returncode, 0)
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "FAILED_BEFORE_WRITE")
        self.assertFalse(r["claim_created"])
        self.assertFalse(r["required_assemblies_loaded"])

    def test_underlying_outcome_retained_and_evidence_pessimistic(self):
        # Refusal is not activated, so durable evidence is not required and pessimistic
        # evidence_persisted stays false (no artifact was written).
        r = json.loads(self._run().stdout)
        self.assertEqual(r["terminal_outcome"], "REFUSED")
        self.assertFalse(r["evidence_persisted"])
        # An active pre-AutoCount failure retains the underlying outcome and, since the
        # state directory is writable, persists the failure evidence durably.
        r2 = json.loads(self._run(*self._active_args()).stdout)
        self.assertEqual(r2["underlying_terminal_outcome"], "FAILED_BEFORE_WRITE")
        self.assertEqual(r2["terminal_outcome"], "FAILED_BEFORE_WRITE")
        self.assertTrue(r2["evidence_persisted"])

    def test_refusal_is_nonzero_with_clean_json_stdout(self):
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(proc.stdout.lstrip().startswith("{"), proc.stdout[:120])
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "REFUSED")
        self.assertFalse(r["activated"])
        self.assertEqual(r["exit_code"], proc.returncode)

    def test_active_pre_autocount_failure_binds_evidence_and_creates_no_claim(self):
        proc = self._run(*self._active_args())
        self.assertNotEqual(proc.returncode, 0)
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "FAILED_BEFORE_WRITE")
        self.assertFalse(r["required_assemblies_loaded"], "no AutoCount assembly may load")
        self.assertFalse(r["claim_created"])
        # Evidence binding present; raw target/credentials/synthetic identity absent.
        for field in ("operation_id", "approval_reference", "executed_at_utc", "target_fingerprint"):
            self.assertTrue(r[field], field)
        self.assertNotIn("SYN_SERVER", proc.stdout)
        self.assertNotIn("SYN_DB", proc.stdout)
        self.assertNotIn("xb.expirydate.probe", proc.stdout)
        self.assertNotIn("XB EXPIRYDATE PROBE", proc.stdout)
        # No claim file was created before the (unreached) save.
        claim = self.tmp / r["claim_basename"]
        self.assertFalse(claim.exists())
        # A durable, unique result file was written.
        self.assertTrue((self.tmp / r["result_basename"]).is_file())

    def test_preexisting_claim_prevents_autocount_and_is_permanently_non_retryable(self):
        # Discover the deterministic claim basename from a first (no-claim) run.
        first = json.loads(self._run(*self._active_args()).stdout)
        claim = self.tmp / first["claim_basename"]
        claim.write_text('{"seeded": true}', encoding="utf-8")
        # Two further runs both fail closed before AutoCount and never delete the claim.
        for _ in range(2):
            proc = self._run(*self._active_args())
            self.assertNotEqual(proc.returncode, 0)
            r = json.loads(proc.stdout)
            self.assertEqual(r["terminal_outcome"], "ATTEMPT_ALREADY_CLAIMED")
            self.assertTrue(r["claim_conflict"])
            self.assertFalse(r["required_assemblies_loaded"])
            self.assertFalse(r["authentication_success"])
            self.assertTrue(claim.exists(), "the permanent claim must never be deleted")


class ExpiryProbeRunbookAndCiTests(unittest.TestCase):
    def setUp(self):
        self.runbook = RUNBOOK.read_text(encoding="utf-8")
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_runbook_exists_and_separates_eight_stages(self):
        self.assertTrue(RUNBOOK.is_file())
        for n in range(1, 9):
            self.assertRegex(self.runbook, rf"(?m)^### {n}\. ")

    def test_runbook_states_required_safety_facts(self):
        self.assertRegex(self.runbook, r"(?i)current-turn owner approval is required before the synthetic")
        self.assertIn("the AutoCount target", self.runbook)
        self.assertIn("exactly one synthetic record", self.runbook)
        self.assertRegex(self.runbook, r"(?i)uncertain save outcome is terminal and must never be retried automatically")
        self.assertRegex(self.runbook, r"(?i)No real,\s+form-derived member is ever used")
        self.assertRegex(self.runbook, r"(?i)not the permanent production member-intake workflow")

    def test_runbook_documents_new_controls(self):
        self.assertIn("-ApprovalReference", self.runbook)
        self.assertIn("-StateDirectory", self.runbook)
        self.assertIn("WRITE_CONFIRMED_READBACK_FAILED", self.runbook)
        self.assertIn("ATTEMPT_ALREADY_CLAIMED", self.runbook)
        self.assertRegex(self.runbook, r"(?i)permanent single-use\s+attempt claim")
        self.assertRegex(self.runbook, r"(?i)exit code")
        self.assertRegex(self.runbook, r"(?i)never removes the attempt claim")
        # New terminal outcomes are documented.
        self.assertIn("CLAIM_PERSISTENCE_FAILED", self.runbook)
        self.assertIn("EVIDENCE_PERSISTENCE_FAILED", self.runbook)

    def test_runbook_has_distinct_bounded_deployment_stage(self):
        # A distinct deployment stage precedes preflight/approval, covers both files via
        # the private bridge, requires bidirectional SHA-256 equality and a bounded
        # backup, and the later command uses the verified VM path (not repo-relative).
        self.assertRegex(self.runbook, r"(?m)^### 3\. Deploy the reviewed probe files")
        self.assertIn("scripts/member_expiry_capability_probe_lib.ps1", self.runbook)
        self.assertIn("Get-FileHash", self.runbook)
        self.assertRegex(self.runbook, r"(?i)hyper-v\s*/\s*smb|shared-folder bridge")
        self.assertRegex(self.runbook, r"(?i)bounded[^.]*backup")
        self.assertRegex(self.runbook, r"(?i)exact-version verification|exact equality|exactly equal")
        self.assertIn(r"C:\XB\create_uat\probe\ac2_member_expiry_capability_probe.ps1", self.runbook)
        deploy_idx = self.runbook.index("### 3. Deploy the reviewed probe files")
        approval_idx = self.runbook.index("### 5.")
        self.assertLess(deploy_idx, approval_idx, "deployment must precede the approval stage")

    def test_runbook_followup_requires_runner_wiring_before_flip(self):
        # The follow-up must wire the runner assignment/read-back, not just flip the flag.
        self.assertRegex(self.runbook, r"(?i)not sufficient and is unsafe")
        self.assertRegex(self.runbook, r"(?i)could save a member with")
        self.assertIn("CREATED_VERIFIED", self.runbook)
        self.assertIn("Wire the runner", self.runbook)
        self.assertRegex(self.runbook, r"(?i)read-back")
        self.assertIn("member_create_uat_approval.py", self.runbook)

    def test_runbook_requires_opaque_approval_reference(self):
        # The approval reference must be opaque; the placeholder must not invite target names.
        self.assertIn("<opaque-approval-id>", self.runbook)
        self.assertNotIn("approval-ref-naming-target", self.runbook)
        self.assertRegex(self.runbook, r"(?i)opaque, non-secret approval identifier")
        self.assertRegex(self.runbook, r"(?i)put the server or database")

    def test_readme_references_probe_and_runbook(self):
        self.assertIn("scripts/ac2_member_expiry_capability_probe.ps1", self.readme)
        self.assertIn("member_expiry_capability_probe_runbook.md", self.readme)

    def test_workflow_triggers_on_validated_documents(self):
        # The focused test module reads these documents; a docs-only PR must trigger it.
        paths_block = self.workflow.split("paths:", 1)[1].split("workflow_dispatch", 1)[0]
        for needed in ("README.md",
                       "docs/autocount2-automation/member_expiry_capability_probe_runbook.md",
                       "docs/autocount2-automation/member_create_uat_runbook.md",
                       "scripts/member_expiry_capability_probe_lib.ps1",
                       "tests/test_ac2_member_expiry_capability_probe.py"):
            self.assertIn(needed, paths_block, needed)


@unittest.skipIf(PS is None, "no PowerShell executable available")
class ExpiryProbeAstTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.inspector = cls.tmp / "expiry_inspector.ps1"
        cls.inspector.write_text(INSPECTOR, encoding="utf-8")

    def _inspect(self):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(self.inspector), "-Path", str(SCRIPT)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_single_gated_save_never_in_a_loop(self):
        info = self._inspect()
        self.assertEqual(info["parseErrors"], 0)
        self.assertTrue(info["funcExists"])
        self.assertEqual(info["saveInvokeCount"], 1)
        self.assertTrue(info["saveInsideNarrow"])
        self.assertEqual(info["callCount"], 1)
        self.assertFalse(info["callInLoop"])

    def test_duplicate_check_before_save_and_no_forbidden_mutations(self):
        info = self._inspect()
        self.assertGreaterEqual(info["getMemberInvokeCount"], 1)
        self.assertTrue(info["dupCheckBeforeSave"])
        self.assertEqual(info["forbiddenMutationCount"], 0)


if __name__ == "__main__":
    unittest.main()
