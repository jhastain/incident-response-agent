from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import truncate_tool_response
from ...tools.gcp_tools import list_alert_policies, query_time_series
from ...tools.tool_resilience import keep_running_on_tool_error

RESOURCE_FILTER_GUIDANCE = """\
## Resource filters must use REAL resource names
A filter that matches no resource returns zero series. That is NOT evidence the resource is
healthy — it means the query was wrong. Verified live 2026-07-28 against acme-production-100000:

- Cloud Run (resource.type cloud_run_revision): filter on resource.labels.service_name using the
  FULL region-suffixed service name, e.g. 'resource.labels.service_name="WebCore-prod-run-apiv2-us-c1"',
  "WebCore-prod-run-hangfire-recurring-us-c1", "WebCore-prod-run-hangfire-background-us-c1". Short
  names like "apiv2" or "hangfire-recurring" match NOTHING. Prod is prefixed "WebCore-prod-run-",
  test "WebCore-test-"; "-us-c1"/"-us-w1" are the regions — query both to compare regions.
- Memorystore is Memorystore for VALKEY: use the memorystore.googleapis.com/* metric namespace
  (e.g. memorystore.googleapis.com/instance/clients/total_connected_clients,
  memorystore.googleapis.com/instance/memory/average_utilization). The legacy
  redis.googleapis.com/* metrics return NO data here. Filter on resource.labels.instance_id with
  a real id: "WebCore-prod-valkey-hangfire-us-c1", "WebCore-prod-valkey-sessionstate-us-c1",
  "WebCore-prod-valkey-dbbacked-us-c1", "WebCore-prod-valkey-persistent-us-c1" (same four "-us-w1").
- Unsure of the exact name? Query with an EMPTY filter_str first — the returned resource labels
  reveal the real names — then re-query with the correct filter.
- When a query returns an empty series, report WHICH metric_type and filter matched nothing and
  label it UNKNOWN. Never report an empty series as a healthy metric.
"""

INSTRUCTION = build_agent_instruction(
    role="GCP Metrics Investigator",
    context_refs=["{classification}", "{service_context}"],
    checklist=[
        "1. List alert policies related to the affected service from {{classification}} alert context",
        "2. Get active alert incidents — identify which policies are currently firing",
        "3. Query time series for the alerted metric over a ±1 hour window centered on "
        "{{classification}}.alert_timestamp (ISO 8601 UTC); if alert_timestamp is 'unknown', use the last 2 hours",
        "4. Classify the metric trend: growing / stable / declining / spike / recovering",
        "5. Query related metrics (CPU, memory, request count, latency) to build a complete picture",
        "6. If multi-region service, compare the same metrics across all regions to identify if issue is regional or global",
    ],
    tools_available=["list_alert_policies", "query_time_series"],
    extra_context=RESOURCE_FILTER_GUIDANCE,
)

gcp_metrics_agent = LlmAgent(
    model=create_fast_model(),
    name="gcp_metrics",
    description="Analyzes GCP Monitoring alert policies, active incidents, and metric time series trends.",
    instruction=INSTRUCTION,
    output_key="findings_gcp_metrics",
    tools=[list_alert_policies, query_time_series],
    after_tool_callback=truncate_tool_response,
    on_tool_error_callback=keep_running_on_tool_error,
)
