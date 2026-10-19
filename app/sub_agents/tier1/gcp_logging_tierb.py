"""GCP Logging Tier B — one ADK LlmAgent + one async batch FunctionTool.

Unlike the Bifrost agents (which discover tools via a JSON-RPC dance), GCP
agents call TYPED SYNC Python SDK functions directly. The speedup here is
running every planned query_gcp_logs(**kwargs) call CONCURRENTLY via
asyncio.to_thread instead of a sequential ReAct loop.

Same shape as datadog_tierb: a single ordinary LlmAgent (so ADK drives the
model and ContextVars stay correct) that has ONE tool: fetch_gcp_logs(queries)
— an async FunctionTool that runs every planned query_gcp_logs call
concurrently in a single batch. The agent: plan the query list -> call
fetch_gcp_logs ONCE -> summarize into findings_gcp_logging. No discovery
meta-tools, no per-query round-trips, no nested agents.
"""

from __future__ import annotations

import asyncio

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from ...config import create_fast_model
from ...tools.gcp_tools import query_gcp_logs
from ...tools.tool_resilience import keep_running_on_tool_error


async def fetch_gcp_logs(queries: list[dict]) -> str:
    """Execute GCP Cloud Logging queries concurrently and return combined results.

    Args:
        queries: A list of kwargs dicts, each passed to query_gcp_logs(**kwargs).
            Each dict may contain: project_id (str, required), filter_str (str,
            required), order_by (str, default "timestamp desc"), limit (int,
            default 20). Pass ALL queries in ONE call — they run concurrently.

    Returns:
        Combined raw results, one block per query.
    """
    if not queries:
        return "No queries provided."

    async def _run_one(q: dict) -> str:
        try:
            return await asyncio.to_thread(query_gcp_logs, **q)
        except Exception as e:
            return f"ERROR running query: {type(e).__name__}: {e}"

    results = await asyncio.gather(*(_run_one(q) for q in queries))
    return "\n\n".join(
        f"### Query {i+1}\nPARAMS: {q}\nRESULT:\n{r}"
        for i, (q, r) in enumerate(zip(queries, results))
    )


fetch_gcp_logs_tool = FunctionTool(func=fetch_gcp_logs)

INSTRUCTION = """\
You are the GCP Logging Investigator. You have ONE tool: fetch_gcp_logs(queries).

## Context
- Classification: {classification}
- Service context: {service_context}

## How you work (do this exactly)
1. PLAN the Cloud Logging queries as a list of kwargs dicts for query_gcp_logs.
2. Call fetch_gcp_logs(queries=[...]) ONE time with ALL of them — they run concurrently.
3. SUMMARIZE the returned results into findings (do NOT call the tool again unless a
   result shows a clear, fixable query error).

## query_gcp_logs kwargs (each dict in the list)
- project_id (str, required): the GCP project ID to query, derived from <service_context>.
- filter_str (str, required): Cloud Logging filter expression.
- order_by (str, optional): default "timestamp desc".
- limit (int, optional): default 20; raise for broad error scans, keep low for targeted lookups.

## Query rules (same anchoring as before)
- Anchor every query on {{classification}}.alert_timestamp (ISO 8601 UTC). Query ERROR/WARNING
  logs in a window from (alert_timestamp - 30 min) to (alert_timestamp + 15 min) using
  timestamp>="<start>" AND timestamp<="<end>" in filter_str. The earlier bound matters: many
  alerts (transient/self-resolving, log-match, no-data) fire AFTER the failing event and may
  already be RESOLVED, so the triggering log precedes the alert time — do NOT query only "now",
  which misses aged-out transients.
- NEVER invent a date. You have no clock, so you cannot know what "the last 2 hours" is.
  alert_timestamp is now always populated — when the alert carried no trigger time it holds
  the run-start time and alert_timestamp_source begins with "APPROXIMATE". Anchor on it
  either way; when it is APPROXIMATE, say so in your findings and treat the window as a
  bound rather than a fact. Asked to guess, a previous run queried 2024-12-20 — the wrong
  YEAR — and reported 0 entries, which is indistinguishable from a healthy service.
- Derive project_id from <service_context> (the GCP project tied to the affected service).
- Plan a small set of queries covering, as relevant to the alert:
  1. Broad error scan: severity>=ERROR AND resource.type="cloud_run_revision" (or the relevant
     resource.type) in the time window, to surface top errors by type/frequency.
  2. Warning-level scan: severity=WARNING in the same window, if the alert suggests degradation
     rather than hard failure.
  3. Audit log query: protoPayload filter (logName has "cloudaudit.googleapis.com") in the window,
     for deployments/config/IAM changes that might correlate.
  4. Cache-specific search using labels.BT_LogType filters ("CacheRetrievalException",
     "ServerCacheStatus") if the alert or service context suggests caching involvement.
  Do NOT over-query — 2-5 queries is typical; only add what the alert justifies.

## Findings output
- Report ONLY what results show. Errors/empty -> "No data retrieved from GCP Logging for <query>".
- Aggregate top errors by type and frequency, extract stack traces, note audit events, and report
  error clusters with timestamps (converted to CST), error messages, and log entry counts. Cross-
  reference the alert time. Label STRONG EVIDENCE / MODERATE EVIDENCE / WEAK SIGNAL / UNKNOWN. No
  fabrication. Under 500 words.
"""

gcp_logging_tierb = LlmAgent(
    model=create_fast_model(),
    name="gcp_logging",  # keep name so synthesis/report/telemetry are unchanged
    description="Investigates GCP Cloud Logging via a single concurrent batch fetch (Tier B).",
    instruction=INSTRUCTION,
    output_key="findings_gcp_logging",
    tools=[fetch_gcp_logs_tool],
    on_tool_error_callback=keep_running_on_tool_error,
)
