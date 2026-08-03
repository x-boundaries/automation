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
* Repository dependencies are a CLOSED contract: ``REPO_DEPENDENCIES`` is the single
  immutable registry, ``repo_path``/``read_repo_text`` are the only sanctioned readers, and
  ``repository_read_violations`` is an independent AST guard that fails on any repository read
  or path derivation escaping them — including unresolvable ones, which are reported rather
  than silently dropped from the inventory. Every registered dependency must also be covered
  by every workflow ``paths`` filter.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---- A2-3: closed repository dependency contract ---- #
# The single explicit, immutable registry of every repository file this module reads or
# semantically asserts. Reads are routed through repo_path()/read_repo_text(), and an
# INDEPENDENT AST guard (repository_read_violations) fails the suite on any repository read or
# path derivation that escapes them. Discovery is therefore fail-closed: an unresolved or
# unrecognised read is an error, never a silently missing inventory entry.
REPO_DEPENDENCIES = types.MappingProxyType({
    "probe_script": "scripts/ac2_member_expiry_capability_probe.ps1",
    "probe_lib": "scripts/member_expiry_capability_probe_lib.ps1",
    "probe_runbook": "docs/autocount2-automation/member_expiry_capability_probe_runbook.md",
    "create_uat_runbook": "docs/autocount2-automation/member_create_uat_runbook.md",
    "readme": "README.md",
    "gitignore": ".gitignore",
    "workflow": ".github/workflows/member-create-uat-tests.yml",
    "focused_tests": "tests/test_ac2_member_expiry_capability_probe.py",
})


def repo_path(key):
    """Resolve a REGISTERED dependency key to its path. An unregistered key fails closed."""
    if key not in REPO_DEPENDENCIES:
        raise KeyError("unregistered repository dependency key: %r" % (key,))
    return ROOT / REPO_DEPENDENCIES[key]


def read_repo_text(key):
    """The only sanctioned repository text read; accepts a literal registered key only."""
    return repo_path(key).read_text(encoding="utf-8")


def read_scratch_text(path):
    """Read a NON-repository (temporary) file. Fails closed on any repository path."""
    resolved = Path(path).resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise AssertionError("the scratch reader refuses a repository path: %s" % resolved)
    return resolved.read_text(encoding="utf-8")


def registered_dependencies():
    """Every registered repository dependency, as repo-relative POSIX paths."""
    return set(REPO_DEPENDENCIES.values())


SCRIPT = repo_path("probe_script")
LIB = repo_path("probe_lib")
RUNBOOK = repo_path("probe_runbook")
CREATE_UAT_RUNBOOK = repo_path("create_uat_runbook")
README = repo_path("readme")
GITIGNORE = repo_path("gitignore")
WORKFLOW = repo_path("workflow")
SELF = repo_path("focused_tests")

# Reads that must resolve to a registered dependency, and the helpers whose own bodies are the
# sanctioned implementations of those reads.
REPO_READ_METHODS = ("read_text", "read_bytes", "open")
SANCTIONED_READ_HELPERS = ("repo_path", "read_repo_text", "read_scratch_text")


def _mentions_root(node):
    return any(isinstance(sub, ast.Name) and sub.id == "ROOT" for sub in ast.walk(node))


# Callables that may resolve a repository path without reading it. Anything not listed here,
# and not a module-level definition or import, counts as an UNRESOLVED callable.
SAFE_CALLABLE_NAMES = frozenset({
    "str", "repr", "len", "int", "float", "bool", "list", "tuple", "set", "dict", "frozenset",
    "sorted", "reversed", "enumerate", "zip", "range", "min", "max", "sum", "any", "all",
    "next", "iter", "print", "isinstance", "getattr_safe", "format", "abs", "id", "type",
})
DYNAMIC_ATTRIBUTE_CALLS = frozenset({"getattr", "attrgetter", "import_module", "__import__"})


def repository_read_violations(source):
    """Fail-closed AST guard: the literal registry reader is the ONLY repository-read route.

    Returns sorted "<line>:<kind>" violations. The policy is deliberately conservative: aliases
    of ``open``, captured bound reader methods, dynamic attribute access, repository-derived
    path taint, wrappers, lambdas, comprehensions and unresolved indirect calls are all
    rejected. Anything that cannot be proven safe is reported rather than accepted.
    """
    tree = ast.parse(source)
    violations = []

    def flag(node, kind):
        violations.append("%d:%s" % (getattr(node, "lineno", 0), kind))

    def is_repo_path_call(node):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "repo_path")

    def literal_registered_key(node):
        args = getattr(node, "args", [])
        return (len(args) == 1 and isinstance(args[0], ast.Constant)
                and isinstance(args[0].value, str) and args[0].value in REPO_DEPENDENCIES)

    # ---- Sanctioned boundary: the exact reviewed top-level helper bodies, nothing else ---- #
    # Nested functions, methods and arbitrary same-named definitions never inherit exemption.
    top_level_helpers = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name in SANCTIONED_READ_HELPERS:
            top_level_helpers.setdefault(stmt.name, []).append(stmt)
    sanctioned_ids = set()
    for defs in top_level_helpers.values():
        if len(defs) == 1:
            for sub in ast.walk(defs[0]):
                sanctioned_ids.add(id(sub))

    # The single module-level ROOT anchor is the one permitted checkout derivation.
    anchor_ids = set()
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "ROOT"):
            for sub in ast.walk(stmt):
                anchor_ids.add(id(sub))

    registered_names = set()
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and is_repo_path_call(stmt.value) and literal_registered_key(stmt.value)):
            registered_names.add(stmt.targets[0].id)

    module_level_defs = {stmt.name for stmt in tree.body
                         if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    imported_names = set()
    open_aliases = {"open"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name.split(".")[0])
                if alias.name == "open":
                    open_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name.split(".")[0])

    tainted_names = {"ROOT"} | set(registered_names)
    reader_names = set()
    funcs_returning_taint = set()
    funcs_returning_reader = set()

    def is_tainted(node):
        if node is None:
            return False
        if isinstance(node, ast.Name):
            return node.id in tainted_names or node.id == "__file__"
        if isinstance(node, ast.Call):
            if is_repo_path_call(node):
                return True
            if isinstance(node.func, ast.Name):
                if node.func.id == "Path":
                    return any(is_tainted(arg) for arg in node.args)
                if node.func.id in funcs_returning_taint:
                    return True
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in ("resolve", "absolute", "expanduser", "joinpath"):
                    return is_tainted(node.func.value)
            return False
        if isinstance(node, ast.Attribute):
            if node.attr in ("parent", "parents"):
                return is_tainted(node.value)
            return False
        if isinstance(node, ast.Subscript):
            return is_tainted(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return is_tainted(node.left) or is_tainted(node.right)
        return False

    def dynamic_attribute_problem(node):
        """True when a dynamic-attribute or dynamic-import call could yield a repository reader.

        Fails closed whenever the attribute name cannot be statically resolved.
        """
        if not isinstance(node, ast.Call):
            return False
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in ("import_module", "__import__"):
            return True
        if name == "getattr":
            args = node.args
            if len(args) < 2:
                return True
            attribute = args[1]
            if not (isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)):
                return True                      # unresolvable attribute name
            return attribute.value in REPO_READ_METHODS or is_tainted(args[0])
        if name == "attrgetter":
            args = node.args
            if not args:
                return True
            first = args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                return True
            return first.value in REPO_READ_METHODS
        return False

    def is_reader(node):
        if node is None:
            return False
        if isinstance(node, ast.Name):
            return node.id in open_aliases or node.id in reader_names
        if isinstance(node, ast.Attribute):
            return node.attr in REPO_READ_METHODS
        if isinstance(node, ast.Call):
            if dynamic_attribute_problem(node):
                return True
            if isinstance(node.func, ast.Name) and node.func.id in funcs_returning_reader:
                return True
            return False
        return False

    def assigned_names(targets):
        names = set()
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
        return names

    # Fixpoint so alias chains and helper returns propagate.
    for _ in range(5):
        before = (len(tainted_names), len(reader_names),
                  len(funcs_returning_taint), len(funcs_returning_reader))
        for node in ast.walk(tree):
            if id(node) in sanctioned_ids:
                continue
            if isinstance(node, ast.Assign):
                if is_tainted(node.value):
                    tainted_names |= assigned_names(node.targets)
                if is_reader(node.value):
                    reader_names |= assigned_names(node.targets)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return):
                        if is_tainted(sub.value):
                            funcs_returning_taint.add(node.name)
                        if is_reader(sub.value):
                            funcs_returning_reader.add(node.name)
            elif isinstance(node, ast.Lambda):
                if is_reader(node.body) or is_tainted(node.body):
                    pass  # reported directly in the violation pass
        after = (len(tainted_names), len(reader_names),
                 len(funcs_returning_taint), len(funcs_returning_reader))
        if before == after:
            break

    def receiver_is_registered(node):
        if isinstance(node, ast.Name):
            return node.id in registered_names
        if is_repo_path_call(node):
            return literal_registered_key(node)
        return False

    def callable_is_resolved(func):
        if isinstance(func, ast.Attribute):
            return True                      # a method/module call, scanned on its own merits
        if isinstance(func, ast.Name):
            return (func.id in SAFE_CALLABLE_NAMES or func.id in module_level_defs
                    or func.id in imported_names or func.id in SANCTIONED_READ_HELPERS
                    or func.id == "Path")
        return False

    # Mark attributes that are the callee of a call, so a bare reference to a reader method
    # (capture, storage, passing, returning) is distinguishable from an immediate call.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            setattr(node.func, "_xb_parent_call", node)

    for node in ast.walk(tree):
        if id(node) in sanctioned_ids or id(node) in anchor_ids:
            continue

        # ---- Aliases of the built-in reader ---- #
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "open":
                    flag(node, "open_alias_import")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in open_aliases:
            flag(node, "builtin_open")

        # ---- Bound path-reader methods: captured, passed, stored or called unsafely ---- #
        if isinstance(node, ast.Attribute) and node.attr in REPO_READ_METHODS:
            parent_call = getattr(node, "_xb_parent_call", None)
            if parent_call is None:
                flag(node, "bound_reader_capture")

        # ---- Dynamic attribute access ---- #
        if isinstance(node, ast.Call):
            func = node.func
            if dynamic_attribute_problem(node):
                flag(node, "dynamic_attribute_access")

            # ---- Registry helper keys ---- #
            if isinstance(func, ast.Name) and func.id in ("repo_path", "read_repo_text"):
                if not literal_registered_key(node):
                    args = getattr(node, "args", [])
                    if len(args) == 1 and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                        flag(node, "unregistered_dependency_key")
                    else:
                        flag(node, "dynamic_dependency_key")

            # ---- Reads ---- #
            if isinstance(func, ast.Attribute) and func.attr in REPO_READ_METHODS:
                if not receiver_is_registered(func.value):
                    flag(node, "unresolved_repository_read")
            if isinstance(func, ast.Name) and func.id in reader_names:
                flag(node, "reader_callable_invocation")
            if isinstance(func, ast.Name) and func.id == "Path" and any(is_tainted(a) for a in node.args):
                flag(node, "path_constructor_from_root")
            if isinstance(func, ast.Attribute) and func.attr == "joinpath" and is_tainted(func.value):
                flag(node, "root_joinpath")

            # ---- Escapes into unresolved callables ---- #
            if not callable_is_resolved(func):
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if is_reader(arg):
                        flag(node, "reader_callable_escape")
                    elif is_tainted(arg):
                        flag(node, "repository_path_escape")
            else:
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if is_reader(arg) and not (isinstance(func, ast.Name) and func.id in SANCTIONED_READ_HELPERS):
                        flag(node, "reader_callable_escape")

        # ---- Repository-derived path arithmetic ---- #
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and is_tainted(node.left):
            flag(node, "root_path_derivation")

        # ---- Escaping returns ---- #
        if isinstance(node, ast.Return):
            if is_reader(node.value):
                flag(node, "reader_callable_escape")
            elif is_tainted(node.value):
                flag(node, "repository_path_escape")

        # ---- Lambdas that read or hand back a reader ---- #
        if isinstance(node, ast.Lambda):
            if is_reader(node.body):
                flag(node, "reader_callable_escape")

    return sorted(set(violations))

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


