from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import (
    build_tool_list,
    create_bifrost_toolset,
    truncate_tool_response,
)
from ...tools.gcp_tools import list_cloud_run_revisions
from ...tools.tool_resilience import keep_running_on_tool_error

EXTRA_CONTEXT = """
CRITICAL RULE: Deploy correlation ≠ causation.

To prove a deploy caused an issue, you MUST show:
(a) New code changed the affected code path (query, endpoint, config)
AND
(b) Errors ONLY appear on the new revision, not on the previous revision

CRITICAL RULE: Only flag PRs first deployed in THIS deployment.

There is NO tool to list or search PRs — PRs can only be fetched by a known ID via repo_get_pull_request_by_id. Do not attempt to enumerate PRs.
If a PR was already running in production for hours/days without issues, it is NOT a suspect.

CRITICAL: Limit CI pipeline queries to the 2-hour deploy window. Do not invent pipeline or PR listing tools.
"""

_bifrost = create_bifrost_toolset()

_bifrost_targets = [
    "search_datadog_ci_pipeline_events",
    "aggregate_dora_deployments",
    "repo_get_pull_request_by_id",
    "repo_get_repo_by_name_or_id",
]

INSTRUCTION = build_agent_instruction(
    role="Deploy Correlation Investigator",
    context_refs=["{classification}", "{service_context}"],
    checklist=[
        "1. Get recent CI pipeline runs/failures for the affected pipeline in the last 2 hours using search_datadog_ci_pipeline_events (Datadog CI Visibility) — limit to top 5 events",
        "2. Check recent deployments within the 2-day window using aggregate_dora_deployments or search_dora_deployments — identify the most recent deploy preceding the alert",
        "3. List recent Cloud Run revisions for the affected service using list_cloud_run_revisions — compare revision timestamps to alert time",
        "4. Note: there is NO tool to list or search PRs. Only fetch a PR by known ID via repo_get_pull_request_by_id if a specific PR ID is available from CI events or deployment metadata — check if it touches affected code paths (queries, endpoints, config)",
        "5. Build a timeline: when was code deployed → when did the revision go live → when did the alert fire",
        "6. Report deploy correlation evidence: (a) what changed, (b) when it deployed, (c) error timing, (d) confidence level (STRONG/MODERATE/WEAK) — always note correlation ≠ causation",
    ],
    tools_available=["list_cloud_run_revisions"],
    extra_context=EXTRA_CONTEXT,
    bifrost_target_tools=_bifrost_targets if _bifrost else None,
)

deploy_correlation_agent = LlmAgent(
    model=create_fast_model(),
    name="deploy_correlation",
    description="Investigates recent deployments, builds, PRs, and code changes to correlate with the alert.",
    instruction=INSTRUCTION,
    output_key="findings_deploy",
    tools=build_tool_list(_bifrost, list_cloud_run_revisions),
    on_tool_error_callback=keep_running_on_tool_error,
    after_tool_callback=truncate_tool_response,
)
