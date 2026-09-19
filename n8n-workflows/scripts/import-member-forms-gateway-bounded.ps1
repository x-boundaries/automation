[CmdletBinding()]
param(
    [ValidateSet("CapturePlan", "Apply", "Inspect")]
    [string]$Mode = "CapturePlan",
    [string]$RepoRoot = "",
    [string]$OperationId = "",
    [string]$BindingManifestFile = "config/member_forms_gateway_bounded_import.v2.template.json",
    [string]$OperationsRoot = ".n8n-local/member-gateway-bounded-import/operations",
    [string]$N8nExecutable = "n8n",
    [string]$DockerExecutable = "docker",
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
$script:BoundedDockerExecutable = $DockerExecutable

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
        [switch]$AllowFloatingPoint,
        [switch]$RequireCanonical
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
    $canonical = if ($AllowFloatingPoint) {
        Get-BoundedCanonicalJsonFromText $text -AllowFloatingPoint
    } else {
        Get-BoundedCanonicalJsonFromText $text
    }
    if ($RequireCanonical -and -not $text.Equals($canonical + $script:BoundedLf, [StringComparison]::Ordinal)) {
        Stop-Bounded "json_not_canonical"
    }
    if ($AllowFloatingPoint) {
        return $canonical | ConvertFrom-Json -Depth 100 -DateKind String
    }
    return $canonical | ConvertFrom-Json -Depth 100 -DateKind String
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

function Assert-BoundedBoolean {
    param(
        [Parameter(Mandatory)]$Value,
        [Parameter(Mandatory)][string]$Code
    )
    if ($Value -isnot [bool]) { Stop-Bounded $Code }
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

function Get-BoundedHttpsUri {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Code
    )
    try {
        $uri = [System.Uri]::new($Value, [System.UriKind]::Absolute)
    } catch {
        Stop-Bounded $Code
    }
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -cne "https" -or -not [string]::IsNullOrEmpty($uri.UserInfo) -or -not [string]::IsNullOrEmpty($uri.Fragment)) {
        Stop-Bounded $Code
    }
    if ([string]::IsNullOrWhiteSpace($uri.Host) -or $uri.Host.EndsWith(".", [StringComparison]::Ordinal)) {
        Stop-Bounded $Code
    }
    return $uri
}

function Get-BoundedCanonicalOrigin {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Code
    )
    $uri = Get-BoundedHttpsUri $Value $Code
    if ($uri.Port -ne 443 -or
        -not [string]::IsNullOrEmpty($uri.UserInfo) -or
        -not [string]::IsNullOrEmpty($uri.Query) -or
        -not [string]::IsNullOrEmpty($uri.Fragment) -or
        $uri.AbsolutePath -ne "/") {
        Stop-Bounded $Code
    }
    $originHost = $uri.Host.ToLowerInvariant()
    $authorityHost = if ($uri.HostNameType -eq [System.UriHostNameType]::IPv6) { "[" + $originHost + "]" } else { $originHost }
    $canonical = "https://{0}:443" -f $authorityHost
    if ([string]$Value -cne $canonical) { Stop-Bounded $Code }
    return $uri
}

function Assert-BoundedOriginMatches {
    param(
        [Parameter(Mandatory)][System.Uri]$Uri,
        [Parameter(Mandatory)][System.Uri]$ApprovedOrigin,
        [Parameter(Mandatory)][string]$Code
    )
    if (-not (Test-BoundedSameOrigin $Uri $ApprovedOrigin)) {
        Stop-Bounded $Code
    }
}

function Test-BoundedSameOrigin {
    param(
        [Parameter(Mandatory)][System.Uri]$Left,
        [Parameter(Mandatory)][System.Uri]$Right
    )
    return $Left.Scheme.Equals($Right.Scheme, [StringComparison]::OrdinalIgnoreCase) -and
        $Left.Host.Equals($Right.Host, [StringComparison]::OrdinalIgnoreCase) -and
        $Left.Port -eq $Right.Port
}

