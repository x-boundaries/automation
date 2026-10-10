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
    285, 286, 287, 4095, 4096, 4097,
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
    "scripts/install_ac2_member_gateway_worker.ps1": "8e5d16c174a88690f1bf2f2daec9ddd1d60f1415",
    "scripts/ac2_member_gateway_worker.ps1": "5ef41368dffae4eecadf56a1de1606508f8eadb5",
    "scripts/ac2_member_gateway_worker_lib.ps1": "4d2140d944d0e5c4384bc8b35a748f5bf53ecb20",
    "scripts/ac2_member_gateway_autocount_adapter.ps1": "f2fdbd56f23bf92b0ba9b0b4d7fde93d4b98e08e",
    "scripts/launch_ac2_member_gateway_worker.ps1": "a913dffc6191a8f8945b6427652bab963ab34d70",
    "scripts/test_ac2_member_gateway_autocount_dependencies.ps1": "2143f59739a413b80e4745bc23053fefade09486",
}

PROTECTED_WORKER_TREES = {
    "member_gateway": "58f0a02a37300c537269d7c21ab158be48390242",
}

WORKER_SCRIPTS = tuple(PROTECTED_WORKER_BLOBS)

ALLOWED_FILES = {
    "docs/autocount2-automation/member_gateway_production_runbook.md",
    "scripts/install_ac2_member_gateway_worker.ps1",
    "tests/test_member_gateway_worker_deployment.py",
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

    if global_shape_ok and (node_count != 286 or not required_methods_ok):
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
            and node_count == 286
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
    if global_shape and (node_count != 286 or not required_methods):
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
            and node_count == 286
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
                "node_count": 286,
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
            "node_count": 286,
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
                    node_count=286,
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
        add("OK node count not canonical", "OK", node_count=285)
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

        add("global success wrong count", "OK", node_count=287)
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
        self.assertEqual(global_shape_success["node_count"], 286, global_shape_success)
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
)


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


def _installer_function(source: str, name: str) -> str:
    start = source.index(f"function {name} {{")
    end = source.find("\nfunction ", start + 1)
    return source[start:] if end < 0 else source[start:end]


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


