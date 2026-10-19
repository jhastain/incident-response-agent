"""Sentry Tier B — one ADK LlmAgent + one async batch FunctionTool.

Same rationale and structure as datadog_tierb.py: a single ordinary LlmAgent
(so ADK drives the model and ContextVars stay correct) with ONE tool —
fetch_sentry(queries) — an async FunctionTool that runs every planned
sentry_token.* call CONCURRENTLY via httpx in a single batch. The agent:
plan the query list -> call fetch_sentry ONCE -> summarize into
findings_sentry. No discovery meta-tools, no per-query ReAct round-trips,
no nested agents, no inline model calls.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from ...config import create_fast_model
from ...tools.bifrost_direct import bifrost_execute_many_annotated
from ...tools.tool_resilience import keep_running_on_tool_error


async def fetch_sentry(queries: list[str]) -> str:
    """Execute Sentry queries concurrently via Bifrost and return combined results.

    Args:
        queries: A list of complete Python code strings, each a Sentry call
            against the `sentry_token` server, e.g.
            'result = sentry_token.search_issues(organizationSlug="acme", projectSlugOrId="webcore-dotnet", query="AppSqlException level:error", sort="freq", limit=5)\\nprint(result)'.
            Pass ALL queries in ONE call — they run in parallel.

    Returns:
        Combined raw results, one block per query.
    """
    if not queries:
        return "No queries provided."

    # The boolean-Sentry rewrite lives in bifrost_direct so it covers every Tier-B
    # agent, not just this one. It was scoped here first, and deploy_correlation
    # promptly sent an unguarded boolean Sentry query straight past it.
    results, codes, notes = await bifrost_execute_many_annotated([str(q) for q in queries])
    blocks = [
        f"### Query {i+1}\nCODE: {q[:200]}\nRESULT:\n{r}"
        for i, (q, r) in enumerate(zip(codes, results))
    ]
    if notes:
        blocks.insert(
            0,
            "### NOTE — queries were rewritten before execution\n" + "\n".join(notes),
        )
    return "\n\n".join(blocks)


fetch_sentry_tool = FunctionTool(func=fetch_sentry)

INSTRUCTION = """\
You are the Sentry Error Investigator. You have ONE tool: fetch_sentry(queries).

## Context
- Classification: {classification}
- Service context: {service_context}

## How you work (do this exactly)
1. PLAN the Sentry queries as a list of Python code strings against `sentry_token`.
2. Call fetch_sentry(queries=[...]) ONE time with ALL of them — they run concurrently.
3. SUMMARIZE the returned results into findings (do NOT call the tool again unless a
   result shows a clear, fixable query error).

## Downstream calls (sentry_token.*), each string must assign `result` and print it
All four forms below were verified live 2026-07-28. Use them EXACTLY as written.
- result = sentry_token.search_issues(organizationSlug="acme", projectSlugOrId="<project>", query="<sentry query>", sort="freq", limit=5)\\nprint(result)
- result = sentry_token.get_sentry_resource(organizationSlug="acme", resourceType="issue", resourceId="<SHORT-ID>")\\nprint(result)
- result = sentry_token.find_releases(organizationSlug="acme", projectSlug="<project>")\\nprint(result)
- result = sentry_token.find_projects(organizationSlug="acme")\\nprint(result)

Parameter traps — getting these wrong produces silently wrong data, not an error:
- get_sentry_resource REQUIRES resourceType, an enum: issue | event | trace | span | breadcrumbs |
  replay. resourceId is the short id from search_issues (e.g. "WEBCORE-DOTNET-3C4D"). There is NO
  issueId parameter — passing it returns an Input Error.
- find_releases takes projectSlug. Passing projectSlugOrId is SILENTLY IGNORED and returns
  org-wide releases from every project (verified differentially: the response header changes from
  "Releases in acme/webcore-dotnet" to "Releases in acme"). Never use projectSlugOrId
  on find_releases — the release correlation would be against the wrong project.

Example known-good call:
result = sentry_token.search_issues(organizationSlug="acme", projectSlugOrId="webcore-dotnet", query="AppSqlException level:error", sort="freq", limit=5)
print(result)

