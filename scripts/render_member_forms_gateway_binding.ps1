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
    [string]$AdmittedDeploymentCommit,
    [string]$AdmittedDeploymentTree,
    [string]$AdmittedDeploymentParent,
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

function Assert-XbExactProperties {
    param([Parameter(Mandatory)]$Value, [Parameter(Mandatory)][string[]]$Expected, [Parameter(Mandatory)][string]$ErrorId)
    if (@(Compare-Object @($Value.PSObject.Properties.Name | Sort-Object) @($Expected | Sort-Object)).Count -ne 0) { throw $ErrorId }
}

function Assert-XbClosedBindingTemplate {
    param([Parameter(Mandatory)]$Value)
    Assert-XbExactProperties $Value @("schema_version", "source_provenance", "deployment_admission", "endpoints", "source", "posture", "credentials", "target") "binding_template_root_shape_invalid"
    Assert-XbExactProperties $Value.source_provenance @("commit", "tree", "parent", "workflow_path", "workflow_blob", "workflow_id", "workflow_name") "binding_template_provenance_shape_invalid"
    Assert-XbExactProperties $Value.deployment_admission @("commit", "tree", "parent") "binding_template_admission_shape_invalid"
    Assert-XbExactProperties $Value.endpoints @("forms_api_url", "source_cursor_url", "gateway_ingest_url", "page_checkpoint_url") "binding_template_endpoint_shape_invalid"
    Assert-XbExactProperties $Value.source @("form_alias", "mapping_version", "form_id_reference", "question_map_sha256", "questions", "cursor_binding_reference", "watermark_binding_reference") "binding_template_source_shape_invalid"
    Assert-XbExactProperties $Value.source.questions @("name", "phone", "email", "birthday_month", "marketing_consent", "pdpa_acknowledged") "binding_template_question_shape_invalid"
    Assert-XbExactProperties $Value.posture @("active", "trigger", "available_in_mcp", "save_manual_executions", "save_success_execution", "save_error_execution", "static_data", "pin_data_present") "binding_template_posture_shape_invalid"
    Assert-XbExactProperties $Value.credentials @("google_forms_responses_read", "gateway_source_bearer") "binding_template_credentials_shape_invalid"
    foreach ($role in @("google_forms_responses_read", "gateway_source_bearer")) { Assert-XbExactProperties $Value.credentials.$role @("type", "reference", "nodes") "binding_template_credential_role_shape_invalid" }
    Assert-XbExactProperties $Value.target @("project_reference", "workflow_reference", "preimage_reference") "binding_template_target_shape_invalid"
    if ([string]$Value.schema_version -cne "xb.member.gateway.n8n.binding.v1" -or [string]$Value.source.form_alias -cne "member_registration" -or [string]$Value.source.mapping_version -cne "member-intake.v1") { throw "binding_template_fixed_value_invalid" }
    if ($Value.posture.active -ne $false -or [string]$Value.posture.trigger -cne "manual_only" -or $Value.posture.available_in_mcp -ne $false -or $Value.posture.save_manual_executions -ne $false -or [string]$Value.posture.save_success_execution -cne "none" -or [string]$Value.posture.save_error_execution -cne "none" -or $null -ne $Value.posture.static_data -or $Value.posture.pin_data_present -ne $false) { throw "binding_template_posture_invalid" }
    if ([string]$Value.credentials.google_forms_responses_read.type -cne "googleOAuth2Api" -or [string]$Value.credentials.gateway_source_bearer.type -cne "httpBearerAuth") { throw "binding_template_credential_type_invalid" }
    if (@($Value.credentials.google_forms_responses_read.nodes).Count -ne 1 -or [string]$Value.credentials.google_forms_responses_read.nodes[0] -cne "Google Forms single page (configured outside repo)") { throw "binding_template_google_nodes_invalid" }
    $gatewayNodes = @("Read durable source cursor", "Protected XB Gateway ingest (configured outside repo)", "Commit durable page checkpoint")
    if (@(Compare-Object @($Value.credentials.gateway_source_bearer.nodes | Sort-Object) @($gatewayNodes | Sort-Object)).Count -ne 0) { throw "binding_template_gateway_nodes_invalid" }
    $locations = [ordered]@{
        "endpoints.forms_api_url"="{{FORMS_API_URL}}"; "endpoints.source_cursor_url"="{{SOURCE_CURSOR_URL}}"; "endpoints.gateway_ingest_url"="{{GATEWAY_INGEST_URL}}"; "endpoints.page_checkpoint_url"="{{PAGE_CHECKPOINT_URL}}"
        deployment_commit="{{DEPLOYMENT_COMMIT}}"; deployment_tree="{{DEPLOYMENT_TREE}}"; deployment_parent="{{DEPLOYMENT_PARENT}}"
        google_reference="{{GOOGLE_CREDENTIAL_REFERENCE}}"; gateway_reference="{{GATEWAY_CREDENTIAL_REFERENCE}}"
    }
    if ([string]$Value.endpoints.forms_api_url -cne $locations['endpoints.forms_api_url'] -or [string]$Value.endpoints.source_cursor_url -cne $locations['endpoints.source_cursor_url'] -or [string]$Value.endpoints.gateway_ingest_url -cne $locations['endpoints.gateway_ingest_url'] -or [string]$Value.endpoints.page_checkpoint_url -cne $locations['endpoints.page_checkpoint_url'] -or [string]$Value.deployment_admission.commit -cne $locations.deployment_commit -or [string]$Value.deployment_admission.tree -cne $locations.deployment_tree -or [string]$Value.deployment_admission.parent -cne $locations.deployment_parent -or [string]$Value.credentials.google_forms_responses_read.reference -cne $locations.google_reference -or [string]$Value.credentials.gateway_source_bearer.reference -cne $locations.gateway_reference) { throw "binding_placeholder_location_invalid" }
}

