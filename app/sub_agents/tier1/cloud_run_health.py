from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import build_tool_list, truncate_tool_response
from ...tools.gcp_mcp import create_cloud_run_toolset
from ...tools.gcp_tools import (
    describe_cloud_run_service,
    list_cloud_run_revisions,
    list_cloud_run_services,
)
from ...tools.tool_resilience import keep_running_on_tool_error

_cloud_run_mcp = create_cloud_run_toolset()

EXTRA_CONTEXT = """
Common Cloud Run Service Name Mappings:
- WebCore → acme-net
- FileService → fileservice-acme-net
- SubscriptionService → subscription-service
- Pricing → app-pricing-com

Investigate the service identified in {{classification}} alert context.

You have BOTH native SDK tools AND Cloud Run MCP tools. Prefer MCP tools
(get_service, list_services) when available. Fall back to native SDK tools
(list_cloud_run_services, describe_cloud_run_service) if MCP returns errors.
"""

INSTRUCTION = build_agent_instruction(
    role="Cloud Run Health Investigator",
    context_refs=["{classification}", "{service_context}"],
    checklist=[
        "1. List Cloud Run services in the project from alert context",
        "2. Describe the affected service — check revision status, traffic split, and deployment state",
        "3. List recent revisions for the service — identify active revision vs latest revision",
        "4. Check instance count vs min/max configured — look for scaling events or startup failures",
        "5. Check CPU and memory utilization vs resource limits — flag if near or at 100%",
        "6. Query Cloud Logging for OOM kills, container restarts, or health probe failures in the last 30 minutes",
    ],
    tools_available=[
        "get_service (Cloud Run MCP)",
        "list_services (Cloud Run MCP)",
        "list_cloud_run_services (native SDK)",
        "describe_cloud_run_service (native SDK)",
        "list_cloud_run_revisions (native SDK — not available via MCP)",
    ],
    extra_context=EXTRA_CONTEXT,
)

cloud_run_health_agent = LlmAgent(
    model=create_fast_model(),
    name="cloud_run_health",
    description="Investigates Cloud Run service health, revisions, scaling, and resource utilization.",
    instruction=INSTRUCTION,
    output_key="findings_cloud_run",
    tools=build_tool_list(
        _cloud_run_mcp,
        list_cloud_run_services,
        describe_cloud_run_service,
        list_cloud_run_revisions,
    ),
    after_tool_callback=truncate_tool_response,
    on_tool_error_callback=keep_running_on_tool_error,
)
