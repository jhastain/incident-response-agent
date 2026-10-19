"""Known Issues (Azure DevOps) Tier B — one ADK LlmAgent + one async batch FunctionTool.

Same rationale as datadog_tierb.py: a single ordinary LlmAgent (so ADK drives the
model and ContextVars stay correct) that has ONE tool: fetch_known_issues(queries) —
an async FunctionTool that runs every planned azure_devops_token.* call CONCURRENTLY
via httpx in a single batch. The agent: plan the query list -> call
fetch_known_issues ONCE -> summarize into findings_known_issues. No discovery
meta-tools, no per-query ReAct round-trips, no nested agents, no inline model calls.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from ...config import create_fast_model
from ...tools.bifrost_direct import bifrost_execute_many_annotated
from ...tools.tool_resilience import keep_running_on_tool_error


async def fetch_known_issues(queries: list[str]) -> str:
    """Execute Azure DevOps queries concurrently via Bifrost and return combined results.

    Args:
        queries: A list of complete Python code strings, each an Azure DevOps call
            against the `azure_devops_token` server, e.g.
            'result = azure_devops_token.wit_get_query_results_by_id(project="AcmeProjects", id="...")\\nprint(result)'.
            Pass ALL queries in ONE call — they run in parallel.

    Returns:
        Combined raw results, one block per query.
    """
    if not queries:
        return "No queries provided."
    # Zip against the codes actually RUN — the executor may rewrite a boolean Sentry
    # query into several, which would misalign a zip against the requested list.
    results, codes, notes = await bifrost_execute_many_annotated([str(q) for q in queries])
    blocks = [
        f"### Query {i+1}\nCODE: {q[:200]}\nRESULT:\n{r}"
        for i, (q, r) in enumerate(zip(codes, results))
    ]
    if notes:
        blocks.insert(0, "### NOTE — queries were rewritten before execution\n" + "\n".join(notes))
    return "\n\n".join(blocks)


fetch_known_issues_tool = FunctionTool(func=fetch_known_issues)

INSTRUCTION = """\
You are the Known Issues Investigator. You search Azure DevOps for existing bugs
and known issues matching the alert pattern. You have ONE tool: fetch_known_issues(queries).

## Context
- Classification: {classification}
- Service context: {service_context}

## CAPABILITY LIMIT — READ THIS FIRST. Keyword search of ADO is NOT AVAILABLE.
Verified live 2026-07-28 against the actual Azure DevOps tool surface. The complete set of ADO
tools reachable through `azure_devops_token` is exactly these ten:
  core_get_identity_ids, repo_get_branch_by_name, repo_get_pull_request_by_id,
  repo_get_repo_by_name_or_id, wit_get_query, wit_get_query_results_by_id, wit_get_work_item,
  wit_get_work_item_type, wit_get_work_items_batch_by_ids, wit_get_work_items_for_iteration

Verified by live probe:
- NO ad-hoc WIQL execution path exists. wit_get_query_results_by_id(id="SELECT ...") returns the
  literal `null`. wit_get_query(query="SELECT ...") also returns `null`. Neither raises an error —
  they return null, which is why this has silently produced "no known issues" on every run.
- wit_get_query_results_by_id requires a SAVED-QUERY GUID. With a real GUID it works and returns
  {queryType, columns, workItems:[{id,url}]}. With a folder GUID: "Querying folders is not
  supported." With a WIQL string: null.
- NO free-text work item search exists. `search_workitem` is NOT on this server (server response:
  "azure_devops_token" struct has no .search_workitem attribute).

THEREFORE you CANNOT search Azure DevOps for bugs matching an alert's error text, endpoint, or
keywords. Do not construct a WIQL string. Do not call wit_get_query with a SELECT.

## How you work (do this exactly)
1. Read {classification} and {service_context}. Look for an explicit ADO work item ID or a
   saved-query GUID.
2. If IDs or a GUID are present, call fetch_known_issues(queries=[...]) ONE time with the
   corresponding calls below. If NEITHER is present, call NO tools at all and go straight to the
   findings output with the capability-limit statement.
3. SUMMARIZE what came back.

## Downstream calls (azure_devops_token.*), each string must assign `result` and print it
- result = azure_devops_token.wit_get_work_items_batch_by_ids(project="<AcmeProjects|Infrastructure>", ids=[<id1>, <id2>])\\nprint(result)
- result = azure_devops_token.wit_get_query_results_by_id(project="<project>", id="<saved-query GUID>")\\nprint(result)

## ADO project mapping (for the ID-based lookups that do work)
- Application repos (WebCore, SubscriptionService, etc.): AcmeProjects
- SRE/platform work: Infrastructure

## Findings output
- If you had no IDs and no GUID, your ENTIRE finding is: "Known-issues search unavailable — this
  deployment has no ad-hoc WIQL or work item text-search capability, so ADO was NOT searched for
  matching bugs." Confidence UNKNOWN.
- NEVER write "no known issues found", "no matching bugs", or "this is not a known issue". A null
  from a WIQL call is a MISSING CAPABILITY, not a negative result — reporting it as an all-clear is
  a false conclusion and the single worst failure this agent can produce.
- If you did fetch work items, report up to 5 with ID, title, state, created date, priority. A
  recent (<7 days) high-priority bug matching the alert pattern -> flag as LIKELY the known root
  cause. A bug filed yesterday for a matching error/endpoint IS the answer.
- Label STRONG EVIDENCE / MODERATE EVIDENCE / WEAK SIGNAL / UNKNOWN. No fabrication. Under 500 words.
"""

known_issues_tierb = LlmAgent(
    model=create_fast_model(),
    name="known_issues",  # keep name so synthesis/report/telemetry are unchanged
    description="Searches Azure DevOps via a single concurrent batch fetch (Tier B).",
    instruction=INSTRUCTION,
    output_key="findings_known_issues",
    tools=[fetch_known_issues_tool],
    on_tool_error_callback=keep_running_on_tool_error,
)
