[CmdletBinding()]
param(
    [string]$TemplatePath = "config/member_forms_gateway_ingest.binding.template.json",
    [string]$OutputPath = ".tmp/member-gateway-g3/member_forms_gateway_ingest.binding.json",
    [string]$FormsApiUrl,
    [string]$SourceCursorUrl,
    [string]$GatewayIngestUrl,
    [string]$PageCheckpointUrl,
    [string]$FormIdReference,
    [string]$QuestionMapPath,
    [string]$AcceptedQuestionMapSha256,
    [string]$CursorBindingReference,
    [string]$WatermarkBindingReference,
    [string]$GoogleCredentialReference,
    [string]$GatewayCredentialReference,
    [string]$TargetProjectReference,
    [string]$TargetWorkflowReference,
    [string]$TargetPreimageReference,
    [switch]$ValidateTemplateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$expectedOutput = ".tmp/member-gateway-g3/member_forms_gateway_ingest.binding.json"
$questionKeys = @("name", "phone", "email", "birthday_month", "marketing_consent", "pdpa_acknowledged")

function Resolve-XbBindingRepoRoot {
    $current = (Resolve-Path -LiteralPath $PSScriptRoot).Path
    while ($true) {
        if ((Test-Path -LiteralPath (Join-Path $current ".git")) -and (Test-Path -LiteralPath (Join-Path $current "n8n-workflows"))) { return $current }
        $parent = Split-Path -Parent $current
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $current) { throw "binding_repo_root_not_found" }
        $current = $parent
    }
}

function Assert-XbHttpsEndpoint {
    param([string]$Value, [string]$ExpectedPath, [string]$FormReference)
    [Uri]$uri = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -cne "https") { throw "binding_endpoint_https_required" }
    if (-not [string]::IsNullOrEmpty($uri.UserInfo) -or -not [string]::IsNullOrEmpty($uri.Query) -or -not [string]::IsNullOrEmpty($uri.Fragment)) { throw "binding_endpoint_components_forbidden" }
    if ($uri.AbsolutePath -cne $ExpectedPath) { throw "binding_endpoint_route_invalid" }
    if (-not [string]::IsNullOrWhiteSpace($FormReference) -and $uri.AbsolutePath -cne ("/v1/forms/{0}/responses" -f $FormReference)) { throw "binding_form_id_mismatch" }
}

function Get-XbSha256Text {
    param([string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    try { return ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes))).Replace("-", "").ToLowerInvariant() }
    finally { [Array]::Clear($bytes, 0, $bytes.Length) }
}

$repoRoot = Resolve-XbBindingRepoRoot
$templateFullPath = Join-Path $repoRoot $TemplatePath
$template = Get-Content -Raw -LiteralPath $templateFullPath
$tokens = @([regex]::Matches($template, '\{\{[A-Z0-9_]+\}\}') | ForEach-Object Value)
if ($tokens.Count -ne (@($tokens | Select-Object -Unique)).Count) { throw "binding_placeholder_duplicate" }
$expectedTokens = @(
    "{{FORMS_API_URL}}", "{{SOURCE_CURSOR_URL}}", "{{GATEWAY_INGEST_URL}}", "{{PAGE_CHECKPOINT_URL}}",
    "{{FORM_ID_REFERENCE}}", "{{QUESTION_MAP_SHA256}}", "{{QUESTION_NAME_REFERENCE}}", "{{QUESTION_PHONE_REFERENCE}}",
    "{{QUESTION_EMAIL_REFERENCE}}", "{{QUESTION_BIRTHDAY_MONTH_REFERENCE}}", "{{QUESTION_MARKETING_CONSENT_REFERENCE}}",
    "{{QUESTION_PDPA_ACKNOWLEDGED_REFERENCE}}", "{{CURSOR_BINDING_REFERENCE}}", "{{WATERMARK_BINDING_REFERENCE}}",
    "{{GOOGLE_CREDENTIAL_REFERENCE}}", "{{GATEWAY_CREDENTIAL_REFERENCE}}", "{{TARGET_PROJECT_REFERENCE}}",
    "{{TARGET_WORKFLOW_REFERENCE}}", "{{TARGET_PREIMAGE_REFERENCE}}"
)
if (@(Compare-Object ($tokens | Sort-Object) ($expectedTokens | Sort-Object)).Count -ne 0) { throw "binding_placeholder_set_invalid" }

