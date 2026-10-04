"""Deployment-tooling split and immutable worker blob contracts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import base64
from concurrent.futures import ThreadPoolExecutor
from itertools import product
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
_MISSING = object()

NODE_COUNTS = (
    -1, 0, 1, 128, 129, 130, 131, 200,
    286, 287, 288, 4095, 4096, 4097,
    2_147_483_647, 2_147_483_648,
)

PARSE_ERRORS = (
    -1, 0, 1, 2_147_483_647, 2_147_483_648,
)

BOOLEAN_ORDER = (
    "native51_x64",
    "parse_ok",
    "global_shape_ok",
    "assemblies_ok",
    "invoicing_ok",
    "member_command_ok",
    "required_methods_ok",
    "accepted",
)


PROTECTED_WORKER_BLOBS = {
    "scripts/install_ac2_member_gateway_worker.ps1": "de99933e6dbc91e084d9576130a068f9348dcdef",
    "scripts/ac2_member_gateway_worker.ps1": "ba8aeb169e86ae42dfe007f43c1d3786e63c13c0",
    "scripts/ac2_member_gateway_worker_lib.ps1": "8293559ec6308a8d9afdf73fd4c8b0af322ae382",
    "scripts/ac2_member_gateway_autocount_adapter.ps1": "82120047e7d07bbe3e484c3207892b4c9859b3df",
    "scripts/launch_ac2_member_gateway_worker.ps1": "64174f398cac61cc2a19f35eaf64070a7b10be0d",
    "scripts/test_ac2_member_gateway_autocount_dependencies.ps1": "4ac24e79c23a70d20ef78cdff921a230c80cff05",
    "scripts/ac2_member_create_primitive.ps1": "2043de36df57159b4d362c17c93f5989eba2b190",
}

PROTECTED_WORKER_TREES = {
    "member_gateway": "08caccf8751ef8851a38fe5ec9066aa4f3043717",
}

WORKER_SCRIPTS = tuple(PROTECTED_WORKER_BLOBS)

CI7_DEFECTIVE_BASELINE_COMMIT = "ef194d43cd5b2a6e56468c3381a1b44bced23d8d"
CI7_DEFECTIVE_INSTALLER_BLOB = "aa6d4f3172bbc82c69f1c4904f6e4bbf50da3741"

ALLOWED_FILES = {
    "docs/autocount2-automation/member_gateway_production_contract.md",
    "docs/autocount2-automation/member_write_v2_live_runbook.md",
    "scripts/ac2_member_create_primitive.ps1",
    "scripts/install_ac2_member_gateway_worker.ps1",
    "tests/fixtures/ac2_member_primitive/cross_contract_cases.v1.fixture",
    "tests/fixtures/ac2_member_primitive/fake_autocount.ps1",
    "tests/test_ac2_member_primitive.py",
    "tests/test_member_gateway_worker_cycle_ps.py",
    "tests/test_member_gateway_worker_deployment.py",
    "tests/test_member_write_v2_cross_contract.py",
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
foreach ($name in @("Create", "GetMember", "NewMember", "SaveMember", "LoadBrowseTable")) {
    if ($methodNames -notcontains $name) { throw "autocount_required_method_missing" }
}

[pscustomobject]@{
    status = "dependency_probe_pass"
    powershell_major = $PSVersionTable.PSVersion.Major
    process_bitness = 64
    assembly_count = $requiredAssemblies.Count
    required_type_count = $requiredTypes.Count
    required_method_count = 5
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
    StringConstantExpressionAst = 54
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
    $expected = @("Create", "GetMember", "NewMember", "SaveMember", "LoadBrowseTable")
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
    $globalShapeOk = $nodes.Count -eq 287 -and $canonicalNodes.Count -eq 287
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


_NATIVE_IDENTITY_MATRIX_WITNESS = r'''[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ManifestPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Fail-Witness {
    param([Parameter(Mandatory)][string]$Message)
    throw "identity matrix witness failed: $Message"
}

function Decode-Field {
    param([Parameter(Mandatory)][string]$Value)
    try {
        return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
    } catch {
        Fail-Witness -Message "invalid metadata encoding"
    }
}

function Test-Region {
    param(
        [Parameter(Mandatory)][System.Management.Automation.Language.Ast]$Node,
        [Parameter(Mandatory)][int]$StartLine,
        [Parameter(Mandatory)][int]$EndLine
    )
    return $Node.Extent.StartLineNumber -ge $StartLine -and
        $Node.Extent.StartLineNumber -le $EndLine
}

function Test-VariablePathIdentity {
    param(
        [Parameter(Mandatory)][System.Management.Automation.VariablePath]$Actual,
        [Parameter(Mandatory)][System.Management.Automation.VariablePath]$Expected
    )
    if ([string]$Actual.UserPath -ine [string]$Expected.UserPath) { return $false }
    foreach ($propertyName in @(
        "IsUnqualified", "IsDriveQualified", "IsGlobal", "IsLocal", "IsPrivate",
        "IsScript", "IsVariable", "IsUnscopedVariable", "DriveName"
    )) {
        if ([string]$Actual.$propertyName -cne [string]$Expected.$propertyName) {
            return $false
        }
    }
    return $true
}

function Test-ReferenceEquals {
    param(
        [AllowNull()][object]$Left,
        [AllowNull()][object]$Right
    )
    if ($null -eq $Left -or $null -eq $Right) { return $false }
    return [object]::ReferenceEquals($Left, $Right)
}

function Test-NoRedirections {
    param([Parameter(Mandatory)][object]$Node)
    $property = $Node.PSObject.Properties["Redirections"]
    if ($null -eq $property -or $null -eq $property.Value) { return $true }
    return @($property.Value).Count -eq 0
}

function Test-NoTraps {
    param([Parameter(Mandatory)][object]$Node)
    $property = $Node.PSObject.Properties["Traps"]
    if ($null -eq $property -or $null -eq $property.Value) { return $true }
    return @($property.Value).Count -eq 0
}

function Get-ExpressionPipeline {
    param([Parameter(Mandatory)][System.Management.Automation.Language.Ast]$Expression)
    $commandExpression = $Expression.Parent
    if ($commandExpression -isnot [System.Management.Automation.Language.CommandExpressionAst]) {
        return $null
    }
    if (-not (Test-ReferenceEquals -Left $commandExpression.Expression -Right $Expression)) {
        return $null
    }
    if (-not (Test-NoRedirections -Node $commandExpression)) { return $null }
    $pipeline = $commandExpression.Parent
    if ($pipeline -isnot [System.Management.Automation.Language.PipelineAst]) {
        return $null
    }
    if (-not (Test-NoRedirections -Node $pipeline)) { return $null }
    $elements = @($pipeline.PipelineElements)
    if ($elements.Count -ne 1 -or
        -not (Test-ReferenceEquals -Left $elements[0] -Right $commandExpression)) {
        return $null
    }
    return $pipeline
}

function Get-CommandPipeline {
    param([Parameter(Mandatory)][System.Management.Automation.Language.CommandAst]$Command)
    if (-not (Test-NoRedirections -Node $Command)) { return $null }
    $pipeline = $Command.Parent
    if ($pipeline -isnot [System.Management.Automation.Language.PipelineAst]) {
        return $null
    }
    if (-not (Test-NoRedirections -Node $pipeline)) { return $null }
    $elements = @($pipeline.PipelineElements)
    if ($elements.Count -ne 1 -or
        -not (Test-ReferenceEquals -Left $elements[0] -Right $Command)) {
        return $null
    }
    return $pipeline
}

function Get-OperationStatement {
    param([Parameter(Mandatory)][System.Management.Automation.Language.Ast]$Operation)
    if ($Operation -is [System.Management.Automation.Language.AssignmentStatementAst]) {
        return $Operation
    }
    if ($Operation -is [System.Management.Automation.Language.UnaryExpressionAst]) {
        return Get-ExpressionPipeline -Expression $Operation
    }
    return $null
}

function Get-SubExpression {
    param([Parameter(Mandatory)][System.Management.Automation.Language.Ast]$Statement)
    $statementBlock = $Statement.Parent
    if ($statementBlock -isnot [System.Management.Automation.Language.StatementBlockAst]) {
        return $null
    }
    $statements = @($statementBlock.Statements)
    if ($statements.Count -ne 1 -or
        -not (Test-ReferenceEquals -Left $statements[0] -Right $Statement) -or
        -not (Test-NoTraps -Node $statementBlock)) {
        return $null
    }
    $subExpression = $statementBlock.Parent
    if ($subExpression -isnot [System.Management.Automation.Language.SubExpressionAst] -or
        -not (Test-ReferenceEquals -Left $subExpression.SubExpression -Right $statementBlock)) {
        return $null
    }
    return $subExpression
}

function Test-RootStatement {
    param(
        [Parameter(Mandatory)][System.Management.Automation.Language.Ast]$Statement,
        [Parameter(Mandatory)][System.Management.Automation.Language.ScriptBlockAst]$Root
    )
    if ($null -ne $Root.Parent) { return $false }
    $namedBlock = $Statement.Parent
    if ($namedBlock -isnot [System.Management.Automation.Language.NamedBlockAst]) {
        return $false
    }
    if (-not (Test-ReferenceEquals -Left $namedBlock.Parent -Right $Root) -or
        -not (Test-ReferenceEquals -Left $Root.EndBlock -Right $namedBlock)) {
        return $false
    }
    $matches = 0
    foreach ($candidate in @($Root.EndBlock.Statements)) {
        if (Test-ReferenceEquals -Left $candidate -Right $Statement) { $matches++ }
    }
    return $matches -eq 1
}

function Get-ExpandableContext {
    param(
        [Parameter(Mandatory)][System.Management.Automation.Language.SubExpressionAst]$SubExpression,
        [Parameter(Mandatory)][string]$StringConstantType,
        [Parameter(Mandatory)][System.Management.Automation.Language.ScriptBlockAst]$Root
    )
    $expandable = $SubExpression.Parent
    if ($expandable -isnot [System.Management.Automation.Language.ExpandableStringExpressionAst]) {
        return $null
    }
    $nestedMatches = 0
    foreach ($candidate in @($expandable.NestedExpressions)) {
        if (Test-ReferenceEquals -Left $candidate -Right $SubExpression) { $nestedMatches++ }
    }
    if ($nestedMatches -ne 1 -or [string]$expandable.StringConstantType -cne $StringConstantType) {
        return $null
    }
    $pipeline = Get-ExpressionPipeline -Expression $expandable
    if ($null -eq $pipeline -or -not (Test-RootStatement -Statement $pipeline -Root $Root)) {
        return $null
    }
    return $expandable
}

function Test-WitnessContext {
    param(
        [Parameter(Mandatory)][System.Management.Automation.Language.Ast]$Node,
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][System.Management.Automation.Language.ScriptBlockAst]$Root
    )
    $statement = Get-OperationStatement -Operation $Node
    if ($null -eq $statement) { return $false }

    switch ($Context) {
        'top-level statement' {
            return Test-RootStatement -Statement $statement -Root $Root
        }
        'expandable-string $()' {
            $subExpression = Get-SubExpression -Statement $statement
            if ($null -eq $subExpression) { return $false }
            return $null -ne (Get-ExpandableContext -SubExpression $subExpression `
                -StringConstantType "DoubleQuoted" -Root $Root)
        }
        'expandable here-string $()' {
            $subExpression = Get-SubExpression -Statement $statement
            if ($null -eq $subExpression) { return $false }
            return $null -ne (Get-ExpandableContext -SubExpression $subExpression `
                -StringConstantType "DoubleQuotedHereString" -Root $Root)
        }
        'nested $($())' {
            $innerSubExpression = Get-SubExpression -Statement $statement
            if ($null -eq $innerSubExpression) { return $false }
            $innerPipeline = Get-ExpressionPipeline -Expression $innerSubExpression
            if ($null -eq $innerPipeline) { return $false }
            $outerSubExpression = Get-SubExpression -Statement $innerPipeline
            if ($null -eq $outerSubExpression) { return $false }
            return $null -ne (Get-ExpandableContext -SubExpression $outerSubExpression `
                -StringConstantType "DoubleQuoted" -Root $Root)
        }
        'invoked scriptblock' {
            $namedBlock = $statement.Parent
            if ($namedBlock -isnot [System.Management.Automation.Language.NamedBlockAst]) {
                return $false
            }
            $statements = @($namedBlock.Statements)
            if ($statements.Count -ne 1 -or
                -not (Test-ReferenceEquals -Left $statements[0] -Right $statement) -or
                -not (Test-NoTraps -Node $namedBlock)) {
                return $false
            }
            $scriptBlock = $namedBlock.Parent
            if ($scriptBlock -isnot [System.Management.Automation.Language.ScriptBlockAst] -or
                -not (Test-ReferenceEquals -Left $scriptBlock.EndBlock -Right $namedBlock) -or
                $null -ne $scriptBlock.ParamBlock -or
                $null -ne $scriptBlock.BeginBlock -or
                $null -ne $scriptBlock.ProcessBlock -or
                $null -ne $scriptBlock.DynamicParamBlock) {
                return $false
            }
            $scriptBlockExpression = $scriptBlock.Parent
            if ($scriptBlockExpression -isnot [System.Management.Automation.Language.ScriptBlockExpressionAst] -or
                -not (Test-ReferenceEquals -Left $scriptBlockExpression.ScriptBlock -Right $scriptBlock)) {
                return $false
            }
            $command = $scriptBlockExpression.Parent
            if ($command -isnot [System.Management.Automation.Language.CommandAst]) {
                return $false
            }
            $commandElements = @($command.CommandElements)
            if ($commandElements.Count -ne 1 -or
                -not (Test-ReferenceEquals -Left $commandElements[0] -Right $scriptBlockExpression) -or
                [string]$command.InvocationOperator -cne "Ampersand") {
                return $false
            }
            $pipeline = Get-CommandPipeline -Command $command
            return $null -ne $pipeline -and (Test-RootStatement -Statement $pipeline -Root $Root)
        }
        'Write-Output (mutation)' {
            $paren = $statement.Parent
            if ($paren -isnot [System.Management.Automation.Language.ParenExpressionAst] -or
                -not (Test-ReferenceEquals -Left $paren.Pipeline -Right $statement)) {
                return $false
            }
            $command = $paren.Parent
            if ($command -isnot [System.Management.Automation.Language.CommandAst]) {
                return $false
            }
            $commandElements = @($command.CommandElements)
            if ($commandElements.Count -ne 2 -or
                -not (Test-ReferenceEquals -Left $commandElements[1] -Right $paren) -or
                $commandElements[0] -isnot [System.Management.Automation.Language.StringConstantExpressionAst] -or
                [string]$commandElements[0].StringConstantType -cne "BareWord" -or
                [string]$commandElements[0].Value -cne "Write-Output" -or
                [string]$command.GetCommandName() -cne "Write-Output" -or
                [string]$command.InvocationOperator -cne "Unknown") {
                return $false
            }
            $pipeline = Get-CommandPipeline -Command $command
            return $null -ne $pipeline -and (Test-RootStatement -Statement $pipeline -Root $Root)
        }
        default {
            Fail-Witness -Message "unknown context '$Context'"
        }
    }
}

function Get-ExpectedAssignmentOperator {
    param([Parameter(Mandatory)][string]$Operator)
    $map = @{
        "=" = "Equals"
        "+=" = "PlusEquals"
        "-=" = "MinusEquals"
        "*=" = "MultiplyEquals"
        "/=" = "DivideEquals"
        "%=" = "RemainderEquals"
    }
    if (-not $map.ContainsKey($Operator)) {
        Fail-Witness -Message "unknown assignment operator '$Operator'"
    }
    return $map[$Operator]
}

function Get-ExpectedUnaryOperator {
    param([Parameter(Mandatory)][string]$Operator)
    $map = @{
        "prefix++" = "PlusPlus"
        "prefix--" = "MinusMinus"
        "postfix++" = "PostfixPlusPlus"
        "postfix--" = "PostfixMinusMinus"
    }
    if (-not $map.ContainsKey($Operator)) {
        Fail-Witness -Message "unknown unary operator '$Operator'"
    }
    return $map[$Operator]
}

function Test-OperationTarget {
    param(
        [Parameter(Mandatory)][System.Management.Automation.Language.Ast]$Operation,
        [Parameter(Mandatory)][string]$ExpectedIdentity,
        [Parameter(Mandatory)][string]$ExpectedTargetSpelling
    )
    $target = $null
    if ($Operation -is [System.Management.Automation.Language.AssignmentStatementAst]) {
        $target = $Operation.Left
    } elseif ($Operation -is [System.Management.Automation.Language.UnaryExpressionAst]) {
        $target = $Operation.Child
    }
    if ($target -isnot [System.Management.Automation.Language.VariableExpressionAst]) {
        return $false
    }
    if ($target.Splatted) { return $false }
    if ([string]$target.Extent.Text -cne $ExpectedTargetSpelling) { return $false }

    $expectedTokens = $null
    $expectedErrors = $null
    $expectedAst = [System.Management.Automation.Language.Parser]::ParseInput(
        $ExpectedIdentity + ' = $null', [ref]$expectedTokens, [ref]$expectedErrors
    )
    if (@($expectedErrors).Count -ne 0) { return $false }
    $expectedVariables = @(
        $expectedAst.FindAll({ param($Candidate)
            $Candidate -is [System.Management.Automation.Language.VariableExpressionAst]
        }, $true)
    )
    if ($expectedVariables.Count -ne 2) {
        return $false
    }
    $expectedVariable = $expectedVariables[0]
    return Test-VariablePathIdentity -Actual $target.VariablePath -Expected $expectedVariable.VariablePath
}

function Test-Case {
    param([Parameter(Mandatory)][string]$Line)
    $fields = $Line -split "\|"
    if ($fields.Count -ne 7) {
        Fail-Witness -Message "invalid metadata field count"
    }
    $sourcePath = Decode-Field -Value $fields[0]
    $identity = Decode-Field -Value $fields[1]
    $operator = Decode-Field -Value $fields[2]
    $context = Decode-Field -Value $fields[3]
    $targetSpelling = Decode-Field -Value $fields[4]
    [int]$startLine = 0
    [int]$endLine = 0
    if (-not [int]::TryParse($fields[5], [ref]$startLine) -or
        -not [int]::TryParse($fields[6], [ref]$endLine) -or
        $startLine -lt 1 -or $endLine -lt $startLine) {
        Fail-Witness -Message "invalid metadata line range"
    }
    if (-not [IO.Path]::IsPathRooted($sourcePath) -or
        -not [IO.File]::Exists($sourcePath)) {
        Fail-Witness -Message "candidate source path is invalid"
    }

    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $sourcePath, [ref]$tokens, [ref]$parseErrors
    )
    if (@($parseErrors).Count -ne 0) {
        Fail-Witness -Message "candidate parser errors for '$operator' '$context'"
    }

    $operations = @(
        $ast.FindAll({ param($Candidate)
            $Candidate -is [System.Management.Automation.Language.AssignmentStatementAst] -or
                $Candidate -is [System.Management.Automation.Language.UnaryExpressionAst]
        }, $true)
    )
    $matches = @()
    foreach ($operationNode in $operations) {
        if (-not (Test-Region -Node $operationNode -StartLine $startLine -EndLine $endLine)) {
            continue
        }
        $operatorMatches = $false
        if ($operator -in @("=", "+=", "-=", "*=", "/=", "%=")) {
            $operatorMatches = $operationNode -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                [string]$operationNode.Operator -ceq (Get-ExpectedAssignmentOperator -Operator $operator)
        } elseif ($operator -in @("prefix++", "prefix--", "postfix++", "postfix--")) {
            $operatorMatches = $operationNode -is [System.Management.Automation.Language.UnaryExpressionAst] -and
                [string]$operationNode.TokenKind -ceq (Get-ExpectedUnaryOperator -Operator $operator)
        } else {
            Fail-Witness -Message "unknown operator '$operator'"
        }
        if (-not $operatorMatches) { continue }
        if (-not (Test-OperationTarget -Operation $operationNode -ExpectedIdentity $identity -ExpectedTargetSpelling $targetSpelling)) {
            continue
        }
        $matches += $operationNode
    }
    if ($matches.Count -ne 1) {
        Fail-Witness -Message "expected one native operation for '$operator' '$context', found $($matches.Count)"
    }
    if (-not (Test-WitnessContext -Node $matches[0] -Context $context -Root $ast)) {
        Fail-Witness -Message "native operation context mismatch for '$operator' '$context'"
    }
}

if (-not [IO.Path]::IsPathRooted($ManifestPath) -or -not [IO.File]::Exists($ManifestPath)) {
    Fail-Witness -Message "manifest path is invalid"
}
$lines = @([IO.File]::ReadAllLines($ManifestPath))
if ($lines.Count -eq 0) { Fail-Witness -Message "manifest is empty" }
$caseCount = 0
foreach ($line in $lines) {
    if ([string]::IsNullOrWhiteSpace($line)) { Fail-Witness -Message "manifest contains a blank line" }
    Test-Case -Line $line
    $caseCount++
}
[Console]::Out.Write("XB_IDENTITY_MATRIX_WITNESS_OK:$caseCount")
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


def _assert_temp_outside_checkout(
    checkout: Path, temporary_root: Path
) -> None:
    if not checkout.is_absolute() or not temporary_root.is_absolute():
        raise AssertionError("native AST inspector paths must be absolute")

    resolved_root = checkout.resolve(strict=True)
    resolved_temp = temporary_root.resolve(strict=True)

    if not resolved_root.is_absolute() or not resolved_temp.is_absolute():
        raise AssertionError(
            "native AST inspector resolved paths must be absolute"
        )

    try:
        resolved_temp.relative_to(resolved_root)
    except ValueError:
        return

    raise AssertionError(
        "native AST inspector temp directory is inside the checkout"
    )


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
    if result["parse_errors"] > 2_147_483_647 or result["node_count"] > 2_147_483_647:
        raise AssertionError("native AST inspector integer exceeds the native Int32 contract")

    reason = result["reason"]
    native = result["native51_x64"]
    parse_ok = result["parse_ok"]
    parse_errors = result["parse_errors"]
    node_count = result["node_count"]
    global_shape_ok = result["global_shape_ok"]
    assemblies_ok = result["assemblies_ok"]
    invoicing_ok = result["invoicing_ok"]
    member_command_ok = result["member_command_ok"]
    required_methods_ok = result["required_methods_ok"]
    accepted = result["accepted"]

    if global_shape_ok and (node_count != 287 or not required_methods_ok):
        raise AssertionError("native AST inspector global-shape result is inconsistent")

    if reason == "NATIVE_REQUIRED":
        valid_state = (
            not native
            and not parse_ok
            and parse_errors == 0
            and node_count == 0
            and not global_shape_ok
            and not assemblies_ok
            and not invoicing_ok
            and not member_command_ok
            and not required_methods_ok
            and not accepted
        )
    elif reason == "INPUT_LIMIT":
        valid_state = (
            native
            and not parse_ok
            and parse_errors == 0
            and node_count == 0
            and not global_shape_ok
            and not assemblies_ok
            and not invoicing_ok
            and not member_command_ok
            and not required_methods_ok
            and not accepted
        )
    elif reason == "PARSE_ERROR":
        valid_state = (
            native
            and not parse_ok
            and parse_errors > 0
            and node_count == 0
            and not global_shape_ok
            and not assemblies_ok
            and not invoicing_ok
            and not member_command_ok
            and not required_methods_ok
            and not accepted
        )
    elif reason == "NODE_LIMIT":
        valid_state = (
            native
            and parse_ok
            and parse_errors == 0
            and node_count >= 130
            and not global_shape_ok
            and not assemblies_ok
            and not invoicing_ok
            and not member_command_ok
            and not required_methods_ok
            and not accepted
        )
    elif reason == "ASSEMBLIES":
        valid_state = (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and not assemblies_ok
            and not accepted
        )
    elif reason == "INVOICING":
        valid_state = (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and assemblies_ok
            and not invoicing_ok
            and not accepted
        )
    elif reason == "MEMBER_COMMAND":
        valid_state = (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and assemblies_ok
            and invoicing_ok
            and not member_command_ok
            and not accepted
        )
    elif reason == "REQUIRED_METHODS":
        valid_state = (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and not global_shape_ok
            and assemblies_ok
            and invoicing_ok
            and member_command_ok
            and not required_methods_ok
            and not accepted
        )
    elif reason == "AST_SHAPE":
        ordinary_rejection = (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and not global_shape_ok
            and assemblies_ok
            and invoicing_ok
            and member_command_ok
            and required_methods_ok
            and not accepted
        )
        canonical_reference_parse_failure = (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and not global_shape_ok
            and not assemblies_ok
            and not invoicing_ok
            and not member_command_ok
            and not required_methods_ok
            and not accepted
        )
        catch_branch = (
            native
            and not parse_ok
            and parse_errors == 0
            and node_count == 0
            and not global_shape_ok
            and not assemblies_ok
            and not invoicing_ok
            and not member_command_ok
            and not required_methods_ok
            and not accepted
        )
        valid_state = (
            ordinary_rejection
            or canonical_reference_parse_failure
            or catch_branch
        )
    else:
        valid_state = (
            native
            and parse_ok
            and parse_errors == 0
            and node_count == 287
            and global_shape_ok
            and assemblies_ok
            and invoicing_ok
            and member_command_ok
            and required_methods_ok
            and accepted
        )

    if not valid_state:
        raise AssertionError("native AST inspector reason/state combination is impossible")

    expected_accepted = reason == "OK"
    if accepted is not expected_accepted:
        raise AssertionError("native AST inspector accepted field is inconsistent")
    return result


def _native_emission_state(
    reason: str,
    boolean_values: tuple[bool, ...],
    node_count: int,
    parse_errors: int,
) -> dict[str, object]:
    if len(boolean_values) != len(BOOLEAN_ORDER):
        raise AssertionError("native emission state has the wrong Boolean arity")
    result: dict[str, object] = {
        "schema_version": 1,
        "parse_errors": parse_errors,
        "node_count": node_count,
        "reason": reason,
    }
    result.update(zip(BOOLEAN_ORDER, boolean_values))
    return result


def _expected_native_emission_valid(
    reason: str,
    boolean_values: tuple[bool, ...],
    node_count: int,
    parse_errors: int,
) -> bool:
    if len(boolean_values) != len(BOOLEAN_ORDER) or any(
        type(value) is not bool for value in boolean_values
    ):
        return False
    if not 0 <= node_count <= 2_147_483_647 or not 0 <= parse_errors <= 2_147_483_647:
        return False

    native, parse_ok, global_shape, assemblies, invoicing, member_command, required_methods, accepted = (
        boolean_values
    )
    if global_shape and (node_count != 287 or not required_methods):
        return False

    if reason == "NATIVE_REQUIRED":
        return (
            not native
            and not parse_ok
            and parse_errors == 0
            and node_count == 0
            and not global_shape
            and not assemblies
            and not invoicing
            and not member_command
            and not required_methods
            and not accepted
        )
    if reason == "INPUT_LIMIT":
        return (
            native
            and not parse_ok
            and parse_errors == 0
            and node_count == 0
            and not global_shape
            and not assemblies
            and not invoicing
            and not member_command
            and not required_methods
            and not accepted
        )
    if reason == "PARSE_ERROR":
        return (
            native
            and not parse_ok
            and parse_errors > 0
            and node_count == 0
            and not global_shape
            and not assemblies
            and not invoicing
            and not member_command
            and not required_methods
            and not accepted
        )
    if reason == "NODE_LIMIT":
        return (
            native
            and parse_ok
            and parse_errors == 0
            and 130 <= node_count <= 2_147_483_647
            and not global_shape
            and not assemblies
            and not invoicing
            and not member_command
            and not required_methods
            and not accepted
        )
    if reason == "ASSEMBLIES":
        return (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and not assemblies
            and not accepted
        )
    if reason == "INVOICING":
        return (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and assemblies
            and not invoicing
            and not accepted
        )
    if reason == "MEMBER_COMMAND":
        return (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and assemblies
            and invoicing
            and not member_command
            and not accepted
        )
    if reason == "REQUIRED_METHODS":
        return (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and not global_shape
            and assemblies
            and invoicing
            and member_command
            and not required_methods
            and not accepted
        )
    if reason == "AST_SHAPE":
        ordinary = (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and not global_shape
            and assemblies
            and invoicing
            and member_command
            and required_methods
            and not accepted
        )
        canonical_reference_parse_failure = (
            native
            and parse_ok
            and parse_errors == 0
            and 1 <= node_count <= 4096
            and not global_shape
            and not assemblies
            and not invoicing
            and not member_command
            and not required_methods
            and not accepted
        )
        catch_branch = (
            native
            and not parse_ok
            and parse_errors == 0
            and node_count == 0
            and not global_shape
            and not assemblies
            and not invoicing
            and not member_command
            and not required_methods
            and not accepted
        )
        return ordinary or canonical_reference_parse_failure or catch_branch
    if reason == "OK":
        return (
            native
            and parse_ok
            and parse_errors == 0
            and node_count == 287
            and global_shape
            and assemblies
            and invoicing
            and member_command
            and required_methods
            and accepted
        )
    return False


def _assert_native_state_paths(
    test_case: unittest.TestCase,
    result: dict[str, object],
    expected_valid: bool,
    decode_result,
) -> None:
    with test_case.subTest(path="direct"):
        if expected_valid:
            test_case.assertIs(_validate_native_result(result), result)
        else:
            with test_case.assertRaises(AssertionError):
                _validate_native_result(result)

    payload = json.dumps(result, separators=(",", ":")).encode("utf-8")
    with test_case.subTest(path="decode"):
        if expected_valid:
            test_case.assertEqual(decode_result(payload), result)
        else:
            with test_case.assertRaises(AssertionError):
                decode_result(payload)


def _encode_matrix_witness_field(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _identity_matrix_witness_case(
    source: str,
    name: str,
    identity: str,
    operator: str,
    context: str,
    *,
    fixture: str | None = None,
    fixture_context: str | None = None,
) -> tuple[str, str, str, str, str, int, int]:
    if fixture is None:
        fixture = _identity_matrix_fixture(source, name, identity, operator, context)
    region_context = fixture_context or context
    if region_context == "top-level statement":
        _, assignment_index = _canonical_assignment_lines(source, name)
        start_line = assignment_index + 1
        end_line = start_line
    else:
        generated_prefix = source.rstrip() + "\n"
        start_line = generated_prefix.count("\n") + 1
        end_line = fixture.rstrip("\r\n").count("\n") + 1
    return (
        fixture,
        identity,
        operator,
        context,
        identity,
        start_line,
        end_line,
    )


def _run_identity_matrix_witness(
    cases: list[tuple[str, str, str, str, str, int, int]],
) -> int:
    native_powershell = _resolve_native_powershell()
    if native_powershell is None:
        raise unittest.SkipTest("native Windows PowerShell Desktop 5.1 x64 is required")
    if not cases:
        raise AssertionError("identity matrix witness requires at least one case")

    temporary_root = Path(tempfile.mkdtemp(prefix="xb-member-worker-matrix-"))
    try:
        _assert_temp_outside_checkout(ROOT, temporary_root)
        manifest_lines: list[str] = []
        for index, (fixture, identity, operator, context, target, start_line, end_line) in enumerate(cases):
            candidate_path = temporary_root / f"candidate-{index:04d}.ps1"
            _write_deterministic_powershell_file(candidate_path, fixture)
            manifest_lines.append(
                "|".join(
                    (
                        _encode_matrix_witness_field(str(candidate_path.resolve())),
                        _encode_matrix_witness_field(identity),
                        _encode_matrix_witness_field(operator),
                        _encode_matrix_witness_field(context),
                        _encode_matrix_witness_field(target),
                        str(start_line),
                        str(end_line),
                    )
                )
            )
        witness_path = temporary_root / "witness.ps1"
        _write_deterministic_powershell_file(witness_path, _NATIVE_IDENTITY_MATRIX_WITNESS)
        witnessed = 0
        for chunk_index in range(0, len(manifest_lines), 120):
            chunk = manifest_lines[chunk_index : chunk_index + 120]
            manifest_path = temporary_root / f"manifest-{chunk_index:04d}.txt"
            manifest_path.write_bytes(("\n".join(chunk) + "\n").encode("ascii"))
            command = [
                native_powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(witness_path.resolve()),
                "-ManifestPath",
                str(manifest_path.resolve()),
            ]
            return_code, stdout, stderr, timed_out, overflow = _bounded_native_process(command)
            if timed_out:
                raise AssertionError("native identity matrix witness timed out")
            if overflow:
                raise AssertionError("native identity matrix witness exceeded bounded output")
            if stderr:
                raise AssertionError(
                    "native identity matrix witness wrote to stderr: "
                    + stderr.decode("utf-8", errors="replace")[:4096]
                )
            if return_code != 0:
                raise AssertionError(
                    f"native identity matrix witness exited with status {return_code}"
                )
            if len(stdout) > 4096:
                raise AssertionError("native identity matrix witness stdout exceeded 4096 bytes")
            expected_marker = f"XB_IDENTITY_MATRIX_WITNESS_OK:{len(chunk)}".encode("ascii")
            if stdout.rstrip(b"\r\n") != expected_marker:
                raise AssertionError("native identity matrix witness emitted an invalid marker")
            witnessed += len(chunk)
        return witnessed
    finally:
        try:
            shutil.rmtree(temporary_root)
        except OSError as error:
            raise AssertionError("native identity matrix witness temporary cleanup failed") from error


def _inspect_autocount_probe(probe: str) -> dict[str, object]:
    native_powershell = _resolve_native_powershell()
    if native_powershell is None:
        raise unittest.SkipTest("native Windows PowerShell Desktop 5.1 x64 is required")
    temporary_root = Path(tempfile.mkdtemp(prefix="xb-member-worker-ast-"))
    try:
        _assert_temp_outside_checkout(ROOT, temporary_root)
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
        mutation = f"{operator[-2:]}{identity}"
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
_METHOD_VALUES = ("Create", "GetMember", "NewMember", "SaveMember", "LoadBrowseTable")


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
                _replace_method_condition(source, "@(\"Create\", \"GetMember\", \"NewMember\", \"SaveMember\", \"LoadBrowseTable\", \"ExtraMember\")"),
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
                + "\nforeach ($name in @(\"Create\", \"GetMember\", \"NewMember\", \"SaveMember\", \"LoadBrowseTable\")) {\n}\n",
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


class NativeAstTempPathTests(unittest.TestCase):
    @staticmethod
    def _mock_path(resolved: str, *, absolute: bool = True):
        path = mock.Mock(spec=Path)
        path.is_absolute.return_value = absolute
        path.resolve.return_value = PureWindowsPath(resolved)
        return path

    @staticmethod
    def _native_success_payload() -> bytes:
        return json.dumps(
            {
                "schema_version": 1,
                "native51_x64": True,
                "parse_ok": True,
                "parse_errors": 0,
                "node_count": 287,
                "global_shape_ok": True,
                "assemblies_ok": True,
                "invoicing_ok": True,
                "member_command_ok": True,
                "required_methods_ok": True,
                "accepted": True,
                "reason": "OK",
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def test_equal_checkout_and_temp_are_rejected(self) -> None:
        checkout = self._mock_path(r"C:\Checkout")
        temporary_root = self._mock_path(r"C:\Checkout")

        with self.assertRaisesRegex(AssertionError, "inside the checkout"):
            _assert_temp_outside_checkout(checkout, temporary_root)

    def test_descendant_temp_is_rejected_case_insensitively(self) -> None:
        checkout = self._mock_path(r"C:\Checkout")
        temporary_root = self._mock_path(r"c:\checkout\temp")

        with self.assertRaisesRegex(AssertionError, "inside the checkout"):
            _assert_temp_outside_checkout(checkout, temporary_root)

    def test_same_drive_sibling_is_accepted(self) -> None:
        checkout = self._mock_path(r"C:\Checkout")
        temporary_root = self._mock_path(r"C:\Sibling")

        self.assertIsNone(_assert_temp_outside_checkout(checkout, temporary_root))

    def test_same_drive_unrelated_absolute_path_is_accepted(self) -> None:
        checkout = self._mock_path(r"C:\Checkout")
        temporary_root = self._mock_path(r"C:\Other\Temp")

        self.assertIsNone(_assert_temp_outside_checkout(checkout, temporary_root))

    def test_different_windows_drive_is_accepted(self) -> None:
        checkout = self._mock_path(r"D:\a\automation\automation")
        temporary_root = self._mock_path(r"C:\xb-member-worker-ast-temp")

        self.assertIsNone(_assert_temp_outside_checkout(checkout, temporary_root))

    def test_checkout_resolution_failure_propagates(self) -> None:
        checkout = self._mock_path(r"C:\Checkout")
        temporary_root = self._mock_path(r"C:\Temp")
        checkout.resolve.side_effect = OSError("checkout resolution failed")

        with self.assertRaisesRegex(OSError, "checkout resolution failed"):
            _assert_temp_outside_checkout(checkout, temporary_root)

    def test_temp_resolution_failure_propagates(self) -> None:
        checkout = self._mock_path(r"C:\Checkout")
        temporary_root = self._mock_path(r"C:\Temp")
        temporary_root.resolve.side_effect = OSError("temp resolution failed")

        with self.assertRaisesRegex(OSError, "temp resolution failed"):
            _assert_temp_outside_checkout(checkout, temporary_root)

    def test_nonabsolute_checkout_is_rejected_before_resolution(self) -> None:
        checkout = self._mock_path("checkout", absolute=False)
        temporary_root = self._mock_path(r"C:\Temp")

        with self.assertRaisesRegex(AssertionError, "paths must be absolute"):
            _assert_temp_outside_checkout(checkout, temporary_root)
        checkout.resolve.assert_not_called()
        temporary_root.resolve.assert_not_called()

    def test_nonabsolute_temp_is_rejected_before_resolution(self) -> None:
        checkout = self._mock_path(r"C:\Checkout")
        temporary_root = self._mock_path("temp", absolute=False)

        with self.assertRaisesRegex(AssertionError, "paths must be absolute"):
            _assert_temp_outside_checkout(checkout, temporary_root)
        checkout.resolve.assert_not_called()
        temporary_root.resolve.assert_not_called()

    def test_resolved_paths_must_be_absolute(self) -> None:
        checkout = self._mock_path("resolved-checkout")
        temporary_root = self._mock_path(r"C:\Temp")

        with self.assertRaisesRegex(AssertionError, "resolved paths must be absolute"):
            _assert_temp_outside_checkout(checkout, temporary_root)

    def test_real_tempfile_directory_outside_checkout_is_accepted(self) -> None:
        temporary_root = Path(tempfile.mkdtemp(prefix="xb-member-worker-ast-test-"))
        try:
            self.assertIsNone(_assert_temp_outside_checkout(ROOT, temporary_root))
        finally:
            shutil.rmtree(temporary_root)

    def test_containment_rejection_precedes_file_writes_and_native_invocation(self) -> None:
        temporary_root = Path(tempfile.mkdtemp(prefix="xb-member-worker-ast-test-"))
        try:
            with (
                mock.patch.object(tempfile, "mkdtemp", return_value=str(temporary_root)),
                mock.patch(f"{__name__}._resolve_native_powershell", return_value="powershell.exe"),
                mock.patch(
                    f"{__name__}._assert_temp_outside_checkout",
                    side_effect=AssertionError("temp directory is inside the checkout"),
                ) as containment,
                mock.patch(f"{__name__}._write_deterministic_powershell_file") as write_file,
                mock.patch(f"{__name__}._bounded_native_process") as native_process,
            ):
                with self.assertRaisesRegex(AssertionError, "inside the checkout"):
                    _inspect_autocount_probe("candidate")

                containment.assert_called_once_with(ROOT, temporary_root)
                write_file.assert_not_called()
                native_process.assert_not_called()
        finally:
            if temporary_root.exists():
                shutil.rmtree(temporary_root)

    def test_containment_rejection_precedes_native_powershell_invocation(self) -> None:
        temporary_root = Path(tempfile.mkdtemp(prefix="xb-member-worker-ast-test-"))
        try:
            with (
                mock.patch.object(tempfile, "mkdtemp", return_value=str(temporary_root)),
                mock.patch(f"{__name__}._resolve_native_powershell", return_value="powershell.exe"),
                mock.patch(
                    f"{__name__}._assert_temp_outside_checkout",
                    side_effect=AssertionError("temp directory is inside the checkout"),
                ),
                mock.patch(f"{__name__}._bounded_native_process") as native_process,
            ):
                with self.assertRaisesRegex(AssertionError, "inside the checkout"):
                    _inspect_autocount_probe("candidate")

                native_process.assert_not_called()
        finally:
            if temporary_root.exists():
                shutil.rmtree(temporary_root)

    def test_cleanup_runs_after_success(self) -> None:
        temporary_root = Path(tempfile.mkdtemp(prefix="xb-member-worker-ast-test-"))
        try:
            with (
                mock.patch.object(tempfile, "mkdtemp", return_value=str(temporary_root)),
                mock.patch(f"{__name__}._resolve_native_powershell", return_value="powershell.exe"),
                mock.patch(f"{__name__}._assert_temp_outside_checkout"),
                mock.patch(
                    f"{__name__}._bounded_native_process",
                    return_value=(0, self._native_success_payload(), b"", False, False),
                ),
                mock.patch.object(shutil, "rmtree", wraps=shutil.rmtree) as remove_tree,
            ):
                result = _inspect_autocount_probe("candidate")

            self.assertTrue(result["accepted"])
            remove_tree.assert_called_once_with(temporary_root)
            self.assertFalse(temporary_root.exists())
        finally:
            if temporary_root.exists():
                shutil.rmtree(temporary_root)

    def test_cleanup_runs_after_inspection_failure(self) -> None:
        temporary_root = Path(tempfile.mkdtemp(prefix="xb-member-worker-ast-test-"))
        try:
            with (
                mock.patch.object(tempfile, "mkdtemp", return_value=str(temporary_root)),
                mock.patch(f"{__name__}._resolve_native_powershell", return_value="powershell.exe"),
                mock.patch(f"{__name__}._assert_temp_outside_checkout"),
                mock.patch(
                    f"{__name__}._bounded_native_process",
                    side_effect=RuntimeError("native inspection failed"),
                ),
                mock.patch.object(shutil, "rmtree", wraps=shutil.rmtree) as remove_tree,
            ):
                with self.assertRaisesRegex(RuntimeError, "native inspection failed"):
                    _inspect_autocount_probe("candidate")

            remove_tree.assert_called_once_with(temporary_root)
            self.assertFalse(temporary_root.exists())
        finally:
            if temporary_root.exists():
                shutil.rmtree(temporary_root)

    def test_cleanup_failure_remains_an_error(self) -> None:
        temporary_root = Path(tempfile.mkdtemp(prefix="xb-member-worker-ast-test-"))
        try:
            with (
                mock.patch.object(tempfile, "mkdtemp", return_value=str(temporary_root)),
                mock.patch(f"{__name__}._resolve_native_powershell", return_value="powershell.exe"),
                mock.patch(f"{__name__}._assert_temp_outside_checkout"),
                mock.patch(f"{__name__}._write_deterministic_powershell_file"),
                mock.patch(
                    f"{__name__}._bounded_native_process",
                    return_value=(0, self._native_success_payload(), b"", False, False),
                ),
                mock.patch.object(
                    shutil,
                    "rmtree",
                    side_effect=OSError("cleanup failed"),
                ),
            ):
                with self.assertRaisesRegex(AssertionError, "temporary cleanup failed"):
                    _inspect_autocount_probe("candidate")
        finally:
            if temporary_root.exists():
                temporary_root.rmdir()


class NativeAstResultConsistencyTests(unittest.TestCase):
    _BOOLEAN_FIELDS = (
        "native51_x64",
        "parse_ok",
        "global_shape_ok",
        "assemblies_ok",
        "invoicing_ok",
        "member_command_ok",
        "required_methods_ok",
        "accepted",
    )

    @staticmethod
    def _result(reason: str, **overrides: object) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 1,
            "native51_x64": True,
            "parse_ok": True,
            "parse_errors": 0,
            "node_count": 287,
            "global_shape_ok": True,
            "assemblies_ok": True,
            "invoicing_ok": True,
            "member_command_ok": True,
            "required_methods_ok": True,
            "accepted": True,
            "reason": reason,
        }
        result.update(
            {
                "NATIVE_REQUIRED": {
                    "native51_x64": False,
                    "parse_ok": False,
                    "node_count": 0,
                    "global_shape_ok": False,
                    "assemblies_ok": False,
                    "invoicing_ok": False,
                    "member_command_ok": False,
                    "required_methods_ok": False,
                    "accepted": False,
                },
                "INPUT_LIMIT": {
                    "parse_ok": False,
                    "node_count": 0,
                    "global_shape_ok": False,
                    "assemblies_ok": False,
                    "invoicing_ok": False,
                    "member_command_ok": False,
                    "required_methods_ok": False,
                    "accepted": False,
                },
                "PARSE_ERROR": {
                    "parse_ok": False,
                    "parse_errors": 1,
                    "node_count": 0,
                    "global_shape_ok": False,
                    "assemblies_ok": False,
                    "invoicing_ok": False,
                    "member_command_ok": False,
                    "required_methods_ok": False,
                    "accepted": False,
                },
                "NODE_LIMIT": {
                    "parse_ok": True,
                    "node_count": 4786,
                    "global_shape_ok": False,
                    "assemblies_ok": False,
                    "invoicing_ok": False,
                    "member_command_ok": False,
                    "required_methods_ok": False,
                    "accepted": False,
                },
                "ASSEMBLIES": {
                    "node_count": 290,
                    "global_shape_ok": False,
                    "assemblies_ok": False,
                    "accepted": False,
                },
                "INVOICING": {
                    "node_count": 290,
                    "global_shape_ok": False,
                    "invoicing_ok": False,
                    "accepted": False,
                },
                "MEMBER_COMMAND": {
                    "node_count": 290,
                    "global_shape_ok": False,
                    "member_command_ok": False,
                    "accepted": False,
                },
                "REQUIRED_METHODS": {
                    "node_count": 290,
                    "global_shape_ok": False,
                    "required_methods_ok": False,
                    "accepted": False,
                },
                "AST_SHAPE": {
                    "node_count": 290,
                    "global_shape_ok": False,
                    "accepted": False,
                },
                "OK": {},
            }[reason]
        )
        result.update(overrides)
        return result

    @staticmethod
    def _payload(result: dict[str, object]) -> bytes:
        return json.dumps(result, separators=(",", ":")).encode("utf-8")

    def _decode_result(self, payload: bytes) -> dict[str, object]:
        with (
            mock.patch(f"{__name__}._resolve_native_powershell", return_value="powershell.exe"),
            mock.patch(
                f"{__name__}._bounded_native_process",
                return_value=(0, payload, b"", False, False),
            ),
        ):
            return _inspect_autocount_probe("synthetic candidate")

    def _assert_decode_rejects(self, result: dict[str, object]) -> None:
        with self.assertRaises(AssertionError):
            _validate_native_result(result)
        with self.assertRaises(AssertionError):
            self._decode_result(self._payload(result))

    def _assert_decode_accepts(self, result: dict[str, object]) -> None:
        self.assertIs(_validate_native_result(result), result)
        self.assertEqual(self._decode_result(self._payload(result)), result)

    def test_legitimate_reason_state_controls(self) -> None:
        controls = (
            (
                "trusted non-native branch",
                self._result("NATIVE_REQUIRED"),
            ),
            (
                "trusted oversized-input branch",
                self._result("INPUT_LIMIT"),
            ),
            (
                "trusted candidate parse-error branch",
                self._result("PARSE_ERROR"),
            ),
            (
                "trusted node-count limit branch",
                self._result("NODE_LIMIT", node_count=4786),
            ),
            (
                "trusted traversal-depth limit branch",
                self._result("NODE_LIMIT", node_count=200),
            ),
            (
                "trusted assemblies rejection",
                self._result("ASSEMBLIES", node_count=290),
            ),
            (
                "trusted global-shape-success assemblies rejection",
                self._result(
                    "ASSEMBLIES",
                    node_count=287,
                    global_shape_ok=True,
                    assemblies_ok=False,
                ),
            ),
            (
                "trusted invoicing rejection",
                self._result("INVOICING", node_count=290),
            ),
            (
                "trusted member-command rejection",
                self._result("MEMBER_COMMAND", node_count=290),
            ),
            (
                "trusted required-method rejection",
                self._result("REQUIRED_METHODS", node_count=290),
            ),
            (
                "trusted ordinary AST-shape rejection",
                self._result("AST_SHAPE", node_count=290),
            ),
            (
                "trusted canonical-reference parse-failure AST-shape branch",
                self._result(
                    "AST_SHAPE",
                    node_count=290,
                    global_shape_ok=False,
                    assemblies_ok=False,
                    invoicing_ok=False,
                    member_command_ok=False,
                    required_methods_ok=False,
                ),
            ),
            (
                "trusted catch AST-shape branch",
                self._result(
                    "AST_SHAPE",
                    parse_ok=False,
                    node_count=0,
                    global_shape_ok=False,
                    assemblies_ok=False,
                    invoicing_ok=False,
                    member_command_ok=False,
                    required_methods_ok=False,
                ),
            ),
            (
                "trusted canonical success",
                self._result("OK"),
            ),
        )
        for label, result in controls:
            with self.subTest(control=label):
                self._assert_decode_accepts(result)

    def test_independent_exhaustive_reason_state_oracle(self) -> None:
        reasons = (
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
        )
        valid_by_reason = {reason: 0 for reason in reasons}
        total_states = 0
        valid_states = 0
        invalid_states = 0
        direct_path_checks = 0
        decode_path_checks = 0
        omitted_input_limit_visits = 0
        omitted_input_limit = (True, False, False, False, False, False, True, False)
        reusable_temp_root = Path(tempfile.mkdtemp(prefix="xb-member-worker-oracle-"))
        real_rmtree = shutil.rmtree

        try:
            with (
                mock.patch(f"{__name__}._resolve_native_powershell", return_value="powershell.exe"),
                mock.patch(f"{__name__}._bounded_native_process") as bounded_process,
                mock.patch(f"{__name__}.tempfile.mkdtemp", return_value=str(reusable_temp_root)),
                mock.patch(f"{__name__}._write_deterministic_powershell_file"),
                mock.patch(f"{__name__}.shutil.rmtree"),
            ):
                def decode_result(payload: bytes) -> dict[str, object]:
                    bounded_process.return_value = (0, payload, b"", False, False)
                    return _inspect_autocount_probe("synthetic candidate")

                for reason in reasons:
                    for boolean_values in product((False, True), repeat=len(BOOLEAN_ORDER)):
                        for node_count in NODE_COUNTS:
                            for parse_errors in PARSE_ERRORS:
                                result = _native_emission_state(
                                    reason, boolean_values, node_count, parse_errors
                                )
                                expected_valid = _expected_native_emission_valid(
                                    reason, boolean_values, node_count, parse_errors
                                )
                                _assert_native_state_paths(
                                    self, result, expected_valid, decode_result
                                )
                                total_states += 1
                                direct_path_checks += 1
                                decode_path_checks += 1
                                if expected_valid:
                                    valid_states += 1
                                    valid_by_reason[reason] += 1
                                else:
                                    invalid_states += 1
                                if (
                                    reason == "INPUT_LIMIT"
                                    and boolean_values == omitted_input_limit
                                    and node_count == 0
                                    and parse_errors == 0
                                ):
                                    omitted_input_limit_visits += 1
                                    self.assertFalse(expected_valid)
        finally:
            real_rmtree(reusable_temp_root)

        self.assertEqual(total_states, 204800)
        self.assertEqual(valid_states, 210)
        self.assertEqual(invalid_states, 204590)
        self.assertEqual(direct_path_checks, 204800)
        self.assertEqual(decode_path_checks, 204800)
        self.assertEqual(omitted_input_limit_visits, 1)
        self.assertEqual(
            valid_by_reason,
            {
                "NATIVE_REQUIRED": 1,
                "INPUT_LIMIT": 1,
                "PARSE_ERROR": 2,
                "NODE_LIMIT": 10,
                "ASSEMBLIES": 92,
                "INVOICING": 46,
                "MEMBER_COMMAND": 23,
                "REQUIRED_METHODS": 11,
                "AST_SHAPE": 23,
                "OK": 1,
            },
        )

    def test_omitted_input_limit_state_is_mutation_sensitive_on_both_paths(self) -> None:
        result = _native_emission_state(
            "INPUT_LIMIT",
            (True, False, False, False, False, False, True, False),
            0,
            0,
        )
        self.assertFalse(_expected_native_emission_valid(
            "INPUT_LIMIT",
            (True, False, False, False, False, False, True, False),
            0,
            0,
        ))

        def run_inner_case() -> unittest.TestResult:
            class SingleStateCase(unittest.TestCase):
                def runTest(inner_self) -> None:
                    with (
                        mock.patch(
                            f"{__name__}._resolve_native_powershell",
                            return_value="powershell.exe",
                        ),
                        mock.patch(f"{__name__}._bounded_native_process") as bounded_process,
                    ):
                        def decode_result(payload: bytes) -> dict[str, object]:
                            bounded_process.return_value = (0, payload, b"", False, False)
                            return _inspect_autocount_probe("synthetic candidate")

                        _assert_native_state_paths(
                            inner_self, result, False, decode_result
                        )

            test_result = unittest.TestResult()
            SingleStateCase().run(test_result)
            return test_result

        baseline = run_inner_case()
        self.assertEqual(baseline.testsRun, 1)
        self.assertTrue(baseline.wasSuccessful(), baseline.failures + baseline.errors)

        original_validator = _validate_native_result

        def accept_only_omitted_input_limit(candidate: dict[str, object]):
            if candidate == result:
                return candidate
            return original_validator(candidate)

        with mock.patch(
            f"{__name__}._validate_native_result",
            side_effect=accept_only_omitted_input_limit,
        ):
            mutated = run_inner_case()

        self.assertEqual(mutated.testsRun, 1)
        self.assertEqual(len(mutated.failures), 2)
        self.assertEqual(len(mutated.errors), 0)
        self.assertEqual(len(mutated.skipped), 0)

    def test_impossible_reason_state_matrix_is_rejected_directly_and_on_decode(self) -> None:
        invalid_cases: list[tuple[str, dict[str, object]]] = []

        def add(label: str, reason: str, **changes: object) -> None:
            invalid_cases.append((label, self._result(reason, **changes)))

        add("OK node count zero", "OK", node_count=0)
        add("OK node count above limit", "OK", node_count=4097)
        add("OK node count not canonical", "OK", node_count=286)
        for field in ("global_shape_ok", "assemblies_ok", "invoicing_ok", "member_command_ok", "required_methods_ok"):
            add(f"OK required flag false: {field}", "OK", **{field: False})
        add("OK accepted false", "OK", accepted=False)
        add("OK native false", "OK", native51_x64=False)
        add("OK parse false", "OK", parse_ok=False)

        add("NODE_LIMIT accepted true", "NODE_LIMIT", accepted=True)
        for field in (
            "global_shape_ok",
            "assemblies_ok",
            "invoicing_ok",
            "member_command_ok",
            "required_methods_ok",
        ):
            add(f"NODE_LIMIT success flag true: {field}", "NODE_LIMIT", **{field: True})
        add("NODE_LIMIT node count zero", "NODE_LIMIT", node_count=0)
        add("NODE_LIMIT node count below depth minimum", "NODE_LIMIT", node_count=129)

        add("PARSE_ERROR parse_ok true", "PARSE_ERROR", parse_ok=True)
        add("PARSE_ERROR parse_errors zero", "PARSE_ERROR", parse_errors=0)
        add("PARSE_ERROR node count nonzero", "PARSE_ERROR", node_count=1)
        add("PARSE_ERROR accepted true", "PARSE_ERROR", accepted=True)
        for field in (
            "global_shape_ok",
            "assemblies_ok",
            "invoicing_ok",
            "member_command_ok",
            "required_methods_ok",
        ):
            add(f"PARSE_ERROR success flag true: {field}", "PARSE_ERROR", **{field: True})

        for reason in (
            "NATIVE_REQUIRED",
            "INPUT_LIMIT",
            "NODE_LIMIT",
            "ASSEMBLIES",
            "INVOICING",
            "MEMBER_COMMAND",
            "REQUIRED_METHODS",
            "AST_SHAPE",
            "OK",
        ):
            add(f"non-parse reason with parse errors: {reason}", reason, parse_errors=1)

        add("ASSEMBLIES own flag true", "ASSEMBLIES", assemblies_ok=True)
        add("INVOICING earlier flag false", "INVOICING", assemblies_ok=False)
        add("INVOICING own flag true", "INVOICING", invoicing_ok=True)
        add("MEMBER_COMMAND earlier assemblies false", "MEMBER_COMMAND", assemblies_ok=False)
        add("MEMBER_COMMAND earlier invoicing false", "MEMBER_COMMAND", invoicing_ok=False)
        add("MEMBER_COMMAND own flag true", "MEMBER_COMMAND", member_command_ok=True)
        add("REQUIRED_METHODS global shape true", "REQUIRED_METHODS", global_shape_ok=True)
        add("REQUIRED_METHODS earlier assemblies false", "REQUIRED_METHODS", assemblies_ok=False)
        add("REQUIRED_METHODS earlier invoicing false", "REQUIRED_METHODS", invoicing_ok=False)
        add("REQUIRED_METHODS earlier member command false", "REQUIRED_METHODS", member_command_ok=False)
        add("REQUIRED_METHODS own flag true", "REQUIRED_METHODS", required_methods_ok=True)

        add("AST_SHAPE global success", "AST_SHAPE", global_shape_ok=True)
        add("AST_SHAPE ordinary branch missing required methods", "AST_SHAPE", required_methods_ok=False)
        add("AST_SHAPE catch branch with parse success", "AST_SHAPE", parse_ok=True, node_count=0)
        add("AST_SHAPE catch branch with nodes", "AST_SHAPE", parse_ok=False, node_count=1)
        add("AST_SHAPE canonical branch with global success", "AST_SHAPE", global_shape_ok=True)
        add("NATIVE_REQUIRED native true", "NATIVE_REQUIRED", native51_x64=True)
        add("NATIVE_REQUIRED parse true", "NATIVE_REQUIRED", parse_ok=True)
        add("NATIVE_REQUIRED node count nonzero", "NATIVE_REQUIRED", node_count=1)
        add("INPUT_LIMIT parse true", "INPUT_LIMIT", parse_ok=True)
        add("INPUT_LIMIT node count nonzero", "INPUT_LIMIT", node_count=1)

        add("global success wrong count", "OK", node_count=288)
        add("global success required methods false", "OK", required_methods_ok=False)

        unknown_field = self._result("OK")
        unknown_field["unknown"] = False
        invalid_cases.append(("unknown field", unknown_field))
        missing_field = self._result("OK")
        del missing_field["reason"]
        invalid_cases.append(("missing field", missing_field))
        unknown_reason = self._result("OK")
        unknown_reason["reason"] = "NOT_A_REASON"
        invalid_cases.append(("unknown reason", unknown_reason))
        invalid_cases.append(("wrong schema version", self._result("OK", schema_version=2)))
        invalid_cases.append(("boolean integer confusion", self._result("OK", accepted=1)))
        invalid_cases.append(("negative integer", self._result("OK", node_count=-1)))
        invalid_cases.append(("integer above native range", self._result("OK", node_count=2_147_483_648)))

        for label, result in invalid_cases:
            with self.subTest(state=label):
                self._assert_decode_rejects(result)

    def test_invalid_json_transport_is_rejected_on_decode(self) -> None:
        invalid_payloads = (
            ("duplicate JSON key", b'{"schema_version":1,"schema_version":1}'),
            ("malformed UTF-8", b"\xff\xfe\xfd"),
            ("nonfinite number", b'{"schema_version":1,"value":NaN}'),
            ("non-object JSON", b"[]"),
            ("multiple JSON documents", b"{}\n{}"),
            ("extra stdout", b'{} trailing'),
        )
        for label, payload in invalid_payloads:
            with self.subTest(payload=label):
                with self.assertRaises(AssertionError):
                    _strict_json_object(payload)
                with mock.patch(
                    f"{__name__}._resolve_native_powershell", return_value="powershell.exe"
                ), mock.patch(
                    f"{__name__}._bounded_native_process",
                    return_value=(0, payload, b"", False, False),
                ):
                    with self.assertRaises(AssertionError):
                        _inspect_autocount_probe("synthetic candidate")


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
        # The gateway tree pin binds the reviewed candidate; it is checked
        # against the committed HEAD tree.
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
            'foreach ($name in @("Create", "GetMember", "NewMember", "SaveMember", "LoadBrowseTable"))',
            'foreach ($name in @("Create", "GetMember", "NewMember", "SaveMember", "LoadBrowseTable", \'SixthSingleQuoted\'))',
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
            ("sixth single-quoted required method", fifth_single_method),
        ):
            with self.subTest(counterexample=name):
                result = _inspect_autocount_probe(counterexample)
                self.assertTrue(result["parse_ok"], result)
                self.assertFalse(result["accepted"], result)

    def test_g2_identity_matrix_witness_rejects_generator_integrity_controls(self) -> None:
        if not self.pwsh:
            self.skipTest("native Windows PowerShell Desktop 5.1 x64 is required")
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        context = "expandable-string $()"
        valid_prefix = _identity_matrix_witness_case(
            probe, "invoicing", "$invoicing", "prefix++", context
        )
        malformed_prefix = _identity_matrix_witness_case(
            probe,
            "invoicing",
            "$invoicing",
            "prefix++",
            context,
            fixture=_context_fixture(probe, "prefix$invoicing", context),
            fixture_context=context,
        )
        swapped_increment = _identity_matrix_witness_case(
            probe,
            "invoicing",
            "$invoicing",
            "prefix--",
            context,
            fixture=valid_prefix[0],
            fixture_context=context,
        )
        prefix_postfix_mismatch = _identity_matrix_witness_case(
            probe,
            "invoicing",
            "$invoicing",
            "postfix++",
            context,
            fixture=valid_prefix[0],
            fixture_context=context,
        )
        wrong_identity = _identity_matrix_witness_case(
            probe,
            "invoicing",
            "$requiredAssemblies",
            "prefix++",
            context,
            fixture=valid_prefix[0],
            fixture_context=context,
        )
        wrong_context = _identity_matrix_witness_case(
            probe,
            "invoicing",
            "$invoicing",
            "prefix++",
            "Write-Output (mutation)",
            fixture=valid_prefix[0],
            fixture_context=context,
        )
        for label, case in (
            ("original malformed prefix output", malformed_prefix),
            ("swapped increment/decrement", swapped_increment),
            ("prefix/postfix mismatch", prefix_postfix_mismatch),
            ("wrong variable identity", wrong_identity),
            ("wrong context", wrong_context),
        ):
            with self.subTest(control=label):
                with self.assertRaises(AssertionError):
                    _run_identity_matrix_witness([case])

    def test_g3_native_relationship_context_matrix_and_regressions(self) -> None:
        if not self.pwsh:
            self.skipTest("native Windows PowerShell Desktop 5.1 x64 is required")
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        positive_specs = (
            (
                "top-level statement",
                _identity_matrix_fixture(probe, "invoicing", "$invoicing", "prefix++", "top-level statement"),
                "top-level statement",
            ),
            (
                "expandable-string $()",
                _context_fixture(probe, "++$invoicing", "expandable-string $()"),
                "expandable-string $()",
            ),
            (
                "expandable here-string $()",
                _context_fixture(probe, "++$invoicing", "expandable here-string $()"),
                "expandable here-string $()",
            ),
            (
                "nested $($())",
                _context_fixture(probe, "++$invoicing", "nested $($())"),
                "nested $($())",
            ),
            (
                "invoked scriptblock",
                _context_fixture(probe, "++$invoicing", "invoked scriptblock"),
                "invoked scriptblock",
            ),
            (
                "Write-Output (mutation)",
                _context_fixture(probe, "++$invoicing", "Write-Output (mutation)"),
                "Write-Output (mutation)",
            ),
        )
        positive_cases = [
            _identity_matrix_witness_case(
                probe,
                "invoicing",
                "$invoicing",
                "prefix++",
                context,
                fixture=fixture,
                fixture_context=region_context,
            )
            for context, fixture, region_context in positive_specs
        ]
        positive_count = 0
        cross_label_count = 0
        for case in positive_cases:
            with self.subTest(positive_context=case[3]):
                self.assertEqual(_run_identity_matrix_witness([case]), 1)
                positive_count += 1

        for context, fixture, region_context in positive_specs:
            for wrong_context, _, _ in positive_specs:
                if wrong_context == context:
                    continue
                wrong_case = _identity_matrix_witness_case(
                    probe,
                    "invoicing",
                    "$invoicing",
                    "prefix++",
                    wrong_context,
                    fixture=fixture,
                    fixture_context=region_context,
                )
                with self.subTest(expected_context=context, wrong_context=wrong_context):
                    with self.assertRaises(AssertionError):
                        _run_identity_matrix_witness([wrong_case])
                cross_label_count += 1

        self.assertEqual(positive_count, 6)
        self.assertEqual(cross_label_count, 30)

        regression_specs = (
            (
                "g4_117_top_level_in_if",
                "top-level statement",
                probe.rstrip() + "\nif ($true) { ++$invoicing }\n",
            ),
            (
                "g4_117_scriptblock_argument_not_invocation",
                "invoked scriptblock",
                probe.rstrip() + "\nWrite-Output { ++$invoicing }\n",
            ),
            (
                "g4_117_wrong_command_identity",
                "Write-Output (mutation)",
                probe.rstrip() + "\nWrite-Warning (++$invoicing)\n",
            ),
            (
                "expandable conditional false positive",
                "expandable-string $()",
                probe.rstrip() + '\n"$(if ($true) { ++$invoicing })"\n',
            ),
            (
                "expandable here-string conditional false positive",
                "expandable here-string $()",
                probe.rstrip() + '\n@"\n$(if ($true) { ++$invoicing })\n"@\n',
            ),
            (
                "nested parenthesized false positive",
                "nested $($())",
                probe.rstrip() + '\n"$($( (++$invoicing) ))"\n',
            ),
            (
                "dot-sourced scriptblock false positive",
                "invoked scriptblock",
                probe.rstrip() + "\n. { ++$invoicing }\n",
            ),
            (
                "Write-Output pipeline false positive",
                "Write-Output (mutation)",
                probe.rstrip() + "\nWrite-Output (++$invoicing) | Out-Null\n",
            ),
            (
                "mixed duplicate operation",
                "expandable-string $()",
                probe.rstrip() + '\n"$(++$invoicing)"; if ($true) { ++$invoicing }\n',
            ),
        )
        for label, context, fixture in regression_specs:
            case = _identity_matrix_witness_case(
                probe,
                "invoicing",
                "$invoicing",
                "prefix++",
                context,
                fixture=fixture,
                fixture_context="appended",
            )
            with self.subTest(regression=label):
                with self.assertRaises(AssertionError):
                    _run_identity_matrix_witness([case])

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
        witness_cases: list[tuple[str, str, str, str, str, int, int]] = []
        for name in ("invoicing", "requiredAssemblies"):
            for identity_index, (identity, is_bare) in enumerate(_identity_forms(name)):
                for operator_index, operator in enumerate(operators):
                    context = contexts[(identity_index + operator_index) % len(contexts)]
                    fixture = _identity_matrix_fixture(probe, name, identity, operator, context)
                    fixtures.append(fixture)
                    witness_cases.append(
                        _identity_matrix_witness_case(
                            probe, name, identity, operator, context, fixture=fixture
                        )
                    )
                    cases.append((name, identity, operator, identity_index, operator_index, is_bare, context))
        self.assertEqual(len(cases), 720)
        self.assertEqual(len(fixtures), 720)
        self.assertEqual(len(witness_cases), 720)
        prefix_cases = [
            case for case in cases if case[2] in {"prefix++", "prefix--"}
        ]
        prefix_witness_cases = [
            case for case in witness_cases if case[2] in {"prefix++", "prefix--"}
        ]
        non_top_level_prefix_cases = [
            case for case in cases
            if case[2] in {"prefix++", "prefix--"} and case[6] != "top-level statement"
        ]
        self.assertEqual(len(prefix_cases), 144)
        self.assertEqual(len(prefix_witness_cases), 144)
        self.assertEqual(len(non_top_level_prefix_cases), 120)
        self.assertEqual(_run_identity_matrix_witness(witness_cases), 720)
        results = _inspect_many(fixtures)
        self.assertEqual(len(results), 720)
        corrected_prefix_results = [
            result for case, result in zip(cases, results)
            if case[2] in {"prefix++", "prefix--"} and case[6] != "top-level statement"
        ]
        self.assertEqual(len(corrected_prefix_results), 120)
        self.assertEqual(sum(bool(result["parse_ok"]) for result in corrected_prefix_results), 120)
        self.assertEqual(sum(not bool(result["accepted"]) for result in corrected_prefix_results), 120)
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
        self.assertEqual(len(variants), 101)  # 92 + 9 derived from the fifth required method literal
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
        self.assertEqual(len(variants), 16)  # 15 + 1 single-quoted variant of the fifth method literal
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

        depth_limited = _inspect_autocount_probe("(" * 65 + "1" + ")" * 65)
        self.assertEqual(depth_limited["reason"], "NODE_LIMIT", depth_limited)
        self.assertTrue(depth_limited["native51_x64"], depth_limited)
        self.assertTrue(depth_limited["parse_ok"], depth_limited)
        self.assertEqual(depth_limited["parse_errors"], 0, depth_limited)
        self.assertGreaterEqual(depth_limited["node_count"], 130, depth_limited)
        self.assertLessEqual(depth_limited["node_count"], 4096, depth_limited)
        for field in (
            "global_shape_ok",
            "assemblies_ok",
            "invoicing_ok",
            "member_command_ok",
            "required_methods_ok",
            "accepted",
        ):
            self.assertFalse(depth_limited[field], depth_limited)

    def test_g2_native_reason_state_positive_controls(self) -> None:
        if not self.pwsh:
            self.skipTest("native Windows PowerShell Desktop 5.1 x64 is required")
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        semantic_controls = (
            (
                "ASSEMBLIES",
                probe.replace('"AutoCount.dll"', '"AutoCount.Missing.dll"', 1),
            ),
            (
                "INVOICING",
                probe.replace(
                    '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
                    '$invoicing = $loaded["AutoCount.Tools.dll"]',
                    1,
                ),
            ),
            (
                "MEMBER_COMMAND",
                probe.replace(
                    '$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)',
                    '$memberCommand = $core.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)',
                    1,
                ),
            ),
            (
                "REQUIRED_METHODS",
                probe.replace('"SaveMember"', '"MissingSaveMember"', 1),
            ),
            (
                "AST_SHAPE",
                probe + "\n$matrixExtraAssignment = 1\n",
            ),
            (
                "INVOICING global-shape-success semantic rejection",
                probe.replace(
                    '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
                    '${invoicing} = $loaded["AutoCount.Invoicing.dll"]',
                    1,
                ),
            ),
        )
        for expected_reason, candidate in semantic_controls:
            with self.subTest(control=expected_reason):
                result = _inspect_autocount_probe(candidate)
                self.assertTrue(result["native51_x64"], result)
                self.assertTrue(result["parse_ok"], result)
                self.assertEqual(result["parse_errors"], 0, result)
                self.assertEqual(result["reason"], expected_reason.split()[0], result)
                self.assertFalse(result["accepted"], result)
        global_shape_success = _inspect_autocount_probe(
            probe.replace(
                '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
                '${invoicing} = $loaded["AutoCount.Invoicing.dll"]',
                1,
            )
        )
        self.assertTrue(global_shape_success["global_shape_ok"], global_shape_success)
        self.assertEqual(global_shape_success["node_count"], 287, global_shape_success)
        self.assertFalse(global_shape_success["invoicing_ok"], global_shape_success)

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
                "autocount_assembly_path": r"C:\\AutoCount",
                "autocount_server_name": "  server exact  ",
                "autocount_database_name": "database exact",
                "autocount_user_id": "user exact",
                "autocount_integration_user_id": "user exact",
                "autocount_book_mode": "production",
                "autocount_production_book": "database exact",
                "autocount_test_book_allowlist": [],
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
    integration_user = $info.EnvironmentVariables['XB_AC2_INTEGRATION_USER_ID']
    production_book = $info.EnvironmentVariables['XB_AC2_PRODUCTION_BOOK']
    test_allowlist = $info.EnvironmentVariables['XB_AC2_TEST_BOOK_ALLOWLIST']
    host_binding = $info.EnvironmentVariables.ContainsKey('XB_MEMBER_GATEWAY_WORKER_HOST_BINDING')
    arguments = $info.Arguments
    interpreter = $info.FileName
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
                    "XB_MEMBER_GATEWAY_WORKER_HOST_BINDING": "host-legacy",
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
            self.assertEqual(observed["integration_user"], "user exact")
            self.assertEqual(observed["production_book"], "database exact")
            self.assertEqual(observed["test_allowlist"], "")
            self.assertFalse(observed["host_binding"])
            self.assertEqual(observed["interpreter"], r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
            self.assertIn('"-Book" "production" "-EnableProductionWorker" "-EnableProductionAdapter"', observed["arguments"])
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
                "param([string]$Book, [switch]$EnableProductionWorker, [switch]$EnableProductionAdapter)\n"
                "if ($Book -cne 'production') { throw 'synthetic_book_missing' }\n"
                "$marker = [Environment]::GetEnvironmentVariable('XB_TEST_CHILD_MARKER', 'Process')\n"
                "if ([string]::IsNullOrWhiteSpace($marker)) { throw 'synthetic_marker_missing' }\n"
                "[IO.File]::WriteAllText($marker, 'started')\n"
                "[pscustomobject]@{ status = 'completed'; writes = 0 } | ConvertTo-Json -Compress\n",
                encoding="utf-8",
            )
            base_config = {
                "gateway_base_url": "https://gateway.example.test",
                "autocount_assembly_path": r"C:\\AutoCount",
                "autocount_server_name": "synthetic-server",
                "autocount_database_name": "synthetic-database",
                "autocount_user_id": "synthetic-user",
                "autocount_integration_user_id": "synthetic-user",
                "autocount_book_mode": "production",
                "autocount_production_book": "synthetic-database",
                "autocount_test_book_allowlist": [],
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

            invalid_cases = [
                (field, value)
                for field in ("autocount_server_name", "autocount_database_name", "autocount_user_id", "autocount_integration_user_id", "autocount_production_book", "autocount_book_mode")
                for value in invalid_values
            ]
            invalid_cases += [("autocount_book_mode", "Production"), ("autocount_book_mode", "staging")]
            invalid_cases += [("autocount_test_book_allowlist", value) for value in (_MISSING, None, "book", [7], [""], ["a;b"])]
            for field, value in invalid_cases:
                if True:
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
            "autocount_integration_user_id": "launcher_autocount_integration_user_id_invalid",
            "autocount_production_book": "launcher_autocount_production_book_invalid",
            "autocount_book_mode": "launcher_autocount_book_mode_invalid",
        }
        for field, error_id in fields.items():
            with self.subTest(field=field):
                self.assertIn(f'PropertyName "{field}"', launcher)
                self.assertIn(error_id, launcher)
        for environment_name in (
            "XB_AC2_SERVER_NAME",
            "XB_AC2_DATABASE_NAME",
            "XB_AC2_USER_ID",
            "XB_AC2_INTEGRATION_USER_ID",
            "XB_AC2_PRODUCTION_BOOK",
            "XB_AC2_TEST_BOOK_ALLOWLIST",
        ):
            self.assertIn(f'EnvironmentVariables["{environment_name}"]', launcher)
        for environment_name in (
            "AC2_PROBE_SERVER_NAME",
            "AC2_PROBE_DATABASE_NAME",
            "AC2_PROBE_USER_ID",
            "AC2_PROBE_PASSWORD",
            "XB_AC2_SESSION_FACTORY",
            "XB_MEMBER_GATEWAY_WORKER_HOST_BINDING",
        ):
            self.assertIn(f'EnvironmentVariables.Remove("{environment_name}")', launcher)
        self.assertNotIn("worker_host_binding", launcher)
        self.assertNotIn("$PSHOME", launcher.replace("never resolved from PATH or $PSHOME", ""))
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
        test_example = json.loads(
            (ROOT / "config/ac2_member_gateway_worker.test_book.example.json").read_text(encoding="utf-8")
        )
        fields = {
            "gateway_base_url",
            "autocount_assembly_path",
            "autocount_server_name",
            "autocount_database_name",
            "autocount_user_id",
            "autocount_integration_user_id",
            "autocount_book_mode",
            "autocount_production_book",
            "autocount_test_book_allowlist",
        }
        for document, mode in ((example, "production"), (test_example, "test")):
            self.assertEqual(set(document), fields)
            self.assertNotIn("worker_host_binding", document)
            self.assertEqual(document["autocount_book_mode"], mode)
            for field in ("autocount_server_name", "autocount_database_name", "autocount_user_id", "autocount_integration_user_id", "autocount_production_book"):
                self.assertIsNone(document[field])
        self.assertEqual(example["autocount_test_book_allowlist"], [])
        self.assertEqual(test_example["autocount_test_book_allowlist"], ["REPLACE_WITH_TEST_BOOK_DATABASE_NAME"])


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


HOSTED_BOUNDARY_SKIP_REASON = "hosted-runner-only production boundary"
TASK_BOUNDARY_MARKER = "XB_TASK_BOUNDARY_INTEGRATION_RAN"
TASK_BOUNDARY_RESULT_PREFIX = "XB_BOUNDARY_RESULT:"
SCHED_S_TASK_HAS_NOT_RUN = 267011
INSTALLER_PATH = "scripts/install_ac2_member_gateway_worker.ps1"
INSTALLER_PACKAGE_FILES = (
    "ac2_member_gateway_worker.ps1",
    "ac2_member_gateway_worker_lib.ps1",
    "ac2_member_gateway_autocount_adapter.ps1",
    "launch_ac2_member_gateway_worker.ps1",
    "test_ac2_member_gateway_autocount_dependencies.ps1",
    "ac2_member_create_primitive.ps1",
)
WINDOWS_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def _hosted_windows_boundary_required(env: dict[str, str] | None = None) -> bool:
    """The real Task Scheduler boundary runs only on disposable GitHub-hosted Windows runners."""
    env = os.environ if env is None else env
    return (
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and env.get("RUNNER_OS") == "Windows"
    )


def _windows_powershell_module_environment() -> dict[str, str]:
    """Pin Windows PowerShell 5.1 module resolution so a pwsh parent cannot shadow ScheduledTasks."""
    env = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
    system_root = env.get("SystemRoot") or env.get("WINDIR") or r"C:\Windows"
    program_files = env.get("ProgramFiles") or r"C:\Program Files"
    env["PSModulePath"] = os.pathsep.join(
        (
            str(PureWindowsPath(system_root, "System32", "WindowsPowerShell", "v1.0", "Modules")),
            str(PureWindowsPath(program_files, "WindowsPowerShell", "Modules")),
        )
    )
    return env


def _git_output(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def _reviewed_package_identity() -> dict[str, object]:
    """Reviewed-package identity bound to the exact checked-out candidate commit and tree."""
    commit = _git_output("rev-parse", "HEAD")
    return {
        "schema_version": "xb.member.gateway.worker.reviewed-package.v1",
        "source": {"commit": commit, "tree": _git_output("rev-parse", "HEAD^{tree}")},
        "package_files": [
            {
                "name": name,
                "sha256": hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest(),
                "git_blob": _git_output("rev-parse", f"{commit}:scripts/{name}"),
            }
            for name in INSTALLER_PACKAGE_FILES
        ],
    }


def _boundary_report(stdout: str) -> dict[str, object] | None:
    lines = [line for line in stdout.splitlines() if line.startswith(TASK_BOUNDARY_RESULT_PREFIX)]
    if len(lines) != 1:
        return None
    return json.loads(lines[0][len(TASK_BOUNDARY_RESULT_PREFIX):])


_CI7_CHILD_SETUP_STAGES = (
    "INPUT",
    "SOURCE_LOAD",
    "ACCOUNT_PRESERVATION",
    "NATIVE_INIT",
    "CREDENTIAL_RESTORE",
    "CREDENTIAL_OBJECT_CREATE",
    "PRODUCT_TOKEN_OPEN",
    "FROZEN_CONTROL",
    "RESULT_EMIT",
    "CLEANUP",
)
_CI7_CHILD_STATUSES = (
    "completed",
    "structured_failure",
    "abnormal_exit",
    "malformed_output",
    "timeout",
    "termination_unproven",
    "launch_failed",
)
_CI7_FROZEN_OUTCOMES = (
    "effective_rights_exceeded",
    "effective_rights_missing",
    "effective_rights_unproven",
    "installation_owned_surface_unknown",
    "installation_manifest_invalid",
    "installation_manifest_membership_invalid",
    "installation_manifest_path_invalid",
    "installation_manifest_task_invalid",
    "installation_runtime_roots_invalid",
    "release_identity_mismatch",
    "unexpected_success",
    "unexpected_error",
    "fixture_setup_failed",
)
_CI7_CHILD_LIFECYCLE_TIMEOUTS_SECONDS = (60, 60, 60, 60, 3, 60, 60, 60)
_CI7_CHILD_LIFECYCLE_CLEANUP_MARGIN_SECONDS = 120
_CI7_CHILD_LIFECYCLE_OUTER_TIMEOUT_SECONDS = 544


def _public_safe_hosted_failure_summary(
    report: object,
    prefix: str = "hosted boundary assertion failed",
) -> str:
    """Render failure evidence using only fixed enums, booleans and bounded counts."""
    if not isinstance(report, dict):
        return f"{prefix}; diagnostics unavailable"

    parts: list[str] = []

    def add_enum(source: object, key: str, label: str, allowed: set[str]) -> None:
        if not isinstance(source, dict):
            return
        value = source.get(key)
        if isinstance(value, str) and value in allowed:
            parts.append(f"{label}={value}")

    def add_bool(source: object, key: str, label: str) -> None:
        if not isinstance(source, dict):
            return
        value = source.get(key)
        if type(value) is bool:
            parts.append(f"{label}={'true' if value else 'false'}")

    def add_count(source: object, key: str, label: str, maximum: int = 255) -> None:
        if not isinstance(source, dict):
            return
        value = source.get(key)
        if type(value) is int and 0 <= value <= maximum:
            parts.append(f"{label}={value}")

    safe_fatal_codes = {
        "ci7_fatal",
        "ci7_required_probes_incomplete",
        "ci7_fixture_cleanup_failed",
        "frozen_control_child_setup_failed",
        "frozen_control_child_structured_failure",
        "frozen_control_child_source_binding_invalid",
        "frozen_control_child_outcome_mismatch",
    }
    case = report.get("cases", {}).get("install_then_uninstall") if isinstance(report.get("cases"), dict) else None
    ci7 = case.get("ci7") if isinstance(case, dict) else None
    add_enum(case, "status", "case_status", {"completed", "error", "failed"})

    fatal = report.get("fatal")
    ci7_fatal = ci7.get("fatal") if isinstance(ci7, dict) else None
    fatal_code = fatal if fatal is not None else ci7_fatal
    if fatal_code is None:
        parts.append("fatal_code=none")
    elif isinstance(fatal_code, str) and fatal_code in safe_fatal_codes:
        parts.append(f"fatal_code={fatal_code}")
    else:
        parts.append("fatal_code=unclassified")

    add_enum(ci7, "frozen_control_child_status", "frozen_child_status", set(_CI7_CHILD_STATUSES))
    add_count(ci7, "frozen_control_child_exit_status", "child_exit_status")
    add_enum(ci7, "frozen_control_child_setup_stage", "child_setup_stage", set(_CI7_CHILD_SETUP_STAGES))
    add_bool(ci7, "fixture_account_preserved", "fixture_account_preserved")
    add_bool(ci7, "credential_username_matches_fixture", "credential_username_matches_fixture")
    add_enum(ci7, "product_token_open", "product_token_open", {"PASS", "FAIL", "NOT_REACHED"})
    add_bool(ci7, "frozen_control_source_binding", "frozen_source_binding")
    add_bool(ci7, "frozen_control_child_process_distinct", "child_process_distinct")
    add_bool(ci7, "frozen_control_parent_function_replaced", "parent_function_replaced")
    add_enum(ci7, "frozen_defective_context_outcome", "frozen_control_outcome", set(_CI7_FROZEN_OUTCOMES))
    add_bool(ci7, "frozen_control_child_terminated", "process_terminated")
    add_enum(ci7, "frozen_control_child_residue", "residue", {"none", "present", "unproven"})
    add_bool(ci7, "frozen_control_child_stderr_present", "stderr_present")

    completion = ci7.get("required_probe_completion") if isinstance(ci7, dict) else None
    add_enum(completion, "status", "required_probe_status", {"complete", "incomplete", "in_progress"})
    add_enum(completion, "completed_through", "required_probe_through", {"retention_delete"})
    if isinstance(case, dict):
        parts.append(f"uninstall={'PASS' if case.get('uninstall_outcome') == 'pass' else 'FAIL'}")

    cleanup = report.get("cleanup")
    required_cleanup = (
        "production_folder_absent",
        "program_files_parent_absent",
        "program_data_parent_absent",
        "stage_residue_absent",
        "lsa_account_object_absent",
        "profile_absent",
        "user_absent",
    )
    if isinstance(cleanup, dict) and all(type(cleanup.get(key)) is bool for key in required_cleanup):
        cleanup_pass = all(cleanup[key] for key in required_cleanup)
        parts.append(f"disposable_cleanup_readback={'PASS' if cleanup_pass else 'FAIL'}")

    secret_exposure = report.get("secret_exposure")
    if secret_exposure == "none":
        parts.append("credential_secret_exposure=none")
    elif secret_exposure == "detected":
        parts.append("credential_secret_exposure=confirmed")
    else:
        parts.append("credential_secret_exposure=possible")
    parts.append("failure_output_privacy=PASS")
    parts.append("private_evidence_exposure=none")

    if not parts:
        return f"{prefix}; diagnostics unavailable"
    return f"{prefix}; " + "; ".join(parts[:24])


class MemberWorkerHostedFailureSummaryTests(unittest.TestCase):
    def test_failure_summary_uses_only_bounded_public_fields(self) -> None:
        private_values = (
            r"C:\Users\private-user\AppData\Local\worker-runtime\task.xml",
            "xbt-private-account-sentinel",
            "S-1-5-21-111111111-222222222-333333333-4444",
            "private-task-arguments-sentinel",
            "private-password-sentinel",
            "private-encrypted-password-sentinel",
            "private-exception-text-sentinel",
        )
        report = {
            "fatal": private_values[0],
            "secret_exposure": "none",
            "cleanup": {"pass": False, "absolute_path": private_values[0]},
            "cases": {
                "install_then_uninstall": {
                    "status": "error",
                    "uninstall_outcome": private_values[6],
                    "ci7": {
                        "fatal": private_values[6],
                        "frozen_control_child_status": "structured_failure",
                        "frozen_control_child_exit_status": 1,
                        "frozen_control_child_setup_stage": "PRODUCT_TOKEN_OPEN",
                        "fixture_account_preserved": True,
                        "credential_username_matches_fixture": True,
                        "product_token_open": "FAIL",
                        "frozen_control_source_binding": True,
                        "frozen_control_child_process_distinct": True,
                        "frozen_control_parent_function_replaced": False,
                        "frozen_defective_context_outcome": "fixture_setup_failed",
                        "frozen_control_child_terminated": True,
                        "frozen_control_child_residue": "none",
                        "frozen_control_child_stderr_present": True,
                        "worker_account": private_values[1],
                        "worker_sid": private_values[2],
                        "task_arguments": private_values[3],
                        "credential": private_values[4],
                        "encrypted_password": private_values[5],
                        "required_probe_completion": {"status": "incomplete", "completed_through": "retention_delete"},
                    },
                },
            },
        }
        summary = _public_safe_hosted_failure_summary(report)
        if any(value in summary for value in private_values):
            raise AssertionError("public-safe hosted diagnostic emitted private fixture data")
        if any(key in summary for key in ("worker_account=", "worker_sid=", "task_arguments=", "credential=", "encrypted_password=")):
            raise AssertionError("public-safe hosted diagnostic emitted a private field name")
        for required in (
            "case_status=error",
            "frozen_child_status=structured_failure",
            "child_exit_status=1",
            "child_setup_stage=PRODUCT_TOKEN_OPEN",
            "fixture_account_preserved=true",
            "credential_username_matches_fixture=true",
            "product_token_open=FAIL",
            "frozen_source_binding=true",
            "child_process_distinct=true",
            "parent_function_replaced=false",
            "frozen_control_outcome=fixture_setup_failed",
            "process_terminated=true",
            "residue=none",
            "stderr_present=true",
            "required_probe_status=incomplete",
            "uninstall=FAIL",
            "credential_secret_exposure=none",
            "failure_output_privacy=PASS",
            "private_evidence_exposure=none",
        ):
            if required not in summary:
                raise AssertionError("public-safe hosted diagnostic omitted a bounded status field")
        if len(summary) > 1024:
            raise AssertionError("public-safe hosted diagnostic exceeded its output bound")

        diagnostic_case = MemberWorkerHostedTaskBoundaryTests("test_no_secret_exposure")
        diagnostic_case.report = report
        try:
            diagnostic_case.assertEqual(private_values[1], private_values[2])
        except AssertionError as error:
            assertion_message = str(error)
        else:
            raise AssertionError("hosted report assertion unexpectedly passed")
        if any(value in assertion_message for value in private_values):
            raise AssertionError("hosted assertion message emitted private report data")


def _installer_function(source: str, name: str) -> str:
    start = source.index(f"function {name} {{")
    end = source.find("\nfunction ", start + 1)
    return source[start:] if end < 0 else source[start:end]


def _frozen_ci7_context_source() -> tuple[str, str]:
    revision = f"{CI7_DEFECTIVE_BASELINE_COMMIT}:{INSTALLER_PATH}"
    blob = _git_output("rev-parse", revision)
    if blob != CI7_DEFECTIVE_INSTALLER_BLOB:
        raise AssertionError(f"frozen CI7 installer blob changed: {blob}")
    completed = subprocess.run(
        ["git", "show", revision],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return _installer_function(completed.stdout, "Open-XbCi7VerificationContext"), blob


class MemberWorkerCi7MutationPolicySourceTests(unittest.TestCase):
    """Bind the defective control and freeze every adjacent CI7 contract."""

    def test_frozen_control_and_adjacent_ci7_contracts_are_immutable(self) -> None:
        frozen_context, blob = _frozen_ci7_context_source()
        self.assertEqual(blob, CI7_DEFECTIVE_INSTALLER_BLOB)
        baseline_source = subprocess.run(
            ["git", "show", f"{CI7_DEFECTIVE_BASELINE_COMMIT}:{INSTALLER_PATH}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        self.assertEqual(frozen_context, _installer_function(baseline_source, "Open-XbCi7VerificationContext"))
        for right in (
            "0x00000002", "0x00000004", "0x00000010", "0x00000040",
            "0x00000100", "0x00010000", "0x00040000", "0x00080000",
        ):
            with self.subTest(frozen_denial=right):
                self.assertIn(right, frozen_context)

        candidate = (ROOT / INSTALLER_PATH).read_text(encoding="utf-8")
        candidate_context = _installer_function(candidate, "Open-XbCi7VerificationContext")
        self.assertIn(
            "$directoryMutationDenials = @([uint32]0x00000002, [uint32]0x00000004, [uint32]0x00000010,\n"
            "            [uint32]0x00000040, [uint32]0x00000100, [uint32]0x00010000, [uint32]0x00040000, [uint32]0x00080000)",
            candidate_context,
        )
        self.assertIn(
            "$ancestorMutationDenials = @([uint32]0x00000040, [uint32]0x00010000, [uint32]0x00040000, [uint32]0x00080000)",
            candidate_context,
        )
        self.assertIn("if ($directoryKey -ieq $protectedRootKey)", candidate_context)

        for function_name in (
            "Get-XbNativePathChain",
            "Invoke-XbNativeAccessCheck",
            "Invoke-XbCi7HandleAccessCheck",
            "Add-XbCi7ProtectedLeaf",
            "Assert-XbCi7DirectoryRights",
            "Assert-XbCi7PathDeletionComposition",
            "Confirm-XbCi7VerificationContext",
        ):
            with self.subTest(unchanged_function=function_name):
                self.assertEqual(
                    _installer_function(candidate, function_name),
                    _installer_function(baseline_source, function_name),
                )

    def test_context_construction_failure_disposes_every_object_in_reverse_order(self) -> None:
        source = (ROOT / INSTALLER_PATH).read_text(encoding="utf-8")
        context = _installer_function(source, "Open-XbCi7VerificationContext")
        self.assertNotIn("[-1..0]", context)
        self.assertIn("$items = @($objects.ToArray())", context)
        self.assertIn("for ($index = $items.Count - 1; $index -ge 0; $index--)", context)
        self.assertEqual(context.count("$items[$index].Dispose()"), 1)
        self.assertIn("try { $items[$index].Dispose() } catch { }", context)
        self.assertIn("$reason = [string]$_.Exception.Message", context)
        self.assertIn('if ($reason -in @("effective_rights_missing", "effective_rights_exceeded", "effective_rights_unproven"', context)

        harness = _HOSTED_TASK_BOUNDARY_HARNESS
        denial = harness.index('$ci7.required_operation_denied = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential }')
        owner_probe = harness.index('$ci7.failure_cleanup_lock_owner_count = [int][XbCi7FailureCleanupProbe]::GetOwnerCount', denial)
        delete_probe = harness.index('$ci7.failure_cleanup_exclusive_delete_succeeded = [bool][XbCi7FailureCleanupProbe]::ProbeExclusiveDelete', owner_probe)
        self.assertLess(denial, owner_probe)
        self.assertLess(owner_probe, delete_probe)
        for marker in (
            '$script:failureCleanupPrimitiveLeafOpened = $false',
            '$script:failureCleanupPrimitiveLeafOpened = $true',
            '$record = & $script:failureCleanupOriginalAddProtectedLeaf @PSBoundParameters',
            '$ci7.failure_cleanup_primitive_leaf_opened = [bool]$script:failureCleanupPrimitiveLeafOpened',
            '$ci7.failure_cleanup_owner_enumeration_completed = [bool]$ownerEnumerationCompleted',
            '$ci7.failure_cleanup_current_harness_owner = [bool]$currentHarnessOwnsPrimitive',
            '$ci7.failure_cleanup_exclusive_delete_completed = [bool]$exclusiveDeleteCompleted',
        ):
            with self.subTest(boundary_regression=marker):
                self.assertIn(marker, harness)


_LOCAL_TASK_CONTRACT_HARNESS = r'''[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InstallerPath,
    [Parameter(Mandatory)][string]$WorkRoot
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. $InstallerPath -LibraryOnly
$out = [ordered]@{}

function Get-XbOutcome {
    param([Parameter(Mandatory)][scriptblock]$Body)
    try { $null = & $Body; return "pass" } catch { return [string]$_.Exception.Message }
}

# In-memory triggerless task built exactly like the installer: CIM exposes Triggers as $null.
$memoryAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoLogo"
$memorySettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) -RestartCount 0 -StartWhenAvailable:$false -Disable
$memoryTask = New-ScheduledTask -Action $memoryAction -Settings $memorySettings
$out.in_memory = [ordered]@{
    triggers_null = ($null -eq $memoryTask.Triggers)
    historical_expression_count = @($memoryTask.Triggers).Count
    filtered_trigger_count = (Get-XbNonNullCount $memoryTask.Triggers)
    filtered_action_count = (Get-XbNonNullCount $memoryTask.Actions)
}

$ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
$zeroXml = '<?xml version="1.0" encoding="UTF-16"?><Task version="1.2" xmlns="' + $ns + '"><Settings><Enabled>false</Enabled></Settings></Task>'
$emptyTriggersXml = '<Task version="1.2" xmlns="' + $ns + '"><Triggers /></Task>'
$oneXml = '<Task version="1.2" xmlns="' + $ns + '"><Triggers><TimeTrigger><StartBoundary>2099-01-01T00:00:00</StartBoundary></TimeTrigger></Triggers></Task>'
$twoXml = '<Task version="1.2" xmlns="' + $ns + '"><Triggers><TimeTrigger /><BootTrigger /></Triggers></Task>'
$bareXml = '<Task><Triggers><TimeTrigger /></Triggers></Task>'
$fakeTrigger = [pscustomobject]@{ Enabled = $true }

function Get-XbOracleOutcome {
    param([int]$Com, [AllowEmptyString()][string]$Xml, $Cim)
    try { return [string](Get-XbTaskTriggerOracleCount -ComTriggerCount $Com -TaskXml $Xml -CimTriggers $Cim) }
    catch { return [string]$_.Exception.Message }
}

$out.oracle = [ordered]@{
    zero = (Get-XbOracleOutcome 0 $zeroXml $null)
    empty_triggers_element = (Get-XbOracleOutcome 0 $emptyTriggersXml $null)
    one = (Get-XbOracleOutcome 1 $oneXml @($fakeTrigger))
    two = (Get-XbOracleOutcome 2 $twoXml @($fakeTrigger, $fakeTrigger))
    com_zero_xml_one = (Get-XbOracleOutcome 0 $oneXml $null)
    com_one_xml_zero = (Get-XbOracleOutcome 1 $zeroXml @($fakeTrigger))
    cim_null_with_one = (Get-XbOracleOutcome 1 $oneXml $null)
    cim_one_with_zero = (Get-XbOracleOutcome 0 $zeroXml @($fakeTrigger))
    un_namespaced = (Get-XbOracleOutcome 1 $bareXml @($fakeTrigger))
    malformed = (Get-XbOracleOutcome 0 '<Task' $null)
    empty = (Get-XbOracleOutcome 0 '' $null)
}

$WorkerAccount = "XBHOST\xbworker"
$launcher = Join-Path $InstallRoot "launch_ac2_member_gateway_worker.ps1"
$identity = Get-XbWorkerTaskIdentity -LauncherPath $launcher -WorkerAccount $WorkerAccount

function New-XbFakeTask {
    param([hashtable]$Override = @{})
    $values = @{
        TaskPath = $taskPath; TaskName = $taskName; State = "Disabled"; Triggers = $null
        Actions = @([pscustomobject]@{ Execute = $identity.executable; Arguments = $identity.arguments; WorkingDirectory = "" })
        MultipleInstances = "IgnoreNew"; ExecutionTimeLimit = "PT10M"; RestartCount = 0; StartWhenAvailable = $false
        UserId = $identity.principal_user_id; LogonType = "Password"; RunLevel = "Limited"
    }
    foreach ($key in @($Override.Keys)) { $values[$key] = $Override[$key] }
    return [pscustomobject]@{
        TaskPath = $values.TaskPath; TaskName = $values.TaskName; State = $values.State
        Triggers = $values.Triggers; Actions = $values.Actions
        Settings = [pscustomobject]@{ MultipleInstances = $values.MultipleInstances; ExecutionTimeLimit = $values.ExecutionTimeLimit; RestartCount = $values.RestartCount; StartWhenAvailable = $values.StartWhenAvailable }
        Principal = [pscustomobject]@{ UserId = $values.UserId; LogonType = $values.LogonType; RunLevel = $values.RunLevel }
    }
}

function New-XbFakeView {
    param([hashtable]$Override = @{})
    $values = @{
        Path = $taskPath + $taskName; Enabled = $false; SettingsEnabled = $false; TriggerCount = 0; Xml = $zeroXml
        ActionCount = 1; ActionType = 0; ActionPath = $identity.executable; ActionArguments = $identity.arguments
    }
    foreach ($key in @($Override.Keys)) { $values[$key] = $Override[$key] }
    $actions = [pscustomobject]@{ Count = $values.ActionCount; Entry = [pscustomobject]@{ Type = $values.ActionType; Path = $values.ActionPath; Arguments = $values.ActionArguments } }
    Add-Member -InputObject $actions -MemberType ScriptMethod -Name Item -Value { param($Index) return $this.Entry }
    return [pscustomobject]@{
        Path = $values.Path; Enabled = $values.Enabled; Xml = $values.Xml
        Definition = [pscustomobject]@{ Settings = [pscustomobject]@{ Enabled = $values.SettingsEnabled }; Triggers = [pscustomobject]@{ Count = $values.TriggerCount }; Actions = $actions }
    }
}

$originalView = ${function:Get-XbRegisteredWorkerTaskView}
$script:fakeView = $null
Set-Item function:script:Get-XbRegisteredWorkerTaskView { return $script:fakeView }
function Get-XbContractOutcome {
    param([hashtable]$TaskOverride = @{}, [hashtable]$ViewOverride = @{})
    $script:fakeView = New-XbFakeView $ViewOverride
    $candidate = New-XbFakeTask $TaskOverride
    return Get-XbOutcome { Assert-XbWorkerTaskContract -Task $candidate -ExpectedIdentity $identity }
}
$out.contract = [ordered]@{
    valid_null_triggers = (Get-XbContractOutcome)
    historical_expression_on_valid = @((New-XbFakeTask).Triggers).Count
    com_enabled = (Get-XbContractOutcome -ViewOverride @{ Enabled = $true })
    settings_enabled = (Get-XbContractOutcome -ViewOverride @{ SettingsEnabled = $true })
    cim_ready = (Get-XbContractOutcome -TaskOverride @{ State = "Ready" })
    one_trigger = (Get-XbContractOutcome -TaskOverride @{ Triggers = @($fakeTrigger) } -ViewOverride @{ TriggerCount = 1; Xml = $oneXml })
    oracle_disagreement = (Get-XbContractOutcome -ViewOverride @{ TriggerCount = 1 })
    cim_actions_null = (Get-XbContractOutcome -TaskOverride @{ Actions = $null })
    com_two_actions = (Get-XbContractOutcome -ViewOverride @{ ActionCount = 2 })
    com_action_path = (Get-XbContractOutcome -ViewOverride @{ ActionPath = "cmd.exe" })
    com_action_type = (Get-XbContractOutcome -ViewOverride @{ ActionType = 5 })
    task_path_mismatch = (Get-XbContractOutcome -TaskOverride @{ TaskPath = "\Other\" })
    restart_count = (Get-XbContractOutcome -TaskOverride @{ RestartCount = 1 })
    logon_type = (Get-XbContractOutcome -TaskOverride @{ LogonType = "S4U" })
}
Set-Item function:script:Get-XbRegisteredWorkerTaskView $originalView

$out.hresult = [ordered]@{
    file_not_found = (Get-XbComHResult ([IO.FileNotFoundException]::new("absent")))
    wrapped_file_not_found = (Get-XbComHResult ([Management.Automation.MethodInvocationException]::new("wrapped", [IO.FileNotFoundException]::new("absent"))))
    access_denied = (Get-XbComHResult ([UnauthorizedAccessException]::new("denied")))
}

$originalConnect = ${function:Connect-XbTaskService}
$script:fakeFolderError = $null
Set-Item function:script:Connect-XbTaskService {
    $service = [pscustomobject]@{}
    Add-Member -InputObject $service -MemberType ScriptMethod -Name GetFolder -Value { param($Path) if ($null -ne $script:fakeFolderError) { throw $script:fakeFolderError }; return [pscustomobject]@{ Path = $Path } }
    return $service
}
function Get-XbFolderOutcome {
    param($ErrorValue)
    $script:fakeFolderError = $ErrorValue
    try { return [string](Test-XbTaskFolderPresent -Path "\X-Boundaries\" -ErrorId "task_folder_preimage_unproven") }
    catch { return [string]$_.Exception.Message }
}
$out.folder_preimage = [ordered]@{
    present = (Get-XbFolderOutcome $null)
    file_not_found = (Get-XbFolderOutcome ([IO.FileNotFoundException]::new("absent")))
    access_denied = (Get-XbFolderOutcome ([UnauthorizedAccessException]::new("denied")))
    path_not_found = (Get-XbFolderOutcome ([Runtime.InteropServices.COMException]::new("path", -2147024893)))
}
Set-Item function:script:Connect-XbTaskService $originalConnect

# Read-only Schedule.Service probes: no folder or task is created or deleted here.
$absentFolder = "\XB-Absent-" + [Guid]::NewGuid().ToString("N") + "\"
$productionTaskPath = $taskPath
$out.real_folder = [ordered]@{
    root_present = (Test-XbTaskFolderPresent -Path "\" -ErrorId "unproven")
    random_absent = (Test-XbTaskFolderPresent -Path $absentFolder -ErrorId "unproven")
}
$script:taskPath = "\"
$out.real_folder.root_cleanup = (Get-XbOutcome { Remove-XbAttemptCreatedTaskFolder })
$script:taskPath = $absentFolder
$out.real_folder.absent_cleanup = (Get-XbOutcome { Remove-XbAttemptCreatedTaskFolder })
$script:taskPath = $productionTaskPath

$containers = Join-Path $WorkRoot "containers"
New-Item -ItemType Directory -Path $containers | Out-Null
function New-XbDirectory { param([string]$Path) New-Item -ItemType Directory -Path $Path -Force | Out-Null; return $Path }
$emptyPath = New-XbDirectory (Join-Path $containers "empty")
$childPath = New-XbDirectory (Join-Path $containers "child")
Set-Content -LiteralPath (Join-Path $childPath "unrelated.txt") -Value "keep"
$hiddenPath = New-XbDirectory (Join-Path $containers "hidden")
Set-Content -LiteralPath (Join-Path $hiddenPath "hidden.txt") -Value "keep"
(Get-Item -LiteralPath (Join-Path $hiddenPath "hidden.txt") -Force).Attributes = "Hidden"
$nestedPath = New-XbDirectory (Join-Path $containers "nested")
New-XbDirectory (Join-Path $nestedPath "inner") | Out-Null
$junctionTarget = New-XbDirectory (Join-Path $containers "junction-target")
$junctionPath = Join-Path $containers "junction"
& cmd.exe /c mklink /J "$junctionPath" "$junctionTarget" | Out-Null
$filePath = Join-Path $containers "file"
Set-Content -LiteralPath $filePath -Value "keep"
$out.container = [ordered]@{
    absent = (Get-XbOutcome { Remove-XbAttemptCreatedContainer -Path (Join-Path $containers "absent") })
    empty = (Get-XbOutcome { Remove-XbAttemptCreatedContainer -Path $emptyPath })
    empty_removed = (-not (Test-Path -LiteralPath $emptyPath))
    child = (Get-XbOutcome { Remove-XbAttemptCreatedContainer -Path $childPath })
    child_preserved = (Test-Path -LiteralPath (Join-Path $childPath "unrelated.txt"))
    hidden_child = (Get-XbOutcome { Remove-XbAttemptCreatedContainer -Path $hiddenPath })
    hidden_child_preserved = (Test-Path -LiteralPath (Join-Path $hiddenPath "hidden.txt"))
    nested = (Get-XbOutcome { Remove-XbAttemptCreatedContainer -Path $nestedPath })
    nested_preserved = (Test-Path -LiteralPath (Join-Path $nestedPath "inner"))
    junction = (Get-XbOutcome { Remove-XbAttemptCreatedContainer -Path $junctionPath })
    junction_preserved = (Test-Path -LiteralPath $junctionPath)
    junction_target_preserved = (Test-Path -LiteralPath $junctionTarget)
    file = (Get-XbOutcome { Remove-XbAttemptCreatedContainer -Path $filePath })
    file_preserved = (Test-Path -LiteralPath $filePath)
}
& cmd.exe /c rmdir "$junctionPath" | Out-Null

$originalTaskRead = ${function:Get-XbWorkerTaskIfPresent}
$originalUnregister = ${function:Remove-XbWorkerScheduledTask}
$script:unregisterCalls = 0
Set-Item function:script:Remove-XbWorkerScheduledTask { $script:unregisterCalls++ }
function Get-XbTaskStepOutcome {
    param([Parameter(Mandatory)][scriptblock]$Presence)
    Set-Item function:script:Get-XbWorkerTaskIfPresent $Presence
    $script:unregisterCalls = 0
    $outcome = Get-XbOutcome { Remove-XbAttemptRegisteredTask -LauncherPath $launcher }
    return [ordered]@{ outcome = $outcome; unregister_calls = $script:unregisterCalls }
}
$out.task_step = [ordered]@{
    absent = (Get-XbTaskStepOutcome { return $null })
    owned = (Get-XbTaskStepOutcome { return (New-XbFakeTask) })
    foreign_action = (Get-XbTaskStepOutcome { return (New-XbFakeTask @{ Actions = @([pscustomobject]@{ Execute = "cmd.exe"; Arguments = "/c"; WorkingDirectory = "" }) }) })
    foreign_principal = (Get-XbTaskStepOutcome { return (New-XbFakeTask @{ UserId = "XBHOST\other" }) })
    ambiguous = (Get-XbTaskStepOutcome { throw "task_presence_unproven" })
}

$script:folderCleanupCalls = 0
Set-Item function:script:Remove-XbAttemptCreatedTaskFolder { $script:folderCleanupCalls++ }
Set-Item function:script:Get-XbWorkerTaskIfPresent { return $null }
function Initialize-XbAttempt {
    param([Parameter(Mandatory)][string]$Base, [switch]$PreexistingParents)
    $script:InstallRoot = Join-Path $Base "pf\X-Boundaries\MemberGatewayWorker"
    $script:RuntimeRoot = Join-Path $Base "pd\X-Boundaries\MemberGatewayWorker"
    New-Item -ItemType Directory -Path (Join-Path $Base "pf") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $Base "pd") -Force | Out-Null
    if ($PreexistingParents) {
        foreach ($parent in @((Split-Path -Parent $InstallRoot), (Split-Path -Parent $RuntimeRoot))) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
            Set-Content -LiteralPath (Join-Path $parent "sentinel.txt") -Value "keep"
        }
    }
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $InstallRoot "installation-manifest.json") -Value "{}"
    foreach ($child in @("config", "secrets", "logs", "rollback")) { New-Item -ItemType Directory -Path (Join-Path $RuntimeRoot $child) -Force | Out-Null }
    $stage = Join-Path $Base "stage"
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $stage "staged.txt") -Value "x"
    return $stage
}
function Get-XbAttemptState {
    param([Parameter(Mandatory)][string]$Base, [Parameter(Mandatory)][string]$Stage)
    return [ordered]@{
        install_root = (Test-Path -LiteralPath $InstallRoot)
        runtime_root = (Test-Path -LiteralPath $RuntimeRoot)
        program_files_parent = (Test-Path -LiteralPath (Split-Path -Parent $InstallRoot))
        program_data_parent = (Test-Path -LiteralPath (Split-Path -Parent $RuntimeRoot))
        program_files_grandparent = (Test-Path -LiteralPath (Join-Path $Base "pf"))
        program_data_grandparent = (Test-Path -LiteralPath (Join-Path $Base "pd"))
        stage = (Test-Path -LiteralPath $Stage)
        folder_cleanup_calls = $script:folderCleanupCalls
    }
}
$absentPreimage = [ordered]@{ program_files_parent = $false; program_data_parent = $false; scheduler_folder = $false }
$presentPreimage = [ordered]@{ program_files_parent = $true; program_data_parent = $true; scheduler_folder = $true }
$out.rollback = [ordered]@{}

$base = Join-Path $WorkRoot "parents-absent"
$stage = Initialize-XbAttempt -Base $base
$script:folderCleanupCalls = 0
$outcome = Get-XbOutcome { Invoke-XbWorkerInstallRollback -Preimage $absentPreimage -StageRoot $stage -RegistrationAttempted $true }
$out.rollback.parents_absent = [ordered]@{ outcome = $outcome; state = (Get-XbAttemptState -Base $base -Stage $stage) }

$base = Join-Path $WorkRoot "parents-preexisting"
$stage = Initialize-XbAttempt -Base $base -PreexistingParents
$script:folderCleanupCalls = 0
$outcome = Get-XbOutcome { Invoke-XbWorkerInstallRollback -Preimage $presentPreimage -StageRoot $stage -RegistrationAttempted $true }
$out.rollback.parents_preexisting = [ordered]@{
    outcome = $outcome
    state = (Get-XbAttemptState -Base $base -Stage $stage)
    program_files_sentinel = (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $InstallRoot) "sentinel.txt"))
    program_data_sentinel = (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $RuntimeRoot) "sentinel.txt"))
}

$base = Join-Path $WorkRoot "not-attempted"
$stage = Initialize-XbAttempt -Base $base
Set-Item function:script:Get-XbWorkerTaskIfPresent { throw "unexpected_task_read" }
$script:folderCleanupCalls = 0
$outcome = Get-XbOutcome { Invoke-XbWorkerInstallRollback -Preimage $absentPreimage -StageRoot $stage -RegistrationAttempted $false }
$out.rollback.not_attempted = [ordered]@{ outcome = $outcome; state = (Get-XbAttemptState -Base $base -Stage $stage) }
Set-Item function:script:Get-XbWorkerTaskIfPresent { return $null }

$base = Join-Path $WorkRoot "unexpected-content"
$stage = Initialize-XbAttempt -Base $base
$unexpected = Join-Path (Split-Path -Parent $RuntimeRoot) "unexpected.txt"
Set-Content -LiteralPath $unexpected -Value "keep"
$script:folderCleanupCalls = 0
$outcome = Get-XbOutcome { Invoke-XbWorkerInstallRollback -Preimage $absentPreimage -StageRoot $stage -RegistrationAttempted $true }
$out.rollback.unexpected_content = [ordered]@{ outcome = $outcome; state = (Get-XbAttemptState -Base $base -Stage $stage); unexpected_preserved = (Test-Path -LiteralPath $unexpected) }

Set-Item function:script:Get-XbWorkerTaskIfPresent $originalTaskRead
Set-Item function:script:Remove-XbWorkerScheduledTask $originalUnregister
[Console]::Out.Write(($out | ConvertTo-Json -Depth 8 -Compress))
'''


_FROZEN_CI7_CHILD_SCRIPT = r'''param([Parameter(Mandatory)]$Fixture)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$childResult = [ordered]@{
    schema_version = "xb.member.worker.ci7.frozen-control.v1"
    fixture_status = "failed"
    outcome = "fixture_setup_failed"
    setup_stage = "INPUT"
    source_commit = ""
    source_blob = ""
    fixture_account_preserved = $null
    credential_username_matches_fixture = $null
    product_token_open = "NOT_REACHED"
    product_token_identity_matches_fixture = $null
}
$setupStage = "INPUT"
$securePassword = $null
$credential = $null
$nativeToken = $null
$context = $null
try {
    $childResult.source_commit = [string]$Fixture.source_commit
    $childResult.source_blob = [string]$Fixture.source_blob
    $installerPath = [string]$Fixture.installer_path
    # CI7_FIXTURE_ACCOUNT_PRESERVATION_BEGIN
    $fixtureWorkerAccount = [string]$Fixture.worker_account
    # CI7_FIXTURE_ACCOUNT_PRESERVATION_END
    $encryptedPassword = [string]$Fixture.encrypted_password
    $frozenContextText = [string]$Fixture.frozen_context_source
    if ($Fixture.source_commit -cne "ef194d43cd5b2a6e56468c3381a1b44bced23d8d" -or
        $Fixture.source_blob -cne "aa6d4f3172bbc82c69f1c4904f6e4bbf50da3741" -or
        $frozenContextText -notmatch '^function Open-XbCi7VerificationContext \{(?s).+\}\s*$' -or
        $frozenContextText.Length -gt 32768 -or
        [string]::IsNullOrWhiteSpace($installerPath) -or
        $fixtureWorkerAccount -cnotmatch '^xbt[0-9a-f]{12}$' -or
        [string]::IsNullOrWhiteSpace($encryptedPassword)) {
        throw "fixture_input_invalid"
    }

    $setupStage = "SOURCE_LOAD"
    . $installerPath -LibraryOnly -WorkerAccount $fixtureWorkerAccount
    $setupStage = "ACCOUNT_PRESERVATION"
    $workerAccount = $fixtureWorkerAccount
    $script:WorkerAccount = $fixtureWorkerAccount
    $childResult.fixture_account_preserved = [bool](
        $workerAccount -ceq $fixtureWorkerAccount -and $script:WorkerAccount -ceq $fixtureWorkerAccount
    )
    if (-not $childResult.fixture_account_preserved) { throw "fixture_account_not_preserved" }

    $setupStage = "NATIVE_INIT"
    Initialize-XbWorkerNativeAccess
    $setupStage = "CREDENTIAL_RESTORE"
    $securePassword = ConvertTo-SecureString -String $encryptedPassword -ErrorAction Stop
    $setupStage = "CREDENTIAL_OBJECT_CREATE"
    $credential = New-Object Management.Automation.PSCredential($fixtureWorkerAccount, $securePassword)
    $childResult.credential_username_matches_fixture = [bool]($credential.UserName -ceq $fixtureWorkerAccount)
    if (-not $childResult.credential_username_matches_fixture) { throw "fixture_credential_identity_mismatch" }
    $encryptedPassword = $null
    $Fixture.encrypted_password = $null

    $setupStage = "PRODUCT_TOKEN_OPEN"
    try {
        $nativeToken = New-XbWorkerBatchToken -Credential $credential
        if ($null -eq $nativeToken) { throw "fixture_product_token_unproven" }
        $fixtureSid = Get-XbAccountSid -Account $fixtureWorkerAccount
        $childResult.product_token_identity_matches_fixture = [bool]($nativeToken.UserSid -ceq $fixtureSid)
        $fixtureSid = $null
        if (-not $childResult.product_token_identity_matches_fixture) { throw "fixture_product_token_identity_mismatch" }
        $childResult.product_token_open = "PASS"
    } catch {
        $childResult.product_token_open = "FAIL"
        throw "fixture_product_token_open_failed"
    }

    $setupStage = "FROZEN_CONTROL"
    $frozenOpen = $frozenContextText.IndexOf("{")
    $frozenClose = $frozenContextText.LastIndexOf("}")
    if ($frozenOpen -lt 0 -or $frozenClose -le $frozenOpen) { throw "fixture_frozen_source_invalid" }
    $frozenContextBody = $frozenContextText.Substring($frozenOpen + 1, $frozenClose - $frozenOpen - 1)
    Set-Item function:script:Open-XbCi7VerificationContext ([scriptblock]::Create($frozenContextBody))
    try {
        $context = Open-XbCi7VerificationContext -Token $nativeToken
        $childResult.outcome = "unexpected_success"
    } catch {
        $safeOutcomes = @(
            "effective_rights_exceeded", "effective_rights_missing", "effective_rights_unproven",
            "installation_owned_surface_unknown", "installation_manifest_invalid",
            "installation_manifest_membership_invalid", "installation_manifest_path_invalid",
            "installation_manifest_task_invalid", "installation_runtime_roots_invalid",
            "release_identity_mismatch"
        )
        $reason = [string]$_.Exception.Message
        $childResult.outcome = if ($safeOutcomes -ccontains $reason) { $reason } else { "unexpected_error" }
    }
    if ($null -ne $context) {
        Dispose-XbCi7VerificationContext -Context $context
        $context = $null
    }
    $childResult.fixture_status = "completed"
    $setupStage = "RESULT_EMIT"
} catch {
    $childResult.fixture_status = "failed"
    $childResult.outcome = "fixture_setup_failed"
    $childResult.setup_stage = $setupStage
} finally {
    $cleanupFailed = $false
    $setupSucceeded = $childResult.fixture_status -ceq "completed"
    if ($setupSucceeded) { $setupStage = "CLEANUP" }
    if ($null -ne $context) {
        try { Dispose-XbCi7VerificationContext -Context $context } catch { $cleanupFailed = $true }
    }
    if ($null -ne $nativeToken) { try { $nativeToken.Dispose() } catch { $cleanupFailed = $true } }
    if ($null -ne $securePassword) { try { $securePassword.Dispose() } catch { $cleanupFailed = $true } }
    $credential = $null
    $Fixture = $null
    $fixtureWorkerAccount = $null
    $workerAccount = $null
    $script:WorkerAccount = $null
    $encryptedPassword = $null
    if ($cleanupFailed -and $setupSucceeded) {
        $childResult.fixture_status = "failed"
        $childResult.outcome = "fixture_setup_failed"
        $childResult.setup_stage = "CLEANUP"
    }
}
if ($childResult.fixture_status -ceq "completed") { $childResult.setup_stage = "RESULT_EMIT" }
[Console]::Out.WriteLine(($childResult | ConvertTo-Json -Depth 4 -Compress))
if ($childResult.fixture_status -cne "completed") { exit 1 }
exit 0
'''


_HOSTED_TASK_BOUNDARY_HARNESS = r'''[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InstallerPath,
    [Parameter(Mandatory)][string]$ReviewedManifestPath,
    [Parameter(Mandatory)][string]$FrozenContextPath,
    [Parameter(Mandatory)][string]$FrozenChildScriptPath,
    [Parameter(Mandatory)][string]$FrozenSourceCommit,
    [Parameter(Mandatory)][string]$FrozenInstallerBlob
)
# Disposable GitHub-hosted Windows runner only. Never starts a task; every created object is removed and read back.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$report = [ordered]@{
    harness = "xb.member.worker.task-boundary.v1"
    environment = [ordered]@{}
    account = [ordered]@{}
    cases = [ordered]@{}
    cleanup = [ordered]@{}
    fatal = $null
    secret_exposure = "unchecked"
}
$secretValues = New-Object 'System.Collections.Generic.List[string]'
$trace = New-Object 'System.Collections.Generic.List[string]'
$state = @{ pristine_proven = $false; user_sid = $null; temp_redirect = $null; stage_before = @(); boundary_folder = $null; boundary_tasks = @(); lsa_loaded = $false }
$securePassword = $null
$secureWrong = $null

$lsaSource = @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Security.Principal;

public static class XbBoundaryLsa
{
    [StructLayout(LayoutKind.Sequential)]
    private struct LsaUnicodeString { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }

    [StructLayout(LayoutKind.Sequential)]
    private struct LsaObjectAttributes { public int Length; public IntPtr RootDirectory; public IntPtr ObjectName; public uint Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService; }

    [DllImport("advapi32.dll")] private static extern uint LsaOpenPolicy(IntPtr systemName, ref LsaObjectAttributes objectAttributes, uint desiredAccess, out IntPtr policyHandle);
    [DllImport("advapi32.dll")] private static extern uint LsaEnumerateAccountRights(IntPtr policyHandle, byte[] accountSid, out IntPtr userRights, out uint countOfRights);
    [DllImport("advapi32.dll")] private static extern uint LsaAddAccountRights(IntPtr policyHandle, byte[] accountSid, ref LsaUnicodeString userRights, uint countOfRights);
    [DllImport("advapi32.dll")] private static extern uint LsaRemoveAccountRights(IntPtr policyHandle, byte[] accountSid, [MarshalAs(UnmanagedType.U1)] bool allRights, IntPtr userRights, uint countOfRights);
    [DllImport("advapi32.dll")] private static extern uint LsaFreeMemory(IntPtr buffer);
    [DllImport("advapi32.dll")] private static extern uint LsaClose(IntPtr policyHandle);
    [DllImport("advapi32.dll")] private static extern int LsaNtStatusToWinError(uint status);

    private const uint PolicyAllAccess = 0x000F0FFF;
    private const uint StatusObjectNameNotFound = 0xC0000034;

    private static IntPtr Open()
    {
        LsaObjectAttributes attributes = new LsaObjectAttributes();
        attributes.Length = Marshal.SizeOf(typeof(LsaObjectAttributes));
        IntPtr handle;
        uint status = LsaOpenPolicy(IntPtr.Zero, ref attributes, PolicyAllAccess, out handle);
        if (status != 0) { throw new InvalidOperationException("lsa_open_failed:" + LsaNtStatusToWinError(status)); }
        return handle;
    }

    private static byte[] SidBytes(string sid)
    {
        SecurityIdentifier identifier = new SecurityIdentifier(sid);
        byte[] bytes = new byte[identifier.BinaryLength];
        identifier.GetBinaryForm(bytes, 0);
        return bytes;
    }

    // Returns null when the SID has no LSA account object.
    public static string[] GetRights(string sid)
    {
        IntPtr handle = Open();
        try
        {
            IntPtr rights;
            uint count;
            uint status = LsaEnumerateAccountRights(handle, SidBytes(sid), out rights, out count);
            if (status == StatusObjectNameNotFound) { return null; }
            if (status != 0) { throw new InvalidOperationException("lsa_enumerate_failed:" + LsaNtStatusToWinError(status)); }
            try
            {
                List<string> names = new List<string>();
                int size = Marshal.SizeOf(typeof(LsaUnicodeString));
                for (int index = 0; index < count; index++)
                {
                    LsaUnicodeString entry = (LsaUnicodeString)Marshal.PtrToStructure(new IntPtr(rights.ToInt64() + (long)index * size), typeof(LsaUnicodeString));
                    names.Add(Marshal.PtrToStringUni(entry.Buffer, entry.Length / 2));
                }
                return names.ToArray();
            }
            finally { LsaFreeMemory(rights); }
        }
        finally { LsaClose(handle); }
    }

    public static void AddAccountRight(string sid, string name)
    {
        if (String.IsNullOrWhiteSpace(name)) { throw new InvalidOperationException("lsa_right_name_invalid"); }
        IntPtr handle = Open();
        IntPtr buffer = IntPtr.Zero;
        try
        {
            buffer = Marshal.StringToHGlobalUni(name);
            LsaUnicodeString right = new LsaUnicodeString();
            right.Length = checked((ushort)(name.Length * 2));
            right.MaximumLength = checked((ushort)(right.Length + 2));
            right.Buffer = buffer;
            uint status = LsaAddAccountRights(handle, SidBytes(sid), ref right, 1);
            if (status != 0) { throw new InvalidOperationException("lsa_add_right_failed:" + LsaNtStatusToWinError(status)); }
        }
        finally
        {
            if (buffer != IntPtr.Zero) { Marshal.FreeHGlobal(buffer); }
            LsaClose(handle);
        }
    }

    public static void RemoveAllRights(string sid)
    {
        IntPtr handle = Open();
        try
        {
            uint status = LsaRemoveAccountRights(handle, SidBytes(sid), true, IntPtr.Zero, 0);
            if (status != 0 && status != StatusObjectNameNotFound) { throw new InvalidOperationException("lsa_remove_failed:" + LsaNtStatusToWinError(status)); }
        }
        finally { LsaClose(handle); }
    }
}
"@

function New-XbBoundaryHex {
    param([Parameter(Mandatory)][int]$Bytes)
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    return (($buffer | ForEach-Object { $_.ToString("x2") }) -join "")
}

function New-XbBoundaryPassword {
    $buffer = New-Object byte[] 24
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    return [Convert]::ToBase64String($buffer) + "aZ9!"
}

function Get-XbBoundaryHResult {
    param([Parameter(Mandatory)]$ErrorRecord)
    $exception = $ErrorRecord.Exception
    while ($null -ne $exception.InnerException) { $exception = $exception.InnerException }
    return [int]$exception.HResult
}

function Get-XbBoundaryOutcome {
    param([Parameter(Mandatory)][scriptblock]$Body)
    try { $null = & $Body; return "pass" } catch { return [string]$_.Exception.Message }
}

function Invoke-XbCi7FrozenControlChild {
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$ScriptText,
        [Parameter(Mandatory)]$Fixture,
        [int]$TimeoutMilliseconds = 60000
    )
    if ($null -eq ("XbCi7BoundedTextCapture" -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

public sealed class XbCi7BoundedTextCapture
{
    private readonly StringBuilder text = new StringBuilder();
    private readonly int limit;
    private readonly Task readerTask;
    private volatile bool overflow;
    private volatile bool readFailed;

    public XbCi7BoundedTextCapture(StreamReader reader, int maximumCharacters)
    {
        if (reader == null) throw new ArgumentNullException("reader");
        if (maximumCharacters < 1) throw new ArgumentOutOfRangeException("maximumCharacters");
        limit = maximumCharacters;
        readerTask = Task.Factory.StartNew(() => {
            char[] buffer = new char[1024];
            try {
                int count;
                while ((count = reader.Read(buffer, 0, buffer.Length)) != 0) {
                    lock (text) {
                        int remaining = limit - text.Length;
                        int keep = Math.Max(0, Math.Min(count, remaining));
                        if (keep > 0) text.Append(buffer, 0, keep);
                        if (keep < count) overflow = true;
                    }
                }
            } catch { readFailed = true; }
        }, CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
    }

    public bool Overflow { get { return overflow; } }
    public bool ReadFailed { get { return readFailed; } }
    public string Text { get { lock (text) return text.ToString(); } }
    public void Wait() { readerTask.Wait(); }
}
'@ -ErrorAction Stop | Out-Null
    }

    $result = [ordered]@{
        status = "launch_failed"
        exit_status = $null
        process_distinct = $false
        process_terminated = $false
        residue = "unproven"
        child_result = $null
        stderr_present = $false
    }
    $process = $null
    $processStarted = $false
    $childTempPath = $null
    $childTempCreated = $false
    $timeoutOccurred = $false
    try {
        if ($TimeoutMilliseconds -lt 1 -or $TimeoutMilliseconds -gt 120000 -or
            [string]::IsNullOrWhiteSpace($ScriptText) -or $ScriptText.Length -gt 32768) {
            throw "fixture_input_invalid"
        }
        $repositoryRootFull = [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\') + '\'
        $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        $childTempPath = Join-Path $tempRoot ("xb-ci7-frozen-control-" + [Guid]::NewGuid().ToString("N"))
        $childTempFull = [IO.Path]::GetFullPath($childTempPath).TrimEnd('\') + '\'
        if ($childTempFull.StartsWith($repositoryRootFull, [StringComparison]::OrdinalIgnoreCase)) {
            throw "fixture_temp_inside_repository"
        }
        New-Item -ItemType Directory -Path $childTempPath -ErrorAction Stop | Out-Null
        $childTempCreated = $true
        if (@(Get-ChildItem -LiteralPath $childTempPath -Force -ErrorAction Stop).Count -ne 0) {
            throw "fixture_temp_preimage_invalid"
        }

        $payload = [ordered]@{ script = $ScriptText; fixture = $Fixture } | ConvertTo-Json -Depth 8 -Compress
        if ([string]::IsNullOrEmpty($payload) -or $payload.Length -gt 262144) { throw "fixture_input_invalid" }
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $wireText = [Convert]::ToBase64String($utf8.GetBytes($payload))
        $wireBytes = [Text.Encoding]::ASCII.GetBytes($wireText + "`n")

        $bootstrap = '$wire=[Console]::In.ReadToEnd();$json=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($wire.Trim()));$packet=ConvertFrom-Json -InputObject $json -ErrorAction Stop;& ([ScriptBlock]::Create([string]$packet.script)) $packet.fixture'
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = Join-Path $PSHOME "powershell.exe"
        $startInfo.Arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + $bootstrap + '"'
        $systemRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
        if ([string]::IsNullOrWhiteSpace($systemRoot)) { throw "fixture_process_environment_invalid" }
        $startInfo.WorkingDirectory = $systemRoot
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardInput = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $environment = $startInfo.EnvironmentVariables
        $environment.Clear()
        $environment["SystemRoot"] = $systemRoot
        $environment["WINDIR"] = $systemRoot
        $environment["PATH"] = Join-Path $systemRoot "System32"
        $environment["TEMP"] = $childTempPath
        $environment["TMP"] = $childTempPath

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        $originalInputEncoding = [Console]::InputEncoding
        try {
            [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false, $true)
            if ([Console]::InputEncoding.GetPreamble().Length -ne 0) { throw "fixture_stdin_encoding_invalid" }
            $processStarted = $process.Start()
        } finally {
            [Console]::InputEncoding = $originalInputEncoding
        }
        if (-not $processStarted) { throw "fixture_child_start_failed" }
        $result.process_distinct = [bool]($process.Id -ne $PID)
        $stdoutCapture = [XbCi7BoundedTextCapture]::new($process.StandardOutput, 4096)
        $stderrCapture = [XbCi7BoundedTextCapture]::new($process.StandardError, 4096)
        $inputStream = $process.StandardInput.BaseStream
        $inputStream.Write($wireBytes, 0, $wireBytes.Length)
        $inputStream.Flush()
        $process.StandardInput.Close()

        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            $timeoutOccurred = $true
            try { $process.Kill() } catch { }
            $result.process_terminated = [bool]($process.WaitForExit(10000) -and $process.HasExited)
        } else {
            $process.WaitForExit()
            $result.process_terminated = [bool]$process.HasExited
        }
        if (-not $result.process_terminated) {
            $result.status = "termination_unproven"
        } else {
            $result.exit_status = [int]$process.ExitCode
            $stdoutCapture.Wait()
            $stderrCapture.Wait()
            $stdout = [string]$stdoutCapture.Text
            $stderr = [string]$stderrCapture.Text
            $result.stderr_present = [bool]($stderr.Length -gt 0)
            if ($timeoutOccurred) {
                $result.status = "timeout"
            } elseif ($stdoutCapture.Overflow -or $stderrCapture.Overflow -or
                $stdoutCapture.ReadFailed -or $stderrCapture.ReadFailed) {
                $result.status = "malformed_output"
            } else {
                $jsonLine = $stdout.TrimEnd("`r", "`n")
                if ([string]::IsNullOrWhiteSpace($jsonLine)) {
                    $result.status = if ($result.exit_status -ne 0) { "abnormal_exit" } else { "malformed_output" }
                } elseif ($jsonLine.Contains("`r") -or $jsonLine.Contains("`n")) {
                    $result.status = "malformed_output"
                } else {
                    try {
                        $child = ConvertFrom-Json -InputObject $jsonLine -ErrorAction Stop
                        $actualFields = @($child.PSObject.Properties | ForEach-Object Name | Sort-Object)
                        $expectedFields = @(
                            "credential_username_matches_fixture", "fixture_account_preserved", "fixture_status",
                            "outcome", "product_token_identity_matches_fixture", "product_token_open",
                            "schema_version", "setup_stage", "source_blob", "source_commit"
                        )
                        $booleansValid = @(
                            ($null -eq $child.fixture_account_preserved -or $child.fixture_account_preserved -is [bool]),
                            ($null -eq $child.credential_username_matches_fixture -or $child.credential_username_matches_fixture -is [bool]),
                            ($null -eq $child.product_token_identity_matches_fixture -or $child.product_token_identity_matches_fixture -is [bool])
                        ) -notcontains $false
                        $shapeValid = ($actualFields -join "|") -ceq ($expectedFields -join "|") -and
                            $child.schema_version -ceq "xb.member.worker.ci7.frozen-control.v1" -and
                            $child.fixture_status -cin @("completed", "failed") -and
                            $child.setup_stage -cin @(
                                "INPUT", "SOURCE_LOAD", "ACCOUNT_PRESERVATION", "NATIVE_INIT", "CREDENTIAL_RESTORE",
                                "CREDENTIAL_OBJECT_CREATE", "PRODUCT_TOKEN_OPEN", "FROZEN_CONTROL", "RESULT_EMIT", "CLEANUP"
                            ) -and
                            $child.outcome -cin @(
                                "effective_rights_exceeded", "effective_rights_missing", "effective_rights_unproven",
                                "installation_owned_surface_unknown", "installation_manifest_invalid",
                                "installation_manifest_membership_invalid", "installation_manifest_path_invalid",
                                "installation_manifest_task_invalid", "installation_runtime_roots_invalid",
                                "release_identity_mismatch", "unexpected_success", "unexpected_error", "fixture_setup_failed"
                            ) -and
                            $child.product_token_open -cin @("PASS", "FAIL", "NOT_REACHED") -and
                            [string]$child.source_commit -match '^[0-9a-f]{40}$' -and
                            [string]$child.source_blob -match '^[0-9a-f]{40}$' -and
                            $booleansValid
                        if (-not $shapeValid) {
                            $result.status = "malformed_output"
                        } elseif ($child.fixture_status -ceq "completed" -and
                            $child.outcome -cne "fixture_setup_failed" -and
                            $child.setup_stage -ceq "RESULT_EMIT" -and
                            $child.fixture_account_preserved -is [bool] -and $child.fixture_account_preserved -and
                            $child.credential_username_matches_fixture -is [bool] -and $child.credential_username_matches_fixture -and
                            $child.product_token_open -ceq "PASS" -and
                            $child.product_token_identity_matches_fixture -is [bool] -and $child.product_token_identity_matches_fixture -and
                            $result.exit_status -eq 0) {
                            $result.child_result = [ordered]@{
                                fixture_status = [string]$child.fixture_status
                                outcome = [string]$child.outcome
                                setup_stage = [string]$child.setup_stage
                                source_commit = [string]$child.source_commit
                                source_blob = [string]$child.source_blob
                                fixture_account_preserved = [bool]$child.fixture_account_preserved
                                credential_username_matches_fixture = [bool]$child.credential_username_matches_fixture
                                product_token_open = [string]$child.product_token_open
                                product_token_identity_matches_fixture = [bool]$child.product_token_identity_matches_fixture
                            }
                            $result.status = if ($result.stderr_present) { "malformed_output" } else { "completed" }
                        } elseif ($child.fixture_status -ceq "failed" -and
                            $child.outcome -ceq "fixture_setup_failed" -and
                            $child.product_token_open -cin @("FAIL", "NOT_REACHED") -and
                            $result.exit_status -ne 0) {
                            $result.child_result = [ordered]@{
                                fixture_status = [string]$child.fixture_status
                                outcome = [string]$child.outcome
                                setup_stage = [string]$child.setup_stage
                                source_commit = [string]$child.source_commit
                                source_blob = [string]$child.source_blob
                                fixture_account_preserved = $child.fixture_account_preserved
                                credential_username_matches_fixture = $child.credential_username_matches_fixture
                                product_token_open = [string]$child.product_token_open
                                product_token_identity_matches_fixture = $child.product_token_identity_matches_fixture
                            }
                            $result.status = "structured_failure"
                        } else {
                            $result.status = "malformed_output"
                        }
                    } catch { $result.status = "malformed_output" }
                }
            }
        }
    } catch {
        $result.status = "launch_failed"
    } finally {
        if ($null -ne $process) {
            try {
                if (-not $process.HasExited) {
                    try { $process.Kill() } catch { }
                    $result.process_terminated = [bool]($process.WaitForExit(10000) -and $process.HasExited)
                }
            } catch { $result.process_terminated = $false }
            try { $process.Dispose() } catch { }
        }
        if ($childTempCreated -and $null -ne $childTempPath -and
            (-not $processStarted -or $result.process_terminated)) {
            try {
                if (Test-Path -LiteralPath $childTempPath) {
                    Remove-Item -LiteralPath $childTempPath -Recurse -Force -ErrorAction Stop
                }
                $result.residue = if (Test-Path -LiteralPath $childTempPath) { "present" } else { "none" }
            } catch { $result.residue = "unproven" }
        } elseif ($childTempCreated -and $processStarted -and -not $result.process_terminated) {
            $result.residue = "unproven"
        } elseif (-not $childTempCreated) {
            $result.residue = "none"
        }
    }
    return [pscustomobject]$result
}

function Assert-XbCi7FrozenControlChildResult {
    param(
        [Parameter(Mandatory)]$RunResult,
        [Parameter(Mandatory)][string]$SourceCommit,
        [Parameter(Mandatory)][string]$SourceBlob
    )
    if ($RunResult.status -ceq "structured_failure") { throw "frozen_control_child_setup_failed" }
    if ($RunResult.status -cnotin @("completed", "structured_failure", "abnormal_exit", "malformed_output", "timeout", "termination_unproven", "launch_failed")) {
        throw "frozen_control_child_status_invalid"
    }
    if ($RunResult.status -cne "completed") { throw ("frozen_control_child_" + [string]$RunResult.status) }
    if (-not [bool]$RunResult.process_distinct) { throw "frozen_control_child_process_not_isolated" }
    if (-not [bool]$RunResult.process_terminated) { throw "frozen_control_child_termination_unproven" }
    if ([string]$RunResult.residue -cne "none") { throw "frozen_control_child_residue_unproven" }
    $child = $RunResult.child_result
    if ($null -eq $child -or $child.fixture_status -cne "completed") { throw "frozen_control_child_result_missing" }
    if ($child.source_commit -cne $SourceCommit -or $child.source_blob -cne $SourceBlob) {
        throw "frozen_control_child_source_binding_invalid"
    }
    if (-not $child.fixture_account_preserved -or -not $child.credential_username_matches_fixture) {
        throw "frozen_control_child_identity_invalid"
    }
    if ($child.product_token_open -cne "PASS" -or -not $child.product_token_identity_matches_fixture) {
        throw "frozen_control_child_product_token_invalid"
    }
    if ($child.outcome -cne "effective_rights_exceeded") { throw "frozen_control_child_outcome_mismatch" }
    return $true
}

function Invoke-XbBoundaryCase {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Body,
        [switch]$PreserveIncrementally
    )
    $record = [ordered]@{ status = "running" }
    $report.cases[$Name] = $record
    try {
        if ($PreserveIncrementally) {
            $null = & $Body $record
        } else {
            $data = @(& $Body)
            if ($data.Count -gt 0 -and $data[-1] -is [Collections.IDictionary]) { foreach ($key in @($data[-1].Keys)) { $record[$key] = $data[-1][$key] } }
        }
        $record.status = "completed"
    } catch {
        $record.status = "error"
        $record.error = [string]$_.Exception.Message
        $record.error_stack = [string]$_.ScriptStackTrace
    }
}

function Invoke-XbBoundaryCleanup {
    param(
        [Parameter(Mandatory)]$CaseRecord,
        [Parameter(Mandatory)][scriptblock]$Preflight,
        [Parameter(Mandatory)][scriptblock]$Body
    )
    $CaseRecord.cleanup = [ordered]@{
        attempted = $false
        pass = $null
        error = $null
        stack = $null
    }
    try {
        $null = & $Preflight $CaseRecord
        $CaseRecord.cleanup.attempted = $true
        $null = & $Body
        $CaseRecord.cleanup.pass = $true
    } catch {
        if ($CaseRecord.cleanup.attempted) { $CaseRecord.cleanup.pass = $false }
        $CaseRecord.cleanup.error = [string]$_.Exception.Message
        $CaseRecord.cleanup.stack = [string]$_.ScriptStackTrace
        throw
    }
}

function Get-XbStageNames {
    return @(Get-ChildItem -LiteralPath ([IO.Path]::GetTempPath()) -Directory -Force -Filter "xb-member-worker-*" | ForEach-Object Name)
}

function Get-XbAccountObservation {
    $user = Get-LocalUser -SID $state.user_sid
    $rights = [XbBoundaryLsa]::GetRights($state.user_sid)
    $profileKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\" + $state.user_sid
    return [ordered]@{
        last_logon = $(if ($null -eq $user.LastLogon) { "never" } else { ([datetime]$user.LastLogon).ToUniversalTime().ToString("o") })
        lsa_account_object = ($null -ne $rights)
        lsa_rights = @(if ($null -ne $rights) { $rights | Sort-Object })
        profile_list_present = (Test-Path -LiteralPath $profileKey)
        user_profile_count = @(Get-CimInstance -ClassName Win32_UserProfile -Filter ("SID='{0}'" -f $state.user_sid)).Count
        worker_process_count = @(Get-Process -IncludeUserName -ErrorAction SilentlyContinue | Where-Object { [string]$_.UserName -ieq $qualifiedAccount }).Count
    }
}

function Get-XbTaskEvidence {
    $cim = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop
    $view = Get-XbRegisteredWorkerTaskView
    $definition = $view.Definition
    $document = New-Object Xml.XmlDocument
    $document.LoadXml([string]$view.Xml)
    $namespaces = New-Object Xml.XmlNamespaceManager($document.NameTable)
    $namespaces.AddNamespace("t", "http://schemas.microsoft.com/windows/2004/02/mit/task")
    $info = Get-ScheduledTaskInfo -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop
    $cimActions = @(@($cim.Actions) | Where-Object { $null -ne $_ })
    $comAction = $definition.Actions.Item(1)
    return [ordered]@{
        cim_triggers_null = ($null -eq $cim.Triggers)
        historical_expression_count = @($cim.Triggers).Count
        cim_filtered_trigger_count = (Get-XbNonNullCount $cim.Triggers)
        com_trigger_count = [int]$definition.Triggers.Count
        xml_trigger_count = $document.SelectNodes("/t:Task/t:Triggers/*", $namespaces).Count
        cim_state = [string]$cim.State
        com_state = [int]$view.State
        com_enabled = [bool]$view.Enabled
        com_settings_enabled = [bool]$definition.Settings.Enabled
        cim_action_count = $cimActions.Count
        com_action_count = [int]$definition.Actions.Count
        com_action_type = [int]$comAction.Type
        action_execute = $(if ($cimActions.Count -gt 0) { [string]$cimActions[0].Execute } else { $null })
        action_arguments = $(if ($cimActions.Count -gt 0) { [string]$cimActions[0].Arguments } else { $null })
        com_action_path = [string]$comAction.Path
        com_action_arguments = [string]$comAction.Arguments
        principal_user_id = [string]$cim.Principal.UserId
        principal_logon_type = [string]$cim.Principal.LogonType
        principal_run_level = [string]$cim.Principal.RunLevel
        multiple_instances = [string]$cim.Settings.MultipleInstances
        execution_time_limit = [string]$cim.Settings.ExecutionTimeLimit
        restart_count = [int]$cim.Settings.RestartCount
        start_when_available = [bool]$cim.Settings.StartWhenAvailable
        last_task_result = [int64]$info.LastTaskResult
        last_run_time = $(if ($null -eq $info.LastRunTime) { $null } else { ([datetime]$info.LastRunTime).ToString("yyyy-MM-ddTHH:mm:ss") })
        missed_runs = [int]$info.NumberOfMissedRuns
        com_last_task_result = [int64]$view.LastTaskResult
        com_last_run_time = ([datetime]$view.LastRunTime).ToString("yyyy-MM-ddTHH:mm:ss")
        com_missed_runs = [int]$view.NumberOfMissedRuns
    }
}

function Get-XbProductionReadback {
    $stageNow = @(Get-XbStageNames)
    return [ordered]@{
        task_present = ($null -ne (Get-XbWorkerTaskIfPresent))
        scheduler_folder_present = (Test-XbTaskFolderPresent -Path $productionTaskPath -ErrorId "readback_folder_unproven")
        install_root_present = (Test-Path -LiteralPath $InstallRoot)
        runtime_root_present = (Test-Path -LiteralPath $RuntimeRoot)
        program_files_parent_present = (Test-Path -LiteralPath $programFilesParent)
        program_data_parent_present = (Test-Path -LiteralPath $programDataParent)
        new_stage_count = @($stageNow | Where-Object { $state.stage_before -notcontains $_ }).Count
    }
}

function Get-XbBoundaryComTaskFolderReadback {
    $requestedFolderPath = ConvertTo-XbComFolderPath -Path $taskPath
    $service = Connect-XbTaskService
    $observation = [ordered]@{ requested_folder_path = $requestedFolderPath }
    try { $folder = $service.GetFolder($requestedFolderPath) }
    catch {
        if ((Get-XbBoundaryHResult $_) -eq -2147024894) {
            $observation.folder_present = $false
            $observation.task_present = $false
            return $observation
        }
        throw "task_folder_presence_unproven"
    }
    $observation.folder_present = $true
    $observation.returned_folder_path = [string]$folder.Path
    if ($observation.returned_folder_path -cne $requestedFolderPath) { throw "task_folder_identity_invalid" }
    $registered = $null
    try { $registered = $folder.GetTask($taskName) }
    catch {
        if ((Get-XbBoundaryHResult $_) -eq -2147024894) {
            $observation.task_present = $false
        } else {
            throw "task_presence_unproven"
        }
    }
    if ($null -ne $registered) {
        $observation.task_present = $true
        $observation.task_path = [string]$registered.Path
        if ($observation.task_path -cne ($taskPath + $taskName)) { throw "task_identity_invalid" }
    }
    $observation.task_count = [int]$folder.GetTasks(1).Count
    $observation.child_folder_count = [int]$folder.GetFolders(0).Count
    return $observation
}

function Set-XbBoundaryTaskPresenceReadback {
    param(
        [Parameter(Mandatory)]$CaseRecord,
        [Parameter(Mandatory)][string]$Name
    )
    $observation = [ordered]@{
        effective_task_path = [string]$taskPath
        effective_task_name = [string]$taskName
    }
    $CaseRecord[$Name] = $observation
    $observation.cim_task_present = ($null -ne (Get-XbWorkerTaskIfPresent))
    $observation.com = Get-XbBoundaryComTaskFolderReadback
}

function Get-XbSyntheticConfigSnapshot {
    param([Parameter(Mandatory)][string]$Path)
    Initialize-XbWorkerNativeAccess
    $fullPath = [IO.Path]::GetFullPath($Path)
    $pathChain = Get-XbNativePathChain -Path $fullPath
    for ($index = 0; $index -lt $pathChain.Count; $index++) {
        $chainItem = Get-Item -LiteralPath $pathChain[$index] -Force -ErrorAction Stop
        $isLeaf = ($index -eq ($pathChain.Count - 1))
        if (($chainItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            ($isLeaf -and [bool]$chainItem.PSIsContainer) -or
            (-not $isLeaf -and -not [bool]$chainItem.PSIsContainer)) {
            throw "effective_rights_unproven"
        }
    }
    $protected = $null
    try {
        $protected = [XbWorkerProtectedObject]::Open($fullPath, $false, $true)
        if ($protected.IsDirectory -or $protected.IsReparsePoint) { throw "effective_rights_unproven" }
        $snapshot = $protected.ReadSnapshot()
        return [ordered]@{
            file_identity = [string]$protected.FileIdentity
            text = [string]$snapshot.Text
            path_chain_parent_count = [int]($pathChain.Count - 1)
            path_chain_ordinary_non_reparse = $true
        }
    } finally { if ($null -ne $protected) { $protected.Dispose() } }
}

function Get-XbRuntimeDirectoryEvidence {
    $expectedDirectories = @("config", "logs", "rollback", "secrets")
    $rootItem = Get-Item -LiteralPath $RuntimeRoot -Force -ErrorAction Stop
    $rootIsDirectory = [bool]$rootItem.PSIsContainer
    $rootReparsePoint = (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
    $evidence = [ordered]@{
        runtime_root_path = [IO.Path]::GetFullPath($RuntimeRoot)
        root_is_directory = $rootIsDirectory
        root_reparse_point = [bool]$rootReparsePoint
    }
    if (-not $rootIsDirectory -or $rootReparsePoint) {
        $evidence.prerequisite_satisfied = $false
        return $evidence
    }

    $rootEntries = @(Get-ChildItem -LiteralPath $RuntimeRoot -Force -ErrorAction Stop)
    $directories = @($rootEntries | Where-Object { $_.PSIsContainer })
    $files = @($rootEntries | Where-Object { -not $_.PSIsContainer })
    $directoryNames = @($directories | ForEach-Object Name | Sort-Object)
    $fileNames = @($files | ForEach-Object Name | Sort-Object)
    $directoryContents = [ordered]@{}
    $emptyExpectedDirectories = $true
    foreach ($name in $expectedDirectories) {
        $matches = @($directories | Where-Object { [string]$_.Name -ceq $name })
        if ($matches.Count -ne 1) {
            $directoryContents[$name] = [ordered]@{
                present = ($matches.Count -gt 0)
                match_count = $matches.Count
            }
            $emptyExpectedDirectories = $false
            continue
        }
        $directory = $matches[0]
        $reparsePoint = (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        if ($reparsePoint) {
            $directoryContents[$name] = [ordered]@{
                present = $true
                reparse_point = $true
            }
            $emptyExpectedDirectories = $false
            continue
        }
        $entries = @(Get-ChildItem -LiteralPath $directory.FullName -Force -ErrorAction Stop)
        $entryNames = @($entries | ForEach-Object Name | Sort-Object)
        $directoryContents[$name] = [ordered]@{
            present = $true
            reparse_point = $false
            entry_count = $entries.Count
            entry_names = $entryNames
        }
        if ($entries.Count -ne 0) { $emptyExpectedDirectories = $false }
    }
    $directorySetMatches = (@(Compare-Object -ReferenceObject $directoryNames -DifferenceObject $expectedDirectories -CaseSensitive).Count -eq 0)
    $evidence.root_directory_count = $directories.Count
    $evidence.root_file_count = $files.Count
    $evidence.root_directory_names = $directoryNames
    $evidence.root_file_names = $fileNames
    $evidence.directory_contents = $directoryContents
    $evidence.prerequisite_satisfied = [bool]($directorySetMatches -and $files.Count -eq 0 -and $emptyExpectedDirectories)
    return $evidence
}

function Get-XbRetainedParentEvidence {
    $parents = [ordered]@{}
    $safeToRemove = $true
    foreach ($spec in @(
        @("program_files_parent", $programFilesParent),
        @("program_data_parent", $programDataParent)
    )) {
        $name = [string]$spec[0]
        $path = [string]$spec[1]
        $item = $null
        try { $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop }
        catch [System.Management.Automation.ItemNotFoundException] {
            $parents[$name] = [ordered]@{ present = $false }
            continue
        } catch {
            throw "container_not_owned_empty"
        }
        $isDirectory = [bool]$item.PSIsContainer
        $reparsePoint = (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        $parentEvidence = [ordered]@{
            present = $true
            is_directory = $isDirectory
            reparse_point = [bool]$reparsePoint
        }
        if (-not $isDirectory -or $reparsePoint) {
            $parents[$name] = $parentEvidence
            $safeToRemove = $false
            continue
        }
        $children = @(Get-ChildItem -LiteralPath $path -Force -ErrorAction Stop)
        $parentEvidence.child_count = $children.Count
        $parentEvidence.child_names = @($children | ForEach-Object Name | Sort-Object)
        $parents[$name] = $parentEvidence
        if ($children.Count -ne 0) { $safeToRemove = $false }
    }
    return [ordered]@{
        safe_to_remove = [bool]$safeToRemove
        parents = $parents
    }
}

function Set-XbProductionTaskIdentity {
    $script:taskPath = $productionTaskPath
    $script:taskName = $productionTaskName
    $script:Operation = "Install"
    $script:ReviewedPackageManifestPath = $ReviewedManifestPath
}

# Disposable-runner cleanup of containers that were proven absent when the harness started.
function Clear-XbRetainedProductionContainers {
    Set-XbProductionTaskIdentity
    Remove-XbAttemptCreatedTaskFolder
    foreach ($parent in @($programFilesParent, $programDataParent)) {
        if (Test-Path -LiteralPath $parent) {
            Get-ChildItem -LiteralPath $parent -Force -File | Remove-Item -Force
            Remove-XbAttemptCreatedContainer -Path $parent
        }
    }
}

try {
    $report.environment.ps_version = $PSVersionTable.PSVersion.ToString()
    $report.environment.ps_edition = [string]$PSVersionTable.PSEdition
    if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) { throw "boundary_requires_windows_powershell_51" }
    $currentPrincipal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    $report.environment.elevated = $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $report.environment.elevated) { throw "boundary_requires_elevation" }
    Import-Module ScheduledTasks -ErrorAction Stop
    $report.environment.scheduled_tasks_module = $true
    Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction Stop
    $probeService = New-Object -ComObject Schedule.Service
    $probeService.Connect()
    $report.environment.schedule_service = [bool]$probeService.Connected
    if (-not $report.environment.schedule_service) { throw "boundary_requires_schedule_service" }
    Add-Type -TypeDefinition $lsaSource -Language CSharp
    $state.lsa_loaded = $true

    . $InstallerPath -LibraryOnly
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

public static class XbCi7FailureCleanupProbe
{
    private const int ErrorMoreData = 234;
    private const uint MaximumOwnerCount = 32;
    private const uint DeleteAccess = 0x00010000;
    private const uint OpenExisting = 3;
    private const uint FileAttributeNormal = 0x00000080;

    [StructLayout(LayoutKind.Sequential)]
    private struct RmFileTime { public uint LowDateTime; public uint HighDateTime; }
    [StructLayout(LayoutKind.Sequential)]
    private struct RmUniqueProcess { public int ProcessId; public RmFileTime ProcessStartTime; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct RmProcessInfo
    {
        public RmUniqueProcess Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string ApplicationName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string ServiceShortName;
        public uint ApplicationType;
        public uint ApplicationStatus;
        public uint SessionId;
        [MarshalAs(UnmanagedType.Bool)] public bool Restartable;
    }

    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(
        string fileName, uint desiredAccess, uint shareMode, IntPtr securityAttributes,
        uint creationDisposition, uint flagsAndAttributes, IntPtr templateFile);
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmStartSession(out uint sessionHandle, uint sessionFlags, StringBuilder sessionKey);
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmRegisterResources(
        uint sessionHandle, uint fileCount,
        [MarshalAs(UnmanagedType.LPArray, ArraySubType = UnmanagedType.LPWStr)] string[] fileNames,
        uint applicationCount, IntPtr applications, uint serviceCount, IntPtr services);
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmGetList(
        uint sessionHandle, out uint processInfoNeeded, ref uint processInfoCount,
        [In, Out, MarshalAs(UnmanagedType.LPArray)] RmProcessInfo[] affectedApplications,
        out uint rebootReasons);
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmEndSession(uint sessionHandle);

    public static int GetOwnerCount(string path, int currentProcessId, out bool completed, out bool currentProcessOwns)
    {
        completed = false;
        currentProcessOwns = false;
        uint sessionHandle;
        StringBuilder sessionKey = new StringBuilder(33);
        if (RmStartSession(out sessionHandle, 0, sessionKey) != 0) return 0;
        try
        {
            if (RmRegisterResources(sessionHandle, 1, new string[] { path }, 0, IntPtr.Zero, 0, IntPtr.Zero) != 0) return 0;
            uint needed;
            uint count = 0;
            uint rebootReasons;
            int status = RmGetList(sessionHandle, out needed, ref count, null, out rebootReasons);
            if (status == 0)
            {
                completed = true;
                return 0;
            }
            if (status != ErrorMoreData || needed > MaximumOwnerCount) return 0;
            RmProcessInfo[] processes = new RmProcessInfo[(int)needed];
            count = needed;
            status = RmGetList(sessionHandle, out needed, ref count, processes, out rebootReasons);
            if (status != 0 || count > MaximumOwnerCount) return 0;
            for (int index = 0; index < (int)count; index++)
            {
                if (processes[index].Process.ProcessId == currentProcessId) currentProcessOwns = true;
            }
            completed = true;
            return (int)count;
        }
        finally { RmEndSession(sessionHandle); }
    }

    public static bool ProbeExclusiveDelete(string path, out bool completed)
    {
        completed = false;
        using (SafeFileHandle handle = CreateFile(
            path, DeleteAccess, 0, IntPtr.Zero, OpenExisting, FileAttributeNormal, IntPtr.Zero))
        {
            completed = true;
            return handle != null && !handle.IsInvalid;
        }
    }
}
'@ -Language CSharp
    $productionTaskPath = $taskPath
    $productionTaskName = $taskName
    $programFilesParent = Split-Path -Parent $InstallRoot
    $programDataParent = Split-Path -Parent $RuntimeRoot
    $report.environment.pristine_before = [ordered]@{
        program_files_parent = (Test-Path -LiteralPath $programFilesParent)
        program_data_parent = (Test-Path -LiteralPath $programDataParent)
        scheduler_folder = (Test-XbTaskFolderPresent -Path $productionTaskPath -ErrorId "pristine_folder_unproven")
    }
    if (@($report.environment.pristine_before.Values | Where-Object { $_ }).Count -ne 0) { throw "hosted_runner_not_pristine" }
    $state.pristine_proven = $true

    $report.environment.temp_drive = [IO.Path]::GetPathRoot([IO.Path]::GetTempPath())
    if ($report.environment.temp_drive -ne [IO.Path]::GetPathRoot($InstallRoot)) {
        $state.temp_redirect = "C:\xb-boundary-tmp-" + (New-XbBoundaryHex 6)
        New-Item -ItemType Directory -Path $state.temp_redirect | Out-Null
        $env:TMP = $state.temp_redirect
        $env:TEMP = $state.temp_redirect
    }
    $report.environment.temp_redirected = ($null -ne $state.temp_redirect)
    $state.stage_before = @(Get-XbStageNames)

    # Ephemeral Users-only account; the CSPRNG passwords exist only in this process and are never emitted.
    $userName = "xbt" + (New-XbBoundaryHex 6)
    $plainPassword = New-XbBoundaryPassword
    $plainWrong = New-XbBoundaryPassword
    $secretValues.Add($plainPassword)
    $secretValues.Add($plainWrong)
    $securePassword = ConvertTo-SecureString -String $plainPassword -AsPlainText -Force
    $secureWrong = ConvertTo-SecureString -String $plainWrong -AsPlainText -Force
    $plainPassword = $null
    $plainWrong = $null
    New-LocalUser -Name $userName -Password $securePassword -PasswordNeverExpires -AccountNeverExpires -UserMayNotChangePassword -Description "XB disposable task boundary test" | Out-Null
    $state.user_sid = (Get-LocalUser -Name $userName).SID.Value
    try { Add-LocalGroupMember -SID "S-1-5-32-545" -Member $userName -ErrorAction Stop } catch [Microsoft.PowerShell.Commands.MemberExistsException] { }
    # Production-faithful WorkerAccount is the bare local account name, matching accepted xb-ac2-worker input;
    # it drives the credential, principal and registration. The machine-qualified form is used only for OS
    # observations that report COMPUTER\user, such as process-owner comparison.
    $workerAccount = $userName
    $qualifiedAccount = "{0}\{1}" -f $env:COMPUTERNAME, $userName
    $credential = New-Object Management.Automation.PSCredential($workerAccount, $securePassword)
    $wrongCredential = New-Object Management.Automation.PSCredential($workerAccount, $secureWrong)
    $script:WorkerAccount = $workerAccount
    if ($null -ne [XbBoundaryLsa]::GetRights($state.user_sid)) { throw "boundary_user_right_preimage_not_clean" }
    [XbBoundaryLsa]::AddAccountRight($state.user_sid, "SeBatchLogonRight")
    $workerRights = @([XbBoundaryLsa]::GetRights($state.user_sid))
    if ($workerRights.Count -ne 1 -or $workerRights[0] -cne "SeBatchLogonRight") { throw "boundary_batch_logon_right_not_assigned" }
    $report.account.batch_logon_right_assigned = $true
    $report.environment.worker_account = $workerAccount
    $report.environment.worker_sid = $state.user_sid
    try {
        $report.environment.administrators_member = (@(Get-LocalGroupMember -SID "S-1-5-32-544" -ErrorAction Stop | Where-Object { $_.SID.Value -eq $state.user_sid }).Count -ne 0)
    } catch {
        $adminNames = @(([ADSI]"WinNT://./Administrators,group").psbase.Invoke("Members") | ForEach-Object { $_.GetType().InvokeMember("Name", "GetProperty", $null, $_, $null) })
        $report.environment.administrators_member = ($adminNames -contains $userName)
    }
    $report.account.before_registration = Get-XbAccountObservation

    $boundaryHex = New-XbBoundaryHex 6
    $state.boundary_folder = "\XB-Boundary-$boundaryHex\"
    $candidateName = "XB Candidate $boundaryHex"
    $controlName = "XB Control $boundaryHex"
    $state.boundary_tasks = @($candidateName, $controlName)
    $fakeLauncher = "C:\xb-boundary-launcher-$boundaryHex\launch_ac2_member_gateway_worker.ps1"

    Invoke-XbBoundaryCase "in_memory_null_trigger_pin" {
        $action = New-ScheduledTaskAction -Execute $script:XbWindowsPowerShellPath -Argument ('-NoLogo -NoProfile -NonInteractive -File "{0}" -Mode DisabledProof' -f $fakeLauncher)
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) -RestartCount 0 -StartWhenAvailable:$false -Disable
        $taskPrincipal = New-ScheduledTaskPrincipal -UserId $workerAccount -LogonType Password -RunLevel Limited
        $memory = New-ScheduledTask -Action $action -Settings $settings -Principal $taskPrincipal
        return [ordered]@{
            triggers_null = ($null -eq $memory.Triggers)
            historical_expression_count = @($memory.Triggers).Count
            filtered_trigger_count = (Get-XbNonNullCount $memory.Triggers)
        }
    }

    Invoke-XbBoundaryCase "zero_trigger_candidate" {
        $script:taskPath = $state.boundary_folder
        $script:taskName = $candidateName
        $script:TaskCredential = $credential
        $registration = Get-XbBoundaryOutcome { Register-XbWorkerScheduledTask -LauncherPath $fakeLauncher }
        $cim = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop
        $expected = Get-XbWorkerTaskIdentity -LauncherPath $fakeLauncher -WorkerAccount $workerAccount
        $contract = Get-XbBoundaryOutcome { Assert-XbWorkerTaskContract -Task $cim -ExpectedIdentity $expected }
        $view = Get-XbRegisteredWorkerTaskView
        $oracle = try { [string](Get-XbTaskTriggerOracleCount -ComTriggerCount ([int]$view.Definition.Triggers.Count) -TaskXml ([string]$view.Xml) -CimTriggers $cim.Triggers) } catch { [string]$_.Exception.Message }
        return [ordered]@{
            registration = $registration
            contract = $contract
            oracle_count = $oracle
            evidence = (Get-XbTaskEvidence)
            account_after_registration = (Get-XbAccountObservation)
        }
    }

    Invoke-XbBoundaryCase "one_trigger_control" {
        $script:taskPath = $state.boundary_folder
        $script:taskName = $controlName
        $action = New-ScheduledTaskAction -Execute $script:XbWindowsPowerShellPath -Argument ('-NoLogo -NoProfile -NonInteractive -File "{0}" -Mode DisabledProof' -f $fakeLauncher)
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) -RestartCount 0 -StartWhenAvailable:$false -Disable
        $taskPrincipal = New-ScheduledTaskPrincipal -UserId $workerAccount -LogonType Password -RunLevel Limited
        $trigger = New-ScheduledTaskTrigger -Once -At ([datetime]::new(2099, 1, 1, 0, 0, 0))
        $controlDefinition = New-ScheduledTask -Action $action -Settings $settings -Principal $taskPrincipal -Trigger $trigger
        $plain = $credential.GetNetworkCredential().Password
        try { Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -InputObject $controlDefinition -User $workerAccount -Password $plain -Force | Out-Null }
        finally { $plain = $null }
        $cim = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop
        $expected = Get-XbWorkerTaskIdentity -LauncherPath $fakeLauncher -WorkerAccount $workerAccount
        $contract = Get-XbBoundaryOutcome { Assert-XbWorkerTaskContract -Task $cim -ExpectedIdentity $expected }
        return [ordered]@{
            in_memory_filtered_trigger_count = (Get-XbNonNullCount $controlDefinition.Triggers)
            contract = $contract
            evidence = (Get-XbTaskEvidence)
        }
    }

    Invoke-XbBoundaryCase "boundary_unregister" {
        $final = [ordered]@{}
        foreach ($name in @($candidateName, $controlName)) {
            $script:taskPath = $state.boundary_folder
            $script:taskName = $name
            $final[$name] = [int64](Get-ScheduledTaskInfo -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop).LastTaskResult
            Remove-XbWorkerScheduledTask
        }
        Remove-XbAttemptCreatedTaskFolder
        return [ordered]@{
            final_last_task_results = @($final.Values)
            folder_present_after = (Test-XbTaskFolderPresent -Path $state.boundary_folder -ErrorId "boundary_folder_unproven")
            account_after_unregister = (Get-XbAccountObservation)
        }
    }

    # Observe rollback internals without changing them: record task/folder presence, then call the real step.
    $originalTaskStep = ${function:Remove-XbAttemptRegisteredTask}
    $originalFolderStep = ${function:Remove-XbAttemptCreatedTaskFolder}
    Set-Item function:script:Remove-XbAttemptRegisteredTask {
        param([Parameter(Mandatory)][string]$LauncherPath)
        $trace.Add("task_step:" + $(if ($null -ne (Get-XbWorkerTaskIfPresent)) { "present" } else { "absent" }))
        & $originalTaskStep -LauncherPath $LauncherPath
    }
    Set-Item function:script:Remove-XbAttemptCreatedTaskFolder {
        $trace.Add("folder_step:" + $(if (Test-XbTaskFolderPresent -Path $taskPath -ErrorId "trace_folder_unproven") { "present" } else { "absent" }))
        & $originalFolderStep
    }
    $originalAssert = ${function:Assert-XbWorkerTaskContract}
    $originalRegister = ${function:Register-XbWorkerScheduledTask}

    Invoke-XbBoundaryCase "registration_failure_parent_absent" {
        Set-XbProductionTaskIdentity
        $script:TaskCredential = $wrongCredential
        $trace.Clear()
        $outcome = Get-XbBoundaryOutcome { Invoke-XbWorkerInstaller }
        return [ordered]@{
            install_outcome = $outcome
            rollback_trace = @($trace)
            readback = (Get-XbProductionReadback)
            historical_unconditional_unregister = (Get-XbBoundaryOutcome { Remove-XbWorkerScheduledTask })
        }
    }

    Invoke-XbBoundaryCase "forced_post_registration_parent_absent" {
        Set-XbProductionTaskIdentity
        $script:TaskCredential = $credential
        $trace.Clear()
        Set-Item function:script:Assert-XbWorkerTaskContract { param($Task, $ExpectedIdentity) throw "forced_post_registration_failure" }
        try { $outcome = Get-XbBoundaryOutcome { Invoke-XbWorkerInstaller } }
        finally { Set-Item function:script:Assert-XbWorkerTaskContract $originalAssert }
        return [ordered]@{ install_outcome = $outcome; rollback_trace = @($trace); readback = (Get-XbProductionReadback) }
    }

    Invoke-XbBoundaryCase "preexisting_parents_sentinel" {
        Set-XbProductionTaskIdentity
        $script:TaskCredential = $credential
        $sentinelValue = New-XbBoundaryHex 16
        foreach ($parent in @($programFilesParent, $programDataParent)) {
            New-Item -ItemType Directory -Path $parent | Out-Null
            Set-Content -LiteralPath (Join-Path $parent "xb-boundary-sentinel.txt") -Value $sentinelValue -NoNewline
        }
        $service = New-Object -ComObject Schedule.Service
        $service.Connect()
        $null = $service.GetFolder("\").CreateFolder($productionTaskPath.Trim('\'))
        $trace.Clear()
        Set-Item function:script:Assert-XbWorkerTaskContract { param($Task, $ExpectedIdentity) throw "forced_post_registration_failure" }
        try { $outcome = Get-XbBoundaryOutcome { Invoke-XbWorkerInstaller } }
        finally { Set-Item function:script:Assert-XbWorkerTaskContract $originalAssert }
        $rollbackTrace = @($trace)
        $readback = Get-XbProductionReadback
        $sentinels = [ordered]@{}
        foreach ($parent in @($programFilesParent, $programDataParent)) {
            $sentinelPath = Join-Path $parent "xb-boundary-sentinel.txt"
            $sentinels[$parent] = [ordered]@{
                children = @(Get-ChildItem -LiteralPath $parent -Force | ForEach-Object Name | Sort-Object)
                sentinel_intact = ((Test-Path -LiteralPath $sentinelPath) -and ((Get-Content -Raw -LiteralPath $sentinelPath) -ceq $sentinelValue))
            }
        }
        $folder = $service.GetFolder($productionTaskPath.TrimEnd('\'))
        $folderContent = [ordered]@{ tasks = [int]$folder.GetTasks(1).Count; folders = [int]$folder.GetFolders(0).Count }
        Clear-XbRetainedProductionContainers
        return [ordered]@{
            install_outcome = $outcome
            rollback_trace = $rollbackTrace
            readback = $readback
            parents = @($sentinels.Values)
            scheduler_folder_content = $folderContent
            post_cleanup = (Get-XbProductionReadback)
        }
    }

    Invoke-XbBoundaryCase "unexpected_container_content_hold" {
        Set-XbProductionTaskIdentity
        $script:TaskCredential = $credential
        $trace.Clear()
        $unexpectedPath = Join-Path $programDataParent "xb-boundary-unexpected.txt"
        Set-Item function:script:Register-XbWorkerScheduledTask {
            param([Parameter(Mandatory)][string]$LauncherPath)
            Set-Content -LiteralPath (Join-Path (Split-Path -Parent $RuntimeRoot) "xb-boundary-unexpected.txt") -Value "unexpected"
            throw "forced_pre_registration_failure"
        }
        try { $outcome = Get-XbBoundaryOutcome { Invoke-XbWorkerInstaller } }
        finally { Set-Item function:script:Register-XbWorkerScheduledTask $originalRegister }
        $rollbackTrace = @($trace)
        $readback = Get-XbProductionReadback
        $unexpectedPreserved = Test-Path -LiteralPath $unexpectedPath
        Clear-XbRetainedProductionContainers
        return [ordered]@{
            install_outcome = $outcome
            rollback_trace = $rollbackTrace
            readback = $readback
            unexpected_preserved = $unexpectedPreserved
            post_cleanup = (Get-XbProductionReadback)
        }
    }

    function Invoke-XbCi7AclProbe {
        param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][Security.AccessControl.FileSystemAccessRule]$Rule, [Parameter(Mandatory)]$Token, [Parameter(Mandatory)][uint32]$DesiredAccess, [Parameter(Mandatory)][Management.Automation.PSCredential]$Credential)
        $original = Get-Acl -LiteralPath $Path -ErrorAction Stop
        try {
            $changed = Get-Acl -LiteralPath $Path -ErrorAction Stop
            $changed.AddAccessRule($Rule)
            Set-Acl -LiteralPath $Path -AclObject $changed -ErrorAction Stop
            $granted = Test-XbNativeAccessAllowed -Path $Path -Token $Token -DesiredAccess $DesiredAccess
            $outcome = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $Credential }
            return [ordered]@{ requested_operation_granted = [bool]$granted; effective_rights = $outcome }
        } finally { Set-Acl -LiteralPath $Path -AclObject $original -ErrorAction Stop }
    }

    function Invoke-XbCi7AccessResultFixture {
        param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][uint32]$DesiredAccess, [Parameter(Mandatory)]$Token, [Parameter(Mandatory)][scriptblock]$Body)
        $subject = [XbWorkerProtectedObject]::Open([IO.Path]::GetFullPath($Path), $true, $false)
        $original = ${function:script:Invoke-XbCi7HandleAccessCheck}
        $script:XbCi7FixtureOriginalHelper = $original
        $script:XbCi7FixtureIdentity = [string]$subject.FileIdentity
        $script:XbCi7FixtureMask = [uint32]$DesiredAccess
        $script:XbCi7FixtureCalls = 0
        try {
            Set-Item function:script:Invoke-XbCi7HandleAccessCheck {
                param($Object, $Token, [uint32]$DesiredAccess)
                if ([string]$Object.FileIdentity -ceq [string]$script:XbCi7FixtureIdentity -and
                    [uint32]$DesiredAccess -eq [uint32]$script:XbCi7FixtureMask) {
                    $script:XbCi7FixtureCalls++
                    return [XbWorkerAccessResult]::new($true, [uint32]$DesiredAccess)
                }
                return & $script:XbCi7FixtureOriginalHelper -Object $Object -Token $Token -DesiredAccess $DesiredAccess
            }
            $fixtureResult = Invoke-XbCi7HandleAccessCheck -Object $subject -Token $Token -DesiredAccess $DesiredAccess
            $script:XbCi7FixtureCalls = 0
            $result = $null
            try {
                $result = & $Body
                $outcome = "pass"
            } catch {
                $outcome = [string]$_.Exception.Message
            } finally {
                if ($null -ne $result -and $null -ne $result.PSObject.Properties["Objects"]) {
                    Dispose-XbCi7VerificationContext -Context $result
                }
            }
            return [ordered]@{
                outcome = $outcome
                fixture_allowed = [bool]$fixtureResult.Allowed
                fixture_granted_access = [uint32]$fixtureResult.GrantedAccess
                policy_consulted_fixture = [bool]($script:XbCi7FixtureCalls -gt 0)
            }
        } finally {
            Set-Item function:script:Invoke-XbCi7HandleAccessCheck $original
            $subject.Dispose()
            Remove-Variable -Scope Script -Name XbCi7FixtureOriginalHelper, XbCi7FixtureIdentity, XbCi7FixtureMask, XbCi7FixtureCalls -ErrorAction SilentlyContinue
        }
    }

    function Invoke-XbCi7OwnerProbe {
        param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][Management.Automation.PSCredential]$Credential, [Parameter(Mandatory)][string]$WrongOwnerSid)
        $original = Get-Acl -LiteralPath $Path -ErrorAction Stop
        try {
            $changed = Get-Acl -LiteralPath $Path -ErrorAction Stop
            $changed.SetOwner([Security.Principal.SecurityIdentifier]::new($WrongOwnerSid))
            Set-Acl -LiteralPath $Path -AclObject $changed -ErrorAction Stop
            return (Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $Credential })
        } finally { Set-Acl -LiteralPath $Path -AclObject $original -ErrorAction Stop }
    }

    function Invoke-XbCi7DenyProbe {
        param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][Security.AccessControl.FileSystemAccessRule]$Rule, [Parameter(Mandatory)]$Token, [Parameter(Mandatory)][uint32]$DesiredAccess, [Parameter(Mandatory)][Management.Automation.PSCredential]$Credential)
        $original = Get-Acl -LiteralPath $Path -ErrorAction Stop
        try {
            $changed = Get-Acl -LiteralPath $Path -ErrorAction Stop
            $changed.AddAccessRule($Rule)
            Set-Acl -LiteralPath $Path -AclObject $changed -ErrorAction Stop
            $denied = -not (Test-XbNativeAccessAllowed -Path $Path -Token $Token -DesiredAccess $DesiredAccess)
            $outcome = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $Credential }
            return [ordered]@{ requested_operation_denied = [bool]$denied; effective_rights = $outcome }
        } finally { Set-Acl -LiteralPath $Path -AclObject $original -ErrorAction Stop }
    }

    Invoke-XbBoundaryCase -Name "install_then_uninstall" -PreserveIncrementally -Body {
        param($case)
        $ci7 = [ordered]@{
            fatal = $null
            required_probe_completion = [ordered]@{ status = "in_progress" }
        }
        $case.ci7 = $ci7
        $case.synthetic_config_snapshot = [ordered]@{}
        Set-XbProductionTaskIdentity
        $script:TaskCredential = $credential
        $trace.Clear()
        $case.install_outcome = Get-XbBoundaryOutcome { Invoke-XbWorkerInstaller }
        $case.installed = Get-XbProductionReadback
        Set-XbBoundaryTaskPresenceReadback -CaseRecord $case -Name "install_task_presence"
        $case.contract = Get-XbBoundaryOutcome { Assert-XbWorkerTaskContract -Task (Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop) }
        $case.ownership = Get-XbBoundaryOutcome { Assert-XbUninstallOwnership }
        $case.evidence = Get-XbTaskEvidence
        $manifest = Get-Content -Raw -LiteralPath (Join-Path $InstallRoot "installation-manifest.json") | ConvertFrom-Json
        $case.manifest_trigger_count = [int]$manifest.task.trigger_count
        $configFile = Join-Path (Join-Path $RuntimeRoot "config") "worker.config.json"
        [IO.File]::WriteAllText($configFile, "{}", [Text.UTF8Encoding]::new($false))
        Set-XbWorkerTrustedOwner -Path $configFile -OwnerSid "S-1-5-18"
        $nativeTypeBeforeSnapshot = "XbWorkerProtectedObject" -as [type]
        $case.synthetic_config_snapshot.native_type_absent_before_first_use = ($null -eq $nativeTypeBeforeSnapshot)
        if ($null -ne $nativeTypeBeforeSnapshot) { throw "synthetic_config_native_type_preloaded" }
        $syntheticConfigSnapshot = Get-XbSyntheticConfigSnapshot -Path $configFile
        $case.synthetic_config_snapshot.native_type_initialized = ($null -ne ("XbWorkerProtectedObject" -as [type]))
        if ($syntheticConfigSnapshot.text -cne "{}") { throw "synthetic_config_fixture_content_unexpected" }
        $configBytes = [IO.File]::ReadAllBytes($configFile)
        $case.synthetic_config_snapshot.fixture_bytes_hex = [BitConverter]::ToString($configBytes).Replace("-", "").ToLowerInvariant()
        if ($configBytes.Length -ne 2 -or $configBytes[0] -ne 0x7b -or $configBytes[1] -ne 0x7d) {
            throw "synthetic_config_fixture_bytes_unexpected"
        }
        $syntheticConfigIdentity = [string]$syntheticConfigSnapshot.file_identity
        $snapshotProbePath = Join-Path (Split-Path -Parent $configFile) ".snapshot-probe.json"
        $snapshotProbeBackupPath = $snapshotProbePath + ".original"
        if ((Test-Path -LiteralPath $snapshotProbePath) -or (Test-Path -LiteralPath $snapshotProbeBackupPath)) {
            throw "synthetic_config_snapshot_probe_preimage_exists"
        }
        try {
            [IO.File]::WriteAllText($snapshotProbePath, '{"wrong":true}', [Text.UTF8Encoding]::new($false))
            Set-XbWorkerTrustedOwner -Path $snapshotProbePath -OwnerSid "S-1-5-18"
            $wrongContentSnapshot = Get-XbSyntheticConfigSnapshot -Path $snapshotProbePath
            $case.synthetic_config_snapshot.wrong_content_exact = [string]::Equals([string]$wrongContentSnapshot.text, '{"wrong":true}', [StringComparison]::Ordinal)
            if (-not $case.synthetic_config_snapshot.wrong_content_exact) { throw "synthetic_config_wrong_content_not_exact" }
            $wrongContentIdentity = [string]$wrongContentSnapshot.file_identity
            Move-Item -LiteralPath $snapshotProbePath -Destination $snapshotProbeBackupPath -ErrorAction Stop
            [IO.File]::WriteAllText($snapshotProbePath, "{}", [Text.UTF8Encoding]::new($false))
            Set-XbWorkerTrustedOwner -Path $snapshotProbePath -OwnerSid "S-1-5-18"
            $replacementSnapshot = Get-XbSyntheticConfigSnapshot -Path $snapshotProbePath
            $case.synthetic_config_snapshot.replacement_identity_changed = ([string]$replacementSnapshot.file_identity -cne $wrongContentIdentity)
            if (-not $case.synthetic_config_snapshot.replacement_identity_changed) { throw "synthetic_config_replacement_identity_not_changed" }
        } finally {
            if (Test-Path -LiteralPath $snapshotProbePath) { Remove-Item -LiteralPath $snapshotProbePath -Force -ErrorAction Stop }
            if (Test-Path -LiteralPath $snapshotProbeBackupPath) { Remove-Item -LiteralPath $snapshotProbeBackupPath -Force -ErrorAction Stop }
        }
        $case.account_installed = Get-XbAccountObservation
        $nativeToken = $null
        $logPath = $null
        $junctionPath = $null
        try {
            $logsPath = Join-Path $RuntimeRoot "logs"
            $runtimeRootAcl = Get-Acl -LiteralPath $RuntimeRoot
            $logsRootAcl = Get-Acl -LiteralPath $logsPath
            $workerSid = Get-XbAccountSid -Account $WorkerAccount
            $workerSidIdentity = [Security.Principal.SecurityIdentifier]::new($workerSid)
            $ci7.root_owner_sid = $logsRootAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value
            $ci7.root_owner_accepted = ($ci7.root_owner_sid -in @("S-1-5-18", "S-1-5-32-544"))
            $ci7.root_dacl_protected = [bool]$logsRootAcl.AreAccessRulesProtected
            $ci7.root_acl_shape = Get-XbBoundaryOutcome { Assert-XbLogsRootAclShape -Path $logsPath -WorkerSid $workerSid }
            $nativeToken = New-XbWorkerBatchToken -Credential $credential
            $ci7.token_user_sid = [string]$nativeToken.UserSid
            $ci7.token_type = [int]$nativeToken.TokenType
            $ci7.token_impersonation_level = [int]$nativeToken.ImpersonationLevel
            $rootMaximum = Invoke-XbNativeAccessCheck -Path $logsPath -Token $nativeToken -DesiredAccess ([uint32]0x02000000)
            $ci7.logs_root_granted_mask = "0x{0:X8}" -f [uint32]$rootMaximum.GrantedAccess
            $ci7.root_write_dac = Test-XbNativeAccessAllowed -Path $logsPath -Token $nativeToken -DesiredAccess ([uint32]0x00040000)
            $ci7.root_write_owner = Test-XbNativeAccessAllowed -Path $logsPath -Token $nativeToken -DesiredAccess ([uint32]0x00080000)
            $ci7.root_delete = Test-XbNativeAccessAllowed -Path $logsPath -Token $nativeToken -DesiredAccess ([uint32]0x00010000)
            $ci7.runtime_root_delete_child = Test-XbNativeAccessAllowed -Path $RuntimeRoot -Token $nativeToken -DesiredAccess ([uint32]0x00000040)
            $ci7.parent_delete_composition = Get-XbBoundaryOutcome { Assert-XbPathNotDeleteable -Path $logsPath -Token $nativeToken }

            $logPath = Join-Path $logsPath ("launcher-ci7-{0}.jsonl" -f (New-XbBoundaryHex 8))
            $nativeToken.Impersonate()
            try {
                Set-Content -LiteralPath $logPath -Value "ci7-create" -NoNewline
                Add-Content -LiteralPath $logPath -Value "|ci7-append" -NoNewline
                $ci7.ps51_enumerated = (@(Get-ChildItem -LiteralPath $logsPath -File -Force | ForEach-Object FullName) -contains $logPath)
                $ci7.ps51_content = [IO.File]::ReadAllText($logPath)
            } finally { [XbWorkerBatchToken]::Revert() }
            $childAcl = Get-Acl -LiteralPath $logPath
            $ci7.child_owner_sid = $childAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value
            $ci7.child_owner_is_worker = ($ci7.child_owner_sid -ceq $workerSid)
            $ci7.child_owner_rights_shape = Get-XbBoundaryOutcome { Assert-XbOwnerRightsLogFile -Path $logPath -WorkerSid $workerSid }
            $ownerRules = @($childAcl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object { $_.IdentityReference.Value -ceq "S-1-3-4" })
            $ci7.child_owner_rights_ace_count = $ownerRules.Count
            $ci7.child_owner_rights_ace_mask = if ($ownerRules.Count -eq 1) { "0x{0:X8}" -f [int]$ownerRules[0].FileSystemRights } else { $null }
            $childMaximum = Invoke-XbNativeAccessCheck -Path $logPath -Token $nativeToken -DesiredAccess ([uint32]0x02000000)
            $ci7.child_granted_mask = "0x{0:X8}" -f [uint32]$childMaximum.GrantedAccess
            $ci7.child_write_dac = Test-XbNativeAccessAllowed -Path $logPath -Token $nativeToken -DesiredAccess ([uint32]0x00040000)
            $ci7.child_write_owner = Test-XbNativeAccessAllowed -Path $logPath -Token $nativeToken -DesiredAccess ([uint32]0x00080000)
            $ci7.child_delete = Test-XbNativeAccessAllowed -Path $logPath -Token $nativeToken -DesiredAccess ([uint32]0x00010000)

            $verification = $null
            try {
                $verification = Invoke-XbInstallVerifier -TaskCredential $credential
                $ci7.verify_outcome = "pass"
                $ci7.verify_status = [string]$verification.status
                $ci7.verify_release_sha256 = [string]$verification.release_sha256
                $ci7.verify_checks = $verification.checks
            } catch { $ci7.verify_outcome = [string]$_.Exception.Message }

            $installChain = [string[]](Get-XbNativePathChain -Path $InstallRoot)
            if ($installChain.Count -lt 1) { throw "ci7_install_chain_missing" }
            $driveRoot = [string]$installChain[0]
            $driveRootObject = [XbWorkerProtectedObject]::Open($driveRoot, $true, $false)
            try {
                $driveRootDescriptorBefore = [string]$driveRootObject.SecurityDescriptorSha256
                $driveRootIdentityBefore = [string]$driveRootObject.FileIdentity
                $driveRootAddDirectory = Invoke-XbCi7HandleAccessCheck -Object $driveRootObject -Token $nativeToken -DesiredAccess ([uint32]0x00000004)
                $ci7.drive_root_index0 = [ordered]@{
                    index = 0
                    path = $driveRoot
                    right = "0x00000004"
                    allowed = [bool]$driveRootAddDirectory.Allowed
                    granted_access = [uint32]$driveRootAddDirectory.GrantedAccess
                }

                $ci7.frozen_control_source_commit = $FrozenSourceCommit
                $ci7.frozen_control_installer_blob = $FrozenInstallerBlob
                $frozenContextText = [IO.File]::ReadAllText($FrozenContextPath)
                $frozenChildScriptText = [IO.File]::ReadAllText($FrozenChildScriptPath)
                if (-not $frozenContextText.StartsWith("function Open-XbCi7VerificationContext {", [StringComparison]::Ordinal) -or
                    $frozenContextText.Length -gt 32768 -or [string]::IsNullOrWhiteSpace($frozenChildScriptText) -or
                    $frozenChildScriptText.Length -gt 32768) {
                    throw "ci7_frozen_control_source_invalid"
                }
                $candidateContextBefore = ${function:script:Open-XbCi7VerificationContext}
                $encryptedPassword = ConvertFrom-SecureString -SecureString $credential.Password
                $childFixture = [ordered]@{
                    installer_path = $InstallerPath
                    worker_account = [string]$credential.UserName
                    encrypted_password = $encryptedPassword
                    frozen_context_source = $frozenContextText
                    source_commit = $FrozenSourceCommit
                    source_blob = $FrozenInstallerBlob
                }
                try {
                    $frozenChildRun = Invoke-XbCi7FrozenControlChild `
                        -RepositoryRoot (Split-Path -Parent (Split-Path -Parent $InstallerPath)) `
                        -ScriptText $frozenChildScriptText `
                        -Fixture $childFixture
                } finally {
                    $childFixture.encrypted_password = $null
                    $encryptedPassword = $null
                }
                $ci7.frozen_control_child_status = [string]$frozenChildRun.status
                $ci7.frozen_control_child_exit_status = $frozenChildRun.exit_status
                $ci7.frozen_control_child_process_distinct = [bool]$frozenChildRun.process_distinct
                $ci7.frozen_control_child_terminated = [bool]$frozenChildRun.process_terminated
                $ci7.frozen_control_child_residue = [string]$frozenChildRun.residue
                $ci7.frozen_control_child_stderr_present = [bool]$frozenChildRun.stderr_present
                $ci7.frozen_control_child_setup_stage = $null
                $ci7.fixture_account_preserved = $null
                $ci7.credential_username_matches_fixture = $null
                $ci7.product_token_open = "NOT_REACHED"
                $ci7.frozen_control_source_binding = $false
                $ci7.frozen_control_parent_function_replaced = -not [object]::ReferenceEquals(
                    $candidateContextBefore, ${function:script:Open-XbCi7VerificationContext}
                )
                if ($null -ne $frozenChildRun.child_result) {
                    $childResult = $frozenChildRun.child_result
                    $ci7.frozen_control_child_setup_stage = [string]$childResult.setup_stage
                    $ci7.fixture_account_preserved = $childResult.fixture_account_preserved
                    $ci7.credential_username_matches_fixture = $childResult.credential_username_matches_fixture
                    $ci7.product_token_open = [string]$childResult.product_token_open
                    $ci7.product_token_identity_matches_fixture = $childResult.product_token_identity_matches_fixture
                    $ci7.frozen_control_source_binding = [bool](
                        $childResult.source_commit -ceq $FrozenSourceCommit -and
                        $childResult.source_blob -ceq $FrozenInstallerBlob
                    )
                    $ci7.frozen_defective_context_outcome = [string]$childResult.outcome
                }
                if ($ci7.frozen_control_parent_function_replaced) { throw "ci7_parent_context_function_replaced" }
                $null = Assert-XbCi7FrozenControlChildResult `
                    -RunResult $frozenChildRun `
                    -SourceCommit $FrozenSourceCommit `
                    -SourceBlob $FrozenInstallerBlob

            } finally { $driveRootObject.Dispose() }
            $driveRootAfter = [XbWorkerProtectedObject]::Open($driveRoot, $true, $false)
            try {
                $ci7.drive_root_descriptor_unchanged = ([string]$driveRootAfter.SecurityDescriptorSha256 -ceq $driveRootDescriptorBefore)
                $ci7.drive_root_identity_unchanged = ([string]$driveRootAfter.FileIdentity -ceq $driveRootIdentityBefore)
            } finally { $driveRootAfter.Dispose() }

            $scopeOutRights = @(
                @{ name = "file_add_file"; mask = [uint32]0x00000002 },
                @{ name = "file_add_subdirectory"; mask = [uint32]0x00000004 },
                @{ name = "file_write_ea"; mask = [uint32]0x00000010 },
                @{ name = "file_write_attributes"; mask = [uint32]0x00000100 }
            )
            $ancestorDeniedRights = @(
                @{ name = "delete_child"; mask = [uint32]0x00000040 },
                @{ name = "delete"; mask = [uint32]0x00010000 },
                @{ name = "write_dac"; mask = [uint32]0x00040000 },
                @{ name = "write_owner"; mask = [uint32]0x00080000 }
            )
            $protectedDeniedRights = @($scopeOutRights + $ancestorDeniedRights)
            $configPath = Join-Path $RuntimeRoot "config"
            $exactProtectedRoots = @($InstallRoot, $configPath)
            $ci7.exact_root_mutation_matrix = @()
            foreach ($protectedRoot in $exactProtectedRoots) {
                foreach ($right in $protectedDeniedRights) {
                    $probe = Invoke-XbCi7AccessResultFixture -Path $protectedRoot -DesiredAccess $right.mask -Token $nativeToken -Body {
                        Open-XbCi7VerificationContext -Token $nativeToken
                    }
                    $ci7.exact_root_mutation_matrix += [ordered]@{
                        root = $protectedRoot
                        right = $right.name
                        mask = ("0x{0:X8}" -f [uint32]$right.mask)
                        outcome = [string]$probe.outcome
                        fixture_allowed = [bool]$probe.fixture_allowed
                        fixture_granted_access = [uint32]$probe.fixture_granted_access
                        policy_consulted_fixture = [bool]$probe.policy_consulted_fixture
                    }
                }
            }

            $ancestorPathByKey = @{}
            foreach ($protectedRoot in $exactProtectedRoots) {
                $chain = [string[]](Get-XbNativePathChain -Path $protectedRoot)
                if ($chain.Count -lt 2) { throw "ci7_protected_root_has_no_ancestor" }
                for ($index = 0; $index -lt ($chain.Count - 1); $index++) {
                    $ancestorPath = [string]$chain[$index]
                    $ancestorKey = Get-XbCi7PathKey -Path $ancestorPath
                    $ancestorPathByKey[$ancestorKey] = $ancestorPath
                }
            }
            $ancestorPaths = @($ancestorPathByKey.Keys | Sort-Object | ForEach-Object { [string]$ancestorPathByKey[$_] })
            $ci7.ancestor_scoped_out_right_matrix = @()
            foreach ($ancestorPath in $ancestorPaths) {
                foreach ($right in $scopeOutRights) {
                    $probe = Invoke-XbCi7AccessResultFixture -Path $ancestorPath -DesiredAccess $right.mask -Token $nativeToken -Body {
                        Open-XbCi7VerificationContext -Token $nativeToken
                    }
                    $ci7.ancestor_scoped_out_right_matrix += [ordered]@{
                        ancestor = $ancestorPath
                        right = $right.name
                        mask = ("0x{0:X8}" -f [uint32]$right.mask)
                        outcome = [string]$probe.outcome
                        fixture_allowed = [bool]$probe.fixture_allowed
                        fixture_granted_access = [uint32]$probe.fixture_granted_access
                        policy_consulted_fixture = [bool]$probe.policy_consulted_fixture
                    }
                }
            }
            $ci7.ancestor_denied_right_matrix = @()
            foreach ($ancestorPath in $ancestorPaths) {
                foreach ($right in $ancestorDeniedRights) {
                    $probe = Invoke-XbCi7AccessResultFixture -Path $ancestorPath -DesiredAccess $right.mask -Token $nativeToken -Body {
                        Open-XbCi7VerificationContext -Token $nativeToken
                    }
                    $ci7.ancestor_denied_right_matrix += [ordered]@{
                        ancestor = $ancestorPath
                        right = $right.name
                        mask = ("0x{0:X8}" -f [uint32]$right.mask)
                        outcome = [string]$probe.outcome
                        fixture_allowed = [bool]$probe.fixture_allowed
                        fixture_granted_access = [uint32]$probe.fixture_granted_access
                        policy_consulted_fixture = [bool]$probe.policy_consulted_fixture
                    }
                }
            }

            $deletionContext = $null
            try {
                $deletionContext = Open-XbCi7VerificationContext -Token $nativeToken
                $deletionProfiles = @(
                    $InstallRoot,
                    (Join-Path $RuntimeRoot "config"),
                    (Join-Path $RuntimeRoot "secrets"),
                    (Join-Path $RuntimeRoot "logs"),
                    (Join-Path $RuntimeRoot "rollback")
                )
                $ci7.delete_chain_matrix = @()
                $ci7.delete_child_parent_edge_matrix = @()
                foreach ($surfacePath in $deletionProfiles) {
                    $surfaceKey = Get-XbCi7PathKey -Path $surfacePath
                    $chain = [string[]]$deletionContext.PathChains[$surfaceKey]
                    for ($index = $chain.Count - 1; $index -ge 0; $index--) {
                        $checkedPath = [string]$chain[$index]
                        $probe = Invoke-XbCi7AccessResultFixture -Path $checkedPath -DesiredAccess ([uint32]0x00010000) -Token $nativeToken -Body {
                            Assert-XbCi7PathDeletionComposition -Path $surfacePath -Token $nativeToken -Context $deletionContext
                        }
                        $ci7.delete_chain_matrix += [ordered]@{
                            surface = $surfacePath
                            path = $checkedPath
                            right = "0x00010000"
                            outcome = [string]$probe.outcome
                            fixture_allowed = [bool]$probe.fixture_allowed
                            fixture_granted_access = [uint32]$probe.fixture_granted_access
                            policy_consulted_fixture = [bool]$probe.policy_consulted_fixture
                        }
                    }
                    for ($index = $chain.Count - 1; $index -gt 0; $index--) {
                        $parentPath = [string]$chain[$index - 1]
                        $probe = Invoke-XbCi7AccessResultFixture -Path $parentPath -DesiredAccess ([uint32]0x00000040) -Token $nativeToken -Body {
                            Assert-XbCi7PathDeletionComposition -Path $surfacePath -Token $nativeToken -Context $deletionContext
                        }
                        $ci7.delete_child_parent_edge_matrix += [ordered]@{
                            surface = $surfacePath
                            parent = $parentPath
                            right = "0x00000040"
                            outcome = [string]$probe.outcome
                            fixture_allowed = [bool]$probe.fixture_allowed
                            fixture_granted_access = [uint32]$probe.fixture_granted_access
                            policy_consulted_fixture = [bool]$probe.policy_consulted_fixture
                        }
                    }
                }
            } finally { Dispose-XbCi7VerificationContext -Context $deletionContext }

            $reviewedIdentity = Read-XbReviewedPackageIdentity -Path $ReviewedManifestPath -PackageRoot (Split-Path -Parent $InstallerPath)
            $upgrade = $null
            try {
                $upgrade = Invoke-XbWorkerUpgrade -SourceRoot (Split-Path -Parent $InstallerPath) -ReviewedIdentity $reviewedIdentity
                $ci7.upgrade_outcome = [string]$upgrade.status
            } catch { $ci7.upgrade_outcome = [string]$_.Exception.Message }
            if ($null -ne $upgrade -and -not [string]::IsNullOrWhiteSpace([string]$upgrade.snapshot)) {
                $upgradeSnapshot = Join-Path (Join-Path $RuntimeRoot "rollback") ([string]$upgrade.snapshot)
                if (Test-Path -LiteralPath $upgradeSnapshot) { Remove-Item -LiteralPath $upgradeSnapshot -Recurse -Force -ErrorAction Stop }
            }
            $ci7.verify_after_upgrade = Get-XbBoundaryOutcome { Invoke-XbInstallVerifier -TaskCredential $credential }
            $configContent = [IO.File]::ReadAllText($configFile)
            Remove-Item -LiteralPath $configFile -Force -ErrorAction Stop
            $ci7.empty_config_verify = Get-XbBoundaryOutcome { Invoke-XbInstallVerifier -TaskCredential $credential }
            [IO.File]::WriteAllText($configFile, $configContent, [Text.UTF8Encoding]::new($false))
            Set-XbWorkerTrustedOwner -Path $configFile -OwnerSid "S-1-5-18"
            $syntheticConfigSnapshot = Get-XbSyntheticConfigSnapshot -Path $configFile
            if ($syntheticConfigSnapshot.text -cne "{}") { throw "synthetic_config_fixture_content_unexpected" }
            $syntheticConfigIdentity = [string]$syntheticConfigSnapshot.file_identity
            $ci7.package_owner_sids = @($packageFiles + "installation-manifest.json" | ForEach-Object { (Get-Acl -LiteralPath (Join-Path $InstallRoot $_)).GetOwner([Security.Principal.SecurityIdentifier]).Value })
            $ci7.config_owner_sid = (Get-Acl -LiteralPath $configFile).GetOwner([Security.Principal.SecurityIdentifier]).Value

            $ci7.missing_credential = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights }
            $mismatchedCredential = New-Object Management.Automation.PSCredential("xb-ci7-other", $securePassword)
            $ci7.mismatched_credential = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $mismatchedCredential }
            $ci7.invalid_password = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $wrongCredential }
            $unsupportedAccount = $WorkerAccount + "@invalid.example"
            $unsupportedCredential = New-Object Management.Automation.PSCredential($unsupportedAccount, $securePassword)
            $originalWorkerAccount = $script:WorkerAccount
            try {
                $script:WorkerAccount = $unsupportedAccount
                $ci7.unsupported_account_profile = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $unsupportedCredential }
            } finally { $script:WorkerAccount = $originalWorkerAccount }

            $originalAccessCheck = ${function:Invoke-XbNativeAccessCheck}
            Set-Item function:script:Invoke-XbNativeAccessCheck { param([string]$Path, $Token, [uint32]$DesiredAccess) throw "forced_native_access_failure" }
            try { $ci7.native_access_failure = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential } }
            finally { Set-Item function:script:Invoke-XbNativeAccessCheck $originalAccessCheck }

            $originalHandleAccessCheck = ${function:Invoke-XbCi7HandleAccessCheck}
            Set-Item function:script:Invoke-XbCi7HandleAccessCheck { param($Object, $Token, [uint32]$DesiredAccess) throw "forced_handle_access_failure" }
            try { $ci7.handle_native_access_failure = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential } }
            finally { Set-Item function:script:Invoke-XbCi7HandleAccessCheck $originalHandleAccessCheck }

            $configPath = Join-Path $RuntimeRoot "config"
            $denyRead = [Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::ReadData, [Security.AccessControl.AccessControlType]::Deny)
            $originalConfigAcl = Get-Acl -LiteralPath $configPath
            $primitivePath = Join-Path $InstallRoot "ac2_member_create_primitive.ps1"
            $script:failureCleanupPrimitiveLeafOpened = $false
            $originalAddProtectedLeaf = ${function:Add-XbCi7ProtectedLeaf}
            $script:failureCleanupOriginalAddProtectedLeaf = $originalAddProtectedLeaf
            try {
                Set-Item function:script:Add-XbCi7ProtectedLeaf {
                    param([string]$Path, [uint32]$AllowedMask, [uint32]$RequiredMask, $Token, [System.Collections.Generic.List[object]]$Objects)
                    $record = & $script:failureCleanupOriginalAddProtectedLeaf @PSBoundParameters
                    if ([IO.Path]::GetFileName($Path) -ceq "ac2_member_create_primitive.ps1") { $script:failureCleanupPrimitiveLeafOpened = $true }
                    return $record
                }
                $deniedConfigAcl = Get-Acl -LiteralPath $configPath
                $deniedConfigAcl.AddAccessRule($denyRead)
                Set-Acl -LiteralPath $configPath -AclObject $deniedConfigAcl -ErrorAction Stop
                $ci7.required_operation_denied = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential }
                Set-Item function:script:Add-XbCi7ProtectedLeaf $originalAddProtectedLeaf
                $ci7.failure_cleanup_primitive_leaf_opened = [bool]$script:failureCleanupPrimitiveLeafOpened
                $ownerEnumerationCompleted = $false
                $currentHarnessOwnsPrimitive = $false
                $ci7.failure_cleanup_lock_owner_count = [int][XbCi7FailureCleanupProbe]::GetOwnerCount($primitivePath, [int]$PID, [ref]$ownerEnumerationCompleted, [ref]$currentHarnessOwnsPrimitive)
                $ci7.failure_cleanup_owner_enumeration_completed = [bool]$ownerEnumerationCompleted
                $ci7.failure_cleanup_current_harness_owner = [bool]$currentHarnessOwnsPrimitive
                $exclusiveDeleteCompleted = $false
                $ci7.failure_cleanup_exclusive_delete_succeeded = [bool][XbCi7FailureCleanupProbe]::ProbeExclusiveDelete($primitivePath, [ref]$exclusiveDeleteCompleted)
                $ci7.failure_cleanup_exclusive_delete_completed = [bool]$exclusiveDeleteCompleted
            } finally {
                try { Set-Item function:script:Add-XbCi7ProtectedLeaf $originalAddProtectedLeaf }
                finally {
                    try { Set-Acl -LiteralPath $configPath -AclObject $originalConfigAcl -ErrorAction Stop }
                    finally {
                        Remove-Variable -Name failureCleanupPrimitiveLeafOpened -Scope Script -ErrorAction SilentlyContinue
                        Remove-Variable -Name failureCleanupOriginalAddProtectedLeaf -Scope Script -ErrorAction SilentlyContinue
                    }
                }
            }

            $ci7.prohibited_write_dac_allow = Invoke-XbCi7AclProbe -Path $configPath -Rule ([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::ChangePermissions, [Security.AccessControl.AccessControlType]::Allow)) -Token $nativeToken -DesiredAccess ([uint32]0x00040000) -Credential $credential
            $ci7.prohibited_write_owner_allow = Invoke-XbCi7AclProbe -Path $configPath -Rule ([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::TakeOwnership, [Security.AccessControl.AccessControlType]::Allow)) -Token $nativeToken -DesiredAccess ([uint32]0x00080000) -Credential $credential
            $ci7.prohibited_delete_allow = Invoke-XbCi7AclProbe -Path $configPath -Rule ([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::Delete, [Security.AccessControl.AccessControlType]::Allow)) -Token $nativeToken -DesiredAccess ([uint32]0x00010000) -Credential $credential
            $ci7.prohibited_delete_child_allow = Invoke-XbCi7AclProbe -Path $configPath -Rule ([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles, [Security.AccessControl.AccessControlType]::Allow)) -Token $nativeToken -DesiredAccess ([uint32]0x00000040) -Credential $credential
            $ci7.child_write_dac_allow = Invoke-XbCi7AclProbe -Path $logPath -Rule ([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::ChangePermissions, [Security.AccessControl.AccessControlType]::Allow)) -Token $nativeToken -DesiredAccess ([uint32]0x00040000) -Credential $credential
            $ci7.child_write_owner_allow = Invoke-XbCi7AclProbe -Path $logPath -Rule ([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::TakeOwnership, [Security.AccessControl.AccessControlType]::Allow)) -Token $nativeToken -DesiredAccess ([uint32]0x00080000) -Credential $credential
            $ci7.parent_delete_child_allow = Invoke-XbCi7AclProbe -Path $RuntimeRoot -Rule ([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles, [Security.AccessControl.AccessControlType]::Allow)) -Token $nativeToken -DesiredAccess ([uint32]0x00000040) -Credential $credential

            $installRootPath = $InstallRoot
            $manifestPath = Join-Path $InstallRoot "installation-manifest.json"
            $packagePath = Join-Path $InstallRoot "ac2_member_gateway_worker.ps1"
            $leafTargets = @()
            foreach ($name in $packageFiles) { $leafTargets += @{ Class = "package"; Name = $name; Path = (Join-Path $InstallRoot $name) } }
            $leafTargets += @{ Class = "manifest"; Name = "installation-manifest.json"; Path = $manifestPath }
            $leafTargets += @{ Class = "config"; Name = "worker.config.json"; Path = $configFile }
            $ci7.positive_leaf_access_matrix = @()
            $ci7.positive_leaf_denial_matrix = @()
            foreach ($target in $leafTargets) {
                $requiredMask = if ($target.Class -eq "package") { [uint32]0x001200A9 } else { [uint32]0x00120089 }
                $ci7.positive_leaf_access_matrix += [ordered]@{ class = $target.Class; leaf = $target.Name; required_mask = ("0x{0:X8}" -f $requiredMask); granted = [bool](Test-XbNativeAccessAllowed -Path $target.Path -Token $nativeToken -DesiredAccess $requiredMask) }
                foreach ($right in @(
                    @{ Name = "write_data"; Mask = [uint32]0x00000002 },
                    @{ Name = "append_data"; Mask = [uint32]0x00000004 },
                    @{ Name = "write_ea"; Mask = [uint32]0x00000010 },
                    @{ Name = "write_attributes"; Mask = [uint32]0x00000100 },
                    @{ Name = "delete"; Mask = [uint32]0x00010000 },
                    @{ Name = "write_dac"; Mask = [uint32]0x00040000 },
                    @{ Name = "write_owner"; Mask = [uint32]0x00080000 }
                )) {
                    $ci7.positive_leaf_denial_matrix += [ordered]@{ class = $target.Class; leaf = $target.Name; right = $right.Name; granted = [bool](Test-XbNativeAccessAllowed -Path $target.Path -Token $nativeToken -DesiredAccess $right.Mask) }
                }
            }
            $leafRights = @(
                @{ Name = "write_data"; Mask = [uint32]0x00000002; Rights = [Security.AccessControl.FileSystemRights]::WriteData }
                @{ Name = "append_data"; Mask = [uint32]0x00000004; Rights = [Security.AccessControl.FileSystemRights]::AppendData }
                @{ Name = "write_ea"; Mask = [uint32]0x00000010; Rights = [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes }
                @{ Name = "write_attributes"; Mask = [uint32]0x00000100; Rights = [Security.AccessControl.FileSystemRights]::WriteAttributes }
                @{ Name = "delete"; Mask = [uint32]0x00010000; Rights = [Security.AccessControl.FileSystemRights]::Delete }
                @{ Name = "write_dac"; Mask = [uint32]0x00040000; Rights = [Security.AccessControl.FileSystemRights]::ChangePermissions }
                @{ Name = "write_owner"; Mask = [uint32]0x00080000; Rights = [Security.AccessControl.FileSystemRights]::TakeOwnership }
            )
            $ci7.leaf_denial_matrix = @()
            foreach ($target in $leafTargets) {
                foreach ($right in $leafRights) {
                    $probe = Invoke-XbCi7AclProbe -Path $target.Path -Rule ([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, $right.Rights, [Security.AccessControl.AccessControlType]::Allow)) -Token $nativeToken -DesiredAccess $right.Mask -Credential $credential
                    $ci7.leaf_denial_matrix += [ordered]@{ class = $target.Class; leaf = $target.Name; right = $right.Name; requested_operation_granted = [bool]$probe.requested_operation_granted; effective_rights = [string]$probe.effective_rights }
                }
            }

            $requiredAccessMatrix = @()
            foreach ($target in $leafTargets) {
                $requiredRights = if ($target.Class -eq "package") {
                    @(@{ Name = "read"; Mask = [uint32]0x00000001; Right = [Security.AccessControl.FileSystemRights]::ReadData },
                      @{ Name = "execute"; Mask = [uint32]0x00000020; Right = [Security.AccessControl.FileSystemRights]::ExecuteFile })
                } else { @(@{ Name = "read"; Mask = [uint32]0x00000001; Right = [Security.AccessControl.FileSystemRights]::ReadData }) }
                foreach ($requiredRight in $requiredRights) {
                    $probe = Invoke-XbCi7DenyProbe -Path $target.Path -Rule ([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, $requiredRight.Right, [Security.AccessControl.AccessControlType]::Deny)) -Token $nativeToken -DesiredAccess $requiredRight.Mask -Credential $credential
                    $requiredAccessMatrix += [ordered]@{ class = $target.Class; leaf = $target.Name; right = $requiredRight.Name; requested_operation_denied = [bool]$probe.requested_operation_denied; effective_rights = [string]$probe.effective_rights }
                }
            }
            $ci7.required_access_matrix = $requiredAccessMatrix

            $ownerTargets = @()
            foreach ($name in $packageFiles) { $ownerTargets += [ordered]@{ class = "package"; path = (Join-Path $InstallRoot $name) } }
            $ownerTargets += [ordered]@{ class = "manifest"; path = $manifestPath }
            $ownerTargets += [ordered]@{ class = "config"; path = $configFile }
            $ci7.owner_denial_matrix = @()
            $ownerSids = @(
                @{ kind = "worker"; sid = $workerSid }
                @{ kind = "arbitrary_admin"; sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
            )
            foreach ($owner in $ownerSids) {
                if ([string]$owner.sid -in @("S-1-5-18", "S-1-5-32-544", $workerSid)) {
                    if ([string]$owner.kind -eq "arbitrary_admin") { throw "ci7_untrusted_owner_fixture_unavailable" }
                }
            }
            foreach ($target in $ownerTargets) {
                foreach ($owner in $ownerSids) {
                    $ci7.owner_denial_matrix += [ordered]@{ class = $target.class; leaf = (Split-Path -Leaf $target.path); owner_kind = $owner.kind; outcome = (Invoke-XbCi7OwnerProbe -Path $target.path -Credential $credential -WrongOwnerSid ([string]$owner.sid)) }
                }
            }

            $configParentOriginal = Get-Acl -LiteralPath $configPath -ErrorAction Stop
            $configLeafOriginal = Get-Acl -LiteralPath $configFile -ErrorAction Stop
            try {
                $parentWithDeleteChild = Get-Acl -LiteralPath $configPath -ErrorAction Stop
                $parentWithDeleteChild.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles, [Security.AccessControl.AccessControlType]::Allow))
                Set-Acl -LiteralPath $configPath -AclObject $parentWithDeleteChild -ErrorAction Stop
                $childWithDeleteDeny = Get-Acl -LiteralPath $configFile -ErrorAction Stop
                $childWithDeleteDeny.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::Delete, [Security.AccessControl.AccessControlType]::Deny))
                Set-Acl -LiteralPath $configFile -AclObject $childWithDeleteDeny -ErrorAction Stop
                $ci7.config_parent_delete_child_granted = Test-XbNativeAccessAllowed -Path $configPath -Token $nativeToken -DesiredAccess ([uint32]0x00000040)
                $ci7.config_child_delete_denied = -not (Test-XbNativeAccessAllowed -Path $configFile -Token $nativeToken -DesiredAccess ([uint32]0x00010000))
                $ci7.config_parent_delete_child_composition = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential }
            } finally {
                Set-Acl -LiteralPath $configFile -AclObject $configLeafOriginal -ErrorAction Stop
                Set-Acl -LiteralPath $configPath -AclObject $configParentOriginal -ErrorAction Stop
            }

            $packageDeleteCompositionLeaf = [string]$packageFiles[0]
            $packageDeleteCompositionPath = Join-Path $InstallRoot $packageDeleteCompositionLeaf
            $packageDeleteCompositionParentOriginal = Get-Acl -LiteralPath $installRootPath -ErrorAction Stop
            $packageDeleteCompositionLeafOriginal = Get-Acl -LiteralPath $packageDeleteCompositionPath -ErrorAction Stop
            try {
                $packageParentWithDeleteChild = Get-Acl -LiteralPath $installRootPath -ErrorAction Stop
                $packageParentWithDeleteChild.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles, [Security.AccessControl.AccessControlType]::Allow))
                Set-Acl -LiteralPath $installRootPath -AclObject $packageParentWithDeleteChild -ErrorAction Stop
                $packageLeafWithDeleteDeny = Get-Acl -LiteralPath $packageDeleteCompositionPath -ErrorAction Stop
                $packageLeafWithDeleteDeny.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::Delete, [Security.AccessControl.AccessControlType]::Deny))
                Set-Acl -LiteralPath $packageDeleteCompositionPath -AclObject $packageLeafWithDeleteDeny -ErrorAction Stop
                $ci7.package_parent_delete_child_leaf = $packageDeleteCompositionLeaf
                $ci7.package_parent_delete_child_granted = Test-XbNativeAccessAllowed -Path $installRootPath -Token $nativeToken -DesiredAccess ([uint32]0x00000040)
                $ci7.package_child_delete_denied = -not (Test-XbNativeAccessAllowed -Path $packageDeleteCompositionPath -Token $nativeToken -DesiredAccess ([uint32]0x00010000))
                $ci7.package_parent_delete_child_composition = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential }
            } finally {
                Set-Acl -LiteralPath $packageDeleteCompositionPath -AclObject $packageDeleteCompositionLeafOriginal -ErrorAction Stop
                Set-Acl -LiteralPath $installRootPath -AclObject $packageDeleteCompositionParentOriginal -ErrorAction Stop
            }

            $usersSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-545")
            $configParentOriginal = Get-Acl -LiteralPath $configPath -ErrorAction Stop
            try {
                $withInheritedGroupGrant = Get-Acl -LiteralPath $configPath -ErrorAction Stop
                $withInheritedGroupGrant.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($usersSid, [Security.AccessControl.FileSystemRights]::WriteData, [Security.AccessControl.InheritanceFlags]::ObjectInherit, [Security.AccessControl.PropagationFlags]::InheritOnly, [Security.AccessControl.AccessControlType]::Allow))
                Set-Acl -LiteralPath $configPath -AclObject $withInheritedGroupGrant -ErrorAction Stop
                $childAclWithInheritedGroupGrant = Get-Acl -LiteralPath $configFile -ErrorAction Stop
                $inheritedRules = @($childAclWithInheritedGroupGrant.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object { $_.IdentityReference.Value -ceq $usersSid.Value -and $_.IsInherited -and ([int]$_.FileSystemRights -band 0x00000002) -ne 0 })
                $ci7.inherited_group_write_granted = Test-XbNativeAccessAllowed -Path $configFile -Token $nativeToken -DesiredAccess ([uint32]0x00000002)
                $ci7.inherited_group_write_ace_count = $inheritedRules.Count
                $ci7.inherited_group_write_rejected = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential }
            } finally { Set-Acl -LiteralPath $configPath -AclObject $configParentOriginal -ErrorAction Stop }

            $ci7.readonly_explicit_control = Invoke-XbCi7AclProbe -Path $configFile -Rule ([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::Read, [Security.AccessControl.AccessControlType]::Allow)) -Token $nativeToken -DesiredAccess ([uint32]0x00120089) -Credential $credential
            $configInheritedRules = @((Get-Acl -LiteralPath $configFile -ErrorAction Stop).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object { $_.IdentityReference.Value -ceq $workerSid -and $_.IsInherited -and $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow })
            $ci7.inherited_readonly_control = [ordered]@{ inherited_worker_read_allow_count = $configInheritedRules.Count; verifier = Get-XbBoundaryOutcome { Invoke-XbInstallVerifier -TaskCredential $credential } }

            $unexpectedConfigDirectory = Join-Path $configPath ".ci7-unsupported-dir"
            try {
                New-Item -ItemType Directory -Path $unexpectedConfigDirectory -ErrorAction Stop | Out-Null
                $ci7.config_subdirectory_rejected = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential }
            } finally {
                if (Test-Path -LiteralPath $unexpectedConfigDirectory) { Remove-Item -LiteralPath $unexpectedConfigDirectory -Force -ErrorAction Stop }
            }

            $context = $null
            try {
                $context = Open-XbCi7VerificationContext -Token $nativeToken
                $firstProtectedLeaf = @($context.Leaves.Values)[0]
                $firstProtectedLeaf.FileIdentity = "00000000:0000000000000000"
                $ci7.conflicting_handle_identity = Get-XbBoundaryOutcome { Confirm-XbCi7VerificationContext -Context $context -Token $nativeToken }
            } finally { Dispose-XbCi7VerificationContext -Context $context }
            $hiddenConfigPath = Join-Path (Join-Path $RuntimeRoot "config") ".ci7-hidden-probe"
            try {
                Set-Content -LiteralPath $hiddenConfigPath -Value "hidden" -Encoding UTF8
                [IO.File]::SetAttributes($hiddenConfigPath, [IO.FileAttributes]::Hidden)
                Set-XbWorkerTrustedOwner -Path $hiddenConfigPath -OwnerSid "S-1-5-18"
                $hiddenAcl = Get-Acl -LiteralPath $hiddenConfigPath
                $hiddenAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::WriteData, [Security.AccessControl.AccessControlType]::Allow))
                Set-Acl -LiteralPath $hiddenConfigPath -AclObject $hiddenAcl -ErrorAction Stop
                $ci7.hidden_config_write_granted = Test-XbNativeAccessAllowed -Path $hiddenConfigPath -Token $nativeToken -DesiredAccess ([uint32]0x00000002)
                $ci7.hidden_config_write_rejected = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential }
            } finally { Remove-Item -LiteralPath $hiddenConfigPath -Force -ErrorAction SilentlyContinue }

            $configAddFileRule = [Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::CreateFiles, [Security.AccessControl.AccessControlType]::Allow)
            $configAddDirectoryRule = [Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::CreateDirectories, [Security.AccessControl.AccessControlType]::Allow)
            $installAddFileRule = [Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::CreateFiles, [Security.AccessControl.AccessControlType]::Allow)
            $installAddDirectoryRule = [Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::CreateDirectories, [Security.AccessControl.AccessControlType]::Allow)
            $ci7.config_create_file = Invoke-XbCi7AclProbe -Path $configPath -Rule $configAddFileRule -Token $nativeToken -DesiredAccess ([uint32]0x00000002) -Credential $credential
            $ci7.config_create_directory = Invoke-XbCi7AclProbe -Path $configPath -Rule $configAddDirectoryRule -Token $nativeToken -DesiredAccess ([uint32]0x00000004) -Credential $credential
            $ci7.install_create_file = Invoke-XbCi7AclProbe -Path $installRootPath -Rule $installAddFileRule -Token $nativeToken -DesiredAccess ([uint32]0x00000002) -Credential $credential
            $ci7.install_create_directory = Invoke-XbCi7AclProbe -Path $installRootPath -Rule $installAddDirectoryRule -Token $nativeToken -DesiredAccess ([uint32]0x00000004) -Credential $credential
            $ci7.install_parent_delete_child_allow = Invoke-XbCi7AclProbe -Path $installRootPath -Rule ([Security.AccessControl.FileSystemAccessRule]::new($workerSidIdentity, [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles, [Security.AccessControl.AccessControlType]::Allow)) -Token $nativeToken -DesiredAccess ([uint32]0x00000040) -Credential $credential

            $ci7.held_leaf_matrix = @()
            foreach ($target in $leafTargets) {
                $backupBytes = [IO.File]::ReadAllBytes($target.Path)
                $backupAlgorithm = [Security.Cryptography.SHA256]::Create()
                try { $backupHash = ([BitConverter]::ToString($backupAlgorithm.ComputeHash($backupBytes))).Replace("-", "").ToLowerInvariant() }
                finally { $backupAlgorithm.Dispose() }
                $renamedPath = $target.Path + ".ci7-rename-" + (New-XbBoundaryHex 4)
                $heldObject = $null
                $renameBlocked = $false
                $replaceBlocked = $false
                $identityBefore = $null
                $identityAfter = $null
                try {
                    $heldObject = [XbWorkerProtectedObject]::Open($target.Path, $false, $true)
                    $identityBefore = [string]$heldObject.FileIdentity
                    try { Move-Item -LiteralPath $target.Path -Destination $renamedPath -ErrorAction Stop; $renameBlocked = $false }
                    catch { $renameBlocked = $true }
                    if (Test-Path -LiteralPath $renamedPath) { Move-Item -LiteralPath $renamedPath -Destination $target.Path -Force -ErrorAction Stop }
                    try { Set-Content -LiteralPath $target.Path -Value "replacement-probe" -ErrorAction Stop; $replaceBlocked = $false }
                    catch { $replaceBlocked = $true }
                    $identityAfter = [string]$heldObject.FileIdentity
                } finally {
                    if ($null -ne $heldObject) { $heldObject.Dispose() }
                    if (Test-Path -LiteralPath $renamedPath) { Move-Item -LiteralPath $renamedPath -Destination $target.Path -Force -ErrorAction Stop }
                    if (Test-Path -LiteralPath $target.Path) {
                        if ((Get-XbFileSha256 -Path $target.Path) -cne $backupHash) {
                            [IO.File]::WriteAllBytes($target.Path, $backupBytes)
                        }
                    } else { [IO.File]::WriteAllBytes($target.Path, $backupBytes) }
                }
                $ci7.held_leaf_matrix += [ordered]@{ class = $target.Class; leaf = $target.Name; rename_blocked = $renameBlocked; replace_blocked = $replaceBlocked; identity_before = $identityBefore; identity_after = $identityAfter }
            }

            $configBackupPath = Join-Path (Split-Path -Parent $configPath) "config-ci7-original"
            Move-Item -LiteralPath $configPath -Destination $configBackupPath -ErrorAction Stop
            & cmd.exe /c mklink /J "$configPath" "$configBackupPath" | Out-Null
            if ($LASTEXITCODE -ne 0) { Move-Item -LiteralPath $configBackupPath -Destination $configPath -ErrorAction Stop; throw "ci7_reparse_ancestor_fixture_create_failed" }
            $ci7.reparse_snapshot_ancestor_rejected = Get-XbBoundaryOutcome { Get-XbSyntheticConfigSnapshot -Path (Join-Path $configPath "worker.config.json") }
            try { $ci7.reparse_ancestor_rejected = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential } }
            finally {
                & cmd.exe /c rmdir "$configPath" | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "ci7_reparse_ancestor_fixture_cleanup_failed" }
                Move-Item -LiteralPath $configBackupPath -Destination $configPath -ErrorAction Stop
            }

            $reparseTarget = Join-Path $configPath ".ci7-leaf-target"
            Move-Item -LiteralPath $configFile -Destination $reparseTarget -ErrorAction Stop
            & cmd.exe /c mklink "$configFile" "$reparseTarget" | Out-Null
            if ($LASTEXITCODE -ne 0) { Move-Item -LiteralPath $reparseTarget -Destination $configFile -ErrorAction Stop; throw "ci7_reparse_leaf_fixture_create_failed" }
            $ci7.reparse_snapshot_leaf_rejected = Get-XbBoundaryOutcome { Get-XbSyntheticConfigSnapshot -Path $configFile }
            try { $ci7.reparse_leaf_rejected = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential } }
            finally {
                if (Test-Path -LiteralPath $configFile) { Remove-Item -LiteralPath $configFile -Force -ErrorAction Stop }
                Move-Item -LiteralPath $reparseTarget -Destination $configFile -ErrorAction Stop
            }

            $junctionPath = Join-Path ([IO.Path]::GetTempPath()) ("xb-ci7-junction-{0}" -f (New-XbBoundaryHex 8))
            & cmd.exe /c mklink /J "$junctionPath" "$logsPath" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "ci7_reparse_fixture_create_failed" }
            $ci7.reparse_fail_closed = Get-XbBoundaryOutcome { Assert-XbOwnerRightsLogFile -Path (Join-Path $junctionPath (Split-Path -Leaf $logPath)) -WorkerSid $workerSid }
            & cmd.exe /c rmdir "$junctionPath" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "ci7_reparse_fixture_cleanup_failed" }
            $ci7.reparse_fixture_absent = (-not (Test-Path -LiteralPath $junctionPath))
            $ci7.unsupported_unc_fail_closed = Get-XbBoundaryOutcome { Get-XbNativePathChain -Path "\\invalid-host\share" }

            $nativeToken.Impersonate()
            try { Remove-Item -LiteralPath $logPath -Force -ErrorAction Stop }
            finally { [XbWorkerBatchToken]::Revert() }
            $ci7.retention_delete = (-not (Test-Path -LiteralPath $logPath))
            $ci7.required_probe_completion.status = "complete"
            $ci7.required_probe_completion.completed_through = "retention_delete"
        } catch {
            $ci7.required_probe_completion.status = "incomplete"
            $ci7.fatal = [string]$_.Exception.Message
            $ci7.fatal_stack = [string]$_.ScriptStackTrace
        } finally {
            if ($null -ne $nativeToken) {
                try { $nativeToken.Dispose() } catch { }
            }
            if ($null -ne $junctionPath -and (Test-Path -LiteralPath $junctionPath)) {
                try { & cmd.exe /c rmdir "$junctionPath" | Out-Null } catch { }
            }
            if ($null -ne $logPath -and (Test-Path -LiteralPath $logPath)) {
                try { Remove-Item -LiteralPath $logPath -Force -ErrorAction Stop } catch { $ci7.cleanup_error = [string]$_.Exception.Message }
            }
        }
        $case.synthetic_config_teardown = [ordered]@{}
        $configTeardown = $case.synthetic_config_teardown
        $expectedConfigFile = [IO.Path]::GetFullPath((Join-Path (Join-Path $RuntimeRoot "config") "worker.config.json"))
        $configTeardown.path = [IO.Path]::GetFullPath($configFile)
        $configTeardown.path_matches_expected = ($configTeardown.path -ceq $expectedConfigFile)
        $configItem = $null
        try { $configItem = Get-Item -LiteralPath $configFile -Force -ErrorAction Stop }
        catch [System.Management.Automation.ItemNotFoundException] { $configItem = $null }
        catch { throw "synthetic_config_presence_unproven" }
        $configTeardown.present_before = ($null -ne $configItem)
        $configTeardown.runtime_before = Get-XbRuntimeDirectoryEvidence
        if ($null -eq $configItem) { throw "synthetic_config_fixture_missing" }

        $configIsLeaf = -not [bool]$configItem.PSIsContainer
        $configReparsePoint = (($configItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        $runtimeRootItem = Get-Item -LiteralPath $RuntimeRoot -Force -ErrorAction Stop
        $configDirectory = Get-Item -LiteralPath (Join-Path $RuntimeRoot "config") -Force -ErrorAction Stop
        $runtimeRootReparsePoint = (($runtimeRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        $configDirectoryReparsePoint = (($configDirectory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        $configTeardown.file_is_leaf = [bool]$configIsLeaf
        $configTeardown.file_reparse_point = [bool]$configReparsePoint
        $configTeardown.runtime_root_reparse_point = [bool]$runtimeRootReparsePoint
        $configTeardown.config_directory_reparse_point = [bool]$configDirectoryReparsePoint
        $configTeardown.ordinary_non_reparse = [bool]($configIsLeaf -and -not $configReparsePoint -and -not $runtimeRootReparsePoint -and -not $configDirectoryReparsePoint)
        if (-not $configTeardown.path_matches_expected -or -not $configTeardown.ordinary_non_reparse) {
            throw "synthetic_config_identity_unproven"
        }

        $currentConfigSnapshot = Get-XbSyntheticConfigSnapshot -Path $configFile
        $configTeardown.parent_chain_component_count = [int]$currentConfigSnapshot.path_chain_parent_count
        $configTeardown.parent_chain_ordinary_non_reparse = [bool]$currentConfigSnapshot.path_chain_ordinary_non_reparse
        $configTeardown.expected_file_identity = [string]$syntheticConfigIdentity
        $configTeardown.observed_file_identity = [string]$currentConfigSnapshot.file_identity
        $configTeardown.identity_matches_created_file = ($currentConfigSnapshot.file_identity -ceq $syntheticConfigIdentity)
        $configTeardown.content_matches_expected = ($currentConfigSnapshot.text -ceq "{}")
        if (-not $configTeardown.identity_matches_created_file -or -not $configTeardown.content_matches_expected) {
            throw "synthetic_config_identity_or_content_unproven"
        }
        if (-not $configTeardown.parent_chain_ordinary_non_reparse -or $configTeardown.parent_chain_component_count -lt 1) {
            throw "synthetic_config_identity_unproven"
        }

        $configTeardown.removal_attempted = $true
        Remove-Item -LiteralPath $configFile -Force -ErrorAction Stop
        $configTeardown.present_after = Test-Path -LiteralPath $configFile
        $configTeardown.removal_pass = (-not $configTeardown.present_after)
        if (-not $configTeardown.removal_pass) { throw "synthetic_config_fixture_teardown_failed" }
        $configTeardown.runtime_after = Get-XbRuntimeDirectoryEvidence
        if (-not $configTeardown.runtime_after.prerequisite_satisfied) {
            throw "runtime_uninstall_prerequisite_unproven"
        }
        if ($null -ne $ci7.fatal) { throw "ci7_fatal" }
        if ($ci7.required_probe_completion.status -cne "complete") { throw "ci7_required_probes_incomplete" }
        if ($ci7.Contains("cleanup_error")) { throw "ci7_fixture_cleanup_failed" }

        $script:Operation = "Uninstall"
        $case.uninstall_outcome = Get-XbBoundaryOutcome { Invoke-XbWorkerInstaller }
        $case.uninstalled = Get-XbProductionReadback
        Set-XbBoundaryTaskPresenceReadback -CaseRecord $case -Name "uninstall_task_presence"
        $case.account_uninstalled = Get-XbAccountObservation
        $case.rollback_trace = @($trace)
        Invoke-XbBoundaryCleanup -CaseRecord $case -Preflight {
            param($case)
            $case.production_folder_before_retained_cleanup = Get-XbBoundaryComTaskFolderReadback
            $case.retained_parent_state_before_cleanup = Get-XbRetainedParentEvidence
            if (-not $case.retained_parent_state_before_cleanup.safe_to_remove) {
                throw "container_not_owned_empty"
            }
        } -Body {
            Clear-XbRetainedProductionContainers
        }
        $case.post_cleanup = Get-XbProductionReadback
        Set-XbBoundaryTaskPresenceReadback -CaseRecord $case -Name "post_cleanup_task_presence"
    }
} catch {
    $report.fatal = [string]$_.Exception.Message
    $report.fatal_stack = [string]$_.ScriptStackTrace
} finally {
    $cleanup = [ordered]@{}
    try {
        $service = New-Object -ComObject Schedule.Service
        $service.Connect()
        $folderSpecs = @()
        if ($null -ne $state.boundary_folder) { $folderSpecs += ,@("boundary_folder_absent", $state.boundary_folder, $state.boundary_tasks) }
        if ($state.pristine_proven) { $folderSpecs += ,@("production_folder_absent", "\X-Boundaries\", @("AC2 Member Gateway Worker")) }
        foreach ($spec in $folderSpecs) {
            $comPath = ([string]$spec[1]).TrimEnd('\')
            $folder = $null
            try { $folder = $service.GetFolder($comPath) } catch { $folder = $null }
            if ($null -ne $folder) {
                foreach ($name in @($spec[2])) { try { $folder.DeleteTask($name, 0) } catch { } }
                if ([int]$folder.GetTasks(1).Count -eq 0 -and [int]$folder.GetFolders(0).Count -eq 0) { $service.GetFolder("\").DeleteFolder($comPath.TrimStart('\'), 0) }
            }
            $absent = $false
            try { $null = $service.GetFolder($comPath) } catch { $absent = ((Get-XbBoundaryHResult $_) -eq -2147024894) }
            $cleanup[$spec[0]] = $absent
        }
    } catch { $cleanup.scheduler_error = [string]$_.Exception.Message }
    if ($state.pristine_proven) {
        foreach ($entry in @(@("program_files_parent_absent", "C:\Program Files\X-Boundaries"), @("program_data_parent_absent", "C:\ProgramData\X-Boundaries"))) {
            try { if (Test-Path -LiteralPath $entry[1]) { Remove-Item -LiteralPath $entry[1] -Recurse -Force } } catch { $cleanup[$entry[0] + "_error"] = [string]$_.Exception.Message }
            $cleanup[$entry[0]] = (-not (Test-Path -LiteralPath $entry[1]))
        }
    }
    try {
        foreach ($name in @(Get-XbStageNames | Where-Object { $state.stage_before -notcontains $_ })) { Remove-Item -LiteralPath (Join-Path ([IO.Path]::GetTempPath()) $name) -Recurse -Force }
        $cleanup.stage_residue_absent = (@(Get-XbStageNames | Where-Object { $state.stage_before -notcontains $_ }).Count -eq 0)
    } catch { $cleanup.stage_error = [string]$_.Exception.Message }
    if ($null -ne $state.user_sid) {
        if ($state.lsa_loaded) {
            try {
                [XbBoundaryLsa]::RemoveAllRights($state.user_sid)
                $cleanup.lsa_account_object_absent = ($null -eq [XbBoundaryLsa]::GetRights($state.user_sid))
            } catch { $cleanup.lsa_error = [string]$_.Exception.Message }
        }
        try {
            foreach ($profile in @(Get-CimInstance -ClassName Win32_UserProfile -Filter ("SID='{0}'" -f $state.user_sid))) { Remove-CimInstance -InputObject $profile }
            $cleanup.profile_absent = ((@(Get-CimInstance -ClassName Win32_UserProfile -Filter ("SID='{0}'" -f $state.user_sid)).Count -eq 0) -and -not (Test-Path -LiteralPath ("HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\" + $state.user_sid)))
        } catch { $cleanup.profile_error = [string]$_.Exception.Message }
        try {
            Remove-LocalUser -SID $state.user_sid
            $cleanup.user_absent = (@(Get-LocalUser -SID $state.user_sid -ErrorAction SilentlyContinue).Count -eq 0)
        } catch { $cleanup.user_error = [string]$_.Exception.Message }
    }
    if ($null -ne $state.temp_redirect) {
        try { Remove-Item -LiteralPath $state.temp_redirect -Recurse -Force; $cleanup.temp_redirect_absent = (-not (Test-Path -LiteralPath $state.temp_redirect)) }
        catch { $cleanup.temp_redirect_error = [string]$_.Exception.Message }
    }
    if ($null -ne $securePassword) { try { $securePassword.Dispose() } catch { $cleanup.secure_password_disposed = $false } }
    if ($null -ne $secureWrong) { try { $secureWrong.Dispose() } catch { $cleanup.secure_wrong_disposed = $false } }
    $report.cleanup = $cleanup
}

$report.secret_exposure = "none"
$json = $report | ConvertTo-Json -Depth 12 -Compress
foreach ($secret in $secretValues) {
    if (-not [string]::IsNullOrEmpty($secret) -and $json.Contains($secret)) { $json = '{"secret_exposure":"detected"}'; break }
}
[Console]::Out.WriteLine("XB_BOUNDARY_RESULT:" + $json)
if ($null -ne $report.fatal) { exit 1 }
exit 0
'''


class MemberWorkerCi7HarnessClosureTests(unittest.TestCase):
    """Bounded tests for child identity preservation and frozen-control lifecycle."""

    def test_dot_source_collision_is_reproduced_and_account_is_preserved(self) -> None:
        pwsh = _resolve_native_powershell()
        if not pwsh:
            raise unittest.SkipTest("Windows PowerShell is required for dot-source parameter binding")
        script = r'''[CmdletBinding()]
param([Parameter(Mandatory)][string]$InstallerPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$fixtureWorkerAccount = "xbt0123456789ab"
$workerAccount = $fixtureWorkerAccount
. $InstallerPath -LibraryOnly
$unpreservedDotSourceLostAccount = [bool]($workerAccount -cne $fixtureWorkerAccount)

. $InstallerPath -LibraryOnly -WorkerAccount $fixtureWorkerAccount
$workerAccount = $fixtureWorkerAccount
$script:WorkerAccount = $fixtureWorkerAccount
$script:ExpectedWorkerAccount = $fixtureWorkerAccount
Set-Item function:script:Get-XbAccountSid {
    param([Parameter(Mandatory)][string]$Account)
    if ($Account -cne $script:ExpectedWorkerAccount) { throw "fixture_account_mismatch" }
    return "S-1-5-18"
}
Set-Item function:script:Initialize-XbWorkerNativeAccess { }
if ($null -ne ("XbWorkerBatchToken" -as [type])) { throw "unexpected_native_type_preimage" }
Add-Type -TypeDefinition @'
using System.Security;
public sealed class XbWorkerBatchToken
{
    public static string LastUserName;
    public static string LastDomain;
    public static int OpenCount;
    public static XbWorkerBatchToken OpenBatch(string userName, string domain, SecureString password, string sid)
    {
        LastUserName = userName;
        LastDomain = domain;
        OpenCount++;
        return new XbWorkerBatchToken();
    }
}
'@ -ErrorAction Stop | Out-Null
$securePassword = New-Object System.Security.SecureString
$securePassword.AppendChar('x')
$securePassword.MakeReadOnly()
$credential = $null
$productToken = $null
try {
    $accountPreserved = [bool]($workerAccount -ceq $fixtureWorkerAccount -and $script:WorkerAccount -ceq $fixtureWorkerAccount)
    $credential = New-Object Management.Automation.PSCredential($fixtureWorkerAccount, $securePassword)
    $credentialMatchesFixture = [bool]($credential.UserName -ceq $fixtureWorkerAccount)
    $productToken = New-XbWorkerBatchToken -Credential $credential
    $productHelperReceivedFixture = [bool](
        $null -ne $productToken -and [XbWorkerBatchToken]::OpenCount -eq 1 -and
        [XbWorkerBatchToken]::LastUserName -ceq $fixtureWorkerAccount -and
        [XbWorkerBatchToken]::LastDomain -ceq [Environment]::MachineName
    )
    $summary = [ordered]@{
        unpreserved_dot_source_lost_account = $unpreservedDotSourceLostAccount
        fixture_account_preserved = $accountPreserved
        credential_username_matches_fixture = $credentialMatchesFixture
        product_helper_received_fixture = $productHelperReceivedFixture
        product_token_open = ($null -ne $productToken)
    }
    [Console]::Out.WriteLine(($summary | ConvertTo-Json -Compress))
} finally {
    $credential = $null
    if ($null -ne $securePassword) { $securePassword.Dispose() }
}
'''
        with tempfile.TemporaryDirectory(prefix="xb-ci7-account-preservation-") as temp_dir:
            harness = Path(temp_dir) / "account_preservation.ps1"
            harness.write_text(script, encoding="utf-8", newline="\n")
            try:
                completed = subprocess.run(
                    [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness), "-InstallerPath", str(ROOT / INSTALLER_PATH)],
                    cwd=ROOT,
                    env=_windows_powershell_module_environment(),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise AssertionError("account-preservation regression exceeded its bound; output withheld") from None
        if completed.returncode != 0:
            raise AssertionError("account-preservation regression failed; output withheld")
        try:
            actual = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            raise AssertionError("account-preservation regression emitted an invalid summary; output withheld") from None
        expected = {
            "unpreserved_dot_source_lost_account": True,
            "fixture_account_preserved": True,
            "credential_username_matches_fixture": True,
            "product_helper_received_fixture": True,
            "product_token_open": True,
        }
        if actual != expected or any(type(value) is not bool for value in actual.values()):
            raise AssertionError("account-preservation regression emitted an unexpected bounded result")

    def test_frozen_control_binding_and_child_order_are_pinned(self) -> None:
        child = _FROZEN_CI7_CHILD_SCRIPT
        harness = _HOSTED_TASK_BOUNDARY_HARNESS
        capture = child.index("$fixtureWorkerAccount = [string]$Fixture.worker_account")
        source_load = child.index(". $installerPath -LibraryOnly -WorkerAccount $fixtureWorkerAccount")
        account_rebind = child.index("$workerAccount = $fixtureWorkerAccount", source_load)
        script_rebind = child.index("$script:WorkerAccount = $fixtureWorkerAccount", account_rebind)
        credential = child.index("New-Object Management.Automation.PSCredential($fixtureWorkerAccount, $securePassword)")
        token_open = child.index("New-XbWorkerBatchToken -Credential $credential")
        frozen_control = child.index("Set-Item function:script:Open-XbCi7VerificationContext")
        self.assertLess(capture, source_load, "collision-free fixture capture must precede dot-source")
        self.assertLess(source_load, account_rebind, "account variables must be rebound after dot-source")
        self.assertLess(account_rebind, script_rebind, "script account must be rebound after dot-source")
        self.assertLess(script_rebind, credential, "credential must use the preserved fixture account")
        self.assertLess(credential, token_open, "credential identity must be checked before product token open")
        self.assertLess(token_open, frozen_control, "product token must open before frozen-control execution")
        self.assertNotIn("Set-Item function:script:Open-XbCi7VerificationContext", harness)
        self.assertIn("process_distinct", harness)
        self.assertIn("process_terminated", harness)
        self.assertIn('residue = "none"', harness)
        child_call = harness.index("$frozenChildRun = Invoke-XbCi7FrozenControlChild")
        product_matrix = harness.index("$ci7.exact_root_mutation_matrix = @()")
        self.assertLess(child_call, product_matrix)
        for marker in (
            "$ci7.ancestor_scoped_out_right_matrix = @()",
            "$ci7.ancestor_denied_right_matrix = @()",
            "$ci7.delete_chain_matrix = @()",
            "$ci7.delete_child_parent_edge_matrix = @()",
            "$ci7.required_probe_completion",
            "$ci7.failure_cleanup_exclusive_delete_succeeded",
        ):
            with self.subTest(matrix_contract=marker.split(".")[-1]):
                self.assertIn(marker, harness)
        source = Path(__file__).read_text(encoding="utf-8")
        hosted_class = source.split("\nclass MemberWorkerHostedTaskBoundaryTests", 1)[1]
        hosted_case = hosted_class.split("    def test_install_then_uninstall(self) -> None:", 1)[1].split("\n    def ", 1)[0]
        for field in (
            "frozen_control_source_binding", "fixture_account_preserved", "credential_username_matches_fixture",
            "product_token_open", "product_token_identity_matches_fixture", "frozen_control_child_status",
            "frozen_control_child_exit_status", "frozen_control_child_process_distinct",
            "frozen_control_child_terminated", "frozen_control_child_residue",
            "frozen_control_child_stderr_present", "frozen_control_parent_function_replaced",
        ):
            with self.subTest(hosted_child_evidence=field):
                self.assertIn(f'ci7["{field}"]', hosted_case)

    def test_child_lifecycle_outer_budget_exceeds_bounded_inner_budget(self) -> None:
        self.assertEqual(
            _CI7_CHILD_LIFECYCLE_TIMEOUTS_SECONDS,
            (60, 60, 60, 60, 3, 60, 60, 60),
        )
        self.assertEqual(_CI7_CHILD_LIFECYCLE_CLEANUP_MARGIN_SECONDS, 120)
        self.assertEqual(_CI7_CHILD_LIFECYCLE_OUTER_TIMEOUT_SECONDS, 544)
        self.assertRegex(
            _installer_function(_HOSTED_TASK_BOUNDARY_HARNESS, "Invoke-XbCi7FrozenControlChild"),
            r"(?m)^\s*\[int\]\$TimeoutMilliseconds\s*=\s*60000\s*$",
        )
        self.assertGreater(
            _CI7_CHILD_LIFECYCLE_OUTER_TIMEOUT_SECONDS,
            sum(_CI7_CHILD_LIFECYCLE_TIMEOUTS_SECONDS) + _CI7_CHILD_LIFECYCLE_CLEANUP_MARGIN_SECONDS,
        )

    def test_frozen_child_and_hosted_harness_powerShell_sources_parse(self) -> None:
        pwsh = _resolve_native_powershell()
        if not pwsh:
            raise unittest.SkipTest("Windows PowerShell is required for parser validation")
        parser_script = r'''param(
    [Parameter(Mandatory)][string]$ChildScriptPath,
    [Parameter(Mandatory)][string]$HarnessScriptPath
)
$sourcePaths = @($ChildScriptPath, $HarnessScriptPath)
foreach ($sourcePath in $sourcePaths) {
    $tokens = $null
    $parseErrors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseInput([IO.File]::ReadAllText([string]$sourcePath), [ref]$tokens, [ref]$parseErrors)
    if (@($parseErrors).Count -ne 0) { exit 17 }
}
[Console]::Out.WriteLine("PASS")
'''
        with tempfile.TemporaryDirectory(prefix="xb-ci7-parse-") as temp_dir:
            root = Path(temp_dir)
            parser_path = root / "parse_sources.ps1"
            child_path = root / "frozen_child.ps1"
            harness_path = root / "hosted_harness.ps1"
            parser_path.write_text(parser_script, encoding="utf-8", newline="\n")
            child_path.write_text(_FROZEN_CI7_CHILD_SCRIPT, encoding="utf-8", newline="\n")
            harness_path.write_text(_HOSTED_TASK_BOUNDARY_HARNESS, encoding="utf-8", newline="\n")
            try:
                completed = subprocess.run(
                    [
                        pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(parser_path), "-ChildScriptPath", str(child_path),
                        "-HarnessScriptPath", str(harness_path),
                    ],
                    cwd=ROOT,
                    env=_windows_powershell_module_environment(),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise AssertionError("PowerShell source parsing exceeded its bound; output withheld") from None
        if completed.returncode != 0 or completed.stdout.strip() != "PASS":
            raise AssertionError("PowerShell source parsing failed; output withheld")

    def test_child_process_runner_fails_closed_and_proves_timeout_termination(self) -> None:
        pwsh = _resolve_native_powershell()
        if not pwsh:
            raise unittest.SkipTest("Windows PowerShell is required for child process lifecycle checks")

        valid_result = {
            "schema_version": "xb.member.worker.ci7.frozen-control.v1",
            "fixture_status": "completed",
            "outcome": "effective_rights_exceeded",
            "setup_stage": "RESULT_EMIT",
            "source_commit": CI7_DEFECTIVE_BASELINE_COMMIT,
            "source_blob": CI7_DEFECTIVE_INSTALLER_BLOB,
            "fixture_account_preserved": True,
            "credential_username_matches_fixture": True,
            "product_token_open": "PASS",
            "product_token_identity_matches_fixture": True,
        }
        structured_failure_result = {
            **valid_result,
            "fixture_status": "failed",
            "outcome": "fixture_setup_failed",
            "setup_stage": "PRODUCT_TOKEN_OPEN",
            "fixture_account_preserved": True,
            "credential_username_matches_fixture": True,
            "product_token_open": "FAIL",
            "product_token_identity_matches_fixture": None,
        }
        helper = _installer_function(_HOSTED_TASK_BOUNDARY_HARNESS, "Invoke-XbCi7FrozenControlChild")
        validator = _installer_function(_HOSTED_TASK_BOUNDARY_HARNESS, "Assert-XbCi7FrozenControlChildResult")
        lifecycle_script = r'''Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
__RUNNER_HELPER__
__RESULT_VALIDATOR__
$validScript = @'
[Console]::Out.WriteLine('__VALID_RESULT__')
'@
$failureScript = @'
[Console]::Out.WriteLine('__FAILURE_RESULT__')
[Console]::Error.WriteLine('private-stderr-sentinel')
exit 1
'@
$wrongOutcomeScript = $validScript.Replace("effective_rights_exceeded", "effective_rights_missing")
$root = Split-Path -Parent $PSCommandPath
$valid = Invoke-XbCi7FrozenControlChild -RepositoryRoot $root -ScriptText $validScript -Fixture @{} -TimeoutMilliseconds __LONG_TIMEOUT__
$malformed = Invoke-XbCi7FrozenControlChild -RepositoryRoot $root -ScriptText '[Console]::Out.WriteLine("malformed")' -Fixture @{} -TimeoutMilliseconds __LONG_TIMEOUT__
$abnormal = Invoke-XbCi7FrozenControlChild -RepositoryRoot $root -ScriptText 'exit 17' -Fixture @{} -TimeoutMilliseconds __LONG_TIMEOUT__
$oversized = Invoke-XbCi7FrozenControlChild -RepositoryRoot $root -ScriptText '[Console]::Out.WriteLine(("x" * 5000))' -Fixture @{} -TimeoutMilliseconds __LONG_TIMEOUT__
$timeout = Invoke-XbCi7FrozenControlChild -RepositoryRoot $root -ScriptText 'Start-Sleep -Seconds 30' -Fixture @{} -TimeoutMilliseconds __SHORT_TIMEOUT__
$wrongOutcome = Invoke-XbCi7FrozenControlChild -RepositoryRoot $root -ScriptText $wrongOutcomeScript -Fixture @{} -TimeoutMilliseconds __LONG_TIMEOUT__
$failure = Invoke-XbCi7FrozenControlChild -RepositoryRoot $root -ScriptText $failureScript -Fixture @{} -TimeoutMilliseconds __LONG_TIMEOUT__
$stderrSuccessScript = $validScript + "`n[Console]::Error.WriteLine('private-stderr-sentinel')"
$stderrSuccess = Invoke-XbCi7FrozenControlChild -RepositoryRoot $root -ScriptText $stderrSuccessScript -Fixture @{} -TimeoutMilliseconds __LONG_TIMEOUT__
$launchFailed = Invoke-XbCi7FrozenControlChild -RepositoryRoot $root -ScriptText 'unused' -Fixture @{} -TimeoutMilliseconds 0
$validAccepted = $false
try { $null = Assert-XbCi7FrozenControlChildResult -RunResult $valid -SourceCommit "ef194d43cd5b2a6e56468c3381a1b44bced23d8d" -SourceBlob "aa6d4f3172bbc82c69f1c4904f6e4bbf50da3741"; $validAccepted = $true } catch { }
$wrongOutcomeRejected = $false
try { $null = Assert-XbCi7FrozenControlChildResult -RunResult $wrongOutcome -SourceCommit "ef194d43cd5b2a6e56468c3381a1b44bced23d8d" -SourceBlob "aa6d4f3172bbc82c69f1c4904f6e4bbf50da3741" }
catch { $wrongOutcomeRejected = ($_.Exception.Message -ceq "frozen_control_child_outcome_mismatch") }
$failureRejected = $false
try { $null = Assert-XbCi7FrozenControlChildResult -RunResult $failure -SourceCommit "ef194d43cd5b2a6e56468c3381a1b44bced23d8d" -SourceBlob "aa6d4f3172bbc82c69f1c4904f6e4bbf50da3741" }
catch { $failureRejected = ($_.Exception.Message -ceq "frozen_control_child_setup_failed") }
$failureResultText = ConvertTo-Json -InputObject $failure -Depth 6 -Compress
$summary = [ordered]@{
    valid = ($valid.status -ceq "completed" -and $valid.process_distinct -and $valid.process_terminated -and $valid.residue -ceq "none" -and $validAccepted)
    malformed = ($malformed.status -ceq "malformed_output" -and $malformed.process_terminated -and $malformed.residue -ceq "none")
    abnormal = ($abnormal.status -ceq "abnormal_exit" -and $abnormal.process_terminated -and $abnormal.residue -ceq "none")
    oversized = ($oversized.status -ceq "malformed_output" -and $oversized.process_terminated -and $oversized.residue -ceq "none")
    timeout = ($timeout.status -ceq "timeout" -and $timeout.process_distinct -and $timeout.process_terminated -and $timeout.residue -ceq "none")
    structured_failure = ($failure.status -ceq "structured_failure" -and $failure.exit_status -eq 1 -and $failure.process_terminated -and $failure.residue -ceq "none" -and $failureRejected)
    structured_failure_stage = [string]$failure.child_result.setup_stage
    structured_failure_stderr_present = [bool]$failure.stderr_present
    structured_failure_raw_withheld = (-not $failureResultText.Contains("private-stderr-sentinel"))
    stderr_success_rejected = ($stderrSuccess.status -ceq "malformed_output" -and $stderrSuccess.stderr_present -and $stderrSuccess.process_terminated -and $stderrSuccess.residue -ceq "none")
    launch_failed = ($launchFailed.status -ceq "launch_failed" -and -not $launchFailed.process_terminated -and $launchFailed.residue -ceq "none")
    wrong_outcome_rejected = $wrongOutcomeRejected
}
[Console]::Out.WriteLine(($summary | ConvertTo-Json -Compress))
'''
        long_timeout_seconds = _CI7_CHILD_LIFECYCLE_TIMEOUTS_SECONDS[0]
        short_timeout_seconds = _CI7_CHILD_LIFECYCLE_TIMEOUTS_SECONDS[4]
        if lifecycle_script.count("__LONG_TIMEOUT__") != 7 or lifecycle_script.count("__SHORT_TIMEOUT__") != 1:
            raise AssertionError("child lifecycle timeout schedule changed; output withheld")
        lifecycle_script = (
            lifecycle_script.replace("__RUNNER_HELPER__", helper)
            .replace("__RESULT_VALIDATOR__", validator)
            .replace("__VALID_RESULT__", json.dumps(valid_result, separators=(",", ":")))
            .replace("__FAILURE_RESULT__", json.dumps(structured_failure_result, separators=(",", ":")))
            .replace("__LONG_TIMEOUT__", str(long_timeout_seconds * 1000))
            .replace("__SHORT_TIMEOUT__", str(short_timeout_seconds * 1000))
        )
        with tempfile.TemporaryDirectory(prefix="xb-ci7-child-lifecycle-") as temp_dir:
            temp_root = Path(temp_dir)
            _assert_temp_outside_checkout(ROOT, temp_root)
            lifecycle_path = temp_root / "child_lifecycle.ps1"
            lifecycle_path.write_text(lifecycle_script, encoding="utf-8", newline="\n")
            try:
                completed = subprocess.run(
                    [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(lifecycle_path)],
                    cwd=ROOT,
                    env=_windows_powershell_module_environment(),
                    capture_output=True,
                    text=True,
                    timeout=_CI7_CHILD_LIFECYCLE_OUTER_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise AssertionError("child lifecycle regression exceeded its outer bound; output withheld") from None
        if completed.returncode != 0:
            raise AssertionError("child lifecycle regression failed; output withheld")
        try:
            result = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            raise AssertionError("child lifecycle regression emitted an invalid summary; output withheld") from None
        expected_keys = {
            "valid", "malformed", "abnormal", "oversized", "timeout", "structured_failure",
            "structured_failure_stage", "structured_failure_stderr_present", "structured_failure_raw_withheld",
            "stderr_success_rejected", "launch_failed", "wrong_outcome_rejected",
        }
        boolean_keys = expected_keys - {"structured_failure_stage"}
        if (
            not isinstance(result, dict)
            or set(result) != expected_keys
            or any(type(result[key]) is not bool for key in boolean_keys)
            or result["structured_failure_stage"] not in _CI7_CHILD_SETUP_STAGES
        ):
            raise AssertionError("child lifecycle regression emitted an unsafe summary; output withheld")
        if not all(result[key] for key in boolean_keys) or result["structured_failure_stage"] != "PRODUCT_TOKEN_OPEN":
            raise AssertionError("child lifecycle regression did not prove each bounded outcome; output withheld")


class MemberWorkerTaskContractSourceTests(unittest.TestCase):
    """Deterministic source pins for the G3-133 Scheduler oracle and rollback correction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / INSTALLER_PATH).read_text(encoding="utf-8").replace("\r\n", "\n")

    def test_faulty_collection_counts_are_removed(self) -> None:
        self.assertNotIn("@($Task.Triggers).Count", self.source)
        self.assertNotIn("@($Task.Actions).Count", self.source)
        self.assertNotIn("$Task.Actions[0]", self.source)
        self.assertNotIn("XbTaskMayExist", self.source)

    def test_registration_mechanism_is_unchanged(self) -> None:
        register = _installer_function(self.source, "Register-XbWorkerScheduledTask")
        self.assertNotIn("Trigger", register)
        self.assertIn(
            "New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) "
            "-RestartCount 0 -StartWhenAvailable:$false -Disable",
            register,
        )
        self.assertIn("New-ScheduledTaskPrincipal -UserId $WorkerAccount -LogonType Password -RunLevel Limited", register)
        self.assertIn("New-ScheduledTask -Action $action -Settings $settings -Principal $taskPrincipal\n", register)
        self.assertIn("-Mode DisabledProof", register)
        self.assertLess(
            register.index("$script:XbTaskRegistrationAttempted = $true"),
            register.index("Register-ScheduledTask -TaskPath"),
        )
        self.assertIn("trigger_count = 0\n", self.source)

    def test_service_backed_trigger_oracle_is_authoritative(self) -> None:
        oracle = _installer_function(self.source, "Get-XbTaskTriggerOracleCount")
        self.assertIn('"http://schemas.microsoft.com/windows/2004/02/mit/task"', oracle)
        self.assertIn('"/t:Task/t:Triggers/*"', oracle)
        self.assertIn("task_trigger_oracle_disagreement", oracle)
        contract = _installer_function(self.source, "Assert-XbWorkerTaskContract")
        self.assertIn("Get-XbRegisteredWorkerTaskView", contract)
        self.assertIn("-ComTriggerCount ([int]$definition.Triggers.Count) -TaskXml ([string]$registered.Xml)", contract)
        self.assertIn('if ($triggerCount -ne 0) { throw "task_triggers_present" }', contract)
        self.assertIn("[int]$definition.Actions.Count -ne 1", contract)
        self.assertIn("New-Object -ComObject Schedule.Service", _installer_function(self.source, "Connect-XbTaskService"))

    def test_preimage_is_captured_before_first_install_mutation(self) -> None:
        installer = _installer_function(self.source, "Invoke-XbWorkerInstaller")
        self.assertLess(installer.index("$preimage = Get-XbInstallPreimage"), installer.index("New-Item -ItemType Directory -Path $stageRoot"))
        self.assertLess(installer.index('throw "task_preimage_exists"'), installer.index("$preimage = Get-XbInstallPreimage"))
        self.assertIn("Invoke-XbWorkerInstallRollback -Preimage $preimage -StageRoot $stageRoot", installer)

    def test_rollback_container_removal_is_non_recursive(self) -> None:
        container = _installer_function(self.source, "Remove-XbAttemptCreatedContainer")
        self.assertNotIn("-Recurse", container)
        self.assertIn("Remove-Item -LiteralPath $Path -Force -ErrorAction Stop", container)
        self.assertIn("[IO.FileAttributes]::ReparsePoint", container)
        folder = _installer_function(self.source, "Remove-XbAttemptCreatedTaskFolder")
        self.assertIn("GetTasks(1)", folder)
        self.assertIn("GetFolders(0)", folder)

    def test_uninstall_ownership_path_is_unchanged(self) -> None:
        self.assertIn("$null = Assert-XbUninstallOwnership\nRemove-XbWorkerOwnedState -TaskMayExist\n}", self.source)
        owned = _installer_function(self.source, "Remove-XbWorkerOwnedState")
        self.assertIn("if ($TaskMayExist) { Remove-XbWorkerScheduledTask }", owned)

    def test_hosted_guard_requires_exact_github_hosted_windows(self) -> None:
        hosted = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Windows"}
        self.assertTrue(_hosted_windows_boundary_required(hosted))
        for key, value in (
            ("GITHUB_ACTIONS", "false"),
            ("RUNNER_ENVIRONMENT", "self-hosted"),
            ("RUNNER_OS", "Linux"),
            ("RUNNER_OS", "windows"),
        ):
            with self.subTest(key=key, value=value):
                self.assertFalse(_hosted_windows_boundary_required({**hosted, key: value}))
        self.assertFalse(_hosted_windows_boundary_required({}))

    def test_hosted_harness_never_starts_a_task(self) -> None:
        for forbidden in ("Start-ScheduledTask", ".Run(", ".RunEx(", "Enable-ScheduledTask"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, _HOSTED_TASK_BOUNDARY_HARNESS)
                self.assertNotIn(forbidden, self.source)


    def test_hosted_case_record_and_cleanup_errors_are_preserved(self) -> None:
        case_helper = _installer_function(_HOSTED_TASK_BOUNDARY_HARNESS, "Invoke-XbBoundaryCase")
        self.assertLess(
            case_helper.index("$report.cases[$Name] = $record"),
            case_helper.index("$null = & $Body $record"),
        )
        self.assertIn("if ($PreserveIncrementally)", case_helper)
        self.assertIn('$record.status = "error"', case_helper)
        cleanup_helper = _installer_function(_HOSTED_TASK_BOUNDARY_HARNESS, "Invoke-XbBoundaryCleanup")
        for field in ("attempted", "pass", "error", "stack"):
            with self.subTest(cleanup_field=field):
                self.assertIn(f"cleanup.{field}", cleanup_helper)
        self.assertIn("throw", cleanup_helper)

    def test_synthetic_config_teardown_is_identity_gated_before_uninstall(self) -> None:
        start = _HOSTED_TASK_BOUNDARY_HARNESS.index(
            'Invoke-XbBoundaryCase -Name "install_then_uninstall"'
        )
        end = _HOSTED_TASK_BOUNDARY_HARNESS.index(
            '\n} catch {\n    $report.fatal',
            start,
        )
        case_source = _HOSTED_TASK_BOUNDARY_HARNESS[start:end]
        for marker in (
            "identity_matches_created_file",
            "content_matches_expected",
            "[IO.FileAttributes]::ReparsePoint",
            "runtime_after.prerequisite_satisfied",
            "required_probe_completion.status",
            "parent_chain_ordinary_non_reparse",
            "parent_chain_component_count",
            "reparse_snapshot_ancestor_rejected",
            "reparse_snapshot_leaf_rejected",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, case_source)
        self.assertIn('[IO.File]::WriteAllText($configFile, "{}", [Text.UTF8Encoding]::new($false))', case_source)
        self.assertLess(case_source.index("$case.ci7 = $ci7"), case_source.index("Set-XbProductionTaskIdentity"))
        self.assertLess(case_source.index("native_type_absent_before_first_use"), case_source.index("Get-XbSyntheticConfigSnapshot -Path $configFile"))
        self.assertIn("replacement_identity_changed", case_source)
        self.assertIn("wrong_content_exact", case_source)
        teardown_start = case_source.index("$case.synthetic_config_teardown")
        uninstall_start = case_source.index('$script:Operation = "Uninstall"')
        teardown = case_source[teardown_start:uninstall_start]
        self.assertEqual(teardown.count("Remove-Item -LiteralPath $configFile"), 1)
        self.assertNotIn("-Recurse", teardown)
        for prerequisite in (
            "if (-not $configTeardown.runtime_after.prerequisite_satisfied)",
            "if ($null -ne $ci7.fatal)",
            "if ($ci7.required_probe_completion.status -cne \"complete\")",
            "if ($ci7.Contains(\"cleanup_error\"))",
        ):
            with self.subTest(prerequisite=prerequisite):
                self.assertLess(case_source.index(prerequisite), uninstall_start)
        self.assertLess(
            case_source.index("$configTeardown.removal_attempted = $true"),
            uninstall_start,
        )
        self.assertEqual(case_source.count('$script:Operation = "Uninstall"'), 1)
        self.assertEqual(
            case_source.count("Get-XbBoundaryOutcome { Invoke-XbWorkerInstaller }"),
            2,
        )

    def test_synthetic_config_snapshot_initializes_before_open_and_uses_one_held_object(self) -> None:
        snapshot = _installer_function(_HOSTED_TASK_BOUNDARY_HARNESS, "Get-XbSyntheticConfigSnapshot")
        self.assertLess(snapshot.index("Initialize-XbWorkerNativeAccess"), snapshot.index("Get-XbNativePathChain"))
        self.assertLess(snapshot.index("Get-XbNativePathChain"), snapshot.index("[XbWorkerProtectedObject]::Open($fullPath, $false, $true)"))
        self.assertLess(snapshot.index("[XbWorkerProtectedObject]::Open($fullPath, $false, $true)"), snapshot.index("$protected.ReadSnapshot()"))
        self.assertIn("$protected.FileIdentity", snapshot)
        self.assertIn("$protected.Dispose()", snapshot)
        self.assertIn("[IO.FileAttributes]::ReparsePoint", snapshot)
        self.assertNotIn("Add-XbCi7ProtectedLeaf", snapshot)


class MemberWorkerTaskContractBehaviorTests(unittest.TestCase):
    """Local deterministic behavior of the oracle, contract and rollback helpers (no task registration)."""

    report: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        pwsh = _resolve_native_powershell()
        if not pwsh:
            if _hosted_windows_boundary_required():
                raise AssertionError("native Windows PowerShell is required on the hosted runner")
            raise unittest.SkipTest("Windows PowerShell is required for installer behavior validation")
        with tempfile.TemporaryDirectory(prefix="xb-task-contract-") as temp_dir:
            harness = Path(temp_dir) / "task_contract_harness.ps1"
            harness.write_text(_LOCAL_TASK_CONTRACT_HARNESS, encoding="utf-8", newline="\n")
            work_root = Path(temp_dir) / "work"
            work_root.mkdir()
            completed = subprocess.run(
                [
                    pwsh, "-ExecutionPolicy", "Bypass", "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-File", str(harness),
                    "-InstallerPath", str(ROOT / INSTALLER_PATH),
                    "-WorkRoot", str(work_root),
                ],
                cwd=ROOT,
                env=_windows_powershell_module_environment(),
                capture_output=True,
                text=True,
                timeout=600,
            )
        if completed.returncode != 0:
            raise AssertionError(completed.stdout + completed.stderr)
        cls.report = json.loads(completed.stdout)

    def test_in_memory_triggerless_task_pins_null_collision(self) -> None:
        self.assertEqual(
            self.report["in_memory"],
            {"triggers_null": True, "historical_expression_count": 1, "filtered_trigger_count": 0, "filtered_action_count": 1},
        )

    def test_trigger_oracle_agreement_matrix(self) -> None:
        disagreement = "task_trigger_oracle_disagreement"
        self.assertEqual(
            self.report["oracle"],
            {
                "zero": "0",
                "empty_triggers_element": "0",
                "one": "1",
                "two": "2",
                "com_zero_xml_one": disagreement,
                "com_one_xml_zero": disagreement,
                "cim_null_with_one": disagreement,
                "cim_one_with_zero": disagreement,
                "un_namespaced": disagreement,
                "malformed": disagreement,
                "empty": disagreement,
            },
        )

    def test_task_contract_matrix(self) -> None:
        self.assertEqual(
            self.report["contract"],
            {
                "valid_null_triggers": "pass",
                "historical_expression_on_valid": 1,
                "com_enabled": "task_not_disabled",
                "settings_enabled": "task_not_disabled",
                "cim_ready": "task_not_disabled",
                "one_trigger": "task_triggers_present",
                "oracle_disagreement": "task_trigger_oracle_disagreement",
                "cim_actions_null": "task_action_count_invalid",
                "com_two_actions": "task_action_count_invalid",
                "com_action_path": "task_identity_invalid",
                "com_action_type": "task_identity_invalid",
                "task_path_mismatch": "task_identity_invalid",
                "restart_count": "task_retry_contract_invalid",
                "logon_type": "task_identity_invalid",
            },
        )

    def test_scheduler_folder_absence_requires_exact_hresult(self) -> None:
        self.assertEqual(
            self.report["hresult"],
            {"file_not_found": -2147024894, "wrapped_file_not_found": -2147024894, "access_denied": -2147024891},
        )
        self.assertEqual(
            self.report["folder_preimage"],
            {
                "present": "True",
                "file_not_found": "False",
                "access_denied": "task_folder_preimage_unproven",
                "path_not_found": "task_folder_preimage_unproven",
            },
        )
        self.assertEqual(
            self.report["real_folder"],
            {"root_present": True, "random_absent": False, "root_cleanup": "container_not_owned_empty", "absent_cleanup": "pass"},
        )

    def test_attempt_created_container_cleanup_is_owned_empty_only(self) -> None:
        hold = "container_not_owned_empty"
        self.assertEqual(
            self.report["container"],
            {
                "absent": "pass",
                "empty": "pass",
                "empty_removed": True,
                "child": hold,
                "child_preserved": True,
                "hidden_child": hold,
                "hidden_child_preserved": True,
                "nested": hold,
                "nested_preserved": True,
                "junction": hold,
                "junction_preserved": True,
                "junction_target_preserved": True,
                "file": hold,
                "file_preserved": True,
            },
        )

    def test_attempted_registration_task_step(self) -> None:
        self.assertEqual(
            self.report["task_step"],
            {
                "absent": {"outcome": "pass", "unregister_calls": 0},
                "owned": {"outcome": "pass", "unregister_calls": 1},
                "foreign_action": {"outcome": "task_identity_unexpected", "unregister_calls": 0},
                "foreign_principal": {"outcome": "task_identity_unexpected", "unregister_calls": 0},
                "ambiguous": {"outcome": "task_presence_unproven", "unregister_calls": 0},
            },
        )

    def test_rollback_restores_parent_preimage(self) -> None:
        rollback = self.report["rollback"]
        restored = {
            "install_root": False,
            "runtime_root": False,
            "program_files_parent": False,
            "program_data_parent": False,
            "program_files_grandparent": True,
            "program_data_grandparent": True,
            "stage": False,
            "folder_cleanup_calls": 1,
        }
        self.assertEqual(rollback["parents_absent"], {"outcome": "pass", "state": restored})
        self.assertEqual(rollback["not_attempted"], {"outcome": "pass", "state": restored})
        self.assertEqual(
            rollback["parents_preexisting"],
            {
                "outcome": "pass",
                "state": {**restored, "program_files_parent": True, "program_data_parent": True, "folder_cleanup_calls": 0},
                "program_files_sentinel": True,
                "program_data_sentinel": True,
            },
        )
        self.assertEqual(
            rollback["unexpected_content"],
            {
                "outcome": "container_not_owned_empty",
                "state": {**restored, "program_data_parent": True},
                "unexpected_preserved": True,
            },
        )


class MemberWorkerBoundaryCaseRecordRegressionTests(unittest.TestCase):
    """Bounded PowerShell regression for evidence retention when retained cleanup throws."""

    def test_cleanup_exception_preserves_evidence_and_fails_the_case(self) -> None:
        pwsh = _resolve_native_powershell()
        if not pwsh:
            raise unittest.SkipTest("Windows PowerShell is required for case-record regression")

        case_helper = _installer_function(_HOSTED_TASK_BOUNDARY_HARNESS, "Invoke-XbBoundaryCase")
        cleanup_helper = _installer_function(_HOSTED_TASK_BOUNDARY_HARNESS, "Invoke-XbBoundaryCleanup")
        harness_source = "\n".join(
            (
                "$ErrorActionPreference = \"Stop\"",
                "$report = [ordered]@{ cases = [ordered]@{} }",
                case_helper,
                cleanup_helper,
                r"""
$body = {
    param($case)
    $case.install_outcome = "pass"
    $case.installed = [ordered]@{ task_present = $true }
    $case.ci7 = [ordered]@{
        fatal = $null
        required_probe_completion = [ordered]@{ status = "complete" }
    }
    Invoke-XbBoundaryCleanup -CaseRecord $case -Preflight {
        param($case)
        $case.production_folder_before_retained_cleanup = [ordered]@{
            task_count = 0
            child_folder_count = 0
        }
    } -Body {
        throw "forced_cleanup_exception"
    }
}
Invoke-XbBoundaryCase -Name "cleanup_exception" -PreserveIncrementally -Body $body
$case = $report.cases["cleanup_exception"]
if ($case.status -cne "error" -or $case.error -cne "forced_cleanup_exception" -or
    $case.install_outcome -cne "pass" -or $case.installed.task_present -ne $true -or
    $null -ne $case.ci7.fatal -or
    $case.ci7.required_probe_completion.status -cne "complete" -or
    $case.production_folder_before_retained_cleanup.task_count -ne 0 -or
    $case.cleanup.attempted -ne $true -or $case.cleanup.pass -ne $false -or
    $case.cleanup.error -cne "forced_cleanup_exception" -or
    [string]::IsNullOrWhiteSpace([string]$case.cleanup.stack) -or
    [string]::IsNullOrWhiteSpace([string]$case.error_stack)) {
    throw "boundary_case_cleanup_regression_failed"
}
[Console]::Out.WriteLine(($case | ConvertTo-Json -Depth 8 -Compress))
""",
            )
        )
        with tempfile.TemporaryDirectory(prefix="xb-boundary-case-record-") as temp_dir:
            harness = Path(temp_dir) / "case_record_regression.ps1"
            harness.write_text(harness_source, encoding="utf-8", newline="\n")
            completed = subprocess.run(
                [
                    pwsh, "-ExecutionPolicy", "Bypass", "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-File", str(harness),
                ],
                cwd=ROOT,
                env=_windows_powershell_module_environment(),
                capture_output=True,
                text=True,
                timeout=60,
            )
        if completed.returncode != 0:
            raise AssertionError(completed.stdout + completed.stderr)
        case = json.loads(completed.stdout)
        self.assertEqual(case["status"], "error")
        self.assertEqual(case["error"], "forced_cleanup_exception")
        self.assertTrue(case["error_stack"])
        self.assertEqual(case["install_outcome"], "pass")
        self.assertEqual(case["installed"], {"task_present": True})
        self.assertEqual(case["ci7"]["fatal"], None)
        self.assertEqual(case["ci7"]["required_probe_completion"]["status"], "complete")
        self.assertEqual(
            case["production_folder_before_retained_cleanup"],
            {"task_count": 0, "child_folder_count": 0},
        )
        self.assertEqual(
            {key: value for key, value in case["cleanup"].items() if key != "stack"},
            {
                "attempted": True,
                "pass": False,
                "error": "forced_cleanup_exception",
            },
        )
        self.assertTrue(case["cleanup"]["stack"])


class MemberWorkerHostedTaskBoundaryTests(unittest.TestCase):
    """Real Schedule.Service/ScheduledTasks boundary on disposable GitHub-hosted Windows only."""

    report: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        if not _hosted_windows_boundary_required():
            raise unittest.SkipTest(HOSTED_BOUNDARY_SKIP_REASON)
        pwsh = _resolve_native_powershell()
        if not pwsh:
            raise AssertionError("hosted boundary requires native Windows PowerShell 5.1")
        try:
            with tempfile.TemporaryDirectory(prefix="xb-task-boundary-") as temp_dir:
                root = Path(temp_dir)
                manifest = root / "reviewed-package.json"
                manifest.write_text(json.dumps(_reviewed_package_identity(), indent=2) + "\n", encoding="utf-8")
                frozen_context, frozen_blob = _frozen_ci7_context_source()
                frozen_context_path = root / "frozen_ci7_context.ps1"
                frozen_context_path.write_text(frozen_context, encoding="utf-8", newline="\n")
                frozen_child_script_path = root / "frozen_ci7_child.ps1"
                frozen_child_script_path.write_text(_FROZEN_CI7_CHILD_SCRIPT, encoding="utf-8", newline="\n")
                harness = root / "task_boundary_harness.ps1"
                harness.write_text(_HOSTED_TASK_BOUNDARY_HARNESS, encoding="utf-8", newline="\n")
                completed = subprocess.run(
                    [
                        pwsh, "-ExecutionPolicy", "Bypass", "-NoLogo", "-NoProfile", "-NonInteractive",
                        "-File", str(harness),
                        "-InstallerPath", str(ROOT / INSTALLER_PATH),
                        "-ReviewedManifestPath", str(manifest),
                        "-FrozenContextPath", str(frozen_context_path),
                        "-FrozenChildScriptPath", str(frozen_child_script_path),
                        "-FrozenSourceCommit", CI7_DEFECTIVE_BASELINE_COMMIT,
                        "-FrozenInstallerBlob", frozen_blob,
                    ],
                    cwd=ROOT,
                    env=_windows_powershell_module_environment(),
                    capture_output=True,
                    text=True,
                    timeout=1500,
                )
        except (OSError, subprocess.TimeoutExpired):
            raise AssertionError("hosted boundary harness exceeded its bounded execution; output withheld") from None
        try:
            report = _boundary_report(completed.stdout)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise AssertionError("hosted boundary harness emitted an invalid report; output withheld") from None
        if report is None:
            raise AssertionError("hosted boundary harness produced no report; output withheld")
        cls.report = report
        print(TASK_BOUNDARY_MARKER, flush=True)
        print(_public_safe_hosted_failure_summary(report, "HOSTED_BOUNDARY_SAFE_EVIDENCE"), flush=True)

    def _formatMessage(self, msg: str | None, standardMsg: str) -> str:
        return _public_safe_hosted_failure_summary(getattr(self, "report", None))

    def fail(self, msg: str | None = None) -> None:
        raise self.failureException(_public_safe_hosted_failure_summary(getattr(self, "report", None)))

    def subTest(self, msg: str | None = None, **params: object):
        return super().subTest(msg="hosted boundary assertion")

    def _case(self, name: str) -> dict[str, object]:
        case = self.report["cases"][name]
        self.assertEqual(case["status"], "completed", case)
        return case

    def _assert_pristine(self, readback: dict[str, object]) -> None:
        self.assertEqual(
            readback,
            {
                "task_present": False,
                "scheduler_folder_present": False,
                "install_root_present": False,
                "runtime_root_present": False,
                "program_files_parent_present": False,
                "program_data_parent_present": False,
                "new_stage_count": 0,
            },
        )

    def _assert_never_run(self, evidence: dict[str, object]) -> None:
        self.assertEqual(evidence["last_task_result"], SCHED_S_TASK_HAS_NOT_RUN)
        self.assertEqual(evidence["com_last_task_result"], SCHED_S_TASK_HAS_NOT_RUN)
        self.assertEqual(evidence["missed_runs"], 0)
        self.assertEqual(evidence["com_missed_runs"], 0)
        self.assertTrue(evidence["last_run_time"] is None or str(evidence["last_run_time"]).startswith("1999-11-30"), evidence)
        self.assertLess(str(evidence["com_last_run_time"]), "2000", evidence)

    def _assert_exact_disabled_proof(self, evidence: dict[str, object], worker_account: str) -> None:
        self.assertEqual(evidence["cim_state"], "Disabled")
        self.assertEqual(evidence["com_state"], 1)
        self.assertFalse(evidence["com_enabled"])
        self.assertFalse(evidence["com_settings_enabled"])
        self.assertEqual(evidence["cim_action_count"], 1)
        self.assertEqual(evidence["com_action_count"], 1)
        self.assertEqual(evidence["com_action_type"], 0)
        self.assertEqual(evidence["action_execute"], WINDOWS_POWERSHELL)
        self.assertEqual(evidence["com_action_path"], WINDOWS_POWERSHELL)
        self.assertEqual(evidence["com_action_arguments"], evidence["action_arguments"])
        self.assertRegex(str(evidence["action_arguments"]), r"-Mode DisabledProof$")
        self.assertNotRegex(str(evidence["action_arguments"]), r"EnableProduction")
        self.assertEqual(evidence["principal_user_id"], worker_account)
        self.assertEqual(evidence["principal_logon_type"], "Password")
        self.assertEqual(evidence["principal_run_level"], "Limited")
        self.assertEqual(evidence["multiple_instances"], "IgnoreNew")
        self.assertEqual(evidence["execution_time_limit"], "PT10M")
        self.assertEqual(evidence["restart_count"], 0)
        self.assertFalse(evidence["start_when_available"])

    def _assert_zero_triggers(self, evidence: dict[str, object]) -> None:
        self.assertTrue(evidence["cim_triggers_null"])
        self.assertEqual(evidence["historical_expression_count"], 1)
        self.assertEqual(evidence["cim_filtered_trigger_count"], 0)
        self.assertEqual(evidence["com_trigger_count"], 0)
        self.assertEqual(evidence["xml_trigger_count"], 0)

    def _assert_no_worker_session(self, observation: dict[str, object]) -> None:
        self.assertFalse(observation["profile_list_present"], observation)
        self.assertEqual(observation["user_profile_count"], 0, observation)
        self.assertEqual(observation["worker_process_count"], 0, observation)

    def test_native_boundary_environment(self) -> None:
        self.assertIsNone(self.report["fatal"], self.report)
        environment = self.report["environment"]
        self.assertTrue(str(environment["ps_version"]).startswith("5.1."))
        self.assertEqual(environment["ps_edition"], "Desktop")
        self.assertTrue(environment["elevated"])
        self.assertTrue(environment["scheduled_tasks_module"])
        self.assertTrue(environment["schedule_service"])
        self.assertEqual(
            environment["pristine_before"],
            {"program_files_parent": False, "program_data_parent": False, "scheduler_folder": False},
        )
        self.assertFalse(environment["administrators_member"])
        # Installer/task identity must bind to the bare local account, as production does.
        self.assertRegex(str(environment["worker_account"]), r"^xbt[0-9a-f]{12}$")

    def test_no_secret_exposure(self) -> None:
        self.assertEqual(self.report["secret_exposure"], "none")

    def test_in_memory_null_trigger_representation(self) -> None:
        case = self._case("in_memory_null_trigger_pin")
        self.assertTrue(case["triggers_null"])
        self.assertEqual(case["historical_expression_count"], 1)
        self.assertEqual(case["filtered_trigger_count"], 0)

    def test_zero_trigger_candidate_passes_service_oracle(self) -> None:
        case = self._case("zero_trigger_candidate")
        self.assertEqual(case["registration"], "pass")
        self.assertEqual(case["contract"], "pass")
        self.assertEqual(case["oracle_count"], "0")
        self._assert_zero_triggers(case["evidence"])
        self._assert_exact_disabled_proof(case["evidence"], self.report["environment"]["worker_account"])
        self._assert_never_run(case["evidence"])
        self._assert_no_worker_session(case["account_after_registration"])

    def test_one_trigger_control_fails_through_same_oracle(self) -> None:
        candidate = self._case("zero_trigger_candidate")["evidence"]
        case = self._case("one_trigger_control")
        evidence = case["evidence"]
        self.assertEqual(case["in_memory_filtered_trigger_count"], 1)
        self.assertEqual(case["contract"], "task_triggers_present")
        self.assertFalse(evidence["cim_triggers_null"])
        self.assertEqual(evidence["cim_filtered_trigger_count"], 1)
        self.assertEqual(evidence["com_trigger_count"], 1)
        self.assertEqual(evidence["xml_trigger_count"], 1)
        # The historical expression cannot tell the zero-trigger candidate from the one-trigger control.
        self.assertEqual(evidence["historical_expression_count"], 1)
        self.assertEqual(candidate["historical_expression_count"], evidence["historical_expression_count"])
        self._assert_exact_disabled_proof(evidence, self.report["environment"]["worker_account"])
        self._assert_never_run(evidence)

    def test_boundary_tasks_unregistered_never_run(self) -> None:
        case = self._case("boundary_unregister")
        self.assertEqual(case["final_last_task_results"], [SCHED_S_TASK_HAS_NOT_RUN, SCHED_S_TASK_HAS_NOT_RUN])
        self.assertFalse(case["folder_present_after"])
        self._assert_no_worker_session(case["account_after_unregister"])

    def test_registration_failure_rollback_treats_absent_task_as_restored(self) -> None:
        case = self._case("registration_failure_parent_absent")
        self.assertNotEqual(case["install_outcome"], "pass")
        self.assertFalse(str(case["install_outcome"]).startswith("install_rollback_failed"), case)
        self.assertIn("task_step:absent", case["rollback_trace"])
        self._assert_pristine(case["readback"])
        self.assertEqual(case["historical_unconditional_unregister"], "task_unregister_failed")

    def test_post_registration_rollback_removes_task_and_parents(self) -> None:
        case = self._case("forced_post_registration_parent_absent")
        self.assertEqual(case["install_outcome"], "forced_post_registration_failure")
        self.assertEqual(case["rollback_trace"], ["task_step:present", "folder_step:present"])
        self._assert_pristine(case["readback"])

    def test_preexisting_parents_and_sentinels_are_preserved(self) -> None:
        case = self._case("preexisting_parents_sentinel")
        self.assertEqual(case["install_outcome"], "forced_post_registration_failure")
        self.assertEqual(case["rollback_trace"], ["task_step:present"])
        self.assertEqual(
            case["readback"],
            {
                "task_present": False,
                "scheduler_folder_present": True,
                "install_root_present": False,
                "runtime_root_present": False,
                "program_files_parent_present": True,
                "program_data_parent_present": True,
                "new_stage_count": 0,
            },
        )
        self.assertEqual(
            case["parents"],
            [{"children": ["xb-boundary-sentinel.txt"], "sentinel_intact": True}] * 2,
        )
        self.assertEqual(case["scheduler_folder_content"], {"tasks": 0, "folders": 0})
        self._assert_pristine(case["post_cleanup"])

    def test_unexpected_attempt_container_content_holds(self) -> None:
        case = self._case("unexpected_container_content_hold")
        self.assertEqual(case["install_outcome"], "install_rollback_failed: container_not_owned_empty")
        self.assertEqual(case["rollback_trace"], ["folder_step:absent"])
        self.assertTrue(case["unexpected_preserved"])
        self.assertEqual(
            case["readback"],
            {
                "task_present": False,
                "scheduler_folder_present": False,
                "install_root_present": False,
                "runtime_root_present": False,
                "program_files_parent_present": False,
                "program_data_parent_present": True,
                "new_stage_count": 0,
            },
        )
        self._assert_pristine(case["post_cleanup"])

    def test_required_operation_failure_releases_primitive_package_handle(self) -> None:
        ci7 = self._case("install_then_uninstall")["ci7"]
        self.assertEqual(ci7["required_operation_denied"], "effective_rights_missing")
        self.assertTrue(ci7["failure_cleanup_primitive_leaf_opened"])
        self.assertTrue(ci7["failure_cleanup_owner_enumeration_completed"])
        self.assertEqual(ci7["failure_cleanup_lock_owner_count"], 0)
        self.assertFalse(ci7["failure_cleanup_current_harness_owner"])
        self.assertTrue(ci7["failure_cleanup_exclusive_delete_completed"])
        self.assertTrue(ci7["failure_cleanup_exclusive_delete_succeeded"])

    def test_install_then_uninstall(self) -> None:
        self.assertTrue(self.report["account"]["batch_logon_right_assigned"])
        case = self._case("install_then_uninstall")
        install_presence = case["install_task_presence"]
        self.assertEqual(install_presence["effective_task_path"], "\\X-Boundaries\\")
        self.assertEqual(install_presence["effective_task_name"], "AC2 Member Gateway Worker")
        self.assertTrue(install_presence["cim_task_present"])
        self.assertTrue(install_presence["com"]["folder_present"])
        self.assertTrue(install_presence["com"]["task_present"])
        self.assertEqual(install_presence["com"]["task_count"], 1)
        self.assertEqual(install_presence["com"]["child_folder_count"], 0)
        self.assertEqual(case["install_outcome"], "pass")
        self.assertEqual(
            case["installed"],
            {
                "task_present": True,
                "scheduler_folder_present": True,
                "install_root_present": True,
                "runtime_root_present": True,
                "program_files_parent_present": True,
                "program_data_parent_present": True,
                "new_stage_count": 0,
            },
        )
        self.assertEqual(case["contract"], "pass")
        self.assertEqual(case["ownership"], "pass")
        self.assertEqual(case["manifest_trigger_count"], 0)
        snapshot = case["synthetic_config_snapshot"]
        self.assertTrue(snapshot["native_type_absent_before_first_use"], snapshot)
        self.assertTrue(snapshot["native_type_initialized"], snapshot)
        self.assertEqual(snapshot["fixture_bytes_hex"], "7b7d")
        self.assertTrue(snapshot["wrong_content_exact"], snapshot)
        self.assertTrue(snapshot["replacement_identity_changed"], snapshot)
        self._assert_zero_triggers(case["evidence"])
        self._assert_exact_disabled_proof(case["evidence"], self.report["environment"]["worker_account"])
        self._assert_never_run(case["evidence"])
        self._assert_no_worker_session(case["account_installed"])
        ci7 = case["ci7"]
        self.assertIsNone(ci7["fatal"], ci7)
        self.assertEqual(
            ci7["required_probe_completion"],
            {"status": "complete", "completed_through": "retention_delete"},
        )
        self.assertTrue(ci7["root_owner_accepted"], ci7)
        self.assertIn(ci7["root_owner_sid"], {"S-1-5-18", "S-1-5-32-544"})
        self.assertTrue(ci7["root_dacl_protected"])
        self.assertEqual(ci7["root_acl_shape"], "pass")
        self.assertEqual(ci7["token_user_sid"], self.report["environment"]["worker_sid"])
        self.assertEqual(ci7["token_type"], 2)
        self.assertEqual(ci7["token_impersonation_level"], 2)
        self.assertEqual(ci7["logs_root_granted_mask"], "0x001200AB")
        self.assertFalse(ci7["root_write_dac"])
        self.assertFalse(ci7["root_write_owner"])
        self.assertFalse(ci7["root_delete"])
        self.assertFalse(ci7["runtime_root_delete_child"])
        self.assertEqual(ci7["parent_delete_composition"], "pass")
        self.assertTrue(ci7["ps51_enumerated"])
        self.assertEqual(ci7["ps51_content"], "ci7-create|ci7-append")
        self.assertTrue(ci7["child_owner_is_worker"])
        self.assertEqual(ci7["child_owner_rights_shape"], "pass")
        self.assertEqual(ci7["child_owner_rights_ace_count"], 1)
        self.assertEqual(ci7["child_owner_rights_ace_mask"], "0x001301BF")
        self.assertEqual(ci7["child_granted_mask"], "0x001301BF")
        self.assertFalse(ci7["child_write_dac"])
        self.assertFalse(ci7["child_write_owner"])
        self.assertTrue(ci7["child_delete"])
        self.assertEqual(ci7["verify_outcome"], "pass")
        self.assertEqual(ci7["verify_status"], "install_verified")
        self.assertRegex(ci7["verify_release_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(ci7["verify_checks"])
        self.assertTrue(all(value == "pass" for value in ci7["verify_checks"].values()))
        self.assertEqual(ci7["frozen_control_source_commit"], CI7_DEFECTIVE_BASELINE_COMMIT)
        self.assertEqual(ci7["frozen_control_installer_blob"], CI7_DEFECTIVE_INSTALLER_BLOB)
        self.assertTrue(ci7["frozen_control_source_binding"])
        self.assertTrue(ci7["fixture_account_preserved"])
        self.assertTrue(ci7["credential_username_matches_fixture"])
        self.assertEqual(ci7["product_token_open"], "PASS")
        self.assertTrue(ci7["product_token_identity_matches_fixture"])
        self.assertEqual(ci7["frozen_control_child_status"], "completed")
        self.assertEqual(ci7["frozen_control_child_exit_status"], 0)
        self.assertTrue(ci7["frozen_control_child_process_distinct"])
        self.assertTrue(ci7["frozen_control_child_terminated"])
        self.assertEqual(ci7["frozen_control_child_residue"], "none")
        self.assertFalse(ci7["frozen_control_child_stderr_present"])
        self.assertFalse(ci7["frozen_control_parent_function_replaced"])
        self.assertEqual(ci7["drive_root_index0"]["index"], 0)
        self.assertEqual(ci7["drive_root_index0"]["right"], "0x00000004")
        self.assertTrue(ci7["drive_root_index0"]["allowed"], ci7["drive_root_index0"])
        self.assertEqual(ci7["drive_root_index0"]["granted_access"], 0x00000004)
        self.assertEqual(ci7["frozen_defective_context_outcome"], "effective_rights_exceeded")
        self.assertTrue(ci7["drive_root_descriptor_unchanged"])
        self.assertTrue(ci7["drive_root_identity_unchanged"])

        exact_matrix = ci7["exact_root_mutation_matrix"]
        self.assertEqual(len(exact_matrix), 16)
        self.assertEqual({probe["right"] for probe in exact_matrix}, {
            "file_add_file", "file_add_subdirectory", "file_write_ea", "delete_child",
            "file_write_attributes", "delete", "write_dac", "write_owner",
        })
        for probe in exact_matrix:
            with self.subTest(ci7_exact_root=probe):
                self.assertEqual(probe["outcome"], "effective_rights_exceeded")
                self.assertTrue(probe["fixture_allowed"])
                self.assertEqual(probe["fixture_granted_access"], int(probe["mask"], 16))
                self.assertTrue(probe["policy_consulted_fixture"])
        for root in {probe["root"] for probe in exact_matrix}:
            self.assertEqual(sum(probe["root"] == root for probe in exact_matrix), 8)

        scope_out_names = {"file_add_file", "file_add_subdirectory", "file_write_ea", "file_write_attributes"}
        ancestor_scope_out = ci7["ancestor_scoped_out_right_matrix"]
        ancestor_paths = {probe["ancestor"] for probe in ancestor_scope_out}
        self.assertGreater(len(ancestor_paths), 0)
        self.assertEqual({probe["right"] for probe in ancestor_scope_out}, scope_out_names)
        self.assertEqual(
            {(probe["ancestor"], probe["right"]) for probe in ancestor_scope_out},
            set(product(ancestor_paths, scope_out_names)),
        )
        for probe in ancestor_scope_out:
            with self.subTest(ci7_scoped_out_ancestor=probe):
                self.assertEqual(probe["outcome"], "pass")
                self.assertTrue(probe["fixture_allowed"])
                self.assertEqual(probe["fixture_granted_access"], int(probe["mask"], 16))
                self.assertFalse(probe["policy_consulted_fixture"])

        ancestor_denied = ci7["ancestor_denied_right_matrix"]
        retained_ancestor_names = {"delete_child", "delete", "write_dac", "write_owner"}
        self.assertEqual({probe["ancestor"] for probe in ancestor_denied}, ancestor_paths)
        self.assertEqual({probe["right"] for probe in ancestor_denied}, retained_ancestor_names)
        self.assertEqual(
            {(probe["ancestor"], probe["right"]) for probe in ancestor_denied},
            set(product(ancestor_paths, retained_ancestor_names)),
        )
        for probe in ancestor_denied:
            with self.subTest(ci7_retained_ancestor=probe):
                self.assertEqual(probe["outcome"], "effective_rights_exceeded")
                self.assertTrue(probe["fixture_allowed"])
                self.assertEqual(probe["fixture_granted_access"], int(probe["mask"], 16))
                self.assertTrue(probe["policy_consulted_fixture"])

        self.assertTrue(ci7["delete_chain_matrix"])
        for probe in ci7["delete_chain_matrix"]:
            with self.subTest(ci7_delete_chain=probe):
                self.assertEqual(probe["right"], "0x00010000")
                self.assertEqual(probe["outcome"], "effective_rights_exceeded")
                self.assertTrue(probe["fixture_allowed"])
                self.assertEqual(probe["fixture_granted_access"], 0x00010000)
                self.assertTrue(probe["policy_consulted_fixture"])
        self.assertTrue(ci7["delete_child_parent_edge_matrix"])
        for probe in ci7["delete_child_parent_edge_matrix"]:
            with self.subTest(ci7_delete_child_edge=probe):
                self.assertEqual(probe["right"], "0x00000040")
                self.assertEqual(probe["outcome"], "effective_rights_exceeded")
                self.assertTrue(probe["fixture_allowed"])
                self.assertEqual(probe["fixture_granted_access"], 0x00000040)
                self.assertTrue(probe["policy_consulted_fixture"])
        self.assertEqual(ci7["upgrade_outcome"], "upgraded")
        self.assertEqual(ci7["verify_after_upgrade"], "pass")
        self.assertEqual(ci7["empty_config_verify"], "pass")
        self.assertEqual(len(ci7["package_owner_sids"]), 7)
        self.assertTrue(all(owner == "S-1-5-18" for owner in ci7["package_owner_sids"]))
        self.assertIn(ci7["config_owner_sid"], {"S-1-5-18", "S-1-5-32-544"})
        for key in ("missing_credential", "mismatched_credential", "invalid_password", "unsupported_account_profile", "native_access_failure", "handle_native_access_failure", "reparse_fail_closed", "reparse_ancestor_rejected", "reparse_leaf_rejected", "reparse_snapshot_ancestor_rejected", "reparse_snapshot_leaf_rejected", "config_subdirectory_rejected", "conflicting_handle_identity", "unsupported_unc_fail_closed"):
            with self.subTest(ci7=key):
                self.assertEqual(ci7[key], "effective_rights_unproven")
        self.assertEqual(ci7["required_operation_denied"], "effective_rights_missing")
        for key in ("prohibited_write_dac_allow", "prohibited_write_owner_allow", "prohibited_delete_allow", "prohibited_delete_child_allow", "child_write_dac_allow", "child_write_owner_allow", "parent_delete_child_allow"):
            with self.subTest(ci7=key):
                self.assertTrue(ci7[key]["requested_operation_granted"], ci7[key])
                self.assertEqual(ci7[key]["effective_rights"], "effective_rights_exceeded")
        self.assertEqual(len(ci7["positive_leaf_access_matrix"]), 8)
        self.assertTrue(all(probe["granted"] for probe in ci7["positive_leaf_access_matrix"]))
        self.assertEqual(len(ci7["positive_leaf_denial_matrix"]), 56)
        self.assertTrue(all(not probe["granted"] for probe in ci7["positive_leaf_denial_matrix"]))
        self.assertEqual({probe["leaf"] for probe in ci7["positive_leaf_access_matrix"] if probe["class"] == "package"}, set(INSTALLER_PACKAGE_FILES))
        self.assertEqual(len(ci7["leaf_denial_matrix"]), 56)
        for probe in ci7["leaf_denial_matrix"]:
            with self.subTest(ci7_leaf=probe):
                self.assertTrue(probe["requested_operation_granted"], probe)
                self.assertEqual(probe["effective_rights"], "effective_rights_exceeded")
        self.assertEqual(len(ci7["required_access_matrix"]), 14)
        for probe in ci7["required_access_matrix"]:
            with self.subTest(ci7_required_access=probe):
                self.assertTrue(probe["requested_operation_denied"], probe)
                self.assertEqual(probe["effective_rights"], "effective_rights_missing")
        self.assertEqual(len(ci7["owner_denial_matrix"]), 16)
        for probe in ci7["owner_denial_matrix"]:
            with self.subTest(ci7_owner=probe):
                self.assertEqual(probe["outcome"], "effective_rights_exceeded")
        self.assertEqual({probe["owner_kind"] for probe in ci7["owner_denial_matrix"]}, {"worker", "arbitrary_admin"})
        self.assertTrue(ci7["hidden_config_write_granted"])
        self.assertEqual(ci7["hidden_config_write_rejected"], "effective_rights_exceeded")
        self.assertTrue(ci7["config_parent_delete_child_granted"])
        self.assertTrue(ci7["config_child_delete_denied"])
        self.assertEqual(ci7["config_parent_delete_child_composition"], "effective_rights_exceeded")
        self.assertIn(ci7["package_parent_delete_child_leaf"], INSTALLER_PACKAGE_FILES)
        self.assertTrue(ci7["package_parent_delete_child_granted"])
        self.assertTrue(ci7["package_child_delete_denied"])
        self.assertEqual(ci7["package_parent_delete_child_composition"], "effective_rights_exceeded")
        self.assertTrue(ci7["inherited_group_write_granted"])
        self.assertGreater(ci7["inherited_group_write_ace_count"], 0)
        self.assertEqual(ci7["inherited_group_write_rejected"], "effective_rights_exceeded")
        self.assertTrue(ci7["readonly_explicit_control"]["requested_operation_granted"])
        self.assertEqual(ci7["readonly_explicit_control"]["effective_rights"], "pass")
        self.assertGreater(ci7["inherited_readonly_control"]["inherited_worker_read_allow_count"], 0)
        self.assertEqual(ci7["inherited_readonly_control"]["verifier"], "pass")
        for key in ("config_create_file", "config_create_directory", "install_create_file", "install_create_directory"):
            with self.subTest(ci7=key):
                self.assertTrue(ci7[key]["requested_operation_granted"], ci7[key])
                self.assertEqual(ci7[key]["effective_rights"], "effective_rights_exceeded")
        self.assertEqual(len(ci7["held_leaf_matrix"]), 8)
        for probe in ci7["held_leaf_matrix"]:
            with self.subTest(ci7_held_leaf=probe):
                self.assertTrue(probe["rename_blocked"], probe)
                self.assertTrue(probe["replace_blocked"], probe)
                self.assertRegex(probe["identity_before"], r"^[0-9a-f]{8}:[0-9a-f]{16}$")
                self.assertEqual(probe["identity_after"], probe["identity_before"])
        self.assertTrue(ci7["install_parent_delete_child_allow"]["requested_operation_granted"])
        self.assertEqual(ci7["install_parent_delete_child_allow"]["effective_rights"], "effective_rights_exceeded")
        self.assertEqual(ci7["config_subdirectory_rejected"], "effective_rights_unproven")
        self.assertEqual(ci7["conflicting_handle_identity"], "effective_rights_unproven")
        self.assertTrue(ci7["reparse_fixture_absent"])
        self.assertTrue(ci7["retention_delete"])
        self.assertNotIn("cleanup_error", ci7)
        teardown = case["synthetic_config_teardown"]
        self.assertTrue(teardown["present_before"])
        self.assertTrue(teardown["path_matches_expected"])
        self.assertTrue(teardown["file_is_leaf"])
        self.assertFalse(teardown["file_reparse_point"])
        self.assertFalse(teardown["runtime_root_reparse_point"])
        self.assertFalse(teardown["config_directory_reparse_point"])
        self.assertTrue(teardown["ordinary_non_reparse"])
        self.assertTrue(teardown["parent_chain_ordinary_non_reparse"])
        self.assertGreater(teardown["parent_chain_component_count"], 0)
        self.assertTrue(teardown["identity_matches_created_file"])
        self.assertEqual(teardown["expected_file_identity"], teardown["observed_file_identity"])
        self.assertTrue(teardown["content_matches_expected"])
        self.assertTrue(teardown["removal_attempted"])
        self.assertTrue(teardown["removal_pass"])
        self.assertFalse(teardown["present_after"])
        self.assertEqual(teardown["runtime_before"]["root_directory_count"], 4)
        self.assertEqual(teardown["runtime_before"]["root_file_count"], 0)
        self.assertEqual(teardown["runtime_before"]["root_directory_names"], ["config", "logs", "rollback", "secrets"])
        self.assertEqual(
            teardown["runtime_before"]["directory_contents"]["config"]["entry_names"],
            ["worker.config.json"],
        )
        self.assertFalse(teardown["runtime_before"]["prerequisite_satisfied"])
        self.assertTrue(teardown["runtime_after"]["prerequisite_satisfied"])
        self.assertTrue(all(
            directory["entry_count"] == 0
            for directory in teardown["runtime_after"]["directory_contents"].values()
        ))

        uninstall_presence = case["uninstall_task_presence"]
        self.assertEqual(uninstall_presence["effective_task_path"], "\\X-Boundaries\\")
        self.assertEqual(uninstall_presence["effective_task_name"], "AC2 Member Gateway Worker")
        self.assertFalse(uninstall_presence["cim_task_present"])
        self.assertTrue(uninstall_presence["com"]["folder_present"])
        self.assertFalse(uninstall_presence["com"]["task_present"])
        self.assertEqual(
            uninstall_presence["com"]["returned_folder_path"],
            uninstall_presence["com"]["requested_folder_path"],
        )
        self.assertEqual(uninstall_presence["com"]["task_count"], 0)
        self.assertEqual(uninstall_presence["com"]["child_folder_count"], 0)
        self.assertEqual(case["uninstall_outcome"], "pass")
        self.assertEqual(
            case["uninstalled"],
            {
                "task_present": False,
                "scheduler_folder_present": True,
                "install_root_present": False,
                "runtime_root_present": False,
                "program_files_parent_present": True,
                "program_data_parent_present": True,
                "new_stage_count": 0,
            },
        )
        self._assert_no_worker_session(case["account_uninstalled"])
        self.assertEqual(case["rollback_trace"], [])
        self.assertEqual(case["production_folder_before_retained_cleanup"]["task_count"], 0)
        self.assertEqual(case["production_folder_before_retained_cleanup"]["child_folder_count"], 0)
        self.assertTrue(case["retained_parent_state_before_cleanup"]["safe_to_remove"])
        self.assertEqual(case["cleanup"], {"attempted": True, "pass": True, "error": None, "stack": None})
        post_cleanup_presence = case["post_cleanup_task_presence"]
        self.assertFalse(post_cleanup_presence["cim_task_present"])
        self.assertFalse(post_cleanup_presence["com"]["folder_present"])
        self.assertFalse(post_cleanup_presence["com"]["task_present"])
        self._assert_pristine(case["post_cleanup"])

    def test_disposable_state_cleanup_readback(self) -> None:
        cleanup = self.report["cleanup"]
        expected = {
            "boundary_folder_absent": True,
            "production_folder_absent": True,
            "program_files_parent_absent": True,
            "program_data_parent_absent": True,
            "stage_residue_absent": True,
            "lsa_account_object_absent": True,
            "profile_absent": True,
            "user_absent": True,
        }
        if self.report["environment"].get("temp_redirected"):
            expected["temp_redirect_absent"] = True
        self.assertEqual(cleanup, expected)



_RELEASE_INTEGRITY_HARNESS = r'''[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InstallerPath,
    [Parameter(Mandatory)][string]$AdapterPath,
    [Parameter(Mandatory)][string]$ScriptsRoot,
    [Parameter(Mandatory)][string]$WorkRoot
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. $InstallerPath -LibraryOnly
$installerPackage = @($packageFiles)
. $AdapterPath
$out = [ordered]@{}
function Get-XbOutcome {
    param([Parameter(Mandatory)][scriptblock]$Body)
    try { $null = & $Body; return "pass" } catch { return [string]$_.Exception.Message }
}
$out.installer_package = @($installerPackage | Sort-Object)
$out.adapter_package = @($script:XbAc2ReleasePackageFiles | Sort-Object)
$out.installer_release = Get-XbReleaseIdentityFromRoot -Root $ScriptsRoot
$out.adapter_release = Get-XbAc2ReleaseIdentity -PackageRoot $ScriptsRoot

function New-XbPackageCopy {
    param([string]$Name)
    $root = Join-Path $WorkRoot $Name
    New-Item -ItemType Directory -Path $root | Out-Null
    foreach ($file in $installerPackage) { Copy-Item -LiteralPath (Join-Path $ScriptsRoot $file) -Destination (Join-Path $root $file) }
    return $root
}
$clean = New-XbPackageCopy "clean"
$out.ci4_clean = Get-XbOutcome { Assert-XbReleaseContent -Root $clean }
$second = New-XbPackageCopy "second-save"
Add-Content -LiteralPath (Join-Path $second "ac2_member_gateway_worker_lib.ps1") -Value '# $x.SaveMember($y)'
$out.ci4_second_save_site = Get-XbOutcome { Assert-XbReleaseContent -Root $second }
$twoInAdapter = New-XbPackageCopy "two-in-adapter"
Add-Content -LiteralPath (Join-Path $twoInAdapter "ac2_member_gateway_autocount_adapter.ps1") -Value '# $command.SaveMember($entity)'
$out.ci4_two_sites_in_adapter = Get-XbOutcome { Assert-XbReleaseContent -Root $twoInAdapter }
$delete = New-XbPackageCopy "delete"
Add-Content -LiteralPath (Join-Path $delete "ac2_member_create_primitive.ps1") -Value '# DeleteMember'
$out.ci4_delete_present = Get-XbOutcome { Assert-XbReleaseContent -Root $delete }
$cleanupFile = New-XbPackageCopy "cleanup-file"
Copy-Item -LiteralPath (Join-Path $ScriptsRoot "ac2_member_test_cleanup.ps1") -Destination (Join-Path $cleanupFile "ac2_member_test_cleanup.ps1")
$out.ci4_cleanup_script_present = Get-XbOutcome { Assert-XbReleaseContent -Root $cleanupFile }
$uat = New-XbPackageCopy "uat-file"
Set-Content -LiteralPath (Join-Path $uat "ac2_member_create_uat_runner.ps1") -Value "#"
$out.ci4_uat_script_present = Get-XbOutcome { Assert-XbReleaseContent -Root $uat }
$missing = New-XbPackageCopy "missing"
Remove-Item -LiteralPath (Join-Path $missing "ac2_member_create_primitive.ps1")
$out.ci4_missing_file = Get-XbOutcome { Assert-XbReleaseContent -Root $missing }

$identity = [pscustomobject]@{
    source = [pscustomobject]@{ commit = ("a" * 40); tree = ("b" * 40) }
    package_files = @($installerPackage | ForEach-Object { [pscustomobject]@{ name = $_; sha256 = (Get-XbFileSha256 (Join-Path $ScriptsRoot $_)); git_blob = ("c" * 40) } })
}
$manifest = New-XbWorkerInstallationManifest -PackageRoot $ScriptsRoot -ReviewedIdentity $identity -WorkerAccount "XBHOST\xbworker"
$out.manifest_schema = $manifest.schema_version
$out.manifest_release = $manifest.release_sha256
$out.manifest_keys = @($manifest.Keys)
$out.manifest_executable = $manifest.task.executable
$out.manifest_arguments = $manifest.task.arguments

$secure = [Security.SecureString]::new()
"synthetic".ToCharArray() | ForEach-Object { $secure.AppendChar($_) }
$securePath = Join-Path $WorkRoot "secure.clixml"
$secure | Export-Clixml -LiteralPath $securePath
$plainPath = Join-Path $WorkRoot "plain.clixml"
"synthetic" | Export-Clixml -LiteralPath $plainPath
$garbagePath = Join-Path $WorkRoot "garbage.clixml"
Set-Content -LiteralPath $garbagePath -Value "<not xml"
$out.ci6_secure_artifact = (Test-XbSecureStringArtifact -Path $securePath)
$out.ci6_plain_artifact = (Test-XbSecureStringArtifact -Path $plainPath)
$out.ci6_garbage_artifact = (Test-XbSecureStringArtifact -Path $garbagePath)

$script:RuntimeRoot = Join-Path $WorkRoot "runtime"
foreach ($child in @("config", "secrets", "logs", "rollback")) { New-Item -ItemType Directory -Path (Join-Path $RuntimeRoot $child) -Force | Out-Null }
$out.ci6_inherited_acl = Get-XbOutcome { Assert-XbRuntimeCustody }

$script:WorkerAccount = "XBHOST\bworker"
$ci7Password = [Security.SecureString]::new()
$ci7Password.AppendChar('x')
$ci7Credential = New-Object Management.Automation.PSCredential($script:WorkerAccount, $ci7Password)
$ci7WrongName = New-Object Management.Automation.PSCredential("XBHOST\other", $ci7Password)
$out.ci7_missing_credential = Get-XbOutcome { Assert-XbWorkerEffectiveRights }
$out.ci7_mismatched_credential = Get-XbOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $ci7WrongName }
$originalTokenFactory = ${function:New-XbWorkerBatchToken}
Set-Item function:script:New-XbWorkerBatchToken { param([Management.Automation.PSCredential]$Credential) throw "forced_native_token_failure" }
try { $out.ci7_native_token_failure = Get-XbOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $ci7Credential } }
finally { Set-Item function:script:New-XbWorkerBatchToken $originalTokenFactory }
Add-Type -TypeDefinition 'public sealed class XbWorkerBatchToken { public static void Touch() { } }'
$out.ci7_preloaded_native_type = Get-XbOutcome { Initialize-XbWorkerNativeAccess }
$ci7Password.Dispose()
[Console]::Out.Write(($out | ConvertTo-Json -Depth 8 -Compress))
'''


class MemberWorkerReleaseIntegrityTests(unittest.TestCase):
    """CI1-CI7 release integrity (installer binding of the W-G2-149 checks) without admin or task registration."""

    report: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        pwsh = _resolve_native_powershell()
        if not pwsh:
            raise unittest.SkipTest("Windows PowerShell is required for installer behavior validation")
        with tempfile.TemporaryDirectory(prefix="xb-release-integrity-") as temp_dir:
            harness = Path(temp_dir) / "release_integrity_harness.ps1"
            harness.write_text(_RELEASE_INTEGRITY_HARNESS, encoding="utf-8", newline="\n")
            work_root = Path(temp_dir) / "work"
            work_root.mkdir()
            completed = subprocess.run(
                [
                    pwsh, "-ExecutionPolicy", "Bypass", "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-File", str(harness),
                    "-InstallerPath", str(ROOT / INSTALLER_PATH),
                    "-AdapterPath", str(ROOT / "scripts/ac2_member_gateway_autocount_adapter.ps1"),
                    "-ScriptsRoot", str(ROOT / "scripts"),
                    "-WorkRoot", str(work_root),
                ],
                cwd=ROOT,
                env=_windows_powershell_module_environment(),
                capture_output=True,
                text=True,
                timeout=600,
            )
        if completed.returncode != 0:
            raise AssertionError(completed.stdout + completed.stderr)
        cls.report = json.loads(completed.stdout)

    def test_package_membership_and_release_identity_agree(self) -> None:
        self.assertEqual(self.report["installer_package"], sorted(INSTALLER_PACKAGE_FILES))
        self.assertEqual(self.report["adapter_package"], sorted(INSTALLER_PACKAGE_FILES))
        lines = "".join(
            f"{name}:{hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()}\n"
            for name in sorted(INSTALLER_PACKAGE_FILES)
        )
        expected = hashlib.sha256(lines.encode("ascii")).hexdigest()
        self.assertEqual(self.report["installer_release"], expected)
        self.assertEqual(self.report["adapter_release"], expected)
        self.assertEqual(self.report["manifest_release"], expected)

    def test_ci4_release_content(self) -> None:
        self.assertEqual(self.report["ci4_clean"], "pass")
        self.assertEqual(self.report["ci4_second_save_site"], "release_content_save_call_sites_invalid")
        self.assertEqual(self.report["ci4_two_sites_in_adapter"], "release_content_save_call_sites_invalid")
        self.assertEqual(self.report["ci4_delete_present"], "release_content_delete_present")
        self.assertEqual(self.report["ci4_cleanup_script_present"], "release_content_forbidden_file")
        self.assertEqual(self.report["ci4_uat_script_present"], "release_content_forbidden_file")
        self.assertEqual(self.report["ci4_missing_file"], "release_content_file_missing")

    def test_installation_manifest_v2_records_release_and_absolute_interpreter(self) -> None:
        self.assertEqual(self.report["manifest_schema"], "xb.member.gateway.worker.installation.v2")
        self.assertEqual(
            self.report["manifest_keys"],
            ["schema_version", "reviewed_source", "release_sha256", "install_root", "runtime_root", "package_files", "task", "rollback_owned_roots"],
        )
        self.assertEqual(self.report["manifest_executable"], WINDOWS_POWERSHELL)
        self.assertRegex(self.report["manifest_arguments"], r"-Mode DisabledProof$")
        self.assertNotRegex(self.report["manifest_arguments"], r"EnableProduction")

    def test_ci6_custody_and_ci7_fail_closed_wrappers(self) -> None:
        self.assertTrue(self.report["ci6_secure_artifact"])
        self.assertFalse(self.report["ci6_plain_artifact"])
        self.assertFalse(self.report["ci6_garbage_artifact"])
        self.assertEqual(self.report["ci6_inherited_acl"], "runtime_custody_acl_invalid")
        self.assertEqual(self.report["ci7_missing_credential"], "effective_rights_unproven")
        self.assertEqual(self.report["ci7_mismatched_credential"], "effective_rights_unproven")
        self.assertEqual(self.report["ci7_native_token_failure"], "effective_rights_unproven")
        self.assertEqual(self.report["ci7_preloaded_native_type"], "effective_rights_unproven")

    def test_ci7_uses_native_token_accesscheck_and_preserves_ci6_oracle(self) -> None:
        source = (ROOT / INSTALLER_PATH).read_text(encoding="utf-8")
        self.assertNotIn("Test-XbRightsWithin", source)
        ci6 = _installer_function(source, "Assert-XbRuntimeCustody")
        self.assertIn("Get-XbGrantedRights", ci6)
        token = _installer_function(source, "Initialize-XbWorkerNativeAccess")
        for required in ("LogonUserW", "LOGON32_LOGON_BATCH", "DuplicateTokenEx", "SecurityImpersonation", "TokenImpersonation", "AccessCheck", "CreateFileW", "GetSecurityInfo", "FileShareRead", "FileFlagOpenReparsePoint", "SecureStringToGlobalAllocUnicode", "ZeroFreeGlobalAllocUnicode"):
            with self.subTest(required=required):
                self.assertIn(required, token)
        access = _installer_function(source, "Invoke-XbNativeAccessCheck")
        self.assertIn("GetSecurityDescriptorBinaryForm", access)
        self.assertIn("$Token.Check($securityDescriptor, $DesiredAccess)", access)
        for marker in (
            "$packageDeleteCompositionLeaf = [string]$packageFiles[0]",
            "$packageParentWithDeleteChild.AddAccessRule(",
            "$packageLeafWithDeleteDeny.AddAccessRule(",
            '$ci7.package_parent_delete_child_granted = Test-XbNativeAccessAllowed -Path $installRootPath -Token $nativeToken',
            '$ci7.package_child_delete_denied = -not (Test-XbNativeAccessAllowed -Path $packageDeleteCompositionPath -Token $nativeToken',
            '$ci7.package_parent_delete_child_composition = Get-XbBoundaryOutcome { Assert-XbWorkerEffectiveRights -TaskCredential $credential }',
            'Set-Acl -LiteralPath $packageDeleteCompositionPath -AclObject $packageDeleteCompositionLeafOriginal -ErrorAction Stop',
            'Set-Acl -LiteralPath $installRootPath -AclObject $packageDeleteCompositionParentOriginal -ErrorAction Stop',
        ):
            with self.subTest(package_composition=marker):
                self.assertIn(marker, _HOSTED_TASK_BOUNDARY_HARNESS)
        leaf = _installer_function(source, "Add-XbCi7ProtectedLeaf")
        for marker in ("$AllowedMask", "$RequiredMask", "effective_rights_missing", "0x00000002", "0x00000004", "0x00000010", "0x00000100", "0x00010000", "0x00040000", "0x00080000", "OwnerSid", "ReadSnapshot"):
            with self.subTest(marker=marker):
                self.assertIn(marker, leaf)
        self.assertNotIn("Get-Acl", leaf)
        handle_check = _installer_function(source, "Invoke-XbCi7HandleAccessCheck")
        self.assertIn("$Object.Check($Token, $DesiredAccess)", handle_check)
        self.assertIn('throw "effective_rights_unproven"', handle_check)
        context = _installer_function(source, "Open-XbCi7VerificationContext")
        for marker in ('Get-XbCi7DirectoryInventory -Path $InstallRoot', 'Get-XbCi7DirectoryInventory -Path $configRoot', '"installation-manifest.json"', 'foreach ($item in $configInventory.Items)', 'directoryMutationDenials', 'XbWorkerProtectedObject]::Open'):
            with self.subTest(context_marker=marker):
                self.assertIn(marker, context)
        self.assertIn("$ancestorMutationDenials", context)
        self.assertIn("if ($directoryKey -ieq $protectedRootKey)", context)
        for marker in (
            "Invoke-XbCi7AccessResultFixture",
            "$ci7.drive_root_index0",
            "$ci7.frozen_defective_context_outcome",
            "$ci7.exact_root_mutation_matrix",
            "$ci7.ancestor_scoped_out_right_matrix",
            "$ci7.ancestor_denied_right_matrix",
            "$ci7.delete_chain_matrix",
            "$ci7.delete_child_parent_edge_matrix",
        ):
            with self.subTest(ci7_policy_fixture=marker):
                self.assertIn(marker, _HOSTED_TASK_BOUNDARY_HARNESS)
        self.assertNotIn('$configInventory.Names -cnotcontains "worker.config.json"', context)
        effective = _installer_function(source, "Assert-XbCi7DirectoryRights")
        for marker in ("effective_rights_missing", "effective_rights_exceeded", "effective_rights_unproven", "Assert-XbLogsRootAclShape", "Assert-XbOwnerRightsLogFile", "Assert-XbCi7PathDeletionComposition"):
            with self.subTest(marker=marker):
                self.assertIn(marker, effective)
        for marker in ("0x001200A9", "0x00120089"):
            with self.subTest(allowed_directory_mask=marker):
                self.assertIn(marker, effective)
        self.assertIn("0x001200A9", context)
        self.assertIn("0x00120089", context)
        verifier = _installer_function(source, "Invoke-XbInstallVerifier")
        self.assertLess(verifier.index("Open-XbCi7VerificationContext"), verifier.index("Get-XbWorkerTaskIfPresent"))
        self.assertLess(verifier.index("Assert-XbCi7DirectoryRights"), verifier.index("Assert-XbWorkerTaskContract"))
        self.assertLess(verifier.index("Assert-XbRuntimeCustody"), verifier.index("Confirm-XbCi7VerificationContext"))
        self.assertIn("0x001301BF", source)
        self.assertIn("0x001200AB", source)

    def test_installer_binds_ci1_to_ci7_and_upgrade_contract(self) -> None:
        source = (ROOT / INSTALLER_PATH).read_text(encoding="utf-8")
        for label in ("[CI1]", "[CI2]", "[CI3]", "[CI4]", "[CI5]", "[CI6]", "[CI7]"):
            self.assertIn(label, source)
        self.assertIn('[ValidateSet("Install", "Upgrade", "Verify", "Uninstall", "ValidateOnly")]', source)
        upgrade = _installer_function(source, "Invoke-XbWorkerUpgrade")
        self.assertLess(upgrade.index("Assert-XbStagedPackageIdentity"), upgrade.index("Remove-Item -LiteralPath (Join-Path $InstallRoot $name)"))
        self.assertLess(upgrade.index("Assert-XbReleaseContent -Root $stageRoot"), upgrade.index("Remove-Item -LiteralPath (Join-Path $InstallRoot $name)"))
        self.assertIn("Register-XbWorkerScheduledTask", upgrade)
        self.assertIn("Set-XbWorkerInstalledPackageOwners", upgrade)
        for protected in ('"config"', '"secrets"', '"logs"'):
            self.assertNotIn(f"Remove-Item -LiteralPath (Join-Path $RuntimeRoot {protected})", upgrade)
        verifier = _installer_function(source, "Invoke-XbInstallVerifier")
        for step in ("Assert-XbWorkerInstallLayout", "Get-XbReleaseIdentityFromEntries", "Assert-XbReleaseContentText", "Assert-XbCi7DirectoryRights", "Assert-XbWorkerTaskContract", "Assert-XbRuntimeCustody", "Confirm-XbCi7VerificationContext"):
            self.assertIn(step, verifier)
        install = _installer_function(source, "Invoke-XbWorkerInstaller")
        self.assertIn("Set-XbWorkerInstalledPackageOwners", install)
        self.assertNotIn("ac2_member_test_cleanup.ps1", source)


# CI7 handle-bound descriptor checks and parent/ancestor delete composition over
# a real scratch path chain. The caller's token restricted to Everyone (S-1-1-0)
# is a disposable native AccessCheck subject: it needs no privilege or password,
# and on a standard temp chain it holds neither DELETE nor DELETE_CHILD until
# the harness grants one.
_PATH_CHAIN_HARNESS = r'''[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InstallerPath,
    [Parameter(Mandatory)][string]$LeafPath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. $InstallerPath -LibraryOnly
$out = [ordered]@{}
function Get-XbOutcome {
    param([Parameter(Mandatory)][scriptblock]$Body)
    try { $null = & $Body; return "pass" } catch { return [string]$_.Exception.Message }
}
function Get-XbBytesSha256 {
    param([Parameter(Mandatory)][byte[]]$Bytes)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($Bytes)).Replace("-", "").ToLowerInvariant() }
    finally { $algorithm.Dispose() }
}
function Invoke-XbHandleAccessProbe {
    param([Parameter(Mandatory)][byte[]]$Bytes, [Parameter(Mandatory)][uint32]$DesiredAccess)
    try {
        return [pscustomobject]@{ completed = $true; result = $token.Check($Bytes, $DesiredAccess) }
    } catch {
        return [pscustomobject]@{ completed = $false; result = $null }
    }
}

Initialize-XbWorkerNativeAccess
$samplePath = Join-Path $LeafPath "handle-bound-sample.txt"
[IO.File]::WriteAllText($samplePath, "local-safe-f2", [Text.UTF8Encoding]::new($false))
$heldSample = [XbWorkerProtectedObject]::Open($samplePath, $false, $true)
try {
    $sampleSnapshot = $heldSample.ReadSnapshot()
    $out.handle_sample_text = [string]$sampleSnapshot.Text
    $out.handle_sample_sha256 = [string]$sampleSnapshot.Sha256
    $out.handle_sample_identity = [string]$heldSample.FileIdentity
    $out.handle_sample_owner_present = (-not [string]::IsNullOrWhiteSpace([string]$heldSample.OwnerSid))
    try { Set-Content -LiteralPath $samplePath -Value "replace-probe" -ErrorAction Stop; $out.handle_replace_blocked = $false }
    catch { $out.handle_replace_blocked = $true }
    try { Move-Item -LiteralPath $samplePath -Destination ($samplePath + ".renamed") -ErrorAction Stop; $out.handle_rename_blocked = $false }
    catch { $out.handle_rename_blocked = $true }
} finally {
    $heldSample.Dispose()
    if (Test-Path -LiteralPath $samplePath) { Remove-Item -LiteralPath $samplePath -Force -ErrorAction Stop }
    if (Test-Path -LiteralPath ($samplePath + ".renamed")) { Remove-Item -LiteralPath ($samplePath + ".renamed") -Force -ErrorAction Stop }
}

Add-Type -Language CSharp -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public sealed class XbTestAccessResult
{
    public bool Allowed { get; private set; }
    public uint GrantedAccess { get; private set; }
    public XbTestAccessResult(bool allowed, uint granted) { Allowed = allowed; GrantedAccess = granted; }
}

public sealed class XbTestRestrictedToken : IDisposable
{
    [StructLayout(LayoutKind.Sequential)]
    private struct GenericMapping { public uint GenericRead, GenericWrite, GenericExecute, GenericAll; }
    [StructLayout(LayoutKind.Sequential)]
    private struct SidAndAttributes { public IntPtr Sid; public uint Attributes; }

    [DllImport("kernel32.dll")] private static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);
    [DllImport("advapi32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ConvertStringSidToSid(string stringSid, out IntPtr sid);
    [DllImport("advapi32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateRestrictedToken(IntPtr existing, uint flags, uint disableCount, IntPtr sidsToDisable,
        uint deleteCount, IntPtr privilegesToDelete, uint restrictCount, [In] SidAndAttributes[] sidsToRestrict, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DuplicateTokenEx(IntPtr source, uint access, IntPtr attributes, int level, int type, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsTokenRestricted(IntPtr token);
    [DllImport("advapi32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AccessCheck(IntPtr securityDescriptor, IntPtr clientToken, uint desiredAccess,
        ref GenericMapping genericMapping, IntPtr privilegeSet, ref uint privilegeSetLength,
        out uint grantedAccess, [MarshalAs(UnmanagedType.Bool)] out bool accessAllowed);

    private IntPtr token;
    public bool Restricted { get; private set; }

    // Current process token restricted to Everyone: effective access is the intersection
    // of the normal check and an Everyone-only check, at the caller's integrity level.
    public XbTestRestrictedToken()
    {
        IntPtr process = IntPtr.Zero, everyone = IntPtr.Zero, restricted = IntPtr.Zero;
        try
        {
            if (!OpenProcessToken(GetCurrentProcess(), 0x000A, out process)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if (!ConvertStringSidToSid("S-1-1-0", out everyone)) throw new Win32Exception(Marshal.GetLastWin32Error());
            SidAndAttributes[] restrict = new SidAndAttributes[1];
            restrict[0].Sid = everyone;
            if (!CreateRestrictedToken(process, 0, 0, IntPtr.Zero, 0, IntPtr.Zero, 1, restrict, out restricted))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            if (!DuplicateTokenEx(restricted, 0x0008, IntPtr.Zero, 2, 2, out token)) throw new Win32Exception(Marshal.GetLastWin32Error());
            Restricted = IsTokenRestricted(token);
        }
        finally
        {
            if (restricted != IntPtr.Zero) CloseHandle(restricted);
            if (everyone != IntPtr.Zero) LocalFree(everyone);
            if (process != IntPtr.Zero) CloseHandle(process);
        }
    }

    // Same AccessCheck call shape as the installer's XbWorkerBatchToken.Check.
    public XbTestAccessResult Check(byte[] securityDescriptorBytes, uint desiredAccess)
    {
        if (token == IntPtr.Zero || securityDescriptorBytes == null || securityDescriptorBytes.Length < 20)
            throw new InvalidOperationException("access_check_input_unproven");
        IntPtr securityDescriptor = Marshal.AllocHGlobal(securityDescriptorBytes.Length);
        IntPtr privilegeSet = Marshal.AllocHGlobal(256);
        try
        {
            Marshal.Copy(securityDescriptorBytes, 0, securityDescriptor, securityDescriptorBytes.Length);
            GenericMapping mapping = new GenericMapping();
            mapping.GenericRead = 0x00120089;
            mapping.GenericWrite = 0x00120116;
            mapping.GenericExecute = 0x001200A0;
            mapping.GenericAll = 0x001F01FF;
            uint privilegeSetLength = 256;
            uint granted;
            bool allowed;
            if (!AccessCheck(securityDescriptor, token, desiredAccess, ref mapping, privilegeSet, ref privilegeSetLength, out granted, out allowed))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return new XbTestAccessResult(allowed, granted);
        }
        finally
        {
            Marshal.FreeHGlobal(privilegeSet);
            Marshal.FreeHGlobal(securityDescriptor);
        }
    }

    public void Dispose()
    {
        if (token == IntPtr.Zero) return;
        CloseHandle(token);
        token = IntPtr.Zero;
    }
}
"@

# Record every native check Assert-XbPathNotDeleteable reaches, then delegate to the real one.
$script:nativeChecks = New-Object System.Collections.Generic.List[string]
$originalAccessCheck = ${function:Invoke-XbNativeAccessCheck}
Set-Item function:script:Invoke-XbNativeAccessCheck {
    param([string]$Path, $Token, [uint32]$DesiredAccess)
    $script:nativeChecks.Add(("{0}|0x{1:X8}" -f $Path, $DesiredAccess))
    & $originalAccessCheck -Path $Path -Token $Token -DesiredAccess $DesiredAccess
}
function Get-XbRecordedOutcome {
    param([Parameter(Mandatory)][scriptblock]$Body)
    $script:nativeChecks.Clear()
    $outcome = Get-XbOutcome $Body
    return [ordered]@{ outcome = $outcome; checks = @($script:nativeChecks) }
}

$parentPath = Split-Path -Parent $LeafPath
$ancestorPath = Split-Path -Parent $parentPath
$everyoneSid = [Security.Principal.SecurityIdentifier]::new("S-1-1-0")
function Invoke-XbGrantProbe {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][Security.AccessControl.FileSystemRights]$Rights, [Parameter(Mandatory)][uint32]$DesiredAccess)
    $original = Get-Acl -LiteralPath $Path
    try {
        $changed = Get-Acl -LiteralPath $Path
        $changed.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($everyoneSid, $Rights, [Security.AccessControl.AccessControlType]::Allow))
        Set-Acl -LiteralPath $Path -AclObject $changed
        $granted = Test-XbNativeAccessAllowed -Path $Path -Token $token -DesiredAccess $DesiredAccess
        $result = Get-XbRecordedOutcome { Assert-XbPathNotDeleteable -Path $LeafPath -Token $token }
        $result.requested_operation_granted = [bool]$granted
        return $result
    } finally { Set-Acl -LiteralPath $Path -AclObject $original }
}

$direct = Get-XbNativePathChain -Path $LeafPath
$out.direct_chain = @($direct | ForEach-Object { [string]$_ })
$out.direct_count = $direct.Count
$out.direct_element_types = @($direct | ForEach-Object { $_.GetType().FullName } | Sort-Object -Unique)
$wrapped = @(Get-XbNativePathChain -Path $LeafPath)
$out.wrapped_count = $wrapped.Count
$out.wrapped_first_type = $wrapped[0].GetType().FullName

$token = [XbTestRestrictedToken]::new()
try {
    $out.token_restricted = $token.Restricted
    $componentGuard = [XbWorkerProtectedObject].GetMethod(
        "HasUsableSecurityDescriptor",
        ([System.Reflection.BindingFlags]::NonPublic -bor [System.Reflection.BindingFlags]::Static)
    )
    $out.group_completeness_guard_present = ($null -ne $componentGuard)
    $out.incomplete_group_descriptor_rejected = $false
    if ($null -ne $componentGuard) {
        $usableWithoutGroup = [bool]$componentGuard.Invoke(
            $null,
            [object[]]@([uint32]0, [IntPtr]::new(1), [IntPtr]::Zero, [IntPtr]::new(2))
        )
        $out.incomplete_group_descriptor_rejected = -not $usableWithoutGroup
    }

    $descriptorDirectory = Join-Path $LeafPath "descriptor-access"
    $null = New-Item -ItemType Directory -Path $descriptorDirectory -ErrorAction Stop
    $descriptorFile = Join-Path $descriptorDirectory "access-target.txt"
    [IO.File]::WriteAllText($descriptorFile, "local-safe-group-sid", [Text.UTF8Encoding]::new($false))
    $originalDescriptorDirectoryAcl = Get-Acl -LiteralPath $descriptorDirectory
    $originalDescriptorFileAcl = Get-Acl -LiteralPath $descriptorFile
    $descriptorDirectoryObject = $null
    $descriptorFileObject = $null
    try {
        $directoryAcl = Get-Acl -LiteralPath $descriptorDirectory
        $directoryAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $everyoneSid,
            [Security.AccessControl.FileSystemRights]::ListDirectory,
            [Security.AccessControl.AccessControlType]::Allow
        ))
        Set-Acl -LiteralPath $descriptorDirectory -AclObject $directoryAcl -ErrorAction Stop
        $fileAcl = Get-Acl -LiteralPath $descriptorFile
        $fileAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $everyoneSid,
            [Security.AccessControl.FileSystemRights]::ReadData,
            [Security.AccessControl.AccessControlType]::Allow
        ))
        Set-Acl -LiteralPath $descriptorFile -AclObject $fileAcl -ErrorAction Stop

        $descriptorDirectoryObject = [XbWorkerProtectedObject]::Open($descriptorDirectory, $true, $true)
        $descriptorFileObject = [XbWorkerProtectedObject]::Open($descriptorFile, $false, $true)
        $directoryBytes = $descriptorDirectoryObject.GetSecurityDescriptorBytes()
        $fileBytes = $descriptorFileObject.GetSecurityDescriptorBytes()
        $directorySecurity = [Security.AccessControl.RawSecurityDescriptor]::new($directoryBytes, 0)
        $fileSecurity = [Security.AccessControl.RawSecurityDescriptor]::new($fileBytes, 0)
        $out.handle_directory_owner_present = ($null -ne $directorySecurity.Owner)
        $out.handle_directory_group_present = ($null -ne $directorySecurity.Group)
        $out.handle_file_owner_present = ($null -ne $fileSecurity.Owner)
        $out.handle_file_group_present = ($null -ne $fileSecurity.Group)
        $out.handle_directory_descriptor_matches_hash =
            ((Get-XbBytesSha256 -Bytes $directoryBytes) -ceq [string]$descriptorDirectoryObject.SecurityDescriptorSha256)
        $out.handle_file_descriptor_matches_hash =
            ((Get-XbBytesSha256 -Bytes $fileBytes) -ceq [string]$descriptorFileObject.SecurityDescriptorSha256)

        $directoryAccess = Invoke-XbHandleAccessProbe -Bytes $directoryBytes -DesiredAccess ([uint32]0x00000001)
        $out.handle_directory_accesscheck_completed = [bool]$directoryAccess.completed
        $out.handle_directory_list_access_allowed =
            [bool]$directoryAccess.completed -and
            $null -ne $directoryAccess.result -and
            [bool]$directoryAccess.result.Allowed -and
            (($directoryAccess.result.GrantedAccess -band [uint32]0x00000001) -ne 0)
        $fileAccess = Invoke-XbHandleAccessProbe -Bytes $fileBytes -DesiredAccess ([uint32]0x00000001)
        $out.handle_file_read_accesscheck_completed = [bool]$fileAccess.completed
        $out.handle_file_read_access_allowed =
            [bool]$fileAccess.completed -and
            $null -ne $fileAccess.result -and
            [bool]$fileAccess.result.Allowed -and
            (($fileAccess.result.GrantedAccess -band [uint32]0x00000001) -ne 0)
        $fileDelete = Invoke-XbHandleAccessProbe -Bytes $fileBytes -DesiredAccess ([uint32]0x00010000)
        $out.handle_file_delete_accesscheck_completed = [bool]$fileDelete.completed
        $out.handle_file_delete_access_denied =
            [bool]$fileDelete.completed -and $null -ne $fileDelete.result -and -not [bool]$fileDelete.result.Allowed
        $maximumAccess = Invoke-XbHandleAccessProbe -Bytes $fileBytes -DesiredAccess ([uint32]0x02000000)
        $out.handle_file_maximum_accesscheck_completed = [bool]$maximumAccess.completed
        $out.handle_file_maximum_access_usable =
            [bool]$maximumAccess.completed -and
            $null -ne $maximumAccess.result -and
            [bool]$maximumAccess.result.Allowed -and
            (($maximumAccess.result.GrantedAccess -band [uint32]0x00000001) -ne 0) -and
            (($maximumAccess.result.GrantedAccess -band [uint32]0x00010000) -eq 0)
    } finally {
        if ($null -ne $descriptorFileObject) { $descriptorFileObject.Dispose() }
        if ($null -ne $descriptorDirectoryObject) { $descriptorDirectoryObject.Dispose() }
        Set-Acl -LiteralPath $descriptorFile -AclObject $originalDescriptorFileAcl -ErrorAction Stop
        Set-Acl -LiteralPath $descriptorDirectory -AclObject $originalDescriptorDirectoryAcl -ErrorAction Stop
        if (Test-Path -LiteralPath $descriptorFile) { Remove-Item -LiteralPath $descriptorFile -Force -ErrorAction Stop }
        if (Test-Path -LiteralPath $descriptorDirectory) { Remove-Item -LiteralPath $descriptorDirectory -Force -ErrorAction Stop }
    }

    $out.pass_case = Get-XbRecordedOutcome { Assert-XbPathNotDeleteable -Path $LeafPath -Token $token }
    $out.leaf_delete = Invoke-XbGrantProbe -Path $LeafPath -Rights ([Security.AccessControl.FileSystemRights]::Delete) -DesiredAccess ([uint32]0x00010000)
    $out.parent_delete_child = Invoke-XbGrantProbe -Path $parentPath -Rights ([Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles) -DesiredAccess ([uint32]0x00000040)
    $out.ancestor_delete = Invoke-XbGrantProbe -Path $ancestorPath -Rights ([Security.AccessControl.FileSystemRights]::Delete) -DesiredAccess ([uint32]0x00010000)
    $out.restored_pass = (Get-XbRecordedOutcome { Assert-XbPathNotDeleteable -Path $LeafPath -Token $token }).outcome

    $throwingToken = [pscustomobject]@{}
    $throwingToken | Add-Member -MemberType ScriptMethod -Name Check -Value { param($Descriptor, $Access) throw "forced_native_check_failure" }
    $out.forced_native_failure = Get-XbRecordedOutcome { Assert-XbPathNotDeleteable -Path $LeafPath -Token $throwingToken }
    $out.missing_path = Get-XbRecordedOutcome { Assert-XbPathNotDeleteable -Path (Join-Path $LeafPath "absent") -Token $token }

    $originalChain = ${function:Get-XbNativePathChain}
    try {
        Set-Item function:script:Get-XbNativePathChain { param([string]$Path) return ,@(,@("C:\", "C:\Windows")) }
        $out.nested_chain_guard = Get-XbRecordedOutcome { Assert-XbPathNotDeleteable -Path $LeafPath -Token $token }
        Set-Item function:script:Get-XbNativePathChain { param([string]$Path) return ,@("C:\", "") }
        $out.empty_element_guard = Get-XbRecordedOutcome { Assert-XbPathNotDeleteable -Path $LeafPath -Token $token }
    } finally { Set-Item function:script:Get-XbNativePathChain $originalChain }
} finally { $token.Dispose() }
$out.disposed_token = Get-XbRecordedOutcome { Assert-XbPathNotDeleteable -Path $LeafPath -Token $token }
[Console]::Out.Write(($out | ConvertTo-Json -Depth 8 -Compress))
'''


class MemberWorkerInstalledManifestParserTests(unittest.TestCase):
    """Pure installed-manifest parsing and exact v1/v2 contract regression matrix."""

    @classmethod
    def setUpClass(cls) -> None:
        pwsh = _resolve_native_powershell()
        if not pwsh:
            if _hosted_windows_boundary_required():
                raise AssertionError("native Windows PowerShell is required on the hosted runner")
            raise unittest.SkipTest("Windows PowerShell is required for manifest parser behavior validation")
        script = r'''[CmdletBinding()]
param([Parameter(Mandatory)][string]$InstallerPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. $InstallerPath -LibraryOnly

$out = [ordered]@{
    parser_available = ($null -ne (Get-Command -Name ConvertFrom-XbInstalledManifestText -CommandType Function -ErrorAction SilentlyContinue))
    native_type_absent_before = ($null -eq ("XbWorkerProtectedObject" -as [type]))
    cases = [ordered]@{}
}

function New-XbTestManifest {
    param([switch]$Previous)
    $names = [string[]]@(
        "ac2_member_gateway_worker.ps1",
        "ac2_member_gateway_worker_lib.ps1",
        "ac2_member_gateway_autocount_adapter.ps1",
        "launch_ac2_member_gateway_worker.ps1",
        "test_ac2_member_gateway_autocount_dependencies.ps1",
        "ac2_member_create_primitive.ps1"
    )
    if ($Previous) { $names = [string[]]@($names | Where-Object { $_ -cne "ac2_member_create_primitive.ps1" }) }
    $entries = @(
        foreach ($name in $names) { [ordered]@{ name = $name; sha256 = ("a" * 64) } }
    )
    $releaseEntries = [ordered]@{}
    foreach ($entry in $entries) { $releaseEntries[[string]$entry.name] = [string]$entry.sha256 }
    $launcher = "C:\Program Files\X-Boundaries\MemberGatewayWorker\launch_ac2_member_gateway_worker.ps1"
    $task = [ordered]@{
        path = "\X-Boundaries\"
        name = "AC2 Member Gateway Worker"
        enabled = $false
        trigger_count = 0
        action_mode = "DisabledProof"
        production_switches = @()
        multiple_instances = "IgnoreNew"
        execution_time_limit = "PT10M"
        restart_count = 0
        start_when_available = $false
        executable = if ($Previous) { "powershell.exe" } else { "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" }
        launcher_path = $launcher
        arguments = ('-NoLogo -NoProfile -NonInteractive -File "{0}" -Mode DisabledProof' -f $launcher)
        working_directory = ""
        principal_user_id = "xb-test-worker"
        principal_logon_type = "Password"
        principal_run_level = "Limited"
    }
    if ($Previous) {
        return [ordered]@{
            schema_version = "xb.member.gateway.worker.installation.v1"
            reviewed_source = [ordered]@{ commit = ("a" * 40); tree = ("b" * 40) }
            install_root = "C:\Program Files\X-Boundaries\MemberGatewayWorker\"
            runtime_root = "C:\ProgramData\X-Boundaries\MemberGatewayWorker\"
            package_files = $entries
            task = $task
            rollback_owned_roots = @("config", "secrets", "logs", "rollback")
        }
    }
    return [ordered]@{
        schema_version = "xb.member.gateway.worker.installation.v2"
        reviewed_source = [ordered]@{ commit = ("a" * 40); tree = ("b" * 40) }
        release_sha256 = Get-XbReleaseIdentityFromEntries -Entries $releaseEntries
        install_root = "C:\Program Files\X-Boundaries\MemberGatewayWorker\"
        runtime_root = "C:\ProgramData\X-Boundaries\MemberGatewayWorker\"
        package_files = $entries
        task = $task
        rollback_owned_roots = @("config", "secrets", "logs", "rollback")
    }
}

function Copy-XbTestValue {
    param($Value)
    return (ConvertFrom-Json -InputObject (ConvertTo-Json -InputObject $Value -Depth 30 -Compress) -ErrorAction Stop)
}

function Get-XbManifestOutcome {
    param([AllowEmptyString()][string]$Text, [switch]$AllowPrevious)
    try {
        $null = ConvertFrom-XbInstalledManifestText -Text $Text -AllowPrevious:$AllowPrevious
        return "pass"
    } catch { return [string]$_.Exception.Message }
}

function Add-XbJsonCase {
    param([string]$Name, [AllowEmptyString()][string]$Text, [switch]$AllowPrevious)
    $out.cases[$Name] = Get-XbManifestOutcome -Text $Text -AllowPrevious:$AllowPrevious
}

function Add-XbObjectCase {
    param([string]$Name, $Value, [switch]$AllowPrevious)
    Add-XbJsonCase -Name $Name -Text (ConvertTo-Json -InputObject $Value -Depth 30 -Compress) -AllowPrevious:$AllowPrevious
}

$valid = New-XbTestManifest
$previous = New-XbTestManifest -Previous
Add-XbObjectCase "valid_v2" $valid
Add-XbObjectCase "v1_with_previous" $previous -AllowPrevious
Add-XbObjectCase "v1_default" $previous
Add-XbJsonCase "empty_text" ""
Add-XbJsonCase "malformed_json" "{"
Add-XbJsonCase "null_root" "null"
Add-XbJsonCase "scalar_root" "7"
Add-XbJsonCase "array_root" "[]"

$reviewed = [pscustomobject]@{
    source = [pscustomobject]@{ commit = ("c" * 40); tree = ("d" * 40) }
    package_files = @(
        foreach ($name in @(
            "ac2_member_gateway_worker.ps1",
            "ac2_member_gateway_worker_lib.ps1",
            "ac2_member_gateway_autocount_adapter.ps1",
            "launch_ac2_member_gateway_worker.ps1",
            "test_ac2_member_gateway_autocount_dependencies.ps1",
            "ac2_member_create_primitive.ps1"
        )) { [pscustomobject]@{ name = $name; sha256 = ("e" * 64) } }
    )
}
$produced = New-XbWorkerInstallationManifest -PackageRoot "unused" -ReviewedIdentity $reviewed -WorkerAccount "xb-test-worker"
Add-XbJsonCase "producer_round_trip" (ConvertTo-Json -InputObject $produced -Depth 30 -Compress)
$out.producer_release_sha256 = [string]$produced.release_sha256

$bad = Copy-XbTestValue $valid
$null = $bad.PSObject.Properties.Remove("runtime_root")
Add-XbObjectCase "top_missing" $bad
$bad = Copy-XbTestValue $valid
Add-Member -InputObject $bad -NotePropertyName "ignored" -NotePropertyValue $true
Add-XbObjectCase "top_extra" $bad
$bad = Copy-XbTestValue $valid
$schemaValue = [string]$bad.schema_version
$null = $bad.PSObject.Properties.Remove("schema_version")
Add-Member -InputObject $bad -NotePropertyName "Schema_version" -NotePropertyValue $schemaValue
Add-XbObjectCase "top_case_changed" $bad
$bad = Copy-XbTestValue $valid
$bad.schema_version = "xb.member.gateway.worker.installation.v9"
Add-XbObjectCase "schema_unsupported" $bad
$bad = Copy-XbTestValue $valid
$bad.schema_version = 2
Add-XbObjectCase "schema_wrong_type" $bad
$bad = Copy-XbTestValue $valid
$null = $bad.PSObject.Properties.Remove("release_sha256")
Add-XbObjectCase "v2_release_missing" $bad
$bad = Copy-XbTestValue $previous
Add-Member -InputObject $bad -NotePropertyName "release_sha256" -NotePropertyValue ("a" * 64)
Add-XbObjectCase "v1_release_extra" $bad -AllowPrevious

$bad = Copy-XbTestValue $valid
$bad.reviewed_source = "source"
Add-XbObjectCase "source_wrong_type" $bad
$bad = Copy-XbTestValue $valid
$null = $bad.reviewed_source.PSObject.Properties.Remove("tree")
Add-XbObjectCase "source_missing" $bad
$bad = Copy-XbTestValue $valid
Add-Member -InputObject $bad.reviewed_source -NotePropertyName "extra" -NotePropertyValue 1
Add-XbObjectCase "source_extra" $bad
$bad = Copy-XbTestValue $valid
$treeValue = [string]$bad.reviewed_source.tree
$null = $bad.reviewed_source.PSObject.Properties.Remove("tree")
Add-Member -InputObject $bad.reviewed_source -NotePropertyName "Tree" -NotePropertyValue $treeValue
Add-XbObjectCase "source_case_changed" $bad
foreach ($sourceCase in @(
    @{ name = "commit_upper"; field = "commit"; value = ("A" * 40) },
    @{ name = "commit_length"; field = "commit"; value = ("a" * 39) },
    @{ name = "commit_nonhex"; field = "commit"; value = (("a" * 39) + "g") },
    @{ name = "commit_wrong_type"; field = "commit"; value = 7 },
    @{ name = "tree_upper"; field = "tree"; value = ("B" * 40) },
    @{ name = "tree_length"; field = "tree"; value = ("b" * 41) },
    @{ name = "tree_nonhex"; field = "tree"; value = (("b" * 39) + "g") },
    @{ name = "tree_wrong_type"; field = "tree"; value = $true }
)) {
    $bad = Copy-XbTestValue $valid
    $bad.reviewed_source.PSObject.Properties[[string]$sourceCase.field].Value = $sourceCase.value
    Add-XbObjectCase ("source_" + [string]$sourceCase.name) $bad
}

$rootVariants = @(
    @{ name = "case"; value = "C:\Program Files\x-Boundaries\MemberGatewayWorker\" },
    @{ name = "drive"; value = "D:\Program Files\X-Boundaries\MemberGatewayWorker\" },
    @{ name = "relative"; value = "MemberGatewayWorker\" },
    @{ name = "traversal"; value = "C:\Program Files\X-Boundaries\MemberGatewayWorker\..\MemberGatewayWorker\" },
    @{ name = "unc"; value = "\\server\share\MemberGatewayWorker\" },
    @{ name = "device"; value = "\\?\C:\Program Files\X-Boundaries\MemberGatewayWorker\" },
    @{ name = "stream"; value = "C:\Program Files\X-Boundaries\MemberGatewayWorker:stream\" },
    @{ name = "missing_trailing"; value = "C:\Program Files\X-Boundaries\MemberGatewayWorker" }
)
foreach ($rootProperty in @("install_root", "runtime_root")) {
    foreach ($variant in $rootVariants) {
        $bad = Copy-XbTestValue $valid
        $bad.PSObject.Properties[$rootProperty].Value = [string]$variant.value
        Add-XbObjectCase ("root_" + $rootProperty + "_" + [string]$variant.name) $bad
    }
    $bad = Copy-XbTestValue $valid
    $bad.PSObject.Properties[$rootProperty].Value = 7
    Add-XbObjectCase ("root_" + $rootProperty + "_wrong_type") $bad
}

$bad = Copy-XbTestValue $valid
$bad.package_files = $null
Add-XbObjectCase "package_null" $bad
$bad = Copy-XbTestValue $valid
$bad.package_files = "files"
Add-XbObjectCase "package_wrong_type" $bad
$bad = Copy-XbTestValue $valid
$bad.package_files = @($bad.package_files[1..($bad.package_files.Length - 1)])
Add-XbObjectCase "package_missing" $bad
$bad = Copy-XbTestValue $valid
$bad.package_files[$bad.package_files.Length - 1].name = [string]$bad.package_files[0].name
Add-XbObjectCase "package_duplicate" $bad
$bad = Copy-XbTestValue $valid
$bad.package_files[0].name = "unknown.ps1"
Add-XbObjectCase "package_unknown" $bad
$bad = Copy-XbTestValue $valid
$bad.package_files[0].name = "..\worker.ps1"
Add-XbObjectCase "package_invalid_filename" $bad
$bad = Copy-XbTestValue $valid
$bad.package_files[0].name = 7
Add-XbObjectCase "package_name_wrong_type" $bad
$bad = Copy-XbTestValue $valid
$null = $bad.package_files[0].PSObject.Properties.Remove("sha256")
Add-XbObjectCase "package_entry_missing" $bad
$bad = Copy-XbTestValue $valid
Add-Member -InputObject $bad.package_files[0] -NotePropertyName "extra" -NotePropertyValue 1
Add-XbObjectCase "package_entry_extra" $bad
$bad = Copy-XbTestValue $valid
$digestValue = [string]$bad.package_files[0].sha256
$null = $bad.package_files[0].PSObject.Properties.Remove("sha256")
Add-Member -InputObject $bad.package_files[0] -NotePropertyName "SHA256" -NotePropertyValue $digestValue
Add-XbObjectCase "package_entry_case_changed" $bad
foreach ($hashCase in @(
    @{ name = "null"; value = $null },
    @{ name = "wrong_type"; value = 7 },
    @{ name = "short"; value = ("a" * 63) },
    @{ name = "uppercase"; value = ("A" * 64) },
    @{ name = "nonhex"; value = (("a" * 63) + "g") }
)) {
    $bad = Copy-XbTestValue $valid
    $bad.package_files[0].sha256 = $hashCase.value
    Add-XbObjectCase ("package_hash_" + [string]$hashCase.name) $bad
}
$bad = Copy-XbTestValue $previous
$bad.package_files = @($bad.package_files) + @([ordered]@{ name = "ac2_member_create_primitive.ps1"; sha256 = ("a" * 64) })
Add-XbObjectCase "v1_six_file_package" $bad -AllowPrevious
$bad = Copy-XbTestValue $valid
$bad.package_files = @($bad.package_files[0..4])
Add-XbObjectCase "v2_five_file_package" $bad

foreach ($releaseCase in @(
    @{ name = "null"; value = $null },
    @{ name = "wrong_type"; value = 7 },
    @{ name = "short"; value = ("a" * 63) },
    @{ name = "uppercase"; value = ("A" * 64) },
    @{ name = "nonhex"; value = (("a" * 63) + "g") }
)) {
    $bad = Copy-XbTestValue $valid
    $bad.release_sha256 = $releaseCase.value
    Add-XbObjectCase ("release_format_" + [string]$releaseCase.name) $bad
}
$bad = Copy-XbTestValue $valid
$bad.release_sha256 = ("b" * 64)
Add-XbObjectCase "release_mismatch" $bad

$bad = Copy-XbTestValue $valid
$bad.task = $null
Add-XbObjectCase "task_null" $bad
$bad = Copy-XbTestValue $valid
$bad.task = @()
Add-XbObjectCase "task_array" $bad
$bad = Copy-XbTestValue $valid
$null = $bad.task.PSObject.Properties.Remove("action_mode")
Add-XbObjectCase "task_missing" $bad
$bad = Copy-XbTestValue $valid
Add-Member -InputObject $bad.task -NotePropertyName "extra" -NotePropertyValue 1
Add-XbObjectCase "task_extra" $bad
$bad = Copy-XbTestValue $valid
$actionValue = [string]$bad.task.action_mode
$null = $bad.task.PSObject.Properties.Remove("action_mode")
Add-Member -InputObject $bad.task -NotePropertyName "Action_mode" -NotePropertyValue $actionValue
Add-XbObjectCase "task_case_changed" $bad
foreach ($taskCase in @(
    @{ name = "path"; value = "\X-Boundaries" },
    @{ name = "name"; value = "Other Task" },
    @{ name = "enabled"; value = $true },
    @{ name = "trigger_count"; value = 1 },
    @{ name = "action_mode"; value = "Production" },
    @{ name = "production_switches"; value = @("EnableProduction") },
    @{ name = "multiple_instances"; value = "Parallel" },
    @{ name = "execution_time_limit"; value = "PT11M" },
    @{ name = "restart_count"; value = 1 },
    @{ name = "start_when_available"; value = $true },
    @{ name = "executable"; value = "powershell.exe" },
    @{ name = "launcher_path"; value = "C:\other\launch.ps1" },
    @{ name = "arguments"; value = "-File other.ps1" },
    @{ name = "working_directory"; value = "C:\work" },
    @{ name = "principal_user_id"; value = " " },
    @{ name = "principal_logon_type"; value = "Interactive" },
    @{ name = "principal_run_level"; value = "Highest" }
)) {
    $bad = Copy-XbTestValue $valid
    $bad.task.PSObject.Properties[[string]$taskCase.name].Value = $taskCase.value
    Add-XbObjectCase ("task_value_" + [string]$taskCase.name) $bad
}
foreach ($taskType in @(
    @{ name = "path"; value = 7 },
    @{ name = "name"; value = $true },
    @{ name = "enabled"; value = "false" },
    @{ name = "trigger_count"; value = "0" },
    @{ name = "action_mode"; value = 7 },
    @{ name = "production_switches"; value = "none" },
    @{ name = "multiple_instances"; value = 7 },
    @{ name = "execution_time_limit"; value = 10.0 },
    @{ name = "restart_count"; value = $false },
    @{ name = "start_when_available"; value = 0 },
    @{ name = "executable"; value = 7 },
    @{ name = "launcher_path"; value = $false },
    @{ name = "arguments"; value = 7 },
    @{ name = "working_directory"; value = $null },
    @{ name = "principal_user_id"; value = $null },
    @{ name = "principal_logon_type"; value = 7 },
    @{ name = "principal_run_level"; value = @() }
)) {
    $bad = Copy-XbTestValue $valid
    $bad.task.PSObject.Properties[[string]$taskType.name].Value = $taskType.value
    Add-XbObjectCase ("task_type_" + [string]$taskType.name) $bad
}
$validJson = ConvertTo-Json -InputObject $valid -Depth 30 -Compress
Add-XbJsonCase "task_type_trigger_count_decimal" $validJson.Replace('"trigger_count":0,', '"trigger_count":0.0,')
Add-XbJsonCase "task_type_restart_count_decimal" $validJson.Replace('"restart_count":0,', '"restart_count":0.0,')
$bad = Copy-XbTestValue $previous
$bad.task.executable = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
Add-XbObjectCase "v1_task_executable_mismatch" $bad -AllowPrevious

$bad = Copy-XbTestValue $valid
$bad.rollback_owned_roots = @("config", "secrets", "logs")
Add-XbObjectCase "roots_missing" $bad
$bad = Copy-XbTestValue $valid
$bad.rollback_owned_roots[3] = "config"
Add-XbObjectCase "roots_duplicate" $bad
$bad = Copy-XbTestValue $valid
$bad.rollback_owned_roots[3] = "other"
Add-XbObjectCase "roots_unknown" $bad
$bad = Copy-XbTestValue $valid
$bad.rollback_owned_roots = "config,secrets,logs,rollback"
Add-XbObjectCase "roots_wrong_type" $bad
$bad = Copy-XbTestValue $valid
$bad.rollback_owned_roots[0] = 7
Add-XbObjectCase "roots_entry_wrong_type" $bad
$bad = Copy-XbTestValue $valid
$bad.rollback_owned_roots[0] = "Config"
Add-XbObjectCase "roots_case_changed" $bad

$out.native_type_absent_after = ($null -eq ("XbWorkerProtectedObject" -as [type]))
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $out -Depth 30 -Compress))
'''
        with tempfile.TemporaryDirectory(prefix="xb-installed-manifest-") as temp_dir:
            harness = Path(temp_dir) / "manifest_parser_harness.ps1"
            harness.write_text(script, encoding="utf-8", newline="\n")
            completed = subprocess.run(
                [
                    pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(harness),
                    "-InstallerPath",
                    str(ROOT / INSTALLER_PATH),
                ],
                cwd=ROOT,
                env=_windows_powershell_module_environment(),
                capture_output=True,
                text=True,
                timeout=120,
            )
        if completed.returncode != 0:
            raise AssertionError(completed.stdout + completed.stderr)
        cls.report = json.loads(completed.stdout)

    def assert_outcomes(self, names: tuple[str, ...], expected: str) -> None:
        for name in names:
            with self.subTest(case=name):
                self.assertEqual(self.report["cases"][name], expected)

    def test_library_only_parser_is_available_before_native_initialization(self) -> None:
        self.assertTrue(self.report["parser_available"])
        self.assertTrue(self.report["native_type_absent_before"])
        self.assertTrue(self.report["native_type_absent_after"])
        self.assertEqual(self.report["cases"]["valid_v2"], "pass")

    def test_v2_producer_round_trip_and_v1_upgrade_only_compatibility(self) -> None:
        self.assertEqual(self.report["cases"]["producer_round_trip"], "pass")
        self.assertRegex(self.report["producer_release_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.report["cases"]["v1_with_previous"], "pass")
        self.assertEqual(self.report["cases"]["v1_default"], "installation_manifest_invalid")
        self.assertEqual(self.report["cases"]["v1_task_executable_mismatch"], "installation_manifest_task_invalid")
        self.assertEqual(self.report["cases"]["v1_six_file_package"], "installation_manifest_membership_invalid")
        self.assertEqual(self.report["cases"]["v2_five_file_package"], "installation_manifest_membership_invalid")

    def test_json_schema_source_and_release_format_errors_are_bounded(self) -> None:
        invalid = (
            "empty_text", "malformed_json", "null_root", "scalar_root", "array_root",
            "top_missing", "top_extra", "top_case_changed", "schema_unsupported", "schema_wrong_type",
            "v2_release_missing", "v1_release_extra", "source_wrong_type", "source_missing",
            "source_extra", "source_case_changed", "source_commit_upper", "source_commit_length",
            "source_commit_nonhex", "source_commit_wrong_type", "source_tree_upper", "source_tree_length",
            "source_tree_nonhex", "source_tree_wrong_type",
            "release_format_null", "release_format_wrong_type", "release_format_short",
            "release_format_uppercase", "release_format_nonhex",
        )
        self.assert_outcomes(invalid, "installation_manifest_invalid")
        self.assertEqual(self.report["cases"]["release_mismatch"], "release_identity_mismatch")

    def test_fixed_roots_require_exact_literal_strings(self) -> None:
        names = tuple(
            f"root_{root}_{variant}"
            for root in ("install_root", "runtime_root")
            for variant in ("case", "drive", "relative", "traversal", "unc", "device", "stream", "missing_trailing", "wrong_type")
        )
        self.assert_outcomes(names, "installation_manifest_path_invalid")

    def test_package_shape_membership_and_hash_matrix(self) -> None:
        names = (
            "package_null", "package_wrong_type", "package_missing", "package_duplicate", "package_unknown",
            "package_invalid_filename", "package_name_wrong_type", "package_entry_missing", "package_entry_extra",
            "package_entry_case_changed", "package_hash_null", "package_hash_wrong_type", "package_hash_short",
            "package_hash_uppercase", "package_hash_nonhex",
        )
        self.assert_outcomes(names, "installation_manifest_membership_invalid")

    def test_every_task_value_and_json_type_is_validated(self) -> None:
        fields = (
            "path", "name", "enabled", "trigger_count", "action_mode", "production_switches",
            "multiple_instances", "execution_time_limit", "restart_count", "start_when_available",
            "executable", "launcher_path", "arguments", "working_directory", "principal_user_id",
            "principal_logon_type", "principal_run_level",
        )
        names = ("task_null", "task_array", "task_missing", "task_extra", "task_case_changed", "v1_task_executable_mismatch")
        names += tuple(f"task_value_{field}" for field in fields)
        names += tuple(f"task_type_{field}" for field in fields)
        names += ("task_type_trigger_count_decimal", "task_type_restart_count_decimal")
        self.assert_outcomes(names, "installation_manifest_task_invalid")

    def test_rollback_roots_are_exact_and_unique(self) -> None:
        self.assert_outcomes(
            ("roots_missing", "roots_duplicate", "roots_unknown", "roots_wrong_type", "roots_entry_wrong_type", "roots_case_changed"),
            "installation_runtime_roots_invalid",
        )

    def test_parser_consumers_and_ci7_error_boundaries_remain_canonical(self) -> None:
        source = (ROOT / INSTALLER_PATH).read_text(encoding="utf-8")
        parser = _installer_function(source, "ConvertFrom-XbInstalledManifestText")
        self.assertIn("ConvertFrom-Json -InputObject $Text -ErrorAction Stop", parser)
        self.assertIn("[StringComparison]::Ordinal", parser)
        for forbidden in ("Get-Content", "Set-Content", "Remove-Item", "git", "TaskService", "New-XbWorkerBatchToken", "Initialize-XbWorkerNativeAccess", "PSCredential", "XbWorkerProtectedObject"):
            with self.subTest(parser_forbidden=forbidden):
                self.assertNotIn(forbidden, parser)
        reader = _installer_function(source, "Read-XbInstalledManifest")
        self.assertIn("-AllowPrevious:$AllowPrevious", reader)
        context = _installer_function(source, "Open-XbCi7VerificationContext")
        self.assertIn('ConvertFrom-XbInstalledManifestText -Text ([string]$manifestRecord.Text)', context)
        self.assertNotIn("Get-Content", context)
        uninstall = _installer_function(source, "Assert-XbUninstallOwnership")
        self.assertIn("Read-XbInstalledManifest", uninstall)
        installer = _installer_function(source, "Invoke-XbWorkerInstaller")
        self.assertLess(installer.index("$null = Assert-XbUninstallOwnership"), installer.index("Remove-XbWorkerOwnedState -TaskMayExist"))
        for function_name in ("Open-XbCi7VerificationContext", "Invoke-XbInstallVerifier"):
            function = _installer_function(source, function_name)
            for error_id in (
                "installation_manifest_invalid",
                "installation_manifest_path_invalid",
                "installation_manifest_membership_invalid",
                "installation_manifest_task_invalid",
                "installation_runtime_roots_invalid",
                "release_identity_mismatch",
            ):
                with self.subTest(function=function_name, error=error_id):
                    self.assertIn(error_id, function)


class MemberWorkerPathChainCompositionTests(unittest.TestCase):
    """CI7 parent/ancestor delete composition consumes individual chain paths and reaches native AccessCheck."""

    report: dict[str, object]
    leaf: PureWindowsPath
    _scratch_dir = None

    @classmethod
    def setUpClass(cls) -> None:
        pwsh = _resolve_native_powershell()
        if not pwsh:
            raise unittest.SkipTest("Windows PowerShell is required for path-chain composition validation")
        cls._scratch_dir = tempfile.TemporaryDirectory(prefix="xb-path-chain-")
        try:
            temp_dir = Path(cls._scratch_dir.name)
            harness = temp_dir / "path_chain_harness.ps1"
            harness.write_text(_PATH_CHAIN_HARNESS, encoding="utf-8", newline="\n")
            leaf = temp_dir / "chain" / "parent" / "leaf"
            leaf.mkdir(parents=True)
            cls.leaf = PureWindowsPath(str(leaf))
            completed = subprocess.run(
                [
                    pwsh, "-ExecutionPolicy", "Bypass", "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-File", str(harness),
                    "-InstallerPath", str(ROOT / INSTALLER_PATH),
                    "-LeafPath", str(leaf),
                ],
                cwd=ROOT,
                env=_windows_powershell_module_environment(),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if completed.returncode != 0:
                raise AssertionError(completed.stdout + completed.stderr)
            cls.report = json.loads(completed.stdout)
        except BaseException:
            cls._scratch_dir.cleanup()
            cls._scratch_dir = None
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._scratch_dir is not None:
            cls._scratch_dir.cleanup()
            cls._scratch_dir = None

    def _expected_chain(self) -> list[str]:
        chain = [self.leaf.anchor]
        for part in self.leaf.parts[1:]:
            chain.append(str(PureWindowsPath(chain[-1], part)))
        return chain

    def _expected_checks(self) -> list[tuple[str, str]]:
        chain = self._expected_chain()
        checks: list[tuple[str, str]] = []
        for index in range(len(chain) - 1, -1, -1):
            checks.append((chain[index], "0x00010000"))
            if index > 0:
                checks.append((chain[index - 1], "0x00000040"))
        return checks

    def _assert_same_path_objects(self, actual: object, expected: list[str]) -> None:
        self.assertIsInstance(actual, list)
        self.assertEqual(len(actual), len(expected))
        for index, (actual_path, expected_path) in enumerate(zip(actual, expected)):
            self.assertIs(type(actual_path), str, f"path at chain position {index} is not an individual string")
            try:
                same_object = os.path.samefile(actual_path, expected_path)
            except OSError as exc:
                self.fail(f"could not resolve filesystem identity at chain position {index}: {type(exc).__name__}")
            self.assertTrue(same_object, f"path at chain position {index} resolves to a different filesystem object")

    def _assert_checks_match(self, actual: object, expected: list[tuple[str, str]]) -> None:
        self.assertIsInstance(actual, list)
        self.assertEqual(len(actual), len(expected))
        for index, (actual_check, (expected_path, expected_mask)) in enumerate(zip(actual, expected)):
            self.assertIs(type(actual_check), str, f"probe at position {index} is not an individual string")
            path_and_mask = actual_check.rsplit("|", 1)
            if len(path_and_mask) != 2:
                self.fail(f"probe at position {index} does not contain a path and access mask")
            actual_path, actual_mask = path_and_mask
            self.assertEqual(actual_mask, expected_mask, f"access mask differs at probe position {index}")
            self._assert_same_path_objects([actual_path], [expected_path])

    def test_direct_assignment_yields_ordered_individual_string_paths(self) -> None:
        expected = self._expected_chain()
        self._assert_same_path_objects(self.report["direct_chain"], expected)
        self.assertEqual(self.report["direct_count"], len(expected))
        self.assertEqual(self.report["direct_element_types"], ["System.String"])

    def test_outer_array_wrapper_would_nest_the_chain(self) -> None:
        # Pins the Windows PowerShell 5.1 semantics behind the corrected defect.
        self.assertEqual(self.report["wrapped_count"], 1)
        self.assertEqual(self.report["wrapped_first_type"], "System.Object[]")

    def test_consumer_uses_direct_assignment_and_producer_shape_is_preserved(self) -> None:
        source = (ROOT / INSTALLER_PATH).read_text(encoding="utf-8")
        consumer = _installer_function(source, "Assert-XbPathNotDeleteable")
        self.assertNotIn("@(Get-XbNativePathChain", consumer)
        self.assertIn("$chain = Get-XbNativePathChain -Path $Path", consumer)
        self.assertIn("return ,$chain", _installer_function(source, "Get-XbNativePathChain"))

    def test_safe_chain_passes_after_every_per_element_native_check(self) -> None:
        self.assertTrue(self.report["token_restricted"])
        self.assertEqual(set(self.report["pass_case"]), {"outcome", "checks"})
        self.assertEqual(self.report["pass_case"]["outcome"], "pass")
        self._assert_checks_match(self.report["pass_case"]["checks"], self._expected_checks())
        self.assertEqual(self.report["restored_pass"], "pass")

    def test_protected_handle_reads_bytes_and_blocks_replace_or_rename(self) -> None:
        self.assertEqual(self.report["handle_sample_text"], "local-safe-f2")
        self.assertEqual(self.report["handle_sample_sha256"], hashlib.sha256(b"local-safe-f2").hexdigest())
        self.assertRegex(self.report["handle_sample_identity"], r"^[0-9a-f]{8}:[0-9a-f]{16}$")
        self.assertTrue(self.report["handle_sample_owner_present"])
        self.assertTrue(self.report["handle_replace_blocked"])
        self.assertTrue(self.report["handle_rename_blocked"])

    def test_handle_acquired_file_and_directory_descriptors_include_owner_and_group(self) -> None:
        for name in (
            "handle_directory_owner_present",
            "handle_directory_group_present",
            "handle_file_owner_present",
            "handle_file_group_present",
            "handle_directory_descriptor_matches_hash",
            "handle_file_descriptor_matches_hash",
        ):
            with self.subTest(component=name):
                self.assertTrue(self.report[name])

    def test_exact_handle_descriptor_bytes_reach_native_accesscheck(self) -> None:
        for name in (
            "handle_directory_accesscheck_completed",
            "handle_file_read_accesscheck_completed",
            "handle_file_delete_accesscheck_completed",
            "handle_file_maximum_accesscheck_completed",
            "handle_directory_list_access_allowed",
            "handle_file_read_access_allowed",
            "handle_file_delete_access_denied",
            "handle_file_maximum_access_usable",
        ):
            with self.subTest(access_check=name):
                self.assertTrue(self.report[name])

    def test_group_incomplete_descriptor_fails_closed_and_component_mask_is_complete(self) -> None:
        self.assertTrue(self.report["group_completeness_guard_present"])
        self.assertTrue(self.report["incomplete_group_descriptor_rejected"])
        source = (ROOT / INSTALLER_PATH).read_text(encoding="utf-8")
        native = _installer_function(source, "Initialize-XbWorkerNativeAccess")
        self.assertIn("GroupSecurityInformation = 0x00000002", native)
        self.assertIn(
            "OwnerSecurityInformation | GroupSecurityInformation | DaclSecurityInformation",
            native,
        )
        self.assertIn("HasUsableSecurityDescriptor(status, owner, group, descriptor)", native)

    def test_ci7_required_probe_completion_remains_mandatory(self) -> None:
        self.assertIn(
            '$ci7.required_probe_completion.status -cne "complete"',
            _HOSTED_TASK_BOUNDARY_HARNESS,
        )

    def test_granted_delete_or_delete_child_is_exceeded(self) -> None:
        checks = self._expected_checks()
        cases = {
            "leaf_delete": checks[:1],
            "parent_delete_child": checks[:2],
            "ancestor_delete": checks[:5],
        }
        for key, reached in cases.items():
            with self.subTest(case=key):
                case = self.report[key]
                self.assertTrue(case["requested_operation_granted"], case)
                self.assertEqual(case["outcome"], "effective_rights_exceeded")
                self._assert_checks_match(case["checks"], reached)

    def test_native_and_shape_failures_stay_unproven(self) -> None:
        for key in ("forced_native_failure", "disposed_token"):
            with self.subTest(case=key):
                self.assertEqual(self.report[key]["outcome"], "effective_rights_unproven")
                self._assert_checks_match(self.report[key]["checks"], self._expected_checks()[:1])
        for key in ("missing_path", "nested_chain_guard", "empty_element_guard"):
            with self.subTest(case=key):
                self.assertEqual(self.report[key], {"outcome": "effective_rights_unproven", "checks": []})


if __name__ == "__main__":
    unittest.main()