function Assert-BoundedManifestEndpointRelationships {
    param([Parameter(Mandatory)]$Manifest)
    $source = Get-BoundedHttpsUri ([string]$Manifest.endpoints.source_cursor) "binding_endpoint_relationship_invalid"
    $forms = Get-BoundedHttpsUri ([string]$Manifest.endpoints.forms_responses) "binding_forms_origin_invalid"
    $gateway = Get-BoundedHttpsUri ([string]$Manifest.endpoints.gateway_ingest) "binding_endpoint_relationship_invalid"
    $checkpoint = Get-BoundedHttpsUri ([string]$Manifest.endpoints.page_checkpoint) "binding_endpoint_relationship_invalid"
    foreach ($uri in @($source, $forms, $gateway, $checkpoint)) {
        if (-not [string]::IsNullOrEmpty($uri.Query)) { Stop-Bounded "binding_endpoint_query_invalid" }
        if ([string]::IsNullOrWhiteSpace($uri.AbsolutePath) -or $uri.AbsolutePath -eq "/") { Stop-Bounded "binding_endpoint_relationship_invalid" }
    }
    $approvedGatewayOrigin = Get-BoundedCanonicalOrigin ([string]$Manifest.security.approved_gateway_origin) "binding_gateway_origin_invalid"
    $approvedFormsOrigin = Get-BoundedCanonicalOrigin "https://forms.googleapis.com:443" "binding_forms_origin_invalid"
    Assert-BoundedOriginMatches $source $approvedGatewayOrigin "binding_gateway_origin_invalid"
    Assert-BoundedOriginMatches $gateway $approvedGatewayOrigin "binding_gateway_origin_invalid"
    Assert-BoundedOriginMatches $checkpoint $approvedGatewayOrigin "binding_gateway_origin_invalid"
    Assert-BoundedOriginMatches $forms $approvedFormsOrigin "binding_forms_origin_invalid"
    if (-not (Test-BoundedSameOrigin $source $gateway) -or -not (Test-BoundedSameOrigin $source $checkpoint)) {
        Stop-Bounded "binding_endpoint_relationship_invalid"
    }
    $sourcePath = $source.AbsolutePath.TrimEnd([char]'/')
    $checkpointPath = $checkpoint.AbsolutePath.TrimEnd([char]'/')
    if ($checkpointPath -cne ($sourcePath + "/page")) { Stop-Bounded "binding_endpoint_relationship_invalid" }
    $expectedFormsPath = "/v1/forms/" + [string]$Manifest.form.id + "/responses"
    if ($forms.AbsolutePath -cne $expectedFormsPath -or
        ([string]$Manifest.endpoints.forms_responses).Equals("https://forms.googleapis.com:443" + $expectedFormsPath, [StringComparison]::Ordinal) -eq $false) {
        Stop-Bounded "binding_form_endpoint_mismatch"
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
        Assert-BoundedExactProperties $role @("credential_id", "credential_name", "credential_type", "node_names") "binding_credential_role_shape_invalid"
        Assert-BoundedSafeIdentity ([string]$role.credential_id) "binding_credential_invalid" -AllowPlaceholder
        Assert-BoundedSafeIdentity ([string]$role.credential_name) "binding_credential_invalid" -AllowPlaceholder
        Assert-BoundedSafeIdentity ([string]$role.credential_type) "binding_credential_invalid"
        $expectedCredentialType = if ($roleName -eq "google_forms_oauth") { "googleOAuth2Api" } else { "httpBearerAuth" }
        if ([string]$role.credential_type -cne $expectedCredentialType) { Stop-Bounded "binding_credential_type_invalid" }
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

    Assert-BoundedExactProperties $Manifest.security @("approved_gateway_origin", "source_token_env", "n8n_target_mode") "binding_security_shape_invalid"
    Get-BoundedCanonicalOrigin ([string]$Manifest.security.approved_gateway_origin) "binding_security_invalid" | Out-Null
    if ([string]$Manifest.security.source_token_env -notmatch '^[A-Z][A-Z0-9_]{2,80}$') { Stop-Bounded "binding_security_invalid" }
    if ([string]$Manifest.security.n8n_target_mode -cne "explicit-reviewed-target") { Stop-Bounded "binding_security_invalid" }
    Assert-BoundedManifestEndpointRelationships $Manifest
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
    foreach ($roleName in @("google_forms_oauth", "gateway_bearer")) {
        $role = $Manifest.credential_roles.$roleName
        if ((Test-BoundedPlaceholder ([string]$role.credential_id)) -or (Test-BoundedPlaceholder ([string]$role.credential_name))) {
            Stop-Bounded "binding_credential_reference_unresolved"
        }
        Assert-BoundedSafeIdentity ([string]$role.credential_id) "binding_credential_reference_invalid"
        Assert-BoundedSafeIdentity ([string]$role.credential_name) "binding_credential_reference_invalid"
    }
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
    param(
        [Parameter(Mandatory)]$Workflow,
        [Parameter(Mandatory)]$Manifest
    )
    $activation = Get-BoundedActivationValue $Workflow
    $staticData = if ($Workflow.PSObject.Properties.Name -contains "staticData") { $Workflow.staticData } else { $null }
    $pinData = if ($Workflow.PSObject.Properties.Name -contains "pinData") { $Workflow.pinData } else { $null }
    $projected = ConvertFrom-BoundedWorkflowJsonText ($Workflow | ConvertTo-Json -Depth 100 -Compress)
    $roleByNode = @{}
    foreach ($roleEntry in @($Manifest.credential_roles.PSObject.Properties)) {
        $roleName = [string]$roleEntry.Name
        $role = $roleEntry.Value
        foreach ($nodeNameValue in @($role.node_names)) {
            $nodeName = [string]$nodeNameValue
            if ($roleByNode.ContainsKey($nodeName)) { Stop-Bounded "credential_role_collision" }
            $roleByNode[$nodeName] = $roleName
        }
    }
    $seenCredentialNodes = New-Object System.Collections.Generic.HashSet[string]
    foreach ($node in @($projected.nodes)) {
        $nodeName = [string]$node.name
        $hasCredentials = $node.PSObject.Properties.Name -contains "credentials"
        if (-not $roleByNode.ContainsKey($nodeName)) {
            if ($hasCredentials) { Stop-Bounded "credential_role_unexpected" }
            continue
        }
        if (-not $hasCredentials -or $null -eq $node.credentials) { Stop-Bounded "credential_binding_missing" }
        $roleName = [string]$roleByNode[$nodeName]
        $role = $Manifest.credential_roles.$roleName
        $expectedType = [string]$role.credential_type
        $credentialProperties = @(Get-BoundedPropertyNames $node.credentials)
        if ($credentialProperties.Count -ne 1 -or $credentialProperties[0] -cne $expectedType) {
            Stop-Bounded "credential_role_mismatch"
        }
        $resolved = $node.credentials.$expectedType
        if ($null -eq $resolved) { Stop-Bounded "credential_binding_invalid" }
        $resolvedProperties = @(Get-BoundedPropertyNames $resolved | Sort-Object)
        $nameOnly = @("name")
        $resolvedShape = @("id", "name")
        $isNameOnly = $resolvedProperties.Count -eq 1 -and $resolvedProperties[0] -ceq "name"
        $isResolved = $resolvedProperties.Count -eq 2 -and
            $resolvedProperties[0] -ceq "id" -and $resolvedProperties[1] -ceq "name"
        if (-not $isNameOnly -and -not $isResolved) { Stop-Bounded "credential_resolution_shape_invalid" }
        if ([string]::IsNullOrWhiteSpace([string]$resolved.name) -or [string]$resolved.name -cne [string]$role.credential_name) {
            Stop-Bounded "credential_name_mismatch"
        }
        if ($isResolved) {
            Assert-BoundedSafeIdentity ([string]$resolved.id) "credential_resolution_id_invalid"
            if ([string]$resolved.id -cne [string]$role.credential_id) {
                Stop-Bounded "credential_id_mismatch"
            }
        }
        $normalisedCredential = [ordered]@{}
        $normalisedValue = [ordered]@{ name = [string]$role.credential_name }
        if ($isResolved) { $normalisedValue = [ordered]@{ id = [string]$resolved.id; name = [string]$role.credential_name } }
        $normalisedCredential[$expectedType] = $normalisedValue
        Set-BoundedProperty $node "credentials" ([pscustomobject]$normalisedCredential)
        [void]$seenCredentialNodes.Add($nodeName)
    }
    foreach ($nodeName in $roleByNode.Keys) {
        if (-not $seenCredentialNodes.Contains([string]$nodeName)) { Stop-Bounded "credential_binding_missing" }
    }
    return [pscustomobject]([ordered]@{
        id = [string]$Workflow.id
        name = [string]$Workflow.name
        active = [bool]$Workflow.active
        activation_enabled = $activation
        nodes = $projected.nodes
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
    $replacementTokens = [ordered]@{}
    $replacementIndex = 0
    foreach ($key in ($replacements.Keys | Sort-Object { $_.Length } -Descending)) {
        $token = "__XB_BOUNDARY_REPLACEMENT_{0}__" -f $replacementIndex
        $replacementIndex++
        $replacementTokens[$token] = [string]$replacements[$key]
        $preparedText = $preparedText.Replace([string]$key, $token)
    }
    foreach ($token in $replacementTokens.Keys) {
        $preparedText = $preparedText.Replace([string]$token, [string]$replacementTokens[$token])
    }
    if ($preparedText -match '[A-Z0-9_]+_PLACEHOLDER') { Stop-Bounded "binding_placeholder_unresolved" }
    $prepared = ConvertFrom-BoundedWorkflowJsonText $preparedText
    Set-BoundedProperty $prepared "id" ([string]$Manifest.workflow.id)
    Set-BoundedProperty $prepared "name" ([string]$Manifest.workflow.name)

    $formsRole = $Manifest.credential_roles.google_forms_oauth
    $gatewayRole = $Manifest.credential_roles.gateway_bearer
    foreach ($role in @($formsRole, $gatewayRole)) {
        if (Test-BoundedPlaceholder ([string]$role.credential_id)) { Stop-Bounded "binding_credential_reference_unresolved" }
        Assert-BoundedSafeIdentity ([string]$role.credential_id) "binding_credential_reference_invalid"
    }
    $formsCredentialNodeCount = 0
    $gatewayCredentialNodeCount = 0
    foreach ($node in @($prepared.nodes)) {
        $nodeName = [string]$node.name
        if ($nodeName -in @($formsRole.node_names)) {
            if ($null -eq $node.parameters) { Stop-Bounded "binding_credential_binding_invalid" }
            $credential = [ordered]@{}
            $credential[[string]$formsRole.credential_type] = [ordered]@{ id = [string]$formsRole.credential_id; name = [string]$formsRole.credential_name }
            Set-BoundedProperty $node "credentials" $credential
            Set-BoundedProperty $node.parameters "authentication" "predefinedCredentialType"
            Set-BoundedProperty $node.parameters "nodeCredentialType" "googleOAuth2Api"
            $formsCredentialNodeCount++
        } elseif ($nodeName -in @($gatewayRole.node_names)) {
            if ($null -eq $node.parameters) { Stop-Bounded "binding_credential_binding_invalid" }
            $credential = [ordered]@{}
            $credential[[string]$gatewayRole.credential_type] = [ordered]@{ id = [string]$gatewayRole.credential_id; name = [string]$gatewayRole.credential_name }
            Set-BoundedProperty $node "credentials" $credential
            Set-BoundedProperty $node.parameters "authentication" "genericCredentialType"
            Set-BoundedProperty $node.parameters "genericAuthType" "httpBearerAuth"
            $gatewayCredentialNodeCount++
        }
    }
    if ($formsCredentialNodeCount -ne @($formsRole.node_names).Count -or $gatewayCredentialNodeCount -ne @($gatewayRole.node_names).Count) {
        Stop-Bounded "binding_credential_binding_invalid"
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

function Get-BoundedOptionalProperty {
    param(
        [Parameter(Mandatory)]$Object,
        [Parameter(Mandatory)][string]$Name
    )
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
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
        archived = [bool](Get-BoundedOptionalProperty $Entry "isArchived")
    })
}

function Get-BoundedWorkflowProjectIdentity {
    param([Parameter(Mandatory)]$Workflow)
    $candidates = New-Object System.Collections.Generic.List[object]
    $properties = @($Workflow.PSObject.Properties.Name)
    if ($properties -contains "projectId" -and $properties -contains "projectName") {
        $candidates.Add([pscustomobject]@{ id = [string]$Workflow.projectId; name = [string]$Workflow.projectName })
    }
    foreach ($shared in @(Get-BoundedOptionalProperty $Workflow "shared")) {
        if ($null -eq $shared) { continue }
        $projectId = [string](Get-BoundedOptionalProperty $shared "projectId")
        $projectName = [string](Get-BoundedOptionalProperty $shared "projectName")
        $sharedProject = Get-BoundedOptionalProperty $shared "project"
        if ($null -ne $sharedProject) {
            if ([string]::IsNullOrWhiteSpace($projectId)) { $projectId = [string](Get-BoundedOptionalProperty $sharedProject "id") }
            if ([string]::IsNullOrWhiteSpace($projectName)) { $projectName = [string](Get-BoundedOptionalProperty $sharedProject "name") }
        }
        if (-not [string]::IsNullOrWhiteSpace($projectId) -and -not [string]::IsNullOrWhiteSpace($projectName)) {
            $candidates.Add([pscustomobject]@{ id = $projectId; name = $projectName })
        }
    }
    if ($candidates.Count -eq 0) { Stop-Bounded "metadata_project_unavailable" }
    $first = $candidates[0]
    foreach ($candidate in $candidates) {
        if ([string]$candidate.id -cne [string]$first.id -or [string]$candidate.name -cne [string]$first.name) {
            Stop-Bounded "metadata_project_conflict"
        }
    }
    return $first
}

function ConvertFrom-BoundedWorkflowExportText {
    param([Parameter(Mandatory)][string]$Text)
    try {
        $value = ConvertFrom-BoundedWorkflowJsonText $Text
    } catch {
        Stop-Bounded "n8n_workflow_export_shape_invalid"
    }
    $items = if ($value -is [System.Array]) { @($value) } elseif ($value.PSObject.Properties.Name -contains "data") { @($value.data) } else { @($value) }
    if ($items.Count -ne 1 -or $null -eq $items[0]) { Stop-Bounded "n8n_workflow_export_shape_invalid" }
    return $items[0]
}

function ConvertFrom-BoundedWorkflowListText {
    param([Parameter(Mandatory)][string]$Text)
    $trimmed = $Text.Trim()
    if ([string]::IsNullOrWhiteSpace($trimmed)) { return @() }
    if ($trimmed.StartsWith("[") -or $trimmed.StartsWith("{")) {
        try {
            $value = ConvertFrom-BoundedJsonText $trimmed
        } catch {
            Stop-Bounded "n8n_workflow_list_shape_invalid"
        }
        if ($value.PSObject.Properties.Name -contains "data") { return @($value.data) }
        if ($value.PSObject.Properties.Name -contains "workflows") { return @($value.workflows) }
        if ($value -is [System.Array]) { return @($value) }
        return @($value)
    }
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($line in ($trimmed -split "`n")) {
        $clean = $line.TrimEnd("`r")
        if ([string]::IsNullOrWhiteSpace($clean)) { continue }
        $parts = $clean.Split([char[]]@("|"), 2)
        if ($parts.Count -ne 2 -or [string]::IsNullOrWhiteSpace($parts[0]) -or [string]::IsNullOrWhiteSpace($parts[1])) {
            Stop-Bounded "n8n_workflow_list_shape_invalid"
        }
        $rows.Add([pscustomobject]([ordered]@{ id = $parts[0]; name = $parts[1] }))
    }
    return @($rows.ToArray())
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
    foreach ($safe in $all) {
        $sameWorkflowId = [string]$safe.workflow_id -ieq [string]$Manifest.workflow.id
        $sameTargetProjectAndName = [string]$safe.project_id -ieq [string]$Manifest.project.id -and [string]$safe.workflow_name -ieq [string]$Manifest.workflow.name
        if ($sameWorkflowId -and (
            [string]$safe.project_id -cne [string]$Manifest.project.id -or
            [string]$safe.project_name -cne [string]$Manifest.project.name -or
            [string]$safe.workflow_id -cne [string]$Manifest.workflow.id -or
            [string]$safe.workflow_name -cne [string]$Manifest.workflow.name
        )) {
            Stop-Bounded "target_identity_conflict"
        }
        if ($sameTargetProjectAndName -and [string]$safe.workflow_id -cne [string]$Manifest.workflow.id) {
            Stop-Bounded "target_identity_conflict"
        }
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

function Invoke-BoundedProcess {
    param(
        [Parameter(Mandatory)][string]$Command,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Arguments,
        [string]$InputText = ""
    )
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $command
    $processInfo.UseShellExecute = $false
    $processInfo.RedirectStandardInput = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $processInfo.CreateNoWindow = $true
    foreach ($argument in $Arguments) { [void]$processInfo.ArgumentList.Add([string]$argument) }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $processInfo
    try {
        if (-not $process.Start()) { Stop-Bounded "n8n_command_start_failed" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not [string]::IsNullOrEmpty($InputText)) {
            $process.StandardInput.Write($InputText)
        }
        $process.StandardInput.Close()
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        return [pscustomobject]([ordered]@{
            exit_code = [int]$process.ExitCode
            stdout = [string]$stdout
            stderr = [string]$stderr
        })
    } finally {
        $process.Dispose()
    }
}

function Invoke-BoundedDockerCommand {
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Arguments)
    return Invoke-BoundedProcess -Command $script:BoundedDockerExecutable -Arguments $Arguments
}

function Invoke-BoundedContainerExec {
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Arguments,
        [switch]$Root
    )
    $dockerArguments = @("exec", "-i")
    if ($Root) { $dockerArguments += @("-u", "0") }
    $dockerArguments += @($N8nContainer)
    $dockerArguments += @($Arguments)
    return Invoke-BoundedDockerCommand $dockerArguments
}

function Get-BoundedContainerSingleLine {
    param(
        [Parameter(Mandatory)]$Result,
        [Parameter(Mandatory)][string]$Code
    )
    if ([int]$Result.exit_code -ne 0) { Stop-Bounded $Code }
    $lines = @(([string]$Result.stdout).Trim() -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    if ($lines.Count -ne 1) { Stop-Bounded $Code }
    return ([string]$lines[0]).Trim()
}

function Write-BoundedContainerCustodyReceipt {
    param(
        [Parameter(Mandatory)]$Context,
        [Parameter(Mandatory)]$Custody,
        [switch]$ReplaceExisting
    )
    Write-BoundedOperationReceipt ([string]$Context.operations_path) ([string]$Context.operation_path) "container-custody.json" $Custody -ReplaceExisting:$ReplaceExisting
}

function Invoke-BoundedExternalCommand {
    param(
        [Parameter(Mandatory)][string]$Verb,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Arguments,
        [string]$InputText = "",
        [string]$ContainerInputKey = "",
        [AllowNull()][object]$ContainerEvidenceContext = $null
    )
    if ($Verb -notin @("list:workflow", "export:workflow", "import:workflow")) { Stop-Bounded "n8n_command_not_allowed" }
    if ($Arguments -match "--all" -or $Arguments -match "export:workflow.*--all") { Stop-Bounded "unbounded_workflow_export" }
    if ([string]::IsNullOrWhiteSpace($N8nContainer)) {
        return Invoke-BoundedProcess -Command $N8nExecutable -Arguments (@($Verb) + @($Arguments)) -InputText $InputText
    }
    if ($N8nContainer -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$') { Stop-Bounded "n8n_container_invalid" }
    if ($Verb -ne "import:workflow") {
        return Invoke-BoundedContainerExec (@("n8n", $Verb) + @($Arguments))
    }

    $inputArguments = @($Arguments | Where-Object { ([string]$_).StartsWith("--input=", [StringComparison]::Ordinal) })
    if ($inputArguments.Count -ne 1) { Stop-Bounded "n8n_container_input_invalid" }
    $hostInputPath = ([string]$inputArguments[0]).Substring(8)
    if (-not (Test-Path -LiteralPath $hostInputPath -PathType Leaf)) { Stop-Bounded "n8n_container_input_missing" }
    if ($null -eq $ContainerEvidenceContext) { Stop-Bounded "container_custody_context_missing" }
    $containerKey = [string]$ContainerEvidenceContext.operation_id
    if ([string]::IsNullOrWhiteSpace($containerKey) -or $containerKey -notmatch '^[a-z0-9][a-z0-9._-]{2,96}$' -or
        (-not [string]::IsNullOrWhiteSpace($ContainerInputKey) -and $ContainerInputKey -cne $containerKey)) {
        Stop-Bounded "n8n_container_input_key_invalid"
    }
    $inputHash = Get-BoundedSha256File $hostInputPath
    $inputSize = [int64]([System.IO.File]::ReadAllBytes($hostInputPath).Length)
    if ($inputSize -le 0) { Stop-Bounded "n8n_container_input_empty" }

    $containerId = Get-BoundedContainerSingleLine (Invoke-BoundedDockerCommand @("inspect", "--format={{.Id}}", $N8nContainer)) "n8n_container_identity_unavailable"
    $imageId = Get-BoundedContainerSingleLine (Invoke-BoundedDockerCommand @("inspect", "--format={{.Image}}", $N8nContainer)) "n8n_container_identity_unavailable"
    if ($containerId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$' -or $imageId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$') { Stop-Bounded "n8n_container_identity_invalid" }

    $tmpStat = (Get-BoundedContainerSingleLine (Invoke-BoundedContainerExec @("stat", "-c", "%a:%u:%g:%F", "/tmp")) "n8n_container_tmp_unavailable").Split(":")
    if ($tmpStat.Count -ne 4 -or $tmpStat[0] -cne "1777" -or $tmpStat[1] -cne "0" -or $tmpStat[2] -cne "0" -or $tmpStat[3] -cne "directory") { Stop-Bounded "n8n_container_tmp_invalid" }

    $importUid = Get-BoundedContainerSingleLine (Invoke-BoundedContainerExec @("id", "-u")) "n8n_container_import_identity_unavailable"
    $importGid = Get-BoundedContainerSingleLine (Invoke-BoundedContainerExec @("id", "-g")) "n8n_container_import_identity_unavailable"
    if ($importUid -notmatch '^[0-9]{1,10}$' -or $importGid -notmatch '^[0-9]{1,10}$' -or [int64]$importUid -le 0 -or [int64]$importGid -le 0) { Stop-Bounded "container_import_root" }

    $nonce = [Guid]::NewGuid().ToString("N")
    $containerDirectory = "/tmp/.xb-member-gateway-{0}-{1}" -f $containerKey, $nonce
    $containerPath = $containerDirectory + "/prepared.workflow.json"
    $custody = [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.container-custody.v1"
        operation_id = [string]$ContainerEvidenceContext.operation_id
        operation_identity = [string]$ContainerEvidenceContext.operation_identity
        plan_digest = [string]$ContainerEvidenceContext.plan_digest
        prepared_workflow_digest = [string]$ContainerEvidenceContext.prepared_workflow_digest
        container_name = [string]$N8nContainer
        container_id = $containerId
        image_id = $imageId
        nonce = $nonce
        import_uid = $importUid
        import_gid = $importGid
        tmp_mode = $tmpStat[0]
        tmp_uid = $tmpStat[1]
        tmp_gid = $tmpStat[2]
        stage_directory = $containerDirectory
        stage_file = $containerPath
        stage_directory_mode = "700"
        stage_directory_uid = $importUid
        stage_directory_gid = $importGid
        stage_file_mode = "600"
        stage_file_uid = $importUid
        stage_file_gid = $importGid
        stage_file_size = $inputSize
        input_sha256 = $inputHash
        container_sha256 = $inputHash
        stage_verified = $false
        import_started = $false
        mutation_possible = $false
        cleanup_state = "pending"
        cleanup_verified = $false
        import_exit_code = $null
    })
    Write-BoundedContainerCustodyReceipt $ContainerEvidenceContext $custody
    $cleanupError = $false
    try {
        $mkdirResult = Invoke-BoundedContainerExec @("mkdir", "-m", "700", "--", $containerDirectory) -Root
        if ([int]$mkdirResult.exit_code -ne 0) { Stop-Bounded "n8n_container_stage_failed" }
        $copyResult = Invoke-BoundedDockerCommand @("cp", $hostInputPath, ($N8nContainer + ":" + $containerPath))
        if ([int]$copyResult.exit_code -ne 0) { Stop-Bounded "n8n_container_input_copy_failed" }
        $owner = "{0}:{1}" -f $importUid, $importGid
        foreach ($path in @($containerDirectory, $containerPath)) {
            $chownResult = Invoke-BoundedContainerExec @("chown", $owner, $path) -Root
            if ([int]$chownResult.exit_code -ne 0) { Stop-Bounded "n8n_container_stage_verification_failed" }
        }
        $chmodDirectory = Invoke-BoundedContainerExec @("chmod", "700", $containerDirectory) -Root
        $chmodFile = Invoke-BoundedContainerExec @("chmod", "600", $containerPath) -Root
        if ([int]$chmodDirectory.exit_code -ne 0 -or [int]$chmodFile.exit_code -ne 0) { Stop-Bounded "n8n_container_stage_verification_failed" }
        $directoryStat = (Get-BoundedContainerSingleLine (Invoke-BoundedContainerExec @("stat", "-c", "%a:%u:%g:%F", $containerDirectory) -Root) "n8n_container_stage_verification_failed").Split(":")
        $fileStat = (Get-BoundedContainerSingleLine (Invoke-BoundedContainerExec @("stat", "-c", "%a:%u:%g:%F:%s", $containerPath) -Root) "n8n_container_stage_verification_failed").Split(":")
        if ($directoryStat.Count -ne 4 -or $directoryStat[0] -cne "700" -or $directoryStat[1] -cne $importUid -or $directoryStat[2] -cne $importGid -or $directoryStat[3] -cne "directory" -or
            $fileStat.Count -ne 5 -or $fileStat[0] -cne "600" -or $fileStat[1] -cne $importUid -or $fileStat[2] -cne $importGid -or $fileStat[3] -cne "regular file" -or [int64]$fileStat[4] -ne $inputSize) {
            Stop-Bounded "n8n_container_stage_verification_failed"
        }
        $containerHashLine = Get-BoundedContainerSingleLine (Invoke-BoundedContainerExec @("sha256sum", $containerPath) -Root) "n8n_container_stage_verification_failed"
        $containerHash = (($containerHashLine -split "\s+")[0]).Trim().ToLowerInvariant()
        if ($containerHash -cne $inputHash) { Stop-Bounded "n8n_container_content_mismatch" }
        $custody.stage_verified = $true
        Write-BoundedContainerCustodyReceipt $ContainerEvidenceContext $custody -ReplaceExisting
        $custody.import_started = $true
        $custody.mutation_possible = $true
        Write-BoundedContainerCustodyReceipt $ContainerEvidenceContext $custody -ReplaceExisting
        $containerArguments = @("n8n", $Verb)
        foreach ($argument in @($Arguments)) {
            if (([string]$argument).StartsWith("--input=", [StringComparison]::Ordinal)) {
                $containerArguments += "--input=$containerPath"
            } else {
                $containerArguments += [string]$argument
            }
        }
        $result = Invoke-BoundedContainerExec $containerArguments
        $custody.import_exit_code = [int]$result.exit_code
        Write-BoundedContainerCustodyReceipt $ContainerEvidenceContext $custody -ReplaceExisting
        return $result
    } finally {
        $cleanupResult = Invoke-BoundedContainerExec @("rm", "-rf", "--", $containerDirectory) -Root
        if ([int]$cleanupResult.exit_code -ne 0) {
            $cleanupError = $true
        } else {
            $absenceResult = Invoke-BoundedContainerExec @("test", "!", "-e", $containerDirectory) -Root
            if ([int]$absenceResult.exit_code -ne 0) { $cleanupError = $true }
        }
        if ($cleanupError) {
            $custody.cleanup_state = "failed"
            $custody.cleanup_verified = $false
        } else {
            $custody.cleanup_state = "cleaned"
            $custody.cleanup_verified = $true
        }
        try {
            Write-BoundedContainerCustodyReceipt $ContainerEvidenceContext $custody -ReplaceExisting
        } catch {
            $cleanupError = $true
        }
        if ($cleanupError) {
            Stop-Bounded "n8n_container_input_cleanup_failed"
        }
    }
}

function Invoke-BoundedExternalJson {
    param(
        [Parameter(Mandatory)][string]$Verb,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Arguments,
        [string]$InputText = "",
        [switch]$AllowFloatingPoint
    )
    $result = Invoke-BoundedExternalCommand $Verb $Arguments $InputText
    if ([int]$result.exit_code -ne 0) { Stop-Bounded "n8n_command_failed" }
    if ($AllowFloatingPoint) {
        return ConvertFrom-BoundedWorkflowJsonText ([string]$result.stdout)
    }
    return ConvertFrom-BoundedJsonText ([string]$result.stdout)
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

function Get-BoundedCursorRequestUri {
    param([Parameter(Mandatory)]$Manifest)
    $uri = Get-BoundedHttpsUri ([string]$Manifest.endpoints.source_cursor) "cursor_endpoint_invalid"
    $existingQuery = $uri.Query.TrimStart("?")
    foreach ($part in ($existingQuery -split "&")) {
        if ([string]::IsNullOrWhiteSpace($part)) { continue }
        $key = ($part -split "=", 2)[0].Replace("+", " ")
        try { $decodedKey = [System.Uri]::UnescapeDataString($key) } catch { Stop-Bounded "cursor_endpoint_invalid" }
        if ($decodedKey -in @("form_alias", "mapping_version")) { Stop-Bounded "cursor_query_collision" }
    }
    $parameters = New-Object System.Collections.Generic.List[string]
    $parameters.Add("form_alias=" + [System.Uri]::EscapeDataString([string]$Manifest.form_alias))
    $parameters.Add("mapping_version=" + [System.Uri]::EscapeDataString([string]$Manifest.mapping_version))
    $builder = [System.UriBuilder]::new($uri)
    $queryParts = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($existingQuery)) { $queryParts.Add($existingQuery) }
    $queryParts.Add(($parameters -join "&"))
    $builder.Query = ($queryParts -join "&")
    return $builder.Uri.AbsoluteUri
}

function Get-BoundedLiveWorkflowMetadata {
    param([Parameter(Mandatory)]$Manifest)
    $listResult = Invoke-BoundedExternalCommand "list:workflow" @()
    if ([int]$listResult.exit_code -ne 0) { Stop-Bounded "n8n_command_failed" }
    $rows = ConvertFrom-BoundedWorkflowListText ([string]$listResult.stdout)
    $metadata = New-Object System.Collections.Generic.List[object]
    foreach ($row in @($rows)) {
        $rowId = [string]$row.id
        $rowName = [string]$row.name
        $candidate = $rowId.Equals([string]$Manifest.workflow.id, [StringComparison]::OrdinalIgnoreCase) -or $rowName.Equals([string]$Manifest.workflow.name, [StringComparison]::OrdinalIgnoreCase)
        if (-not $candidate) { continue }
        if (@($row.PSObject.Properties.Name) -contains "projectId" -and @($row.PSObject.Properties.Name) -contains "projectName") {
            $metadata.Add((Get-BoundedMetadataEntry $row))
            continue
        }
        $exportResult = Invoke-BoundedExternalCommand "export:workflow" @("--id", $rowId, "--pretty")
        if ([int]$exportResult.exit_code -ne 0) { Stop-Bounded "n8n_command_failed" }
        $workflow = ConvertFrom-BoundedWorkflowExportText ([string]$exportResult.stdout)
        if ([string]$workflow.id -cne $rowId -or [string]$workflow.name -cne $rowName) { Stop-Bounded "metadata_workflow_identity_mismatch" }
        $project = Get-BoundedWorkflowProjectIdentity $workflow
        $archivedValue = Get-BoundedOptionalProperty $workflow "isArchived"
        if ($null -eq $archivedValue) { $archivedValue = Get-BoundedOptionalProperty $workflow "archived" }
        $metadata.Add([pscustomobject]([ordered]@{
            projectId = [string]$project.id
            projectName = [string]$project.name
            id = $rowId
            name = $rowName
            isArchived = [bool]$archivedValue
        }))
    }
    return @($metadata.ToArray())
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
        $response = Invoke-WebRequest -UseBasicParsing -Uri (Get-BoundedCursorRequestUri $Manifest) -Headers $headers -Method Get
        $cursor = ConvertFrom-BoundedJsonText ([string]$response.Content)
        Assert-BoundedCursor $cursor $Manifest
        $metadata = Get-BoundedLiveWorkflowMetadata $Manifest
        $selection = Select-BoundedWorkflowTarget $metadata $Manifest
        $workflow = $null
        if ($selection.state -eq "existing") {
            $exportResult = Invoke-BoundedExternalCommand "export:workflow" @("--id", [string]$selection.target.workflow_id, "--pretty")
            if ([int]$exportResult.exit_code -ne 0) { Stop-Bounded "n8n_command_failed" }
            $workflow = ConvertFrom-BoundedWorkflowExportText ([string]$exportResult.stdout)
            if ([string]$workflow.id -cne [string]$selection.target.workflow_id -or [string]$workflow.name -cne [string]$selection.target.workflow_name) {
                Stop-Bounded "metadata_workflow_identity_mismatch"
            }
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
        if ($script:BoundedFixture -and [string]$script:BoundedFixture.custody_mode -in @("acl_failure", "manifest_acl_failure")) { Stop-Bounded "private_acl_verification_failed" }
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

function Assert-BoundedProtectedAcl {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ($script:BoundedTestOnly) {
        if ($script:BoundedFixture -and [string]$script:BoundedFixture.custody_mode -in @("acl_failure", "manifest_acl_failure")) { Stop-Bounded "private_acl_verification_failed" }
        return
    }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { Stop-Bounded "private_acl_unavailable" }
    try {
        $acl = Get-Acl -LiteralPath $Path
        if (-not $acl.AreAccessRulesProtected) { Stop-Bounded "private_acl_verification_failed" }
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $allowed = @($identity, "S-1-5-18", "S-1-5-32-544")
        $seen = New-Object System.Collections.Generic.HashSet[string]
        foreach ($rule in @($acl.Access)) {
            if ($rule.AccessControlType -ne "Allow") { Stop-Bounded "private_acl_verification_failed" }
            $sid = [string]$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            if ($sid -notin $allowed) { Stop-Bounded "private_acl_verification_failed" }
            [void]$seen.Add($sid)
        }
        foreach ($sid in $allowed) {
            if (-not $seen.Contains($sid)) { Stop-Bounded "private_acl_verification_failed" }
        }
    } catch {
        if ($_.Exception.Message -match "^private_acl_") { throw }
        Stop-Bounded "private_acl_verification_failed"
    }
}

function Assert-BoundedCustody {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$OperationsPath,
        [Parameter(Mandatory)][string]$OperationPath
    )
    Assert-BoundedPrivateDestination $Root $OperationsPath
    foreach ($path in @($OperationsPath, $OperationPath)) {
        if (Test-Path -LiteralPath $path) {
            Assert-BoundedNoUnsafePathComponents $path $Root
            Assert-BoundedProtectedAcl $path
            foreach ($entry in @(Get-ChildItem -LiteralPath $path -Force)) {
                if (Test-BoundedUnsafeLink $entry) { Stop-Bounded "unsafe_link" }
                if (-not $entry.PSIsContainer) { Assert-BoundedProtectedAcl $entry.FullName }
            }
        }
    }
}

function Assert-BoundedPrivateDestination {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$OperationsPath
    )
    if (-not (Test-BoundedStrictChild $OperationsPath $Root)) { Stop-Bounded "private_path_escape" }
    $resolvedOperations = Resolve-BoundedFullPath $OperationsPath
    $canonicalOperations = Resolve-BoundedFullPath (Join-Path $Root ".n8n-local/member-gateway-bounded-import/operations")
    if (-not $resolvedOperations.Equals($canonicalOperations, (Get-BoundedComparison))) { Stop-Bounded "private_path_not_canonical" }
    Assert-BoundedNoUnsafePathComponents $resolvedOperations $Root
    if (-not $script:BoundedTestOnly) {
        $relative = $resolvedOperations.Substring((Resolve-BoundedFullPath $Root).Length).TrimStart([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
        $ignored = & git -C $Root check-ignore --quiet --no-index -- $relative 2>$null
        if ($LASTEXITCODE -ne 0) { Stop-Bounded "private_path_not_ignored" }
        $tracked = @(& git -C $Root ls-files -- $relative)
        if ($tracked.Count -gt 0) { Stop-Bounded "private_path_tracked" }
    }
}

function Assert-BoundedManifestCustody {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$ManifestPath
    )
    $privateRoot = Resolve-BoundedFullPath (Join-Path $Root ".n8n-local/member-gateway-bounded-import")
    $resolvedManifest = Resolve-BoundedFullPath $ManifestPath
    if (-not (Test-BoundedStrictChild $resolvedManifest $privateRoot)) { Stop-Bounded "binding_manifest_path_not_private" }
    Assert-BoundedNoUnsafePathComponents $resolvedManifest $privateRoot
    if (-not (Test-Path -LiteralPath $resolvedManifest -PathType Leaf)) { Stop-Bounded "binding_manifest_missing" }
    $manifestItem = Get-Item -LiteralPath $resolvedManifest -Force -ErrorAction Stop
    if (Test-BoundedUnsafeLink $manifestItem) { Stop-Bounded "unsafe_link" }
    if (-not $script:BoundedTestOnly) {
        $relative = $resolvedManifest.Substring((Resolve-BoundedFullPath $Root).Length).TrimStart([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
        $ignored = & git -C $Root check-ignore --quiet --no-index -- $relative 2>$null
        if ($LASTEXITCODE -ne 0) { Stop-Bounded "binding_manifest_not_ignored" }
        $tracked = @(& git -C $Root ls-files -- $relative)
        if ($tracked.Count -gt 0) { Stop-Bounded "binding_manifest_tracked" }
    }
    Assert-BoundedProtectedAcl (Split-Path -Parent $resolvedManifest)
    Assert-BoundedProtectedAcl $resolvedManifest
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

function Assert-BoundedContainerCustody {
    param(
        [Parameter(Mandatory)]$Custody,
        [Parameter(Mandatory)]$Plan
    )
    Assert-BoundedExactProperties $Custody @(
        "schema_version", "operation_id", "operation_identity", "plan_digest", "prepared_workflow_digest",
        "container_name", "container_id", "image_id", "nonce", "import_uid", "import_gid",
        "tmp_mode", "tmp_uid", "tmp_gid", "stage_directory", "stage_file",
        "stage_directory_mode", "stage_directory_uid", "stage_directory_gid",
        "stage_file_mode", "stage_file_uid", "stage_file_gid", "stage_file_size",
        "input_sha256", "container_sha256", "stage_verified", "import_started", "mutation_possible",
        "cleanup_state", "cleanup_verified", "import_exit_code"
    ) "container_custody_shape_invalid"
    Assert-BoundedBoolean $Custody.stage_verified "container_custody_state_invalid"
    Assert-BoundedBoolean $Custody.import_started "container_custody_state_invalid"
    Assert-BoundedBoolean $Custody.mutation_possible "container_custody_state_invalid"
    Assert-BoundedBoolean $Custody.cleanup_verified "container_custody_state_invalid"
    if ([string]$Custody.schema_version -cne "xb.member.gateway.bounded_import.container-custody.v1" -or
        [string]$Custody.operation_id -cne [string]$Plan.operation_id -or
        [string]$Custody.operation_identity -cne [string]$Plan.operation_identity -or
        [string]$Custody.prepared_workflow_digest -cne [string]$Plan.identity_seed.prepared_workflow_digest) {
        Stop-Bounded "container_custody_identity_invalid"
    }
    if ([string]$Custody.container_name -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$' -or
        [string]$Custody.container_id -notmatch '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$' -or
        [string]$Custody.image_id -notmatch '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$' -or
        [string]$Custody.nonce -notmatch '^[0-9a-f]{32}$') {
        Stop-Bounded "container_custody_identity_invalid"
    }
    foreach ($uid in @([string]$Custody.import_uid, [string]$Custody.import_gid, [string]$Custody.tmp_uid, [string]$Custody.tmp_gid, [string]$Custody.stage_directory_uid, [string]$Custody.stage_directory_gid, [string]$Custody.stage_file_uid, [string]$Custody.stage_file_gid)) {
        if ($uid -notmatch '^[0-9]{1,10}$') { Stop-Bounded "container_custody_owner_invalid" }
    }
    if ([int64]$Custody.import_uid -le 0 -or [int64]$Custody.import_gid -le 0) { Stop-Bounded "container_import_root" }
    if ([string]$Custody.tmp_mode -cne "1777" -or [string]$Custody.stage_directory_mode -cne "700" -or [string]$Custody.stage_file_mode -cne "600") { Stop-Bounded "container_custody_mode_invalid" }
    if ([string]$Custody.tmp_uid -cne "0" -or [string]$Custody.tmp_gid -cne "0" -or
        [string]$Custody.stage_directory_uid -cne [string]$Custody.import_uid -or
        [string]$Custody.stage_directory_gid -cne [string]$Custody.import_gid -or
        [string]$Custody.stage_file_uid -cne [string]$Custody.import_uid -or
        [string]$Custody.stage_file_gid -cne [string]$Custody.import_gid) {
        Stop-Bounded "container_custody_owner_invalid"
    }
    $operationPattern = [regex]::Escape([string]$Plan.operation_id)
    if ([string]$Custody.stage_directory -notmatch ("^/tmp/\.xb-member-gateway-" + $operationPattern + "-[0-9a-f]{32}$") -or
        [string]$Custody.stage_file -cne ([string]$Custody.stage_directory + "/prepared.workflow.json")) {
        Stop-Bounded "container_custody_path_invalid"
    }
    Assert-BoundedDigest ([string]$Custody.input_sha256) "container_custody_digest_invalid"
    Assert-BoundedDigest ([string]$Custody.container_sha256) "container_custody_digest_invalid"
    if ([string]$Custody.input_sha256 -cne [string]$Custody.container_sha256 -or [int64]$Custody.stage_file_size -le 0) { Stop-Bounded "container_custody_content_invalid" }
    if ([string]$Custody.cleanup_state -notin @("pending", "cleaned", "failed")) { Stop-Bounded "container_custody_state_invalid" }
    if ([string]$Custody.cleanup_state -eq "cleaned" -and -not $Custody.cleanup_verified) { Stop-Bounded "container_cleanup_unverified" }
    if ([string]$Custody.cleanup_state -eq "failed" -and $Custody.cleanup_verified) { Stop-Bounded "container_cleanup_state_invalid" }
    if ($Custody.import_started -and -not $Custody.mutation_possible) { Stop-Bounded "container_custody_state_invalid" }
    if ($null -ne $Custody.import_exit_code -and ([int64]$Custody.import_exit_code -lt -1 -or [int64]$Custody.import_exit_code -gt 255)) { Stop-Bounded "container_custody_state_invalid" }
}

function Assert-BoundedContainerCleanupVerified {
    param([AllowNull()][object]$Custody)
    if ($null -eq $Custody) { return }
    if ([string]$Custody.cleanup_state -cne "cleaned" -or -not $Custody.cleanup_verified) {
        Stop-Bounded "container_cleanup_not_verified"
    }
}

function Get-BoundedOperationState {
    param([Parameter(Mandatory)][string]$OperationPath)
    $files = @(Get-BoundedOperationFiles $OperationPath)
    $base = @("binding.json", "cursor-state.json", "mutation-intent.json", "plan.json", "prepared.workflow.json")
    foreach ($name in $base) { if ($name -notin $files) { Stop-Bounded "operation_incomplete" } }
    $preimage = @($files | Where-Object { $_ -in @("preimage.workflow.json", "absence-evidence.json") })
    if ($preimage.Count -ne 1) { Stop-Bounded "operation_preimage_shape_invalid" }
    $optional = @($files | Where-Object { $_ -in @("container-custody.json", "dispatch-ownership.json", "dispatch-receipt.json", "completion-receipt.json") })
    foreach ($file in $files) {
        if ($file -notin ($base + $preimage + @("container-custody.json", "dispatch-ownership.json", "dispatch-receipt.json", "completion-receipt.json"))) { Stop-Bounded "operation_extra_material" }
    }
    if ("completion-receipt.json" -in $optional -and "dispatch-receipt.json" -notin $optional) { Stop-Bounded "completion_without_dispatch" }
    $plan = Read-BoundedJsonFile (Join-Path $OperationPath "plan.json") -RequireCanonical
    $planDigest = Get-BoundedPlanDigest $OperationPath
    Assert-BoundedExactProperties $plan @("schema_version", "operation_id", "immutable", "operation_identity", "identity_seed", "expected_projection_digest", "expected_files") "plan_shape_invalid"
    Assert-BoundedBoolean $plan.immutable "plan_identity_invalid"
    if ([string]$plan.schema_version -cne "xb.member.gateway.bounded_import.plan.v2" -or [string]$plan.operation_id -ne [string]$OperationId -or -not $plan.immutable) { Stop-Bounded "plan_identity_invalid" }
    Assert-BoundedDigest ([string]$plan.operation_identity) "operation_identity_invalid"
    Assert-BoundedDigest ([string]$plan.expected_projection_digest) "plan_projection_digest_invalid"
    Assert-BoundedExactProperties $plan.identity_seed @(
        "schema_version", "repository", "canonical_workflow", "project_identity", "workflow_identity",
        "prepared_workflow_digest", "cursor_digest", "cursor_state_version", "watermark_digest",
        "watermark_value", "preimage_state", "preimage_digest", "preimage_selection_digest",
        "binding_manifest_digest", "credential_binding_digest"
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
        [string]$plan.identity_seed.binding_manifest_digest,
        [string]$plan.identity_seed.credential_binding_digest
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
    $binding = Read-BoundedJsonFile (Join-Path $OperationPath "binding.json") -RequireCanonical
    $cursorState = Read-BoundedJsonFile (Join-Path $OperationPath "cursor-state.json") -RequireCanonical
    $intent = Read-BoundedJsonFile (Join-Path $OperationPath "mutation-intent.json") -RequireCanonical
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
    if ([string]$plan.identity_seed.project_identity.id -cne [string]$binding.manifest.project.id -or
        [string]$plan.identity_seed.project_identity.name -cne [string]$binding.manifest.project.name -or
        [string]$plan.identity_seed.workflow_identity.id -cne [string]$binding.manifest.workflow.id -or
        [string]$plan.identity_seed.workflow_identity.name -cne [string]$binding.manifest.workflow.name) {
        Stop-Bounded "identity_seed_binding_mismatch"
    }
    Assert-BoundedDigest ([string]$binding.manifest_digest) "binding_manifest_digest_invalid"
    Assert-BoundedDigest ([string]$binding.plan_digest) "plan_digest_invalid"
    Assert-BoundedDigest ([string]$cursorState.cursor_digest) "cursor_digest_invalid"
    Assert-BoundedDigest ([string]$intent.prepared_workflow_digest) "prepared_digest_invalid"
    Assert-BoundedDigest ([string]$intent.expected_projection_digest) "intent_projection_digest_invalid"
    Assert-BoundedBoolean $intent.generic_hooks_allowed "mutation_intent_scope_invalid"
    Assert-BoundedBoolean $intent.source_execution_allowed "mutation_intent_scope_invalid"
    if ($intent.generic_hooks_allowed -or $intent.source_execution_allowed) { Stop-Bounded "mutation_intent_scope_invalid" }
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
    if ([string]$plan.identity_seed.credential_binding_digest -cne (Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $binding.manifest.credential_roles))) { Stop-Bounded "credential_binding_digest_mismatch" }
    $cursor = $cursorState.cursor
    Assert-BoundedCursor $cursor $binding.manifest
    if ([string]$cursorState.cursor_digest -cne [string]$plan.identity_seed.cursor_digest -or (Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $cursor)) -cne [string]$cursorState.cursor_digest) { Stop-Bounded "cursor_digest_mismatch" }
    Assert-BoundedFileDigest (Join-Path $OperationPath "prepared.workflow.json") ([string]$plan.identity_seed.prepared_workflow_digest) "prepared_digest_mismatch"
    if ($preimage[0] -eq "preimage.workflow.json") {
        Assert-BoundedFileDigest (Join-Path $OperationPath $preimage[0]) ([string]$plan.identity_seed.preimage_digest) "preimage_digest_mismatch"
    } else {
        $absence = Read-BoundedJsonFile (Join-Path $OperationPath "absence-evidence.json") -RequireCanonical
        Assert-BoundedAbsenceEvidence $absence ([pscustomobject]$binding.manifest)
        $absenceText = (Get-BoundedCanonicalJsonFromObject $absence) + $script:BoundedLf
        if ((Get-BoundedSha256Text $absenceText) -cne [string]$plan.identity_seed.preimage_digest) { Stop-Bounded "absence_digest_mismatch" }
    }
    if ([string]$intent.expected_projection_digest -ne [string]$plan.expected_projection_digest) { Stop-Bounded "mutation_intent_mismatch" }
    $containerCustody = $null
    if ("container-custody.json" -in $optional) {
        $containerCustody = Read-BoundedJsonFile (Join-Path $OperationPath "container-custody.json") -RequireCanonical
        Assert-BoundedContainerCustody $containerCustody $plan
        if ([string]$containerCustody.plan_digest -cne [string]$planDigest) { Stop-Bounded "container_custody_identity_invalid" }
    }
    $ownership = $null
    $dispatch = $null
    $completion = $null
    if ("dispatch-ownership.json" -in $optional) {
        $ownership = Read-BoundedJsonFile (Join-Path $OperationPath "dispatch-ownership.json") -RequireCanonical
        Assert-BoundedExactProperties $ownership @("schema_version", "operation_id", "operation_identity", "plan_digest", "mutation_intent_digest", "ownership_id") "dispatch_ownership_shape_invalid"
        if ([string]$ownership.schema_version -cne "xb.member.gateway.bounded_import.dispatch-ownership.v1" -or [string]$ownership.operation_id -ne [string]$OperationId) { Stop-Bounded "dispatch_ownership_identity_invalid" }
        if ([string]$ownership.ownership_id -notmatch '^[0-9a-f]{32}$') { Stop-Bounded "dispatch_ownership_identity_invalid" }
        Assert-BoundedDigest ([string]$ownership.plan_digest) "dispatch_ownership_plan_digest_invalid"
        Assert-BoundedDigest ([string]$ownership.mutation_intent_digest) "dispatch_ownership_intent_digest_invalid"
        if ([string]$ownership.plan_digest -ne $planDigest -or [string]$ownership.operation_identity -ne [string]$plan.operation_identity) { Stop-Bounded "dispatch_ownership_chain_mismatch" }
        if ([string]$ownership.mutation_intent_digest -ne (Get-BoundedSha256File (Join-Path $OperationPath "mutation-intent.json"))) { Stop-Bounded "dispatch_ownership_chain_mismatch" }
    }
    if ("dispatch-receipt.json" -in $optional) {
        $dispatch = Read-BoundedJsonFile (Join-Path $OperationPath "dispatch-receipt.json") -RequireCanonical
        Assert-BoundedExactProperties $dispatch @("schema_version", "operation_id", "operation_identity", "plan_digest", "mutation_intent_digest", "dispatch_state", "outcome", "replay_allowed") "dispatch_receipt_shape_invalid"
        Assert-BoundedBoolean $dispatch.replay_allowed "dispatch_receipt_state_invalid"
        $dispatchStateValid = (
            ([string]$dispatch.dispatch_state -ceq "dispatching" -and [string]$dispatch.outcome -ceq "pending" -and -not $dispatch.replay_allowed) -or
            ([string]$dispatch.dispatch_state -ceq "dispatched" -and [string]$dispatch.outcome -in @("completed", "ambiguous") -and -not $dispatch.replay_allowed) -or
            ([string]$dispatch.dispatch_state -ceq "not_dispatched" -and [string]$dispatch.outcome -ceq "pre_dispatch_failure" -and $dispatch.replay_allowed)
        )
        if (-not $dispatchStateValid) { Stop-Bounded "dispatch_receipt_state_invalid" }
        Assert-BoundedDigest ([string]$dispatch.mutation_intent_digest) "dispatch_intent_digest_invalid"
        if ([string]$dispatch.schema_version -cne "xb.member.gateway.bounded_import.dispatch.v2" -or [string]$dispatch.operation_id -ne [string]$OperationId) { Stop-Bounded "dispatch_receipt_identity_invalid" }
        if ([string]$dispatch.mutation_intent_digest -cne (Get-BoundedSha256File (Join-Path $OperationPath "mutation-intent.json"))) { Stop-Bounded "dispatch_chain_mismatch" }
        if ([string]$dispatch.plan_digest -ne $planDigest -or [string]$dispatch.operation_identity -ne [string]$plan.operation_identity) { Stop-Bounded "dispatch_chain_mismatch" }
    }
    if ($null -ne $ownership -and $null -eq $dispatch) { Stop-Bounded "dispatch_ownership_without_receipt" }
    if ("completion-receipt.json" -in $optional) {
        $completion = Read-BoundedJsonFile (Join-Path $OperationPath "completion-receipt.json") -RequireCanonical
        Assert-BoundedExactProperties $completion @(
            "schema_version", "operation_id", "operation_identity", "plan_digest", "dispatch_digest",
            "expected_projection_digest", "target_projection_digest", "original_preimage_state", "ownership", "complete"
        ) "completion_receipt_shape_invalid"
        Assert-BoundedBoolean $completion.complete "completion_receipt_state_invalid"
        if (-not $completion.complete -or [string]$completion.original_preimage_state -notin @("existing", "absent") -or [string]$completion.ownership -notin @("created", "updated_or_noop")) { Stop-Bounded "completion_receipt_state_invalid" }
        Assert-BoundedDigest ([string]$completion.dispatch_digest) "completion_dispatch_digest_invalid"
        Assert-BoundedDigest ([string]$completion.target_projection_digest) "completion_target_digest_invalid"
        if ([string]$completion.dispatch_digest -ne (Get-BoundedSha256File (Join-Path $OperationPath "dispatch-receipt.json"))) { Stop-Bounded "completion_chain_mismatch" }
        if ([string]$completion.expected_projection_digest -ne [string]$plan.expected_projection_digest) { Stop-Bounded "completion_identity_mismatch" }
        if ([string]$completion.plan_digest -ne $planDigest -or [string]$completion.operation_identity -ne [string]$plan.operation_identity) { Stop-Bounded "completion_chain_mismatch" }
        if ($null -eq $dispatch -or [string]$dispatch.dispatch_state -cne "dispatched" -or [string]$dispatch.outcome -cne "completed") { Stop-Bounded "completion_without_completed_dispatch" }
        if ([string]$completion.schema_version -cne "xb.member.gateway.bounded_import.completion.v2" -or [string]$completion.operation_id -ne [string]$OperationId -or [string]$completion.target_projection_digest -cne [string]$plan.expected_projection_digest) { Stop-Bounded "completion_receipt_identity_invalid" }
        if ([string]$completion.original_preimage_state -cne [string]$plan.identity_seed.preimage_state) { Stop-Bounded "completion_preimage_state_mismatch" }
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
        ownership = $ownership
        container_custody = $containerCustody
        dispatch = $dispatch
        completion = $completion
    })
}

function Assert-BoundedReviewedWorkflowBytes {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)]$Repository
    )
    $path = Get-BoundedWorkflowFile $Root
    $actual = & git -C $Root hash-object -- n8n-workflows/member_forms_gateway_ingest.workflow.json 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$actual)) { Stop-Bounded "repository_workflow_blob_unavailable" }
    $actualHash = ([string]$actual).Trim()
    if ($actualHash -notmatch '^[0-9a-f]{40}$' -or $actualHash -cne [string]$Repository.workflow_blob) { Stop-Bounded "repository_workflow_blob_mismatch" }
    return $path
}

function New-BoundedIdentitySeed {
    param(
        [Parameter(Mandatory)]$Repository,
        [Parameter(Mandatory)]$Manifest,
        [Parameter(Mandatory)]$Cursor,
        [Parameter(Mandatory)][string]$PreparedWorkflowDigest,
        [Parameter(Mandatory)][string]$PreimageState,
        [Parameter(Mandatory)][string]$PreimageDigest,
        [Parameter(Mandatory)][string]$PreimageSelectionDigest,
        [Parameter(Mandatory)][string]$BindingManifestDigest
    )
    return [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.identity.v2"
        repository = $Repository
        canonical_workflow = [pscustomobject]([ordered]@{
            path = "n8n-workflows/member_forms_gateway_ingest.workflow.json"
            git_blob = [string]$Repository.workflow_blob
        })
        project_identity = $Manifest.project
        workflow_identity = $Manifest.workflow
        prepared_workflow_digest = $PreparedWorkflowDigest
        cursor_digest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $Cursor)
        cursor_state_version = [int64]$Cursor.state_version
        watermark_digest = Get-BoundedWatermarkDigest ([string]$Manifest.source_system) ([string]$Manifest.form_alias) ([string]$Manifest.mapping_version) ([string]$Cursor.watermark)
        watermark_value = [string]$Cursor.watermark
        preimage_state = $PreimageState
        preimage_digest = $PreimageDigest
        preimage_selection_digest = $PreimageSelectionDigest
        binding_manifest_digest = $BindingManifestDigest
        credential_binding_digest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $Manifest.credential_roles)
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
    Assert-BoundedReviewedWorkflowBytes $Root $repo | Out-Null
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
    $projection = Get-BoundedWorkflowProjection $prepared $Manifest
    $projectionDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $projection -AllowFloatingPoint)
    $selectionDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $Evidence.selection)
    $identitySeed = New-BoundedIdentitySeed $repo $Manifest $Evidence.cursor $preparedDigest $preimageState $preimageDigest $selectionDigest $manifestDigest
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
        [Parameter(Mandatory)]$Value,
        [switch]$ReplaceExisting
    )
    $stage = New-BoundedStagingRoot $OperationsPath
    try {
        $text = (Get-BoundedCanonicalJsonFromObject $Value) + $script:BoundedLf
        Write-BoundedCreateNewText $stage $stage $Name $text
        $target = Join-Path $OperationPath $Name
        $source = Join-Path $stage $Name
        if (Test-Path -LiteralPath $target) {
            if (-not $ReplaceExisting) { Stop-Bounded "evidence_already_exists" }
            $backup = Join-Path $OperationPath ($Name + "." + [Guid]::NewGuid().ToString("N") + ".bounded-backup")
            [System.IO.File]::Replace($source, $target, $backup, $true)
            if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Force -ErrorAction Stop }
        } else {
            if ($ReplaceExisting) { Stop-Bounded "evidence_missing" }
            [System.IO.File]::Move($source, $target)
        }
        Set-BoundedProtectedAcl $target
    } finally {
        if (Test-Path -LiteralPath $stage) {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function New-BoundedDispatchOwnership {
    param([Parameter(Mandatory)]$State)
    return [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.dispatch-ownership.v1"
        operation_id = [string]$State.plan.operation_id
        operation_identity = [string]$State.plan.operation_identity
        plan_digest = [string]$State.plan_digest
        mutation_intent_digest = Get-BoundedSha256File (Join-Path $State.operation_path "mutation-intent.json")
        ownership_id = [Guid]::NewGuid().ToString("N")
    })
}

function Try-BoundedAcquireDispatchOwnership {
    param(
        [Parameter(Mandatory)][string]$OperationPath,
        [Parameter(Mandatory)]$Ownership
    )
    $target = Join-Path $OperationPath "dispatch-ownership.json"
    $text = (Get-BoundedCanonicalJsonFromObject $Ownership) + $script:BoundedLf
    $bytes = (Get-BoundedUtf8NoBom).GetBytes($text)
    try {
        $stream = [System.IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
            $script:BoundedPrivateBytesWritten = $true
        } finally {
            $stream.Dispose()
        }
        Set-BoundedProtectedAcl $target
        return $true
    } catch [System.IO.IOException] {
        if (Test-Path -LiteralPath $target -PathType Leaf) { return $false }
        Stop-Bounded "dispatch_ownership_acquire_failed"
    } catch {
        if ($_.Exception.Message -match "^private_acl_") { throw }
        Stop-Bounded "dispatch_ownership_acquire_failed"
    }
}

function Release-BoundedDispatchOwnership {
    param(
        [Parameter(Mandatory)][string]$OperationPath,
        [Parameter(Mandatory)]$Ownership
    )
    $target = Join-Path $OperationPath "dispatch-ownership.json"
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { Stop-Bounded "dispatch_ownership_missing" }
    $current = Read-BoundedJsonFile $target -RequireCanonical
    if ([string]$current.ownership_id -cne [string]$Ownership.ownership_id) { Stop-Bounded "dispatch_ownership_mismatch" }
    Remove-Item -LiteralPath $target -Force -ErrorAction Stop
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
    $selectionDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $Evidence.selection)
    if (-not (Compare-BoundedText $selectionDigest ([string]$State.plan.identity_seed.preimage_selection_digest))) { Stop-Bounded "preimage_selection_digest_mismatch" }
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
    Assert-BoundedReviewedWorkflowBytes $script:BoundedRepoRoot $currentRepository | Out-Null
    $currentRepositoryDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $currentRepository)
    $plannedRepositoryDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $State.plan.identity_seed.repository)
    if (-not (Compare-BoundedText $currentRepositoryDigest $plannedRepositoryDigest)) { Stop-Bounded "repository_identity_mismatch" }
    if ([string]$currentRepository.workflow_blob -cne [string]$State.plan.identity_seed.canonical_workflow.git_blob) { Stop-Bounded "repository_workflow_blob_mismatch" }
    $manifestDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $Manifest)
    if (-not (Compare-BoundedText $manifestDigest ([string]$State.plan.identity_seed.binding_manifest_digest))) { Stop-Bounded "binding_digest_mismatch" }
    Assert-BoundedCursor $Evidence.cursor $Manifest
    $cursorDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $Evidence.cursor)
    if (-not (Compare-BoundedText $cursorDigest ([string]$State.plan.identity_seed.cursor_digest))) { Stop-Bounded "cursor_digest_mismatch" }
    if (-not (Compare-BoundedText ([string]$Evidence.cursor.state_version) ([string]$State.plan.identity_seed.cursor_state_version))) { Stop-Bounded "cursor_state_version_mismatch" }
    if (-not (Compare-BoundedText ([string]$Evidence.cursor.watermark) ([string]$State.plan.identity_seed.watermark_value))) { Stop-Bounded "watermark_value_mismatch" }
    $watermarkDigest = Get-BoundedWatermarkDigest ([string]$Manifest.source_system) ([string]$Manifest.form_alias) ([string]$Manifest.mapping_version) ([string]$Evidence.cursor.watermark)
    if (-not (Compare-BoundedText $watermarkDigest ([string]$State.plan.identity_seed.watermark_digest))) { Stop-Bounded "watermark_digest_mismatch" }
    $prepared = New-BoundedPreparedWorkflow $Manifest $script:BoundedRepoRoot
    $preparedText = Get-BoundedWorkflowJsonText $prepared
    $preparedDigest = Get-BoundedSha256Text $preparedText
    if (-not (Compare-BoundedText $preparedDigest ([string]$State.plan.identity_seed.prepared_workflow_digest))) { Stop-Bounded "prepared_source_digest_mismatch" }
    Assert-BoundedFileDigest (Join-Path $State.operation_path "prepared.workflow.json") ([string]$State.plan.identity_seed.prepared_workflow_digest) "prepared_digest_mismatch"
    $projection = Get-BoundedWorkflowProjection $prepared $Manifest
    $projectionDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $projection -AllowFloatingPoint)
    if (-not (Compare-BoundedText $projectionDigest ([string]$State.plan.expected_projection_digest))) { Stop-Bounded "prepared_projection_mismatch" }
    $freshSeed = New-BoundedIdentitySeed $currentRepository $Manifest $Evidence.cursor $preparedDigest ([string]$State.plan.identity_seed.preimage_state) ([string]$State.plan.identity_seed.preimage_digest) ([string]$State.plan.identity_seed.preimage_selection_digest) $manifestDigest
    $freshIdentity = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $freshSeed)
    if (-not (Compare-BoundedText $freshIdentity ([string]$State.plan.operation_identity))) { Stop-Bounded "operation_identity_mismatch" }
}

