[CmdletBinding()]
param(
    [ValidateSet("CapturePlan", "Apply", "Inspect")]
    [string]$Mode = "CapturePlan",
    [string]$RepoRoot = "",
    [string]$OperationId = "",
    [string]$BindingManifestFile = "config/member_forms_gateway_bounded_import.v2.template.json",
    [string]$OperationsRoot = ".n8n-local/member-gateway-bounded-import/operations",
    [string]$N8nExecutable = "n8n",
    [string]$N8nContainer = "",
    [string]$FixtureFile = "",
    [switch]$ConfirmBoundedApply,
    [switch]$TestOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:BoundedLf = [string][char]10

$script:BoundedJsonOptions = New-Object System.Text.Json.JsonSerializerOptions
$script:BoundedPrivateBytesWritten = $false
$script:BoundedRepoRoot = $null
$script:BoundedOperationsRoot = $null
$script:BoundedFixture = $null
$script:BoundedTestOnly = $false
$script:BoundedExitCode = 0

function Stop-Bounded {
    param([Parameter(Mandatory)][string]$Code)
    throw $Code
}

function Get-BoundedComparison {
    if ([System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([System.Runtime.InteropServices.OSPlatform]::Windows)) {
        return [System.StringComparison]::OrdinalIgnoreCase
    }
    return [System.StringComparison]::Ordinal
}

function Resolve-BoundedFullPath {
    param([Parameter(Mandatory)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-BoundedStrictChild {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Parent
    )
    $resolvedPath = (Resolve-BoundedFullPath $Path).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $resolvedParent = (Resolve-BoundedFullPath $Parent).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $comparison = Get-BoundedComparison
    if ($resolvedPath.Equals($resolvedParent, $comparison)) {
        return $false
    }
    return $resolvedPath.StartsWith($resolvedParent + [System.IO.Path]::DirectorySeparatorChar, $comparison)
}

function Test-BoundedUnsafeLink {
    param([Parameter(Mandatory)]$Item)
    if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        return $true
    }
    $linkProperty = $Item.PSObject.Properties["LinkType"]
    return $null -ne $linkProperty -and -not [string]::IsNullOrWhiteSpace([string]$linkProperty.Value)
}

function Assert-BoundedNoUnsafePathComponents {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$StopAt
    )
    $current = Resolve-BoundedFullPath $Path
    $stop = (Resolve-BoundedFullPath $StopAt).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $comparison = Get-BoundedComparison
    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (Test-BoundedUnsafeLink $item) {
                Stop-Bounded "unsafe_link"
            }
        }
        if ($current.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar).Equals($stop, $comparison)) {
            return
        }
        $parent = Split-Path -Parent $current
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent.Equals($current, $comparison)) {
            Stop-Bounded "path_root_escape"
        }
        $current = $parent
    }
}

function Get-BoundedUtf8NoBom {
    return [System.Text.UTF8Encoding]::new($false, $true)
}

function Get-BoundedSha256Bytes {
    param([Parameter(Mandatory)][byte[]]$Bytes)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function Get-BoundedSha256Text {
    param([Parameter(Mandatory)][string]$Text)
    return Get-BoundedSha256Bytes ((Get-BoundedUtf8NoBom).GetBytes($Text))
}

function Get-BoundedSha256File {
    param([Parameter(Mandatory)][string]$Path)
    return Get-BoundedSha256Bytes ([System.IO.File]::ReadAllBytes($Path))
}

function ConvertTo-BoundedCanonicalJsonElement {
    param(
        [Parameter(Mandatory)][System.Text.Json.JsonElement]$Element,
        [switch]$AllowFloatingPoint
    )
    switch ($Element.ValueKind) {
        ([System.Text.Json.JsonValueKind]::Null) { return "null" }
        ([System.Text.Json.JsonValueKind]::True) { return "true" }
        ([System.Text.Json.JsonValueKind]::False) { return "false" }
        ([System.Text.Json.JsonValueKind]::String) {
            return [System.Text.Json.JsonSerializer]::Serialize($Element.GetString(), $script:BoundedJsonOptions)
        }
        ([System.Text.Json.JsonValueKind]::Number) {
            $integer = [long]0
            if (-not $AllowFloatingPoint -and -not $Element.TryGetInt64([ref]$integer)) {
                Stop-Bounded "json_float_or_unsupported_number"
            }
            if ($AllowFloatingPoint) {
                return $Element.GetRawText()
            }
            return $integer.ToString([Globalization.CultureInfo]::InvariantCulture)
        }
        ([System.Text.Json.JsonValueKind]::Array) {
            $values = New-Object System.Collections.Generic.List[string]
            foreach ($child in $Element.EnumerateArray()) {
                $values.Add((ConvertTo-BoundedCanonicalJsonElement $child -AllowFloatingPoint:$AllowFloatingPoint))
            }
            return "[" + ($values -join ",") + "]"
        }
        ([System.Text.Json.JsonValueKind]::Object) {
            $names = New-Object System.Collections.Generic.List[string]
            $children = @{}
            foreach ($property in $Element.EnumerateObject()) {
                if ($children.ContainsKey($property.Name)) {
                    Stop-Bounded "json_duplicate_property"
                }
                $children[$property.Name] = $property.Value
                $names.Add($property.Name)
            }
            $nameArray = $names.ToArray()
            [Array]::Sort($nameArray, [StringComparer]::Ordinal)
            $parts = New-Object System.Collections.Generic.List[string]
            foreach ($name in $nameArray) {
                $encodedName = [System.Text.Json.JsonSerializer]::Serialize($name, $script:BoundedJsonOptions)
                $parts.Add($encodedName + ":" + (ConvertTo-BoundedCanonicalJsonElement $children[$name] -AllowFloatingPoint:$AllowFloatingPoint))
            }
            return "{" + ($parts -join ",") + "}"
        }
        default { Stop-Bounded "json_value_kind_invalid" }
    }
}

function Get-BoundedCanonicalJsonFromText {
    param(
        [Parameter(Mandatory)][string]$Text,
        [switch]$AllowFloatingPoint
    )
    if ($Text.Length -gt 0 -and [int]$Text[0] -eq 0xFEFF) {
        Stop-Bounded "json_bom_forbidden"
    }
    try {
        $document = [System.Text.Json.JsonDocument]::Parse($Text)
        try {
            return ConvertTo-BoundedCanonicalJsonElement $document.RootElement -AllowFloatingPoint:$AllowFloatingPoint
        } finally {
            $document.Dispose()
        }
    } catch {
        if ($_.Exception.Message -match "^(json_|json_)") {
            throw
        }
        Stop-Bounded "json_invalid"
    }
}

function Get-BoundedCanonicalJsonFromObject {
    param(
        [Parameter(Mandatory)]$Value,
        [switch]$AllowFloatingPoint
    )
    $text = $Value | ConvertTo-Json -Depth 100 -Compress
    return Get-BoundedCanonicalJsonFromText $text -AllowFloatingPoint:$AllowFloatingPoint
}

function ConvertFrom-BoundedJsonText {
    param([Parameter(Mandatory)][string]$Text)
    $canonical = Get-BoundedCanonicalJsonFromText $Text
    return $canonical | ConvertFrom-Json -Depth 100 -DateKind String
}

function ConvertFrom-BoundedWorkflowJsonText {
    param([Parameter(Mandatory)][string]$Text)
    $canonical = Get-BoundedCanonicalJsonFromText $Text -AllowFloatingPoint
    return $canonical | ConvertFrom-Json -Depth 100 -DateKind String
}

function Read-BoundedJsonFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$AllowFloatingPoint
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Stop-Bounded "evidence_missing"
    }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        Stop-Bounded "json_bom_forbidden"
    }
    if (@($bytes | Where-Object { $_ -eq 0x0D }).Count -gt 0 -or $bytes.Length -eq 0 -or $bytes[$bytes.Length - 1] -ne 0x0A) {
        Stop-Bounded "json_line_ending_invalid"
    }
    try {
        $text = (Get-BoundedUtf8NoBom).GetString($bytes)
    } catch {
        Stop-Bounded "json_utf8_invalid"
    }
    if ($AllowFloatingPoint) {
        return ConvertFrom-BoundedWorkflowJsonText $text
    }
    return ConvertFrom-BoundedJsonText $text
}

function Get-BoundedPropertyNames {
    param([Parameter(Mandatory)]$Value)
    return @($Value.PSObject.Properties.Name)
}

function Assert-BoundedExactProperties {
    param(
        [Parameter(Mandatory)]$Value,
        [Parameter(Mandatory)][string[]]$Expected,
        [Parameter(Mandatory)][string]$Code
    )
    $actual = @(Get-BoundedPropertyNames $Value | Sort-Object)
    $expectedSorted = @($Expected | Sort-Object)
    if ($actual.Count -ne $expectedSorted.Count) {
        Stop-Bounded $Code
    }
    for ($index = 0; $index -lt $actual.Count; $index++) {
        if (-not $actual[$index].Equals($expectedSorted[$index], [StringComparison]::Ordinal)) {
            Stop-Bounded $Code
        }
    }
}

function Test-BoundedPlaceholder {
    param([Parameter(Mandatory)][string]$Value)
    return $Value -match '^[A-Z0-9_]+_PLACEHOLDER$'
}

function Assert-BoundedSafeIdentity {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Code,
        [switch]$AllowPlaceholder
    )
    if ($AllowPlaceholder -and (Test-BoundedPlaceholder $Value)) {
        return
    }
    if ($Value -notmatch '^[A-Za-z0-9._:-]{1,200}$') {
        Stop-Bounded $Code
    }
}

