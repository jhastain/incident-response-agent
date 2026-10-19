from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import build_tool_list, truncate_tool_response
from ...tools.gcp_mcp import create_cloud_logging_toolset
from ...tools.gcp_tools import query_gcp_logs
from ...tools.tool_resilience import keep_running_on_tool_error

_cloud_logging_mcp = create_cloud_logging_toolset()

INSTRUCTION = build_agent_instruction(
    role="GCP Logging Investigator",
    context_refs=["{classification}", "{service_context}"],
    checklist=[
        "1. Anchor every query on {{classification}}.alert_timestamp (ISO 8601 UTC). Query ERROR/WARNING logs in a "
        "window from (alert_timestamp - 30 min) to (alert_timestamp + 15 min). The earlier bound matters: many alerts "
        "(transient/self-resolving, log-match, no-data) fire AFTER the failing event and may already be RESOLVED, so the "
        "triggering log precedes the alert time — do NOT query only 'now', which misses aged-out transients. "
        "If alert_timestamp is 'unknown', fall back to the last 2 hours.",
        "2. Aggregate top 10 errors by error type and frequency — look for repeated patterns",
        "3. Extract stack traces from application logs (filter by resource.type matching the service)",
        "4. Query audit logs (protoPayload filter) for administrative actions in the time window — deployments, config changes, IAM changes",
        "5. Search for cache-specific errors using labels.BT_LogType filters: 'CacheRetrievalException' and 'ServerCacheStatus'",
        "6. Return error clusters with timestamps (converted to CST), error messages, and log entry counts",
    ],
    tools_available=[
        "list_log_entries (Cloud Logging MCP)",
        "list_log_names (Cloud Logging MCP)",
        "query_gcp_logs (native SDK)",
    ],
    extra_context="You have BOTH native SDK tools AND Cloud Logging MCP tools. "
    "Prefer MCP tools (list_log_entries) for log queries. "
    "Fall back to native query_gcp_logs if MCP returns errors.",
)

gcp_logging_agent = LlmAgent(
    model=create_fast_model(),
    name="gcp_logging",
    description="Searches GCP Cloud Logging for errors, warnings, stack traces, and audit events around alert time.",
    instruction=INSTRUCTION,
    output_key="findings_gcp_logging",
    tools=build_tool_list(_cloud_logging_mcp, query_gcp_logs),
    after_tool_callback=truncate_tool_response,
    on_tool_error_callback=keep_running_on_tool_error,
)
