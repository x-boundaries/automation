"""Static, AST, library, and subprocess tests for the synthetic ExpiryDate probe.

No test executes the probe's AutoCount path or contacts AutoCount, SQL Server, n8n, Google
Sheets, or the host/VM. Subprocess runs of the probe are driven so they stop before any
AutoCount assembly load (refusal, an approval-reference rejection, or an untrusted canonical
claim root). The pure state/claim/publication/fingerprint helpers are exercised directly
against scripts/member_expiry_capability_probe_lib.ps1 without AutoCount.

Two deliberate boundaries:

* The probe's claim/result root is a FIXED code constant, so it is not redirectable from a
  test. Every behaviour that needs a *trusted* root (claim races, publication, authoritative
  validation) is therefore tested at pure-library level with an injected temporary root, and
  the script-level tests never run an active probe that could create an artefact inside the
  real canonical root. The few script tests that would otherwise do so are skipped, with a
  visible reason, on a machine where that root exists (i.e. the AutoCount VM).
* Repository path dependencies are declared as module-level ``ROOT / ...`` constants. The
  dependency-closure test discovers them mechanically from this module's own AST, so a new
  direct dependency fails closure until the focused workflow filter covers it.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# ---- Declared repository path dependencies (discovered mechanically; see closure test). ---- #
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_member_expiry_capability_probe.ps1"
LIB = ROOT / "scripts" / "member_expiry_capability_probe_lib.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"
RUNBOOK = DOCS / "member_expiry_capability_probe_runbook.md"
CREATE_UAT_RUNBOOK = DOCS / "member_create_uat_runbook.md"
README = ROOT / "README.md"
GITIGNORE = ROOT / ".gitignore"
WORKFLOW = ROOT / ".github" / "workflows" / "member-create-uat-tests.yml"
SELF = ROOT / "tests" / "test_ac2_member_expiry_capability_probe.py"

WRITE_SWITCHES = (
    "EnableExpiryCapabilityProbe",
    "ConfirmSyntheticExpiryDateTest",
    "ConfirmSingleSyntheticMember",
    "ConfirmAutoCountWrite",
    "ConfirmDryRunPreflightPassed",
    "ConfirmNoUpdateOrDelete",
)

# The one canonical claim/result root, duplicated here on purpose: the test asserts the
# reviewed code constant is exactly this value, so the literal must not be imported from it.
CANONICAL_STATE_ROOT = r"C:\XB\create_uat\expiry_probe_state"
SCHEMA_VERSION = "member_expiry_capability_probe/v1"
PUBLICATION_CONTRACT_VERSION = "member_expiry_capability_probe_publication/v1"

# An active probe run with a valid approval reference reaches the canonical root. On a machine
# where that root exists (the AutoCount VM) such a run would create a real artefact inside the
# operator's evidence root, so those script-level tests are skipped there. The equivalent
# behaviour is covered unconditionally at pure-library level with an injected root.
CANONICAL_ROOT_PRESENT = os.path.isdir(CANONICAL_STATE_ROOT)
CANONICAL_ROOT_SKIP = (
    "the canonical probe state root exists on this machine; refusing to run an active probe "
    "that would create an artefact inside the operator's evidence root (library-level tests "
    "cover the trusted-root behaviour)"
)

TERMINAL_CODES = (
    "REFUSED",
    "CLAIM_ROOT_UNAVAILABLE",
    "ATTEMPT_ALREADY_CLAIMED",
    "ATTEMPT_CLAIM_LOST_AFTER_CONTACT",
    "CLAIM_PERSISTENCE_FAILED",
    "BLOCKED_MEMBER_EXISTS",
    "FAILED_BEFORE_WRITE",
    "WRITE_OUTCOME_UNCERTAIN",
    "WRITE_CONFIRMED_READBACK_FAILED",
    "EXPIRY_READBACK_MISMATCH",
    "EXPIRY_VERIFIED",
    "EVIDENCE_PERSISTENCE_FAILED",
)


def find_powershell():
    for exe in ("pwsh", "powershell", "powershell.exe"):
        found = shutil.which(exe)
        if found:
            return found
    return None


PS = find_powershell()
IS_WINDOWS = sys.platform.startswith("win")


def as_list(value):
    """Normalise a PowerShell-serialised array that may collapse to a scalar or null."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def verified_record(operation_id="expop_authoritative01"):
    """A minimal, internally consistent authoritative EXPIRY_VERIFIED record."""
    final_basename = "expiry_probe_result_%s.json" % operation_id
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": operation_id,
        "claim_basename": "expiry_probe_claim_afp_test.claim",
        "result_basename": final_basename,
        "staging_basename": "expiry_probe_staging_%s.incomplete" % operation_id,
        "publication_contract": {
            "publication_contract_version": PUBLICATION_CONTRACT_VERSION,
            "authoritative_result_basename": final_basename,
            "authority_rule": "authoritative only when the current basename equals authoritative_result_basename",
        },
        "state_root_trusted": True,
        "claim_root_unavailable": False,
        "activated": True,
        "autocount_contacted": True,
        "authentication_success": True,
        "initial_member_read_attempted": True,
        "member_exists_initial": False,
        "member_recheck_attempted": True,
        "member_exists_recheck": False,
        "claim_created": True,
        "claim_conflict": False,
        "claim_lost_after_contact": False,
        "claim_persist_failed": False,
        "save_member_attempted": True,
        "save_member_confirmed": True,
        "save_outcome": "confirmed",
        "readback_found": True,
        "expiry_match": True,
        "underlying_terminal_outcome": "EXPIRY_VERIFIED",
        "terminal_outcome": "EXPIRY_VERIFIED",
        "evidence_persisted": True,
        "exit_code": 0,
    }


INSPECTOR = r"""
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Path)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errs)
$paramNames = @()
if ($null -ne $ast.ParamBlock) {
    $paramNames = @($ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
}
$funcs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
$narrow = $funcs | Where-Object { $_.Name -eq 'Invoke-ExpiryProbeSaveMemberOnce' } | Select-Object -First 1
$calls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) |
    Where-Object { $_.GetCommandName() -eq 'Invoke-ExpiryProbeSaveMemberOnce' })
$removeCalls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) |
    Where-Object { @('Remove-Item', 'ri', 'del', 'erase', 'rd') -contains $_.GetCommandName() })
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
$fileApiInvokes = @($invokes | Where-Object {
    $_.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
    $_.Expression.Extent.Text -match '^\[(System\.)?IO\.File\]$|^\[System\.IO\.File\]$' })
$fileMoveInvokes = @($fileApiInvokes | Where-Object { $_.Member.Value -eq 'Move' })
$fileDeleteInvokes = @($fileApiInvokes | Where-Object { @('Delete','Replace') -contains $_.Member.Value })
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
    paramNames             = $paramNames
    funcExists             = [bool]$narrow
    callCount              = $calls.Count
    saveInvokeCount        = $saveInvokes.Count
    saveInsideNarrow       = $saveInsideNarrow
    callInLoop             = $callInLoop
    getMemberInvokeCount   = $getInvokes.Count
    dupCheckBeforeSave     = $dupBeforeSave
    forbiddenMutationCount = $forbidden.Count
    removeItemCount        = $removeCalls.Count
    fileMoveCount          = $fileMoveInvokes.Count
    fileDeleteCount        = $fileDeleteInvokes.Count
} | ConvertTo-Json -Compress
"""