function Assert-BoundedManifest {
    param([Parameter(Mandatory)]$Manifest)
    Assert-BoundedExactProperties $Manifest @(
        "schema_version", "source_system", "form_alias", "mapping_version",
        "project", "workflow", "endpoints", "form", "question_mapping",
        "credential_roles", "cursor_expectation", "security"
    ) "binding_shape_invalid"
    if ([string]$Manifest.schema_version -cne "xb.member.gateway.bounded_import.binding.v2") { Stop-Bounded "binding_schema_invalid" }
    if ([string]$Manifest.source_system -cne "google_forms" -or [string]$Manifest.form_alias -cne "member_registration" -or [string]$Manifest.mapping_version -cne "member-intake.v1") {
        Stop-Bounded "binding_identity_invalid"
    }

    Assert-BoundedExactProperties $Manifest.project @("id", "name") "binding_project_shape_invalid"
    Assert-BoundedExactProperties $Manifest.workflow @("id", "name") "binding_workflow_shape_invalid"
    foreach ($value in @([string]$Manifest.project.id, [string]$Manifest.workflow.id)) {
        Assert-BoundedSafeIdentity $value "binding_identity_invalid" -AllowPlaceholder
    }
    if ([string]$Manifest.workflow.name -cne "Member Gateway - Google Forms durable source adapter (inactive)") { Stop-Bounded "binding_workflow_name_invalid" }

    Assert-BoundedExactProperties $Manifest.endpoints @("source_cursor", "forms_responses", "gateway_ingest", "page_checkpoint") "binding_endpoint_shape_invalid"
    foreach ($property in @("source_cursor", "forms_responses", "gateway_ingest", "page_checkpoint")) {
        $url = [string]$Manifest.endpoints.$property
        if ($url -notmatch '^https://[A-Za-z0-9._:/{}?=&%+\-]+$') { Stop-Bounded "binding_endpoint_invalid" }
    }

    Assert-BoundedExactProperties $Manifest.form @("id") "binding_form_shape_invalid"
    Assert-BoundedSafeIdentity ([string]$Manifest.form.id) "binding_form_invalid" -AllowPlaceholder
    $questionNames = @("name", "phone", "email", "birthday_month", "marketing_consent", "pdpa_acknowledged")
    Assert-BoundedExactProperties $Manifest.question_mapping $questionNames "binding_question_shape_invalid"
    foreach ($property in $questionNames) {
        Assert-BoundedSafeIdentity ([string]$Manifest.question_mapping.$property) "binding_question_invalid" -AllowPlaceholder
    }

    Assert-BoundedExactProperties $Manifest.credential_roles @("google_forms_oauth", "gateway_bearer") "binding_credentials_shape_invalid"
    foreach ($roleName in @("google_forms_oauth", "gateway_bearer")) {
        $role = $Manifest.credential_roles.$roleName
        Assert-BoundedExactProperties $role @("credential_name", "credential_type", "node_names") "binding_credential_role_shape_invalid"
        Assert-BoundedSafeIdentity ([string]$role.credential_name) "binding_credential_invalid" -AllowPlaceholder
        Assert-BoundedSafeIdentity ([string]$role.credential_type) "binding_credential_invalid"
        if (@($role.node_names).Count -lt 1) { Stop-Bounded "binding_credential_nodes_invalid" }
        foreach ($nodeName in @($role.node_names)) {
            if ([string]::IsNullOrWhiteSpace([string]$nodeName)) { Stop-Bounded "binding_credential_nodes_invalid" }
        }
    }
    if (@($Manifest.credential_roles.google_forms_oauth.node_names).Count -ne 1 -or [string]$Manifest.credential_roles.google_forms_oauth.node_names[0] -cne "Google Forms single page (configured outside repo)") {
        Stop-Bounded "binding_credential_nodes_invalid"
    }
    $gatewayNodes = @($Manifest.credential_roles.gateway_bearer.node_names)
    if ($gatewayNodes.Count -ne 3 -or [string]$gatewayNodes[0] -cne "Read durable source cursor" -or [string]$gatewayNodes[1] -cne "Protected XB Gateway ingest (configured outside repo)" -or [string]$gatewayNodes[2] -cne "Commit durable page checkpoint") {
        Stop-Bounded "binding_credential_nodes_invalid"
    }

    Assert-BoundedExactProperties $Manifest.cursor_expectation @("state_version", "watermark", "watermark_digest") "binding_cursor_shape_invalid"
    if ([int64]$Manifest.cursor_expectation.state_version -lt 0) { Stop-Bounded "binding_cursor_invalid" }
    $watermark = [string]$Manifest.cursor_expectation.watermark
    $watermarkDigest = [string]$Manifest.cursor_expectation.watermark_digest
    if (-not (Test-BoundedPlaceholder $watermark) -and $watermark -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$') { Stop-Bounded "binding_watermark_invalid" }
    if (-not (Test-BoundedPlaceholder $watermarkDigest) -and $watermarkDigest -notmatch '^[0-9a-f]{64}$') { Stop-Bounded "binding_watermark_digest_invalid" }

    Assert-BoundedExactProperties $Manifest.security @("source_token_env", "n8n_target_mode") "binding_security_shape_invalid"
    if ([string]$Manifest.security.source_token_env -notmatch '^[A-Z][A-Z0-9_]{2,80}$') { Stop-Bounded "binding_security_invalid" }
    if ([string]$Manifest.security.n8n_target_mode -cne "explicit-reviewed-target") { Stop-Bounded "binding_security_invalid" }
}

function Get-BoundedWatermarkDigest {
    param(
        [Parameter(Mandatory)][string]$SourceSystem,
        [Parameter(Mandatory)][string]$FormAlias,
        [Parameter(Mandatory)][string]$MappingVersion,
        [Parameter(Mandatory)][string]$Watermark
    )
    return Get-BoundedSha256Text ("xb.member.gateway.watermark.v1|{0}|{1}|{2}|{3}" -f $SourceSystem, $FormAlias, $MappingVersion, $Watermark)
}

function Assert-BoundedDigest {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Code
    )
    if ($Value -notmatch '^[0-9a-f]{64}$') { Stop-Bounded $Code }
}

function Assert-BoundedReviewedBinding {
    param([Parameter(Mandatory)]$Manifest)
    $watermark = [string]$Manifest.cursor_expectation.watermark
    $watermarkDigest = [string]$Manifest.cursor_expectation.watermark_digest
    if ((Test-BoundedPlaceholder $watermark) -or (Test-BoundedPlaceholder $watermarkDigest)) {
        Stop-Bounded "binding_cursor_reference_unresolved"
    }
    if ($watermark -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$') {
        Stop-Bounded "binding_watermark_invalid"
    }
    $expectedDigest = Get-BoundedWatermarkDigest ([string]$Manifest.source_system) ([string]$Manifest.form_alias) ([string]$Manifest.mapping_version) $watermark
    if ($watermarkDigest -cne $expectedDigest) { Stop-Bounded "binding_watermark_reference_mismatch" }
}

function Assert-BoundedCursor {
    param(
        [Parameter(Mandatory)]$Cursor,
        [Parameter(Mandatory)]$Manifest
    )
    Assert-BoundedExactProperties $Cursor @(
        "schema_version", "source_system", "form_alias", "mapping_version",
        "watermark", "last_admitted_create_time", "last_admitted_response_id",
        "state_version", "scan_lower_bound", "resume_page_token",
        "initial_window_admission_count"
    ) "cursor_shape_invalid"
    if ([string]$Cursor.schema_version -cne "xb.member.gateway.source_cursor.v1" -or [string]$Cursor.source_system -cne [string]$Manifest.source_system -or [string]$Cursor.form_alias -cne [string]$Manifest.form_alias -or [string]$Cursor.mapping_version -cne [string]$Manifest.mapping_version) {
        Stop-Bounded "cursor_identity_invalid"
    }
    if ([int64]$Cursor.state_version -ne [int64]$Manifest.cursor_expectation.state_version) {
        Stop-Bounded "cursor_state_version_invalid"
    }
    $watermark = [string]$Cursor.watermark
    if ($watermark -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$') { Stop-Bounded "watermark_value_invalid" }
    try {
        $parsed = [DateTimeOffset]::ParseExact($watermark, "yyyy-MM-dd'T'HH:mm:ss'Z'", [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal)
        if ($parsed.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", [Globalization.CultureInfo]::InvariantCulture) -cne $watermark) { Stop-Bounded "watermark_value_invalid" }
    } catch {
        Stop-Bounded "watermark_value_invalid"
    }
    if ([string]$Manifest.cursor_expectation.watermark -cnotmatch 'PLACEHOLDER' -and [string]$Manifest.cursor_expectation.watermark -cne $watermark) {
        Stop-Bounded "watermark_value_mismatch"
    }
    $expectedDigest = Get-BoundedWatermarkDigest ([string]$Manifest.source_system) ([string]$Manifest.form_alias) ([string]$Manifest.mapping_version) $watermark
    if ([string]$Manifest.cursor_expectation.watermark_digest -cnotmatch 'PLACEHOLDER' -and [string]$Manifest.cursor_expectation.watermark_digest -cne $expectedDigest) {
        Stop-Bounded "watermark_digest_invalid"
    }
    if ([int64]$Cursor.initial_window_admission_count -lt 0) { Stop-Bounded "cursor_count_invalid" }
    foreach ($property in @("last_admitted_create_time", "last_admitted_response_id", "resume_page_token")) {
        if ($null -ne $Cursor.$property -and [string]$Cursor.$property -match '[\r\n]') { Stop-Bounded "cursor_value_invalid" }
    }
}

function Get-BoundedRepositoryIdentity {
    param([Parameter(Mandatory)][string]$Root)
    $values = @{}
    foreach ($name in @("head", "tree", "parent", "workflow_blob")) {
        $argument = switch ($name) {
            "head" { "HEAD" }
            "tree" { "HEAD^{tree}" }
            "parent" { "HEAD^" }
            "workflow_blob" { "HEAD:n8n-workflows/member_forms_gateway_ingest.workflow.json" }
        }
        $result = & git -C $Root rev-parse --verify $argument 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$result)) {
            if ($name -eq "parent") { $values[$name] = $null; continue }
            Stop-Bounded "repository_identity_unavailable"
        }
        $value = ([string]$result).Trim()
        if ($name -ne "parent" -and $value -notmatch '^[0-9a-f]{40}$') { Stop-Bounded "repository_identity_invalid" }
        $values[$name] = $value
    }
    return [pscustomobject]([ordered]@{
        head = $values.head
        tree = $values.tree
        parent = $values.parent
        workflow_blob = $values.workflow_blob
    })
}

function Get-BoundedWorkflowFile {
    param([Parameter(Mandatory)][string]$Root)
    $path = Join-Path $Root "n8n-workflows/member_forms_gateway_ingest.workflow.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Stop-Bounded "canonical_workflow_missing" }
    return $path
}

function Get-BoundedActivationValue {
    param([Parameter(Mandatory)]$Workflow)
    $matches = @()
    foreach ($node in @($Workflow.nodes)) {
        if ($node.name -ne "Repository-safe source configuration") { continue }
        foreach ($assignment in @($node.parameters.assignments.assignments)) {
            if ([string]$assignment.name -ceq "activation_enabled") { $matches += $assignment }
        }
    }
    if ($matches.Count -ne 1) { Stop-Bounded "activation_binding_invalid" }
    return [bool]$matches[0].value
}

function Get-BoundedWorkflowProjection {
    param([Parameter(Mandatory)]$Workflow)
    $activation = Get-BoundedActivationValue $Workflow
    $staticData = if ($Workflow.PSObject.Properties.Name -contains "staticData") { $Workflow.staticData } else { $null }
    $pinData = if ($Workflow.PSObject.Properties.Name -contains "pinData") { $Workflow.pinData } else { $null }
    return [pscustomobject]([ordered]@{
        id = [string]$Workflow.id
        name = [string]$Workflow.name
        active = [bool]$Workflow.active
        activation_enabled = $activation
        nodes = $Workflow.nodes
        connections = $Workflow.connections
        settings = $Workflow.settings
        staticData = $staticData
        pinData = $pinData
    })
}

function Assert-BoundedInactiveWorkflow {
    param([Parameter(Mandatory)]$Workflow)
    if ([bool]$Workflow.active) { Stop-Bounded "workflow_active" }
    if ([bool](Get-BoundedActivationValue $Workflow)) { Stop-Bounded "activation_enabled" }
    if ([bool]$Workflow.settings.availableInMCP) { Stop-Bounded "mcp_exposure_enabled" }
    $triggers = @($Workflow.nodes | Where-Object { [string]$_.type -like "*Trigger" })
    if ($triggers.Count -ne 1 -or [string]$triggers[0].type -ne "n8n-nodes-base.manualTrigger") { Stop-Bounded "workflow_trigger_posture_invalid" }
    $staticData = if ($Workflow.PSObject.Properties.Name -contains "staticData") { $Workflow.staticData } else { $null }
    $pinData = if ($Workflow.PSObject.Properties.Name -contains "pinData") { $Workflow.pinData } else { $null }
    if ($null -ne $staticData -or $null -ne $pinData) { Stop-Bounded "workflow_runtime_state_present" }
}

function Set-BoundedProperty {
    param(
        [Parameter(Mandatory)]$Object,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)]$Value
    )
    if ($Object.PSObject.Properties.Name -contains $Name) {
        $Object.$Name = $Value
    } else {
        $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value
    }
}