# ---- A3-1: the closed authoritative-result schema ---- #
# These categories mirror the reviewed $result object emitted by the unchanged probe script.
# test_library_schema_matches_the_probe_result_contract proves the library's declared schema and
# this expectation both still match that script exactly.
AUTHORITATIVE_BOOLEAN_FIELDS = (
    "state_root_trusted", "claim_root_unavailable", "activated",
    "confirm_synthetic_expiry_test", "confirm_single_synthetic", "confirm_auto_count_write",
    "confirm_dry_run_preflight", "confirm_no_update_or_delete", "ac_root_exists",
    "required_assemblies_loaded", "autocount_contacted", "authentication_success",
    "member_command_found", "get_member_found", "initial_member_read_attempted",
    "member_exists_initial", "new_member_success", "assignment_success", "expiry_date_assigned",
    "member_recheck_attempted", "member_exists_recheck", "claim_created", "claim_conflict",
    "claim_lost_after_contact", "claim_persist_failed", "save_member_method_found",
    "save_member_attempted", "save_member_confirmed", "readback_found", "expiry_match",
    "synthetic_member_may_remain", "evidence_persisted", "non_authoritative_staging_may_remain",
)
AUTHORITATIVE_STRING_FIELDS = (
    "schema_version", "mode", "operation_id", "approval_reference", "executed_at_utc",
    "target_fingerprint", "synthetic_fingerprint", "attempt_fingerprint",
    "intended_expiry_date", "claim_basename", "result_basename", "staging_basename",
    "save_outcome", "masked_member_no", "residual_record_note",
    "underlying_terminal_outcome", "terminal_outcome",
)
AUTHORITATIVE_NULLABLE_STRING_FIELDS = ("readback_error", "expiry_date_readback_value")
AUTHORITATIVE_INTEGRAL_FIELDS = ("exit_code",)
AUTHORITATIVE_ARRAY_FIELDS = ("claim_root_failure_reasons",)
AUTHORITATIVE_OBJECT_FIELDS = ("publication_contract",)
AUTHORITATIVE_NULLABLE_OBJECT_FIELDS = ("error",)

AUTHORITATIVE_TOP_LEVEL_FIELDS = (
    AUTHORITATIVE_BOOLEAN_FIELDS + AUTHORITATIVE_STRING_FIELDS
    + AUTHORITATIVE_NULLABLE_STRING_FIELDS + AUTHORITATIVE_INTEGRAL_FIELDS
    + AUTHORITATIVE_ARRAY_FIELDS + AUTHORITATIVE_OBJECT_FIELDS
    + AUTHORITATIVE_NULLABLE_OBJECT_FIELDS
)

PUBLICATION_CONTRACT_FIELDS = (
    "publication_contract_version", "authoritative_result_basename", "authority_rule",
)

AUTHORITY_RULE_TEXT = (
    "This artefact is authoritative ONLY when its current file basename is exactly equal to "
    "authoritative_result_basename. Any other basename, including an "
    "expiry_probe_staging_<operation_id>.incomplete staging artefact, is NON-AUTHORITATIVE "
    "regardless of the terminal_outcome, evidence_persisted or exit_code it contains."
)