LIBPROBE = r"""
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Lib, [Parameter(Mandatory)][string]$Op,
      [string]$CtxJson, [string]$Text, [string]$Dir, [string]$Extra)
$ErrorActionPreference = 'Stop'
. $Lib
switch ($Op) {
    'terminal' {
        $ctx = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $code = Get-ExpiryProbeTerminalOutcome -Flags $ctx
        $contr = @(Get-ExpiryProbeStateContradictions -Flags $ctx).Count
        Write-Output ($code + '|' + $contr)
    }
    'contradictions' {
        $ctx = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Output (@(Get-ExpiryProbeStateContradictions -Flags $ctx) -join ',')
    }
    'exit' { Write-Output ([string](Get-ExpiryProbeExitCode -TerminalOutcome $Text)) }
    'final' {
        # $Text = "underlying|durableRequired(0/1)|evidencePersisted(0/1)"
        $parts = $Text.Split('|')
        $o = Get-ExpiryProbeFinalOutcome -UnderlyingOutcome $parts[0] -DurableRequired ([bool][int]$parts[1]) -EvidencePersisted ([bool][int]$parts[2])
        Write-Output ($o + '|' + (Get-ExpiryProbeExitCode -TerminalOutcome $o))
    }
    'redact' { Write-Output (Get-ExpiryProbePathRedacted -Text $Text) }
    'vocabulary' { Write-Output (($script:ExpiryProbeTerminalCodes) -join ',') }
    'canonroot' {
        # The production accessor is nullary: it takes no parameters at all, so nothing can
        # redirect the claim/result root. Report the value and the parameter count.
        $cmd = Get-Command Get-ExpiryProbeCanonicalStateRoot
        $declared = @($cmd.Parameters.Keys | Where-Object { @('Verbose','Debug','ErrorAction','WarningAction','InformationAction','ProgressAction','ErrorVariable','WarningVariable','InformationVariable','OutVariable','OutBuffer','PipelineVariable') -notcontains $_ })
        [pscustomobject]@{
            root = (Get-ExpiryProbeCanonicalStateRoot)
            declaredParameterCount = $declared.Count
        } | ConvertTo-Json -Compress
    }
    'trust' {
        # $Dir = root under test; $Text = '1' to require Windows.
        if ($Text -eq '1') { $v = Test-ExpiryProbeTrustedStateRoot -Root $Dir -RequireWindows }
        else { $v = Test-ExpiryProbeTrustedStateRoot -Root $Dir }
        $v | ConvertTo-Json -Compress -Depth 3
    }
    'paths' {
        # $Dir = injected root; $Text = "<attemptFingerprint>|<operationId>"
        $parts = $Text.Split('|')
        $p = Get-ExpiryProbeStatePaths -Root $Dir -AttemptFingerprint $parts[0] -OperationId $parts[1]
        [pscustomobject]@{
            claim_basename    = $p.claim_basename
            result_basename   = $p.result_basename
            staging_basename  = $p.staging_basename
            claim_path        = $p.claim_path
            result_path       = $p.result_path
            staging_path      = $p.staging_path
            pure_claim        = (Get-ExpiryProbeClaimBasename -AttemptFingerprint $parts[0])
            pure_result       = (Get-ExpiryProbeResultBasename -OperationId $parts[1])
            pure_staging      = (Get-ExpiryProbeStagingBasename -OperationId $parts[1])
            cwd               = (Get-Location).Path
        } | ConvertTo-Json -Compress
    }
    'contract' {
        (New-ExpiryProbePublicationContract -OperationId $Text) | ConvertTo-Json -Compress
    }
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
    'claimrace' {
        # One contender. $Text is the shared canonical claim path; exactly one concurrent
        # process can exclusively create it.
        $created = $false
        try { New-ExpiryProbeDurableArtifact -Path $Text -Content 'contender'; $created = $true } catch { $created = $false }
        [pscustomobject]@{ created = $created } | ConvertTo-Json -Compress
    }
    'publish' {
        # $Dir = injected root; $Text = operation id; $CtxJson = staged record bytes.
        $stg = Join-Path $Dir (Get-ExpiryProbeStagingBasename -OperationId $Text)
        $fin = Join-Path $Dir (Get-ExpiryProbeResultBasename -OperationId $Text)
        $content = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content $content
        $secondBlocked = $false
        try { Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content 'beta' } catch { $secondBlocked = $true }
        $published = Get-Content -LiteralPath $fin -Raw -Encoding UTF8 | ConvertFrom-Json
        [pscustomobject]@{
            finalExists = (Test-Path -LiteralPath $fin)
            stagingLeft = (Test-Path -LiteralPath $stg)
            secondBlocked = $secondBlocked
            finalOperationId = $published.operation_id
            finalTerminalOutcome = $published.terminal_outcome
        } | ConvertTo-Json -Compress
    }
    'stagingconflict' {
        # A pre-existing staging artefact is never overwritten, truncated or deleted.
        $stg = Join-Path $Dir (Get-ExpiryProbeStagingBasename -OperationId $Text)
        $fin = Join-Path $Dir (Get-ExpiryProbeResultBasename -OperationId $Text)
        New-ExpiryProbeDurableArtifact -Path $stg -Content 'pre-existing staging bytes'
        $blocked = $false
        try { Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content 'new bytes' } catch { $blocked = $true }
        [pscustomobject]@{
            blocked = $blocked
            stagingUnchanged = ((Get-Content -LiteralPath $stg -Raw).Trim() -eq 'pre-existing staging bytes')
            finalExists = (Test-Path -LiteralPath $fin)
        } | ConvertTo-Json -Compress
    }
    'movefail' {
        # Pure dependency injection: simulate a failing final move (e.g. rename permission
        # denied) with no executable-script parameter and no live bypass.
        $stg = Join-Path $Dir (Get-ExpiryProbeStagingBasename -OperationId $Text)
        $fin = Join-Path $Dir (Get-ExpiryProbeResultBasename -OperationId $Text)
        $content = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        $threw = $false
        try {
            Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content $content `
                -MoveAction { param($s, $d) throw "simulated rename permission denied" }
        }
        catch { $threw = $true }
        # Read the staged bytes back and report only scalars, so the harness never nests one
        # JSON document inside another.
        $stagedBound = ''
        $stagedTerminal = ''
        $stagedExit = -1
        $stagedPersisted = $null
        if (Test-Path -LiteralPath $stg) {
            $staged = Get-Content -LiteralPath $stg -Raw -Encoding UTF8 | ConvertFrom-Json
            $stagedBound = $staged.publication_contract.authoritative_result_basename
            $stagedTerminal = $staged.terminal_outcome
            $stagedExit = [int]$staged.exit_code
            $stagedPersisted = [bool]$staged.evidence_persisted
        }
        [pscustomobject]@{
            threw = $threw
            stagingExists = (Test-Path -LiteralPath $stg)
            finalExists = (Test-Path -LiteralPath $fin)
            stagingBasename = ([System.IO.Path]::GetFileName($stg))
            stagingPath = $stg
            stagedBoundBasename = $stagedBound
            stagedTerminalOutcome = $stagedTerminal
            stagedExitCode = $stagedExit
            stagedEvidencePersisted = $stagedPersisted
        } | ConvertTo-Json -Compress
    }
    'authoritative' {
        # $Text = candidate current path; $CtxJson = parsed record source.
        $record = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $v = Test-ExpiryProbeAuthoritativeResult -Path $Text -Record $record
        [pscustomobject]@{ authoritative = $v.authoritative; reasons = @($v.reasons) } | ConvertTo-Json -Compress -Depth 4
    }
}
"""


