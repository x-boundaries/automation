[CmdletBinding()]
param(
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$JsonOut
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:AcRootPath = [System.IO.Path]::GetFullPath($AcRoot)
$expectedDlls = @(
    "AutoCount.dll",
    "AutoCount.Accounting.dll",
    "AutoCount.Invoicing.dll",
    "AutoCount.ImportExport.dll",
    "AutoCount.Tools.dll"
)
$targetDll = "AutoCount.Invoicing.dll"
$targetTypes = @(
    "AutoCount.BonusPoint.Member.MemberCommand",
    "AutoCount.BonusPoint.Member.MemberEntity",
    "AutoCount.BonusPoint.Member.MemberTypeCommand",
    "AutoCount.BonusPoint.Member.MemberTypeEntity",
    "AutoCount.BonusPoint.Member.MemberRecord",
    "AutoCount.BonusPoint.Member.MemberTypeRecord"
)
$forbiddenMemberApiOperations = @(
    "SaveMember",
    "DeleteMember",
    "SaveMemberType",
    "DeleteMemberType"
)

Write-Warning "AC2 member API probe is metadata-only. It performs reflection, does not instantiate member commands, and does not read member rows."

function Format-TypeName {
    param([Type]$Type)

    if ($null -eq $Type) {
        return ""
    }

    if ([string]::IsNullOrWhiteSpace($Type.FullName)) {
        return $Type.Name
    }

    return $Type.FullName
}

function Format-ParameterList {
    param([System.Reflection.ParameterInfo[]]$Parameters)

    return @($Parameters | ForEach-Object {
        "$(Format-TypeName $_.ParameterType) $($_.Name)"
    })
}

function Get-MemberVisibility {
    param($Member)

    if ($Member.IsPublic) {
        return "public"
    }
    if ($Member.IsFamily) {
        return "protected"
    }
    if ($Member.IsAssembly) {
        return "internal"
    }
    if ($Member.IsFamilyOrAssembly) {
        return "protected internal"
    }
    if ($Member.IsPrivate) {
        return "private"
    }

    return "non-public"
}

function Convert-ConstructorMetadata {
    param([System.Reflection.ConstructorInfo]$Constructor)

    $parameters = @(Format-ParameterList $Constructor.GetParameters())
    return [ordered]@{
        visibility = Get-MemberVisibility $Constructor
        is_static = $Constructor.IsStatic
        parameters = $parameters
        signature = "$($Constructor.DeclaringType.Name)($($parameters -join ', '))"
    }
}

function Convert-MethodMetadata {
    param([System.Reflection.MethodInfo]$Method)

    $parameters = @(Format-ParameterList $Method.GetParameters())
    return [ordered]@{
        name = $Method.Name
        return_type = Format-TypeName $Method.ReturnType
        is_static = $Method.IsStatic
        is_special_name = $Method.IsSpecialName
        parameters = $parameters
        signature = "$(Format-TypeName $Method.ReturnType) $($Method.Name)($($parameters -join ', '))"
    }
}

function Convert-PropertyMetadata {
    param([System.Reflection.PropertyInfo]$Property)

    return [ordered]@{
        name = $Property.Name
        property_type = Format-TypeName $Property.PropertyType
        can_read = $Property.CanRead
        can_write = $Property.CanWrite
    }
}

if (-not (Test-Path -LiteralPath $script:AcRootPath -PathType Container)) {
    throw "AC2 root path was not found: $script:AcRootPath"
}

$assemblyResolveHandler = [System.ResolveEventHandler]{
    param($sender, $eventArgs)

    $assemblyName = [System.Reflection.AssemblyName]::new($eventArgs.Name)
    $candidatePath = Join-Path $script:AcRootPath ($assemblyName.Name + ".dll")
    if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
        return [System.Reflection.Assembly]::LoadFrom($candidatePath)
    }

    return $null
}
[System.AppDomain]::CurrentDomain.add_AssemblyResolve($assemblyResolveHandler)

try {
    $dllStatus = @($expectedDlls | ForEach-Object {
        $path = Join-Path $script:AcRootPath $_
        [ordered]@{
            name = $_
            path = $path
            exists = Test-Path -LiteralPath $path -PathType Leaf
        }
    })

    $targetDllPath = Join-Path $script:AcRootPath $targetDll
    if (-not (Test-Path -LiteralPath $targetDllPath -PathType Leaf)) {
        throw "Required AC2 assembly was not found: $targetDllPath"
    }

    $assembly = [System.Reflection.Assembly]::LoadFrom($targetDllPath)
    $constructorFlags = [System.Reflection.BindingFlags]::Instance -bor
        [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::NonPublic
    $declaredPublicFlags = [System.Reflection.BindingFlags]::Instance -bor
        [System.Reflection.BindingFlags]::Static -bor
        [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::DeclaredOnly

    $typeResults = @($targetTypes | ForEach-Object {
        $typeName = $_
        $type = $assembly.GetType($typeName, $false, $false)
        $typeResult = [ordered]@{
            name = $typeName
            found = $null -ne $type
            constructors = @()
            public_declared_methods = @()
            public_declared_properties = @()
        }

        if ($null -ne $type) {
            $typeResult.constructors = @(
                $type.GetConstructors($constructorFlags) |
                    Sort-Object { $_.ToString() } |
                    ForEach-Object { Convert-ConstructorMetadata $_ }
            )
            $typeResult.public_declared_methods = @(
                $type.GetMethods($declaredPublicFlags) |
                    Sort-Object Name, { $_.ToString() } |
                    ForEach-Object { Convert-MethodMetadata $_ }
            )
            $typeResult.public_declared_properties = @(
                $type.GetProperties($declaredPublicFlags) |
                    Sort-Object Name |
                    ForEach-Object { Convert-PropertyMetadata $_ }
            )
        }

        $typeResult
    })

    $metadata = [ordered]@{
        mode = "metadata-only reflection"
        warning = "No member command instantiation, row reads, writeback, or direct SQL is performed."
        ac_root = $script:AcRootPath
        target_dll = $targetDll
        target_namespace = "AutoCount.BonusPoint.Member"
        expected_dlls = $dllStatus
        loaded_assembly = [ordered]@{
            full_name = $assembly.FullName
            location = $assembly.Location
        }
        inspected_types = $typeResults
        blocked_member_api_operations = $forbiddenMemberApiOperations
    }

    $json = $metadata | ConvertTo-Json -Depth 8

    if (-not [string]::IsNullOrWhiteSpace($JsonOut)) {
        $jsonOutPath = [System.IO.Path]::GetFullPath($JsonOut)
        $jsonOutParent = [System.IO.Path]::GetDirectoryName($jsonOutPath)
        if (-not [string]::IsNullOrWhiteSpace($jsonOutParent)) {
            New-Item -ItemType Directory -Path $jsonOutParent -Force | Out-Null
        }
        Set-Content -LiteralPath $jsonOutPath -Value $json -Encoding UTF8
        Write-Host "Wrote sanitized metadata JSON to $jsonOutPath"
    }

    $json
}
finally {
    [System.AppDomain]::CurrentDomain.remove_AssemblyResolve($assemblyResolveHandler)
}