_LOCAL_UPGRADE_ROLLBACK_HARNESS = r'''[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InstallerPath,
    [Parameter(Mandatory)][string]$WorkRoot,
    [ValidateSet("all", "crash", "recover")][string]$HarnessMode = "all",
    [string]$FixtureName = "",
    [ValidateSet("Upgrade", "Rollback")][string]$CrashOperation = "Upgrade",
    [ValidateSet("journal", "move")][string]$CrashKind = "journal",
    [string]$CrashTarget = "",
    [ValidateSet("before", "after")][string]$CrashPhase = "before",
    [int]$CrashIndex = 0
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:HarnessPath = $MyInvocation.MyCommand.Path
$script:NativePowerShellPath = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
$script:SourceRoot = Join-Path $WorkRoot $(if ($HarnessMode -eq "recover") { "source-unavailable" } else { "synthetic-package-source" })
if ($HarnessMode -eq "recover") {
    $script:RecoveryDispatcherRoot = Join-Path $WorkRoot ("recovery-dispatcher-" + $FixtureName)
    if (Test-Path -LiteralPath $script:RecoveryDispatcherRoot) { throw "recovery_dispatcher_path_exists" }
    New-Item -ItemType Directory -Path $script:RecoveryDispatcherRoot -ErrorAction Stop | Out-Null
    $script:FixtureInstallerPath = Join-Path $script:RecoveryDispatcherRoot "install_ac2_member_gateway_worker.ps1"
    Copy-Item -LiteralPath $InstallerPath -Destination $script:FixtureInstallerPath -ErrorAction Stop
    $dispatcherEntries = @(Get-ChildItem -LiteralPath $script:RecoveryDispatcherRoot -Force)
    if ($dispatcherEntries.Count -ne 1 -or $dispatcherEntries[0].Name -cne "install_ac2_member_gateway_worker.ps1") { throw "recovery_dispatcher_not_isolated" }
} else {
    New-Item -ItemType Directory -Path $script:SourceRoot -Force | Out-Null
    $script:FixtureInstallerPath = Join-Path $script:SourceRoot "install_ac2_member_gateway_worker.ps1"
    Copy-Item -LiteralPath $InstallerPath -Destination $script:FixtureInstallerPath
}
$syntheticPackageFiles = @(
    "ac2_member_gateway_worker.ps1",
    "ac2_member_gateway_worker_lib.ps1",
    "ac2_member_gateway_autocount_adapter.ps1",
    "launch_ac2_member_gateway_worker.ps1",
    "test_ac2_member_gateway_autocount_dependencies.ps1"
)
if ($HarnessMode -ne "recover") {
    foreach ($name in $syntheticPackageFiles) { [IO.File]::WriteAllText((Join-Path $script:SourceRoot $name), ("synthetic payload: " + $name + "`n"), ([Text.UTF8Encoding]::new($false))) }
    [IO.File]::WriteAllText((Join-Path $script:SourceRoot "synthetic-reviewed-identity.json"), "{}", ([Text.UTF8Encoding]::new($false)))
}
$script:fixtureTask = $null
$script:fixtureView = $null
$script:fixtureReview = $null
$script:reviewOverride = $null
$script:reviewCalls = 0
$script:reviewForbidden = $false
$script:lastError = ""
$script:activeWorker = $false
$script:processMode = "valid"
$script:taskMode = "valid"
$script:forbiddenCalls = 0
$script:Fault = @{ enabled = $false; kind = ""; target = ""; phase = ""; index = 0; calls = 0; fired = $false }
$script:exitAtFault = $false
. $script:FixtureInstallerPath -LibraryOnly

$script:auditAclEvidence = "native"
try { $null = Get-Acl -LiteralPath $script:FixtureInstallerPath -Audit -ErrorAction Stop }
catch {
    if ([string]$_.Exception.Message -notmatch "SeSecurityPrivilege") { throw }
    $script:auditAclEvidence = "synthetic_dacl_only: SeSecurityPrivilege unavailable"
    # This desktop identity cannot query SACLs. Keep native filesystem/DACL
    # coverage while reporting that the SACL preservation boundary is unqualified.
    Set-Item function:script:Get-XbWorkerUpgradeAclSddl {
        param([Parameter(Mandatory)][string]$Path)
        try {
            $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
            if (-not $acl.AreAccessRulesCanonical) { throw "acl_order_unproven" }
            return $acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::All)
        } catch { throw "upgrade_acl_unverifiable" }
    }
    Set-Item function:script:Set-XbWorkerUpgradeAclSddl {
        param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Sddl)
        try {
            $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
            $acl.SetSecurityDescriptorSddlForm($Sddl, [Security.AccessControl.AccessControlSections]::All)
            Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
        } catch { throw "upgrade_acl_preservation_failed" }
        $actualSddl = Get-XbWorkerUpgradeAclSddl -Path $Path
        if ((Convert-XbWorkerUpgradeAclSddl $actualSddl) -cne (Convert-XbWorkerUpgradeAclSddl $Sddl)) { throw "upgrade_acl_preservation_failed" }
    }
}

Set-Item function:script:Assert-XbWorkerInstallLayout { }
Set-Item function:script:Assert-XbWorkerAdministrator { }
Set-Item function:script:Get-XbWorkerTaskIfPresent { return $script:fixtureTask }
Set-Item function:script:Get-XbRegisteredWorkerTaskView { return $script:fixtureView }
Set-Item function:script:Get-CimInstance {
    param([string]$ClassName, [string]$Filter, [string]$ErrorAction)
    if ($script:processMode -eq "missing_command_line") { return ,([pscustomobject]@{}) }
    if ($script:activeWorker) { return ,([pscustomobject]@{ CommandLine = [IO.Path]::GetFullPath((Join-Path $script:InstallRoot "launch_ac2_member_gateway_worker.ps1")) }) }
    return @()
}
Set-Item function:script:Read-XbReviewedPackageIdentity {
    param([string]$Path, [string]$PackageRoot)
    $script:reviewCalls++
    if ($script:reviewForbidden) { throw "source_read_forbidden" }
    if ($null -ne $script:reviewOverride) { return $script:reviewOverride }
    return $script:fixtureReview
}
Set-Item function:script:Register-XbWorkerScheduledTask { $script:forbiddenCalls++; throw "forbidden_scheduler_registration" }
Set-Item function:script:Remove-XbWorkerScheduledTask { $script:forbiddenCalls++; throw "forbidden_scheduler_removal" }
Set-Item function:script:Remove-XbWorkerOwnedState { $script:forbiddenCalls++; throw "forbidden_owned_state_removal" }
Set-Item function:script:Set-XbWorkerAcl { $script:forbiddenCalls++; throw "forbidden_install_acl_rewrite" }

function New-XbFixtureReviewedIdentity {
    param([string]$Commit = ("1" * 40), [string]$Tree = ("2" * 40))
    $files = foreach ($name in $packageFiles) {
        [ordered]@{ name = $name; sha256 = (Get-XbFileSha256 (Join-Path $script:SourceRoot $name)) }
    }
    return [pscustomobject]@{
        schema_version = "xb.member.gateway.worker.reviewed-package.v1"
        source = [pscustomobject]@{ commit = $Commit; tree = $Tree }
        package_files = @($files)
    }
}

function Set-XbFixtureTask {
    $worker = "XBHOST\XB_G3_SYNTHETIC"
    $launcher = [IO.Path]::GetFullPath((Join-Path $script:InstallRoot "launch_ac2_member_gateway_worker.ps1"))
    $identity = Get-XbWorkerTaskIdentity -LauncherPath $launcher -WorkerAccount $worker
    $state = "Disabled"
    $triggers = $null
    $execute = $identity.executable
    $arguments = $identity.arguments
    $principal = $identity.principal_user_id
    $enabled = $false
    $triggerCount = 0
    $xmlTriggers = "<Triggers />"
    switch ($script:taskMode) {
        "enabled" { $state = "Ready"; $enabled = $true }
        "triggered" { $triggers = @([pscustomobject]@{ Enabled = $true }); $triggerCount = 1; $xmlTriggers = "<Triggers><TimeTrigger /></Triggers>" }
        "action" { $execute = "cmd.exe" }
        "principal" { $principal = "XBHOST\FOREIGN" }
    }
    $script:fixtureTask = [pscustomobject]@{
        TaskPath = $taskPath; TaskName = $taskName; State = $state; Triggers = $triggers
        Actions = @([pscustomobject]@{ Execute = $execute; Arguments = $arguments; WorkingDirectory = "" })
        Settings = [pscustomobject]@{ MultipleInstances = "IgnoreNew"; ExecutionTimeLimit = "PT10M"; RestartCount = 0; StartWhenAvailable = $false }
        Principal = [pscustomobject]@{ UserId = $principal; LogonType = "Password"; RunLevel = "Limited" }
    }
    $comAction = [pscustomobject]@{ Type = 0; Path = $execute; Arguments = $arguments }
    $actions = [pscustomobject]@{ Count = 1; Entry = $comAction }
    Add-Member -InputObject $actions -MemberType ScriptMethod -Name Item -Value { param($Index) return $this.Entry }
    $viewXml = '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Settings><Enabled>' + ([string]$enabled).ToLowerInvariant() + '</Enabled></Settings>' + $xmlTriggers + '</Task>'
    $script:fixtureView = [pscustomobject]@{
        Path = ($taskPath + $taskName); Enabled = $enabled; Xml = $viewXml
        Definition = [pscustomobject]@{
            Settings = [pscustomobject]@{ Enabled = $enabled }
            Triggers = [pscustomobject]@{ Count = $triggerCount }
            Actions = $actions
        }
    }
}

function Initialize-XbFixture {
    param([Parameter(Mandatory)][string]$Name)
    $base = Join-Path $WorkRoot $Name
    New-Item -ItemType Directory -Path $base -Force | Out-Null
    $script:InstallRoot = Join-Path $base "pf\X-Boundaries\MemberGatewayWorker"
    $script:RuntimeRoot = Join-Path $base "pd\X-Boundaries\MemberGatewayWorker"
    $script:taskPath = "\XB-G3-SYNTHETIC\"
    $script:taskName = "Synthetic Worker"
    $script:taskMode = "valid"
    $script:reviewOverride = $null
    $script:reviewForbidden = $false
    $script:activeWorker = $false
    $script:processMode = "valid"
    $script:fixtureReview = New-XbFixtureReviewedIdentity -Commit ("0" * 40) -Tree ("0" * 40)
    New-Item -ItemType Directory -Path $script:InstallRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $script:RuntimeRoot -Force | Out-Null
    foreach ($name in $packageFiles) { Copy-Item -LiteralPath (Join-Path $script:SourceRoot $name) -Destination (Join-Path $script:InstallRoot $name) }
    $installed = New-XbWorkerInstallationManifest -PackageRoot $script:SourceRoot -ReviewedIdentity $script:fixtureReview -WorkerAccount "XBHOST\XB_G3_SYNTHETIC"
    ($installed | ConvertTo-Json -Depth 12) | Set-Content -LiteralPath (Join-Path $script:InstallRoot "installation-manifest.json") -Encoding UTF8
    New-Item -ItemType Directory -Path (Join-Path $script:RuntimeRoot "config") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $script:RuntimeRoot "secrets") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $script:RuntimeRoot "logs") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $script:RuntimeRoot "rollback") -Force | Out-Null
    "synthetic opaque secret sentinel" | Set-Content -LiteralPath (Join-Path $script:RuntimeRoot "secrets\opaque.bin") -Encoding ASCII
    "synthetic log sentinel" | Set-Content -LiteralPath (Join-Path $script:RuntimeRoot "logs\worker.log") -Encoding ASCII
    "preexisting recovery sentinel" | Set-Content -LiteralPath (Join-Path $script:RuntimeRoot "rollback\keep.dat") -Encoding ASCII
    Set-XbFixtureTask
    $script:baseline = Get-XbWorkerPackageIdentity -Root $script:InstallRoot
    $script:taskBaseline = Get-XbFixtureTaskSnapshot
    $script:runtimeBaseline = Get-XbFixtureRuntimeSnapshot
    [IO.File]::WriteAllText((Join-Path $base "runtime-baseline.json"), $script:runtimeBaseline, ([Text.UTF8Encoding]::new($false)))
    return $base
}

function Get-XbFixtureTaskSnapshot {
    return ConvertTo-Json -InputObject ([ordered]@{
        path = [string]$script:fixtureTask.TaskPath; name = [string]$script:fixtureTask.TaskName; state = [string]$script:fixtureTask.State
        triggers = (ConvertTo-Json -InputObject $script:fixtureTask.Triggers -Depth 4 -Compress)
        execute = [string]$script:fixtureTask.Actions[0].Execute; arguments = [string]$script:fixtureTask.Actions[0].Arguments
        principal = [string]$script:fixtureTask.Principal.UserId; logon = [string]$script:fixtureTask.Principal.LogonType; run_level = [string]$script:fixtureTask.Principal.RunLevel
    }) -Depth 5 -Compress
}

function Get-XbFixtureRuntimeSnapshot {
    $entries = foreach ($item in @(Get-ChildItem -LiteralPath $script:RuntimeRoot -Force -Recurse | Sort-Object FullName)) {
        if ($item.PSIsContainer) { [ordered]@{ name = $item.FullName.Substring($script:RuntimeRoot.Length); kind = "directory" } }
        else { [ordered]@{ name = $item.FullName.Substring($script:RuntimeRoot.Length); kind = "file"; sha256 = (Get-XbFileSha256 $item.FullName) } }
    }
    return ConvertTo-Json -InputObject @($entries) -Depth 6 -Compress
}

function Get-XbFixtureAclSnapshot {
    param([Parameter(Mandatory)][string]$Root)
    $acl = Get-XbWorkerUpgradeAclIdentity -Root $Root
    $result = [ordered]@{ root = (Convert-XbWorkerUpgradeAclSddl ([string]$acl.root)); files = @() }
    foreach ($entry in @($acl.files)) { $result.files += [ordered]@{ name = [string]$entry.name; sddl = (Convert-XbWorkerUpgradeAclSddl ([string]$entry.sddl)) } }
    return ConvertTo-Json -InputObject $result -Depth 8 -Compress
}

function Get-XbFixtureTreeSnapshot {
    param([Parameter(Mandatory)][string]$Root)
    $entries = foreach ($item in @(Get-ChildItem -LiteralPath $Root -Force -Recurse | Sort-Object FullName)) {
        if ($item.PSIsContainer) { [ordered]@{ name = $item.FullName.Substring($Root.Length); kind = "directory" } }
        else { [ordered]@{ name = $item.FullName.Substring($Root.Length); kind = "file"; sha256 = (Get-XbFileSha256 $item.FullName) } }
    }
    return ConvertTo-Json -InputObject @($entries) -Depth 6 -Compress
}

function Invoke-XbFixtureOperation {
    param([Parameter(Mandatory)][ValidateSet("Upgrade", "Rollback")][string]$Name)
    $script:Operation = $Name
    $script:ReviewedPackageManifestPath = if ($Name -eq "Upgrade") { Join-Path $script:SourceRoot "synthetic-reviewed-identity.json" } else { $null }
    $script:WorkerAccount = $null
    $script:TaskCredential = $null
    $script:lastError = ""
    try { $null = Invoke-XbWorkerInstaller; return "pass" } catch { $script:lastError = [string]$_.Exception.Message; return $script:lastError }
}

function Test-XbFixtureRestored {
    $package = $false
    try { Assert-XbWorkerUpgradePackageMatches -Root $script:InstallRoot -Expected $script:baseline; $package = $true } catch { }
    return [ordered]@{
        package = $package
        task = ((Get-XbFixtureTaskSnapshot) -ceq $script:taskBaseline)
        runtime = ((Get-XbFixtureRuntimeSnapshot) -ceq $script:runtimeBaseline)
        forbidden_calls = $script:forbiddenCalls
    }
}

$script:OriginalJournalWriter = ${function:Write-XbWorkerUpgradeJournal}
$script:OriginalDirectoryMover = ${function:Move-XbWorkerUpgradeDirectory}
Set-Item function:script:Write-XbWorkerUpgradeJournal {
    param([Parameter(Mandatory)]$Journal)
    $fault = $script:Fault
    $match = $fault.enabled -and $fault.kind -eq "journal" -and [string]$Journal.state -ceq $fault.target -and -not $fault.fired
    if ($match -and $fault.phase -eq "before") { $fault.fired = $true; if ($script:exitAtFault) { [Environment]::Exit(86) }; throw "synthetic_process_crash" }
    & $script:OriginalJournalWriter -Journal $Journal
    if ($match -and $fault.phase -eq "after") { $fault.fired = $true; if ($script:exitAtFault) { [Environment]::Exit(86) }; throw "synthetic_process_crash" }
}
Set-Item function:script:Move-XbWorkerUpgradeDirectory {
    param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Destination)
    $fault = $script:Fault
    $fault.calls++
    $match = $fault.enabled -and $fault.kind -eq "move" -and $fault.calls -eq $fault.index -and -not $fault.fired
    if ($match -and $fault.phase -eq "before") { $fault.fired = $true; if ($script:exitAtFault) { [Environment]::Exit(86) }; throw "synthetic_process_crash" }
    & $script:OriginalDirectoryMover -Source $Source -Destination $Destination
    if ($match -and $fault.phase -eq "after") { $fault.fired = $true; if ($script:exitAtFault) { [Environment]::Exit(86) }; throw "synthetic_process_crash" }
}

function Set-XbFault {
    param([string]$Kind, [string]$Target, [string]$Phase, [int]$Index = 0)
    $script:Fault = @{ enabled = $true; kind = $Kind; target = $Target; phase = $Phase; index = $Index; calls = 0; fired = $false }
}
function Clear-XbFault { $script:Fault.enabled = $false }

$out = [ordered]@{ positive = @{}; adversarial = @{} }

function Invoke-XbFreshHarnessChild {
    param(
        [Parameter(Mandatory)][ValidateSet("crash", "recover")][string]$ChildMode,
        [Parameter(Mandatory)][string]$Name,
        [string]$Operation = "Upgrade",
        [string]$Kind = "journal",
        [string]$Target = "",
        [string]$Phase = "before",
        [int]$Index = 0
    )
    $arguments = @(
        "-ExecutionPolicy", "Bypass", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", $script:HarnessPath,
        "-InstallerPath", $InstallerPath, "-WorkRoot", $WorkRoot, "-HarnessMode", $ChildMode, "-FixtureName", $Name
    )
    if ($ChildMode -eq "crash") {
        $arguments += @("-CrashOperation", $Operation, "-CrashKind", $Kind, "-CrashTarget", $Target, "-CrashPhase", $Phase, "-CrashIndex", [string]$Index)
    }
    $childOutput = & $script:NativePowerShellPath @arguments 2>&1
    return [pscustomobject]@{ exit_code = [int]$LASTEXITCODE; output = (@($childOutput | ForEach-Object { [string]$_ }) -join "`n") }
}

if ($HarnessMode -eq "recover") {
    if ([string]::IsNullOrWhiteSpace($FixtureName)) { throw "fresh_recovery_fixture_required" }
    $base = Join-Path $WorkRoot $FixtureName
    $script:InstallRoot = Join-Path $base "pf\X-Boundaries\MemberGatewayWorker"
    $script:RuntimeRoot = Join-Path $base "pd\X-Boundaries\MemberGatewayWorker"
    $script:taskPath = "\XB-G3-SYNTHETIC\"
    $script:taskName = "Synthetic Worker"
    Set-XbFixtureTask
    $paths = Get-XbWorkerUpgradePaths
    $runtimeBaselinePath = Join-Path $base "runtime-baseline.json"
    if (-not (Test-Path -LiteralPath $runtimeBaselinePath -PathType Leaf)) { throw "fresh_runtime_baseline_missing" }
    $script:runtimeBaseline = [IO.File]::ReadAllText($runtimeBaselinePath)
    $null = $script:runtimeBaseline | ConvertFrom-Json -ErrorAction Stop
    $journalReadError = ""
    if (Test-Path -LiteralPath $paths.journal) {
        try { $script:baseline = (Read-XbWorkerUpgradeJournal -Path $paths.journal).old_identity }
        catch { $journalReadError = [string]$_.Exception.Message; $script:baseline = Get-XbWorkerPackageIdentity -Root $script:InstallRoot }
    } else { $script:baseline = Get-XbWorkerPackageIdentity -Root $script:InstallRoot }
    $script:taskBaseline = Get-XbFixtureTaskSnapshot
    $script:reviewForbidden = $true
    $first = Invoke-XbFixtureOperation "Rollback"
    $second = Invoke-XbFixtureOperation "Rollback"
    $recovery = [ordered]@{
        first = $first
        repeated = $second
        restored = (Test-XbFixtureRestored)
        source_root_absent = (-not (Test-Path -LiteralPath $script:SourceRoot))
        dispatcher_package_files_absent = (@($packageFiles | Where-Object { Test-Path -LiteralPath (Join-Path $script:RecoveryDispatcherRoot $_) }).Count -eq 0)
        runtime_baseline = "pre-crash fixture evidence"
        source_reads = $script:reviewCalls
        forbidden_calls = $script:forbiddenCalls
        journal_read_error = $journalReadError
    }
    [Console]::Out.Write(($recovery | ConvertTo-Json -Depth 8 -Compress))
    exit 0
}

if ($HarnessMode -eq "crash") {
    if ([string]::IsNullOrWhiteSpace($FixtureName) -or [string]::IsNullOrWhiteSpace($CrashTarget)) { throw "fresh_crash_fixture_required" }
    $null = Initialize-XbFixture $FixtureName
    if ($CrashOperation -eq "Rollback") {
        if ($CrashTarget -in @("rollback_stage_cleanup", "aborted")) {
            Set-XbFault -Kind journal -Target "staged" -Phase after
            $seed = Invoke-XbFixtureOperation "Upgrade"
            Clear-XbFault
            if ($seed -ne "synthetic_process_crash") { [Console]::Error.WriteLine("fresh_crash_seed_failed:" + $seed); exit 88 }
        } else {
            $seed = Invoke-XbFixtureOperation "Upgrade"
            if ($seed -ne "pass") { [Console]::Error.WriteLine("fresh_crash_seed_failed:" + $seed); exit 88 }
        }
    }
    Set-XbFault -Kind $CrashKind -Target $CrashTarget -Phase $CrashPhase -Index $CrashIndex
    $script:exitAtFault = $true
    $unexpected = Invoke-XbFixtureOperation $CrashOperation
    [Console]::Error.WriteLine("fresh_crash_boundary_not_reached:" + $unexpected)
    exit 87
}

# Positive dispatcher path. Defer source-independent rollback until all other
# fixtures have used their benign synthetic source package.
$null = Initialize-XbFixture "positive"
$out.positive.upgrade = Invoke-XbFixtureOperation "Upgrade"
if ($out.positive.upgrade -ne "pass") { $out.positive.error = $script:lastError; [Console]::Out.WriteLine(($out | ConvertTo-Json -Depth 16 -Compress)); exit 2 }
$upgradeJournal = Read-XbWorkerUpgradeJournal -Path (Get-XbWorkerUpgradePaths).journal
$out.positive.complete = [string]$upgradeJournal.state
$out.positive.current_is_new = $false
try { Assert-XbWorkerUpgradePackageMatches -Root $script:InstallRoot -Expected $upgradeJournal.new_identity; $out.positive.current_is_new = $true } catch { }
$out.positive.layout_before_rollback = Assert-XbWorkerUpgradeLayout -Journal $upgradeJournal
$out.positive.task_unchanged_after_upgrade = ((Get-XbFixtureTaskSnapshot) -ceq $script:taskBaseline)
$out.positive.runtime_unchanged_after_upgrade = ((Get-XbFixtureRuntimeSnapshot) -ceq $script:runtimeBaseline)
$script:positiveFixture = @{
    install = $script:InstallRoot; runtime = $script:RuntimeRoot; task_path = $script:taskPath; task_name = $script:taskName
    task = $script:fixtureTask; view = $script:fixtureView; baseline = $script:baseline
    task_baseline = $script:taskBaseline; runtime_baseline = $script:runtimeBaseline
}

function Invoke-XbFreshProcessRecoveryCase {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet("Upgrade", "Rollback")][string]$Operation,
        [Parameter(Mandatory)][ValidateSet("journal", "move")][string]$Kind,
        [Parameter(Mandatory)][string]$Target,
        [Parameter(Mandatory)][ValidateSet("before", "after")][string]$Phase,
        [int]$Index = 0,
        [switch]$InjectRuntimeDrift
    )
    $fixture = "fresh-" + $Name
    $crash = Invoke-XbFreshHarnessChild -ChildMode crash -Name $fixture -Operation $Operation -Kind $Kind -Target $Target -Phase $Phase -Index $Index
    if ($crash.exit_code -ne 86) { throw ("fresh_process_crash_not_injected:" + $fixture + ":" + $crash.exit_code + ":" + $crash.output) }
    if ($InjectRuntimeDrift) {
        $runtimeDriftRoot = Join-Path (Join-Path $WorkRoot $fixture) "pd\X-Boundaries\MemberGatewayWorker"
        $runtimeDriftPath = Join-Path $runtimeDriftRoot "logs\recovery-drift.txt"
        [IO.File]::WriteAllText($runtimeDriftPath, "synthetic post-crash runtime drift`n", ([Text.UTF8Encoding]::new($false)))
    }
    $recovered = Invoke-XbFreshHarnessChild -ChildMode recover -Name $fixture
    if ($recovered.exit_code -ne 0) { throw ("fresh_process_recovery_failed:" + $fixture + ":" + $recovered.exit_code + ":" + $recovered.output) }
    try { $record = $recovered.output | ConvertFrom-Json -ErrorAction Stop }
    catch { throw ("fresh_process_recovery_report_invalid:" + $fixture + ":" + $recovered.output) }
    return [ordered]@{
        crash_exit = $crash.exit_code
        first = [string]$record.first
        repeated = [string]$record.repeated
        restored = $record.restored
        source_root_absent = [bool]$record.source_root_absent
        dispatcher_package_files_absent = [bool]$record.dispatcher_package_files_absent
        runtime_baseline = [string]$record.runtime_baseline
        runtime_drift_injected = [bool]$InjectRuntimeDrift
        source_reads = [int]$record.source_reads
        forbidden_calls = [int]$record.forbidden_calls
    }
}

$out.fresh_process_fault_recovery = [ordered]@{}
foreach ($stateName in @("preparing", "staged", "old_move_intent", "new_move_intent", "complete")) {
    foreach ($phase in @("before", "after")) {
        $key = "upgrade_journal_" + $stateName + "_" + $phase
        $out.fresh_process_fault_recovery[$key] = Invoke-XbFreshProcessRecoveryCase -Name $key -Operation Upgrade -Kind journal -Target $stateName -Phase $phase
    }
}
foreach ($stateName in @("rollback_stage_cleanup", "aborted", "rollback_new_intent", "rollback_old_intent", "rolled_back")) {
    foreach ($phase in @("before", "after")) {
        $key = "rollback_journal_" + $stateName + "_" + $phase
        $out.fresh_process_fault_recovery[$key] = Invoke-XbFreshProcessRecoveryCase -Name $key -Operation Rollback -Kind journal -Target $stateName -Phase $phase
    }
}
foreach ($operation in @("Upgrade", "Rollback")) {
    foreach ($index in 1, 2) {
        foreach ($phase in @("before", "after")) {
            $key = $operation.ToLowerInvariant() + "_rename_" + $index + "_" + $phase
            $out.fresh_process_fault_recovery[$key] = Invoke-XbFreshProcessRecoveryCase -Name $key -Operation $operation -Kind move -Target "rename" -Phase $phase -Index $index
        }
    }
}
$out.fresh_process_fault_recovery["runtime_drift_control"] = Invoke-XbFreshProcessRecoveryCase -Name "runtime_drift_control" -Operation Upgrade -Kind journal -Target "complete" -Phase after -InjectRuntimeDrift

# Source SHA, malformed package preimages, disabled-task contract, active worker, lock, stage/custody tampering.
$null = Initialize-XbFixture "wrong-source-sha"
$script:reviewOverride = New-XbFixtureReviewedIdentity
$script:reviewOverride.package_files[0].sha256 = ("f" * 64)
$snapshot = Get-XbFixtureTreeSnapshot -Root $script:InstallRoot
$out.adversarial.wrong_source_sha = [ordered]@{ outcome = (Invoke-XbFixtureOperation "Upgrade"); unchanged = ((Get-XbFixtureTreeSnapshot -Root $script:InstallRoot) -ceq $snapshot); runtime_unchanged = ((Get-XbFixtureRuntimeSnapshot) -ceq $script:runtimeBaseline) }

foreach ($case in @("missing_file", "extra_file", "duplicate_file", "tampered_manifest", "stale_package_sha")) {
    $null = Initialize-XbFixture ("preimage-" + $case)
    $manifestPath = Join-Path $script:InstallRoot "installation-manifest.json"
    if ($case -eq "missing_file") { Remove-Item -LiteralPath (Join-Path $script:InstallRoot $packageFiles[0]) -Force }
    elseif ($case -eq "extra_file") { "foreign" | Set-Content -LiteralPath (Join-Path $script:InstallRoot "unexpected.txt") }
    elseif ($case -eq "tampered_manifest") { "{}" | Set-Content -LiteralPath $manifestPath }
    else {
        $m = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
        if ($case -eq "duplicate_file") { $m.package_files += $m.package_files[0] }
        else { $m.package_files[0].sha256 = ("f" * 64) }
        ($m | ConvertTo-Json -Depth 12) | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    }
    $snapshot = Get-XbFixtureTreeSnapshot -Root $script:InstallRoot
    $out.adversarial[$case] = [ordered]@{ outcome = (Invoke-XbFixtureOperation "Upgrade"); unchanged = ((Get-XbFixtureTreeSnapshot -Root $script:InstallRoot) -ceq $snapshot); runtime_unchanged = ((Get-XbFixtureRuntimeSnapshot) -ceq $script:runtimeBaseline) }
}

$out.adversarial.task = @{}
foreach ($mode in @("enabled", "triggered", "action", "principal")) {
    $null = Initialize-XbFixture ("task-" + $mode)
    $script:taskMode = $mode
    Set-XbFixtureTask
    $script:taskBaseline = Get-XbFixtureTaskSnapshot
    $out.adversarial.task[$mode] = [ordered]@{ outcome = (Invoke-XbFixtureOperation "Upgrade"); unchanged = ((Get-XbFixtureTaskSnapshot) -ceq $script:taskBaseline); package_unchanged = (Test-XbFixtureRestored).package }
}

$null = Initialize-XbFixture "active-worker"
$script:activeWorker = $true
$out.adversarial.active_worker = [ordered]@{ outcome = (Invoke-XbFixtureOperation "Upgrade"); package_unchanged = (Test-XbFixtureRestored).package }
$script:activeWorker = $false

$null = Initialize-XbFixture "unknown-worker-process"
$script:processMode = "missing_command_line"
$out.adversarial.unknown_worker_process = [ordered]@{ outcome = (Invoke-XbFixtureOperation "Upgrade"); package_unchanged = (Test-XbFixtureRestored).package }
$script:processMode = "valid"

$null = Initialize-XbFixture "hardlink-package-payload"
$hardlinkPath = Join-Path $script:InstallRoot $packageFiles[0]
$hardlinkTarget = Join-Path $WorkRoot "external-hardlink-target.bin"
[IO.File]::WriteAllText($hardlinkTarget, "synthetic private sentinel`n", ([Text.UTF8Encoding]::new($false)))
$hardlinkTargetSha = Get-XbFileSha256 -Path $hardlinkTarget
Remove-Item -LiteralPath $hardlinkPath -Force
$null = & "$env:SystemRoot\System32\cmd.exe" /d /c mklink /H $hardlinkPath $hardlinkTarget
$hardlinkCreated = (Test-Path -LiteralPath $hardlinkPath) -and ([XbWorkerUpgradeNativeFileInfo]::LinkCount($hardlinkPath) -eq 2)
$hardlinkSnapshot = Get-XbFixtureTreeSnapshot -Root $script:InstallRoot
$script:hardlinkHashCalls = 0
$script:OriginalFileHasher = ${function:Get-XbFileSha256}
Set-Item function:script:Get-XbFileSha256 {
    param([Parameter(Mandatory)][string]$Path)
    if ([IO.Path]::GetFullPath($Path) -ceq [IO.Path]::GetFullPath($hardlinkPath)) { $script:hardlinkHashCalls++ }
    & $script:OriginalFileHasher -Path $Path
}
$hardlinkOutcome = Invoke-XbFixtureOperation "Upgrade"
Set-Item function:script:Get-XbFileSha256 $script:OriginalFileHasher
$out.adversarial.hardlink_package_payload = [ordered]@{
    created = $hardlinkCreated
    outcome = $hardlinkOutcome
    alias_hash_calls = $script:hardlinkHashCalls
    external_preserved = ((Get-XbFileSha256 -Path $hardlinkTarget) -ceq $hardlinkTargetSha)
    package_unchanged = ((Get-XbFixtureTreeSnapshot -Root $script:InstallRoot) -ceq $hardlinkSnapshot)
}

$null = Initialize-XbFixture "hardlink-package-manifest"
$manifestLink = Join-Path $script:InstallRoot "installation-manifest.json"
$manifestTarget = Join-Path $WorkRoot "external-manifest-target.bin"
[IO.File]::WriteAllText($manifestTarget, "synthetic external manifest sentinel`n", ([Text.UTF8Encoding]::new($false)))
$manifestTargetSha = Get-XbFileSha256 -Path $manifestTarget
Remove-Item -LiteralPath $manifestLink -Force
$null = & "$env:SystemRoot\System32\cmd.exe" /d /c mklink /H $manifestLink $manifestTarget
$manifestLinkCreated = (Test-Path -LiteralPath $manifestLink) -and ([XbWorkerUpgradeNativeFileInfo]::LinkCount($manifestLink) -eq 2)
$manifestLinkSnapshot = Get-XbFixtureTreeSnapshot -Root $script:InstallRoot
$script:manifestReadCalls = 0
Set-Item function:script:Get-Content {
    [CmdletBinding()]
    param([string]$LiteralPath, [switch]$Raw, [string]$Encoding)
    if ([IO.Path]::GetFullPath($LiteralPath) -ceq [IO.Path]::GetFullPath($manifestLink)) { $script:manifestReadCalls++ }
    Microsoft.PowerShell.Management\Get-Content @PSBoundParameters
}
$manifestLinkOutcome = Invoke-XbFixtureOperation "Upgrade"
Remove-Item function:script:Get-Content
$out.adversarial.hardlink_package_manifest = [ordered]@{
    created = $manifestLinkCreated
    outcome = $manifestLinkOutcome
    manifest_read_calls = $script:manifestReadCalls
    external_preserved = ((Get-XbFileSha256 -Path $manifestTarget) -ceq $manifestTargetSha)
    package_unchanged = ((Get-XbFixtureTreeSnapshot -Root $script:InstallRoot) -ceq $manifestLinkSnapshot)
}

$null = Initialize-XbFixture "hardlink-reviewed-source"
$reviewSourcePath = Join-Path $script:SourceRoot $packageFiles[0]
$reviewSourceTarget = Join-Path $WorkRoot "external-reviewed-source-target.bin"
[IO.File]::WriteAllText($reviewSourceTarget, "synthetic private source sentinel`n", ([Text.UTF8Encoding]::new($false)))
$reviewSourceTargetSha = Get-XbFileSha256 -Path $reviewSourceTarget
Remove-Item -LiteralPath $reviewSourcePath -Force
$null = & "$env:SystemRoot\System32\cmd.exe" /d /c mklink /H $reviewSourcePath $reviewSourceTarget
$reviewSourceLinkCreated = (Test-Path -LiteralPath $reviewSourcePath) -and ([XbWorkerUpgradeNativeFileInfo]::LinkCount($reviewSourcePath) -eq 2)
$script:reviewCalls = 0
$reviewSourceOutcome = Invoke-XbFixtureOperation "Upgrade"
$out.adversarial.hardlink_reviewed_source = [ordered]@{
    created = $reviewSourceLinkCreated
    outcome = $reviewSourceOutcome
    source_reads = $script:reviewCalls
    external_preserved = ((Get-XbFileSha256 -Path $reviewSourceTarget) -ceq $reviewSourceTargetSha)
    install_unchanged = (Test-XbFixtureRestored).package
    runtime_unchanged = (Test-XbFixtureRestored).runtime
}
Remove-Item -LiteralPath $reviewSourcePath -Force
[IO.File]::WriteAllText($reviewSourcePath, ("synthetic payload: " + $packageFiles[0] + "`n"), ([Text.UTF8Encoding]::new($false)))

$null = Initialize-XbFixture "hardlink-reviewed-manifest"
$reviewManifestPath = Join-Path $script:SourceRoot "synthetic-reviewed-identity.json"
$reviewManifestTarget = Join-Path $WorkRoot "external-reviewed-manifest-target.bin"
[IO.File]::WriteAllText($reviewManifestTarget, "synthetic private manifest sentinel`n", ([Text.UTF8Encoding]::new($false)))
$reviewManifestTargetSha = Get-XbFileSha256 -Path $reviewManifestTarget
Remove-Item -LiteralPath $reviewManifestPath -Force
$null = & "$env:SystemRoot\System32\cmd.exe" /d /c mklink /H $reviewManifestPath $reviewManifestTarget
$reviewManifestLinkCreated = (Test-Path -LiteralPath $reviewManifestPath) -and ([XbWorkerUpgradeNativeFileInfo]::LinkCount($reviewManifestPath) -eq 2)
$script:reviewCalls = 0
$reviewManifestOutcome = Invoke-XbFixtureOperation "Upgrade"
$out.adversarial.hardlink_reviewed_manifest = [ordered]@{
    created = $reviewManifestLinkCreated
    outcome = $reviewManifestOutcome
    source_reads = $script:reviewCalls
    external_preserved = ((Get-XbFileSha256 -Path $reviewManifestTarget) -ceq $reviewManifestTargetSha)
    install_unchanged = (Test-XbFixtureRestored).package
    runtime_unchanged = (Test-XbFixtureRestored).runtime
}
Remove-Item -LiteralPath $reviewManifestPath -Force
[IO.File]::WriteAllText($reviewManifestPath, "{}", ([Text.UTF8Encoding]::new($false)))

$null = Initialize-XbFixture "hardlink-transaction-journal"
$null = Invoke-XbFixtureOperation "Upgrade"
$paths = Get-XbWorkerUpgradePaths
$committedJournal = Read-XbWorkerUpgradeJournal -Path $paths.journal
$journalLinkPreviousRoot = [string]$committedJournal.previous_root
$journalLinkPrevious = Get-XbFixtureTreeSnapshot -Root $journalLinkPreviousRoot
$externalJournal = Join-Path $WorkRoot "external-journal-target.bin"
[IO.File]::WriteAllText($externalJournal, "synthetic external journal sentinel`n", ([Text.UTF8Encoding]::new($false)))
$externalJournalSha = Get-XbFileSha256 -Path $externalJournal
Remove-Item -LiteralPath $paths.journal -Force
$null = & "$env:SystemRoot\System32\cmd.exe" /d /c mklink /H $paths.journal $externalJournal
$journalLinkCreated = (Test-Path -LiteralPath $paths.journal) -and ([XbWorkerUpgradeNativeFileInfo]::LinkCount($paths.journal) -eq 2)
$journalLinkInstall = Get-XbFixtureTreeSnapshot -Root $script:InstallRoot
$journalLinkOutcome = Invoke-XbFixtureOperation "Rollback"
$out.adversarial.hardlink_transaction_journal = [ordered]@{
    created = $journalLinkCreated
    outcome = $journalLinkOutcome
    external_preserved = ((Get-XbFileSha256 -Path $externalJournal) -ceq $externalJournalSha)
    install_unchanged = ((Get-XbFixtureTreeSnapshot -Root $script:InstallRoot) -ceq $journalLinkInstall)
    previous_unchanged = ((Get-XbFixtureTreeSnapshot -Root $journalLinkPreviousRoot) -ceq $journalLinkPrevious)
}

$null = Initialize-XbFixture "hardlink-transaction-lock"
$paths = Get-XbWorkerUpgradePaths
$externalLock = Join-Path $WorkRoot "external-lock-target.bin"
[IO.File]::WriteAllText($externalLock, "synthetic external lock sentinel`n", ([Text.UTF8Encoding]::new($false)))
$externalLockSha = Get-XbFileSha256 -Path $externalLock
$null = & "$env:SystemRoot\System32\cmd.exe" /d /c mklink /H $paths.lock $externalLock
$lockLinkCreated = (Test-Path -LiteralPath $paths.lock) -and ([XbWorkerUpgradeNativeFileInfo]::LinkCount($paths.lock) -eq 2)
$lockLinkInstall = Get-XbFixtureTreeSnapshot -Root $script:InstallRoot
$lockLinkOutcome = Invoke-XbFixtureOperation "Rollback"
$out.adversarial.hardlink_transaction_lock = [ordered]@{
    created = $lockLinkCreated
    outcome = $lockLinkOutcome
    external_preserved = ((Get-XbFileSha256 -Path $externalLock) -ceq $externalLockSha)
    install_unchanged = ((Get-XbFixtureTreeSnapshot -Root $script:InstallRoot) -ceq $lockLinkInstall)
}

$null = Initialize-XbFixture "hardlink-transaction-temp"
$paths = Get-XbWorkerUpgradePaths
$externalTemp = Join-Path $WorkRoot "external-temp-target.bin"
[IO.File]::WriteAllText($externalTemp, "synthetic external temp sentinel`n", ([Text.UTF8Encoding]::new($false)))
$externalTempSha = Get-XbFileSha256 -Path $externalTemp
$null = & "$env:SystemRoot\System32\cmd.exe" /d /c mklink /H $paths.journal_temp $externalTemp
$tempLinkCreated = (Test-Path -LiteralPath $paths.journal_temp) -and ([XbWorkerUpgradeNativeFileInfo]::LinkCount($paths.journal_temp) -eq 2)
$tempLinkInstall = Get-XbFixtureTreeSnapshot -Root $script:InstallRoot
$tempLinkOutcome = Invoke-XbFixtureOperation "Rollback"
$out.adversarial.hardlink_transaction_temp = [ordered]@{
    created = $tempLinkCreated
    outcome = $tempLinkOutcome
    external_preserved = ((Get-XbFileSha256 -Path $externalTemp) -ceq $externalTempSha)
    temp_preserved = (Test-Path -LiteralPath $paths.journal_temp)
    install_unchanged = ((Get-XbFixtureTreeSnapshot -Root $script:InstallRoot) -ceq $tempLinkInstall)
}

$null = Initialize-XbFixture "concurrent-lock"
$lockPath = (Get-XbWorkerUpgradePaths).lock
$lockHandle = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
$out.adversarial.concurrent_upgrade = [ordered]@{ outcome = (Invoke-XbFixtureOperation "Upgrade"); package_unchanged = (Test-XbFixtureRestored).package }
$lockHandle.Dispose()

$null = Initialize-XbFixture "stage-corruption"
Set-XbFault -Kind journal -Target "staged" -Phase after
$null = Invoke-XbFixtureOperation "Upgrade"
Clear-XbFault
$journal = Read-XbWorkerUpgradeJournal -Path (Get-XbWorkerUpgradePaths).journal
$stageFile = Join-Path $journal.candidate_root $packageFiles[0]
"tampered stage" | Set-Content -LiteralPath $stageFile -Encoding ASCII
$installSnapshot = Get-XbFixtureTreeSnapshot -Root $script:InstallRoot
$out.adversarial.stage_corruption = [ordered]@{
    outcome = (Invoke-XbFixtureOperation "Rollback")
    installed_old_unchanged = ((Get-XbFixtureTreeSnapshot -Root $script:InstallRoot) -ceq $installSnapshot)
    task_unchanged = ((Get-XbFixtureTaskSnapshot) -ceq $script:taskBaseline)
    runtime_unchanged = ((Get-XbFixtureRuntimeSnapshot) -ceq $script:runtimeBaseline)
}

$null = Initialize-XbFixture "custody-corruption"
Set-XbFault -Kind move -Index 1 -Phase after
$null = Invoke-XbFixtureOperation "Upgrade"
Clear-XbFault
$journal = Read-XbWorkerUpgradeJournal -Path (Get-XbWorkerUpgradePaths).journal
$custodyFile = Join-Path $journal.previous_root $packageFiles[0]
"tampered custody" | Set-Content -LiteralPath $custodyFile -Encoding ASCII
$custodySnapshot = Get-XbFixtureTreeSnapshot -Root $journal.previous_root
$out.adversarial.recovery_custody_corruption = [ordered]@{
    outcome = (Invoke-XbFixtureOperation "Rollback")
    install_absent = (-not (Test-Path -LiteralPath $script:InstallRoot))
    custody_unchanged = ((Get-XbFixtureTreeSnapshot -Root $journal.previous_root) -ceq $custodySnapshot)
    task_unchanged = ((Get-XbFixtureTaskSnapshot) -ceq $script:taskBaseline)
    runtime_unchanged = ((Get-XbFixtureRuntimeSnapshot) -ceq $script:runtimeBaseline)
}

$null = Initialize-XbFixture "partial-staging"
Set-XbFault -Kind journal -Target "preparing" -Phase after
$partialCrash = Invoke-XbFixtureOperation "Upgrade"
Clear-XbFault
$journal = Read-XbWorkerUpgradeJournal -Path (Get-XbWorkerUpgradePaths).journal
New-Item -ItemType Directory -Path $journal.candidate_root -ErrorAction Stop | Out-Null
[IO.File]::WriteAllText((Join-Path $journal.candidate_root $packageFiles[0]), "interrupted synthetic copy")
$partialRecovery = Invoke-XbFixtureOperation "Rollback"
$out.adversarial.partial_staging_recovery = [ordered]@{ operation = $partialCrash; outcome = $partialRecovery; restored = (Test-XbFixtureRestored); candidate_absent = (-not (Test-Path -LiteralPath $journal.candidate_root)) }

$null = Initialize-XbFixture "foreign-staging"
Set-XbFault -Kind journal -Target "preparing" -Phase after
$foreignCrash = Invoke-XbFixtureOperation "Upgrade"
Clear-XbFault
$journal = Read-XbWorkerUpgradeJournal -Path (Get-XbWorkerUpgradePaths).journal
New-Item -ItemType Directory -Path $journal.candidate_root -ErrorAction Stop | Out-Null
[IO.File]::WriteAllText((Join-Path $journal.candidate_root "foreign.txt"), "unknown synthetic content")
$foreignStageSnapshot = Get-XbFixtureTreeSnapshot -Root $journal.candidate_root
$foreignRecovery = Invoke-XbFixtureOperation "Rollback"
$out.adversarial.foreign_staging = [ordered]@{ operation = $foreignCrash; outcome = $foreignRecovery; install_unchanged = (Test-XbFixtureRestored).package; candidate_preserved = ((Get-XbFixtureTreeSnapshot -Root $journal.candidate_root) -ceq $foreignStageSnapshot) }

# Reconcile disk states left by an interrupted temporary journal write in a new
# PowerShell process, and reject a corrupt committed journal without changing it.
$null = Initialize-XbFixture "journal-temp-no-commit"
$paths = Get-XbWorkerUpgradePaths
[IO.File]::WriteAllText($paths.journal_temp, '{"schema_version":"xb.member', ([Text.UTF8Encoding]::new($false)))
$tempNoCommitInstall = Get-XbFixtureTreeSnapshot -Root $script:InstallRoot
$tempNoCommitRecovery = Invoke-XbFreshHarnessChild -ChildMode recover -Name "journal-temp-no-commit"
$tempNoCommitReport = $tempNoCommitRecovery.output | ConvertFrom-Json
$out.adversarial.journal_temp_no_commit = [ordered]@{
    fresh_process = ($tempNoCommitRecovery.exit_code -eq 0)
    first = [string]$tempNoCommitReport.first
    repeated = [string]$tempNoCommitReport.repeated
    temp_absent = (-not (Test-Path -LiteralPath $paths.journal_temp))
    install_unchanged = ((Get-XbFixtureTreeSnapshot -Root $script:InstallRoot) -ceq $tempNoCommitInstall)
    restored = $tempNoCommitReport.restored
}

$null = Initialize-XbFixture "journal-temp-with-commit"
$null = Invoke-XbFixtureOperation "Upgrade"
$paths = Get-XbWorkerUpgradePaths
[IO.File]::WriteAllText($paths.journal_temp, '{"interrupted":', ([Text.UTF8Encoding]::new($false)))
$tempWithCommitRecovery = Invoke-XbFreshHarnessChild -ChildMode recover -Name "journal-temp-with-commit"
$tempWithCommitReport = $tempWithCommitRecovery.output | ConvertFrom-Json
$out.adversarial.journal_temp_with_commit = [ordered]@{
    fresh_process = ($tempWithCommitRecovery.exit_code -eq 0)
    first = [string]$tempWithCommitReport.first
    repeated = [string]$tempWithCommitReport.repeated
    temp_absent = (-not (Test-Path -LiteralPath $paths.journal_temp))
    restored = $tempWithCommitReport.restored
    source_reads = [int]$tempWithCommitReport.source_reads
}

$null = Initialize-XbFixture "corrupt-committed-journal"
$null = Invoke-XbFixtureOperation "Upgrade"
$paths = Get-XbWorkerUpgradePaths
$corruptInstall = Get-XbFixtureTreeSnapshot -Root $script:InstallRoot
$journal = Read-XbWorkerUpgradeJournal -Path $paths.journal
$corruptPrevious = Get-XbFixtureTreeSnapshot -Root $journal.previous_root
[IO.File]::WriteAllText($paths.journal, '{"torn":', ([Text.UTF8Encoding]::new($false)))
$corruptJournalBytes = Get-XbFileSha256 -Path $paths.journal
$corruptRecovery = Invoke-XbFreshHarnessChild -ChildMode recover -Name "corrupt-committed-journal"
$corruptRecoveryReport = $corruptRecovery.output | ConvertFrom-Json
$out.adversarial.corrupt_committed_journal = [ordered]@{
    fresh_process = ($corruptRecovery.exit_code -eq 0)
    first = [string]$corruptRecoveryReport.first
    repeated = [string]$corruptRecoveryReport.repeated
    journal_preserved = ((Get-XbFileSha256 -Path $paths.journal) -ceq $corruptJournalBytes)
    install_unchanged = ((Get-XbFixtureTreeSnapshot -Root $script:InstallRoot) -ceq $corruptInstall)
    previous_unchanged = ((Get-XbFixtureTreeSnapshot -Root $journal.previous_root) -ceq $corruptPrevious)
    task_unchanged = ((Get-XbFixtureTaskSnapshot) -ceq $script:taskBaseline)
    runtime_unchanged = ((Get-XbFixtureRuntimeSnapshot) -ceq $script:runtimeBaseline)
}

$out.adversarial.acl_preservation = @{}
$null = Initialize-XbFixture "acl-preimage"
$rootAcl = Get-Acl -LiteralPath $script:InstallRoot
$rootAcl.SetAccessRuleProtection($true, $true)
Set-Acl -LiteralPath $script:InstallRoot -AclObject $rootAcl
$saclControl = [ordered]@{ supported = ($script:auditAclEvidence -eq "native"); evidence = $script:auditAclEvidence }
$saclPath = Join-Path $script:InstallRoot $packageFiles[0]
if ($saclControl.supported) {
    $auditAcl = Get-Acl -LiteralPath $saclPath -Audit -ErrorAction Stop
    $auditIdentity = [Security.Principal.SecurityIdentifier]::new("S-1-1-0")
    $auditFlags = [Security.AccessControl.AuditFlags]::Success -bor [Security.AccessControl.AuditFlags]::Failure
    $auditRule = [Security.AccessControl.FileSystemAuditRule]::new($auditIdentity, [Security.AccessControl.FileSystemRights]::ReadData, $auditFlags)
    $null = $auditAcl.AddAuditRule($auditRule)
    Set-Acl -LiteralPath $saclPath -AclObject $auditAcl -ErrorAction Stop
    $saclControl.before = Convert-XbWorkerUpgradeAclSddl (Get-XbWorkerUpgradeAclSddl -Path $saclPath)
}
$script:baseline = Get-XbWorkerPackageIdentity -Root $script:InstallRoot
$aclBefore = Get-XbFixtureAclSnapshot -Root $script:InstallRoot
$out.adversarial.acl_preservation.upgrade = Invoke-XbFixtureOperation "Upgrade"
$journal = Read-XbWorkerUpgradeJournal -Path (Get-XbWorkerUpgradePaths).journal
$aclAfterUpgrade = Get-XbFixtureAclSnapshot -Root $script:InstallRoot
$out.adversarial.acl_preservation.upgrade_preserved = ($aclAfterUpgrade -ceq $aclBefore)
if ($saclControl.supported) {
    $saclAfterUpgrade = Convert-XbWorkerUpgradeAclSddl (Get-XbWorkerUpgradeAclSddl -Path (Join-Path $script:InstallRoot $packageFiles[0]))
    $saclControl.upgrade_preserved = ($saclAfterUpgrade -ceq $saclControl.before)
}
$out.adversarial.acl_preservation.rollback = Invoke-XbFixtureOperation "Rollback"
$aclAfterRollback = Get-XbFixtureAclSnapshot -Root $script:InstallRoot
$out.adversarial.acl_preservation.rollback_preserved = ($aclAfterRollback -ceq $aclBefore)
if ($saclControl.supported) {
    $saclAfterRollback = Convert-XbWorkerUpgradeAclSddl (Get-XbWorkerUpgradeAclSddl -Path (Join-Path $script:InstallRoot $packageFiles[0]))
    $saclControl.rollback_preserved = ($saclAfterRollback -ceq $saclControl.before)
}
$out.adversarial.acl_preservation.sacl_control = $saclControl

$null = Initialize-XbFixture "acl-denied"
$paths = Get-XbWorkerUpgradePaths
$lockSeed = [IO.File]::Open($paths.lock, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
$lockSeed.Dispose()
$parent = Split-Path -Parent $script:InstallRoot
$parentAcl = Get-Acl -LiteralPath $parent
$deniedSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$deny = [Security.AccessControl.FileSystemAccessRule]::new($deniedSid, [Security.AccessControl.FileSystemRights]::CreateDirectories, [Security.AccessControl.InheritanceFlags]::None, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Deny)
$denyAcl = Get-Acl -LiteralPath $parent
$denyAcl.AddAccessRule($deny)
Set-Acl -LiteralPath $parent -AclObject $denyAcl
$observedDeny = Get-Acl -LiteralPath $parent
$denyApplied = @($observedDeny.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object { [string]$_.IdentityReference -ceq [string]$deniedSid -and $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny -and (([int]$_.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::CreateDirectories) -ne 0) }).Count -gt 0
$deniedSnapshot = Get-XbFixtureTreeSnapshot -Root $script:InstallRoot
$deniedOutcome = Invoke-XbFixtureOperation "Upgrade"
Set-Acl -LiteralPath $parent -AclObject $parentAcl
$out.adversarial.denied_permission = [ordered]@{ deny_applied = $denyApplied; outcome = $deniedOutcome; install_unchanged = ((Get-XbFixtureTreeSnapshot -Root $script:InstallRoot) -ceq $deniedSnapshot); runtime_unchanged = ((Get-XbFixtureRuntimeSnapshot) -ceq $script:runtimeBaseline) }

$null = Initialize-XbFixture "install-reparse"
$movedInstall = $script:InstallRoot + ".owned"
[IO.Directory]::Move($script:InstallRoot, $movedInstall)
$null = & cmd.exe /c mklink /J $script:InstallRoot $movedInstall
$junctionExists = ((Get-Item -LiteralPath $script:InstallRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
$out.adversarial.install_reparse = [ordered]@{ created = $junctionExists; outcome = (Invoke-XbFixtureOperation "Upgrade"); target_present = (Test-Path -LiteralPath (Join-Path $movedInstall "installation-manifest.json")) }
if ($junctionExists) { $null = & cmd.exe /c rmdir $script:InstallRoot; [IO.Directory]::Move($movedInstall, $script:InstallRoot) }

$null = Initialize-XbFixture "parent-reparse"
$physicalParent = Split-Path -Parent (Split-Path -Parent $script:InstallRoot)
$movedParent = $physicalParent + ".owned"
[IO.Directory]::Move($physicalParent, $movedParent)
$null = & cmd.exe /c mklink /J $physicalParent $movedParent
$parentJunctionExists = ((Get-Item -LiteralPath $physicalParent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
$parentOutcome = Invoke-XbFixtureOperation "Upgrade"
$parentTargetPresent = Test-Path -LiteralPath (Join-Path $movedParent "X-Boundaries\MemberGatewayWorker\installation-manifest.json")
if ($parentJunctionExists) { $null = & cmd.exe /c rmdir $physicalParent; [IO.Directory]::Move($movedParent, $physicalParent) }
$out.adversarial.parent_reparse = [ordered]@{ created = $parentJunctionExists; outcome = $parentOutcome; target_present = $parentTargetPresent }

$null = Initialize-XbFixture "state-reparse"
Set-XbFault -Kind journal -Target "preparing" -Phase after
$null = Invoke-XbFixtureOperation "Upgrade"
Clear-XbFault
$journal = Read-XbWorkerUpgradeJournal -Path (Get-XbWorkerUpgradePaths).journal
$paths = Get-XbWorkerUpgradePaths
$external = Join-Path $WorkRoot "state-alias-target"
New-Item -ItemType Directory -Path $external -Force | Out-Null
"external sentinel" | Set-Content -LiteralPath (Join-Path $external "keep.txt")
$null = & cmd.exe /c mklink /J $journal.candidate_root $external
$aliasExists = ((Get-Item -LiteralPath $journal.candidate_root -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
$aliasOutcome = Invoke-XbFixtureOperation "Rollback"
$externalSnapshot = Get-XbFixtureTreeSnapshot -Root $external
if ($aliasExists) { $null = & cmd.exe /c rmdir $journal.candidate_root }
$recoveredAfterAliasRemoval = Invoke-XbFixtureOperation "Rollback"
$out.adversarial.state_reparse = [ordered]@{ created = $aliasExists; outcome = $aliasOutcome; external_preserved = ((Get-XbFixtureTreeSnapshot -Root $external) -ceq $externalSnapshot); recovered_after_removal = $recoveredAfterAliasRemoval; restored = (Test-XbFixtureRestored) }

# Prove Rollback needs neither package source bytes nor worker/task credentials.
$script:InstallRoot = $script:positiveFixture.install
$script:RuntimeRoot = $script:positiveFixture.runtime
$script:taskPath = $script:positiveFixture.task_path
$script:taskName = $script:positiveFixture.task_name
$script:fixtureTask = $script:positiveFixture.task
$script:fixtureView = $script:positiveFixture.view
$script:baseline = $script:positiveFixture.baseline
$script:taskBaseline = $script:positiveFixture.task_baseline
$script:runtimeBaseline = $script:positiveFixture.runtime_baseline
$script:reviewForbidden = $true
$script:reviewCalls = 0
Remove-Item -LiteralPath $script:SourceRoot -Recurse -Force -ErrorAction Stop
$out.positive.rollback = Invoke-XbFixtureOperation "Rollback"
$out.positive.rollback_again = Invoke-XbFixtureOperation "Rollback"
$out.positive.rollback_source_reads = $script:reviewCalls
$out.positive.rollback_without_credentials = [string]::IsNullOrEmpty([string]$script:WorkerAccount) -and $null -eq $script:TaskCredential
$script:reviewForbidden = $false
$out.positive.restored = Test-XbFixtureRestored
$out.positive.task_after_rollback = ((Get-XbFixtureTaskSnapshot) -ceq $script:taskBaseline)
$out.positive.runtime_after_rollback = ((Get-XbFixtureRuntimeSnapshot) -ceq $script:runtimeBaseline)

$out.forbidden_call_count = $script:forbiddenCalls
$out.native = [ordered]@{ powershell = $PSVersionTable.PSVersion.ToString(); edition = $PSVersionTable.PSEdition; architecture_bits = ([IntPtr]::Size * 8); administrator_gate_stubbed = $true; scheduler_task_mutations = "none (task fixture held in memory)"; audit_acl_evidence = $script:auditAclEvidence }
[Console]::Out.WriteLine(($out | ConvertTo-Json -Depth 16 -Compress))
'''


