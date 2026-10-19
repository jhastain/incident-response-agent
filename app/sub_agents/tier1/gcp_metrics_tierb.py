"""GCP Metrics Tier B — one ADK LlmAgent + one async batch FunctionTool.

Same pattern as gcp_logging_tierb.py: GCP agents call TYPED SYNC Python SDK
functions directly (no Bifrost discovery dance). The speedup here is running
every planned list_alert_policies(**kwargs) / query_time_series(**kwargs) call
CONCURRENTLY via asyncio.to_thread instead of a sequential ReAct loop.

A single ordinary LlmAgent (so ADK drives the model and ContextVars stay
correct) that has ONE tool: fetch_gcp_metrics(queries) — an async FunctionTool
that dispatches each planned query to the right SDK function and runs them all
concurrently in a single batch. The agent: plan the query list -> call
fetch_gcp_metrics ONCE -> summarize into findings_gcp_metrics. No discovery
meta-tools, no per-query round-trips, no nested agents.
"""

from __future__ import annotations

import asyncio

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from ...config import create_fast_model
from ...tools.gcp_tools import list_alert_policies, query_time_series
from ...tools.tool_resilience import keep_running_on_tool_error

_DISPATCH = {
    "query_time_series": query_time_series,
    "list_alert_policies": list_alert_policies,
}


def _diagnose_empty(q: dict, result: str) -> str:
    """Turn a zero-series time series result into a self-diagnosing message.

    An empty result means the metric_type + filter_str combination matched no
    resource — NOT that the resource is healthy. Name the filter that matched
    nothing so the finding is reportable instead of silently implying health.
    """
    if q.get("tool") != "query_time_series" or result.strip() not in ("[]", "[ ]"):
        return result

    metric_type = q.get("metric_type", "<unset>")
    filter_str = q.get("filter_str", "")
    matched = f'filter {filter_str}' if filter_str else "no filter (whole project)"
    return (
        f"NO SERIES MATCHED: metric_type={metric_type!r} with {matched} returned zero "
        f"time series over the last {q.get('interval_minutes', 30)} minutes. This means the "
        f"filter matched NO resource — it does NOT mean the resource is healthy. Most likely "
        f"the resource label value is wrong (e.g. a short service name like 'apiv2' instead of "
        f"the full Cloud Run service name 'WebCore-prod-run-apiv2-us-c1'), the metric namespace is "
        f"wrong (Memorystore for Valkey publishes memorystore.googleapis.com/*, NOT "
        f"redis.googleapis.com/*), or the metric is not emitted in this project. "
        f"Report this as 'no data for filter {matched}' — do not infer a healthy metric."
    )


async def fetch_gcp_metrics(queries: list[dict]) -> str:
    """Execute GCP Monitoring queries concurrently and return combined results.

    Args:
        queries: A list of dicts, each with a "tool" key naming which function
            to call ("query_time_series" or "list_alert_policies") plus the
            kwargs for that function.
            - query_time_series kwargs: project_id (str, required), metric_type
              (str, required), filter_str (str, default ""), interval_minutes
              (int, default 30).
            - list_alert_policies kwargs: project_id (str, required).
            Pass ALL queries in ONE call — they run concurrently.

    Returns:
        Combined raw results, one block per query.
    """
    if not queries:
        return "No queries provided."

    async def _run_one(q: dict) -> str:
        q = dict(q)
        tool_name = q.pop("tool", None)
        fn = _DISPATCH.get(tool_name)
        if fn is None:
            return f"ERROR: unknown tool '{tool_name}'. Must be one of {list(_DISPATCH)}."
        try:
            result = await asyncio.to_thread(fn, **q)
        except Exception as e:
            return f"ERROR running query: {type(e).__name__}: {e}"
        return _diagnose_empty({**q, "tool": tool_name}, result)

    results = await asyncio.gather(*(_run_one(q) for q in queries))
    return "\n\n".join(
        f"### Query {i+1}\nPARAMS: {q}\nRESULT:\n{r}"
        for i, (q, r) in enumerate(zip(queries, results))
    )


fetch_gcp_metrics_tool = FunctionTool(func=fetch_gcp_metrics)