## Query rules
- Organization slug is always "acme". Filter to the Production environment only.
- Project slugs, verified live 2026-07-28 via find_projects. Pick the slug(s) matching the affected
  service in {classification}/{service_context}:
  - WebCore monolith (.NET) -> webcore-dotnet
  - WebCore frontend (React) -> webcore-react
  - WebCore mobile -> webcore-mobile
  - SubscriptionService -> subscription-service
  - API gateway / API v2 -> api-v2
  - Also available: accounting-service, auth0-logs, platform-ai, webportal-js, webportal-php, partner-rebates,
    global-product-catalog, media-service, notifications-service, open-platform,
    payment-processing-service, payment-processing-service-js, rebate-service-dotnet,
    rebate-service-react, reference-architecture, search-service, sms-bot, sms-bot-hj,
    underwriting-service, wallet-core
  These slugs DO NOT EXIST (verified HTTP 404 live): acme-net, webcore-frontend, fileservice,
  subscriptionservice, pricing. There is no FileService or Pricing project; FileService errors
  surface under webcore-dotnet.
  If the exact project slug is unclear, include a find_projects call to resolve it.
- A WRONG SLUG returns HTTP 404 "Project does not exist" — that is a BROKEN QUERY, not a finding.
  Never report a 404 as "no Sentry errors for this service". Re-resolve the slug via find_projects
  and say the original slug was invalid.
- 164K events/day across all projects — always pass limit=5 to search_issues, sort="freq".
- Derive the search_issues query string from the alert's error pattern/exception type in
  {classification} (e.g. "AppSqlException level:error", "TimeoutException level:error") —
  do NOT hardcode a query to one specific incident.
- NEVER put `OR` or `AND` in a search_issues query. Sentry rejects the entire query with
  `HTTP 400 — Boolean statements containing "OR" or "AND" are not supported in this search`,
  so you get ZERO issues back and it looks identical to "this service is clean". Parentheses
  do not help. Verified live 2026-08-07: 24 boolean queries, 44 rejections, no data.
  Sentry search is implicit-AND on space-separated terms, so `AppSqlException level:error`
  already means both terms. To cover N candidate terms, send N SEPARATE queries in the same
  fetch_sentry batch — they run concurrently, so it costs no extra wall-clock — and merge the
  results yourself.
    WRONG: query="cloud-run OR autoscaler OR probe level:error"     <- 400, zero data
    RIGHT: query="cloud-run level:error", query="autoscaler level:error",
           query="probe level:error"                                <- three entries, one batch
  This applies to Sentry only. Datadog queries (fetch_datadog) DO support OR/AND — do not
  carry this restriction across to them.
- Plan 3-5 queries total: search_issues for the affected project(s) (top issues by frequency),
  get_sentry_resource for at most the top 2-3 issue IDs returned (to pull stack traces/tags/
  affected users), and find_releases for the affected project to check release correlation.
  Do NOT over-query.

## Findings output
- Report ONLY what results show. Errors/empty -> "No data retrieved from Sentry for <query>".
- Distinguish these four cases and never collapse them into an all-clear:
  1. HTTP 404 "Project does not exist" -> the slug was WRONG. Report the broken query, not health.
  2. A real result listing zero issues -> a genuine negative finding. Quote it.
  3. A tool error/timeout -> report the error text verbatim. Not a finding about the service.
  4. HTTP 400 "Boolean statements ... are not supported" -> your query was MALFORMED and ran
     against nothing. Never report this as "no Sentry errors". Re-issue it as separate
     single-term queries and say the first attempt was rejected.
- Determine error rate trend: NEW (first seen in last 24h), SPIKING (10x increase), or
  PRE-EXISTING. Extract affected endpoints/user counts from tags and request.url. Check release
  correlation (issue.firstSeen vs release deployment time). Cross-reference error timestamps
  with the alert time. Label STRONG EVIDENCE / MODERATE EVIDENCE / WEAK SIGNAL / UNKNOWN. No
  fabrication. Under 500 words.
- SOURCE LINKS: for every issue you cite, include its `permalink` field from the result verbatim
  (e.g. https://acme.sentry.io/issues/<id>/) so the report can link to it. Use the
  permalink as-is; do not construct or guess it. If an issue has no permalink, cite it without a link.
"""

sentry_tierb = LlmAgent(
    model=create_fast_model(),
    name="sentry_error",  # keep name so synthesis/report/telemetry are unchanged
    description="Investigates Sentry via a single concurrent batch fetch (Tier B).",
    instruction=INSTRUCTION,
    output_key="findings_sentry",
    tools=[fetch_sentry_tool],
    on_tool_error_callback=keep_running_on_tool_error,
)