def verified_record(operation_id="expop_authoritative01"):
    """A complete, correctly typed, internally consistent authoritative EXPIRY_VERIFIED record.

    Every top-level field the reviewed probe emits is present with its real CLR type: actual
    Booleans, an actual integral exit code and actual strings. Adversarial tests mutate exactly
    one field at a time from this baseline.
    """
    final_basename = "expiry_probe_result_%s.json" % operation_id
    record = {
        "schema_version": SCHEMA_VERSION,
        "mode": "member-expiry-capability-probe",
        "operation_id": operation_id,
        "approval_reference": "APPROVAL-TEST-001",
        "executed_at_utc": "2026-08-03T00:00:00Z",
        "target_fingerprint": "tfp_" + ("a" * 64),
        "synthetic_fingerprint": "smf_" + ("b" * 64),
        "attempt_fingerprint": "afp_" + ("c" * 64),
        "intended_expiry_date": "2028-06-30",
        "claim_basename": "expiry_probe_claim_afp_%s.claim" % ("c" * 64),
        "result_basename": final_basename,
        "staging_basename": "expiry_probe_staging_%s.incomplete" % operation_id,
        "publication_contract": {
            "publication_contract_version": PUBLICATION_CONTRACT_VERSION,
            "authoritative_result_basename": final_basename,
            "authority_rule": AUTHORITY_RULE_TEXT,
        },
        "claim_root_failure_reasons": [],
        "save_outcome": "confirmed",
        "masked_member_no": "XB***1",
        "residual_record_note": "No automatic member update, delete, rollback, or cleanup is performed.",
        "readback_error": None,
        "expiry_date_readback_value": "2028-06-30",
        "underlying_terminal_outcome": "EXPIRY_VERIFIED",
        "terminal_outcome": "EXPIRY_VERIFIED",
        "exit_code": 0,
        "error": None,
    }
    true_flags = (
        "state_root_trusted", "activated", "confirm_synthetic_expiry_test",
        "confirm_single_synthetic", "confirm_auto_count_write", "confirm_dry_run_preflight",
        "confirm_no_update_or_delete", "ac_root_exists", "required_assemblies_loaded",
        "autocount_contacted", "authentication_success", "member_command_found",
        "get_member_found", "initial_member_read_attempted", "new_member_success",
        "assignment_success", "expiry_date_assigned", "member_recheck_attempted",
        "claim_created", "save_member_method_found", "save_member_attempted",
        "save_member_confirmed", "readback_found", "expiry_match",
        "synthetic_member_may_remain", "evidence_persisted",
    )
    for field in AUTHORITATIVE_BOOLEAN_FIELDS:
        record[field] = field in true_flags
    return record


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
        # The production write-through rename is Windows-only and fails closed elsewhere, so off
        # Windows this portable test drives the same preflight and no-clobber staging contract
        # through the sanctioned pure-test move seam. The real native publication is covered by
        # the Windows-only nativemove and nativemoveraw tests.
        if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
            Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content $content
        }
        else {
            Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content $content `
                -MoveAction { param($s, $d) [System.IO.File]::Move($s, $d) }
        }
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
    'schemamatrix' {
        # A3-1 adversarial type matrix, executed in ONE process. $CtxJson = a complete, valid
        # baseline record; $Text = comma-separated field names; $Extra = candidate final path.
        $baseText = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        $bad = New-Object object[] 9
        $bad[0] = 'false'; $bad[1] = 'true'; $bad[2] = 0; $bad[3] = 1; $bad[4] = $null
        $bad[5] = @(); $bad[6] = @(1, 2); $bad[7] = @{}; $bad[8] = @{ injected = 1 }
        $variant = @('string_false', 'string_true', 'int_zero', 'int_one', 'null',
                     'empty_array', 'array', 'empty_object', 'object')
        $results = New-Object System.Collections.Generic.List[object]
        foreach ($field in $Text.Split(',')) {
            for ($i = 0; $i -lt $bad.Count; $i++) {
                $record = $baseText | ConvertFrom-Json
                $record.$field = $bad[$i]
                $v = Test-ExpiryProbeAuthoritativeResult -Path $Extra -Record $record
                $results.Add([pscustomobject]@{
                    field = $field; variant = $variant[$i]
                    authoritative = [bool]$v.authoritative; reasons = @($v.reasons)
                })
            }
        }
        ConvertTo-Json -InputObject $results.ToArray() -Compress -Depth 5
    }
    'exitcodematrix' {
        # $CtxJson = valid baseline; $Extra = candidate final path.
        $baseText = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        $bad = New-Object object[] 9
        $bad[0] = '0'; $bad[1] = '1'; $bad[2] = $true; $bad[3] = $false; $bad[4] = 0.0
        # Multi-element arrays: PowerShell unrolls a single-element array on property
        # assignment, so @(0) would reach the validator as a scalar and prove nothing.
        $bad[5] = 1.5; $bad[6] = $null; $bad[7] = @(0, 1); $bad[8] = 2
        $variant = @('string_zero', 'string_one', 'boolean_true', 'boolean_false', 'float_zero',
                     'float', 'null', 'array', 'out_of_range')
        $results = New-Object System.Collections.Generic.List[object]
        for ($i = 0; $i -lt $bad.Count; $i++) {
            $record = $baseText | ConvertFrom-Json
            $record.exit_code = $bad[$i]
            $v = Test-ExpiryProbeAuthoritativeResult -Path $Extra -Record $record
            $results.Add([pscustomobject]@{
                variant = $variant[$i]; authoritative = [bool]$v.authoritative; reasons = @($v.reasons)
            })
        }
        ConvertTo-Json -InputObject $results.ToArray() -Compress -Depth 5
    }
    'stringtypematrix' {
        # Every authority-relevant string field replaced by a non-string of each shape.
        $baseText = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        $bad = New-Object object[] 6
        # Multi-element array for the same unrolling reason as the exit-code matrix.
        $bad[0] = 1; $bad[1] = $true; $bad[2] = $null; $bad[3] = @('x', 'y'); $bad[4] = @{ a = 1 }; $bad[5] = ''
        $variant = @('integer', 'boolean', 'null', 'array', 'object', 'empty_string')
        $results = New-Object System.Collections.Generic.List[object]
        foreach ($field in $Text.Split(',')) {
            for ($i = 0; $i -lt $bad.Count; $i++) {
                $record = $baseText | ConvertFrom-Json
                $record.$field = $bad[$i]
                $v = Test-ExpiryProbeAuthoritativeResult -Path $Extra -Record $record
                $results.Add([pscustomobject]@{
                    field = $field; variant = $variant[$i]
                    authoritative = [bool]$v.authoritative; reasons = @($v.reasons)
                })
            }
        }
        ConvertTo-Json -InputObject $results.ToArray() -Compress -Depth 5
    }
    'datetimerecord' {
        # PowerShell 7's ConvertFrom-Json converts ISO-8601 text to [datetime] while Windows
        # PowerShell 5.1 leaves it as [string]. This op reproduces the 7.x shape on ANY host so
        # the cross-version contract is covered locally, not only in hosted CI.
        $record = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $record.executed_at_utc = [datetime]::SpecifyKind([datetime]::ParseExact(
            '2026-08-03T00:00:00Z', 'yyyy-MM-ddTHH:mm:ssZ',
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AdjustToUniversal), [System.DateTimeKind]::Utc)
        $record.intended_expiry_date = [datetime]::ParseExact('2028-06-30', 'yyyy-MM-dd',
            [System.Globalization.CultureInfo]::InvariantCulture)
        $record.expiry_date_readback_value = [datetime]::ParseExact('2028-06-30', 'yyyy-MM-dd',
            [System.Globalization.CultureInfo]::InvariantCulture)
        $v = Test-ExpiryProbeAuthoritativeResult -Path $Extra -Record $record
        [pscustomobject]@{
            authoritative = [bool]$v.authoritative
            reasons = @($v.reasons)
            executedType = $record.executed_at_utc.GetType().Name
        } | ConvertTo-Json -Compress -Depth 4
    }
    'schemashape' {
        # Missing/unknown top-level and publication-contract fields. $Text selects the case.
        $record = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8 | ConvertFrom-Json
        switch ($Text) {
            'missing_top' { $record.PSObject.Properties.Remove('activated') }
            'unknown_top' { $record | Add-Member -NotePropertyName 'injected_field' -NotePropertyValue 'x' }
            'missing_contract' { $record.publication_contract.PSObject.Properties.Remove('authority_rule') }
            'unknown_contract' { $record.publication_contract | Add-Member -NotePropertyName 'injected' -NotePropertyValue 'x' }
            'scalar_contract' { $record.publication_contract = 'not-an-object' }
            'null_contract' { $record.publication_contract = $null }
            'array_contract' { $record.publication_contract = @(1, 2) }
        }
        $v = Test-ExpiryProbeAuthoritativeResult -Path $Extra -Record $record
        [pscustomobject]@{ authoritative = [bool]$v.authoritative; reasons = @($v.reasons) } | ConvertTo-Json -Compress -Depth 4
    }
    'schemafields' {
        # The library's declared closed top-level schema, for comparison against the script.
        [pscustomobject]@{
            topLevel = @($script:ExpiryProbeAuthoritativeTopLevelFields)
            booleans = @($script:ExpiryProbeAuthoritativeBooleanFields)
            contract = @($script:ExpiryProbeAuthoritativePublicationFields)
        } | ConvertTo-Json -Compress -Depth 4
    }
    'publishpaths' {
        # A3-2 path preflight. $Text = "<stagingPath>|<finalPath>"; content is never written
        # when the preflight rejects.
        $parts = $Text.Split('|')
        $threw = $false
        $message = ''
        try { Publish-ExpiryProbeResultAtomic -StagingPath $parts[0] -FinalPath $parts[1] -Content 'bytes' `
                -MoveAction { param($s, $d) throw 'the move must never be reached' } }
        catch { $threw = $true; $message = $_.Exception.Message }
        [pscustomobject]@{
            threw = $threw
            stagingCreated = (Test-Path -LiteralPath $parts[0])
            finalCreated = (Test-Path -LiteralPath $parts[1])
            message = $message
        } | ConvertTo-Json -Compress
    }
    'nativemove' {
        # A3-2 real Windows write-through publication between two temporary same-directory
        # paths. Never the canonical root: $Dir is always a test temporary directory.
        $stg = Join-Path $Dir (Get-ExpiryProbeStagingBasename -OperationId $Text)
        $fin = Join-Path $Dir (Get-ExpiryProbeResultBasename -OperationId $Text)
        $content = Get-Content -LiteralPath $CtxJson -Raw -Encoding UTF8
        $published = $false
        $publishError = ''
        try { Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content $content; $published = $true }
        catch { $publishError = $_.Exception.Message }
        # A second publication against the now-existing destination must fail without replacing.
        $secondBlocked = $false
        try { Publish-ExpiryProbeResultAtomic -StagingPath $stg -FinalPath $fin -Content 'replacement bytes' }
        catch { $secondBlocked = $true }
        $finalOperationId = ''
        if (Test-Path -LiteralPath $fin) {
            $finalOperationId = (Get-Content -LiteralPath $fin -Raw -Encoding UTF8 | ConvertFrom-Json).operation_id
        }
        [pscustomobject]@{
            published = $published
            publishError = $publishError
            finalExists = (Test-Path -LiteralPath $fin)
            stagingLeft = (Test-Path -LiteralPath $stg)
            secondBlocked = $secondBlocked
            finalOperationId = $finalOperationId
        } | ConvertTo-Json -Compress
    }
    'nativemoveraw' {
        # Direct native-layer proof that the write-through move is NO-REPLACE: with the
        # destination already present the API must fail and leave both files untouched. This is
        # the guarantee that remains authoritative against a race after the preflight.
        Initialize-ExpiryProbeNativePublicationApi
        $src = Join-Path $Dir 'native_source.tmp'
        $dst = Join-Path $Dir 'native_destination.tmp'
        Set-Content -LiteralPath $src -Value 'source bytes' -NoNewline -Encoding UTF8
        Set-Content -LiteralPath $dst -Value 'destination bytes' -NoNewline -Encoding UTF8
        $blocked = [XbExpiryProbe.NativePublication]::MoveNoReplaceWriteThrough($src, $dst)
        $fresh = Join-Path $Dir 'native_fresh.tmp'
        $allowed = [XbExpiryProbe.NativePublication]::MoveNoReplaceWriteThrough($src, $fresh)
        [pscustomobject]@{
            blockedOk = $blocked.Ok
            blockedErrorCode = $blocked.NativeStatus
            destinationUnchanged = ((Get-Content -LiteralPath $dst -Raw) -eq 'destination bytes')
            sourceStillPresentAfterBlock = $true
            allowedOk = $allowed.Ok
            freshExists = (Test-Path -LiteralPath $fresh)
            sourceGoneAfterMove = (-not (Test-Path -LiteralPath $src))
        } | ConvertTo-Json -Compress
    }
    'leaseacquire' {
        # Acquire and immediately dispose a trusted state-root lease over an injected root.
        # $Dir = injected root; $Text = '1' to require Windows.
        $requireWindows = ($Text -eq '1')
        if ($requireWindows) { $lease = New-ExpiryProbeTrustedRootLease -Root $Dir -RequireWindows }
        else { $lease = New-ExpiryProbeTrustedRootLease -Root $Dir }
        $result = [pscustomobject]@{
            acquired       = $lease.acquired
            reasons        = @($lease.reasons)
            componentCount = $lease.component_count
            identityCount  = $lease.identity_count
            heldCount      = $lease.held_count
        }
        Close-ExpiryProbeTrustedRootLease -Lease $lease
        $result | ConvertTo-Json -Compress -Depth 4
    }
    'leasehold' {
        # Hold a trusted state-root lease across an interactive handshake so an INDEPENDENT
        # process can attempt real filesystem renames while the Windows directory handles
        # are retained. Nothing here touches the canonical root: $Dir is always a temporary
        # directory supplied by the test.
        $lease = New-ExpiryProbeTrustedRootLease -Root $Dir -RequireWindows
        [Console]::Out.WriteLine('ACQUIRED|' + [bool]$lease.acquired + '|' + (@($lease.reasons) -join ';') + '|' + $lease.held_count)
        [Console]::Out.Flush()
        [void][Console]::In.ReadLine()
        Close-ExpiryProbeTrustedRootLease -Lease $lease
        [Console]::Out.WriteLine('RELEASED|' + $lease.held_count)
        [Console]::Out.Flush()
        [void][Console]::In.ReadLine()
    }
}
"""


# The single README bullet that states this probe's public contract. Amendment
# DL-XB-115-001-A1 corrects only that bullet; every other README surface is out of scope.
README_PROBE_BULLET_PREFIX = "- `scripts/ac2_member_expiry_capability_probe.ps1`"


def readme_probe_bullets(text):
    """Every README bullet describing the synthetic ExpiryDate capability probe."""
    return [line for line in text.splitlines() if line.startswith(README_PROBE_BULLET_PREFIX)]


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


# A2-4: the literal exact-head assertion step both jobs must run immediately after checkout.
EXACT_HEAD_STEP_NAME = "Assert literal exact-head checkout"


def workflow_job_blocks(text):
    """Map each job id under `jobs:` to its raw block text."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if re.match(r"^jobs:\s*$", line))
    except StopIteration:
        return {}
    end_of_jobs = len(lines)
    headers = []
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.startswith(" "):
            end_of_jobs = i
            break
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            headers.append((i, match.group(1)))
    blocks = {}
    for index, (line_no, name) in enumerate(headers):
        stop = headers[index + 1][0] if index + 1 < len(headers) else end_of_jobs
        blocks[name] = "\n".join(lines[line_no:stop])
    return blocks


