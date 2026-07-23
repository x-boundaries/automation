"""Static and AST-based tests for the synthetic ExpiryDate capability probe.

These tests never execute the probe's AutoCount path and never contact AutoCount,
n8n, Google Sheets, or the physical host / VM. They assert, per the approved design:

* the probe is inactive by default and requires every explicit write switch;
* the single SaveMember call site is behind one narrowly scoped function, called at
  most once, and is never inside a retry loop (PowerShell AST, not string matching);
* a GetMember duplicate check occurs before that SaveMember call (AST ordering);
* there is no update, delete, rollback, or cleanup path;
* an uncertain save is terminal and never replayed;
* the emitted output cannot contain the raw synthetic member number, name, or email.

The AST assertions run real PowerShell (pwsh or powershell.exe) when available and
skip cleanly when it is not, matching tests/test_member_create_uat_runner_ps.py.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_member_expiry_capability_probe.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"
RUNBOOK = DOCS / "member_expiry_capability_probe_runbook.md"

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


class ExpiryProbeStaticTests(unittest.TestCase):
    def setUp(self):
        self.script = SCRIPT.read_text(encoding="utf-8")

    def test_probe_exists(self):
        self.assertTrue(SCRIPT.is_file())

    # ---- C8: requires every explicit write switch; inactive by default ---- #
    def test_requires_every_write_switch_before_loading_autocount(self):
        for switch in WRITE_SWITCHES:
            self.assertIn(switch, self.script)
        # The enable/confirm conjunction gates the whole run.
        conj = re.search(r"\$allConfirmed\s*=\s*(.+?)\n\nif \(-not \$allConfirmed\)", self.script, re.S)
        self.assertIsNotNone(conj, "the all-confirmed conjunction must gate the run")
        for switch in WRITE_SWITCHES:
            self.assertIn(switch, conj.group(1))
        # Refusal happens before any assembly is loaded.
        refusal_index = self.script.index("Refusing to run")
        first_load_index = self.script.index("LoadFrom")
        self.assertLess(refusal_index, first_load_index)

    # ---- Save-gated MemberCommand flow present ---- #
    def test_uses_save_gated_member_command_flow(self):
        for term in (
            "AutoCount.BonusPoint.Member.MemberCommand",
            "MemberCommand.Create",
            "GetMember",
            "NewMember",
            "SaveMember",
            "CreateAutoCountDefaultDBSetting",
            "Authenticate",
            '"Login"',
        ):
            self.assertIn(term, self.script)

    # ---- B8 / A3: synthetic ExpiryDate 2028-06-30 is assigned ---- #
    def test_assigns_synthetic_expiry_date_2028_06_30(self):
        self.assertIn('$script:SyntheticExpiryDate = "2028-06-30"', self.script)
        self.assertRegex(self.script, r"ExpiryDate\s*=\s*\[datetime\]::ParseExact\(\$script:SyntheticExpiryDate")
        self.assertIn("expiry_date_readback_match", self.script)
        self.assertIn("expiry_date_assigned", self.script)

    # ---- B4: authenticate from AC2_PROBE_* env only, never printing values ---- #
    def test_reads_password_from_env_var_only(self):
        self.assertIn("AC2_PROBE_PASSWORD", self.script)
        self.assertIn("PasswordEnvVar", self.script)
        self.assertIn("[Environment]::GetEnvironmentVariable($PasswordEnvVar)", self.script)
        self.assertNotRegex(self.script, r"(?i)\[string\]\s*\$Password\b")
        self.assertNotRegex(self.script, r"(?i)(Password\s*=|PWD\s*=|User\s+ID\s*=|Server\s*=|Database\s*=)")

    # ---- C11: no update / delete / rollback / cleanup CODE path ---- #
    def test_no_update_delete_rollback_or_cleanup_path(self):
        # No member-mutating method calls other than the single SaveMember.
        self.assertNotRegex(self.script, r"(?i)\b(DeleteMember|UpdateMember|RemoveMember|DeleteMemberType|SaveMemberType)\b")
        self.assertNotRegex(self.script, r"\.\s*(Delete|Update|Rollback)\s*\(")
        # No cleanup/delete/rollback helper functions.
        self.assertNotRegex(self.script, r"(?im)^\s*function\s+[A-Za-z-]*(Cleanup|Delete|Rollback|Remove)[A-Za-z-]*")
        # No SQL surface.
        self.assertNotRegex(self.script, r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE\s+TABLE|ALTER|DROP|TRUNCATE)\b")

    # ---- C13: uncertain save is terminal and not replayed ---- #
    def test_uncertain_save_is_terminal_and_not_retried(self):
        self.assertIn('$result.save_outcome = "uncertain"', self.script)
        self.assertIn('SAVE_UNCERTAIN', self.script)
        # Exactly one invocation of the narrow save function anywhere in the script.
        self.assertEqual(self.script.count("Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod"), 1)

    # ---- B16 / B17: residual record + owner approval documented ---- #
    def test_documents_residual_record_and_owner_approval(self):
        self.assertIn("synthetic_member_may_remain", self.script)
        self.assertIn("residual_record_note", self.script)
        self.assertRegex(self.script, r"(?i)owner approval")
        self.assertRegex(self.script, r"(?i)synthetic capability probe")
        self.assertRegex(self.script, r"(?i)form-derived member")

    # ---- C14: output masks/sanitises identity; no raw member no / name / email ---- #
    def test_output_is_sanitised_and_masks_member_no(self):
        self.assertIn("Get-MaskedMemberNo", self.script)
        self.assertIn("masked_member_no", self.script)
        # The sanitiser redacts the synthetic identity from any message.
        for repl in ("<synthetic-member-no>", "<synthetic-email>", "<synthetic-name>"):
            self.assertIn(repl, self.script)
        # No result field exposes the raw synthetic member number, name, or email.
        self.assertNotRegex(self.script, r"\$result\.[A-Za-z_]+\s*=\s*\$script:SyntheticMemberNo\b")
        self.assertNotRegex(self.script, r"\$result\.[A-Za-z_]+\s*=\s*\$script:SyntheticName\b")
        self.assertNotRegex(self.script, r"\$result\.[A-Za-z_]+\s*=\s*\$script:SyntheticEmail\b")
        # The JSON written to disk is the sanitised aggregate, not raw values.
        self.assertIn("Set-Content -LiteralPath $jsonOutPath -Value $safeJson", self.script)


class ExpiryProbeRunbookTests(unittest.TestCase):
    """D. The runbook separates the stages and states the required safety facts."""

    def setUp(self):
        self.runbook = RUNBOOK.read_text(encoding="utf-8")
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_runbook_exists_and_separates_seven_stages(self):
        self.assertTrue(RUNBOOK.is_file())
        for n in range(1, 8):
            self.assertRegex(self.runbook, rf"(?m)^### {n}\. ")
        for phrase in (
            "Laptop development",
            "Host pull",
            "dry-run",
            "owner approval",
            "ExpiryDate persistence test",
            "Read-back evidence",
            "Follow-up PR",
        ):
            self.assertIn(phrase, self.runbook)

    def test_runbook_states_required_safety_facts(self):
        self.assertRegex(self.runbook, r"(?i)current-turn owner approval is required before the synthetic")
        self.assertIn("the AutoCount target", self.runbook)
        self.assertIn("exactly one synthetic record", self.runbook)
        self.assertRegex(self.runbook, r"(?i)uncertain save outcome is terminal and must never be retried automatically")
        self.assertRegex(self.runbook, r"(?i)No real,\s+form-derived member is ever used")
        self.assertRegex(self.runbook, r"(?i)not the permanent production member-intake workflow")

    def test_readme_references_probe_and_runbook(self):
        self.assertIn("scripts/ac2_member_expiry_capability_probe.ps1", self.readme)
        self.assertIn("member_expiry_capability_probe_runbook.md", self.readme)


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
        self.assertTrue(info["funcExists"], "Invoke-ExpiryProbeSaveMemberOnce must exist")
        self.assertEqual(info["saveInvokeCount"], 1, "exactly one SaveMember .Invoke call site")
        self.assertTrue(info["saveInsideNarrow"], "the SaveMember call must be inside the narrow function")
        self.assertEqual(info["callCount"], 1, "the narrow save function is called exactly once")
        self.assertFalse(info["callInLoop"], "no retry loop may enclose the SaveMember call")

    def test_duplicate_check_before_save_and_no_forbidden_mutations(self):
        info = self._inspect()
        self.assertGreaterEqual(info["getMemberInvokeCount"], 1, "a GetMember duplicate check is required")
        self.assertTrue(info["dupCheckBeforeSave"], "a GetMember check must precede the SaveMember call")
        self.assertEqual(info["forbiddenMutationCount"], 0, "no delete/update/rollback member calls")


if __name__ == "__main__":
    unittest.main()
