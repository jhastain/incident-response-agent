"""Cloud Run Health Tier B — one ADK LlmAgent + one async batch FunctionTool.

Same pattern as datadog_tierb.py: a single ordinary LlmAgent (so ADK drives the
model and ContextVars stay correct) with ONE tool, fetch_cloud_run(queries) —
an async FunctionTool that runs every planned Cloud Run SDK call CONCURRENTLY
via asyncio.to_thread in a single batch. The agent: plan the query list -> call
fetch_cloud_run ONCE -> summarize into findings_cloud_run. No MCP discovery
toolset, no per-query ReAct round-trips, no nested agents.
"""

from __future__ import annotations

import asyncio

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from ...config import create_fast_model
from ...tools.gcp_tools import (
    describe_cloud_run_service,
    list_cloud_run_revisions,
    list_cloud_run_services,
)
from ...tools.tool_resilience import keep_running_on_tool_error

_TOOL_MAP = {
    "list_cloud_run_services": list_cloud_run_services,
    "list_cloud_run_revisions": list_cloud_run_revisions,
    "describe_cloud_run_service": describe_cloud_run_service,
}


async def fetch_cloud_run(queries: list[dict]) -> str:
    """Execute Cloud Run SDK queries concurrently and return combined results.

    Args:
        queries: A list of dicts, each with a "tool" key naming one of
            "list_cloud_run_services", "list_cloud_run_revisions",
            "describe_cloud_run_service", plus that tool's kwargs
            (project_id, region, service_name as applicable), e.g.
            {"tool": "describe_cloud_run_service", "project_id": "webcore-prod-project",
             "region": "us-central1", "service_name": "acme-net"}.
            Pass ALL queries in ONE call — they run concurrently.

    Returns:
        Combined raw results, one block per query.
    """
    if not queries:
        return "No queries provided."

    async def _run_one(q: dict) -> str:
        tool_name = q.get("tool", "")
        func = _TOOL_MAP.get(tool_name)
        kwargs = {k: v for k, v in q.items() if k != "tool"}
        if func is None:
            return f"ERROR: unknown tool '{tool_name}'"
        try:
            return await asyncio.to_thread(func, **kwargs)
        except Exception as e:
            return f"ERROR calling {tool_name}({kwargs}): {type(e).__name__}: {e}"

    results = await asyncio.gather(*(_run_one(q) for q in queries))
    return "\n\n".join(
        f"### Query {i+1}: {q.get('tool', 'unknown')}({ {k: v for k, v in q.items() if k != 'tool'} })\n"
        f"RESULT:\n{r}"
        for i, (q, r) in enumerate(zip(queries, results))
    )


fetch_cloud_run_tool = FunctionTool(func=fetch_cloud_run)

INSTRUCTION = """\
You are the Cloud Run Health Investigator. You have ONE tool: fetch_cloud_run(queries).

## Context
- Classification: {classification}
- Service context: {service_context}

## How you work (do this exactly)
1. PLAN the Cloud Run queries as a list of dicts derived from the alert context above.
2. Call fetch_cloud_run(queries=[...]) ONE time with ALL of them — they run concurrently.
3. SUMMARIZE the returned results into findings (do NOT call the tool again unless a
   result shows a clear, fixable query error, e.g. a wrong service_name to retry once).

## Available query dicts (each needs a "tool" key plus kwargs)
- {"tool": "list_cloud_run_services", "project_id": <str>, "region": <str, default "us-central1">}
- {"tool": "list_cloud_run_revisions", "project_id": <str>, "region": <str, default "us-central1">, "service_name": <str, optional filter>}
- {"tool": "describe_cloud_run_service", "project_id": <str>, "region": <str, default "us-central1">, "service_name": <str, REQUIRED>}

## Domain rules
- Determine project_id and region from {{service_context}} / {{classification}}. Default region
  is "us-central1" if not otherwise indicated.
- Common Cloud Run service name mappings (derive service_name from the affected app in the alert):
  WebCore -> acme-net ; FileService -> fileservice-acme-net ;
  SubscriptionService -> subscription-service ; Pricing -> app-pricing-com.
- Plan queries generically from what the alert names — do NOT hardcode a single service if the
  alert implicates multiple services or is ambiguous; in that case also list_cloud_run_services
  to discover candidates.
- Typical plan: list_cloud_run_services (discover/confirm), describe_cloud_run_service for the
  affected service (traffic split, revision status, conditions), and list_cloud_run_revisions for
  that service (compare active vs latest revision, deployment recency around the alert time).

## Findings output
- Report ONLY what results show. Errors/empty -> "No data retrieved from Cloud Run for <query>".
- Extract: revision mismatches (active vs latest ready/created), traffic split anomalies, non-True
  conditions/messages (e.g. failed health probes, scaling limits, container startup failures), and
  any signs of a recent bad deployment around the alert time. Label STRONG EVIDENCE / MODERATE
  EVIDENCE / WEAK SIGNAL / UNKNOWN. No fabrication. Under 500 words.
"""

cloud_run_health_tierb = LlmAgent(
    model=create_fast_model(),
    name="cloud_run_health",  # keep name so synthesis/report/telemetry are unchanged
    description="Investigates Cloud Run service health, revisions, scaling, and resource utilization via a single concurrent batch fetch (Tier B).",
    instruction=INSTRUCTION,
    output_key="findings_cloud_run",
    tools=[fetch_cloud_run_tool],
    on_tool_error_callback=keep_running_on_tool_error,
)