function Test-BoundedExpectedTarget {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)]$Evidence
    )
    if ($Evidence.selection.state -ne "existing") { return $false }
    $actualProjection = Get-BoundedWorkflowProjection $Evidence.workflow $State.binding.manifest
    $actualDigest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject $actualProjection -AllowFloatingPoint)
    return Compare-BoundedText $actualDigest ([string]$State.plan.expected_projection_digest)
}

function Write-BoundedFixtureMarker {
    param([Parameter(Mandatory)][string]$Directory)
    if ([string]::IsNullOrWhiteSpace($Directory)) { Stop-Bounded "fixture_marker_path_invalid" }
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $path = Join-Path $Directory (([Guid]::NewGuid()).ToString("N") + ".marker")
    $stream = [System.IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
    return $path
}

function Wait-BoundedFixtureAdmissionBarrier {
    param([AllowNull()]$Fixture)
    if ($null -eq $Fixture) { return }
    if (-not $script:BoundedTestOnly -or [string]$Fixture.dispatch_mode -ne "concurrent_success") { return }
    $barrier = [string]$Fixture.admission_barrier_directory
    Write-BoundedFixtureMarker $barrier | Out-Null
    for ($attempt = 0; $attempt -lt 240; $attempt++) {
        if (@(Get-ChildItem -LiteralPath $barrier -Filter "*.marker" -File -ErrorAction Stop).Count -ge 2) { return }
        Start-Sleep -Milliseconds 25
    }
    Stop-Bounded "fixture_admission_barrier_timeout"
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
    if ($mode -eq "interrupt_after_start") {
        Stop-Bounded "dispatch_interrupted"
    }
    if ($mode -eq "concurrent_success") {
        Write-BoundedFixtureMarker ([string]$Fixture.dispatch_marker_directory) | Out-Null
        return [pscustomobject]([ordered]@{ started = $true; outcome = "completed" })
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
    if ($script:BoundedTestOnly -and [string]$script:BoundedFixture.dispatch_mode -ne "container_visible_import") {
        return Invoke-BoundedOfflineDispatch $script:BoundedFixture
    }
    $containerEvidenceContext = if ([string]::IsNullOrWhiteSpace($N8nContainer)) {
        $null
    } else {
        [pscustomobject]([ordered]@{
            operations_path = $script:BoundedOperationsRoot
            operation_path = $OperationPath
            operation_id = [string]$State.plan.operation_id
            operation_identity = [string]$State.plan.operation_identity
            plan_digest = [string]$State.plan_digest
            prepared_workflow_digest = [string]$State.plan.identity_seed.prepared_workflow_digest
        })
    }
    $importResult = Invoke-BoundedExternalCommand "import:workflow" @(
        "--input=$preparedPath",
        "--projectId=$([string]$State.intent.project.id)",
        "--activeState=false"
    ) -ContainerInputKey ([string]$State.plan.operation_id) -ContainerEvidenceContext $containerEvidenceContext
    if ([int]$importResult.exit_code -ne 0) { Stop-Bounded "n8n_command_failed" }
    return [pscustomobject]([ordered]@{ started = $true; outcome = "completed" })
}

function Get-BoundedReceipt {
    param([Parameter(Mandatory)][string]$OperationPath, [Parameter(Mandatory)][string]$Name)
    $path = Join-Path $OperationPath $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    return Read-BoundedJsonFile $path -RequireCanonical
}

function New-BoundedDispatchReceipt {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)][string]$DispatchState,
        [Parameter(Mandatory)][string]$Outcome,
        [Parameter(Mandatory)][bool]$ReplayAllowed
    )
    return [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.dispatch.v2"
        operation_id = [string]$State.plan.operation_id
        operation_identity = [string]$State.plan.operation_identity
        plan_digest = [string]$State.plan_digest
        mutation_intent_digest = Get-BoundedSha256File (Join-Path $State.operation_path "mutation-intent.json")
        dispatch_state = $DispatchState
        outcome = $Outcome
        replay_allowed = $ReplayAllowed
    })
}