function New-BoundedPreparedWorkflow {
    param(
        [Parameter(Mandatory)]$Manifest,
        [Parameter(Mandatory)][string]$Root
    )
    $canonicalPath = Get-BoundedWorkflowFile $Root
    $raw = (Get-BoundedUtf8NoBom).GetString([System.IO.File]::ReadAllBytes($canonicalPath))
    $canonical = Get-BoundedCanonicalJsonFromText $raw -AllowFloatingPoint
    $preparedText = $canonical
    $replacements = [ordered]@{
        "https://forms.example.com/v1/forms/GOOGLE_FORM_ID_PLACEHOLDER/responses" = [string]$Manifest.endpoints.forms_responses
        "https://gateway.example.com/v1/source/cursor" = [string]$Manifest.endpoints.source_cursor
        "https://gateway.example.com/v1/source-events" = [string]$Manifest.endpoints.gateway_ingest
        "https://gateway.example.com/v1/source/cursor/page" = [string]$Manifest.endpoints.page_checkpoint
        "GOOGLE_FORM_ID_PLACEHOLDER" = [string]$Manifest.form.id
        "QUESTION_ID_NAME_PLACEHOLDER" = [string]$Manifest.question_mapping.name
        "QUESTION_ID_PHONE_PLACEHOLDER" = [string]$Manifest.question_mapping.phone
        "QUESTION_ID_EMAIL_PLACEHOLDER" = [string]$Manifest.question_mapping.email
        "QUESTION_ID_BIRTHDAY_MONTH_PLACEHOLDER" = [string]$Manifest.question_mapping.birthday_month
        "QUESTION_ID_MARKETING_CONSENT_PLACEHOLDER" = [string]$Manifest.question_mapping.marketing_consent
        "QUESTION_ID_PDPA_ACKNOWLEDGED_PLACEHOLDER" = [string]$Manifest.question_mapping.pdpa_acknowledged
    }
    foreach ($key in $replacements.Keys) {
        $preparedText = $preparedText.Replace([string]$key, [string]$replacements[$key])
    }
    if ($preparedText -match '[A-Z0-9_]+_PLACEHOLDER') { Stop-Bounded "binding_placeholder_unresolved" }
    $prepared = ConvertFrom-BoundedWorkflowJsonText $preparedText
    Set-BoundedProperty $prepared "id" ([string]$Manifest.workflow.id)
    Set-BoundedProperty $prepared "name" ([string]$Manifest.workflow.name)

    $formsRole = $Manifest.credential_roles.google_forms_oauth
    $gatewayRole = $Manifest.credential_roles.gateway_bearer
    foreach ($node in @($prepared.nodes)) {
        $nodeName = [string]$node.name
        if ($nodeName -in @($formsRole.node_names)) {
            $credential = [ordered]@{}
            $credential[[string]$formsRole.credential_type] = [ordered]@{ name = [string]$formsRole.credential_name }
            Set-BoundedProperty $node "credentials" $credential
        } elseif ($nodeName -in @($gatewayRole.node_names)) {
            $credential = [ordered]@{}
            $credential[[string]$gatewayRole.credential_type] = [ordered]@{ name = [string]$gatewayRole.credential_name }
            Set-BoundedProperty $node "credentials" $credential
        }
    }
    Assert-BoundedInactiveWorkflow $prepared
    return $prepared
}

function Get-BoundedWorkflowJsonText {
    param([Parameter(Mandatory)]$Workflow)
    return (Get-BoundedCanonicalJsonFromObject $Workflow -AllowFloatingPoint) + $script:BoundedLf
}

function Read-BoundedWorkflowFile {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Stop-Bounded "evidence_missing" }
    $raw = (Get-BoundedUtf8NoBom).GetString([System.IO.File]::ReadAllBytes($Path))
    return ConvertFrom-BoundedWorkflowJsonText $raw
}

function Get-BoundedMetadataEntry {
    param([Parameter(Mandatory)]$Entry)
    $projectId = [string]$Entry.projectId
    $projectName = [string]$Entry.projectName
    $workflowId = [string]$Entry.id
    $workflowName = [string]$Entry.name
    if ([string]::IsNullOrWhiteSpace($projectId) -or [string]::IsNullOrWhiteSpace($projectName) -or [string]::IsNullOrWhiteSpace($workflowId) -or [string]::IsNullOrWhiteSpace($workflowName)) {
        Stop-Bounded "metadata_identity_incomplete"
    }
    return [pscustomobject]([ordered]@{
        project_id = $projectId
        project_name = $projectName
        workflow_id = $workflowId
        workflow_name = $workflowName
        archived = [bool]$Entry.isArchived
    })
}

function Select-BoundedWorkflowTarget {
    param(
        [AllowNull()][AllowEmptyCollection()]$Metadata,
        [Parameter(Mandatory)]$Manifest
    )
    $exact = New-Object System.Collections.Generic.List[object]
    $near = New-Object System.Collections.Generic.List[object]
    $all = New-Object System.Collections.Generic.List[object]
    foreach ($entry in @($Metadata)) {
        if ($null -eq $entry) { continue }
        $safe = Get-BoundedMetadataEntry $entry
        $all.Add($safe)
        $pairs = @(
            @($safe.project_id, [string]$Manifest.project.id),
            @($safe.project_name, [string]$Manifest.project.name),
            @($safe.workflow_id, [string]$Manifest.workflow.id),
            @($safe.workflow_name, [string]$Manifest.workflow.name)
        )
        $allExact = $true
        $anyNear = $false
        foreach ($pair in $pairs) {
            $left = [string]$pair[0]
            $right = [string]$pair[1]
            if (-not $left.Equals($right, [StringComparison]::Ordinal)) { $allExact = $false }
            if ($left.Equals($right, [StringComparison]::OrdinalIgnoreCase) -and -not $left.Equals($right, [StringComparison]::Ordinal)) { $anyNear = $true }
        }
        if ($allExact) { $exact.Add($safe) }
        elseif ($anyNear) { $near.Add($safe) }
    }
    if ($exact.Count -gt 1) { Stop-Bounded "target_duplicate" }
    if ($near.Count -gt 0) { Stop-Bounded "target_case_distinct_collision" }
    if ($exact.Count -eq 1) {
        if ($exact[0].archived) { Stop-Bounded "target_archived" }
        return [pscustomobject]([ordered]@{ state = "existing"; target = $exact[0]; metadata = @($all.ToArray()) })
    }
    return [pscustomobject]([ordered]@{ state = "absent"; target = $null; metadata = @($all.ToArray()) })
}