$commitShape = (& git -C $repoRoot show -s --format="%H %T %P" 52308eebb95d9bc4f5a09fbe036c197a5e95b176).Trim()
if ($commitShape -cne "52308eebb95d9bc4f5a09fbe036c197a5e95b176 3ab7e3363b7189d7d1bb79ec8a1988eb23e1c7b3 1052f40e62a5ff5884c8ba49aac8114cc634b81c") { throw "binding_canonical_commit_mismatch" }
$workflowPath = Join-Path $repoRoot "n8n-workflows/member_forms_gateway_ingest.workflow.json"
$workflowBlob = (& git -C $repoRoot hash-object $workflowPath).Trim()
if ($workflowBlob -cne "4eeff984fb8a9a2fb569c1648e8dd4350c45b2fe") { throw "binding_canonical_workflow_mismatch" }

if ($ValidateTemplateOnly) {
    [pscustomobject]@{ status = "template_valid"; placeholder_count = $tokens.Count; canonical_commit_match = $true; canonical_workflow_match = $true } | ConvertTo-Json -Compress
    exit 0
}

if ($OutputPath.Replace('\', '/') -cne $expectedOutput) { throw "binding_output_path_invalid" }
foreach ($value in @($FormIdReference, $CursorBindingReference, $WatermarkBindingReference, $GoogleCredentialReference, $GatewayCredentialReference, $TargetProjectReference, $TargetWorkflowReference, $TargetPreimageReference)) {
    if ([string]::IsNullOrWhiteSpace($value) -or $value.Length -gt 300 -or $value -match '[\r\n]') { throw "binding_reference_invalid" }
}
Assert-XbHttpsEndpoint -Value $FormsApiUrl -ExpectedPath ("/v1/forms/{0}/responses" -f $FormIdReference) -FormReference $FormIdReference
Assert-XbHttpsEndpoint -Value $SourceCursorUrl -ExpectedPath "/v1/source/cursor"
Assert-XbHttpsEndpoint -Value $GatewayIngestUrl -ExpectedPath "/v1/source-events"
Assert-XbHttpsEndpoint -Value $PageCheckpointUrl -ExpectedPath "/v1/source/cursor/page"

$questionMap = Get-Content -Raw -LiteralPath $QuestionMapPath | ConvertFrom-Json
$questionMapKeys = @($questionMap.PSObject.Properties.Name)
if (@(Compare-Object ($questionMapKeys | Sort-Object) ($questionKeys | Sort-Object)).Count -ne 0) { throw "binding_question_key_set_invalid" }
foreach ($key in $questionKeys) {
    $questionValue = [string]$questionMap.PSObject.Properties[$key].Value
    if ([string]::IsNullOrWhiteSpace($questionValue) -or $questionValue -notmatch '^[A-Za-z0-9._:-]{1,200}$') { throw "binding_question_reference_invalid" }
}
$canonicalQuestionMap = [ordered]@{}
foreach ($key in ($questionKeys | Sort-Object)) { $canonicalQuestionMap[$key] = [string]$questionMap.PSObject.Properties[$key].Value }
$questionMapHash = Get-XbSha256Text (($canonicalQuestionMap | ConvertTo-Json -Compress))
if ($AcceptedQuestionMapSha256 -cne $questionMapHash) { throw "binding_question_map_hash_mismatch" }

$values = [ordered]@{
    "{{FORMS_API_URL}}" = $FormsApiUrl; "{{SOURCE_CURSOR_URL}}" = $SourceCursorUrl; "{{GATEWAY_INGEST_URL}}" = $GatewayIngestUrl; "{{PAGE_CHECKPOINT_URL}}" = $PageCheckpointUrl
    "{{FORM_ID_REFERENCE}}" = $FormIdReference; "{{QUESTION_MAP_SHA256}}" = $questionMapHash
    "{{QUESTION_NAME_REFERENCE}}" = [string]$questionMap.name; "{{QUESTION_PHONE_REFERENCE}}" = [string]$questionMap.phone
    "{{QUESTION_EMAIL_REFERENCE}}" = [string]$questionMap.email; "{{QUESTION_BIRTHDAY_MONTH_REFERENCE}}" = [string]$questionMap.birthday_month
    "{{QUESTION_MARKETING_CONSENT_REFERENCE}}" = [string]$questionMap.marketing_consent; "{{QUESTION_PDPA_ACKNOWLEDGED_REFERENCE}}" = [string]$questionMap.pdpa_acknowledged
    "{{CURSOR_BINDING_REFERENCE}}" = $CursorBindingReference; "{{WATERMARK_BINDING_REFERENCE}}" = $WatermarkBindingReference
    "{{GOOGLE_CREDENTIAL_REFERENCE}}" = $GoogleCredentialReference; "{{GATEWAY_CREDENTIAL_REFERENCE}}" = $GatewayCredentialReference
    "{{TARGET_PROJECT_REFERENCE}}" = $TargetProjectReference; "{{TARGET_WORKFLOW_REFERENCE}}" = $TargetWorkflowReference; "{{TARGET_PREIMAGE_REFERENCE}}" = $TargetPreimageReference
}
$rendered = $template
foreach ($entry in $values.GetEnumerator()) {
    $escaped = ($entry.Value | ConvertTo-Json -Compress)
    $rendered = $rendered.Replace(('"' + $entry.Key + '"'), $escaped)
}
if ($rendered -match '\{\{[A-Z0-9_]+\}\}') { throw "binding_placeholder_unresolved" }
$manifest = $rendered | ConvertFrom-Json
$rendered = ($manifest | ConvertTo-Json -Depth 20) + "`n"
if ($rendered -match '(?i)authorization|bearer\s+[a-z0-9]|password|private[_ -]?key|api[_ -]?key|token\s*[:=]') { throw "binding_secret_content_detected" }

$outputFullPath = Join-Path $repoRoot $OutputPath
New-Item -ItemType Directory -Path (Split-Path -Parent $outputFullPath) -Force | Out-Null
$rendered | Set-Content -LiteralPath $outputFullPath -Encoding UTF8
& git -C $repoRoot check-ignore --quiet -- $OutputPath
if ($LASTEXITCODE -ne 0) { Remove-Item -LiteralPath $outputFullPath -Force; throw "binding_output_not_ignored" }
$trackedMatch = @(& git -C $repoRoot ls-files -- $OutputPath)
if ($LASTEXITCODE -ne 0) { Remove-Item -LiteralPath $outputFullPath -Force; throw "binding_tracking_check_failed" }
if ($trackedMatch.Count -ne 0) { Remove-Item -LiteralPath $outputFullPath -Force; throw "binding_output_tracked" }
if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($sid in @($currentSid, [Security.Principal.SecurityIdentifier]::new("S-1-5-18"), [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544"))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, "FullControl", "Allow"))
    }
    [IO.File]::SetAccessControl($outputFullPath, $acl)
    $effective = [IO.File]::GetAccessControl($outputFullPath)
    if (-not $effective.AreAccessRulesProtected) { Remove-Item -LiteralPath $outputFullPath -Force; throw "binding_output_acl_permissive" }
}

[pscustomobject]@{ status = "binding_rendered"; placeholder_count = $tokens.Count; question_count = $questionKeys.Count; question_map_sha256 = $questionMapHash; ignored = $true; tracked = $false; acl_restricted = $true } | ConvertTo-Json -Compress