# --------------------------------------------------------------------------- #
# Mechanical focused-CI dependency closure.
# --------------------------------------------------------------------------- #
def module_repo_dependencies(module_path):
    """Every repository file this module reads or semantically asserts.

    Derived from the module's own AST: any module-level constant or inline expression built
    as ``ROOT / "..."`` (directly or through another such constant) is a declared repository
    path dependency. Directories are dropped; only existing files are returned. Adding a new
    ``ROOT / ...`` constant therefore extends the inventory automatically.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    symbols = {"ROOT": ()}

    def resolve(node):
        if isinstance(node, ast.Name):
            return symbols.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = resolve(node.left)
            if left is None:
                return None
            right = node.right
            if isinstance(right, ast.Constant) and isinstance(right.value, str):
                return left + (right.value,)
            return None
        return None

    # Module-level assignments in source order, so later constants can build on earlier ones.
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            parts = resolve(stmt.value)
            if parts is not None:
                symbols[stmt.targets[0].id] = parts

    discovered = set()
    for parts in symbols.values():
        if parts:
            discovered.add("/".join(parts))
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = resolve(node)
            if parts:
                discovered.add("/".join(parts))
    return {rel for rel in discovered if (ROOT / rel).is_file()}


def workflow_path_filters(text):
    """Map every ``on:`` event that declares a ``paths:`` filter to its pattern list."""
    lines = text.splitlines()
    index = 0
    total = len(lines)
    while index < total and not re.match(r"^on:\s*$", lines[index]):
        index += 1
    filters = {}
    if index == total:
        return filters
    index += 1
    event = None
    while index < total:
        line = lines[index]
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            index += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if indent == 2:
            event = stripped.split(":", 1)[0]
        elif indent == 4 and stripped == "paths:" and event:
            items = []
            probe = index + 1
            while probe < total:
                candidate = lines[probe]
                inner = candidate.strip()
                if inner == "" or inner.startswith("#"):
                    probe += 1
                    continue
                inner_indent = len(candidate) - len(candidate.lstrip())
                if inner_indent >= 6 and inner.startswith("- "):
                    items.append(inner[2:].strip().strip('"').strip("'"))
                    probe += 1
                    continue
                break
            filters[event] = items
            index = probe
            continue
        index += 1
    return filters


def glob_to_regex(pattern):
    """GitHub Actions path-filter glob: ``**`` crosses separators, ``*`` and ``?`` do not."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def uncovered_dependencies(dependencies, patterns):
    compiled = [glob_to_regex(p) for p in patterns]
    return sorted(dep for dep in dependencies if not any(rx.match(dep) for rx in compiled))


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

    def test_requires_approval_reference(self):
        self.assertIn("$ApprovalReference", self.script)
        self.assertRegex(self.script, r"-ApprovalReference is required")

    # ---- Canonical claim/result authority ---- #
    def test_script_exposes_no_state_directory_or_json_out_anywhere(self):
        # Neither the removed operator-selected claim root nor the removed secondary output
        # path may survive as a parameter, a variable, a validation, or a redaction entry.
        for removed in ("StateDirectory", "JsonOut"):
            self.assertNotIn("$" + removed, self.script, removed)
            self.assertNotIn("-" + removed, self.script, removed)
        self.assertNotIn("$StateDirectory", self.lib)
        self.assertNotIn("$JsonOut", self.lib)
        # The removed operator-path validations are gone with them.
        for gone in ("Test-ExpiryProbeSafePath", "Test-ExpiryProbePathInsideRepo", "Get-ExpiryProbeRepoRoot"):
            self.assertNotIn(gone, self.script, gone)
            self.assertNotIn(gone, self.lib, gone)

    def test_canonical_state_root_is_a_fixed_code_constant(self):
        self.assertIn('$script:ExpiryProbeCanonicalStateRoot = "%s"' % CANONICAL_STATE_ROOT, self.lib)
        # The script takes the root from the nullary accessor and from nothing else. Only
        # executable (non-comment) lines count; the parameter block documents the accessor.
        self.assertIn("$script:ExpiryProbeStateRoot = Get-ExpiryProbeCanonicalStateRoot", self.script)
        code_lines = [ln for ln in self.script.splitlines() if not ln.lstrip().startswith("#")]
        accessor_lines = [ln for ln in code_lines if "Get-ExpiryProbeCanonicalStateRoot" in ln]
        self.assertEqual(len(accessor_lines), 1, accessor_lines)
        self.assertEqual(self.script.count("$script:ExpiryProbeStateRoot ="), 1)
        # Not an environment value, not deployment/current-directory relative. Only the
        # assignment matters; other lines merely read the resolved root (e.g. the sanitiser).
        assignment_lines = [ln for ln in self.script.splitlines()
                            if re.match(r"\s*\$script:ExpiryProbeStateRoot\s*=", ln)]
        self.assertEqual(len(assignment_lines), 1, assignment_lines)
        for line in assignment_lines:
            self.assertNotIn("GetEnvironmentVariable", line, line)
            self.assertNotIn("$env:", line, line)
            self.assertNotIn("$scriptDir", line, line)
            self.assertNotIn("$PSScriptRoot", line, line)
            self.assertNotIn("Get-Location", line, line)
        self.assertNotIn("AC2_PROBE_STATE", self.script)

    def test_canonical_root_is_outside_any_repository_checkout(self):
        # The fixed root replaces the old "reject a state directory inside the repo" guard:
        # it is absolute and cannot be inside this checkout.
        self.assertTrue(re.match(r"^[A-Za-z]:\\", CANONICAL_STATE_ROOT))
        self.assertFalse(str(ROOT).lower().startswith(CANONICAL_STATE_ROOT.lower()))
        self.assertFalse(CANONICAL_STATE_ROOT.lower().startswith(str(ROOT).lower()))

    def test_probe_never_creates_repairs_or_cleans_the_state_root(self):
        self.assertNotIn("New-Item", self.script)
        self.assertNotIn("CreateDirectory", self.script)
        self.assertNotIn("New-Item", self.lib)
        self.assertNotIn("CreateDirectory", self.lib)

    def test_trusted_root_validation_precedes_all_live_access(self):
        trust_idx = self.script.index("Test-ExpiryProbeTrustedStateRoot")
        self.assertLess(trust_idx, self.script.index("LoadFrom"))
        self.assertLess(trust_idx, self.script.index("$authenticateMethod.Invoke"))
        self.assertLess(trust_idx, self.script.index("$result.autocount_contacted = $true"))
        self.assertLess(trust_idx, self.script.index("$getMemberMethod.Invoke"))
        # Path derivation, and therefore any filesystem use of the root, happens after it.
        self.assertLess(trust_idx, self.script.index("Get-ExpiryProbeStatePaths"))

    def test_trusted_root_validation_fails_closed_on_every_condition(self):
        for reason in ("platform_not_windows", "root_not_absolute", "root_unresolvable",
                       "root_not_local_volume", "volume_root_unexpected", "root_missing",
                       "component_missing", "root_not_directory", "component_not_directory",
                       "root_reparse_point", "component_reparse_point", "component_stat_failed"):
            self.assertIn("'%s'" % reason, self.lib, reason)
        # Every stat/access exception around component inspection fails closed with the
        # dedicated reason rather than continuing.
        stat_catches = re.findall(r"catch \{ \$reason = 'component_stat_failed'; break \}", self.lib)
        self.assertGreaterEqual(len(stat_catches), 3, "each component stat/access step must fail closed")
        # Redirected components are rejected, never followed or resolved.
        self.assertIn("ReparsePoint", self.lib)
        self.assertNotIn("ResolveLinkTarget", self.lib)
        self.assertNotIn("LinkTarget", self.lib)

    def test_claim_root_unavailable_is_terminal_and_pre_contact(self):
        self.assertIn("CLAIM_ROOT_UNAVAILABLE", self.lib)
        self.assertIn("$result.claim_root_unavailable = $true", self.script)
        unavailable_idx = self.script.index("$result.claim_root_unavailable = $true")
        self.assertLess(unavailable_idx, self.script.index("$result.autocount_contacted = $true"))
        self.assertLess(unavailable_idx, self.script.index("Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod"))
        # The raw root is never emitted; only a reason code is.
        self.assertIn("claim_root_failure_reasons", self.script)
        self.assertNotIn(CANONICAL_STATE_ROOT, self.script)

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

    # ---- Publication: no clobber, no cleanup, content-bound authority ---- #
    def test_no_artefact_is_ever_deleted_or_overwritten(self):
        # No Set-Content and no Remove-Item in either file: not for a claim, not for a
        # staging artefact, and not for a result.
        self.assertNotIn("Set-Content", self.script)
        self.assertNotIn("Remove-Item", self.script)
        self.assertNotIn("Set-Content", self.lib)
        self.assertNotIn("Remove-Item", self.lib)
        self.assertNotRegex(self.lib, r"\[System\.IO\.File\]::Delete")
        self.assertNotRegex(self.script, r"\[System\.IO\.File\]::Delete")
        self.assertIn("New-ExpiryProbeDurableArtifact", self.script)
        self.assertIn("Publish-ExpiryProbeResultAtomic", self.script)

    def test_lib_publishes_via_no_clobber_staging_and_no_replace_move(self):
        self.assertNotIn("Write-ExpiryProbeResultAtomic", self.lib)
        self.assertNotIn("Write-ExpiryProbeResultAtomic", self.script)
        # The old ".tmp" writer (which deleted a pre-existing temp file) is gone.
        self.assertNotIn('$Path + ".tmp"', self.lib)
        self.assertNotIn(".tmp", self.lib)
        self.assertIn("expiry_probe_staging_", self.lib)
        self.assertIn(".incomplete", self.lib)
        self.assertRegex(self.lib, r"FileMode\]::CreateNew")
        self.assertRegex(self.lib, r"\[System\.IO\.File\]::Move\(\$StagingPath, \$FinalPath\)")
        self.assertRegex(self.lib, r"refusing to overwrite evidence")

    def test_publication_contract_is_content_borne_and_mechanical(self):
        for field in ("publication_contract_version", "authoritative_result_basename", "authority_rule"):
            self.assertIn(field, self.lib, field)
        self.assertIn('$script:ExpiryProbePublicationContractVersion = "%s"' % PUBLICATION_CONTRACT_VERSION, self.lib)
        # The rule states the mechanical test, not a vague warning.
        self.assertRegex(self.lib, r"authoritative ONLY when its current file basename is exactly equal to authoritative_result_basename")
        self.assertRegex(self.lib, r"(?i)NON-AUTHORITATIVE regardless of the terminal_outcome")
        # The staged bytes carry it.
        self.assertIn("publication_contract          = $null", self.script)
        self.assertIn("New-ExpiryProbePublicationContract -OperationId $operationId", self.script)

    def test_move_failure_leaves_staging_and_reports_failure(self):
        self.assertRegex(self.script, r"\$result\.evidence_persisted\s*=\s*\$false")
        self.assertRegex(self.script, r"-EvidencePersisted\s+\$false")
        self.assertIn("$result.non_authoritative_staging_may_remain = $true", self.script)
        self.assertRegex(self.script, r"(?i)NON-AUTHORITATIVE staged artefact may remain")
        self.assertRegex(self.script, r"(?i)Do not delete, rename or republish it")
        # The library does not clean up, rename or rewrite the staged object on failure.
        publish = self.lib[self.lib.index("function Publish-ExpiryProbeResultAtomic"):]
        publish = publish[:publish.index("\nfunction ")]
        self.assertNotIn("Remove-Item", publish)
        self.assertNotIn("catch", publish, "a move failure must propagate, not be swallowed")

    def test_authoritative_validator_is_pure_and_basename_bound(self):
        self.assertIn("function Test-ExpiryProbeAuthoritativeResult", self.lib)
        for reason in ("basename_not_authoritative", "publication_contract_missing",
                       "publication_contract_version_mismatch", "schema_version_mismatch",
                       "operation_id_basename_mismatch", "artefact_is_staging",
                       "state_contradiction", "evidence_not_persisted"):
            self.assertIn("'%s'" % reason, self.lib, reason)
        # The decisive check is exact basename equality against the content-bound value, not
        # a filename glob or suffix test.
        self.assertRegex(self.lib, r"\$basename\.Equals\(\$boundBasename, \[System\.StringComparison\]::Ordinal\)")

    def test_claim_created_before_save_in_source_order(self):
        claim_idx = self.script.index("New-ExpiryProbeDurableArtifact -Path $claimPath")
        save_idx = self.script.index("Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod")
        recheck_idx = self.script.index("$result.member_recheck_attempted = $true")
        self.assertLess(recheck_idx, claim_idx, "the claim is created after the second duplicate check")
        self.assertLess(claim_idx, save_idx)

    # ---- Truthful contact and race state ---- #
    def test_autocount_contacted_is_set_before_the_first_authentication_call(self):
        contact_idx = self.script.index("$result.autocount_contacted = $true")
        auth_idx = self.script.index("[void]$authenticateMethod.Invoke")
        self.assertLess(contact_idx, auth_idx, "contact must be recorded before the call, not after success")
        self.assertEqual(self.script.count("$result.autocount_contacted = $true"), 1)
        # It is never reset.
        self.assertNotIn("$result.autocount_contacted = $false", self.script)
        self.assertLess(contact_idx, self.script.index("$result.authentication_success = $loginOk"))

    def test_read_attempt_flags_precede_their_reads(self):
        initial_flag = self.script.index("$result.initial_member_read_attempted = $true")
        initial_read = self.script.index("$existing = $getMemberMethod.Invoke")
        recheck_flag = self.script.index("$result.member_recheck_attempted = $true")
        recheck_read = self.script.index("$recheck = $getMemberMethod.Invoke")
        self.assertLess(initial_flag, initial_read)
        self.assertLess(recheck_flag, recheck_read)
        self.assertLess(initial_read, recheck_flag)

    def test_post_contact_claim_loss_is_a_distinct_outcome(self):
        self.assertIn("ATTEMPT_CLAIM_LOST_AFTER_CONTACT", self.lib)
        self.assertIn("$result.claim_lost_after_contact = $true", self.script)
        lost_idx = self.script.index("$result.claim_lost_after_contact = $true")
        save_idx = self.script.index("Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod")
        self.assertLess(lost_idx, save_idx, "the losing contender must throw before any save")
        # The branch is chosen from the truthful contact flag, and the message says contact
        # DID occur (it must not claim a pre-contact refusal).
        self.assertIn("if ($result.autocount_contacted) {", self.script)
        self.assertRegex(self.script, r"Another contender owns the permanent attempt claim")
        self.assertRegex(self.script, r"had already made live AutoCount contact")
        # The pre-contact branch keeps the pre-contact wording.
        self.assertRegex(self.script, r"refusing to write before any AutoCount contact")

    def test_pre_contact_claim_check_happens_before_any_contact(self):
        precheck_idx = self.script.index("if (Test-Path -LiteralPath $claimPath) {")
        self.assertLess(precheck_idx, self.script.index("$result.autocount_contacted = $true"))
        self.assertLess(precheck_idx, self.script.index("LoadFrom"))
        # The claim namespace comes from the fixed root, never from a process argument.
        self.assertIn("$claimPath = $statePaths.claim_path", self.script)
        self.assertIn("Get-ExpiryProbeStatePaths -Root $script:ExpiryProbeStateRoot", self.script)

    def test_readback_failure_is_isolated_from_pre_write_classification(self):
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

    def test_lib_flush_fallback_is_narrowed_to_unsupported_runtime(self):
        self.assertRegex(self.lib, r"catch \[System\.NotSupportedException\]\s*\{\s*\$stream\.Flush\(\)")
        self.assertNotRegex(self.lib, r"catch\s*\{\s*\$stream\.Flush\(\)")

    def test_lib_target_fingerprint_is_case_insensitive(self):
        self.assertIn("ToLowerInvariant()", self.lib)

    def test_claim_durability_failure_fails_closed_before_save(self):
        self.assertIn("could not be durably persisted", self.script)

    def test_claim_persistence_failure_is_distinct_from_conflict(self):
        self.assertIn("claim_persist_failed", self.script)
        self.assertIn("post-create-persist-failed", self.script)
        self.assertIn("post-create-persist-failed", self.lib)

    def test_evidence_persisted_is_pessimistic_and_underlying_retained(self):
        self.assertRegex(self.script, r"evidence_persisted\s*=\s*\$false")
        self.assertIn("underlying_terminal_outcome", self.script)
        self.assertIn("EVIDENCE_PERSISTENCE_FAILED", self.lib)

    def test_approval_reference_alphabet_is_strict_and_rejects_target(self):
        self.assertIn("'^[A-Za-z0-9._-]{3,64}$'", self.script)
        self.assertRegex(self.script, r"must not contain the server or database")
        self.assertIn("ToLowerInvariant()", self.script)

    def test_sanitizer_redacts_runtime_paths(self):
        self.assertIn("Get-ExpiryProbePathRedacted", self.script)
        self.assertIn("Get-ExpiryProbePathRedacted", self.lib)
        # The canonical state root and the AutoCount root are redaction inputs; the removed
        # operator-selected paths are not (they no longer exist).
        self.assertRegex(self.script, r"\$script:ExpiryProbeStateRoot,\s*\$AcRoot")

    def test_approval_reference_recorded_only_after_validation(self):
        activation_idx = self.script.index("$result.activated = $true")
        substr_idx = self.script.index("must not contain the server or database")
        assign_idx = self.script.index("$result.approval_reference = $ApprovalReference")
        self.assertGreater(assign_idx, substr_idx, "approval_reference must be set only after validation")
        self.assertGreater(assign_idx, activation_idx)
        self.assertEqual(self.script.count("$result.approval_reference = $ApprovalReference"), 1)

    def test_gitignore_covers_probe_evidence(self):
        gi = GITIGNORE.read_text(encoding="utf-8")
        self.assertIn("expiry_probe_claim_*.claim", gi)
        self.assertIn("expiry_probe_result_*.json", gi)
        self.assertIn("expiry_probe_staging_*.incomplete", gi)


