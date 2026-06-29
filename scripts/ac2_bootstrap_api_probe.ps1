[CmdletBinding()]
param(
    [string]$AcRoot = "C:\Program Files\AutoCount\Accounting 2.2",
    [string]$JsonOut
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:AcRootPath = [System.IO.Path]::GetFullPath($AcRoot)
$assemblyNames = @(
    "AutoCount.dll",
    "AutoCount.Accounting.dll",
    "AutoCount.Invoicing.dll",
    "AutoCount.ImportExport.dll",
    "AutoCount.Tools.dll"
)
$targetTypes = @(
    "AutoCount.Authentication.UserSession",
    "AutoCount.Data.DBSetting"
)
$candidateTerms = @(
    "UserSession",
    "DBSetting",
    "Login",
    "Authentication",
    "Auth",
    "AccountBook",
    "Company",
    "Database",
    "DB",
    "Session"
)
$blockedRuntimeOperations = @(
    "MemberCommand factory invocation",
    "MemberTypeCommand factory invocation",
    "member list/read",
    "member writeback",
    "database connection"
)

Write-Warning "AC2 bootstrap API probe is metadata-only. It performs reflection only, does not instantiate types, does not invoke factories or methods, and does not connect to a database."

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

function Get-LoadableTypes {
    param([System.Reflection.Assembly]$Assembly)

    try {
        return @($Assembly.GetTypes())
    }
    catch [System.Reflection.ReflectionTypeLoadException] {
        return @($_.Exception.Types | Where-Object { $null -ne $_ })
    }
}

function Test-CandidateName {
    param([string]$FullName)

    foreach ($term in $candidateTerms) {
        if ($FullName.IndexOf($term, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            return $true
        }
    }

    return $false
}

function Convert-TypeMetadata {
    param(
        [Type]$Type,
        [bool]$IsExactTarget
    )

    $constructorFlags = [System.Reflection.BindingFlags]::Instance -bor
        [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::NonPublic
    $publicStaticFlags = [System.Reflection.BindingFlags]::Static -bor
        [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::DeclaredOnly
    $publicInstanceFlags = [System.Reflection.BindingFlags]::Instance -bor
        [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::DeclaredOnly
    $publicPropertyFlags = [System.Reflection.BindingFlags]::Instance -bor
        [System.Reflection.BindingFlags]::Static -bor
        [System.Reflection.BindingFlags]::Public -bor
        [System.Reflection.BindingFlags]::DeclaredOnly

    return [ordered]@{
        assembly_name = $Type.Assembly.GetName().Name
        type_full_name = $Type.FullName
        exact_target = $IsExactTarget
        constructors = @(
            $Type.GetConstructors($constructorFlags) |
                Sort-Object { $_.ToString() } |
                ForEach-Object { Convert-ConstructorMetadata $_ }
        )
        public_static_methods = @(
            $Type.GetMethods($publicStaticFlags) |
                Sort-Object Name, { $_.ToString() } |
                ForEach-Object { Convert-MethodMetadata $_ }
        )
        public_instance_methods = @(
            $Type.GetMethods($publicInstanceFlags) |
                Sort-Object Name, { $_.ToString() } |
                ForEach-Object { Convert-MethodMetadata $_ }
        )
        public_properties = @(
            $Type.GetProperties($publicPropertyFlags) |
                Sort-Object Name |
                ForEach-Object { Convert-PropertyMetadata $_ }
        )
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
    $assemblyStatus = @($assemblyNames | ForEach-Object {
        $path = Join-Path $script:AcRootPath $_
        [ordered]@{
            name = $_
            path = $path
            exists = Test-Path -LiteralPath $path -PathType Leaf
        }
    })

    $loadedAssemblies = @()
    foreach ($status in $assemblyStatus) {
        if ($status.exists) {
            $loadedAssemblies += [System.Reflection.Assembly]::LoadFrom($status.path)
        }
    }

    if ($loadedAssemblies.Count -eq 0) {
        throw "No expected AC2 assemblies were found under: $script:AcRootPath"
    }

    $typesByFullName = @{}
    $candidateTypes = @()
    foreach ($assembly in $loadedAssemblies) {
        foreach ($type in Get-LoadableTypes $assembly) {
            if ([string]::IsNullOrWhiteSpace($type.FullName)) {
                continue
            }

            if (-not $typesByFullName.ContainsKey($type.FullName)) {
                $typesByFullName[$type.FullName] = $type
            }

            if (Test-CandidateName $type.FullName) {
                $candidateTypes += $type
            }
        }
    }

    $exactTargetResults = @($targetTypes | ForEach-Object {
        $typeName = $_
        $type = $typesByFullName[$typeName]
        [ordered]@{
            name = $typeName
            found = $null -ne $type
            metadata = if ($null -ne $type) { Convert-TypeMetadata $type $true } else { $null }
        }
    })

    $candidateResults = @(
        $candidateTypes |
            Sort-Object FullName -Unique |
            ForEach-Object {
                Convert-TypeMetadata $_ ($targetTypes -contains $_.FullName)
            }
    )

    $metadata = [ordered]@{
        mode = "metadata-only reflection"
        warning = "No type instantiation, method invocation, command factory invocation, member read/list, writeback, or database connection is performed."
        ac_root = $script:AcRootPath
        inspected_assemblies = $assemblyStatus
        target_types = $exactTargetResults
        candidate_terms = $candidateTerms
        candidate_types = $candidateResults
        blocked_runtime_operations = $blockedRuntimeOperations
    }

    $json = $metadata | ConvertTo-Json -Depth 10

    if (-not [string]::IsNullOrWhiteSpace($JsonOut)) {
        $jsonOutPath = [System.IO.Path]::GetFullPath($JsonOut)
        $jsonOutParent = [System.IO.Path]::GetDirectoryName($jsonOutPath)
        if (-not [string]::IsNullOrWhiteSpace($jsonOutParent)) {
            New-Item -ItemType Directory -Path $jsonOutParent -Force | Out-Null
        }
        Set-Content -LiteralPath $jsonOutPath -Value $json -Encoding UTF8
        Write-Host "Wrote sanitized bootstrap metadata JSON to $jsonOutPath"
    }

    $json
}
finally {
    [System.AppDomain]::CurrentDomain.remove_AssemblyResolve($assemblyResolveHandler)
}