function Set-XbPrivateAcl {
    param([Parameter(Mandatory)][string]$Path)
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { return }
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($sid in @($currentSid, [Security.Principal.SecurityIdentifier]::new("S-1-5-18"), [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544"))) { $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")) }
    [IO.Directory]::SetAccessControl($Path, $acl)
    if (-not [IO.Directory]::GetAccessControl($Path).AreAccessRulesProtected) { throw "binding_output_acl_permissive" }
}

function Invoke-XbBindingRenderer {
$repoRoot = Resolve-XbBindingRepoRoot
$trackedStatus = @(& git -C $repoRoot status --porcelain --untracked-files=no)
if ($LASTEXITCODE -ne 0 -or $trackedStatus.Count -ne 0) { throw "binding_deployment_checkout_not_clean" }
$templateFullPath = Join-Path $repoRoot $TemplatePath
$template = Get-Content -Raw -LiteralPath $templateFullPath
$templateObject = $template | ConvertFrom-Json
Assert-XbClosedBindingTemplate $templateObject
$tokens = @([regex]::Matches($template, '\{\{[A-Z0-9_]+\}\}') | ForEach-Object Value)
if ($tokens.Count -ne (@($tokens | Select-Object -Unique)).Count) { throw "binding_placeholder_duplicate" }
$expectedTokens = @(
    "{{FORMS_API_URL}}", "{{SOURCE_CURSOR_URL}}", "{{GATEWAY_INGEST_URL}}", "{{PAGE_CHECKPOINT_URL}}",
    "{{FORM_ID_REFERENCE}}", "{{QUESTION_MAP_SHA256}}", "{{QUESTION_NAME_REFERENCE}}", "{{QUESTION_PHONE_REFERENCE}}",
    "{{QUESTION_EMAIL_REFERENCE}}", "{{QUESTION_BIRTHDAY_MONTH_REFERENCE}}", "{{QUESTION_MARKETING_CONSENT_REFERENCE}}",
    "{{QUESTION_PDPA_ACKNOWLEDGED_REFERENCE}}", "{{CURSOR_BINDING_REFERENCE}}", "{{WATERMARK_BINDING_REFERENCE}}",
    "{{GOOGLE_CREDENTIAL_REFERENCE}}", "{{GATEWAY_CREDENTIAL_REFERENCE}}", "{{TARGET_PROJECT_REFERENCE}}",
    "{{TARGET_WORKFLOW_REFERENCE}}", "{{TARGET_PREIMAGE_REFERENCE}}"
    , "{{DEPLOYMENT_COMMIT}}", "{{DEPLOYMENT_TREE}}", "{{DEPLOYMENT_PARENT}}"
)
if (@(Compare-Object ($tokens | Sort-Object) ($expectedTokens | Sort-Object)).Count -ne 0) { throw "binding_placeholder_set_invalid" }

$commitShape = (& git -C $repoRoot show -s --format="%H %T %P" 52308eebb95d9bc4f5a09fbe036c197a5e95b176).Trim()
if ($commitShape -cne "52308eebb95d9bc4f5a09fbe036c197a5e95b176 3ab7e3363b7189d7d1bb79ec8a1988eb23e1c7b3 1052f40e62a5ff5884c8ba49aac8114cc634b81c") { throw "binding_source_provenance_commit_mismatch" }
$workflowPath = Join-Path $repoRoot "n8n-workflows/member_forms_gateway_ingest.workflow.json"
$workflowBlob = (& git -C $repoRoot hash-object $workflowPath).Trim()
if ($workflowBlob -cne "4eeff984fb8a9a2fb569c1648e8dd4350c45b2fe") { throw "binding_source_provenance_workflow_mismatch" }

if ($ValidateTemplateOnly) {
    [pscustomobject]@{ status = "template_valid"; placeholder_count = $tokens.Count; source_provenance_commit_match = $true; source_provenance_workflow_match = $true } | ConvertTo-Json -Compress
    exit 0
}

if ($OutputPath.Replace('\', '/') -cne $expectedOutput) { throw "binding_output_path_invalid" }
$deploymentCommit = (& git -C $repoRoot rev-parse HEAD).Trim()
$deploymentTree = (& git -C $repoRoot rev-parse 'HEAD^{tree}').Trim()
$deploymentParent = (& git -C $repoRoot rev-parse 'HEAD^').Trim()
if ($LASTEXITCODE -ne 0 -or $AdmittedDeploymentCommit -cne $deploymentCommit -or $AdmittedDeploymentTree -cne $deploymentTree -or $AdmittedDeploymentParent -cne $deploymentParent) { throw "binding_deployment_admission_mismatch" }
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
    "{{DEPLOYMENT_COMMIT}}" = $deploymentCommit; "{{DEPLOYMENT_TREE}}" = $deploymentTree; "{{DEPLOYMENT_PARENT}}" = $deploymentParent
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
& git -C $repoRoot check-ignore --quiet -- $OutputPath
if ($LASTEXITCODE -ne 0) { throw "binding_output_not_ignored" }
$trackedMatch = @(& git -C $repoRoot ls-files -- $OutputPath)
if ($LASTEXITCODE -ne 0) { throw "binding_tracking_check_failed" }
if ($trackedMatch.Count -ne 0) { throw "binding_output_tracked" }
if (Test-Path -LiteralPath $outputFullPath) { throw "binding_output_preimage_exists" }
$outputParent = Split-Path -Parent $outputFullPath
New-Item -ItemType Directory -Path $outputParent -Force | Out-Null
$stageRoot = Join-Path $outputParent (".binding-stage-" + [Guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory -Path $stageRoot | Out-Null
    Set-XbPrivateAcl -Path $stageRoot
    $stageFile = Join-Path $stageRoot "binding.json"
    $rendered | Set-Content -LiteralPath $stageFile -Encoding UTF8
    Move-Item -LiteralPath $stageFile -Destination $outputFullPath
} catch {
    if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $outputFullPath) { Remove-Item -LiteralPath $outputFullPath -Force -ErrorAction SilentlyContinue }
    throw
} finally {
    if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue }
}

[pscustomobject]@{ status = "binding_rendered"; placeholder_count = $tokens.Count; question_count = $questionKeys.Count; question_map_sha256 = $questionMapHash; ignored = $true; tracked = $false; acl_restricted = $true } | ConvertTo-Json -Compress
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-XbBindingRenderer }