function New-BoundedAbsenceEvidence {
    param(
        [Parameter(Mandatory)]$Selection,
        [Parameter(Mandatory)]$Manifest
    )
    return [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.absence.v2"
        selection = "absent"
        project = [pscustomobject]([ordered]@{ id = [string]$Manifest.project.id; name = [string]$Manifest.project.name })
        workflow = [pscustomobject]([ordered]@{ id = [string]$Manifest.workflow.id; name = [string]$Manifest.workflow.name })
        metadata = @($Selection.metadata)
        case_exact_matches = @()
        case_distinct_matches = @()
    })
}

function Assert-BoundedAbsenceEvidence {
    param(
        [Parameter(Mandatory)]$Evidence,
        [Parameter(Mandatory)]$Manifest
    )
    Assert-BoundedExactProperties $Evidence @("schema_version", "selection", "project", "workflow", "metadata", "case_exact_matches", "case_distinct_matches") "absence_shape_invalid"
    if ([string]$Evidence.schema_version -cne "xb.member.gateway.bounded_import.absence.v2" -or [string]$Evidence.selection -cne "absent") { Stop-Bounded "absence_state_invalid" }
    if ([string]$Evidence.project.id -cne [string]$Manifest.project.id -or [string]$Evidence.project.name -cne [string]$Manifest.project.name -or [string]$Evidence.workflow.id -cne [string]$Manifest.workflow.id -or [string]$Evidence.workflow.name -cne [string]$Manifest.workflow.name) {
        Stop-Bounded "absence_identity_invalid"
    }
    if (@($Evidence.case_exact_matches).Count -ne 0 -or @($Evidence.case_distinct_matches).Count -ne 0) { Stop-Bounded "absence_matches_present" }
}

function Invoke-BoundedExternalJson {
    param(
        [Parameter(Mandatory)][string]$Verb,
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$InputText = "",
        [switch]$AllowFloatingPoint
    )
    if ($Verb -notin @("list:workflow", "export:workflow", "import:workflow")) { Stop-Bounded "n8n_command_not_allowed" }
    if ($Arguments -match "--all" -or $Arguments -match "export:workflow.*--all") { Stop-Bounded "unbounded_workflow_export" }
    $command = $N8nExecutable
    $finalArgs = @()
    if (-not [string]::IsNullOrWhiteSpace($N8nContainer)) {
        $command = "docker"
        $finalArgs += @("exec", "-i", $N8nContainer, "n8n")
    }
    $finalArgs += $Verb
    $finalArgs += $Arguments
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $command
    $processInfo.UseShellExecute = $false
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $processInfo.CreateNoWindow = $true
    foreach ($argument in $finalArgs) { [void]$processInfo.ArgumentList.Add([string]$argument) }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $processInfo
    try {
        if (-not $process.Start()) { Stop-Bounded "n8n_command_start_failed" }
        if (-not [string]::IsNullOrEmpty($InputText)) {
            $process.StandardInput.Write($InputText)
            $process.StandardInput.Close()
        }
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { Stop-Bounded "n8n_command_failed" }
        if ($AllowFloatingPoint) {
            return ConvertFrom-BoundedWorkflowJsonText $stdout
        }
        return ConvertFrom-BoundedJsonText $stdout
    } finally {
        $process.Dispose()
    }
}

function Get-BoundedFixtureEvidence {
    param(
        [Parameter(Mandatory)]$Manifest,
        [Parameter(Mandatory)]$Fixture,
        [switch]$AfterDispatch
    )
    $cursor = ConvertFrom-BoundedJsonText ($Fixture.cursor | ConvertTo-Json -Depth 100 -Compress)
    Assert-BoundedCursor $cursor $Manifest
    $metadataValue = if ($AfterDispatch) { $Fixture.after_metadata } else { $Fixture.metadata }
    $metadata = if ($null -eq $metadataValue) { @() } else { @($metadataValue) }
    $selection = Select-BoundedWorkflowTarget $metadata $Manifest
    $workflow = $null
    if ($selection.state -eq "existing") {
        $workflowValue = if ($AfterDispatch) { $Fixture.after_workflow } else { $Fixture.workflow }
        if ($null -eq $workflowValue) { Stop-Bounded "preimage_missing" }
        $workflow = ConvertFrom-BoundedWorkflowJsonText ($workflowValue | ConvertTo-Json -Depth 100 -Compress)
        Assert-BoundedInactiveWorkflow $workflow
    }
    return [pscustomobject]([ordered]@{ cursor = $cursor; selection = $selection; workflow = $workflow })
}

function Get-BoundedLiveEvidence {
    param(
        [Parameter(Mandatory)]$Manifest,
        [switch]$AfterDispatch
    )
    $tokenName = [string]$Manifest.security.source_token_env
    $token = [Environment]::GetEnvironmentVariable($tokenName, "Process")
    if ([string]::IsNullOrWhiteSpace($token)) { Stop-Bounded "source_token_missing" }
    $headers = @{ Authorization = "Bearer " + $token }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ([string]$Manifest.endpoints.source_cursor) -Headers $headers -Method Get
        $cursor = ConvertFrom-BoundedJsonText ([string]$response.Content)
        Assert-BoundedCursor $cursor $Manifest
        $metadata = Invoke-BoundedExternalJson "list:workflow" @("--output=json")
        $selection = Select-BoundedWorkflowTarget $metadata $Manifest
        $workflow = $null
        if ($selection.state -eq "existing") {
            $workflow = Invoke-BoundedExternalJson "export:workflow" @("--id", [string]$selection.target.workflow_id, "--pretty") -AllowFloatingPoint
            Assert-BoundedInactiveWorkflow $workflow
        }
        return [pscustomobject]([ordered]@{ cursor = $cursor; selection = $selection; workflow = $workflow })
    } catch {
        if ($_.Exception.Message -match "^source_token_missing$|^cursor_|^watermark_|^metadata_|^target_|^workflow_|^n8n_") { throw }
        Stop-Bounded "authoritative_read_failed"
    } finally {
        $token = $null
        $headers = $null
    }
}

function Get-BoundedEvidence {
    param(
        [Parameter(Mandatory)]$Manifest,
        [switch]$AfterDispatch
    )
    if ($script:BoundedTestOnly) {
        return Get-BoundedFixtureEvidence $Manifest $script:BoundedFixture -AfterDispatch:$AfterDispatch
    }
    return Get-BoundedLiveEvidence $Manifest -AfterDispatch:$AfterDispatch
}

function Assert-BoundedNoGenericHooks {
    param([Parameter(Mandatory)][string]$Root)
    foreach ($name in @("N8N_WORKFLOW_HOOK_SCRIPT", "N8N_WORKFLOW_HOOK_AUTOLOAD", "N8N_WORKFLOW_VALIDATION_RULES", "N8N_WORKFLOW_VALIDATION_RULES_AUTOLOAD")) {
        if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, "Process"))) {
            Stop-Bounded "generic_hook_surface_present"
        }
    }
    foreach ($relative in @(
        "scripts/n8n-workflow-hooks.cjs", "scripts/n8n-workflow-hooks.js", "scripts/n8n-workflow-hooks.ps1",
        ".n8n-local/n8n-workflow-hooks.cjs", ".n8n-local/n8n-workflow-hooks.js", ".n8n-local/n8n-workflow-hooks.ps1",
        ".n8n-workflow-hooks.cjs", ".n8n-workflow-hooks.js", ".n8n-workflow-hooks.ps1"
    )) {
        if (Test-Path -LiteralPath (Join-Path $Root $relative)) { Stop-Bounded "generic_hook_surface_present" }
    }
}

function Set-BoundedProtectedAcl {
    param([Parameter(Mandatory)][string]$Path)
    if ($script:BoundedTestOnly) {
        if ($script:BoundedFixture -and [string]$script:BoundedFixture.custody_mode -eq "acl_failure") { Stop-Bounded "private_acl_verification_failed" }
        return
    }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { Stop-Bounded "private_acl_unavailable" }
    try {
        $acl = Get-Acl -LiteralPath $Path
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
        foreach ($sid in @($identity, "S-1-5-18", "S-1-5-32-544")) {
            $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
                [System.Security.Principal.SecurityIdentifier]::new($sid),
                [System.Security.AccessControl.FileSystemRights]::FullControl,
                $inheritance,
                [System.Security.AccessControl.PropagationFlags]::None,
                [System.Security.AccessControl.AccessControlType]::Allow
            )
            [void]$acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $Path -AclObject $acl
        $verified = Get-Acl -LiteralPath $Path
        if (-not $verified.AreAccessRulesProtected) { Stop-Bounded "private_acl_verification_failed" }
        $allowed = @($identity, "S-1-5-18", "S-1-5-32-544")
        foreach ($rule in @($verified.Access)) {
            if ($rule.AccessControlType -ne "Allow" -or [string]$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $allowed) {
                Stop-Bounded "private_acl_verification_failed"
            }
        }
    } catch {
        if ($_.Exception.Message -match "^private_acl_") { throw }
        Stop-Bounded "private_acl_verification_failed"
    }
}