def checkout_head_binding_problems(text):
    """Problems preventing literal exact-head binding. Empty list means compliant."""
    problems = []
    blocks = workflow_job_blocks(text)
    if not blocks:
        return ["no_jobs_found"]
    for job, block in blocks.items():
        if "actions/checkout@" not in block:
            problems.append("%s:no_checkout" % job)
            continue
        checkout_step = block.split("actions/checkout@", 1)[1].split("\n      - ", 1)[0]
        ref = re.search(r"^\s*ref:\s*(.+)$", checkout_step, re.M)
        if not ref:
            problems.append("%s:checkout_without_explicit_ref" % job)
        else:
            expression = ref.group(1)
            if "github.event.pull_request.head.sha" not in expression:
                problems.append("%s:ref_missing_pr_head_sha" % job)
            if "github.sha" not in expression:
                problems.append("%s:ref_missing_dispatch_sha" % job)
            if "merge_commit_sha" in expression or "/merge" in expression:
                problems.append("%s:ref_uses_merge_ref" % job)
        if EXACT_HEAD_STEP_NAME not in block:
            problems.append("%s:missing_exact_head_assertion" % job)
            continue
        assertion_at = block.index(EXACT_HEAD_STEP_NAME)
        assertion_step = block[assertion_at:].split("\n      - ", 1)[0]
        if "merge_commit_sha" in assertion_step:
            problems.append("%s:assertion_uses_merge_sha" % job)
        if "github.event.pull_request.head.sha" not in assertion_step:
            problems.append("%s:assertion_missing_pr_head_sha" % job)
        if "continue-on-error" in assertion_step:
            problems.append("%s:assertion_continue_on_error" % job)
        if "rev-parse HEAD" not in assertion_step:
            problems.append("%s:assertion_does_not_read_head" % job)
        for later in ("PowerShell parse check", "python -m unittest", "_run_ci_full_suite"):
            position = block.find(later)
            if position != -1 and position < assertion_at:
                problems.append("%s:assertion_runs_after_work" % job)
    return sorted(set(problems))


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
        # The lease performs the trusted-root validation (it reuses the same validator), so
        # acquiring it is the single point that gates every live access.
        trust_idx = self.script.index("New-ExpiryProbeTrustedRootLease")
        self.assertLess(trust_idx, self.script.index("LoadFrom"))
        self.assertLess(trust_idx, self.script.index("$authenticateMethod.Invoke"))
        self.assertLess(trust_idx, self.script.index("$result.autocount_contacted = $true"))
        self.assertLess(trust_idx, self.script.index("$getMemberMethod.Invoke"))
        # Path derivation, and therefore any filesystem use of the root, happens after it.
        self.assertLess(trust_idx, self.script.index("Get-ExpiryProbeStatePaths"))
        # The lease is the only caller of the validator from the executable path.
        self.assertIn("Test-ExpiryProbeTrustedStateRoot", self.lib)
        self.assertNotIn("Test-ExpiryProbeTrustedStateRoot", self.script)

    # ---- A2-2: the trusted-root lease must span the whole irreversible operation ---- #
    def test_lease_is_acquired_before_any_live_access(self):
        acquire = self.script.index("New-ExpiryProbeTrustedRootLease")
        for later in ("LoadFrom", "$authenticateMethod.Invoke",
                      "$result.autocount_contacted = $true", "$getMemberMethod.Invoke",
                      "New-ExpiryProbeDurableArtifact -Path $claimPath",
                      "Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod"):
            self.assertLess(acquire, self.script.index(later), later)

    def test_lease_is_released_only_after_terminal_evidence_handling(self):
        release = self.script.index("Close-ExpiryProbeTrustedRootLease")
        # Exactly one disposal site, and it is in the outer cleanup path.
        self.assertEqual(self.script.count("Close-ExpiryProbeTrustedRootLease"), 1)
        for earlier in ("Invoke-ExpiryProbeSaveMemberOnce -SaveMemberMethod",
                        "New-ExpiryProbeDurableArtifact -Path $claimPath",
                        "$result.expiry_match ="):
            self.assertLess(self.script.index(earlier), release, earlier)
        # The disposal must sit in the outer finally, after final publication is adjudicated,
        # so no early release can precede the irreversible boundary.
        publish = self.script.index("Complete-ExpiryProbeRun -DurableEvidence:$stateReady")
        self.assertLess(publish, release,
                        "the lease must outlive result publication and publication-failure handling")
        tail = self.script[publish:]
        self.assertRegex(tail, r"(?s)finally\s*\{[^}]*Close-ExpiryProbeTrustedRootLease")

    def test_no_lease_or_root_override_reaches_the_executable_script(self):
        self.assertIn("New-ExpiryProbeTrustedRootLease -Root $script:ExpiryProbeStateRoot", self.script)
        self.assertEqual(self.script.count("New-ExpiryProbeTrustedRootLease"), 1)
        for override in ("$Lease", "-Lease $", "LeaseRoot", "StateRoot ="):
            if override == "-Lease $":
                continue
            self.assertNotIn("[string]$" + override.strip("$"), self.script, override)
        # The lease helper is reachable only with the fixed canonical root.
        self.assertNotRegex(self.script, r"New-ExpiryProbeTrustedRootLease\s+-Root\s+(?!\$script:ExpiryProbeStateRoot)")

    def test_lease_uses_windows_handles_without_delete_sharing(self):
        for token in ("FILE_FLAG_BACKUP_SEMANTICS", "FILE_FLAG_OPEN_REPARSE_POINT",
                      "FILE_SHARE_READ", "FILE_SHARE_WRITE", "GetFileInformationByHandle",
                      "CreateFileW", "SafeFileHandle"):
            self.assertIn(token, self.lib, token)
        # Delete sharing must never be GRANTED: that is what pins the namespace. Comments may
        # explain its absence, so only executable lines are inspected.
        code_lines = [line for line in self.lib.splitlines()
                      if not line.lstrip().startswith(("#", "//"))]
        code = "\n".join(code_lines)
        self.assertNotIn("FILE_SHARE_DELETE", code)
        self.assertNotIn("0x00000004", code)
        self.assertIn("FILE_SHARE_READ | FILE_SHARE_WRITE,", code)
        # Handles are retained and disposed in reverse order, never re-opened per use.
        self.assertIn("function New-ExpiryProbeTrustedRootLease", self.lib)
        self.assertIn("function Close-ExpiryProbeTrustedRootLease", self.lib)
        self.assertRegex(self.lib, r"(?i)reverse order")
        # Nothing in the lease creates or repairs a component.
        lease_source = self.lib[self.lib.index("function New-ExpiryProbeTrustedRootLease"):]
        for forbidden in ("New-Item", "CreateDirectory", "Remove-Item", "Delete("):
            self.assertNotIn(forbidden, lease_source, forbidden)

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
        self.assertNotIn("[System.IO.File]::Move", self.lib)
        self.assertIn("MoveNoReplaceWriteThrough($StagingPath, $FinalPath)", self.lib)
        self.assertRegex(self.lib, r"refusing to overwrite evidence")

    # ---- A3-2: native write-through, no-replace publication ---- #
    def test_production_publication_uses_write_through_moveedfileex_only(self):
        code_lines = [line for line in self.lib.splitlines()
                      if not line.lstrip().startswith(("#", "//"))]
        code = "\n".join(code_lines)
        for token in ("MoveFileExW", "MOVEFILE_WRITE_THROUGH", "NativePublication",
                      "MoveNoReplaceWriteThrough"):
            self.assertIn(token, code, token)
        # Within the native publication type, write-through is the ONLY flag: replacement,
        # copying and reboot-delayed scheduling are never declared or requested.
        native = code[code.index("function Initialize-ExpiryProbeNativePublicationApi"):]
        native = native[:native.index("\nfunction ")]
        for forbidden in ("MOVEFILE_REPLACE_EXISTING", "MOVEFILE_COPY_ALLOWED",
                          "MOVEFILE_DELAY_UNTIL_REBOOT", "0x00000001", "0x00000002", "0x00000004"):
            self.assertNotIn(forbidden, native, forbidden)
        self.assertIn("MOVEFILE_WRITE_THROUGH = 0x00000008", native)
        self.assertEqual(native.count("MoveFileExW(source, destination,"), 1)
        self.assertIn("MoveFileExW(source, destination, MOVEFILE_WRITE_THROUGH)", native)
        # No ordinary-move or copy/delete/replace fallback survives anywhere in the library.
        for fallback in ("[System.IO.File]::Move", "Move-Item", "Copy-Item",
                         "[System.IO.File]::Copy", "[System.IO.File]::Replace",
                         "[System.IO.File]::Delete"):
            self.assertNotIn(fallback, code, fallback)
        self.assertNotIn("[System.IO.File]::Move", self.script)

    def test_publication_preflight_contract_is_declared(self):
        for reason in ("staging_not_absolute", "final_not_absolute", "publication_parent_mismatch",
                       "publication_parent_missing", "publication_same_basename",
                       "publication_volume_mismatch", "publication_final_exists"):
            self.assertIn("'%s'" % reason, self.lib, reason)
        # The injected move action stays a pure-test seam and never reaches the probe script.
        self.assertIn("[scriptblock]$MoveAction", self.lib)
        self.assertNotIn("-MoveAction", self.script)
        # Off Windows the production path fails closed rather than falling back.
        publish = self.lib[self.lib.index("function Publish-ExpiryProbeResultAtomic"):]
        publish = publish[:publish.index("\nfunction ")]
        self.assertIn("Win32NT", publish)

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
        self.assertEqual(read_scratch_text(claim).strip(), "contender")

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
        self.assertIn("schema_missing_field", as_list(verdict["reasons"]))

        wrong_exit = verified_record(operation_id)
        wrong_exit["exit_code"] = 1
        path = self._record_file("validator_exit.json", wrong_exit)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"])
        self.assertIn("exit_code_inconsistent", as_list(verdict["reasons"]))

    # ---- A2-1: authority must be derived from runtime facts, never from declarations ---- #
    def _reject(self, operation_id, name, mutate):
        """Build a record from the consistent verified template, mutate it, expect rejection."""
        record = verified_record(operation_id)
        mutate(record)
        path = self._record_file(name, record)
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"],
                         "%s must not validate as authoritative" % name)
        return as_list(verdict["reasons"])

    def test_fabricated_all_default_verified_record_is_rejected(self):
        # The core A2-1 case, kept meaningful under A3-1: the record is COMPLETE and correctly
        # typed, so it clears the strict schema gate and must still be caught by runtime-fact
        # derivation. Every runtime flag is an actual $false, yet it declares a durably
        # persisted EXPIRY_VERIFIED with exit 0; the flags derive FAILED_BEFORE_WRITE.
        operation_id = "expop_fabricated01"
        fabricated = verified_record(operation_id)
        for field in AUTHORITATIVE_BOOLEAN_FIELDS:
            fabricated[field] = False
        fabricated["activated"] = True
        fabricated["evidence_persisted"] = True
        fabricated["save_outcome"] = "not_attempted"
        fabricated["expiry_date_readback_value"] = None
        # The lie:
        fabricated["underlying_terminal_outcome"] = "EXPIRY_VERIFIED"
        fabricated["terminal_outcome"] = "EXPIRY_VERIFIED"
        fabricated["exit_code"] = 0
        path = self._record_file("validator_fabricated.json", fabricated)
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"],
                         "a record whose flags show no contact, claim or save must never be authoritative")
        reasons = as_list(verdict["reasons"])
        self.assertIn("underlying_outcome_not_derived_from_flags", reasons)
        self.assertIn("verified_without_required_runtime_state", reasons)

    def test_save_success_without_a_created_claim_is_rejected(self):
        reasons = self._reject("expop_noclaim01", "validator_noclaim.json",
                               lambda r: r.__setitem__("claim_created", False))
        self.assertIn("save_attempted_without_claim", reasons)

    def test_save_confirmation_without_a_save_attempt_is_rejected(self):
        reasons = self._reject("expop_noattempt01", "validator_noattempt.json",
                               lambda r: r.__setitem__("save_member_attempted", False))
        self.assertIn("state_contradiction", reasons)

    def test_readback_success_without_a_confirmed_save_is_rejected(self):
        def mutate(record):
            record["save_member_confirmed"] = False
            record["save_outcome"] = "uncertain"
        reasons = self._reject("expop_norbsave01", "validator_norbsave.json", mutate)
        self.assertIn("state_contradiction", reasons)

    def test_expiry_match_without_a_found_readback_is_rejected(self):
        reasons = self._reject("expop_nomatchrb01", "validator_nomatchrb.json",
                               lambda r: r.__setitem__("readback_found", False))
        self.assertTrue(reasons)
        self.assertTrue({"state_contradiction", "underlying_outcome_not_derived_from_flags"} & set(reasons),
                        reasons)

    def test_every_declared_underlying_outcome_must_match_the_flag_derivation(self):
        # The flags always describe a verified run; each declared underlying outcome other
        # than the derived one must be rejected.
        for index, code in enumerate(c for c in TERMINAL_CODES if c != "EXPIRY_VERIFIED"):
            operation_id = "expop_underlying%02d" % index
            reasons = self._reject(operation_id, "validator_underlying_%s.json" % code,
                                   lambda r, c=code: r.__setitem__("underlying_terminal_outcome", c))
            self.assertIn("underlying_outcome_not_derived_from_flags", reasons, code)

    def test_every_declared_final_outcome_must_match_the_derived_final_outcome(self):
        for index, code in enumerate(c for c in TERMINAL_CODES if c != "EXPIRY_VERIFIED"):
            operation_id = "expop_final%02d" % index
            reasons = self._reject(operation_id, "validator_final_%s.json" % code,
                                   lambda r, c=code: r.__setitem__("terminal_outcome", c))
            self.assertIn("terminal_outcome_inconsistent", reasons, code)

    def test_final_outcome_must_account_for_recorded_persistence_state(self):
        # evidence_persisted=false means the derived final outcome is
        # EVIDENCE_PERSISTENCE_FAILED, so a declared EXPIRY_VERIFIED is inconsistent. The
        # validator must use the RECORD's persistence fact, not assume it is true because the
        # artefact reached the validator.
        reasons = self._reject("expop_persist01", "validator_persist.json",
                               lambda r: r.__setitem__("evidence_persisted", False))
        self.assertIn("evidence_not_persisted", reasons)
        self.assertIn("terminal_outcome_inconsistent", reasons)

    # ---- A3-1: strict authoritative-record schema and types ---- #
    def _valid_baseline(self, operation_id="expop_schema01"):
        record = verified_record(operation_id)
        path = self._record_file("schema_baseline_%s.json" % operation_id, record)
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        return path, final

    def test_library_schema_matches_the_probe_result_contract(self):
        # The library's closed schema must be exactly the reviewed $result contract emitted by
        # the unchanged probe script, so the schema cannot drift from the producer.
        declared = self._json("schemafields")
        script_fields = re.findall(
            r"^\s{4}([a-z_]+)\s*=", read_repo_text("probe_script").split("$result = [ordered]@{", 1)[1]
            .split("\n}", 1)[0], re.M)
        self.assertEqual(len(script_fields), 56)
        self.assertEqual(sorted(as_list(declared["topLevel"])), sorted(script_fields))
        self.assertEqual(sorted(as_list(declared["topLevel"])), sorted(AUTHORITATIVE_TOP_LEVEL_FIELDS))
        self.assertEqual(sorted(as_list(declared["booleans"])), sorted(AUTHORITATIVE_BOOLEAN_FIELDS))
        self.assertEqual(sorted(as_list(declared["contract"])), sorted(PUBLICATION_CONTRACT_FIELDS))

    def test_every_boolean_authority_field_rejects_every_invalid_type(self):
        path, final = self._valid_baseline("expop_boolmatrix")
        results = as_list(self._json("schemamatrix", CtxJson=str(path), Extra=str(final),
                                     Text=",".join(AUTHORITATIVE_BOOLEAN_FIELDS)))
        self.assertEqual(len(results), len(AUTHORITATIVE_BOOLEAN_FIELDS) * 9)
        for case in results:
            self.assertFalse(case["authoritative"],
                             "%s=%s must not be authoritative" % (case["field"], case["variant"]))
            self.assertTrue(any(r.startswith("schema_") for r in as_list(case["reasons"])),
                            "%s=%s reasons=%s" % (case["field"], case["variant"], as_list(case["reasons"])))

    def test_complete_false_shaped_string_record_is_rejected_before_derivation(self):
        # Every runtime fact is the STRING "true"/"false", which PowerShell would coerce to
        # $true. The record declares a fully successful, persisted, exit-zero run.
        operation_id = "expop_falseshaped"
        record = verified_record(operation_id)
        for field in AUTHORITATIVE_BOOLEAN_FIELDS:
            record[field] = "true" if record[field] else "false"
        record["save_outcome"] = "confirmed"
        record["underlying_terminal_outcome"] = "EXPIRY_VERIFIED"
        record["terminal_outcome"] = "EXPIRY_VERIFIED"
        record["exit_code"] = "0"
        path = self._record_file("schema_false_shaped.json", record)
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertFalse(verdict["authoritative"],
                         "false-shaped strings must never fabricate an authoritative success")
        reasons = as_list(verdict["reasons"])
        self.assertTrue(any(r.startswith("schema_") for r in reasons), reasons)
        # It must fail at the schema gate, before any terminal derivation runs.
        for derived in ("underlying_outcome_not_derived_from_flags", "terminal_outcome_inconsistent",
                        "state_contradiction", "verified_without_required_runtime_state"):
            self.assertNotIn(derived, reasons,
                             "derivation must not run on an unvalidated record: %s" % reasons)

    def test_exit_code_rejects_every_non_integral_or_out_of_range_value(self):
        path, final = self._valid_baseline("expop_exitmatrix")
        results = as_list(self._json("exitcodematrix", CtxJson=str(path), Extra=str(final)))
        self.assertEqual(len(results), 9)
        for case in results:
            self.assertFalse(case["authoritative"], case["variant"])
            self.assertTrue(any(r.startswith("schema_") for r in as_list(case["reasons"])),
                            "%s reasons=%s" % (case["variant"], as_list(case["reasons"])))

    def test_every_authority_string_field_rejects_non_string_values(self):
        path, final = self._valid_baseline("expop_stringmatrix")
        results = as_list(self._json("stringtypematrix", CtxJson=str(path), Extra=str(final),
                                     Text=",".join(AUTHORITATIVE_STRING_FIELDS)))
        self.assertEqual(len(results), len(AUTHORITATIVE_STRING_FIELDS) * 6)
        for case in results:
            self.assertFalse(case["authoritative"],
                             "%s=%s must not be authoritative" % (case["field"], case["variant"]))
            self.assertTrue(any(r.startswith("schema_") for r in as_list(case["reasons"])),
                            "%s=%s reasons=%s" % (case["field"], case["variant"], as_list(case["reasons"])))

    def test_missing_and_unknown_schema_fields_fail_closed(self):
        path, final = self._valid_baseline("expop_shape")
        expected = {
            "missing_top": "schema_missing_field",
            "unknown_top": "schema_unknown_field",
            "missing_contract": "schema_publication_contract_missing_field",
            "unknown_contract": "schema_publication_contract_unknown_field",
            "scalar_contract": "schema_publication_contract_not_object",
            "null_contract": "schema_publication_contract_not_object",
            "array_contract": "schema_publication_contract_not_object",
        }
        for case, reason in expected.items():
            verdict = self._json("schemashape", CtxJson=str(path), Extra=str(final), Text=case)
            self.assertFalse(verdict["authoritative"], case)
            self.assertIn(reason, as_list(verdict["reasons"]), "%s -> %s" % (case, as_list(verdict["reasons"])))

    def test_schema_accepts_both_json_date_shapes_across_powershell_versions(self):
        # Windows PowerShell 5.1 yields [string] for ISO-8601 JSON values; PowerShell 7 yields
        # [datetime]. Both are the correct output of supported JSON parsing, so both must
        # validate identically. Every other substitute for those fields is still rejected by
        # test_every_authority_string_field_rejects_non_string_values.
        path, final = self._valid_baseline("expop_datetime")
        info = self._json("datetimerecord", CtxJson=str(path), Extra=str(final))
        self.assertEqual(info["executedType"], "DateTime",
                         "the fixture must actually exercise the [datetime] shape")
        self.assertTrue(info["authoritative"], "reasons=%s" % as_list(info["reasons"]))
        self.assertEqual(as_list(info["reasons"]), [])

    def test_schema_reasons_never_echo_malformed_values(self):
        operation_id = "expop_noecho"
        record = verified_record(operation_id)
        record["activated"] = "SENSITIVE-MARKER-VALUE"
        record["operation_id"] = 12345
        path = self._record_file("schema_noecho.json", record)
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        proc = self._lib("authoritative", Text=str(final), CtxJson=str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("SENSITIVE-MARKER-VALUE", proc.stdout)
        self.assertNotIn("SENSITIVE-MARKER-VALUE", proc.stderr)
        self.assertNotIn("12345", proc.stdout)
        verdict = json.loads(proc.stdout)
        self.assertFalse(verdict["authoritative"])
        for reason in as_list(verdict["reasons"]):
            self.assertRegex(reason, r"^[a-z0-9_]+$", "reason codes must be generic: %s" % reason)

    # ---- A3-2: write-through, no-replace final publication ---- #
    def test_publication_preflight_rejects_paths_before_writing_staging(self):
        directory = self.tmp / "preflight"
        other = self.tmp / "preflight_other"
        directory.mkdir(exist_ok=True)
        other.mkdir(exist_ok=True)
        staging = directory / "expiry_probe_staging_expop_pf.incomplete"
        final_elsewhere = other / "expiry_probe_result_expop_pf.json"
        same_name = directory / "expiry_probe_staging_expop_pf.incomplete"

        cross = self._json("publishpaths", Text="%s|%s" % (staging, final_elsewhere))
        self.assertTrue(cross["threw"], "a different parent directory must be rejected")
        self.assertFalse(cross["stagingCreated"], "staging must not be written before the preflight passes")
        self.assertFalse(cross["finalCreated"])

        identical = self._json("publishpaths", Text="%s|%s" % (staging, same_name))
        self.assertTrue(identical["threw"], "identical source and destination must be rejected")
        self.assertFalse(identical["stagingCreated"])

        relative = self._json("publishpaths", Text="relative_staging.incomplete|relative_result.json")
        self.assertTrue(relative["threw"], "relative paths must be rejected")

    @unittest.skipUnless(IS_WINDOWS, "the production write-through publication is Windows-only")
    def test_real_native_write_through_move_publishes_and_never_replaces(self):
        directory = self.tmp / "nativepublish"
        directory.mkdir(exist_ok=True)
        operation_id = "expop_native01"
        record = self._record_file("native_record.json", verified_record(operation_id))
        info = self._json("nativemove", Dir=str(directory), Text=operation_id, CtxJson=str(record))
        self.assertTrue(info["published"], info["publishError"])
        self.assertTrue(info["finalExists"], "the write-through move must publish the final name")
        self.assertFalse(info["stagingLeft"], "the staging name must not survive a successful move")
        self.assertEqual(info["finalOperationId"], operation_id)
        self.assertTrue(info["secondBlocked"], "an existing destination must fail no-clobber")

    @unittest.skipUnless(IS_WINDOWS, "native no-replace semantics are Windows-only")
    def test_native_move_is_no_replace_at_the_api_layer(self):
        directory = self.tmp / "nativeraw"
        directory.mkdir(exist_ok=True)
        info = self._json("nativemoveraw", Dir=str(directory))
        self.assertFalse(info["blockedOk"], "MoveFileExW must fail when the destination exists")
        self.assertNotEqual(info["blockedErrorCode"], 0)
        self.assertTrue(info["destinationUnchanged"], "the destination must never be replaced")
        self.assertTrue(info["allowedOk"], "a fresh destination must succeed")
        self.assertTrue(info["freshExists"])
        self.assertTrue(info["sourceGoneAfterMove"])

    def test_consistent_authoritative_verified_record_still_passes(self):
        operation_id = "expop_stillgood01"
        path = self._record_file("validator_stillgood.json", verified_record(operation_id))
        final = self.tmp / ("expiry_probe_result_%s.json" % operation_id)
        verdict = self._json("authoritative", Text=str(final), CtxJson=str(path))
        self.assertTrue(verdict["authoritative"], "reasons=%s" % as_list(verdict["reasons"]))
        self.assertEqual(as_list(verdict["reasons"]), [])


@unittest.skipIf(PS is None, "no PowerShell executable available")
class ExpiryProbeTrustedRootLeaseTests(unittest.TestCase):
    """A2-2: the trusted state-root namespace must be PINNED, not merely re-validated.

    Every test here operates on temporary directories only. Nothing opens, inspects or
    modifies the real canonical root (asserted mechanically by
    test_no_lease_test_targets_the_real_canonical_root).
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.harness = cls.tmp / "leaseprobe.ps1"
        cls.harness.write_text(LIBPROBE, encoding="utf-8")

    def _cmd(self, op, **kw):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(self.harness), "-Lib", str(LIB), "-Op", op]
        for k, v in kw.items():
            cmd += ["-" + k, str(v)]
        return cmd

    def _json(self, op, **kw):
        proc = subprocess.run(self._cmd(op, **kw), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def _chain(self, name):
        """<tmp>/<name>/outer/inner/state, every level a plain local directory."""
        root = self.tmp / name / "outer" / "inner" / "state"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _rename_fails(target):
        """True when an independent process (this one) cannot rename `target`."""
        moved = target.parent / (target.name + "_moved")
        try:
            os.rename(str(target), str(moved))
        except OSError:
            return True
        os.rename(str(moved), str(target))  # undo: the rename unexpectedly succeeded
        return False

    def _hold(self, root):
        return subprocess.Popen(self._cmd("leasehold", Dir=str(root)),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    @unittest.skipUnless(IS_WINDOWS, "the production lease path is Windows-only")
    def test_lease_acquires_over_a_temporary_directory_chain(self):
        root = self._chain("acquirechain")
        info = self._json("leaseacquire", Dir=str(root), Text="1")
        self.assertTrue(info["acquired"], "reasons=%s" % as_list(info["reasons"]))
        self.assertEqual(as_list(info["reasons"]), [])
        # One retained handle and one recorded identity per existing component, volume root
        # through leaf.
        self.assertGreaterEqual(info["componentCount"], 4)
        self.assertEqual(info["heldCount"], info["componentCount"])
        self.assertEqual(info["identityCount"], info["componentCount"])

    @unittest.skipUnless(IS_WINDOWS, "real share-mode semantics are Windows-only")
    def test_lease_blocks_root_and_ancestor_rename_until_released(self):
        root = self._chain("holdchain")
        marker = root / "identity.marker"
        marker.write_text("pinned-namespace", encoding="utf-8")
        inner, outer = root.parent, root.parent.parent
        proc = self._hold(root)
        try:
            line = proc.stdout.readline().strip()
            self.assertTrue(line.startswith("ACQUIRED|True|"),
                            "lease not acquired: %s %s" % (line, proc.stderr.read() if proc.poll() else ""))
            self.assertTrue(line.endswith("|%d" % int(line.rsplit("|", 1)[1])))
            # While the lease is held, an INDEPENDENT process cannot rename the root...
            self.assertTrue(self._rename_fails(root), "the leased root must not be renameable")
            # ...nor any mutable ancestor in the leased chain.
            self.assertTrue(self._rename_fails(inner), "a leased ancestor must not be renameable")
            self.assertTrue(self._rename_fails(outer), "a leased ancestor must not be renameable")
            # The canonical path therefore still resolves to the SAME directory: no second
            # backing claim namespace can be swapped in underneath the running probe.
            self.assertTrue(marker.is_file())
            self.assertEqual(read_scratch_text(marker), "pinned-namespace")

            proc.stdin.write("release\n")
            proc.stdin.flush()
            released = proc.stdout.readline().strip()
            self.assertTrue(released.startswith("RELEASED"), released)
            # After disposal the very same rename succeeds, proving the block came from the
            # retained handles and not from some unrelated condition.
            moved = root.parent / (root.name + "_after_release")
            os.rename(str(root), str(moved))
            self.assertTrue(moved.is_dir())
            os.rename(str(moved), str(root))
        finally:
            try:
                proc.stdin.write("exit\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

    @unittest.skipUnless(IS_WINDOWS, "real share-mode semantics are Windows-only")
    def test_lease_prevents_replacement_creating_a_second_claim_namespace(self):
        root = self._chain("replacechain")
        (root / "original.marker").write_text("original", encoding="utf-8")
        replacement = self.tmp / "replacement_namespace"
        replacement.mkdir(exist_ok=True)
        (replacement / "attacker.marker").write_text("attacker", encoding="utf-8")
        proc = self._hold(root)
        try:
            line = proc.stdout.readline().strip()
            self.assertTrue(line.startswith("ACQUIRED|True|"), line)
            # A replacement needs the leased directory out of the way first; that step fails,
            # so the swap can never complete.
            self.assertTrue(self._rename_fails(root))
            with self.assertRaises(OSError):
                os.rename(str(replacement), str(root))
            # The canonical path still backs exactly one namespace: the original one.
            self.assertTrue((root / "original.marker").is_file())
            self.assertFalse((root / "attacker.marker").exists())
            self.assertTrue((replacement / "attacker.marker").is_file())
            # And only one exclusive claim can exist in that single namespace.
            claim = root / "expiry_probe_claim_afp_pinned.claim"
            first = self._json("claimrace", Text=str(claim))
            second = self._json("claimrace", Text=str(claim))
            self.assertTrue(first["created"])
            self.assertFalse(second["created"],
                             "a second contender must remain ineligible for the save boundary")
        finally:
            try:
                proc.stdin.write("release\n")
                proc.stdin.flush()
                proc.stdout.readline()
                proc.stdin.write("exit\n")
                proc.stdin.flush()
                proc.communicate(timeout=30)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                proc.kill()
                proc.communicate()

    @unittest.skipUnless(IS_WINDOWS, "partial-acquisition handle release is Windows-specific")
    def test_partial_acquisition_releases_already_opened_handles(self):
        chain = self._chain("partialchain")
        missing = chain / "absent_leaf"
        self.assertFalse(missing.exists())
        proc = self._hold(missing)
        try:
            line = proc.stdout.readline().strip()
            self.assertTrue(line.startswith("ACQUIRED|False|"), line)
            self.assertIn("root_missing", line)
            self.assertTrue(line.endswith("|0"), "every partially acquired handle must be disposed: %s" % line)
            # Because the partial handles were released, the ancestors are renameable again.
            self.assertFalse(self._rename_fails(chain),
                             "a failed acquisition must not leave the chain pinned")
        finally:
            try:
                proc.stdin.write("release\n")
                proc.stdin.flush()
                proc.stdout.readline()
                proc.stdin.write("exit\n")
                proc.stdin.flush()
                proc.communicate(timeout=30)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                proc.kill()
                proc.communicate()

    def test_lease_rejects_a_missing_or_non_directory_root(self):
        missing = self.tmp / "no_such_lease_root"
        info = self._json("leaseacquire", Dir=str(missing), Text="0" if not IS_WINDOWS else "1")
        self.assertFalse(info["acquired"])
        self.assertEqual(info["heldCount"], 0)
        expected = "platform_not_windows" if not IS_WINDOWS else "root_missing"
        self.assertIn(expected, as_list(info["reasons"]))

    @unittest.skipIf(IS_WINDOWS, "non-Windows fail-closed behaviour")
    def test_lease_fails_closed_off_windows_without_invoking_windows_apis(self):
        root = self._chain("posixchain")
        info = self._json("leaseacquire", Dir=str(root), Text="1")
        self.assertFalse(info["acquired"],
                         "the active probe path must fail closed off Windows, not skip the contract")
        self.assertEqual(as_list(info["reasons"]), ["platform_not_windows"])
        self.assertEqual(info["heldCount"], 0)
        # The platform gate must precede any native interop in the library source.
        lib = LIB.read_text(encoding="utf-8")
        self.assertLess(lib.index("platform_not_windows"), lib.index("Add-Type"),
                        "the Windows-only gate must precede native interop")

    def test_no_lease_test_targets_the_real_canonical_root(self):
        # Mechanical guarantee that no lease/trust/harness invocation in this module can be
        # pointed at the operator's real evidence root. This guard's own body necessarily
        # names the forbidden shapes, so it is excluded from the scan.
        guard_name = "def test_no_lease_test_targets_the_real_canonical_root"
        lines = read_repo_text("focused_tests").splitlines()
        start = next(i for i, line in enumerate(lines) if guard_name in line)
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end].startswith("        ")):
            end += 1
        scanned = lines[:start] + lines[end:]
        self.assertLess(start, end, "the guard body must be locatable")
        for line in scanned:
            if re.search(r"\b(Dir|_hold)\s*[=(]", line):
                self.assertNotIn(CANONICAL_STATE_ROOT, line, line)
                self.assertNotIn("CANONICAL_STATE_ROOT", line, line)
        # The canonical root is referenced only as a constant, a static expectation and the
        # skip guard: never as a filesystem target opened by a test.
        joined = "\n".join(scanned)
        for forbidden in ("Path(CANONICAL_STATE_ROOT)", "mkdir(CANONICAL_STATE_ROOT",
                          "New-ExpiryProbeTrustedRootLease -Root " + CANONICAL_STATE_ROOT):
            self.assertNotIn(forbidden, joined, forbidden)


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

    # ---- README probe contract (Design Lock amendment DL-XB-115-001-A1) ---- #
    def _probe_bullet(self):
        bullets = readme_probe_bullets(self.readme)
        self.assertEqual(len(bullets), 1, "exactly one README bullet must describe this probe")
        return bullets[0]

    def test_readme_probe_contract_has_no_removed_path_parameter(self):
        # The public contract must not advertise an operator-selected claim/result root or a
        # secondary output path: both parameters were removed from the probe.
        bullet = self._probe_bullet()
        for removed in ("-StateDirectory", "StateDirectory", "-JsonOut", "JsonOut"):
            self.assertNotIn(removed, bullet, removed)

    def test_readme_probe_contract_names_the_fixed_canonical_root(self):
        self.assertIn(CANONICAL_STATE_ROOT, self._probe_bullet())

    def test_readme_probe_contract_states_root_is_not_operator_selectable(self):
        bullet = self._probe_bullet()
        self.assertRegex(bullet, r"(?i)not operator-selectable")
        self.assertRegex(bullet, r"(?i)fixed in reviewed code")

    def test_readme_probe_contract_states_the_root_must_pre_exist_and_is_never_managed(self):
        bullet = self._probe_bullet()
        self.assertRegex(bullet, r"(?i)must already exist")
        self.assertRegex(bullet, r"(?i)never creates, repairs, redirects, migrates or cleans it")

    def test_readme_probe_contract_states_validation_precedes_autocount_contact(self):
        self.assertRegex(self._probe_bullet(), r"(?i)validated before any AutoCount contact")

    def test_readme_probe_contract_preserves_the_permanent_claim_boundary(self):
        bullet = self._probe_bullet()
        self.assertRegex(bullet, r"(?i)permanent single-use attempt claim")
        self.assertRegex(bullet, r"(?i)never overwritten or deleted")
        self.assertRegex(bullet, r"(?i)fail closed")
        self.assertRegex(bullet, r"(?i)exactly one synthetic member")
        self.assertRegex(bullet, r"(?i)never updates, deletes, rolls back, or cleans up")
        self.assertRegex(bullet, r"(?i)never retried")
        self.assertRegex(bullet, r"(?i)owner approval")

    def test_readme_probe_contract_states_path_bound_publication(self):
        bullet = self._probe_bullet()
        self.assertRegex(bullet, r"(?i)no-clobber")
        self.assertRegex(bullet, r"(?i)staging")
        self.assertRegex(bullet, r"(?i)non-authoritative")
        self.assertRegex(bullet, r"(?i)no-replace")
        # A failed publication is nonzero, and only an authoritative verified result exits 0.
        self.assertRegex(bullet, r"(?i)nonzero")
        self.assertRegex(bullet, r"exit `0` only for an authoritative `EXPIRY_VERIFIED`")

    def test_readme_remains_in_the_mechanical_dependency_inventory(self):
        self.assertIn("README.md", registered_dependencies())

    def test_readme_remains_covered_by_every_workflow_path_filter(self):
        filters = workflow_path_filters(self.workflow)
        self.assertTrue(filters, "the focused workflow must declare at least one path filter")
        for event, patterns in filters.items():
            self.assertEqual(uncovered_dependencies({"README.md"}, patterns), [],
                             "event '%s' does not trigger for README.md" % event)

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
        inventory = registered_dependencies()
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
        inventory = registered_dependencies()
        for event, patterns in filters.items():
            missing = uncovered_dependencies(inventory, patterns)
            self.assertEqual(missing, [],
                             "event '%s' does not trigger for: %s" % (event, missing))

    # ---- A2-3: the closed contract must be fail-closed, not best-effort ---- #
    def test_module_performs_no_repository_read_outside_the_closed_registry(self):
        violations = repository_read_violations(read_repo_text("focused_tests"))
        self.assertEqual(violations, [],
                         "every repository read must resolve through repo_path/read_repo_text")

    def test_ast_guard_rejects_every_escaping_repository_read_form(self):
        header = "from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        fixtures = {
            "joinpath": (header + 'X = ROOT.joinpath("new-contract.md").read_text()\n',
                         "root_joinpath"),
            "path_constructor": (header + 'X = Path(ROOT, "new-contract.md").read_text()\n',
                                 "path_constructor_from_root"),
            "variable_path": (header + 'name = "new-contract.md"\nX = (ROOT / name).read_text()\n',
                              "root_path_derivation"),
            "helper_returned": (header + 'def helper(n):\n    return ROOT / n\n'
                                         'X = helper("new-contract.md").read_text()\n',
                                "unresolved_repository_read"),
            "dynamic_key": (header + 'key = "readme"\nX = read_repo_text(key)\n',
                            "dynamic_dependency_key"),
            "unregistered_key": (header + 'X = read_repo_text("not_registered")\n',
                                 "unregistered_dependency_key"),
            "builtin_open": (header + 'X = open(str(ROOT) + "/new-contract.md").read()\n',
                             "builtin_open"),
        }
        for name, (source, expected) in fixtures.items():
            violations = repository_read_violations(source)
            self.assertTrue(violations, "%s must not be silently ignored" % name)
            self.assertTrue(any(v.endswith(expected) for v in violations),
                            "%s: expected %s, got %s" % (name, expected, violations))

    def test_ast_guard_rejects_alias_and_indirection_read_routes(self):
        # A3-3: realistic indirection must fail closed, not merely the direct forms.
        header = "from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        derived = "target = Path(__file__).resolve().parents[1] / 'new-contract.md'\n"
        fixtures = {
            "bound_read_text": (header + derived + "reader = target.read_text\ntext = reader()\n",
                                "bound_reader_capture"),
            "bound_read_bytes": (header + derived + "reader = target.read_bytes\nblob = reader()\n",
                                 "bound_reader_capture"),
            "bound_path_open": (header + derived + "opener = target.open\nhandle = opener()\n",
                                "bound_reader_capture"),
            "imported_open_alias": ("from io import open as io_open\n" + header + derived
                                    + "text = io_open(target).read()\n", "open_alias_import"),
            "assigned_open_alias": (header + derived + "reader = open\ntext = reader(target).read()\n",
                                    "builtin_open"),
            "alias_of_alias": (header + derived + "first = open\nsecond = first\ntext = second(target).read()\n",
                               "builtin_open"),
            "getattr_literal": (header + derived + "reader = getattr(target, 'read_text')\ntext = reader()\n",
                                "dynamic_attribute_access"),
            "getattr_variable": (header + derived + "name = 'read_text'\nreader = getattr(target, name)\ntext = reader()\n",
                                 "dynamic_attribute_access"),
            "constructed_attribute": (header + derived + "name = 'read' + '_text'\nreader = getattr(target, name)\ntext = reader()\n",
                                      "dynamic_attribute_access"),
            "attrgetter": ("from operator import attrgetter\n" + header + derived
                           + "reader = attrgetter('read_text')(target)\ntext = reader()\n",
                           "dynamic_attribute_access"),
            "wrapper_reads_path": (header + derived + "def load():\n    return target.read_text()\n",
                                   "unresolved_repository_read"),
            "wrapper_returns_path": (header + "def where():\n    return ROOT / 'new-contract.md'\n"
                                     + "text = where().read_text()\n", "repository_path_escape"),
            "wrapper_returns_reader": (header + derived + "def make():\n    return target.read_text\n"
                                       + "text = make()()\n", "reader_callable_escape"),
            "lambda_reader": (header + derived + "load = lambda: target.read_text()\ntext = load()\n",
                              "unresolved_repository_read"),
            "list_comprehension": (header + derived + "texts = [target.read_text() for _ in range(1)]\n",
                                   "unresolved_repository_read"),
            "dict_comprehension": (header + derived + "texts = {i: target.read_text() for i in range(1)}\n",
                                   "unresolved_repository_read"),
            "generator_reader": (header + derived + "texts = (target.read_text() for _ in range(1))\n",
                                 "unresolved_repository_read"),
            "closure_over_path": (header + derived + "def outer():\n    def inner():\n        return target.read_text()\n    return inner\n",
                                  "unresolved_repository_read"),
            "file_derived_path": ("from pathlib import Path\n"
                                  + "base = Path(__file__).resolve().parents[1]\n"
                                  + "text = (base / 'new-contract.md').read_text()\n",
                                  "root_path_derivation"),
            "file_derived_alias": ("from pathlib import Path\n"
                                   + "base = Path(__file__).resolve().parents[1]\n"
                                   + "alias = base\ntext = (alias / 'new-contract.md').read_text()\n",
                                   "root_path_derivation"),
            "helper_returned_path": (header + "def helper(n):\n    return ROOT / n\n"
                                     + "text = helper('new-contract.md').read_text()\n",
                                     "unresolved_repository_read"),
            "helper_returned_reader": (header + derived + "def helper():\n    return target.read_bytes\n"
                                       + "blob = helper()()\n", "reader_callable_escape"),
            "unresolved_call_with_path": (header + derived + "sink(target)\n",
                                          "repository_path_escape"),
            "unresolved_call_with_reader": (header + derived + "reader = target.read_text\nsink(reader)\n",
                                            "reader_callable_escape"),
            "reader_in_collection": (header + derived + "readers = [target.read_text]\ntext = readers[0]()\n",
                                     "bound_reader_capture"),
            "dynamic_import_reader": ("import importlib\n" + header + derived
                                      + "mod = importlib.import_module('io')\ntext = mod.open(target).read()\n",
                                      "dynamic_attribute_access"),
        }
        self.assertGreaterEqual(len(fixtures), 25, "the A3-3 fixture matrix must stay complete")
        for name, (source, expected) in fixtures.items():
            violations = repository_read_violations(source)
            self.assertTrue(violations, "%s must not be silently accepted" % name)
            self.assertTrue(any(v.endswith(expected) for v in violations),
                            "%s: expected %s, got %s" % (name, expected, violations))

    def test_sanctioned_helper_exemption_cannot_be_borrowed(self):
        # A nested or same-named function must not inherit the sanctioned-helper exemption.
        header = "from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        nested = header + ("class Sneaky:\n"
                           "    def read_repo_text(self, key):\n"
                           "        return (ROOT / key).read_text()\n")
        self.assertTrue(repository_read_violations(nested),
                        "a method named like a sanctioned helper must not be exempt")
        inner = header + ("def outer():\n"
                          "    def read_scratch_text(p):\n"
                          "        return (ROOT / p).read_text()\n"
                          "    return read_scratch_text\n")
        self.assertTrue(repository_read_violations(inner),
                        "a nested function named like a sanctioned helper must not be exempt")

    def test_ast_guard_is_independent_of_the_registry_contents(self):
        # Even a REGISTERED file read through an escaping form must fail: the guard checks the
        # shape of the read expression, so closure cannot pass merely because the expected and
        # actual inventories came from the same incomplete resolver.
        header = "from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        violations = repository_read_violations(header + 'X = ROOT.joinpath("README.md").read_text()\n')
        self.assertTrue(any(v.endswith("root_joinpath") for v in violations), violations)
        self.assertTrue(any(v.endswith("unresolved_repository_read") for v in violations), violations)
        # The sanctioned form for the same registered file is accepted.
        self.assertEqual(repository_read_violations(header + 'X = read_repo_text("readme")\n'), [])

    def test_registry_is_immutable(self):
        with self.assertRaises(TypeError):
            REPO_DEPENDENCIES["injected"] = "somewhere/else.md"

    def test_unregistered_dependency_key_fails_closed_at_runtime(self):
        # Called through an alias on purpose: a literal repo_path("<unregistered>") is itself
        # a guard violation, and this negative self-test performs no read. The guard still
        # catches the dangerous case, because reading from an alias-returned path is an
        # unresolved_repository_read.
        resolver = repo_path
        self.assertNotIn("definitely_not_registered", REPO_DEPENDENCIES)
        with self.assertRaises(KeyError):
            resolver("definitely_not_registered")

    def test_a_newly_registered_dependency_must_also_enter_the_workflow_filter(self):
        # Closure in the other direction: a registry entry with no matching path pattern is
        # reported, so adding a dependency without wiring CI cannot pass.
        filters = workflow_path_filters(self.workflow)
        self.assertTrue(filters)
        new_dependency = "docs/autocount2-automation/new-contract.md"
        for patterns in filters.values():
            self.assertEqual(uncovered_dependencies({new_dependency}, patterns), [new_dependency])

    def test_closure_assertion_fails_when_a_required_path_is_dropped(self):
        # Negative control: the closure check must actually bite. Removing one required
        # filter entry from a fixture copy of the workflow must be detected.
        fixture = "\n".join(line for line in self.workflow.splitlines()
                            if line.strip() != '- ".gitignore"')
        self.assertNotEqual(fixture, self.workflow, "the fixture must differ from the real workflow")
        filters = workflow_path_filters(fixture)
        self.assertTrue(filters)
        inventory = registered_dependencies()
        for patterns in filters.values():
            self.assertIn(".gitignore", uncovered_dependencies(inventory, patterns))

    # ---- A2-4: literal exact-head CI checkout ---- #
    def test_both_jobs_check_out_the_literal_exact_head(self):
        self.assertEqual(checkout_head_binding_problems(self.workflow), [])
        blocks = workflow_job_blocks(self.workflow)
        self.assertEqual(sorted(blocks), ["ubuntu-focused", "windows-full-suite"])

    def test_checkout_ref_resolves_pr_head_for_pull_request_and_github_sha_otherwise(self):
        for job, block in workflow_job_blocks(self.workflow).items():
            checkout_step = block.split("actions/checkout@", 1)[1].split("\n      - ", 1)[0]
            ref = re.search(r"^\s*ref:\s*(.+)$", checkout_step, re.M)
            self.assertIsNotNone(ref, job)
            expression = ref.group(1)
            # One expression that selects the PR head on pull_request and github.sha on
            # workflow_dispatch, where the pull_request context is empty.
            self.assertIn("github.event.pull_request.head.sha", expression, job)
            self.assertIn("github.sha", expression, job)
            self.assertNotIn("merge_commit_sha", expression, job)

    def test_exact_head_assertion_is_required_and_precedes_all_work(self):
        for job, block in workflow_job_blocks(self.workflow).items():
            self.assertIn(EXACT_HEAD_STEP_NAME, block, job)
            assertion_at = block.index(EXACT_HEAD_STEP_NAME)
            for later in ("PowerShell parse check", "python -m unittest", "_run_ci_full_suite"):
                position = block.find(later)
                if position != -1:
                    self.assertLess(assertion_at, position, "%s: %s" % (job, later))
            assertion_step = block[assertion_at:].split("\n      - ", 1)[0]
            self.assertNotIn("continue-on-error", assertion_step, job)
            self.assertIn("rev-parse HEAD", assertion_step, job)
            # It reports the two SHAs and nothing else; no environment dump.
            for dump in ("env |", "Get-ChildItem Env:", "printenv", "${{ toJSON("):
                self.assertNotIn(dump, assertion_step, job)

    def test_default_unqualified_checkout_fixture_fails(self):
        fixture = "\n".join(line for line in self.workflow.splitlines()
                            if not re.match(r"^\s*ref:\s*\$\{\{", line))
        self.assertNotEqual(fixture, self.workflow)
        problems = checkout_head_binding_problems(fixture)
        self.assertTrue(any(p.endswith("checkout_without_explicit_ref") for p in problems), problems)

    def test_merge_sha_assertion_fixture_fails(self):
        fixture = self.workflow.replace("github.event.pull_request.head.sha",
                                        "github.event.pull_request.merge_commit_sha")
        self.assertNotEqual(fixture, self.workflow)
        problems = checkout_head_binding_problems(fixture)
        self.assertTrue(any("merge" in p for p in problems), problems)

    def test_missing_or_masked_assertion_fixtures_fail(self):
        removed = self.workflow.replace(EXACT_HEAD_STEP_NAME, "Unrelated step name")
        self.assertTrue(any(p.endswith("missing_exact_head_assertion")
                            for p in checkout_head_binding_problems(removed)))
        masked = self.workflow.replace('name: %s' % EXACT_HEAD_STEP_NAME,
                                       'name: %s\n        continue-on-error: true' % EXACT_HEAD_STEP_NAME)
        self.assertNotEqual(masked, self.workflow)
        self.assertTrue(any(p.endswith("assertion_continue_on_error")
                            for p in checkout_head_binding_problems(masked)),
                        checkout_head_binding_problems(masked))

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
        # The library's injectable helpers are never bound to a script parameter: every
        # -Root argument in the executable path is the fixed canonical root.
        script_text = read_repo_text("probe_script")
        self.assertIn("New-ExpiryProbeTrustedRootLease -Root $script:ExpiryProbeStateRoot", script_text)
        self.assertEqual(script_text.count("New-ExpiryProbeTrustedRootLease"), 1)
        self.assertEqual(script_text.count("-Root $script:ExpiryProbeStateRoot"), 2)
        self.assertEqual(script_text.count("-Root "), 2)
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
        self.assertEqual(lib_info["fileMoveCount"], 0,
                         "publication uses the native write-through move, not System.IO.File")
        self.assertEqual(lib_info["fileDeleteCount"], 0)
        script_info = self._inspect(SCRIPT)
        self.assertEqual(script_info["fileMoveCount"], 0)
        self.assertEqual(script_info["fileDeleteCount"], 0)


if __name__ == "__main__":
    unittest.main()
