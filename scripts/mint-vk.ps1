# Mint (or reuse) the IR Agent's Bifrost virtual key for an environment and store it in
# that environment's Secret Manager.
#
# Scope comes from bifrost_mcp_config.json — the same three MCP servers and exact tool
# lists the agent uses today, so test and prod get dev's capability set and nothing more.
# Deliberately NO ADO pipeline tools: pipeline analysis goes through direct ADO REST in
# app/tools/ado_pipeline_tools.py using IR_AGENT_ADO_READONLY, not the VK.
#
# Idempotent: an existing VK with the same name is reused rather than duplicated.
# The secret container must already exist (Terraform owns it).
#
# Usage:
#   .\mint-vk.ps1 -TargetProject aiops-test-project -WhatIf
#   .\mint-vk.ps1 -TargetProject aiops-test-project
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("aiops-test-project", "aiops-prod-project")]
    [string]$TargetProject,

    [string]$VkName     = "incident-response-agent-bfvk",
    [string]$SecretName = "incident-response-agent-bfvk",

    # Bifrost management API. Defaults are derived from $TargetProject below.
    [string]$BifrostUrl        = "",
    [string]$MgmtKeySecret     = "bifrost-mgmt-api-key",
    [string]$MgmtKeySecretProject = "",

    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

# Each environment has its own Bifrost instance; the VK must be minted on the one the
# agent will actually call (BIFROST_LLM_URL / BIFROST_MCP_URL at deploy time).
if (-not $BifrostUrl) {
    $BifrostUrl = switch ($TargetProject) {
        "aiops-test-project" { "https://llm-gateway-test.internal.example" }
        "aiops-prod-project" { "https://llm-gateway.internal.example" }
    }
}
if (-not $MgmtKeySecretProject) { $MgmtKeySecretProject = $TargetProject }

$cfgPath = Join-Path $PSScriptRoot "..\bifrost_mcp_config.json"
if (-not (Test-Path $cfgPath)) { throw "bifrost_mcp_config.json not found at $cfgPath" }
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json

$mcpConfigs = @()
foreach ($s in $cfg.mcp_servers) {
    # "mcp_client_name" (string), NOT "mcp_client_id" (int) — the int form 500s with
    # "failed to get MCP client: not found" even though GET echoes it back that way.
    # Verified against live Bifrost; see services/agent-provisioner/app/bifrost_client.py.
    $mcpConfigs += @{
        mcp_client_name  = $s.name
        tools_to_execute = @($s.tools)
    }
}

Write-Host "Bifrost:        $BifrostUrl"
Write-Host "VK name:        $VkName"
Write-Host "Secret:         $SecretName (in $TargetProject)"
Write-Host "MCP scope:      $($mcpConfigs.Count) servers, $(($mcpConfigs | ForEach-Object { $_.tools_to_execute.Count } | Measure-Object -Sum).Sum) tools total"

# Preflight: Terraform owns the container; refuse to create one out of band.
gcloud secrets describe $SecretName --project=$TargetProject 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Secret container '$SecretName' does not exist in $TargetProject. Run the IaC pipeline first — Terraform owns the container, this script only adds the value."
}

$mgmtKey = gcloud secrets versions access latest --secret=$MgmtKeySecret --project=$MgmtKeySecretProject 2>$null
if ($LASTEXITCODE -ne 0 -or -not $mgmtKey) {
    throw "Could not read '$MgmtKeySecret' from $MgmtKeySecretProject — needed to call the Bifrost governance API."
}
$headers = @{ Authorization = "Bearer $mgmtKey"; "Content-Type" = "application/json" }

# Reuse an existing VK if present. No reliable server-side name filter, so filter client-side.
Write-Host "`nChecking for an existing VK named '$VkName'..."
$existing = $null
try {
    $list = Invoke-RestMethod -Uri "$BifrostUrl/api/governance/virtual-keys" -Headers $headers -Method Get
    $existing = @($list.virtual_keys | Where-Object { $_.name -eq $VkName })[0]
} catch {
    throw "Failed to list virtual keys on ${BifrostUrl}: $($_.Exception.Message)"
}

if ($existing) {
    Write-Host "  Found existing VK id=$($existing.id) — will reuse (not creating a duplicate)." -ForegroundColor Yellow
    Write-Host "  NOTE: its value is only returned at creation. If the stored secret is empty," -ForegroundColor Yellow
    Write-Host "        rotate via the Bifrost UI or delete + re-run this script." -ForegroundColor Yellow
    if (-not $WhatIf) { Write-Host "`nNothing to do." }
    return
}

$body = @{
    name             = $VkName
    description      = "Incident Response Agent - MCP tool access (Datadog, Sentry, ADO work items) + LLM inference. Managed by scripts/mint-vk.ps1."
    provider_configs = @(
        @{ provider = "vertex";    allow_all_keys = $true; allowed_models = @("*"); blacklisted_models = @() },
        @{ provider = "anthropic"; allow_all_keys = $true; allowed_models = @("*"); blacklisted_models = @() }
    )
    mcp_configs      = $mcpConfigs
} | ConvertTo-Json -Depth 10

if ($WhatIf) {
    Write-Host "`n[WhatIf] would POST to $BifrostUrl/api/governance/virtual-keys:" -ForegroundColor Magenta
    Write-Host $body
    Write-Host "`n[WhatIf] then add the returned value as a new version of $SecretName in $TargetProject." -ForegroundColor Magenta
    return
}

Write-Host "`nCreating VK..."
$resp = Invoke-RestMethod -Uri "$BifrostUrl/api/governance/virtual-keys" -Headers $headers -Method Post -Body $body
$vkValue = $resp.virtual_key.value
if (-not $vkValue) { throw "Bifrost did not return a virtual key value. Response: $($resp | ConvertTo-Json -Depth 5)" }
Write-Host "  Created VK id=$($resp.virtual_key.id)" -ForegroundColor Green

$tmp = New-TemporaryFile
try {
    # -NoNewline equivalent: a trailing newline would become part of the bearer token.
    [System.IO.File]::WriteAllText($tmp.FullName, $vkValue)
    gcloud secrets versions add $SecretName --project=$TargetProject --data-file=$tmp.FullName | Out-Null
    Write-Host "  Stored in $SecretName ($TargetProject)" -ForegroundColor Green
} finally {
    Remove-Item $tmp.FullName -Force -ErrorAction SilentlyContinue
}

Write-Host "`nVerifying the VK authenticates against $BifrostUrl/v1 ..."
try {
    $probe = @{ model = "vertex/claude-haiku-4-5"; messages = @(@{ role = "user"; content = "ok" }); max_tokens = 5 } | ConvertTo-Json -Depth 5
    Invoke-RestMethod -Uri "$BifrostUrl/v1/chat/completions" `
        -Headers @{ Authorization = "Bearer $vkValue"; "Content-Type" = "application/json" } `
        -Method Post -Body $probe -TimeoutSec 60 | Out-Null
    Write-Host "  200 OK — VK works." -ForegroundColor Green
} catch {
    Write-Host "  Probe failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  The VK was created and stored; check Bifrost routing rules for this environment." -ForegroundColor Yellow
}