function Assert-BoundedPrivateDestination {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$OperationsPath
    )
    if (-not (Test-BoundedStrictChild $OperationsPath $Root)) { Stop-Bounded "private_path_escape" }
    $resolvedOperations = Resolve-BoundedFullPath $OperationsPath
    $requiredFragment = [System.IO.Path]::DirectorySeparatorChar + ".n8n-local" + [System.IO.Path]::DirectorySeparatorChar + "member-gateway-bounded-import" + [System.IO.Path]::DirectorySeparatorChar + "operations"
    if (-not $resolvedOperations.Contains($requiredFragment, [StringComparison]::OrdinalIgnoreCase)) { Stop-Bounded "private_path_not_canonical" }
    Assert-BoundedNoUnsafePathComponents $resolvedOperations $Root
    if (-not $script:BoundedTestOnly) {
        $relative = $resolvedOperations.Substring((Resolve-BoundedFullPath $Root).Length).TrimStart([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
        $ignored = & git -C $Root check-ignore --quiet --no-index -- $relative 2>$null
        if ($LASTEXITCODE -ne 0) { Stop-Bounded "private_path_not_ignored" }
        $tracked = @(& git -C $Root ls-files -- $relative)
        if ($tracked.Count -gt 0) { Stop-Bounded "private_path_tracked" }
    }
}

function New-BoundedStagingRoot {
    param([Parameter(Mandatory)][string]$Parent)
    $stage = Join-Path $Parent (".bounded-stage-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Set-BoundedProtectedAcl $stage
    return $stage
}

function Write-BoundedCreateNewBytes {
    param(
        [Parameter(Mandatory)][string]$StageRoot,
        [Parameter(Mandatory)][string]$TargetRoot,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][byte[]]$Bytes
    )
    $stagePath = Join-Path $StageRoot ($Name + "." + [Guid]::NewGuid().ToString("N") + ".bounded-write")
    $targetPath = Join-Path $TargetRoot $Name
    if (Test-Path -LiteralPath $targetPath) { Stop-Bounded "evidence_already_exists" }
    $stream = [System.IO.File]::Open($stagePath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $stream.Write($Bytes, 0, $Bytes.Length)
        $stream.Flush($true)
        $script:BoundedPrivateBytesWritten = $true
    } finally {
        $stream.Dispose()
    }
    [System.IO.File]::Move($stagePath, $targetPath)
    Set-BoundedProtectedAcl $targetPath
}

function Write-BoundedCreateNewText {
    param(
        [Parameter(Mandatory)][string]$StageRoot,
        [Parameter(Mandatory)][string]$TargetRoot,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Text
    )
    Write-BoundedCreateNewBytes $StageRoot $TargetRoot $Name ((Get-BoundedUtf8NoBom).GetBytes($Text))
}

function Get-BoundedOperationFiles {
    param([Parameter(Mandatory)][string]$OperationPath)
    if (-not (Test-Path -LiteralPath $OperationPath -PathType Container)) { Stop-Bounded "operation_missing" }
    $entries = @(Get-ChildItem -LiteralPath $OperationPath -Force)
    foreach ($entry in $entries) {
        if ($entry.PSIsContainer -or (Test-BoundedUnsafeLink $entry)) { Stop-Bounded "operation_extra_material" }
    }
    return @($entries.Name)
}

function Get-BoundedPlanDigest {
    param([Parameter(Mandatory)][string]$OperationPath)
    return Get-BoundedSha256File (Join-Path $OperationPath "plan.json")
}

function Assert-BoundedFileDigest {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Expected,
        [Parameter(Mandatory)][string]$Code
    )
    if ((Get-BoundedSha256File $Path) -cne $Expected) { Stop-Bounded $Code }
}

function Get-BoundedOperationState {
    param([Parameter(Mandatory)][string]$OperationPath)
    $files = @(Get-BoundedOperationFiles $OperationPath)
    $base = @("binding.json", "cursor-state.json", "mutation-intent.json", "plan.json", "prepared.workflow.json")
    foreach ($name in $base) { if ($name -notin $files) { Stop-Bounded "operation_incomplete" } }
    $preimage = @($files | Where-Object { $_ -in @("preimage.workflow.json", "absence-evidence.json") })
    if ($preimage.Count -ne 1) { Stop-Bounded "operation_preimage_shape_invalid" }
    $optional = @($files | Where-Object { $_ -in @("dispatch-receipt.json", "completion-receipt.json") })
    foreach ($file in $files) {
        if ($file -notin ($base + $preimage + @("dispatch-receipt.json", "completion-receipt.json"))) { Stop-Bounded "operation_extra_material" }
    }
    if ("completion-receipt.json" -in $optional -and "dispatch-receipt.json" -notin $optional) { Stop-Bounded "completion_without_dispatch" }
    $plan = Read-BoundedJsonFile (Join-Path $OperationPath "plan.json")
    $planDigest = Get-BoundedPlanDigest $OperationPath
    Assert-BoundedExactProperties $plan @("schema_version", "operation_id", "immutable", "operation_identity", "identity_seed", "expected_projection_digest", "expected_files") "plan_shape_invalid"
    if ([string]$plan.schema_version -cne "xb.member.gateway.bounded_import.plan.v2" -or [string]$plan.operation_id -ne [string]$OperationId -or -not [bool]$plan.immutable) { Stop-Bounded "plan_identity_invalid" }
    Assert-BoundedDigest ([string]$plan.operation_identity) "operation_identity_invalid"
    Assert-BoundedDigest ([string]$plan.expected_projection_digest) "plan_projection_digest_invalid"
    Assert-BoundedExactProperties $plan.identity_seed @(
        "schema_version", "repository", "canonical_workflow", "project_identity", "workflow_identity",
        "prepared_workflow_digest", "cursor_digest", "cursor_state_version", "watermark_digest",
        "watermark_value", "preimage_state", "preimage_digest", "preimage_selection_digest",
        "binding_manifest_digest"
    ) "identity_seed_shape_invalid"
    Assert-BoundedExactProperties $plan.identity_seed.repository @("head", "tree", "parent", "workflow_blob") "repository_identity_shape_invalid"
    Assert-BoundedExactProperties $plan.identity_seed.canonical_workflow @("path", "git_blob") "canonical_workflow_identity_shape_invalid"
    Assert-BoundedExactProperties $plan.identity_seed.project_identity @("id", "name") "project_identity_shape_invalid"
    Assert-BoundedExactProperties $plan.identity_seed.workflow_identity @("id", "name") "workflow_identity_shape_invalid"
    foreach ($digest in @(
        [string]$plan.identity_seed.prepared_workflow_digest,
        [string]$plan.identity_seed.cursor_digest,
        [string]$plan.identity_seed.watermark_digest,
        [string]$plan.identity_seed.preimage_digest,
        [string]$plan.identity_seed.preimage_selection_digest,
        [string]$plan.identity_seed.binding_manifest_digest
    )) { Assert-BoundedDigest $digest "identity_seed_digest_invalid" }
    if ([string]$plan.identity_seed.canonical_workflow.path -cne "n8n-workflows/member_forms_gateway_ingest.workflow.json") { Stop-Bounded "canonical_workflow_identity_invalid" }
    foreach ($repositoryValue in @(
        [string]$plan.identity_seed.repository.head,
        [string]$plan.identity_seed.repository.tree,
        [string]$plan.identity_seed.repository.workflow_blob,
        [string]$plan.identity_seed.canonical_workflow.git_blob
    )) { if ($repositoryValue -notmatch '^[0-9a-f]{40}$') { Stop-Bounded "repository_identity_invalid" } }
    if ($null -ne $plan.identity_seed.repository.parent -and [string]$plan.identity_seed.repository.parent -notmatch '^[0-9a-f]{40}$') { Stop-Bounded "repository_identity_invalid" }
    if ([string]$plan.identity_seed.preimage_state -notin @("existing", "absent")) { Stop-Bounded "preimage_state_invalid" }
    if ([int64]$plan.identity_seed.cursor_state_version -lt 0 -or [string]$plan.identity_seed.watermark_value -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$') { Stop-Bounded "identity_reference_invalid" }
    $expectedFiles = @(
        "plan.json", "binding.json", "cursor-state.json", "prepared.workflow.json",
        $(if ([string]$plan.identity_seed.preimage_state -eq "existing") { "preimage.workflow.json" } else { "absence-evidence.json" }),
        "mutation-intent.json"
    )
    if (@($plan.expected_files).Count -ne $expectedFiles.Count -or (@($plan.expected_files) -join "|") -cne ($expectedFiles -join "|")) { Stop-Bounded "plan_file_set_invalid" }
    $seedDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $plan.identity_seed)
    if ($seedDigest -cne [string]$plan.operation_identity) { Stop-Bounded "operation_identity_mismatch" }
    $binding = Read-BoundedJsonFile (Join-Path $OperationPath "binding.json")
    $cursorState = Read-BoundedJsonFile (Join-Path $OperationPath "cursor-state.json")
    $intent = Read-BoundedJsonFile (Join-Path $OperationPath "mutation-intent.json")
    Assert-BoundedExactProperties $binding @("schema_version", "operation_id", "operation_identity", "plan_digest", "manifest_digest", "manifest") "binding_state_shape_invalid"
    Assert-BoundedExactProperties $cursorState @("schema_version", "operation_id", "operation_identity", "plan_digest", "cursor_digest", "cursor") "cursor_state_shape_invalid"
    Assert-BoundedExactProperties $intent @(
        "schema_version", "operation_id", "operation_identity", "plan_digest", "operation", "target", "project",
        "prepared_workflow_digest", "expected_projection_digest", "original_preimage_state", "generic_hooks_allowed", "source_execution_allowed"
    ) "mutation_intent_shape_invalid"
    Assert-BoundedExactProperties $intent.target @("id", "name") "mutation_intent_target_shape_invalid"
    Assert-BoundedExactProperties $intent.project @("id", "name") "mutation_intent_project_shape_invalid"
    Assert-BoundedManifest $binding.manifest
    Assert-BoundedReviewedBinding $binding.manifest
    Assert-BoundedDigest ([string]$binding.manifest_digest) "binding_manifest_digest_invalid"
    Assert-BoundedDigest ([string]$binding.plan_digest) "plan_digest_invalid"
    Assert-BoundedDigest ([string]$cursorState.cursor_digest) "cursor_digest_invalid"
    Assert-BoundedDigest ([string]$intent.prepared_workflow_digest) "prepared_digest_invalid"
    Assert-BoundedDigest ([string]$intent.expected_projection_digest) "intent_projection_digest_invalid"
    if ([bool]$intent.generic_hooks_allowed -or [bool]$intent.source_execution_allowed) { Stop-Bounded "mutation_intent_scope_invalid" }
    if ([string]$binding.schema_version -cne "xb.member.gateway.bounded_import.binding-state.v2" -or [string]$binding.operation_id -ne [string]$OperationId) { Stop-Bounded "binding_state_identity_invalid" }
    if ([string]$cursorState.schema_version -cne "xb.member.gateway.bounded_import.cursor-state.v2" -or [string]$cursorState.operation_id -ne [string]$OperationId) { Stop-Bounded "cursor_state_identity_invalid" }
    if ([string]$intent.schema_version -cne "xb.member.gateway.bounded_import.mutation-intent.v2" -or [string]$intent.operation_id -ne [string]$OperationId -or [string]$intent.operation -cne "import_one_workflow") { Stop-Bounded "mutation_intent_identity_invalid" }
    if ((Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $binding.manifest)) -cne [string]$binding.manifest_digest) { Stop-Bounded "binding_digest_mismatch" }
    if ([string]$intent.target.id -cne [string]$binding.manifest.workflow.id -or [string]$intent.target.name -cne [string]$binding.manifest.workflow.name -or [string]$intent.project.id -cne [string]$binding.manifest.project.id -or [string]$intent.project.name -cne [string]$binding.manifest.project.name) { Stop-Bounded "mutation_intent_target_invalid" }
    if ([string]$intent.prepared_workflow_digest -cne [string]$plan.identity_seed.prepared_workflow_digest -or [string]$intent.original_preimage_state -cne [string]$plan.identity_seed.preimage_state) { Stop-Bounded "mutation_intent_chain_mismatch" }
    foreach ($value in @($binding, $cursorState, $intent)) {
        if ([string]$value.plan_digest -cne $planDigest -or [string]$value.operation_identity -cne [string]$plan.operation_identity) { Stop-Bounded "evidence_chain_mismatch" }
    }
    if ([string]$binding.manifest_digest -cne [string]$plan.identity_seed.binding_manifest_digest) { Stop-Bounded "binding_digest_mismatch" }
    $cursor = $cursorState.cursor
    Assert-BoundedCursor $cursor $binding.manifest
    if ([string]$cursorState.cursor_digest -cne [string]$plan.identity_seed.cursor_digest -or (Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $cursor)) -cne [string]$cursorState.cursor_digest) { Stop-Bounded "cursor_digest_mismatch" }
    Assert-BoundedFileDigest (Join-Path $OperationPath "prepared.workflow.json") ([string]$plan.identity_seed.prepared_workflow_digest) "prepared_digest_mismatch"
    if ($preimage[0] -eq "preimage.workflow.json") {
        Assert-BoundedFileDigest (Join-Path $OperationPath $preimage[0]) ([string]$plan.identity_seed.preimage_digest) "preimage_digest_mismatch"
    } else {
        $absence = Read-BoundedJsonFile (Join-Path $OperationPath "absence-evidence.json")
        Assert-BoundedAbsenceEvidence $absence ([pscustomobject]$binding.manifest)
        $absenceText = (Get-BoundedCanonicalJsonFromObject $absence) + $script:BoundedLf
        if ((Get-BoundedSha256Text $absenceText) -cne [string]$plan.identity_seed.preimage_digest) { Stop-Bounded "absence_digest_mismatch" }
    }
    if ([string]$intent.expected_projection_digest -ne [string]$plan.expected_projection_digest) { Stop-Bounded "mutation_intent_mismatch" }
    $dispatch = $null
    $completion = $null
    if ("dispatch-receipt.json" -in $optional) {
        $dispatch = Read-BoundedJsonFile (Join-Path $OperationPath "dispatch-receipt.json")
        Assert-BoundedExactProperties $dispatch @("schema_version", "operation_id", "operation_identity", "plan_digest", "mutation_intent_digest", "dispatch_state", "outcome", "replay_allowed") "dispatch_receipt_shape_invalid"
        if ([string]$dispatch.dispatch_state -cne "dispatched" -or [string]$dispatch.outcome -notin @("completed", "ambiguous") -or [bool]$dispatch.replay_allowed) { Stop-Bounded "dispatch_receipt_state_invalid" }
        Assert-BoundedDigest ([string]$dispatch.mutation_intent_digest) "dispatch_intent_digest_invalid"
        if ([string]$dispatch.schema_version -cne "xb.member.gateway.bounded_import.dispatch.v2" -or [string]$dispatch.operation_id -ne [string]$OperationId) { Stop-Bounded "dispatch_receipt_identity_invalid" }
        if ([string]$dispatch.mutation_intent_digest -cne (Get-BoundedSha256File (Join-Path $OperationPath "mutation-intent.json"))) { Stop-Bounded "dispatch_chain_mismatch" }
        if ([string]$dispatch.plan_digest -ne $planDigest -or [string]$dispatch.operation_identity -ne [string]$plan.operation_identity) { Stop-Bounded "dispatch_chain_mismatch" }
    }
    if ("completion-receipt.json" -in $optional) {
        $completion = Read-BoundedJsonFile (Join-Path $OperationPath "completion-receipt.json")
        Assert-BoundedExactProperties $completion @(
            "schema_version", "operation_id", "operation_identity", "plan_digest", "dispatch_digest",
            "expected_projection_digest", "target_projection_digest", "original_preimage_state", "ownership", "complete"
        ) "completion_receipt_shape_invalid"
        if (-not [bool]$completion.complete -or [string]$completion.original_preimage_state -notin @("existing", "absent") -or [string]$completion.ownership -notin @("created", "updated_or_noop")) { Stop-Bounded "completion_receipt_state_invalid" }
        Assert-BoundedDigest ([string]$completion.dispatch_digest) "completion_dispatch_digest_invalid"
        Assert-BoundedDigest ([string]$completion.target_projection_digest) "completion_target_digest_invalid"
        if ([string]$completion.dispatch_digest -ne (Get-BoundedSha256File (Join-Path $OperationPath "dispatch-receipt.json"))) { Stop-Bounded "completion_chain_mismatch" }
        if ([string]$completion.expected_projection_digest -ne [string]$plan.expected_projection_digest) { Stop-Bounded "completion_identity_mismatch" }
        if ([string]$completion.plan_digest -ne $planDigest -or [string]$completion.operation_identity -ne [string]$plan.operation_identity) { Stop-Bounded "completion_chain_mismatch" }
        if ([string]$completion.schema_version -cne "xb.member.gateway.bounded_import.completion.v2" -or [string]$completion.operation_id -ne [string]$OperationId -or [string]$completion.target_projection_digest -cne [string]$plan.expected_projection_digest) { Stop-Bounded "completion_receipt_identity_invalid" }
        if (([string]$completion.original_preimage_state -eq "absent" -and [string]$completion.ownership -cne "created") -or ([string]$completion.original_preimage_state -eq "existing" -and [string]$completion.ownership -cne "updated_or_noop")) { Stop-Bounded "completion_receipt_state_invalid" }
    }
    return [pscustomobject]([ordered]@{
        operation_path = $OperationPath
        plan = $plan
        plan_digest = $planDigest
        binding = $binding
        cursor_state = $cursorState
        intent = $intent
        preimage_name = $preimage[0]
        dispatch = $dispatch
        completion = $completion
    })
}