function New-BoundedCompletionReceipt {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)][string]$DispatchDigest,
        [Parameter(Mandatory)]$Evidence,
        [Parameter(Mandatory)]$Manifest
    )
    $persistedState = Get-BoundedOperationState ([string]$State.operation_path)
    Assert-BoundedContainerCleanupVerified $persistedState.container_custody
    return [pscustomobject]([ordered]@{
        schema_version = "xb.member.gateway.bounded_import.completion.v2"
        operation_id = [string]$State.plan.operation_id
        operation_identity = [string]$State.plan.operation_identity
        plan_digest = [string]$State.plan_digest
        dispatch_digest = $DispatchDigest
        expected_projection_digest = [string]$State.plan.expected_projection_digest
        target_projection_digest = Get-BoundedSha256Text (Get-BoundedCanonicalJsonFromObject -Value (Get-BoundedWorkflowProjection $Evidence.workflow $Manifest) -AllowFloatingPoint)
        original_preimage_state = [string]$State.plan.identity_seed.preimage_state
        ownership = if ([string]$State.plan.identity_seed.preimage_state -eq "absent") { "created" } else { "updated_or_noop" }
        complete = $true
    })
}

function Invoke-BoundedDispatchAttempt {
    param(
        [Parameter(Mandatory)]$State,
        [Parameter(Mandatory)][string]$OperationsPath,
        [Parameter(Mandatory)][string]$OperationPath
    )
    Wait-BoundedFixtureAdmissionBarrier $script:BoundedFixture
    $ownership = New-BoundedDispatchOwnership $State
    if (-not (Try-BoundedAcquireDispatchOwnership $OperationPath $ownership)) { Stop-Bounded "dispatch_ownership_unavailable" }
    $pending = New-BoundedDispatchReceipt $State "dispatching" "pending" $false
    $replacePending = Test-Path -LiteralPath (Join-Path $OperationPath "dispatch-receipt.json") -PathType Leaf
    Write-BoundedOperationReceipt $OperationsPath $OperationPath "dispatch-receipt.json" $pending -ReplaceExisting:$replacePending
    $result = Invoke-BoundedMutation $State $OperationPath
    if (-not $result.started) {
        $notDispatched = New-BoundedDispatchReceipt $State "not_dispatched" "pre_dispatch_failure" $true
        Write-BoundedOperationReceipt $OperationsPath $OperationPath "dispatch-receipt.json" $notDispatched -ReplaceExisting
        Release-BoundedDispatchOwnership $OperationPath $ownership
        Stop-Bounded "pre_dispatch_failure"
    }
    if ([string]$result.outcome -notin @("completed", "ambiguous")) { Stop-Bounded "dispatch_outcome_invalid" }
    $dispatched = New-BoundedDispatchReceipt $State "dispatched" ([string]$result.outcome) $false
    Write-BoundedOperationReceipt $OperationsPath $OperationPath "dispatch-receipt.json" $dispatched -ReplaceExisting
    return $result
}