_HOSTED_TASK_BOUNDARY_HARNESS = r'''[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InstallerPath,
    [Parameter(Mandatory)][string]$ReviewedManifestPath
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

function Invoke-XbBoundaryCase {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][scriptblock]$Body)
    $record = [ordered]@{ status = "error" }
    try {
        $data = @(& $Body)
        if ($data.Count -gt 0 -and $data[-1] -is [Collections.IDictionary]) { foreach ($key in @($data[-1].Keys)) { $record[$key] = $data[-1][$key] } }
        $record.status = "completed"
    } catch {
        $record.error = [string]$_.Exception.Message
        $record.error_stack = [string]$_.ScriptStackTrace
    }
    $report.cases[$Name] = $record
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
    $report.environment.worker_account = $workerAccount
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
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ('-NoLogo -NoProfile -NonInteractive -File "{0}" -Mode DisabledProof' -f $fakeLauncher)
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
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ('-NoLogo -NoProfile -NonInteractive -File "{0}" -Mode DisabledProof' -f $fakeLauncher)
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

    Invoke-XbBoundaryCase "install_then_uninstall" {
        Set-XbProductionTaskIdentity
        $script:TaskCredential = $credential
        $trace.Clear()
        $installOutcome = Get-XbBoundaryOutcome { Invoke-XbWorkerInstaller }
        $installed = Get-XbProductionReadback
        $contract = Get-XbBoundaryOutcome { Assert-XbWorkerTaskContract -Task (Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction Stop) }
        $ownership = Get-XbBoundaryOutcome { Assert-XbUninstallOwnership }
        $evidence = Get-XbTaskEvidence
        $manifest = Get-Content -Raw -LiteralPath (Join-Path $InstallRoot "installation-manifest.json") | ConvertFrom-Json
        $accountInstalled = Get-XbAccountObservation
        $script:Operation = "Uninstall"
        $uninstallOutcome = Get-XbBoundaryOutcome { Invoke-XbWorkerInstaller }
        $uninstalled = Get-XbProductionReadback
        $accountUninstalled = Get-XbAccountObservation
        $rollbackTrace = @($trace)
        Clear-XbRetainedProductionContainers
        return [ordered]@{
            install_outcome = $installOutcome
            installed = $installed
            contract = $contract
            ownership = $ownership
            evidence = $evidence
            manifest_trigger_count = [int]$manifest.task.trigger_count
            account_installed = $accountInstalled
            uninstall_outcome = $uninstallOutcome
            uninstalled = $uninstalled
            account_uninstalled = $accountUninstalled
            rollback_trace = $rollbackTrace
            post_cleanup = (Get-XbProductionReadback)
        }
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

    def test_upgrade_and_rollback_are_separate_non_destructive_operations(self) -> None:
        self.assertIn('[ValidateSet("Install", "Uninstall", "ValidateOnly", "Upgrade", "Rollback")]', self.source)
        dispatcher = _installer_function(self.source, "Invoke-XbWorkerInstaller")
        rollback = _installer_function(self.source, "Invoke-XbWorkerRollback")
        upgrade = _installer_function(self.source, "Invoke-XbWorkerUpgrade")
        self.assertLess(dispatcher.index('if ($Operation -ne "Rollback")'), dispatcher.index("Read-XbReviewedPackageIdentity"))
        self.assertIn("Enter-XbWorkerUpgradeLock", dispatcher)
        self.assertIn("Assert-XbWorkerAdministrator", dispatcher)
        self.assertLess(dispatcher.index("Assert-XbWorkerUpgradeFileNoHardLinks -Path $ReviewedPackageManifestPath"), dispatcher.index("Read-XbReviewedPackageIdentity -Path $ReviewedPackageManifestPath"))
        for forbidden in ("Read-XbReviewedPackageIdentity", "New-XbWorkerInstallationManifest", "Register-XbWorkerScheduledTask", "Remove-XbWorkerOwnedState", "Remove-XbWorkerScheduledTask", "Set-XbWorkerAcl"):
            with self.subTest(rollback_forbidden=forbidden):
                self.assertNotIn(forbidden, rollback)
        for forbidden in ("Invoke-WebRequest", "Invoke-RestMethod", "System.Net.Http", "git.exe"):
            with self.subTest(rollback_external_access=forbidden):
                self.assertNotIn(forbidden, rollback)
        self.assertLess(upgrade.index('Write-XbWorkerUpgradeJournal -Journal $journal'), upgrade.index('Move-XbWorkerUpgradeDirectory -Source $paths.install'))
        self.assertIn('$journal.state = "old_move_intent"', upgrade)
        self.assertIn('$journal.state = "new_move_intent"', upgrade)
        self.assertIn('$journal.state = "complete"', upgrade)
        for forbidden in ("Get-ChildItem -LiteralPath $RuntimeRoot", "Get-Content -LiteralPath $RuntimeRoot", "Set-XbWorkerAcl", "Register-XbWorkerScheduledTask", "Remove-XbWorkerOwnedState", "Start-ScheduledTask", "GetMember", "SaveMember", "AutoCount"):
            with self.subTest(upgrade_forbidden=forbidden):
                self.assertNotIn(forbidden, upgrade)

    def test_journal_uses_flushed_write_through_moves_and_same_parent_directories(self) -> None:
        writer = _installer_function(self.source, "Write-XbWorkerUpgradeJournal")
        mover = _installer_function(self.source, "Move-XbWorkerUpgradeDirectory")
        self.assertIn("$stream.Flush($true)", writer)
        self.assertIn("[IO.FileOptions]::WriteThrough", writer)
        self.assertIn("Use-XbWorkerUpgradeNativeMove", writer)
        self.assertIn("[XbWorkerUpgradeNativeMove]::Move", writer)
        self.assertIn("[IO.Path]::GetPathRoot", mover)
        self.assertIn("Use-XbWorkerUpgradeNativeMove", mover)

    def test_upgrade_identity_rejects_file_aliases_and_unverifiable_audit_acl(self) -> None:
        file_identity = _installer_function(self.source, "Assert-XbWorkerUpgradeFileNoHardLinks")
        package_identity = _installer_function(self.source, "Get-XbWorkerPackageIdentity")
        journal_read = _installer_function(self.source, "Read-XbWorkerUpgradeJournal")
        acl_read = _installer_function(self.source, "Get-XbWorkerUpgradeAclSddl")
        journal_acl = _installer_function(self.source, "Set-XbWorkerUpgradeJournalAcl")
        self.assertIn("NumberOfLinks", self.source)
        self.assertIn('throw "upgrade_file_alias_ambiguous"', file_identity)
        self.assertIn("Assert-XbWorkerUpgradeFileNoHardLinks -Path $manifestPath", package_identity)
        self.assertIn("Assert-XbWorkerUpgradeFileNoHardLinks -Path $path", package_identity)
        self.assertIn("Assert-XbWorkerUpgradeFileNoHardLinks -Path $Path", journal_read)
        self.assertIn("Get-Acl -LiteralPath $Path -Audit", acl_read)
        self.assertIn('throw "upgrade_acl_unverifiable"', acl_read)
        self.assertIn("$acl.SetOwner($ownerSid)", journal_acl)

    def test_unknown_worker_process_identity_fails_closed(self) -> None:
        stopped = _installer_function(self.source, "Assert-XbWorkerStopped")
        self.assertIn('throw "worker_process_state_unproven"', stopped)
        self.assertIn('$process.PSObject.Properties["CommandLine"]', stopped)

    def test_upgrade_allowlist_is_exact(self) -> None:
        self.assertEqual(
            ALLOWED_FILES,
            {
                "scripts/install_ac2_member_gateway_worker.ps1",
                "tests/test_member_gateway_worker_deployment.py",
                "docs/autocount2-automation/member_gateway_production_runbook.md",
            },
        )

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


class MemberWorkerUpgradeRollbackBehaviorTests(unittest.TestCase):
    """Native PowerShell 5.1 synthetic dispatcher and crash-recovery evidence."""

    report: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        powershell = _resolve_native_powershell()
        if not powershell:
            if _hosted_windows_boundary_required():
                raise AssertionError("native Windows PowerShell is required on the hosted runner")
            raise unittest.SkipTest("Windows PowerShell 5.1 is required for upgrade/rollback behavior validation")
        local_temp = Path(os.environ["LOCALAPPDATA"]) / "Temp"
        with tempfile.TemporaryDirectory(prefix="xbg3-", dir=local_temp) as temp_dir:
            root = Path(temp_dir)
            harness = root / "upgrade_rollback_harness.ps1"
            harness.write_text(_LOCAL_UPGRADE_ROLLBACK_HARNESS, encoding="utf-8", newline="\n")
            work_root = root / "f"
            work_root.mkdir()
            completed = subprocess.run(
                [
                    powershell, "-ExecutionPolicy", "Bypass", "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-File", str(harness), "-InstallerPath", str(ROOT / INSTALLER_PATH), "-WorkRoot", str(work_root),
                ],
                cwd=ROOT,
                env=_windows_powershell_module_environment(),
                capture_output=True,
                text=True,
                timeout=900,
            )
        if completed.returncode != 0:
            raise AssertionError(completed.stdout + completed.stderr)
        try:
            cls.report = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(completed.stdout + completed.stderr) from exc

    def test_dispatcher_upgrade_and_source_independent_rollback(self) -> None:
        positive = self.report["positive"]
        self.assertEqual(positive["upgrade"], "pass")
        self.assertEqual(positive["complete"], "complete")
        self.assertTrue(positive["current_is_new"])
        self.assertTrue(positive["task_unchanged_after_upgrade"])
        self.assertTrue(positive["runtime_unchanged_after_upgrade"])
        self.assertEqual(positive["rollback"], "pass", json.dumps(positive))
        self.assertEqual(positive["rollback_again"], "pass")
        self.assertEqual(positive["rollback_source_reads"], 0)
        self.assertTrue(positive["rollback_without_credentials"], json.dumps(positive))
        self.assertEqual(positive["restored"], {"package": True, "task": True, "runtime": True, "forbidden_calls": 0})
        self.assertTrue(positive["task_after_rollback"])
        self.assertTrue(positive["runtime_after_rollback"])

    def test_every_journal_transition_recovers_before_and_after(self) -> None:
        cases = {name: evidence for name, evidence in self.report["fresh_process_fault_recovery"].items() if "_journal_" in name}
        self.assertEqual(len(cases), 20)
        for name, evidence in cases.items():
            with self.subTest(case=name):
                self.assertEqual(evidence["crash_exit"], 86, evidence)
                if name == "upgrade_journal_preparing_before":
                    self.assertEqual(evidence["first"], "upgrade_transaction_missing")
                    self.assertEqual(evidence["repeated"], "upgrade_transaction_missing")
                else:
                    self.assertEqual(evidence["first"], "pass")
                    self.assertEqual(evidence["repeated"], "pass")
                self.assertEqual(evidence["restored"], {"package": True, "task": True, "runtime": True, "forbidden_calls": 0})

    def test_both_upgrade_and_rollback_directory_renames_recover(self) -> None:
        cases = {name: evidence for name, evidence in self.report["fresh_process_fault_recovery"].items() if "_rename_" in name}
        self.assertEqual(len(cases), 8)
        for name, evidence in cases.items():
            with self.subTest(case=name):
                self.assertEqual(evidence["crash_exit"], 86, evidence)
                self.assertEqual(evidence["first"], "pass")
                self.assertEqual(evidence["repeated"], "pass")
                self.assertEqual(evidence["restored"], {"package": True, "task": True, "runtime": True, "forbidden_calls": 0})

    def test_every_fault_recovers_in_a_fresh_powershell_process(self) -> None:
        cases = self.report["fresh_process_fault_recovery"]
        self.assertEqual(len(cases), 29)
        for name, evidence in cases.items():
            with self.subTest(case=name):
                self.assertEqual(evidence["crash_exit"], 86, evidence)
                if name == "upgrade_journal_preparing_before":
                    self.assertEqual(evidence["first"], "upgrade_transaction_missing")
                    self.assertEqual(evidence["repeated"], "upgrade_transaction_missing")
                else:
                    self.assertEqual(evidence["first"], "pass")
                    self.assertEqual(evidence["repeated"], "pass")
                expected_restored = {"package": True, "task": True, "runtime": True, "forbidden_calls": 0}
                if name == "runtime_drift_control":
                    expected_restored["runtime"] = False
                    self.assertTrue(evidence["runtime_drift_injected"])
                else:
                    self.assertFalse(evidence["runtime_drift_injected"])
                self.assertEqual(evidence["restored"], expected_restored)
                self.assertTrue(evidence["source_root_absent"])
                self.assertTrue(evidence["dispatcher_package_files_absent"])
                self.assertEqual(evidence["runtime_baseline"], "pre-crash fixture evidence")
                self.assertEqual(evidence["source_reads"], 0)
                self.assertEqual(evidence["forbidden_calls"], 0)

    def test_precrash_runtime_baseline_detects_post_crash_drift(self) -> None:
        evidence = self.report["fresh_process_fault_recovery"]["runtime_drift_control"]
        self.assertTrue(evidence["runtime_drift_injected"])
        self.assertEqual(evidence["runtime_baseline"], "pre-crash fixture evidence")
        self.assertEqual(evidence["restored"], {"package": True, "task": True, "runtime": False, "forbidden_calls": 0})

    def test_adversarial_package_task_owner_and_runtime_controls(self) -> None:
        adversarial = self.report["adversarial"]
        self.assertEqual(adversarial["wrong_source_sha"]["outcome"], "reviewed_package_source_bytes_mismatch")
        self.assertTrue(adversarial["wrong_source_sha"]["unchanged"])
        self.assertTrue(adversarial["wrong_source_sha"]["runtime_unchanged"])
        for name in ("missing_file", "extra_file", "duplicate_file", "tampered_manifest", "stale_package_sha"):
            with self.subTest(preimage=name):
                self.assertNotEqual(adversarial[name]["outcome"], "pass")
                self.assertTrue(adversarial[name]["unchanged"])
                self.assertTrue(adversarial[name]["runtime_unchanged"])
        for name in ("enabled", "triggered", "action", "principal"):
            with self.subTest(task=name):
                self.assertNotEqual(adversarial["task"][name]["outcome"], "pass")
                self.assertTrue(adversarial["task"][name]["unchanged"])
                self.assertTrue(adversarial["task"][name]["package_unchanged"])
        self.assertEqual(adversarial["active_worker"]["outcome"], "worker_process_active")
        self.assertTrue(adversarial["active_worker"]["package_unchanged"])
        self.assertEqual(adversarial["unknown_worker_process"]["outcome"], "worker_process_state_unproven")
        self.assertTrue(adversarial["unknown_worker_process"]["package_unchanged"])
        self.assertTrue(adversarial["hardlink_package_payload"]["created"])
        self.assertEqual(adversarial["hardlink_package_payload"]["outcome"], "upgrade_file_alias_ambiguous")
        self.assertEqual(adversarial["hardlink_package_payload"]["alias_hash_calls"], 0)
        self.assertTrue(adversarial["hardlink_package_payload"]["external_preserved"])
        self.assertTrue(adversarial["hardlink_package_payload"]["package_unchanged"])
        self.assertTrue(adversarial["hardlink_package_manifest"]["created"])
        self.assertEqual(adversarial["hardlink_package_manifest"]["outcome"], "upgrade_file_alias_ambiguous")
        self.assertEqual(adversarial["hardlink_package_manifest"]["manifest_read_calls"], 0)
        self.assertTrue(adversarial["hardlink_package_manifest"]["external_preserved"])
        self.assertTrue(adversarial["hardlink_package_manifest"]["package_unchanged"])
        self.assertTrue(adversarial["hardlink_reviewed_source"]["created"])
        self.assertEqual(adversarial["hardlink_reviewed_source"]["outcome"], "upgrade_file_alias_ambiguous")
        self.assertEqual(adversarial["hardlink_reviewed_source"]["source_reads"], 0)
        self.assertTrue(adversarial["hardlink_reviewed_source"]["external_preserved"])
        self.assertTrue(adversarial["hardlink_reviewed_source"]["install_unchanged"])
        self.assertTrue(adversarial["hardlink_reviewed_source"]["runtime_unchanged"])
        self.assertTrue(adversarial["hardlink_reviewed_manifest"]["created"])
        self.assertEqual(adversarial["hardlink_reviewed_manifest"]["outcome"], "upgrade_file_alias_ambiguous")
        self.assertEqual(adversarial["hardlink_reviewed_manifest"]["source_reads"], 0)
        self.assertTrue(adversarial["hardlink_reviewed_manifest"]["external_preserved"])
        self.assertTrue(adversarial["hardlink_reviewed_manifest"]["install_unchanged"])
        self.assertTrue(adversarial["hardlink_reviewed_manifest"]["runtime_unchanged"])
        self.assertTrue(adversarial["hardlink_transaction_journal"]["created"])
        self.assertEqual(adversarial["hardlink_transaction_journal"]["outcome"], "upgrade_file_alias_ambiguous")
        self.assertTrue(adversarial["hardlink_transaction_journal"]["external_preserved"])
        self.assertTrue(adversarial["hardlink_transaction_journal"]["install_unchanged"])
        self.assertTrue(adversarial["hardlink_transaction_journal"]["previous_unchanged"])
        self.assertTrue(adversarial["hardlink_transaction_lock"]["created"])
        self.assertEqual(adversarial["hardlink_transaction_lock"]["outcome"], "upgrade_file_alias_ambiguous")
        self.assertTrue(adversarial["hardlink_transaction_lock"]["external_preserved"])
        self.assertTrue(adversarial["hardlink_transaction_lock"]["install_unchanged"])
        self.assertTrue(adversarial["hardlink_transaction_temp"]["created"])
        self.assertEqual(adversarial["hardlink_transaction_temp"]["outcome"], "upgrade_file_alias_ambiguous")
        self.assertTrue(adversarial["hardlink_transaction_temp"]["external_preserved"])
        self.assertTrue(adversarial["hardlink_transaction_temp"]["temp_preserved"])
        self.assertTrue(adversarial["hardlink_transaction_temp"]["install_unchanged"])
        self.assertEqual(adversarial["concurrent_upgrade"]["outcome"], "upgrade_maintenance_owner_present")
        self.assertTrue(adversarial["concurrent_upgrade"]["package_unchanged"])
        self.assertEqual(self.report["forbidden_call_count"], 0)

    def test_acl_reparse_and_corruption_controls_fail_closed(self) -> None:
        adversarial = self.report["adversarial"]
        acl = adversarial["acl_preservation"]
        self.assertEqual(acl["upgrade"], "pass", json.dumps(acl))
        self.assertTrue(acl["upgrade_preserved"])
        self.assertEqual(acl["rollback"], "pass")
        self.assertTrue(acl["rollback_preserved"])
        if acl["sacl_control"]["supported"]:
            self.assertTrue(acl["sacl_control"]["upgrade_preserved"])
            self.assertTrue(acl["sacl_control"]["rollback_preserved"])
        else:
            self.assertEqual(acl["sacl_control"]["evidence"], "synthetic_dacl_only: SeSecurityPrivilege unavailable")
        self.assertTrue(adversarial["denied_permission"]["deny_applied"])
        self.assertNotEqual(adversarial["denied_permission"]["outcome"], "pass")
        self.assertTrue(adversarial["denied_permission"]["install_unchanged"])
        self.assertTrue(adversarial["denied_permission"]["runtime_unchanged"])
        self.assertTrue(adversarial["install_reparse"]["created"])
        self.assertEqual(adversarial["install_reparse"]["outcome"], "upgrade_path_reparse_point")
        self.assertTrue(adversarial["install_reparse"]["target_present"])
        self.assertTrue(adversarial["parent_reparse"]["created"])
        self.assertEqual(adversarial["parent_reparse"]["outcome"], "upgrade_path_reparse_point")
        self.assertTrue(adversarial["parent_reparse"]["target_present"])
        self.assertEqual(adversarial["stage_corruption"]["outcome"], "upgrade_stage_sha_mismatch")
        self.assertTrue(adversarial["stage_corruption"]["installed_old_unchanged"])
        self.assertTrue(adversarial["stage_corruption"]["task_unchanged"])
        self.assertTrue(adversarial["stage_corruption"]["runtime_unchanged"])
        self.assertNotEqual(adversarial["recovery_custody_corruption"]["outcome"], "pass")
        self.assertTrue(adversarial["recovery_custody_corruption"]["install_absent"])
        self.assertTrue(adversarial["recovery_custody_corruption"]["custody_unchanged"])
        self.assertEqual(adversarial["partial_staging_recovery"]["operation"], "synthetic_process_crash")
        self.assertEqual(adversarial["partial_staging_recovery"]["outcome"], "pass")
        self.assertEqual(adversarial["partial_staging_recovery"]["restored"], {"package": True, "task": True, "runtime": True, "forbidden_calls": 0})
        self.assertTrue(adversarial["partial_staging_recovery"]["candidate_absent"])
        self.assertEqual(adversarial["foreign_staging"]["outcome"], "upgrade_stage_membership_invalid")
        self.assertTrue(adversarial["foreign_staging"]["install_unchanged"])
        self.assertTrue(adversarial["foreign_staging"]["candidate_preserved"])
        self.assertTrue(adversarial["journal_temp_no_commit"]["fresh_process"])
        self.assertEqual(adversarial["journal_temp_no_commit"]["first"], "upgrade_transaction_missing")
        self.assertEqual(adversarial["journal_temp_no_commit"]["repeated"], "upgrade_transaction_missing")
        self.assertTrue(adversarial["journal_temp_no_commit"]["temp_absent"])
        self.assertTrue(adversarial["journal_temp_no_commit"]["install_unchanged"])
        self.assertTrue(adversarial["journal_temp_no_commit"]["restored"]["package"])
        self.assertTrue(adversarial["journal_temp_with_commit"]["fresh_process"])
        self.assertEqual(adversarial["journal_temp_with_commit"]["first"], "pass")
        self.assertEqual(adversarial["journal_temp_with_commit"]["repeated"], "pass")
        self.assertTrue(adversarial["journal_temp_with_commit"]["temp_absent"])
        self.assertEqual(adversarial["journal_temp_with_commit"]["source_reads"], 0)
        self.assertTrue(adversarial["journal_temp_with_commit"]["restored"]["package"])
        self.assertTrue(adversarial["corrupt_committed_journal"]["fresh_process"])
        self.assertEqual(adversarial["corrupt_committed_journal"]["first"], "upgrade_journal_invalid")
        self.assertEqual(adversarial["corrupt_committed_journal"]["repeated"], "upgrade_journal_invalid")
        for preserved in ("journal_preserved", "install_unchanged", "previous_unchanged", "task_unchanged", "runtime_unchanged"):
            self.assertTrue(adversarial["corrupt_committed_journal"][preserved])
        self.assertTrue(adversarial["state_reparse"]["created"])
        self.assertEqual(adversarial["state_reparse"]["outcome"], "upgrade_path_reparse_point")
        self.assertTrue(adversarial["state_reparse"]["external_preserved"])
        self.assertEqual(adversarial["state_reparse"]["recovered_after_removal"], "pass")
        self.assertTrue(adversarial["state_reparse"]["restored"]["package"])

    def test_native_runtime_and_fixture_boundary_are_reported(self) -> None:
        self.assertTrue(self.report["native"]["powershell"].startswith("5.1."))
        self.assertEqual(self.report["native"]["edition"], "Desktop")
        self.assertEqual(self.report["native"]["architecture_bits"], 64)
        self.assertTrue(self.report["native"]["administrator_gate_stubbed"])
        self.assertEqual(self.report["native"]["scheduler_task_mutations"], "none (task fixture held in memory)")
        self.assertIn(self.report["native"]["audit_acl_evidence"], ("native", "synthetic_dacl_only: SeSecurityPrivilege unavailable"))


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
        with tempfile.TemporaryDirectory(prefix="xb-task-boundary-") as temp_dir:
            root = Path(temp_dir)
            manifest = root / "reviewed-package.json"
            manifest.write_text(json.dumps(_reviewed_package_identity(), indent=2) + "\n", encoding="utf-8")
            harness = root / "task_boundary_harness.ps1"
            harness.write_text(_HOSTED_TASK_BOUNDARY_HARNESS, encoding="utf-8", newline="\n")
            completed = subprocess.run(
                [
                    pwsh, "-ExecutionPolicy", "Bypass", "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-File", str(harness),
                    "-InstallerPath", str(ROOT / INSTALLER_PATH),
                    "-ReviewedManifestPath", str(manifest),
                ],
                cwd=ROOT,
                env=_windows_powershell_module_environment(),
                capture_output=True,
                text=True,
                timeout=1500,
            )
        report = _boundary_report(completed.stdout)
        if report is None:
            raise AssertionError(
                "hosted boundary harness produced no report\n" + completed.stdout[-4000:] + completed.stderr[-4000:]
            )
        print(TASK_BOUNDARY_MARKER, flush=True)
        print(json.dumps(report, sort_keys=True), flush=True)
        cls.report = report

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
        self.assertEqual(evidence["action_execute"], "powershell.exe")
        self.assertEqual(evidence["com_action_path"], "powershell.exe")
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

    def test_install_then_uninstall(self) -> None:
        case = self._case("install_then_uninstall")
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
        self._assert_zero_triggers(case["evidence"])
        self._assert_exact_disabled_proof(case["evidence"], self.report["environment"]["worker_account"])
        self._assert_never_run(case["evidence"])
        self._assert_no_worker_session(case["account_installed"])
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


if __name__ == "__main__":
    unittest.main()