function New-BoundedPlanArtifacts {
    param(
        [Parameter(Mandatory)]$Manifest,
        [Parameter(Mandatory)]$Evidence,
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$OperationId
    )
    $repo = Get-BoundedRepositoryIdentity $Root
    $prepared = New-BoundedPreparedWorkflow $Manifest $Root
    $preparedText = Get-BoundedWorkflowJsonText $prepared
    $preparedDigest = Get-BoundedSha256Text $preparedText
    $cursorCanonical = Get-BoundedCanonicalJsonFromObject $Evidence.cursor
    $cursorDigest = Get-BoundedSha256Text $cursorCanonical
    $preimageState = [string]$Evidence.selection.state
    $preimageObject = $null
    $preimageText = $null
    if ($preimageState -eq "existing") {
        $preimageText = Get-BoundedWorkflowJsonText $Evidence.workflow
        $preimageDigest = Get-BoundedSha256Text $preimageText
    } else {
        $preimageObject = New-BoundedAbsenceEvidence $Evidence.selection $Manifest
        $preimageText = (Get-BoundedCanonicalJsonFromObject $preimageObject) + $script:BoundedLf
        $preimageDigest = Get-BoundedSha256Text $preimageText
    }
    $manifestCanonical = Get-BoundedCanonicalJsonFromObject $Manifest
    $manifestDigest = Get-BoundedSha256Text $manifestCanonical
    $projection = Get-BoundedWorkflowProjection $prepared
    $projectionDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $projection -AllowFloatingPoint)
    $selectionDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $Evidence.selection)
    $identitySeed = [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.identity.v2"
        repository = $repo
        canonical_workflow = [pscustomobject]([ordered]@{
            path = "n8n-workflows/member_forms_gateway_ingest.workflow.json"
            git_blob = [string]$repo.workflow_blob
        })
        project_identity = $Manifest.project
        workflow_identity = $Manifest.workflow
        prepared_workflow_digest = $preparedDigest
        cursor_digest = $cursorDigest
        cursor_state_version = [int64]$Evidence.cursor.state_version
        watermark_digest = Get-BoundedWatermarkDigest ([string]$Manifest.source_system) ([string]$Manifest.form_alias) ([string]$Manifest.mapping_version) ([string]$Evidence.cursor.watermark)
        watermark_value = [string]$Evidence.cursor.watermark
        preimage_state = $preimageState
        preimage_digest = $preimageDigest
        preimage_selection_digest = $selectionDigest
        binding_manifest_digest = $manifestDigest
    })
    $operationIdentity = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $identitySeed)
    $plan = [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.plan.v2"
        operation_id = $OperationId
        immutable = $true
        operation_identity = $operationIdentity
        identity_seed = $identitySeed
        expected_projection_digest = $projectionDigest
        expected_files = @(
            "plan.json", "binding.json", "cursor-state.json", "prepared.workflow.json",
            $(if ($preimageState -eq "existing") { "preimage.workflow.json" } else { "absence-evidence.json" }),
            "mutation-intent.json"
        )
    })
    $planText = (Get-BoundedCanonicalJsonFromObject $plan) + $script:BoundedLf
    $planDigest = Get-BoundedSha256Text $planText
    $binding = [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.binding-state.v2"
        operation_id = $OperationId
        operation_identity = $operationIdentity
        plan_digest = $planDigest
        manifest_digest = $manifestDigest
        manifest = $Manifest
    })
    $cursorState = [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.cursor-state.v2"
        operation_id = $OperationId
        operation_identity = $operationIdentity
        plan_digest = $planDigest
        cursor_digest = $cursorDigest
        cursor = $Evidence.cursor
    })
    $intent = [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.mutation-intent.v2"
        operation_id = $OperationId
        operation_identity = $operationIdentity
        plan_digest = $planDigest
        operation = "import_one_workflow"
        target = $Manifest.workflow
        project = $Manifest.project
        prepared_workflow_digest = $preparedDigest
        expected_projection_digest = $projectionDigest
        original_preimage_state = $preimageState
        generic_hooks_allowed = $false
        source_execution_allowed = $false
    })
    $artifacts = [ordered]@{
        "plan.json" = $planText
        "binding.json" = (Get-BoundedCanonicalJsonFromObject $binding) + $script:BoundedLf
        "cursor-state.json" = (Get-BoundedCanonicalJsonFromObject $cursorState) + $script:BoundedLf
        "prepared.workflow.json" = $preparedText
        "mutation-intent.json" = (Get-BoundedCanonicalJsonFromObject $intent) + $script:BoundedLf
    }
    if ($preimageState -eq "existing") { $artifacts["preimage.workflow.json"] = $preimageText }
    else { $artifacts["absence-evidence.json"] = $preimageText }
    return [pscustomobject]([ordered]@{
        artifacts = $artifacts
        plan = $plan
        plan_digest = $planDigest
        operation_identity = $operationIdentity
        expected_projection = $projection
    })
}

