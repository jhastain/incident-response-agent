from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import (
    build_tool_list,
    create_bifrost_toolset,
    truncate_tool_response,
)
from ...tools.tool_resilience import keep_running_on_tool_error

EXTRA_CONTEXT = """
## CAPABILITY LIMIT — READ THIS FIRST. Keyword search of ADO is NOT AVAILABLE.
Verified live 2026-07-28 against the actual Azure DevOps tool surface. The complete set of ADO
tools reachable here is exactly these ten:
  core_get_identity_ids, repo_get_branch_by_name, repo_get_pull_request_by_id,
  repo_get_repo_by_name_or_id, wit_get_query, wit_get_query_results_by_id, wit_get_work_item,
  wit_get_work_item_type, wit_get_work_items_batch_by_ids, wit_get_work_items_for_iteration

What this means, verified by live probe:
- There is NO ad-hoc WIQL execution path. Passing a WIQL string to
  wit_get_query_results_by_id(id="SELECT ...") returns the literal `null`. Passing one to
  wit_get_query(query="SELECT ...") also returns `null`. Neither errors — they return null, which
  is exactly why this has silently produced "no known issues" on every run.
- wit_get_query_results_by_id requires a SAVED-QUERY GUID. With a real GUID it works and returns
  {queryType, columns, workItems:[{id,url}]}. With a folder GUID it returns "Querying folders is not
  supported." With a WIQL string it returns null.
- There is NO free-text work item search tool. `search_workitem` DOES NOT EXIST on this server
  (server response: "azure_devops_token" struct has no .search_workitem attribute).

THEREFORE: you CANNOT search Azure DevOps for bugs matching an alert's error text, endpoint, or
keywords. Do not attempt it. Do not construct a WIQL string.

## What you MUST report
State plainly: "Known-issues search unavailable — this deployment has no ad-hoc WIQL or work item
text-search capability, so ADO was NOT searched for matching bugs." Label it UNKNOWN.
NEVER write "no known issues found", "no matching bugs", "this is not a known issue", or anything
implying a search happened and came back clean. A null from a WIQL call is a MISSING CAPABILITY,
not a negative result. Reporting it as an all-clear is a false conclusion.

## What you CAN do
- If {classification} or {service_context} contains an explicit work item ID, fetch it with
  wit_get_work_items_batch_by_ids(project=<project>, ids=[<id>, ...]) — this works.
- If a saved-query GUID is supplied in context, run it via wit_get_query_results_by_id, then batch
  fetch the returned ids. Only do this when given an actual GUID.
- Otherwise make NO tool calls and report the capability limit above.

ADO Project Mapping (for the ID-based lookups that do work):
- Application repos (WebCore, SubscriptionService, etc.): AcmeProjects
- SRE/platform work: Infrastructure
"""

_bifrost = create_bifrost_toolset()

_bifrost_targets = ["wit_get_work_items_batch_by_ids", "wit_get_query_results_by_id"]

INSTRUCTION = build_agent_instruction(
    role="Known Issues Investigator",
    context_refs=["{classification}", "{service_context}"],
    checklist=[
        "1. Check {classification} and {service_context} for an explicit ADO work item ID or a "
        "saved-query GUID. Do NOT construct a WIQL query — there is no execution path for one here.",
        "2. If work item IDs are present, batch-fetch them with wit_get_work_items_batch_by_ids "
        "(one call) — report ID, title, state, created date, priority. If a saved-query GUID is "
        "present, run wit_get_query_results_by_id with that GUID, then batch-fetch the returned ids.",
        "3. If neither an ID nor a GUID is present, make NO tool calls and report: 'Known-issues "
        "search unavailable — no ad-hoc WIQL or work item text-search capability in this "
        "deployment; ADO was not searched.' Confidence UNKNOWN. Do NOT report this as 'no known "
        "issues found'.",
        "4. If a fetched bug is recent (<7 days), high priority, and matches the alert pattern, flag "
        "it as LIKELY the known root cause.",
    ],
    tools_available=[],
    extra_context=EXTRA_CONTEXT,
    bifrost_target_tools=_bifrost_targets if _bifrost else None,
)

known_issues_agent = LlmAgent(
    model=create_fast_model(),
    name="known_issues",
    description="Searches Azure DevOps for existing bugs and known issues matching the alert pattern.",
    instruction=INSTRUCTION,
    output_key="findings_known_issues",
    tools=build_tool_list(_bifrost),
    on_tool_error_callback=keep_running_on_tool_error,
    after_tool_callback=truncate_tool_response,
)