@unittest.skipIf(PS is None, "no PowerShell executable available")
class ExpiryProbeLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.harness = cls.tmp / "libprobe.ps1"
        cls.harness.write_text(LIBPROBE, encoding="utf-8")

    def _cmd(self, op, **kw):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(self.harness), "-Lib", str(LIB), "-Op", op]
        for k, v in kw.items():
            cmd += ["-" + k, str(v)]
        return cmd

    def _lib(self, op, **kw):
        return subprocess.run(self._cmd(op, **kw), capture_output=True, text=True)

    def _json(self, op, **kw):
        proc = self._lib(op, **kw)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def _terminal(self, **flags):
        base = dict(activated=True, claim_root_unavailable=False, claim_conflict=False,
                    claim_lost_after_contact=False, claim_persist_failed=False,
                    claim_created=False, autocount_contacted=False,
                    initial_member_read_attempted=False, member_recheck_attempted=False,
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

    def _contacted(self, **flags):
        """Flags for a run that authenticated and performed both duplicate reads."""
        base = dict(autocount_contacted=True, initial_member_read_attempted=True,
                    member_recheck_attempted=True)
        base.update(flags)
        return base

    # ---- Canonical authority ---- #
    def test_canonical_root_accessor_is_nullary_and_exact(self):
        info = self._json("canonroot")
        self.assertEqual(info["root"], CANONICAL_STATE_ROOT)
        self.assertEqual(info["declaredParameterCount"], 0,
                         "the production accessor must take no parameters at all")

    def test_artefact_basenames_are_pure_and_working_directory_independent(self):
        root = self.tmp / "stateroot"
        root.mkdir(exist_ok=True)
        elsewhere = self.tmp / "otherwd"
        elsewhere.mkdir(exist_ok=True)
        text = "afp_deadbeef|expop_abc123"
        first = self._json("paths", Dir=str(root), Text=text)
        second = subprocess.run(self._cmd("paths", Dir=str(root), Text=text),
                                capture_output=True, text=True, cwd=str(elsewhere))
        self.assertEqual(second.returncode, 0, second.stderr)
        second = json.loads(second.stdout)
        self.assertNotEqual(first["cwd"], second["cwd"], "the two runs must use different working directories")
        for key in ("claim_basename", "result_basename", "staging_basename", "claim_path"):
            self.assertEqual(first[key], second[key], key)
        self.assertEqual(first["claim_basename"], "expiry_probe_claim_afp_deadbeef.claim")
        self.assertEqual(first["result_basename"], "expiry_probe_result_expop_abc123.json")
        self.assertEqual(first["staging_basename"], "expiry_probe_staging_expop_abc123.incomplete")
        # The path helper and the pure basename helpers cannot diverge.
        self.assertEqual(first["claim_basename"], first["pure_claim"])
        self.assertEqual(first["result_basename"], first["pure_result"])
        self.assertEqual(first["staging_basename"], first["pure_staging"])

    # ---- Trusted-root validation ---- #
    def test_trusted_root_accepts_a_plain_directory_chain(self):
        root = self.tmp / "trusted_root"
        root.mkdir(exist_ok=True)
        verdict = self._json("trust", Dir=str(root), Text="0")
        self.assertTrue(verdict["trusted"], "reasons=%s" % as_list(verdict["reasons"]))
        self.assertEqual(as_list(verdict["reasons"]), [])

    def test_missing_root_fails_closed(self):
        missing = self.tmp / "definitely_absent"
        verdict = self._json("trust", Dir=str(missing), Text="0")
        self.assertFalse(verdict["trusted"])
        self.assertEqual(as_list(verdict["reasons"]), ["root_missing"])

    def test_missing_ancestor_fails_closed(self):
        deep = self.tmp / "absent_parent" / "child"
        verdict = self._json("trust", Dir=str(deep), Text="0")
        self.assertFalse(verdict["trusted"])
        self.assertEqual(as_list(verdict["reasons"]), ["component_missing"])

    def test_non_directory_root_fails_closed(self):
        plain = self.tmp / "root_is_a_file"
        plain.write_text("not a directory", encoding="utf-8")
        verdict = self._json("trust", Dir=str(plain), Text="0")
        self.assertFalse(verdict["trusted"])
        self.assertEqual(as_list(verdict["reasons"]), ["root_not_directory"])

    def test_non_directory_ancestor_fails_closed(self):
        plain = self.tmp / "ancestor_is_a_file"
        plain.write_text("not a directory", encoding="utf-8")
        verdict = self._json("trust", Dir=str(plain / "child"), Text="0")
        self.assertFalse(verdict["trusted"])
        # A file cannot have children, so the chain stops at the non-directory component.
        self.assertIn(as_list(verdict["reasons"])[0], ("component_not_directory", "component_missing"))

    def test_relative_and_empty_roots_fail_closed(self):
        verdict = self._json("trust", Dir="relative/state/root", Text="0")
        self.assertFalse(verdict["trusted"])
        self.assertEqual(as_list(verdict["reasons"]), ["root_not_absolute"])

    def test_non_windows_platform_fails_closed_for_the_active_path(self):
        root = self.tmp / "platform_root"
        root.mkdir(exist_ok=True)
        verdict = self._json("trust", Dir=str(root), Text="1")
        if IS_WINDOWS:
            self.assertTrue(verdict["trusted"], "reasons=%s" % as_list(verdict["reasons"]))
        else:
            self.assertFalse(verdict["trusted"])
            self.assertEqual(as_list(verdict["reasons"]), ["platform_not_windows"],
                             "the active probe path must fail closed off Windows, not skip the contract")

    @unittest.skipUnless(IS_WINDOWS, "UNC rejection is a Windows-only root condition")
    def test_unc_root_is_rejected_without_touching_the_network(self):
        verdict = self._json("trust", Dir=r"\\no-such-host\no-such-share\state", Text="1")
        self.assertFalse(verdict["trusted"])
        self.assertEqual(as_list(verdict["reasons"]), ["root_not_local_volume"])

    @unittest.skipUnless(IS_WINDOWS, "junction coverage is Windows-specific")
    def test_windows_junction_ancestor_and_root_fail_closed(self):
        real = self.tmp / "junction_target"
        (real / "state").mkdir(parents=True, exist_ok=True)
        link = self.tmp / "junction_link"
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                              capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        ancestor = self._json("trust", Dir=str(link / "state"), Text="1")
        self.assertFalse(ancestor["trusted"])
        self.assertEqual(as_list(ancestor["reasons"]), ["component_reparse_point"])
        leaf = self._json("trust", Dir=str(link), Text="1")
        self.assertFalse(leaf["trusted"])
        self.assertEqual(as_list(leaf["reasons"]), ["root_reparse_point"])

    @unittest.skipIf(IS_WINDOWS, "POSIX symlink equivalent of the junction coverage")
    def test_posix_symlink_ancestor_and_root_fail_closed(self):
        real = self.tmp / "symlink_target"
        (real / "state").mkdir(parents=True, exist_ok=True)
        link = self.tmp / "symlink_link"
        if not link.exists():
            os.symlink(str(real), str(link), target_is_directory=True)
        ancestor = self._json("trust", Dir=str(link / "state"), Text="0")
        self.assertFalse(ancestor["trusted"])
        self.assertEqual(as_list(ancestor["reasons"]), ["component_reparse_point"])
        leaf = self._json("trust", Dir=str(link), Text="0")
        self.assertFalse(leaf["trusted"])
        self.assertEqual(as_list(leaf["reasons"]), ["root_reparse_point"])

    # ---- Terminal truth ---- #
    def test_terminal_vocabulary_is_complete(self):
        proc = self._lib("vocabulary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        codes = proc.stdout.strip().split(",")
        self.assertEqual(sorted(codes), sorted(TERMINAL_CODES))

    def test_terminal_state_matrix_is_honest_and_consistent(self):
        self.assertEqual(self._terminal(activated=False), ("REFUSED", 0))
        self.assertEqual(self._terminal(claim_root_unavailable=True), ("CLAIM_ROOT_UNAVAILABLE", 0))
        self.assertEqual(self._terminal(claim_conflict=True), ("ATTEMPT_ALREADY_CLAIMED", 0))
        self.assertEqual(self._terminal(**self._contacted(claim_lost_after_contact=True)),
                         ("ATTEMPT_CLAIM_LOST_AFTER_CONTACT", 0))
        self.assertEqual(self._terminal(**self._contacted(claim_persist_failed=True)),
                         ("CLAIM_PERSISTENCE_FAILED", 0))
        self.assertEqual(self._terminal(**self._contacted(member_exists_initial=True)),
                         ("BLOCKED_MEMBER_EXISTS", 0))
        self.assertEqual(self._terminal(**self._contacted(member_exists_recheck=True)),
                         ("BLOCKED_MEMBER_EXISTS", 0))
        self.assertEqual(self._terminal(), ("FAILED_BEFORE_WRITE", 0))
        self.assertEqual(self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                         save_outcome="uncertain")),
                         ("WRITE_OUTCOME_UNCERTAIN", 0))
        self.assertEqual(self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                         save_member_confirmed=True,
                                                         save_outcome="confirmed", readback_found=False)),
                         ("WRITE_CONFIRMED_READBACK_FAILED", 0))
        self.assertEqual(self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                         save_member_confirmed=True, save_outcome="confirmed",
                                                         readback_found=True, expiry_match=False)),
                         ("EXPIRY_READBACK_MISMATCH", 0))
        self.assertEqual(self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                         save_member_confirmed=True, save_outcome="confirmed",
                                                         readback_found=True, expiry_match=True)),
                         ("EXPIRY_VERIFIED", 0))

    def test_pre_contact_claim_conflict_requires_no_contact(self):
        code, contradictions = self._terminal(claim_conflict=True)
        self.assertEqual(code, "ATTEMPT_ALREADY_CLAIMED")
        self.assertEqual(contradictions, 0)
        # ATTEMPT_ALREADY_CLAIMED with live contact is impossible by definition.
        _, contradictions = self._terminal(**self._contacted(claim_conflict=True))
        self.assertGreater(contradictions, 0)
        reasons = self._contradiction_reasons(**self._contacted(claim_conflict=True))
        self.assertIn("already_claimed_after_contact", reasons)

    def _contradiction_reasons(self, **flags):
        base = dict(activated=True, claim_root_unavailable=False, claim_conflict=False,
                    claim_lost_after_contact=False, claim_persist_failed=False,
                    claim_created=False, autocount_contacted=False,
                    initial_member_read_attempted=False, member_recheck_attempted=False,
                    member_exists_initial=False, member_exists_recheck=False,
                    save_member_attempted=False, save_member_confirmed=False,
                    save_outcome="not_attempted", readback_found=False, expiry_match=False)
        base.update(flags)
        ctx = self.tmp / "ctx_reasons.json"
        ctx.write_text(json.dumps(base), encoding="utf-8")
        proc = self._lib("contradictions", CtxJson=str(ctx))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [r for r in proc.stdout.strip().split(",") if r]

    def test_post_contact_claim_loss_never_reaches_save(self):
        code, contradictions = self._terminal(**self._contacted(claim_lost_after_contact=True))
        self.assertEqual(code, "ATTEMPT_CLAIM_LOST_AFTER_CONTACT")
        self.assertEqual(contradictions, 0)
        # A loser that somehow recorded a save attempt, a created claim, or no contact at all
        # is an impossible state.
        self.assertIn("claim_lost_but_save_attempted",
                      self._contradiction_reasons(**self._contacted(claim_lost_after_contact=True,
                                                                    save_member_attempted=True,
                                                                    save_outcome="uncertain")))
        self.assertIn("claim_lost_but_claim_created",
                      self._contradiction_reasons(**self._contacted(claim_lost_after_contact=True,
                                                                    claim_created=True)))
        self.assertIn("claim_lost_without_contact",
                      self._contradiction_reasons(claim_lost_after_contact=True))
        self.assertIn("claim_lost_and_pre_contact_conflict",
                      self._contradiction_reasons(**self._contacted(claim_lost_after_contact=True,
                                                                    claim_conflict=True)))

    def test_claim_root_unavailable_excludes_every_live_flag(self):
        reasons = self._contradiction_reasons(claim_root_unavailable=True, autocount_contacted=True,
                                              initial_member_read_attempted=True,
                                              save_member_attempted=True, save_outcome="uncertain",
                                              claim_created=True)
        for expected in ("claim_root_unavailable_after_contact",
                         "claim_root_unavailable_with_save_attempt",
                         "claim_root_unavailable_with_claim",
                         "claim_root_unavailable_with_live_read"):
            self.assertIn(expected, reasons)

    def test_live_reads_and_saves_require_recorded_contact(self):
        self.assertIn("initial_read_without_contact",
                      self._contradiction_reasons(initial_member_read_attempted=True))
        self.assertIn("recheck_read_without_contact",
                      self._contradiction_reasons(member_recheck_attempted=True))
        self.assertIn("save_without_contact",
                      self._contradiction_reasons(save_member_attempted=True, save_outcome="uncertain"))
        self.assertIn("member_exists_without_read_attempt",
                      self._contradiction_reasons(autocount_contacted=True, member_exists_initial=True))
        self.assertIn("recheck_without_read_attempt",
                      self._contradiction_reasons(autocount_contacted=True,
                                                  initial_member_read_attempted=True,
                                                  member_exists_recheck=True))

    def test_post_save_readback_failure_is_never_failed_before_write(self):
        code, contr = self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                       save_member_confirmed=True,
                                                       save_outcome="confirmed", readback_found=False))
        self.assertEqual(code, "WRITE_CONFIRMED_READBACK_FAILED")
        self.assertEqual(contr, 0)

    def test_impossible_flag_combinations_are_flagged(self):
        _, c1 = self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                 save_member_confirmed=False, save_outcome="confirmed"))
        self.assertGreater(c1, 0)
        _, c2 = self._terminal(**self._contacted(save_member_attempted=True, claim_created=True,
                                                 save_outcome="uncertain", readback_found=True))
        self.assertGreater(c2, 0)
        _, c3 = self._terminal(activated=False, save_member_attempted=True)
        self.assertGreater(c3, 0)

    def test_exit_codes_only_verified_is_zero(self):
        for code in TERMINAL_CODES:
            proc = self._lib("exit", Text=code)
            expected = "0" if code == "EXPIRY_VERIFIED" else "1"
            self.assertEqual(proc.stdout.strip(), expected, code)

    def _final(self, underlying, durable, persisted):
        proc = self._lib("final", Text="%s|%d|%d" % (underlying, 1 if durable else 0, 1 if persisted else 0))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outcome, code = proc.stdout.strip().split("|")
        return outcome, int(code)

    def test_evidence_override_gates_success(self):
        self.assertEqual(self._final("EXPIRY_VERIFIED", True, True), ("EXPIRY_VERIFIED", 0))
        self.assertEqual(self._final("EXPIRY_VERIFIED", True, False), ("EVIDENCE_PERSISTENCE_FAILED", 1))
        self.assertEqual(self._final("FAILED_BEFORE_WRITE", True, True), ("FAILED_BEFORE_WRITE", 1))
        self.assertEqual(self._final("FAILED_BEFORE_WRITE", True, False), ("EVIDENCE_PERSISTENCE_FAILED", 1))
        self.assertEqual(self._final("REFUSED", False, True), ("REFUSED", 1))
        self.assertEqual(self._final("CLAIM_ROOT_UNAVAILABLE", False, True), ("CLAIM_ROOT_UNAVAILABLE", 1))
        self.assertEqual(self._final("ATTEMPT_CLAIM_LOST_AFTER_CONTACT", False, True),
                         ("ATTEMPT_CLAIM_LOST_AFTER_CONTACT", 1))

    def test_fingerprints_deterministic_shaped_case_and_whitespace_insensitive(self):
        info = self._json("fp")
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
        info = self._json("badclaim", Dir=str(d))
        self.assertTrue(info["threw"], "a durable persistence failure must propagate")
        self.assertFalse(info["exists"], "no usable artefact may remain on failure")

    # ---- Claim exclusivity and concurrency ---- #
    def test_claim_is_exclusive_create_and_never_deleted(self):
        d = self.tmp / "claimdir"
        d.mkdir(exist_ok=True)
        info = self._json("claim", Dir=str(d))
        self.assertTrue(info["exists"])
        self.assertTrue(info["secondBlocked"], "a second claim on the same path must fail closed (one winner)")
        self.assertTrue(info["unchanged"], "the original claim content must survive a second attempt")

    def test_two_concurrent_contenders_for_one_claim_produce_one_winner(self):
        d = self.tmp / "racedir"
        d.mkdir(exist_ok=True)
        claim = d / "expiry_probe_claim_afp_race.claim"
        procs = [subprocess.Popen(self._cmd("claimrace", Text=str(claim)),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(2)]
        outputs = [p.communicate() for p in procs]
        created = []
        for stdout, stderr in outputs:
            self.assertTrue(stdout.strip(), stderr)
            created.append(json.loads(stdout)["created"])
        self.assertEqual(created.count(True), 1, "exactly one contender may create the canonical claim")
        self.assertEqual(created.count(False), 1, "the loser must fail closed")
        self.assertTrue(claim.is_file())
        self.assertEqual(claim.read_text(encoding="utf-8").strip(), "contender")

    # ---- Publication ---- #
    def _record_file(self, name, record):
        path = self.tmp / name
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_publication_uses_no_clobber_staging_then_no_replace_move(self):
        d = self.tmp / "publishdir"
        d.mkdir(exist_ok=True)
        operation_id = "expop_publish01"
        record = self._record_file("publish_record.json", verified_record(operation_id))
        info = self._json("publish", Dir=str(d), Text=operation_id, CtxJson=str(record))
        self.assertTrue(info["finalExists"])
        self.assertFalse(info["stagingLeft"], "a successful publication leaves no staging artefact")
        self.assertTrue(info["secondBlocked"], "a second publication must not overwrite prior evidence")
        self.assertEqual(info["finalOperationId"], operation_id)
        self.assertEqual(info["finalTerminalOutcome"], "EXPIRY_VERIFIED")

    def test_existing_staging_artefact_is_never_overwritten_or_removed(self):
        d = self.tmp / "stagingconflictdir"
        d.mkdir(exist_ok=True)
        info = self._json("stagingconflict", Dir=str(d), Text="expop_conflict01")
        self.assertTrue(info["blocked"], "an existing staging artefact must fail closed")
        self.assertTrue(info["stagingUnchanged"], "the pre-existing staging bytes must survive byte-identically")
        self.assertFalse(info["finalExists"], "nothing may be published over a staging conflict")

    def test_move_failure_leaves_a_self_invalidating_staging_artefact(self):
        d = self.tmp / "movefaildir"
        d.mkdir(exist_ok=True)
        operation_id = "expop_movefail01"
        record = self._record_file("movefail_record.json", verified_record(operation_id))
        info = self._json("movefail", Dir=str(d), Text=operation_id, CtxJson=str(record))
        self.assertTrue(info["threw"], "a failed final move must propagate, never be swallowed")
        self.assertTrue(info["stagingExists"], "the staging artefact must be left untouched")
        self.assertFalse(info["finalExists"], "no authoritative result may exist after a failed move")
        self.assertEqual(info["stagingBasename"], "expiry_probe_staging_%s.incomplete" % operation_id)
        # The staged bytes bind themselves to the final basename they never reached, even
        # though they contain a candidate EXPIRY_VERIFIED with exit_code 0.
        self.assertEqual(info["stagedBoundBasename"], "expiry_probe_result_%s.json" % operation_id)
        self.assertNotEqual(info["stagedBoundBasename"], info["stagingBasename"])
        self.assertEqual(info["stagedTerminalOutcome"], "EXPIRY_VERIFIED")
        self.assertEqual(info["stagedExitCode"], 0)
        self.assertTrue(info["stagedEvidencePersisted"])
        # And the authoritative validator rejects it at its actual current path.
        verdict = self._json("authoritative", Text=info["stagingPath"], CtxJson=str(record))
        self.assertFalse(verdict["authoritative"],
                         "a staged candidate EXPIRY_VERIFIED must never validate as authoritative")
        reasons = as_list(verdict["reasons"])
        self.assertIn("basename_not_authoritative", reasons)
        self.assertIn("artefact_is_staging", reasons)

    def test_publication_contract_binds_one_authoritative_basename(self):
        contract = self._json("contract", Text="expop_contract01")
        self.assertEqual(contract["publication_contract_version"], PUBLICATION_CONTRACT_VERSION)
        self.assertEqual(contract["authoritative_result_basename"], "expiry_probe_result_expop_contract01.json")
        self.assertRegex(contract["authority_rule"], r"(?i)authoritative ONLY when its current file basename")

    def test_authoritative_validator_accepts_only_the_bound_final_basename(self):
        operation_id = "expop_validator01"
        record_path = self._record_file("validator_record.json", verified_record(operation_id))
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(record_path))
        self.assertTrue(verdict["authoritative"], "reasons=%s" % as_list(verdict["reasons"]))
        self.assertEqual(as_list(verdict["reasons"]), [])
        # A different operation's basename fails, even with otherwise valid content.
        other = self.tmp / "expiry_probe_result_expop_someone_else.json"
        verdict = self._json("authoritative", Text=str(other), CtxJson=str(record_path))
        self.assertFalse(verdict["authoritative"])
        reasons = as_list(verdict["reasons"])
        self.assertIn("basename_not_authoritative", reasons)
        self.assertIn("operation_id_basename_mismatch", reasons)
        # A staging basename fails even though the bytes are the same.
        staging = self.tmp / ("expiry_probe_staging_%s.incomplete" % operation_id)
        verdict = self._json("authoritative", Text=str(staging), CtxJson=str(record_path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("artefact_is_staging", as_list(verdict["reasons"]))

    def test_authoritative_validator_rejects_inconsistent_records(self):
        operation_id = "expop_validator02"
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)

        contradictory = verified_record(operation_id)
        contradictory["claim_conflict"] = True
        path = self._record_file("validator_contradiction.json", contradictory)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("state_contradiction", as_list(verdict["reasons"]))

        unpersisted = verified_record(operation_id)
        unpersisted["evidence_persisted"] = False
        path = self._record_file("validator_unpersisted.json", unpersisted)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("evidence_not_persisted", as_list(verdict["reasons"]))

        wrong_contract = verified_record(operation_id)
        wrong_contract["publication_contract"]["publication_contract_version"] = "something/v0"
        path = self._record_file("validator_contract.json", wrong_contract)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("publication_contract_version_mismatch", as_list(verdict["reasons"]))

        no_contract = verified_record(operation_id)
        del no_contract["publication_contract"]
        path = self._record_file("validator_nocontract.json", no_contract)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("publication_contract_missing", as_list(verdict["reasons"]))

        wrong_exit = verified_record(operation_id)
        wrong_exit["exit_code"] = 1
        path = self._record_file("validator_exit.json", wrong_exit)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("exit_code_inconsistent", as_list(verdict["reasons"]))


@unittest.skipIf(PS is None, "no PowerShell executable available")
class ExpiryProbeScriptExecutionTests(unittest.TestCase):
    """Runs the probe so it always stops before any AutoCount assembly load.

    Runs with a VALID approval reference reach the canonical state-root check, so they are
    skipped on a machine where that root exists (see CANONICAL_ROOT_SKIP).
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = dict(os.environ)
        self.env["AC2_PROBE_PASSWORD"] = ""  # force a pre-AutoCount stop for active runs

    def _run(self, *args, **kw):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(SCRIPT), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=self.env, **kw)

    def _active_args(self, appref="APPROVAL-TEST-001"):
        return ("-EnableExpiryCapabilityProbe", "-ConfirmSyntheticExpiryDateTest",
                "-ConfirmSingleSyntheticMember", "-ConfirmAutoCountWrite",
                "-ConfirmDryRunPreflightPassed", "-ConfirmNoUpdateOrDelete",
                "-ApprovalReference", appref,
                "-ServerName", "SYN_SERVER", "-DatabaseName", "SYN_DB", "-UserId", "SYN_USER",
                "-AcRoot", "C:\\NoSuchAcRoot")

    def test_removed_parameters_are_rejected_by_the_parameter_binder(self):
        # Passing the removed switches must fail: there is no state-directory or JSON-output
        # parameter to bind, so no operator-selected path can reach the probe.
        for removed in ("-StateDirectory", "-JsonOut"):
            proc = self._run(*self._active_args(), removed, str(self.tmp))
            self.assertNotEqual(proc.returncode, 0, removed)
            self.assertNotIn('"terminal_outcome"', proc.stdout, removed)

    def test_approval_reference_containing_target_fails_closed(self):
        proc = self._run(*self._active_args(appref="XB-SYN_DB-01"))  # embeds the database name
        self.assertNotEqual(proc.returncode, 0)
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "FAILED_BEFORE_WRITE")
        self.assertFalse(r["required_assemblies_loaded"])
        self.assertFalse(r["autocount_contacted"])
        self.assertFalse(r["claim_created"])
        self.assertFalse(r["state_root_trusted"])
        # Rejected before the claim namespace is even derived.
        self.assertIsNone(r["claim_basename"])
        self.assertIsNone(r["publication_contract"])
        # The rejected target-bearing reference must NOT be retained or emitted.
        self.assertIsNone(r["approval_reference"])
        self.assertNotIn("SYN_DB", proc.stdout)

    def test_approval_reference_bad_characters_fail_closed(self):
        proc = self._run(*self._active_args(appref="XB:BAD/REF"))  # forbidden characters
        self.assertNotEqual(proc.returncode, 0)
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "FAILED_BEFORE_WRITE")
        self.assertFalse(r["claim_created"])
        self.assertFalse(r["required_assemblies_loaded"])
        self.assertFalse(r["autocount_contacted"])

    def test_refusal_is_nonzero_with_clean_json_stdout(self):
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(proc.stdout.lstrip().startswith("{"), proc.stdout[:120])
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "REFUSED")
        self.assertFalse(r["activated"])
        self.assertFalse(r["autocount_contacted"])
        self.assertFalse(r["evidence_persisted"])
        self.assertEqual(r["exit_code"], proc.returncode)

    @unittest.skipIf(CANONICAL_ROOT_PRESENT, CANONICAL_ROOT_SKIP)
    def test_untrusted_canonical_root_fails_closed_before_any_contact(self):
        proc = self._run(*self._active_args())
        self.assertNotEqual(proc.returncode, 0)
        r = json.loads(proc.stdout)
        self.assertEqual(r["terminal_outcome"], "CLAIM_ROOT_UNAVAILABLE")
        self.assertEqual(r["underlying_terminal_outcome"], "CLAIM_ROOT_UNAVAILABLE")
        self.assertFalse(r["state_root_trusted"])
        self.assertTrue(r["claim_root_unavailable"])
        self.assertFalse(r["required_assemblies_loaded"], "no AutoCount assembly may load")
        self.assertFalse(r["autocount_contacted"])
        self.assertFalse(r["initial_member_read_attempted"])
        self.assertFalse(r["member_recheck_attempted"])
        self.assertFalse(r["save_member_attempted"])
        self.assertFalse(r["claim_created"])
        self.assertFalse(r["evidence_persisted"], "no artefact may be created without a trusted root")
        self.assertFalse(r["non_authoritative_staging_may_remain"])
        # A reason code is reported; the raw private path never is.
        reasons = as_list(r["claim_root_failure_reasons"])
        self.assertTrue(reasons)
        expected = "platform_not_windows" if not IS_WINDOWS else "component_missing"
        self.assertIn(expected, reasons, reasons)
        self.assertNotIn(CANONICAL_STATE_ROOT, proc.stdout)
        self.assertNotIn("XB\\\\create_uat", proc.stdout)
        # Evidence binding is present and the claim namespace is already fixed.
        for field in ("operation_id", "approval_reference", "executed_at_utc", "target_fingerprint",
                      "claim_basename", "result_basename", "staging_basename"):
            self.assertTrue(r[field], field)
        self.assertNotIn("SYN_SERVER", proc.stdout)
        self.assertNotIn("SYN_DB", proc.stdout)
        self.assertNotIn("xb.expirydate.probe", proc.stdout)
        self.assertNotIn("XB EXPIRYDATE PROBE", proc.stdout)

    @unittest.skipIf(CANONICAL_ROOT_PRESENT, CANONICAL_ROOT_SKIP)
    def test_working_directory_cannot_change_the_claim_namespace(self):
        first_dir = self.tmp / "wd_one"
        second_dir = self.tmp / "wd_two"
        first_dir.mkdir()
        second_dir.mkdir()
        first = json.loads(self._run(*self._active_args(), cwd=str(first_dir)).stdout)
        second = json.loads(self._run(*self._active_args(), cwd=str(second_dir)).stdout)
        self.assertEqual(first["claim_basename"], second["claim_basename"])
        self.assertEqual(first["attempt_fingerprint"], second["attempt_fingerprint"])
        self.assertEqual(first["terminal_outcome"], "CLAIM_ROOT_UNAVAILABLE")
        self.assertEqual(second["terminal_outcome"], "CLAIM_ROOT_UNAVAILABLE")
        # Per-run identity still differs, so results never collide.
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.assertNotEqual(first["result_basename"], second["result_basename"])

    @unittest.skipIf(CANONICAL_ROOT_PRESENT, CANONICAL_ROOT_SKIP)
    def test_no_artefact_is_written_anywhere_outside_the_canonical_root(self):
        before = sorted(p.name for p in self.tmp.iterdir())
        self._run(*self._active_args(), cwd=str(self.tmp))
        after = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(before, after, "the probe must not write evidence into the working directory")


class ExpiryProbeRunbookAndCiTests(unittest.TestCase):
    def setUp(self):
        self.runbook = RUNBOOK.read_text(encoding="utf-8")
        self.readme = README.read_text(encoding="utf-8")
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

    def test_runbook_documents_current_controls(self):
        self.assertIn("-ApprovalReference", self.runbook)
        self.assertIn("WRITE_CONFIRMED_READBACK_FAILED", self.runbook)
        self.assertIn("ATTEMPT_ALREADY_CLAIMED", self.runbook)
        self.assertRegex(self.runbook, r"(?i)permanent single-use\s+attempt claim")
        self.assertRegex(self.runbook, r"(?i)exit code")
        self.assertRegex(self.runbook, r"(?i)never removes the attempt claim")
        for outcome in ("CLAIM_PERSISTENCE_FAILED", "EVIDENCE_PERSISTENCE_FAILED",
                        "CLAIM_ROOT_UNAVAILABLE", "ATTEMPT_CLAIM_LOST_AFTER_CONTACT"):
            self.assertIn(outcome, self.runbook, outcome)

    def test_runbook_documents_the_fixed_canonical_state_root(self):
        self.assertIn(CANONICAL_STATE_ROOT, self.runbook)
        self.assertRegex(self.runbook, r"(?i)fixed canonical state root")
        self.assertRegex(self.runbook, r"(?i)not selectable")
        self.assertRegex(self.runbook, r"(?i)no `-StateDirectory` and no `-JsonOut`")
        self.assertRegex(self.runbook, r"(?i)never creates, repairs, migrates, cleans or\s+redirects it")
        self.assertRegex(self.runbook, r"(?i)junction, symbolic link or other reparse point")

    def test_runbook_probe_command_passes_no_path_parameters(self):
        # The operator-facing invocation must not reintroduce a selectable path.
        commands = [block for block in re.findall(r"```powershell\n(.*?)```", self.runbook, re.S)
                    if "ac2_member_expiry_capability_probe.ps1 -EnableExpiryCapabilityProbe" in block]
        self.assertEqual(len(commands), 1, "exactly one probe invocation command is expected")
        command = commands[0]
        self.assertIn("-ApprovalReference", command)
        self.assertNotIn("-StateDirectory", command)
        self.assertNotIn("-JsonOut", command)

    def test_runbook_documents_the_publication_contract_and_validator(self):
        for token in ("expiry_probe_staging_<operation_id>.incomplete",
                      "authoritative_result_basename", "publication_contract_version",
                      "authority_rule", "Test-ExpiryProbeAuthoritativeResult",
                      "non_authoritative_staging_may_remain"):
            self.assertIn(token, self.runbook, token)
        self.assertRegex(self.runbook, r"(?i)left exactly as written")
        self.assertRegex(self.runbook, r"(?i)do \*\*not\*\* rely on a filename glob alone")

    def test_runbook_host_sync_gate_precedes_the_pull_command(self):
        gate_idx = self.runbook.index("host-sync gate")
        pull_idx = self.runbook.index("git pull --ff-only origin main")
        self.assertLess(gate_idx, pull_idx, "the host-sync approval gate must precede the pull command")
        # The gate names the host and the operation.
        gate_text = self.runbook[gate_idx:pull_idx]
        self.assertIn("DESKTOP-Q43QKQF", gate_text)
        self.assertRegex(gate_text, r"(?i)pull/sync operation")
        self.assertRegex(gate_text, r"(?i)current-turn owner approval")
        # And it states that no later approval covers it.
        self.assertRegex(gate_text, r"(?i)deployment approval \(stage 3\) does \*\*not\*\* cover this host sync")
        self.assertRegex(gate_text, r"(?i)preflight approval \(stage 4\) does \*\*not\*\* cover this host sync")
        self.assertRegex(gate_text, r"(?i)`SaveMember` write approval \(stage 5\) does \*\*not\*\* cover this host sync")

    def test_runbook_has_distinct_bounded_deployment_stage(self):
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
        self.assertRegex(self.runbook, r"(?i)not sufficient and is unsafe")
        self.assertRegex(self.runbook, r"(?i)could save a member with")
        self.assertIn("CREATED_VERIFIED", self.runbook)
        self.assertIn("Wire the runner", self.runbook)
        self.assertRegex(self.runbook, r"(?i)read-back")
        self.assertIn("member_create_uat_approval.py", self.runbook)

    def test_runbook_has_separate_approval_gates_for_each_external_action(self):
        self.assertRegex(self.runbook, r"(?i)host-sync gate")
        self.assertRegex(self.runbook, r"(?i)deployment gate")
        self.assertRegex(self.runbook, r"(?i)preflight gate")
        self.assertRegex(self.runbook, r"(?i)destructive mutation of live AutoCount")
        self.assertRegex(self.runbook, r"(?i)carry over\s+to deletion")
        self.assertRegex(self.runbook, r"(?i)does \*\*not\*\* authorise it|does \*\*not\*\* cover it")

    def test_runbook_requires_opaque_approval_reference(self):
        self.assertIn("<opaque-approval-id>", self.runbook)
        self.assertNotIn("approval-ref-naming-target", self.runbook)
        self.assertRegex(self.runbook, r"(?i)opaque, non-secret approval identifier")
        self.assertRegex(self.runbook, r"(?i)put the server or database")

    def test_runbook_cross_reference_to_the_main_create_uat_runbook_resolves(self):
        # The probe runbook links the main single-member creation UAT runbook by relative
        # path; that target must exist (its contents are owned by the create-UAT lane).
        self.assertIn("(member_create_uat_runbook.md)", self.runbook)
        self.assertTrue(CREATE_UAT_RUNBOOK.is_file())
        self.assertTrue(CREATE_UAT_RUNBOOK.read_text(encoding="utf-8").strip())

    def test_readme_references_probe_and_runbook(self):
        self.assertIn("scripts/ac2_member_expiry_capability_probe.ps1", self.readme)
        self.assertIn("member_expiry_capability_probe_runbook.md", self.readme)

    def test_workflow_triggers_on_validated_documents(self):
        paths_block = self.workflow.split("paths:", 1)[1].split("workflow_dispatch", 1)[0]
        for needed in ("README.md",
                       "docs/autocount2-automation/member_expiry_capability_probe_runbook.md",
                       "docs/autocount2-automation/member_create_uat_runbook.md",
                       "scripts/member_expiry_capability_probe_lib.ps1",
                       "tests/test_ac2_member_expiry_capability_probe.py",
                       ".gitignore"):
            self.assertIn(needed, paths_block, needed)

    # ---- Mechanical focused-CI dependency closure ---- #
    def test_dependency_inventory_covers_every_known_contract_file(self):
        inventory = module_repo_dependencies(SELF)
        for required in (".gitignore",
                         ".github/workflows/member-create-uat-tests.yml",
                         "tests/test_ac2_member_expiry_capability_probe.py",
                         "scripts/ac2_member_expiry_capability_probe.ps1",
                         "scripts/member_expiry_capability_probe_lib.ps1",
                         "docs/autocount2-automation/member_expiry_capability_probe_runbook.md",
                         "docs/autocount2-automation/member_create_uat_runbook.md",
                         "README.md"):
            self.assertIn(required, inventory, required)

    def test_focused_workflow_filter_closes_over_every_dependency(self):
        filters = workflow_path_filters(self.workflow)
        self.assertTrue(filters, "the focused workflow must declare at least one path filter")
        # paths-ignore would invert the semantics this closure relies on.
        self.assertNotIn("paths-ignore", self.workflow)
        inventory = module_repo_dependencies(SELF)
        for event, patterns in filters.items():
            missing = uncovered_dependencies(inventory, patterns)
            self.assertEqual(missing, [],
                             "event '%s' does not trigger for: %s" % (event, missing))

    def test_closure_assertion_fails_when_a_required_path_is_dropped(self):
        # Negative control: the closure check must actually bite. Removing one required
        # filter entry from a fixture copy of the workflow must be detected.
        fixture = "\n".join(line for line in self.workflow.splitlines()
                            if line.strip() != '- ".gitignore"')
        self.assertNotEqual(fixture, self.workflow, "the fixture must differ from the real workflow")
        filters = workflow_path_filters(fixture)
        self.assertTrue(filters)
        inventory = module_repo_dependencies(SELF)
        for patterns in filters.values():
            self.assertIn(".gitignore", uncovered_dependencies(inventory, patterns))

    def test_glob_matcher_respects_separator_boundaries(self):
        self.assertTrue(glob_to_regex("tests/test_member_create_uat_*.py").match("tests/test_member_create_uat_x.py"))
        self.assertFalse(glob_to_regex("tests/test_member_create_uat_*.py").match("tests/sub/test_member_create_uat_x.py"))
        self.assertTrue(glob_to_regex("a/**/b.py").match("a/x/y/b.py"))
        self.assertTrue(glob_to_regex(".gitignore").match(".gitignore"))
        self.assertFalse(glob_to_regex(".gitignore").match("sub/.gitignore"))


@unittest.skipIf(PS is None, "no PowerShell executable available")
class ExpiryProbeAstTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.inspector = cls.tmp / "expiry_inspector.ps1"
        cls.inspector.write_text(INSPECTOR, encoding="utf-8")

    def _inspect(self, target=SCRIPT):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(self.inspector), "-Path", str(target)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_parameter_block_exposes_no_path_or_output_override(self):
        info = self._inspect()
        self.assertEqual(info["parseErrors"], 0)
        names = as_list(info["paramNames"])
        self.assertIn("ApprovalReference", names)
        for forbidden in ("StateDirectory", "JsonOut"):
            self.assertNotIn(forbidden, names, forbidden)
        # No parameter of any name may select a claim/result location or a secondary output.
        for name in names:
            lowered = name.lower()
            for fragment in ("statedir", "stateroot", "claimdir", "claimroot", "resultdir",
                             "resultpath", "jsonout", "outfile", "outpath", "evidence", "staging"):
                self.assertNotIn(fragment, lowered, name)
        # The library's injectable helpers are never bound to a script parameter.
        script_text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Test-ExpiryProbeTrustedStateRoot -Root $script:ExpiryProbeStateRoot", script_text)
        self.assertEqual(script_text.count("Test-ExpiryProbeTrustedStateRoot"), 1)
        self.assertEqual(script_text.count("-Root $script:ExpiryProbeStateRoot"), 2)
        self.assertNotIn("-MoveAction", script_text)

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

    def test_no_artefact_removal_in_script_or_library(self):
        for target in (SCRIPT, LIB):
            info = self._inspect(target)
            self.assertEqual(info["parseErrors"], 0, str(target))
            self.assertEqual(info["removeItemCount"], 0, "no Remove-Item may exist in %s" % target.name)
        # The library's only System.IO.File mutation is the single no-replace publication
        # move: never a Delete and never a Replace (which would clobber prior evidence).
        lib_info = self._inspect(LIB)
        self.assertEqual(lib_info["fileMoveCount"], 1)
        self.assertEqual(lib_info["fileDeleteCount"], 0)
        script_info = self._inspect(SCRIPT)
        self.assertEqual(script_info["fileMoveCount"], 0)
        self.assertEqual(script_info["fileDeleteCount"], 0)


if __name__ == "__main__":
    unittest.main()
