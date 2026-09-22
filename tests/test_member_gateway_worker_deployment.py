"""Deployment-tooling split and immutable worker blob contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_MISSING = object()


PROTECTED_WORKER_BLOBS = {
    "scripts/install_ac2_member_gateway_worker.ps1": "fee4fac43185eaf893966e176d23216ac9544b7f",
    "scripts/ac2_member_gateway_worker.ps1": "27f0a3f8c78ba9b391b1a09ad33fe5805f723cc1",
    "scripts/ac2_member_gateway_worker_lib.ps1": "332af5a25f2be996694fdb6cef085139192ebf19",
    "scripts/ac2_member_gateway_autocount_adapter.ps1": "37ea54fea46c57671b02cbc15a138791a2977244",
    "scripts/launch_ac2_member_gateway_worker.ps1": "a913dffc6191a8f8945b6427652bab963ab34d70",
    "scripts/test_ac2_member_gateway_autocount_dependencies.ps1": "2143f59739a413b80e4745bc23053fefade09486",
}

PROTECTED_WORKER_TREES = {
    "member_gateway": "1114248f3c2663cb88bb06c48e810ef786de5cc1",
}

WORKER_SCRIPTS = tuple(PROTECTED_WORKER_BLOBS)

ALLOWED_FILES = {
    ".github/workflows/member-gateway-tests.yml",
    "config/ac2_member_gateway_worker.production.example.json",
    "config/member_forms_gateway_bounded_import.v2.template.json",
    "docs/autocount2-automation/member_gateway_production_runbook.md",
    "n8n-workflows/README.md",
    "n8n-workflows/scripts/README.md",
    "n8n-workflows/scripts/import-member-forms-gateway-bounded.ps1",
    "scripts/install_ac2_member_gateway_worker.ps1",
    "scripts/launch_ac2_member_gateway_worker.ps1",
    "scripts/test_ac2_member_gateway_autocount_dependencies.ps1",
    "tests/test_member_gateway_worker_deployment.py",
    "tests/test_member_gateway_bounded_import_security.py",
    "tests/test_member_gateway_ci.py",
}


_CANONICAL_AUTCOUNT_PROBE = r'''[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$AssemblyRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSEdition -ne "Desktop" -or $PSVersionTable.PSVersion.Major -ne 5) {
    throw "autocount_windows_powershell_5_required"
}
if (-not [Environment]::Is64BitProcess) { throw "autocount_64bit_powershell_required" }

$root = [IO.Path]::GetFullPath($AssemblyRoot)
if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw "autocount_assembly_path_invalid" }
$requiredAssemblies = @(
    "AutoCount.dll",
    "AutoCount.Accounting.dll",
    "AutoCount.Invoicing.dll",
    "AutoCount.ImportExport.dll",
    "AutoCount.Tools.dll"
)
$loaded = @{}
foreach ($name in $requiredAssemblies) {
    $path = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "autocount_assembly_missing" }
    $loaded[$name] = [Reflection.Assembly]::ReflectionOnlyLoadFrom($path)
}

$core = $loaded["AutoCount.dll"]
$invoicing = $loaded["AutoCount.Invoicing.dll"]
$requiredTypes = @(
    @($core, "AutoCount.Data.DBSetting"),
    @($core, "AutoCount.Authentication.UserSession"),
    @($invoicing, "AutoCount.BonusPoint.Member.MemberCommand")
)
foreach ($requirement in $requiredTypes) {
    if ($null -eq $requirement[0].GetType($requirement[1], $false, $false)) { throw "autocount_required_type_missing" }
}

$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)
$methodNames = @($memberCommand.GetMethods() | ForEach-Object Name)
foreach ($name in @("Create", "GetMember", "NewMember", "SaveMember")) {
    if ($methodNames -notcontains $name) { throw "autocount_required_method_missing" }
}

[pscustomobject]@{
    status = "dependency_probe_pass"
    powershell_major = $PSVersionTable.PSVersion.Major
    process_bitness = 64
    assembly_count = $requiredAssemblies.Count
    required_type_count = $requiredTypes.Count
    required_method_count = 4
} | ConvertTo-Json -Compress
'''


_NATIVE_AST_INSPECTOR = r'''[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SourcePath
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$ExpectedNodeCounts = [ordered]@{
    ArrayExpressionAst = 7
    ArrayLiteralAst = 6
    AssignmentStatementAst = 11
    AttributeAst = 2
    BinaryExpressionAst = 5
    CommandAst = 6
    CommandExpressionAst = 39
    CommandParameterAst = 6
    ConstantExpressionAst = 6
    ConvertExpressionAst = 1
    ForEachStatementAst = 3
    HashtableAst = 2
    IfStatementAst = 6
    IndexExpressionAst = 5
    InvokeMemberExpressionAst = 5
    MemberExpressionAst = 8
    NamedAttributeArgumentAst = 1
    NamedBlockAst = 1
    ParamBlockAst = 1
    ParameterAst = 1
    ParenExpressionAst = 2
    PipelineAst = 33
    ScriptBlockAst = 1
    StatementBlockAst = 16
    StringConstantExpressionAst = 53
    ThrowStatementAst = 6
    TypeConstraintAst = 2
    TypeExpressionAst = 3
    UnaryExpressionAst = 3
    VariableExpressionAst = 45
}
$ExpectedReasons = @(
    "NATIVE_REQUIRED", "INPUT_LIMIT", "PARSE_ERROR", "NODE_LIMIT", "ASSEMBLIES",
    "INVOICING", "MEMBER_COMMAND", "REQUIRED_METHODS", "AST_SHAPE", "OK"
)
$SkipFingerprintProperties = @(
    "Extent", "Parent", "StaticType", "ErrorPosition", "StringConstantType"
)
$CanonicalSource = @'
__CANONICAL_SOURCE__
'@

function Convert-ToFingerprintString {
    param([AllowNull()]$Value)

    if ($null -eq $Value) { return "<null>" }
    if ($Value -is [System.Management.Automation.Language.Ast]) {
        return (Get-AstFingerprint -Ast $Value)
    }
    if ($Value -is [System.Management.Automation.VariablePath]) {
        $userPath = ([string]$Value.UserPath).ToLowerInvariant()
        $fields = @(
            "VariablePath",
            $userPath,
            "U=$([int]$Value.IsUnqualified)",
            "D=$([int]$Value.IsDriveQualified)",
            "G=$([int]$Value.IsGlobal)",
            "L=$([int]$Value.IsLocal)",
            "P=$([int]$Value.IsPrivate)",
            "S=$([int]$Value.IsScript)",
            "V=$([int]$Value.IsVariable)",
            "Q=$([int]$Value.IsUnscopedVariable)",
            "Drive=$([string]$Value.DriveName)"
        )
        return ($fields -join ":")
    }
    $type = $Value.GetType()
    if ($type.FullName -eq "System.Management.Automation.Language.TypeName") {
        return "TypeName:" + ([string]$Value.FullName)
    }
    if ($type.FullName.StartsWith("System.Tuple") -and $null -ne $type.GetProperty("Item1") -and
        $null -ne $type.GetProperty("Item2")) {
        return "Tuple2(" + (Convert-ToFingerprintString $Value.Item1) + "," +
            (Convert-ToFingerprintString $Value.Item2) + ")"
    }
    if ($Value -is [System.Collections.IEnumerable] -and -not ($Value -is [string])) {
        $items = @(
            foreach ($item in $Value) { Convert-ToFingerprintString $item }
        )
        return "[" + [string]::Join(",", [string[]]$items) + "]"
    }
    if ($Value -is [System.Enum]) {
        return "Enum:" + $type.FullName + ":" + ([string]$Value)
    }
    if ($Value -is [string]) {
        return "String:" + [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Value))
    }
    if ($Value -is [bool]) {
        return "Bool:" + ($(if ($Value) { "1" } else { "0" }))
    }
    if ($Value -is [System.ValueType]) {
        return "Value:" + $type.FullName + ":" + ([string]$Value)
    }
    return "Object:" + $type.FullName + ":" + ([string]$Value)
}

function Get-AstFingerprint {
    param([System.Management.Automation.Language.Ast]$Ast)

    $parts = New-Object System.Collections.Generic.List[string]
    [void]$parts.Add($Ast.GetType().Name)
    foreach ($propertyName in @($Ast.PSObject.Properties.Name | Sort-Object)) {
        if ($SkipFingerprintProperties -contains $propertyName) { continue }
        $property = $Ast.PSObject.Properties[$propertyName]
        [void]$parts.Add($propertyName + "=" + (Convert-ToFingerprintString $property.Value))
    }
    return ($parts -join "|")
}

function Get-AstNodes {
    param([System.Management.Automation.Language.Ast]$Root)
    return @($Root.FindAll({ param($Candidate) $true }, $true))
}

function Get-AstNodeCounts {
    param([object[]]$Nodes)
    $counts = @{}
    foreach ($node in $Nodes) {
        $name = $node.GetType().Name
        if (-not $counts.ContainsKey($name)) { $counts[$name] = 0 }
        $counts[$name]++
    }
    return $counts
}

function Test-BareTarget {
    param(
        [object]$Node,
        [string]$Name,
        [bool]$RequireBareExtent = $true
    )
    if ($Node -isnot [System.Management.Automation.Language.VariableExpressionAst]) { return $false }
    if ($Node.Splatted) { return $false }
    if (-not $Node.VariablePath.IsUnqualified) { return $false }
    if ($Node.VariablePath.UserPath -ine $Name) { return $false }
    if ($RequireBareExtent -and $Node.Extent.Text -notmatch '^\$[A-Za-z_][A-Za-z0-9_]*$') { return $false }
    return $true
}

function Get-TargetAssignments {
    param(
        [object[]]$Nodes,
        [string]$Name
    )
    foreach ($node in $Nodes) {
        if ($node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            (Test-BareTarget -Node $node.Left -Name $Name)) {
            $node
        }
    }
}

function Test-RequiredAssemblies {
    param([object[]]$Nodes)
    $expected = @(
        "AutoCount.dll",
        "AutoCount.Accounting.dll",
        "AutoCount.Invoicing.dll",
        "AutoCount.ImportExport.dll",
        "AutoCount.Tools.dll"
    )
    $assignments = @(Get-TargetAssignments -Nodes $Nodes -Name "requiredAssemblies")
    if ($assignments.Count -ne 1) { return $false }
    $assignment = $assignments[0]
    if ([string]$assignment.Operator -ne "Equals") { return $false }
    if ($assignment.Right -isnot [System.Management.Automation.Language.CommandExpressionAst]) { return $false }
    $arrayExpression = $assignment.Right.Expression
    if ($arrayExpression -isnot [System.Management.Automation.Language.ArrayExpressionAst]) { return $false }
    $block = $arrayExpression.SubExpression
    if ($block -isnot [System.Management.Automation.Language.StatementBlockAst]) { return $false }
    $statements = @($block.Statements)
    if ($statements.Count -ne 1 -or $statements[0] -isnot [System.Management.Automation.Language.PipelineAst]) { return $false }
    $pipeline = $statements[0]
    $elements = @($pipeline.PipelineElements)
    if ($elements.Count -ne 1 -or $elements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) { return $false }
    $literal = $elements[0].Expression
    if ($literal -isnot [System.Management.Automation.Language.ArrayLiteralAst]) { return $false }
    $literalElements = @($literal.Elements)
    if ($literalElements.Count -ne $expected.Count) { return $false }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if ($literalElements[$index] -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { return $false }
        if ([string]$literalElements[$index].Value -cne $expected[$index]) { return $false }
    }
    return $true
}

function Test-Invoicing {
    param([object[]]$Nodes)
    $assignments = @(Get-TargetAssignments -Nodes $Nodes -Name "invoicing")
    if ($assignments.Count -ne 1) { return $false }
    $assignment = $assignments[0]
    if ([string]$assignment.Operator -ne "Equals") { return $false }
    if ($assignment.Right -isnot [System.Management.Automation.Language.CommandExpressionAst]) { return $false }
    $index = $assignment.Right.Expression
    if ($index -isnot [System.Management.Automation.Language.IndexExpressionAst]) { return $false }
    if ($index.Target -isnot [System.Management.Automation.Language.VariableExpressionAst]) { return $false }
    if ($index.Target.Splatted -or -not $index.Target.VariablePath.IsUnqualified -or
        $index.Target.VariablePath.UserPath -ine "loaded") { return $false }
    if ($index.Index -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { return $false }
    return [string]$index.Index.Value -ceq "AutoCount.Invoicing.dll"
}

function Test-BooleanVariable {
    param([object]$Node, [bool]$Expected)
    if ($Node -isnot [System.Management.Automation.Language.VariableExpressionAst]) { return $false }
    if ($Node.Splatted -or -not $Node.VariablePath.IsUnqualified) { return $false }
    $expectedName = $(if ($Expected) { "true" } else { "false" })
    return $Node.VariablePath.UserPath -ceq $expectedName
}

function Test-MemberCommand {
    param([object[]]$Nodes)
    $assignments = @(Get-TargetAssignments -Nodes $Nodes -Name "memberCommand")
    if ($assignments.Count -ne 1) { return $false }
    $assignment = $assignments[0]
    if ([string]$assignment.Operator -ne "Equals") { return $false }
    if ($assignment.Right -isnot [System.Management.Automation.Language.CommandExpressionAst]) { return $false }
    $invoke = $assignment.Right.Expression
    if ($invoke -isnot [System.Management.Automation.Language.InvokeMemberExpressionAst]) { return $false }
    if ($invoke.Static) { return $false }
    if ($invoke.Expression -isnot [System.Management.Automation.Language.VariableExpressionAst]) { return $false }
    if ($invoke.Expression.Splatted -or -not $invoke.Expression.VariablePath.IsUnqualified -or
        $invoke.Expression.VariablePath.UserPath -ine "invoicing") { return $false }
    if ($invoke.Member -isnot [System.Management.Automation.Language.StringConstantExpressionAst] -or
        [string]$invoke.Member.Value -cne "GetType") { return $false }
    $arguments = @($invoke.Arguments)
    if ($arguments.Count -ne 3) { return $false }
    if ($arguments[0] -isnot [System.Management.Automation.Language.StringConstantExpressionAst] -or
        [string]$arguments[0].Value -cne "AutoCount.BonusPoint.Member.MemberCommand") { return $false }
    return (Test-BooleanVariable -Node $arguments[1] -Expected $true) -and
        (Test-BooleanVariable -Node $arguments[2] -Expected $false)
}

function Test-RequiredMethods {
    param([object[]]$Nodes)
    $expected = @("Create", "GetMember", "NewMember", "SaveMember")
    $matches = @()
    foreach ($loop in $Nodes) {
        if ($loop -isnot [System.Management.Automation.Language.ForEachStatementAst]) { continue }
        if (-not (Test-BareTarget -Node $loop.Variable -Name "name" -RequireBareExtent $false)) { continue }
        if ($loop.Condition -isnot [System.Management.Automation.Language.PipelineAst]) { continue }
        $pipelineElements = @($loop.Condition.PipelineElements)
        if ($pipelineElements.Count -ne 1 -or
            $pipelineElements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) { continue }
        $arrayExpression = $pipelineElements[0].Expression
        if ($arrayExpression -isnot [System.Management.Automation.Language.ArrayExpressionAst]) { continue }
        $block = $arrayExpression.SubExpression
        if ($block -isnot [System.Management.Automation.Language.StatementBlockAst]) { continue }
        $statements = @($block.Statements)
        if ($statements.Count -ne 1 -or $statements[0] -isnot [System.Management.Automation.Language.PipelineAst]) { continue }
        $innerElements = @($statements[0].PipelineElements)
        if ($innerElements.Count -ne 1 -or
            $innerElements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) { continue }
        $literal = $innerElements[0].Expression
        if ($literal -isnot [System.Management.Automation.Language.ArrayLiteralAst]) { continue }
        $values = @($literal.Elements)
        if ($values.Count -ne $expected.Count) { continue }
        $same = $true
        for ($index = 0; $index -lt $expected.Count; $index++) {
            if ($values[$index] -isnot [System.Management.Automation.Language.StringConstantExpressionAst] -or
                [string]$values[$index].Value -cne $expected[$index]) {
                $same = $false
                break
            }
        }
        if ($same) { $matches += $loop }
    }
    return $matches.Count -eq 1
}

function Emit-Result {
    param(
        [bool]$Native51X64,
        [bool]$ParseOk,
        [int]$ParseErrors,
        [int]$NodeCount,
        [bool]$GlobalShapeOk,
        [bool]$AssembliesOk,
        [bool]$InvoicingOk,
        [bool]$MemberCommandOk,
        [bool]$RequiredMethodsOk,
        [string]$Reason
    )
    $accepted = $ParseOk -and $ParseErrors -eq 0 -and $GlobalShapeOk -and
        $AssembliesOk -and $InvoicingOk -and $MemberCommandOk -and
        $RequiredMethodsOk -and $Reason -eq "OK"
    $result = [ordered]@{
        schema_version = 1
        native51_x64 = $Native51X64
        parse_ok = $ParseOk
        parse_errors = $ParseErrors
        node_count = $NodeCount
        global_shape_ok = $GlobalShapeOk
        assemblies_ok = $AssembliesOk
        invoicing_ok = $InvoicingOk
        member_command_ok = $MemberCommandOk
        required_methods_ok = $RequiredMethodsOk
        accepted = $accepted
        reason = $Reason
    }
    [Console]::Out.Write(($result | ConvertTo-Json -Compress))
}

try {
    $nativeOk = $PSVersionTable.PSEdition -eq "Desktop" -and
        $PSVersionTable.PSVersion.Major -eq 5 -and [Environment]::Is64BitProcess
    if (-not $nativeOk) {
        Emit-Result -Native51X64 $false -ParseOk $false -ParseErrors 0 -NodeCount 0 `
            -GlobalShapeOk $false -AssembliesOk $false -InvoicingOk $false `
            -MemberCommandOk $false -RequiredMethodsOk $false -Reason "NATIVE_REQUIRED"
        exit 0
    }
    if (-not [IO.File]::Exists($SourcePath)) {
        Emit-Result -Native51X64 $true -ParseOk $false -ParseErrors 0 -NodeCount 0 `
            -GlobalShapeOk $false -AssembliesOk $false -InvoicingOk $false `
            -MemberCommandOk $false -RequiredMethodsOk $false -Reason "INPUT_LIMIT"
        exit 0
    }
    $inputBytes = [IO.File]::ReadAllBytes($SourcePath)
    if ($inputBytes.Length -gt 65536) {
        Emit-Result -Native51X64 $true -ParseOk $false -ParseErrors 0 -NodeCount 0 `
            -GlobalShapeOk $false -AssembliesOk $false -InvoicingOk $false `
            -MemberCommandOk $false -RequiredMethodsOk $false -Reason "INPUT_LIMIT"
        exit 0
    }

    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $SourcePath, [ref]$tokens, [ref]$parseErrors
    )
    $parseErrorCount = @($parseErrors).Count
    if ($parseErrorCount -gt 0) {
        Emit-Result -Native51X64 $true -ParseOk $false -ParseErrors $parseErrorCount -NodeCount 0 `
            -GlobalShapeOk $false -AssembliesOk $false -InvoicingOk $false `
            -MemberCommandOk $false -RequiredMethodsOk $false -Reason "PARSE_ERROR"
        exit 0
    }

    $nodes = @(Get-AstNodes -Root $ast)
    if ($nodes.Count -gt 4096) {
        Emit-Result -Native51X64 $true -ParseOk $true -ParseErrors 0 -NodeCount $nodes.Count `
            -GlobalShapeOk $false -AssembliesOk $false -InvoicingOk $false `
            -MemberCommandOk $false -RequiredMethodsOk $false -Reason "NODE_LIMIT"
        exit 0
    }
    foreach ($node in $nodes) {
        $depth = 0
        $parent = $node.Parent
        while ($null -ne $parent) {
            $depth++
            if ($depth -gt 128) {
                Emit-Result -Native51X64 $true -ParseOk $true -ParseErrors 0 -NodeCount $nodes.Count `
                    -GlobalShapeOk $false -AssembliesOk $false -InvoicingOk $false `
                    -MemberCommandOk $false -RequiredMethodsOk $false -Reason "NODE_LIMIT"
                exit 0
            }
            $parent = $parent.Parent
        }
    }

    $canonicalTokens = $null
    $canonicalErrors = $null
    $canonicalAst = [System.Management.Automation.Language.Parser]::ParseInput(
        $CanonicalSource, [ref]$canonicalTokens, [ref]$canonicalErrors
    )
    if (@($canonicalErrors).Count -ne 0) {
        Emit-Result -Native51X64 $true -ParseOk $true -ParseErrors 0 -NodeCount $nodes.Count `
            -GlobalShapeOk $false -AssembliesOk $false -InvoicingOk $false `
            -MemberCommandOk $false -RequiredMethodsOk $false -Reason "AST_SHAPE"
        exit 0
    }
    $canonicalNodes = @(Get-AstNodes -Root $canonicalAst)
    $counts = Get-AstNodeCounts -Nodes $nodes
    $globalShapeOk = $nodes.Count -eq 286 -and $canonicalNodes.Count -eq 286
    if ($globalShapeOk -and $counts.Count -eq $ExpectedNodeCounts.Count) {
        foreach ($name in $ExpectedNodeCounts.Keys) {
            if (-not $counts.ContainsKey($name) -or $counts[$name] -ne $ExpectedNodeCounts[$name]) {
                $globalShapeOk = $false
                break
            }
        }
    } else {
        $globalShapeOk = $false
    }
    if ($globalShapeOk) {
        $candidateFingerprint = Get-AstFingerprint -Ast $ast
        $canonicalFingerprint = Get-AstFingerprint -Ast $canonicalAst
        $globalShapeOk = $candidateFingerprint -ceq $canonicalFingerprint
    }

    $assembliesOk = Test-RequiredAssemblies -Nodes $nodes
    $invoicingOk = Test-Invoicing -Nodes $nodes
    $memberCommandOk = Test-MemberCommand -Nodes $nodes
    $requiredMethodsOk = Test-RequiredMethods -Nodes $nodes

    if (-not $assembliesOk) { $reason = "ASSEMBLIES" }
    elseif (-not $invoicingOk) { $reason = "INVOICING" }
    elseif (-not $memberCommandOk) { $reason = "MEMBER_COMMAND" }
    elseif (-not $requiredMethodsOk) { $reason = "REQUIRED_METHODS" }
    elseif (-not $globalShapeOk) { $reason = "AST_SHAPE" }
    else { $reason = "OK" }
    Emit-Result -Native51X64 $true -ParseOk $true -ParseErrors 0 -NodeCount $nodes.Count `
        -GlobalShapeOk $globalShapeOk -AssembliesOk $assembliesOk -InvoicingOk $invoicingOk `
        -MemberCommandOk $memberCommandOk -RequiredMethodsOk $requiredMethodsOk -Reason $reason
} catch {
    Emit-Result -Native51X64 $true -ParseOk $false -ParseErrors 0 -NodeCount 0 `
        -GlobalShapeOk $false -AssembliesOk $false -InvoicingOk $false `
        -MemberCommandOk $false -RequiredMethodsOk $false -Reason "AST_SHAPE"
    exit 0
}
'''


def _resolve_native_powershell() -> str | None:
    if os.name != "nt":
        return None
    system_root = Path(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows")
    system_path = "Sysnative" if __import__("struct").calcsize("P") == 4 else "System32"
    candidates = (
        system_root / system_path / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _write_deterministic_powershell_file(path: Path, source: str) -> None:
    normalised = source.replace("\r\n", "\n").replace("\r", "\n")
    path.write_bytes(b"\xef\xbb\xbf" + normalised.encode("utf-8"))


def _bounded_native_process(command: list[str]) -> tuple[int, bytes, bytes, bool, bool]:
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()

    def drain(stream, buffer: bytearray) -> None:
        while True:
            chunk = stream.read(1024)
            if not chunk:
                return
            if len(buffer) < 4097:
                buffer.extend(chunk[: 4097 - len(buffer)])
            if len(buffer) > 4096:
                overflow.set()

    stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    deadline = time.monotonic() + 15.0
    while process.poll() is None:
        if overflow.is_set():
            process.kill()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            process.kill()
            break
        time.sleep(0.01)
    try:
        return_code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        return_code = process.wait(timeout=5)
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    return return_code, bytes(stdout), bytes(stderr), timed_out, overflow.is_set()


def _strict_json_object(payload: bytes) -> dict[str, object]:
    if payload.endswith(b"\r\n"):
        payload = payload[:-2]
    elif payload.endswith(b"\n"):
        payload = payload[:-1]
    if not payload or b"\r" in payload or b"\n" in payload:
        raise AssertionError("native AST inspector emitted non-compact stdout")

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AssertionError(f"native AST inspector emitted duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_nonfinite_numbers(value):
        raise AssertionError(f"native AST inspector emitted nonfinite number: {value}")

    try:
        decoded = payload.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite_numbers,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AssertionError("native AST inspector emitted invalid JSON") from error
    if not isinstance(value, dict):
        raise AssertionError("native AST inspector did not emit one JSON object")
    return value


def _validate_native_result(result: dict[str, object]) -> dict[str, object]:
    expected_fields = {
        "schema_version",
        "native51_x64",
        "parse_ok",
        "parse_errors",
        "node_count",
        "global_shape_ok",
        "assemblies_ok",
        "invoicing_ok",
        "member_command_ok",
        "required_methods_ok",
        "accepted",
        "reason",
    }
    if set(result) != expected_fields:
        raise AssertionError("native AST inspector JSON fields do not match the contract")
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise AssertionError("native AST inspector schema version is invalid")
    boolean_fields = (
        "native51_x64",
        "parse_ok",
        "global_shape_ok",
        "assemblies_ok",
        "invoicing_ok",
        "member_command_ok",
        "required_methods_ok",
        "accepted",
    )
    for field in boolean_fields:
        if type(result[field]) is not bool:
            raise AssertionError(f"native AST inspector field is not Boolean: {field}")
    for field in ("parse_errors", "node_count"):
        if type(result[field]) is not int or result[field] < 0:
            raise AssertionError(f"native AST inspector field is not a nonnegative integer: {field}")
    if not isinstance(result["reason"], str) or result["reason"] not in {
        "NATIVE_REQUIRED",
        "INPUT_LIMIT",
        "PARSE_ERROR",
        "NODE_LIMIT",
        "ASSEMBLIES",
        "INVOICING",
        "MEMBER_COMMAND",
        "REQUIRED_METHODS",
        "AST_SHAPE",
        "OK",
    }:
        raise AssertionError("native AST inspector reason is invalid")
    if result["reason"] == "PARSE_ERROR":
        if result["parse_ok"] or result["parse_errors"] <= 0:
            raise AssertionError("native AST inspector parse error result is inconsistent")
    elif result["parse_errors"] != 0:
        raise AssertionError("native AST inspector reported parse errors for a non-parse result")
    expected_accepted = (
        result["reason"] == "OK"
        and result["native51_x64"]
        and result["parse_ok"]
        and result["parse_errors"] == 0
        and result["global_shape_ok"]
        and result["assemblies_ok"]
        and result["invoicing_ok"]
        and result["member_command_ok"]
        and result["required_methods_ok"]
    )
    if result["accepted"] is not expected_accepted:
        raise AssertionError("native AST inspector accepted field is inconsistent")
    return result


def _inspect_autocount_probe(probe: str) -> dict[str, object]:
    native_powershell = _resolve_native_powershell()
    if native_powershell is None:
        raise unittest.SkipTest("native Windows PowerShell Desktop 5.1 x64 is required")
    temporary_root = Path(tempfile.mkdtemp(prefix="xb-member-worker-ast-"))
    try:
        try:
            if os.path.commonpath((str(ROOT.resolve()), str(temporary_root.resolve()))) == str(ROOT.resolve()):
                raise AssertionError("native AST inspector temp directory is inside the checkout")
        except ValueError as error:
            raise AssertionError("native AST inspector temp directory could not be validated") from error
        inspector_path = temporary_root / "inspector.ps1"
        candidate_path = temporary_root / "candidate.ps1"
        inspector_source = _NATIVE_AST_INSPECTOR.replace(
            "__CANONICAL_SOURCE__", _CANONICAL_AUTCOUNT_PROBE
        )
        _write_deterministic_powershell_file(inspector_path, inspector_source)
        _write_deterministic_powershell_file(candidate_path, probe)
        command = [
            native_powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(inspector_path.resolve()),
            "-SourcePath",
            str(candidate_path.resolve()),
        ]
        return_code, stdout, stderr, timed_out, overflow = _bounded_native_process(command)
        if timed_out:
            raise AssertionError("native AST inspector timed out")
        if overflow:
            raise AssertionError("native AST inspector exceeded bounded output")
        if stderr:
            raise AssertionError("native AST inspector wrote to stderr")
        if return_code != 0:
            raise AssertionError(f"native AST inspector exited with status {return_code}")
        if len(stdout) > 4096:
            raise AssertionError("native AST inspector stdout exceeded 4096 bytes")
        return _validate_native_result(_strict_json_object(stdout))
    finally:
        try:
            shutil.rmtree(temporary_root)
        except OSError as error:
            raise AssertionError("native AST inspector temporary cleanup failed") from error


def _assert_autocount_probe_contract(probe: str) -> None:
    result = _inspect_autocount_probe(probe)
    if not result["accepted"]:
        raise AssertionError(f"native AST contract rejected candidate: {result['reason']}")


def _inspect_many(probes: list[str]) -> list[dict[str, object]]:
    worker_count = min(8, max(1, os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(_inspect_autocount_probe, probes))


def _identity_forms(name: str) -> list[tuple[str, bool]]:
    case_forms = [
        name,
        name.upper(),
        name.lower(),
        name.title(),
        "".join(character.upper() if index % 2 else character.lower() for index, character in enumerate(name)),
        name.swapcase(),
    ]
    forms: list[tuple[str, bool]] = [("$" + value, True) for value in case_forms]
    forms.extend(
        [
            ("${" + name + "}", False),
            ("${" + name.upper() + "}", False),
            ("${" + name.title() + "}", False),
        ]
    )
    for scope in ("local", "script", "private", "global", "variable"):
        forms.extend(
            [
                (f"${scope}:{name}", False),
                (f"${scope.upper()}:{name.upper()}", False),
                (f"${{{scope}:{name}}}", False),
                (f"${{{scope}:{name.upper()}}}", False),
            ]
        )
    escaped_positions = range(len(name))
    forms.extend(
        [
            ("${" + name[:position] + "`" + name[position:] + "}", False)
            for position in escaped_positions
        ]
    )
    unique: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for form, is_bare in forms:
        if form not in seen:
            unique.append((form, is_bare))
            seen.add(form)
    if len(unique) > 36:
        unique = unique[:36]
    if len(unique) != 36:
        raise AssertionError(f"identity fixture generator produced {len(unique)} forms for {name}")
    return unique


def _canonical_assignment_lines(source: str, name: str) -> tuple[list[str], int]:
    lines = source.splitlines()
    prefix = f"${name} ="
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            return lines, index
    raise AssertionError(f"canonical assignment not found: {name}")


def _assignment_fixture(source: str, name: str, target: str, operator: str) -> str:
    lines, assignment_index = _canonical_assignment_lines(source, name)
    if operator == "=":
        lines[assignment_index] = lines[assignment_index].replace(f"${name} =", f"{target} =", 1)
    else:
        end_index = assignment_index
        if name == "requiredAssemblies":
            while not lines[end_index].strip() == ")":
                end_index += 1
        replacement = f"{target} {operator} $null"
        if operator in {"prefix++", "prefix--"}:
            replacement = f"{operator[-2:]}{target}"
        elif operator in {"postfix++", "postfix--"}:
            replacement = f"{target}{operator[-2:]}"
        lines[assignment_index : end_index + 1] = [replacement]
    return "\n".join(lines) + "\n"


def _context_fixture(source: str, mutation: str, context: str) -> str:
    if context == "top-level statement":
        suffix = mutation
    elif context == "expandable-string $()":
        suffix = '"$({})"'.format(mutation)
    elif context == "expandable here-string $()":
        suffix = '@"\nmatrix\n$({})\n"@'.format(mutation)
    elif context == "nested $($())":
        suffix = '"$($({}))"'.format(mutation)
    elif context == "invoked scriptblock":
        suffix = "& { " + mutation + " }"
    elif context == "Write-Output (mutation)":
        suffix = "Write-Output (" + mutation + ")"
    else:
        raise AssertionError(f"unknown mutation context: {context}")
    return source.rstrip() + "\n" + suffix + "\n"


def _identity_matrix_fixture(
    source: str,
    name: str,
    identity: str,
    operator: str,
    context: str,
) -> str:
    if context == "top-level statement":
        return _assignment_fixture(source, name, identity, operator)
    if operator in {"prefix++", "prefix--"}:
        mutation = f"{operator[:-2]}{identity}"
    elif operator in {"postfix++", "postfix--"}:
        mutation = f"{identity}{operator[-2:]}"
    else:
        mutation = f"{identity} {operator} $null"
    return _context_fixture(source, mutation, context)


def _indirect_mutations() -> list[str]:
    value = "$loaded['AutoCount.Tools.dll']"
    return [
        f"Set-Variable -Name 'invoicing' -Value {value}",
        f"sv -Name 'invoicing' -Value {value}",
        f"set -Name 'invoicing' -Value {value}",
        f"Microsoft.PowerShell.Utility\\Set-Variable -Name 'invoicing' -Value {value}",
        f"& ('Set-Variable') -Name 'invoicing' -Value {value}",
        f"Set-Item variable:invoicing -Value {value}",
        f"si variable:invoicing -Value {value}",
        f"(Get-Variable -Name 'invoicing').Value = {value}",
        f"$ExecutionContext.SessionState.PSVariable.Set('invoicing', {value})",
        f"([ref]$invoicing).Value = {value}",
        "Write-Output 1 -OutVariable invoicing",
        "Write-Output 1 -ov invoicing",
        "Write-Output 1 -PipelineVariable invoicing | ForEach-Object { $_ }",
        "Write-Output 1 -pv invoicing | ForEach-Object { $_ }",
        "Invoke-Expression '$invoicing = $loaded[\"AutoCount.Tools.dll\"]'",
        "iex '$invoicing = $loaded[\"AutoCount.Tools.dll\"]'",
        f"function Set-XbInvoicing {{ Set-Variable -Name 'invoicing' -Value {value} }}; Set-XbInvoicing",
        f"Set-Alias xbSet Set-Variable; xbSet -Name 'invoicing' -Value {value}",
        f". {{ $invoicing = {value} }}",
        f"& {{ $invoicing = {value} }}",
        "Clear-Variable -Name invoicing",
        "Remove-Variable -Name invoicing",
        f"New-Variable -Name invoicing -Value {value} -Force",
        f"Set-Content variable:invoicing -Value {value}",
        f"Write-Output 1 | Tee-Object -Variable invoicing",
    ]


_ASSEMBLY_VALUES = (
    "AutoCount.dll",
    "AutoCount.Accounting.dll",
    "AutoCount.Invoicing.dll",
    "AutoCount.ImportExport.dll",
    "AutoCount.Tools.dll",
)
_METHOD_VALUES = ("Create", "GetMember", "NewMember", "SaveMember")


def _array_assignment_block(name: str, elements: list[str]) -> str:
    rendered = [f"    {element}" + ("," if index < len(elements) - 1 else "") for index, element in enumerate(elements)]
    return f"${name} = @(\n" + "\n".join(rendered) + "\n)"


def _replace_assembly_block(source: str, elements: list[str]) -> str:
    start = source.index("$requiredAssemblies = @(")
    close = source.index("\n)", start)
    return source[:start] + _array_assignment_block("requiredAssemblies", elements) + source[close + 2 :]


def _replace_assembly_expression(source: str, expression: str) -> str:
    start = source.index("$requiredAssemblies = @(")
    close = source.index("\n)", start)
    return source[:start] + f"$requiredAssemblies = {expression}" + source[close + 2 :]


def _replace_method_condition(source: str, condition: str) -> str:
    start = source.index("foreach ($name in @(")
    brace = source.index("{", start)
    return source[:start] + f"foreach ($name in {condition}) " + source[brace:]


def _collection_variants(source: str) -> list[tuple[str, str]]:
    assembly_literals = [f'"{value}"' for value in _ASSEMBLY_VALUES]
    variants: list[tuple[str, str]] = []
    for index in range(len(assembly_literals)):
        variants.append((f"assembly omission {index}", _replace_assembly_block(source, assembly_literals[:index] + assembly_literals[index + 1 :])))
        replacement = assembly_literals.copy()
        replacement[index] = '"AutoCount.Extended.dll"'
        variants.append((f"assembly replacement {index}", _replace_assembly_block(source, replacement)))
        variable = assembly_literals.copy()
        variable[index] = "$assemblyName"
        variants.append((f"assembly variable {index}", _replace_assembly_block(source, variable)))
        binary = assembly_literals.copy()
        binary[index] = '"AutoCount" + ".dll"'
        variants.append((f"assembly binary {index}", _replace_assembly_block(source, binary)))
        subexpression = assembly_literals.copy()
        subexpression[index] = '$("AutoCount.dll")'
        variants.append((f"assembly subexpression {index}", _replace_assembly_block(source, subexpression)))
        expandable = assembly_literals.copy()
        expandable[index] = '"AutoCount.$($assemblySuffix)"'
        variants.append((f"assembly expandable string {index}", _replace_assembly_block(source, expandable)))
        nested = assembly_literals.copy()
        nested[index] = '@("AutoCount.dll")'
        variants.append((f"assembly nested array {index}", _replace_assembly_block(source, nested)))
    for index in range(len(assembly_literals) - 1):
        transposed = assembly_literals.copy()
        transposed[index], transposed[index + 1] = transposed[index + 1], transposed[index]
        variants.append((f"assembly adjacent transposition {index}", _replace_assembly_block(source, transposed)))
    for index, literal in enumerate(assembly_literals):
        duplicate = assembly_literals[:index] + [literal, literal] + assembly_literals[index + 1 :]
        variants.append((f"assembly duplicate {index}", _replace_assembly_block(source, duplicate)))
    variants.extend(
        [
            (
                "assembly sixth element",
                _replace_assembly_block(source, assembly_literals + ['"AutoCount.Extended.dll"']),
            ),
            (
                "assembly flattened comma output",
                _replace_assembly_expression(source, ", ".join(assembly_literals)),
            ),
            (
                "assembly concatenated arrays",
                _replace_assembly_expression(source, " + ".join(f"@({literal})" for literal in assembly_literals)),
            ),
            (
                "assembly variable-backed collection",
                _replace_assembly_expression(source, "$assemblyValues")
                .replace(
                    "$requiredAssemblies = $assemblyValues",
                    _array_assignment_block("assemblyValues", assembly_literals) + "\n$requiredAssemblies = $assemblyValues",
                    1,
                ),
            ),
            (
                "assembly command-built collection",
                _replace_assembly_expression(source, "Get-ChildItem -LiteralPath $root -Name"),
            ),
            (
                "assembly duplicate assignment",
                source.rstrip() + "\n$requiredAssemblies = @()\n",
            ),
            (
                "assembly semicolon flattened output",
                _replace_assembly_expression(source, "; ".join(assembly_literals)),
            ),
        ]
    )

    method_literals = [f'"{value}"' for value in _METHOD_VALUES]
    for index in range(len(method_literals)):
        omitted = method_literals[:index] + method_literals[index + 1 :]
        variants.append((f"method omission {index}", _replace_method_condition(source, "@(" + ", ".join(omitted) + ")")))
        replacement = method_literals.copy()
        replacement[index] = '"ReplaceMember"'
        variants.append((f"method replacement {index}", _replace_method_condition(source, "@(" + ", ".join(replacement) + ")")))
        variable = method_literals.copy()
        variable[index] = "$methodName"
        variants.append((f"method variable {index}", _replace_method_condition(source, "@(" + ", ".join(variable) + ")")))
        binary = method_literals.copy()
        binary[index] = '"Save" + "Member"'
        variants.append((f"method binary {index}", _replace_method_condition(source, "@(" + ", ".join(binary) + ")")))
        subexpression = method_literals.copy()
        subexpression[index] = '$("Create")'
        variants.append((f"method subexpression {index}", _replace_method_condition(source, "@(" + ", ".join(subexpression) + ")")))
        expandable = method_literals.copy()
        expandable[index] = '"$($methodName)"'
        variants.append((f"method expandable string {index}", _replace_method_condition(source, "@(" + ", ".join(expandable) + ")")))
        nested = method_literals.copy()
        nested[index] = '@("Create")'
        variants.append((f"method nested array {index}", _replace_method_condition(source, "@(" + ", ".join(nested) + ")")))
    for index in range(len(method_literals) - 1):
        transposed = method_literals.copy()
        transposed[index], transposed[index + 1] = transposed[index + 1], transposed[index]
        variants.append((f"method adjacent transposition {index}", _replace_method_condition(source, "@(" + ", ".join(transposed) + ")")))
    for index, literal in enumerate(method_literals):
        duplicate = method_literals[:index] + [literal, literal] + method_literals[index + 1 :]
        variants.append((f"method duplicate {index}", _replace_method_condition(source, "@(" + ", ".join(duplicate) + ")")))
    variants.extend(
        [
            (
                "method fifth element",
                _replace_method_condition(source, "@(\"Create\", \"GetMember\", \"NewMember\", \"SaveMember\", \"ExtraMember\")"),
            ),
            (
                "method flattened comma output",
                _replace_method_condition(source, ", ".join(method_literals)),
            ),
            (
                "method variable-backed collection",
                _replace_method_condition(source, "$requiredMethodNames").replace(
                    "foreach ($name in $requiredMethodNames)",
                    _array_assignment_block("requiredMethodNames", method_literals)
                    + "\nforeach ($name in $requiredMethodNames)",
                    1,
                ),
            ),
            (
                "method command-built collection",
                _replace_method_condition(source, "(Get-Content -LiteralPath $path)"),
            ),
            (
                "method duplicate loop",
                source.rstrip()
                + "\nforeach ($name in @(\"Create\", \"GetMember\", \"NewMember\", \"SaveMember\")) {\n}\n",
            ),
            (
                "method replacement collection",
                _replace_method_condition(source, "$methodNames"),
            ),
        ]
    )
    return variants


def _insert_after(source: str, needle: str, addition: str) -> str:
    if source.count(needle) != 1:
        raise AssertionError(f"fixture anchor is not unique: {needle}")
    return source.replace(needle, needle + "\n" + addition, 1)


def _member_resolver_variants(source: str) -> list[tuple[str, str]]:
    member_line = '$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)'
    variants = [
        (
            "core receiver",
            source.replace(
                "$memberCommand = $invoicing.GetType(",
                "$memberCommand = $core.GetType(",
                1,
            ),
        ),
        (
            "tools receiver",
            source.replace(
                member_line,
                '$memberCommand = $loaded["AutoCount.Tools.dll"].GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)',
                1,
            ),
        ),
        (
            "loaded values scan",
            _insert_after(
                source,
                member_line,
                '$alternateMemberCommand = @($loaded.Values | ForEach-Object { $_.GetType("AutoCount.BonusPoint.Member.MemberCommand", $false, $false) } | Where-Object { $null -ne $_ } | Select-Object -First 1)',
            ),
        ),
        (
            "AppDomain loaded assembly enumeration",
            _insert_after(
                source,
                member_line,
                '$alternateMemberCommand = [AppDomain]::CurrentDomain.GetAssemblies() | ForEach-Object { $_.GetType("AutoCount.BonusPoint.Member.MemberCommand", $false, $false) }',
            ),
        ),
        (
            "reflection-only enumeration",
            _insert_after(
                source,
                member_line,
                '$alternateMemberCommand = [Reflection.Assembly]::ReflectionOnlyLoadFrom($path).GetTypes() | Where-Object FullName -eq "AutoCount.BonusPoint.Member.MemberCommand"',
            ),
        ),
        (
            "fallback branch",
            _insert_after(
                source,
                member_line,
                'if ($null -eq $memberCommand) { $memberCommand = $core.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false) }',
            ),
        ),
        (
            "Type.GetType",
            source.replace(
                member_line,
                '$memberCommand = [Type]::GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)',
                1,
            ),
        ),
        (
            "reflected invocation",
            source.replace(
                member_line,
                '$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false).Invoke($null, $null)',
                1,
            ),
        ),
        (
            "dynamic member invocation",
            source.replace(
                member_line,
                '$memberName = "GetType"; $memberCommand = $invoicing.$memberName("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)',
                1,
            ),
        ),
        (
            "function resolution",
            _insert_after(
                source,
                member_line,
                'function Resolve-XbMemberCommand { $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false) }; $memberCommand = Resolve-XbMemberCommand',
            ),
        ),
        (
            "alias resolution",
            _insert_after(
                source,
                member_line,
                'Set-Alias xbResolve Get-Member; $memberCommand = xbResolve',
            ),
        ),
        (
            "scriptblock resolution",
            _insert_after(
                source,
                member_line,
                '$resolver = { $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false) }; $memberCommand = & $resolver',
            ),
        ),
        (
            "additional memberCommand assignment",
            _insert_after(
                source,
                member_line,
                '$memberCommand = $core.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)',
            ),
        ),
        (
            "altered requiredTypes receiver",
            source.replace(
                '@($invoicing, "AutoCount.BonusPoint.Member.MemberCommand")',
                '@($core, "AutoCount.BonusPoint.Member.MemberCommand")',
                1,
            ),
        ),
        (
            "AssemblyResolve",
            _insert_after(
                source,
                member_line,
                '[AppDomain]::CurrentDomain.add_AssemblyResolve({ param($sender, $args) $null })',
            ),
        ),
        (
            "Get-ChildItem",
            _insert_after(source, member_line, 'Get-ChildItem -LiteralPath $root -File'),
        ),
        (
            "IO.Directory.GetFiles",
            _insert_after(source, member_line, '[IO.Directory]::GetFiles($root)'),
        ),
        (
            "EnumerateFiles",
            _insert_after(source, member_line, '[IO.Directory]::EnumerateFiles($root)'),
        ),
        (
            "GetFileSystemEntries",
            _insert_after(source, member_line, '[IO.Directory]::GetFileSystemEntries($root)'),
        ),
        (
            "dot-sourced alternate resolution",
            _insert_after(
                source,
                member_line,
                '. { $memberCommand = $core.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false) }',
            ),
        ),
    ]
    return variants


def _positive_variants(source: str) -> list[tuple[str, str]]:
    variants = [("canonical", source)]
    variants.append(
        (
            "harmless whitespace and comments",
            "# leading comment\n\n"
            + source.replace(
                "$requiredAssemblies = @(",
                "# collection comment\n$requiredAssemblies = @(\n",
                1,
            ),
        )
    )
    for value in _ASSEMBLY_VALUES:
        variants.append(
            (
                f"single-quoted assembly {value}",
                source.replace(f'"{value}"', f"'{value}'", 1),
            )
        )
    for value in _METHOD_VALUES:
        variants.append(
            (
                f"single-quoted method {value}",
                source.replace(f'"{value}"', f"'{value}'", 1),
            )
        )
    variants.append(("all literals single-quoted", source.replace('"', "'")))
    escaped = source
    for value in _ASSEMBLY_VALUES + _METHOD_VALUES:
        escaped = escaped.replace(f'"{value}"', '"`' + value[0] + value[1:] + '"', 1)
    variants.append(("native-decoded safe literal escapes", escaped))
    variants.append(
        (
            "case-only invoicing identifier",
            source.replace(
                '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
                '$INVOICING = $loaded["AutoCount.Invoicing.dll"]',
                1,
            ),
        )
    )
    variants.append(
        (
            "case-only required assembly identifier",
            source.replace("$requiredAssemblies = @(", "$REQUIREDASSEMBLIES = @(", 1),
        )
    )
    return variants


def _parser_invalid_variants(source: str) -> list[tuple[str, str]]:
    return [
        ("malformed string", source + '\n$invalid = "unterminated\n'),
        ("malformed delimiter", source + "\nif ($true { throw 'x' }\n"),
        ("malformed assignment", source + "\n$invalid = (\n"),
        ("Windows PowerShell 5.1 invalid ??=", source + "\n$invoicing ??= $null\n"),
    ]


class MemberGatewayWorkerDeploymentTests(unittest.TestCase):
    @staticmethod
    def _blob(path: str) -> str:
        return subprocess.run(
            ["git", "hash-object", path],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @staticmethod
    def _tree(path: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", f"HEAD:{path}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @classmethod
    def setUpClass(cls) -> None:
        cls.pwsh = _resolve_native_powershell()

    @staticmethod
    def _desktop_powershell_environment() -> dict[str, str]:
        env = os.environ.copy()
        module_path_key = next((key for key in env if key.casefold() == "psmodulepath"), None)
        module_path = env.get(module_path_key) if module_path_key else None
        if module_path and module_path_key:
            env[module_path_key] = os.pathsep.join(
                path
                for path in module_path.split(os.pathsep)
                if "native\\powershell\\modules" not in path.casefold()
            )
        return env

    def _run_powershell_harness(self, script: str, *arguments: str, env: dict[str, str] | None = None):
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-harness-") as temp_dir:
            harness = Path(temp_dir) / "harness.ps1"
            harness.write_text(script, encoding="utf-8", newline="\n")
            return subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(harness),
                    *arguments,
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )

    def test_protected_worker_blobs_are_unchanged(self) -> None:
        for path, expected in PROTECTED_WORKER_BLOBS.items():
            with self.subTest(path=path):
                self.assertEqual(self._blob(path), expected)
        for path, expected in PROTECTED_WORKER_TREES.items():
            with self.subTest(path=path):
                self.assertEqual(self._tree(path), expected)

    def test_approved_worker_scripts_parse_without_execution(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for parser validation")
        for relative in WORKER_SCRIPTS:
            command = (
                "$errors = $null; $tokens = $null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path '{relative}'), "
                "[ref]$tokens, [ref]$errors); "
                "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
            )
            with self.subTest(path=relative):
                completed = subprocess.run(
                    [self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_autocount_probe_binds_member_command_to_invoicing(self) -> None:
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        _assert_autocount_probe_contract(probe)

        rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Tools.dll"]',
            1,
        )
        subsequent_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]\n'
            '$invoicing = $loaded["AutoCount.Tools.dll"]',
            1,
        )
        case_insensitive_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]\n'
            '$InVoIcInG = $loaded["AutoCount.Tools.dll"]',
            1,
        )
        compound_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]\n'
            '$invoicing += $loaded["AutoCount.Tools.dll"]',
            1,
        )
        indirect_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]\n'
            "Set-Variable -Name 'invoicing' -Value $loaded[\"AutoCount.Tools.dll\"]",
            1,
        )
        dynamic_indirect_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]\n'
            "& ('Set-Variable') -Name 'invoicing' -Value $loaded[\"AutoCount.Tools.dll\"]",
            1,
        )
        alternate_scan = probe.replace(
            '$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)',
            '$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)\n'
            '$alternateMemberCommand = @($loaded.Values | ForEach-Object { $_.GetType("AutoCount.BonusPoint.Member.MemberCommand", $false, $false) } | Where-Object { $null -ne $_ } | Select-Object -First 1)\n'
            'if ($null -eq $memberCommand) { $memberCommand = $alternateMemberCommand }',
            1,
        )
        sixth_assembly = probe.replace(
            '    "AutoCount.Tools.dll"\n)',
            '    "AutoCount.Tools.dll",\n    "AutoCount.Extended.dll"\n)',
            1,
        )
        sixth_single_assembly = probe.replace(
            '    "AutoCount.Tools.dll"\n)',
            "    \"AutoCount.Tools.dll\",\n    'AutoCount.Extended.dll'\n)",
            1,
        )
        sixth_non_literal_assembly = probe.replace(
            '    "AutoCount.Tools.dll"\n)',
            '    "AutoCount.Tools.dll",\n    ("AutoCount.Extended" + ".dll")\n)',
            1,
        )
        expandable_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]\n'
            '"$($invoicing = $loaded[\'AutoCount.Tools.dll\'])"',
            1,
        )
        escaped_braced_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '${invoi`cing} = $loaded["AutoCount.Tools.dll"]',
            1,
        )
        fifth_single_method = probe.replace(
            'foreach ($name in @("Create", "GetMember", "NewMember", "SaveMember"))',
            'foreach ($name in @("Create", "GetMember", "NewMember", "SaveMember", \'FifthSingleQuoted\'))',
            1,
        )

        for name, counterexample in (
            ("wrong invoicing binding", rebind),
            ("subsequent invoicing rebinding", subsequent_rebind),
            ("case-insensitive invoicing rebinding", case_insensitive_rebind),
            ("compound invoicing rebinding", compound_rebind),
            ("indirect invoicing rebinding", indirect_rebind),
            ("dynamic indirect invoicing rebinding", dynamic_indirect_rebind),
            ("alternate assembly MemberCommand scan", alternate_scan),
            ("sixth double-quoted required assembly", sixth_assembly),
            ("sixth single-quoted required assembly", sixth_single_assembly),
            ("sixth non-literal required assembly", sixth_non_literal_assembly),
            ("expandable-string invoicing rebinding", expandable_rebind),
            ("escaped-braced invoicing rebinding", escaped_braced_rebind),
            ("fifth single-quoted required method", fifth_single_method),
        ):
            with self.subTest(counterexample=name):
                result = _inspect_autocount_probe(counterexample)
                self.assertTrue(result["parse_ok"], result)
                self.assertFalse(result["accepted"], result)

    def test_g2_identity_operator_context_matrix(self) -> None:
        if not self.pwsh:
            self.skipTest("native Windows PowerShell Desktop 5.1 x64 is required")
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        operators = ("=", "+=", "-=", "*=", "/=", "%=", "prefix++", "postfix++", "prefix--", "postfix--")
        contexts = (
            "top-level statement",
            "expandable-string $()",
            "expandable here-string $()",
            "nested $($())",
            "invoked scriptblock",
            "Write-Output (mutation)",
        )
        cases: list[tuple[str, str, str, int, int, bool, str]] = []
        fixtures: list[str] = []
        for name in ("invoicing", "requiredAssemblies"):
            for identity_index, (identity, is_bare) in enumerate(_identity_forms(name)):
                for operator_index, operator in enumerate(operators):
                    context = contexts[(identity_index + operator_index) % len(contexts)]
                    fixtures.append(_identity_matrix_fixture(probe, name, identity, operator, context))
                    cases.append((name, identity, operator, identity_index, operator_index, is_bare, context))
        self.assertEqual(len(fixtures), 720)
        results = _inspect_many(fixtures)
        for case, result in zip(cases, results):
            name, identity, operator, identity_index, operator_index, is_bare, context = case
            with self.subTest(
                name=name,
                identity=identity,
                operator=operator,
                context=context,
            ):
                self.assertTrue(result["parse_ok"], result)
                expected_acceptance = is_bare and operator == "=" and context == "top-level statement"
                self.assertEqual(result["accepted"], expected_acceptance, result)
        self.assertEqual(sum(bool(result["accepted"]) for result in results), 2)

    def test_g2_indirect_mutation_matrix(self) -> None:
        if not self.pwsh:
            self.skipTest("native Windows PowerShell Desktop 5.1 x64 is required")
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        mutations = _indirect_mutations()
        self.assertEqual(len(mutations), 25)
        contexts = ("top-level statement", "expandable-string $()")
        fixtures = [
            _context_fixture(probe, mutation, context)
            for mutation in mutations
            for context in contexts
        ]
        self.assertEqual(len(fixtures), 50)
        results = _inspect_many(fixtures)
        for index, (mutation, context, result) in enumerate(zip(
            (mutation for mutation in mutations for _ in contexts),
            (context for _ in mutations for context in contexts),
            results,
        )):
            with self.subTest(index=index, mutation=mutation, context=context):
                self.assertTrue(result["parse_ok"], result)
                self.assertFalse(result["accepted"], result)

    def test_g2_collection_and_method_matrix(self) -> None:
        if not self.pwsh:
            self.skipTest("native Windows PowerShell Desktop 5.1 x64 is required")
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        variants = _collection_variants(probe)
        self.assertEqual(len(variants), 92)
        results = _inspect_many([source for _, source in variants])
        for (label, _), result in zip(variants, results):
            with self.subTest(variant=label):
                self.assertTrue(result["parse_ok"], result)
                self.assertFalse(result["accepted"], result)

    def test_g2_member_command_and_resolver_matrix(self) -> None:
        if not self.pwsh:
            self.skipTest("native Windows PowerShell Desktop 5.1 x64 is required")
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        variants = _member_resolver_variants(probe)
        self.assertEqual(len(variants), 20)
        results = _inspect_many([source for _, source in variants])
        for (label, _), result in zip(variants, results):
            with self.subTest(variant=label):
                self.assertTrue(result["parse_ok"], result)
                self.assertFalse(result["accepted"], result)

    def test_g2_positive_controls(self) -> None:
        if not self.pwsh:
            self.skipTest("native Windows PowerShell Desktop 5.1 x64 is required")
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        variants = _positive_variants(probe)
        self.assertEqual(len(variants), 15)
        results = _inspect_many([source for _, source in variants])
        for (label, _), result in zip(variants, results):
            with self.subTest(variant=label):
                self.assertTrue(result["parse_ok"], result)
                self.assertTrue(result["accepted"], result)

    def test_g2_parser_integrity_and_resource_limits(self) -> None:
        if not self.pwsh:
            self.skipTest("native Windows PowerShell Desktop 5.1 x64 is required")
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        parser_invalid = _parser_invalid_variants(probe)
        parser_results = _inspect_many([source for _, source in parser_invalid])
        for (label, _), result in zip(parser_invalid, parser_results):
            with self.subTest(fixture=label):
                self.assertFalse(result["parse_ok"], result)
                self.assertEqual(result["reason"], "PARSE_ERROR", result)
                self.assertFalse(result["accepted"], result)

        oversized = _inspect_autocount_probe("x" * 65537)
        self.assertEqual(oversized["reason"], "INPUT_LIMIT", oversized)
        self.assertFalse(oversized["accepted"], oversized)

        ast_heavy = probe + ("\n" + "if ($true) { }" * 900)
        node_limited = _inspect_autocount_probe(ast_heavy)
        self.assertEqual(node_limited["reason"], "NODE_LIMIT", node_limited)
        self.assertFalse(node_limited["accepted"], node_limited)

    def test_native_inspector_transport_and_schema_contract(self) -> None:
        if not self.pwsh:
            self.skipTest("native Windows PowerShell Desktop 5.1 x64 is required")
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        result = _inspect_autocount_probe(probe)
        self.assertEqual(
            set(result),
            {
                "schema_version",
                "native51_x64",
                "parse_ok",
                "parse_errors",
                "node_count",
                "global_shape_ok",
                "assemblies_ok",
                "invoicing_ok",
                "member_command_ok",
                "required_methods_ok",
                "accepted",
                "reason",
            },
        )
        for payload in (
            b'{"schema_version":1,"schema_version":2}',
            b'{"value":NaN}',
            b"\xff\xfe\xfd",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(AssertionError):
                    _strict_json_object(payload)

        unknown_field = dict(result)
        unknown_field["unexpected"] = False
        with self.assertRaises(AssertionError):
            _validate_native_result(unknown_field)
        wrong_boolean = dict(result)
        wrong_boolean["accepted"] = 1
        with self.assertRaises(AssertionError):
            _validate_native_result(wrong_boolean)
        inconsistent = dict(result)
        inconsistent["accepted"] = False
        with self.assertRaises(AssertionError):
            _validate_native_result(inconsistent)

    def test_production_launcher_accepts_exact_strings_and_preserves_dpapi_mapping(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-runtime-") as temp_dir:
            runtime = Path(temp_dir)
            (runtime / "config").mkdir()
            (runtime / "secrets").mkdir()
            config = {
                "gateway_base_url": "https://gateway.example.test",
                "worker_host_binding": "host-ac2-worker",
                "autocount_assembly_path": r"C:\\AutoCount",
                "autocount_server_name": "  server exact  ",
                "autocount_database_name": "database exact",
                "autocount_user_id": "user exact",
            }
            (runtime / "config" / "worker.config.json").write_text(json.dumps(config), encoding="utf-8")
            launcher_text = (ROOT / "scripts/launch_ac2_member_gateway_worker.ps1").read_text(encoding="utf-8")
            self.assertIn("Import-Clixml -LiteralPath $Path", launcher_text)
            self.assertIn("$secureValue -isnot [Security.SecureString]", launcher_text)
            harness = r'''
$launcher = Get-Content -Raw -LiteralPath $args[0]
$prefix = $launcher.Substring(0, $launcher.IndexOf('$runId = '))
Invoke-Expression $prefix
function Read-XbCurrentUserSecretArtifact {
    param([Parameter(Mandatory)][string]$Path)
    if ($Path -like '*worker-token.clixml') { return 'token' }
    return 'password'
}
$info = New-XbWorkerProcessStartInfo -WorkerScript $args[1] -LauncherMode Production -RuntimeRootPath $args[2]
[ordered]@{
    server = $info.EnvironmentVariables['XB_AC2_SERVER_NAME']
    database = $info.EnvironmentVariables['XB_AC2_DATABASE_NAME']
    user = $info.EnvironmentVariables['XB_AC2_USER_ID']
    probe_server = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_SERVER_NAME')
    probe_database = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_DATABASE_NAME')
    probe_user = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_USER_ID')
    probe_password = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_PASSWORD')
    session_factory = $info.EnvironmentVariables.ContainsKey('XB_AC2_SESSION_FACTORY')
    password = $info.EnvironmentVariables['XB_AC2_PASSWORD']
    password_env = $info.EnvironmentVariables['XB_AC2_PASSWORD_ENV_VAR']
} | ConvertTo-Json -Compress
'''
            env = os.environ.copy()
            env.update(
                {
                    "AC2_PROBE_SERVER_NAME": "legacy-server",
                    "AC2_PROBE_DATABASE_NAME": "legacy-database",
                    "AC2_PROBE_USER_ID": "legacy-user",
                    "AC2_PROBE_PASSWORD": "legacy-password",
                    "XB_AC2_SESSION_FACTORY": "legacy-factory",
                }
            )
            completed = self._run_powershell_harness(
                harness,
                str(ROOT / "scripts/launch_ac2_member_gateway_worker.ps1"),
                r"C:\synthetic-worker.ps1",
                str(runtime),
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            observed = json.loads(completed.stdout)
            self.assertEqual(observed["server"], "  server exact  ")
            self.assertEqual(observed["database"], "database exact")
            self.assertEqual(observed["user"], "user exact")
            for name in ("probe_server", "probe_database", "probe_user", "probe_password", "session_factory"):
                self.assertFalse(observed[name], name)
            self.assertEqual(observed["password"], "password")
            self.assertEqual(observed["password_env"], "XB_AC2_PASSWORD")

    def test_invalid_production_identity_cannot_start_synthetic_child(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        invalid_values = (_MISSING, None, 7, [], "", "   ")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-invalid-") as temp_dir:
            root = Path(temp_dir)
            install = root / "install"
            runtime = root / "runtime"
            (runtime / "config").mkdir(parents=True)
            (runtime / "secrets").mkdir()
            install.mkdir()
            marker = root / "child-started.txt"
            (install / "ac2_member_gateway_worker.ps1").write_text(
                "param([switch]$EnableProductionWorker, [switch]$EnableProductionAdapter)\n"
                "$marker = [Environment]::GetEnvironmentVariable('XB_TEST_CHILD_MARKER', 'Process')\n"
                "if ([string]::IsNullOrWhiteSpace($marker)) { throw 'synthetic_marker_missing' }\n"
                "[IO.File]::WriteAllText($marker, 'started')\n"
                "[pscustomobject]@{ status = 'completed'; writes = 0 } | ConvertTo-Json -Compress\n",
                encoding="utf-8",
            )
            base_config = {
                "gateway_base_url": "https://gateway.example.test",
                "worker_host_binding": "host-ac2-worker",
                "autocount_assembly_path": r"C:\\AutoCount",
                "autocount_server_name": "synthetic-server",
                "autocount_database_name": "synthetic-database",
                "autocount_user_id": "synthetic-user",
            }
            (runtime / "config" / "worker.config.json").write_text(
                json.dumps(base_config), encoding="utf-8"
            )
            secret_environment = self._desktop_powershell_environment()
            secret_environment["XB_TEST_WORKER_TOKEN_PATH"] = str(
                runtime / "secrets" / "worker-token.clixml"
            )
            secret_environment["XB_TEST_PASSWORD_PATH"] = str(
                runtime / "secrets" / "autocount-password.clixml"
            )
            secrets = subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$ErrorActionPreference = 'Stop'; "
                    "$workerToken = [Security.SecureString]::new(); "
                    "'synthetic-worker-token'.ToCharArray() | ForEach-Object { $workerToken.AppendChar($_) }; "
                    "$workerToken.MakeReadOnly(); $workerToken | "
                    "Export-Clixml -LiteralPath $env:XB_TEST_WORKER_TOKEN_PATH; "
                    "$autocountPassword = [Security.SecureString]::new(); "
                    "'synthetic-autocount-password'.ToCharArray() | ForEach-Object { $autocountPassword.AppendChar($_) }; "
                    "$autocountPassword.MakeReadOnly(); $autocountPassword | "
                    "Export-Clixml -LiteralPath $env:XB_TEST_PASSWORD_PATH",
                ],
                cwd=ROOT,
                env=secret_environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(secrets.returncode, 0, secrets.stdout + secrets.stderr)
            self.assertTrue((runtime / "secrets" / "worker-token.clixml").is_file())
            self.assertTrue((runtime / "secrets" / "autocount-password.clixml").is_file())
            env = self._desktop_powershell_environment()
            env["XB_TEST_CHILD_MARKER"] = str(marker)
            launcher = ROOT / "scripts/launch_ac2_member_gateway_worker.ps1"

            control = subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(launcher),
                    "-Mode",
                    "Production",
                    "-InstallRoot",
                    str(install),
                    "-RuntimeRoot",
                    str(runtime),
                    "-ExecutionTimeoutMilliseconds",
                    "5000",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
            control_result = json.loads(control.stdout)
            self.assertEqual(control_result["exit_code"], 0)
            self.assertEqual(control_result["terminal_status"], "worker_completed")
            self.assertTrue(marker.exists(), control.stdout + control.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "started")

            for field in ("autocount_server_name", "autocount_database_name", "autocount_user_id"):
                for value in invalid_values:
                    with self.subTest(field=field, value=value):
                        config = dict(base_config)
                        if value is _MISSING:
                            config.pop(field)
                        else:
                            config[field] = value
                        (runtime / "config" / "worker.config.json").write_text(
                            json.dumps(config), encoding="utf-8"
                        )
                        if marker.exists():
                            marker.unlink()
                        completed = subprocess.run(
                            [
                                self.pwsh,
                                "-ExecutionPolicy",
                                "Bypass",
                                "-NoLogo",
                                "-NoProfile",
                                "-NonInteractive",
                                "-File",
                                str(launcher),
                                "-Mode",
                                "Production",
                                "-InstallRoot",
                                str(install),
                                "-RuntimeRoot",
                                str(runtime),
                                "-ExecutionTimeoutMilliseconds",
                                "5000",
                            ],
                            cwd=ROOT,
                            env=env,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                        result = json.loads(completed.stdout)
                        self.assertEqual(result["exit_code"], 1)
                        self.assertEqual(result["terminal_status"], "launcher_failed")
                        self.assertFalse(marker.exists(), completed.stdout + completed.stderr)

    def test_disabled_proof_does_not_read_production_config_or_secrets(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-disabled-") as temp_dir:
            root = Path(temp_dir)
            install = root / "install"
            runtime = root / "runtime"
            install.mkdir()
            (install / "ac2_member_gateway_worker.ps1").write_text(
                "[pscustomobject]@{ status = 'disabled'; writes = 0; dispatch_fence = $false } | ConvertTo-Json -Compress\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(ROOT / "scripts/launch_ac2_member_gateway_worker.ps1"),
                    "-Mode",
                    "DisabledProof",
                    "-InstallRoot",
                    str(install),
                    "-RuntimeRoot",
                    str(runtime),
                    "-ExecutionTimeoutMilliseconds",
                    "5000",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            observed = json.loads(completed.stdout)
            self.assertEqual(observed["terminal_status"], "disabled_proof_pass")

    def test_production_launcher_binds_exact_identity_and_removes_legacy_environment(self) -> None:
        launcher = (ROOT / "scripts/launch_ac2_member_gateway_worker.ps1").read_text(encoding="utf-8")
        fields = {
            "autocount_server_name": "launcher_autocount_server_name_invalid",
            "autocount_database_name": "launcher_autocount_database_name_invalid",
            "autocount_user_id": "launcher_autocount_user_id_invalid",
        }
        for field, error_id in fields.items():
            with self.subTest(field=field):
                self.assertIn(f'PropertyName "{field}"', launcher)
                self.assertIn(error_id, launcher)
        for environment_name in (
            "XB_AC2_SERVER_NAME",
            "XB_AC2_DATABASE_NAME",
            "XB_AC2_USER_ID",
        ):
            self.assertIn(f'EnvironmentVariables["{environment_name}"]', launcher)
        for environment_name in (
            "AC2_PROBE_SERVER_NAME",
            "AC2_PROBE_DATABASE_NAME",
            "AC2_PROBE_USER_ID",
            "AC2_PROBE_PASSWORD",
            "XB_AC2_SESSION_FACTORY",
        ):
            self.assertIn(f'EnvironmentVariables.Remove("{environment_name}")', launcher)
        self.assertIn('$value -isnot [string]', launcher)
        self.assertIn('[string]::IsNullOrWhiteSpace($value)', launcher)
        self.assertNotIn('[string]$config.autocount_server_name', launcher)
        self.assertNotIn('[string]$config.autocount_database_name', launcher)
        self.assertNotIn('[string]$config.autocount_user_id', launcher)
        self.assertLess(
            launcher.index('$autocountServerName = Get-XbRequiredProductionConfigString'),
            launcher.index('if (-not $process.Start())'),
        )

    def test_worker_production_example_has_unbound_identity_placeholders(self) -> None:
        example = json.loads(
            (ROOT / "config/ac2_member_gateway_worker.production.example.json").read_text(encoding="utf-8")
        )
        for field in (
            "gateway_base_url",
            "worker_host_binding",
            "autocount_assembly_path",
            "autocount_server_name",
            "autocount_database_name",
            "autocount_user_id",
        ):
            self.assertIn(field, example)
        for field in ("autocount_server_name", "autocount_database_name", "autocount_user_id"):
            self.assertIsNone(example[field])

    def test_worktree_change_allowlist_is_narrow(self) -> None:
        status = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        for line in status:
            self.assertGreaterEqual(len(line), 4, line)
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            self.assertIn(path.replace("\\", "/"), ALLOWED_FILES, line)


if __name__ == "__main__":
    unittest.main()