INSTRUCTION = """\
You are the GCP Metrics Investigator. You have ONE tool: fetch_gcp_metrics(queries).

## Context
- Classification: {classification}
- Service context: {service_context}

## YOU HAVE NO BIFROST DISCOVERY META-TOOLS — do not plan or narrate one

`executeToolCode`, `listToolFiles`, `getToolDocs`, and `readToolFile` are NOT declared to
you. `fetch_gcp_metrics` is your only tool — call it directly. It dispatches straight to the
typed GCP SDK functions, so there is no Bifrost hop here at all. The two query shapes below
are the complete surface and there is nothing to discover first: never write a plan that
"lists tool files" or "reads the tool docs", and never narrate the output of a discovery
step you did not take.

Measured 2026-08-07 across 7 sessions: 6 `executeToolCode` calls were emitted by Tier-B
agents, one of them by this one, each immediately preceded in the same turn by invented
narration of the form `The Output from allowed tools calls is - {"listToolFiles": ...}`.
The model wrote the fiction first, then called the meta-tool to complete it. ADK rejects
the call — `Tool 'executeToolCode' not found` — so it returns no data, and the wasted slot
still counts against the 6-call investigation budget: roughly 17% of the budget spent on a
tool that does not exist. If you need to compute something over data you already hold, do
it in your own reasoning — there is no code-execution tool.

## How you work (do this exactly)
1. PLAN the GCP Monitoring queries as a list of dicts, each derived FROM THE ALERT
   (<classification>'s category, affected service, keywords/error_message).
2. Call fetch_gcp_metrics(queries=[...]) ONE time with ALL of them — they run concurrently.
3. SUMMARIZE the returned results into findings (do NOT call the tool again unless a
   result shows a clear, fixable query error).

## Query dict shapes (each dict in the list needs a "tool" key)
- {{"tool": "list_alert_policies", "project_id": "<project>"}}
  -> lists alert policies configured in the project, to identify what could be firing.
- {{"tool": "query_time_series", "project_id": "<project>", "metric_type": "<metric>",
   "filter_str": "<optional resource.labels filter>", "interval_minutes": <int>}}
  -> time series data points for one metric over the given window.

## Query rules (same anchoring/selection as before)
- Anchor the window on <classification>.alert_timestamp: compute interval_minutes as the
  number of minutes from alert_timestamp to now, with at least a ±1 hour span centered on
  the alert (i.e. cover from ~1 hour before to ~1 hour after). query_time_series only
  supports a window counting back from now, so size interval_minutes to comfortably include
  the alert time. alert_timestamp is now always populated; if alert_timestamp_source begins
  with "APPROXIMATE" it is the run-start time rather than a real trigger time, so use
  interval_minutes=120 and say in your findings that the window is a bound, not the incident
  window. Never invent an absolute date — you have no clock.
- Derive project_id from <service_context> (the GCP project tied to the affected service).
- RESOURCE FILTERS MUST USE REAL RESOURCE NAMES — a wrong name returns zero series, which is
  NOT evidence of health. Verified live 2026-07-28 against acme-production-100000:
  - Cloud Run: resource.type is cloud_run_revision and the filter label is
    resource.labels.service_name. The value is the FULL deployed service name, which is
    region-suffixed — e.g. "WebCore-prod-run-apiv2-us-c1" / "WebCore-prod-run-apiv2-us-w1",
    "WebCore-prod-run-hangfire-recurring-us-c1", "WebCore-prod-run-hangfire-background-us-c1".
    A short name ("apiv2", "hangfire-recurring") matches NOTHING. Prod services are prefixed
    "WebCore-prod-run-", test services "WebCore-test-"; regions are the "-us-c1"/"-us-w1" suffixes,
    so query BOTH suffixes to compare regions.
  - Memorystore (Valkey): these are Memorystore for VALKEY instances, so the metric namespace is
    memorystore.googleapis.com/* — the legacy redis.googleapis.com/* metrics return NO data here.
    resource.type is memorystore.googleapis.com/Instance (instance-level) or
    memorystore.googleapis.com/InstanceNode (node-level), and the filter label is
    resource.labels.instance_id. Real prod instance_ids: "WebCore-prod-valkey-hangfire-us-c1",
    "WebCore-prod-valkey-sessionstate-us-c1", "WebCore-prod-valkey-dbbacked-us-c1",
    "WebCore-prod-valkey-persistent-us-c1" (and the same four with a "-us-w1" suffix). Pick the
    instance by ROLE: hangfire jobs -> valkey-hangfire, session/JWT/login -> valkey-sessionstate,
    general cache -> valkey-dbbacked.
- If unsure of the exact resource name, run the query with NO filter_str first — the returned
  resource labels reveal the real names, then re-query with the correct filter.
- Metric selection by <classification>.category (pick the metrics relevant to the category,
  do not query every metric for every alert):
  - cloud-infrastructure: run.googleapis.com/container/memory/utilizations,
    run.googleapis.com/container/cpu/utilizations, run.googleapis.com/container/instance_count,
    run.googleapis.com/request_count (health check / OOM / resource exhaustion signals).
  - pubsub-messaging: pubsub.googleapis.com/subscription/num_undelivered_messages,
    pubsub.googleapis.com/subscription/dead_letter_message_count,
    pubsub.googleapis.com/subscription/oldest_unacked_message_age.
  - performance: run.googleapis.com/request_latencies, run.googleapis.com/request_count.
  - sql-database, scheduled-jobs, data-integrity: no direct GCP metric — skip time series
    unless the alert explicitly names a GCP-monitored resource; rely on list_alert_policies only.
  - auth-security: run.googleapis.com/request_count with a filter_str isolating 4xx/5xx if
    available, otherwise skip.
  - application-errors, business-logic, deploy-release, monitoring-meta, unknown: query
    run.googleapis.com/request_count and run.googleapis.com/container/cpu/utilizations for the
    affected service as a general health check.
- REDIS / CACHE / SESSION / JWT / LOGIN signals (any category — if the alert or findings
  mention redis, valkey, cache, session, JWT, token, or an auth/login endpoint): ALSO query
  Memorystore metrics to CONFIRM or REFUTE a cache-timeout hypothesis with real numbers
  (do not just assert "Redis timeout" — measure it), using the VALKEY metric namespace:
  memorystore.googleapis.com/instance/clients/total_connected_clients,
  memorystore.googleapis.com/instance/memory/average_utilization,
  memorystore.googleapis.com/instance/cpu/average_utilization,
  memorystore.googleapis.com/instance/commandstats/total_calls_count, and for blocked clients
  memorystore.googleapis.com/instance/node/clients/blocked_clients. Filter with
  resource.labels.instance_id="<one of the WebCore-*-valkey-* ids above>". Project = the
  Memorystore project (usually the prod project, e.g. acme-production-100000). If these return NO
  spike in the alert window, that REFUTES the Redis-timeout hypothesis — say so, and do not
  commit to Redis as the cause. If they return NO SERIES AT ALL, the filter is wrong, which
  refutes nothing — report it as missing data, not as a healthy cache.
- DEPLOY HANG / step-took-too-long (deploy-release with a Cloud Run deploy step): query
  run.googleapis.com/container/instance_count for the affected service to see the actual
  instance ramp — this is the concrete artifact needed to back (or refute) a "min-instance
  scaling delay" mechanism. If instance_count does not show a slow ramp in the window, do NOT
  assert scaling delay as the cause.
- Always start with 1 list_alert_policies call for the project, then 2-5 query_time_series
  calls for the relevant metrics above. Do NOT over-query.
- If the service spans multiple regions, add filter_str entries per region to compare and
  determine if the issue is regional or global.

## Findings output
- Report ONLY what results show. Errors -> "No data retrieved from GCP Monitoring for <query>".
- A result beginning "NO SERIES MATCHED" is a REPORTABLE FINDING, not a healthy metric. State
  which metric_type and which filter matched nothing, and label it UNKNOWN — never write
  "CPU normal" / "memory healthy" / "no Redis pressure" off an empty series.
- Classify each metric's trend: growing / stable / declining / spike / recovering, with values
  and timestamps (converted to CST). Note which alert policies are enabled/related. Cross-
  reference the alert time. Label STRONG EVIDENCE / MODERATE EVIDENCE / WEAK SIGNAL / UNKNOWN.
  No fabrication. Under 500 words.
"""

gcp_metrics_tierb = LlmAgent(
    model=create_fast_model(),
    name="gcp_metrics",  # keep name so synthesis/report/telemetry are unchanged
    description="Analyzes GCP Monitoring alert policies and metric time series via a single concurrent batch fetch (Tier B).",
    instruction=INSTRUCTION,
    output_key="findings_gcp_metrics",
    tools=[fetch_gcp_metrics_tool],
    on_tool_error_callback=keep_running_on_tool_error,
)
