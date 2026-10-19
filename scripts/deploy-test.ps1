# Deploy the IR agent to aiops-test-project.
#
# This exists because the deploy is a long flag list and every omission is a silent
# behaviour change, not an error:
#
#   --cpu 4        omit and agents-cli deploys at cpu=1, which is NOT the Agent Engine
#                  API default (4). At cpu=1, 4 of 5 concurrent investigations were lost
#                  mid-Tier-1 (measured 2026-08-10). See README "--cpu 4 is REQUIRED".
#   --concurrency  API-recommended value is 2*cpu+1, so 9 at cpu=4.
#   --secrets      DD_API_KEY is easy to drop; without it Datadog silently never enables.
#   --update-env-vars  REPLACES the entire env map. Every currently-set var must be listed
#                  or it is removed. GCP_MCP_ENABLED and the BIFROST_* URLs are load-bearing.
#
# minInstances is deliberately NOT set: maxInstances (default 100) is what autoscales under
# load, and a warm floor costs idle billing without fixing concurrency — cpu does that.

$ErrorActionPreference = 'Stop'

$Project = 'aiops-test-project'
$Region  = 'us-central1'
$Sa      = 'ir-agent-sa@aiops-test-project.iam.gserviceaccount.com'

$Secrets = @(
    'BIFROST_API_TOKEN=incident-response-agent-bfvk:latest'
    'IR_AGENT_ADO_READONLY=ir-agent-ado-readonly:latest'
    'IR_AGENT_OCTOPUS_TASK_WATCHER=ir-agent-octopus-task-watcher:latest'
    'DD_API_KEY=ir-agent-datadog-api-key:latest'
    'BIFROST_LOGS_API_KEY=ir-agent-gateway-logs-read-key:latest'
) -join ','

$EnvVars = @(
    'GCP_MCP_ENABLED=false'
    'DD_ENV=test'
    'BIFROST_MCP_URL=https://llm-gateway-test.internal.example/mcp'
    'BIFROST_LLM_URL=https://llm-gateway-test.internal.example/v1'
    'AGENT_VERSION=0.1.0'
    'GOOGLE_GENAI_USE_VERTEXAI=true'
    'GOOGLE_CLOUD_LOCATION=global'
    'GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true'
    'OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT'
    'ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false'
    'DD_LLMOBS_ENABLED=true'
    'DD_SITE=us5.datadoghq.com'
    'DD_SERVICE=ir-agent'
    'DD_LLMOBS_ML_APP=ir-agent'
    'DD_TRACE_PARTIAL_FLUSH_ENABLED=false'
    # ddtrace's google-adk patch detaches an OTel context token in a different
    # asyncio task than the one that created it once tier1_parallel fans out,
    # which kills the TaskGroup. Our own spans come from DatadogRunPlugin.
    'DD_TRACE_GOOGLE_ADK_ENABLED=false'
) -join ','

Write-Host "Deploying incident_response_agent -> $Project" -ForegroundColor Cyan
Write-Host '  cpu 4 / memory 4Gi / concurrency 9 | 5 secrets | 16 env vars'

agents-cli deploy `
    --deployment-target agent_runtime `
    --project $Project `
    --region $Region `
    --no-confirm-project `
    --service-account $Sa `
    --cpu 4 `
    --memory 4Gi `
    --concurrency 9 `
    --secrets $Secrets `
    --update-env-vars $EnvVars
