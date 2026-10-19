# Deploy the PagerDuty -> IR Agent webhook receiver to Cloud Run.
# This is a SEPARATE deployable from the agent (which is on Vertex AI Agent Engine).
# The receiver's runtime service account needs roles/aiplatform.user on the engine
# project so it can call reasoningEngines streamQuery.
#
# Usage:
#   .\deploy.ps1 -Project aiops-test-project
#   .\deploy.ps1 -Project aiops-prod-project
#   .\deploy.ps1 -Project aiops-test-project -EngineId projects/.../reasoningEngines/123
param(
    [string]$Project        = "aiops-test-project",
    [string]$Region         = "us-central1",
    [string]$Service        = "ir-pagerduty-receiver",
    [string]$EngineDisplayName = "incident_response_agent",
    # Explicit engine resource name. Overrides the displayName lookup below.
    [string]$EngineId       = "",
    # Runtime service account. Defaults to ir-pd-receiver-sa in the TARGET project —
    # deriving it from $Project rather than hardcoding one environment's SA, so that
    # passing -Project alone cannot silently deploy with another project's identity.
    [string]$ServiceAccount = ""
)

$ErrorActionPreference = "Stop"

if (-not $ServiceAccount) {
    $ServiceAccount = "ir-pd-receiver-sa@$Project.iam.gserviceaccount.com"
}

# Resolve the engine by displayName in the TARGET project rather than reading
# deployment_metadata.json.
#
# That file is rewritten wholesale by `agents-cli deploy` on every deploy and holds
# exactly ONE flat remote_agent_runtime_id with no environment key (see
# google/agents/cli/deploy/agent_runtime.py::write_deployment_metadata). With the agent
# living in more than one project, whichever environment deployed most recently owns the
# file — so a test deploy would repoint the prod receiver at the test engine, and vice
# versa. It is a mutable pointer to "the last thing deployed from this checkout", not a
# per-environment record.
#
# A displayName lookup against $Project is self-correcting: it always returns the engine
# actually running in the environment being deployed to, regardless of checkout state.
if (-not $EngineId) {
    Write-Host "Resolving engine '$EngineDisplayName' in $Project..."
    $token = (gcloud auth print-access-token)
    $uri = "https://$Region-aiplatform.googleapis.com/v1beta1/projects/$Project/locations/$Region/reasoningEngines"
    $resp = Invoke-RestMethod -Uri $uri -Headers @{ Authorization = "Bearer $token" } -Method Get

    $matched = @($resp.reasoningEngines | Where-Object { $_.displayName -eq $EngineDisplayName })
    if ($matched.Count -eq 0) {
        throw "No reasoning engine named '$EngineDisplayName' in $Project. Deploy the agent first (agents-cli deploy --project $Project), or pass -EngineId explicitly."
    }
    if ($matched.Count -gt 1) {
        throw "Ambiguous: $($matched.Count) engines named '$EngineDisplayName' in $Project. Pass -EngineId explicitly. Found: $($matched.name -join ', ')"
    }
    $EngineId = $matched[0].name
}

$engineUrl = "https://$Region-aiplatform.googleapis.com/v1beta1/$EngineId"

Write-Host "Project:        $Project"
Write-Host "ServiceAccount: $ServiceAccount"
Write-Host "Engine:         $EngineId"

gcloud run deploy $Service `
    --source $PSScriptRoot `
    --project $Project `
    --region $Region `
    --service-account $ServiceAccount `
    --allow-unauthenticated `
    --min-instances 1 `
    --no-cpu-throttling `
    --set-env-vars "IR_AGENT_ENGINE_URL=$engineUrl" `
    --set-secrets "PD_WEBHOOK_SECRET=pagerduty-webhook-signing-secret:latest,PD_REST_API_TOKEN=pagerduty-rest-api-token:latest,PD_FROM_EMAIL=pagerduty-from-email:latest"

# NOTE: --allow-unauthenticated is required — PagerDuty webhook POSTs carry no GCP identity,
# so an IAM-protected endpoint would 403 every delivery. The security boundary is the
# X-PagerDuty-Signature HMAC check in main.py, which rejects (403) before any work.
# --no-cpu-throttling keeps CPU allocated after the 202 ACK so the ~200-600s investigation
# in BackgroundTasks actually runs (Cloud Run throttles CPU to ~0 post-response otherwise,
# which would silently drop every triage). --min-instances 1 avoids cold-start on the
# webhook path (PD's initial-ACK delivery timeout).
#
# The three pagerduty-* secrets resolve per-project: each environment has its own webhook
# signing secret (from that environment's own PD webhook subscription) and they must never
# be shared between test and prod.