function Initialize-BoundedPrivateRoot {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$OperationsPath,
        [Parameter(Mandatory)][string]$OperationPath
    )
    Assert-BoundedPrivateDestination $Root $OperationsPath
    $operationsParent = Split-Path -Parent (Resolve-BoundedFullPath $OperationsPath)
    New-Item -ItemType Directory -Path $operationsParent -Force | Out-Null
    New-Item -ItemType Directory -Path $OperationsPath -Force | Out-Null
    Set-BoundedProtectedAcl $OperationsPath
    if (Test-Path -LiteralPath $OperationPath) {
        Assert-BoundedNoUnsafePathComponents $OperationPath $OperationsPath
        Set-BoundedProtectedAcl $OperationPath
    } else {
        New-Item -ItemType Directory -Path $OperationPath | Out-Null
        Set-BoundedProtectedAcl $OperationPath
    }
    return New-BoundedStagingRoot $OperationsPath
}

function Publish-BoundedPlanArtifacts {
    param(
        [Parameter(Mandatory)]$Artifacts,
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$OperationsPath,
        [Parameter(Mandatory)][string]$OperationPath
    )
    if (Test-Path -LiteralPath $OperationPath) {
        $existing = @(Get-ChildItem -LiteralPath $OperationPath -Force)
        if ($existing.Count -gt 0) { Stop-Bounded "operation_already_exists" }
    }
    $stage = $null
    $createdOperation = $false
    try {
        $stage = New-BoundedStagingRoot $OperationsPath
        foreach ($name in $Artifacts.artifacts.Keys) {
            Write-BoundedCreateNewText $stage $stage $name ([string]$Artifacts.artifacts[$name])
        }
        New-Item -ItemType Directory -Path $OperationPath -Force | Out-Null
        Set-BoundedProtectedAcl $OperationPath
        foreach ($name in $Artifacts.artifacts.Keys) {
            $source = Join-Path $stage $name
            $target = Join-Path $OperationPath $name
            if (Test-Path -LiteralPath $target) { Stop-Bounded "evidence_already_exists" }
            [System.IO.File]::Move($source, $target)
            Set-BoundedProtectedAcl $target
        }
        Remove-Item -LiteralPath $stage -Force -ErrorAction Stop
        $stage = $null
    } catch {
        if (-not $script:BoundedPrivateBytesWritten) {
            if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
            if ((Test-Path -LiteralPath $OperationPath) -and @(Get-ChildItem -LiteralPath $OperationPath -Force).Count -eq 0) {
                Remove-Item -LiteralPath $OperationPath -Force -ErrorAction SilentlyContinue
            }
        }
        throw
    }
}

function Write-BoundedOperationReceipt {
    param(
        [Parameter(Mandatory)][string]$OperationsPath,
        [Parameter(Mandatory)][string]$OperationPath,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)]$Value
    )
    $stage = New-BoundedStagingRoot $OperationsPath
    try {
        $text = (Get-BoundedCanonicalJsonFromObject $Value) + $script:BoundedLf
        Write-BoundedCreateNewText $stage $stage $Name $text
        $target = Join-Path $OperationPath $Name
        if (Test-Path -LiteralPath $target) { Stop-Bounded "evidence_already_exists" }
        [System.IO.File]::Move((Join-Path $stage $Name), $target)
        Set-BoundedProtectedAcl $target
    } finally {
        if (Test-Path -LiteralPath $stage) {
            if (@(Get-ChildItem -LiteralPath $stage -Force).Count -eq 0) { Remove-Item -LiteralPath $stage -Force -ErrorAction SilentlyContinue }
        }
    }
}

function Compare-BoundedText {
    param([Parameter(Mandatory)][string]$Left, [Parameter(Mandatory)][string]$Right)
    return $Left.Equals($Right, [StringComparison]::Ordinal)
}

function Assert-BoundedPlanEvidence {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)]$Evidence,
        [Parameter(Mandatory)]$Manifest
    )
    Assert-BoundedCursor $Evidence.cursor $Manifest
    $cursorDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $Evidence.cursor)
    if (-not (Compare-BoundedText $cursorDigest ([string]$State.plan.identity_seed.cursor_digest))) { Stop-Bounded "cursor_digest_mismatch" }
    if (-not (Compare-BoundedText ([string]$Evidence.cursor.watermark) ([string]$State.plan.identity_seed.watermark_value))) { Stop-Bounded "watermark_value_mismatch" }
    $watermarkDigest = Get-BoundedWatermarkDigest ([string]$Manifest.source_system) ([string]$Manifest.form_alias) ([string]$Manifest.mapping_version) ([string]$Evidence.cursor.watermark)
    if (-not (Compare-BoundedText $watermarkDigest ([string]$State.plan.identity_seed.watermark_digest))) { Stop-Bounded "watermark_digest_mismatch" }
    if (-not (Compare-BoundedText ([string]$Evidence.selection.state) ([string]$State.plan.identity_seed.preimage_state))) { Stop-Bounded "preimage_state_mismatch" }
    if ($Evidence.selection.state -eq "existing") {
        $currentText = Get-BoundedWorkflowJsonText $Evidence.workflow
        $currentDigest = Get-BoundedSha256Text $currentText
        if (-not (Compare-BoundedText $currentDigest ([string]$State.plan.identity_seed.preimage_digest))) { Stop-Bounded "preimage_digest_mismatch" }
    } else {
        $absence = New-BoundedAbsenceEvidence $Evidence.selection $Manifest
        $absenceDigest = Get-BoundedSha256Text ((Get-BoundedCanonicalJsonFromObject $absence) + $script:BoundedLf)
        if (-not (Compare-BoundedText $absenceDigest ([string]$State.plan.identity_seed.preimage_digest))) { Stop-Bounded "absence_digest_mismatch" }
    }
}

function Assert-BoundedCurrentOperationIdentity {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)]$Evidence,
        [Parameter(Mandatory)]$Manifest
    )
    $currentRepository = Get-BoundedRepositoryIdentity $script:BoundedRepoRoot
    $currentRepositoryDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $currentRepository)
    $plannedRepositoryDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $State.plan.identity_seed.repository)
    if (-not (Compare-BoundedText $currentRepositoryDigest $plannedRepositoryDigest)) { Stop-Bounded "repository_identity_mismatch" }
    $manifestDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $Manifest)
    if (-not (Compare-BoundedText $manifestDigest ([string]$State.plan.identity_seed.binding_manifest_digest))) { Stop-Bounded "binding_digest_mismatch" }
    Assert-BoundedCursor $Evidence.cursor $Manifest
    $cursorDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $Evidence.cursor)
    if (-not (Compare-BoundedText $cursorDigest ([string]$State.plan.identity_seed.cursor_digest))) { Stop-Bounded "cursor_digest_mismatch" }
    if (-not (Compare-BoundedText ([string]$Evidence.cursor.state_version) ([string]$State.plan.identity_seed.cursor_state_version))) { Stop-Bounded "cursor_state_version_mismatch" }
    if (-not (Compare-BoundedText ([string]$Evidence.cursor.watermark) ([string]$State.plan.identity_seed.watermark_value))) { Stop-Bounded "watermark_value_mismatch" }
    $watermarkDigest = Get-BoundedWatermarkDigest ([string]$Manifest.source_system) ([string]$Manifest.form_alias) ([string]$Manifest.mapping_version) ([string]$Evidence.cursor.watermark)
    if (-not (Compare-BoundedText $watermarkDigest ([string]$State.plan.identity_seed.watermark_digest))) { Stop-Bounded "watermark_digest_mismatch" }
    $seedDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $State.plan.identity_seed)
    if (-not (Compare-BoundedText $seedDigest ([string]$State.plan.operation_identity))) { Stop-Bounded "operation_identity_mismatch" }
}

function Test-BoundedExpectedTarget {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)]$Evidence
    )
    if ($Evidence.selection.state -ne "existing") { return $false }
    $actualProjection = Get-BoundedWorkflowProjection $Evidence.workflow
    $actualDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $actualProjection -AllowFloatingPoint)
    return Compare-BoundedText $actualDigest ([string]$State.plan.expected_projection_digest)
}