function Invoke-BoundedApply {
    param(
        [Parameter(Mandatory)]$Manifest,
        [Parameter(Mandatory)][string]$OperationsPath,
        [Parameter(Mandatory)][string]$OperationPath
    )
    Assert-BoundedNoGenericHooks $script:BoundedRepoRoot
    Assert-BoundedReviewedBinding $Manifest
    Assert-BoundedCustody $script:BoundedRepoRoot $OperationsPath $OperationPath
    $state = Get-BoundedOperationState $OperationPath
    $completion = $state.completion
    if ($null -ne $completion) {
        $evidence = Get-BoundedEvidence $Manifest -AfterDispatch
        Assert-BoundedCurrentOperationIdentity $state $evidence $Manifest
        if (-not (Test-BoundedExpectedTarget $state $evidence)) { Stop-Bounded "completed_target_changed" }
        Assert-BoundedContainerCleanupVerified $state.container_custody
        return [pscustomobject]([ordered]@{ status = "no_op_success"; mutation_attempted = 0; replay = $false; operation_identity = [string]$state.plan.operation_identity })
    }
    if ($null -ne $state.dispatch) {
        if ([string]$state.dispatch.dispatch_state -eq "not_dispatched") {
            $before = Get-BoundedEvidence $Manifest
            Assert-BoundedCurrentOperationIdentity $state $before $Manifest
            Assert-BoundedPlanEvidence $state $before $Manifest
            Assert-BoundedContainerCleanupVerified $state.container_custody
            $result = Invoke-BoundedDispatchAttempt $state $OperationsPath $OperationPath
            if ([string]$result.outcome -eq "ambiguous") { Stop-Bounded "dispatch_outcome_ambiguous" }
            $after = Get-BoundedEvidence $Manifest -AfterDispatch
            Assert-BoundedCurrentOperationIdentity $state $after $Manifest
            if (-not (Test-BoundedExpectedTarget $state $after)) { Stop-Bounded "readback_mismatch" }
            $completionState = Get-BoundedOperationState $OperationPath
            Assert-BoundedContainerCleanupVerified $completionState.container_custody
            $completion = New-BoundedCompletionReceipt $state (Get-BoundedSha256File (Join-Path $OperationPath "dispatch-receipt.json")) $after $Manifest
            Write-BoundedOperationReceipt $OperationsPath $OperationPath "completion-receipt.json" $completion
            return [pscustomobject]([ordered]@{ status = "applied_and_verified"; mutation_attempted = 1; replay = $false; operation_identity = [string]$state.plan.operation_identity })
        }
        $evidence = Get-BoundedEvidence $Manifest -AfterDispatch
        Assert-BoundedCurrentOperationIdentity $state $evidence $Manifest
        Assert-BoundedContainerCleanupVerified $state.container_custody
        if (Test-BoundedExpectedTarget $state $evidence) {
            if ([string]$state.dispatch.dispatch_state -ne "dispatched" -or [string]$state.dispatch.outcome -ne "completed") {
                $reconciled = New-BoundedDispatchReceipt $state "dispatched" "completed" $false
                Write-BoundedOperationReceipt $OperationsPath $OperationPath "dispatch-receipt.json" $reconciled -ReplaceExisting
            }
            $receipt = New-BoundedCompletionReceipt $state (Get-BoundedSha256File (Join-Path $OperationPath "dispatch-receipt.json")) $evidence $Manifest
            Write-BoundedOperationReceipt $OperationsPath $OperationPath "completion-receipt.json" $receipt
            return [pscustomobject]([ordered]@{ status = "completed_existing_dispatch"; mutation_attempted = 0; replay = $false; operation_identity = [string]$state.plan.operation_identity })
        }
        Stop-Bounded "dispatch_outcome_ambiguous"
    }
    $before = Get-BoundedEvidence $Manifest
    Assert-BoundedCurrentOperationIdentity $state $before $Manifest
    Assert-BoundedPlanEvidence $state $before $Manifest
    $result = Invoke-BoundedDispatchAttempt $state $OperationsPath $OperationPath
    if ([string]$result.outcome -eq "ambiguous") {
        Stop-Bounded "dispatch_outcome_ambiguous"
    }
    $after = Get-BoundedEvidence $Manifest -AfterDispatch
    Assert-BoundedCurrentOperationIdentity $state $after $Manifest
    if (-not (Test-BoundedExpectedTarget $state $after)) {
        Stop-Bounded "readback_mismatch"
    }
    $completionState = Get-BoundedOperationState $OperationPath
    Assert-BoundedContainerCleanupVerified $completionState.container_custody
    $completion = New-BoundedCompletionReceipt $state (Get-BoundedSha256File (Join-Path $OperationPath "dispatch-receipt.json")) $after $Manifest
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
    Assert-BoundedPrivateDestination $root $operations
    Assert-BoundedNoGenericHooks $root
    if ([string]::IsNullOrWhiteSpace($OperationId) -or $OperationId -notmatch '^[a-z0-9][a-z0-9._-]{2,96}$') { Stop-Bounded "operation_id_invalid" }
    return [pscustomobject]@{ root = $root; operations = $operations; operation = Join-Path $operations $OperationId }
}

function Invoke-BoundedMain {
    try {
        $context = Initialize-BoundedContext
        if ($Mode -eq "Apply" -and -not $ConfirmBoundedApply -and -not $TestOnly) { Stop-Bounded "apply_confirmation_required" }
        $manifestPath = if ([System.IO.Path]::IsPathRooted($BindingManifestFile)) { $BindingManifestFile } else { Join-Path $context.root $BindingManifestFile }
        $resolvedManifestPath = Resolve-BoundedFullPath $manifestPath
        Assert-BoundedManifestCustody $context.root $resolvedManifestPath
        $manifest = Read-BoundedJsonFile $resolvedManifestPath
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
            $stage = Initialize-BoundedPrivateRoot $context.root $context.operations $context.operation
            if ($stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
            Publish-BoundedPlanArtifacts $artifacts $context.root $context.operations $context.operation
            Write-Output (([pscustomobject]@{ status = "capture_plan_complete"; operation_id = $OperationId; operation_identity = $artifacts.operation_identity; mutation_attempted = 0 }) | ConvertTo-Json -Compress)
            $script:BoundedExitCode = 0
            return
        }
        if ($Mode -eq "Inspect") {
            Assert-BoundedCustody $context.root $context.operations $context.operation
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