function Invoke-BoundedOfflineDispatch {
    param([Parameter(Mandatory)]$Fixture)
    $mode = [string]$Fixture.dispatch_mode
    if ($mode -eq "pre_dispatch_failure") {
        return [pscustomobject]([ordered]@{ started = $false; outcome = "pre_dispatch_failure" })
    }
    if ($mode -eq "ambiguous") {
        return [pscustomobject]([ordered]@{ started = $true; outcome = "ambiguous" })
    }
    if ($mode -ne "success") { Stop-Bounded "fixture_dispatch_mode_invalid" }
    return [pscustomobject]([ordered]@{ started = $true; outcome = "completed" })
}

function Invoke-BoundedMutation {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)][string]$OperationPath
    )
    Assert-BoundedNoGenericHooks $script:BoundedRepoRoot
    $preparedPath = Join-Path $OperationPath "prepared.workflow.json"
    $prepared = Read-BoundedWorkflowFile $preparedPath
    Assert-BoundedInactiveWorkflow $prepared
    if ($script:BoundedTestOnly) {
        return Invoke-BoundedOfflineDispatch $script:BoundedFixture
    }
    $preparedText = (Get-BoundedUtf8NoBom).GetString([System.IO.File]::ReadAllBytes($preparedPath))
    $null = Invoke-BoundedExternalJson "import:workflow" @("--input=-") $preparedText
    return [pscustomobject]([ordered]@{ started = $true; outcome = "completed" })
}

function Get-BoundedReceipt {
    param([Parameter(Mandatory)][string]$OperationPath, [Parameter(Mandatory)][string]$Name)
    $path = Join-Path $OperationPath $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    return Read-BoundedJsonFile $path
}

function New-BoundedDispatchReceipt {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)]$Result
    )
    return [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.dispatch.v2"
        operation_id = [string]$State.plan.operation_id
        operation_identity = [string]$State.plan.operation_identity
        plan_digest = [string]$State.plan_digest
        mutation_intent_digest = Get-BoundedSha256File (Join-Path $State.operation_path "mutation-intent.json")
        dispatch_state = if ($Result.started) { "dispatched" } else { "not_dispatched" }
        outcome = [string]$Result.outcome
        replay_allowed = $false
    })
}

function New-BoundedCompletionReceipt {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)][string]$DispatchDigest,
        [Parameter(Mandatory)]$Evidence
    )
    return [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.completion.v2"
        operation_id = [string]$State.plan.operation_id
        operation_identity = [string]$State.plan.operation_identity
        plan_digest = [string]$State.plan_digest
        dispatch_digest = $DispatchDigest
        expected_projection_digest = [string]$State.plan.expected_projection_digest
        target_projection_digest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject -Value (Get-BoundedWorkflowProjection $Evidence.workflow) -AllowFloatingPoint)
        original_preimage_state = [string]$State.plan.identity_seed.preimage_state
        ownership = if ([string]$State.plan.identity_seed.preimage_state -eq "absent") { "created" } else { "updated_or_noop" }
        complete = $true
    })
}

function Invoke-BoundedApply {
    param(
        [Parameter(Mandatory)]$Manifest,
        [Parameter(Mandatory)][string]$OperationsPath,
        [Parameter(Mandatory)][string]$OperationPath
    )
    Assert-BoundedNoGenericHooks $script:BoundedRepoRoot
    Assert-BoundedReviewedBinding $Manifest
    $state = Get-BoundedOperationState $OperationPath
    $expected = Read-BoundedWorkflowFile (Join-Path $OperationPath "prepared.workflow.json")
    $completion = $state.completion
    if ($null -ne $completion) {
        $evidence = Get-BoundedEvidence $Manifest -AfterDispatch
        Assert-BoundedCurrentOperationIdentity $state $evidence $Manifest
        if (-not (Test-BoundedExpectedTarget $state $evidence)) { Stop-Bounded "completed_target_changed" }
        return [pscustomobject]([ordered]@{ status = "no_op_success"; mutation_attempted = 0; replay = $false; operation_identity = [string]$state.plan.operation_identity })
    }
    if ($null -ne $state.dispatch) {
        $evidence = Get-BoundedEvidence $Manifest -AfterDispatch
        Assert-BoundedCurrentOperationIdentity $state $evidence $Manifest
        if (Test-BoundedExpectedTarget $state $evidence) {
            $receipt = New-BoundedCompletionReceipt $state (Get-BoundedSha256File (Join-Path $OperationPath "dispatch-receipt.json")) $evidence
            Write-BoundedOperationReceipt $OperationsPath $OperationPath "completion-receipt.json" $receipt
            return [pscustomobject]([ordered]@{ status = "completed_existing_dispatch"; mutation_attempted = 0; replay = $false; operation_identity = [string]$state.plan.operation_identity })
        }
        Stop-Bounded "dispatch_outcome_ambiguous"
    }
    $before = Get-BoundedEvidence $Manifest
    Assert-BoundedCurrentOperationIdentity $state $before $Manifest
    Assert-BoundedPlanEvidence $state $before $Manifest
    $result = Invoke-BoundedMutation $state $OperationPath
    if (-not $result.started) {
        Stop-Bounded "pre_dispatch_failure"
    }
    $dispatch = New-BoundedDispatchReceipt $state $result
    Write-BoundedOperationReceipt $OperationsPath $OperationPath "dispatch-receipt.json" $dispatch
    if ([string]$result.outcome -eq "ambiguous") {
        Stop-Bounded "dispatch_outcome_ambiguous"
    }
    $after = Get-BoundedEvidence $Manifest -AfterDispatch
    Assert-BoundedCurrentOperationIdentity $state $after $Manifest
    if (-not (Test-BoundedExpectedTarget $state $after)) {
        Stop-Bounded "readback_mismatch"
    }
    $completion = New-BoundedCompletionReceipt $state (Get-BoundedSha256File (Join-Path $OperationPath "dispatch-receipt.json")) $after
    Write-BoundedOperationReceipt $OperationsPath $OperationPath "completion-receipt.json" $completion
    return [pscustomobject]([ordered]@{ status = "applied_and_verified"; mutation_attempted = 1; replay = $false; operation_identity = [string]$state.plan.operation_identity })
}

function Initialize-BoundedContext {
    $root = if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
        Resolve-BoundedFullPath (Join-Path $PSScriptRoot "..\..")
    } else {
        Resolve-BoundedFullPath $RepoRoot
    }
    if (-not (Test-Path -LiteralPath (Join-Path $root ".git"))) { Stop-Bounded "repository_root_invalid" }
    $operations = Resolve-BoundedFullPath (Join-Path $root $OperationsRoot)
    $script:BoundedRepoRoot = $root
    $script:BoundedOperationsRoot = $operations
    $script:BoundedTestOnly = [bool]$TestOnly
    if ($TestOnly -and [string]::IsNullOrWhiteSpace($FixtureFile)) { Stop-Bounded "fixture_required" }
    if ($TestOnly) {
        $script:BoundedFixture = Read-BoundedJsonFile (Resolve-BoundedFullPath $FixtureFile) -AllowFloatingPoint
    }
    Assert-BoundedNoGenericHooks $root
    if ([string]::IsNullOrWhiteSpace($OperationId) -or $OperationId -notmatch '^[a-z0-9][a-z0-9._-]{2,96}$') { Stop-Bounded "operation_id_invalid" }
    return [pscustomobject]@{ root = $root; operations = $operations; operation = Join-Path $operations $OperationId }
}

function Invoke-BoundedMain {
    try {
        $context = Initialize-BoundedContext
        if ($Mode -eq "Apply" -and -not $ConfirmBoundedApply -and -not $TestOnly) { Stop-Bounded "apply_confirmation_required" }
        $manifestPath = if ([System.IO.Path]::IsPathRooted($BindingManifestFile)) { $BindingManifestFile } else { Join-Path $context.root $BindingManifestFile }
        $manifest = Read-BoundedJsonFile (Resolve-BoundedFullPath $manifestPath)
        Assert-BoundedManifest $manifest
        Assert-BoundedReviewedBinding $manifest
        if ($Mode -eq "CapturePlan") {
            if (Test-Path -LiteralPath $context.operation) {
                Stop-Bounded "operation_already_exists"
            }
            $evidence = Get-BoundedEvidence $manifest
            if (($evidence.selection.state -eq "absent") -and ((Test-BoundedPlaceholder ([string]$manifest.workflow.id)) -or (Test-BoundedPlaceholder ([string]$manifest.project.id)))) {
                Stop-Bounded "binding_placeholder_unresolved"
            }
            $artifacts = New-BoundedPlanArtifacts $manifest $evidence $context.root $OperationId
            Assert-BoundedPrivateDestination $context.root $context.operations
            $stage = Initialize-BoundedPrivateRoot $context.root $context.operations $context.operation
            if ($stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
            Publish-BoundedPlanArtifacts $artifacts $context.root $context.operations $context.operation
            Write-Output (([pscustomobject]@{ status = "capture_plan_complete"; operation_id = $OperationId; operation_identity = $artifacts.operation_identity; mutation_attempted = 0 }) | ConvertTo-Json -Compress)
            $script:BoundedExitCode = 0
            return
        }
        if ($Mode -eq "Inspect") {
            $state = Get-BoundedOperationState $context.operation
            Write-Output (([pscustomobject]@{ status = "operation_valid"; operation_id = $OperationId; operation_identity = $state.plan.operation_identity; dispatch_present = ($null -ne $state.dispatch); completion_present = ($null -ne $state.completion) }) | ConvertTo-Json -Compress)
            $script:BoundedExitCode = 0
            return
        }
        $result = Invoke-BoundedApply $manifest $context.operations $context.operation
        Write-Output ($result | ConvertTo-Json -Compress)
        $script:BoundedExitCode = 0
        return
    } catch {
        Write-Error ("bounded_import_blocked:" + [string]$_.Exception.Message)
        $script:BoundedExitCode = 1
        return
    }
}

if ($MyInvocation.InvocationName -ne ".") {
    Invoke-BoundedMain
    exit $script:BoundedExitCode
}
